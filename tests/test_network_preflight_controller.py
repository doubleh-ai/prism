"""Credential-free fixed actions on disposable state; no Linux/network claims."""

import json
import os
import types
from contextlib import contextmanager
from pathlib import Path

import pytest
import test_live_integration_controller as live_tests
import test_market_controller_candidate as market_tests
import test_release_controller as existing
from test_network_gate_wiring import MemoryKernel, ProofReference, Proxy, root_metadata

NOW, COMMIT = existing.NOW, existing.COMMIT
live_prepared, market_prepared = live_tests.prepared, market_tests.prepared
modules = market_tests.modules


@pytest.fixture(params=("live", "market"))
def context(request, monkeypatch):
    role = request.param
    value = request.getfixturevalue(role + "_prepared")
    if role == "live":
        fixture, data = value
        value = types.SimpleNamespace(
            fixture=fixture,
            engine=fixture.engine,
            data=data,
            remote=live_tests.remote,
            controller=live_tests.controller,
        )
    value.role = role
    value.attempt = value.data["integration_attempt"]
    value.data["action"] = "integration_" + role + "_network_preflight"
    for key in ("archive", "dependencies", "external_source_manifest"):
        value.data.pop(key, None)
    value.data["receipts"].pop("model_environment", None)
    value.data["receipts"].pop("expected_current_fingerprint", None)
    value.metadata = {
        "MainPID": "111",
        "InvocationID": "a" * 32,
        "WorkingDirectory": str(value.engine.production),
        "ExecStart": "{ path=/trusted/python ; argv[]=python app.py ; }",
    }
    process = value.fixture.root / "proc/111"
    process.mkdir(parents=True, exist_ok=True)
    (process / "stat").write_text("111 (synthetic process) S " + "0 " * 18 + "555")
    (process / "cwd").symlink_to(value.engine.production)
    facts = {
        "schema": 1,
        "candidate": COMMIT,
        "unit": value.remote.SERVICE,
        "production_pid": 111,
        "production_startticks": 555,
        "invocation_id": value.metadata["InvocationID"],
        "working_directory": value.metadata["WorkingDirectory"],
        "exec_start_sha256": value.remote.digest(value.metadata["ExecStart"].encode()),
        "source_manifest_sha256": value.data["receipts"]["expected_source_manifest_sha256"],
    }
    value.data["receipts"]["expected_network_runtime_fingerprint"] = value.remote.digest(
        value.remote.encoded(facts)
    )
    existing.bind(value.data)
    original_run = value.engine.run
    command = [
        "/usr/bin/systemctl",
        "show",
        "--property=" + value.remote.NETWORK_RUNTIME_PROPERTIES,
        value.remote.SERVICE,
    ]

    def observations(argv, **kwargs):
        if argv == command:
            return ("\n".join(key + "=" + item for key, item in value.metadata.items()) + "\n").encode()
        if argv == value.remote.LIVE_RESOLVE_COMMAND:
            return b'["1.1.1.1"]'
        return original_run(argv, **kwargs)

    monkeypatch.setattr(value.engine, "run", observations)
    for name in ("runtime", "ready", "live_environment_binding", "read_bound_model_key", "live_model_gate"):
        monkeypatch.setattr(
            value.engine,
            name,
            lambda *args, _name=name, **kwargs: pytest.fail("forbidden preflight path: " + _name),
        )
    original_open = os.open

    def no_credentials(path, *args, **kwargs):
        if os.fspath(path).endswith("/.env"):
            pytest.fail("preflight opened a credential file")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(value.remote.os, "open", no_credentials)
    monkeypatch.setattr(
        value.remote.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("preflight HTTP")
    )
    return value


