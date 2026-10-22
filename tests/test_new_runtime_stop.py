"""Offline contracts only; none of these mocks certifies actual Linux acceptance."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import sys
import threading
import types
from pathlib import Path

import pytest

from scripts import new_runtime_stop as stop


@pytest.fixture
def request_value():
    return {
        "commit": "a" * 40,
        "release_id": "prism-new-stop-test",
        "attempt": "new-stop-attempt-001",
        "role": "candidate",
        "candidate": "b" * 40,
        "archive_sha256": "c" * 64,
        "environment_sha256": "d" * 64,
    }


@pytest.fixture
def plan(request_value):
    return stop.descriptor(request_value, nonce="e" * 32)


def worker_functions():
    namespace = {"__name__": "offline_worker_functions"}
    exec(compile(stop.WORKER_SOURCE, "<fixed-stop-worker>", "exec"), namespace)
    return namespace


def census(plan, phase):
    after = phase == "after"
    return {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": 90001,
        "at_us": {"before": 100, "during": 300, "after": 500}[phase],
        "phase": phase,
        "future_id": 111,
        "future_done": after,
        "future_cancelled": False,
        "analysis_id": "f" * 32,
        "analysis_status": "done" if after else "running",
        "executor_thread_id": 222,
        "executor_thread_alive": not after,
        "executor_finished": after,
        "persistence_saved": after,
        "server_unwound": phase != "before",
        "uvloop_running": True,
        "asyncio_pending_task_count": 0 if after else 2,
        "live_thread_ids": [333] if after else [222, 333],
    }


def kernel_value(plan):
    return {
        "pid": 90001,
        "start_ticks": 77,
        "invocation_id": "1" * 32,
        "uids": [61000] * 4,
        "gids": [61000] * 4,
        "network_namespace_inode": 100,
        "host_network_namespace_inode": 101,
        "mount_namespace_inode": 200,
        "host_mount_namespace_inode": 201,
        "exe_device": 10,
        "exe_inode": 11,
        "control_group": plan["control_group"],
        "members": [90001],
        "runtime_device": 12,
        "runtime_inode": 13,
        "mounts": {
            point: {
                "device": 14,
                "inode": i + 20,
                "mode": 0 if point != "/run" else 0o755,
                "host_device": 15,
                "host_inode": i + 30,
                "fstype": "tmpfs",
                "readonly": True,
            }
            for i, point in enumerate(("/run", "/run/systemd", "/run/dbus"))
        },
        "readonly_paths": {
            point: {"mountpoint": point, "options": ["ro", "nosuid"], "fstype": "tmpfs"}
            for point in (
                "/home",
                str(stop.RELEASES / plan["request"]["candidate"]),
                str(stop.SOURCES / stop.WORKER_SHA256),
            )
        },
        "no_new_privs": 1,
        "capabilities": {k: 0 for k in ("CapEff", "CapPrm", "CapBnd", "CapInh", "CapAmb")},
    }


def policy_state(plan, own, *, after=False):
    value = dict.fromkeys(stop.PROPERTIES.split(","), "")
    value.update(stop.POLICY)
    value.update(stop.EXTRA_POLICY)
    value.update(
        LoadState="loaded",
        ActiveState="inactive" if after else "active",
        SubState="dead" if after else "running",
        MainPID="0" if after else str(own["pid"]),
        ExecMainPID=str(own["pid"]),
        InvocationID=own["invocation_id"],
        ControlGroup=plan["control_group"],
        Transient="yes",
        DynamicUser="yes",
        Job="0",
    )
    if after:
        value.update(
            ExecMainCode="1", ExecMainStatus="0", Result="success", ExecMainExitTimestampMonotonic="600"
        )
    return value


def observer_value(plan):
    value = dict.fromkeys(stop.OBSERVER_PROPERTIES.split(","), "")
    value.update(
        LoadState="loaded",
        ActiveState="active",
        SubState="exited",
        MainPID="0",
        ExecMainPID="90002",
        InvocationID="2" * 32,
        ControlGroup="/system.slice/" + plan["observer"],
        ExecMainCode="1",
        ExecMainStatus="0",
        Result="success",
        Transient="yes",
        DynamicUser="yes",
        RemainAfterExit="yes",
        Wants=plan["unit"],
        Job="0",
    )
    return {
        "invocation_id": "2" * 32,
        "exec_main_pid": 90002,
        "control_group": "/system.slice/" + plan["observer"],
        "empty": True,
        "state": value,
        "job": {"type": "(uo)", "data": [0, "/"]},
        "private_tmp_dependency": None,
    }


def canaries(plan):
    return {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": 90001,
        "checks": {
            k: 13
            for k in (
                "production_read_errno",
                "other_process_read_errno",
                "production_write_errno",
                "candidate_write_errno",
                "private_run_write_errno",
                "inet_socket_errno",
                "systemd_socket_errno",
                "dbus_socket_errno",
            )
        },
    }


def artifacts(plan, monkeypatch):
    monkeypatch.setattr(stop, "file_hash", lambda path: "9" * 64)
    request = plan["request"]
    own = kernel_value(plan)
    sources = {
        key: "8" * 64
        for key in (
            "candidate_source_sha256",
            "uvicorn_sha256",
            "uvloop_binary_sha256",
            "analysis_engine_sha256",
            "database_sha256",
            "config_sha256",
        )
    }
    sources.update(
        environment_sha256=request["environment_sha256"], system_python_sha256=stop.SYSTEM_PYTHON_SHA256
    )
    sources.update(
        {name + "_sha256": value for name, value in stop.APPLICATION_SOURCES[request["role"]].items()}
    )
    identity = {
        "schema": 1,
        "request": request,
        "plan": plan,
        "sources": sources,
        "helper_sha256": "9" * 64,
        "worker_sha256": stop.WORKER_SHA256,
        "boot_id": "00000000-1111-2222-3333-444444444444",
    }
    boot = {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": 90001,
        "candidate_imported": False,
        "uvicorn_imported": False,
        "uvloop_imported": False,
    }
    before, during, after = [census(plan, p) for p in ("before", "during", "after")]
    result = {
        "schema": 1,
        "kind": "analysis_executor_database",
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "analysis_id": before["analysis_id"],
        "owner": "newstop_" + plan["nonce"][:16],
        "status": "done",
        "signal": "hold",
        "report_marker": "PRISM_SYNTHETIC_STOP_RESULT_197",
        "foreign_owner_denied": True,
        "cache_readback": True,
        "database_sha256": "7" * 64,
        "source_modules": {
            name: sources[name + "_sha256"] for name in ("analysis_engine", "database", "config")
        },
    }
    finished = {
        "schema": 1,
        "binding_sha256": plan["binding_sha256"],
        "nonce": plan["nonce"],
        "pid": 90001,
        "python_version": "3.12.3",
        "uvicorn_version": "0.34.3",
        "uvloop_version": "0.23.0",
        "uvicorn_sha256": sources["uvicorn_sha256"],
        "uvloop_binary_sha256": sources["uvloop_binary_sha256"],
        "before_sha256": stop.digest(stop.encoded(before)),
        "during_sha256": stop.digest(stop.encoded(during)),
        "after_sha256": stop.digest(stop.encoded(after)),
        "result_sha256": stop.digest(stop.encoded(result)),
    }
    empty = {"cgroup_v2": True, "root_exists": False, "groups": []}
    observer = observer_value(plan)
    summary = {
        "schema": 1,
        "request": request,
        "actual_linux": True,
        "passed": True,
        "error_class": None,
        "operation": "systemctl_stop_no_block",
        "python_version": "3.12.3",
        "uvicorn_version": "0.34.3",
        "uvloop_version": "0.23.0",
        "clean_exit_zero": True,
        "recursive_cgroup_empty": True,
        "pending_results_preserved": True,
        "task_census_sha256": stop.digest(stop.encoded([before, during, after])),
        "stop_properties_sha256": stop.digest(stop.encoded(stop.POLICY)),
        "helper_sha256": "9" * 64,
        "worker_sha256": stop.WORKER_SHA256,
        "completed_at_us": 700,
        "cleanup_observed": True,
        "production_verified": False,
        "production_tasks_drained": False,
        "proof_scope": "analysis_executor_database",
    }
    details = {
        "sources_verified": identity,
        "start_intent": {"nonce": plan["nonce"], "unit": plan["unit"]},
        "policy_verified": policy_state(plan, own),
        "exec_arrays_verified": {
            key: copy.deepcopy(stop.EMPTY_EXEC_ARRAY) for key in ("ExecStop", "ExecStopPost")
        },
        "typed_job_verified": {"type": "(uo)", "data": [0, "/"]},
        "kernel_bootstrap": {
            "identity": own,
            "bootstrap": boot,
            "bootstrap_sha256": stop.digest(stop.encoded(boot)),
        },
        "canaries_verified": canaries(plan),
        "census_before": before,
        "observer_start_intent": {"unit": plan["observer"]},
        "observer_ready": observer,
        "stop_intent": {"pid": 90001, "start_ticks": 77, "invocation_id": "1" * 32, "at_us": 200},
        "census_during": during,
        "stop_waiting": {
            "job": 8,
            "typed_job": {"type": "(uo)", "data": [8, "/org/freedesktop/systemd1/job/8"]},
            "pid": 90001,
            "invocation_id": "1" * 32,
            "active_state": "deactivating",
            "sub_state": "stop-sigterm",
            "identity": own,
        },
        "work_release_intent": {"at_us": 400, "during_sha256": stop.digest(stop.encoded(during))},
        "census_after": after,
        "result_preserved": {"finished": finished, "result": result, "release_at_us": 400},
        "exit_observed": {
            "pid": 90001,
            "invocation_id": "1" * 32,
            "exit_code": 1,
            "exit_status": 0,
            "job": {"type": "(uo)", "data": [0, "/"]},
            "result": "success",
            "exit_at_us": 600,
            "cgroup": empty,
            "state": policy_state(plan, own, after=True),
        },
        "observer_release_intent": observer,
        "observer_released": {
            "state": dict.fromkeys(stop.OBSERVER_PROPERTIES.split(","), "") | {"LoadState": "not-found"},
            "cgroup": empty,
            "job": None,
            "private_tmp_dependency": None,
        },
        "sources_reverified": sources,
        "completed": summary,
    }
    details["stop_committed"] = copy.deepcopy(details["stop_intent"])
    return identity, details


def chain(identity, details):
    tail = "0" * 64
    lines = []
    for sequence, name in enumerate(stop.SUCCESS_EVENTS, 1):
        line = (
            stop.encoded(
                {"sequence": sequence, "previous_sha256": tail, "event": name, "details": details[name]}
            )
            + b"\n"
        )
        lines.append(line)
        tail = stop.digest(line)
    receipt = {
        "schema": 1,
        "identity_sha256": stop.digest(stop.encoded(identity)),
        "summary": details["completed"],
        "events": len(lines),
        "last_event_sha256": tail,
    }
    return b"".join(lines), receipt


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("role", "production"),
        ("candidate", "x" * 40),
        ("commit", True),
        ("archive_sha256", "z" * 64),
        ("environment_sha256", "a" * 63),
        ("attempt", "../escape"),
        ("attempt", "a" * 41),
        ("release_id", "../escape"),
    ],
)
def test_invalid_request_rejected(request_value, key, value):
    request_value[key] = value
    with pytest.raises(stop.StopError, match="request_rejected"):
        stop.valid_request(request_value)


def test_request_exact_shape(request_value):
    request_value["path"] = "/home/ubuntu"
    with pytest.raises(stop.StopError):
        stop.valid_request(request_value)


@pytest.mark.parametrize("role", ["candidate", "recovery"])
def test_descriptor_and_actual_command_are_finite(request_value, role):
    request_value["role"] = role
    plan = stop.descriptor(request_value, nonce="e" * 32)
    worker = stop.SOURCES / stop.WORKER_SHA256 / "worker.py"
    cmd = stop.command(plan, worker)
    props = {
        part[len("--property=") :].split("=", 1)[0]: part.split("=", 2)[2]
        for part in cmd
        if part.startswith("--property=")
    }
    assert props["KillSignal"] == "SIGINT" and props["KillMode"] == "process"
    assert {key: props[key] for key in ("SendSIGKILL", "SendSIGHUP", "Restart")} == {
        "SendSIGKILL": "no",
        "SendSIGHUP": "no",
        "Restart": "no",
    }
    assert props["TimeoutStopSec"] == "infinity" and props["PrivateNetwork"] == "yes"
    assert props["RestrictAddressFamilies"] == "AF_UNIX" and props["DynamicUser"] == "yes"
    assert props["ProtectHome"] == "yes" and props["NoNewPrivileges"] == "yes"
    i = cmd.index("--")
    argv = cmd[i + 1 :]
    assert argv[:7] == [
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        str(stop.RELEASES / ("b" * 40) / ".venv/bin/python"),
        "-I",
        "-B",
        "-S",
    ]
    assert argv[7] == str(worker) and json.loads(argv[8])["runtime"] == plan["runtime"]
    assert len(plan["runtime"] + "/server.sock") < 108
    assert "stock-app.service" not in cmd


def test_observer_empty_and_no_stop_propagation(plan):
    cmd = stop.observer_command(plan)
    assert cmd[-4:] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/usr/bin/true"]
    assert "--property=Wants=" + plan["unit"] in cmd
    assert not any(any(k in part for k in ("PartOf=", "BindsTo=", "PropagatesStopTo=")) for part in cmd)


def test_show_requires_all_empty_properties(plan):
    state = policy_state(plan, kernel_value(plan))
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        if argv[0] == "/usr/bin/busctl":
            return stop.encoded(
                {"type": "(uo)", "data": [0, "/"]} if argv[-1] == "Job" else stop.EMPTY_EXEC_ARRAY
            )
        return b"".join(
            (k + "=" + v + "\n").encode() for k, v in state.items() if k in stop.PROPERTIES.split(",")
        )

    assert stop.show(run, plan["unit"]) == state
    assert calls[0][2] == "--all" and state["ExecStop"] == "" and state["ExecStopPost"] == ""
    for raw in (
        b"LoadState=not-found\n",
        b"\xff",
        b"".join((k + "=" + v + "\n").encode() for k, v in state.items() if k in stop.PROPERTIES.split(","))
        + b"Job=0\n",
    ):
        with pytest.raises(stop.StopError):
            stop.show(lambda *a, **kw: raw, plan["unit"])


def test_command_surface_never_retries_uncertain_stop(plan):
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        raise TimeoutError("not retained")

    validation = stop.Validation(
        plan["request"],
        boundary=lambda _: None,
        run=run,
        record=stop.OPERATIONS / plan["request"]["release_id"],
    )
    validation.plan = plan
    validation.last_event = "stop_committed"
    argv = ["/usr/bin/systemctl", "stop", "--no-block", plan["unit"]]
    with pytest.raises(TimeoutError):
        validation.run(argv, timeout=10)
    with pytest.raises(stop.StopError, match="stop_already_consumed"):
        validation.run(argv, timeout=10)
    for forbidden in (
        ["/usr/bin/systemctl", "stop", "stock-app.service"],
        ["/usr/bin/systemctl", "kill", plan["unit"]],
        ["/usr/bin/systemctl", "restart", plan["unit"]],
    ):
        with pytest.raises(stop.StopError):
            validation.run(forbidden, timeout=10)
    assert calls == [argv]


@pytest.mark.parametrize("phase", ["before", "during", "after"])
def test_census_truth_from_executor_and_actual_status(plan, phase):
    value = census(plan, phase)
    assert stop.validate_census(value, plan, 90001, phase) == value
    for field, replacement in [
        ("uvloop_running", False),
        ("executor_thread_alive", phase == "after"),
        ("analysis_status", "starting"),
        ("future_id", True),
        ("persistence_saved", phase != "after"),
    ]:
        broken = dict(value)
        broken[field] = replacement
        with pytest.raises(stop.StopError):
            stop.validate_census(broken, plan, 90001, phase)


def test_cancelled_wrapper_does_not_hide_live_executor(plan):
    value = census(plan, "during")
    value.update(future_done=True, future_cancelled=True)
    stop.validate_census(value, plan, 90001, "during")
    value["executor_thread_alive"] = False
    with pytest.raises(stop.StopError):
        stop.validate_census(value, plan, 90001, "during")


def test_replay_checks_actual_facts_not_passed_booleans(plan, monkeypatch):
    identity, details = artifacts(plan, monkeypatch)
    raw, receipt = chain(identity, details)
    got = stop.replay(identity, raw, receipt, plan["request"])
    assert got[0] == plan and got[2][2]["analysis_status"] == "done"
    assert receipt["summary"]["production_verified"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "namespace",
        "readonly",
        "capability",
        "other_process_read",
        "pending_thread",
        "fake_db",
        "other_owner",
        "changed_source",
        "restart",
        "extra_stop",
        "early_release",
        "early_exit",
        "observer_propagation",
        "nonempty_subgroup",
        "broad_scope",
        "future_identity",
    ],
)
def test_replay_rejects_rehashed_false_evidence(plan, monkeypatch, mutation):
    identity, details = copy.deepcopy(artifacts(plan, monkeypatch))
    if mutation == "namespace":
        details["kernel_bootstrap"]["identity"]["network_namespace_inode"] = 101
    if mutation == "readonly":
        details["kernel_bootstrap"]["identity"]["readonly_paths"]["/home"]["options"] = ["rw"]
    if mutation == "capability":
        details["kernel_bootstrap"]["identity"]["capabilities"]["CapEff"] = 1
    if mutation == "other_process_read":
        details["canaries_verified"]["checks"]["other_process_read_errno"] = 0
    if mutation == "pending_thread":
        details["census_during"]["executor_thread_alive"] = False
    if mutation == "fake_db":
        details["result_preserved"]["result"]["kind"] = "public_synthetic_result"
    if mutation == "other_owner":
        details["result_preserved"]["result"]["foreign_owner_denied"] = False
    if mutation == "changed_source":
        details["result_preserved"]["result"]["source_modules"]["analysis_engine"] = "0" * 64
    if mutation == "restart":
        details["policy_verified"]["Restart"] = "always"
    if mutation == "extra_stop":
        details["stop_committed"]["attempt"] = 2
    if mutation == "early_release":
        details["work_release_intent"]["at_us"] = 250
    if mutation == "early_exit":
        details["exit_observed"]["exit_at_us"] = 450
    if mutation == "observer_propagation":
        details["observer_ready"]["state"]["PartOf"] = "stock-app.service"
    if mutation == "nonempty_subgroup":
        details["exit_observed"]["cgroup"] = {
            "cgroup_v2": True,
            "root_exists": True,
            "groups": [
                {"relative": ".", "members": [], "populated": 0},
                {"relative": "child", "members": [123], "populated": 1},
            ],
        }
    if mutation == "broad_scope":
        details["completed"]["proof_scope"] = "all_pending_tasks"
    if mutation == "future_identity":
        details["census_after"]["future_id"] = 112
    raw, receipt = chain(identity, details)
    with pytest.raises(stop.StopError):
        stop.replay(identity, raw, receipt, plan["request"])


def test_chain_tail_and_sequence_are_required(plan, monkeypatch):
    identity, details = artifacts(plan, monkeypatch)
    raw, receipt = chain(identity, details)
    for changed in (
        raw[:-1],
        raw.replace(b'"sequence":1}', b'"sequence":true}', 1),
        raw.replace(b'"stop_committed"', b'"stop_intent"', 1),
    ):
        with pytest.raises(stop.StopError):
            stop.replay(identity, changed, receipt, plan["request"])
    receipt["summary"]["passed"] = True
    with pytest.raises(stop.StopError):
        stop.replay(identity, b"{}\n", receipt, plan["request"])


def test_worker_never_adds_app_shield_or_replaces_persistence_functions():
    source = stop.WORKER_SOURCE
    assert "asyncio.shield" not in source and "persist_result" not in source
    assert "await engine.start_analysis(" in source
    assert "engine._persist_analysis=" not in source and "engine._save_cached_analysis=" not in source
    assert "sys.setprofile(observe)" in source and "finally:sys.setprofile(old)" in source
    assert "engine._store.pop(aid)" in source and "engine.get_analysis_record(aid,state['owner'])" in source
    assert "PRAGMA wal_checkpoint(TRUNCATE)" in source and "os.fsync(fd)" in source
    assert stop.WORKER_SHA256 == hashlib.sha256(source.encode()).hexdigest()


@pytest.mark.parametrize("cancel_wrapper", [False, True])
def test_actual_analysis_executor_and_real_database_survive_wrapper_cancel(
    tmp_path, monkeypatch, cancel_wrapper
):
    """Actual app source and DB, external Graph only fixture; not Linux/systemd evidence."""
    from server import analysis_engine as engine
    from server import database
    from server.services import auxiliary_model
    from server.work_lifecycle import Manager, bind_context

    ns = worker_functions()
    release = threading.Event()
    monkeypatch.setitem(ns, "read_gate", lambda root, name, binding: release.is_set())
    before_fn = engine._persist_analysis
    cache_fn = engine._save_cached_analysis
    state = {
        "root": tmp_path,
        "binding": "a" * 64,
        "nonce": "b" * 32,
        "future": None,
        "analysis_id": None,
        "thread": None,
        "engine": engine,
        "executor_finished": False,
        "persistence_saved": False,
        "server_unwound": False,
        "uvloop_running": False,
        "before_sha": None,
        "during_sha": None,
        "owner": "newstop_synthetic",
    }
    Graph = ns["graph_fixture"](state)
    for name in (
        "tradingagents",
        "tradingagents.graph",
        "tradingagents.graph.trading_graph",
        "tradingagents.default_config",
    ):
        module = types.ModuleType(name)
        module.__path__ = []
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["tradingagents.graph.trading_graph"].TradingAgentsGraph = Graph
    sys.modules["tradingagents.default_config"].DEFAULT_CONFIG = {}
    monkeypatch.setattr(auxiliary_model, "run_text", lambda *a, **kw: "")
    monkeypatch.setattr(engine, "ASTOCK_CACHE_TTL_SECONDS", 3600)
    monkeypatch.setattr(engine, "ASTOCK_PROJECT", str(tmp_path / "external-stub"))
    errors = []
    manager = Manager(max_workers=1)
    original_start, original_submit = manager.start, manager._pool.submit

    def observed_submit(*args, **kwargs):
        # Preserve the actual ThreadPoolExecutor future; no fake thread/result.
        future = original_submit(*args, **kwargs)
        state["executor_future"] = future
        return future

    def observed_start(kind, function, *, parent=None, persist=None):
        handle = original_start(kind, function, parent=parent, persist=persist)
        if kind == "analysis":
            state["handle"] = handle
            # This supervised HTTP waiter may cancel; the real executor and the
            # business owner remain held by the application lifecycle manager.
            state["future"] = asyncio.create_task(handle.wait())
        return handle

    monkeypatch.setattr(manager._pool, "submit", observed_submit)
    monkeypatch.setattr(manager, "start", observed_start)

    async def execute():
        loop = asyncio.get_running_loop()
        state["loop"] = loop
        loop.set_exception_handler(lambda loop, context: errors.append(context.get("exception")))

        with bind_context(manager):
            state["analysis_id"] = await engine.start_analysis(
                "600519", username=state["owner"], date="2026-10-22", mode="fast", force_refresh=True
            )
        try:
            for _ in range(2000):
                if state["before_sha"]:
                    break
                await asyncio.sleep(0.001)
            assert state["before_sha"] and state["thread"].is_alive()
            actual_future = state["executor_future"]
            assert not actual_future.done() and not actual_future.cancelled()
            assert manager.observe()["pending_cf"] == 1
            with database.get_db() as db:
                assert (
                    db.execute(
                        "SELECT status FROM analysis_runs WHERE id=?", [state["analysis_id"]]
                    ).fetchone()[0]
                    == "running"
                )
            if cancel_wrapper:
                assert state["future"].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await state["future"]
                assert not actual_future.done() and not actual_future.cancelled()
                assert manager.observe()["pending_cf"] == 1
            state["server_unwound"] = True
            for _ in range(2000):
                if state["during_sha"]:
                    break
                await asyncio.sleep(0.001)
            assert state["during_sha"] and state["thread"].is_alive()
            assert state["executor_future"] is actual_future and not actual_future.done()
            manager.begin_drain(manager.ticket())
            assert not await manager.wait_drained(timeout=0.01)
        finally:
            release.set()
        assert await manager.wait_drained(timeout=3)
        await state["handle"].wait()
        if not cancel_wrapper:
            await state["future"]
        assert actual_future.done() and not actual_future.cancelled()
        assert manager.observe()["pending_cf"] == manager.observe()["persist_pending"] == 0
        # Unlike the old default executor, this real application pool is owned
        # explicitly. Only draining+empty identity-free test close may join it.
        manager.close_development()
        await loop.shutdown_default_executor()

    try:
        asyncio.run(execute())
        assert engine._persist_analysis is before_fn and engine._save_cached_analysis is cache_fn
        aid = state["analysis_id"]
        engine._store.pop(aid)
        record = engine.get_analysis_record(aid, state["owner"])
        assert (
            record["status"] == "done"
            and record["report"]["final_decision"] == "PRISM_SYNTHETIC_STOP_RESULT_197"
        )
        assert engine.get_analysis_record(aid, "foreign_owner") is None
        assert (
            engine._load_cached_analysis(state["owner"], "600519", "2026-10-22", "fast")["report"][
                "final_decision"
            ]
            == "PRISM_SYNTHETIC_STOP_RESULT_197"
        )
        assert state["executor_finished"] and not state["thread"].is_alive()
        if cancel_wrapper:
            assert state["future"].cancelled()
            assert state["executor_future"].done() and not state["executor_future"].cancelled()
            assert not errors  # Expected HTTP cancellation is observed, never an unhandled owner error.
        else:
            assert state["future"].done() and not state["future"].cancelled() and not errors
    finally:
        release.set()
        manager._pool.shutdown(wait=True, cancel_futures=False)  # Failure-path test cleanup only.
        engine._store.pop(state.get("analysis_id"), None)
        while str(tmp_path / "external-stub") in sys.path:
            sys.path.remove(str(tmp_path / "external-stub"))


def test_read_accepted_cannot_run_on_mac_without_actual_root_kernel(request_value, monkeypatch):
    monkeypatch.setattr(stop.sys, "platform", "darwin")
    with pytest.raises(stop.StopError, match="root_linux_required"):
        stop.read_accepted(
            request_value,
            role="candidate",
            candidate=request_value["candidate"],
            archive_sha256=request_value["archive_sha256"],
            environment_sha256=request_value["environment_sha256"],
            record=stop.OPERATIONS / request_value["release_id"],
            run=lambda *a, **k: b"",
        )


def test_private_json_rejects_duplicate_keys_and_nonfinite():
    for raw in (b'{"schema":1,"schema":true}', b'{"number":NaN}'):
        with pytest.raises(ValueError):
            stop.parse_json(raw)


def test_read_fd_rejects_symlink_hardlink_and_wrong_mode(tmp_path):
    file = tmp_path / "data"
    stop.write(file, b"public", 0o600)
    assert stop.read(file, uid=os.geteuid(), mode=0o600) == b"public"
    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(stop.StopError):
        stop.read(link, uid=os.geteuid())
    hard = tmp_path / "hard"
    os.link(file, hard)
    with pytest.raises(stop.StopError):
        stop.read(file, uid=os.geteuid())
    hard.unlink()
    file.chmod(0o644)
    with pytest.raises(stop.StopError):
        stop.read(file, uid=os.geteuid(), mode=0o600)


def test_recursive_cgroup_detects_nested_residue(tmp_path, monkeypatch):
    monkeypatch.setattr(stop, "read", lambda path, **kw: path.read_bytes())
    root = tmp_path / "main"
    nested = root / "child"
    nested.mkdir(parents=True)
    (root / "cgroup.procs").write_bytes(b"")
    (nested / "cgroup.procs").write_bytes(b"123\n")
    assert stop.empty_group(root) is False
    (nested / "cgroup.procs").write_bytes(b"")
    assert stop.empty_group(root)
    (root / "alias").symlink_to(nested, target_is_directory=True)
    with pytest.raises(stop.StopError):
        stop.empty_group(root)


def test_source_identity_binds_actual_candidate_modules_and_environment(tmp_path, request_value, monkeypatch):
    root = tmp_path / request_value["candidate"]
    (root / ".venv/lib/python3.12/site-packages/uvloop").mkdir(parents=True)
    (root / ".venv/lib/python3.12/site-packages/uvloop/loop.fixture.so").write_bytes(
        b"public synthetic extension"
    )
    (root / ".venv/lib/python3.12/site-packages/uvicorn").mkdir()
    (root / ".venv/lib/python3.12/site-packages/uvicorn/__init__.py").write_bytes(b"public synthetic uvicorn")
    (root / "server").mkdir()
    for name in ("analysis_engine", "database", "config"):
        (root / "server" / (name + ".py")).write_bytes(name.encode())
    (root / ".deploy-revision").write_text(request_value["candidate"] + "\n")
    monkeypatch.setattr(stop, "RELEASES", tmp_path)
    monkeypatch.setattr(
        stop,
        "APPLICATION_SOURCES",
        {
            "candidate": {
                name: stop.digest(name.encode()) for name in ("analysis_engine", "database", "config")
            }
        },
    )
    monkeypatch.setattr(stop, "locked", lambda path: None)
    original = stop.read
    monkeypatch.setattr(
        stop, "read", lambda path, **kwargs: original(path, **{**kwargs, "uid": os.geteuid()})
    )
    monkeypatch.setattr(
        stop,
        "file_hash",
        lambda path: (
            stop.SYSTEM_PYTHON_SHA256
            if path == Path("/usr/bin/python3.12")
            else stop.digest(path.read_bytes())
        ),
    )
    monkeypatch.setattr(
        stop,
        "tree_hash",
        lambda path, **kwargs: request_value["environment_sha256"] if path.name == ".venv" else "6" * 64,
    )
    value = stop.source_identity(request_value)
    assert value["analysis_engine_sha256"] == stop.digest(b"analysis_engine")
    assert value["database_sha256"] == stop.digest(b"database") and value["config_sha256"] == stop.digest(
        b"config"
    )
    wrong = dict(request_value)
    wrong["environment_sha256"] = "f" * 64
    # The current environment measurement stays fixed even if input changes.
    monkeypatch.setattr(stop, "tree_hash", lambda path, **kwargs: request_value["environment_sha256"])
    with pytest.raises(stop.StopError, match="environment_changed"):
        stop.source_identity(wrong)


def test_private_reader_replays_chain_and_checks_all_retained_witnesses(tmp_path, plan, monkeypatch):
    """Contract simulation with mocked kernel/filesystem ownership, never actual Linux proof."""
    identity, details = artifacts(plan, monkeypatch)
    raw, receipt = chain(identity, details)
    ops = tmp_path / "operations"
    record = ops / plan["request"]["release_id"]
    directory = record / "new-runtime-stop" / plan["request"]["attempt"]
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    for name in ("identity.json", "events.jsonl", "receipt.json"):
        (directory / name).write_bytes(b"synthetic private contract")
    runtime = tmp_path / "retained-private-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setattr(stop, "OPERATIONS", ops)
    monkeypatch.setattr(stop.sys, "platform", "linux")
    monkeypatch.setattr(stop.os, "geteuid", lambda: 0)
    monkeypatch.setattr(stop, "locked", lambda path: None)
    monkeypatch.setattr(stop, "source_identity", lambda request: identity["sources"])
    monkeypatch.setattr(
        stop, "group_evidence", lambda plan: {"cgroup_v2": True, "root_exists": False, "groups": []}
    )
    monkeypatch.setattr(stop, "process_stat", lambda pid: 78)
    monkeypatch.setattr(stop, "host_runtime", lambda plan: runtime)
    own = details["kernel_bootstrap"]["identity"]
    info = runtime.stat()
    own["runtime_device"] = info.st_dev
    own["runtime_inode"] = info.st_ino
    # Rebuild all dependent references and root chain after changing its simulated inode.
    details["stop_waiting"]["identity"] = own
    raw, receipt = chain(identity, details)
    witness = {
        "identity.json": identity,
        "receipt.json": receipt,
        **{
            "census-" + phase + ".json": details["census_" + phase] for phase in ("before", "during", "after")
        },
        "finished.json": details["result_preserved"]["finished"],
        "result.json": details["result_preserved"]["result"],
        "bootstrap-ready.json": details["kernel_bootstrap"]["bootstrap"],
        "canaries.json": details["canaries_verified"],
        **{
            name: {
                "schema": 1,
                "binding_sha256": plan["binding_sha256"],
                "nonce": plan["nonce"],
                "phase": phase,
            }
            for name, phase in (
                ("allow-canaries.json", "allow_canaries"),
                ("allow-import.json", "allow_import"),
                ("release-work.json", "release_work"),
            )
        },
    }

    def read_json(path, **kwargs):
        assert kwargs.get("mode") in {0o444, 0o600}
        return copy.deepcopy(witness[path.name])

    monkeypatch.setattr(stop, "read_json", read_json)

    def read(path, **kwargs):
        if path.name == "events.jsonl":
            return raw
        if path.name == "boot_id":
            return identity["boot_id"].encode()
        if path.name == "worker.py":
            return stop.WORKER_SOURCE.encode()
        if path.name == "chat.db":
            return b"public synthetic database"
        raise AssertionError("unexpected read")

    monkeypatch.setattr(stop, "read", read)
    details["result_preserved"]["result"]["database_sha256"] = stop.digest(b"public synthetic database")
    details["result_preserved"]["finished"]["result_sha256"] = stop.digest(
        stop.encoded(details["result_preserved"]["result"])
    )
    raw, receipt = chain(identity, details)
    witness["receipt.json"] = receipt
    witness["result.json"] = details["result_preserved"]["result"]
    witness["finished.json"] = details["result_preserved"]["finished"]
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        if argv == stop.INVENTORY:
            return (plan["unit"] + " loaded inactive dead fixture\n").encode()
        names = stop.OBSERVER_PROPERTIES if argv[-1] == plan["observer"] else stop.PROPERTIES
        state = dict.fromkeys(names.split(","), "")
        state["LoadState"] = "not-found"
        return b"".join((k + "=" + v + "\n").encode() for k, v in state.items())

    result = stop.read_accepted(
        plan["request"],
        role="candidate",
        candidate=plan["request"]["candidate"],
        archive_sha256=plan["request"]["archive_sha256"],
        environment_sha256=plan["request"]["environment_sha256"],
        record=record,
        run=run,
    )
    assert set(result) == {
        "sha256",
        "candidate",
        "archive_sha256",
        "environment_sha256",
        "stop_properties_sha256",
        "actual_linux",
        "operation",
        "uvloop_version",
        "clean_exit_zero",
        "recursive_cgroup_empty",
        "pending_results_preserved",
        "task_census_sha256",
    }
    assert len(calls) == 3 and all(argv[1] in {"show", "list-units"} for argv in calls)
    witness["census-during.json"]["executor_thread_alive"] = False
    with pytest.raises(stop.StopError):
        stop.read_accepted(
            plan["request"],
            role="candidate",
            candidate=plan["request"]["candidate"],
            archive_sha256=plan["request"]["archive_sha256"],
            environment_sha256=plan["request"]["environment_sha256"],
            record=record,
            run=run,
        )


def test_worker_imports_app_only_after_actual_root_gate():
    source = stop.WORKER_SOURCE
    assert source.index("read_gate(root,'allow-canaries.json'") < source.index("'other_process_read_errno'")
    assert source.index("read_gate(root,'allow-import.json'") < source.index(
        "from server import analysis_engine"
    )
    assert "site.main" not in source and "sys.path.insert(0,str(sites))" in source
    assert source.index("os.environ.clear()") < source.index("from server import analysis_engine")


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"type":"a(sasbttttuii)","data":[[]]}',
        b'{"type":"as","data":[]}',
        b'{"type":"a(sasbttttuii)","data":[["public-command"]]}',
        b'{"type":"a(sasbttttuii)","data":[],"extra":true}',
        b'{"type":"a(sasbttttuii)","data":[1],"data":[]}',
        b"not json",
    ],
)
def test_new_runtime_requires_actual_typed_empty_exec_arrays(plan, raw):
    with pytest.raises(stop.StopError, match="stop_policy_rejected"):
        stop.empty_exec_arrays(lambda *args, **kwargs: raw, plan["unit"])


def test_new_runtime_missing_unit_never_reads_service_exec_arrays(plan):
    state = dict.fromkeys(stop.PROPERTIES.split(","), "")
    state["LoadState"] = "not-found"
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == ["/usr/bin/systemctl", "show", "--all", "--property=" + stop.PROPERTIES, plan["unit"]]
        return "".join(k + "=" + v + "\n" for k, v in state.items()).encode()

    assert stop.show(run, plan["unit"]) == state and len(calls) == 1


def test_new_runtime_replay_needs_durable_typed_proof(plan, monkeypatch):
    identity, details = artifacts(plan, monkeypatch)
    details["exec_arrays_verified"]["ExecStop"]["data"] = [[]]
    raw, receipt = chain(identity, details)
    with pytest.raises(stop.StopError):
        stop.replay(identity, raw, receipt, plan["request"])


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"type": "uo", "data": [0, "/"]},
        {"type": "(uo)", "data": []},
        {"type": "(uo)", "data": [False, "/"]},
        {"type": "(uo)", "data": [-1, "/"]},
        {"type": "(uo)", "data": [2**32, "/"]},
        {"type": "(uo)", "data": ["0", "/"]},
        {"type": "(uo)", "data": [0, "/org/freedesktop/systemd1/job/0"]},
        {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/8"]},
        {"type": "(uo)", "data": [7, "/"]},
        {"type": "(uo)", "data": [0, "/"], "extra": True},
    ],
)
def test_typed_job_rejects_bad_uint32_paths_and_shapes(value):
    with pytest.raises(stop.StopError, match="job_property_rejected"):
        stop.validate_job(value)


@pytest.mark.parametrize("printed", ["", "0"])
def test_empty_printed_job_is_zero_only_after_actual_typed_read(plan, printed):
    calls = []
    unit = plan["unit"]

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == stop.job_command(unit) and timeout == 10
        return b'{"type":"(uo)","data":[0,"/"]}'

    assert stop.typed_job(run, unit, printed) == {"type": "(uo)", "data": [0, "/"]}
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw", [b"", b"not json", b'{"type":"(uo)","data":[7,"/org/freedesktop/systemd1/job/7"],"data":[0,"/"]}']
)
def test_missing_or_duplicate_typed_job_is_not_empty_job(plan, raw):
    with pytest.raises(stop.StopError, match="job_property_rejected"):
        stop.typed_job(lambda *args, **kwargs: raw, plan["unit"], "")


def test_nonempty_show_job_must_match_actual_typed_value(plan):
    with pytest.raises(stop.StopError, match="job_property_rejected"):
        stop.typed_job(lambda *args, **kwargs: b'{"type":"(uo)","data":[0,"/"]}', plan["unit"], "7")
    actual = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    assert stop.validate_job(actual, "7") == actual
    assert stop.validate_job(actual, "") == actual


@pytest.mark.parametrize("proof", ["before", "waiting", "after", "observer", "observer-release"])
def test_rehashed_typed_job_proofs_cannot_hide_pending_manager_work(plan, monkeypatch, proof):
    identity, details = artifacts(plan, monkeypatch)
    bad = {"type": "(uo)", "data": [7, "/org/freedesktop/systemd1/job/7"]}
    if proof == "before":
        details["typed_job_verified"] = bad
    elif proof == "waiting":
        details["stop_waiting"]["typed_job"] = {"type": "(uo)", "data": [0, "/"]}
    elif proof == "after":
        details["exit_observed"]["job"] = bad
    elif proof == "observer":
        details["observer_ready"]["job"] = bad
    else:
        details["observer_released"]["job"] = bad
    raw, receipt = chain(identity, details)
    with pytest.raises(stop.StopError):
        stop.replay(identity, raw, receipt, plan["request"])


def test_pending_stop_job_is_polled_until_manager_finishes_not_prematurely_failed(plan, monkeypatch):
    operation = stop.Validation(
        plan["request"],
        boundary=lambda *args: None,
        run=lambda *args, **kwargs: None,
        record=stop.OPERATIONS / plan["request"]["release_id"],
    )
    state = policy_state(plan, kernel_value(plan), after=True)
    state["Job"] = "7"
    monkeypatch.setattr(stop, "show", lambda *args, **kwargs: state)
    assert operation.own_state(allow_pending=True) == state
    with pytest.raises(stop.StopError, match="stop_rejected"):
        operation.own_state()
    state["Job"] = "0"
    assert operation.own_state() == state


@pytest.mark.parametrize(
    "wants", ["MAIN tmp.mount extra.service", "MAIN tmp.mount tmp.mount", "tmp.mount", "MAIN other.mount"]
)
def test_observer_extra_dependencies_are_finite_not_generic_mount_allowlist(plan, monkeypatch, wants):
    main = plan["unit"]
    state = {"Wants": wants.replace("MAIN", main), "PrivateTmp": "yes"}
    called = []
    with pytest.raises(stop.StopError, match="observer_dependency_rejected"):
        stop.observer_dependency(lambda *a, **k: called.append(a), state, main)
    assert not called


@pytest.mark.parametrize(
    "change", [None, "private-tmp", "mounted", "missing-root", "tmp-mounted", "duplicate-property"]
)
def test_only_actual_private_tmp_weak_want_and_absent_host_mount_are_accepted(plan, monkeypatch, change):
    main = plan["unit"]
    state = {"Wants": main + " tmp.mount", "PrivateTmp": "no" if change == "private-tmp" else "yes"}
    mount = dict(stop.ABSENT_TMP_MOUNT)
    if change == "mounted":
        mount.update(LoadState="loaded", ActiveState="active", SubState="mounted", Where="/tmp")
    raw = "".join(k + "=" + v + "\n" for k, v in mount.items()).encode()
    if change == "duplicate-property":
        raw += b"Where=\n"
    kernel = b"36 25 8:1 / / rw - ext4 /dev/public rw\n"
    if change == "missing-root":
        kernel = b""
    if change == "tmp-mounted":
        kernel += b"37 36 0:2 / /tmp rw - tmpfs tmpfs rw\n"
    monkeypatch.setattr(stop, "read", lambda *args, **kwargs: kernel)
    calls = []

    def run(argv, *, timeout):
        calls.append(argv)
        assert argv == stop.TMP_MOUNT_COMMAND and timeout == 10
        return raw

    if change is None:
        proof = stop.observer_dependency(run, state, main)
        assert stop.validate_observer_dependency(state, main, proof) == proof
        assert proof["mount_unit"] == stop.ABSENT_TMP_MOUNT and proof["kernel_tmp_mount_count"] == 0
    else:
        with pytest.raises(stop.StopError, match="observer_dependency_rejected"):
            stop.observer_dependency(run, state, main)


def test_single_expected_want_needs_no_mount_exception_or_host_read(plan, monkeypatch):
    main = plan["unit"]
    assert (
        stop.observer_dependency(
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()), {"Wants": main}, main
        )
        is None
    )


@pytest.mark.parametrize("change", [None, "mount-state", "kernel-mount", "private-tmp", "extra-want"])
def test_private_reader_replays_exact_absent_tmp_mount_dependency_proof(plan, monkeypatch, change):
    identity, details = artifacts(plan, monkeypatch)
    observer = details["observer_ready"]
    observer["state"].update(Wants=plan["unit"] + " tmp.mount", PrivateTmp="yes")
    observer["private_tmp_dependency"] = {
        "schema": 1,
        "private_tmp": "yes",
        "extra_want": "tmp.mount",
        "mount_unit": dict(stop.ABSENT_TMP_MOUNT),
        "kernel_tmp_mount_count": 0,
        "kernel_root_mount_observed": True,
    }
    if change == "mount-state":
        observer["private_tmp_dependency"]["mount_unit"]["Where"] = "/tmp"
    if change == "kernel-mount":
        observer["private_tmp_dependency"]["kernel_tmp_mount_count"] = 1
    if change == "private-tmp":
        observer["state"]["PrivateTmp"] = "no"
    if change == "extra-want":
        observer["state"]["Wants"] += " other.service"
    details["observer_release_intent"] = copy.deepcopy(observer)
    raw, receipt = chain(identity, details)
    if change is None:
        assert stop.replay(identity, raw, receipt, plan["request"])
    else:
        with pytest.raises(stop.StopError):
            stop.replay(identity, raw, receipt, plan["request"])
