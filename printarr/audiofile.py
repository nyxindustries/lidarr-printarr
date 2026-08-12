"""Scanning folders for audio files and reading their existing metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import mutagen

from printarr.log import get_logger

log = get_logger(__name__)

# Formats printarr can read and (except wav) tag. Keys are lowercase suffixes.
AUDIO_EXTENSIONS = {
    ".flac", ".mp3", ".m4a", ".m4b", ".mp4", ".ogg", ".oga", ".opus",
    ".ape", ".wv", ".wma", ".aiff", ".aif", ".wav",
}

# Files smaller than this are ignored (cover thumbnails, partial junk, samples)
MIN_FILE_SIZE_BYTES = 128 * 1024

_TRACKNO_PATTERNS = (
    re.compile(r"^\s*(?P<disc>\d{1,2})[-.](?P<track>\d{1,3})\b"),   # 1-02 / 1.02
    re.compile(r"^\s*(?P<track>\d{1,3})\b"),                        # 02 - Title
)


@dataclass
class AudioFile:
    path: Path
    duration: float = 0.0
    size: int = 0
    tag_title: str = ""
    tag_artist: str = ""
    tag_album: str = ""
    tag_albumartist: str = ""
    tag_track: int | None = None
    tag_disc: int | None = None
    filename_track: int | None = None
    filename_disc: int | None = None
    fingerprint: str | None = None
    # AcoustID lookup result: recording MBID -> best match score
    recordings: dict[str, float] = field(default_factory=dict)
    # Release MBIDs containing any matched recording (candidate votes)
    release_ids: set[str] = field(default_factory=set)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def track_hint(self) -> int | None:
        return self.tag_track if self.tag_track is not None else self.filename_track

    @property
    def disc_hint(self) -> int | None:
        return self.tag_disc if self.tag_disc is not None else self.filename_disc


def _first(tags, key: str) -> str:
    value = tags.get(key)
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value).strip()


def _parse_number(raw: str) -> int | None:
    # Tag values like "3", "3/12" or "03"
    match = re.match(r"\s*(\d+)", raw)
    return int(match.group(1)) if match else None


def parse_filename_numbers(name: str) -> tuple[int | None, int | None]:
    """Extract (disc, track) hints from the start of a file name."""
    for pattern in _TRACKNO_PATTERNS:
        match = pattern.match(name)
        if match:
            groups = match.groupdict()
            disc = int(groups["disc"]) if groups.get("disc") else None
            track = int(groups["track"])
            # A leading 4-digit blob is more likely a year than a track number
            if track > 999:
                continue
            return disc, track
    return None, None


def read_audio_file(path: Path) -> AudioFile | None:
    """Read duration and existing tags. Returns None for unreadable files."""
    try:
        parsed = mutagen.File(path, easy=True)
    except Exception as exc:
        log.warning("unreadable audio file %s: %s", path, exc)
        return None
    if parsed is None:
        return None

    info = getattr(parsed, "info", None)
    duration = float(getattr(info, "length", 0.0) or 0.0)
    tags = parsed.tags or {}

    disc_hint, track_hint = parse_filename_numbers(path.stem)
    audio = AudioFile(
        path=path,
        duration=duration,
        size=path.stat().st_size,
        tag_title=_first(tags, "title"),
        tag_artist=_first(tags, "artist"),
        tag_album=_first(tags, "album"),
        tag_albumartist=_first(tags, "albumartist"),
        tag_track=_parse_number(_first(tags, "tracknumber")),
        tag_disc=_parse_number(_first(tags, "discnumber")),
        filename_track=track_hint,
        filename_disc=disc_hint,
    )
    return audio


def scan_folder(folder: Path) -> list[AudioFile]:
    """Recursively collect audio files in a folder, in a stable natural order."""
    if folder.is_file():
        candidates = [folder]
    else:
        candidates = sorted(
            (p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS),
            key=_natural_key,
        )

    files: list[AudioFile] = []
    for path in candidates:
        try:
            if path.stat().st_size < MIN_FILE_SIZE_BYTES:
                log.debug("skipping tiny file %s", path)
                continue
        except OSError:
            continue
        audio = read_audio_file(path)
        if audio is not None:
            files.append(audio)
    return files


def _natural_key(path: Path):
    parts = re.split(r"(\d+)", str(path).lower())
    return [int(part) if part.isdigit() else part for part in parts]