def safe_result(context, module):
    if context.role == "live":
        result = live_tests.safe_summary(context.data)
        result.update(
            action="integration_network_preflight",
            stage="network_preflight_complete",
            preflight_only=True,
            key_acquisition_attempted=False,
            worker_result_sha256=None,
            broker_summary_sha256=None,
            official_source_count=0,
        )
        for name in (
            "live_model_verified",
            "native_search_verified",
            "owned_mcp_verified",
            "server_clock_verified",
        ):
            result[name] = False
        result["requests"] = dict.fromkeys(result["requests"], 0)
        return result
    result = market_tests.synthetic_result(context, module, accepted=True)
    result.update(
        probe="credential_free_market_network_preflight",
        scope="pinned_tls_handshake_only",
        preflight_only=True,
        market_provider_verified=False,
    )
    result.pop("worker_result")
    result.pop("worker_result_sha256")
    return result


def record_path(context):
    return context.engine.record / ("network-preflight-" + context.role + "-" + context.attempt)


def install_driver(context, monkeypatch, *, mutation=None, cleanup_failure=False, cancel_cleanup=False):
    method = context.role + "_module"
    original_module = getattr(context.engine, method)
    events = []
    holder = {}

    @contextmanager
    def load(contents):
        with original_module(contents) as module:
            original_execute = module.Validation.execute

            def execute(value):
                assert value.preflight_only is True
                if context.role == "live":
                    assert value.key_provider is None
                assert value.network_gate_path == Path(module.__file__).with_name("cgroup_network_gate.py")
                assert value.network_gate_path.read_bytes() == contents["cgroup_network_gate.py"]
                # Exercise the real sealed dataclass loader on actual descriptors;
                # only root metadata and the kernel backend are fixtures.
                original_fstat = os.fstat
                with monkeypatch.context() as patch:
                    patch.setattr(
                        module, "root_locked" if context.role == "live" else "locked", lambda path: None
                    )
                    patch.setattr(module, "os", Proxy(os, fstat=lambda fd: root_metadata(original_fstat(fd))))
                    gate_module = module.load_network_gate(value.network_gate_path)
                kernel = MemoryKernel(gate_module, events)
                desc = gate_module.ControllerDescriptor(
                    role=context.role,
                    candidate_prefix=COMMIT[:12],
                    nonce="d" * 32,
                    invocation_id="e" * 32,
                    pid=654 if context.role == "live" else 321,
                    startticks=55,
                    uid=2002,
                    cgroup_inode=91,
                    cgroup_device=31,
                    provider_ipv4=("1.1.1.1",),
                )
                gate = gate_module.OwnedNetworkGate(desc, _kernel=kernel)
                value.network_gate, value.network_programs = gate, gate.install()
                assert gate.verify() == value.network_programs
                value.reference_roles = ("broker", "worker") if context.role == "live" else ("market",)
                value.reference_identities = {}
                value.reference_unique_names = {}
                value.unit_references = {}
                for role in value.reference_roles:
                    identity = {
                        "unit": desc.unit,
                        **{
                            name: getattr(desc, name)
                            for name in ("invocation_id", "pid", "startticks", "uid")
                        },
                    }
                    if role == "worker":
                        identity["unit"] = desc.unit.replace("-broker-", "-worker-")
                        identity["pid"] = 321
                        identity["uid"] = 2001
                    value.reference_identities[role] = identity
                    value.reference_unique_names[role] = ":1.234"
                    value.unit_references[role] = ProofReference(identity)
                value.report["unit_reference_acquire_sha256"] = "a" * 64
                events.append("permit_after_attach")
                value.work = context.engine.operations / "integration" / context.attempt / context.role
                value.candidate = context.engine.release
                value.nonce, value.issued = "d" * 32, NOW
                run = value._run if context.role == "live" else value.run
                if context.role == "live":
                    accounts = {
                        name: types.SimpleNamespace(pw_name=name, pw_uid=uid, pw_gid=uid)
                        for name, uid in (("prism-integration", 2001), ("prism-integration-broker", 2002))
                    }
                    monkeypatch.setattr(context.remote.pwd, "getpwnam", accounts.__getitem__)
                    monkeypatch.setattr(
                        context.remote.grp, "getgrnam", lambda name: types.SimpleNamespace(gr_gid=2003)
                    )
                    value.accounts = {
                        role: accounts["prism-integration" + ("-broker" if role == "broker" else "")]
                        for role in ("broker", "worker")
                    }
                    value.bridge_gid, value.pids = 2003, {"broker": 654}
                    value.units = {
                        role: "prism-live-" + role + "-" + COMMIT[:12] + "-" + value.nonce + ".service"
                        for role in ("broker", "worker")
                    }
                    resolver = context.fixture.root / "resolv.conf"
                    resolver.write_text("nameserver 127.0.0.53\n")
                    module.RESOLV = resolver
                    previous_run = context.engine.run

                    def states(argv, **kwargs):
                        if argv[:2] == ["/usr/bin/systemctl", "show"] and argv[-1] in value.units.values():
                            role = next(role for role in value.units if value.units[role] == argv[-1])
                            return ("MainPID=" + ("654" if role == "broker" else "321") + "\n").encode()
                        return previous_run(argv, **kwargs)

                    monkeypatch.setattr(context.engine, "run", states)
                    run(context.remote.LIVE_RESOLVE_COMMAND, timeout=8)
                    commands = [
                        (role, value.unit_command(role, ["1.1.1.1"], ["127.0.0.53"]))
                        for role in ("broker", "worker")
                    ]
                    for role, argv in commands:
                        run(
                            [
                                "/usr/bin/systemctl",
                                "list-units",
                                "--all",
                                "--plain",
                                "--no-legend",
                                value.units[role],
                            ],
                            timeout=5,
                        )
                        if mutation is not None:
                            argv = mutation(argv)
                        run(argv, timeout=15)
                        value.show(role)
                    units = value.units.values()
                else:
                    value.unit = "prism-market-probe-" + COMMIT[:12] + "-" + value.nonce + ".service"
                    value.pins = {"qt.gtimg.cn": ["1.1.1.1"], "money.finance.sina.com.cn": ["8.8.8.8"]}
                    value.negative = "9.9.9.9"
                    run(value.inventory_command(), timeout=5)
                    argv = value.command()
                    run(mutation(argv) if mutation else argv, timeout=15)
                    units = [value.unit]
                events.extend(("strict_negative_13", "tls_handshake_fixture"))
                if cancel_cleanup:
                    monkeypatch.setattr(context.engine, "now", lambda: NOW + 181)
                    monkeypatch.setattr(context.engine, "control", lambda: True)
                    if context.role == "live":
                        new_commands = [
                            ["/usr/sbin/groupadd", "--system", "prism-integration-bridge"],
                            context.remote.LIVE_RESOLVE_COMMAND,
                            [
                                "/usr/sbin/useradd",
                                "--system",
                                "--user-group",
                                "--no-create-home",
                                "--shell",
                                "/usr/sbin/nologin",
                                "prism-integration",
                            ],
                        ]
                    else:
                        new_commands = [
                            [
                                "/usr/bin/systemctl",
                                "show",
                                "--property=MainPID,InvocationID,WorkingDirectory",
                                context.remote.SERVICE,
                            ]
                        ]
                    new_commands.append(commands[0][1] if context.role == "live" else value.command())
                    for argv in new_commands:
                        with pytest.raises(context.remote.ReleaseError):
                            run(argv, timeout=5)
                    with pytest.raises(context.remote.ReleaseError):
                        run(["/usr/bin/systemctl", "stop", "stock-app.service"], timeout=5)
                    with pytest.raises(context.remote.ReleaseError):
                        run(
                            ["/usr/bin/systemctl", "show", "--property=Environment", "stock-app.service"],
                            timeout=5,
                        )
                value.work.mkdir(parents=True, exist_ok=True)
                value.prepare_network_stop()
                for unit in units:
                    if cancel_cleanup:
                        properties = (
                            "MainPID,ExecMainPID,InvocationID,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE"
                            if context.role == "live"
                            else context.remote.MARKET_UNIT_PROPERTIES
                        )
                        run(["/usr/bin/systemctl", "show", "--property=" + properties, unit], timeout=5)
                        events.append("cleanup_identity_under_stop")
                    run(["/usr/bin/systemctl", "stop", unit], timeout=5)
                    if cancel_cleanup:
                        run(
                            [
                                "/usr/bin/systemctl",
                                "show",
                                "--property=" + ("MainPID" if context.role == "live" else properties),
                                unit,
                            ],
                            timeout=5,
                        )
                        events.append("cleanup_reconcile_under_stop")
                kernel.stopped = True
                if cleanup_failure:
                    kernel.check_identity = lambda *args, **kwargs: (_ for _ in ()).throw(
                        gate_module.GateError("collected_cgroup")
                    )
                    with pytest.raises(gate_module.GateError, match="network_gate_cleanup_failed"):
                        gate.cleanup()
                else:
                    value.cleanup_network_gate()
                    if context.role == "live":
                        value.release_unit_reference("worker")
                    assert gate.state == "cleaned"
                result = safe_result(context, module)
                for name in context.remote.NETWORK_CLEANUP_FIELDS:
                    result[name] = value.report[name]
                if cancel_cleanup:
                    result.update(passed=False, error_class="operator_stop")
                if cleanup_failure:
                    result.update(
                        passed=False,
                        error_class="cleanup_failed",
                        cleanup_error_class="cleanup_failed",
                        network_gate_cleaned=False,
                        network_gate_verified=False,
                        network_gate_objects_released=False,
                    )
                value.record(result)
                holder["result"] = result
                return result

            monkeypatch.setattr(module.Validation, "execute", execute)
            holder["original_execute"] = original_execute
            yield module

    monkeypatch.setattr(context.engine, method, load)
    return events, holder


