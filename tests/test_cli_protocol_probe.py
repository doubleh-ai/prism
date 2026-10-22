"""Offline protocol contracts; fake children never certify a real CLI/provider."""

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import time
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
probe = types.ModuleType("cli_protocol_probe")
probe.__file__ = str(ROOT / "scripts/cli_protocol_probe.py")
exec(compile(Path(probe.__file__).read_bytes(), probe.__file__, "exec"), probe.__dict__)
COMMIT, DIGEST, NONCE = "a" * 40, "b" * 64, "c" * 32
SESSION = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "cli-protocol"
    path.mkdir(mode=0o755)
    for name in ("home", "tmp", "data", "output"):
        (path / name).mkdir(mode=0o700)
    return path


def test_fixed_identity_paths():
    candidate, cli, work = probe.fixed_paths(
        COMMIT, DIGEST, "/var/lib/prism-operations/integration/release-test-001/cli-protocol"
    )
    assert candidate == Path("/opt/prism/releases") / COMMIT
    assert cli == Path("/opt/prism/tools/claude") / DIGEST / "claude"
    assert work.name == "cli-protocol"


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/cli-protocol",
        "/var/lib/prism-operations/integration/.hidden-one/cli-protocol",
        "/var/lib/prism-operations/integration/release-test-001/astock",
        "/var/lib/prism-operations/integration/release-test-001/cli-protocol/",
    ],
)
def test_no_arbitrary_fixture_directories(path):
    with pytest.raises(probe.FixtureError):
        probe.fixed_paths(COMMIT, DIGEST, path)


