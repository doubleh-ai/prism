"""No real CLI/model is invoked: assert the boundary at the child process call."""

import asyncio
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import config
from server.services import auxiliary_model as helper


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_MODEL", "deepseek")
    monkeypatch.setattr(
        config,
        "MODELS",
        {
            "deepseek": {
                "model": "configured-deepseek",
                "base_url": "https://model.example/anthropic",
                "api_key": "synthetic-provider-key",
            },
            "minimax": {"model": "unconfigured-model", "base_url": "https://other.example", "api_key": ""},
        },
    )
    monkeypatch.setattr(config, "CLAUDE_BIN", "/disabled/claude")
    for key in (
        "PRISM_CAPABILITY_TOKEN",
        "DEEPSEEK_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "NODE_OPTIONS",
        "HTTPS_PROXY",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.setenv(key, "must-not-inherit")


def assert_boundary(cmd, options):
    for flag in ("--tools", "--setting-sources"):
        assert cmd[cmd.index(flag) + 1] == ""
    assert cmd[cmd.index("--disallowedTools") + 1] == "mcp__*"
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in cmd and "--no-session-persistence" in cmd
    assert "--max-tokens" not in cmd and cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--model") + 1] == "configured-deepseek"
    env = options["env"]
    assert set(env) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SystemRoot",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "NO_COLOR",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    }
    assert env["ANTHROPIC_AUTH_TOKEN"] == "synthetic-provider-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://model.example/anthropic"
    assert "synthetic-provider-key" not in repr(cmd)
    directory = Path(options["cwd"])
    assert env["HOME"] == env["TMPDIR"] == str(directory)
    assert directory != Path.home() and stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert options["stderr"] == helper.subprocess.DEVNULL
    return directory


def test_sync_uses_configured_model_without_tools_or_inherited_credentials(configured, monkeypatch):
    before, directories = dict(os.environ), []

    def fake(cmd, **options):
        directories.append(assert_boundary(cmd, options))
        return SimpleNamespace(stdout=b'{"result":"safe"}', returncode=0)

    monkeypatch.setattr(helper.subprocess, "run", fake)
    for _ in range(2):
        assert helper.run_text("untrusted body", output_format="json") == '{"result":"safe"}'
    assert directories[0] != directories[1] and all(not d.exists() for d in directories)
    assert os.environ == before


@pytest.mark.parametrize("model_id", ["minimax", "missing"])
def test_unconfigured_model_cannot_start_a_child(configured, monkeypatch, model_id):
    def forbidden(*args, **kwargs):
        pytest.fail("Unconfigured models must not start the CLI")

    monkeypatch.setattr(helper.subprocess, "run", forbidden)
    with pytest.raises(helper.AuxiliaryModelUnavailable, match="尚未配置"):
        helper.run_text("input", model_id=model_id)


@pytest.mark.parametrize(
    "returncode,stdout", [(1, b"private SDK error"), (0, b"x" * 256_001), (0, b"synthetic-provider-key")]
)
def test_failed_or_unbounded_output_is_never_relayed(configured, monkeypatch, returncode, stdout):
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    with pytest.raises(helper.AuxiliaryModelUnavailable) as error:
        helper.run_text("input")
    assert "private SDK error" not in str(error.value)


async def test_async_has_the_same_tool_and_credential_boundary(configured, monkeypatch):
    directories = []

    async def fake(*cmd, **options):
        directories.append(assert_boundary(cmd, options))

        async def communicate():
            return b"safe response", None

        return SimpleNamespace(returncode=0, communicate=communicate)

    monkeypatch.setattr(helper.asyncio, "create_subprocess_exec", fake)
    assert await helper.run_text_async("untrusted context") == "safe response"
    assert all(not directory.exists() for directory in directories)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_timeout_or_caller_cancellation_stops_and_reaps_child(configured, monkeypatch, cancelled):
    started = asyncio.Event()

    class Process:
        returncode = None
        terminated = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        async def wait(self):
            return self.returncode

    process = Process()

    async def fake(*args, **kwargs):
        return process

    monkeypatch.setattr(helper.asyncio, "create_subprocess_exec", fake)
    task = asyncio.create_task(helper.run_text_async("input", timeout=1 if cancelled else 0.01))
    await started.wait()
    if cancelled:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else helper.AuxiliaryModelUnavailable):
        await task
    assert process.terminated and process.returncode is not None


async def test_explanation_cache_is_owner_and_context_scoped(client, auth_cookies, monkeypatch):
    from server import main
    from server.auth import create_session, hash_password
    from server.database import create_user

    main._term_cache.clear()
    calls = []

    async def fake(prompt, **kwargs):
        calls.append(prompt)
        return "private explanation " + str(len(calls))

    monkeypatch.setattr(helper, "run_text_async", fake)
    create_user("other-owner", hash_password("test-password"))
    other = {"token": create_session("other-owner")}
    body = {"term": "same-term", "context": "private context A"}
    first = await client.post("/api/explain-term", json=body, cookies=auth_cookies)
    same = await client.post("/api/explain-term", json=body, cookies=auth_cookies)
    foreign = await client.post("/api/explain-term", json=body, cookies=other)
    changed = await client.post(
        "/api/explain-term", json={**body, "context": "private context B"}, cookies=auth_cookies
    )
    assert first.json()["explanation"] == same.json()["explanation"]
    assert same.json()["cached"] is True
    assert foreign.json()["explanation"] != first.json()["explanation"]
    assert changed.json()["explanation"] != first.json()["explanation"] and len(calls) == 3
    main._term_cache.clear()


async def test_unconfirmed_analysis_start_never_echoes_provider_exception(
    client, auth_cookies, monkeypatch, caplog
):
    from server import main

    secret = "synthetic-provider-key-do-not-return"

    async def fail(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(main, "start_analysis", fail)
    response = await client.post("/api/analysis/start", cookies=auth_cookies, json={"ticker": "600519"})
    assert response.status_code == 500 and response.json()["code"] == "engine_unavailable"
    assert secret not in response.text and secret not in caplog.text


def test_indicator_fallback_uses_the_same_configured_no_tool_boundary(configured, monkeypatch):
    from server.indicators import translator

    calls = []

    def fake(cmd, **options):
        assert_boundary(cmd, options)
        calls.append(cmd)
        return SimpleNamespace(
            stdout=json.dumps({"kind": "plot", "name": "Close", "dsl": {"price": "close"}}).encode(),
            returncode=0,
        )

    monkeypatch.setattr(helper.subprocess, "run", fake)
    result = translator.translate("unrecognized custom indicator request")
    assert result["source"] == "llm" and result["dsl"] == {"price": "close"}
    assert len(calls) == 1


def test_indicator_rule_fastpath_never_starts_model(monkeypatch):
    from server.indicators import translator

    def forbidden(*args, **kwargs):
        pytest.fail("Known rules must remain local without a model call")

    monkeypatch.setattr(helper, "run_text", forbidden)
    assert translator.translate("MACD金叉")["source"] == "rule"
