"""Astock SDK transport configuration stays separate from Native messages."""

import asyncio
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest


@pytest.fixture
def engine_config(monkeypatch):
    from server import analysis_engine as engine

    defaults = {
        "llm_provider": "openai",
        "backend_url": "https://unrelated-upstream.invalid/v1",
        "memory_log_path": "legacy-memory-must-stay-disabled",
    }
    module = types.ModuleType("tradingagents.default_config")
    module.DEFAULT_CONFIG = defaults
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", module)
    monkeypatch.setattr(engine, "ASTOCK_LLM_PROVIDER", "deepseek")
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", "")
    yield engine, defaults
    engine._store.clear()


@pytest.mark.parametrize(
    "explicit,expected",
    [
        (None, ""),
        ("https://gateway.example/api/v1/", "https://gateway.example/api/v1/"),
    ],
)
def test_config_import_never_reuses_native_url_or_an_ignored_api_key(tmp_path, explicit, expected):
    root = str(Path(__file__).resolve().parents[1])
    data = tmp_path / "isolated-config"
    data.mkdir(mode=0o700)
    environment = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "STOCK_DATA_DIR": str(data),
        "ANTHROPIC_BASE_URL": "https://native.example/anthropic",
        "ANTHROPIC_API_KEY": "synthetic-native-key-never-used",
        "ASTOCK_LLM_API_KEY": "synthetic-ignored-setting-never-used",
    }
    if explicit is not None:
        environment["ASTOCK_LLM_BASE_URL"] = explicit
    code = (
        "import sys,socket,json\n"
        "def denied(*args,**kwargs):raise AssertionError('No network in config import')\n"
        "socket.socket=denied;socket.create_connection=denied\n"
        f"sys.path.insert(0,{root!r})\n"
        "from server import config\n"
        "print(json.dumps({'base':config.ASTOCK_LLM_BASE_URL,"
        "'ignored_key_exposed':hasattr(config,'ASTOCK_LLM_API_KEY')}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        env=environment,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {"base": expected, "ignored_key_exposed": False}
    assert "synthetic-native-key" not in result.stdout + result.stderr
    assert "synthetic-ignored-setting" not in result.stdout + result.stderr


def test_provider_default_does_not_inherit_an_upstream_backend(engine_config, monkeypatch):
    engine, defaults = engine_config
    before = dict(defaults)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://native.example/anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-native-key-never-used")
    environment = dict(os.environ)
    built = engine._build_config("fast")
    assert built["llm_provider"] == "deepseek"
    assert built["backend_url"] is None
    assert "api_key" not in built and "ASTOCK_LLM_API_KEY" not in built
    assert defaults == before and os.environ == environment


@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("https://api.deepseek.com/", "https://api.deepseek.com"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1"),
        ("https://gateway.example/api/openai/v1/", "https://gateway.example/api/openai/v1"),
        ("http://127.0.0.1:9876/private/chat/", "http://127.0.0.1:9876/private/chat"),
        ("http://localhost:1/v1/", "http://localhost:1/v1"),
        ("http://[::1]:65535/private/v1/", "http://[::1]:65535/private/v1"),
        ("https://[2001:db8::1]/gateway/v1", "https://[2001:db8::1]/gateway/v1"),
        ("http://[fe80::1%25en0]:9876/v1", "http://[fe80::1%25en0]:9876/v1"),
        ("https://model_gateway:8443/api/v2", "https://model_gateway:8443/api/v2"),
        ("https://gateway.example.:0443/a%2Fb/%3F/%23/", "https://gateway.example.:0443/a%2Fb/%3F/%23"),
        ("HTTPS://GATEWAY.EXAMPLE/proxy/Models/v2", "HTTPS://GATEWAY.EXAMPLE/proxy/Models/v2"),
        ("https://模型.example/接口/v1/", "https://模型.example/接口/v1"),
    ],
)
def test_explicit_sdk_urls_preserve_their_path(engine_config, monkeypatch, base, expected):
    engine, defaults = engine_config
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", base)
    built = engine._build_config("deep")
    assert built["backend_url"] == expected
    assert defaults["backend_url"] == "https://unrelated-upstream.invalid/v1"


@pytest.mark.parametrize(
    "base",
    [
        "https://api.deepseek.com/anthropic",
        "https://api.deepseek.com/anthropic/v1/",
        "https://gateway.example/api/Anthropic/v1",
        "https://gateway.example/%61nthropic/v1",
    ],
)
def test_deepseek_messages_protocol_is_rejected_before_sdk_construction(engine_config, monkeypatch, base):
    engine, _ = engine_config
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", base)
    monkeypatch.setattr(engine, "ASTOCK_LLM_PROVIDER", "DeepSeek")
    with pytest.raises(engine.AnalysisConfigurationError) as error:
        engine._build_config("fast")
    assert error.value.code == "astock_transport_mismatch"
    assert str(error.value) == "astock_transport_mismatch"
    assert base not in str(error.value)


