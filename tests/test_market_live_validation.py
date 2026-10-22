"""Offline orchestration/kernel fixtures; never an actual Linux market receipt."""

import ast
import base64
import hashlib
import os
import types
from pathlib import Path

import pytest
from test_network_gate_wiring import ProofGate, bind_reference

from scripts import market_live_validation as validation


def request():
    raw = (Path(__file__).resolve().parents[1] / "scripts/market_live_probe.py").read_bytes()
    return {
        "schema": 1,
        "release_id": "prism-public-market-test",
        "candidate": "a" * 40,
        "production_pid": 111,
        "probe_source": {"sha256": validation.PROBE_SHA256, "content": base64.b64encode(raw).decode()},
    }


def engine(tmp_path):
    calls, events, boundaries = [], [], []
    value = validation.Validation(
        request(),
        boundary=lambda label: boundaries.append(label),
        run=lambda *args, **kwargs: calls.append((args, kwargs)),
        record=events.append,
    )
    value.work = tmp_path / "market"
    value.work.mkdir()
    for name in ("output", "home", "tmp", "data"):
        (value.work / name).mkdir()
    value.account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    value.pid, value.invocation, value.ticks = 222, "b" * 32, 55
    value.pins = {"qt.gtimg.cn": ["1.1.1.1"], "money.finance.sina.com.cn": ["8.8.4.4"]}
    value.negative = "9.9.9.9"
    value.network_gate = ProofGate()
    value.network_programs = value.network_gate.programs
    bind_reference(value)
    return value, calls, events, boundaries


def binding(value):
    fields = {key: value.request[key] for key in ("candidate", "release_id", "production_pid")}
    return {
        **fields,
        "pid": value.pid,
        "nonce": value.nonce,
        "issued_at": value.issued,
        "probe_sha256": validation.PROBE_SHA256,
        "worker_sha256": validation.WORKER_SHA256,
    }


def proof(value):
    return {
        "schema": 1,
        "binding": binding(value),
        "checked_at": value.issued,
        "checks": dict.fromkeys(validation.BOOT_CHECKS, True),
        "positive_tls": {
            host: {"peer": peers[0], "tls_verified": True} for host, peers in value.pins.items()
        },
        "additional_network_errno": dict.fromkeys(validation.EXTRA_NEGATIVES, 13),
        "negative_errno": dict.fromkeys(
            {
                "production_8088_denied",
                "vibe_8899_denied",
                "metadata_denied",
                "private_ip_denied",
                "other_public_denied",
            },
            13,
        ),
    }


def result(value):
    return {
        "schema": 1,
        "binding": binding(value),
        "checked_at": value.issued,
        "passed": True,
        "market_provider_verified": True,
        "production_verified": False,
        "research_quality_verified": False,
        "live_model_verified": False,
        "native_search_verified": False,
        "native_fetch_verified": False,
        "error_class": None,
        "checks": dict.fromkeys(validation.CHECKS, True),
        "request_counts": {"quote": 1, "kline": 1},
        "https": {
            key: {
                "https_verified": True,
                "redirect_followed": False,
                "response_sha256": "c" * 64,
                "response_bytes": 100,
            }
            for key in ("quote", "kline")
        },
        "cached_result_sha256": {"quote": "d" * 64, "kline": "e" * 64},
        "kline_provenance_missing": [],
        "kline_adapter_is_tencent_actual_endpoint_is_sina": True,
        "failure_injection_disclosed": True,
        "fetched_at_is_not_trade_date": True,
    }


def save(path, value):
    path.write_bytes(validation.encoded(value))
    path.chmod(0o600)


@pytest.mark.parametrize("case", ["extra", "wrong_hash", "wrong_bytes", "credential", "path", "schema_bool"])
def test_only_exact_public_source_and_binding_request_accepted(case):
    value = request()
    if case == "extra":
        value["command"] = "arbitrary"
    elif case == "wrong_hash":
        value["probe_source"]["sha256"] = "f" * 64
    elif case == "wrong_bytes":
        value["probe_source"]["content"] = base64.b64encode(b"never execute").decode()
    elif case == "credential":
        value["api_key"] = "synthetic-only"
    elif case == "path":
        value["release_id"] = "../../production"
    else:
        value["schema"] = True
    with pytest.raises(validation.ValidationError):
        validation.validate_request(value)


