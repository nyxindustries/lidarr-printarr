"""Tests for the review web UI: MB reference parsing, forced assignment,
worker review methods, and an HTTP round trip against a live server."""

import json
import time
import urllib.request

import pytest
from mutagen.flac import FLAC

from printarr.config import Config
from printarr.processor import REPORT_FILENAME
from printarr.queueworker import QueueWorker
from printarr.webui import WebUI, parse_mb_reference
from tests.test_processor import (
    REL_ID,
    RG_ID,
    FakeAcoustid,
    FakeMB,
    build_album,
    make_processor,
    make_release,
)

UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class TestParseMBReference:
    def test_release_url(self):
        mbid, kind = parse_mb_reference(
            f"https://musicbrainz.org/release/{UUID}")
        assert (mbid, kind) == (UUID, "release")

    def test_release_group_url(self):
        mbid, kind = parse_mb_reference(
            f"https://musicbrainz.org/release-group/{UUID}")
        assert (mbid, kind) == (UUID, "release-group")

    def test_bare_mbid(self):
        mbid, kind = parse_mb_reference(f"  {UUID.upper()}  ")
        assert (mbid, kind) == (UUID, "unknown")

    def test_garbage(self):
        assert parse_mb_reference("not an mbid") == (None, "")
        assert parse_mb_reference("") == (None, "")


class TestForcedProcessing:
    def test_forced_release_overrides_low_confidence(self, audio_factory, tmp_path):
        # Files whose durations do NOT fit the release well: automatic
        # matching refuses, but a human override must be accepted.
        album = build_album(audio_factory, tmp_path,
                            [("x1.flac", 5.0), ("x2.flac", 7.0)])
        release = make_release([5.0, 7.0])
        mb = FakeMB([release])
        processor = make_processor(mb, None, tmp_path)  # no fingerprints at all

        auto = processor.process_folder(album)
        assert not auto.success  # sanity: automatic matching says no

        forced = processor.process_folder(album, forced_release_mbid=REL_ID)
        assert forced.success, forced.reason
        assert forced.reason == "forced"
        audio = FLAC(album / "01 - Song Number 1.flac")
        assert audio["MUSICBRAINZ_ALBUMID"] == [REL_ID]

    def test_forced_release_group_picks_best_release(self, audio_factory, tmp_path):
        album = build_album(audio_factory, tmp_path,
                            [("y1.flac", 5.0), ("y2.flac", 7.0)])
        good = make_release([5.0, 7.0])
        bad = make_release([100.0, 200.0, 300.0],
                           release_id="bbbbbbbb-1111-1111-1111-111111111111",
                           group=RG_ID, title="Wrong Pressing")
        mb = FakeMB([good, bad])
        acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
        processor = make_processor(mb, acoustid, tmp_path)

        result = processor.process_folder(album, forced_release_group_mbid=RG_ID)
        assert result.success, result.reason
        assert result.release.id == REL_ID

    def test_forced_unknown_release_fails_cleanly(self, audio_factory, tmp_path):
        album = build_album(audio_factory, tmp_path, [("z1.flac", 5.0)])
        processor = make_processor(FakeMB([]), None, tmp_path)
        result = processor.process_folder(
            album, forced_release_mbid="00000000-0000-0000-0000-000000000000")
        assert not result.success
        assert "lookup failed" in result.reason


class RecordingLidarr:
    """Stub Lidarr: serves a fixed queue, then an empty one after a scan."""

    def __init__(self, records):
        self.records = records
        self.scans = []

    @staticmethod
    def is_stuck(record):
        from printarr.lidarr import LidarrClient
        return LidarrClient.is_stuck(record)

    def queue(self):
        return [] if self.scans else self.records

    def trigger_downloaded_albums_scan(self, path, download_client_id=None,
                                       import_mode="auto"):
        self.scans.append((path, download_client_id))
        return {"id": 1}

    def wait_for_command(self, command_id, timeout=300.0):
        return {"status": "completed", "result": "successful"}


def make_worker_env(audio_factory, tmp_path, monkeypatch):
    album = build_album(audio_factory, tmp_path,
                        [("t1.flac", 5.0), ("t2.flac", 7.0)])
    release = make_release([5.0, 7.0])
    mb = FakeMB([release])
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path)
    record = {
        "downloadId": "DL1", "title": "Some Release",
        "status": "completed", "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importBlocked",
        "outputPath": str(album), "statusMessages": [],
    }
    lidarr = RecordingLidarr([record])
    worker = QueueWorker(processor.config, lidarr, processor)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    return album, worker, lidarr


