"""Offline worker/MCP fixtures; CLI subprocesses and sockets are synthetic.

Candidate authentication/dispatch/ledger functions are real, on the test DB.
These tests are never a Linux isolation, real model, search or market receipt.
"""

import asyncio
import hashlib
import importlib
import io
import json
import types

import pytest

from scripts import live_integration_policy as policy
from scripts import live_integration_transport as transport
from scripts import live_integration_worker as worker


class Channel:
    def __init__(self, incoming):
        self.incoming, self.outgoing = io.BytesIO(incoming), bytearray()

    def recv(self, count):
        return self.incoming.read(count)

    def settimeout(self, value):
        pass

    def sendall(self, value):
        self.outgoing.extend(value)


def request(
    token, *, name="research_read", arguments=None, method="POST", path="/api/internal/capabilities/call"
):
    body = (
        transport.canonical(
            {
                "name": name,
                "arguments": arguments if arguments is not None else {},
                "request_id": hashlib.sha256((name + path).encode()).hexdigest(),
            }
        )
        if method == "POST"
        else b""
    )
    return (
        f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:4321\r\nAuthorization: Bearer {token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def modules():
    names = (
        "server.main",
        "server.chat",
        "server.config",
        "server.services.claude_runner",
        "server.research_context",
        "server.research",
        "server.database",
        "server.capabilities",
        "server.auth",
        "httpx",
    )
    return {name: importlib.import_module(name) for name in names}


@pytest.fixture
def owned_gateway():
    loaded = modules()
    database, context, ledger, capabilities = (
        loaded[name]
        for name in ("server.database", "server.research_context", "server.research", "server.capabilities")
    )
    with database.get_db() as db:
        for name in ("public-synthetic-owner", "public-synthetic-foreign"):
            db.execute("INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-no-login"])
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [name + "-conv", name])
    research = context.build_research_context(run_id="public-synthetic-run")
    ledger.create_research_run(
        "public-synthetic-owner", "public-synthetic-owner-conv", "public synthetic probe", research
    )
    gateway = worker.CapabilityGateway(loaded, transport, policy, "public-synthetic-owner")
    gateway.run_id = research["run_id"]
    token = capabilities.issue_run_token(
        "public-synthetic-owner", "public-synthetic-owner-conv", research["run_id"]
    )
    gateway.grant(token, "public-synthetic-owner", "public-synthetic-owner-conv", research["run_id"])
    return gateway, token, loaded


@pytest.mark.parametrize("name", ["indicator_list", "research_read"])
def test_gateway_runs_real_candidate_auth_schema_dispatch_and_own_ledger(owned_gateway, name):
    gateway, token, loaded = owned_gateway
    channel = Channel(request(token, name=name))
    gateway.handle(channel, 4321)
    assert channel.outgoing.startswith(b"HTTP/1.1 200 OK")
    result = json.loads(channel.outgoing.split(b"\r\n\r\n", 1)[1])
    assert result["ok"] is True and result["call"]["capability_id"] == name
    assert result["call"]["run_id"] == gateway.run_id
    if name == "research_read":
        assert result["data"]["run"]["run_id"] == gateway.run_id
    calls = loaded["server.capabilities"].list_calls("public-synthetic-owner", run_id=gateway.run_id)
    assert calls[0]["call_id"] == gateway.calls[0]["call_id"]
    assert token.encode() not in channel.outgoing


def test_tools_list_exposes_only_actual_read_only_candidate_definitions(owned_gateway):
    gateway, token, _ = owned_gateway
    channel = Channel(request(token, method="GET", path="/api/internal/capabilities/tools"))
    gateway.handle(channel, 4321)
    result = json.loads(channel.outgoing.split(b"\r\n\r\n", 1)[1])
    assert {tool["name"] for tool in result["tools"]} == {"indicator_list", "research_read"}
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in result["tools"])
    assert not any(tool["inputSchema"].get("required") for tool in result["tools"])


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("research_read", {"id": "foreign-run"}),
        ("analysis_start", {}),
        ("vibe_start", {}),
        ("market_quote", {"ticker": "600519"}),
        ("indicator_list", {"write": True}),
    ],
)
def test_gateway_denies_all_other_tools_and_nonempty_arguments_before_dispatch(
    owned_gateway, name, arguments
):
    gateway, token, loaded = owned_gateway
    channel = Channel(request(token, name=name, arguments=arguments))
    gateway.handle(channel, 4321)
    assert channel.outgoing.startswith(b"HTTP/1.1 502") and not gateway.calls
    assert loaded["server.capabilities"].list_calls("public-synthetic-owner", run_id=gateway.run_id) == []


