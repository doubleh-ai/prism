"""Guardrails: the retired Vibe feature must stay removed.

The historical ``vibe_plans`` table is deliberately retained for account
history and database compatibility, but no runtime code may read, write or
expose those rows any more.
"""

from pathlib import Path

import pytest

from server import repair_scopes
from server.work_lifecycle import DURABLE_KINDS, KINDS

ROOT = Path(__file__).resolve().parents[1]

REMOVED_GET_PATHS = [
    "/api/vibe/engine",
    "/api/vibe/plans",
    "/api/vibe/plans/saved",
]
REMOVED_POST_PATHS = [
    "/api/vibe/plan",
    "/api/vibe/plans",
    "/api/vibe/plans/saved/cancel",
    "/api/vibe/plans/saved/follow-up",
    "/api/vibe/plans/saved/save-report",
]
REMOVED_DELETE_PATHS = ["/api/vibe/plans/saved"]

REMOVED_SOURCE_FILES = (
    "server/vibe.py",
    "server/vibe_engine.py",
    "static/js/app.vibe.js",
    "tests/vibe_history.py",
)
SHIPPED_TEXT_SUFFIXES = {".html", ".js", ".css"}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", REMOVED_GET_PATHS)
async def test_retired_vibe_get_routes_return_not_found(client, auth_cookies, path):
    response = await client.get(path, cookies=auth_cookies)
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", REMOVED_POST_PATHS)
async def test_retired_vibe_post_routes_return_not_found(client, auth_cookies, path):
    response = await client.post(path, cookies=auth_cookies, json={})
    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", REMOVED_DELETE_PATHS)
async def test_retired_vibe_delete_routes_return_not_found(client, auth_cookies, path):
    response = await client.delete(path, cookies=auth_cookies)
    assert response.status_code == 404


def test_fastapi_app_registers_no_vibe_routes():
    from server.main import app

    paths = [getattr(route, "path", "") for route in app.routes]
    assert not [path for path in paths if "vibe" in path.lower()]


def test_removed_vibe_source_files_are_gone():
    for relative in REMOVED_SOURCE_FILES:
        assert not (ROOT / relative).exists(), relative


def test_no_shipped_asset_or_template_references_vibe():
    candidates = list((ROOT / "templates").rglob("*.html")) + list((ROOT / "static").rglob("*"))
    offenders = []
    for path in candidates:
        if not path.is_file() or path.suffix not in SHIPPED_TEXT_SUFFIXES:
            continue
        if "vibe" in path.read_text(encoding="utf-8").lower():
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_server_code_keeps_only_the_retained_history_schema():
    offenders = []
    for path in (ROOT / "server").rglob("*.py"):
        if path.name == "database.py":
            continue
        if "vibe" in path.read_text(encoding="utf-8").lower():
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_client_config_has_no_vibe_engine_settings():
    config = (ROOT / "server/config.py").read_text(encoding="utf-8")
    assert "VIBE" not in config
    assert "vibe" not in config.lower()


def test_capability_catalog_does_not_advertise_vibe_tools():
    from server import capabilities

    catalog = capabilities.capability_catalog()
    assert catalog
    for item in catalog:
        for key in ("id", "view", "target_view"):
            assert "vibe" not in item[key].lower(), item
    assert "vibe_status" not in capabilities.REGISTRY


@pytest.mark.asyncio
async def test_platform_products_do_not_advertise_vibe(client, auth_cookies):
    assert (await client.get("/api/platform", cookies=auth_cookies)).status_code == 404
    prism = await client.get("/prism", cookies=auth_cookies)
    assert prism.status_code == 200
    assert "vibe" not in prism.text.lower()


def test_retired_vibe_repair_scope_degrades_safely():
    assert "deep-research" not in repair_scopes.SCOPE_FEATURES
    assert "vibe" not in repair_scopes.SCOPE_FEATURES.values()
    assert repair_scopes.normalize_scope("deep-research") == "unknown"
    assert repair_scopes.signal_scope("deep-research", feature="vibe") == {}


def test_work_lifecycle_does_not_declare_a_vibe_kind():
    assert "vibe" not in KINDS
    assert "vibe" not in DURABLE_KINDS


def test_vibe_plans_history_table_is_retained_but_unused():
    schema = (ROOT / "server/database.py").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS vibe_plans" in schema
    assert "Historical table retained deliberately" in schema
    from server.database import get_db

    with get_db() as db:
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='vibe_plans'"
        ).fetchone()
    assert table is not None