class TestWorkerReviewMethods:
    def test_dry_run_refusal_still_lists_candidates(self, audio_factory, tmp_path,
                                                    monkeypatch):
        """Dry-run writes no report files, so the UI candidates must come
        from the in-memory state (regression: UI showed only the URL box)."""
        album = build_album(audio_factory, tmp_path,
                            [("d1.flac", 5.0), ("d2.flac", 7.0)])
        # Only a badly fitting release exists -> identification refuses
        decoy = make_release([100.0, 200.0, 300.0],
                             release_id="dddddddd-0000-0000-0000-000000000000",
                             group="eeeeeeee-0000-0000-0000-000000000000",
                             title="Wrong Album")
        mb = FakeMB([decoy])
        # Fingerprints resolve to recordings the decoy does not contain, so
        # the release is considered (via votes) but scores far too low
        acoustid = FakeAcoustid({5: "unrelated-rec-1", 7: "unrelated-rec-2"},
                                [decoy.id])
        processor = make_processor(mb, acoustid, tmp_path)
        processor.config.general.dry_run = True
        record = {
            "downloadId": "DRY1", "title": "Some Dry Release",
            "status": "completed", "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importBlocked",
            "outputPath": str(album), "statusMessages": [],
        }
        lidarr = RecordingLidarr([record])
        worker = QueueWorker(processor.config, lidarr, processor)
        monkeypatch.setattr(time, "sleep", lambda s: None)

        worker.run_once()
        assert not (album / REPORT_FILENAME).exists()  # dry run: no files
        items = worker.list_review_items()
        entry = items["queue"][0]
        assert entry["candidates"], "candidates must be served from state"
        assert entry["candidates"][0]["title"] == "Wrong Album"
        assert "score" in entry["candidates"][0]

    def test_list_review_items(self, audio_factory, tmp_path, monkeypatch):
        album, worker, lidarr = make_worker_env(audio_factory, tmp_path, monkeypatch)
        (album / REPORT_FILENAME).write_text(json.dumps({
            "candidates": [{"release_id": REL_ID, "title": "Real Album",
                            "artist": "Real Artist", "score": 0.55}]}))
        items = worker.list_review_items()
        assert items["error"] == ""
        assert len(items["queue"]) == 1
        entry = items["queue"][0]
        assert entry["download_id"] == "DL1"
        assert entry["exists"] is True
        assert entry["candidates"][0]["release_id"] == REL_ID

    def test_assign_queue_item(self, audio_factory, tmp_path, monkeypatch):
        album, worker, lidarr = make_worker_env(audio_factory, tmp_path, monkeypatch)
        ok, reason = worker.assign_queue_item("DL1", release_mbid=REL_ID)
        assert ok, reason
        assert lidarr.scans == [(str(album), "DL1")]
        audio = FLAC(album / "01 - Song Number 1.flac")
        assert audio["MUSICBRAINZ_ALBUMID"] == [REL_ID]
        assert worker.state.data["queue"]["DL1"]["outcome"] == "imported"

    def test_assign_missing_queue_item(self, audio_factory, tmp_path, monkeypatch):
        _, worker, _ = make_worker_env(audio_factory, tmp_path, monkeypatch)
        ok, reason = worker.assign_queue_item("NOPE", release_mbid=REL_ID)
        assert not ok
        assert "no longer exists" in reason

    def test_assign_folder(self, audio_factory, tmp_path, monkeypatch):
        album = build_album(audio_factory, tmp_path, [("f1.flac", 5.0),
                                                      ("f2.flac", 7.0)])
        release = make_release([5.0, 7.0])
        processor = make_processor(FakeMB([release]), FakeAcoustid(
            {5: "recording-1", 7: "recording-2"}, [REL_ID]), tmp_path)
        worker = QueueWorker(processor.config, RecordingLidarr([]), processor)
        ok, reason = worker.assign_folder(str(album), release_mbid=REL_ID)
        assert ok, reason
        key = str(album.resolve())
        assert worker.state.data["folders"][key]["outcome"] == "processed"


@pytest.fixture
def web_server(audio_factory, tmp_path, monkeypatch):
    album, worker, lidarr = make_worker_env(audio_factory, tmp_path, monkeypatch)
    config = worker.config
    config.web.host = "127.0.0.1"
    config.web.port = 0  # ephemeral
    ui = WebUI(config, worker)
    ui.start_background()
    port = ui.server.server_address[1]
    yield f"http://127.0.0.1:{port}", album, lidarr
    ui.stop()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as res:
        return res.status, json.loads(res.read()) if "api" in url else res.read()


def _post(url, payload):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


class TestHTTP:
    def test_page_and_health(self, web_server):
        base, _, _ = web_server
        status, body = _get(base + "/")
        assert status == 200 and b"printarr" in body
        status, health = _get(base + "/api/health")
        assert status == 200 and health["status"] == "ok"

    def test_items_endpoint(self, web_server):
        base, _, _ = web_server
        status, items = _get(base + "/api/items")
        assert status == 200
        assert [i["download_id"] for i in items["queue"]] == ["DL1"]

    def test_assign_via_http(self, web_server):
        base, album, lidarr = web_server
        status, result = _post(base + "/api/assign", {
            "kind": "queue", "id": "DL1",
            "release": f"https://musicbrainz.org/release/{REL_ID}"})
        assert status == 200, result
        assert result["success"] is True
        assert lidarr.scans  # import was triggered

    def test_assign_rejects_garbage(self, web_server):
        base, _, _ = web_server
        status, result = _post(base + "/api/assign", {
            "kind": "queue", "id": "DL1", "release": "definitely not an mbid"})
        assert status == 422
        assert "no MusicBrainz ID" in result["reason"]

    def test_unknown_route(self, web_server):
        base, _, _ = web_server
        status, result = _post(base + "/api/nope", {})
        assert status == 404


def test_config_has_web_section():
    config = Config()
    assert config.web.enabled is False
    assert config.web.port == 8687