def test_anthropic_sdk_explicit_path_is_not_rewritten_or_misclassified(engine_config, monkeypatch):
    engine, _ = engine_config
    monkeypatch.setattr(engine, "ASTOCK_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", "https://gateway.example/anthropic/v1/")
    assert engine._build_config("fast")["backend_url"] == "https://gateway.example/anthropic/v1"


@pytest.mark.parametrize(
    "base",
    [
        "v1",
        "/api/v1",
        "//gateway.example/v1",
        "ftp://gateway.example/v1",
        "file:///api/v1",
        "https:/gateway.example/v1",
        "https:///v1",
        "https://:443/v1",
        "https://[invalid-host/anthropic",
        "https://[v1.example]/v1",
        "https://[::1]extra/v1",
        "https://::1/v1",
        "https://[fe80::1%en0]/v1",
        "https://[fe80::1%25]/v1",
        "https://[fe80::1%25bad\\zone]/v1",
        "https://gateway.example:/v1",
        "https://[::1]:/v1",
        "https://gateway.example:0/v1",
        "https://gateway.example:65536/v1",
        "https://gateway.example:-1/v1",
        "https://gateway.example:+443/v1",
        "https://gateway.example:４４３/v1",
        "https://gateway.example:port/v1",
        "https://gateway.example:443:80/v1",
        "https://@gateway.example/v1",
        "https://synthetic-user@gateway.example/v1",
        "https://synthetic-user:synthetic-secret@gateway.example/v1",
        "https://gateway.example?",
        "https://gateway.example/v1?api_key=synthetic-secret",
        "https://gateway.example#",
        "https://gateway.example/v1#synthetic-secret",
        " https://gateway.example/v1",
        "https://gateway.example/v1 ",
        "https://gate\nway.example/v1",
        "https://gateway.example/\t/v1",
        "https://gateway.example/\x00/v1",
        "https://gateway.example/\x7f/v1",
        "https://gateway.example/\x85/v1",
        "https://gateway.example/\u200b/v1",
        "https://gateway.example/\ud800/v1",
        "https://bad host.example/v1",
        "https://bad\\host.example/v1",
        "https://bad%host.example/v1",
        "https://bad..example/v1",
        "https://-bad.example/v1",
        "https://999.999.999.999/v1",
        "https://" + "a" * 64 + ".example/v1",
    ],
)
def test_invalid_url_failure_has_no_configured_value(engine_config, monkeypatch, base):
    engine, _ = engine_config
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", base)
    environment = dict(os.environ)
    with pytest.raises(engine.AnalysisConfigurationError) as error:
        engine._build_config("fast")
    assert error.value.code == "astock_transport_invalid"
    assert str(error.value) == "astock_transport_invalid"
    assert base not in str(error.value)
    assert "synthetic-secret" not in str(error.value)
    assert os.environ == environment


@pytest.mark.parametrize(
    "base,code",
    [
        ("https://api.deepseek.com/anthropic/v1", "astock_transport_mismatch"),
        ("https://synthetic-user:synthetic-secret@gateway.example/v1", "astock_transport_invalid"),
        ("https://gateway.example/v1?api_key=synthetic-secret", "astock_transport_invalid"),
        ("https://gateway.example:/v1", "astock_transport_invalid"),
    ],
)
async def test_started_run_reports_transport_error_without_constructing_graph(
    engine_config, monkeypatch, caplog, base, code
):
    engine, _ = engine_config
    constructed = []

    class ForbiddenGraph:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
            raise AssertionError("No graph or model may start for a transport error")

    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = ForbiddenGraph
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", module)
    monkeypatch.setattr(engine, "_ensure_astock", lambda: None)
    monkeypatch.setattr(engine, "ASTOCK_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(engine, "ASTOCK_LLM_BASE_URL", base)
    aid = await engine.start_analysis("600000", username="synthetic-owner", force_refresh=True)
    deadline = time.monotonic() + 4
    while engine._store[aid]["status"] != "error" or engine._ENGINE_RUN_LOCK.locked():
        assert time.monotonic() < deadline, "Bounded synthetic worker did not finish"
        await asyncio.sleep(0.01)
    assert constructed == []
    errors = [event for event in engine._store[aid]["events"] if event["type"] == "error"]
    assert len(errors) == 1 and errors[0]["code"] == code
    assert "anthropic" not in errors[0]["message"]
    record = engine.get_analysis_record(aid, "synthetic-owner")
    assert record["status"] == "error" and record["report"] is None
    public_output = json.dumps(engine._store[aid]["events"]) + json.dumps(record) + caplog.text
    assert base not in public_output and "synthetic-secret" not in public_output
