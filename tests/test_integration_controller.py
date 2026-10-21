"""Offline orchestration only: fake systemd, synthetic workdirs, no live models."""

import ast
import base64
import json
import os
import shlex
import subprocess
import sys
import types
from pathlib import Path

import pytest
import test_release_controller as existing

remote, controller = existing.remote, existing.controller
NOW, COMMIT, RELEASE = existing.NOW, existing.COMMIT, existing.RELEASE


@pytest.fixture
def prepared():
    fixture = existing.Fixture()
    fixture.setUp()
    fixture.prepared()
    try:
        yield fixture
    finally:
        fixture.tearDown()


def integration_fixture(fixture, monkeypatch, action="integration_astock", corrupt=None):
    engine = fixture.engine
    kind, pin = remote.INTEGRATION_PROBES[action]
    filename = controller.INTEGRATION_PROBES[action][0]
    source = (existing.ROOT / "scripts" / filename).read_bytes()
    assert remote.digest(source) == pin
    data = dict(
        fixture.data,
        action=action,
        integration_probe=base64.b64encode(source).decode(),
        integration_probe_sha256=pin,
    )
    account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    monkeypatch.setattr(engine, "integration_account", lambda: account)
    monkeypatch.setattr(engine, "integration_idle", lambda uid: None)
    monkeypatch.setattr(
        engine,
        "integration_identity",
        lambda *args: {
            "pid": 222,
            "production_pid": 111,
            "dedicated_user": True,
            "separate_network_namespace": True,
            "separate_stage_namespace": True,
            "read_only_private_runtime_mount": True,
            "checked_at": NOW,
        },
    )
    process = fixture.root / "proc/111"
    process.mkdir(parents=True)
    (process / "cwd").symlink_to(engine.production)
    original = engine.run
    state = {}

    def run(args, **kwargs):
        if args[0] == "/usr/bin/systemd-run":
            fixture.commands.append((args, kwargs))
            work = engine.operations / "integration" / data.get("integration_attempt", RELEASE) / kind
            nonce = args[args.index("--nonce") + 1] if action == "integration_astock" else args[-4]
            proof = {
                "schema": 1,
                "candidate": COMMIT,
                "engine": (engine.release / ".engine-revision").read_text().strip(),
                "nonce": nonce,
                "pid": 222,
                "production_pid": 111,
                "issued_at": NOW,
                "checked_at": NOW,
                "probe_sha256": pin,
                "checks": dict.fromkeys(remote.INTEGRATION_BOOT_CHECKS, True),
            }
            state.update(work=work, proof=proof)
            if corrupt == "proof":
                proof["pid"] = 333
            path = work / "output/bootstrap-proof.json"
            path.write_bytes(remote.encoded(proof))
            path.chmod(0o600)
            return b""
        if args[:3] == ["/usr/bin/systemctl", "show", "--property=MainPID,ExecMainStatus,Result"]:
            assert (state["work"] / "root-allow.json").exists()
            proof = state["proof"]
            result = {key: proof[key] for key in ("schema", "candidate", "nonce", "pid", "probe_sha256")}
            result.update(
                checked_at=NOW,
                passed=True,
                error_class=None,
                checks=dict.fromkeys(remote.INTEGRATION_RESULT_CHECKS[action], True),
            )
            if action == "integration_astock":
                result.update(
                    probe="astock_import_tls",
                    engine=proof["engine"],
                    production_pid=111,
                    network_namespace_verified=False,
                    live_model_verified=False,
                    live_market_verified=False,
                    error_detail=None,
                )
                name = "result.json"
            else:
                result.update(
                    probe="cli_protocol_fixture",
                    cli_sha256=fixture.cli_digest,
                    cli_protocol_fixture_verified=True,
                    requests={
                        phase: {"messages": 1, "count_tokens": 0}
                        for phase in ("stream_new", "stream_resume", "auxiliary")
                    },
                    fixture_model_configuration_override=True,
                    live_model_verified=False,
                    native_search_verified=False,
                    native_fetch_verified=False,
                )
                name = "cli-result.json"
            if corrupt == "result":
                result["live_model_verified"] = True
            path = state["work"] / "output" / name
            path.write_bytes(remote.encoded(result))
            path.chmod(0o600)
            state["result"] = result
            return b"MainPID=0\nExecMainStatus=0\nResult=success\n"
        return original(args, **kwargs)

    monkeypatch.setattr(engine, "run", run)
    return data, state


