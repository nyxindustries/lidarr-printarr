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
import os
import threading
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
        self._lock = threading.Lock()
        self.data: dict = {"queue": {}, "folders": {}}
        if self.path and self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, dict):
                    self.data = loaded
                else:
                    log.warning("state file %s is not a JSON object, ignoring", self.path)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("could not read state file %s: %s", self.path, exc)
        self.data.setdefault("queue", {})
        self.data.setdefault("folders", {})

    def save(self) -> None:
        if not self.path:
            return
        # Atomic replace so a crash mid-write cannot corrupt the state file
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(temp, "w") as fh:
                json.dump(self.data, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            log.warning("could not write state file %s: %s", self.path, exc)
            temp.unlink(missing_ok=True)

    def should_skip_download(self, download_id: str, cooldown: int) -> bool:
        entry = self.data["queue"].get(download_id)
        if not entry:
            return False
        if entry.get("outcome") == "imported":
            return True
        return (time.time() - entry.get("at", 0)) < cooldown

    def record_download(self, download_id: str, outcome: str, reason: str = "",
                       title: str = "", path: str = "") -> None:
        with self._lock:
            entry = {"at": time.time(), "outcome": outcome, "reason": reason}
            if title:
                entry["title"] = title
            if path:
                entry["path"] = path
            self.data["queue"][download_id] = entry
            self.save()

    def clear_download(self, download_id: str) -> None:
        with self._lock:
            if self.data["queue"].pop(download_id, None) is not None:
                self.save()

    def should_skip_folder(self, key: str, cooldown: int) -> bool:
        entry = self.data["folders"].get(key)
        if not entry:
            return False
        if entry.get("outcome") == "processed":
            return True
        return (time.time() - entry.get("at", 0)) < cooldown

    def record_folder(self, key: str, outcome: str, reason: str = "") -> None:
        with self._lock:
            self.data["folders"][key] = {
                "at": time.time(), "outcome": outcome, "reason": reason,
            }
            self.save()

    def clear_folder(self, key: str) -> None:
        with self._lock:
            if self.data["folders"].pop(key, None) is not None:
                self.save()


class QueueWorker:
    def __init__(self, config: Config, lidarr: LidarrClient, processor: Processor):
        self.config = config
        self.lidarr = lidarr
        self.processor = processor
        self.mapper = PathMapper(config.queue.path_mappings)
        self.state = StateStore(config.general.state_file)
        if config.general.dry_run:
            # Keep outcomes in memory (so dry-run watch loops don't reprocess
            # everything each cycle) but never persist them
            self.state.path = None
        # Serializes folder processing between the watch loop and the web UI
        self.process_lock = threading.Lock()

    # ------------------------------------------------------------- queue pass

    def run_once(self) -> list[QueueOutcome]:
        """One pass over the queue. Raises LidarrError if the queue fetch fails."""
        records = self.lidarr.queue()
        stuck = [r for r in records if self.lidarr.is_stuck(r)]
        log.info("queue: %d items, %d stuck", len(records), len(stuck))
        outcomes = []
        for record in stuck:
            try:
                with self.process_lock:
                    outcome = self._handle_record(record)
            except Exception as exc:
                # One poisoned item must not abort the pass or dodge the cooldown
                title = record.get("title", "?")
                log.exception("unexpected error handling %s", title)
                download_id = record.get("downloadId") or ""
                if download_id:
                    self.state.record_download(download_id, "failed",
                                               f"unexpected error: {exc}", title=title)
                outcome = QueueOutcome(download_id, title, False,
                                       f"unexpected error: {exc}")
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
                f"path not accessible: {local_path} (configure queue.path_mappings?)",
                path=str(local_path))

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
            return self._fail(download_id, title, result.reason, path=str(local_path))

        if self.config.general.dry_run:
            log.info("[dry-run] would trigger Lidarr import for %s", title)
            self.state.record_download(download_id, "dry-run", "dry run",
                                       title=title, path=str(local_path))
            return QueueOutcome(download_id, title, True, "dry run")

        triggered, reason = self._trigger_import(record, result)
        outcome = "imported" if triggered else "import-failed"
        self.state.record_download(download_id, outcome, reason,
                                   title=title, path=str(local_path))
        return QueueOutcome(download_id, title, triggered, reason,
                            import_triggered=triggered)

    def _fail(self, download_id: str, title: str, reason: str,
              path: str = "") -> QueueOutcome:
        # In dry-run mode the StateStore is in-memory only, so recording is safe
        log.warning("%s: %s", title, reason)
        self.state.record_download(download_id, "failed", reason,
                                   title=title, path=path)
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
            local = self.mapper.to_local(item_path)
            try:
                # pair.file.path is resolved (process_folder resolves the
                # folder), so canonicalize this side too or symlinked download
                # dirs never match
                key = str(local.resolve())
            except OSError:
                key = str(local)
            by_local_path[key] = item

        files = []
        for pair in result.match.pairs:
            item = by_local_path.get(str(pair.file.path))
            if item is None:
                log.warning("file missing from manualimport listing: %s",
                            pair.file.path.name)
                continue
            db_track_id = candidate.db_track_ids.get(
                pair.orig_track_id or pair.track.id)
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
            log.warning("no manualimport items matched the processed files for %s "
                        "— falling back to a folder scan (path mapping mismatch?)",
                        record.get("title", ""))
            return self._trigger_scan(record)

        try:
            command = self.lidarr.trigger_manual_import(
                files, import_mode=self.config.queue.import_mode)
            status = self.lidarr.wait_for_command(command.get("id"))
        except LidarrError as exc:
            return False, f"ManualImport command failed: {exc}"
        if status.get("status") != "completed":
            return False, f"ManualImport ended with status {status.get('status')}"
        # A "completed" command does not mean the files imported — Lidarr
        # swallows per-file failures. Only the queue tells the truth.
        if not self._import_verified(download_id):
            return False, "queue item still stuck after ManualImport"
        log.info("import verified for %d file(s): %s", len(files),
                 record.get("title", ""))
        return True, f"imported {len(files)} file(s)"

    def _trigger_scan(self, record: dict) -> tuple[bool, str]:
        output_path = record.get("outputPath") or ""
        download_id = record.get("downloadId") or ""
        try:
            command = self.lidarr.trigger_downloaded_albums_scan(
                output_path, download_client_id=download_id,
                import_mode=self.config.queue.import_mode)
            status = self.lidarr.wait_for_command(command.get("id"))
        except LidarrError as exc:
            return False, f"DownloadedAlbumsScan failed: {exc}"
        if status.get("status") != "completed":
            return False, f"scan ended with status {status.get('status')}"
        if str(status.get("result", "")).lower() == "unsuccessful":
            return False, "DownloadedAlbumsScan imported nothing"
        if download_id and not self._import_verified(download_id):
            return False, "queue item still stuck after DownloadedAlbumsScan"
        return True, "DownloadedAlbumsScan imported the download"

    def _import_verified(self, download_id: str, settle_seconds: float = 3.0) -> bool:
        """True when the queue item is gone or no longer stuck after an import."""
        time.sleep(settle_seconds)  # Lidarr refreshes tracked downloads async
        try:
            records = self.lidarr.queue()
        except LidarrError as exc:
            log.warning("could not verify import of %s: %s", download_id, exc)
            # Unverifiable counts as failure: the cooldown retry is benign,
            # wrongly recording "imported" abandons the item forever
            return False
        for record in records:
            if record.get("downloadId") == download_id:
                return not self.lidarr.is_stuck(record)
        return True

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
        with self.process_lock:
            self._process_watch_child_locked(child)

    def _process_watch_child_locked(self, child: Path) -> None:
        key = str(child.resolve())
        if self.state.should_skip_folder(key, self.config.queue.retry_cooldown):
            return
        try:
            quiet_for = time.time() - self._newest_mtime(child)
        except OSError as exc:
            log.debug("watch child vanished before processing: %s (%s)", child, exc)
            return
        if quiet_for < self.config.watch.quiet_seconds:
            log.debug("folder still settling: %s", child)
            return
        try:
            result = self.processor.process_folder(child)
        except Exception as exc:
            log.exception("unexpected error processing %s", child)
            self.state.record_folder(key, "failed", f"unexpected error: {exc}")
            return
        if self.config.general.dry_run:
            # State is in-memory in dry-run mode; never trigger Lidarr actions
            self.state.record_folder(key, "processed" if result.success else "failed",
                                     result.reason)
            return
        if result.success and self.config.watch.trigger_lidarr_scan:
            remote = self.mapper.to_remote(str(child))
            try:
                self.lidarr.trigger_downloaded_albums_scan(remote)
            except LidarrError as exc:
                log.warning("scan trigger failed for %s: %s", child, exc)
        self.state.record_folder(key, "processed" if result.success else "failed",
                                 result.reason)

    # ------------------------------------------------------- review UI support

    def list_review_items(self) -> dict:
        """Stuck queue items and failed watch folders, with their candidate
        tables from the per-folder report files. Feeds the web UI."""
        queue_items: list[dict] = []
        error = ""
        try:
            records = self.lidarr.queue()
        except LidarrError as exc:
            records = []
            error = str(exc)
        for record in records:
            if not self.lidarr.is_stuck(record):
                continue
            download_id = record.get("downloadId") or ""
            local = self.mapper.to_local(record.get("outputPath") or "")
            entry = self.state.data["queue"].get(download_id, {})
            queue_items.append({
                "download_id": download_id,
                "title": record.get("title", "?"),
                "state": record.get("trackedDownloadState", ""),
                "messages": [m.get("title", "") for m in
                             (record.get("statusMessages") or [])],
                "outcome": entry.get("outcome", ""),
                "reason": entry.get("reason", ""),
                "path": str(local),
                "exists": local.exists(),
                "candidates": self._read_report(local),
            })

        folders: list[dict] = []
        for key, entry in list(self.state.data["folders"].items()):
            if entry.get("outcome") != "failed":
                continue
            folder = Path(key)
            if not folder.is_dir():
                continue
            folders.append({
                "path": key,
                "title": folder.name,
                "reason": entry.get("reason", ""),
                "candidates": self._read_report(folder),
            })
        return {"queue": queue_items, "folders": folders, "error": error}

    @staticmethod
    def _read_report(folder: Path) -> list[dict]:
        from printarr.processor import REPORT_FILENAME
        try:
            data = json.loads((folder / REPORT_FILENAME).read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return []
        candidates = data.get("candidates", [])
        return candidates if isinstance(candidates, list) else []

    def assign_queue_item(self, download_id: str, release_mbid: str | None = None,
                          release_group_mbid: str | None = None) -> tuple[bool, str]:
        """Human-confirmed match for a stuck queue item: force-process, then
        import via a folder scan (the fresh MusicBrainz tags make Lidarr's
        matching deterministic)."""
        with self.process_lock:
            try:
                records = self.lidarr.queue()
            except LidarrError as exc:
                return False, f"queue fetch failed: {exc}"
            record = next((r for r in records
                           if r.get("downloadId") == download_id), None)
            if record is None:
                self.state.clear_download(download_id)
                return False, "queue item no longer exists in Lidarr"
            title = record.get("title", "?")
            local = self.mapper.to_local(record.get("outputPath") or "")
            if not local.exists():
                return False, f"path not accessible: {local}"

            result = self.processor.process_folder(
                local, forced_release_mbid=release_mbid,
                forced_release_group_mbid=release_group_mbid)
            if not result.success:
                return False, result.reason
            if self.config.general.dry_run:
                return True, "dry run: matched, would trigger import"

            triggered, reason = self._trigger_scan(record)
            self.state.record_download(
                download_id, "imported" if triggered else "import-failed",
                reason, title=title, path=str(local))
            return triggered, reason

    def assign_folder(self, path: str, release_mbid: str | None = None,
                      release_group_mbid: str | None = None) -> tuple[bool, str]:
        """Human-confirmed match for a watch folder."""
        with self.process_lock:
            folder = Path(path)
            if not folder.is_dir():
                return False, f"not a folder: {path}"
            key = str(folder.resolve())
            result = self.processor.process_folder(
                folder, forced_release_mbid=release_mbid,
                forced_release_group_mbid=release_group_mbid)
            if not result.success:
                self.state.record_folder(key, "failed", result.reason)
                return False, result.reason
            if self.config.general.dry_run:
                return True, "dry run: matched"
            if self.config.watch.trigger_lidarr_scan:
                remote = self.mapper.to_remote(key)
                try:
                    self.lidarr.trigger_downloaded_albums_scan(remote)
                except LidarrError as exc:
                    log.warning("scan trigger failed for %s: %s", folder, exc)
            self.state.record_folder(key, "processed", "assigned manually")
            return True, "processed"

    def retry_queue_item(self, download_id: str) -> tuple[bool, str]:
        """Re-run automatic identification for one stuck item right now."""
        self.state.clear_download(download_id)
        try:
            records = self.lidarr.queue()
        except LidarrError as exc:
            return False, f"queue fetch failed: {exc}"
        record = next((r for r in records
                       if r.get("downloadId") == download_id), None)
        if record is None:
            return False, "queue item no longer exists in Lidarr"
        try:
            with self.process_lock:
                outcome = self._handle_record(record)
        except Exception as exc:
            log.exception("retry failed for %s", download_id)
            return False, f"unexpected error: {exc}"
        if outcome is None:
            return False, "item was skipped"
        return outcome.success, outcome.reason

    def retry_folder(self, path: str) -> tuple[bool, str]:
        self.state.clear_folder(str(Path(path).resolve()))
        folder = Path(path)
        if not folder.is_dir():
            return False, f"not a folder: {path}"
        with self.process_lock:
            result = self.processor.process_folder(folder)
            key = str(folder.resolve())
            if self.config.general.dry_run:
                return result.success, result.reason or "dry run"
            self.state.record_folder(key,
                                     "processed" if result.success else "failed",
                                     result.reason)
        return result.success, result.reason

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