def test_json_and_sse_are_real_anthropic_message_shapes():
    body = {"model": probe.MODEL}
    message = probe.message(body)
    assert probe.message(body)["id"] != message["id"]
    assert message["type"] == "message" and message["stop_reason"] == "end_turn"
    assert message["content"] == [{"type": "text", "text": probe.MARKER}]
    blocks = probe.streamed_message(body).decode().strip().split("\n\n")
    events = [json.loads(block.split("data: ")[1]) for block in blocks]
    assert [event["type"] for event in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[0]["message"]["content"] == []
    assert events[2]["delta"]["text"] == probe.MARKER
    assert events[4]["delta"]["stop_reason"] == "end_turn"
    assert probe.TOKEN.encode() not in probe.streamed_message(body)


@pytest.mark.parametrize(
    "path", ["/v1/messages", "/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens"]
)
def test_fixture_accepts_only_synthetic_authorized_requests(path):
    state = probe.FixtureState()
    state.phase = "stream_new"
    result = state.accept(path, {"Authorization": "Bearer " + probe.TOKEN}, {"model": probe.MODEL})
    assert result in {"messages", "count_tokens"}
    assert all(state.checks.values())


@pytest.mark.parametrize(
    "path,auth,model",
    [
        ("/private", probe.TOKEN, probe.MODEL),
        ("/v1/messages?token=not-allowed", probe.TOKEN, probe.MODEL),
        ("/v1/messages", "not-our-token", probe.MODEL),
        ("/v1/messages", probe.TOKEN, "not-our-model"),
    ],
)
def test_fixture_rejects_unknown_paths_real_auth_and_models(path, auth, model):
    state = probe.FixtureState()
    state.phase = "stream_new"
    with pytest.raises(probe.FixtureError):
        state.accept(path, {"x-api-key": auth}, {"model": model})
    assert not all(state.checks.values())


def test_fixture_bounds_requests_and_auxiliary_tools():
    state = probe.FixtureState()
    state.phase = "auxiliary"
    with pytest.raises(probe.FixtureError, match="auxiliary_tools_rejected"):
        state.accept(
            "/v1/messages",
            {"x-api-key": probe.TOKEN},
            {"model": probe.MODEL, "tools": [{"name": "WebSearch"}]},
        )
    assert state.checks["auxiliary_http_tools_absent"] is False
    state = probe.FixtureState()
    state.phase = "stream_new"
    for _ in range(8):
        state.accept("/v1/messages", {"x-api-key": probe.TOKEN}, {"model": probe.MODEL})
    with pytest.raises(probe.FixtureError, match="fixture_budget_exceeded"):
        state.accept("/v1/messages", {"x-api-key": probe.TOKEN}, {"model": probe.MODEL})
    assert state.checks["request_budget_respected"] is False


def test_fixture_rejects_mixed_synthetic_and_unexpected_auth_headers():
    state = probe.FixtureState()
    state.phase = "stream_new"
    with pytest.raises(probe.FixtureError):
        state.accept(
            "/v1/messages",
            {"Authorization": "Bearer " + probe.TOKEN, "x-api-key": "unexpected-value"},
            {"model": probe.MODEL},
        )
    assert state.checks["synthetic_auth_only"] is False


def stream_command(cli, *, resume=False):
    command = [
        str(cli),
        "-p",
        probe.PROMPT,
        "--model",
        probe.MODEL,
        "--print",
        "--verbose",
        "--output-format",
        "stream-json",
        "--tools",
        probe.NATIVE_TOOLS,
        "--allowedTools",
        probe.NATIVE_TOOLS,
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--setting-sources",
        "",
    ]
    if resume:
        command += ["--resume", SESSION]
    return command


def child_environment(work, endpoint):
    return {
        "HOME": str(work / "home"),
        "ANTHROPIC_AUTH_TOKEN": probe.TOKEN,
        "ANTHROPIC_BASE_URL": endpoint,
        "DISABLE_UPDATES": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


@pytest.mark.parametrize("bad", ["cli", "key-argv", "other-env", "bridge", "shell", "resume"])
def test_child_boundary_rejects_arbitrary_execution_or_credentials(work, bad):
    state = probe.FixtureState()
    state.phase = "stream_new"
    cli = Path("/opt/prism/tools/claude") / DIGEST / "claude"
    boundary = probe.ChildBoundary(mock.Mock(), cli, work, "http://127.0.0.1:1234", state)
    command, env, options = stream_command(cli), child_environment(work, boundary.endpoint), {}
    if bad == "cli":
        command[0] = "/bin/sh"
    elif bad == "key-argv":
        command.append(probe.TOKEN)
    elif bad == "other-env":
        env["DEEPSEEK_API_KEY"] = "synthetic-should-not-inherit"
    elif bad == "bridge":
        env["PRISM_CAPABILITY_TOKEN"] = "synthetic-should-not-inherit"
    elif bad == "shell":
        options["shell"] = True
    else:
        command += ["--resume", SESSION]
    with pytest.raises(probe.FixtureError):
        boundary.spawn(command, env=env, **options)
    boundary.original.assert_not_called()


@pytest.mark.parametrize("wrong", [None, "uid", "executable", "group"])
def test_child_identity_is_checked_against_real_proc_and_new_process_group(work, wrong):
    state = probe.FixtureState()
    cli = Path("/opt/prism/tools/claude") / DIGEST / "claude"
    boundary = probe.ChildBoundary(mock.Mock(), cli, work, "http://127.0.0.1:1234", state)
    child = types.SimpleNamespace(pid=123456)
    with (
        mock.patch.object(
            probe.Path,
            "stat",
            return_value=types.SimpleNamespace(st_uid=os.getuid() + (1 if wrong == "uid" else 0)),
        ),
        mock.patch.object(
            probe.os, "readlink", return_value="/wrong/executable" if wrong == "executable" else str(cli)
        ),
        mock.patch.object(probe.os, "getpgid", return_value=child.pid + (1 if wrong == "group" else 0)),
    ):
        if wrong:
            with pytest.raises(probe.FixtureError, match="cli_child_identity_rejected"):
                boundary.verify_child(child)
        else:
            boundary.verify_child(child)


def test_cleanup_only_targets_tracked_synthetic_process_groups(work, monkeypatch):
    boundary = probe.ChildBoundary(
        mock.Mock(), Path("/fixed/cli"), work, "http://127.0.0.1:1234", probe.FixtureState()
    )
    child = types.SimpleNamespace(pid=123456, code=None)
    child.poll = lambda: child.code

    def wait(**kwargs):
        child.code = 0
        return 0

    child.wait = wait
    boundary.children = [child]
    signals = []
    monkeypatch.setattr(probe.os, "killpg", lambda pid, signal: signals.append((pid, signal)))
    assert boundary.cleanup() is True
    assert signals == [(123456, probe.signal.SIGTERM)]


async def test_stream_new_and_resume_require_actual_terminal_marker():
    calls = []

    class Runner:
        async def run_claude_stream(self, prompt, **options):
            calls.append((prompt, options))
            yield {"type": "system", "subtype": "init", "session_id": SESSION}
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": probe.MARKER}]}}
            yield {"type": "result", "subtype": "success", "is_error": False, "result": probe.MARKER}

    model = {"model": probe.MODEL}
    assert await probe.consume_stream(Runner(), model) == SESSION
    assert await probe.consume_stream(Runner(), model, session_id=SESSION) == SESSION
    assert calls[0][1]["user"] == "anon" and calls[0][1]["conv_id"] is None
    assert calls[1][1]["session_id"] == SESSION


@pytest.mark.parametrize("failure", ["no-session", "no-marker", "error", "wrong-resume", "failed-result"])
async def test_stream_does_not_treat_error_or_partial_output_as_acceptance(failure):
    class Runner:
        async def run_claude_stream(self, *args, **kwargs):
            yield {
                "type": "system",
                "subtype": "init",
                "session_id": "unknown" if failure == "no-session" else SESSION,
            }
            if failure == "error":
                yield {"type": "error", "message": "synthetic"}
            yield {
                "type": "result",
                "subtype": "error" if failure == "failed-result" else "success",
                "is_error": failure == "failed-result",
                "result": "wrong" if failure == "no-marker" else probe.MARKER,
            }

    with pytest.raises(probe.FixtureError):
        await probe.consume_stream(
            Runner(),
            {},
            session_id="22222222-2222-4222-8222-222222222222" if failure == "wrong-resume" else None,
        )


def bootstrap_fixture(work, tmp_path, monkeypatch):
    candidate = tmp_path / COMMIT
    (candidate / ".venv").mkdir(parents=True)
    binary = b"\x7fELFsynthetic CLI bytes; never executed"
    cli_digest = hashlib.sha256(binary).hexdigest()
    cli = tmp_path / "tools" / cli_digest / "claude"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(binary)
    cli.chmod(0o755)
    (candidate / ".deploy-revision").write_text(COMMIT)
    (candidate / ".cli-revision").write_text(cli_digest)
    helper_source = work / "probe.py"
    helper_source.write_bytes(b"synthetic root helper, never executed")
    helper_digest = hashlib.sha256(helper_source.read_bytes()).hexdigest()
    monkeypatch.setattr(probe, "__file__", str(helper_source))
    monkeypatch.setattr(probe, "locked", lambda _: None)
    monkeypatch.setattr(
        probe,
        "sys",
        types.SimpleNamespace(
            platform="linux",
            executable=str(candidate / ".venv/bin/python"),
            flags=types.SimpleNamespace(isolated=True, no_site=True, dont_write_bytecode=True),
        ),
    )
    monkeypatch.setattr(
        probe.pwd,
        "getpwuid",
        lambda _: types.SimpleNamespace(
            pw_uid=os.getuid(), pw_name="prism-integration", pw_gid=991, pw_shell="/usr/sbin/nologin"
        ),
    )
    monkeypatch.setattr(probe.os, "getgroups", lambda: [991])
    issued = 1800000000
    proof = {
        "schema": 1,
        "candidate": COMMIT,
        "engine": DIGEST,
        "nonce": NONCE,
        "pid": os.getpid(),
        "production_pid": os.getpid() + 1,
        "issued_at": issued,
        "checked_at": issued,
        "probe_sha256": helper_digest,
        "checks": dict.fromkeys(probe.BOOT_CHECKS, True),
    }
    allow = {key: proof[key] for key in ("schema", "candidate", "engine", "nonce", "pid", "probe_sha256")}
    allow.update(bootstrap_proof_sha256=hashlib.sha256(probe.encoded(proof)).hexdigest(), allowed_at=issued)
    monkeypatch.setattr(
        probe,
        "read_record",
        lambda path, **kwargs: (
            (allow, probe.encoded(allow)) if path.name == "root-allow.json" else (proof, probe.encoded(proof))
        ),
    )
    args = argparse.Namespace(
        candidate=COMMIT, cli_sha256=cli_digest, nonce=NONCE, probe_sha256=helper_digest
    )
    return args, candidate, cli, proof, allow, issued


def test_same_pid_bootstrap_is_required_before_candidate_import(work, tmp_path, monkeypatch):
    args, candidate, cli, _, _, issued = bootstrap_fixture(work, tmp_path, monkeypatch)
    assert probe.require_bootstrap(args, candidate, cli, work, now=lambda: issued) == issued


@pytest.mark.parametrize(
    "corruption", ["pid", "nonce", "candidate", "check", "schema-bool", "expired", "allow-hash", "allow-pid"]
)
def test_other_process_or_unbound_bootstrap_is_blocked(work, tmp_path, monkeypatch, corruption):
    args, candidate, cli, proof, allow, issued = bootstrap_fixture(work, tmp_path, monkeypatch)
    if corruption == "pid":
        proof["pid"] += 1
    elif corruption == "nonce":
        proof["nonce"] = "e" * 32
    elif corruption == "candidate":
        proof["candidate"] = "e" * 40
    elif corruption == "check":
        proof["checks"]["production_http_denied"] = False
    elif corruption == "schema-bool":
        proof["schema"] = True
    elif corruption == "allow-hash":
        allow["bootstrap_proof_sha256"] = "e" * 64
    elif corruption == "allow-pid":
        allow["pid"] += 1
    with pytest.raises(probe.FixtureError):
        probe.require_bootstrap(
            args, candidate, cli, work, now=lambda: issued + (121 if corruption == "expired" else 0)
        )


async def test_direct_function_cannot_bypass_missing_bootstrap(work, monkeypatch):
    candidate = Path("/opt/prism/releases") / COMMIT
    cli = Path("/opt/prism/tools/claude") / DIGEST / "claude"
    monkeypatch.setattr(probe, "fixed_paths", lambda *a: (candidate, cli, work))
    monkeypatch.setattr(probe, "require_bootstrap", mock.Mock(side_effect=probe.FixtureError("missing")))
    importer = mock.Mock(side_effect=AssertionError("must not import candidate before root allow"))
    result = await probe.run_protocol_fixture(
        candidate, DIGEST, work, nonce=NONCE, probe_sha256=DIGEST, importer=importer
    )
    assert result["passed"] is False and result["error_class"] == "same_pid_bootstrap_required"
    importer.assert_not_called()
    assert not (work / "output/cli-result.json").exists()


async def test_complete_fake_fixture_scope_is_never_live_model_or_search(work, monkeypatch):
    # Real helper invocation construction, but fake CLI children and transport.
    from server.services import auxiliary_model

    candidate = Path("/opt/prism/releases") / COMMIT
    cli = Path("/opt/prism/tools/claude") / DIGEST / "claude"
    endpoint, shared = "http://127.0.0.1:12345", {}
    config = types.SimpleNamespace(MODELS={"original": {}}, DEFAULT_MODEL="original", CLAUDE_BIN="original")
    old_models = config.MODELS
    monkeypatch.setattr(auxiliary_model, "config", config)
    monkeypatch.setattr(probe, "fixed_paths", lambda *a: (candidate, cli, work))
    monkeypatch.setattr(probe, "require_bootstrap", lambda *a: int(time.time()))
    monkeypatch.setattr(probe, "private_log", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(probe.ChildBoundary, "verify_child", lambda *a: None)
    monkeypatch.setattr(probe.tempfile, "tempdir", str(work / "tmp"))

    @contextlib.contextmanager
    def server(state):
        shared["state"] = state
        yield endpoint

    monkeypatch.setattr(probe, "protocol_server", server)

    class Child:
        returncode = 0
        pid = 99123

        def __init__(self, command, *args, **kwargs):
            self.args = command
            shared["state"].accept("/v1/messages", {"x-api-key": probe.TOKEN}, {"model": probe.MODEL})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def communicate(self, *args, **kwargs):
            return probe.MARKER.encode(), None

        def poll(self):
            return 0

        def wait(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(probe.subprocess, "Popen", Child)

    class Runner:
        async def run_claude_stream(self, prompt, **kwargs):
            with subprocess.Popen(
                stream_command(cli, resume=bool(kwargs["session_id"])), env=child_environment(work, endpoint)
            ):
                yield {"type": "system", "subtype": "init", "session_id": SESSION}
                yield {"type": "result", "subtype": "success", "is_error": False, "result": probe.MARKER}

    result = await probe.run_protocol_fixture(
        candidate,
        DIGEST,
        work,
        nonce=NONCE,
        probe_sha256=DIGEST,
        importer=lambda *a: (config, Runner(), auxiliary_model),
    )
    assert result["passed"] is result["cli_protocol_fixture_verified"] is True
    assert result["fixture_model_configuration_override"] is True
    assert (
        result["live_model_verified"]
        is result["native_search_verified"]
        is result["native_fetch_verified"]
        is False
    )
    assert config.MODELS is old_models and config.DEFAULT_MODEL == "original"
    assert all(result["checks"].values())
    assert all(value["messages"] == 1 for value in result["requests"].values())
    assert probe.TOKEN not in json.dumps(result) and probe.PROMPT not in json.dumps(result)
    assert json.loads((work / "output/cli-result.json").read_bytes()) == result


def test_result_cannot_overwrite_earlier_attempt_or_follow_link(work):
    probe.write_result(work, {"passed": False})
    with pytest.raises(FileExistsError):
        probe.write_result(work, {"passed": True})


def test_bootstrap_record_must_be_private_regular_single_link(work):
    path = work / "output/proof.json"
    path.write_text('{"schema":1}')
    path.chmod(0o600)
    assert probe.read_record(path)[0] == {"schema": 1}
    path.chmod(0o644)
    with pytest.raises(probe.FixtureError):
        probe.read_record(path)
    link = work / "output/symlink.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        probe.read_record(link)


def test_bad_entry_arguments_are_nonzero_and_safe(capfd):
    assert probe.main(["--private", "synthetic-never-printed"]) == 1
    text = capfd.readouterr().out
    assert "synthetic-never-printed" not in text
    assert json.loads(text)["passed"] is False