@pytest.mark.parametrize("action", remote.INTEGRATION_PROBES)
def test_independent_actions_gate_and_preserve_scope(prepared, monkeypatch, action):
    data, state = integration_fixture(prepared, monkeypatch, action)
    result = prepared.engine.execute(data)["integration"]
    assert result["passed"] and result["unit_stopped"]
    assert result["live_model_verified"] is False and result["live_market_verified"] is False
    assert result["native_search_verified"] is False and result["native_fetch_verified"] is False
    assert result["identity"]["read_only_private_runtime_mount"] is True
    assert result["deployment_contract"]["systemd_syntax_verified"] is True
    assert result["deployment_contract"]["unit_started"] is False
    allow = state["work"] / "root-allow.json"
    assert allow.stat().st_mode & 0o777 == 0o444
    assert json.loads(allow.read_bytes())["bootstrap_proof_sha256"] == result["bootstrap_proof_sha256"]
    command = next(
        args
        for args, _ in prepared.commands
        if args[0] == "/usr/bin/systemd-run"
        and any(value.startswith("--unit=prism-integration-") for value in args)
    )
    for prop in (
        "User=prism-integration",
        "Group=prism-integration",
        "PrivateNetwork=yes",
        "ProtectHome=yes",
        "ProtectProc=invisible",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "TemporaryFileSystem=/run:ro",
        "RuntimeMaxSec=60",
    ):
        assert "--property=" + prop in command
    assert "--collect" not in command and "-i" in command
    assert "--property=InaccessiblePaths=-" + str(prepared.engine.operations / "staging") in command
    stops = [args for args, _ in prepared.commands if args[:2] == ["/usr/bin/systemctl", "stop"]]
    assert len(stops) == 1 and stops[0][-1].startswith("prism-integration-")
    assert prepared.boundaries[-3:] == [
        "integration_validate_launch",
        "integration_start",
        "integration_allow_imports",
    ]
    syntax = next(args for args, _ in prepared.commands if args[0] == "/usr/bin/systemd-analyze")
    assert syntax[1] == "verify"
    contract = Path(syntax[2]).read_text()
    assert remote.digest(Path(syntax[2]).read_bytes()) == result["deployment_contract"]["unit_sha256"]
    assert str(prepared.engine.production / ".env") not in contract
    assert "EnvironmentFile=" + str(state["work"] / "synthetic-environment") in contract
    assert (
        prepared.engine.record
        / ("integration-" + remote.INTEGRATION_PROBES[action][0] + "-" + RELEASE + ".json")
    ).exists()
    if action == "integration_cli":
        assert remote.CLI_INTEGRATION_BOOTSTRAP in command
        parsed = ast.parse(remote.CLI_INTEGRATION_BOOTSTRAP)
        imports = {node.names[0].name for node in ast.walk(parsed) if isinstance(node, ast.Import)}
        assert imports == {"errno", "hashlib", "json", "os", "re", "socket", "stat", "sys", "time"}
        assert remote.CLI_INTEGRATION_BOOTSTRAP.index(
            'allow["allowed_at"]'
        ) < remote.CLI_INTEGRATION_BOOTSTRAP.index("exec(compile(content")
    else:
        assert remote.CLI_INTEGRATION_BOOTSTRAP not in command


@pytest.mark.parametrize("case", ["proof", "result", "permit", "cleanup"])
def test_failed_attempt_preserved_and_cannot_be_reused(prepared, monkeypatch, case):
    data, state = integration_fixture(prepared, monkeypatch, corrupt=case)
    if case == "permit":
        monkeypatch.setattr(
            prepared.engine,
            "permit",
            lambda nonce, label: None if label == "integration_allow_imports" else nonce,
        )
    if case == "cleanup":
        calls = []

        def idle(uid):
            calls.append(uid)
            if len(calls) >= 3:
                raise remote.ReleaseError("integration_process_active")

        monkeypatch.setattr(prepared.engine, "integration_idle", idle)
    with pytest.raises(remote.ReleaseError):
        prepared.engine.execute(data)
    path = prepared.engine.record / ("integration-astock-" + RELEASE + ".json")
    receipt = json.loads(path.read_bytes())
    assert receipt["passed"] is False and receipt["failure_code"]
    assert state["work"].is_dir()
    if case in {"proof", "permit"}:
        assert not (state["work"] / "root-allow.json").exists()
    monkeypatch.setattr(prepared.engine, "integration_idle", lambda uid: None)
    with pytest.raises(remote.ReleaseError, match="integration_attempt_already_exists"):
        prepared.engine.execute(data)
    assert json.loads(path.read_bytes()) == receipt


@pytest.mark.parametrize("case", ["hash", "bytes", "attempt"])
def test_source_or_attempt_rejection_before_workspace(prepared, monkeypatch, case):
    data, _ = integration_fixture(prepared, monkeypatch)
    if case == "hash":
        data["integration_probe_sha256"] = "f" * 64
    elif case == "bytes":
        data["integration_probe"] = base64.b64encode(b"raise RuntimeError('must never run')").decode()
    else:
        data["integration_attempt"] = "../../production"
    with pytest.raises(remote.ReleaseError):
        prepared.engine.execute(data)
    assert not (prepared.engine.operations / "integration").exists()


