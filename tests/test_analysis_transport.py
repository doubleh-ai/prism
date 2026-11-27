"""Removed provider settings cannot restore the retired execution route."""

import builtins
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from server import config
from server.database import get_db


@pytest.mark.parametrize(
    "provider,base",
    [
        ("deepseek", "https://api.deepseek.com"),
        ("anthropic", "https://provider.invalid/messages"),
        ("legacy", "not-a-url"),
    ],
)
async def test_fresh_compatibility_import_and_start_never_load_provider_or_read_database(
    monkeypatch, provider, base
):
    monkeypatch.setenv("ASTOCK_LLM_PROVIDER", provider)
    monkeypatch.setenv("ASTOCK_LLM_BASE_URL", base)
    monkeypatch.setenv("PRISM_ASTOCK_ENABLED", "1")
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.startswith(
            ("tradingagents", "langchain", "langgraph", "anthropic", "openai", "claude_runner")
        ):
            pytest.fail("the retired compatibility module must not import a model executor")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    path = Path(__file__).resolve().parents[1] / "server/analysis_engine.py"
    spec = importlib.util.spec_from_file_location("server._isolated_analysis_compat", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def no_database():
        pytest.fail("rejected admission must not inspect history or caches")

    monkeypatch.setattr(module, "get_db", no_database)
    with pytest.raises(HTTPException) as blocked:
        await module.start_analysis("600519", username="synthetic", force_refresh=True)
    assert blocked.value.status_code == 503
    assert base not in str(blocked.value)
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0] == 0


def test_runtime_configuration_has_no_retired_model_or_execution_options():
    assert not any(name.startswith("ASTOCK_") for name in vars(config))