def test_fixed_preflight_uses_no_business_or_credential_or_http_path(context, monkeypatch):
    events, holder = install_driver(context, monkeypatch)
    result = context.engine.execute(context.data)
    assert "integration" not in result
    report = result["network_preflight"]
    assert report["preflight_only"] and report["network_gate_verified"] and report["passed"]
    assert report["summary"]["live_model_verified"] is False
    assert report["summary"]["market_provider_verified"] is False
    assert (
        events.index("attach_15")
        < events.index("permit_after_attach")
        < events.index("strict_negative_13")
        < events.index("tls_handshake_fixture")
        < events.index("detach_10")
    )
    receipt = json.loads((record_path(context) / "receipt.json").read_bytes())
    assert receipt["operation"] == context.data["action"] and receipt["baseline_kind"] == "network_metadata"
    assert receipt["summary"] == holder["result"]
    assert not (context.engine.record / ("integration-" + context.role + "-" + context.attempt)).exists()


def test_collected_cgroup_is_a_failed_preflight_with_preserved_before_cleanup(context, monkeypatch):
    install_driver(context, monkeypatch, cleanup_failure=True)
    with pytest.raises(context.remote.ReleaseError):
        context.engine.execute(context.data)
    result = json.loads((record_path(context) / "receipt.json").read_bytes())["summary"]
    assert result["network_gate_verified_before_cleanup"] is True
    assert result["network_gate_verified"] is False and result["network_gate_cleaned"] is False
    assert result["passed"] is False and result["cleanup_error_class"] == "cleanup_failed"


