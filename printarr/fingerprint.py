"""Chromaprint fingerprinting (fpcalc) and AcoustID lookups."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import requests

from printarr import USER_AGENT
from printarr.audiofile import AudioFile
from printarr.log import get_logger

log = get_logger(__name__)

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"
# AcoustID asks clients to stay under 3 requests/second
ACOUSTID_MIN_INTERVAL = 1.0 / 3.0
FPCALC_TIMEOUT = 120


class FingerprintError(Exception):
    pass


def fpcalc_available() -> bool:
    return shutil.which("fpcalc") is not None


def compute_fingerprint(path: Path) -> tuple[float, str] | None:
    """Run fpcalc on a file; returns (duration, fingerprint) or None on failure."""
    try:
        completed = subprocess.run(
            ["fpcalc", "-json", str(path)],
            capture_output=True, text=True, timeout=FPCALC_TIMEOUT, check=False,
        )
    except FileNotFoundError:
        raise FingerprintError(
            "fpcalc not found — install Chromaprint (Debian/Ubuntu: libchromaprint-tools)"
        ) from None
    except subprocess.TimeoutExpired:
        log.warning("fpcalc timed out on %s", path)
        return None
    if completed.returncode != 0:
        log.warning("fpcalc failed on %s: %s", path, completed.stderr.strip()[:200])
        return None
    try:
        data = json.loads(completed.stdout)
        return float(data["duration"]), str(data["fingerprint"])
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("unexpected fpcalc output for %s: %s", path, exc)
        return None


class AcoustidClient:
    """Minimal AcoustID web service client with rate limiting and retries."""

    def __init__(self, api_key: str, session: requests.Session | None = None):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = ACOUSTID_MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def lookup(self, duration: float, fingerprint: str) -> tuple[dict[str, float], set[str]]:
        """Look up a fingerprint.

        Returns ({recording_mbid: score}, {release_mbid, ...}) — the releases
        containing any matched recording, used for candidate generation.
        """
        payload = {
            "client": self.api_key,
            "format": "json",
            "duration": str(int(duration)),
            "fingerprint": fingerprint,
            "meta": "recordingids releaseids",
        }
        for attempt in range(3):
            self._throttle()
            try:
                # POST keeps long fingerprints out of URL length limits
                response = self.session.post(ACOUSTID_LOOKUP_URL, data=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("AcoustID request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                log.warning("AcoustID HTTP %d, backing off", response.status_code)
                time.sleep(2 ** attempt)
                continue
            try:
                data = response.json()
            except ValueError:
                log.warning("AcoustID returned invalid JSON, backing off")
                time.sleep(2 ** attempt)
                continue
            if data.get("status") != "ok":
                message = data.get("error", {}).get("message", "unknown error")
                raise FingerprintError(f"AcoustID error: {message}")
            return self._parse_results(data)
        log.warning("AcoustID lookup gave up after retries")
        return {}, set()

    @staticmethod
    def _parse_results(data: dict) -> tuple[dict[str, float], set[str]]:
        recordings: dict[str, float] = {}
        releases: set[str] = set()
        for result in data.get("results", []):
            score = float(result.get("score", 0.0))
            for recording in result.get("recordings", []) or []:
                mbid = recording.get("id")
                if mbid:
                    recordings[mbid] = max(recordings.get(mbid, 0.0), score)
                for release in recording.get("releases", []) or []:
                    release_id = release.get("id")
                    if release_id:
                        releases.add(release_id)
        return recordings, releases


def fingerprint_files(files: list[AudioFile], client: AcoustidClient,
                      min_score: float) -> None:
    """Fill in fingerprint + AcoustID recording matches on each file, in place."""
    for file in files:
        result = compute_fingerprint(file.path)
        if result is None:
            continue
        duration, fingerprint = result
        file.fingerprint = fingerprint
        if not file.duration:
            file.duration = duration
        try:
            matches, release_ids = client.lookup(duration, fingerprint)
        except FingerprintError as exc:
            log.warning("AcoustID lookup failed for %s: %s", file.path.name, exc)
            continue
        file.recordings = {mbid: score for mbid, score in matches.items()
                           if score >= min_score}
        file.release_ids = release_ids
        log.debug("%s: %d recording candidates", file.path.name, len(file.recordings))
