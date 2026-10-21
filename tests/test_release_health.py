"""Safe release observations; no CLI, network provider or production is invoked."""

import io
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from server import health


@pytest.fixture
def basic_health(monkeypatch):
    monkeypatch.setattr(health, "_check_db", lambda: {"status": "ok", "latency_ms": 1})
    monkeypatch.setattr(
        health, "release_payload", lambda: {"version": "v0.19.7", "deployed_revision": "a" * 40}
    )
    monkeypatch.setattr(health.time, "time", lambda: 1792540800)

    def forbidden():
        raise AssertionError("Public readiness must not invoke CLI, skills or model checks")

    for name in ("_check_claude_cli", "_skill_checks", "_model_checks"):
        monkeypatch.setattr(health, name, forbidden)


def test_readiness_has_only_safe_fields_and_does_not_call_external_checks(basic_health):
    assert health.readiness_payload() == {
        "status": "ready",
        "version": "v0.19.7",
        "deployed_revision": "a" * 40,
        "checked_at": 1792540800,
        "tz": "Asia/Shanghai",
    }


@pytest.mark.parametrize(
    "revision", [None, "", "abc1234", "g" * 40, "a" * 40 + "\nextra", {"unexpected": "value"}]
)
def test_unknown_revision_is_not_invented_or_treated_as_release_acceptance(
    basic_health, monkeypatch, revision
):
    monkeypatch.setattr(
        health,
        "release_payload",
        lambda: {"version": None, "deployed_revision": revision, "private_path": "/not/public"},
    )
    payload = health.readiness_payload()
    assert payload["status"] == "ready"  # Only the basic DB check passed, not a release gate.
    assert payload["version"] == payload["deployed_revision"] == "unknown"
    assert "private_path" not in payload


def test_full_revision_is_normalized_and_db_failure_stays_unready(basic_health, monkeypatch):
    monkeypatch.setattr(
        health, "release_payload", lambda: {"version": "v0.19.7", "deployed_revision": "A" * 40}
    )
    monkeypatch.setattr(health, "_check_db", lambda: {"status": "error", "error": "private detail"})
    result = health.readiness_payload()
    assert result["status"] == "unready"
    assert result["deployed_revision"] == "a" * 40
    assert "private detail" not in json.dumps(result)


def test_unreadable_metadata_does_not_disclose_details_or_override_db_status(basic_health, monkeypatch):
    def broken_metadata():
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private metadata detail")

    monkeypatch.setattr(health, "release_payload", broken_metadata)
    payload = health.readiness_payload()
    assert payload["status"] == "ready"
    assert payload["version"] == payload["deployed_revision"] == "unknown"
    assert "private metadata detail" not in json.dumps(payload)


def test_db_probe_only_selects_one(monkeypatch):
    statements = []

    class DB:
        def execute(self, sql):
            statements.append(sql)
            return self

        def fetchone(self):
            return {"ok": 1}

    @contextmanager
    def database():
        yield DB()

    monkeypatch.setattr(health, "get_db", database)
    assert health._check_db()["status"] == "ok"
    assert statements == ["SELECT 1 AS ok"]


@pytest.mark.parametrize("legacy_present", [False, True])
def test_admin_uses_builtin_skill_and_missing_legacy_is_not_critical(monkeypatch, legacy_present):
    monkeypatch.setattr(health, "release_payload", lambda: {})
    monkeypatch.setattr(health, "_check_db", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "_check_claude_cli", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "_model_checks", lambda: [])
    skills = [
        {"id": "prism-research", "status": "ok", "has_claude_md": True},
        {
            "id": "bottleneck-hunter",
            "status": "ok" if legacy_present else "missing",
            "has_claude_md": legacy_present,
        },
    ]
    monkeypatch.setattr(health, "_skill_checks", lambda: skills)
    assert health.admin_health_payload()["ok"] is True
    skills[0]["has_claude_md"] = False
    assert health.admin_health_payload()["ok"] is False


@pytest.mark.parametrize("status,exit_code", [("ready", 0), ("unready", 1)])
def test_docker_probe_executes_the_public_readiness_contract(monkeypatch, status, exit_code):
    import urllib.request

    root = Path(__file__).resolve().parents[1]
    line = next(line for line in (root / "Dockerfile").read_text().splitlines() if "CMD python -c" in line)
    code = line.split('CMD python -c "', 1)[1].rsplit('"', 1)[0]

    def response(url, timeout):
        assert url == "http://127.0.0.1:8088/health/ready"
        assert timeout == 3
        return io.BytesIO(json.dumps({"status": status}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", response)
    with pytest.raises(SystemExit) as exited:
        exec(code, {})
    assert exited.value.code == exit_code


@pytest.mark.parametrize("db_status,http_status", [("ok", 200), ("error", 503)])
async def test_public_readiness_is_read_only_and_uses_200_or_503(
    client, basic_health, monkeypatch, db_status, http_status
):
    monkeypatch.setattr(health, "_check_db", lambda: {"status": db_status})
    response = await client.get("/health/ready")
    assert response.status_code == http_status
    assert set(response.json()) == {"status", "version", "deployed_revision", "checked_at", "tz"}