def test_new_attempt_keeps_existing_evidence(prepared, monkeypatch):
    data, state = integration_fixture(prepared, monkeypatch)
    prepared.engine.execute(data)
    old = state["work"] / "output/result.json"
    original = old.read_bytes()
    data["integration_attempt"] = "release-new-attempt-002"
    prepared.engine.execute(data)
    assert old.read_bytes() == original
    assert state["work"].parent.name == "release-new-attempt-002"


@pytest.mark.parametrize(
    "passed,detail,accepted",
    [
        (False, "startup_hook_rejected", True),
        (False, "module_not_found", True),
        (False, "unreviewed diagnostic text", False),
        (False, None, False),
        (True, "startup_hook_rejected", False),
        (True, None, True),
    ],
)
def test_astock_diagnostics_accept_only_fixed_failure_categories(
    prepared, monkeypatch, passed, detail, accepted
):
    data, state = integration_fixture(prepared, monkeypatch)
    prepared.engine.execute(data)
    value = dict(state["result"], passed=passed, error_detail=detail)
    value["error_class"] = None if passed else "astock_import_rejected"
    path = state["work"] / "output/result.json"
    path.write_bytes(remote.encoded(value))
    expected = {key: value for key, value in state["proof"].items() if key not in {"checks", "checked_at"}}
    if accepted:
        result, _ = prepared.engine.integration_result(
            "integration_astock", state["work"], os.getuid(), expected, prepared.cli_digest
        )
        assert result["error_detail"] == detail
    else:
        with pytest.raises(remote.ReleaseError, match="integration_result_rejected"):
            prepared.engine.integration_result(
                "integration_astock", state["work"], os.getuid(), expected, prepared.cli_digest
            )


def test_astock_diagnostic_enum_matches_frozen_probe():
    from scripts import integration_probe

    assert remote.ASTOCK_ERROR_DETAILS == integration_probe.ERROR_DETAILS


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("pid", True),
        ("nonce", "f" * 32),
        ("checked_at", True),
        ("checked_at", NOW + 1),
        ("issued_at", NOW - 121),
        ("checks", {"production_http_denied": True}),
    ],
)
def test_proof_strict_types_freshness_and_identity(prepared, field, value):
    work = prepared.root / "proof-work"
    (work / "output").mkdir(parents=True)
    expected = {
        "schema": 1,
        "candidate": COMMIT,
        "engine": "a" * 64,
        "nonce": "b" * 32,
        "pid": 222,
        "production_pid": 111,
        "issued_at": NOW,
        "probe_sha256": "c" * 64,
    }
    proof = dict(expected, checked_at=NOW, checks=dict.fromkeys(remote.INTEGRATION_BOOT_CHECKS, True))
    proof[field] = value
    path = work / "output/bootstrap-proof.json"
    path.write_bytes(remote.encoded(proof))
    path.chmod(0o600)
    with pytest.raises(remote.ReleaseError, match="integration_bootstrap_proof_rejected"):
        prepared.engine.integration_proof(work, os.getuid(), expected)


@pytest.mark.parametrize("case", ["shared_uid", "shared_gid", "supplementary", "shell"])
def test_account_is_not_stage_or_production(prepared, monkeypatch, case):
    account = types.SimpleNamespace(pw_uid=303, pw_gid=303, pw_shell="/usr/sbin/nologin")
    if case == "shared_uid":
        account.pw_uid = 202
    if case == "shared_gid":
        account.pw_gid = 202
    if case == "shell":
        account.pw_shell = "/bin/sh"
    monkeypatch.setattr(
        remote.pwd,
        "getpwnam",
        lambda name: (
            account
            if name == "prism-integration"
            else types.SimpleNamespace(
                pw_uid=202 if name == "prism-stage" else 101, pw_gid=202 if name == "prism-stage" else 101
            )
        ),
    )
    monkeypatch.setattr(
        remote.grp, "getgrgid", lambda gid: types.SimpleNamespace(gr_name="prism-integration")
    )
    monkeypatch.setattr(
        remote.grp,
        "getgrall",
        lambda: [types.SimpleNamespace(gr_mem=["prism-integration"])] if case == "supplementary" else [],
    )
    with pytest.raises(remote.ReleaseError, match="integration_account_not_isolated"):
        prepared.engine.integration_account()


