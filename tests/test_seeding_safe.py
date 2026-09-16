"""Torrent downloads keep seeding from their folder: printarr must never touch
those files and instead work on a disposable copy."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from printarr.config import Config
from printarr.processor import ProcessResult
from printarr.queueworker import STAGING_DIRNAME, QueueWorker


class FakeLidarr:
    """Just enough of LidarrClient for the queue flow."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.scans: list[dict] = []
        self.manual_imports: list[dict] = []
        self.manual_import_lookups: list[dict] = []

    @staticmethod
    def is_stuck(record: dict) -> bool:
        return record.get("trackedDownloadState") == "importFailed"

    def queue(self) -> list[dict]:
        return list(self.records)

    def manual_import(self, download_id=None, folder=None, filter_existing=False):
        self.manual_import_lookups.append({"download_id": download_id, "folder": folder})
        if folder is None:
            record = next(r for r in self.records if r["downloadId"] == download_id)
            folder = record["outputPath"]
        return [{"path": str(p), "quality": {"quality": {"id": 6}}}
                for p in sorted(Path(folder).rglob("*.flac"))]

    def trigger_manual_import(self, files, import_mode="auto", replace_existing=False):
        self.manual_imports.append({"files": files, "import_mode": import_mode})
        self.records = []  # imported: the queue item is gone
        return {"id": 1}

    def trigger_downloaded_albums_scan(self, path, download_client_id=None,
                                       import_mode="auto"):
        self.scans.append({"path": path, "download_client_id": download_client_id,
                           "import_mode": import_mode})
        self.records = []
        return {"id": 2}

    def wait_for_command(self, command_id, timeout=300.0):
        return {"status": "completed", "result": "successful"}


class FakeProcessor:
    """Simulates tagging by appending bytes to every audio file it is handed."""

    def __init__(self, candidate=None):
        self.candidate = candidate
        self.folders: list[Path] = []

    def process_folder(self, folder, **kwargs):
        folder = Path(folder)
        self.folders.append(folder)
        pairs = []
        for path in sorted(folder.rglob("*.flac")):
            with open(path, "ab") as fh:
                fh.write(b"+TAGS")
            pairs.append(SimpleNamespace(
                file=SimpleNamespace(path=path.resolve()),
                track=SimpleNamespace(id="t1", title="Song"),
                orig_track_id=None))
        return ProcessResult(folder, True, match=SimpleNamespace(pairs=pairs),
                             lidarr_candidate=self.candidate)


def _download(tmp_path: Path, name: str = "Album") -> Path:
    folder = tmp_path / "downloads" / name
    folder.mkdir(parents=True)
    (folder / "01 - a.flac").write_bytes(b"ORIGINAL-A")
    (folder / "02 - b.flac").write_bytes(b"ORIGINAL-B")
    (folder / "cover.jpg").write_bytes(b"JPG")
    return folder


def _record(folder: Path, protocol: str = "torrent", download_id: str = "HASH1") -> dict:
    return {"downloadId": download_id, "title": folder.name, "protocol": protocol,
            "trackedDownloadState": "importFailed", "outputPath": str(folder)}


def _worker(tmp_path: Path, records: list[dict], processor=None, dry_run: bool = False,
            **queue_overrides):
    config = Config()
    config.general.state_file = str(tmp_path / "state.json")
    config.general.dry_run = dry_run
    for key, value in queue_overrides.items():
        setattr(config.queue, key, value)
    lidarr = FakeLidarr(records)
    processor = processor or FakeProcessor()
    return QueueWorker(config, lidarr, processor), lidarr, processor


@pytest.fixture
def no_settle(monkeypatch):
    # _import_verified waits for Lidarr to refresh its queue; pointless with fakes
    monkeypatch.setattr("printarr.queueworker.time.sleep", lambda _seconds: None)