def test_owned_cleanup_survives_expired_budget_and_operator_stop(context, monkeypatch):
    events, holder = install_driver(context, monkeypatch, cancel_cleanup=True)
    with pytest.raises(context.remote.ReleaseError):
        context.engine.execute(context.data)
    count = 2 if context.role == "live" else 1
    assert events.count("cleanup_identity_under_stop") == count
    assert events.count("cleanup_reconcile_under_stop") == count
    assert "detach_15" in events
    assert holder["result"]["network_gate_cleaned"] is True
    assert holder["result"]["passed"] is False
    assert holder["result"]["error_class"] == "operator_stop"


@pytest.mark.parametrize("change", ("pid", "ticks", "invocation", "command", "cwd", "source", "receipt"))
def test_metadata_baseline_rejects_any_bound_fact_change_without_http(context, change):
    context.engine.network_original_state()
    if change == "pid":
        context.metadata["MainPID"] = "222"
    elif change == "ticks":
        path = context.fixture.root / "proc/111/stat"
        path.write_text(path.read_text().removesuffix("555") + "556")
    elif change == "invocation":
        context.metadata["InvocationID"] = "b" * 32
    elif change == "command":
        context.metadata["ExecStart"] += " altered"
    elif change == "cwd":
        context.metadata["WorkingDirectory"] += "/other"
    elif change == "source":
        (context.engine.production / "app.py").write_text("# altered source\n")
    else:
        context.data["receipts"]["expected_network_runtime_fingerprint"] = "f" * 64
    with pytest.raises((context.remote.ReleaseError, FileNotFoundError)):
        context.engine.network_original_state()


