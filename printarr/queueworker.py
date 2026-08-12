"""Processing stuck Lidarr queue items and standalone watch folders.

Queue flow per stuck item:
1. Resolve the download folder (with path mapping for split-container setups).
2. Build match candidates from the album Lidarr grabbed (its known releases),
   falling back to blind identification for unknown-artist downloads.
3. Fingerprint, match, tag (with MusicBrainz IDs) and rename the files.
4. Trigger the import: an explicit ManualImport command with per-file track
   mapping when the album is in Lidarr, else a DownloadedAlbumsScan.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from printarr.config import Config
from printarr.lidarr import (
    LidarrClient,
    LidarrError,
    candidates_from_album,
)
from printarr.log import get_logger
from printarr.processor import Processor, ProcessResult

log = get_logger(__name__)


class PathMapper:
    """Translates between Lidarr's paths and printarr's paths ("a => b")."""

    def __init__(self, mappings: list[str]):
        self.pairs: list[tuple[str, str]] = []
        for raw in mappings:
            remote, sep, local = raw.partition("=>")
            if not sep:
                raise ValueError(
                    f"invalid path mapping {raw!r} — expected 'lidarr-path => local-path'")
            self.pairs.append((remote.strip(), local.strip()))

    @staticmethod
    def _swap_prefix(path: str, old: str, new: str) -> str | None:
        # Handle both separator styles: Lidarr may run on Windows
        for pure in (PurePosixPath, PureWindowsPath):
            try:
                relative = pure(path).relative_to(pure(old))
            except ValueError:
                continue
            # Join using the target prefix's separator style
            target_style = PureWindowsPath if "\\" in new else PurePosixPath
            return str(target_style(new).joinpath(*relative.parts))
        return None

    def to_local(self, path: str) -> Path:
        for remote, local in self.pairs:
            swapped = self._swap_prefix(path, remote, local)
            if swapped is not None:
                return Path(swapped)
        return Path(path)

    def to_remote(self, path: str) -> str:
        for remote, local in self.pairs:
            swapped = self._swap_prefix(path, local, remote)
            if swapped is not None:
                return swapped
        return path


@dataclass
class QueueOutcome:
    download_id: str
    title: str
    success: bool
    reason: str
    import_triggered: bool = False


class StateStore:
    """Remembers processed downloads/folders so polling doesn't loop on failures."""

    def __init__(self, path: str):
        self.path = Path(path) if path else None
        self.data: dict = {"queue": {}, "folders": {}}
        if self.path and self.path.is_file():
            try:
                self.data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not read state file %s: %s", self.path, exc)
        self.data.setdefault("queue", {})
        self.data.setdefault("folders", {})

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.write_text(json.dumps(self.data, indent=2))
        except OSError as exc:
            log.warning("could not write state file %s: %s", self.path, exc)

    def should_skip_download(self, download_id: str, cooldown: int) -> bool:
        entry = self.data["queue"].get(download_id)
        if not entry:
            return False
        if entry.get("outcome") == "imported":
            return True
        return (time.time() - entry.get("at", 0)) < cooldown

    def record_download(self, download_id: str, outcome: str, reason: str = "") -> None:
        self.data["queue"][download_id] = {
            "at": time.time(), "outcome": outcome, "reason": reason,
        }
        self.save()

    def should_skip_folder(self, key: str, cooldown: int) -> bool:
        entry = self.data["folders"].get(key)
        if not entry:
            return False
        if entry.get("outcome") == "processed":
            return True
        return (time.time() - entry.get("at", 0)) < cooldown

    def record_folder(self, key: str, outcome: str, reason: str = "") -> None:
        self.data["folders"][key] = {
            "at": time.time(), "outcome": outcome, "reason": reason,
        }
        self.save()