def test_missing_revoked_foreign_and_wrong_run_grants_cannot_be_relabelled_owned(owned_gateway):
    gateway, token, loaded = owned_gateway
    with pytest.raises(worker.WorkerError, match="owned_scope_required"):
        gateway.grant("synthetic-token", "public-synthetic-foreign", "foreign-conv", gateway.run_id)
    with pytest.raises(worker.WorkerError, match="owned_scope_required"):
        gateway.grant("synthetic-token", gateway.owner, gateway.conv_id, "foreign-run")
    foreign = loaded["server.capabilities"].issue_run_token(
        "public-synthetic-foreign", "public-synthetic-foreign-conv", "foreign-run"
    )
    for supplied in ("not-issued-token", foreign):
        channel = Channel(request(supplied))
        gateway.handle(channel, 4321)
        assert channel.outgoing.startswith(b"HTTP/1.1 502")
    loaded["server.capabilities"].revoke_run_token(gateway.run_id)
    channel = Channel(request(token))
    gateway.handle(channel, 4321)
    assert channel.outgoing.startswith(b"HTTP/1.1 502") and not gateway.calls


def test_model_text_links_are_not_native_search_evidence():
    observer = worker.FrameObserver()
    observer.observe(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "[link](https://www.sse.com.cn/disclosure/)"}]},
        }
    )
    assert not observer.native_frames and not observer.urls


@pytest.mark.parametrize(
    "content",
    [
        [],
        {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"},
        [{"type": "text", "text": "https://www.sse.com.cn/disclosure/"}],
    ],
)
def test_native_search_errors_do_not_pass_even_with_http_200(content):
    observer = worker.FrameObserver()
    observer.observe(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "server_tool_use",
                        "name": "web_search",
                        "id": "s",
                        "input": {"query": "public synthetic"},
                    },
                    {"type": "web_search_tool_result", "tool_use_id": "s", "content": content},
                ]
            },
        }
    )
    assert not observer.native_frames and not observer.urls


def test_documented_matched_server_frames_are_metadata_only_and_not_ledger_acceptance():
    observer = worker.FrameObserver()
    result = {
        "type": "web_search_tool_result",
        "tool_use_id": "s",
        "content": [
            {
                "type": "web_search_result",
                "url": "https://www.sse.com.cn/disclosure/public",
                "title": "public synthetic",
                "encrypted_content": "SYNTHETIC_OPAQUE",
                "page_age": "yesterday",
            }
        ],
    }
    observer.observe({"type": "assistant", "message": {"content": [result]}})
    assert not observer.native_frames
    observer.observe(
        {
            "type": "assistant",
            "message": {"content": [{"type": "server_tool_use", "name": "web_search", "id": "s"}, result]},
        }
    )
    assert observer.native_frames and observer.server_frames
    assert observer.urls == {"https://www.sse.com.cn/disclosure/public"}
    assert "SYNTHETIC_OPAQUE" not in json.dumps(observer.__dict__, default=list)
    assert not hasattr(observer, "source_ledger_persisted")


class ProcessFixture:
    def __init__(self, pid, events):
        self.pid, self.returncode = pid, None
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(b"".join(encode_event(event) for event in events))
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self):
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def encode_event(event):
    return json.dumps(event).encode() + b"\n"


