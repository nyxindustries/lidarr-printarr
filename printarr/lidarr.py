"""Lidarr v1 API client and adapters.

Field names and enum values follow Lidarr's OpenAPI spec (develop branch).
Queue items stuck on import show status "completed" with trackedDownloadStatus
"warning"/"error" and trackedDownloadState importBlocked/importPending/importFailed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from printarr import USER_AGENT
from printarr.log import get_logger
from printarr.musicbrainz import MBRelease, MBTrack

log = get_logger(__name__)

# trackedDownloadState values that mean "download finished but not imported"
STUCK_STATES = {"importBlocked", "importPending", "importFailed"}


class LidarrError(Exception):
    pass


@dataclass
class LidarrCandidate:
    """A Lidarr DB release paired with its MusicBrainz-shaped track list."""
    release: MBRelease           # id == MB release MBID
    db_release_id: int
    db_album_id: int
    db_artist_id: int
    # MB track MBID (foreignTrackId) -> Lidarr DB track id
    db_track_ids: dict[str, int] = field(default_factory=dict)


class LidarrClient:
    def __init__(self, url: str, api_key: str, verify_ssl: bool = True,
                 session: requests.Session | None = None):
        self.base = url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["X-Api-Key"] = api_key
        self.session.headers["User-Agent"] = USER_AGENT
        self.session.verify = verify_ssl

    def _request(self, method: str, path: str, params: dict | None = None,
                 json_body=None):
        url = f"{self.base}/api/v1/{path.lstrip('/')}"
        try:
            response = self.session.request(method, url, params=params,
                                            json=json_body, timeout=60)
        except requests.RequestException as exc:
            raise LidarrError(f"Lidarr request failed: {method} {path}: {exc}") from exc
        if response.status_code == 401:
            raise LidarrError("Lidarr rejected the API key (HTTP 401)")
        if not response.ok:
            detail = response.text[:300]
            raise LidarrError(f"Lidarr HTTP {response.status_code} for {method} {path}: {detail}")
        if not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ queue

    def queue(self) -> list[dict]:
        """All queue records across pages, including unknown-artist items."""
        records: list[dict] = []
        page = 1
        while True:
            data = self._request("GET", "queue", params={
                "page": page,
                "pageSize": 50,
                "includeUnknownArtistItems": "true",
                "includeArtist": "true",
                "includeAlbum": "true",
            })
            batch = data.get("records", [])
            records.extend(batch)
            if page * 50 >= int(data.get("totalRecords", 0)) or not batch:
                return records
            page += 1

    @staticmethod
    def is_stuck(record: dict) -> bool:
        """Download finished, but Lidarr could not (or will not) import it."""
        state = record.get("trackedDownloadState", "")
        if state in ("importBlocked", "importFailed"):
            return True
        if state == "importPending":
            # importPending is also the transient state right before a normal
            # import; only treat it as stuck when Lidarr flags a problem.
            return record.get("trackedDownloadStatus") in ("warning", "error")
        return False

    # ---------------------------------------------------------- album lookups

    def album_by_release_group(self, release_group_mbid: str) -> dict | None:
        albums = self._request("GET", "album",
                               params={"foreignAlbumId": release_group_mbid})
        return albums[0] if albums else None

    def album(self, album_id: int) -> dict:
        return self._request("GET", f"album/{album_id}")

    def tracks_for_release(self, album_release_id: int) -> list[dict]:
        return self._request("GET", "track",
                             params={"albumReleaseId": album_release_id}) or []

    # -------------------------------------------------------------- importing

    def manual_import(self, download_id: str | None = None,
                      folder: str | None = None,
                      filter_existing: bool = False) -> list[dict]:
        params: dict = {"filterExistingFiles": "true" if filter_existing else "false"}
        if download_id:
            params["downloadId"] = download_id
        elif folder:
            params["folder"] = folder
        else:
            raise ValueError("manual_import needs download_id or folder")
        return self._request("GET", "manualimport", params=params) or []

    def command(self, name: str, **payload) -> dict:
        return self._request("POST", "command", json_body={"name": name, **payload})

    def command_status(self, command_id: int) -> dict:
        return self._request("GET", f"command/{command_id}")

    def wait_for_command(self, command_id: int, timeout: float = 300.0) -> dict:
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.command_status(command_id)
            if status.get("status") in ("completed", "failed", "aborted",
                                        "cancelled", "orphaned"):
                return status
            time.sleep(2)
        log.warning("command %d did not finish within %.0fs", command_id, timeout)
        return status

    def trigger_manual_import(self, files: list[dict], import_mode: str = "auto",
                              replace_existing: bool = False) -> dict:
        return self.command("ManualImport", files=files, importMode=import_mode,
                            replaceExistingFiles=replace_existing)

    def trigger_downloaded_albums_scan(self, path: str,
                                       download_client_id: str | None = None,
                                       import_mode: str = "auto") -> dict:
        payload: dict = {"path": path, "importMode": import_mode}
        if download_client_id:
            payload["downloadClientId"] = download_client_id
        return self.command("DownloadedAlbumsScan", **payload)


# ------------------------------------------------------------------- adapters

def candidates_from_album(client: LidarrClient, album: dict, artist_name: str = "",
                          file_count: int | None = None,
                          max_candidates: int = 10) -> list[LidarrCandidate]:
    """Build match candidates from a Lidarr album's known releases.

    Uses Lidarr's own release/track data (mirrored from MusicBrainz), avoiding
    MusicBrainz round-trips while scoring. Each candidate costs one /track call,
    so releases are pre-ranked by track-count fit and capped.
    """
    album_id = album.get("id")
    artist_id = album.get("artistId")
    artist = album.get("artist") or {}
    name = artist_name or artist.get("artistName", "")
    release_group_mbid = album.get("foreignAlbumId", "")
    release_date = (album.get("releaseDate") or "")[:10]

    releases = list(album.get("releases", []) or [])
    if file_count is not None:
        releases.sort(key=lambda r: (abs((r.get("trackCount") or 0) - file_count),
                                     not r.get("monitored", False)))
    releases = releases[:max_candidates]

    candidates: list[LidarrCandidate] = []
    for release in releases:
        foreign_release_id = release.get("foreignReleaseId")
        db_release_id = release.get("id")
        if not foreign_release_id or db_release_id is None:
            continue
        try:
            raw_tracks = client.tracks_for_release(db_release_id)
        except LidarrError as exc:
            log.warning("could not fetch tracks for release %s: %s", db_release_id, exc)
            continue

        tracks: list[MBTrack] = []
        db_track_ids: dict[str, int] = {}
        for absolute, raw in enumerate(sorted(
                raw_tracks, key=lambda t: (t.get("mediumNumber", 1),
                                           t.get("absoluteTrackNumber", 0))), start=1):
            track_number = str(raw.get("trackNumber", "") or "")
            position = (int(track_number) if track_number.isdigit()
                        else int(raw.get("absoluteTrackNumber", absolute)))
            duration_ms = raw.get("duration") or 0
            track = MBTrack(
                id=raw.get("foreignTrackId", ""),
                recording_id=raw.get("foreignRecordingId", ""),
                title=raw.get("title", ""),
                position=position,
                medium=int(raw.get("mediumNumber", 1) or 1),
                length=(duration_ms / 1000.0) if duration_ms else None,
                absolute_position=absolute,
            )
            tracks.append(track)
            if track.id:
                db_track_ids[track.id] = raw.get("id")

        media = release.get("media", []) or []
        mb_release = MBRelease(
            id=foreign_release_id,
            title=release.get("title") or album.get("title", ""),
            artist=name,
            release_group_id=release_group_mbid,
            status=release.get("status", "") or "",
            date=release_date,
            country=", ".join(release.get("country", []) or []),
            label=", ".join(release.get("label", []) or []),
            media_formats=[m.get("mediumFormat", "") for m in media],
            medium_track_counts=[],
            tracks=tracks,
        )
        candidates.append(LidarrCandidate(
            release=mb_release,
            db_release_id=db_release_id,
            db_album_id=album_id,
            db_artist_id=artist_id,
            db_track_ids=db_track_ids,
        ))
    return candidates
