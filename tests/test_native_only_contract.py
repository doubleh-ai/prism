"""App entry points cannot opt into or fall back to an external agent executor."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.responses import JSONResponse

from server import config
from server.work_lifecycle import KINDS, LifecycleError, Manager

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("old_switch", [False, True, "1", "true"])
async def test_compatibility_routes_are_native_even_with_a_stale_old_switch(monkeypatch, old_switch):
    from server import main

    # A stale in-memory attribute cannot restore an old code path. The actual
    # config has no such option and never reads its former environment variable.
    monkeypatch.setattr(config, "LEGACY_CHAT_ENABLED", old_switch, raising=False)
    calls = []
    request = SimpleNamespace()

    async def native_stream(received):
        assert received is request
        calls.append("stream")
        return JSONResponse({"engine": "prism-native", "status": "failed"}, status_code=503)

    async def native_cancel(received):
        assert received is request
        calls.append("cancel")
        return JSONResponse({"external_stop_confirmed": False}, status_code=409)

    monkeypatch.setattr(main, "native_chat_stream", native_stream)
    monkeypatch.setattr(main, "native_chat_cancel", native_cancel)
    assert (await main.api_chat_stream(request)).status_code == 503
    assert (await main.api_chat_cancel(request)).status_code == 409
    assert calls == ["stream", "cancel"]


def test_runtime_has_no_external_agent_configuration_or_execution_module():
    assert not (ROOT / "server/services/claude_runner.py").exists()
    forbidden = {"CLAUDE_BIN", "SKILLS_HOME", "SKILL_FALLBACK_ROOTS", "SKILL_MAP", "LEGACY_CHAT_ENABLED"}
    tree = ast.parse((ROOT / "server/config.py").read_text())
    assert not any(isinstance(node, ast.Name) and node.id in forbidden for node in ast.walk(tree))
    assert "PRISM_LEGACY_CHAT_ENABLED" not in (ROOT / "server/config.py").read_text()
    for source in (ROOT / "server").rglob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "claude_runner" not in (node.module or ""), source.name
            elif isinstance(node, ast.Import):
                assert all("claude_runner" not in item.name for item in node.names), source.name
    assert not any(hasattr(config, name) for name in forbidden)


def test_lifecycle_cannot_admit_a_retired_cli_producer():
    manager = Manager()
    try:
        assert "cli" not in KINDS
        with pytest.raises(LifecycleError, match="work_kind_rejected"):
            manager.admit("cli")
        assert sum(manager.observe()["leases"].values()) == 0
    finally:
        manager._pool.shutdown(wait=True)