def test_frozen_probe_source_and_worker_are_bound():
    raw = validation.validate_request(request())
    assert hashlib.sha256(raw).hexdigest() == validation.PROBE_SHA256
    compile(validation.WORKER_SOURCE, "<trusted-worker>", "exec")
    assert hashlib.sha256(validation.WORKER_SOURCE.encode()).hexdigest() == validation.WORKER_SHA256
    assert validation.WORKER_SOURCE.index("allow['actual_kernel_verified']") < validation.WORKER_SOURCE.index(
        "exec(compile(raw"
    )
    assert "key_provider" not in validation.WORKER_SOURCE
    assert "site.main" not in validation.WORKER_SOURCE
    tree = ast.parse(validation.WORKER_SOURCE)
    assert not any(
        isinstance(node, ast.Import) and any(alias.name == "subprocess" for alias in node.names)
        for node in ast.walk(tree)
    )


def resolve(host, port, **kwargs):
    value = "1.1.1.1" if host == "qt.gtimg.cn" else "8.8.4.4"
    return [(2, 1, 6, "", (value, port))]


@pytest.mark.parametrize(
    "value", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "198.18.1.2", "::1", "::ffff:1.1.1.1"]
)
def test_private_mixed_and_mac_fake_dns_fail_before_launch(value):
    def bad(host, port, **kwargs):
        return resolve(host, port, **kwargs) + [(2, 1, 6, "", (value, port))]

    with pytest.raises(validation.ValidationError, match="dns_rejected"):
        validation.network_policy(bad)


def test_fixed_direct_peers_need_no_resolver_egress(tmp_path):
    pins, negative = validation.network_policy(resolve)
    value, _, _, _ = engine(tmp_path)
    value.pins, value.negative = pins, negative
    command = value.command()
    allow = next(part for part in command if part.startswith("--property=IPAddressAllow="))
    assert set(allow.partition("=")[2].partition("=")[2].split()) == {"1.1.1.1", "8.8.4.4"}
    assert "--property=IPAddressDeny=any" in command
    assert "--property=PrivateNetwork=no" in command
    assert "--property=TemporaryFileSystem=/run:ro" in command
    assert "--property=LimitCORE=0" in command
    assert "--property=RuntimeMaxSec=60" in command
    assert "--property=KillMode=control-group" in command
    assert "-i" in command and "-I" in command and "-S" in command and "-B" in command
    assert not any("Proxy" in item or "127.0.0.53" in item for item in command)


@pytest.mark.parametrize(
    "case", ["bool_schema", "wrong_pid", "old", "missing_canary", "false_canary", "extra"]
)
def test_actual_proof_has_strict_binding_freshness_and_all_controls(tmp_path, case):
    value, _, _, _ = engine(tmp_path)
    data = proof(value)
    if case == "bool_schema":
        data["schema"] = True
    elif case == "wrong_pid":
        data["binding"]["pid"] += 1
    elif case == "old":
        data["checked_at"] -= 100
    elif case == "missing_canary":
        data["checks"].pop("metadata_denied")
    elif case == "false_canary":
        data["checks"]["metadata_denied"] = False
    else:
        data["raw_exception"] = "synthetic-only"
    save(value.work / "output/bootstrap.json", data)
    with pytest.raises(validation.ValidationError, match="proof_rejected"):
        value.proof()


