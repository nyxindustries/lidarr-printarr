import json
import time
from pathlib import Path

from printarr.queueworker import PathMapper, StateStore


class TestPathMapper:
    def test_no_mappings_passthrough(self):
        mapper = PathMapper([])
        assert mapper.to_local("/downloads/x") == Path("/downloads/x")
        assert mapper.to_remote("/downloads/x") == "/downloads/x"

    def test_posix_mapping(self):
        mapper = PathMapper(["/data/downloads => /downloads"])
        assert mapper.to_local("/data/downloads/Album X") == Path("/downloads/Album X")
        assert mapper.to_remote("/downloads/Album X") == "/data/downloads/Album X"

    def test_windows_remote(self):
        mapper = PathMapper(["C:\\downloads => /downloads"])
        assert mapper.to_local("C:\\downloads\\Album") == Path("/downloads/Album")
        assert mapper.to_remote("/downloads/Album") == "C:\\downloads\\Album"

    def test_unmatched_prefix_left_alone(self):
        mapper = PathMapper(["/data/downloads => /downloads"])
        assert mapper.to_local("/other/place") == Path("/other/place")

    def test_invalid_mapping_rejected(self):
        try:
            PathMapper(["/a:/b"])
        except ValueError as exc:
            assert "expected" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestStateStore:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(str(path))
        store.record_download("abc", "imported")
        again = StateStore(str(path))
        assert again.should_skip_download("abc", cooldown=60)

    def test_imported_always_skipped(self, tmp_path):
        store = StateStore(str(tmp_path / "s.json"))
        store.record_download("abc", "imported")
        store.data["queue"]["abc"]["at"] = time.time() - 999999
        assert store.should_skip_download("abc", cooldown=60)

    def test_failed_retried_after_cooldown(self, tmp_path):
        store = StateStore(str(tmp_path / "s.json"))
        store.record_download("abc", "failed", "reason")
        assert store.should_skip_download("abc", cooldown=3600)
        store.data["queue"]["abc"]["at"] = time.time() - 7200
        assert not store.should_skip_download("abc", cooldown=3600)

    def test_unknown_not_skipped(self, tmp_path):
        store = StateStore(str(tmp_path / "s.json"))
        assert not store.should_skip_download("nope", cooldown=60)

    def test_corrupt_state_file_recovers(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{corrupt")
        store = StateStore(str(path))
        assert store.data == {"queue": {}, "folders": {}}

    def test_disabled_state(self):
        store = StateStore("")
        store.record_download("abc", "imported")  # no crash, nothing persisted
        assert store.path is None

    def test_folder_state(self, tmp_path):
        store = StateStore(str(tmp_path / "s.json"))
        store.record_folder("/music/x", "processed")
        assert store.should_skip_folder("/music/x", cooldown=1)
        stored = json.loads((tmp_path / "s.json").read_text())
        assert "/music/x" in stored["folders"]
