"""Regression tests for defects found in the adversarial code review."""

import time
from pathlib import Path

import mutagen
import pytest
from mutagen.asf import ASF
from mutagen.flac import FLAC
from mutagen.id3 import APIC, ID3
from mutagen.mp4 import MP4
from mutagen.oggflac import OggFLAC

from printarr.config import Config, ConfigError, TaggingConfig, load_config
from printarr.matching import assign_tracks, choose_release
from printarr.musicbrainz import _retry_after_seconds
from printarr.processor import REPORT_FILENAME
from printarr.queueworker import QueueOutcome, QueueWorker, StateStore
from printarr.renamer import RenamePlan, apply_renames
from printarr.tagger import build_track_tags, write_tags
from tests.test_matching import CONFIG, make_file, make_release, make_track
from tests.test_processor import (
    REL_ID,
    FakeAcoustid,
    FakeMB,
    build_album,
    make_processor,
)
from tests.test_processor import make_release as make_mb_release

# ------------------------------------------------------------------- matching


def test_ambiguity_not_masked_by_same_group_sibling():
    """A same-group runner-up must not hide a different-group candidate."""
    files = [
        make_file("01.flac", 180, recordings={"rec-1": 0.95}),
        make_file("02.flac", 210, recordings={"rec-2": 0.95}),
    ]
    tracks = [make_track("rec-1", 1, "One", 180),
              make_track("rec-2", 2, "Two", 210)]
    best = make_release("rel-a", list(tracks), group="rg-1")
    sibling = make_release("rel-b", list(tracks), group="rg-1")
    other_group = make_release("rel-c", list(tracks), group="rg-2")
    match, report = choose_release(files, [best, sibling, other_group], CONFIG)
    assert match is None
    assert "ambiguous" in report.reason


def test_tied_scores_do_not_strand_files():
    """Equal-score edges once stranded a file the augmenting pass now matches."""
    # Both files fingerprint-match both tracks equally; only a one-to-one
    # assignment covering both files is acceptable.
    files = [
        make_file("a.flac", 200, recordings={"rec-1": 0.9, "rec-2": 0.9}),
        make_file("b.flac", 200, recordings={"rec-1": 0.9}),
    ]
    release = make_release("rel-1", [
        make_track("rec-1", 1, "One", 200),
        make_track("rec-2", 2, "Two", 200),
    ])
    match = assign_tracks(files, release, CONFIG)
    assert len(match.pairs) == 2
    assert not match.unmatched_files
    # b.flac only matches rec-1, so a.flac must have yielded it
    by_file = {p.file.path.name: p.track.recording_id for p in match.pairs}
    assert by_file == {"a.flac": "rec-2", "b.flac": "rec-1"}


# -------------------------------------------------------------------- renaming


def test_swapped_filenames_rename_without_suffixes(tmp_path):
    a = tmp_path / "01 - B Song.flac"
    b = tmp_path / "02 - A Song.flac"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    plans = [RenamePlan(a, tmp_path / "02 - A Song.flac"),
             RenamePlan(b, tmp_path / "01 - B Song.flac")]
    apply_renames(plans)
    assert (tmp_path / "01 - B Song.flac").read_bytes() == b"b"
    assert (tmp_path / "02 - A Song.flac").read_bytes() == b"a"
    assert len(list(tmp_path.iterdir())) == 2  # no temp or " (1)" leftovers


def test_processor_swap_scenario(audio_factory, tmp_path):
    """Mislabeled files whose correct names are each other's current names."""
    album = build_album(audio_factory, tmp_path,
                        [("01 - Song Number 2.flac", 5.0),
                         ("02 - Song Number 1.flac", 7.0)])
    release = make_mb_release([5.0, 7.0])
    mb = FakeMB([release])
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path)

    result = processor.process_folder(album)
    assert result.success, result.reason
    names = sorted(p.name for p in album.iterdir())
    assert names == ["01 - Song Number 1.flac", "02 - Song Number 2.flac"]
    audio = FLAC(album / "01 - Song Number 1.flac")
    assert audio["MUSICBRAINZ_TRACKID"] == ["recording-1"]


def test_stale_report_removed_on_success(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path, [("a.flac", 5.0), ("b.flac", 7.0)])
    (album / REPORT_FILENAME).write_text("{}")
    release = make_mb_release([5.0, 7.0])
    mb = FakeMB([release])
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path)

    result = processor.process_folder(album)
    assert result.success, result.reason
    assert not (album / REPORT_FILENAME).exists()


# --------------------------------------------------------------------- config