@pytest.mark.parametrize(
    "case",
    ["raw", "credential", "model_claim", "bool_count", "extra_request", "redirect", "metadata_fabrication"],
)
def test_result_cannot_include_raw_data_extra_calls_or_other_acceptance_claims(tmp_path, case):
    value, _, _, _ = engine(tmp_path)
    save(value.work / "output/bootstrap.json", proof(value))
    data = result(value)
    if case == "raw":
        data["quote_price"] = 100
    elif case == "credential":
        data["api_key"] = "synthetic-only"
    elif case == "model_claim":
        data["live_model_verified"] = True
    elif case == "bool_count":
        data["request_counts"]["quote"] = True
    elif case == "extra_request":
        data["request_counts"]["quote"] = 2
    elif case == "redirect":
        data["https"]["quote"]["redirect_followed"] = True
    else:
        data["kline_provenance_missing"] = ["raw unpublished extra"]
    save(value.work / "output/market-result.json", data)
    with pytest.raises(validation.ValidationError, match="result_rejected"):
        value.result()


def test_actual_frozen_missing_auth_and_provenance_stay_incomplete(tmp_path):
    value, _, _, _ = engine(tmp_path)
    save(value.work / "output/bootstrap.json", proof(value))
    data = result(value)
    data["checks"]["unauthenticated_api_denied"] = False
    data["kline_provenance_missing"] = ["source", "retrieved_at", "data_timestamp"]
    data.update(passed=False, error_class="market_acceptance_incomplete")
    save(value.work / "output/market-result.json", data)
    assert value.result() is False
    assert value.report["market_provider_verified"] is True
    assert value.report["worker_result"]["kline_provenance_missing"] == data["kline_provenance_missing"]


@pytest.mark.parametrize(
    "case", ["valid", "uid", "groups", "netns", "exe", "source", "invocation", "allowlist"]
)
def test_parent_requires_actual_process_and_source_facts_not_requested_flags(tmp_path, monkeypatch, case):
    value, _, _, _ = engine(tmp_path)
    value.candidate = tmp_path / "candidate"
    executable = value.candidate / ".venv/bin/python"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"public synthetic interpreter inode")
    processes = tmp_path / "proc"
    process = processes / str(value.pid)
    (process / "ns").mkdir(parents=True)
    (processes / "1/ns").mkdir(parents=True)
    host_net = processes / "1/ns/net"
    host_net.write_bytes(b"public kernel fixture")
    (process / "ns/net").symlink_to(host_net)
    (process / "exe").symlink_to(executable)
    uid, gid = value.account.pw_uid, value.account.pw_gid
    status = f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{gid}\t{gid}\t{gid}\t{gid}\nGroups:\t{gid}\n"
    (process / "status").write_text(status)
    fields = ["0"] * 20
    fields[19] = str(value.ticks)
    (process / "stat").write_text(str(value.pid) + " (python) " + " ".join(fields))
    (process / "cgroup").write_text("0::/system.slice/" + value.unit)
    source = base64.b64decode(request()["probe_source"]["content"])
    (value.work / "probe.py").write_bytes(source)
    (value.work / "worker.py").write_bytes(validation.WORKER_SOURCE.encode())
    for name in ("probe.py", "worker.py"):
        (value.work / name).chmod(0o444)
    state = {
        "MainPID": str(value.pid),
        "ExecMainPID": str(value.pid),
        "InvocationID": value.invocation,
        "User": "prism-integration",
        "Group": "prism-integration",
        "NoNewPrivileges": "yes",
        "ProtectSystem": "strict",
        "ProtectHome": "yes",
        "PrivateNetwork": "no",
        "KillMode": "control-group",
        "LimitCORE": "0",
        "ControlGroup": "/system.slice/" + value.unit,
        "RuntimeMaxUSec": "1min",
        "IPAddressDeny": "0.0.0.0/0 ::/0",
        "IPAddressAllow": "1.1.1.1/32 8.8.4.4/32",
    }
    if case == "uid":
        (process / "status").write_text(status.replace(f"Uid:\t{uid}", "Uid:\t0"))
    elif case == "groups":
        (process / "status").write_text(status.replace(f"Groups:\t{gid}", f"Groups:\t{gid} 999"))
    elif case == "netns":
        (process / "ns/net").unlink()
        (process / "ns/net").write_bytes(b"different actual inode")
    elif case == "exe":
        (process / "exe").unlink()
        (process / "exe").write_bytes(b"wrong inode")
    elif case == "source":
        (value.work / "probe.py").chmod(0o644)
        (value.work / "probe.py").write_bytes(b"changed source")
        (value.work / "probe.py").chmod(0o444)
    elif case == "invocation":
        state["InvocationID"] = "f" * 32
    elif case == "allowlist":
        state["IPAddressAllow"] = "0.0.0.0/0"
    monkeypatch.setattr(validation, "PROC", processes)
    monkeypatch.setattr(validation, "locked", lambda path: None)
    observed = []
    monkeypatch.setattr(validation, "private_run", lambda path: observed.append(path))
    monkeypatch.setattr(value, "show", lambda: state)
    if case == "valid":
        identity = value.identity()
        assert identity["host_network_namespace"] and identity["uids"] == [uid] * 4
        assert observed == [process]
    else:
        with pytest.raises(validation.ValidationError):
            value.identity()