@pytest.mark.parametrize(
    "extra",
    ("network_gate_path", "preflight_only", "pins", "command", "key_provider", "external_source_manifest"),
)
def test_remote_preflight_rejects_caller_selected_path_mode_or_behavior(context, extra):
    context.data[extra] = "/unreviewed"
    with pytest.raises(context.remote.ReleaseError, match="network_preflight_request_rejected"):
        getattr(context.engine, context.data["action"])()


@pytest.mark.parametrize("mutation", ("uid", "bpf_mode", "helper", "runtime", "shell", "provider"))
def test_preflight_reconstructs_every_owned_command(context, monkeypatch, mutation):
    def mutate(argv):
        argv = list(argv)
        if mutation == "uid":
            return [item.replace("User=prism-integration", "User=root") for item in argv]
        if mutation == "runtime":
            return [
                item.replace("RuntimeMaxSec=180", "RuntimeMaxSec=181").replace(
                    "RuntimeMaxSec=60", "RuntimeMaxSec=61"
                )
                for item in argv
            ]
        if mutation == "shell":
            argv[argv.index("/usr/bin/env")] = "/bin/sh"
            return argv
        if mutation == "provider":
            return [item.replace("IPAddressAllow=1.1.1.1", "IPAddressAllow=8.8.4.4") for item in argv]
        config = json.loads(argv[-1])
        config["preflight_only" if mutation == "bpf_mode" else "network_gate_sha256"] = (
            False if mutation == "bpf_mode" else "f" * 64
        )
        argv[-1] = context.remote.encoded(config).decode()
        return argv

    install_driver(context, monkeypatch, mutation=mutate)
    with pytest.raises(context.remote.ReleaseError, match=context.role + "_command_rejected"):
        context.engine.execute(context.data)
    assert not any(
        argv[0] == "/usr/bin/systemd-run"
        for argv, _ in context.fixture.commands
        if "--property=RemainAfterExit=yes" in argv
    )


@pytest.mark.parametrize("code", (11, 97, 110))
def test_strict_errno_never_accepts_timeout_or_eagain(context, code):
    with getattr(context.engine, context.role + "_module")(
        getattr(context.engine, context.role + "_helper_sources")()
    ) as module:
        result = safe_result(context, module)
        key = "broker_negative_errno" if context.role == "live" else "additional_network_errno"
        result[key][next(iter(result[key]))] = code

        def check():
            if context.role == "live":
                return context.engine.live_safe_record(result, context.attempt)
            return context.engine.market_safe_record(
                result, context.attempt, module, context.engine.network_original_state()
            )

        with pytest.raises(context.remote.ReleaseError, match=context.role + "_safe_record_rejected"):
            check()
        result.update(
            passed=False,
            error_class="broker_preflight_rejected" if context.role == "live" else "proof_rejected",
            network_gate_verified=False,
            network_gate_verified_before_cleanup=False,
        )
        assert check()[key] == result[key]


