from mutagen.flac import FLAC

from printarr.audiofile import parse_filename_numbers, read_audio_file, scan_folder


def test_parse_filename_numbers_plain():
    assert parse_filename_numbers("02 - Song Title") == (None, 2)


def test_parse_filename_numbers_disc_track():
    assert parse_filename_numbers("1-02 - Song Title") == (1, 2)
    assert parse_filename_numbers("2.11 Song") == (2, 11)


def test_parse_filename_numbers_none():
    assert parse_filename_numbers("Song Title") == (None, None)


def test_parse_filename_numbers_year_not_track():
    assert parse_filename_numbers("2001 A Space Odyssey") == (None, None)


def test_read_audio_file(audio_factory, tmp_path):
    path = audio_factory("03 - Something.flac", dest_dir=tmp_path)
    audio = FLAC(path)
    audio["TITLE"] = ["Something"]
    audio["ARTIST"] = ["Somebody"]
    audio["TRACKNUMBER"] = ["3/10"]
    audio.save()

    result = read_audio_file(path)
    assert result is not None
    assert result.tag_title == "Something"
    assert result.tag_artist == "Somebody"
    assert result.tag_track == 3
    assert result.filename_track == 3
    assert result.duration > 4
    assert result.track_hint == 3


def test_scan_folder_orders_naturally(audio_factory, tmp_path):
    album = tmp_path / "album"
    audio_factory("10 - Ten.flac", dest_dir=album)
    audio_factory("2 - Two.flac", dest_dir=album)
    audio_factory("1 - One.flac", dest_dir=album)
    files = scan_folder(album)
    assert [f.path.name for f in files] == [
        "1 - One.flac", "2 - Two.flac", "10 - Ten.flac"]


def test_scan_folder_skips_junk(audio_factory, tmp_path):
    album = tmp_path / "album2"
    audio_factory("01 - Song.flac", dest_dir=album)
    (album / "cover.jpg").write_bytes(b"jpeg" * 100000)
    (album / "tiny.mp3").write_bytes(b"junk")  # below the size threshold
    files = scan_folder(album)
    assert [f.path.name for f in files] == ["01 - Song.flac"]


def test_scan_folder_recurses(audio_factory, tmp_path):
    album = tmp_path / "album3"
    audio_factory("01 - A.flac", dest_dir=album / "CD1")
    audio_factory("01 - B.flac", dest_dir=album / "CD2")
    files = scan_folder(album)
    assert len(files) == 2