def test_reused_attempt_does_not_write_or_stop_original_attempt(tmp_path, monkeypatch):
    value, calls, events, _ = engine(tmp_path)
    existing = value.work / "validation-result.json"
    existing.write_bytes(b"original failed receipt")
    monkeypatch.setattr(validation.sys, "platform", "linux")
    monkeypatch.setattr(validation.os, "getuid", lambda: 0)
    monkeypatch.setattr(validation, "network_policy", lambda: (value.pins, value.negative))
    monkeypatch.setattr(value, "production", lambda: ("original-invocation", 100))

    def reused():
        raise validation.ValidationError("workspace_reused")

    monkeypatch.setattr(value, "workspace", reused)
    report = value.execute()
    assert report["passed"] is False and report["error_class"] == "workspace_reused"
    assert existing.read_bytes() == b"original failed receipt"
    assert not calls and events[-1] == report


def test_nonlinux_cannot_be_reported_as_actual_linux_collection(tmp_path, monkeypatch):
    value, calls, events, _ = engine(tmp_path)
    monkeypatch.setattr(validation.sys, "platform", "darwin")
    report = value.execute()
    assert report["passed"] is False and report["actual_linux_collection"] is False
    assert report["original_runtime_unchanged"] is False
    assert report["error_class"] == "runtime_rejected"
    assert not calls and events[-1] == report


