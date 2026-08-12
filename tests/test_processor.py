"""End-to-end: real audio files on disk, faked AcoustID/MusicBrainz backends."""

from mutagen.flac import FLAC

from printarr.config import Config
from printarr.lidarr import LidarrCandidate
from printarr.musicbrainz import MBRelease, MBTrack
from printarr.processor import Processor

REL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RG_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def make_release(track_durations, release_id=REL_ID, group=RG_ID,
                 title="Real Album"):
    tracks = [
        MBTrack(id=f"track-{i}", recording_id=f"recording-{i}",
                title=f"Song Number {i}", position=i, medium=1,
                length=duration, artist="Real Artist", absolute_position=i)
        for i, duration in enumerate(track_durations, start=1)
    ]
    return MBRelease(id=release_id, title=title, artist="Real Artist",
                     artist_ids=["cccccccc-cccc-cccc-cccc-cccccccccccc"],
                     release_group_id=group, date="2003-05-01",
                     status="Official", media_formats=["CD"], tracks=tracks,
                     has_front_cover=False)


class FakeMB:
    """Duck-typed MusicBrainzClient replacement."""

    def __init__(self, releases):
        self.releases = {r.id: r for r in releases}
        self.release_calls = []

    def release(self, mbid):
        self.release_calls.append(mbid)
        from printarr.musicbrainz import MusicBrainzError
        if mbid not in self.releases:
            raise MusicBrainzError(f"not found: {mbid}")
        return self.releases[mbid]

    def release_group_releases(self, rg_mbid):
        return [{"id": r.id, "status": r.status, "date": r.date,
                 "track-count": len(r.tracks)}
                for r in self.releases.values() if r.release_group_id == rg_mbid]

    def search_releases(self, artist, release, track_count=None, limit=10):
        return [{"id": r.id, "status": r.status, "date": r.date,
                 "track-count": len(r.tracks)} for r in self.releases.values()]

    def front_cover(self, release_mbid, release_group_mbid="", size=500):
        return None


class FakeAcoustid:
    """Maps files to recordings by rounded duration (files have distinct lengths)."""

    def __init__(self, by_duration, release_ids):
        self.by_duration = by_duration
        self.release_ids = release_ids

    def lookup(self, duration, fingerprint):
        recording = self.by_duration.get(round(duration))
        if recording is None:
            return {}, set()
        return {recording: 0.95}, set(self.release_ids)


def build_album(audio_factory, tmp_path, names_durations):
    album = tmp_path / "Some.Release-GRP"
    for name, seconds in names_durations:
        audio_factory(name, seconds=seconds, dest_dir=album)
    return album


def make_processor(mb, acoustid, tmp_path, **overrides):
    config = Config()
    config.general.state_file = str(tmp_path / "state.json")
    config.tagging.write_cover_art = False
    for key, value in overrides.items():
        section, option = key.split("__")
        setattr(getattr(config, section), option, value)
    processor = Processor(config, mb, acoustid)
    if acoustid is None:
        processor.acoustid = None
    return processor


def test_blind_identification_end_to_end(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path,
                        [("trk_a.flac", 5.0), ("trk_b.flac", 7.0), ("trk_c.flac", 9.0)])
    release = make_release([5.0, 7.0, 9.0])
    decoy = make_release([100.0, 200.0, 300.0],
                         release_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
                         group="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
                         title="Wrong Album")
    mb = FakeMB([release, decoy])
    acoustid = FakeAcoustid(
        {5: "recording-1", 7: "recording-2", 9: "recording-3"},
        [REL_ID],
    )
    processor = make_processor(mb, acoustid, tmp_path)

    result = processor.process_folder(album)

    assert result.success, result.reason
    assert result.release.id == REL_ID
    # Files were renamed to the configured template
    names = sorted(p.name for p in album.iterdir())
    assert names == ["01 - Song Number 1.flac", "02 - Song Number 2.flac",
                     "03 - Song Number 3.flac"]
    # Tags carry the MusicBrainz IDs Lidarr matches on
    audio = FLAC(album / "01 - Song Number 1.flac")
    assert audio["MUSICBRAINZ_ALBUMID"] == [REL_ID]
    assert audio["MUSICBRAINZ_TRACKID"] == ["recording-1"]
    assert audio["ALBUM"] == ["Real Album"]
    assert audio["ARTIST"] == ["Real Artist"]


def test_release_group_hint_path(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path,
                        [("a.flac", 5.0), ("b.flac", 7.0)])
    release = make_release([5.0, 7.0])
    mb = FakeMB([release])
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path)

    result = processor.process_folder(album, release_group_hint=RG_ID)
    assert result.success, result.reason
    assert result.release.id == REL_ID


def test_refuses_junk_folder(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path, [("x.flac", 5.0)])
    release = make_release([100.0, 200.0])
    mb = FakeMB([release])
    processor = make_processor(mb, None, tmp_path)

    result = processor.process_folder(album)
    assert not result.success
    # A namer-style report documents what was considered
    report = album / "printarr-report.json"
    assert report.exists()


def test_dry_run_changes_nothing(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path,
                        [("a.flac", 5.0), ("b.flac", 7.0)])
    release = make_release([5.0, 7.0])
    mb = FakeMB([release])
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path, general__dry_run=True)

    before = sorted(p.name for p in album.iterdir())
    result = processor.process_folder(album)
    assert result.success
    assert sorted(p.name for p in album.iterdir()) == before
    audio = FLAC(album / "a.flac")
    assert "MUSICBRAINZ_ALBUMID" not in audio


def test_lidarr_candidates_skip_mb_search(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path,
                        [("a.flac", 5.0), ("b.flac", 7.0)])
    release = make_release([5.0, 7.0])
    mb = FakeMB([release])  # enrichment source
    acoustid = FakeAcoustid({5: "recording-1", 7: "recording-2"}, [REL_ID])
    processor = make_processor(mb, acoustid, tmp_path)

    candidate = LidarrCandidate(
        release=make_release([5.0, 7.0]),
        db_release_id=100, db_album_id=7, db_artist_id=3,
        db_track_ids={"track-1": 900, "track-2": 901},
    )
    result = processor.process_folder(album, lidarr_candidates=[candidate])
    assert result.success, result.reason
    assert result.lidarr_candidate is candidate
    # Enrichment fetched the full MB release exactly once
    assert mb.release_calls == [REL_ID]


def test_tag_fallback_without_fingerprints(audio_factory, tmp_path):
    album = build_album(audio_factory, tmp_path,
                        [("01 - Song Number 1.flac", 5.0),
                         ("02 - Song Number 2.flac", 7.0)])
    for i, name in enumerate(["01 - Song Number 1.flac", "02 - Song Number 2.flac"],
                             start=1):
        audio = FLAC(album / name)
        audio["ALBUM"] = ["Real Album"]
        audio["ARTIST"] = ["Real Artist"]
        audio["TITLE"] = [f"Song Number {i}"]
        audio["TRACKNUMBER"] = [str(i)]
        audio.save()

    release = make_release([5.0, 7.0])
    mb = FakeMB([release])
    processor = make_processor(mb, None, tmp_path,
                               matching__min_release_confidence=0.5)

    result = processor.process_folder(album)
    assert result.success, result.reason
    assert result.release.id == REL_ID
