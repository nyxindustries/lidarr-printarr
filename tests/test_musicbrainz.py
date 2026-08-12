from printarr.musicbrainz import _escape_lucene, parse_release

RELEASE_JSON = {
    "id": "rel-mbid",
    "title": "Geogaddi",
    "status": "Official",
    "date": "2002-02-18",
    "country": "GB",
    "barcode": "5021603094123",
    "artist-credit": [
        {"name": "Boards of Canada", "joinphrase": "",
         "artist": {"id": "artist-mbid", "name": "Boards of Canada",
                    "sort-name": "Boards of Canada"}},
    ],
    "release-group": {"id": "rg-mbid", "primary-type": "Album"},
    "label-info": [
        {"catalog-number": "WARPCD101", "label": {"id": "label-mbid", "name": "Warp"}},
    ],
    "cover-art-archive": {"artwork": True, "front": True, "count": 2},
    "media": [
        {
            "position": 1,
            "format": "CD",
            "track-count": 2,
            "tracks": [
                {"id": "track-1", "position": 1, "number": "1", "title": "Ready Lets Go",
                 "length": 59000,
                 "recording": {"id": "recording-1", "title": "Ready Lets Go",
                               "length": 59000}},
                {"id": "track-2", "position": 2, "number": "2", "title": "Music Is Math",
                 "length": 320000,
                 "recording": {"id": "recording-2", "title": "Music Is Math",
                               "length": 320000}},
            ],
        },
        {
            "position": 2,
            "format": "CD",
            "track-count": 1,
            "tracks": [
                {"id": "track-3", "position": 1, "number": "1", "title": "Bonus",
                 "length": None,
                 "recording": {"id": "recording-3", "title": "Bonus", "length": 100000}},
            ],
        },
    ],
}


def test_parse_release_basics():
    release = parse_release(RELEASE_JSON)
    assert release.id == "rel-mbid"
    assert release.title == "Geogaddi"
    assert release.artist == "Boards of Canada"
    assert release.artist_ids == ["artist-mbid"]
    assert release.artist_sort == "Boards of Canada"
    assert release.release_group_id == "rg-mbid"
    assert release.release_group_type == "Album"
    assert release.label == "Warp"
    assert release.catalog_number == "WARPCD101"
    assert release.year == "2002"
    assert release.has_front_cover is True


def test_parse_release_tracks():
    release = parse_release(RELEASE_JSON)
    assert len(release.tracks) == 3
    assert release.disc_count == 2
    first, second, third = release.tracks
    assert first.recording_id == "recording-1"
    assert first.length == 59.0
    assert first.medium == 1
    assert third.medium == 2
    assert third.position == 1
    assert third.absolute_position == 3
    # length falls back to the recording when the track has none
    assert third.length == 100.0
    assert release.media_formats == ["CD", "CD"]


def test_parse_release_joinphrase():
    data = dict(RELEASE_JSON)
    data["artist-credit"] = [
        {"name": "A", "joinphrase": " & ", "artist": {"id": "1", "name": "A"}},
        {"name": "B", "joinphrase": "", "artist": {"id": "2", "name": "B"}},
    ]
    release = parse_release(data)
    assert release.artist == "A & B"
    assert release.artist_ids == ["1", "2"]


def test_escape_lucene():
    assert _escape_lucene("AC/DC") == "AC\\/DC"
    assert _escape_lucene('say "what"') == 'say \\"what\\"'
    assert _escape_lucene("plain") == "plain"
