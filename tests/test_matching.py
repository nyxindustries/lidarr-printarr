from pathlib import Path

from printarr.audiofile import AudioFile
from printarr.config import MatchingConfig
from printarr.matching import assign_tracks, choose_release, score_pair
from printarr.musicbrainz import MBRelease, MBTrack


def make_track(recording_id: str, position: int, title: str, length: float,
               medium: int = 1) -> MBTrack:
    return MBTrack(id=f"track-{recording_id}", recording_id=recording_id,
                   title=title, position=position, medium=medium, length=length,
                   absolute_position=position)


def make_release(release_id: str, tracks: list[MBTrack], group: str = "rg-1",
                 title: str = "Album", date: str = "2001-03-24") -> MBRelease:
    return MBRelease(id=release_id, title=title, artist="Artist",
                     release_group_id=group, date=date, tracks=tracks)


def make_file(name: str, duration: float, recordings: dict | None = None,
              title: str = "", track: int | None = None) -> AudioFile:
    return AudioFile(path=Path(f"/music/{name}"), duration=duration,
                     recordings=recordings or {}, tag_title=title,
                     filename_track=track)


CONFIG = MatchingConfig()


class TestScorePair:
    def test_fingerprint_match_dominates(self):
        file = make_file("01.flac", 200, recordings={"rec-1": 1.0})
        track = make_track("rec-1", 1, "Song", 200)
        score, fp = score_pair(file, track, CONFIG)
        assert fp is True
        assert score == 1.0

    def test_fingerprint_below_min_score_ignored(self):
        file = make_file("01.flac", 200, recordings={"rec-1": 0.2})
        track = make_track("rec-1", 1, "Song", 200)
        score, fp = score_pair(file, track, CONFIG)
        assert fp is False

    def test_duration_and_title_fallback(self):
        file = make_file("01.flac", 200, title="My Song", track=1)
        track = make_track("rec-1", 1, "My Song", 201)
        score, fp = score_pair(file, track, CONFIG)
        assert fp is False
        assert score > 0.85  # near-exact duration + exact title + position

    def test_bad_duration_scores_low(self):
        file = make_file("01.flac", 100)
        track = make_track("rec-1", 1, "Song", 300)
        score, _ = score_pair(file, track, CONFIG)
        assert score < 0.25


class TestAssignTracks:
    def test_one_to_one_assignment(self):
        files = [
            make_file("a.flac", 100, recordings={"rec-1": 0.9}),
            make_file("b.flac", 200, recordings={"rec-2": 0.9}),
        ]
        release = make_release("rel-1", [
            make_track("rec-1", 1, "One", 100),
            make_track("rec-2", 2, "Two", 200),
        ])
        match = assign_tracks(files, release, CONFIG)
        assert len(match.pairs) == 2
        assert not match.unmatched_files
        assert match.pairs[0].track.recording_id == "rec-1"
        assert match.pairs[0].file.path.name == "a.flac"

    def test_extra_file_left_unmatched(self):
        files = [
            make_file("a.flac", 100, recordings={"rec-1": 0.9}),
            make_file("junk.flac", 999),
        ]
        release = make_release("rel-1", [make_track("rec-1", 1, "One", 100)])
        match = assign_tracks(files, release, CONFIG)
        assert len(match.pairs) == 1
        assert [f.path.name for f in match.unmatched_files] == ["junk.flac"]


class TestChooseRelease:
    def _files(self):
        return [
            make_file("01.flac", 180, recordings={"rec-1": 0.95}, title="One", track=1),
            make_file("02.flac", 210, recordings={"rec-2": 0.95}, title="Two", track=2),
            make_file("03.flac", 240, recordings={"rec-3": 0.95}, title="Three", track=3),
        ]

    def _release(self, release_id="rel-1", group="rg-1"):
        return make_release(release_id, [
            make_track("rec-1", 1, "One", 180),
            make_track("rec-2", 2, "Two", 210),
            make_track("rec-3", 3, "Three", 240),
        ], group=group)

    def test_picks_matching_release(self):
        wrong = make_release("rel-2", [
            make_track("rec-9", 1, "Other", 100),
            make_track("rec-8", 2, "Stuff", 120),
        ], group="rg-2")
        match, report = choose_release(self._files(), [wrong, self._release()], CONFIG)
        assert match is not None
        assert match.release.id == "rel-1"
        assert report.reason == "matched"

    def test_refuses_when_no_candidates(self):
        match, report = choose_release(self._files(), [], CONFIG)
        assert match is None
        assert "no candidate" in report.reason

    def test_refuses_low_confidence(self):
        wrong = make_release("rel-2", [make_track("rec-9", 1, "Other", 100)])
        match, report = choose_release(self._files(), [wrong], CONFIG)
        assert match is None
        assert "below min_release_confidence" in report.reason or "unmatched" in report.reason

    def test_refuses_ambiguous_different_groups(self):
        # Two identical-scoring releases from different release groups
        a = self._release("rel-a", group="rg-a")
        b = self._release("rel-b", group="rg-b")
        match, report = choose_release(self._files(), [a, b], CONFIG)
        assert match is None
        assert "ambiguous" in report.reason

    def test_allows_close_scores_within_same_group(self):
        a = self._release("rel-a", group="rg-1")
        b = self._release("rel-b", group="rg-1")
        match, report = choose_release(self._files(), [a, b], CONFIG)
        assert match is not None

    def test_refuses_unmatched_files_when_required(self):
        files = self._files() + [make_file("bonus.flac", 999)]
        match, report = choose_release(files, [self._release()], CONFIG)
        assert match is None
        assert "unmatched files" in report.reason

    def test_accepts_partial_when_not_required(self):
        config = MatchingConfig(require_all_files_matched=False,
                                min_release_confidence=0.5)
        files = self._files() + [make_file("bonus.flac", 999)]
        match, _ = choose_release(files, [self._release()], config)
        assert match is not None
        assert len(match.unmatched_files) == 1

    def test_report_lists_candidates(self):
        match, report = choose_release(self._files(), [self._release()], CONFIG)
        assert len(report.candidates) == 1
        assert report.candidates[0]["fingerprint_hits"] == 3
