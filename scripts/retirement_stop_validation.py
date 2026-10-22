"""Isolated Linux systemctl-stop experiment, never a production drain action.

The fixed controller must seal/pin this helper and its two imported helpers,
serialize writers, supply durable record/boundary callbacks, and bind the actual
system Python digest. Only two fixed synthetic units and their empty observers
can receive stop. No kill, restart, or reset-failed path exists here.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import suppress
from pathlib import Path

from scripts import retirement_probe as probe
from scripts import retirement_validation as base

PROBE_SOURCE_SHA256 = "e6e9181b0dcd33dda28bf4125781579555039f71af932e10641c2da3bedb4624"
VALIDATION_SOURCE_SHA256 = "8331a6713244e8be8983fd0e6639492435fcf3a11552890c45997d440d41586e"
PYTHON_VERSION = "3.12.3"
CASES = ("int-executor", "int-post")
POLICY = {
    "KillSignal": "2",
    "KillMode": "process",
    "SendSIGKILL": "no",
    "SendSIGHUP": "no",
    "TimeoutStopUSec": "infinity",
    "Restart": "no",
    "ExecStop": "",
    "ExecStopPost": "",
    "WatchdogUSec": "0",
    "RuntimeMaxUSec": "infinity",
    "NRestarts": "0",
    "RemainAfterExit": "yes",
}
PROPERTIES = base.PROPERTIES + "," + ",".join((*POLICY, "Job"))
OBSERVER_PROPERTIES = base.PROPERTIES + ",Wants,PartOf,BindsTo,PropagatesStopTo,DynamicUser,RemainAfterExit"
VERSION_COMMAND = [
    "/usr/bin/python3.12",
    "-I",
    "-S",
    "-B",
    "-c",
    "import sys; print('.'.join(map(str,sys.version_info[:3])))",
]
OBSERVER_INVENTORY = [
    "/usr/bin/systemctl",
    "list-units",
    "--all",
    "--no-pager",
    "--no-legend",
    "--plain",
    "prism-retirement-stop-ref-*.service",
]


def reject(code):
    raise base.RetirementValidationError("retirement_stop_" + code)


def _python_digest(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        reject("python_digest_rejected")
    return value


def observer_unit(plan, case_id):
    probe._case(plan, case_id)
    if case_id not in CASES:
        reject("case_rejected")
    return "prism-retirement-stop-ref-" + plan["nonce"] + "-" + case_id + ".service"


def descriptor(request, *, expected_python_sha256, nonce=None):
    plan = probe.descriptor(request, nonce=nonce)
    python = _python_digest(expected_python_sha256)
    identity = {
        "bindings": plan["bindings"],
        "python_binary_sha256": python,
        "python_version": PYTHON_VERSION,
        "probe_source_sha256": PROBE_SOURCE_SHA256,
        "validation_source_sha256": VALIDATION_SOURCE_SHA256,
        "policy": POLICY,
    }
    return {
        "schema": 1,
        "kind": "isolated_systemctl_stop_validation",
        "signal_plan": plan,
        "binding_sha256": probe.sha256(probe.encoded(identity)),
        **identity,
        "cases": list(CASES),
        "production_quiescence_proven": False,
        "all_production_memory_persisted": False,
        "actual_linux_acceptance": False,
    }


def observer_command(plan, case_id):
    case = probe._case(plan, case_id)
    observer = observer_unit(plan, case_id)
    properties = dict(case["properties"])
    for key in ("RuntimeDirectory", "RuntimeDirectoryMode", "RuntimeDirectoryPreserve", "ReadWritePaths"):
        properties.pop(key)
    properties.update(
        Type="oneshot",
        WorkingDirectory="/",
        RemainAfterExit="yes",
        Wants=case["unit"],
        StandardOutput="null",
        StandardError="null",
    )
    # Wants retains loaded exit identity; no stop/restart propagation is added.
    return [
        "/usr/bin/systemd-run",
        "--quiet",
        "--unit=" + observer,
        *("--property=" + key + "=" + value for key, value in sorted(properties.items())),
        "--",
        "/usr/bin/env",
        "-i",
        "PATH=/usr/bin:/bin",
        "/usr/bin/true",
    ]


def _rows(raw, allowed):
    if type(raw) is not bytes or len(raw) > 32768:
        reject("systemd_output_rejected")
    try:
        rows = [line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line]
    except UnicodeError:
        reject("systemd_output_rejected")
    result = dict(rows)
    if len(rows) != len(result) or set(result) != set(allowed.split(",")):
        reject("systemd_properties_rejected")
    return result


def _job(value):
    # systemctl formats the Unit.Job tuple as its numeric job ID.
    return base._number(value)


class StopValidation(base.Validation):
    def __init__(
        self,
        request,
        *,
        expected_python_sha256,
        boundary,
        run,
        record,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ):
        super().__init__(request, boundary=boundary, run=run, record=record, monotonic=monotonic, sleep=sleep)
        self.stop_plan = descriptor(
            request, expected_python_sha256=expected_python_sha256, nonce=self.plan["nonce"]
        )
        self.stop_consumed = set()
        self.observers = {}
        self.observer_stop_consumed = set()
        self.case_identities = {}

    def no_existing_observers(self):
        raw = self.run(OBSERVER_INVENTORY, timeout=10)
        if type(raw) is not bytes or len(raw) > 32768:
            reject("observer_inventory_rejected")
        try:
            rows = [line.split() for line in raw.decode("ascii").splitlines() if line.strip()]
        except UnicodeError:
            reject("observer_inventory_rejected")
        for row in rows:
            if len(row) < 4 or not re.fullmatch(
                r"prism-retirement-stop-ref-[0-9a-f]{32}-(?:int-executor|int-post)\.service", row[0]
            ):
                reject("observer_inventory_rejected")
        if rows:
            # Empty active observers retain the old invocation too: neither
            # their cgroup absence nor a new nonce authorizes replacing them.
            reject("previous_observer_unreconciled")
        return True

    def show(self, unit):
        if unit == base.PRODUCTION_UNIT:
            return super().show(unit)
        if unit not in {probe._case(self.plan, case_id)["unit"] for case_id in CASES}:
            reject("host_unit_rejected")
        return _rows(
            self.run(["/usr/bin/systemctl", "show", "--all", "--property=" + PROPERTIES, unit], timeout=10),
            PROPERTIES,
        )

    def observer_show(self, case):
        self.fixed_case(case)
        name = observer_unit(self.plan, case["case_id"])
        return _rows(
            self.run(
                ["/usr/bin/systemctl", "show", "--all", "--property=" + OBSERVER_PROPERTIES, name], timeout=10
            ),
            OBSERVER_PROPERTIES,
        )

    def python_identity(self):
        raw, info = base.read_data(Path("/usr/bin/python3.12"), trusted=True)
        digest = probe.sha256(raw)
        if info.st_mode & 0o022 or digest != self.stop_plan["python_binary_sha256"]:
            reject("python_binary_changed")
        if self.run(VERSION_COMMAND, timeout=10) != (PYTHON_VERSION + "\n").encode():
            reject("python_version_changed")
        return digest

    def policy(self, case, *, waiting=False):
        self.fixed_case(case)
        if case["case_id"] not in CASES:
            reject("case_rejected")
        state = self.show(case["unit"])
        if any(state.get(key) != value for key, value in POLICY.items()):
            reject("actual_policy_rejected")
        if (waiting and _job(state["Job"]) == 0) or (not waiting and _job(state["Job"]) != 0):
            reject("job_state_rejected")
        return state

    def observer_ready(self, case):
        state = self.observer_show(case)
        if state["LoadState"] == "not-found" or state["ActiveState"] == "activating":
            return None
        if (
            state["LoadState"] != "loaded"
            or state["Transient"] != "yes"
            or (state["ActiveState"], state["SubState"]) != ("active", "exited")
            or state["MainPID"] != "0"
            or state["ExecMainCode"] != "1"
            or state["ExecMainStatus"] != "0"
            or state["Result"] != "success"
            or state["Wants"].split() != [case["unit"]]
            or any(state[name] for name in ("PartOf", "BindsTo", "PropagatesStopTo"))
            or state["DynamicUser"] != "yes"
            or state["RemainAfterExit"] != "yes"
            or not re.fullmatch(r"[0-9a-f]{32}", state["InvocationID"])
            or not self.observer_empty(case, state)
        ):
            reject("observer_reference_rejected")
        return state["InvocationID"]

    def observer_empty(self, case, state):
        self.fixed_case(case)
        name = observer_unit(self.plan, case["case_id"])
        expected = "/system.slice/" + name
        if state["ControlGroup"] not in {"", expected}:
            reject("observer_group_rejected")
        path = probe.CGROUP_ROOT / expected.lstrip("/")
        if path.is_symlink():
            reject("observer_group_rejected")
        if not path.exists():
            return True
        for directory, folders, _ in os.walk(path, followlinks=False):
            root = Path(directory)
            if any(p.is_symlink() or not p.is_dir() for p in [root, *(root / p for p in folders)]):
                reject("observer_group_rejected")
            raw, _ = probe._read_regular(root / "cgroup.procs", 256 * 1024)
            if raw.split():
                return False
        return True

    def same_process_waiting(self, case, live, stop_at):
        self.fixed_case(case)
        if not base.server_finished(self.plan, case["case_id"], stop_at):
            return None
        state = self.policy(case, waiting=True)
        if (
            state["LoadState"] != "loaded"
            or state["Transient"] != "yes"
            or state["InvocationID"] != live["invocation"]
            or base._number(state["MainPID"]) != live["pid"]
            or base._number(state["ExecMainPID"]) != live["pid"]
            or state["ControlGroup"] != case["control_group"]
            or (state["ActiveState"], state["SubState"]) != ("deactivating", "stop-sigterm")
        ):
            reject("stop_not_waiting_on_original_main")
        identity = probe.linux_process_identity(self.plan, case["case_id"], live["pid"])
        if (
            identity["start_ticks"] != live["start_ticks"]
            or not all(identity[k] for k in ("uid_isolated", "private_network", "cgroup_bound"))
            or base._uids(live["pid"]) != live["uid"]
        ):
            reject("stop_process_changed")
        markers = probe.read_markers(self.plan, case["case_id"])
        if (
            markers.get("post_saved", False)
            or markers.get("fixture_gate_expired", False)
            or base._deadline(case)
        ):
            reject("executor_was_not_pending")
        if self.observer_ready(case) != self.observers[case["case_id"]]:
            reject("observer_changed")
        return True

    def stopped(self, case, live, stop_at):
        self.fixed_case(case)
        exited = self.exit_state(case, live, stop_at)
        if not exited:
            return None
        state, group = exited
        if _job(state["Job"]) != 0:
            return None
        if (
            state["LoadState"] != "loaded"
            or state["Transient"] != "yes"
            or (state["ActiveState"], state["SubState"]) != ("inactive", "dead")
            or state["ExecMainCode"] != "1"
            or state["ExecMainStatus"] != "0"
            or state["Result"] != "success"
            or not group["observed"]
            or not group["empty"]
            or self.observer_ready(case) != self.observers[case["case_id"]]
        ):
            reject("clean_stop_unverified")
        if self.policy(case) != state:
            reject("exit_policy_changed")
        return state, group

    def release_observer(self, case):
        self.fixed_case(case)
        case_id = case["case_id"]
        invocation = self.observers.get(case_id)
        if invocation is None:
            return True
        original = self.case_identities.get(case_id)
        if original is None:
            return False
        state = self.show(case["unit"])
        if (
            state["InvocationID"] != original["invocation"]
            or base._number(state["ExecMainPID"]) != original["pid"]
            or base._number(state["MainPID"]) != 0
            or not self._empty(case)
        ):
            return False
        if self.observer_ready(case) != invocation or case_id in self.observer_stop_consumed:
            return False
        self.record("synthetic_observer_release_intent", {"case_id": case_id, "invocation_id": invocation})
        self.observer_stop_consumed.add(case_id)
        self.run(["/usr/bin/systemctl", "stop", "--no-block", observer_unit(self.plan, case_id)], timeout=10)

        def inactive():
            state = self.observer_show(case)
            return state["LoadState"] == "not-found" or (
                state["ActiveState"] == "inactive"
                and state["MainPID"] == "0"
                and self.observer_empty(case, state)
            )

        self.poll(inactive, 5)
        return True

    def case(self, case):
        self.fixed_case(case)
        case_id = case["case_id"]
        if case_id not in CASES or case_id in self.stop_consumed:
            reject("stop_already_consumed_or_case_rejected")
        self.check_production()
        self.python_identity()
        self.boundary("retirement_stop_start_" + case_id)
        runtime = Path(case["host_runtime_directory"])
        paths = (runtime, Path(case["runtime_directory"]), Path(case["managed_runtime_alias"]))
        if any(path.exists() or path.is_symlink() for path in paths):
            reject("runtime_already_exists")
        if self.show(case["unit"])["LoadState"] != "not-found":
            reject("unit_already_exists")
        self.record("synthetic_start_intent", {"case_id": case_id, "nonce": self.plan["nonce"]})
        self.start_intents[case_id] = int(self.monotonic() * 1_000_000)
        self.active_case = case
        self.run(base.launch_command(self.plan, case_id), timeout=15)
        live = self.poll(lambda: self._ready(case), 5)
        self.case_identities[case_id] = dict(live)
        self.policy(case)
        self.record(
            "synthetic_stop_actual_isolation", {"case_id": case_id, **live, "actual_kernel_verified": True}
        )
        base._request_fixture(case, live["uid"], live["pid"])

        def ready():
            markers = probe.read_markers(self.plan, case_id)
            return (
                markers
                if all(markers[k] for k in ("fixture_ready", "stream_finished", case["start_phase_marker"]))
                else None
            )

        before = self.poll(ready, 5)
        self.boundary("retirement_stop_observer_" + case_id)
        self.check_production()
        if self.observer_show(case)["LoadState"] != "not-found":
            reject("observer_already_exists")
        self.record("synthetic_observer_start_intent", {"case_id": case_id})
        self.run(observer_command(self.plan, case_id), timeout=15)
        self.observers[case_id] = self.poll(lambda: self.observer_ready(case), 5)
        self.boundary("retirement_stop_prepare_" + case_id)

        def verify_before():
            self.check_production()
            self.policy(case)
            if self.live(case) != live or self.observer_ready(case) != self.observers[case_id]:
                reject("identity_changed_before_stop")
            current = probe.read_markers(self.plan, case_id)
            if (
                any(current.get(k, False) for k in ("post_saved", "fixture_gate_expired"))
                or base._deadline(case)
                or (runtime / "release-work").exists()
                or (runtime / "release-work").is_symlink()
            ):
                reject("work_changed_before_stop")

        verify_before()
        stop_at = int(self.monotonic() * 1_000_000)
        self.record(
            "synthetic_stop_intent",
            {
                "case_id": case_id,
                "invocation_id": live["invocation"],
                "pid": live["pid"],
                "start_ticks": live["start_ticks"],
                "stop_at_monotonic_us": stop_at,
            },
        )
        self.boundary("retirement_stop_dispatch_" + case_id)
        verify_before()
        self.record("synthetic_stop_committed", {"case_id": case_id, "invocation_id": live["invocation"]})
        self.stop_consumed.add(case_id)  # Uncertain delivery is never retried.
        self.run(["/usr/bin/systemctl", "stop", "--no-block", case["unit"]], timeout=10)
        self.poll(lambda: self.same_process_waiting(case, live, stop_at), 5)
        self.record(
            "synthetic_stop_runner_join_observed",
            {"case_id": case_id, "stop_job_waited_with_executor_pending": True},
        )
        base._open_gate(self.plan, case_id)
        exited, group = self.poll(lambda: self.stopped(case, live, stop_at), 10)
        markers = probe.read_markers(self.plan, case_id)
        observation = {
            **{
                k: True
                for k in (
                    "linux",
                    "source_bound",
                    "uid_isolated",
                    "private_network",
                    "cgroup_bound",
                    "same_invocation",
                    "exit_after_signal",
                    "inactive",
                    "result_success",
                    "cgroup_observed",
                    "cgroup_empty",
                    "cleanup_cgroup_empty",
                    "gate_closed_before_signal",
                )
            },
            "exec_main_code": 1,
            "exec_main_status": 0,
            "signals_sent": 1,
            "deadline_expired": base._deadline(case),
            "cgroup_member_count": group["member_count"],
            "markers": markers,
            "markers_before_signal": before,
        }
        result = probe.evaluate_case(self.plan, case_id, observation)
        if not result["passed"]:
            reject("durable_markers_unverified")
        self.python_identity()
        self.check_production()
        self.record(
            "synthetic_stop_case_result",
            {
                **result,
                "systemctl_stop_end_to_end_proven": True,
                "stop_dispatches": 1,
                "exit_monotonic_us": base._number(exited["ExecMainExitTimestampMonotonic"]),
            },
        )
        if not self.release_observer(case):
            reject("observer_release_unverified")
        self.active_case = None
        return result

    def cleanup(self):
        case = self.active_case
        if case is None:
            return True
        try:
            self.fixed_case(case)
            if self._namespace_failed_no_work(case):
                return True
            with suppress(Exception):
                base._open_gate(self.plan, case["case_id"])
            deadline = self.monotonic() + base.CLEANUP_SECONDS
            while self.monotonic() < deadline:
                state = self.show(case["unit"])
                if state["LoadState"] == "not-found":
                    return False  # Lost invocation is not an accepted stop result.
                if state["MainPID"] == "0" and self._empty(case):
                    original = self.case_identities.get(case["case_id"])
                    if (
                        original is None
                        or state["InvocationID"] != original["invocation"]
                        or base._number(state["ExecMainPID"]) != original["pid"]
                    ):
                        return False
                    observer_released = self.release_observer(case)
                    return observer_released and state["ActiveState"] in {"inactive", "failed"}
                self.sleep(0.1)
        except Exception:
            return False
        return False


def run_stop_validation(request, *, expected_python_sha256, boundary, run, record):
    """Fixed controller interface; do not call from candidates or old environments.

    The controller must trusted_source+seal all THREE helpers under a root-owned
    0555/0444 helper tree before importing. record(event, safe_dict) must append
    and fsync before returning. The bounded bytes runner suppresses raw output.
    A new nonce is permitted only after the caller has reconciled prior private
    journals/observer units; failed attempts are never replaced. This module
    adds fixed safe show-property lists, observer systemd-run and version checks
    to the original helper's command surface. No production stop is accepted.
    """
    probe._require_linux()
    validator = StopValidation(
        request, expected_python_sha256=expected_python_sha256, boundary=boundary, run=run, record=record
    )
    results, error = [], None
    try:
        for module, expected in ((probe, PROBE_SOURCE_SHA256), (base, VALIDATION_SOURCE_SHA256)):
            raw, _ = base.read_data(Path(module.__file__), trusted=True)
            if probe.sha256(raw) != expected:
                reject("helper_source_changed")
        validator.production = validator.production_identity()
        validator.python_identity()
        base.no_existing_synthetic_workloads(validator.plan)
        validator.no_existing_observers()
        base.install_bundles(validator.plan, boundary, record)
        for case_id in CASES:
            results.append(validator.case(probe._case(validator.plan, case_id)))
    except Exception as exc:
        error = (
            str(exc)
            if isinstance(exc, (base.RetirementValidationError, probe.RetirementProbeError))
            and re.fullmatch(r"retirement_[a-z0-9_]{1,90}", str(exc))
            else "retirement_stop_boundary_or_command_failed"
        )
    finally:
        cleanup = validator.cleanup()
        if validator.namespace_setup_failed:
            error = "retirement_stop_namespace_setup_failed"
    summary = {
        "schema": 1,
        "bindings": validator.plan["bindings"],
        "stop_binding_sha256": validator.stop_plan["binding_sha256"],
        "retirement_stop_validation_passed": error is None and cleanup and len(results) == len(CASES),
        "systemctl_stop_end_to_end_proven": error is None and cleanup and len(results) == len(CASES),
        "synthetic_cleanup_verified": cleanup,
        "actual_linux_collection": True,
        "cases": results,
        "production_quiescence_proven": False,
        "all_production_memory_persisted": False,
        "task_census_still_required": True,
        "python_version": PYTHON_VERSION,
        "python_binary_sha256": validator.stop_plan["python_binary_sha256"],
        "probe_source_sha256": PROBE_SOURCE_SHA256,
        "validation_source_sha256": VALIDATION_SOURCE_SHA256,
        "bundle_sha256": validator.plan["bundle_sha256"],
        "uvicorn_server_sha256": probe.UVICORN_SHA256,
        "uvloop_binary_sha256": probe.UVLOOP_SHA256,
        "policy_sha256": probe.sha256(probe.encoded(POLICY)),
    }
    if error:
        summary["error_code"] = error
    record("retirement_stop_validation_result", summary)
    return summary
