"""Unreleased project notes must not masquerade as a deployed release."""

from server import release


def test_unreleased_is_not_latest_and_does_not_consume_release_limit(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Pending diagnostic delivery\n\n"
        "## [v0.23.0] - 2026-11-07\n\n### Added\n- Released platform\n\n"
        "## [v0.22.0] - 2026-11-02\n\n### Fixed\n- Earlier release\n"
    )
    monkeypatch.setattr(release, "CHANGELOG_PATH", changelog)
    releases = release.parse_changelog(limit=1)
    assert releases == [
        {
            "version": "v0.23.0",
            "date": "2026-11-07",
            "sections": [{"title": "Added", "items": ["Released platform"]}],
        }
    ]
    assert release.release_payload()["latest"]["version"] == "v0.23.0"
    assert "Pending diagnostic" not in str(release.release_payload())


def test_only_unreleased_keeps_deployed_version_metadata(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [unreleased]\n### Added\n- Work in progress\n")
    monkeypatch.setattr(release, "CHANGELOG_PATH", changelog)
    assert release.parse_changelog() == []
    assert release.release_payload()["latest"]["version"] == release.APP_VERSION


def test_unreleased_boundary_cannot_append_its_notes_to_a_published_section(tmp_path, monkeypatch):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [v0.23.0] - 2026-11-07\n### Fixed\n- Released\n"
        "## [Unreleased]\n### Fixed\n- Future\n"
        "## [v0.22.0] - 2026-11-02\n### Added\n- Earlier\n"
    )
    monkeypatch.setattr(release, "CHANGELOG_PATH", changelog)
    assert [r["version"] for r in release.parse_changelog()] == ["v0.23.0", "v0.22.0"]
    assert "Future" not in str(release.parse_changelog())
