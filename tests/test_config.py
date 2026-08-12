import pytest

from printarr.config import Config, ConfigError, load_config


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={})
    assert config.lidarr.url == ""
    assert config.queue.import_mode == "auto"
    assert config.matching.min_release_confidence == 0.6


def test_load_toml(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text(
        """
[lidarr]
url = "http://lidarr:8686"
api_key = "secret"

[matching]
min_release_confidence = 0.75

[queue]
path_mappings = ["/data/downloads => /downloads"]
"""
    )
    config = load_config(path, environ={})
    assert config.lidarr.url == "http://lidarr:8686"
    assert config.matching.min_release_confidence == 0.75
    assert config.queue.path_mappings == ["/data/downloads => /downloads"]


def test_env_overrides_file(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text('[lidarr]\nurl = "http://from-file"\n')
    config = load_config(path, environ={
        "PRINTARR_LIDARR_URL": "http://from-env",
        "PRINTARR_GENERAL_DRY_RUN": "true",
        "PRINTARR_QUEUE_POLL_INTERVAL": "120",
        "PRINTARR_WATCH_FOLDERS": "/a, /b",
    })
    assert config.lidarr.url == "http://from-env"
    assert config.general.dry_run is True
    assert config.queue.poll_interval == 120
    assert config.watch.folders == ["/a", "/b"]


def test_unknown_option_rejected(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text("[lidarr]\nurll = \"typo\"\n")
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(path, environ={})


def test_unknown_section_rejected(tmp_path):
    path = tmp_path / "printarr.toml"
    path.write_text("[lidar]\nurl = \"x\"\n")
    with pytest.raises(ConfigError, match="unknown section"):
        load_config(path, environ={})


def test_missing_explicit_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", environ={})


def test_bad_bool_env():
    with pytest.raises(ConfigError, match="boolean"):
        load_config(None, environ={"PRINTARR_GENERAL_DRY_RUN": "maybe"})


def test_config_is_dataclass_complete():
    # every section reachable & env naming stable
    config = Config()
    for section in ("lidarr", "acoustid", "musicbrainz", "matching",
                    "tagging", "renaming", "queue", "watch", "general"):
        assert hasattr(config, section)