class QueueWorker:
    def __init__(self, config: Config, lidarr: LidarrClient, processor: Processor):
        self.config = config
        self.lidarr = lidarr
        self.processor = processor
        self.mapper = PathMapper(config.queue.path_mappings)
        self.state = StateStore(config.general.state_file)

    # ------------------------------------------------------------- queue pass

    def run_once(self) -> list[QueueOutcome]:
        try:
            records = self.lidarr.queue()
        except LidarrError as exc:
            log.error("could not fetch Lidarr queue: %s", exc)
            return []

        stuck = [r for r in records if self.lidarr.is_stuck(r)]
        log.info("queue: %d items, %d stuck", len(records), len(stuck))
        outcomes = []
        for record in stuck:
            outcome = self._handle_record(record)
            if outcome is not None:
                outcomes.append(outcome)
        return outcomes

    def _handle_record(self, record: dict) -> QueueOutcome | None:
        download_id = record.get("downloadId") or ""
        title = record.get("title", "?")
        if not download_id:
            log.debug("skipping queue item without downloadId: %s", title)
            return None
        if self.state.should_skip_download(download_id, self.config.queue.retry_cooldown):
            log.debug("skipping recently handled download %s", title)
            return None

        output_path = record.get("outputPath") or ""
        if not output_path:
            return self._fail(download_id, title, "queue item has no outputPath")
        local_path = self.mapper.to_local(output_path)
        if not local_path.exists():
            return self._fail(
                download_id, title,
                f"path not accessible: {local_path} (configure queue.path_mappings?)")

        log.info("handling stuck download: %s (%s)", title,
                 record.get("trackedDownloadState"))

        candidates = None
        album = record.get("album")
        if album and album.get("id") is not None:
            try:
                # Re-fetch to make sure the releases[] array is populated
                full_album = self.lidarr.album(album["id"])
                artist_name = (record.get("artist") or {}).get("artistName", "")
                from printarr.audiofile import scan_folder
                file_count = len(scan_folder(local_path))
                candidates = candidates_from_album(
                    self.lidarr, full_album, artist_name, file_count=file_count,
                    max_candidates=self.config.matching.max_release_candidates)
            except LidarrError as exc:
                log.warning("could not build candidates from Lidarr album: %s", exc)

        result = self.processor.process_folder(local_path, lidarr_candidates=candidates)
        if not result.success:
            return self._fail(download_id, title, result.reason)

        if self.config.general.dry_run:
            log.info("[dry-run] would trigger Lidarr import for %s", title)
            return QueueOutcome(download_id, title, True, "dry run")

        triggered, reason = self._trigger_import(record, result)
        outcome = "imported" if triggered else "import-failed"
        self.state.record_download(download_id, outcome, reason)
        return QueueOutcome(download_id, title, triggered, reason,
                            import_triggered=triggered)

    def _fail(self, download_id: str, title: str, reason: str) -> QueueOutcome:
        log.warning("%s: %s", title, reason)
        if not self.config.general.dry_run:
            self.state.record_download(download_id, "failed", reason)
        return QueueOutcome(download_id, title, False, reason)

    # ---------------------------------------------------------------- imports

    def _trigger_import(self, record: dict, result: ProcessResult) -> tuple[bool, str]:
        download_id = record["downloadId"]
        candidate = result.lidarr_candidate

        if candidate is None:
            # Album unknown to Lidarr (unmatched download): the files now carry
            # MusicBrainz IDs, so a scan will match if the artist is monitored.
            return self._trigger_scan(record)

        try:
            items = self.lidarr.manual_import(download_id=download_id)
        except LidarrError as exc:
            return False, f"manualimport lookup failed: {exc}"

        by_local_path: dict[str, dict] = {}
        for item in items:
            item_path = item.get("path") or ""
            by_local_path[str(self.mapper.to_local(item_path))] = item

        files = []
        for pair in result.match.pairs:
            item = by_local_path.get(str(pair.file.path))
            if item is None:
                log.warning("file missing from manualimport listing: %s",
                            pair.file.path.name)
                continue
            db_track_id = candidate.db_track_ids.get(pair.track.id)
            if db_track_id is None:
                log.warning("no Lidarr track id for %s", pair.track.title)
                continue
            files.append({
                "path": item["path"],
                "artistId": candidate.db_artist_id,
                "albumId": candidate.db_album_id,
                "albumReleaseId": candidate.db_release_id,
                "trackIds": [db_track_id],
                "quality": item.get("quality"),
                "indexerFlags": item.get("indexerFlags", 0),
                "downloadId": download_id,
                "disableReleaseSwitching": True,
            })

        if not files:
            return self._trigger_scan(record)

        try:
            command = self.lidarr.trigger_manual_import(
                files, import_mode=self.config.queue.import_mode)
            status = self.lidarr.wait_for_command(command.get("id"))
        except LidarrError as exc:
            return False, f"ManualImport command failed: {exc}"
        if status.get("status") != "completed":
            return False, f"ManualImport ended with status {status.get('status')}"
        log.info("import triggered for %d file(s): %s", len(files),
                 record.get("title", ""))
        return True, f"imported {len(files)} file(s)"

    def _trigger_scan(self, record: dict) -> tuple[bool, str]:
        output_path = record.get("outputPath") or ""
        try:
            command = self.lidarr.trigger_downloaded_albums_scan(
                output_path, download_client_id=record.get("downloadId"),
                import_mode=self.config.queue.import_mode)
            status = self.lidarr.wait_for_command(command.get("id"))
        except LidarrError as exc:
            return False, f"DownloadedAlbumsScan failed: {exc}"
        if status.get("status") != "completed":
            return False, f"scan ended with status {status.get('status')}"
        return True, "DownloadedAlbumsScan triggered"

    # ----------------------------------------------------------- watch folders

    def process_watch_folders(self) -> None:
        for folder in self.config.watch.folders:
            root = Path(folder).expanduser()
            if not root.is_dir():
                log.warning("watch folder missing: %s", root)
                continue
            for child in sorted(p for p in root.iterdir() if p.is_dir()):
                self._process_watch_child(child)

    def _newest_mtime(self, folder: Path) -> float:
        newest = folder.stat().st_mtime
        for path in folder.rglob("*"):
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        return newest

    def _process_watch_child(self, child: Path) -> None:
        key = str(child.resolve())
        if self.state.should_skip_folder(key, self.config.queue.retry_cooldown):
            return
        quiet_for = time.time() - self._newest_mtime(child)
        if quiet_for < self.config.watch.quiet_seconds:
            log.debug("folder still settling: %s", child)
            return
        result = self.processor.process_folder(child)
        if self.config.general.dry_run:
            # Dry runs must not persist state or trigger scans
            return
        if result.success and self.config.watch.trigger_lidarr_scan:
            remote = self.mapper.to_remote(str(child))
            try:
                self.lidarr.trigger_downloaded_albums_scan(remote)
            except LidarrError as exc:
                log.warning("scan trigger failed for %s: %s", child, exc)
        self.state.record_folder(key, "processed" if result.success else "failed",
                                 result.reason)

    # ------------------------------------------------------------------- watch

    def watch(self) -> None:
        interval = max(self.config.queue.poll_interval, 10)
        log.info("watching Lidarr queue every %ds (Ctrl+C to stop)", interval)
        while True:
            try:
                self.run_once()
                self.process_watch_folders()
            except Exception:
                log.exception("watch cycle failed")
            time.sleep(interval)