def test_mac_preflight_requires_new_fingerprint_and_helper_pin_without_model_binding(context, tmp_path):
    arguments = (
        live_tests.mac_arguments(context.fixture, context.data, tmp_path)
        if context.role == "live"
        else market_tests.mac_arguments(context, tmp_path)
    )
    arguments.action = context.data["action"]
    request, _ = context.controller.request_for(arguments)
    assert "model_environment" not in request["receipts"]
    assert request["receipts"]["expected_network_runtime_fingerprint"]
    assert (
        request[context.role + "_helpers"]["cgroup_network_gate.py"]["sha256"]
        == context.remote.LIVE_INTEGRATION_HELPERS["cgroup_network_gate.py"]
    )
    arguments.cgroup_network_gate_sha256 = None
    with pytest.raises(context.controller.ControlError, match="approved_.*_integration_helpers_required"):
        context.controller.request_for(arguments)
    arguments.cgroup_network_gate_sha256 = context.remote.LIVE_INTEGRATION_HELPERS["cgroup_network_gate.py"]
    receipts = json.loads(Path(arguments.receipts).read_bytes())
    receipts.pop("expected_network_runtime_fingerprint")
    Path(arguments.receipts).write_bytes(context.remote.encoded(receipts))
    with pytest.raises(
        context.controller.ControlError, match=context.role + "_integration_private_bindings_required"
    ):
        context.controller.request_for(arguments)


def test_a_verified_flag_requires_strict_denials_even_on_a_failed_business_result(context):
    with getattr(context.engine, context.role + "_module")(
        getattr(context.engine, context.role + "_helper_sources")()
    ) as module:
        result = safe_result(context, module)
        result.update(
            passed=False,
            error_class="cleanup_failed",
            cleanup_error_class="cleanup_failed",
            network_gate_verified=False,
            network_gate_cleaned=False,
            network_gate_objects_released=False,
        )
        key = "broker_negative_errno" if context.role == "live" else "negative_errno"
        result[key][next(iter(result[key]))] = 11
        with pytest.raises(context.remote.ReleaseError, match=context.role + "_safe_record_rejected"):
            if context.role == "live":
                context.engine.live_safe_record(result, context.attempt)
            else:
                context.engine.market_safe_record(
                    result, context.attempt, module, context.engine.network_original_state()
                )


def test_preflight_cannot_relabel_network_proof_as_business_acceptance(context):
    with getattr(context.engine, context.role + "_module")(
        getattr(context.engine, context.role + "_helper_sources")()
    ) as module:
        result = safe_result(context, module)
        if context.role == "live":
            result["live_model_verified"] = True
            with pytest.raises(context.remote.ReleaseError, match="live_safe_record_rejected"):
                context.engine.live_safe_record(result, context.attempt)
        else:
            result["market_provider_verified"] = True
            with pytest.raises(context.remote.ReleaseError, match="market_safe_record_rejected"):
                context.engine.market_safe_record(
                    result, context.attempt, module, context.engine.network_original_state()
                )


@pytest.mark.parametrize(
    "change",
    [
        "raw_diagnostic",
        "bool_errno",
        "unknown_stage",
        "unknown_command",
        "missing_stop",
        "missing_cleanup",
        "unreleased",
        "partial",
        "bad_mode",
    ],
)
def test_safe_journal_rejects_inconsistent_network_cleanup_evidence(context, change):
    with getattr(context.engine, context.role + "_module")(
        getattr(context.engine, context.role + "_helper_sources")()
    ) as module:
        result = safe_result(context, module)
        if change == "raw_diagnostic":
            result["network_gate_diagnostic"]["raw"] = "synthetic unsafe detail"
        elif change == "bool_errno":
            result["network_gate_diagnostic"]["errno"] = True
        elif change == "unknown_stage":
            result["network_gate_diagnostic"]["stage"] = "unbounded"
        elif change == "unknown_command":
            result["network_gate_diagnostic"]["command"] = 4
        elif change == "missing_stop":
            result["network_gate_stop_proof_sha256"] = None
        elif change == "missing_cleanup":
            result["network_gate_cleanup_proof_sha256"] = None
        elif change == "unreleased":
            result["network_gate_objects_released"] = False
        elif change == "partial":
            result["network_gate_cleanup_mode"] = "partial_closed"
        else:
            result["network_gate_cleanup_mode"] = []
        with pytest.raises(context.remote.ReleaseError, match=context.role + "_safe_record_rejected"):
            if context.role == "live":
                context.engine.live_safe_record(result, context.attempt)
            else:
                context.engine.market_safe_record(
                    result, context.attempt, module, context.engine.network_original_state()
                )
