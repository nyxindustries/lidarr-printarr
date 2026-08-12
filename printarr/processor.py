"""Orchestration: identify a folder of music, fix its tags, rename its files.

Candidate releases come from three sources, tried in this order:
1. Explicitly supplied candidates (queue mode: the releases Lidarr already
   knows for the grabbed album — no MusicBrainz calls needed for scoring).
2. A release-group MBID hint (CLI flag or Lidarr's foreignAlbumId).
3. Blind identification: AcoustID release votes across files, falling back
   to a MusicBrainz text search built from the existing tags.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from printarr.audiofile import AudioFile, scan_folder
from printarr.config import Config
from printarr.fingerprint import AcoustidClient, FingerprintError, fingerprint_files
from printarr.lidarr import LidarrCandidate
from printarr.log import get_logger
from printarr.matching import MatchReport, ReleaseMatch, choose_release
from printarr.musicbrainz import MBRelease, MusicBrainzClient, MusicBrainzError
from printarr.renamer import RenamePlan, apply_renames, format_template, unique_target
from printarr.tagger import build_track_tags, write_tags

log = get_logger(__name__)

REPORT_FILENAME = "printarr-report.json"


@dataclass
class ProcessResult:
    folder: Path
    success: bool
    reason: str = ""
    match: ReleaseMatch | None = None
    report: MatchReport | None = None
    tagged: list[Path] = field(default_factory=list)
    renamed: dict[str, str] = field(default_factory=dict)  # old path -> new path
    # Set when the match came from a supplied LidarrCandidate
    lidarr_candidate: LidarrCandidate | None = None

    @property
    def release(self) -> MBRelease | None:
        return self.match.release if self.match else None


class Processor:
    def __init__(self, config: Config, mb: MusicBrainzClient,
                 acoustid: AcoustidClient | None):
        self.config = config
        self.mb = mb
        self.acoustid = acoustid

    # ---------------------------------------------------------- identification

    def fingerprint(self, files: list[AudioFile]) -> None:
        if self.acoustid is None:
            log.info("no AcoustID key configured — matching on tags/durations only")
            return
        try:
            fingerprint_files(files, self.acoustid,
                              self.config.matching.min_acoustid_score)
        except FingerprintError as exc:
            log.warning("fingerprinting unavailable: %s", exc)

    def candidates_from_release_group(self, release_group_mbid: str,
                                      file_count: int) -> list[MBRelease]:
        try:
            stubs = self.mb.release_group_releases(release_group_mbid)
        except MusicBrainzError as exc:
            log.warning("release group lookup failed: %s", exc)
            return []
        return self._fetch_release_stubs(stubs, file_count)

    def candidates_from_votes(self, files: list[AudioFile],
                              file_count: int) -> list[MBRelease]:
        votes: Counter[str] = Counter()
        voting_files = 0
        for file in files:
            if file.release_ids:
                voting_files += 1
                votes.update(set(file.release_ids))
        if not voting_files:
            return []
        # A plausible release must cover at least half the files that voted
        threshold = (voting_files + 1) // 2
        # Deterministic order: vote count desc, then MBID, so ties at the
        # candidate cap cannot flip between runs
        ranked = sorted((mbid for mbid, count in votes.items() if count >= threshold),
                        key=lambda mbid: (-votes[mbid], mbid))
        candidates: list[MBRelease] = []
        for mbid in ranked[: self.config.matching.max_release_candidates]:
            try:
                candidates.append(self.mb.release(mbid))
            except MusicBrainzError as exc:
                log.debug("skipping release %s: %s", mbid, exc)
        return candidates

    def candidates_from_tags(self, files: list[AudioFile],
                             file_count: int) -> list[MBRelease]:
        artists = Counter(f.tag_albumartist or f.tag_artist
                          for f in files if (f.tag_albumartist or f.tag_artist))
        albums = Counter(f.tag_album for f in files if f.tag_album)
        if not albums:
            return []
        artist = artists.most_common(1)[0][0] if artists else ""
        album = albums.most_common(1)[0][0]
        try:
            stubs = self.mb.search_releases(artist, album, track_count=file_count)
        except MusicBrainzError as exc:
            log.warning("release search failed: %s", exc)
            return []
        return self._fetch_release_stubs(stubs, file_count)

    def _fetch_release_stubs(self, stubs: list[dict],
                             file_count: int) -> list[MBRelease]:
        """Rank release stubs cheaply, then fetch full track lists for the top few."""
        def stub_rank(stub: dict):
            track_count = stub.get("track-count")
            if track_count is None:
                track_count = sum(m.get("track-count", 0)
                                  for m in stub.get("media", []) or [])
            count_delta = abs((track_count or 0) - file_count)
            official = 0 if stub.get("status") == "Official" else 1
            return (count_delta, official, stub.get("date") or "9999")

        ranked = sorted(stubs, key=stub_rank)
        candidates: list[MBRelease] = []
        for stub in ranked[: self.config.matching.max_release_candidates]:
            try:
                candidates.append(self.mb.release(stub["id"]))
            except (MusicBrainzError, KeyError) as exc:
                log.debug("skipping release stub: %s", exc)
        return candidates

    def identify(self, files: list[AudioFile],
                 lidarr_candidates: list[LidarrCandidate] | None = None,
                 release_group_hint: str | None = None,
                 ) -> tuple[ReleaseMatch | None, MatchReport, LidarrCandidate | None]:
        self.fingerprint(files)

        if lidarr_candidates:
            releases = [c.release for c in lidarr_candidates]
            match, report = choose_release(files, releases, self.config.matching)
            chosen = None
            if match is not None:
                chosen = next(c for c in lidarr_candidates
                              if c.release.id == match.release.id)
            return match, report, chosen

        if release_group_hint:
            candidates = self.candidates_from_release_group(release_group_hint,
                                                            len(files))
            if candidates:
                match, report = choose_release(files, candidates, self.config.matching)
                return match, report, None

        candidates = self.candidates_from_votes(files, len(files))
        if not candidates:
            candidates = self.candidates_from_tags(files, len(files))
        match, report = choose_release(files, candidates, self.config.matching)
        return match, report, None

    # --------------------------------------------------------------- enriching

    def enrich(self, match: ReleaseMatch) -> None:
        """Swap a Lidarr-derived release for full MusicBrainz metadata before tagging.

        pair.orig_track_id keeps the originally matched track MBID, which is
        what the Lidarr import mapping is keyed on.
        """
        try:
            full = self.mb.release(match.release.id)
        except MusicBrainzError as exc:
            log.warning("could not enrich release %s from MusicBrainz: %s",
                        match.release.id, exc)
            return
        by_track_id = {track.id: track for track in full.tracks}
        by_recording: dict[str, list] = {}
        for track in full.tracks:
            by_recording.setdefault(track.recording_id, []).append(track)
        for pair in match.pairs:
            pair.orig_track_id = pair.orig_track_id or pair.track.id
            enriched = by_track_id.get(pair.track.id)
            if enriched is None:
                # Same recording can appear more than once; prefer the same slot
                options = by_recording.get(pair.track.recording_id) or []
                enriched = next(
                    (t for t in options
                     if (t.medium, t.position) == (pair.track.medium, pair.track.position)),
                    options[0] if options else None)
            if enriched is not None:
                pair.track = enriched
        match.release = full

    # ----------------------------------------------------------------- actions

    def tag(self, match: ReleaseMatch) -> list[Path]:
        cover = None
        cover_mime = "image/jpeg"
        if self.config.tagging.write_cover_art and not self.config.general.dry_run:
            if match.release.has_front_cover is not False:
                cover = self.mb.front_cover(match.release.id,
                                            match.release.release_group_id)
            if cover and cover[:8] == b"\x89PNG\r\n\x1a\n":
                cover_mime = "image/png"

        tagged: list[Path] = []
        for pair in match.pairs:
            tags = build_track_tags(match.release, pair.track)
            if self.config.general.dry_run:
                log.info("[dry-run] would tag %s as %s - %s (track %d)",
                         pair.file.path.name, tags.artist, tags.title, tags.track)
                continue
            write_tags(pair.file.path, tags, self.config.tagging,
                       cover=cover, cover_mime=cover_mime)
            tagged.append(pair.file.path)
        return tagged

    def rename(self, match: ReleaseMatch) -> dict[str, str]:
        if not self.config.renaming.enabled:
            return {}
        multi_disc = match.release.disc_count > 1
        template = (self.config.renaming.multi_disc_template if multi_disc
                    else self.config.renaming.file_template)
        plans: list[RenamePlan] = []
        taken: set[Path] = set()
        # Files being renamed vacate their current names, so those names are
        # free as targets (e.g. two swapped filenames must not collide).
        vacating = {pair.file.path for pair in match.pairs}
        for pair in match.pairs:
            values = {
                "track": pair.track.position,
                "disc": pair.track.medium,
                "title": pair.track.title,
                "artist": pair.track.artist or match.release.artist,
                "albumartist": match.release.artist,
                "album": match.release.title,
                "year": match.release.year,
            }
            new_name = format_template(template, values) + pair.file.path.suffix.lower()
            target = unique_target(pair.file.path.with_name(new_name), taken,
                                   vacated=vacating)
            taken.add(target)
            plans.append(RenamePlan(pair.file.path, target))

        applied = apply_renames(plans, dry_run=self.config.general.dry_run)
        renamed: dict[str, str] = {}
        for plan in applied:
            renamed[str(plan.source)] = str(plan.target)
        if not self.config.general.dry_run:
            for pair in match.pairs:
                new_path = renamed.get(str(pair.file.path))
                if new_path:
                    pair.file.path = Path(new_path)
        return renamed

    # ------------------------------------------------------------------ folder

    def process_folder(self, folder: Path,
                       lidarr_candidates: list[LidarrCandidate] | None = None,
                       release_group_hint: str | None = None) -> ProcessResult:
        folder = folder.resolve()
        if not folder.exists():
            return ProcessResult(folder, False, reason=f"path does not exist: {folder}")

        files = scan_folder(folder)
        if not files:
            return ProcessResult(folder, False, reason="no audio files found")
        log.info("processing %s (%d audio files)", folder, len(files))

        match, report, chosen = self.identify(files, lidarr_candidates,
                                              release_group_hint)
        if match is None:
            log.warning("no confident match for %s: %s", folder, report.reason)
            self._write_report(folder, report, success=False)
            return ProcessResult(folder, False, reason=report.reason, report=report)

        if chosen is not None:
            self.enrich(match)

        log.info("matched: %s — %s (%s, %s, %d/%d tracks, score %.2f)",
                 match.release.artist, match.release.title, match.release.year,
                 match.release.id, len(match.pairs), len(match.release.tracks),
                 match.score)

        if not self.config.general.dry_run and folder.is_dir():
            # Drop the report a previously refused attempt may have left behind
            try:
                (folder / REPORT_FILENAME).unlink(missing_ok=True)
            except OSError as exc:
                log.debug("could not remove stale report file: %s", exc)

        try:
            tagged = self.tag(match) if self.config.tagging.enabled else []
            renamed = self.rename(match)
        except Exception as exc:
            log.error("tag/rename failed for %s: %s", folder, exc)
            return ProcessResult(folder, False, reason=f"tag/rename failed: {exc}",
                                 match=match, report=report, lidarr_candidate=chosen)
        return ProcessResult(folder, True, reason=report.reason, match=match,
                             report=report, tagged=tagged, renamed=renamed,
                             lidarr_candidate=chosen)

    def _write_report(self, folder: Path, report: MatchReport, success: bool) -> None:
        if self.config.general.dry_run or not folder.is_dir():
            return
        payload = {
            "success": success,
            "reason": report.reason,
            "candidates": report.candidates,
        }
        try:
            (folder / REPORT_FILENAME).write_text(json.dumps(payload, indent=2))
        except OSError as exc:
            log.debug("could not write report file: %s", exc)
