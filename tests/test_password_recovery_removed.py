"""Retired email password recovery stays gone: no routes, assets or login entry.

The feature was removed in v0.26 rather than disabled, so these checks guard the
removal against accidental reintroduction and against dead links in the UI.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

REMOVED_GET_PATHS = ("/recover", "/api/auth/recovery/status", "/api/admin/recovery")
REMOVED_POST_PATHS = ("/api/auth/recovery/request", "/api/auth/recovery/reset")


@pytest.mark.parametrize("path", REMOVED_GET_PATHS)
async def test_retired_password_recovery_reads_are_not_served(client, path):
    assert (await client.get(path)).status_code == 404


@pytest.mark.parametrize("path", REMOVED_POST_PATHS)
async def test_retired_password_recovery_writes_are_not_served(client, path):
    assert (await client.post(path, json={})).status_code == 404


async def test_login_page_has_no_retired_password_recovery_entry(client):
    login = await client.get("/login")
    assert login.status_code == 200
    assert "/recover" not in login.text
    assert "password-recovery.js" not in login.text


def test_retired_password_recovery_assets_are_deleted():
    assert not (ROOT / "templates/recover.html").exists()
    assert not (ROOT / "static/js/password-recovery.js").exists()
