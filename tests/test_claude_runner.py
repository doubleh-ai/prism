"""Claude runner streaming safeguards."""

import asyncio

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


@pytest.mark.asyncio
async def test_claude_stream_idle_timeout_emits_error_and_cleans_process(monkeypatch, tmp_path):
    from server.services import claude_runner

    class HangingStdout:
        async def read(self, _n):
            await asyncio.sleep(60)
            return b""

    class EmptyStderr:
        async def read(self, _n=-1):
            return b""

    class DummyStdin:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.stdout = HangingStdout()
            self.stderr = EmptyStderr()
            self.stdin = DummyStdin()
            self.returncode = None
            self.terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def kill(self):
            self.returncode = -9

        async def wait(self):
            return self.returncode or 0

    fake_proc = FakeProcess()

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    async def fake_proxy_config(*_args, **_kwargs):
        return None

    monkeypatch.setattr(claude_runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(claude_runner, "get_proxy_config", fake_proxy_config)
    monkeypatch.setattr(claude_runner, "_resolve_skill_dir", lambda _name: (tmp_path, [tmp_path]))
    monkeypatch.setattr(claude_runner, "CHAT_STREAM_HEARTBEAT_SECONDS", 0.1)
    monkeypatch.setattr(claude_runner, "CHAT_STREAM_IDLE_TIMEOUT_SECONDS", 0.1)

    events = []
    async for ev in claude_runner.run_claude_stream(
        "question",
        model_cfg={"model": "unit-model", "base_url": "", "api_key": ""},
        conv_id="conv-idle",
    ):
        events.append(ev)

    assert events
    assert events[-1]["type"] == "error"
    assert "没有新内容" in events[-1]["message"]
    assert fake_proc.terminated is True


@pytest.mark.asyncio
async def test_actual_cancelled_process_emits_cancelled_and_preserves_new_process(monkeypatch, tmp_path):
    from server.services import claude_runner as runner

    started = asyncio.Event()
    stopped = asyncio.Event()
    replacement = object()

    class Stdout:
        async def read(self, _n):
            started.set()
            await stopped.wait()
            return b""

    class Stderr:
        async def read(self, _n):
            return b""

    class Process:
        pid = 4321
        stdout = Stdout()
        stderr = Stderr()
        returncode = None

        def terminate(self):
            self.returncode = -15
            stopped.set()

        def kill(self):
            self.returncode = -9
            stopped.set()

        async def wait(self):
            # A new request has started in the same conversation while the old
            # process was finishing. Neither cleanup path may remove it.
            runner._active_procs["conv-cancel-race"] = replacement
            return self.returncode

    proc = Process()

    async def spawn(*args, **kwargs):
        return proc

    async def proxy(*args, **kwargs):
        return None

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(runner, "get_proxy_config", proxy)
    monkeypatch.setattr(runner, "_resolve_skill_dir", lambda _name: (tmp_path, [tmp_path]))

    async def consume():
        return [
            event
            async for event in runner.run_claude_stream(
                "question",
                model_cfg={"model": "unit-model", "base_url": "", "api_key": ""},
                conv_id="conv-cancel-race",
            )
        ]

    try:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=2)
        assert await runner.cancel_process("conv-cancel-race") is True
        events = await asyncio.wait_for(task, timeout=2)
        assert [event["type"] for event in events] == ["cancelled"]
        assert id(proc) not in runner._cancelled_procs
        assert runner._active_procs["conv-cancel-race"] is replacement
    finally:
        runner._active_procs.pop("conv-cancel-race", None)
        runner._cancelled_procs.discard(id(proc))


@pytest.mark.asyncio
async def test_missing_process_cancellation_cannot_mark_unrelated_process():
    from server.services import claude_runner as runner

    before = set(runner._cancelled_procs)
    assert await runner.cancel_process("absent-synthetic-conversation") is False
    assert runner._cancelled_procs == before


@pytest.mark.asyncio
async def test_cancel_keeps_process_visible_until_killed_child_is_reaped(monkeypatch):
    from server.services import claude_runner as runner

    class Process:
        returncode = None
        waits = 0
        killed = False

        def terminate(self):
            pass

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waits += 1
            assert runner._active_procs["cancel-reap"] is self
            if self.waits == 1:
                raise TimeoutError
            assert self.killed
            self.returncode = -9
            return -9

    process = Process()
    monkeypatch.setitem(runner._active_procs, "cancel-reap", process)
    try:
        assert await runner.cancel_process("cancel-reap")
        assert process.waits == 2
        assert "cancel-reap" not in runner._active_procs
    finally:
        runner._active_procs.pop("cancel-reap", None)
        runner._cancelled_procs.discard(id(process))


@pytest.mark.asyncio
async def test_unconfirmed_cancel_remains_visible_and_does_not_report_success(monkeypatch):
    from server.services import claude_runner as runner

    class Process:
        returncode = None

        def terminate(self):
            pass

        def kill(self):
            pass

        async def wait(self):
            raise TimeoutError

    process = Process()
    monkeypatch.setitem(runner._active_procs, "cancel-unconfirmed", process)
    try:
        assert not await runner.cancel_process("cancel-unconfirmed")
        assert runner._active_procs["cancel-unconfirmed"] is process
    finally:
        runner._active_procs.pop("cancel-unconfirmed", None)
        runner._cancelled_procs.discard(id(process))
