from printarr.lidarr import LidarrClient, candidates_from_album


def make_client(monkeypatch, responses):
    """LidarrClient whose _request is served from a canned {(method, path): value} map."""
    client = LidarrClient("http://lidarr:8686", "key")
    calls = []

    def fake_request(method, path, params=None, json_body=None):
        calls.append((method, path, params, json_body))
        value = responses[(method, path)]
        return value(params) if callable(value) else value

    monkeypatch.setattr(client, "_request", fake_request)
    client._calls = calls
    return client


class TestIsStuck:
    def test_import_blocked(self):
        assert LidarrClient.is_stuck({
            "status": "completed", "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importBlocked"})

    def test_import_failed(self):
        assert LidarrClient.is_stuck({"trackedDownloadState": "importFailed",
                                      "trackedDownloadStatus": "ok"})

    def test_import_pending_ok_is_transient(self):
        assert not LidarrClient.is_stuck({
            "trackedDownloadState": "importPending", "trackedDownloadStatus": "ok"})

    def test_import_pending_warning_is_stuck(self):
        assert LidarrClient.is_stuck({
            "trackedDownloadState": "importPending",
            "trackedDownloadStatus": "warning"})

    def test_downloading_not_stuck(self):
        assert not LidarrClient.is_stuck({
            "trackedDownloadState": "downloading", "trackedDownloadStatus": "ok"})


class TestQueue:
    def test_pagination(self, monkeypatch):
        def queue_page(params):
            page = params["page"]
            records = [{"id": i} for i in range((page - 1) * 50, min(page * 50, 60))]
            return {"totalRecords": 60, "records": records}

        client = make_client(monkeypatch, {("GET", "queue"): queue_page})
        records = client.queue()
        assert len(records) == 60


ALBUM = {
    "id": 7,
    "artistId": 3,
    "title": "Test Album",
    "foreignAlbumId": "rg-mbid",
    "releaseDate": "2002-02-18T00:00:00Z",
    "artist": {"artistName": "Test Artist"},
    "releases": [
        {"id": 100, "foreignReleaseId": "rel-a", "title": "Test Album",
         "status": "Official", "trackCount": 2, "monitored": True,
         "country": ["GB"], "label": ["Warp"],
         "media": [{"mediumNumber": 1, "mediumFormat": "CD"}]},
        {"id": 101, "foreignReleaseId": "rel-b", "title": "Test Album (Deluxe)",
         "status": "Official", "trackCount": 30, "monitored": False,
         "country": ["US"], "label": [],
         "media": [{"mediumNumber": 1, "mediumFormat": "CD"}]},
    ],
}

TRACKS_A = [
    {"id": 900, "foreignTrackId": "track-1", "foreignRecordingId": "recording-1",
     "title": "One", "trackNumber": "1", "absoluteTrackNumber": 1,
     "mediumNumber": 1, "duration": 180000},
    {"id": 901, "foreignTrackId": "track-2", "foreignRecordingId": "recording-2",
     "title": "Two", "trackNumber": "2", "absoluteTrackNumber": 2,
     "mediumNumber": 1, "duration": 200000},
]


class TestCandidatesFromAlbum:
    def test_builds_candidates(self, monkeypatch):
        client = make_client(monkeypatch, {
            ("GET", "track"): lambda params: (
                TRACKS_A if params["albumReleaseId"] == 100 else []),
        })
        candidates = candidates_from_album(client, ALBUM, file_count=2,
                                           max_candidates=1)
        # trackCount fit puts rel-a (2 tracks) before rel-b (30 tracks)
        assert len(candidates) == 1
        cand = candidates[0]
        assert cand.release.id == "rel-a"
        assert cand.db_release_id == 100
        assert cand.db_album_id == 7
        assert cand.db_artist_id == 3
        assert cand.db_track_ids == {"track-1": 900, "track-2": 901}
        assert cand.release.artist == "Test Artist"
        assert cand.release.release_group_id == "rg-mbid"
        assert cand.release.date == "2002-02-18"
        assert [t.recording_id for t in cand.release.tracks] == [
            "recording-1", "recording-2"]
        assert cand.release.tracks[0].length == 180.0

    def test_all_releases_without_file_count(self, monkeypatch):
        client = make_client(monkeypatch, {
            ("GET", "track"): lambda params: TRACKS_A,
        })
        candidates = candidates_from_album(client, ALBUM)
        assert len(candidates) == 2
