"""Matching downloaded files against MusicBrainz releases.

The design mirrors what works in namer, with the priorities inverted for music:
the acoustic fingerprint (AcoustID recording IDs) is the primary signal, and
text/duration/track-number similarity is the secondary signal. Ambiguous
results are refused rather than guessed — mistagging a music library is worse
than leaving a download for manual review.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from printarr.audiofile import AudioFile
from printarr.config import MatchingConfig
from printarr.log import get_logger
from printarr.musicbrainz import MBRelease, MBTrack

log = get_logger(__name__)

# Two releases closer than this in score are considered ambiguous unless the
# runner-up is a different pressing of the same release group.
AMBIGUITY_MARGIN = 0.05


@dataclass
class Pair:
    file: AudioFile
    track: MBTrack
    score: float
    fingerprint_matched: bool


@dataclass
class ReleaseMatch:
    release: MBRelease
    pairs: list[Pair]
    unmatched_files: list[AudioFile]
    score: float

    @property
    def complete(self) -> bool:
        return not self.unmatched_files


@dataclass
class MatchReport:
    """What was considered, for the per-folder report file (namer-style)."""
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""


def _title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def score_pair(file: AudioFile, track: MBTrack, config: MatchingConfig) -> tuple[float, bool]:
    """Score how well a file matches a release track. Returns (score, fp_matched)."""
    fp_score = file.recordings.get(track.recording_id)
    if fp_score is not None and fp_score >= config.min_acoustid_score:
        # A fingerprint hit is near-certain; the AcoustID score refines it.
        return 0.7 + 0.3 * fp_score, True

    duration_score = 0.0
    if file.duration and track.length:
        delta = abs(file.duration - track.length)
        duration_score = max(0.0, 1.0 - delta / max(config.duration_tolerance, 0.1))

    title_score = _title_similarity(file.tag_title, track.title)

    position_score = 0.0
    if file.track_hint is not None:
        position_score = 1.0 if file.track_hint == track.position else 0.0
        if file.disc_hint is not None and track.medium > 0:
            position_score *= 1.0 if file.disc_hint == track.medium else 0.3

    return 0.45 * duration_score + 0.35 * title_score + 0.2 * position_score, False


def assign_tracks(files: list[AudioFile], release: MBRelease,
                  config: MatchingConfig) -> ReleaseMatch:
    """Greedy one-to-one assignment of files to tracks, best scores first."""
    scored: list[tuple[float, bool, int, int]] = []
    for f_idx, file in enumerate(files):
        for t_idx, track in enumerate(release.tracks):
            score, fp = score_pair(file, track, config)
            if score > 0.1:
                scored.append((score, fp, f_idx, t_idx))
    scored.sort(key=lambda item: (-item[0], item[2], item[3]))

    used_files: set[int] = set()
    used_tracks: set[int] = set()
    pairs: list[Pair] = []
    for score, fp, f_idx, t_idx in scored:
        if f_idx in used_files or t_idx in used_tracks:
            continue
        used_files.add(f_idx)
        used_tracks.add(t_idx)
        pairs.append(Pair(files[f_idx], release.tracks[t_idx], score, fp))

    unmatched = [f for idx, f in enumerate(files) if idx not in used_files]
    pairs.sort(key=lambda p: (p.track.medium, p.track.position))
    return ReleaseMatch(release, pairs, unmatched, _release_score(files, release, pairs))


def _release_score(files: list[AudioFile], release: MBRelease, pairs: list[Pair]) -> float:
    if not files or not pairs:
        return 0.0
    pair_mean = sum(p.score for p in pairs) / len(pairs)
    file_coverage = len(pairs) / len(files)
    track_coverage = len(pairs) / max(len(release.tracks), 1)
    # Unmatched files are a strong negative (wrong release or junk in folder);
    # missing tracks are a weaker one (partial downloads happen).
    return pair_mean * file_coverage * (0.5 + 0.5 * track_coverage)


def choose_release(files: list[AudioFile], candidates: list[MBRelease],
                   config: MatchingConfig) -> tuple[ReleaseMatch | None, MatchReport]:
    """Pick the best-fitting release, refusing on low confidence or ambiguity."""
    report = MatchReport()
    if not candidates:
        report.reason = "no candidate releases"
        return None, report

    matches = [assign_tracks(files, release, config) for release in candidates]
    matches.sort(key=lambda m: (-m.score,
                                abs(len(m.release.tracks) - len(files)),
                                m.release.date or "9999"))

    for match in matches:
        report.candidates.append({
            "release_id": match.release.id,
            "title": match.release.title,
            "artist": match.release.artist,
            "date": match.release.date,
            "country": match.release.country,
            "tracks": len(match.release.tracks),
            "matched_files": len(match.pairs),
            "fingerprint_hits": sum(1 for p in match.pairs if p.fingerprint_matched),
            "score": round(match.score, 4),
        })

    best = matches[0]
    if best.score < config.min_release_confidence:
        report.reason = (f"best candidate '{best.release.title}' scored {best.score:.2f}, "
                         f"below min_release_confidence {config.min_release_confidence}")
        return None, report

    if config.require_all_files_matched and best.unmatched_files:
        names = ", ".join(f.path.name for f in best.unmatched_files[:5])
        report.reason = f"unmatched files remain ({names})"
        return None, report

    if len(matches) > 1:
        runner_up = matches[1]
        same_group = runner_up.release.release_group_id == best.release.release_group_id
        if not same_group and best.score - runner_up.score < AMBIGUITY_MARGIN:
            report.reason = (f"ambiguous: '{best.release.title}' ({best.score:.2f}) vs "
                             f"'{runner_up.release.title}' ({runner_up.score:.2f})")
            return None, report

    report.reason = "matched"
    return best, report