class TestSeedingSafe:
    def test_torrent_is_processed_on_a_copy(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)])

        outcomes = worker.run_once()

        assert outcomes[0].success, outcomes[0].reason
        staged = tmp_path / "downloads" / STAGING_DIRNAME / folder.name
        assert processor.folders == [staged]
        # the seeding originals are byte-identical
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A"
        assert (folder / "02 - b.flac").read_bytes() == b"ORIGINAL-B"
        # Lidarr imported the copy, moving it out
        assert lidarr.scans == [{"path": str(staged), "download_client_id": "HASH1",
                                 "import_mode": "move"}]
        # nothing left behind, state points at the real download
        assert not staged.exists()
        assert worker.state.data["queue"]["HASH1"]["outcome"] == "imported"
        assert worker.state.data["queue"]["HASH1"]["path"] == str(folder)

    def test_manual_import_targets_the_copy(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        candidate = SimpleNamespace(db_track_ids={"t1": 11}, db_artist_id=1,
                                    db_album_id=2, db_release_id=3)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)],
                                            processor=FakeProcessor(candidate))

        outcomes = worker.run_once()

        assert outcomes[0].success, outcomes[0].reason
        staged = tmp_path / "downloads" / STAGING_DIRNAME / folder.name
        assert lidarr.manual_import_lookups == [{"download_id": None, "folder": str(staged)}]
        assert lidarr.scans == []
        [imported] = lidarr.manual_imports
        assert imported["import_mode"] == "move"
        assert len(imported["files"]) == 2
        for entry in imported["files"]:
            assert Path(entry["path"]).parent == staged
            assert entry["downloadId"] == "HASH1"
            assert entry["trackIds"] == [11]
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A"
        assert not staged.exists()

    def test_usenet_is_processed_in_place(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder, protocol="usenet")])

        worker.run_once()

        assert processor.folders == [folder]
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A+TAGS"
        assert not (tmp_path / "downloads" / STAGING_DIRNAME).exists()
        assert lidarr.scans[0]["path"] == str(folder)
        assert lidarr.scans[0]["import_mode"] == "auto"

    def test_protection_can_be_switched_off(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)],
                                            protect_torrents=False)

        worker.run_once()

        assert processor.folders == [folder]
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A+TAGS"
        assert not (tmp_path / "downloads" / STAGING_DIRNAME).exists()

    def test_dry_run_never_copies(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)], dry_run=True)

        worker.run_once()

        assert processor.folders == [folder]
        assert not (tmp_path / "downloads" / STAGING_DIRNAME).exists()
        assert lidarr.scans == [] and lidarr.manual_imports == []

    def test_custom_staging_dir(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)],
                                            staging_dir=str(tmp_path / "stage"))

        worker.run_once()

        assert processor.folders == [tmp_path / "stage" / folder.name]
        assert not (tmp_path / "downloads" / STAGING_DIRNAME).exists()
        assert not (tmp_path / "stage" / folder.name).exists()

    def test_staging_dir_inside_download_is_refused(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)],
                                            staging_dir=str(folder / "stage"))

        outcomes = worker.run_once()

        assert not outcomes[0].success
        assert "staging" in outcomes[0].reason
        assert processor.folders == []
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A"

    def test_stale_copy_is_replaced(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        stale = tmp_path / "downloads" / STAGING_DIRNAME / folder.name
        stale.mkdir(parents=True)
        (stale / "leftover.flac").write_bytes(b"OLD")
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)])

        worker.run_once()

        assert lidarr.scans[0]["path"] == str(stale)
        assert not stale.exists()

    def test_review_assignment_uses_the_copy(self, tmp_path, no_settle):
        folder = _download(tmp_path)
        worker, lidarr, processor = _worker(tmp_path, [_record(folder)])

        ok, reason = worker.assign_queue_item("HASH1", release_mbid="some-mbid")

        assert ok, reason
        staged = tmp_path / "downloads" / STAGING_DIRNAME / folder.name
        assert processor.folders == [staged]
        assert (folder / "01 - a.flac").read_bytes() == b"ORIGINAL-A"
        assert lidarr.scans[0] == {"path": str(staged), "download_client_id": "HASH1",
                                   "import_mode": "move"}
        assert not staged.exists()