@pytest.mark.parametrize("bad_proof,pid_reused", [(False, False), (True, False), (False, True)])
@pytest.mark.parametrize("preflight_only", [False, True])
@pytest.mark.parametrize("close_failure", [False, True])
def test_offline_supervisor_requires_real_gate_before_allow_and_only_stops_own_unit(
    tmp_path, monkeypatch, bad_proof, pid_reused, preflight_only, close_failure
):
    value, _, events, boundaries = engine(tmp_path)
    if close_failure:
        monkeypatch.setattr(
            value.unit_references["market"],
            "close",
            lambda: (_ for _ in ()).throw(RuntimeError("public synthetic close failure")),
        )
    value.preflight_only = preflight_only
    if preflight_only:
        monkeypatch.setattr(
            value, "result", lambda: pytest.fail("network preflight must not enter market HTTP result")
        )
    calls, observations = [], []
    monkeypatch.setattr(validation.sys, "platform", "linux")
    monkeypatch.setattr(validation.os, "getuid", lambda: 0)
    monkeypatch.setattr(validation, "network_policy", lambda: (value.pins, value.negative))
    monkeypatch.setattr(value, "production", lambda: ("original-invocation", 100))

    def workspace():
        value.workspace_created = True

    monkeypatch.setattr(value, "workspace", workspace)
    monkeypatch.setattr(validation, "startticks", lambda path: 55)
    # The synthetic PID must be independent of the CI runner's real /proc.
    processes = tmp_path / "proc"
    processes.mkdir()
    if pid_reused:
        (processes / "222").mkdir()
    monkeypatch.setattr(validation, "PROC", processes)

    def identity():
        observations.append("kernel")
        return {"verified_fixture": True}

    monkeypatch.setattr(value, "identity", identity)
    monkeypatch.setattr(value, "install_network_gate", lambda: observations.append("attach"))
    monkeypatch.setattr(value, "empty", lambda: True)
    monkeypatch.setattr(value, "idle", lambda: None)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "/usr/bin/systemd-run":
            save(value.work / "output/hello.json", {"offline_fixture": True})
            data = proof(value)
            if bad_proof:
                data["checks"]["metadata_denied"] = False
            save(value.work / "output/bootstrap.json", data)
        return b""

    value.run = run
    actual_write = validation.write

    def checked_write(path, raw, mode=0o600):
        if path.name == "result-ack.json":
            assert (value.work / "network-gate-stop.json").exists()
            assert value.report["network_gate_stop_proof_sha256"] is not None
        actual_write(path, raw, mode)

    monkeypatch.setattr(validation, "write", checked_write)

    def show():
        allowed = (value.work / "root-allow.json").exists()
        if allowed:
            assert len(observations) >= 2
            if not (value.work / "output/market-result.json").exists():
                save(value.work / "output/market-result.json", result(value))
        return {
            "MainPID": "0"
            if (value.work / "result-ack.json").exists()
            or (preflight_only and pid_reused and value.report["network_gate_verified_before_cleanup"])
            else "222",
            "ExecMainPID": "222",
            "InvocationID": "b" * 32,
            "ControlGroup": "/system.slice/" + value.unit,
            "ExecMainStatus": "0",
            "Result": "success",
        }

    monkeypatch.setattr(value, "show", show)
    report = value.execute()
    assert report["passed"] is (not bad_proof and not pid_reused and not close_failure)
    if close_failure:
        from scripts import remote_release

        assert report["unit_reference_error_class"] == "reference_state_rejected"
        assert report["network_gate_cleaned"] is False
        assert remote_release.valid_network_cleanup_record(report)
    assert report["unit_stopped"] is (not pid_reused)
    assert report["original_runtime_unchanged"] is True
    assert (value.work / "root-allow.json").exists() is (not bad_proof and not preflight_only)
    if preflight_only:
        assert report["market_provider_verified"] is False
    if bad_proof:
        assert "market_allow_public_http" not in boundaries
        assert report["error_class"] == "proof_rejected"
    if pid_reused:
        assert report["error_class"] == "cleanup_failed"
        if preflight_only:
            assert report["network_gate_verified_before_cleanup"] and not report["network_gate_verified"]
    stops = [argv for argv in calls if argv[:2] == ["/usr/bin/systemctl", "stop"]]
    assert stops == ([] if pid_reused else [["/usr/bin/systemctl", "stop", value.unit]])
    assert value.unit.startswith("prism-market-probe-") and "stock-app.service" not in str(stops)
    assert events[-1] == report


@pytest.mark.parametrize("code", [0, 11, 97, 111])
def test_failed_network_errno_stays_typed_without_becoming_a_deny(tmp_path, code):
    value, _, _, _ = engine(tmp_path)
    document = proof(value)
    document["additional_network_errno"]["ipv6_loopback"] = code
    save(value.work / "output/bootstrap.json", document)
    with pytest.raises(validation.ValidationError, match="proof_rejected"):
        value.proof()
    assert value.report["additional_network_errno"]["ipv6_loopback"] == code
    assert not value.report["network_gate_verified"] and not (value.work / "root-allow.json").exists()


def test_changed_network_gate_blocks_market_result_before_reading_worker_data(tmp_path):
    value, _, _, _ = engine(tmp_path)
    value.network_gate.verify = lambda: (_ for _ in ()).throw(RuntimeError("synthetic changed hook"))
    with pytest.raises(validation.ValidationError, match="network_gate_rejected"):
        value.result()
    assert not value.report["market_provider_verified"]


