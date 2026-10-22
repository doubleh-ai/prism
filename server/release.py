"""Release metadata helpers for Prism."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

APP_VERSION = "v0.19.8-recovery"
ROOT_DIR = Path(__file__).resolve().parent.parent
CHANGELOG_PATH = ROOT_DIR / "CHANGELOG.md"
DEPLOY_REVISION_PATH = ROOT_DIR / ".deploy-revision"

_HEADING_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?: - (?P<date>\d{4}-\d{2}-\d{2}))?\s*$")
_SECTION_RE = re.compile(r"^### (?P<title>.+?)\s*$")


def _read_deployed_revision() -> str:
    if not DEPLOY_REVISION_PATH.exists():
        return ""
    try:
        return DEPLOY_REVISION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _append_section(release: dict[str, Any], title: str, lines: list[str]) -> None:
    bullets = []
    for line in lines:
        item = line.strip()
        if item.startswith("- "):
            bullets.append(item[2:].strip())
    if bullets:
        release.setdefault("sections", []).append({"title": title, "items": bullets})


def parse_changelog(limit: int = 8) -> list[dict[str, Any]]:
    """Parse top release entries from CHANGELOG.md into API-friendly data."""
    if not CHANGELOG_PATH.exists():
        return []
    try:
        lines = CHANGELOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    releases: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section_title = ""
    section_lines: list[str] = []

    for line in lines:
        heading = _HEADING_RE.match(line)
        if heading:
            if current:
                _append_section(current, section_title, section_lines)
                releases.append(current)
                if len(releases) >= limit:
                    break
            current = {
                "version": heading.group("version"),
                "date": heading.group("date") or "",
                "sections": [],
            }
            section_title = ""
            section_lines = []
            continue

        if not current:
            continue

        section = _SECTION_RE.match(line)
        if section:
            _append_section(current, section_title, section_lines)
            section_title = section.group("title")
            section_lines = []
            continue

        section_lines.append(line)

    if current and len(releases) < limit:
        _append_section(current, section_title, section_lines)
        releases.append(current)

    return releases


def release_payload() -> dict[str, Any]:
    releases = parse_changelog()
    latest = releases[0] if releases else {"version": APP_VERSION, "date": "", "sections": []}
    return {
        "version": APP_VERSION,
        "deployed_revision": _read_deployed_revision(),
        "latest": latest,
        "releases": releases,
    }
