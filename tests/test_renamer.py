import pytest

from printarr.renamer import (
    RenamePlan,
    TemplateError,
    apply_renames,
    format_template,
    sanitize_component,
    unique_target,
)


def test_sanitize_strips_illegal_characters():
    assert sanitize_component('AC/DC: "Back" in <Black>?') == "ACDC Back in Black"


def test_sanitize_trailing_dots_and_spaces():
    assert sanitize_component("  Album Vol. 1.  ") == "Album Vol. 1"


def test_sanitize_never_empty():
    assert sanitize_component("???") == "_"


def test_sanitize_length_capped():
    assert len(sanitize_component("x" * 500)) <= 180


def test_format_template_track_padding():
    result = format_template("{track:02d} - {title}", {"track": 3, "title": "Song"})
    assert result == "03 - Song"


def test_format_template_sanitizes_values():
    result = format_template("{track:02d} - {title}",
                             {"track": 1, "title": "A/B: C*"})
    assert result == "01 - AB C"


def test_format_template_unknown_field():
    with pytest.raises(TemplateError, match="unknown template field"):
        format_template("{nope}", {"track": 1})


def test_unique_target_appends_counter(tmp_path):
    existing = tmp_path / "01 - Song.flac"
    existing.touch()
    target = unique_target(existing, set())
    assert target.name == "01 - Song (1).flac"


def test_unique_target_respects_taken_set(tmp_path):
    target = tmp_path / "01 - Song.flac"
    result = unique_target(target, {target})
    assert result.name == "01 - Song (1).flac"


def test_apply_renames(tmp_path):
    source = tmp_path / "old.flac"
    source.write_bytes(b"x")
    plan = RenamePlan(source, tmp_path / "new.flac")
    applied = apply_renames([plan])
    assert applied == [plan]
    assert not source.exists()
    assert (tmp_path / "new.flac").exists()


def test_apply_renames_dry_run(tmp_path):
    source = tmp_path / "old.flac"
    source.write_bytes(b"x")
    apply_renames([RenamePlan(source, tmp_path / "new.flac")], dry_run=True)
    assert source.exists()
    assert not (tmp_path / "new.flac").exists()


def test_apply_renames_skips_noop(tmp_path):
    source = tmp_path / "same.flac"
    source.write_bytes(b"x")
    applied = apply_renames([RenamePlan(source, source)])
    assert applied == []
