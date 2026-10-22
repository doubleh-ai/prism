"""Real local transport/children: cancellation may detach waiters, never ownership."""

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from server import capabilities, database, proxy
from server.research_context import build_research_context
from server.services import claude_runner as runner
from server.services import process_lease
from server.work_lifecycle import Manager

# Fixed external I/O fixture, derived from the frozen application matrix CLI.
# No tools, network, model provider, variable commands or real credentials.
CLI = """import json,sys
print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"matrix partial"}]}}),flush=True)
sys.stdin.buffer.read(1)
print(json.dumps({"type":"result","is_error":False}),flush=True)
"""


@pytest.fixture
def fixed_cli(monkeypatch, tmp_path):
    from server.research import init_research_db

    # The shared DB fixture initializes base tables; this case also uses real
    # current-turn revision persistence in its own freshly isolated database.
    init_research_db()
    cli = tmp_path / "cli"
    cli.write_text(f"#!{sys.executable}\n{CLI}")
    cli.chmod(0o700)
    monkeypatch.setattr(runner, "CLAUDE_BIN", str(cli))
    monkeypatch.setattr(runner, "_resolve_skill_dir", lambda name: (tmp_path, [tmp_path]))

    async def no_tcp(*args, **kwargs):
        return False

    # Only the external network probe is replaced; all app/resource owners stay real.
    monkeypatch.setattr(proxy, "_check_tcp", no_tcp)
    return cli


def counts():
    with database.get_db() as db:
        return {
            "assistant_messages": db.execute(
                "SELECT COUNT(*) FROM messages WHERE role='assistant'"
            ).fetchone()[0],
            "revisions": db.execute("SELECT COUNT(*) FROM research_revisions").fetchone()[0],
            "active_grants": db.execute("SELECT COUNT(*) FROM capability_runs WHERE revoked=0").fetchone()[0],
        }