@pytest.mark.parametrize("case", ["valid", "uid", "netns", "stage_netns", "runtime"])
def test_parent_requires_actual_uid_namespaces_and_mount(prepared, monkeypatch, case):
    engine = prepared.engine
    for pid, uid, net in [(111, 101, "net:[1]"), (222, 303, "net:[2]"), (333, 202, "net:[3]")]:
        path = prepared.root / "proc" / str(pid)
        (path / "ns").mkdir(parents=True)
        (path / "status").write_text("Uid:\t" + "\t".join([str(uid)] * 4) + "\n")
        (path / "ns/net").symlink_to(net)
    if case == "uid":
        (prepared.root / "proc/222/status").write_text("Uid:\t303\t0\t303\t303\n")
    if case in {"netns", "stage_netns"}:
        path = prepared.root / ("proc/111/ns/net" if case == "netns" else "proc/333/ns/net")
        path.unlink()
        path.symlink_to("net:[2]")
    monkeypatch.setattr(engine, "process_id", lambda unit: 111 if unit == remote.SERVICE else 222)
    monkeypatch.setattr(engine, "run", lambda *args: b"prism-integration\n")
    monkeypatch.setattr(remote.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_uid=202))
    monkeypatch.setattr(engine, "stage_runtime_mount", lambda path: case != "runtime")
    if case == "valid":
        identity = engine.integration_identity("fixed-unit", 111, 303)
        assert identity["pid"] == 222 and identity["read_only_private_runtime_mount"] is True
    else:
        with pytest.raises(remote.ReleaseError):
            engine.integration_identity("fixed-unit", 111, 303)


@pytest.mark.parametrize("action", remote.INTEGRATION_PROBES)
def test_mac_requires_literal_approved_probe_and_explicit_pin(tmp_path, action):
    receipts = tmp_path / "receipts.json"
    receipts.write_text(json.dumps({"commit": COMMIT}))
    args = types.SimpleNamespace(
        action=action,
        commit=COMMIT,
        release_id=RELEASE,
        receipts=receipts,
        executor_sha256=controller.sha((controller.ROOT / "remote_release.py").read_bytes()),
        source_manifest=None,
        external_source_manifest=None,
        astock_probe_sha256=None,
        cli_probe_sha256=None,
    )
    with pytest.raises(controller.ControlError, match="approved_integration_probe_required"):
        controller.request_for(args)
    field = "astock_probe_sha256" if action == "integration_astock" else "cli_probe_sha256"
    setattr(args, field, controller.INTEGRATION_PROBES[action][1])
    request, _ = controller.request_for(args)
    assert controller.sha(base64.b64decode(request["integration_probe"])) == getattr(args, field)
    assert request["integration_attempt"] == RELEASE


def test_all_deployment_constraints_override_synthetic_environment_file(prepared):
    engine = prepared.engine
    text = engine.dropin(engine.release).decode()
    command = shlex.split(
        next(
            line.removeprefix("ExecStart=")
            for line in text.splitlines()
            if line.startswith("ExecStart=/usr/bin/env ")
        )
    )
    python = command.index(str(engine.release / ".venv/bin/python"))
    constraints = dict(item.split("=", 1) for item in command[1:python])
    assert set(constraints) == {
        "CLAUDE_BIN",
        "STOCK_DATA_DIR",
        "STOCK_LOG_FILE",
        "PRISM_INTERNAL_API_URL",
        "PRISM_ASTOCK_PROJECT",
        "PRISM_SINGLE_WORKER",
        "PRISM_ENVIRONMENT",
        "PYTHONDONTWRITEBYTECODE",
    }
    assert "Environment=" not in text
    # Synthetic EnvironmentFile values arrive before /usr/bin/env, as systemd
    # specifies. Execute the actual assignment boundary without importing app.
    inherited = dict.fromkeys(constraints, "/synthetic-conflicting-setting")
    inherited["SYNTHETIC_CREDENTIAL_SENTINEL"] = "synthetic-only"
    code = (
        "import json,os; print(json.dumps({key:os.environ[key] for key in "
        + repr(list(constraints))
        + "})); assert os.environ['SYNTHETIC_CREDENTIAL_SENTINEL']=='synthetic-only'"
    )
    result = subprocess.run(
        command[:python] + [sys.executable, "-I", "-B", "-c", code],
        env=inherited,
        capture_output=True,
        check=True,
        timeout=5,
    )
    assert json.loads(result.stdout) == constraints
    assert constraints["PRISM_ASTOCK_PROJECT"].startswith(str(prepared.root / "opt/prism/engines/astock"))
    assert constraints["STOCK_DATA_DIR"] == str(engine.production / "data")


@pytest.mark.parametrize("suffix", [" with-space", "%n", "$HOME", '"quoted', "\\escape", ";separator"])
def test_dropin_rejects_systemd_special_paths(prepared, suffix):
    prepared.engine.production = Path(str(prepared.engine.production) + suffix)
    with pytest.raises(remote.ReleaseError, match="systemd_fixed_path_rejected"):
        prepared.engine.dropin(prepared.engine.release)
