import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg")

# Pink noise compresses poorly, keeping generated FLACs above the
# MIN_FILE_SIZE_BYTES junk filter that scan_folder applies.
def _generate(target: Path, seconds: float, codec_args: list[str]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:r=44100:a=0.4",
         "-ac", "2", *codec_args, str(target)],
        check=True, capture_output=True,
    )
    return target


@pytest.fixture(scope="session")
def audio_factory(tmp_path_factory):
    """Returns make(filename, seconds) -> Path, creating real audio files."""
    if FFMPEG is None:
        pytest.skip("ffmpeg not available")
    base = tmp_path_factory.mktemp("audio-src")
    cache: dict[tuple[str, float], Path] = {}

    codec_map = {
        ".flac": [],
        ".mp3": ["-b:a", "128k"],
        ".m4a": ["-c:a", "aac", "-b:a", "128k"],
        ".ogg": ["-c:a", "libvorbis", "-q:a", "3"],
        ".opus": ["-c:a", "libopus", "-b:a", "96k"],
        ".oga": ["-c:a", "flac", "-f", "ogg"],  # Ogg FLAC, not Vorbis
        ".wav": ["-c:a", "pcm_s16le"],
        ".aiff": ["-c:a", "pcm_s16be"],
        ".wma": ["-c:a", "wmav2", "-b:a", "128k"],
    }

    def make(filename: str, seconds: float = 6.0, dest_dir: Path | None = None) -> Path:
        suffix = Path(filename).suffix.lower()
        if suffix not in codec_map:
            raise ValueError(f"unsupported test format {suffix}")
        key = (suffix, seconds)
        if key not in cache:
            cache[key] = _generate(base / f"master-{seconds}{suffix}", seconds,
                                   codec_map[suffix])
        target = (dest_dir or base) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if target != cache[key]:
            shutil.copyfile(cache[key], target)
        return target

    return make
