"""Claude runner streaming safeguards."""
import asyncio

import pytest


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
