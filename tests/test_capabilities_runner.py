"""Runner config and lifecycle without launching Claude or network access."""

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def mocked_registration_adapter(monkeypatch):
    """Legacy launcher mocks explicitly replace registration, never product gates."""
    from server.services import claude_runner as runner_adapter
    from server.services import process_lease

    monkeypatch.setattr(process_lease, "register_process", lambda lease, process: False)

    async def wait(lease, process, registered):
        return await process.wait()

    monkeypatch.setattr(runner_adapter, "wait_process", wait)


from server import capabilities
from server.database import get_db
from server.research_context import build_research_context
from server.services import claude_runner as runner


@pytest.fixture
def run_context(test_user, monkeypatch, tmp_path):
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('runner-conv',?)", [test_user["username"]])
    monkeypatch.setattr(runner, "_resolve_skill_dir", lambda _name: (tmp_path, [tmp_path]))

    async def proxy():
        return None

    monkeypatch.setattr(runner, "get_proxy_config", proxy)
    return test_user["username"], build_research_context(run_id="runner-run")


class FakeProcess:
    pid = 4242
    stdin = None

    def __init__(self, payload=b"", code=0):
        self.returncode = code
        self.payload = payload
        self.stdout = self
        self.stderr = self

    async def read(self, _n):
        payload, self.payload = self.payload, b""
        return payload

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


async def test_runner_restricts_tools_token_not_argv_and_emits_owned_calls(run_context, monkeypatch):
    user, research = run_context
    captured = {}

    async def spawn(*argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        # Simulate a platform tool while the CLI is running, without an HTTP call.
        response = await capabilities.dispatch_call(
            {"username": user, "conv_id": "runner-conv", "run_id": research["run_id"]},
            capabilities.CallRequest(name="industry_overview", request_id="runner-call"),
        )
        assert response["ok"]
        return FakeProcess(
            (
                json.dumps({"type": "assistant", "text": kwargs["env"]["PRISM_CAPABILITY_TOKEN"]}) + "\n"
            ).encode()
        )

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-inherit")
    events = [
        event
        async for event in runner.run_claude_stream(
            "research request",
            user=user,
            model_cfg={"model": "unit-model", "api_key": "unit-model-secret"},
            conv_id="runner-conv",
            research_context=research,
        )
    ]
    argv = captured["argv"]
    token = captured["env"]["PRISM_CAPABILITY_TOKEN"]
    assert captured["env"]["DISABLE_UPDATES"] == captured["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert token not in json.dumps(argv)
    assert "unit-model-secret" not in json.dumps(argv)
    assert "UNRELATED_SECRET" not in captured["env"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch,AskUserQuestion"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--setting-sources") + 1] == ""
    mcp = json.loads(argv[argv.index("--mcp-config") + 1])
    assert set(mcp["mcpServers"]) == {"prism"}
    assert "env" not in mcp["mcpServers"]["prism"]
    assert argv.count("--append-system-prompt") == 1
    assert research["as_of"] in argv[argv.index("--append-system-prompt") + 1]
    assert token not in json.dumps(events)
    calls = [event["call"] for event in events if event["type"] == "platform_call"]
    assert len(calls) == 1 and calls[0]["run_id"] == research["run_id"]
    with get_db() as db:
        assert (
            db.execute("SELECT revoked FROM capability_runs WHERE run_id=?", [research["run_id"]]).fetchone()[
                0
            ]
            == 1
        )


async def test_spawn_failure_revokes_token(run_context, monkeypatch):
    user, research = run_context

    async def spawn(*argv, **kwargs):
        raise OSError("mock missing CLI")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError):
        _ = [
            event
            async for event in runner.run_claude_stream(
                "question",
                user=user,
                model_cfg={"model": "unit"},
                conv_id="runner-conv",
                research_context=research,
            )
        ]
    with get_db() as db:
        assert (
            db.execute("SELECT revoked FROM capability_runs WHERE run_id=?", [research["run_id"]]).fetchone()[
                0
            ]
            == 1
        )


async def test_cancellation_revokes_token_and_terminates_process(run_context, monkeypatch):
    user, research = run_context
    proc = FakeProcess(code=None)
    started = asyncio.Event()

    async def read(_n):
        started.set()
        await asyncio.sleep(100)

    proc.read = read

    async def spawn(*argv, **kwargs):
        return proc

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)

    async def consume():
        return [
            event
            async for event in runner.run_claude_stream(
                "question",
                user=user,
                model_cfg={"model": "unit"},
                conv_id="runner-conv",
                research_context=research,
            )
        ]

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode == -15
    with get_db() as db:
        assert (
            db.execute("SELECT revoked FROM capability_runs WHERE run_id=?", [research["run_id"]]).fetchone()[
                0
            ]
            == 1
        )


async def test_missing_legacy_skill_explicitly_falls_back_and_unknown_rejected(
    run_context, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        runner,
        "_resolve_skill_dir",
        lambda name: (tmp_path, [tmp_path]) if name == "prism-research" else (None, []),
    )

    async def spawn(*argv, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    events = [
        event
        async for event in runner.run_claude_stream(
            "question", skill_name="bottleneck-hunter", model_cfg={"model": "unit"}
        )
    ]
    assert events[0]["type"] == "status"
    assert "内置" in events[0]["message"]
    unknown = [
        event
        async for event in runner.run_claude_stream(
            "question", skill_name="unregistered-skill", model_cfg={"model": "unit"}
        )
    ]
    assert unknown[0]["type"] == "error"


@pytest.mark.parametrize(
    "requested", ["../outside", "/tmp/outside", "folder/child", "folder\\child", "unregistered"]
)
async def test_auxiliary_skill_cannot_read_unregistered_paths(tmp_path, monkeypatch, requested):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("private sentinel must not enter a prompt")
    launched = False

    async def spawn(*argv, **kwargs):
        nonlocal launched
        launched = True
        return FakeProcess()

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    events = [
        event
        async for event in runner.run_claude_stream(
            "question", extra_skills=[requested], model_cfg={"model": "unit"}
        )
    ]
    assert not launched
    assert events[0]["type"] == "error"
    assert "private sentinel" not in json.dumps(events)


def test_skill_directory_and_instruction_symlinks_cannot_escape_root(tmp_path, monkeypatch):
    root = tmp_path / "approved"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "CLAUDE.md").write_text("private sentinel must not enter a prompt")
    monkeypatch.setattr(runner, "SKILLS_HOME", root)
    monkeypatch.setattr(runner, "SKILL_FALLBACK_ROOTS", [])
    directory = root / "serenity-bottleneck-hunter"
    directory.symlink_to(outside, target_is_directory=True)
    assert runner._resolve_skill_dir("serenity-bottleneck-hunter")[0] is None
    directory.unlink()
    directory.mkdir()
    (directory / "CLAUDE.md").symlink_to(outside / "CLAUDE.md")
    assert runner._resolve_skill_dir("serenity-bottleneck-hunter")[0] is None
    assert runner._candidate_skill_dirs(str(outside)) == []
    assert runner._candidate_skill_dirs("../outside") == []
    (directory / "CLAUDE.md").unlink()
    (directory / "CLAUDE.md").write_text("registered method")
    assert runner._resolve_skill_dir("serenity-bottleneck-hunter")[0] == directory
