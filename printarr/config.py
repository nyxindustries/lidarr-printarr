"""Configuration loading: TOML file with PRINTARR_* environment overrides.

Precedence (highest wins): environment variables > TOML file > defaults.
Environment variables follow the pattern PRINTARR_<SECTION>_<KEY>, e.g.
PRINTARR_LIDARR_API_KEY or PRINTARR_MATCHING_MIN_RELEASE_CONFIDENCE.
"""

from __future__ import annotations

import os
import tomllib
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

DEFAULT_CONFIG_LOCATIONS = (
    "./printarr.toml",
    "~/.config/printarr/printarr.toml",
    "/config/printarr.toml",
)


class ConfigError(Exception):
    pass


@dataclass
class LidarrConfig:
    url: str = ""
    api_key: str = ""
    verify_ssl: bool = True


@dataclass
class AcoustidConfig:
    # Free application key from https://acoustid.org/new-application
    api_key: str = ""


@dataclass
class MusicbrainzConfig:
    # Contact info included in the MusicBrainz User-Agent, as their API terms ask for
    contact: str = ""


@dataclass
class MatchingConfig:
    min_acoustid_score: float = 0.5
    min_release_confidence: float = 0.6
    duration_tolerance: float = 10.0
    # How many releases of a release group to fully inspect (each costs one MB request)
    max_release_candidates: int = 8
    require_all_files_matched: bool = True


@dataclass
class TaggingConfig:
    enabled: bool = True
    write_cover_art: bool = True
    clear_existing_tags: bool = False


@dataclass
class RenamingConfig:
    enabled: bool = True
    file_template: str = "{track:02d} - {title}"
    # Prefix files with the disc number when a release has multiple discs
    multi_disc_template: str = "{disc}-{track:02d} - {title}"


@dataclass
class QueueConfig:
    poll_interval: int = 60
    # 'auto' lets Lidarr copy while a torrent is still seeding, else move
    import_mode: str = "auto"  # 'auto' | 'move' | 'copy'
    # Retry a previously failed/refused item after this many seconds
    retry_cooldown: int = 6 * 3600
    # Path prefix translations between Lidarr's view and printarr's view,
    # e.g. "/data/downloads => /downloads" when running in separate containers
    path_mappings: list[str] = field(default_factory=list)


@dataclass
class WatchConfig:
    folders: list[str] = field(default_factory=list)
    # Seconds a folder must be unmodified before it is picked up
    quiet_seconds: int = 120
    # Trigger a Lidarr DownloadedAlbumsScan after successfully fixing a folder
    trigger_lidarr_scan: bool = False


@dataclass
class GeneralConfig:
    dry_run: bool = False
    log_level: str = "INFO"
    state_file: str = ".printarr-state.json"


@dataclass
class Config:
    lidarr: LidarrConfig = field(default_factory=LidarrConfig)
    acoustid: AcoustidConfig = field(default_factory=AcoustidConfig)
    musicbrainz: MusicbrainzConfig = field(default_factory=MusicbrainzConfig)
    matching: MatchingConfig = field(default_factory=MatchingConfig)
    tagging: TaggingConfig = field(default_factory=TaggingConfig)
    renaming: RenamingConfig = field(default_factory=RenamingConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)


def _coerce(value: str, target_type):
    if target_type is bool:
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ConfigError(f"cannot interpret {value!r} as boolean")
    try:
        if target_type is int:
            return int(value)
        if target_type is float:
            return float(value)
    except ValueError:
        raise ConfigError(
            f"cannot interpret {value!r} as {target_type.__name__}") from None
    if target_type is list or target_type == list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _check_type(value, target_type) -> bool:
    """Validate a TOML value against a declared field type."""
    if target_type is bool:
        return isinstance(value, bool)
    if target_type is int:
        # bool subclasses int; poll_interval = true must be rejected
        return isinstance(value, int) and not isinstance(value, bool)
    if target_type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if target_type is str:
        return isinstance(value, str)
    if target_type is list or target_type == list[str]:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    return True


def _type_name(target_type) -> str:
    return getattr(target_type, "__name__", str(target_type))


def _apply_dict(section_obj, data: dict, context: str) -> None:
    valid = {f.name for f in fields(section_obj)}
    hints = typing.get_type_hints(type(section_obj))
    for key, value in data.items():
        normalized = key.replace("-", "_")
        if normalized not in valid:
            raise ConfigError(f"unknown option '{key}' in [{context}]")
        declared = hints[normalized]
        if not _check_type(value, declared):
            raise ConfigError(
                f"[{context}].{key}: expected {_type_name(declared)}, "
                f"got {type(value).__name__} ({value!r})")
        if declared is float and isinstance(value, int):
            value = float(value)
        setattr(section_obj, normalized, value)


def _apply_env(config: Config, environ: dict[str, str]) -> None:
    sections = {f.name: getattr(config, f.name) for f in fields(config)}
    for section_name, section_obj in sections.items():
        hints = typing.get_type_hints(type(section_obj))
        for f in fields(section_obj):
            env_key = f"PRINTARR_{section_name.upper()}_{f.name.upper()}"
            if env_key in environ:
                try:
                    coerced = _coerce(environ[env_key], hints[f.name])
                except ConfigError as exc:
                    raise ConfigError(f"invalid value for {env_key}: {exc}") from None
                setattr(section_obj, f.name, coerced)


def load_config(path: str | Path | None = None, environ: dict[str, str] | None = None) -> Config:
    """Load configuration from an explicit path, or the first default location found."""
    environ = os.environ if environ is None else environ
    config = Config()

    config_path: Path | None = None
    if path is not None:
        config_path = Path(path).expanduser()
        if not config_path.is_file():
            raise ConfigError(f"config file not found: {config_path}")
    else:
        for candidate in DEFAULT_CONFIG_LOCATIONS:
            expanded = Path(candidate).expanduser()
            if expanded.is_file():
                config_path = expanded
                break

    if config_path is not None:
        with open(config_path, "rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
        for section_name, section_data in data.items():
            normalized = section_name.replace("-", "_")
            if not hasattr(config, normalized) or not is_dataclass(getattr(config, normalized)):
                raise ConfigError(f"unknown section [{section_name}] in {config_path}")
            if not isinstance(section_data, dict):
                raise ConfigError(f"[{section_name}] must be a table in {config_path}")
            _apply_dict(getattr(config, normalized), section_data, section_name)

    _apply_env(config, dict(environ))
    return config


def validate_for_lidarr(config: Config) -> None:
    if not config.lidarr.url:
        raise ConfigError("lidarr.url is required (or set PRINTARR_LIDARR_URL)")
    if not config.lidarr.api_key:
        raise ConfigError("lidarr.api_key is required (or set PRINTARR_LIDARR_API_KEY)")


def validate_for_identification(config: Config) -> None:
    if not config.acoustid.api_key:
        raise ConfigError(
            "acoustid.api_key is required — register a free application key at "
            "https://acoustid.org/new-application (or set PRINTARR_ACOUSTID_API_KEY)"
        )