@pytest.mark.parametrize("mode", ["inventory_exists", "launch_uncertain", "identity_unknown"])
def test_unknown_or_preexisting_instance_never_receives_cleanup_stop(tmp_path, monkeypatch, mode):
    value, _, events, _ = engine(tmp_path)
    calls, observed = [], []
    monkeypatch.setattr(validation.sys, "platform", "linux")
    monkeypatch.setattr(validation.os, "getuid", lambda: 0)
    monkeypatch.setattr(validation, "network_policy", lambda: (value.pins, value.negative))
    monkeypatch.setattr(value, "production", lambda: ("original-invocation", 100))
    monkeypatch.setattr(value, "workspace", lambda: setattr(value, "workspace_created", True))
    monkeypatch.setattr(validation, "startticks", lambda path: 55)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv == value.inventory_command():
            return (value.unit + " loaded active running\n").encode() if mode == "inventory_exists" else b""
        if argv[0] == "/usr/bin/systemd-run" and mode == "launch_uncertain":
            raise TimeoutError("synthetic uncertain delivery")
        return b""

    def show():
        observed.append("read_only_show")
        return {
            "MainPID": "222",
            "ExecMainPID": "999",
            "InvocationID": "b" * 32,
            "ControlGroup": "/system.slice/" + value.unit,
        }

    value.run = run
    monkeypatch.setattr(value, "show", show)
    result = value.execute()
    assert result["passed"] is False and result["unit_stopped"] is False and events[-1] == result
    assert not any(argv[:2] == ["/usr/bin/systemctl", "stop"] for argv in calls)
    assert calls[0] == value.inventory_command()
    if mode == "inventory_exists":
        assert not observed and not any(argv[0] == "/usr/bin/systemd-run" for argv in calls)
        assert result["error_class"] == "unit_rejected"
    else:
        assert observed and result["cleanup_error_class"] == "cleanup_failed"
        assert result["error_class"] in {"supervision_failed", "unit_rejected"}


@pytest.mark.parametrize(
    "change", ["invocation", "exec_pid", "main_pid", "cgroup", "pid_reused", "startticks"]
)
def test_cleanup_rechecks_bound_invocation_pid_startticks_and_cgroup(tmp_path, monkeypatch, change):
    value, calls, _, _ = engine(tmp_path)
    value.instance = {
        "pid": 222,
        "invocation": "b" * 32,
        "ticks": 55,
        "control_group": "/system.slice/" + value.unit,
    }
    state = {
        "MainPID": "222",
        "ExecMainPID": "222",
        "InvocationID": "b" * 32,
        "ControlGroup": "/system.slice/" + value.unit,
    }
    if change == "invocation":
        state["InvocationID"] = "e" * 32
    elif change == "exec_pid":
        state["ExecMainPID"] = "999"
    elif change == "main_pid":
        state["MainPID"] = "999"
    elif change == "cgroup":
        state["ControlGroup"] = "/system.slice/stock-app.service"
    elif change == "pid_reused":
        state["MainPID"] = "0"
        monkeypatch.setattr(validation, "PROC", tmp_path)
        (tmp_path / "222").mkdir()
    else:
        value.ticks = 999
    monkeypatch.setattr(value, "show", lambda: state)
    monkeypatch.setattr(value, "identity", lambda: pytest.fail("changed identity must not be accepted"))
    assert value.same_instance_for_cleanup() is False
    assert not calls


def test_launch_is_not_owned_until_actual_kernel_identity_passes(tmp_path, monkeypatch):
    value, _, _, _ = engine(tmp_path)
    state = {
        "MainPID": "222",
        "ExecMainPID": "222",
        "InvocationID": "b" * 32,
        "ControlGroup": "/system.slice/" + value.unit,
    }

    def rejected():
        raise validation.ValidationError("kernel_rejected")

    monkeypatch.setattr(value, "identity", rejected)
    with pytest.raises(validation.ValidationError, match="kernel_rejected"):
        value.acquire_instance(state)
    assert value.instance is None and value.launched is False