@pytest.mark.parametrize("server_frames", [False, True])
async def test_actual_runner_builds_two_bounded_cli_calls_and_actual_owned_candidate_roundtrip(
    monkeypatch, tmp_path, server_frames
):
    loaded = modules()
    runner, context, ledger = (
        loaded[name]
        for name in ("server.services.claude_runner", "server.research_context", "server.research")
    )
    identity = {
        "candidate": "a" * 40,
        "cli_sha256": "b" * 64,
        "probe_sha256": "c" * 64,
        "nonce": "d" * 32,
        "worker_pid": 123,
        "broker_pid": 456,
    }
    endpoint = "http://127.0.0.1:4322/anthropic"
    gateway = worker.CapabilityGateway(loaded, transport, policy, "pending")
    (tmp_path / "output").mkdir()
    launches, killed = [], []
    monkeypatch.setattr(runner, "CLAUDE_BIN", "/opt/prism/tools/claude/" + "b" * 64 + "/claude")
    monkeypatch.setattr(runner, "CAPABILITY_INTERNAL_URL", "http://127.0.0.1:4321")

    async def no_proxy():
        return None

    monkeypatch.setattr(runner, "get_proxy_config", no_proxy)
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    async def synthetic_cli(*argv, **kwargs):
        launches.append((argv, kwargs))
        assert kwargs["start_new_session"] is True
        phase = len(launches) - 1
        if phase == 0:
            for name in ("indicator_list", "research_read"):
                channel = Channel(request(kwargs["env"]["PRISM_CAPABILITY_TOKEN"], name=name))
                await asyncio.to_thread(gateway.handle, channel, 4321)
                assert channel.outgoing.startswith(b"HTTP/1.1 200 OK")
            date = ledger.read_research_run(gateway.owner, gateway.run_id)["run"]["as_of"][:10]
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": date + " Asia/Shanghai public synthetic reply"}]
                    },
                }
            ]
        else:
            events = [
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "native-search-one",
                                "name": "WebSearch",
                                "input": {"query": "site:sse.com.cn public synthetic"},
                            }
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "native-search-one",
                                "content": '[{"url":"https://www.sse.com.cn/disclosure/public","title":"public synthetic metadata"}]',
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Public synthetic search metadata, original not verified.",
                            }
                        ]
                    },
                },
            ]
            if server_frames:
                events = [
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "server_tool_use",
                                    "id": "native-search-one",
                                    "name": "web_search",
                                    "input": {"query": "site:sse.com.cn public synthetic"},
                                },
                                {
                                    "type": "web_search_tool_result",
                                    "tool_use_id": "native-search-one",
                                    "content": [
                                        {
                                            "type": "web_search_result",
                                            "url": "https://www.sse.com.cn/disclosure/public",
                                            "title": "synthetic metadata",
                                            "encrypted_content": "SYNTHETIC_OPAQUE",
                                        }
                                    ],
                                },
                                {"type": "text", "text": "Synthetic metadata only, original not verified."},
                            ]
                        },
                    }
                ]
        events.append({"type": "result", "subtype": "success", "is_error": False})
        return ProcessFixture(900001 + phase, events)

    monkeypatch.setattr(worker.asyncio, "create_subprocess_exec", synthetic_cli)
    result = await worker.exercise(loaded, tmp_path, identity, endpoint, gateway, policy)
    assert result["real_runner_invocations"] == 2
    assert (
        result["owned_mcp_roundtrip_verified"] is True and result["foreign_owner_http_404_verified"] is True
    )
    assert all(phase["model_completed"] and phase["server_clock_verified"] for phase in result["phases"])
    assert result["phases"][0]["model_date_echo_verified"]
    assert result["phases"][1]["source_ledger_persisted"] is (not server_frames)
    assert result["phases"][1]["chat_sse_ledger_match"] is (not server_frames)
    assert result["phases"][1]["server_search_unhandled_by_candidate"] is server_frames
    assert result["phases"][1]["search_call_count"] == 1
    assert result["phases"][1]["official_source_urls"] == ["https://www.sse.com.cn/disclosure/public"]
    for index, (argv, kwargs) in enumerate(launches):
        assert argv[argv.index("--max-turns") + 1] == str((4, 2)[index])
        assert argv[argv.index("--tools") + 1] == ("" if index == 0 else "WebSearch")
        assert "WebFetch" not in argv[argv.index("--allowedTools") + 1]
        assert kwargs["env"]["ANTHROPIC_AUTH_TOKEN"].startswith("PRISM_LIVE_SYNTHETIC_")
        assert kwargs["env"]["ANTHROPIC_BASE_URL"] == endpoint
    raw = (tmp_path / "output/model-events.jsonl").read_text()
    assert "PRISM_LIVE_SYNTHETIC_" not in raw
    assert all(launch[1]["env"]["PRISM_CAPABILITY_TOKEN"] not in raw for launch in launches)
    assert {pid for pid, _ in killed} == {900001, 900002}
    with loaded["server.database"].get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM capability_runs WHERE revoked=0").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_no_key_or_arbitrary_question_cli_entry_and_no_fetch_claim():
    assert len(worker.PROMPTS) == 2 and "WebFetch" in worker.PROMPTS[1]
    assert worker.TOOLS == {"indicator_list", "research_read"}
    assert worker.POLICY_SHA256 == transport.POLICY_SHA256
    assert worker.scrub({"response": "public SYNTHETIC_SECRET answer"}, ["SYNTHETIC_SECRET"]) == {
        "response": "public [redacted] answer"
    }


def test_worker_rejects_nonisolated_entry_before_loading_transport_or_candidate(monkeypatch):
    args = types.SimpleNamespace(
        candidate="a" * 40,
        cli_sha256="b" * 64,
        probe_sha256="c" * 64,
        transport_sha256="e" * 64,
        nonce="d" * 32,
        workdir="/var/lib/prism-operations/integration/public-synthetic/live",
        production_pid=999998,
        broker_pid=999997,
        issued_at=int(worker.time.time()),
        bridge_gid=1234,
    )
    monkeypatch.setattr(
        worker, "load_transport", lambda *args: pytest.fail("pre-gate module loading forbidden")
    )
    monkeypatch.setattr(
        worker, "prepare_candidate", lambda *args: pytest.fail("pre-gate candidate import forbidden")
    )
    with pytest.raises(worker.WorkerError, match="bootstrap_identity_rejected"):
        worker.bootstrap(args)


async def test_failed_bootstrap_never_imports_candidate_or_runs_qa(monkeypatch):
    def blocked(*args):
        raise worker.WorkerError("bootstrap_isolation_rejected")

    monkeypatch.setattr(worker, "bootstrap", blocked)
    monkeypatch.setattr(worker, "prepare_candidate", lambda *args: pytest.fail("candidate import forbidden"))
    with pytest.raises(worker.WorkerError, match="bootstrap_isolation_rejected"):
        await worker.run_worker(types.SimpleNamespace())
