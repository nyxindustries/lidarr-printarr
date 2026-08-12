"""MusicBrainz web service client (JSON API, rate-limited, retrying)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from printarr import USER_AGENT
from printarr.log import get_logger

log = get_logger(__name__)

MB_API_ROOT = "https://musicbrainz.org/ws/2"
CAA_ROOT = "https://coverartarchive.org"
# MusicBrainz asks anonymous clients to stay at or under 1 request/second
MB_MIN_INTERVAL = 1.0

RELEASE_INC = "artist-credits+recordings+release-groups+media+labels"


class MusicBrainzError(Exception):
    pass


@dataclass
class MBTrack:
    id: str                # track MBID (release-specific)
    recording_id: str      # recording MBID (what AcoustID returns)
    title: str
    position: int          # 1-based position on its medium
    medium: int            # 1-based disc number
    length: float | None   # seconds
    artist: str = ""       # track artist credit (may differ from album artist)
    absolute_position: int = 0  # 1-based across all media


@dataclass
class MBRelease:
    id: str
    title: str
    artist: str
    artist_ids: list[str] = field(default_factory=list)
    artist_sort: str = ""
    release_group_id: str = ""
    release_group_type: str = ""
    status: str = ""
    date: str = ""
    country: str = ""
    label: str = ""
    catalog_number: str = ""
    barcode: str = ""
    media_formats: list[str] = field(default_factory=list)
    medium_track_counts: list[int] = field(default_factory=list)
    tracks: list[MBTrack] = field(default_factory=list)
    # From the release JSON's cover-art-archive block; None when unknown
    has_front_cover: bool | None = None

    @property
    def year(self) -> str:
        return self.date[:4] if self.date else ""

    @property
    def disc_count(self) -> int:
        return max((t.medium for t in self.tracks), default=1)


def _join_artist_credit(credits: list[dict]) -> str:
    parts = []
    for credit in credits or []:
        parts.append(credit.get("name") or credit.get("artist", {}).get("name", ""))
        parts.append(credit.get("joinphrase", ""))
    return "".join(parts).strip()


def _artist_ids(credits: list[dict]) -> list[str]:
    ids = []
    for credit in credits or []:
        artist_id = credit.get("artist", {}).get("id")
        if artist_id:
            ids.append(artist_id)
    return ids


def _artist_sort(credits: list[dict]) -> str:
    for credit in credits or []:
        sort_name = credit.get("artist", {}).get("sort-name")
        if sort_name:
            return sort_name
    return ""


def parse_release(data: dict) -> MBRelease:
    release = MBRelease(
        id=data["id"],
        title=data.get("title", ""),
        artist=_join_artist_credit(data.get("artist-credit", [])),
        artist_ids=_artist_ids(data.get("artist-credit", [])),
        artist_sort=_artist_sort(data.get("artist-credit", [])),
        release_group_id=data.get("release-group", {}).get("id", ""),
        release_group_type=data.get("release-group", {}).get("primary-type", "") or "",
        status=data.get("status", "") or "",
        date=data.get("date", "") or "",
        country=data.get("country", "") or "",
        barcode=data.get("barcode", "") or "",
    )
    caa = data.get("cover-art-archive")
    if isinstance(caa, dict):
        release.has_front_cover = bool(caa.get("front"))
    for label_info in data.get("label-info", []) or []:
        label_name = (label_info.get("label") or {}).get("name")
        if label_name and not release.label:
            release.label = label_name
        catalog = label_info.get("catalog-number")
        if catalog and not release.catalog_number:
            release.catalog_number = catalog

    absolute = 0
    for medium_index, medium in enumerate(data.get("media", []) or [], start=1):
        media_format = medium.get("format") or ""
        release.media_formats.append(media_format)
        tracks = medium.get("tracks", []) or []
        release.medium_track_counts.append(medium.get("track-count", len(tracks)))
        for track in tracks:
            absolute += 1
            recording = track.get("recording", {}) or {}
            length_ms = track.get("length") or recording.get("length")
            release.tracks.append(MBTrack(
                id=track.get("id", ""),
                recording_id=recording.get("id", ""),
                title=track.get("title") or recording.get("title", ""),
                position=int(track.get("position", absolute)),
                medium=medium_index,
                length=(length_ms / 1000.0) if length_ms else None,
                artist=_join_artist_credit(track.get("artist-credit", [])
                                           or recording.get("artist-credit", [])),
                absolute_position=absolute,
            ))
    return release


class MusicBrainzClient:
    def __init__(self, contact: str = "", session: requests.Session | None = None):
        self.session = session or requests.Session()
        agent = USER_AGENT if not contact else f"{USER_AGENT.rstrip(')')}; {contact})"
        self.session.headers["User-Agent"] = agent
        self.session.headers["Accept"] = "application/json"
        self._last_request = 0.0
        self._release_cache: dict[str, MBRelease] = {}

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "fmt": "json"}
        for attempt in range(4):
            wait = MB_MIN_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = self.session.get(f"{MB_API_ROOT}/{path}", params=params, timeout=30)
            except requests.RequestException as exc:
                log.warning("MusicBrainz request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 404:
                raise MusicBrainzError(f"not found: {path}")
            if response.status_code in (429, 503) or response.status_code >= 500:
                delay = _retry_after_seconds(response.headers.get("Retry-After"),
                                             fallback=2 ** attempt)
                log.warning("MusicBrainz HTTP %d, waiting %.1fs", response.status_code, delay)
                time.sleep(delay)
                continue
            if not response.ok:
                raise MusicBrainzError(f"HTTP {response.status_code} for {path}")
            return response.json()
        raise MusicBrainzError(f"giving up on {path} after retries")

    def release(self, mbid: str) -> MBRelease:
        """Full release with track list."""
        if mbid in self._release_cache:
            return self._release_cache[mbid]
        data = self._get(f"release/{mbid}", {"inc": RELEASE_INC})
        release = parse_release(data)
        self._release_cache[mbid] = release
        return release

    def release_group_releases(self, release_group_mbid: str) -> list[dict]:
        """Release stubs (no track lists) of a release group, for pre-filtering.

        Each stub has id, title, status, date, country, media (format + track-count),
        and track-count. Full track lists cost one release() call each.
        """
        data = self._get(f"release-group/{release_group_mbid}",
                         {"inc": "releases+media"})
        return data.get("releases", []) or []

    def releases_by_recording(self, recording_mbid: str) -> list[dict]:
        """Release stubs that contain a recording (used to derive candidates)."""
        data = self._get(f"recording/{recording_mbid}",
                         {"inc": "releases+release-groups+media"})
        return data.get("releases", []) or []

    def search_releases(self, artist: str, release: str, track_count: int | None = None,
                        limit: int = 10) -> list[dict]:
        """Text search for release stubs, used as a fallback when no fingerprints match."""
        terms = []
        if artist:
            terms.append(f'artist:"{_escape_lucene(artist)}"')
        if release:
            terms.append(f'release:"{_escape_lucene(release)}"')
        if track_count:
            terms.append(f"tracks:{track_count}")
        if not terms:
            return []
        data = self._get("release", {"query": " AND ".join(terms), "limit": str(limit)})
        return data.get("releases", []) or []

    def front_cover(self, release_mbid: str, release_group_mbid: str = "",
                    size: int = 500) -> bytes | None:
        """Front cover from the Cover Art Archive, falling back to the release group."""
        cover = self._fetch_cover(f"{CAA_ROOT}/release/{release_mbid}/front-{size}")
        if cover is None and release_group_mbid:
            cover = self._fetch_cover(
                f"{CAA_ROOT}/release-group/{release_group_mbid}/front-{size}")
        return cover

    def _fetch_cover(self, url: str) -> bytes | None:
        try:
            response = self.session.get(url, timeout=60, allow_redirects=True)
        except requests.RequestException as exc:
            log.warning("cover art fetch failed (%s): %s", url, exc)
            return None
        if response.status_code == 404:
            return None
        if not response.ok:
            log.warning("cover art HTTP %d (%s)", response.status_code, url)
            return None
        return response.content


def _retry_after_seconds(header: str | None, fallback: float) -> float:
    """Parse a Retry-After header (seconds or HTTP-date), clamped to [0, 60]."""
    delay = fallback
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                delay = (parsedate_to_datetime(header)
                         - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                pass  # unparseable header: keep the fallback backoff
    # A 503/429 means "slow down" — never retry faster than 1s even when the
    # server sends Retry-After: 0
    return max(1.0, min(delay, 60.0))


def _escape_lucene(text: str) -> str:
    escaped = []
    for char in text:
        if char in '+-&|!(){}[]^"~*?:\\/':
            escaped.append("\\")
        escaped.append(char)
    return "".join(escaped)