def ledger(manager):
    result = manager.observe()
    with manager._lock:
        result["process_rows"] = [
            {"returncode": row["process"].returncode, "reaped": row["reaped"]}
            for lease in manager._leases.values()
            for row in lease._processes.values()
        ]
    return result


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="requires local Unix sockets")
async def test_real_uds_disconnect_reaps_and_finishes_durable_turn(fixed_cli, test_user, tmp_path):
    from server import main as application

    absent = object()
    prior_bindings = {
        name: getattr(application.app.state, name, absent) for name in ("work_manager", "work_runtime")
    }
    token = database.create_session(test_user["username"])
    with database.get_db() as db:
        db.execute("INSERT INTO conversations(id,username) VALUES('uds-reap',?)", [test_user["username"]])
    # RUNROOT is deliberately short on macOS; no IP listener is opened.
    uds = tmp_path / "u.sock"
    assert len(str(uds).encode()) < 100
    server = uvicorn.Server(
        uvicorn.Config(
            application.app,
            uds=str(uds),
            loop="asyncio",
            http="h11",
            lifespan="on",
            log_level="critical",
            access_log=False,
            timeout_graceful_shutdown=2,
        )
    )
    serving = asyncio.create_task(server.serve())
    timeline = []
    started = time.monotonic()
    manager = None
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.005)
        manager = application.app.state.work_manager
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(uds)),
            base_url="http://synthetic.invalid",
            timeout=5,
            cookies={"token": token},
            headers={"X-Requested-With": "XMLHttpRequest"},
        ) as client:
            async with client.stream(
                "POST",
                "/api/chat/stream",
                json={
                    "question": "public synthetic current turn",
                    "conv_id": "uds-reap",
                    "skill": "prism-research",
                    "model": "minimax-m3",
                },
            ) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if "matrix partial" in line:
                        timeline.append(
                            {"event": "partial_before_real_disconnect", "ledger": ledger(manager)}
                        )
                        break
                else:
                    pytest.fail("fixed CLI partial was not observed")
        async with asyncio.timeout(5):
            while True:
                observed = manager.observe()
                if not (
                    observed["pending_processes"]
                    or observed["persist_pending"]
                    or observed["pending_requests"]
                ):
                    break
                await asyncio.sleep(0.01)
        timeline.append({"event": "after_real_disconnect", "ledger": ledger(manager), "durable": counts()})
        assert counts() == {"assistant_messages": 1, "revisions": 1, "active_grants": 0}
        assert not capabilities._run_bindings
        assert "uds-reap" not in runner._active_procs
        manager.begin_drain(manager.ticket())
        assert await manager.wait_drained(timeout=1)
        timeline.append({"event": "actual_drain", "ledger": ledger(manager)})
        server.should_exit = True
        await asyncio.wait_for(asyncio.shield(serving), 5)
        assert time.monotonic() - started < 30
    finally:
        if manager is not None:
            timeline.append({"event": "final_observation", "ledger": ledger(manager), "durable": counts()})
        proof = os.environ.get("PRISM_REAP_PROOF")
        if proof:
            Path(proof).write_text(
                json.dumps(
                    {
                        "local_macos_only": sys.platform == "darwin",
                        "linux_acceptance": False,
                        "release_eligible": False,
                        "elapsed": round(time.monotonic() - started, 3),
                        "timeline": timeline,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        server.should_exit = True
        if not serving.done():
            serving.cancel()
        with contextlib.suppress(BaseException):
            await serving
        if serving.done() and manager is not None:
            final = manager.observe()
            if final["state"] == "closed" and not any(final["leases"].values()):
                # Other HTTP tests reuse this app without running its lifespan.
                # Restore only prior bindings after the actual runtime closed;
                # a failed/uncertain cleanup must keep its real owner visible.
                assert application.app.state.work_manager is manager
                for name, previous in prior_bindings.items():
                    if previous is absent:
                        delattr(application.app.state, name)
                    else:
                        setattr(application.app.state, name, previous)


@pytest.fixture
def local_manager():
    manager = Manager()
    yield manager
    manager._pool.shutdown(wait=True, cancel_futures=False)


def stream_for(manager, **kwargs):
    root = manager.admit("request")
    return root, runner.run_claude_stream(
        "public synthetic",
        model_cfg={"model": "synthetic", "api_key": "synthetic"},
        parent_lease=root,
        **kwargs,
    )


async def test_unknown_identity_still_stops_real_child_and_stays_blocked(
    fixed_cli, local_manager, monkeypatch
):
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        return process

    def unknown(pid):
        raise process_lease.ProcessIdentityUnknown("synthetic_kernel_probe_unavailable")

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(process_lease, "process_start_ticks", unknown)
    root, stream = stream_for(local_manager, conv_id="unknown-birth")
    try:
        assert (await anext(stream))["type"] == "assistant"
        assert local_manager.observe()["blocked"] == 1
        root.release()
        await asyncio.wait_for(stream.aclose(), 3)
        assert await asyncio.wait_for(processes[0].wait(), 1) == -15
        assert "unknown-birth" not in runner._active_procs
        local_manager.begin_drain(local_manager.ticket())
        assert not await local_manager.wait_drained(timeout=0.01)
        assert local_manager.observe()["blocked"] == 1
    finally:
        root.release()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_repeated_cancel_detaches_waiter_not_real_reap_or_grant_revocation(
    fixed_cli, local_manager, monkeypatch, test_user
):
    waited, release = asyncio.Event(), asyncio.Event()
    processes = []
    real_spawn = asyncio.create_subprocess_exec
    with database.get_db() as db:
        db.execute(
            "INSERT INTO conversations(id,username) VALUES('cancel-reap-owned',?)", [test_user["username"]]
        )

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        real_wait = process.wait

        async def delayed_wait():
            code = await real_wait()
            waited.set()
            await release.wait()
            return code

        process.wait = delayed_wait
        processes.append(process)
        return process

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    root, stream = stream_for(
        local_manager,
        user=test_user["username"],
        conv_id="cancel-reap-owned",
        research_context=build_research_context(run_id="cancel-reap-run"),
    )
    try:
        assert (await anext(stream))["type"] == "assistant"
        root.release()
        closing = asyncio.create_task(stream.aclose())
        await asyncio.wait_for(waited.wait(), 2)
        assert processes[0].returncode == -15
        assert "cancel-reap-run" not in capabilities._run_bindings
        assert counts()["active_grants"] == 0
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        local_manager.begin_drain(local_manager.ticket())
        assert local_manager.observe()["pending_processes"] == 1
        assert not await local_manager.wait_drained(timeout=0.01)
        release.set()
        assert await local_manager.wait_drained(timeout=2)
        assert "cancel-reap-owned" not in runner._active_procs
        local_manager.close_development()
    finally:
        release.set()
        root.release()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_cancel_during_actual_launch_keeps_cleanup_until_spawn_resolves(
    fixed_cli, local_manager, monkeypatch
):
    born, return_process = asyncio.Event(), asyncio.Event()
    processes = []
    real_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        process = await real_spawn(*args, **kwargs)
        processes.append(process)
        born.set()
        await return_process.wait()
        return process

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    root, stream = stream_for(local_manager)
    reading = asyncio.create_task(anext(stream))
    try:
        await asyncio.wait_for(born.wait(), 2)
        reading.cancel()
        await asyncio.sleep(0)
        reading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reading
        root.release()
        local_manager.begin_drain(local_manager.ticket())
        assert not await local_manager.wait_drained(timeout=0.01)
        return_process.set()
        assert await local_manager.wait_drained(timeout=3)
        assert await processes[0].wait() == -15
        local_manager.close_development()
    finally:
        return_process.set()
        root.release()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_normal_eof_still_uses_registered_actual_wait(fixed_cli, local_manager, monkeypatch):
    processes, waits = [], []
    real_spawn, actual_wait = asyncio.create_subprocess_exec, runner.wait_process

    async def spawn(*args, **kwargs):
        process = await real_spawn(sys.executable, "-u", "-c", "import time;time.sleep(.05)", **kwargs)
        processes.append(process)
        return process

    async def observed_wait(lease, process, registered):
        waits.append({"registered": registered, "returncode": process.returncode})
        return await actual_wait(lease, process, registered)

    monkeypatch.setattr(runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(runner, "wait_process", observed_wait)
    root, stream = stream_for(local_manager)
    try:
        assert [event async for event in stream] == []
        root.release()
        assert waits and all(row["registered"] for row in waits)
        assert processes[0].returncode == 0
        local_manager.begin_drain(local_manager.ticket())
        assert await local_manager.wait_drained(timeout=2)
        local_manager.close_development()
    finally:
        root.release()
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
