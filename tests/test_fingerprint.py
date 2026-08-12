import pytest

from printarr.fingerprint import AcoustidClient, compute_fingerprint, fpcalc_available

ACOUSTID_RESPONSE = {
    "status": "ok",
    "results": [
        {
            "id": "acoustid-uuid",
            "score": 0.97,
            "recordings": [
                {
                    "id": "rec-1",
                    "releases": [{"id": "rel-1"}, {"id": "rel-2"}],
                },
                {"id": "rec-2"},
            ],
        },
        {"id": "other-uuid", "score": 0.41,
         "recordings": [{"id": "rec-1"}]},
        {"id": "no-recordings", "score": 0.9},
    ],
}


def test_parse_results_scores_and_releases():
    recordings, releases = AcoustidClient._parse_results(ACOUSTID_RESPONSE)
    assert recordings == {"rec-1": 0.97, "rec-2": 0.97}
    assert releases == {"rel-1", "rel-2"}


def test_parse_results_keeps_best_score():
    data = {"status": "ok", "results": [
        {"id": "a", "score": 0.5, "recordings": [{"id": "rec-1"}]},
        {"id": "b", "score": 0.9, "recordings": [{"id": "rec-1"}]},
    ]}
    recordings, _ = AcoustidClient._parse_results(data)
    assert recordings == {"rec-1": 0.9}


def test_parse_results_empty():
    assert AcoustidClient._parse_results({"status": "ok", "results": []}) == ({}, set())


@pytest.mark.skipif(not fpcalc_available(), reason="fpcalc not installed")
def test_compute_fingerprint_real_file(audio_factory, tmp_path):
    path = audio_factory("fp.flac", dest_dir=tmp_path)
    result = compute_fingerprint(path)
    assert result is not None
    duration, fingerprint = result
    assert 4 < duration < 10
    assert len(fingerprint) > 20


def test_compute_fingerprint_bad_file(tmp_path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"not audio at all")
    if not fpcalc_available():
        pytest.skip("fpcalc not installed")
    assert compute_fingerprint(bad) is None
