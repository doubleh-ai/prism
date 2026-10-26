"""Native health is local configuration evidence, never an execution probe."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from server import health


@pytest.fixture
def native_health(monkeypatch):
    monkeypatch.setattr(health, "_check_db", lambda: {"status": "ok", "latency_ms": 1})
    monkeypatch.setattr(
        health,
        "release_payload",
        lambda: {
            "version": "v0.20.0",
            "deployed_revision": "a" * 40,
            "private_path": "/private/runtime",
        },
    )
    monkeypatch.setattr(
        health.config,
        "MODELS",
        {
            "test-model": {
                "name": "Test Model",
                "api_key": "synthetic-private-credential",
                "base_url": "https://api.deepseek.com/anthropic",
                "model": "test-model-version",
            }
        },
    )


def test_admin_health_uses_native_configuration_without_processes_network_or_legacy_files(
    native_health, monkeypatch
):
    from server.agent import provider, runtime, tools

    def forbidden(*args, **kwargs):
        raise AssertionError("Health must not start work, spawn processes or contact providers")

    for owner, name in (
        (subprocess, "Popen"),
        (subprocess, "run"),
        (asyncio, "create_subprocess_exec"),
        (asyncio, "create_subprocess_shell"),
        (socket, "create_connection"),
        (socket.socket, "connect"),
        (httpx.Client, "request"),
        (httpx.AsyncClient, "request"),
        (httpx.AsyncClient, "send"),
        (provider, "stream"),
        (provider.AnthropicMessagesProvider, "stream"),
        (runtime, "model_stream"),
        (runtime.NativeAgent, "start"),
        (tools.NativeTools, "execute"),
    ):
        monkeypatch.setattr(owner, name, forbidden)

    for name in ("exists", "is_dir", "read_text"):
        original = getattr(Path, name)

        def guard(path, *args, _original=original, **kwargs):
            assert ".claude" not in path.parts, "Health must not read external execution configuration"
            return _original(path, *args, **kwargs)

        monkeypatch.setattr(Path, name, guard)

    payload = health.admin_health_payload()
    assert payload["ok"] is True
    assert payload["scope"] == "local_configuration"
    assert payload["runtime"]["engine"] == "prism-native"
    assert set(payload["checks"]) == {
        "database",
        "native_core",
        "tool_catalog",
        "provider_configuration",
    }
    assert payload["checks"]["tool_catalog"]["count"] > 0
    assert payload["checks"]["provider_configuration"] == {
        "status": "ok",
        "configured_models": 1,
        "model_count": 1,
        "live_checked": False,
    }
    assert payload["models"][0]["configured"] is True
    serialized = json.dumps(payload)
    for private in (
        "synthetic-private-credential",
        "https://api.deepseek.com",
        "/private/runtime",
        "claude_cli",
        "skills_home",
        "data_dir",
    ):
        assert private not in serialized
    assert "paths" not in payload
    assert "skills" not in payload


@pytest.mark.parametrize("component", ["database", "native_core", "tool_catalog", "provider_configuration"])
def test_admin_health_requires_each_native_prerequisite(native_health, monkeypatch, component):
    if component == "provider_configuration":
        monkeypatch.setattr(health.config, "MODELS", {})
    else:
        function = {
            "database": "_check_db",
            "native_core": "_check_native_core",
            "tool_catalog": "_check_tool_catalog",
        }[component]
        monkeypatch.setattr(health, function, lambda: {"status": "error"})
    payload = health.admin_health_payload()
    assert payload["ok"] is False
    # Basic readiness is deliberately narrower than model/core availability.
    assert health.readiness_payload()["status"] == ("unready" if component == "database" else "ready")


@pytest.mark.parametrize(
    "field,value,flag",
    [
        ("api_key", "", "credential_configured"),
        ("api_key", "secret\nnot-a-header", "credential_configured"),
        ("base_url", "https://private.invalid/model?token=secret", "endpoint_supported"),
        ("model", "", "model_configured"),
        ("model", "invalid\nmodel", "model_configured"),
    ],
)
def test_admin_detects_invalid_provider_configuration_without_echoing_values(
    native_health, monkeypatch, field, value, flag
):
    model = health.config.MODELS["test-model"]
    monkeypatch.setitem(model, field, value)
    payload = health.admin_health_payload()
    assert payload["ok"] is False
    assert payload["models"][0]["configured"] is False
    assert payload["models"][0][flag] is False
    if value:
        assert value not in json.dumps(payload)


def test_tool_catalog_failure_is_safe_and_does_not_report_ready(native_health, monkeypatch):
    from server.agent import tools

    def broken():
        raise RuntimeError("/private/tools?credential=secret")

    monkeypatch.setattr(tools, "native_catalog", broken)
    payload = health.admin_health_payload()
    assert payload["ok"] is False
    assert payload["checks"]["tool_catalog"]["error"] == "native_tools_unavailable"
    assert "/private/tools" not in json.dumps(payload)


@pytest.mark.parametrize(
    "status,revision,expected,exit_code",
    [
        ("ready", "a" * 40, "a" * 40, 0),
        ("ready", "a" * 40, "A" * 40, 0),
        ("ready", "unknown", "", 0),
        ("ready", "unknown", "a" * 40, 1),
        ("ready", "a" * 40, "b" * 40, 1),
        ("unready", "a" * 40, "a" * 40, 1),
        ("ready", "/private/runtime", "", 1),
    ],
)
def test_read_only_verify_checks_actual_readiness_and_expected_served_revision(
    tmp_path, status, revision, expected, exit_code
):
    root = Path(__file__).resolve().parents[1]
    curl = tmp_path / "curl"
    curl.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['TEST_CURL_RECEIPT']).write_text(json.dumps(sys.argv[1:]))\n"
        "print(os.environ['TEST_READINESS_JSON'])\n"
    )
    curl.chmod(0o700)
    receipt = tmp_path / "curl-receipt.json"
    result = subprocess.run(
        ["/bin/bash", str(root / "scripts/verify.sh")],
        env={
            **os.environ,
            "PATH": str(tmp_path) + os.pathsep + os.environ.get("PATH", ""),
            "PRISM_PYTHON": sys.executable,
            "PRISM_EXPECTED_REVISION": expected,
            "PRISM_VERIFY_URL": "http://127.0.0.1:8088",
            "TEST_CURL_RECEIPT": str(receipt),
            "TEST_READINESS_JSON": json.dumps({"status": status, "deployed_revision": revision}),
        },
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == exit_code
    assert json.loads(receipt.read_text()) == [
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        "5",
        "http://127.0.0.1:8088/health/ready",
    ]
    assert "/private/runtime" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "payload,expected,passed",
    [
        ({"status": "ready", "deployed_revision": "a" * 40}, "a" * 40, True),
        ({"status": "ready", "deployed_revision": "a" * 40}, "A" * 40, True),
        ({"status": "ready", "deployed_revision": "unknown"}, "", True),
        ({"status": "ready", "deployed_revision": "unknown"}, "a" * 40, False),
        ({"status": "ready", "deployed_revision": "a" * 40}, "b" * 40, False),
        ({"status": "unready", "deployed_revision": "a" * 40}, "", False),
        ({"status": "ready", "deployed_revision": "/private/runtime"}, "", False),
    ],
)
def test_smoke_script_is_read_only_and_requires_observed_readiness(monkeypatch, payload, expected, passed):
    from scripts import smoke_test

    requests = []

    def response(url, *, timeout):
        requests.append((url, timeout))
        value = io.BytesIO(json.dumps(payload).encode())
        value.status = 200
        return value

    monkeypatch.setattr(smoke_test, "urlopen", response)
    assert smoke_test.check_readiness("http://127.0.0.1:8088", expected) is passed
    assert requests == [("http://127.0.0.1:8088/health/ready", 5)]
