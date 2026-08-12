import mutagen
import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from printarr.config import TaggingConfig
from printarr.musicbrainz import MBRelease, MBTrack
from printarr.tagger import build_track_tags, write_tags

RELEASE = MBRelease(
    id="11111111-1111-1111-1111-111111111111",
    title="Test Album",
    artist="Test Artist",
    artist_ids=["22222222-2222-2222-2222-222222222222"],
    artist_sort="Artist, Test",
    release_group_id="33333333-3333-3333-3333-333333333333",
    release_group_type="Album",
    status="Official",
    date="2001-03-24",
    country="DE",
    label="Test Label",
    catalog_number="CAT-001",
    barcode="1234567890123",
    media_formats=["CD"],
    tracks=[
        MBTrack(id="44444444-4444-4444-4444-444444444444",
                recording_id="55555555-5555-5555-5555-555555555555",
                title="First Song", position=1, medium=1, length=180.0,
                artist="Test Artist", absolute_position=1),
        MBTrack(id="66666666-6666-6666-6666-666666666666",
                recording_id="77777777-7777-7777-7777-777777777777",
                title="Second Song", position=2, medium=1, length=200.0,
                artist="Test Artist", absolute_position=2),
    ],
)

CONFIG = TaggingConfig(write_cover_art=False)


def test_build_track_tags():
    tags = build_track_tags(RELEASE, RELEASE.tracks[0])
    assert tags.title == "First Song"
    assert tags.album == "Test Album"
    assert tags.track == 1
    assert tags.track_total == 2
    assert tags.disc == 1
    assert tags.disc_total == 1
    assert tags.year == "2001"
    assert tags.media == "CD"
    assert tags.mb_release_id == RELEASE.id
    assert tags.mb_recording_id == RELEASE.tracks[0].recording_id


def test_write_flac(audio_factory, tmp_path):
    path = audio_factory("song.flac", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), CONFIG)
    audio = FLAC(path)
    assert audio["TITLE"] == ["First Song"]
    assert audio["ALBUMARTIST"] == ["Test Artist"]
    assert audio["TRACKNUMBER"] == ["1"]
    assert audio["TRACKTOTAL"] == ["2"]
    assert audio["MUSICBRAINZ_ALBUMID"] == [RELEASE.id]
    assert audio["MUSICBRAINZ_TRACKID"] == [RELEASE.tracks[0].recording_id]
    assert audio["MUSICBRAINZ_RELEASETRACKID"] == [RELEASE.tracks[0].id]
    assert audio["MUSICBRAINZ_RELEASEGROUPID"] == [RELEASE.release_group_id]
    assert audio["CATALOGNUMBER"] == ["CAT-001"]
    assert audio["DATE"] == ["2001-03-24"]


def test_write_flac_preserves_unrelated_tags(audio_factory, tmp_path):
    path = audio_factory("song2.flac", dest_dir=tmp_path)
    audio = FLAC(path)
    audio["REPLAYGAIN_TRACK_GAIN"] = ["-6.5 dB"]
    audio.save()
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), CONFIG)
    audio = FLAC(path)
    assert audio["REPLAYGAIN_TRACK_GAIN"] == ["-6.5 dB"]
    assert audio["TITLE"] == ["First Song"]


def test_write_flac_clear_existing(audio_factory, tmp_path):
    path = audio_factory("song3.flac", dest_dir=tmp_path)
    audio = FLAC(path)
    audio["REPLAYGAIN_TRACK_GAIN"] = ["-6.5 dB"]
    audio.save()
    config = TaggingConfig(write_cover_art=False, clear_existing_tags=True)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), config)
    audio = FLAC(path)
    assert "REPLAYGAIN_TRACK_GAIN" not in audio
    assert audio["TITLE"] == ["First Song"]


def test_write_mp3(audio_factory, tmp_path):
    path = audio_factory("song.mp3", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[1]), CONFIG)
    id3 = ID3(path)
    assert id3["TIT2"].text == ["Second Song"]
    assert id3["TRCK"].text == ["2/2"]
    assert id3["TPOS"].text == ["1/1"]
    assert id3["TXXX:MusicBrainz Album Id"].text == [RELEASE.id]
    assert id3["TXXX:MusicBrainz Release Track Id"].text == [RELEASE.tracks[1].id]
    ufid = id3["UFID:http://musicbrainz.org"]
    assert ufid.data == RELEASE.tracks[1].recording_id.encode("ascii")
    assert id3["TPUB"].text == ["Test Label"]


def test_write_mp4(audio_factory, tmp_path):
    path = audio_factory("song.m4a", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), CONFIG)
    audio = MP4(path)
    assert audio["\xa9nam"] == ["First Song"]
    assert audio["trkn"] == [(1, 2)]
    assert audio["disk"] == [(1, 1)]
    freeform = audio["----:com.apple.iTunes:MusicBrainz Album Id"]
    assert bytes(freeform[0]).decode() == RELEASE.id
    recording = audio["----:com.apple.iTunes:MusicBrainz Track Id"]
    assert bytes(recording[0]).decode() == RELEASE.tracks[0].recording_id


@pytest.mark.parametrize("ext", ["ogg", "opus"])
def test_write_vorbis_family(audio_factory, tmp_path, ext):
    path = audio_factory(f"song.{ext}", dest_dir=tmp_path)
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), CONFIG)
    audio = mutagen.File(path)
    assert audio["TITLE"] == ["First Song"]
    assert audio["MUSICBRAINZ_ALBUMID"] == [RELEASE.id]
    assert audio["MUSICBRAINZ_TRACKID"] == [RELEASE.tracks[0].recording_id]


def test_write_cover_art_flac(audio_factory, tmp_path):
    path = audio_factory("cover.flac", dest_dir=tmp_path)
    config = TaggingConfig(write_cover_art=True)
    fake_jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    write_tags(path, build_track_tags(RELEASE, RELEASE.tracks[0]), config,
               cover=fake_jpeg, cover_mime="image/jpeg")
    audio = FLAC(path)
    assert audio.pictures
    assert audio.pictures[0].data == fake_jpeg
    assert audio.pictures[0].type == 3


def test_multi_disc_totals():
    release = MBRelease(
        id="r", title="A", artist="B", media_formats=["CD", "CD"],
        tracks=[
            MBTrack(id="t1", recording_id="r1", title="One", position=1,
                    medium=1, length=100, absolute_position=1),
            MBTrack(id="t2", recording_id="r2", title="Two", position=1,
                    medium=2, length=100, absolute_position=2),
        ])
    tags = build_track_tags(release, release.tracks[1])
    assert tags.disc == 2
    assert tags.disc_total == 2
    assert tags.track == 1
    assert tags.track_total == 1  # one track on disc 2
    assert tags.media == "CD"
