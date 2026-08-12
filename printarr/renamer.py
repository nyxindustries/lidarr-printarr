"""Filename templating and collision-safe renaming."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from printarr.log import get_logger

log = get_logger(__name__)

# Windows-illegal characters are the strictest superset; strip them everywhere
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_COMPONENT_LENGTH = 180


class TemplateError(Exception):
    pass


def sanitize_component(name: str) -> str:
    """Make a single path component safe on common filesystems."""
    cleaned = _ILLEGAL.sub("", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Trailing dots/spaces break Windows; leading dots hide files
    cleaned = cleaned.strip(". ")
    if len(cleaned) > _MAX_COMPONENT_LENGTH:
        cleaned = cleaned[:_MAX_COMPONENT_LENGTH].rstrip(". ")
    return cleaned or "_"


class _SafeFormatDict(dict):
    def __missing__(self, key):
        raise TemplateError(f"unknown template field {{{key}}}")


def format_template(template: str, values: dict) -> str:
    """Render a filename template; every substituted value is sanitized."""
    safe_values = {
        key: sanitize_component(value) if isinstance(value, str) else value
        for key, value in values.items()
    }
    try:
        rendered = template.format_map(_SafeFormatDict(safe_values))
    except (ValueError, IndexError) as exc:
        raise TemplateError(f"invalid template {template!r}: {exc}") from exc
    return sanitize_component(rendered)


@dataclass
class RenamePlan:
    source: Path
    target: Path

    @property
    def is_noop(self) -> bool:
        return self.source == self.target


def unique_target(target: Path, taken: set[Path],
                  vacated: set[Path] | None = None) -> Path:
    """Avoid overwriting: suffix ' (1)', ' (2)', ... before the extension.

    Paths in `vacated` belong to files that are being renamed away in the same
    batch, so they do not count as collisions (unless already claimed via
    `taken`).
    """
    vacated = vacated or set()

    def free(candidate: Path) -> bool:
        if candidate in taken:
            return False
        return candidate in vacated or not candidate.exists()

    if free(target):
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(1, 100):
        candidate = target.with_name(f"{stem} ({counter}){suffix}")
        if free(candidate):
            return candidate
    raise TemplateError(f"cannot find a free name for {target}")


def apply_renames(plans: list[RenamePlan], dry_run: bool = False) -> list[RenamePlan]:
    """Execute rename plans; returns the plans actually applied.

    When a plan's target is another plan's source (swapped or shifted track
    numbers), renames happen in two phases via temporary names — Path.rename
    would otherwise silently overwrite the not-yet-moved file on POSIX.
    """
    pending = [plan for plan in plans if not plan.is_noop]
    if dry_run:
        for plan in pending:
            log.info("[dry-run] would rename %s -> %s", plan.source, plan.target)
        return pending

    sources = {plan.source for plan in pending}
    overlapping = any(plan.target in sources for plan in pending)

    staged: list[tuple[RenamePlan, Path]] = []
    if overlapping:
        for index, plan in enumerate(pending):
            temp = plan.source.with_name(
                f".printarr-tmp-{index}{plan.source.suffix}")
            plan.source.rename(temp)
            staged.append((plan, temp))
    else:
        staged = [(plan, plan.source) for plan in pending]

    for plan, current in staged:
        plan.target.parent.mkdir(parents=True, exist_ok=True)
        current.rename(plan.target)
        log.info("renamed %s -> %s", plan.source.name, plan.target.name)
    return pending