def test_toml_type_mismatch_rejected(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text('[matching]\nmin_release_confidence = "high"\n')
    with pytest.raises(ConfigError, match="expected float"):
        load_config(path, environ={})


def test_toml_bool_for_int_rejected(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text("[queue]\npoll_interval = true\n")
    with pytest.raises(ConfigError, match="expected int"):
        load_config(path, environ={})


def test_toml_int_accepted_for_float_field(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text("[matching]\nduration_tolerance = 12\n")
    config = load_config(path, environ={})
    assert config.matching.duration_tolerance == 12.0
    assert isinstance(config.matching.duration_tolerance, float)


def test_env_coerced_to_declared_type(tmp_path):
    # TOML sets an int for the float field; the env override must still
    # coerce to the declared float type, not the current value's type
    path = tmp_path / "printarr.toml"
    path.write_text("[matching]\nduration_tolerance = 10\n")
    config = load_config(path, environ={
        "PRINTARR_MATCHING_DURATION_TOLERANCE": "12.5"})
    assert config.matching.duration_tolerance == 12.5


def test_invalid_numeric_env_is_config_error():
    with pytest.raises(ConfigError, match="PRINTARR_QUEUE_POLL_INTERVAL"):
        load_config(None, environ={"PRINTARR_QUEUE_POLL_INTERVAL": "soon"})


def test_dead_renaming_options_rejected(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text('[renaming]\nrename_folders = true\n')
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(path, environ={})


# --------------------------------------------------------------------- tagger

RELEASE = make_mb_release([6.0, 6.0])


def test_write_wav_gets_id3_chunk(audio_factory, tmp_path):
    path = audio_factory("song.wav", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]),
               TaggingConfig(write_cover_art=False))
    audio = mutagen.File(path)
    assert str(audio.tags["TIT2"]) == "Song Number 1"
    assert audio.tags["TXXX:MusicBrainz Album Id"].text == [REL_ID]


def test_write_aiff_gets_id3_chunk(audio_factory, tmp_path):
    path = audio_factory("song.aiff", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[1]),
               TaggingConfig(write_cover_art=False))
    audio = mutagen.File(path)
    assert str(audio.tags["TIT2"]) == "Song Number 2"
    ufid = audio.tags["UFID:http://musicbrainz.org"]
    assert ufid.data == b"recording-2"


def test_write_wma_standard_attributes(audio_factory, tmp_path):
    path = audio_factory("song.wma", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]),
               TaggingConfig(write_cover_art=False))
    audio = ASF(path)
    assert str(audio["Title"][0]) == "Song Number 1"
    assert str(audio["WM/AlbumTitle"][0]) == "Real Album"
    assert str(audio["MusicBrainz/Album Id"][0]) == REL_ID
    assert str(audio["MusicBrainz/Track Id"][0]) == "recording-1"


def test_write_oga_with_flac_stream(audio_factory, tmp_path):
    path = audio_factory("song.oga", dest_dir=tmp_path)
    assert isinstance(mutagen.File(path), OggFLAC)  # fixture really is Ogg FLAC
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]),
               TaggingConfig(write_cover_art=False))
    audio = mutagen.File(path)
    assert audio["TITLE"] == ["Song Number 1"]
    assert audio["MUSICBRAINZ_ALBUMID"] == [REL_ID]


def test_mp4_multivalue_artist_ids(audio_factory, tmp_path):
    release = make_mb_release([6.0])
    release.artist_ids = ["id-one", "id-two"]
    path = audio_factory("multi.m4a", dest_dir=tmp_path)
    write_tags(path, build_track_tags(release, release.tracks[0]),
               TaggingConfig(write_cover_art=False))
    audio = MP4(path)
    values = [bytes(v).decode() for v in
              audio["----:com.apple.iTunes:MusicBrainz Artist Id"]]
    assert values == ["id-one", "id-two"]


def test_clear_existing_preserves_cover_without_replacement(audio_factory, tmp_path):
    path = audio_factory("keepcover.mp3", dest_dir=tmp_path)
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"0" * 50
    id3 = ID3()
    id3.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="", data=fake_jpeg))
    id3.save(path)

    config = TaggingConfig(write_cover_art=True, clear_existing_tags=True)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), config,
               cover=None)
    id3 = ID3(path)
    covers = id3.getall("APIC")
    assert covers and covers[0].data == fake_jpeg
    # ...while the other old tags were cleared and rewritten
    assert str(id3["TIT2"]) == "Song Number 1"


def test_wav_hints_readable(audio_factory, tmp_path):
    from printarr.audiofile import read_audio_file
    path = audio_factory("hints.wav", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]),
               TaggingConfig(write_cover_art=False))
    audio = read_audio_file(path)
    assert audio.tag_title == "Song Number 1"
    assert audio.tag_album == "Real Album"
    assert audio.tag_track == 1


# --------------------------------------------------------------- musicbrainz


def test_retry_after_numeric():
    assert _retry_after_seconds("5", fallback=2.0) == 5.0


def test_retry_after_clamped():
    assert _retry_after_seconds("86400", fallback=2.0) == 60.0
    assert _retry_after_seconds("-5", fallback=2.0) == 0.0


def test_retry_after_http_date():
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime
    header = format_datetime(datetime.now(UTC) + timedelta(seconds=10))
    delay = _retry_after_seconds(header, fallback=2.0)
    assert 5.0 < delay <= 11.0


def test_retry_after_garbage_keeps_fallback():
    assert _retry_after_seconds("soon-ish", fallback=2.0) == 2.0


# --------------------------------------------------------------- queue worker


class StubLidarr:
    """Minimal duck-typed LidarrClient for worker-level tests."""

    def __init__(self, queue_after=None, command_status=None):
        self.queue_after = queue_after if queue_after is not None else []
        self.command_result = command_status or {"status": "completed"}
        self.commands = []

    @staticmethod
    def is_stuck(record):
        from printarr.lidarr import LidarrClient
        return LidarrClient.is_stuck(record)

    def queue(self):
        return self.queue_after

    def trigger_downloaded_albums_scan(self, path, download_client_id=None,
                                       import_mode="auto"):
        self.commands.append(("scan", path, download_client_id))
        return {"id": 1}

    def wait_for_command(self, command_id, timeout=300.0):
        return self.command_result


def make_worker(tmp_path, lidarr, **config_overrides) -> QueueWorker:
    config = Config()
    config.general.state_file = str(tmp_path / "state.json")
    for key, value in config_overrides.items():
        section, option = key.split("__")
        setattr(getattr(config, section), option, value)
    return QueueWorker(config, lidarr, processor=None)


def test_scan_unsuccessful_result_is_failure(tmp_path, monkeypatch):
    lidarr = StubLidarr(command_status={"status": "completed",
                                       "result": "unsuccessful"})
    worker = make_worker(tmp_path, lidarr)
    ok, reason = worker._trigger_scan({"outputPath": "/x", "downloadId": "d1"})
    assert not ok
    assert "imported nothing" in reason


def test_scan_verified_against_queue(tmp_path, monkeypatch):
    still_stuck = [{"downloadId": "d1", "status": "completed",
                    "trackedDownloadStatus": "warning",
                    "trackedDownloadState": "importBlocked"}]
    lidarr = StubLidarr(queue_after=still_stuck)
    worker = make_worker(tmp_path, lidarr)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ok, reason = worker._trigger_scan({"outputPath": "/x", "downloadId": "d1"})
    assert not ok
    assert "still stuck" in reason


def test_scan_success_when_item_left_queue(tmp_path, monkeypatch):
    lidarr = StubLidarr(queue_after=[])
    worker = make_worker(tmp_path, lidarr)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    ok, reason = worker._trigger_scan({"outputPath": "/x", "downloadId": "d1"})
    assert ok


def test_poisoned_record_does_not_abort_pass(tmp_path):
    stuck = [
        {"downloadId": "bad", "title": "Bad", "status": "completed",
         "trackedDownloadStatus": "warning",
         "trackedDownloadState": "importFailed"},
        {"downloadId": "good", "title": "Good", "status": "completed",
         "trackedDownloadStatus": "warning",
         "trackedDownloadState": "importFailed"},
    ]
    lidarr = StubLidarr(queue_after=stuck)
    worker = make_worker(tmp_path, lidarr)

    handled = []

    def explode_then_work(record):
        handled.append(record["downloadId"])
        if record["downloadId"] == "bad":
            raise RuntimeError("boom")
        return QueueOutcome("good", "Good", True, "ok")

    worker._handle_record = explode_then_work
    outcomes = worker.run_once()
    assert handled == ["bad", "good"]
    assert [o.success for o in outcomes] == [False, True]
    # The poisoned item got a state entry so the cooldown applies
    assert worker.state.data["queue"]["bad"]["outcome"] == "failed"


def test_dry_run_state_stays_in_memory(tmp_path):
    lidarr = StubLidarr()
    worker = make_worker(tmp_path, lidarr, general__dry_run=True)
    worker.state.record_download("d1", "dry-run")
    assert worker.state.should_skip_download("d1", cooldown=3600)
    assert not Path(tmp_path / "state.json").exists()


def test_state_save_is_atomic(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(str(path))
    store.record_download("a", "imported")
    # No temp file left behind, and the file parses
    assert not path.with_suffix(".json.tmp").exists()
    assert StateStore(str(path)).data["queue"]["a"]["outcome"] == "imported"


def test_state_non_dict_recovers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('["not", "a", "dict"]')
    store = StateStore(str(path))
    assert store.data == {"queue": {}, "folders": {}}
