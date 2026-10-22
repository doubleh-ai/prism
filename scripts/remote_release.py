#!/usr/bin/env python3
"""Fixed-host release RPC. Never run on import; stdin is private, output is redacted.

Only release_controller may invoke this reviewed code. Candidate application code
is not imported by validation. No arbitrary command, host, unit or path is accepted.
"""

from __future__ import annotations

import base64
import ctypes
import fcntl
import grp
import hashlib
import http.client
import io
import json
import os
import pwd
import re
import resource
import secrets
import select
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
import types
import urllib.error
import urllib.request
from contextlib import closing, contextmanager
from http.cookies import SimpleCookie
from pathlib import Path, PurePosixPath

ACTIONS = {
    "inspect",
    "prepare",
    "diagnose_build",
    "quarantine_failed_prepare",
    "stage",
    "finish_stage",
    "integration_astock",
    "integration_cli",
    "integration_live",
    "integration_market",
    "integration_live_network_preflight",
    "integration_market_network_preflight",
    "validate_legacy_retirement",
    "validate_legacy_stop",
    "quiesce",
    "switch",
    "verify",
    "rollback",
    "resume",
}
SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
SERVICE = "stock-app.service"
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_INPUT = 384 * 1024 * 1024
MAX_DEPENDENCIES = 192 * 1024 * 1024
RESERVE = 2 * 1024 * 1024 * 1024
NGINX_SITES = ("stock-analyzer", "stock-app")
HOLD_MESSAGE = "系统升级中，请稍后重试"
CLI_ENTRY = "home/ubuntu/.npm-global/bin/claude"
CLI_SOURCE = "home/ubuntu/.npm-global/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
MAX_CLI_BINARY = 512 * 1024 * 1024
INTEGRATION_PROBES = {
    "integration_astock": ("astock", "612d7194b63e16b7ffd1e9badaa483da1f09a4a270670df8f950628d968a9184"),
    "integration_cli": ("cli-protocol", "bf5726fc01cad92eeb1d6aeff768e46a98fc5ad91c823988cff4e04f357c3789"),
}
MARKET_INTEGRATION_HELPERS = {
    "market_live_probe.py": "904617183a1d9c1c99dca2a07d38a26c4165246385979fc038dfb61a0b0ebbab",
    "market_live_validation.py": "298ff2992bc8c3d9a87a22a956af3c8c8d4373269873c1124ab5ccf09ae6b41f",
    "cgroup_network_gate.py": "da13fbd0110493c4746126fa00fc45f7393ee77a773b5c27a642eaec124f7134",
    "systemd_unit_reference.py": "b160660e92a3cd98e11b37dc21855b9292150b137752c9300f0c234d5a78a4be",
}
MARKET_WORKER_SHA256 = "ef6d587b97f9c8a6ad9c532a20c0ddf56baa46b64774326c09ed689eab86d7cc"
MARKET_BOUNDARIES = {
    "market_workspace",
    "market_start",
    "market_allow_public_http",
    "market_wait_proof",
    "market_wait_result",
    "market_wait_hello",
    "market_wait_exit",
    "market_network_attach",
    "market_network_permit",
}
NETWORK_EXTRA_NEGATIVES = {"ipv6_loopback", "ipv4_mapped_public", "udp_provider", "udp6_loopback"}
NETWORK_DIAGNOSTIC_STAGES = frozenset(
    {
        "not_started",
        "construct",
        "open_cgroup",
        "identity",
        "query_direct",
        "query_effective",
        "load",
        "info",
        "attach",
        "verify",
        "prepare_stop",
        "stop_identity",
        "offline_query",
        "detach",
        "close_owned",
        "object_release",
        "partial_cleanup",
        "complete",
        "unknown",
    }
)
UNIT_REFERENCE_ERRORS = frozenset(
    {
        "reference_platform_rejected",
        "reference_library_rejected",
        "reference_bus_rejected",
        "reference_identity_rejected",
        "reference_refs_rejected",
        "reference_state_rejected",
        "reference_call_failed",
        "reference_close_failed",
    }
)
NETWORK_CLEANUP_FIELDS = {
    "network_gate_diagnostic",
    "network_gate_stop_proof_sha256",
    "network_gate_cleanup_proof_sha256",
    "network_gate_cleanup_mode",
    "network_gate_objects_released",
    "unit_reference_acquire_sha256",
    "unit_reference_stop_sha256",
    "unit_reference_release_sha256",
    "unit_reference_released",
    "unit_reference_error_class",
}


def valid_network_cleanup_record(value):
    """Finite safe evidence only; private kernel proofs remain in the workdir."""
    diagnostic = value.get("network_gate_diagnostic")
    if (
        type(diagnostic) is not dict
        or set(diagnostic) != {"stage", "command", "errno"}
        or type(diagnostic["stage"]) is not str
        or diagnostic["stage"] not in NETWORK_DIAGNOSTIC_STAGES
        or diagnostic["command"] is not None
        and (type(diagnostic["command"]) is not int or diagnostic["command"] not in {5, 8, 9, 13, 15, 16})
        or diagnostic["errno"] is not None
        and (type(diagnostic["errno"]) is not int or not 0 <= diagnostic["errno"] <= 4095)
        or type(value.get("network_gate_objects_released")) is not bool
    ):
        return False
    stop, cleanup = (value.get("network_gate_" + name + "_proof_sha256") for name in ("stop", "cleanup"))
    if any(
        item is not None and (type(item) is not str or not DIGEST.fullmatch(item)) for item in (stop, cleanup)
    ):
        return False
    mode = value.get("network_gate_cleanup_mode")
    if mode is not None and (
        type(mode) is not str or mode not in {"retained_empty", "collected_offline", "partial_closed"}
    ):
        return False
    reference = [value.get("unit_reference_" + name + "_sha256") for name in ("acquire", "stop", "release")]
    if (
        type(value.get("unit_reference_released")) is not bool
        or value.get("unit_reference_error_class") is not None
        and (
            type(value["unit_reference_error_class"]) is not str
            or value["unit_reference_error_class"] not in UNIT_REFERENCE_ERRORS
        )
        or any(
            item is not None and (type(item) is not str or not DIGEST.fullmatch(item)) for item in reference
        )
        or reference[1] is not None
        and reference[0] is None
        or reference[2] is not None
        and any(item is None for item in reference[:2])
        or value["unit_reference_released"] is not (reference[2] is not None)
    ):
        return False
    released = value["network_gate_objects_released"]
    positive = mode in {"retained_empty", "collected_offline"}
    return (
        (cleanup is None) == (mode is None)
        and (not released or positive and stop is not None and cleanup is not None)
        and value.get("network_gate_cleaned")
        is (
            released
            and positive
            and stop is not None
            and cleanup is not None
            and value["unit_reference_released"]
            and value["unit_reference_error_class"] is None
        )
    )


NETWORK_RUNTIME_PROPERTIES = "MainPID,InvocationID,WorkingDirectory,ExecStart"
MARKET_CHECKS = {
    "unauthenticated_api_denied",
    "quote_real_provider",
    "kline_real_provider",
    "quote_owned_capability_cache_readback",
    "kline_owned_capability_cache_readback",
    "quote_retained_stale_after_injected_failure",
    "kline_retained_stale_after_injected_failure",
    "no_cache_failure_explicit_503",
}
MARKET_BOOT_CHECKS = {
    "production_home_denied",
    "production_proc_denied",
    "candidate_write_denied",
    "run_readonly",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_ip_denied",
    "other_public_denied",
    "synthetic_empty_data",
    "staging_denied",
}
MARKET_UNIT_PROPERTIES = "MainPID,ExecMainPID,InvocationID,ActiveState,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE"
LIVE_INTEGRATION_HELPERS = {
    "live_integration_validation.py": "a5dbd65258475ca4107d2f3ce98b814057f231633622b5b1caaf663d9c76e2af",
    "live_integration_worker.py": "f7f21d576417d45195c8fcf91f25f181728dafb7f30acea1077d4c75f75a75a4",
    "live_integration_transport.py": "e92bf2120df61b9b14aeea1b1e147bf11218785f54ab50d84f7e12a33cc80e76",
    "live_integration_policy.py": "c6fa205466800636fe6fbe557eb8f625785567a129b405ebb4935b7b65e325d9",
    "cgroup_network_gate.py": "da13fbd0110493c4746126fa00fc45f7393ee77a773b5c27a642eaec124f7134",
    "systemd_unit_reference.py": "b160660e92a3cd98e11b37dc21855b9292150b137752c9300f0c234d5a78a4be",
}
LIVE_RESOLVE_CODE = 'import json,socket;print(json.dumps(sorted({row[4][0] for row in socket.getaddrinfo("api.deepseek.com",443,type=socket.SOCK_STREAM)})))'
LIVE_RESOLVE_COMMAND = ["/usr/bin/python3", "-I", "-B", "-S", "-c", LIVE_RESOLVE_CODE]
LIVE_STAGES = {
    "request_validated",
    "accounts",
    "workspace",
    "broker_start",
    "worker_start",
    "preflight",
    "allow",
    "key_release",
    "key_send",
    "public_qa",
    "result_validation",
    "broker_finish",
    "verification",
    "complete",
    "network_attach",
    "network_permit",
    "network_launch",
    "network_preflight_complete",
}
LIVE_BOUNDARIES = {"live_" + stage for stage in LIVE_STAGES} | {
    "live_wait_bootstrap",
    "live_wait_qa",
    "live_wait_broker",
}
LIVE_ERRORS = {
    "request_rejected",
    "root_linux_required",
    "source_rejected",
    "candidate_rejected",
    "account_rejected",
    "uid_busy",
    "workspace_exists",
    "workspace_rejected",
    "dns_rejected",
    "unit_rejected",
    "kernel_rejected",
    "private_run_rejected",
    "bootstrap_rejected",
    "ipc_rejected",
    "broker_preflight_rejected",
    "key_rejected",
    "worker_result_rejected",
    "worker_failed",
    "broker_failed",
    "cleanup_failed",
    "supervision_failed",
    "operator_stop",
    "network_gate_rejected",
}
LIVE_SOURCE_PINS = {
    name.removeprefix("live_integration_").removesuffix(".py"): value
    for name, value in LIVE_INTEGRATION_HELPERS.items()
    if name in {"live_integration_worker.py", "live_integration_transport.py", "live_integration_policy.py"}
}
RETIREMENT_HELPERS = {
    "retirement_probe.py": "e6e9181b0dcd33dda28bf4125781579555039f71af932e10641c2da3bedb4624",
    "retirement_validation.py": "8331a6713244e8be8983fd0e6639492435fcf3a11552890c45997d440d41586e",
}
RETIREMENT_STOP_HELPERS = {
    **RETIREMENT_HELPERS,
    "retirement_stop_validation.py": "854382bae5bfdcc7b75d1716bb2543271deefeac9ef288e196153ee80112b58b",
}
RETIREMENT_SYSTEM_PYTHON_SHA256 = "e1efa562c2cc2e35521a5c9c9b9939921001ff8ca9708a13ef15ace68cc2ccd7"
RETIREMENT_PRESTART_ATTEMPT = "prism-20261022-stop-e2e-001"
RETIREMENT_PRESTART_STOP_SHA256 = "f0dd83ad2f3bc78dbe9018c100661a5ec1cec6b2fa9bee08eef6ec91cb67ff87"
RETIREMENT_PRESTART_REVIEWED = {
    RETIREMENT_PRESTART_ATTEMPT: {"helper_sha256": RETIREMENT_PRESTART_STOP_SHA256},
    "prism-20261022-stop-e2e-002": {
        "helper_sha256": "3b074d93feea5d3d5c4ad5846427c06725498c65eb49471885104d1c1fdb41fa",
        "receipt_sha256": "4d40e1aa368bdbbb682a6fcd29de39d4fba125e7514901b7d731467cadc273a1",
        "events_sha256": "1e6f4629f625e5a812ea7d210995379c3052671564fe180e3fcec97efb567ea4",
        "identity_sha256": "604cc660df068813ec45cba9df6d244b5d41512e7c74800d3657fa28b107b016",
        "prestart_reconciliation_attempts": (RETIREMENT_PRESTART_ATTEMPT,),
    },
}
RETIREMENT_RETAINED_START = {
    "commit": "f0897e84cdc0fa31871eeee90ed2e2511cc72a13",
    "release_id": "prism-20261022-main-f0897e84",
    "attempt": "prism-20261022-stop-e2e-003",
    "nonce": "4aec30715bde22080e9bc04430644de4",
    "case_id": "int-executor",
    "invocation_id": "35f8c14c6c8540be8d54f1c72870c961",
    "exec_main_pid": 1587380,
    "helper_sha256": "d864de5273af26c045a496d10117d52932333b71d1a139f665611eaa69b5addc",
    "identity_sha256": "fe397a434884158deea7a2621e5274b4f8a2d5953d5f96b3b40d9985927996fb",
    "receipt_sha256": "068a9145c87d0331aaff39e5d400a18145857e38e8d0504072d453cf52873999",
    "events_sha256": "8d34615ca08b4d198b79adb90249d6d1a7ccab819dacabfc88d77f0e2a9cbd59",
    "argv_sha256": "06b7ea1aad40d13c89bb9c54793ef30f77c8d5aa4b5ecfa3b53af65fa16d78a6",
}
RETIREMENT_RETAINED_OBSERVER_START = {
    "commit": "f0897e84cdc0fa31871eeee90ed2e2511cc72a13",
    "release_id": "prism-20261022-main-f0897e84",
    "attempt": "prism-20261022-stop-e2e-004",
    "nonce": "0e17a0ff8cb154f535b748421034ffd3",
    "case_id": "int-executor",
    "invocation_id": "35314cc6a3a940d3a63bdc45b49fc515",
    "exec_main_pid": 2179861,
    "start_ticks": 1131196658,
    "uid": 63683,
    "helper_sha256": "5ef57f46e21327105d233987a85f7a70e9ea6efc7f90b22e075466062ecc7034",
    "identity_sha256": "21701aa76167aa8b4ee5430bb8aafb02e4d0302a0ac4f913833316061b515b4b",
    "receipt_sha256": "49fa3c3b2efaada39f9afcf534a69d00bf35c4ec0c70e53581ac66d88a123e6e",
    "events_sha256": "0bb34da9347e0017f3b0a7e9b6719b83fc4fa16e2dfa2a7848b8191278b3868b",
    "main_argv_sha256": "18b63f1317656b4d0b49e71f519a1d16abeb4bd8efc128cba78f84d1fa4d6fd1",
    "observer_argv_sha256": "cdffa3e18bfe6ae671972f377072654ed2022b6049c3894bc0bdab584985038f",
}
RETIREMENT_RETAINED_PROPERTIES = "LoadState,ActiveState,SubState,MainPID,ExecMainPID,ExecMainCode,ExecMainStatus,Result,InvocationID,ControlGroup,Job,Transient,ExecMainExitTimestampMonotonic"
RETIREMENT_BASE_ENV_SHA256 = "a395e2170c27c264e5c8f3503902732aec06a8905baf8653908abb6b45b5b072"
ASTOCK_ERROR_DETAILS = {
    "astock_import_contract_rejected",
    "bootstrap_identity_rejected",
    "bootstrap_isolation_rejected",
    "candidate_site_packages_required",
    "candidate_site_prefix_mismatch",
    "credential_environment_rejected",
    "dedicated_integration_user_required",
    "engine_output_path_rejected",
    "fixed_workdir_required",
    "import_error",
    "invalid_arguments",
    "invalid_identity",
    "isolated_candidate_interpreter_required",
    "module_not_found",
    "module_source_mismatch",
    "operating_system_error",
    "outside_database_rejected",
    "outside_network_rejected",
    "outside_write_rejected",
    "private_result_write_failed",
    "private_source_read_rejected",
    "private_workdir_required",
    "root_allow_rejected",
    "root_allow_unavailable",
    "source_revision_mismatch",
    "startup_hook_imported",
    "startup_hook_rejected",
    "subprocess_rejected",
    "symlink_path_rejected",
    "synthetic_certificate_failed",
    "synthetic_configuration_rejected",
    "synthetic_tls_cleanup_failed",
    "tls_verification_rejected",
    "trusted_probe_source_required",
    "unexpected_error",
    "unlocked_source_rejected",
    "untrusted_python_path",
    "workdir_reused",
}
RETIREMENT_CASE_IDS = {
    "int-executor",
    "int-post",
    "int-nested",
    "int-async",
    "int-daemon",
    "int-child",
    "term-control",
}
RETIREMENT_PROPERTIES = "LoadState,ActiveState,SubState,MainPID,ExecMainPID,InvocationID,ControlGroup,ExecMainCode,ExecMainStatus,ExecMainExitTimestampMonotonic,Result,Transient"
RETIREMENT_STOP_PROPERTIES = (
    RETIREMENT_PROPERTIES
    + ",KillSignal,KillMode,SendSIGKILL,SendSIGHUP,TimeoutStopUSec,Restart,WatchdogUSec,RuntimeMaxUSec,NRestarts,RemainAfterExit,Job"
)
RETIREMENT_OBSERVER_PROPERTIES = (
    RETIREMENT_PROPERTIES
    + ",Wants,PartOf,BindsTo,PropagatesStopTo,DynamicUser,RemainAfterExit,Job,PrivateTmp"
)
RETIREMENT_PYTHON_VERSION_COMMAND = [
    "/usr/bin/python3.12",
    "-I",
    "-S",
    "-B",
    "-c",
    "import sys; print('.'.join(map(str,sys.version_info[:3])))",
]
RETIREMENT_OBSERVER_INVENTORY = [
    "/usr/bin/systemctl",
    "list-units",
    "--all",
    "--no-pager",
    "--no-legend",
    "--plain",
    "prism-retirement-stop-ref-*.service",
]

INTEGRATION_BOOT_CHECKS = {
    "production_environment_denied",
    "production_process_environment_denied",
    "production_http_denied",
    "sidecar_http_denied",
    "candidate_write_denied",
    "staging_directory_denied",
}
INTEGRATION_RESULT_CHECKS = {
    "integration_astock": {
        "credential_free_synthetic_environment",
        "root_bootstrap_gate_accepted",
        "astock_import_and_fixed_sources",
        "default_tls_verification",
        "tls_urllib_rejected",
        "tls_requests_rejected",
        "tls_httpx_rejected",
    },
    "integration_cli": {
        "real_runner_new_session",
        "real_runner_resume",
        "real_auxiliary_no_tools",
        "synthetic_children_reaped",
        "cli_argv_and_environment",
        "only_three_cli_children",
        "paths_allowlisted",
        "request_body_bounded",
        "synthetic_auth_only",
        "synthetic_model_only",
        "request_budget_respected",
        "auxiliary_http_tools_absent",
        "requests_well_formed",
        "three_real_cli_invocations",
        "all_phases_reached_fixture",
    },
}

# The CLI fixture consumes a same-PID gate but never manufactures that gate.
# This reviewed stdlib bootstrap runs before site or candidate imports.
CLI_INTEGRATION_BOOTSTRAP = r"""
import errno
import hashlib
import json
import os
import re
import socket
import stat
import sys
import time
from pathlib import Path


def denied(path, write=False):
    flags = os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_WRONLY | os.O_CREAT | os.O_EXCL if write else os.O_RDONLY)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as error:
        return error.errno in ({errno.EACCES, errno.EPERM, errno.EROFS} if write else {errno.EACCES, errno.EPERM, errno.ENOENT})
    os.close(fd)
    if write:
        os.unlink(path)
    return False


def disconnected(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as channel:
        channel.settimeout(0.5)
        code = channel.connect_ex(("127.0.0.1", port))
    return code in {errno.EACCES, errno.EPERM, errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH}


def main():
    if len(sys.argv) != 9:
        raise ValueError("bootstrap_arguments_rejected")
    candidate, engine, cli, location, nonce, production, issued, expected = sys.argv[1:]
    if (not re.fullmatch("[0-9a-f]{40}", candidate) or not all(re.fullmatch("[0-9a-f]{64}", value) for value in (engine, cli, expected))
        or not re.fullmatch("[0-9a-f]{32}", nonce) or not re.fullmatch("[1-9][0-9]{0,9}", production)
        or int(production) <= 1 or not re.fullmatch("[0-9]{1,12}", issued)):
        raise ValueError("bootstrap_arguments_rejected")
    work = Path(location)
    if (work.parent.parent != Path("/var/lib/prism-operations/integration") or work.name != "cli-protocol"
        or not re.fullmatch("[a-z0-9][a-z0-9-]{7,63}", work.parent.name) or not 0 <= time.time() - int(issued) <= 120):
        raise ValueError("bootstrap_identity_rejected")
    proof = {"schema": 1, "candidate": candidate, "engine": engine, "nonce": nonce, "pid": os.getpid(),
        "production_pid": int(production), "issued_at": int(issued), "checked_at": int(time.time()), "probe_sha256": expected,
        "checks": {
            "production_environment_denied": denied("/home/ubuntu/stock-analyzer/.env"),
            "production_process_environment_denied": denied("/proc/" + production + "/environ"),
            "production_http_denied": disconnected(8088), "sidecar_http_denied": disconnected(8899),
            "candidate_write_denied": denied("/opt/prism/releases/" + candidate + "/.integration-canary-" + nonce, True),
            "staging_directory_denied": denied("/var/lib/prism-operations/staging"),
        }}
    raw = json.dumps(proof, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    fd = os.open(work / "output/bootstrap-proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    if not all(proof["checks"].values()):
        raise ValueError("bootstrap_isolation_rejected")
    allow_path = work / "root-allow.json"
    deadline = time.monotonic() + 20
    while not allow_path.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    fd = os.open(allow_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444 or info.st_size > 4096:
            raise ValueError("root_allow_rejected")
        allow = json.loads(stream.read(4097))
    values = {key: proof[key] for key in ("schema", "candidate", "engine", "nonce", "pid", "probe_sha256")}
    values["bootstrap_proof_sha256"] = hashlib.sha256(raw).hexdigest()
    if (type(allow) is not dict or set(allow) != {*values, "allowed_at"}
        or any(type(allow.get(key)) is not type(value) or allow[key] != value for key, value in values.items())
        or type(allow["allowed_at"]) is not int or not proof["checked_at"] <= allow["allowed_at"] <= time.time() <= int(issued) + 120):
        raise ValueError("root_allow_rejected")
    source = work / "probe.py"
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > 131072:
            raise ValueError("probe_source_rejected")
        content = stream.read(131073)
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("probe_source_rejected")
    # No subprocess: frozen helper checks this exact PID/proof before imports.
    sys.argv = [str(source), "--candidate", candidate, "--cli-sha256", cli, "--workdir", str(work), "--nonce", nonce, "--probe-sha256", expected]
    exec(compile(content, str(source), "exec"), {"__name__": "__main__", "__file__": str(source), "__package__": None})


main()
"""

# Passed verbatim to the isolated interpreter, never supplied by a candidate.
# -S keeps even site initialization behind the probes; site.main() subsequently
# enables the already verified venv. Probe output contains no file contents.
STAGE_BOOTSTRAP = r"""
import errno
import json
import os
import re
import socket
import sys
import time


def denied_open(path, writable=False):
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= (os.O_WRONLY | os.O_CREAT | os.O_EXCL) if writable else os.O_RDONLY
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        categories = {errno.EACCES: "permission_denied", errno.EPERM: "operation_denied"}
        if writable:
            categories[errno.EROFS] = "read_only_filesystem"
        else:
            categories[errno.ENOENT] = "not_visible"
        return {"blocked": exc.errno in categories, "errno": categories.get(exc.errno, "unexpected")}
    os.close(fd)  # Never read bytes, including in the unsafe-success case.
    if writable:
        os.unlink(path)  # Only the new O_EXCL canary, never an existing file.
    return {"blocked": False, "errno": "opened"}


def denied_connect(port):
    categories = {
        errno.ECONNREFUSED: "connection_refused",
        errno.ENETUNREACH: "network_unreachable",
        errno.EHOSTUNREACH: "host_unreachable",
        errno.EACCES: "permission_denied",
        errno.EPERM: "operation_denied",
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            code = connection.connect_ex(("127.0.0.1", port))
    except OSError as exc:
        code = exc.errno
    return {"blocked": code in categories, "errno": categories.get(code, "unexpected")}


def main(arguments):
    if len(arguments) != 6:
        raise RuntimeError("stage_probe_identity_rejected")
    commit, release_id, production_pid, nonce, issued_at, application = arguments
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", release_id)
        or not re.fullmatch(r"[1-9][0-9]{0,9}", production_pid)
        or int(production_pid) <= 1
        or not re.fullmatch(r"[0-9a-f]{32}", nonce)
        or not re.fullmatch(r"[0-9]{10}", issued_at)
        or not 0 <= time.time() - int(issued_at) <= 120
        or application not in {"server.main:app", "server.recovery:app"}
    ):
        raise RuntimeError("stage_probe_identity_rejected")
    release = "/opt/prism/releases/" + commit
    stage = "/var/lib/prism-operations/staging/" + release_id
    checks = {
        "production_environment": denied_open("/home/ubuntu/stock-analyzer/.env"),
        "production_process_environment": denied_open("/proc/" + production_pid + "/environ"),
        "production_http": denied_connect(8088),
        "sidecar_http": denied_connect(8899),
        "candidate_write": denied_open(release + "/.stage-write-probe-" + nonce, writable=True),
        "private_runtime_write": denied_open("/run/.prism-stage-write-probe-" + nonce, writable=True),
    }
    proof = {
        "schema": 1,
        "commit": commit,
        "release_id": release_id,
        "nonce": nonce,
        "production_pid": int(production_pid),
        "stage_pid": os.getpid(),
        "issued_at": int(issued_at),
        "checked_at": int(time.time()),
        "application": application,
        "checks": checks,
    }
    fd = os.open(stage + "/isolation-proof.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        json.dump(proof, output, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    if not all(result["blocked"] is True for result in checks.values()):
        raise RuntimeError("stage_isolation_probe_failed")
    # This is the first environment/candidate import point, in the probe PID.
    import site
    site.main()
    sys.path.insert(0, release)
    import uvicorn
    uvicorn.run(application, uds=stage + "/stage.sock", workers=1)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        # No raw path, provider exception or credential is sent to the journal.
        raise SystemExit("stage_bootstrap_failed") from None
"""


def admission_config(raw):
    """Only insert a fixed block at one direct server/location / declaration."""
    text = raw.decode("utf-8")
    tokens = list(re.finditer(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\#[^\n]*|[{};]|[^\s{};]+""", text))
    stack, header, matches = [], [], []
    for token in tokens:
        value = token[0]
        if value.startswith("#"):
            continue
        if value == "{":
            words = [item[0] for item in header]
            if words and words[0] == "location":
                if any(word in {"~", "~*"} for word in words[1:]):
                    raise ReleaseError("regex_location_requires_review")
                if any("/api" in word.lower() for word in words[1:]):
                    raise ReleaseError("existing_api_location")
                if words == ["location", "/"]:
                    if not stack or stack[-1] != "server":
                        raise ReleaseError("root_location_not_direct_server")
                    matches.append(header[0].start())
            stack.append(words[0] if words else "")
            header = []
        elif value == "}":
            if not stack or header:
                raise ReleaseError("nginx_structure_rejected")
            stack.pop()
        elif value == ";":
            header = []
        else:
            header.append(token)
    if stack or header or len(matches) != 1:
        raise ReleaseError("unique_root_location_required")
    position = matches[0]
    start = text.rfind("\n", 0, position) + 1
    indent = text[start:position]
    if indent.strip():
        raise ReleaseError("standalone_root_location_required")
    block = (
        "# Prism controlled admission hold\n"
        + indent
        + "location ^~ /api/ {\n"
        + indent
        + "    default_type application/json;\n"
        + indent
        + "    add_header Retry-After 60 always;\n"
        + indent
        + '    return 503 \'{"error":"'
        + HOLD_MESSAGE
        + "\"}';\n"
        + indent
        + "}\n"
        + indent
    )
    return (text[:position] + block + text[position:]).encode()


class ReleaseError(RuntimeError):
    """A safe category, never an upstream error body, command or credential."""


def failure_code(error):
    code = str(error) if isinstance(error, ReleaseError) else ""
    return code if re.fullmatch(r"[a-z0-9_]{1,96}", code) else "executor_failed"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def selected_model_key(raw):
    """Extract only the existing DeepSeek key; never evaluate shell or dotenv code."""
    if type(raw) is not bytes or len(raw) > 65536 or b"\0" in raw:
        raise ReleaseError("live_model_credential_unavailable")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise ReleaseError("live_model_credential_unavailable") from None
    selected = {}
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?(DEEPSEEK_API_KEY|LLM_API_KEY)\s*=(.*)$", line)
        if match:
            if match[1] in selected:
                raise ReleaseError("live_model_credential_unavailable")
            selected[match[1]] = match[2].strip()
    value = selected.get("DEEPSEEK_API_KEY", selected.get("LLM_API_KEY", ""))
    # This matches the candidate's provider-specific preference and legacy
    # fallback. Only literal token syntax is accepted; no substitutions, shell
    # escapes, multiline strings or unrelated provider values are interpreted.
    match = re.fullmatch(r"([A-Za-z0-9._-]{16,4096})(?:\s+#.*)?", value)
    if match:
        return match[1]
    match = re.fullmatch(r"(['\"])([A-Za-z0-9._-]{16,4096})\1(?:\s+#.*)?", value)
    if match:
        return match[2]
    raise ReleaseError("live_model_credential_unavailable")


def seal_executor_memory():
    """Disable dumps of this executor only, before it opens configuration."""
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ValueError
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise ValueError
        call = ctypes.CDLL(None, use_errno=True).prctl
        call.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        call.restype = ctypes.c_int
        if (
            call(4, 0, 0, 0, 0) != 0
            or call(3, 0, 0, 0, 0) != 0
            or resource.getrlimit(resource.RLIMIT_CORE) != (0, 0)
        ):
            raise ValueError
        return True
    except Exception:
        raise ReleaseError("live_executor_memory_unsealed") from None


def tree_digest(path, *, entries=None):
    """Hash package files and link text; external contents need a separate manifest."""
    inventory, size = [], 0
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        if item.is_symlink():
            inventory.append([relative, "link", os.readlink(item)])
        elif item.is_dir():
            inventory.append([relative, "directory", stat.S_IMODE(item.stat().st_mode)])
        elif item.is_file():
            hasher = hashlib.sha256()
            with item.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            size += item.stat().st_size
            inventory.append(
                [relative, item.stat().st_size, stat.S_IMODE(item.stat().st_mode), hasher.hexdigest()]
            )
    if entries is not None:
        entries.extend(inventory)
    return digest(encoded(inventory)), size


def safe_path(value):
    path = PurePosixPath(value)
    if (
        not value
        or not value.isascii()
        or "\\" in value
        or path.is_absolute()
        or any(p in {"", ".", ".."} for p in value.split("/"))
    ):
        raise ReleaseError("unsafe_path")
    if any(p.startswith(".") for p in path.parts):
        raise ReleaseError("hidden_path_forbidden")
    return value


def validate_archive(raw, expected, manifest_digest):
    """Independently verify all bytes/modes; extraction occurs only after this returns."""
    if len(raw) > MAX_ARCHIVE or digest(raw) != expected:
        raise ReleaseError("archive_digest_mismatch")
    files, total = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            safe_path(member.name)
            if (
                not member.isfile()
                or member.name in files
                or member.mode not in {0o644, 0o755}
                or member.size > 8 * 1024 * 1024
            ):
                raise ReleaseError("archive_member_rejected")
            total += member.size
            if total > MAX_ARCHIVE or len(files) > 4096:
                raise ReleaseError("archive_limits")
            content = archive.extractfile(member).read()
            if len(content) != member.size:
                raise ReleaseError("archive_truncated")
            files[member.name] = (content, member.mode)
    manifest_raw = files.pop("release-manifest.json", (b"", 0))[0]
    if digest(manifest_raw) != manifest_digest:
        raise ReleaseError("manifest_digest_mismatch")
    manifest = json.loads(manifest_raw)
    entries = manifest.get("files", [])
    if (
        manifest.get("kind") != "candidate"
        or manifest.get("deployed") is not False
        or not SHA.fullmatch(manifest.get("commit", ""))
    ):
        raise ReleaseError("manifest_identity")
    if len(entries) != len(files) or {item["path"] for item in entries} != set(files):
        raise ReleaseError("manifest_inventory")
    for item in entries:
        content, mode = files[item["path"]]
        if digest(content) != item["sha256"] or len(content) != item["size"] or f"{mode:04o}" != item["mode"]:
            raise ReleaseError("manifest_content_mismatch")
    return manifest, files, manifest_raw


def run_command(args, *, env=None, timeout=120):
    """Suppress package/systemd logs: they can contain paths or configuration."""
    capture = (
        args[:2] == ["/usr/bin/systemctl", "show"]
        or args
        in (
            RETIREMENT_PYTHON_VERSION_COMMAND,
            RETIREMENT_OBSERVER_INVENTORY,
            LIVE_RESOLVE_COMMAND,
        )
        or (
            len(args) == 6
            and args[:5] == ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend"]
            and re.fullmatch(r"prism-live-(?:worker|broker)-[0-9a-f]{12}-[0-9a-f]{32}\.service", args[5])
        )
    )
    capture = capture or (
        len(args) == 6
        and args[:5] == ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend"]
        and re.fullmatch(r"prism-market-probe-[0-9a-f]{12}-[0-9a-f]{32}\.service", args[5])
    )
    capture = capture or (
        len(args) == 8
        and args[:5]
        == ["/usr/bin/busctl", "--system", "--json=short", "get-property", "org.freedesktop.systemd1"]
        and args[6] == "org.freedesktop.systemd1.Service"
        and args[7] in {"ExecStop", "ExecStopPost"}
        and re.fullmatch(
            r"/org/freedesktop/systemd1/unit/prism_2d(?:retirement_2dprobe_2d[0-9a-f]{32}_2dint_2d(?:executor|post)|new_2druntime_2dstop_2d[0-9a-f]{32}_2d(?:candidate|recovery))_2eservice",
            args[5],
        )
    )
    capture = capture or (
        len(args) == 8
        and args[:5]
        == ["/usr/bin/busctl", "--system", "--json=short", "get-property", "org.freedesktop.systemd1"]
        and args[6:] == ["org.freedesktop.systemd1.Unit", "Job"]
        and re.fullmatch(
            r"/org/freedesktop/systemd1/unit/prism_2d(?:retirement_2d(?:probe|stop_2dref)_2d[0-9a-f]{32}_2dint_2d(?:executor|post)|new_2druntime_2dstop_2d[0-9a-f]{32}_2d(?:candidate|recovery)(?:_2dref)?)_2eservice",
            args[5],
        )
    )
    capture = capture or args == [
        "/usr/bin/busctl",
        "--system",
        "--json=short",
        "get-property",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1/unit/prism_2dretirement_2dprobe_2d"
        + RETIREMENT_RETAINED_START["nonce"]
        + "_2dint_2dexecutor_2eservice",
        "org.freedesktop.systemd1.Service",
        "ExecStart",
    ]
    retained_prefix = "/org/freedesktop/systemd1/unit/prism_2dretirement_2d"
    retained_nonce = RETIREMENT_RETAINED_OBSERVER_START["nonce"]
    capture = capture or (
        len(args) == 8
        and args[:5]
        == ["/usr/bin/busctl", "--system", "--json=short", "get-property", "org.freedesktop.systemd1"]
        and args[5]
        in {
            retained_prefix + role + retained_nonce + "_2dint_2dexecutor_2eservice"
            for role in ("probe_2d", "stop_2dref_2d")
        }
        and args[6] == "org.freedesktop.systemd1.Service"
        and args[7] in {"ExecStart", "ExecStop", "ExecStopPost"}
    )
    result = subprocess.run(
        args,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        check=False,
    )
    missing_stage = (
        capture
        and args[2:3] == ["--property=LoadState,ActiveState"]
        and len(args) == 4
        and re.fullmatch(
            r"prism-(?:stage-[0-9a-f]{12}|integration-[0-9a-f]{12}-(?:astock|cli)-[0-9a-f]{8}|diagnose-[0-9a-f]{12}-(?:namespace|check|dryrun|runonly|privaterun|runtimemasks|basicnamespace)-[0-9a-f]{8})\.service",
            args[-1],
        )
        and b"LoadState=not-found" in (result.stdout or b"").splitlines()
    )
    retirement_properties = None
    if args[:2] == ["/usr/bin/systemctl", "show"]:
        if len(args) == 4:
            patterns = (
                (
                    RETIREMENT_PROPERTIES,
                    r"prism-retirement-(?:probe-[0-9a-f]{32}-(?:int-executor|int-post|int-nested|int-async|int-daemon|int-child|term-control)|stop-ref-[0-9a-f]{32}-(?:int-executor|int-post))\.service",
                ),
                (
                    "LoadState",
                    r"prism-retirement-(?:probe|stop-ref)-[0-9a-f]{32}-(?:int-executor|int-post)\.service",
                ),
            )
            property_argument, unit_argument = args[2], args[3]
        elif len(args) == 5 and args[2] == "--all":
            patterns = (
                (
                    RETIREMENT_STOP_PROPERTIES,
                    r"prism-retirement-probe-[0-9a-f]{32}-(?:int-executor|int-post)\.service",
                ),
                (
                    RETIREMENT_OBSERVER_PROPERTIES,
                    r"prism-retirement-stop-ref-[0-9a-f]{32}-(?:int-executor|int-post)\.service",
                ),
            )
            property_argument, unit_argument = args[3], args[4]
        else:
            patterns, property_argument, unit_argument = (), "", ""
        retirement_properties = next(
            (
                properties
                for properties, pattern in patterns
                if property_argument == "--property=" + properties and re.fullmatch(pattern, unit_argument)
            ),
            None,
        )
    missing_retirement = (
        retirement_properties is not None
        and b"LoadState=not-found" in (result.stdout or b"").splitlines()
        and all(
            line.split(b"=", 1)[0].decode("ascii", "replace") in retirement_properties.split(",")
            for line in (result.stdout or b"").splitlines()
        )
    )
    missing_retained_tmp = (
        args
        == [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=Id,LoadState,ActiveState,SubState,Where",
            "tmp.mount",
        ]
        and b"LoadState=not-found" in (result.stdout or b"").splitlines()
        and all(
            line.split(b"=", 1)[0] in {b"Id", b"LoadState", b"ActiveState", b"SubState", b"Where"}
            for line in (result.stdout or b"").splitlines()
        )
    )
    if result.returncode and not (missing_stage or missing_retirement or missing_retained_tmp):
        raise ReleaseError("command_failed")
    if result.stdout and len(result.stdout) > 65536:
        raise ReleaseError("command_output_limit")
    return result.stdout or b""


def run_diagnostic(args, *, timeout=75):
    """Keep at most 256 KiB in memory; never persist or print package output."""
    process = subprocess.Popen(
        args,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output, truncated, timed_out = bytearray(), False, False
    deadline = time.monotonic() + timeout
    try:
        while True:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if not select.select([process.stdout], [], [], 0.1)[0]:
                continue
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                break
            remaining = 256 * 1024 - len(output)
            output.extend(block[:remaining])
            if len(block) > remaining:
                truncated = True
                break
        if truncated or timed_out:
            process.terminate()  # The caller also stops only this fixed unit.
        try:
            code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait(timeout=5)
        return {"return_code": code, "output": bytes(output), "truncated": truncated, "timed_out": timed_out}
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def safe_build_diagnostics(raw, *, release, packages):
    """Extract fixed categories and already inventoried package identifiers only."""
    categories, named, paths, system_paths, unknown = set(), set(), set(), set(), 0
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if len(line) > 4096:
            continue
        lower = line.lower()
        for token, code in (
            ("no broken requirements found", "dependencies_consistent"),
            ("which is not installed", "missing_dependency"),
            ("has requirement", "dependency_conflict"),
            ("conflicting dependencies", "resolver_conflict"),
            ("resolutionimpossible", "resolver_conflict"),
            ("no matching distribution found", "wheel_not_found"),
            ("could not find a version", "wheel_not_found"),
            ("permission denied", "permission_denied"),
            ("read-only file system", "read_only_filesystem"),
            ("do not match the hashes", "hash_mismatch"),
            ("ignoring invalid distribution", "invalid_distribution"),
            ("would install", "dry_run_ready"),
            ("226/namespace", "namespace_setup_failed"),
            ("status=226", "namespace_setup_failed"),
            ("mount namespacing", "namespace_setup_failed"),
            ("step namespace", "namespace_setup_failed"),
            ("network namespacing", "network_namespace_setup_failed"),
            ("operation not permitted", "operation_not_permitted"),
            ("no such file or directory", "missing_path"),
            ("invalid argument", "invalid_property_or_mount_argument"),
        ):
            if token in lower:
                categories.add(code)
        references = []
        if re.search(r"(?:has requirement|requires .+, which is not installed)", line):
            match = re.match(r"([A-Za-z][A-Za-z0-9_.-]{0,79})\s", line)
            if match:
                references.append(match[1])
        for pattern in (
            r"(?:has requirement|requires|requirement|distribution found for)\s+([A-Za-z][A-Za-z0-9_.-]{0,79})",
        ):
            references.extend(re.findall(pattern, line))
        for name in references:
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in packages:
                named.add(normalized)
            else:
                unknown += 1
        if "permission denied" in lower or "read-only file system" in lower:
            for value in re.findall(r"['\"]([^'\"\r\n]+)['\"]", line):
                prefix = str(release) + "/"
                if value.startswith(prefix):
                    relative = value[len(prefix) :]
                    parts = PurePosixPath(relative).parts
                    if (
                        parts
                        and all(part not in {".", ".."} for part in parts)
                        and len(relative) <= 240
                        and re.fullmatch(r"[A-Za-z0-9_./+\-]+", relative)
                    ):
                        paths.add(relative)
        if any(
            token in lower
            for token in ("namespac", "mount", "permission denied", "no such file", "invalid argument")
        ):
            for value in re.findall(r"/[A-Za-z0-9_./+\-]+", line):
                value = value.rstrip("./")
                if value in {
                    "/run",
                    "/run/user",
                    "/run/systemd/unit-root",
                    "/run/systemd/unit-root/run",
                    "/run/systemd/unit-root/run/user",
                    "/run/systemd/inaccessible/dir",
                    "/run/systemd/inaccessible/reg",
                    "/run/systemd/inaccessible/sock",
                }:
                    system_paths.add(value)
                elif value.startswith("/run/systemd/"):
                    system_paths.add("systemd_runtime_mount")
                elif value == "/var/lib/prism-operations/staging":
                    system_paths.add("staging_directory")
                elif value.startswith(str(release) + "/"):
                    relative = value[len(str(release)) + 1 :]
                    if len(relative) <= 240 and all(
                        part not in {".", ".."} for part in PurePosixPath(relative).parts
                    ):
                        paths.add(relative)
    return {
        "categories": sorted(categories) or ["unclassified_package_output"],
        "packages": [{"name": name, "installed_version": packages[name]} for name in sorted(named)[:16]],
        "candidate_relative_paths": sorted(paths)[:8],
        "unrecognized_package_references": unknown,
        "sandbox_system_paths": sorted(system_paths)[:8],
    }


class RemoteRelease:
    def __init__(
        self,
        root=Path("/"),
        runner=run_command,
        permit=None,
        control=None,
        now=time.time,
        diagnostic_runner=run_diagnostic,
    ):
        self.root, self.run, self.permit, self.control, self.now = Path(root), runner, permit, control, now
        self.production = self.root / "home/ubuntu/stock-analyzer"
        self.old_venv = self.root / "home/ubuntu/stock-venv"
        self.releases = self.root / "opt/prism/releases"
        self.operations = self.root / "var/lib/prism-operations"
        self.override = self.root / "etc/systemd/system/stock-app.service.d/90-prism-release.conf"
        self.record = None
        self.diagnostic_runner = diagnostic_runner

    def checked(self, path):
        path = Path(path)
        for ancestor in [path, *path.parents]:
            if ancestor == self.root.parent:
                break
            if ancestor.is_symlink():
                raise ReleaseError("symlink_path_rejected")
        return path

    def stop(self):
        if (self.control and self.control()) or self.checked(self.operations / "STOP").exists():
            raise ReleaseError("operator_stop")

    def boundary(self, label):
        self.stop()
        nonce = secrets.token_hex(16)
        if not self.permit or self.permit(nonce, label) != nonce:
            raise ReleaseError("fresh_operator_permit_required")
        self.stop()

    def space(self, path, needed):
        while not path.exists():
            path = path.parent
        if shutil.disk_usage(path).free < needed + RESERVE:
            raise ReleaseError("insufficient_disk_reserve")

    def public_directory(self, path):
        self.checked(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o755)
        if self.root == Path("/") and path.stat().st_uid != 0:
            raise ReleaseError("root_owned_directory_required")
        path.chmod(0o755)

    def write(self, path, content, mode=0o600):
        self.checked(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(path.name + ".pending")
        self.checked(temporary)
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        temporary.replace(path)

    def journal(self, stage, outcome, *, failure=None):
        previous = (
            json.loads((self.record / "status.json").read_bytes())
            if (self.record / "status.json").exists()
            else {}
        )
        cutover = (
            json.loads((self.record / "cutover.json").read_bytes())
            if (self.record / "cutover.json").exists()
            else {}
        )
        current_time = int(self.now())
        event = {
            "release_id": self.request["release_id"],
            "commit": self.request["commit"],
            "archive_sha256": self.request.get("archive_sha256"),
            "stage": stage,
            "outcome": outcome,
            "status": "running" if outcome == "started" else outcome,
            "at": current_time,
            "started_at": previous.get("started_at", current_time),
            "updated_at": current_time,
            "operator": "Prism release controller",
            "operator_issue": "https://github.com/doubleh-ai/prism/issues/9",
            "previous_commit": cutover.get("previous_runtime", {}).get("served_revision"),
            "recovery_commit": self.request.get("receipts", {}).get("recovery", {}).get("commit"),
            "last_completed_stage": stage if outcome == "completed" else previous.get("last_completed_stage"),
            "cutover_occurred": previous.get("cutover_occurred", False) or stage == "cutover_config_written",
            "steps": previous.get("steps", [])[-49:]
            + [{"stage": stage, "status": outcome, "at": current_time}],
            "service_verified": (self.record / "verified.json").exists()
            and not (self.record / "rollback.json").exists(),
            "production_verified": outcome == "completed"
            and stage == "resume"
            and (self.record / "verified.json").exists()
            and not (self.record / "rollback.json").exists(),
        }
        private_event = dict(event)
        if failure is not None:
            private_event["failure_code"] = failure_code(failure)
        path = self.checked(self.record / "journal.jsonl")
        with path.open("ab") as output:
            output.write(encoded(private_event) + b"\n")
            output.flush()
            os.fsync(output.fileno())
        path.chmod(0o600)
        self.write(self.record / "status.json", encoded(private_event))
        self.write(self.operations / "status.json", encoded(event), 0o644)
        return event

    def receipt(self, name):
        receipt = self.request.get("receipts", {})
        raw = base64.b64decode(self.request.get("receipts_raw", ""), validate=True)
        if digest(raw) != self.request.get("receipts_sha256") or json.loads(raw) != receipt:
            raise ReleaseError("receipt_digest_mismatch")
        if (
            receipt.get("commit") != self.request["commit"]
            or receipt.get("archive_sha256") != self.request["archive_sha256"]
        ):
            raise ReleaseError("receipt_not_bound")
        if not DIGEST.fullmatch(self.request.get("receipts_sha256", "")):
            raise ReleaseError("receipt_digest_required")
        item = receipt.get(name, {})
        if item.get("verified") is not True or not DIGEST.fullmatch(item.get("receipt_sha256", "")):
            raise ReleaseError("reviewed_receipt_required_" + name)
        if not 0 <= self.now() - item.get("checked_at", 0) <= (120 if name == "activity" else 86400):
            raise ReleaseError("receipt_expired_" + name)
        if name == "compatibility" and item.get("database_forward_and_backward") is not True:
            raise ReleaseError("database_compatibility_required")
        return item

    def runtime(self):
        cwd = (
            self.run(["/usr/bin/systemctl", "show", "--value", "--property=WorkingDirectory", SERVICE])
            .decode()
            .strip()
        )
        command = self.run(["/usr/bin/systemctl", "show", "--value", "--property=ExecStart", SERVICE])
        pid = (
            self.run(["/usr/bin/systemctl", "show", "--value", "--property=MainPID", SERVICE])
            .decode()
            .strip()
        )
        path = self.checked(Path(cwd))
        if path != self.production and not (path.parent == self.releases and SHA.fullmatch(path.name)):
            raise ReleaseError("unexpected_current_working_directory")
        if not pid.isdigit() or pid == "0" or Path(os.readlink(self.root / "proc" / pid / "cwd")) != path:
            raise ReleaseError("runtime_directory_not_reconciled")
        # Legacy v0.19.7 has no readiness route. The PID/source fingerprint remains
        # real; an unavailable served revision is explicitly unknown, not inferred.
        try:
            revision = self.ready().get("deployed_revision")
        except Exception:
            revision = None
        result = {"working_directory": cwd, "exec_start_sha256": digest(command), "served_revision": revision}
        result["fingerprint"] = digest(encoded(result))
        return result

    def ready(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ReleaseError("readiness_redirect_rejected")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open("http://127.0.0.1:8088/health/ready", timeout=10) as response:
            raw = response.read(65537)
            if len(raw) > 65536 or response.status != 200:
                raise ReleaseError("readiness_unavailable")
        return json.loads(raw)

    def source_inventory(self, supplied, source=None):
        """Hash declared files and independently detect added application source files."""
        expected = supplied.get("files", [])
        source = source or self.production
        names = {safe_path(item["path"]) for item in expected}
        if not names or len(names) != len(expected):
            raise ReleaseError("source_inventory_required")
        found = set()
        excluded = {"data", "logs", "reports", "uploads", "backups", "__pycache__", "node_modules", "venv"}
        for directory, folders, filenames in os.walk(source, followlinks=False):
            folders[:] = [name for name in folders if not name.startswith(".") and name not in excluded]
            for name in folders + filenames:
                path = Path(directory) / name
                if path.is_symlink() and not name.startswith("."):
                    raise ReleaseError("production_source_symlink")
            for name in filenames:
                path = Path(directory) / name
                if not name.startswith(".") and path.suffix in {
                    ".py",
                    ".js",
                    ".css",
                    ".html",
                    ".json",
                    ".svg",
                    ".md",
                    ".pth",
                    ".so",
                }:
                    found.add(path.relative_to(source).as_posix())
        if found - names:
            raise ReleaseError("new_production_source")
        for item in expected:
            path = self.checked(source / item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != item["size"]
                or digest(path.read_bytes()) != item["sha256"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("production_source_drift")
        return digest(encoded(supplied))

    def no_active_tasks(self):
        activity = self.receipt("activity")
        if activity.get("admission_paused") is not True:
            raise ReleaseError("reviewed_admission_pause_required")
        database = self.checked(self.production / "data/chat.db")
        if not database.is_file():
            raise ReleaseError("production_database_missing")
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ("analysis_runs", "indicator_scan_jobs", "capability_calls"):
                if (
                    table in tables
                    and db.execute(
                        f"SELECT 1 FROM {table} WHERE status IN ('queued','starting','running','cancelling') LIMIT 1"
                    ).fetchone()
                ):
                    raise ReleaseError("active_tasks")
            if "vibe_plans" in tables:
                for row in db.execute("SELECT plan_json FROM vibe_plans"):
                    if json.loads(row[0]).get("status") in {"queued", "running", "cancelling"}:
                        raise ReleaseError("active_tasks")
            if (
                "capability_runs" in tables
                and db.execute(
                    "SELECT 1 FROM capability_runs WHERE revoked=0 AND expires_at>? LIMIT 1",
                    [int(self.now())],
                ).fetchone()
            ):
                raise ReleaseError("active_tasks")
            if "research_runs" in tables:
                for row in db.execute("SELECT payload FROM research_runs"):
                    if json.loads(row[0]).get("status") not in {
                        "completed",
                        "partial",
                        "failed",
                        "cancelled",
                    }:
                        raise ReleaseError("active_tasks")

    def verify_tree(self):
        if not (self.record / "prepared.json").exists():
            raise ReleaseError("prepare_not_completed")
        if self.root == Path("/") and (
            self.release.stat().st_uid != 0 or self.release.stat().st_mode & 0o022
        ):
            raise ReleaseError("prepared_tree_writable_by_nonroot")
        manifest_raw = (self.record / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        identity = json.loads((self.record / "identity.json").read_bytes())
        prepared = json.loads((self.record / "prepared.json").read_bytes())
        if digest(manifest_raw) != identity.get("manifest_sha256") or any(
            item.get("commit") != self.request["commit"] for item in (manifest, identity, prepared)
        ):
            raise ReleaseError("prepared_manifest_not_bound")
        allowed = {item["path"] for item in manifest["files"]} | {
            ".deploy-revision",
            ".engine-revision",
            ".cli-revision",
        }
        for path in self.release.rglob("*"):
            relative = path.relative_to(self.release)
            if (
                self.root == Path("/")
                and not path.is_symlink()
                and (path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) & 0o022)
            ):
                raise ReleaseError("prepared_tree_writable_by_nonroot")
            if relative.parts[0] == ".venv":
                continue
            self.checked(path)
            if path.is_file() and relative.as_posix() not in allowed:
                raise ReleaseError("unexpected_prepared_file")
        if (self.release / ".deploy-revision").read_text().strip() != self.request["commit"]:
            raise ReleaseError("prepared_revision_mismatch")
        if (self.release / ".engine-revision").read_text().strip() != prepared.get("external_sources_sha256"):
            raise ReleaseError("prepared_engine_revision_mismatch")
        for item in manifest["files"]:
            path = self.checked(self.release / item["path"])
            if (
                not path.is_file()
                or digest(path.read_bytes()) != item["sha256"]
                or path.stat().st_size != item["size"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("prepared_source_drift")
        if tree_digest(self.release / ".venv")[0] != prepared.get("environment_sha256"):
            raise ReleaseError("prepared_environment_drift")
        self.verify_external(self.record)
        self.verify_cli(self.record, self.release)

    @staticmethod
    def cli_identity(info):
        return tuple(
            getattr(info, name)
            for name in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_gid",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        )

    def cli_target(self, expected):
        if not isinstance(expected, str) or not DIGEST.fullmatch(expected):
            raise ReleaseError("cli_binary_receipt_required")
        return self.checked(self.root / "opt/prism/tools/claude" / expected / "claude")

    def check_cli_binary(self, expected):
        target = self.cli_target(expected)
        info = target.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o755:
            raise ReleaseError("cli_binary_not_root_locked")
        if self.root == Path("/"):
            for path in (target, *target.parents):
                info = path.lstat()
                if info.st_uid != 0 or info.st_mode & 0o022:
                    raise ReleaseError("cli_binary_not_root_locked")
        with target.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ReleaseError("cli_binary_drift")
        return target

    def verify_cli(self, record, release):
        prepared = json.loads(self.checked(record / "prepared.json").read_bytes())
        expected = prepared.get("cli_binary_sha256")
        if self.checked(release / ".cli-revision").read_text().strip() != expected:
            raise ReleaseError("prepared_cli_revision_mismatch")
        return self.check_cli_binary(expected)

    def prepare_cli(self):
        expected = self.request["receipts"].get("dependencies", {}).get("cli_binary_sha256")
        target = self.cli_target(expected)
        entry, source = self.root / CLI_ENTRY, self.checked(self.root / CLI_SOURCE)
        self.checked(entry.parent)
        if not entry.is_symlink() or entry.resolve(strict=True) != source:
            raise ReleaseError("fixed_cli_source_required")
        entry_identity, link = self.cli_identity(entry.lstat()), os.readlink(entry)
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            original = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(original.st_mode)
                # The official installation can have multiple source hard
                # links. Read this fixed inode only as data; every identity
                # field (including link count) and its hash must remain stable.
                or original.st_nlink < 1
                or original.st_mode & 0o7000
                or not original.st_mode & 0o111
                or not 4 <= original.st_size <= MAX_CLI_BINARY
            ):
                raise ReleaseError("cli_source_not_regular_binary")
            identity = self.cli_identity(original)

            def source_unchanged():
                return (
                    self.cli_identity(os.fstat(stream.fileno())) == identity
                    and self.cli_identity(source.lstat()) == identity
                    and self.cli_identity(entry.lstat()) == entry_identity
                    and os.readlink(entry) == link
                    and entry.resolve(strict=True) == source
                )

            if stream.read(4) != b"\x7fELF":
                raise ReleaseError("cli_source_not_regular_binary")
            stream.seek(0)
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected or not source_unchanged():
                raise ReleaseError("cli_source_drift")
            self.space(target.parent, original.st_size)
            self.boundary("prepare_fixed_cli_binary")
            if not source_unchanged():
                raise ReleaseError("cli_source_drift")
            for path in reversed(target.parents):
                if path.is_relative_to(self.root / "opt/prism/tools"):
                    self.public_directory(path)
            if not target.exists():
                pending = self.checked(target.with_name("claude.pending"))
                created_pending = False
                try:
                    stream.seek(0)
                    copied = hashlib.sha256()
                    with pending.open("xb") as output:
                        created_pending = True
                        while block := stream.read(1024 * 1024):
                            self.stop()
                            output.write(block)
                            copied.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                    if copied.hexdigest() != expected or not source_unchanged():
                        raise ReleaseError("cli_source_drift")
                    stream.seek(0)
                    if (
                        hashlib.file_digest(stream, "sha256").hexdigest() != expected
                        or not source_unchanged()
                    ):
                        raise ReleaseError("cli_source_drift")
                    pending.chmod(0o755)
                    pending.replace(target)
                finally:
                    if created_pending and pending.exists():
                        pending.unlink()
            if not source_unchanged():
                raise ReleaseError("cli_source_drift")
        self.check_cli_binary(expected)
        return {"sha256": expected, "source_identity": identity, "entry_identity": entry_identity}

    def external_files(self, manifest):
        if manifest.get("source_root") != "/home/ubuntu/TradingAgents-astock" or not SHA.fullmatch(
            manifest.get("git_commit", "")
        ):
            raise ReleaseError("fixed_external_source_required")
        entries = manifest.get("files", [])
        paths = []
        forbidden = {"data", "cache", "logs", "log", "reports", "venv", "__pycache__", "node_modules"}
        for item in entries:
            path = safe_path(item.get("path", ""))
            if any(part in forbidden for part in PurePosixPath(path).parts) or Path(path).suffix in {
                ".pyc",
                ".db",
                ".sqlite",
                ".log",
                ".pem",
                ".key",
                ".env",
            }:
                raise ReleaseError("external_runtime_or_secret_forbidden")
            if (
                item.get("mode") not in {"0644", "0755"}
                or not DIGEST.fullmatch(item.get("sha256", ""))
                or not isinstance(item.get("size"), int)
                or not 0 <= item["size"] <= 8 * 1024 * 1024
            ):
                raise ReleaseError("invalid_external_manifest")
            paths.append(path)
        if (
            not entries
            or len(entries) > 4096
            or len(set(paths)) != len(paths)
            or sum(item["size"] for item in entries) > MAX_ARCHIVE
        ):
            raise ReleaseError("external_manifest_limits")
        return entries

    def external_target(self, manifest):
        return self.checked(self.root / "opt/prism/engines/astock" / digest(encoded(manifest)))

    def verify_external(self, record, expected=None):
        raw = self.checked(record / "external-source-manifest.json").read_bytes()
        manifest = json.loads(raw)
        if expected is None:
            expected = json.loads((record / "prepared.json").read_bytes()).get("external_sources_sha256")
        if digest(encoded(manifest)) != expected:
            raise ReleaseError("prepared_external_manifest_changed")
        entries = self.external_files(manifest)
        target = self.external_target(manifest)
        if (
            not target.is_dir()
            or self.root == Path("/")
            and (target.stat().st_uid != 0 or target.stat().st_mode & 0o022)
        ):
            raise ReleaseError("external_tree_not_root_locked")
        expected = {item["path"] for item in entries}
        found = set()
        for path in target.rglob("*"):
            self.checked(path)
            if self.root == Path("/") and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
                raise ReleaseError("external_tree_not_root_locked")
            if path.is_file():
                found.add(path.relative_to(target).as_posix())
        if found != expected:
            raise ReleaseError("external_inventory_changed")
        for item in entries:
            path = target / item["path"]
            if (
                path.stat().st_size != item["size"]
                or digest(path.read_bytes()) != item["sha256"]
                or f"{stat.S_IMODE(path.stat().st_mode):04o}" != item["mode"]
            ):
                raise ReleaseError("external_source_drift")
        return target

    def prepare_external(self):
        manifest = self.request.get("external_source_manifest", {})
        expected = self.request["receipts"]["dependencies"].get("external_sources_sha256")
        if digest(encoded(manifest)) != expected:
            raise ReleaseError("external_source_receipt_required")
        entries = self.external_files(manifest)
        source = self.checked(self.root / "home/ubuntu/TradingAgents-astock")
        names = {item["path"] for item in entries}
        excluded = {"data", "cache", "logs", "reports", "venv", "__pycache__", "node_modules"}
        for directory, folders, filenames in os.walk(source, followlinks=False):
            ignored = excluded | ({"tests", "examples", "scripts"} if Path(directory) == source else set())
            folders[:] = [name for name in folders if not name.startswith(".") and name not in ignored]
            for name in folders + filenames:
                path = Path(directory) / name
                if path.is_symlink() and not name.startswith("."):
                    raise ReleaseError("external_source_symlink")
                if path.suffix == ".py" and path.relative_to(source).as_posix() not in names:
                    raise ReleaseError("unregistered_external_python")
        contents = {}
        for item in entries:
            path = self.checked(source / item["path"])
            if (
                not path.is_file()
                or path.stat().st_size != item["size"]
                or path.stat().st_mode & 0o7000
                or bool(path.stat().st_mode & 0o111) != (item["mode"] == "0755")
            ):
                raise ReleaseError("external_source_drift")
            content = path.read_bytes()
            if digest(content) != item["sha256"]:
                raise ReleaseError("external_source_drift")
            contents[item["path"]] = content
        target = self.external_target(manifest)
        self.space(target, sum(item["size"] for item in entries))
        self.boundary("prepare_fixed_external_source")
        self.public_directory(target.parent.parent)
        self.public_directory(target.parent)
        if not target.exists():
            target.mkdir(mode=0o755)
            target.chmod(0o755)
            for item in entries:
                path = target / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                for parent in path.parents:
                    if parent == target.parent:
                        break
                    parent.chmod(0o755)
                self.write(path, contents[item["path"]], int(item["mode"], 8))
        self.write(self.record / "external-source-manifest.json", encoded(manifest))
        self.verify_external(self.record, expected)
        return expected

    def prepare(self):
        backup = self.receipt("backup")
        if backup.get("recovery_verified") is not True:
            raise ReleaseError("recovery_receipt_required")
        self.receipt("compatibility")
        raw = base64.b64decode(self.request["archive"], validate=True)
        manifest, files, manifest_raw = validate_archive(
            raw, self.request["archive_sha256"], self.request["manifest_sha256"]
        )
        if manifest["commit"] != self.request["commit"] or self.release.exists():
            raise ReleaseError("release_identity_or_existing_path")
        dependencies = self.request.get("dependencies", [])
        dependency_size = sum(len(item.get("content", "")) * 3 // 4 for item in dependencies)
        if dependency_size > MAX_DEPENDENCIES:
            raise ReleaseError("dependency_limits")
        inherited_entries = []
        inherited, inherited_size = tree_digest(self.old_venv, entries=inherited_entries)
        if inherited != self.request["receipts"].get("dependencies", {}).get("base_environment_sha256"):
            raise ReleaseError("base_environment_changed")
        self.space(self.releases, inherited_size * 2 + dependency_size * 3 + MAX_ARCHIVE)
        self.space(self.operations, dependency_size)
        self.boundary("prepare_candidate")
        external_digest = self.prepare_external()
        cli = self.prepare_cli()
        self.public_directory(self.releases.parent)
        self.public_directory(self.releases)
        self.release.mkdir(parents=True, mode=0o755)
        self.release.chmod(0o755)
        for name, (content, mode) in files.items():
            path = self.release / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            for parent in path.parents:
                if parent == self.release.parent:
                    break
                parent.chmod(0o755)
            self.write(path, content, mode)
        self.write(self.release / ".deploy-revision", (manifest["commit"] + "\n").encode(), 0o644)
        self.write(self.release / ".engine-revision", (external_digest + "\n").encode(), 0o644)
        self.write(self.release / ".cli-revision", (cli["sha256"] + "\n").encode(), 0o644)
        self.write(self.record / "manifest.json", manifest_raw)
        inputs = self.record / "dependencies"
        inputs.mkdir(mode=0o700)
        for item in dependencies:
            name = item.get("name", "")
            if name != "core.lock" and not re.fullmatch(r"[A-Za-z0-9_.+\-]+\.whl", name):
                raise ReleaseError("dependency_filename_rejected")
            content = base64.b64decode(item["content"], validate=True)
            if digest(content) != item["sha256"] or (inputs / name).exists():
                raise ReleaseError("dependency_digest_mismatch")
            self.write(inputs / name, content)
        if not (inputs / "core.lock").is_file():
            raise ReleaseError("dependency_lock_required")
        lock_digest = digest((inputs / "core.lock").read_bytes())
        lock_entry = next((item for item in manifest["files"] if item["path"] == "requirements.lock"), {})
        if lock_digest != lock_entry.get("sha256") or lock_digest != self.request["receipts"][
            "dependencies"
        ].get("lock_sha256"):
            raise ReleaseError("candidate_dependency_lock_mismatch")
        self.boundary("prepare_environment")
        self.stage_account()
        env = {
            "PATH": "/usr/bin:/bin",
            "PIP_CONFIG_FILE": "/dev/null",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        target = self.release / ".venv"
        self.run(["/usr/bin/python3.12", "-I", "-B", "-S", "-m", "venv", "--copies", str(target)], env=env)
        sources = list((self.old_venv / "lib").glob("python*/site-packages"))
        destinations = list((target / "lib").glob("python*/site-packages"))
        if (
            len(sources) != 1
            or len(destinations) != 1
            or sources[0].parent.name != destinations[0].parent.name
        ):
            raise ReleaseError("python_environment_mismatch")
        for path in sources[0].rglob("*"):
            if path.is_symlink() and (
                not path.resolve(strict=True).is_relative_to(sources[0]) or path.is_dir()
            ):
                raise ReleaseError("package_symlink_requires_review")
        inventory = {item[0]: item for item in inherited_entries}

        def copy_verified(source, destination):
            source = Path(source)
            expected = inventory.get(source.relative_to(self.old_venv).as_posix())
            if source.is_symlink():
                if expected is None or expected[1:] != ["link", os.readlink(source)]:
                    raise ReleaseError("inherited_package_drift")
                source = source.resolve(strict=True)
                if not source.is_relative_to(sources[0]):
                    raise ReleaseError("package_symlink_requires_review")
                expected = inventory.get(source.relative_to(self.old_venv).as_posix())
            if expected is None or len(expected) != 4 or not isinstance(expected[1], int):
                raise ReleaseError("inherited_package_drift")
            hasher = hashlib.sha256()
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as original, Path(destination).open("wb") as copied:
                info = os.fstat(original.fileno())
                if not stat.S_ISREG(info.st_mode) or (info.st_size, stat.S_IMODE(info.st_mode)) != tuple(
                    expected[1:3]
                ):
                    raise ReleaseError("inherited_package_drift")
                for block in iter(lambda: original.read(1024 * 1024), b""):
                    hasher.update(block)
                    copied.write(block)
            if hasher.hexdigest() != expected[3]:
                raise ReleaseError("inherited_package_drift")
            Path(destination).chmod(expected[2])
            return str(destination)

        # This explicit projection removes the reviewed unused UI distribution
        # and the editable installation replaced by the fixed engine manifest.
        # Exact observed root names avoid pruning unrelated/transitive packages.
        projected_out = {
            "streamlit": "unused_ui",
            "streamlit-1.58.0.dist-info": "unused_ui_metadata",
            "tradingagents_astock-0.2.16.dist-info": "replaced_editable_metadata",
            "~radingagents_astock-0.2.15.dist-info": "retired_editable_metadata",
            "__editable__.tradingagents_astock-0.2.16.pth": "replaced_editable_hook",
            "__editable___tradingagents_astock_0_2_16_finder.py": "replaced_editable_finder",
            "~ttpx-0.25.2.dist-info": "retired_httpx_metadata",
        }
        excluded_roots = []
        startup_patterns = (
            "*.pth",
            "__pycache__",
            "*.pyc",
            "*.pyo",
            "sitecustomize*",
            "usercustomize*",
            "patch_ssl*",
        )
        ignore_startup = shutil.ignore_patterns(*startup_patterns)

        def ignore_inherited(directory, names):
            ignored = set(ignore_startup(directory, names))
            if Path(directory) == sources[0]:
                for name in sorted(set(names) & projected_out.keys()):
                    ignored.add(name)
                    excluded_roots.append({"name": name, "reason": projected_out[name]})
            return ignored

        # Internal regular-file links are materialized; no link back to writable
        # old packages, TLS bypass hook, or legacy editable startup hook survives.
        shutil.copytree(
            sources[0],
            destinations[0],
            dirs_exist_ok=True,
            symlinks=False,
            copy_function=copy_verified,
            ignore=ignore_inherited,
        )
        if tree_digest(self.old_venv)[0] != inherited:
            raise ReleaseError("base_environment_changed_during_copy")
        if self.request["receipts"]["dependencies"].get("allowed_pth"):
            raise ReleaseError("legacy_editable_hooks_rejected_use_fixed_engine_path")
        build_inputs = self.checked(self.operations / "build-inputs" / self.request["release_id"])
        self.public_directory(build_inputs.parent)
        build_inputs.mkdir(mode=0o750)
        build_inputs.chmod(0o750)
        self.run(["/usr/bin/chown", "root:prism-stage", str(build_inputs)])
        for item in inputs.iterdir():
            self.write(build_inputs / item.name, item.read_bytes(), 0o640)
            self.run(["/usr/bin/chown", "root:prism-stage", str(build_inputs / item.name)])
        self.run(["/usr/bin/chown", "-hR", "prism-stage:prism-stage", str(target)])
        self.boundary("offline_dependency_install")
        self.build_command(
            target,
            "install",
            [
                "install",
                "--no-cache-dir",
                "--no-index",
                "--find-links",
                str(build_inputs),
                "--require-hashes",
                "--only-binary=:all:",
                "--upgrade",
                "-r",
                str(build_inputs / "core.lock"),
            ],
        )
        self.stop()
        self.build_command(target, "check", ["check"])
        self.run(["/usr/bin/chown", "-hR", "root:root", str(target)])
        self.stop()
        target.chmod(0o755)
        for path in target.rglob("*"):
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        self.write(
            self.record / "prepared.json",
            encoded(
                {
                    "commit": manifest["commit"],
                    "receipt": self.request["receipts_sha256"],
                    "environment_sha256": tree_digest(target)[0],
                    "inherited_environment_sha256": inherited,
                    "external_sources_sha256": external_digest,
                    "cli_binary_sha256": cli["sha256"],
                    "cli_source_identity": cli["source_identity"],
                    "cli_entry_identity": cli["entry_identity"],
                    "inherited_package_projection": {
                        "policy": "reviewed_legacy_ui_and_editable_v1",
                        "excluded_roots": excluded_roots,
                        "excluded_startup_and_bytecode_patterns": list(startup_patterns),
                        "replacement_engine_manifest_sha256": external_digest,
                        "source_inventory": "entire old environment before and after copy",
                    },
                }
            ),
        )
        self.verify_external(self.record)
        self.verify_cli(self.record, self.release)

    def kernel_uids(self, process):
        try:
            # Non-dumpable processes can expose a root-owned /proc/PID.
            # Only the kernel's real/effective/saved/fs UIDs establish UID.
            with (process / "status").open("rb") as source:
                values = None
                for _ in range(64):
                    line = source.readline(4097)
                    if not line or len(line) > 4096:
                        break
                    if line.startswith(b"Uid:"):
                        match = re.fullmatch(
                            rb"Uid:[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]+([0-9]{1,10})[\t ]*\n?",
                            line,
                        )
                        if match:
                            values = tuple(int(value) for value in match.groups())
                        break
            if values is None or any(value > 4294967295 for value in values):
                raise ReleaseError("process_status_rejected")
            return values
        except FileNotFoundError:
            try:
                process.stat()  # Only disappearance, never directory ownership.
            except FileNotFoundError:
                return None
            except OSError:
                raise ReleaseError("process_inspection_failed") from None
            raise ReleaseError("process_status_rejected") from None
        except OSError:
            raise ReleaseError("process_inspection_failed") from None

    def no_stage_processes(self):
        uid = pwd.getpwnam("prism-stage").pw_uid
        for process in (self.root / "proc").iterdir():
            if not process.name.isdigit():
                continue
            try:
                values = self.kernel_uids(process)
            except ReleaseError as error:
                raise ReleaseError("failed_prepare_" + failure_code(error)) from None
            if values is not None and uid in values:
                raise ReleaseError("failed_prepare_stage_process_active")

    def stage_cgroup_members(self, group):
        root = self.checked(self.root / "sys/fs/cgroup")
        if not (root / "cgroup.controllers").is_file():
            raise ReleaseError("stage_cgroup_v2_required")
        directory = self.checked(root / group.lstrip("/"))
        if not directory.exists():
            return set()
        paths = [directory / "cgroup.procs", *directory.glob("**/*/cgroup.procs")]
        if len(paths) > 64:
            raise ReleaseError("stage_cgroup_limit")
        members = set()
        for path in paths:
            try:
                with self.checked(path).open("rb") as source:
                    raw = source.read(8193)
            except FileNotFoundError:
                if not path.parent.exists():
                    continue
                raise ReleaseError("stage_cgroup_unavailable") from None
            except OSError:
                raise ReleaseError("stage_cgroup_unavailable") from None
            if len(raw) > 8192 or any(
                not re.fullmatch(rb"[1-9][0-9]{0,9}", line) or int(line) <= 1 for line in raw.splitlines()
            ):
                raise ReleaseError("stage_cgroup_rejected")
            members.update(int(line) for line in raw.splitlines())
        return members

    def finish_stage(self):
        self.verify_tree()
        staged = json.loads(self.checked(self.record / "staged.json").read_bytes())
        isolation = json.loads(self.checked(self.record / "stage-isolation.json").read_bytes())
        journeys = json.loads(self.checked(self.record / "stage-journeys.json").read_bytes())
        proof, identity = isolation.get("proof", {}), isolation.get("identity", {})
        if (
            staged.get("read_only_private_runtime_mount") is not True
            or identity.get("read_only_private_runtime_mount") is not True
            or proof.get("commit") != self.request["commit"]
            or proof.get("stage_pid") != identity.get("stage_pid")
            or staged.get("isolation_proof_sha256") != digest(encoded(proof))
            or staged.get("journeys_sha256") != digest(encoded(journeys))
            or journeys.get("commit") != self.request["commit"]
            or journeys.get("served_commit") != self.request["commit"]
            or journeys.get("passed") is not True
        ):
            raise ReleaseError("accepted_stage_cleanup_required")
        unit = "prism-stage-" + self.request["commit"][:12] + ".service"
        group = "/system.slice/" + unit

        def reconcile():
            raw = self.run(["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState", unit]).decode()
            state = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
            members = self.stage_cgroup_members(group)
            if state.get("LoadState") == "not-found":
                if members:
                    raise ReleaseError("stage_cgroup_state_unknown")
                return False, members
            if state.get("ActiveState") not in {
                "active",
                "activating",
                "deactivating",
                "reloading",
                "inactive",
                "failed",
            }:
                raise ReleaseError("stage_cgroup_state_unknown")
            current = (
                self.run(["/usr/bin/systemctl", "show", "--value", "--property=ControlGroup", unit])
                .decode()
                .strip()
            )
            pid = self.run(["/usr/bin/systemctl", "show", "--value", "--property=MainPID", unit]).strip()
            if current not in {"", group} or not re.fullmatch(rb"[0-9]{1,10}", pid):
                raise ReleaseError("stage_cgroup_identity_mismatch")
            if int(pid):
                if int(pid) != identity.get("stage_pid") or int(pid) not in members:
                    raise ReleaseError("stage_cgroup_identity_mismatch")
                process = self.root / "proc" / pid.decode()
                if Path(os.readlink(process / "cwd")) != self.release:
                    raise ReleaseError("stage_cgroup_identity_mismatch")
            elif members:
                raise ReleaseError("stage_cgroup_state_unknown")
            uid = pwd.getpwnam("prism-stage").pw_uid
            for member in members:
                process = self.root / "proc" / str(member)
                try:
                    values = self.kernel_uids(process)
                    with (process / "cgroup").open("rb") as source:
                        membership = source.read(8193)
                except OSError:
                    raise ReleaseError("stage_cgroup_identity_unavailable") from None
                if (
                    values != (uid,) * 4
                    or len(membership) > 8192
                    or not any(
                        line == b"0::" + group.encode() or line.startswith(b"0::" + group.encode() + b"/")
                        for line in membership.splitlines()
                    )
                ):
                    raise ReleaseError("stage_cgroup_identity_mismatch")
            return True, members

        present, members = reconcile()
        before = len(members)
        if present:
            self.boundary("finish_accepted_stage_unit")
            reconcile()  # A fresh permit cannot authorize a changed unit/cgroup.
            self.run(["/usr/bin/systemctl", "stop", unit])
        deadline = time.monotonic() + 5
        while self.stage_cgroup_members(group):
            if time.monotonic() > deadline:
                raise ReleaseError("stage_cgroup_not_empty")
            time.sleep(0.1)
        result = {
            "schema": 1,
            "commit": self.request["commit"],
            "checked_at": int(self.now()),
            "unit_stopped": True,
            "cgroup_empty": True,
            "members_before": before,
            "other_stage_units_changed": False,
            "production_changed": False,
            "stage_files_preserved": True,
        }
        destination = self.checked(self.record / "finished-stage.json")
        if not destination.exists():
            self.write(destination, encoded(result))
        self.action_report = result

    def failed_prepare_guard(self, *, candidate_required=True):
        forbidden = (
            "prepared.json",
            "staged.json",
            "previous.json",
            "cutover.json",
            "switched.json",
            "recovery-intent.json",
            "rollback.json",
            "verified.json",
            "admission.json",
        )
        if any(self.checked(self.record / name).exists() for name in forbidden):
            raise ReleaseError("failed_prepare_only")
        raw = self.checked(self.record / "journal.jsonl").read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ReleaseError("failed_prepare_journal_limit")
        history = [json.loads(line) for line in raw.splitlines() if line]
        prepares = [
            row
            for row in history
            if row.get("stage") == "prepare" and row.get("outcome") in {"failed", "completed", "cancelled"}
        ]
        if (
            not prepares
            or prepares[-1].get("outcome") != "failed"
            or any(row.get("outcome") == "completed" for row in prepares)
        ):
            raise ReleaseError("failed_prepare_record_required")
        manifest_raw = self.checked(self.record / "manifest.json").read_bytes()
        manifest = json.loads(manifest_raw)
        if (
            digest(manifest_raw) != self.request["manifest_sha256"]
            or manifest.get("commit") != self.request["commit"]
            or str(manifest.get("version", "")).endswith("-recovery")
            or self.request.get("receipts", {}).get("recovery", {}).get("commit") == self.request["commit"]
        ):
            raise ReleaseError("failed_prepare_identity_rejected")
        if self.runtime()["working_directory"] != str(self.production):
            raise ReleaseError("failed_prepare_original_runtime_required")
        self.no_stage_processes()
        if candidate_required:
            if not self.release.is_dir():
                raise ReleaseError("failed_prepare_candidate_required")
            if self.root == Path("/") and (
                self.release.stat().st_uid != 0 or self.release.stat().st_mode & 0o022
            ):
                raise ReleaseError("failed_prepare_source_drift")
            for item in manifest["files"]:
                path = self.checked(self.release / safe_path(item["path"]))
                info = path.stat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_size != item["size"]
                    or digest(path.read_bytes()) != item["sha256"]
                    or f"{stat.S_IMODE(info.st_mode):04o}" != item["mode"]
                    or self.root == Path("/")
                    and (info.st_uid != 0 or info.st_mode & 0o022)
                ):
                    raise ReleaseError("failed_prepare_source_drift")
            if self.checked(self.release / ".deploy-revision").read_text().strip() != self.request["commit"]:
                raise ReleaseError("failed_prepare_identity_rejected")
            dependencies = self.request.get("receipts", {}).get("dependencies", {})
            external = dependencies.get("external_sources_sha256")
            self.verify_external(self.record, external)
            self.check_cli_binary(dependencies.get("cli_binary_sha256"))
            if self.checked(
                self.release / ".engine-revision"
            ).read_text().strip() != external or self.checked(
                self.release / ".cli-revision"
            ).read_text().strip() != dependencies.get("cli_binary_sha256"):
                raise ReleaseError("failed_prepare_identity_rejected")
        return manifest

    def diagnostic_packages(self, lock, target):
        packages = {"pip": "unknown", "setuptools": "unknown", "wheel": "unknown"}
        for name in re.findall(r"^([A-Za-z][A-Za-z0-9_.-]{0,79})==", lock.decode("ascii"), re.M):
            packages[re.sub(r"[-_.]+", "-", name).lower()] = "not_installed"
        for metadata in target.glob("lib/python3.12/site-packages/*.dist-info/METADATA"):
            self.checked(metadata)
            if not metadata.is_file() or metadata.stat().st_size > 1024 * 1024:
                raise ReleaseError("diagnostic_metadata_rejected")
            with metadata.open("rb") as source:
                header = source.read(65536).split(b"\n\n", 1)[0].decode("utf-8", errors="replace")
            name = re.search(r"^Name: ([A-Za-z][A-Za-z0-9_.-]{0,79})$", header, re.M)
            version = re.search(r"^Version: ([A-Za-z0-9.!+_\-]{1,64})$", header, re.M)
            if name and version:
                packages[re.sub(r"[-_.]+", "-", name[1]).lower()] = version[1]
                for requirement in re.findall(
                    r"^Requires-Dist: ([A-Za-z][A-Za-z0-9_.-]{0,79})(?:\W|$)", header, re.M
                ):
                    packages.setdefault(re.sub(r"[-_.]+", "-", requirement).lower(), "not_installed")
        return packages

    def stop_diagnostic_unit(self, unit):
        raw = self.run(["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState", unit]).decode()
        values = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
        if values.get("LoadState") == "not-found":
            self.no_stage_processes()
            return
        if values.get("ActiveState") not in {
            "active",
            "activating",
            "deactivating",
            "reloading",
            "inactive",
            "failed",
        }:
            raise ReleaseError("diagnostic_unit_state_unknown")
        self.run(["/usr/bin/systemctl", "stop", unit])
        self.no_stage_processes()

    def observe_diagnostic(self, command, unit, packages):
        try:
            observed = self.diagnostic_runner(command)
            if observed["truncated"] or observed["timed_out"]:
                self.stop_diagnostic_unit(unit)
            summary = safe_build_diagnostics(observed["output"], release=self.release, packages=packages)
            summary.update({key: observed[key] for key in ("return_code", "truncated", "timed_out")})
            journal = self.diagnostic_runner(
                [
                    "/usr/bin/journalctl",
                    "--no-pager",
                    "-o",
                    "cat",
                    "-n",
                    "40",
                    "--since",
                    "@" + str(int(self.now()) - 120),
                    "-u",
                    unit,
                ],
                timeout=10,
            )
            summary["unit_journal"] = safe_build_diagnostics(
                journal["output"], release=self.release, packages=packages
            )
            summary["unit_journal"].update(
                {key: journal[key] for key in ("return_code", "truncated", "timed_out")}
            )
            return summary
        finally:
            self.stop_diagnostic_unit(unit)

    def diagnose_namespace_variants(self, target, packages, report, report_path):
        """Fixed /usr/bin/true matrix only; no candidate/Python acceptance."""
        variants = (
            ("without_optional_staging", "runonly", ["--property=InaccessiblePaths=/run"]),
            (
                "private_run_tmpfs",
                "privaterun",
                [
                    "--property=TemporaryFileSystem=/run:ro",
                    "--property=InaccessiblePaths=-" + str(self.operations / "staging"),
                ],
            ),
            (
                "masked_runtime_endpoints",
                "runtimemasks",
                ["--property=InaccessiblePaths=-/run/dbus -/run/systemd"],
            ),
            ("basic_namespace", "basicnamespace", []),
        )
        result = {}
        for name, phase, properties in variants:
            self.boundary("diagnose_build_" + name)
            self.no_stage_processes()
            nonce = secrets.token_hex(4)
            unit = "prism-diagnose-" + self.request["commit"][:12] + "-" + phase + "-" + nonce + ".service"
            command = [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=ReadWritePaths=" + str(target),
                *properties,
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=60",
                "--property=KillMode=control-group",
                "--property=UMask=0077",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=/nonexistent",
                "PIP_CONFIG_FILE=/dev/null",
                "PIP_NO_INPUT=1",
                "PYTHONDONTWRITEBYTECODE=1",
                "/usr/bin/true",
            ]
            summary = self.observe_diagnostic(command, unit, packages)
            summary.update(
                attempt=nonce, diagnostic_only=True, isolation_verified=False, prepare_verified=False
            )
            result[name] = summary
            report["namespace_variants"] = result
            self.write(report_path, encoded(report))
        return result

    def diagnose_build(self):
        self.failed_prepare_guard()
        target = self.checked(self.release / ".venv")
        binary = self.checked(target / "bin/python")
        system = self.checked(self.root / "usr/bin/python3.12")
        if not binary.is_file() or digest(binary.read_bytes()) != digest(system.read_bytes()):
            raise ReleaseError("diagnostic_system_python_copy_required")
        inputs = self.checked(self.operations / "build-inputs" / self.request["release_id"])
        lock = self.checked(inputs / "core.lock").read_bytes()
        expected_lock = self.request.get("receipts", {}).get("dependencies", {}).get("lock_sha256")
        if digest(lock) != expected_lock or lock != (self.release / "requirements.lock").read_bytes():
            raise ReleaseError("diagnostic_lock_mismatch")
        packages = self.diagnostic_packages(lock, target)
        report = {
            "schema": 1,
            "commit": self.request["commit"],
            "checked_at": int(self.now()),
            "dry_run_only": True,
            "prepare_verified": False,
            "production_changed": False,
            "canonical_runtime_policy": "read_only_private_run",
            "commands": {},
        }
        attempt = secrets.token_hex(4)
        report["attempt"] = attempt
        filename = "build-diagnostic-" + secrets.token_hex(8) + ".json"
        report_path = self.record / filename
        for phase, arguments in (
            ("namespace", []),
            ("check", ["check"]),
            (
                "dryrun",
                [
                    "install",
                    "--dry-run",
                    "--no-cache-dir",
                    "--no-index",
                    "--find-links",
                    str(inputs),
                    "--require-hashes",
                    "--only-binary=:all:",
                    "--upgrade",
                    "-r",
                    str(inputs / "core.lock"),
                ],
            ),
        ):
            self.boundary("diagnose_build_" + phase)
            self.no_stage_processes()
            unit = "prism-diagnose-" + self.request["commit"][:12] + "-" + phase + "-" + attempt + ".service"
            command = [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--unit=" + unit,
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=TemporaryFileSystem=/run:ro",
                "--property=InaccessiblePaths=-" + str(self.operations / "staging"),
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=60",
                "--property=KillMode=control-group",
                "--property=UMask=0077",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=/nonexistent",
                "PIP_CONFIG_FILE=/dev/null",
                "PIP_NO_INPUT=1",
                "PYTHONDONTWRITEBYTECODE=1",
            ]
            if phase == "namespace":
                command.insert(command.index("/usr/bin/env"), "--property=ReadWritePaths=" + str(target))
                command.append("/usr/bin/true")
            else:
                command.extend([str(binary), "-I", "-B", "-m", "pip", *arguments])
            summary = self.observe_diagnostic(command, unit, packages)
            report["commands"][phase] = summary
            if phase == "namespace" and (
                summary["return_code"] != 0 or summary["truncated"] or summary["timed_out"]
            ):
                report["pip_checks_skipped"] = "namespace_preflight_failed"
                report["original_namespace_succeeded"] = False
                self.write(report_path, encoded(report))
                report["namespace_variants"] = self.diagnose_namespace_variants(
                    target, packages, report, report_path
                )
                break
            if phase == "namespace":
                report["original_namespace_succeeded"] = True
            self.write(report_path, encoded(report))
        report["service_stopped"] = True
        self.write(report_path, encoded(report))
        self.action_report = report

    def quarantine_failed_prepare(self):
        pointer = self.checked(self.record / "quarantine-intent.json")
        intent = json.loads(pointer.read_bytes()) if pointer.exists() else None
        resuming = isinstance(intent, dict) and intent.get("completed") is False
        self.failed_prepare_guard(candidate_required=not resuming)
        if not resuming:
            nonce = secrets.token_hex(8)
            private = self.checked(self.operations / "failed-prepares" / self.request["release_id"] / nonce)
            failed = self.checked(self.root / "opt/prism/failed")
            for path in (failed, private):
                self.checked(path).mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)
                if self.root == Path("/") and path.stat().st_uid != 0:
                    raise ReleaseError("root_owned_directory_required")
            sources = [
                self.release,
                self.record / "dependencies",
                self.operations / "build-inputs" / self.request["release_id"],
            ]
            destinations = [
                failed / (self.request["commit"] + "-" + nonce),
                private / "dependencies",
                private / "build-inputs",
            ]
            items = []
            for source, destination in zip(sources, destinations, strict=True):
                info = self.checked(source).lstat()
                if not stat.S_ISDIR(info.st_mode) or self.checked(destination).exists():
                    raise ReleaseError("quarantine_directory_rejected")
                if info.st_dev != destination.parent.stat().st_dev:
                    raise ReleaseError("quarantine_cross_device_rejected")
                items.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "device": info.st_dev,
                        "inode": info.st_ino,
                    }
                )
            intent = {
                "schema": 1,
                "commit": self.request["commit"],
                "manifest_sha256": self.request["manifest_sha256"],
                "nonce": nonce,
                "completed": False,
                "items": items,
            }
            self.write(pointer, encoded(intent))
        nonce = intent.get("nonce", "")
        private = self.operations / "failed-prepares" / self.request["release_id"] / nonce
        expected_pairs = [
            (self.release, self.root / "opt/prism/failed" / (self.request["commit"] + "-" + nonce)),
            (self.record / "dependencies", private / "dependencies"),
            (self.operations / "build-inputs" / self.request["release_id"], private / "build-inputs"),
        ]
        if (
            intent.get("schema") != 1
            or intent.get("commit") != self.request["commit"]
            or intent.get("manifest_sha256") != self.request["manifest_sha256"]
            or not re.fullmatch(r"[0-9a-f]{16}", nonce)
            or not isinstance(intent.get("items"), list)
            or len(intent["items"]) != 3
        ):
            raise ReleaseError("quarantine_intent_rejected")
        self.boundary("quarantine_failed_prepare")
        labels = ("quarantine_candidate", "quarantine_dependencies", "quarantine_build_inputs")
        for label, item, (source, destination) in zip(labels, intent["items"], expected_pairs, strict=True):
            if item.get("source") != str(source) or item.get("destination") != str(destination):
                raise ReleaseError("quarantine_intent_rejected")
            self.checked(source)
            self.checked(destination)
            if source.exists() == destination.exists():
                raise ReleaseError("quarantine_state_requires_reconciliation")
            current = source if source.exists() else destination
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                item.get("device"),
                item.get("inode"),
            ):
                raise ReleaseError("quarantine_identity_changed")
            if source.exists():
                self.boundary(label)
                if self.runtime()["working_directory"] != str(self.production):
                    raise ReleaseError("failed_prepare_original_runtime_required")
                self.no_stage_processes()
                self.checked(source)
                self.checked(destination)
                if not source.exists() or destination.exists():
                    raise ReleaseError("quarantine_state_requires_reconciliation")
                info = source.lstat()
                if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
                    item.get("device"),
                    item.get("inode"),
                ):
                    raise ReleaseError("quarantine_identity_changed")
                # Rename only: EXDEV or any conflict fails closed, never copy/delete.
                source.rename(destination)
        completed = dict(intent, completed=True, checked_at=int(self.now()))
        self.write(self.record / ("quarantine-" + nonce + ".json"), encoded(completed))
        self.write(pointer, encoded(completed))

    def build_command(self, target, phase, arguments):
        self.run(
            [
                "/usr/bin/systemd-run",
                "--wait",
                "--pipe",
                "--quiet",
                "--collect",
                "--unit=prism-build-" + self.request["commit"][:12] + "-" + phase,
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=NoNewPrivileges=yes",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=ReadWritePaths=" + str(target),
                "--property=TemporaryFileSystem=/run:ro",
                "--property=InaccessiblePaths=-" + str(self.operations / "staging"),
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=1200",
                "--property=KillMode=control-group",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=/nonexistent",
                "PIP_CONFIG_FILE=/dev/null",
                "PIP_NO_INPUT=1",
                "PYTHONDONTWRITEBYTECODE=1",
                str(target / "bin/python"),
                "-I",
                "-B",
                "-m",
                "pip",
                *arguments,
            ],
            timeout=1250,
        )

    def stage_account(self):
        try:
            account = pwd.getpwnam("prism-stage")
        except KeyError:
            self.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "prism-stage",
                ]
            )
            account = pwd.getpwnam("prism-stage")
        ubuntu = pwd.getpwnam("ubuntu")
        if (
            account.pw_uid == ubuntu.pw_uid
            or account.pw_uid == 0
            or account.pw_shell != "/usr/sbin/nologin"
            or grp.getgrgid(account.pw_gid).gr_name != "prism-stage"
            or any("prism-stage" in group.gr_mem for group in grp.getgrall())
        ):
            raise ReleaseError("stage_account_not_isolated")

    def integration_account(self):
        try:
            account = pwd.getpwnam("prism-integration")
        except KeyError:
            self.boundary("integration_account")
            self.run(
                [
                    "/usr/sbin/useradd",
                    "--system",
                    "--user-group",
                    "--no-create-home",
                    "--shell",
                    "/usr/sbin/nologin",
                    "prism-integration",
                ]
            )
            account = pwd.getpwnam("prism-integration")
        others = [pwd.getpwnam(name) for name in ("ubuntu", "prism-stage")]
        if (
            account.pw_uid == 0
            or account.pw_gid == 0
            or any(account.pw_uid == item.pw_uid or account.pw_gid == item.pw_gid for item in others)
            or account.pw_shell != "/usr/sbin/nologin"
            or grp.getgrgid(account.pw_gid).gr_name != "prism-integration"
            or any("prism-integration" in group.gr_mem for group in grp.getgrall())
        ):
            raise ReleaseError("integration_account_not_isolated")
        return account

    def integration_idle(self, uid):
        for process in (self.root / "proc").iterdir():
            if process.name.isdigit():
                values = self.kernel_uids(process)
                if values is not None and uid in values:
                    raise ReleaseError("integration_process_active")

    def live_original_state(self):
        current = self.runtime()
        expected = self.request.get("receipts", {})
        if current["working_directory"] != str(self.production) or current["fingerprint"] != expected.get(
            "expected_current_fingerprint"
        ):
            raise ReleaseError("live_original_runtime_changed")
        if self.source_inventory(self.request.get("source_manifest", {}), self.production) != expected.get(
            "expected_source_manifest_sha256"
        ):
            raise ReleaseError("live_original_source_changed")
        return {"production_pid": self.process_id(SERVICE), "runtime_fingerprint": current["fingerprint"]}

    def network_preflight_request(self, role):
        """A fixed action has no caller-selected mode, path, address or command."""
        if self.request.get("action") != "integration_" + role + "_network_preflight" or set(
            self.request
        ) != {
            "action",
            "commit",
            "release_id",
            "receipts",
            "receipts_sha256",
            "receipts_raw",
            "archive_sha256",
            "manifest_sha256",
            "source_manifest",
            "integration_attempt",
            role + "_helpers",
        }:
            raise ReleaseError("network_preflight_request_rejected")

    def network_original_state(self):
        """Metadata-only baseline; never runtime(), ready(), HTTP or environment.

        expected_network_runtime_fingerprint is SHA256(encoded(facts)), where
        encoded is canonical UTF-8 JSON (sort_keys=True, separators=(',', ':')).
        ExecStart hashes the exact UTF-8 property value, excluding the line LF.
        The receipt must be collected independently for this candidate/source.
        """
        command = ["/usr/bin/systemctl", "show", "--property=" + NETWORK_RUNTIME_PROPERTIES, SERVICE]

        def properties():
            raw = self.run(command, timeout=5)
            if type(raw) is not bytes or len(raw) > 32768:
                raise ReleaseError("network_original_runtime_changed")
            try:
                rows = [line.split("=", 1) for line in raw.decode("utf-8").splitlines()]
                state = dict(rows)
            except (ValueError, UnicodeError):
                raise ReleaseError("network_original_runtime_changed") from None
            if len(rows) != 4 or set(state) != set(NETWORK_RUNTIME_PROPERTIES.split(",")):
                raise ReleaseError("network_original_runtime_changed")
            return state

        state = properties()
        pid = state["MainPID"]
        if (
            not re.fullmatch("[1-9][0-9]*", pid)
            or int(pid) <= 1
            or not re.fullmatch("[0-9a-f]{32}", state["InvocationID"])
            or state["WorkingDirectory"] != str(self.production)
            or not state["ExecStart"]
        ):
            raise ReleaseError("network_original_runtime_changed")
        process = self.root / "proc" / pid

        def process_facts():
            raw = (process / "stat").read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            if (
                raw.split(" ", 1)[0] != pid
                or len(fields) < 20
                or not fields[19].isdigit()
                or int(fields[19]) <= 0
            ):
                raise ReleaseError("network_original_runtime_changed")
            if Path(os.readlink(process / "cwd")) != self.production:
                raise ReleaseError("network_original_runtime_changed")
            return int(fields[19])

        ticks = process_facts()
        source_sha = self.source_inventory(self.request.get("source_manifest", {}), self.production)
        expected = self.request.get("receipts", {})
        if source_sha != expected.get("expected_source_manifest_sha256"):
            raise ReleaseError("network_original_source_changed")
        facts = {
            "schema": 1,
            "candidate": self.request["commit"],
            "unit": SERVICE,
            "production_pid": int(pid),
            "production_startticks": ticks,
            "invocation_id": state["InvocationID"],
            "working_directory": state["WorkingDirectory"],
            "exec_start_sha256": digest(state["ExecStart"].encode("utf-8")),
            "source_manifest_sha256": source_sha,
        }
        fingerprint = digest(encoded(facts))
        if (
            type(expected.get("expected_network_runtime_fingerprint")) is not str
            or not DIGEST.fullmatch(expected["expected_network_runtime_fingerprint"])
            or fingerprint != expected["expected_network_runtime_fingerprint"]
            or properties() != state
            or process_facts() != ticks
        ):
            raise ReleaseError("network_original_runtime_changed")
        return {
            "production_pid": int(pid),
            "runtime_fingerprint": fingerprint,
            "source_manifest_sha256": source_sha,
        }

    def live_helper_sources(self):
        supplied = self.request.get("live_helpers")
        if type(supplied) is not dict or set(supplied) != set(LIVE_INTEGRATION_HELPERS):
            raise ReleaseError("approved_live_integration_helpers_required")
        contents = {}
        for name, approved in LIVE_INTEGRATION_HELPERS.items():
            item = supplied[name]
            if (
                type(item) is not dict
                or set(item) != {"sha256", "content"}
                or item["sha256"] != approved
                or type(item["content"]) is not str
                or len(item["content"]) > 180000
            ):
                raise ReleaseError("approved_live_integration_helpers_required")
            try:
                raw = base64.b64decode(item["content"], validate=True)
            except (ValueError, TypeError):
                raise ReleaseError("approved_live_integration_helpers_required") from None
            if not 0 < len(raw) <= 131072 or digest(raw) != approved:
                raise ReleaseError("approved_live_integration_helpers_required")
            contents[name] = raw
        return contents

    def live_sealed_sources(self, directory):
        self.checked(directory)
        if set(path.name for path in directory.iterdir()) != set(LIVE_INTEGRATION_HELPERS):
            raise ReleaseError("live_helper_source_rejected")
        for parent in (directory, *directory.parents):
            if parent == self.root.parent:
                break
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o022
                or self.root == Path("/")
                and info.st_uid != 0
            ):
                raise ReleaseError("live_helper_source_rejected")
        contents = {}
        for name, approved in LIVE_INTEGRATION_HELPERS.items():
            path = self.checked(directory / name)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_size > 131072
                    or self.root == Path("/")
                    and info.st_uid != 0
                ):
                    raise ReleaseError("live_helper_source_rejected")
                raw = stream.read(131073)
                if (
                    self.cli_identity(info) != self.cli_identity(os.fstat(stream.fileno()))
                    or self.cli_identity(info) != self.cli_identity(path.lstat())
                    or digest(raw) != approved
                ):
                    raise ReleaseError("live_helper_source_rejected")
            contents[name] = raw
        return contents

    @contextmanager
    def live_module(self, contents):
        directory = self.checked(
            self.root / "opt/prism/validation/live" / digest(encoded(LIVE_INTEGRATION_HELPERS))
        )
        if not directory.exists():
            self.boundary("live_install_helpers")
            self.public_directory(directory.parent.parent)
            self.public_directory(directory.parent)
            directory.mkdir(mode=0o700)
            for name, raw in contents.items():
                self.write(directory / name, raw, 0o444)
            directory.chmod(0o555)
        sealed = self.live_sealed_sources(directory)
        module = types.ModuleType("reviewed_live_integration_validation")
        module.__file__ = str(directory / "live_integration_validation.py")
        exec(compile(sealed["live_integration_validation.py"], module.__file__, "exec"), module.__dict__)
        if (
            module.SOURCE_PINS != LIVE_SOURCE_PINS
            or module.SAFE_ERRORS != LIVE_ERRORS
            or module.RESOLVE_CODE != LIVE_RESOLVE_CODE
            or LIVE_INTEGRATION_HELPERS["cgroup_network_gate.py"] != module.NETWORK_GATE_SHA256
            or LIVE_INTEGRATION_HELPERS["systemd_unit_reference.py"] != module.UNIT_REFERENCE_SHA256
            or module.UNIT_REFERENCE_ERRORS != UNIT_REFERENCE_ERRORS
            or module.EXTRA_NEGATIVES != NETWORK_EXTRA_NEGATIVES
        ):
            raise ReleaseError("live_helper_source_rejected")
        self._live_module = module
        try:
            yield module
        finally:
            self._live_module = None

    def live_model_gate(self, work, expected_binding=None):
        """Re-read the root permit and real processes before opening any config."""
        try:
            module = getattr(self, "_live_module", None)
            validator = getattr(self, "_live_validator", None)
            attempt = self.request.get("integration_attempt")
            if (
                self.request.get("action") != "integration_live"
                or module is None
                or type(attempt) is not str
                or not re.fullmatch("[a-z0-9][a-z0-9-]{7,39}", attempt)
                or getattr(self, "_live_attempt", None) != attempt
                or type(validator) is not module.Validation
                or validator.preflight_only is not False
                or validator.work != Path(work)
                or validator.request.get("candidate") != self.request["commit"]
                or validator.request.get("release_id") != attempt
            ):
                raise ValueError
            validator.verify_network_gate()
            fixed = self.checked(self.operations / "integration" / attempt / "live")
            if Path(work) != fixed:
                raise ValueError
            for path in (fixed, *fixed.parents):
                if path == self.root.parent:
                    break
                info = path.lstat()
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_mode & 0o022
                    or self.root == Path("/")
                    and info.st_uid != 0
                ):
                    raise ValueError
            path = self.checked(fixed / "root-allow.json")
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_size > 16384
                    or self.root == Path("/")
                    and info.st_uid != 0
                ):
                    raise ValueError
                allow = module.decode(stream.read(16385))
                if self.cli_identity(info) != self.cli_identity(
                    os.fstat(stream.fileno())
                ) or self.cli_identity(info) != self.cli_identity(path.lstat()):
                    raise ValueError
            if (
                type(allow) is not dict
                or set(allow)
                != {
                    "schema",
                    "binding",
                    "policy_sha256",
                    "bootstrap_proof_sha256",
                    "allowed_at",
                    "witness",
                    "kernel",
                    "sources",
                    "broker_memory_sealed",
                }
                or type(allow["schema"]) is not int
                or allow["schema"] != 1
                or allow["sources"] != LIVE_SOURCE_PINS
                or allow["policy_sha256"] != LIVE_SOURCE_PINS["policy"]
                or allow["broker_memory_sealed"] is not True
                or type(allow["allowed_at"]) is not int
                or not 0 <= self.now() - allow["allowed_at"] <= 120
            ):
                raise ValueError
            binding = allow["binding"]
            if binding != validator.binding:
                raise ValueError
            cli = (self.release / ".cli-revision").read_text().strip()
            if (
                type(binding) is not dict
                or set(binding)
                != {"candidate", "cli_sha256", "probe_sha256", "nonce", "worker_pid", "broker_pid"}
                or binding["candidate"] != self.request["commit"]
                or binding["cli_sha256"] != cli
                or binding["probe_sha256"] != LIVE_SOURCE_PINS["worker"]
                or type(binding["nonce"]) is not str
                or not re.fullmatch("[0-9a-f]{32}", binding["nonce"])
                or any(
                    type(binding[key]) is not int or binding[key] <= 1 for key in ("worker_pid", "broker_pid")
                )
                or binding["worker_pid"] == binding["broker_pid"]
                or expected_binding is not None
                and binding != expected_binding
            ):
                raise ValueError
            kernel = allow["kernel"]
            keys = {
                "worker_uids",
                "broker_uids",
                "worker_netns_inode",
                "broker_netns_inode",
                "host_netns_inode",
                "worker_startticks",
                "broker_startticks",
            }
            if (
                type(kernel) is not dict
                or set(kernel) != keys
                or any(
                    type(kernel[key]) is not int or kernel[key] <= 0
                    for key in keys - {"worker_uids", "broker_uids"}
                )
            ):
                raise ValueError
            host = (self.root / "proc/1/ns/net").stat().st_ino
            production_pid = self.process_id(SERVICE)
            production_ns = (self.root / "proc" / str(production_pid) / "ns/net").stat().st_ino
            identities = [
                pwd.getpwnam(name)
                for name in ("prism-integration", "prism-integration-broker", "ubuntu", "prism-stage")
            ]
            if (
                0 in {item.pw_uid for item in identities[:2]}
                or len({item.pw_uid for item in identities}) != 4
                or len({item.pw_gid for item in identities}) != 4
                or any(item.pw_shell != "/usr/sbin/nologin" for item in identities[:2])
            ):
                raise ValueError
            for role, account in zip(("worker", "broker"), identities[:2], strict=True):
                pid = binding[role + "_pid"]
                unit = (
                    "prism-live-"
                    + role
                    + "-"
                    + self.request["commit"][:12]
                    + "-"
                    + binding["nonce"]
                    + ".service"
                )
                raw = self.run(
                    [
                        "/usr/bin/systemctl",
                        "show",
                        "--property=MainPID,ExecMainPID,InvocationID,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,LimitCORE",
                        unit,
                    ],
                    timeout=5,
                )
                state = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line)
                if (
                    state.get("MainPID") != str(pid)
                    or state.get("ExecMainPID") != str(pid)
                    or not re.fullmatch("[0-9a-f]{32}", state.get("InvocationID", ""))
                    or state.get("User") != account.pw_name
                    or state.get("Group") != account.pw_name
                    or state.get("ControlGroup") != "/system.slice/" + unit
                    or state.get("NoNewPrivileges") != "yes"
                    or state.get("ProtectSystem") != "strict"
                    or state.get("ProtectHome") != "yes"
                    or state.get("PrivateNetwork") != ("yes" if role == "worker" else "no")
                    or state.get("KillMode") != "control-group"
                    or state.get("LimitCORE") != "0"
                ):
                    raise ValueError
                process = self.root / "proc" / str(pid)
                if (
                    self.kernel_uids(process) != (account.pw_uid,) * 4
                    or module.core_disabled(process) is not True
                    or kernel[role + "_uids"] != [account.pw_uid] * 4
                    or (process / "cgroup").read_text().strip() != "0::/system.slice/" + unit
                    or module.startticks(process) != kernel[role + "_startticks"]
                    or (process / "ns/net").stat().st_ino != kernel[role + "_netns_inode"]
                    or self.stage_runtime_mount(process) is not True
                ):
                    raise ValueError
            if (
                kernel["host_netns_inode"] != host
                or kernel["broker_netns_inode"] != host
                or kernel["worker_netns_inode"] in {host, production_ns}
            ):
                raise ValueError
            for process in (self.root / "proc").iterdir():
                if process.name.isdigit():
                    values = self.kernel_uids(process)
                    if (
                        values is not None
                        and identities[3].pw_uid in values
                        and (process / "ns/net").stat().st_ino == kernel["worker_netns_inode"]
                    ):
                        raise ValueError
            proof, proof_sha = self.integration_read(
                fixed / "output/bootstrap-proof.json", identities[0].pw_uid
            )
            if (
                set(proof)
                != {"schema", "binding", "production_pid", "issued_at", "checked_at", "sources", "checks"}
                or type(proof["schema"]) is not int
                or proof["schema"] != 1
                or proof["binding"] != binding
                or type(proof["production_pid"]) is not int
                or proof["production_pid"] != production_pid
                or type(proof["issued_at"]) is not int
                or type(proof["checked_at"]) is not int
                or not proof["issued_at"]
                <= proof["checked_at"]
                <= allow["allowed_at"]
                <= self.now()
                <= proof["issued_at"] + 120
                or proof["sources"] != LIVE_SOURCE_PINS
                or type(proof["checks"]) is not dict
                or set(proof["checks"]) != module.BOOT_CHECKS
                or any(flag is not True for flag in proof["checks"].values())
                or proof_sha != allow["bootstrap_proof_sha256"]
            ):
                raise ValueError
            witness = allow["witness"]
            if (
                type(witness) is not dict
                or set(witness) != {"binding", "checked_at", "checks"}
                or witness["binding"] != binding
                or type(witness["checked_at"]) is not int
                or not proof["checked_at"] <= witness["checked_at"] <= allow["allowed_at"]
                or type(witness["checks"]) is not dict
                or set(witness["checks"]) != module.WITNESSES
                or any(flag is not True for flag in witness["checks"].values())
            ):
                raise ValueError
            for name, approved in LIVE_SOURCE_PINS.items():
                source = self.checked(fixed / (name + ".py"))
                info = source.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or info.st_mode & 0o022
                    or info.st_size > 131072
                    or self.root == Path("/")
                    and info.st_uid != 0
                    or digest(source.read_bytes()) != approved
                ):
                    raise ValueError
            return binding
        except Exception:
            raise ReleaseError("live_model_authorization_rejected") from None

    def read_bound_model_key(self, work, expected_binding):
        """Only a live supervisor's post-isolation callback may invoke this."""
        if getattr(self, "_live_key_attempted", False):
            raise ReleaseError("live_model_credential_unavailable")
        self.live_model_gate(work, expected_binding)
        self.boundary("live_credential_access")
        self.live_original_state()
        self.live_model_gate(work, expected_binding)
        expected = self.live_environment_binding()
        self._live_key_attempted = True
        self._live_executor_memory_sealed = seal_executor_memory()
        if self._live_executor_memory_sealed is not True:
            raise ReleaseError("live_executor_memory_unsealed")
        path = self.checked(self.production / ".env")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            before = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid not in {0, pwd.getpwnam("ubuntu").pw_uid}
                or not 0 < before.st_size <= 65536
                or before.st_size != expected.get("size")
                or f"{stat.S_IMODE(before.st_mode):04o}" != expected.get("mode")
            ):
                raise ReleaseError("live_model_credential_unavailable")
            raw = source.read(65537)
            if (
                digest(raw) != expected.get("sha256")
                or self.cli_identity(before) != self.cli_identity(os.fstat(source.fileno()))
                or self.cli_identity(before) != self.cli_identity(path.lstat())
            ):
                raise ReleaseError("live_model_credential_unavailable")
            key = selected_model_key(raw)
            self.boundary("live_credential_reconcile")
            self.live_original_state()
            self.live_model_gate(work, expected_binding)
            if self.cli_identity(before) != self.cli_identity(os.fstat(source.fileno())) or self.cli_identity(
                before
            ) != self.cli_identity(path.lstat()):
                raise ReleaseError("live_model_credential_unavailable")
            return key

    def live_environment_binding(self):
        expected = self.request.get("receipts", {}).get("model_environment")
        if (
            type(expected) is not dict
            or set(expected) != {"sha256", "size", "mode"}
            or type(expected.get("sha256")) is not str
            or not DIGEST.fullmatch(expected["sha256"])
            or type(expected.get("size")) is not int
            or not 0 < expected["size"] <= 65536
            or type(expected.get("mode")) is not str
            or not re.fullmatch("0[0-7]{3}", expected["mode"])
        ):
            raise ReleaseError("live_model_credential_unavailable")
        return expected

    def live_safe_record(self, value, attempt):
        """Whitelist every value before any journal/write/output sees it."""
        preflight = self.request.get("action") == "integration_live_network_preflight"
        stages = LIVE_STAGES - (
            {
                "allow",
                "key_release",
                "key_send",
                "public_qa",
                "result_validation",
                "broker_finish",
                "verification",
                "complete",
            }
            if preflight
            else {"network_preflight_complete"}
        )
        progress = {
            "schema",
            "action",
            "candidate",
            "release_id",
            "stage",
            "at",
            "passed",
            "production_verified",
        }
        final = {
            *NETWORK_CLEANUP_FIELDS,
            "schema",
            "action",
            "candidate",
            "release_id",
            "source_sha256",
            "started_at",
            "finished_at",
            "stage",
            "passed",
            "error_class",
            "live_model_verified",
            "native_search_verified",
            "owned_mcp_verified",
            "server_clock_verified",
            "native_fetch_verified",
            "market_provider_verified",
            "research_quality_verified",
            "production_verified",
            "units_stopped",
            "cleanup_error_class",
            "broker_negative_errno",
            "broker_filesystem_checks",
            "broker_preflight_facts",
            "broker_memory_sealed",
            "provider_peer_count",
            "bootstrap_proof_sha256",
            "worker_result_sha256",
            "blocked_provider_header_names",
            "official_source_count",
            "broker_summary_sha256",
            "requests",
            "preflight_only",
            "network_gate_verified",
            "network_gate_verified_before_cleanup",
            "network_gate_cleaned",
            "key_acquisition_attempted",
        }
        if (
            type(value) is not dict
            or set(value) not in (progress, final)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("action") != ("integration_network_preflight" if preflight else "integration_live")
            or value.get("candidate") != self.request["commit"]
            or value.get("release_id") != attempt
            or value.get("stage") not in stages
            or type(value.get("passed")) is not bool
            or value.get("production_verified") is not False
        ):
            raise ReleaseError("live_safe_record_rejected")
        if set(value) == progress:
            if (
                value["passed"] is not False
                or type(value.get("at")) is not int
                or not 0 <= self.now() - value["at"] <= 300
            ):
                raise ReleaseError("live_safe_record_rejected")
            return json.loads(encoded(value))
        flags = {
            "live_model_verified",
            "native_search_verified",
            "owned_mcp_verified",
            "server_clock_verified",
            "native_fetch_verified",
            "market_provider_verified",
            "research_quality_verified",
            "production_verified",
            "units_stopped",
            "broker_memory_sealed",
            "preflight_only",
            "network_gate_verified",
            "network_gate_verified_before_cleanup",
            "network_gate_cleaned",
            "key_acquisition_attempted",
        }
        if not valid_network_cleanup_record(value):
            raise ReleaseError("live_safe_record_rejected")
        requests = value.get("requests")
        errno = value.get("broker_negative_errno")
        filesystem = value.get("broker_filesystem_checks")
        preflight_facts = value.get("broker_preflight_facts")
        if (
            value.get("source_sha256") != LIVE_SOURCE_PINS
            or value.get("preflight_only") is not preflight
            or any(type(value.get(key)) is not bool for key in flags)
            or any(
                value[key] is not False
                for key in (
                    "native_fetch_verified",
                    "market_provider_verified",
                    "research_quality_verified",
                    "production_verified",
                )
            )
            or value.get("error_class") not in LIVE_ERRORS | {None}
            or value.get("cleanup_error_class") not in {None, "cleanup_failed"}
            or any(type(value.get(key)) is not int for key in ("started_at", "finished_at"))
            or not value["started_at"] <= value["finished_at"] <= self.now() + 1
            or not 0 <= value["finished_at"] - value["started_at"] <= 300
            or type(requests) is not dict
            or set(requests) != {"messages", "count_tokens", "quote", "kline", "public_fetch"}
            or any(type(item) is not int or item < 0 for item in requests.values())
            or requests["messages"] > 6
            or requests["count_tokens"] > 2
            or any(requests[key] != 0 for key in ("quote", "kline", "public_fetch"))
            or type(errno) is not dict
            or set(errno)
            not in (
                set(),
                {"production", "vibe", "metadata", "private", "disallowed_public"} | NETWORK_EXTRA_NEGATIVES,
            )
            or any(type(code) is not int or not 0 <= code <= 4095 for code in errno.values())
            or type(filesystem) is not dict
            or set(filesystem)
            not in (
                set(),
                {
                    "production_home_denied",
                    "production_proc_denied",
                    "config_write_denied",
                    "staging_directory_denied",
                },
            )
            or any(type(flag) is not bool for flag in filesystem.values())
            or type(preflight_facts) is not dict
            or set(preflight_facts)
            not in (set(), {"public_dns_pinned", "upstream_tls_verified", "broker_memory_sealed"})
            or any(type(flag) is not bool for flag in preflight_facts.values())
            or (
                bool(preflight_facts)
                and value["broker_memory_sealed"] != preflight_facts["broker_memory_sealed"]
            )
        ):
            raise ReleaseError("live_safe_record_rejected")
        if (
            value["network_gate_verified"]
            != (
                value["network_gate_verified_before_cleanup"]
                and value["network_gate_cleaned"]
                and value["units_stopped"]
                and value["cleanup_error_class"] is None
            )
            or value["network_gate_verified_before_cleanup"]
            and (
                not errno
                or any(code not in {1, 13} for code in errno.values())
                or not filesystem
                or not all(filesystem.values())
                or not preflight_facts
                or not all(preflight_facts.values())
                or not value["broker_memory_sealed"]
                or type(value["provider_peer_count"]) is not int
                or value["provider_peer_count"] < 1
            )
            or preflight
            and (
                value["key_acquisition_attempted"]
                or any(
                    value[key]
                    for key in (
                        "live_model_verified",
                        "native_search_verified",
                        "owned_mcp_verified",
                        "server_clock_verified",
                    )
                )
                or any(requests.values())
                or value["official_source_count"] != 0
                or value["worker_result_sha256"] is not None
                or value["broker_summary_sha256"] is not None
            )
        ):
            raise ReleaseError("live_safe_record_rejected")
        for name, maximum in (("provider_peer_count", 16), ("official_source_count", 16)):
            if type(value.get(name)) is not int or not 0 <= value[name] <= maximum:
                raise ReleaseError("live_safe_record_rejected")
        for name in ("bootstrap_proof_sha256", "worker_result_sha256", "broker_summary_sha256"):
            if value.get(name) is not None and (
                type(value[name]) is not str or not DIGEST.fullmatch(value[name])
            ):
                raise ReleaseError("live_safe_record_rejected")
        headers = value.get("blocked_provider_header_names")
        if (
            type(headers) is not list
            or len(headers) > 64
            or any(
                type(name) is not str or not re.fullmatch("[a-z][a-z0-9-]{0,63}", name) for name in headers
            )
        ):
            raise ReleaseError("live_safe_record_rejected")
        if value["passed"] and (
            value["error_class"] is not None
            or value["cleanup_error_class"] is not None
            or value["stage"] != ("network_preflight_complete" if preflight else "complete")
            or value["units_stopped"] is not True
            or value["broker_memory_sealed"] is not True
            or not value["network_gate_verified"]
            or value["key_acquisition_attempted"] is preflight
            or not preflight
            and any(
                value[key] is not True
                for key in (
                    "live_model_verified",
                    "native_search_verified",
                    "owned_mcp_verified",
                    "server_clock_verified",
                )
            )
            or not preflight
            and requests["messages"] < 2
            or not errno
            or not filesystem
            or not preflight_facts
            or any(code not in {1, 13} for code in errno.values())
            or any(flag is not True for flag in filesystem.values())
            or any(flag is not True for flag in preflight_facts.values())
            or value["provider_peer_count"] < 1
            or not preflight
            and value["official_source_count"] < 1
            or headers
            or any(
                value[name] is None
                for name in (
                    ("bootstrap_proof_sha256",)
                    if preflight
                    else ("bootstrap_proof_sha256", "worker_result_sha256", "broker_summary_sha256")
                )
            )
        ):
            raise ReleaseError("live_safe_record_rejected")
        if not value["passed"] and value["error_class"] is None:
            raise ReleaseError("live_safe_record_rejected")
        return json.loads(encoded(value))

    def integration_live(self):
        return self._integration_live(network_preflight=False)

    def integration_live_network_preflight(self):
        self.network_preflight_request("live")
        return self._integration_live(network_preflight=True)

    def _integration_live(self, *, network_preflight):
        if self.request.get("action") != (
            "integration_live_network_preflight" if network_preflight else "integration_live"
        ):
            raise ReleaseError("network_preflight_request_rejected")
        self.verify_tree()
        if not network_preflight:
            self.live_environment_binding()
        contents = self.live_helper_sources()
        attempt = self.request.get("integration_attempt")
        if type(attempt) is not str or not re.fullmatch("[a-z0-9][a-z0-9-]{7,39}", attempt):
            raise ReleaseError("explicit_live_integration_attempt_required")
        original_state = self.network_original_state if network_preflight else self.live_original_state
        baseline = original_state()
        work = self.checked(self.operations / "integration" / attempt / "live")
        directory = self.checked(
            self.record
            / (("network-preflight-live-" if network_preflight else "integration-live-") + attempt)
        )
        if work.exists() or directory.exists():
            raise ReleaseError("live_integration_attempt_already_exists")
        self.boundary("live_attempt")
        directory.mkdir(mode=0o700)
        descriptor = os.open(
            directory / "events.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        summary, count, chain, cancelled, boundary_error, key_error = None, 0, digest(b""), None, None, None
        key_provided = False
        own_units = set()
        stopped_units = set()
        inventories, observed_pids = {}, {}
        resolved_policy = None
        validator = None
        self._live_attempt = attempt
        self._live_key_attempted = False
        self._live_executor_memory_sealed = False
        try:
            with self.live_module(contents) as module:
                validation_type = module.Validation
                network_gate_path = Path(module.__file__).with_name("cgroup_network_gate.py")

                def record(value):
                    nonlocal summary, count, chain
                    safe = self.live_safe_record(value, attempt)
                    if summary is not None or count >= 32:
                        raise ReleaseError("live_events_after_result_rejected")
                    entry = encoded({"sequence": count + 1, "previous_sha256": chain, "event": safe}) + b"\n"
                    if len(entry) > 16384:
                        raise ReleaseError("live_safe_record_rejected")
                    view = memoryview(entry)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise ReleaseError("live_event_write_failed")
                        view = view[written:]
                    os.fsync(descriptor)
                    count += 1
                    chain = digest(entry)
                    if "finished_at" in safe:
                        summary = safe

                def boundary(label):
                    nonlocal cancelled, boundary_error
                    if (
                        label not in LIVE_BOUNDARIES
                        or network_preflight
                        and label
                        in {
                            "live_allow",
                            "live_key_release",
                            "live_key_send",
                            "live_public_qa",
                            "live_result_validation",
                            "live_broker_finish",
                            "live_verification",
                            "live_complete",
                            "live_wait_qa",
                            "live_wait_broker",
                        }
                    ):
                        raise module.ValidationError("supervision_failed")
                    try:
                        self.boundary(label)
                        if original_state() != baseline:
                            raise ReleaseError("live_original_runtime_changed")
                    except Exception as error:
                        code = failure_code(error)
                        if code in {
                            "operator_stop",
                            "fresh_operator_permit_required",
                            "permit_timeout",
                            "controller_disconnected",
                        }:
                            cancelled = "operator_stop"
                            raise module.ValidationError("operator_stop") from None
                        boundary_error = code
                        raise module.ValidationError("supervision_failed") from None

                def run(argv, *, timeout):
                    nonlocal resolved_policy
                    if (
                        type(argv) is not list
                        or any(type(item) is not str or "\0" in item for item in argv)
                        or type(timeout) not in {int, float}
                        or not 0 < timeout <= 30
                    ):
                        raise ReleaseError("live_command_rejected")
                    if (
                        validator is None
                        or type(validator.nonce) is not str
                        or not re.fullmatch("[0-9a-f]{32}", validator.nonce)
                        or type(validator.issued) is not int
                        or validator.issued <= 0
                    ):
                        raise ReleaseError("live_command_rejected")
                    units = {
                        role: "prism-live-"
                        + role
                        + "-"
                        + self.request["commit"][:12]
                        + "-"
                        + validator.nonce
                        + ".service"
                        for role in ("worker", "broker")
                    }
                    owned_cleanup = (
                        len(argv) == 3
                        and argv[:2] == ["/usr/bin/systemctl", "stop"]
                        and argv[-1] in own_units
                        and argv[-1] in units.values()
                    ) or (
                        len(argv) == 4
                        and argv[:2] == ["/usr/bin/systemctl", "show"]
                        and argv[-1] in own_units
                        and argv[-1] in units.values()
                        and argv[2]
                        in {
                            "--property=MainPID",
                            "--property=MainPID,ExecMainPID,InvocationID,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE",
                        }
                    )
                    if not owned_cleanup and not 0 <= self.now() - validator.issued <= 180:
                        raise ReleaseError("live_command_rejected")
                    allowed = False
                    if argv[:1] == ["/usr/bin/systemd-run"]:
                        names = [item.removeprefix("--unit=") for item in argv if item.startswith("--unit=")]
                        if (
                            len(names) == 1
                            and names[0] in units.values()
                            and names[0] not in own_units
                            and inventories.get(names[0]) is True
                            and resolved_policy is not None
                        ):
                            role = next(key for key, value in units.items() if value == names[0])
                            expected = object.__new__(validation_type)
                            expected.request, expected.work, expected.candidate = bindings, work, self.release
                            expected.nonce, expected.issued = validator.nonce, validator.issued
                            expected.preflight_only = network_preflight
                            expected.units = {
                                key: "prism-live-"
                                + key
                                + "-"
                                + self.request["commit"][:12]
                                + "-"
                                + expected.nonce
                                + ".service"
                                for key in ("worker", "broker")
                            }
                            expected.accounts = {
                                key: pwd.getpwnam(name)
                                for key, name in (
                                    ("worker", "prism-integration"),
                                    ("broker", "prism-integration-broker"),
                                )
                            }
                            expected.bridge_gid = grp.getgrnam("prism-integration-bridge").gr_gid
                            expected.pids = observed_pids
                            if role == "broker" or observed_pids.get("broker", 0) > 1:
                                allowed = argv == expected.unit_command(
                                    role, resolved_policy[0], resolved_policy[1]
                                )
                        if allowed:
                            own_units.add(names[0])
                    elif argv[:2] == ["/usr/bin/systemctl", "stop"]:
                        allowed = len(argv) == 3 and argv[2] in own_units
                    elif argv[:2] == ["/usr/bin/systemctl", "list-units"]:
                        allowed = (
                            len(argv) == 6
                            and argv[2:5] == ["--all", "--plain", "--no-legend"]
                            and argv[5] in units.values()
                            and argv[5] not in own_units
                        )
                    elif argv[:2] == ["/usr/bin/systemctl", "show"]:
                        allowed = (
                            len(argv) == 4
                            and argv[3] in own_units
                            and argv[2]
                            in {
                                "--property=MainPID",
                                "--property=MainPID,ExecMainPID,InvocationID,ExecMainStatus,Result,User,Group,ControlGroup,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateNetwork,KillMode,RuntimeMaxUSec,IPAddressDeny,IPAddressAllow,LimitCORE",
                            }
                        ) or argv == [
                            "/usr/bin/systemctl",
                            "show",
                            "--value",
                            "--property=MainPID",
                            SERVICE,
                        ]
                    elif argv[:1] == ["/usr/sbin/useradd"]:
                        allowed = argv[:-1] == [
                            "/usr/sbin/useradd",
                            "--system",
                            "--user-group",
                            "--no-create-home",
                            "--shell",
                            "/usr/sbin/nologin",
                        ] and argv[-1] in {"prism-integration", "prism-integration-broker"}
                    elif argv == ["/usr/sbin/groupadd", "--system", "prism-integration-bridge"]:
                        allowed = True
                    elif argv == LIVE_RESOLVE_COMMAND:
                        allowed = resolved_policy is None and not own_units
                    if not allowed:
                        raise ReleaseError("live_command_rejected")
                    # Read identity before stopping, then stop/reconcile only our
                    # launched nonce. Cleanup must survive budget expiry and STOP.
                    if not owned_cleanup:
                        self.stop()
                    output = self.run(argv, timeout=timeout)
                    if argv[:2] == ["/usr/bin/systemctl", "stop"]:
                        stopped_units.add(argv[-1])
                    if argv == LIVE_RESOLVE_COMMAND:
                        resolved_policy = module.network_policy(lambda: json.loads(output))
                    if argv[:2] == ["/usr/bin/systemctl", "list-units"]:
                        inventories[argv[-1]] = (
                            type(output) is bytes and len(output) <= 65536 and not output.strip()
                        )
                    if argv[:2] == ["/usr/bin/systemctl", "show"] and argv[-1] in own_units:
                        state = dict(
                            line.split("=", 1) for line in output.decode("ascii").splitlines() if "=" in line
                        )
                        if state.get("MainPID", "").isdigit() and int(state["MainPID"]) > 1:
                            role = next(key for key, value in units.items() if value == argv[-1])
                            previous = observed_pids.setdefault(role, int(state["MainPID"]))
                            if previous != int(state["MainPID"]):
                                raise ReleaseError("live_command_rejected")
                    return output

                def key_provider():
                    nonlocal key_error, key_provided
                    try:
                        binding = self.live_model_gate(work)
                        key = self.read_bound_model_key(work, binding)
                        key_provided = True
                        return key
                    except Exception as error:
                        key_error = failure_code(error)
                        raise

                bindings = {
                    "schema": 1,
                    "release_id": attempt,
                    "candidate": self.request["commit"],
                    "cli_sha256": (self.release / ".cli-revision").read_text().strip(),
                    "production_pid": baseline["production_pid"],
                    "sources": {
                        name: base64.b64encode(contents["live_integration_" + name + ".py"]).decode()
                        for name in LIVE_SOURCE_PINS
                    },
                }
                if network_preflight:
                    # The fixed preflight API has no key-provider parameter.
                    # Its constructor creates the same trusted validator; capture
                    # only that instance for exact owned-unit command validation.
                    original_validation = module.Validation

                    def preflight_validation(*args, **kwargs):
                        nonlocal validator
                        validator = original_validation(*args, **kwargs)
                        return validator

                    module.Validation = preflight_validation
                    try:
                        result = module.run_network_preflight(
                            bindings,
                            boundary=boundary,
                            run=run,
                            record=record,
                            network_gate_path=network_gate_path,
                        )
                    finally:
                        module.Validation = original_validation
                else:
                    validator = module.Validation(
                        bindings,
                        boundary=boundary,
                        run=run,
                        record=record,
                        key_provider=key_provider,
                        network_gate_path=network_gate_path,
                    )
                    self._live_validator = validator
                    result = validator.execute()
                returned = self.live_safe_record(result, attempt)
                if summary != returned:
                    raise ReleaseError("live_durable_result_required")
                if cancelled:
                    raise ReleaseError(cancelled)
                if original_state() != baseline:
                    raise ReleaseError("live_original_runtime_changed")
                if returned["passed"] is not True:
                    raise ReleaseError(key_error or boundary_error or "live_integration_failed")
                if len(own_units) != 2 or stopped_units != own_units:
                    raise ReleaseError("live_cleanup_not_observed")
                if not network_preflight and (
                    not key_provided or self._live_executor_memory_sealed is not True
                ):
                    raise ReleaseError("live_model_authorization_rejected")
                self.action_report = {
                    "attempt": attempt,
                    "summary": returned,
                    "events": count,
                    "last_event_sha256": chain,
                    "passed": True,
                    "production_verified": False,
                    "executor_memory_sealed": not network_preflight,
                    "model_key_read_after_gate": key_provided,
                    "preflight_only": network_preflight,
                    "network_gate_verified": returned["network_gate_verified"],
                }
        finally:
            self._live_attempt = None
            self._live_module = None
            self._live_validator = None
            self._live_key_attempted = True
            os.close(descriptor)
            self.write(
                directory / "receipt.json",
                encoded(
                    {
                        "schema": 1,
                        "attempt": attempt,
                        "candidate": self.request["commit"],
                        "helper_sha256": dict(LIVE_INTEGRATION_HELPERS),
                        "summary": summary,
                        "events": count,
                        "last_event_sha256": chain,
                        "operation": self.request["action"],
                        "preflight_only": network_preflight,
                        "baseline_kind": "network_metadata" if network_preflight else "business_runtime",
                        "runtime_fingerprint": baseline["runtime_fingerprint"],
                        "model_environment_binding_supplied": not network_preflight,
                        "executor_memory_sealed": getattr(self, "_live_executor_memory_sealed", False)
                        is True,
                        "model_key_read_after_gate": key_provided,
                        "production_verified": False,
                    }
                ),
            )

    def market_helper_sources(self):
        supplied = self.request.get("market_helpers")
        if type(supplied) is not dict or set(supplied) != set(MARKET_INTEGRATION_HELPERS):
            raise ReleaseError("approved_market_integration_helpers_required")
        contents = {}
        for name, approved in MARKET_INTEGRATION_HELPERS.items():
            item = supplied[name]
            if (
                type(item) is not dict
                or set(item) != {"sha256", "content"}
                or item["sha256"] != approved
                or type(item["content"]) is not str
                or len(item["content"]) > 180000
            ):
                raise ReleaseError("approved_market_integration_helpers_required")
            try:
                raw = base64.b64decode(item["content"], validate=True)
            except (ValueError, TypeError):
                raise ReleaseError("approved_market_integration_helpers_required") from None
            if not 0 < len(raw) <= 131072 or digest(raw) != approved:
                raise ReleaseError("approved_market_integration_helpers_required")
            contents[name] = raw
        return contents

    def market_sealed_sources(self, directory):
        self.checked(directory)
        if set(path.name for path in directory.iterdir()) != set(MARKET_INTEGRATION_HELPERS):
            raise ReleaseError("market_helper_source_rejected")
        for parent in (directory, *directory.parents):
            if parent == self.root.parent:
                break
            info = parent.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o022
                or self.root == Path("/")
                and info.st_uid != 0
            ):
                raise ReleaseError("market_helper_source_rejected")
        contents = {}
        for name, approved in MARKET_INTEGRATION_HELPERS.items():
            path = self.checked(directory / name)
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_size > 131072
                    or self.root == Path("/")
                    and info.st_uid != 0
                ):
                    raise ReleaseError("market_helper_source_rejected")
                raw = stream.read(131073)
                if (
                    self.cli_identity(info) != self.cli_identity(os.fstat(stream.fileno()))
                    or self.cli_identity(info) != self.cli_identity(path.lstat())
                    or digest(raw) != approved
                ):
                    raise ReleaseError("market_helper_source_rejected")
            contents[name] = raw
        return contents

    @contextmanager
    def market_module(self, contents):
        directory = self.checked(
            self.root / "opt/prism/validation/market" / digest(encoded(MARKET_INTEGRATION_HELPERS))
        )
        if not directory.exists():
            self.boundary("market_install_helpers")
            self.public_directory(directory.parent.parent)
            self.public_directory(directory.parent)
            directory.mkdir(mode=0o700)
            for name, raw in contents.items():
                self.write(directory / name, raw, 0o444)
            directory.chmod(0o555)
        sealed = self.market_sealed_sources(directory)
        module = types.ModuleType("reviewed_market_live_validation")
        module.__file__ = str(directory / "market_live_validation.py")
        exec(compile(sealed["market_live_validation.py"], module.__file__, "exec"), module.__dict__)
        if (
            MARKET_INTEGRATION_HELPERS["market_live_probe.py"] != module.PROBE_SHA256
            or module.WORKER_SHA256 != MARKET_WORKER_SHA256
            or module.CHECKS != MARKET_CHECKS
            or module.BOOT_CHECKS != MARKET_BOOT_CHECKS
            or {"qt.gtimg.cn", "money.finance.sina.com.cn"} != module.HOSTS
            or MARKET_INTEGRATION_HELPERS["cgroup_network_gate.py"] != module.NETWORK_GATE_SHA256
            or MARKET_INTEGRATION_HELPERS["systemd_unit_reference.py"] != module.UNIT_REFERENCE_SHA256
            or module.UNIT_REFERENCE_ERRORS != UNIT_REFERENCE_ERRORS
            or module.EXTRA_NEGATIVES != NETWORK_EXTRA_NEGATIVES
        ):
            raise ReleaseError("market_helper_source_rejected")
        yield module

    def market_original_state(self):
        runtime = self.runtime()
        receipts = self.request.get("receipts", {})
        if runtime["working_directory"] != str(self.production) or runtime["fingerprint"] != receipts.get(
            "expected_current_fingerprint"
        ):
            raise ReleaseError("market_original_runtime_changed")
        source_sha = self.source_inventory(self.request.get("source_manifest", {}), self.production)
        if source_sha != receipts.get("expected_source_manifest_sha256"):
            raise ReleaseError("market_original_source_changed")
        pid = self.process_id(SERVICE)
        if type(pid) is not int or pid <= 1:
            raise ReleaseError("market_original_runtime_changed")
        return {
            "production_pid": pid,
            "runtime_fingerprint": runtime["fingerprint"],
            "source_manifest_sha256": source_sha,
        }

    def market_safe_record(self, value, attempt, module, baseline):
        preflight = self.request.get("action") == "integration_market_network_preflight"

        def rejected():
            raise ReleaseError("market_safe_record_rejected")

        def integer(item, minimum=0):
            return type(item) is int and item >= minimum

        def digest_or_reject(item):
            if type(item) is not str or not DIGEST.fullmatch(item):
                rejected()

        if (
            type(value) is not dict
            or value.get("candidate") != self.request["commit"]
            or value.get("release_id") != attempt
        ):
            rejected()
        if set(value) == {"stage", "candidate", "release_id", "at"}:
            if (
                value["stage"]
                not in (
                    {"workspace", "start", "network_attach", "network_permit"}
                    | (set() if preflight else {"allow_public_http"})
                )
                or not integer(value["at"])
                or not 0 <= self.now() - value["at"] <= 300
            ):
                rejected()
            return json.loads(encoded(value))
        fixed = {
            "schema": 1,
            "probe": "credential_free_market_network_preflight"
            if preflight
            else "credential_free_market_linux",
            "scope": "pinned_tls_handshake_only" if preflight else "market_public_https_only",
            "preflight_only": preflight,
            "production_verified": False,
            "research_quality_verified": False,
            "live_model_verified": False,
            "native_search_verified": False,
            "native_fetch_verified": False,
        }
        required = {
            *fixed,
            *NETWORK_CLEANUP_FIELDS,
            "candidate",
            "release_id",
            "started_at",
            "finished_at",
            "passed",
            "actual_linux_collection",
            "original_runtime_unchanged",
            "market_provider_verified",
            "error_class",
            "unit_stopped",
            "network_gate_verified",
            "network_gate_verified_before_cleanup",
            "network_gate_cleaned",
            "negative_errno",
            "additional_network_errno",
            "cleanup_error_class",
        }
        optional = {
            "identity",
            "bootstrap_sha256",
            "os_denial_controls",
            "worker_result_sha256",
            "worker_result",
        }
        if (
            not required <= set(value) <= required | optional
            or any(
                type(value.get(key)) is not type(item) or value[key] != item for key, item in fixed.items()
            )
            or any(
                type(value[key]) is not bool
                for key in (
                    "passed",
                    "actual_linux_collection",
                    "original_runtime_unchanged",
                    "market_provider_verified",
                    "unit_stopped",
                    "network_gate_verified",
                    "network_gate_verified_before_cleanup",
                    "network_gate_cleaned",
                )
            )
            or not integer(value["started_at"])
            or not integer(value["finished_at"])
            or not value["started_at"] <= value["finished_at"] <= self.now()
            or not 0 <= self.now() - value["started_at"] <= 300
            or value["error_class"] not in module.ERRORS | {None}
            or value["cleanup_error_class"] not in {None, "cleanup_failed"}
        ):
            rejected()
        if not valid_network_cleanup_record(value):
            rejected()
        for name, keys in (
            (
                "negative_errno",
                {
                    "production_8088_denied",
                    "vibe_8899_denied",
                    "metadata_denied",
                    "private_ip_denied",
                    "other_public_denied",
                },
            ),
            ("additional_network_errno", NETWORK_EXTRA_NEGATIVES),
        ):
            codes = value[name]
            if (
                type(codes) is not dict
                or set(codes) not in (set(), keys)
                or any(type(code) is not int or not 0 <= code <= 4095 for code in codes.values())
            ):
                rejected()
        if (
            value["network_gate_verified"]
            != (
                value["network_gate_verified_before_cleanup"]
                and value["network_gate_cleaned"]
                and value["unit_stopped"]
                and value["cleanup_error_class"] is None
            )
            or value["network_gate_cleaned"]
            and not value["unit_stopped"]
            or value["network_gate_verified_before_cleanup"]
            and (
                not value["negative_errno"]
                or not value["additional_network_errno"]
                or any(
                    code not in {1, 13}
                    for codes in (value["negative_errno"], value["additional_network_errno"])
                    for code in codes.values()
                )
                or "os_denial_controls" not in value
                or type(value["os_denial_controls"]) is not dict
                or set(value["os_denial_controls"]) != MARKET_BOOT_CHECKS
                or any(flag is not True for flag in value["os_denial_controls"].values())
            )
            or preflight
            and (
                "worker_result" in value
                or "worker_result_sha256" in value
                or value["market_provider_verified"]
            )
        ):
            rejected()
        for name in ("bootstrap_sha256", "worker_result_sha256"):
            if name in value:
                digest_or_reject(value[name])
        if "os_denial_controls" in value:
            checks = value["os_denial_controls"]
            if (
                type(checks) is not dict
                or set(checks) != MARKET_BOOT_CHECKS
                or any(type(item) is not bool for item in checks.values())
            ):
                rejected()
        if "identity" in value:
            identity = value["identity"]
            if (
                type(identity) is not dict
                or set(identity)
                != {
                    "pid",
                    "startticks",
                    "invocation",
                    "uids",
                    "host_network_namespace",
                    "network_namespace_inode",
                    "private_readonly_run",
                }
                or any(
                    not integer(identity[key], 1) for key in ("pid", "startticks", "network_namespace_inode")
                )
                or type(identity["invocation"]) is not str
                or not re.fullmatch("[0-9a-f]{32}", identity["invocation"])
                or type(identity["uids"]) is not list
                or len(identity["uids"]) != 4
                or any(not integer(item, 1) for item in identity["uids"])
                or len(set(identity["uids"])) != 1
                or identity["host_network_namespace"] is not True
                or identity["private_readonly_run"] is not True
            ):
                rejected()
        worker = value.get("worker_result")
        if worker is not None:
            keys = {
                "schema",
                "binding",
                "checked_at",
                "passed",
                "market_provider_verified",
                "production_verified",
                "research_quality_verified",
                "live_model_verified",
                "native_search_verified",
                "native_fetch_verified",
                "error_class",
                "checks",
                "request_counts",
                "https",
                "cached_result_sha256",
                "kline_provenance_missing",
                "kline_adapter_is_tencent_actual_endpoint_is_sina",
                "failure_injection_disclosed",
                "fetched_at_is_not_trade_date",
            }
            if type(worker) is not dict or set(worker) != keys:
                rejected()
            binding = worker["binding"]
            if (
                type(binding) is not dict
                or set(binding)
                != {
                    "candidate",
                    "release_id",
                    "production_pid",
                    "pid",
                    "nonce",
                    "issued_at",
                    "probe_sha256",
                    "worker_sha256",
                }
                or binding["candidate"] != self.request["commit"]
                or binding["release_id"] != attempt
                or type(binding["production_pid"]) is not int
                or binding["production_pid"] != baseline["production_pid"]
                or not integer(binding["pid"], 2)
                or not integer(binding["issued_at"])
                or type(binding["nonce"]) is not str
                or not re.fullmatch("[0-9a-f]{32}", binding["nonce"])
                or binding["probe_sha256"] != MARKET_INTEGRATION_HELPERS["market_live_probe.py"]
                or binding["worker_sha256"] != MARKET_WORKER_SHA256
                or type(worker["schema"]) is not int
                or worker["schema"] != 1
                or not integer(worker["checked_at"])
                or not binding["issued_at"] <= worker["checked_at"] <= value["finished_at"]
                or any(
                    worker[name] is not False
                    for name in (
                        "production_verified",
                        "research_quality_verified",
                        "live_model_verified",
                        "native_search_verified",
                        "native_fetch_verified",
                    )
                )
                or any(type(worker[name]) is not bool for name in ("passed", "market_provider_verified"))
                or worker["error_class"] not in {None, "worker_failed", "market_acceptance_incomplete"}
                or type(worker["checks"]) is not dict
                or set(worker["checks"]) != MARKET_CHECKS
                or any(type(item) is not bool for item in worker["checks"].values())
                or type(worker["request_counts"]) is not dict
                or set(worker["request_counts"]) != {"quote", "kline"}
                or any(type(item) is not int or item != 1 for item in worker["request_counts"].values())
                or type(worker["kline_provenance_missing"]) is not list
                or any(
                    type(item) is not str or item not in {"source", "retrieved_at", "data_timestamp"}
                    for item in worker["kline_provenance_missing"]
                )
            ):
                rejected()
            for name in ("https", "cached_result_sha256"):
                if type(worker[name]) is not dict or not set(worker[name]) <= {"quote", "kline"}:
                    rejected()
            for details in worker["https"].values():
                if (
                    type(details) is not dict
                    or set(details)
                    != {"https_verified", "redirect_followed", "response_sha256", "response_bytes"}
                    or details["https_verified"] is not True
                    or details["redirect_followed"] is not False
                    or not integer(details["response_bytes"], 1)
                    or details["response_bytes"] > 524288
                ):
                    rejected()
                digest_or_reject(details["response_sha256"])
            for item in worker["cached_result_sha256"].values():
                digest_or_reject(item)
            if any(
                worker[name] is not True
                for name in (
                    "kline_adapter_is_tencent_actual_endpoint_is_sina",
                    "failure_injection_disclosed",
                    "fetched_at_is_not_trade_date",
                )
            ):
                rejected()
            provider_verified = (
                worker["checks"]["quote_real_provider"] and worker["checks"]["kline_real_provider"]
            )
            accepted = (
                all(worker["checks"].values())
                and not worker["kline_provenance_missing"]
                and set(worker["https"]) == {"quote", "kline"}
                and set(worker["cached_result_sha256"]) == {"quote", "kline"}
            )
            if (
                worker["market_provider_verified"] != provider_verified
                or worker["passed"] != accepted
                or value["market_provider_verified"] != provider_verified
            ):
                rejected()
        elif value["market_provider_verified"] is not False:
            rejected()
        if value["passed"] and (
            not preflight
            and (worker is None or worker["passed"] is not True)
            or set(value)
            != required | (optional - {"worker_result", "worker_result_sha256"} if preflight else optional)
            or any(
                value[name] is not True
                for name in ("actual_linux_collection", "original_runtime_unchanged", "unit_stopped")
            )
            or not all(value["os_denial_controls"].values())
            or value["error_class"] is not None
            or value["cleanup_error_class"] is not None
            or not value["network_gate_verified"]
            or not value["negative_errno"]
            or not value["additional_network_errno"]
            or any(
                code not in {1, 13}
                for codes in (value["negative_errno"], value["additional_network_errno"])
                for code in codes.values()
            )
        ):
            rejected()
        if not value["passed"] and value["error_class"] is None:
            rejected()
        return json.loads(encoded(value))

    def integration_market(self):
        return self._integration_market(network_preflight=False)

    def integration_market_network_preflight(self):
        self.network_preflight_request("market")
        return self._integration_market(network_preflight=True)

    def _integration_market(self, *, network_preflight):
        if self.request.get("action") != (
            "integration_market_network_preflight" if network_preflight else "integration_market"
        ):
            raise ReleaseError("network_preflight_request_rejected")
        self.verify_tree()
        contents = self.market_helper_sources()
        attempt = self.request.get("integration_attempt")
        if type(attempt) is not str or not re.fullmatch("[a-z0-9][a-z0-9-]{7,39}", attempt):
            raise ReleaseError("explicit_market_integration_attempt_required")
        original_state = self.network_original_state if network_preflight else self.market_original_state
        baseline = original_state()
        work = self.checked(self.operations / "integration" / attempt / "market")
        directory = self.checked(
            self.record
            / (("network-preflight-market-" if network_preflight else "integration-market-") + attempt)
        )
        if work.exists() or directory.exists():
            raise ReleaseError("market_integration_attempt_already_exists")
        self.boundary("market_attempt")
        directory.mkdir(mode=0o700)
        descriptor = os.open(
            directory / "events.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        summary, count, chain, cancelled, error_code = None, 0, digest(b""), None, None
        original_after, candidate_after = False, False
        launch, stopped, inventory_unit, inventory_clear = None, False, None, False
        try:
            for parent in (directory, self.record):
                fd = os.open(
                    self.checked(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            with self.market_module(contents) as module:
                network_gate_path = Path(module.__file__).with_name("cgroup_network_gate.py")
                bindings = {
                    "schema": 1,
                    "release_id": attempt,
                    "candidate": self.request["commit"],
                    "production_pid": baseline["production_pid"],
                    "probe_source": {
                        "sha256": MARKET_INTEGRATION_HELPERS["market_live_probe.py"],
                        "content": base64.b64encode(contents["market_live_probe.py"]).decode(),
                    },
                }

                def record(value):
                    nonlocal summary, count, chain
                    safe = self.market_safe_record(value, attempt, module, baseline)
                    if summary is not None or count >= 16:
                        raise ReleaseError("market_event_order_rejected")
                    entry = encoded({"sequence": count + 1, "previous_sha256": chain, "event": safe}) + b"\n"
                    if len(entry) > 16384:
                        raise ReleaseError("market_safe_record_rejected")
                    view = memoryview(entry)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise ReleaseError("market_event_write_failed")
                        view = view[written:]
                    os.fsync(descriptor)
                    count, chain = count + 1, digest(entry)
                    if "finished_at" in safe:
                        summary = safe

                def boundary(label):
                    nonlocal cancelled
                    if (
                        label not in MARKET_BOUNDARIES
                        or network_preflight
                        and label in {"market_allow_public_http", "market_wait_result", "market_wait_exit"}
                    ):
                        raise module.ValidationError("supervision_failed")
                    try:
                        self.boundary(label)
                        if original_state() != baseline:
                            raise ReleaseError("market_original_runtime_changed")
                        if label == "market_allow_public_http":
                            self.verify_tree()
                    except Exception as error:
                        code = failure_code(error)
                        if code in {
                            "operator_stop",
                            "fresh_operator_permit_required",
                            "permit_timeout",
                            "controller_disconnected",
                        }:
                            cancelled = code
                            raise module.ValidationError("operator_stop") from None
                        raise module.ValidationError("supervision_failed") from None

                def run(argv, *, timeout):
                    nonlocal launch, stopped, inventory_unit, inventory_clear
                    if (
                        type(argv) is not list
                        or not argv
                        or any(type(item) is not str or "\0" in item for item in argv)
                        or type(timeout) not in {int, float}
                        or not 0 < timeout <= 15
                    ):
                        raise ReleaseError("market_command_rejected")
                    allowed = False
                    if argv[:2] == ["/usr/bin/systemctl", "list-units"]:
                        allowed = (
                            len(argv) == 6
                            and argv[:5]
                            == ["/usr/bin/systemctl", "list-units", "--all", "--plain", "--no-legend"]
                            and re.fullmatch(
                                "prism-market-probe-"
                                + self.request["commit"][:12]
                                + "-[0-9a-f]{32}\\.service",
                                argv[5],
                            )
                            and inventory_unit in {None, argv[5]}
                            and launch is None
                        )
                        if allowed:
                            inventory_unit = argv[5]
                    elif argv[:1] == ["/usr/bin/systemd-run"] and launch is None:
                        try:
                            config = json.loads(argv[-1])
                            fixed = {
                                "candidate": self.request["commit"],
                                "release_id": attempt,
                                "production_pid": baseline["production_pid"],
                                "work": str(work),
                                "probe_sha256": MARKET_INTEGRATION_HELPERS["market_live_probe.py"],
                                "worker_sha256": MARKET_WORKER_SHA256,
                                "preflight_only": network_preflight,
                                "network_gate_sha256": MARKET_INTEGRATION_HELPERS["cgroup_network_gate.py"],
                                "network_role": "market",
                            }
                            if (
                                type(config) is not dict
                                or set(config)
                                != {*fixed, "nonce", "issued_at", "pins", "negative_public", "network_pins"}
                                or any(
                                    type(config.get(key)) is not type(item) or config[key] != item
                                    for key, item in fixed.items()
                                )
                                or type(config["nonce"]) is not str
                                or not re.fullmatch("[0-9a-f]{32}", config["nonce"])
                                or type(config["issued_at"]) is not int
                                or not 0 <= self.now() - config["issued_at"] <= 65
                                or type(config["pins"]) is not dict
                                or set(config["pins"]) != module.HOSTS
                                or any(
                                    type(values) is not list or len(values) != 1 or type(values[0]) is not str
                                    for values in config["pins"].values()
                                )
                            ):
                                raise ValueError
                            pins, negative = module.network_policy(
                                resolve=lambda host, *_args, **_kwargs: [
                                    (None, None, None, None, (config["pins"][host][0], 443))
                                ]
                            )
                            if (
                                pins != config["pins"]
                                or negative != config["negative_public"]
                                or config["network_pins"]
                                != sorted({value for values in pins.values() for value in values})
                            ):
                                raise ValueError
                            expected = object.__new__(module.Validation)
                            expected.request, expected.work, expected.candidate = bindings, work, self.release
                            expected.nonce, expected.issued = config["nonce"], config["issued_at"]
                            expected.preflight_only = network_preflight
                            expected.pins, expected.negative = pins, negative
                            expected.unit = (
                                "prism-market-probe-"
                                + self.request["commit"][:12]
                                + "-"
                                + expected.nonce
                                + ".service"
                            )
                            allowed = (
                                argv == expected.command()
                                and inventory_clear
                                and expected.unit == inventory_unit
                            )
                            if allowed:
                                launch = expected.unit
                        except (ValueError, TypeError, KeyError, module.ValidationError):
                            allowed = False
                    elif (
                        argv
                        == [
                            "/usr/bin/systemctl",
                            "show",
                            "--property=MainPID,InvocationID,WorkingDirectory",
                            SERVICE,
                        ]
                        or launch is not None
                        and argv == ["/usr/bin/systemctl", "stop", launch]
                        or launch is not None
                        and argv
                        == [
                            "/usr/bin/systemctl",
                            "show",
                            "--property=" + MARKET_UNIT_PROPERTIES,
                            launch,
                        ]
                    ):
                        allowed = True
                    if not allowed:
                        raise ReleaseError("market_command_rejected")
                    # Cleanup of this nonce remains available after STOP; no new work.
                    if not (
                        launch is not None
                        and (
                            argv == ["/usr/bin/systemctl", "stop", launch]
                            or argv
                            == ["/usr/bin/systemctl", "show", "--property=" + MARKET_UNIT_PROPERTIES, launch]
                        )
                    ):
                        self.stop()
                    output = self.run(argv, timeout=timeout)
                    if argv[:2] == ["/usr/bin/systemctl", "list-units"]:
                        inventory_clear = type(output) is bytes and not output.strip()
                    if argv[:2] == ["/usr/bin/systemctl", "stop"]:
                        stopped = True
                    return output

                returned = self.market_safe_record(
                    (module.run_network_preflight if network_preflight else module.run_validation)(
                        bindings,
                        boundary=boundary,
                        run=run,
                        record=record,
                        network_gate_path=network_gate_path,
                    ),
                    attempt,
                    module,
                    baseline,
                )
                if summary != returned:
                    raise ReleaseError("market_durable_result_required")
                if returned["passed"] and (launch is None or not stopped):
                    raise ReleaseError("market_cleanup_not_observed")
                self.boundary("market_reconcile")
                original_after = original_state() == baseline
                self.verify_tree()
                candidate_after = True
                self.action_report = {
                    "attempt": attempt,
                    "summary": returned,
                    "events": count,
                    "last_event_sha256": chain,
                    "market_provider_verified": returned["market_provider_verified"],
                    "passed": returned["passed"],
                    "production_verified": False,
                    "preflight_only": network_preflight,
                    "network_gate_verified": returned["network_gate_verified"],
                }
                if cancelled:
                    raise ReleaseError(cancelled)
                if not original_after:
                    raise ReleaseError("market_original_runtime_changed")
                if returned["passed"] is not True:
                    raise ReleaseError("market_acceptance_incomplete")
        except Exception as error:
            error_code = failure_code(error)
            raise
        finally:
            os.close(descriptor)
            try:
                original_after = original_state() == baseline
            except Exception:
                original_after = False
            try:
                self.verify_tree()
                candidate_after = True
            except Exception:
                candidate_after = False
            reconciliation_error = error_code is None and (not original_after or not candidate_after)
            if reconciliation_error:
                error_code = "market_final_reconciliation_failed"
            receipt = {
                "schema": 1,
                "attempt": attempt,
                "candidate": self.request["commit"],
                "helper_sha256": dict(MARKET_INTEGRATION_HELPERS),
                "operation": self.request["action"],
                "preflight_only": network_preflight,
                "baseline_kind": "network_metadata" if network_preflight else "business_runtime",
                "runtime_fingerprint": baseline["runtime_fingerprint"],
                "summary": summary,
                "events": count,
                "last_event_sha256": chain,
                "original_runtime_unchanged": original_after,
                "candidate_unchanged": candidate_after,
                "passed": error_code is None
                and summary is not None
                and summary["passed"] is True
                and original_after
                and candidate_after,
                "error_class": error_code,
                "production_verified": False,
            }
            self.write(directory / "receipt.json", encoded(receipt))
            for parent in (directory, self.record):
                fd = os.open(
                    self.checked(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            if reconciliation_error:
                raise ReleaseError(error_code)

    def integration_identity(self, unit, production_pid, uid):
        pid = self.process_id(unit)
        process = self.root / "proc" / str(pid)
        production = self.root / "proc" / str(production_pid)
        if (
            pid == production_pid
            or self.process_id(SERVICE) != production_pid
            or self.run(["/usr/bin/systemctl", "show", "--value", "--property=User", unit]).strip()
            != b"prism-integration"
            or self.kernel_uids(process) != (uid,) * 4
            or (process / "ns/net").readlink() == (production / "ns/net").readlink()
        ):
            raise ReleaseError("integration_process_identity_rejected")
        stage_uid = pwd.getpwnam("prism-stage").pw_uid
        namespace = (process / "ns/net").readlink()
        for other in (self.root / "proc").iterdir():
            if not other.name.isdigit():
                continue
            values = self.kernel_uids(other)
            if values is not None and stage_uid in values:
                try:
                    if (other / "ns/net").readlink() == namespace:
                        raise ReleaseError("integration_stage_namespace_shared")
                except FileNotFoundError:
                    if other.exists():
                        raise ReleaseError("integration_process_identity_rejected") from None
        if self.stage_runtime_mount(process) is not True:
            raise ReleaseError("integration_private_runtime_mount_required")
        if (
            self.process_id(unit) != pid
            or self.process_id(SERVICE) != production_pid
            or self.kernel_uids(process) != (uid,) * 4
            or (process / "ns/net").readlink() != namespace
            or namespace == (production / "ns/net").readlink()
        ):
            raise ReleaseError("integration_process_identity_rejected")
        return {
            "pid": pid,
            "production_pid": production_pid,
            "dedicated_user": True,
            "separate_network_namespace": True,
            "separate_stage_namespace": True,
            "read_only_private_runtime_mount": True,
            "checked_at": int(self.now()),
        }

    def integration_read(self, path, uid):
        descriptor = os.open(self.checked(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != uid
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > 16384
            ):
                raise ReleaseError("integration_record_rejected")
            raw = source.read(16385)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ReleaseError("integration_record_rejected") from None
        if type(value) is not dict or len(raw) > 16384:
            raise ReleaseError("integration_record_rejected")
        return value, digest(raw)

    def integration_proof(self, work, uid, expected):
        proof, sha = self.integration_read(work / "output/bootstrap-proof.json", uid)
        if (
            set(proof) != {*expected, "checked_at", "checks"}
            or any(
                type(proof.get(key)) is not type(value) or proof[key] != value
                for key, value in expected.items()
            )
            or type(proof.get("checked_at")) is not int
            or not expected["issued_at"] <= proof["checked_at"] <= self.now() <= expected["issued_at"] + 120
            or type(proof.get("checks")) is not dict
            or set(proof["checks"]) != INTEGRATION_BOOT_CHECKS
            or any(value is not True for value in proof["checks"].values())
        ):
            raise ReleaseError("integration_bootstrap_proof_rejected")
        return proof, sha

    def integration_result(self, action, work, uid, expected, cli_sha):
        filename = "result.json" if action == "integration_astock" else "cli-result.json"
        value, sha = self.integration_read(work / "output" / filename, uid)
        identity = {key: expected[key] for key in ("schema", "candidate", "nonce", "pid", "probe_sha256")}
        if action == "integration_astock":
            identity.update(
                engine=expected["engine"],
                production_pid=expected["production_pid"],
                probe="astock_import_tls",
                network_namespace_verified=False,
                live_model_verified=False,
                live_market_verified=False,
            )
            errors = {
                "identity_rejected",
                "credential_environment_rejected",
                "bootstrap_rejected",
                "astock_import_rejected",
                "tls_verification_rejected",
                "probe_execution_failed",
                "private_result_write_failed",
                "invalid_arguments",
            }
            extra = {"error_detail"}
        else:
            identity.update(
                cli_sha256=cli_sha,
                probe="cli_protocol_fixture",
                fixture_model_configuration_override=True,
                live_model_verified=False,
                native_search_verified=False,
                native_fetch_verified=False,
            )
            errors = {
                "cli_protocol_fixture_failed",
                "same_pid_bootstrap_required",
                "synthetic_cleanup_failed",
                "private_result_write_failed",
            }
            extra = {"cli_protocol_fixture_verified", "requests"}
        allowed = {*identity, "checked_at", "passed", "checks", "error_class", *extra}
        checks = value.get("checks")
        if (
            set(value) != allowed
            or any(
                type(value.get(key)) is not type(item) or value[key] != item for key, item in identity.items()
            )
            or type(value.get("checked_at")) is not int
            or not expected["issued_at"] <= value["checked_at"] <= self.now() <= expected["issued_at"] + 120
            or type(value.get("passed")) is not bool
            or type(checks) is not dict
            or not set(checks).issubset(INTEGRATION_RESULT_CHECKS[action])
            or any(type(item) is not bool for item in checks.values())
            or (
                value.get("error_class") is not None
                and (type(value["error_class"]) is not str or value["error_class"] not in errors)
            )
            or (
                value["passed"]
                and (
                    set(checks) != INTEGRATION_RESULT_CHECKS[action]
                    or not all(checks.values())
                    or value["error_class"] is not None
                )
            )
        ):
            raise ReleaseError("integration_result_rejected")
        if action == "integration_astock":
            detail = value.get("error_detail")
            if (
                (detail is not None and (type(detail) is not str or detail not in ASTOCK_ERROR_DETAILS))
                or (value["passed"] and detail is not None)
                or (not value["passed"] and detail is None)
            ):
                raise ReleaseError("integration_result_rejected")
        if action == "integration_cli":
            requests = value.get("requests")
            if (
                type(value.get("cli_protocol_fixture_verified")) is not bool
                or value["cli_protocol_fixture_verified"] != value["passed"]
                or type(requests) is not dict
                or not set(requests).issubset({"stream_new", "stream_resume", "auxiliary"})
                or any(
                    type(row) is not dict
                    or set(row) != {"messages", "count_tokens"}
                    or any(type(item) is not int or not 0 <= item <= 8 for item in row.values())
                    for row in requests.values()
                )
                or sum(sum(row.values()) for row in requests.values()) > 24
                or (
                    value["passed"]
                    and (
                        set(requests) != {"stream_new", "stream_resume", "auxiliary"}
                        or any(row["messages"] < 1 for row in requests.values())
                    )
                )
            ):
                raise ReleaseError("integration_result_rejected")
        return value, sha

    def integration_command(
        self, action, unit, work, engine_sha, cli_sha, nonce, production_pid, issued, source_sha
    ):
        arguments = [
            "/usr/bin/systemd-run",
            "--quiet",
            "--unit=" + unit,
            "--property=Type=exec",
            "--property=RemainAfterExit=yes",
            "--property=User=prism-integration",
            "--property=Group=prism-integration",
            "--property=SupplementaryGroups=",
            "--property=WorkingDirectory=" + str(work),
            "--property=PrivateNetwork=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectHome=yes",
            "--property=ProtectProc=invisible",
            "--property=ProcSubset=pid",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=TemporaryFileSystem=/run:ro",
            "--property=InaccessiblePaths=-" + str(self.operations / "staging"),
            "--property=UMask=0077",
            "--property=ReadWritePaths="
            + " ".join(str(work / name) for name in ("output", "home", "tmp", "data")),
            "--property=MemoryMax=768M",
            "--property=TasksMax=32",
            "--property=CPUQuota=100%",
            "--property=RuntimeMaxSec=60",
            "--property=KillMode=control-group",
            "--property=StandardOutput=null",
            "--property=StandardError=null",
            "/usr/bin/env",
            "-i",
            "PATH=/usr/bin:/bin",
            "HOME=" + str(work / "home"),
            "TMPDIR=" + str(work / "tmp"),
            "PYTHONDONTWRITEBYTECODE=1",
            "LANG=C.UTF-8",
            "TZ=Asia/Shanghai",
            str(self.release / ".venv/bin/python"),
            "-I",
            "-B",
            "-S",
        ]
        if action == "integration_astock":
            arguments += [
                str(work / "probe.py"),
                "--candidate",
                self.request["commit"],
                "--engine",
                engine_sha,
                "--workdir",
                str(work),
                "--nonce",
                nonce,
                "--production-pid",
                str(production_pid),
                "--issued-at",
                str(issued),
                "--probe-sha256",
                source_sha,
            ]
        else:
            arguments += [
                "-c",
                CLI_INTEGRATION_BOOTSTRAP,
                self.request["commit"],
                engine_sha,
                cli_sha,
                str(work),
                nonce,
                str(production_pid),
                str(issued),
                source_sha,
            ]
        return arguments

    def integration_astock(self):
        self.integration("integration_astock")

    def integration_cli(self):
        self.integration("integration_cli")

    def integration_launch_syntax(self, work):
        """Parse the fixed deployment command without opening real config."""
        content = self.dropin(self.release)
        environment = work / "synthetic-environment"
        unit = work / "deployment-contract.service"
        marker = b"EnvironmentFile=" + str(self.production / ".env").encode() + b"\n"
        if content.count(marker) != 1:
            raise ReleaseError("deployment_contract_rejected")
        values = (
            "CLAUDE_BIN",
            "STOCK_DATA_DIR",
            "STOCK_LOG_FILE",
            "PRISM_INTERNAL_API_URL",
            "PRISM_ASTOCK_PROJECT",
            "PRISM_SINGLE_WORKER",
            "PRISM_ENVIRONMENT",
            "PYTHONDONTWRITEBYTECODE",
        )
        self.write(
            environment, "".join(name + "=/synthetic-conflicting-setting\n" for name in values).encode()
        )
        content = content.replace(marker, b"EnvironmentFile=" + str(environment).encode() + b"\n")
        unit_content = b"[Unit]\nDescription=Prism fixed deployment syntax contract\n" + content
        self.write(unit, unit_content)
        self.boundary("integration_validate_launch")
        self.run(["/usr/bin/systemd-analyze", "verify", str(unit)], timeout=15)
        return {
            "systemd_syntax_verified": True,
            "synthetic_environment_only": True,
            "unit_started": False,
            "unit_sha256": digest(unit_content),
            "fixed_constraints": list(values),
        }

    def integration(self, action):
        self.verify_tree()
        kind, source_sha = INTEGRATION_PROBES[action]
        attempt = self.request.get("integration_attempt", self.request["release_id"])
        if not isinstance(attempt, str) or not RELEASE_ID.fullmatch(attempt):
            raise ReleaseError("integration_attempt_rejected")
        try:
            source = base64.b64decode(self.request.get("integration_probe", ""), validate=True)
        except (ValueError, TypeError):
            raise ReleaseError("approved_integration_probe_required") from None
        if (
            self.request.get("integration_probe_sha256") != source_sha
            or not 0 < len(source) <= 131072
            or digest(source) != source_sha
        ):
            raise ReleaseError("approved_integration_probe_required")
        account = self.integration_account()
        self.integration_idle(account.pw_uid)
        production_pid = self.process_id(SERVICE)
        if Path(os.readlink(self.root / "proc" / str(production_pid) / "cwd")) != self.production:
            raise ReleaseError("integration_original_runtime_required")
        engine_sha = (self.release / ".engine-revision").read_text().strip()
        cli_sha = (self.release / ".cli-revision").read_text().strip()
        if not DIGEST.fullmatch(engine_sha) or not DIGEST.fullmatch(cli_sha):
            raise ReleaseError("integration_candidate_identity_rejected")
        work = self.checked(self.operations / "integration" / attempt / kind)
        receipt = self.checked(self.record / ("integration-" + kind + "-" + attempt + ".json"))
        if work.exists() or receipt.exists():
            raise ReleaseError("integration_attempt_already_exists")
        self.boundary("integration_workspace")
        self.public_directory(work.parent.parent)
        self.public_directory(work.parent)
        work.mkdir(mode=0o755)
        for name in ("output", "home", "tmp", "data"):
            directory = work / name
            directory.mkdir(mode=0o700)
            os.chown(directory, account.pw_uid, account.pw_gid)
        self.write(work / "probe.py", source, 0o644)
        nonce, issued = secrets.token_hex(16), int(self.now())
        unit = (
            "prism-integration-"
            + self.request["commit"][:12]
            + ("-astock-" if action == "integration_astock" else "-cli-")
            + nonce[:8]
            + ".service"
        )
        expected = {
            "schema": 1,
            "candidate": self.request["commit"],
            "engine": engine_sha,
            "nonce": nonce,
            "production_pid": production_pid,
            "issued_at": issued,
            "probe_sha256": source_sha,
        }
        report = {
            "schema": 1,
            "action": action,
            "commit": self.request["commit"],
            "attempt": attempt,
            "probe_sha256": source_sha,
            "passed": False,
            "production_verified": False,
            "live_model_verified": False,
            "live_market_verified": False,
            "native_search_verified": False,
            "native_fetch_verified": False,
            "unit_stopped": False,
        }
        launched = False
        try:
            report["deployment_contract"] = self.integration_launch_syntax(work)
            self.boundary("integration_start")
            self.integration_idle(account.pw_uid)
            if self.process_id(SERVICE) != production_pid:
                raise ReleaseError("integration_production_process_changed")
            launched = True
            self.run(
                self.integration_command(
                    action, unit, work, engine_sha, cli_sha, nonce, production_pid, issued, source_sha
                ),
                timeout=15,
            )
            deadline = time.monotonic() + 15
            while not (work / "output/bootstrap-proof.json").exists():
                self.stop()
                if time.monotonic() >= deadline:
                    raise ReleaseError("integration_bootstrap_timeout")
                time.sleep(0.05)
            identity = self.integration_identity(unit, production_pid, account.pw_uid)
            expected["pid"] = identity["pid"]
            proof, proof_sha = self.integration_proof(work, account.pw_uid, expected)
            self.boundary("integration_allow_imports")
            if self.integration_identity(unit, production_pid, account.pw_uid)["pid"] != identity["pid"]:
                raise ReleaseError("integration_process_identity_rejected")
            # Re-read untrusted output immediately before the root-owned gate.
            if self.integration_proof(work, account.pw_uid, expected)[1] != proof_sha:
                raise ReleaseError("integration_bootstrap_proof_changed")
            allow = {
                key: expected[key]
                for key in ("schema", "candidate", "engine", "nonce", "pid", "probe_sha256")
            }
            allow.update(bootstrap_proof_sha256=proof_sha, allowed_at=int(self.now()))
            self.write(work / "root-allow.json", encoded(allow), 0o444)
            report.update(
                identity=identity, bootstrap_proof_sha256=proof_sha, bootstrap_checks=proof["checks"]
            )
            deadline = time.monotonic() + 65
            while True:
                self.stop()
                raw = self.run(
                    ["/usr/bin/systemctl", "show", "--property=MainPID,ExecMainStatus,Result", unit]
                )
                status = dict(line.split("=", 1) for line in raw.decode("ascii").splitlines() if "=" in line)
                if status.get("MainPID") == "0":
                    break
                if status.get("MainPID") != str(identity["pid"]) or time.monotonic() >= deadline:
                    raise ReleaseError("integration_execution_timeout_or_identity_changed")
                time.sleep(0.1)
            result, result_sha = self.integration_result(action, work, account.pw_uid, expected, cli_sha)
            report.update(result=result, result_sha256=result_sha)
            if (
                status.get("ExecMainStatus") != "0"
                or status.get("Result") != "success"
                or result["passed"] is not True
            ):
                raise ReleaseError("integration_probe_failed")
            report["passed"] = True
        except Exception as error:
            report["failure_code"] = failure_code(error)
            raise
        finally:
            try:
                if launched:
                    self.run(["/usr/bin/systemctl", "stop", unit], timeout=15)
                    self.integration_idle(account.pw_uid)
                    report["unit_stopped"] = True
            except Exception:
                report["passed"] = False
                report["failure_code"] = "integration_cleanup_unconfirmed"
                raise ReleaseError("integration_cleanup_unconfirmed") from None
            finally:
                report["checked_at"] = int(self.now())
                self.action_report = report
                self.write(receipt, encoded(report))

    def retirement_sources(self, *, stop=False):
        approved_helpers = RETIREMENT_STOP_HELPERS if stop else RETIREMENT_HELPERS
        supplied = self.request.get("retirement_stop_helpers" if stop else "retirement_helpers")
        if type(supplied) is not dict or set(supplied) != set(approved_helpers):
            raise ReleaseError("approved_retirement_helpers_required")
        contents = {}
        for filename, approved in approved_helpers.items():
            item = supplied[filename]
            if (
                type(item) is not dict
                or set(item) != {"sha256", "content"}
                or item["sha256"] != approved
                or type(item["content"]) is not str
                or len(item["content"]) > 180000
            ):
                raise ReleaseError("approved_retirement_helpers_required")
            try:
                raw = base64.b64decode(item["content"], validate=True)
            except ValueError:
                raise ReleaseError("approved_retirement_helpers_required") from None
            if not 0 < len(raw) <= 131072 or digest(raw) != approved:
                raise ReleaseError("approved_retirement_helpers_required")
            contents[filename] = raw
        return contents

    def retirement_sealed_sources(self, directory, *, stop=False):
        approved_helpers = RETIREMENT_STOP_HELPERS if stop else RETIREMENT_HELPERS
        for path, names in ((directory, {"scripts"}), (directory / "scripts", set(approved_helpers))):
            self.checked(path)
            info = path.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o555
                or (self.root == Path("/") and info.st_uid != 0)
                or {item.name for item in path.iterdir()} != names
            ):
                raise ReleaseError("retirement_helper_directory_rejected")
        contents = {}
        for filename, expected in approved_helpers.items():
            path = self.checked(directory / "scripts" / filename)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o444
                    or info.st_nlink != 1
                    or info.st_size > 131072
                    or (self.root == Path("/") and info.st_uid != 0)
                ):
                    raise ReleaseError("retirement_helper_file_rejected")
                raw = source.read(131073)
                if (
                    digest(raw) != expected
                    or self.cli_identity(info) != self.cli_identity(os.fstat(source.fileno()))
                    or self.cli_identity(info) != self.cli_identity(path.lstat())
                ):
                    raise ReleaseError("retirement_helper_source_changed")
            contents[filename] = raw
        return contents

    def install_retirement_helpers(self, contents, *, stop=False):
        approved_helpers = RETIREMENT_STOP_HELPERS if stop else RETIREMENT_HELPERS
        directory = self.checked(
            self.root / "opt/prism/validation/helpers" / digest(encoded(approved_helpers))
        )
        if not directory.exists():
            self.boundary("retirement_install_helpers")
            self.public_directory(directory.parent.parent)
            self.public_directory(directory.parent)
            directory.mkdir(mode=0o700)
            (directory / "scripts").mkdir(mode=0o700)
            for filename, raw in contents.items():
                self.write(directory / "scripts" / filename, raw, 0o444)
            (directory / "scripts").chmod(0o555)
            directory.chmod(0o555)
        self.retirement_sealed_sources(directory, stop=stop)
        return directory

    @contextmanager
    def retirement_modules(self, directory, *, stop=False):
        approved_helpers = RETIREMENT_STOP_HELPERS if stop else RETIREMENT_HELPERS
        names = ("scripts", *("scripts." + name.removesuffix(".py") for name in approved_helpers))
        if any(name in sys.modules for name in names):
            raise ReleaseError("retirement_module_namespace_rejected")
        contents = self.retirement_sealed_sources(directory, stop=stop)
        package = types.ModuleType("scripts")
        package.__path__ = [str(directory / "scripts")]
        sys.modules["scripts"] = package
        try:
            modules = {}
            for filename in approved_helpers:
                short = filename.removesuffix(".py")
                module = types.ModuleType("scripts." + short)
                module.__file__, module.__package__ = str(directory / "scripts" / filename), "scripts"
                setattr(package, short, module)
                sys.modules[module.__name__] = module
                exec(compile(contents[filename], module.__file__, "exec"), module.__dict__)
                modules[short] = module
            yield tuple(modules[name.removesuffix(".py")] for name in approved_helpers)
        finally:
            for name in reversed(names):
                sys.modules.pop(name, None)

    def retirement_baseline(self):
        runtime = self.runtime()
        receipts = self.request["receipts"]
        if runtime["working_directory"] != str(self.production) or runtime["fingerprint"] != receipts.get(
            "expected_current_fingerprint"
        ):
            raise ReleaseError("retirement_original_runtime_changed")
        source_sha = self.source_inventory(self.request.get("source_manifest", {}), self.production)
        if source_sha != receipts.get("expected_source_manifest_sha256"):
            raise ReleaseError("retirement_original_source_changed")
        base_sha = receipts.get("dependencies", {}).get("base_environment_sha256")
        if base_sha != RETIREMENT_BASE_ENV_SHA256 or tree_digest(self.old_venv)[0] != base_sha:
            raise ReleaseError("retirement_original_environment_changed")
        return {
            "production_pid": self.process_id(SERVICE),
            "runtime_fingerprint": runtime["fingerprint"],
            "source_manifest_sha256": source_sha,
            "base_environment_sha256": base_sha,
        }

    def retirement_case_result(self, value):
        keys = {
            "case_id",
            "passed",
            "clean_exit_zero",
            "executor_results_preserved",
            "expected_counterexample",
            "production_quiescence_proven",
            "task_census_still_required",
        }
        if (
            type(value) is not dict
            or set(value) != keys
            or type(value.get("case_id")) is not str
            or value["case_id"] not in RETIREMENT_CASE_IDS
            or any(type(item) is not bool for key, item in value.items() if key != "case_id")
            or value["production_quiescence_proven"] is not False
            or value["task_census_still_required"] is not True
        ):
            raise ReleaseError("retirement_safe_result_rejected")

    def retirement_summary(self, value, bindings, probe, validation):
        fixed = {
            "schema": 1,
            "bindings": bindings,
            "production_quiescence_proven": False,
            "task_census_still_required": True,
            "actual_linux_collection": True,
            "validates_single_main_signal_only": True,
            "systemctl_stop_end_to_end_proven": False,
            "probe_source_sha256": RETIREMENT_HELPERS["retirement_probe.py"],
            "python_minor": "3.12",
            "pinned_versions": dict(probe.VERSIONS),
            "uvicorn_server_sha256": probe.UVICORN_SHA256,
            "uvloop_binary_sha256": probe.UVLOOP_SHA256,
            "bundle_sha256": probe.bundle_identity(),
            "wrapper_sha256": validation.wrapper_directory().name,
        }
        keys = {*fixed, "retirement_validation_passed", "cases", "synthetic_cleanup_verified"}
        if (
            type(value) is not dict
            or set(value)
            not in (keys | {"actual_linux_acceptance_requires_root_collection"}, keys | {"error_code"})
            or any(
                type(value.get(key)) is not type(item) or value[key] != item for key, item in fixed.items()
            )
            or type(value.get("retirement_validation_passed")) is not bool
            or type(value.get("synthetic_cleanup_verified")) is not bool
            or type(value.get("cases")) is not list
            or len(value["cases"]) > 7
        ):
            raise ReleaseError("retirement_safe_result_rejected")
        for case in value["cases"]:
            self.retirement_case_result(case)
        case_ids = [case["case_id"] for case in value["cases"]]
        if len(case_ids) != len(set(case_ids)):
            raise ReleaseError("retirement_safe_result_rejected")
        if "error_code" in value:
            if (
                type(value["error_code"]) is not str
                or not re.fullmatch(r"retirement_[a-z0-9_]{1,80}", value["error_code"])
                or value["retirement_validation_passed"]
            ):
                raise ReleaseError("retirement_safe_result_rejected")
        elif value["actual_linux_acceptance_requires_root_collection"] is not True:
            raise ReleaseError("retirement_safe_result_rejected")
        if value["retirement_validation_passed"] and (
            set(case_ids) != RETIREMENT_CASE_IDS
            or not all(case["passed"] for case in value["cases"])
            or not value["synthetic_cleanup_verified"]
        ):
            raise ReleaseError("retirement_safe_result_rejected")
        return value

    def retirement_event(self, event, value, bindings, probe, validation):
        if event == "retirement_validation_result":
            return self.retirement_summary(value, bindings, probe, validation)
        if event == "synthetic_case_result":
            self.retirement_case_result(value)
            return value
        fields = {
            "bundle_copy_intent": {"bundle_sha256": "digest"},
            "public_bundles_verified": {"bundle_sha256": "digest", "wrapper_sha256": "digest"},
            "synthetic_start_intent": {"case_id": "case", "nonce": "nonce"},
            "actual_isolation_verified": {
                "case_id": "case",
                "invocation_id": "nonce",
                "main_pid": "number",
                "start_ticks": "number",
                "uid": "number",
                "control_group": "group",
                "actual_private_network_and_mounts": "true",
                "runtime_bind_same_inode": "true",
                "production_directory_access_denied": "true",
                "inet_socket_creation_denied": "true",
            },
            "synthetic_signal_intent": {
                "case_id": "case",
                "invocation_id": "nonce",
                "signal": "signal",
                "signal_at_monotonic_us": "number",
            },
            "synthetic_runner_join_observed": {
                "case_id": "case",
                "server_unwound_with_executor_pending": "true",
            },
            "synthetic_exit_observed": {
                "case_id": "case",
                "recursive_member_count": "number",
                "exit_code": "number",
                "exit_status": "number",
            },
        }.get(event)
        if fields is None or type(value) is not dict or set(value) != set(fields):
            raise ReleaseError("retirement_safe_event_rejected")
        for key, category in fields.items():
            item = value[key]
            valid = (
                item is True
                if category == "true"
                else type(item) is int and 0 <= item < 2**63
                if category == "number"
                else type(item) is str
                and (
                    bool(DIGEST.fullmatch(item))
                    if category == "digest"
                    else bool(re.fullmatch(r"[0-9a-f]{32}", item))
                    if category == "nonce"
                    else item in RETIREMENT_CASE_IDS
                    if category == "case"
                    else item in {"SIGINT", "SIGTERM"}
                    if category == "signal"
                    else bool(
                        re.fullmatch(
                            r"/system.slice/prism-retirement-probe-[0-9a-f]{32}-(?:int-executor|int-post|int-nested|int-async|int-daemon|int-child|term-control)\.service",
                            item,
                        )
                    )
                )
            )
            if not valid:
                raise ReleaseError("retirement_safe_event_rejected")
        return value

    def validate_legacy_retirement(self):
        self.verify_tree()
        contents = self.retirement_sources()
        baseline = self.retirement_baseline()
        attempt = self.request.get("retirement_attempt", self.request["release_id"])
        if type(attempt) is not str or not RELEASE_ID.fullmatch(attempt):
            raise ReleaseError("retirement_attempt_rejected")
        directory = self.checked(self.record / "legacy-retirement" / attempt)
        if directory.exists():
            raise ReleaseError("retirement_attempt_already_exists")
        self.boundary("retirement_attempt")
        directory.mkdir(parents=True, mode=0o700)
        self.write(
            directory / "identity.json",
            encoded(
                {
                    "commit": self.request["commit"],
                    "attempt": attempt,
                    "helpers": RETIREMENT_HELPERS,
                    "baseline": baseline,
                }
            ),
        )
        events = self.checked(directory / "events.jsonl")
        fd = os.open(events, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        count, chain = 0, "0" * 64
        summary = None
        passed, baseline_verified_after, error_code = False, False, None
        recorded_summary, cancelled_code = None, None
        try:
            # The append fd's name and attempt hierarchy must survive a crash,
            # before the helper may emit its first durable intent.
            for parent in (directory, directory.parent, self.record):
                directory_fd = os.open(
                    self.checked(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            helper_root = self.install_retirement_helpers(contents)
            bindings = {
                "commit": self.request["commit"],
                "release_id": self.request["release_id"],
                "base_environment_sha256": baseline["base_environment_sha256"],
            }
            with self.retirement_modules(helper_root) as (probe, validation):

                def record(event, value):
                    nonlocal count, chain, recorded_summary
                    value = self.retirement_event(event, value, bindings, probe, validation)
                    if recorded_summary is not None:
                        raise ReleaseError("retirement_events_after_summary_rejected")
                    entry = (
                        encoded(
                            {
                                "sequence": count + 1,
                                "at": int(self.now()),
                                "previous_sha256": chain,
                                "event": event,
                                "details": value,
                            }
                        )
                        + b"\n"
                    )
                    if len(entry) > 16384:
                        raise ReleaseError("retirement_event_limit")
                    offset = 0
                    while offset < len(entry):
                        written = os.write(fd, entry[offset:])
                        if written <= 0:
                            raise ReleaseError("retirement_event_write_failed")
                        offset += written
                    os.fsync(fd)
                    count += 1
                    chain = digest(entry)
                    if event == "retirement_validation_result":
                        recorded_summary = value

                def boundary(label):
                    nonlocal cancelled_code
                    allowed = {"retirement_copy_public_libraries"} | {
                        "retirement_" + phase + "_" + case
                        for phase in ("start", "signal")
                        for case in RETIREMENT_CASE_IDS
                    }
                    if label not in allowed:
                        raise ReleaseError("retirement_boundary_rejected")
                    try:
                        self.boundary(label)
                    except ReleaseError as error:
                        if str(error) in {
                            "operator_stop",
                            "fresh_operator_permit_required",
                            "permit_timeout",
                            "controller_disconnected",
                        }:
                            cancelled_code = str(error)
                        raise
                    if (
                        self.process_id(SERVICE) != baseline["production_pid"]
                        or self.runtime()["fingerprint"] != baseline["runtime_fingerprint"]
                    ):
                        raise ReleaseError("retirement_original_runtime_changed")

                def run(argv, *, timeout):
                    if type(timeout) not in {int, float} or not 0 < timeout <= 15:
                        raise ReleaseError("retirement_command_timeout_rejected")
                    return self.run(argv, timeout=timeout)

                summary = self.retirement_summary(
                    validation.run_validation(bindings, boundary=boundary, run=run, record=record),
                    bindings,
                    probe,
                    validation,
                )
                if recorded_summary != summary:
                    raise ReleaseError("retirement_durable_summary_required")
                if cancelled_code is not None:
                    raise ReleaseError(cancelled_code)
            if self.retirement_baseline() != baseline:
                raise ReleaseError("retirement_original_baseline_changed")
            baseline_verified_after = True
            if not summary["retirement_validation_passed"] or not summary["synthetic_cleanup_verified"]:
                raise ReleaseError("legacy_retirement_validation_failed")
            passed = True
            self.action_report = {
                "attempt": attempt,
                "summary": summary,
                "events": count,
                "last_event_sha256": chain,
                "production_verified": False,
                "passed": True,
                "baseline_verified_after": True,
            }
        except Exception as error:
            error_code = failure_code(error)
            raise
        finally:
            os.close(fd)
            self.write(
                directory / "receipt.json",
                encoded(
                    {
                        "attempt": attempt,
                        "summary": summary,
                        "events": count,
                        "last_event_sha256": chain,
                        "production_verified": False,
                        "passed": passed,
                        "baseline_verified_after": baseline_verified_after,
                        "failure_code": error_code,
                    }
                ),
            )

    def retirement_private_record(self, path, *, limit=262144, json_value=True):
        """Read a controller-owned immutable record, never a candidate-supplied path."""
        path = self.checked(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > limit
                or (self.root == Path("/") and info.st_uid != 0)
            ):
                raise ReleaseError("retirement_private_record_rejected")
            raw = source.read(limit + 1)
            if (
                len(raw) > limit
                or self.cli_identity(info) != self.cli_identity(os.fstat(source.fileno()))
                or self.cli_identity(info) != self.cli_identity(path.lstat())
            ):
                raise ReleaseError("retirement_private_record_changed")
        try:
            return (json.loads(raw) if json_value else raw), digest(raw)
        except (ValueError, UnicodeError):
            raise ReleaseError("retirement_private_record_rejected") from None

    def retirement_signal_proof(self, baseline, bindings, probe, validation):
        attempt = self.request.get("retirement_proof_attempt")
        if type(attempt) is not str or not RELEASE_ID.fullmatch(attempt):
            raise ReleaseError("retirement_signal_proof_required")
        directory = self.checked(self.record / "legacy-retirement" / attempt)
        if not directory.is_dir():
            raise ReleaseError("retirement_signal_proof_required")
        identity, identity_sha = self.retirement_private_record(directory / "identity.json")
        if identity != {
            "commit": self.request["commit"],
            "attempt": attempt,
            "helpers": RETIREMENT_HELPERS,
            "baseline": baseline,
        }:
            raise ReleaseError("retirement_signal_proof_identity_changed")
        receipt, receipt_sha = self.retirement_private_record(directory / "receipt.json")
        keys = {
            "attempt",
            "summary",
            "events",
            "last_event_sha256",
            "production_verified",
            "passed",
            "baseline_verified_after",
            "failure_code",
        }
        if (
            type(receipt) is not dict
            or set(receipt) != keys
            or receipt["attempt"] != attempt
            or receipt["passed"] is not True
            or receipt["production_verified"] is not False
            or receipt["baseline_verified_after"] is not True
            or receipt["failure_code"] is not None
            or type(receipt["events"]) is not int
            or not 1 <= receipt["events"] <= 256
        ):
            raise ReleaseError("retirement_signal_proof_required")
        summary = self.retirement_summary(receipt["summary"], bindings, probe, validation)
        if not summary["retirement_validation_passed"] or not summary["synthetic_cleanup_verified"]:
            raise ReleaseError("retirement_signal_proof_required")
        plan = probe.descriptor(bindings, nonce="0" * 32)
        expected = {case["case_id"]: case["expectation"] for case in plan["cases"]}
        for case in summary["cases"]:
            positive = expected[case["case_id"]] == "executor_preserved"
            if case["executor_results_preserved"] is not positive or case["expected_counterexample"] is not (
                not positive
            ):
                raise ReleaseError("retirement_signal_proof_case_rejected")
        raw, events_sha = self.retirement_private_record(
            directory / "events.jsonl", limit=1048576, json_value=False
        )
        lines, chain = raw.splitlines(keepends=True), "0" * 64
        observed, nonce, final = {}, None, None
        try:
            for number, line in enumerate(lines, 1):
                row = json.loads(line)
                if (
                    type(row) is not dict
                    or set(row) != {"sequence", "at", "previous_sha256", "event", "details"}
                    or type(row["sequence"]) is not int
                    or row["sequence"] != number
                    or type(row["at"]) is not int
                    or row["at"] <= 0
                    or row["previous_sha256"] != chain
                    or not line.endswith(b"\n")
                    or final is not None
                ):
                    raise ReleaseError("retirement_signal_proof_chain_rejected")
                details = self.retirement_event(row["event"], row["details"], bindings, probe, validation)
                case_id = details.get("case_id")
                if case_id:
                    per_case = observed.setdefault(case_id, {})
                    if row["event"] in per_case:
                        raise ReleaseError("retirement_signal_proof_chain_rejected")
                    per_case[row["event"]] = details
                    if row["event"] == "synthetic_start_intent":
                        nonce = nonce or details["nonce"]
                        if nonce != details["nonce"]:
                            raise ReleaseError("retirement_signal_proof_chain_rejected")
                if row["event"] == "retirement_validation_result":
                    final = details
                chain = digest(line)
        except (ValueError, UnicodeError, TypeError, AttributeError):
            raise ReleaseError("retirement_signal_proof_chain_rejected") from None
        if (
            len(lines) != receipt["events"]
            or chain != receipt["last_event_sha256"]
            or final != summary
            or set(observed) != RETIREMENT_CASE_IDS
        ):
            raise ReleaseError("retirement_signal_proof_chain_rejected")
        summary_cases = {case["case_id"]: case for case in summary["cases"]}
        for case_id, events in observed.items():
            required = {
                "synthetic_start_intent",
                "actual_isolation_verified",
                "synthetic_signal_intent",
                "synthetic_exit_observed",
                "synthetic_case_result",
            }
            if not required <= set(events) or events["synthetic_case_result"] != summary_cases[case_id]:
                raise ReleaseError("retirement_signal_proof_case_rejected")
            order = list(events)
            phases = [
                "synthetic_start_intent",
                "actual_isolation_verified",
                "synthetic_signal_intent",
                "synthetic_exit_observed",
                "synthetic_case_result",
            ]
            if [order.index(phase) for phase in phases] != sorted(order.index(phase) for phase in phases):
                raise ReleaseError("retirement_signal_proof_case_rejected")
            isolation, signal = events["actual_isolation_verified"], events["synthetic_signal_intent"]
            descriptor = probe._case(probe.descriptor(bindings, nonce=nonce), case_id)
            if (
                isolation["invocation_id"] != signal["invocation_id"]
                or isolation["control_group"] != descriptor["control_group"]
                or signal["signal"] != descriptor["signal"]
                or isolation["main_pid"] <= 1
                or isolation["uid"] <= 0
                or isolation["start_ticks"] <= 0
                or signal["signal_at_monotonic_us"] <= 0
            ):
                raise ReleaseError("retirement_signal_proof_case_rejected")
        return {
            "attempt": attempt,
            "identity_sha256": identity_sha,
            "receipt_sha256": receipt_sha,
            "events_sha256": events_sha,
            "last_event_sha256": chain,
        }

    def retirement_stop_summary(self, value, bindings, probe, validation, stop):
        fixed = {
            "schema": 1,
            "bindings": bindings,
            "stop_binding_sha256": stop.descriptor(
                bindings, expected_python_sha256=RETIREMENT_SYSTEM_PYTHON_SHA256
            )["binding_sha256"],
            "actual_linux_collection": True,
            "production_quiescence_proven": False,
            "all_production_memory_persisted": False,
            "task_census_still_required": True,
            "python_version": "3.12.3",
            "python_binary_sha256": RETIREMENT_SYSTEM_PYTHON_SHA256,
            "probe_source_sha256": RETIREMENT_HELPERS["retirement_probe.py"],
            "validation_source_sha256": RETIREMENT_HELPERS["retirement_validation.py"],
            "bundle_sha256": probe.bundle_identity(),
            "uvicorn_server_sha256": probe.UVICORN_SHA256,
            "uvloop_binary_sha256": probe.UVLOOP_SHA256,
            "policy_sha256": probe.sha256(probe.encoded(stop.POLICY)),
        }
        keys = {
            *fixed,
            "retirement_stop_validation_passed",
            "systemctl_stop_end_to_end_proven",
            "synthetic_cleanup_verified",
            "cases",
        }
        if (
            type(value) is not dict
            or set(value) not in (keys, keys | {"error_code"})
            or any(type(value.get(k)) is not type(v) or value[k] != v for k, v in fixed.items())
            or any(
                type(value.get(k)) is not bool
                for k in (
                    "retirement_stop_validation_passed",
                    "systemctl_stop_end_to_end_proven",
                    "synthetic_cleanup_verified",
                )
            )
            or value["systemctl_stop_end_to_end_proven"] != value["retirement_stop_validation_passed"]
            or type(value["cases"]) is not list
            or len(value["cases"]) > 2
        ):
            raise ReleaseError("retirement_stop_safe_result_rejected")
        for case in value["cases"]:
            self.retirement_case_result(case)
            if case["case_id"] not in stop.CASES or case["expected_counterexample"] is not False:
                raise ReleaseError("retirement_stop_safe_result_rejected")
        ids = [case["case_id"] for case in value["cases"]]
        if ids != list(stop.CASES)[: len(ids)]:
            raise ReleaseError("retirement_stop_safe_result_rejected")
        if "error_code" in value and (
            type(value["error_code"]) is not str
            or not re.fullmatch(r"retirement_[a-z0-9_]{1,90}", value["error_code"])
            or value["retirement_stop_validation_passed"]
        ):
            raise ReleaseError("retirement_stop_safe_result_rejected")
        if value["retirement_stop_validation_passed"] and (
            ids != list(stop.CASES)
            or not value["synthetic_cleanup_verified"]
            or not all(
                case["passed"] and case["clean_exit_zero"] and case["executor_results_preserved"]
                for case in value["cases"]
            )
        ):
            raise ReleaseError("retirement_stop_safe_result_rejected")
        return value

    def retirement_stop_event(self, event, value, bindings, probe, validation, stop):
        if event == "retirement_stop_validation_result":
            return self.retirement_stop_summary(value, bindings, probe, validation, stop)
        if event == "synthetic_stop_exec_arrays_observed":
            if (
                type(value) is not dict
                or set(value) != {"case_id", "properties"}
                or value["case_id"] not in stop.CASES
                or stop.EMPTY_EXEC_ARRAY is None
                or encoded(value["properties"])
                != encoded({key: stop.EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")})
            ):
                raise ReleaseError("retirement_stop_safe_event_rejected")
            return value
        if event in {
            "bundle_copy_intent",
            "public_bundles_verified",
            "synthetic_start_intent",
            "synthetic_exit_observed",
        }:
            value = self.retirement_event(event, value, bindings, probe, validation)
            if "case_id" in value and value["case_id"] not in stop.CASES:
                raise ReleaseError("retirement_stop_safe_event_rejected")
            return value
        fields = {
            "synthetic_observer_release_intent": {"case_id": "case", "invocation_id": "nonce"},
            "synthetic_observer_start_intent": {"case_id": "case"},
            "synthetic_stop_actual_isolation": {
                "case_id": "case",
                "invocation": "nonce",
                "pid": "positive",
                "uid": "positive",
                "start_ticks": "positive",
                "actual_kernel_verified": "true",
            },
            "synthetic_stop_intent": {
                "case_id": "case",
                "invocation_id": "nonce",
                "pid": "positive",
                "start_ticks": "positive",
                "stop_at_monotonic_us": "positive",
            },
            "synthetic_stop_committed": {"case_id": "case", "invocation_id": "nonce"},
            "synthetic_stop_runner_join_observed": {
                "case_id": "case",
                "stop_job_waited_with_executor_pending": "true",
            },
        }.get(event)
        if event == "synthetic_stop_case_result":
            extra = {"systemctl_stop_end_to_end_proven", "stop_dispatches", "exit_monotonic_us"}
            if type(value) is not dict or not extra <= set(value):
                raise ReleaseError("retirement_stop_safe_event_rejected")
            self.retirement_case_result({k: v for k, v in value.items() if k not in extra})
            if (
                value["case_id"] not in stop.CASES
                or value["expected_counterexample"] is not False
                or value["systemctl_stop_end_to_end_proven"] is not True
                or type(value["stop_dispatches"]) is not int
                or value["stop_dispatches"] != 1
                or type(value["exit_monotonic_us"]) is not int
                or not 0 < value["exit_monotonic_us"] < 2**63
            ):
                raise ReleaseError("retirement_stop_safe_event_rejected")
            return value
        if fields is None or type(value) is not dict or set(value) != set(fields):
            raise ReleaseError("retirement_stop_safe_event_rejected")
        for key, kind in fields.items():
            item = value[key]
            valid = (
                item is True
                if kind == "true"
                else type(item) is int and 0 < item < 2**63
                if kind == "positive"
                else type(item) is str
                and (item in stop.CASES if kind == "case" else bool(re.fullmatch(r"[0-9a-f]{32}", item)))
            )
            if not valid:
                raise ReleaseError("retirement_stop_safe_event_rejected")
        return value

    def retirement_retained_start_state(self, bindings, probe, validation, stop):
        """Only read the exact reviewed exited invocation; never stop or unload it."""
        reviewed = RETIREMENT_RETAINED_START
        plan = probe.descriptor(bindings, nonce=reviewed["nonce"])
        case = probe._case(plan, reviewed["case_id"])
        unit = case["unit"]
        command = [
            "/usr/bin/systemctl",
            "show",
            "--all",
            "--property=" + RETIREMENT_RETAINED_PROPERTIES,
            unit,
        ]
        before = stop._rows(self.run(command, timeout=10), RETIREMENT_RETAINED_PROPERTIES)
        expected = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "exited",
            "MainPID": "0",
            "ExecMainPID": str(reviewed["exec_main_pid"]),
            "ExecMainCode": "1",
            "ExecMainStatus": "0",
            "Result": "success",
            "InvocationID": reviewed["invocation_id"],
            "ControlGroup": "",
            "Transient": "yes",
        }
        if any(before[key] != value for key, value in expected.items()):
            raise ReleaseError("retirement_retained_start_unreconciled")
        job = stop.typed_job(self.run, unit, before["Job"])
        if job != {"type": "(uo)", "data": [0, "/"]}:
            raise ReleaseError("retirement_retained_start_unreconciled")
        start_command = stop.exec_array_commands(unit)[0]
        start_command[-1] = "ExecStart"
        raw = self.run(start_command, timeout=10)
        if type(raw) is not bytes or len(raw) > 16384:
            raise ReleaseError("retirement_retained_start_unreconciled")

        def pairs(rows):
            value = dict(rows)
            if len(value) != len(rows):
                raise ValueError("duplicate_key")
            return value

        start = json.loads(raw, object_pairs_hook=pairs)
        launch = validation.launch_command(plan, reviewed["case_id"])
        argv = launch[launch.index("--") + 1 :]
        if digest(encoded(argv)) != reviewed["argv_sha256"]:
            raise ReleaseError("retirement_retained_start_unreconciled")
        if (
            type(start) is not dict
            or set(start) != {"type", "data"}
            or start["type"] != "a(sasbttttuii)"
            or type(start["data"]) is not list
            or len(start["data"]) != 1
        ):
            raise ReleaseError("retirement_retained_start_unreconciled")
        row = start["data"][0]
        if (
            type(row) is not list
            or len(row) != 10
            or row[0] != argv[0]
            or row[1] != argv
            or row[2] is not False
            or any(type(number) is not int or not 0 < number < 2**64 for number in row[3:7])
            or any(type(number) is not int for number in row[7:])
            or not (row[3] <= row[5] and row[4] <= row[6])
            or row[7:] != [reviewed["exec_main_pid"], 1, 0]
            or before["ExecMainExitTimestampMonotonic"] != str(row[6])
        ):
            raise ReleaseError("retirement_retained_start_unreconciled")
        observer = stop.observer_unit(plan, reviewed["case_id"])
        missing = self.run(["/usr/bin/systemctl", "show", "--property=LoadState", observer], timeout=10)
        if stop._rows(missing, "LoadState") != {"LoadState": "not-found"}:
            raise ReleaseError("retirement_retained_start_unreconciled")
        cgroot = self.checked(self.root / "sys/fs/cgroup")
        if not self.checked(cgroot / "cgroup.controllers").is_file():
            raise ReleaseError("retirement_retained_start_unreconciled")
        for name in (unit, observer):
            group = self.checked(cgroot / "system.slice" / name)
            if group.exists():
                raise ReleaseError("retirement_retained_start_unreconciled")
        after = stop._rows(self.run(command, timeout=10), RETIREMENT_RETAINED_PROPERTIES)
        if before != after:
            raise ReleaseError("retirement_retained_start_unreconciled")
        return {
            "unit": unit,
            "invocation_id": reviewed["invocation_id"],
            "exec_main_pid": reviewed["exec_main_pid"],
            "typed_job": job,
            "state_sha256": digest(encoded(before)),
            "exec_start_argv_sha256": digest(encoded(argv)),
            "case_cgroup_absent": True,
            "observer_not_found": True,
            "observer_cgroup_absent": True,
            "no_signal_or_cleanup_authorized": True,
        }

    def retirement_reconcile_retained_start(
        self, previous, baseline, bindings, probe, validation, stop, proof, prestart
    ):
        """Independently reconcile one pinned started failure, never call it passed."""
        reviewed = RETIREMENT_RETAINED_START
        try:
            if previous.name != reviewed["attempt"] or [item["attempt"] for item in prestart] != list(
                RETIREMENT_PRESTART_REVIEWED
            ):
                raise ReleaseError("retirement_retained_start_unreconciled")
            identity, identity_sha = self.retirement_private_record(previous / "identity.json")
            receipt, receipt_sha = self.retirement_private_record(previous / "receipt.json")
            raw, events_sha = self.retirement_private_record(
                previous / "events.jsonl", limit=32768, json_value=False
            )
            if any(
                actual != reviewed[key]
                for key, actual in (
                    ("identity_sha256", identity_sha),
                    ("receipt_sha256", receipt_sha),
                    ("events_sha256", events_sha),
                )
            ):
                raise ReleaseError("retirement_retained_start_unreconciled")
            if identity != {
                "commit": self.request["commit"],
                "attempt": previous.name,
                "helpers": {**RETIREMENT_HELPERS, "retirement_stop_validation.py": reviewed["helper_sha256"]},
                "baseline": baseline,
                "signal_proof": proof,
                "system_python_sha256": RETIREMENT_SYSTEM_PYTHON_SHA256,
                "prestart_reconciliations": prestart,
            }:
                raise ReleaseError("retirement_retained_start_unreconciled")
            summary = self.retirement_stop_summary(receipt.get("summary"), bindings, probe, validation, stop)
            if (
                set(receipt)
                != {
                    "attempt",
                    "signal_proof",
                    "summary",
                    "events",
                    "last_event_sha256",
                    "production_verified",
                    "passed",
                    "baseline_verified_after",
                    "failure_code",
                }
                or receipt["attempt"] != previous.name
                or receipt["signal_proof"] != proof
                or receipt["events"] != 3
                or type(receipt["events"]) is not int
                or receipt["production_verified"] is not False
                or receipt["passed"] is not False
                or receipt["baseline_verified_after"] is not True
                or receipt["failure_code"] != "legacy_stop_validation_failed"
                or summary["cases"] != []
                or summary["synthetic_cleanup_verified"] is not False
                or summary["retirement_stop_validation_passed"] is not False
                or summary["systemctl_stop_end_to_end_proven"] is not False
                or summary.get("error_code") != "retirement_systemd_number_rejected"
            ):
                raise ReleaseError("retirement_retained_start_unreconciled")
            expected = (
                (
                    "public_bundles_verified",
                    {
                        "bundle_sha256": probe.bundle_identity(),
                        "wrapper_sha256": validation.wrapper_directory().name,
                    },
                ),
                ("synthetic_start_intent", {"case_id": reviewed["case_id"], "nonce": reviewed["nonce"]}),
                ("retirement_stop_validation_result", summary),
            )
            lines, chain = raw.splitlines(keepends=True), "0" * 64
            if len(lines) != 3:
                raise ReleaseError("retirement_retained_start_unreconciled")
            for sequence, (line, (event, details)) in enumerate(zip(lines, expected, strict=True), 1):
                row = json.loads(line)
                if (
                    type(row) is not dict
                    or set(row) != {"sequence", "at", "previous_sha256", "event", "details"}
                    or type(row["sequence"]) is not int
                    or row["sequence"] != sequence
                    or type(row["at"]) is not int
                    or row["at"] <= 0
                    or row["previous_sha256"] != chain
                    or row["event"] != event
                    or encoded(row["details"]) != encoded(details)
                    or not line.endswith(b"\n")
                ):
                    raise ReleaseError("retirement_retained_start_unreconciled")
                chain = digest(line)
            if chain != receipt["last_event_sha256"]:
                raise ReleaseError("retirement_retained_start_unreconciled")
            observed = self.retirement_retained_start_state(bindings, probe, validation, stop)
            return {
                "schema": 1,
                "attempt": previous.name,
                "identity_sha256": identity_sha,
                "receipt_sha256": receipt_sha,
                "events_sha256": events_sha,
                "last_event_sha256": chain,
                "observation": observed,
                "failed_record_retained": True,
                "previous_passed": False,
                "previous_cleanup_verified": False,
                "no_stop_intent_in_complete_chain": True,
                "baseline_verified_after": True,
            }
        except Exception:
            raise ReleaseError("retirement_retained_start_unreconciled") from None

    def retirement_retained_observer_state(self, bindings, probe, validation, stop):
        reviewed = RETIREMENT_RETAINED_OBSERVER_START
        plan = probe.descriptor(bindings, nonce=reviewed["nonce"])
        case = probe._case(plan, reviewed["case_id"])
        unit, observer = case["unit"], stop.observer_unit(plan, reviewed["case_id"])
        query = ["/usr/bin/systemctl", "show", "--all", "--property=" + stop.OBSERVER_PROPERTIES, unit]
        before = stop._rows(self.run(query, timeout=10), stop.OBSERVER_PROPERTIES)
        expected = {
            "LoadState": "loaded",
            "ActiveState": "active",
            "SubState": "exited",
            "MainPID": "0",
            "ExecMainPID": str(reviewed["exec_main_pid"]),
            "ExecMainCode": "1",
            "ExecMainStatus": "0",
            "Result": "success",
            "InvocationID": reviewed["invocation_id"],
            "ControlGroup": "",
            "Transient": "yes",
            "DynamicUser": "yes",
            "RemainAfterExit": "yes",
            "PrivateTmp": "yes",
        }
        if (
            any(before[key] != value for key, value in expected.items())
            or before["Wants"].split() != ["tmp.mount"]
            or any(before[key] for key in ("PartOf", "BindsTo", "PropagatesStopTo"))
        ):
            raise ReleaseError("retirement_retained_observer_unreconciled")
        job = stop.typed_job(self.run, unit, before["Job"])
        if job != {"type": "(uo)", "data": [0, "/"]}:
            raise ReleaseError("retirement_retained_observer_unreconciled")

        def typed(command):
            raw = self.run(command, timeout=10)
            if type(raw) is not bytes or len(raw) > 16384:
                raise ReleaseError("retirement_retained_observer_unreconciled")

            def pairs(rows):
                value = dict(rows)
                if len(value) != len(rows):
                    raise ValueError("duplicate_key")
                return value

            return json.loads(raw, object_pairs_hook=pairs)

        launch = validation.launch_command(plan, reviewed["case_id"])
        argv = launch[launch.index("--") + 1 :]
        command = stop.job_command(unit)
        command[-2:] = ["org.freedesktop.systemd1.Service", "ExecStart"]
        start = typed(command)
        if (
            digest(encoded(argv)) != reviewed["main_argv_sha256"]
            or type(start) is not dict
            or set(start) != {"type", "data"}
            or start["type"] != "a(sasbttttuii)"
            or type(start["data"]) is not list
            or len(start["data"]) != 1
        ):
            raise ReleaseError("retirement_retained_observer_unreconciled")
        row = start["data"][0]
        if (
            type(row) is not list
            or len(row) != 10
            or row[0] != argv[0]
            or row[1] != argv
            or row[2] is not False
            or any(type(number) is not int for number in row[3:])
            or any(not 0 < number < 2**64 for number in row[3:7])
            or row[7:] != [reviewed["exec_main_pid"], 1, 0]
            or before["ExecMainExitTimestampMonotonic"] != str(row[6])
        ):
            raise ReleaseError("retirement_retained_observer_unreconciled")
        arrays = {}
        for name in (unit, observer):
            arrays[name] = {}
            for key in ("ExecStop", "ExecStopPost"):
                command = stop.job_command(name)
                command[-2:] = ["org.freedesktop.systemd1.Service", key]
                value = typed(command)
                if encoded(value) != encoded(stop.EMPTY_EXEC_ARRAY):
                    raise ReleaseError("retirement_retained_observer_unreconciled")
                arrays[name][key] = value
        observed = stop.retained_observer_state(self.run, bindings)
        if observed["exec_start_argv_sha256"] != reviewed["observer_argv_sha256"]:
            raise ReleaseError("retirement_retained_observer_unreconciled")
        cgroot = self.checked(self.root / "sys/fs/cgroup")
        if not self.checked(cgroot / "cgroup.controllers").is_file() or any(
            self.checked(cgroot / "system.slice" / name).exists() for name in (unit, observer)
        ):
            raise ReleaseError("retirement_retained_observer_unreconciled")
        if before != stop._rows(self.run(query, timeout=10), stop.OBSERVER_PROPERTIES):
            raise ReleaseError("retirement_retained_observer_unreconciled")
        return {
            "main": {
                "unit": unit,
                "invocation_id": reviewed["invocation_id"],
                "exec_main_pid": reviewed["exec_main_pid"],
                "typed_job": job,
                "state_sha256": digest(encoded(before)),
                "exec_start_argv_sha256": digest(encoded(argv)),
                "cgroup_absent": True,
            },
            "observer": observed,
            "empty_stop_arrays": arrays,
            "no_signal_or_cleanup_authorized": True,
        }

    def retirement_reconcile_retained_observer(
        self, previous, baseline, bindings, probe, validation, stop, proof, prestart, retained_start
    ):
        reviewed = RETIREMENT_RETAINED_OBSERVER_START
        try:
            if (
                previous.name != reviewed["attempt"]
                or [item["attempt"] for item in prestart] != list(RETIREMENT_PRESTART_REVIEWED)
                or retained_start is None
            ):
                raise ReleaseError("retirement_retained_observer_unreconciled")
            identity, identity_sha = self.retirement_private_record(previous / "identity.json")
            receipt, receipt_sha = self.retirement_private_record(previous / "receipt.json")
            raw, events_sha = self.retirement_private_record(
                previous / "events.jsonl", limit=32768, json_value=False
            )
            if any(
                actual != reviewed[key]
                for key, actual in (
                    ("identity_sha256", identity_sha),
                    ("receipt_sha256", receipt_sha),
                    ("events_sha256", events_sha),
                )
            ):
                raise ReleaseError("retirement_retained_observer_unreconciled")
            if identity != {
                "commit": self.request["commit"],
                "attempt": previous.name,
                "helpers": {**RETIREMENT_HELPERS, "retirement_stop_validation.py": reviewed["helper_sha256"]},
                "baseline": baseline,
                "signal_proof": proof,
                "system_python_sha256": RETIREMENT_SYSTEM_PYTHON_SHA256,
                "prestart_reconciliations": prestart,
                "retained_start_reconciliation": retained_start,
            }:
                raise ReleaseError("retirement_retained_observer_unreconciled")
            summary = self.retirement_stop_summary(receipt.get("summary"), bindings, probe, validation, stop)
            if (
                set(receipt)
                != {
                    "attempt",
                    "signal_proof",
                    "summary",
                    "events",
                    "last_event_sha256",
                    "production_verified",
                    "passed",
                    "baseline_verified_after",
                    "failure_code",
                }
                or receipt["attempt"] != previous.name
                or receipt["signal_proof"] != proof
                or type(receipt["events"]) is not int
                or receipt["events"] != 6
                or receipt["production_verified"] is not False
                or receipt["passed"] is not False
                or receipt["baseline_verified_after"] is not True
                or receipt["failure_code"] != "legacy_stop_validation_failed"
                or summary["cases"] != []
                or summary["synthetic_cleanup_verified"] is not False
                or summary["retirement_stop_validation_passed"] is not False
                or summary["systemctl_stop_end_to_end_proven"] is not False
                or summary.get("error_code") != "retirement_stop_observer_reference_rejected"
            ):
                raise ReleaseError("retirement_retained_observer_unreconciled")
            expected = (
                (
                    "public_bundles_verified",
                    {
                        "bundle_sha256": probe.bundle_identity(),
                        "wrapper_sha256": validation.wrapper_directory().name,
                    },
                ),
                ("synthetic_start_intent", {"case_id": reviewed["case_id"], "nonce": reviewed["nonce"]}),
                (
                    "synthetic_stop_actual_isolation",
                    {
                        "case_id": reviewed["case_id"],
                        "invocation": reviewed["invocation_id"],
                        "pid": reviewed["exec_main_pid"],
                        "start_ticks": reviewed["start_ticks"],
                        "uid": reviewed["uid"],
                        "actual_kernel_verified": True,
                    },
                ),
                (
                    "synthetic_stop_exec_arrays_observed",
                    {
                        "case_id": reviewed["case_id"],
                        "properties": {key: stop.EMPTY_EXEC_ARRAY for key in ("ExecStop", "ExecStopPost")},
                    },
                ),
                ("synthetic_observer_start_intent", {"case_id": reviewed["case_id"]}),
                ("retirement_stop_validation_result", summary),
            )
            lines, chain = raw.splitlines(keepends=True), "0" * 64
            if len(lines) != 6:
                raise ReleaseError("retirement_retained_observer_unreconciled")
            for sequence, (line, (event, details)) in enumerate(zip(lines, expected, strict=True), 1):
                row = json.loads(line)
                if (
                    type(row) is not dict
                    or set(row) != {"sequence", "at", "previous_sha256", "event", "details"}
                    or type(row["sequence"]) is not int
                    or row["sequence"] != sequence
                    or type(row["at"]) is not int
                    or row["at"] <= 0
                    or row["previous_sha256"] != chain
                    or row["event"] != event
                    or encoded(row["details"]) != encoded(details)
                    or not line.endswith(b"\n")
                ):
                    raise ReleaseError("retirement_retained_observer_unreconciled")
                chain = digest(line)
            if chain != receipt["last_event_sha256"]:
                raise ReleaseError("retirement_retained_observer_unreconciled")
            observation = self.retirement_retained_observer_state(bindings, probe, validation, stop)
            return {
                "schema": 1,
                "attempt": previous.name,
                "identity_sha256": identity_sha,
                "receipt_sha256": receipt_sha,
                "events_sha256": events_sha,
                "last_event_sha256": chain,
                "observation": observation,
                "previous_passed": False,
                "previous_cleanup_verified": False,
                "failed_record_retained": True,
                "no_stop_intent_in_complete_chain": True,
                "baseline_verified_after": True,
            }
        except Exception:
            raise ReleaseError("retirement_retained_observer_unreconciled") from None

    def retirement_reconcile_prestart(self, baseline, bindings, probe, validation, stop, proof):
        """Reconcile only the reviewed pre-actuation property-query failure."""
        parent = self.checked(self.record / "legacy-stop")
        reconciled = []
        self._retirement_retained_start_reconciliation = None
        self._retirement_retained_observer_reconciliation = None
        known = RETIREMENT_RETAINED_START
        if (
            all(bindings[key] == known[key] for key in ("commit", "release_id"))
            and not self.checked(parent / known["attempt"]).is_dir()
        ):
            raise ReleaseError("retirement_previous_stop_unreconciled")
        known_observer = RETIREMENT_RETAINED_OBSERVER_START
        if (
            all(bindings[key] == known_observer[key] for key in ("commit", "release_id"))
            and not self.checked(parent / known_observer["attempt"]).is_dir()
        ):
            raise ReleaseError("retirement_previous_stop_unreconciled")
        if not parent.exists():
            return reconciled
        # The second reviewed record contains the first reconciliation. Replay
        # dependencies before comparing that exact identity, regardless of the
        # filesystem's directory enumeration order.
        for previous in sorted(parent.iterdir(), key=lambda path: path.name):
            try:
                receipt, receipt_sha = self.retirement_private_record(previous / "receipt.json")
                if previous.name == RETIREMENT_RETAINED_OBSERVER_START["attempt"]:
                    self._retirement_retained_observer_reconciliation = (
                        self.retirement_reconcile_retained_observer(
                            previous,
                            baseline,
                            bindings,
                            probe,
                            validation,
                            stop,
                            proof,
                            reconciled,
                            self._retirement_retained_start_reconciliation,
                        )
                    )
                    continue
                if previous.name == RETIREMENT_RETAINED_START["attempt"]:
                    self._retirement_retained_start_reconciliation = self.retirement_reconcile_retained_start(
                        previous, baseline, bindings, probe, validation, stop, proof, reconciled
                    )
                    continue
                if (
                    type(receipt) is dict
                    and receipt.get("passed") is True
                    and receipt.get("baseline_verified_after") is True
                    and type(receipt.get("summary")) is dict
                    and receipt["summary"].get("synthetic_cleanup_verified") is True
                ):
                    continue
                reviewed = RETIREMENT_PRESTART_REVIEWED.get(previous.name)
                if reviewed is None:
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                identity, identity_sha = self.retirement_private_record(previous / "identity.json")
                expected_identity = {
                    "commit": self.request["commit"],
                    "attempt": previous.name,
                    "helpers": {
                        **RETIREMENT_HELPERS,
                        "retirement_stop_validation.py": reviewed["helper_sha256"],
                    },
                    "baseline": baseline,
                    "signal_proof": proof,
                    "system_python_sha256": RETIREMENT_SYSTEM_PYTHON_SHA256,
                }
                if "prestart_reconciliation_attempts" in reviewed:
                    prior = {item["attempt"]: item for item in reconciled}
                    expected_identity["prestart_reconciliations"] = [
                        prior[name] for name in reviewed["prestart_reconciliation_attempts"]
                    ]
                keys = {
                    "attempt",
                    "signal_proof",
                    "summary",
                    "events",
                    "last_event_sha256",
                    "production_verified",
                    "passed",
                    "baseline_verified_after",
                    "failure_code",
                }
                if (
                    identity != expected_identity
                    or type(receipt) is not dict
                    or set(receipt) != keys
                    or receipt["attempt"] != previous.name
                    or receipt["signal_proof"] != proof
                    or receipt["passed"] is not False
                    or receipt["baseline_verified_after"] is not True
                    or receipt["production_verified"] is not False
                    or receipt["failure_code"] != "legacy_stop_validation_failed"
                    or type(receipt["events"]) is not int
                    or receipt["events"] != 2
                ):
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                summary = self.retirement_stop_summary(receipt["summary"], bindings, probe, validation, stop)
                if (
                    summary["retirement_stop_validation_passed"] is not False
                    or summary["systemctl_stop_end_to_end_proven"] is not False
                    or summary["synthetic_cleanup_verified"] is not True
                    or summary["cases"] != []
                    or summary.get("error_code") != "retirement_stop_systemd_properties_rejected"
                ):
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                raw, events_sha = self.retirement_private_record(
                    previous / "events.jsonl", limit=32768, json_value=False
                )
                if any(
                    reviewed.get(key, actual) != actual
                    for key, actual in (
                        ("receipt_sha256", receipt_sha),
                        ("identity_sha256", identity_sha),
                        ("events_sha256", events_sha),
                    )
                ):
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                lines, chain = raw.splitlines(keepends=True), "0" * 64
                if len(lines) != 2:
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                for sequence, (line, event) in enumerate(
                    zip(lines, ("public_bundles_verified", "retirement_stop_validation_result"), strict=True),
                    1,
                ):
                    row = json.loads(line)
                    if (
                        type(row) is not dict
                        or set(row) != {"sequence", "at", "previous_sha256", "event", "details"}
                        or type(row["sequence"]) is not int
                        or row["sequence"] != sequence
                        or type(row["at"]) is not int
                        or row["at"] <= 0
                        or row["event"] != event
                        or row["previous_sha256"] != chain
                        or not line.endswith(b"\n")
                    ):
                        raise ReleaseError("retirement_previous_stop_unreconciled")
                    details = self.retirement_stop_event(
                        event, row["details"], bindings, probe, validation, stop
                    )
                    expected = (
                        {
                            "bundle_sha256": probe.bundle_identity(),
                            "wrapper_sha256": validation.wrapper_directory().name,
                        }
                        if sequence == 1
                        else summary
                    )
                    if details != expected:
                        raise ReleaseError("retirement_previous_stop_unreconciled")
                    chain = digest(line)
                if chain != receipt["last_event_sha256"]:
                    raise ReleaseError("retirement_previous_stop_unreconciled")
                reconciled.append(
                    {
                        "attempt": previous.name,
                        "identity_sha256": identity_sha,
                        "receipt_sha256": receipt_sha,
                        "events_sha256": events_sha,
                        "last_event_sha256": chain,
                        "no_synthetic_actuation": True,
                        "baseline_verified_after": True,
                        "reason": "retirement_stop_systemd_properties_rejected",
                    }
                )
            except Exception:
                raise ReleaseError("retirement_previous_stop_unreconciled") from None
        return reconciled

    def validate_legacy_stop(self):
        self.verify_tree()
        attempt = self.request.get("retirement_stop_attempt")
        if type(attempt) is not str or not RELEASE_ID.fullmatch(attempt):
            raise ReleaseError("explicit_retirement_stop_attempt_required")
        if self.request["receipts"].get("system_python_sha256") != RETIREMENT_SYSTEM_PYTHON_SHA256:
            raise ReleaseError("retirement_system_python_binding_required")
        contents = self.retirement_sources(stop=True)
        baseline = self.retirement_baseline()
        retained = None
        retained_observer = None

        def active_baseline():
            # No legacy environment is executed: the fixture uses separately
            # verified public bundles. Scan its full legacy origin before and
            # after the experiment, not inside the fixed 20-second work gate.
            try:
                current = self.live_original_state()  # Full production source + runtime.
            except ReleaseError:
                raise ReleaseError("retirement_original_baseline_changed") from None
            expected = {key: baseline[key] for key in ("production_pid", "runtime_fingerprint")}
            if current != expected:
                raise ReleaseError("retirement_original_baseline_changed")
            if (
                retained is not None
                and self.retirement_retained_start_state(bindings, probe, validation, stop)
                != retained["observation"]
            ):
                raise ReleaseError("retirement_retained_start_unreconciled")
            if (
                retained_observer is not None
                and self.retirement_retained_observer_state(bindings, probe, validation, stop)
                != retained_observer["observation"]
            ):
                raise ReleaseError("retirement_retained_observer_unreconciled")

        bindings = {
            "commit": self.request["commit"],
            "release_id": self.request["release_id"],
            "base_environment_sha256": baseline["base_environment_sha256"],
        }
        directory = self.checked(self.record / "legacy-stop" / attempt)
        if directory.exists():
            raise ReleaseError("retirement_stop_attempt_already_exists")
        helper_root = self.install_retirement_helpers(contents, stop=True)
        with self.retirement_modules(helper_root, stop=True) as (probe, validation, stop):
            proof = self.retirement_signal_proof(baseline, bindings, probe, validation)
            reconciled = self.retirement_reconcile_prestart(
                baseline, bindings, probe, validation, stop, proof
            )
            retained = self._retirement_retained_start_reconciliation
            retained_observer = self._retirement_retained_observer_reconciliation
            self.boundary("retirement_stop_attempt")
            active_baseline()
            directory.mkdir(parents=True, mode=0o700)
            self.write(
                directory / "identity.json",
                encoded(
                    {
                        "commit": self.request["commit"],
                        "attempt": attempt,
                        "helpers": RETIREMENT_STOP_HELPERS,
                        "baseline": baseline,
                        "signal_proof": proof,
                        "system_python_sha256": RETIREMENT_SYSTEM_PYTHON_SHA256,
                        "prestart_reconciliations": reconciled,
                        **({"retained_start_reconciliation": retained} if retained is not None else {}),
                        **(
                            {"retained_observer_reconciliation": retained_observer}
                            if retained_observer is not None
                            else {}
                        ),
                    }
                ),
            )
            if reconciled:
                self.write(
                    directory / "prestart-reconciliation.json",
                    encoded(
                        {
                            "schema": 1,
                            "candidate": self.request["commit"],
                            "release_id": self.request["release_id"],
                            "records": reconciled,
                            "old_records_unchanged": True,
                        }
                    ),
                )
            if retained is not None:
                self.write(directory / "retained-start-reconciliation.json", encoded(retained))
            if retained_observer is not None:
                self.write(directory / "retained-observer-reconciliation.json", encoded(retained_observer))
            fd = os.open(
                self.checked(directory / "events.jsonl"),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            count, chain, summary, recorded = 0, "0" * 64, None, None
            passed, after, error_code, cancelled = False, False, None, None
            nonce, plan = None, None
            state, consumed, observer_consumed = {}, set(), set()
            allowed_boundaries = {"retirement_copy_public_libraries"} | {
                "retirement_stop_" + phase + "_" + case
                for phase in ("start", "observer", "prepare", "dispatch")
                for case in stop.CASES
            }
            try:
                for path in (directory, directory.parent, self.record):
                    parent_fd = os.open(
                        self.checked(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                    )
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)

                def bind_nonce(value):
                    nonlocal nonce, plan
                    if nonce is not None and value != nonce:
                        raise ReleaseError("retirement_stop_nonce_changed")
                    nonce = value
                    plan = probe.descriptor(bindings, nonce=nonce)

                def boundary(label):
                    nonlocal cancelled
                    if label not in allowed_boundaries:
                        raise ReleaseError("retirement_stop_boundary_rejected")
                    try:
                        self.boundary(label)
                    except ReleaseError as error:
                        if str(error) in {
                            "operator_stop",
                            "fresh_operator_permit_required",
                            "permit_timeout",
                            "controller_disconnected",
                        }:
                            cancelled = str(error)
                        raise
                    active_baseline()

                def record(event, value):
                    nonlocal count, chain, recorded
                    value = self.retirement_stop_event(event, value, bindings, probe, validation, stop)
                    value = json.loads(encoded(value))
                    if recorded is not None:
                        raise ReleaseError("retirement_events_after_summary_rejected")
                    case_id = value.get("case_id")
                    if case_id:
                        events = state.setdefault(case_id, {})
                        if event in events:
                            raise ReleaseError("retirement_stop_duplicate_event")
                        if event == "synthetic_start_intent":
                            bind_nonce(value["nonce"])
                        elif event == "synthetic_stop_actual_isolation":
                            if "synthetic_start_intent" not in events or value["pid"] <= 1:
                                raise ReleaseError("retirement_stop_event_order_rejected")
                        elif event == "synthetic_stop_exec_arrays_observed":
                            if "synthetic_stop_actual_isolation" not in events:
                                raise ReleaseError("retirement_stop_event_order_rejected")
                        elif event in {"synthetic_stop_intent", "synthetic_stop_committed"}:
                            live = events.get("synthetic_stop_actual_isolation", {})
                            if value["invocation_id"] != live.get("invocation"):
                                raise ReleaseError("retirement_stop_event_identity_rejected")
                            if "synthetic_stop_exec_arrays_observed" not in events:
                                raise ReleaseError("retirement_stop_durable_exec_arrays_required")
                            if event == "synthetic_stop_intent" and any(
                                value[k] != live.get(k) for k in ("pid", "start_ticks")
                            ):
                                raise ReleaseError("retirement_stop_event_identity_rejected")
                            if event == "synthetic_stop_committed" and "synthetic_stop_intent" not in events:
                                raise ReleaseError("retirement_stop_event_order_rejected")
                        elif (
                            event == "synthetic_stop_runner_join_observed"
                            and case_id not in consumed
                            or event == "synthetic_stop_case_result"
                            and (
                                case_id not in consumed
                                or "synthetic_stop_runner_join_observed" not in events
                                or value["exit_monotonic_us"]
                                <= events["synthetic_stop_intent"]["stop_at_monotonic_us"]
                            )
                        ):
                            raise ReleaseError("retirement_stop_event_order_rejected")
                        events[event] = value
                    if (
                        event == "retirement_stop_validation_result"
                        and value["retirement_stop_validation_passed"]
                    ):
                        for case in value["cases"]:
                            events = state.get(case["case_id"], {})
                            observed = events.get("synthetic_stop_case_result", {})
                            if (
                                case["case_id"] not in consumed
                                or case["case_id"] not in observer_consumed
                                or {k: observed.get(k) for k in case} != case
                            ):
                                raise ReleaseError("retirement_stop_durable_case_required")
                    entry = (
                        encoded(
                            {
                                "sequence": count + 1,
                                "at": int(self.now()),
                                "previous_sha256": chain,
                                "event": event,
                                "details": value,
                            }
                        )
                        + b"\n"
                    )
                    if len(entry) > 16384 or count >= 256:
                        raise ReleaseError("retirement_event_limit")
                    offset = 0
                    while offset < len(entry):
                        written = os.write(fd, entry[offset:])
                        if written <= 0:
                            raise ReleaseError("retirement_event_write_failed")
                        offset += written
                    os.fsync(fd)
                    count += 1
                    chain = digest(entry)
                    if event == "retirement_stop_validation_result":
                        recorded = value

                def run(argv, *, timeout):
                    nonlocal cancelled
                    if (
                        type(argv) is not list
                        or any(type(arg) is not str for arg in argv)
                        or type(timeout) not in {int, float}
                        or not 0 < timeout <= 15
                    ):
                        raise ReleaseError("retirement_stop_command_rejected")
                    try:
                        self.stop()
                    except ReleaseError as error:
                        cancelled = str(error)
                        raise
                    if argv == stop.VERSION_COMMAND or argv == stop.OBSERVER_INVENTORY:
                        return self.run(argv, timeout=timeout)
                    if retained_observer is not None and argv in stop.retained_observer_read_commands(
                        bindings
                    ):
                        return self.run(argv, timeout=timeout)
                    if argv == stop.TMP_MOUNT_COMMAND and any(
                        item.get("observer_dispatched") is True for item in state.values()
                    ):
                        return self.run(argv, timeout=timeout)
                    if argv == ["/usr/bin/systemctl", "show", "--property=" + validation.PROPERTIES, SERVICE]:
                        return self.run(argv, timeout=timeout)
                    if (
                        len(argv) == 4
                        and argv[:2] == ["/usr/bin/systemctl", "show"]
                        and argv[2] in {"--property=" + validation.PROPERTIES, "--property=LoadState"}
                    ):
                        match = re.fullmatch(
                            r"prism-retirement-(probe|stop-ref)-([0-9a-f]{32})-(int-executor|int-post)\.service",
                            argv[3],
                        )
                        if match:
                            bind_nonce(match[2])
                            return self.run(argv, timeout=timeout)
                    if len(argv) == 5 and argv[:3] == ["/usr/bin/systemctl", "show", "--all"]:
                        match = re.fullmatch(
                            r"prism-retirement-(probe|stop-ref)-([0-9a-f]{32})-(int-executor|int-post)\.service",
                            argv[4],
                        )
                        if match:
                            bind_nonce(match[2])
                            properties = stop.PROPERTIES if match[1] == "probe" else stop.OBSERVER_PROPERTIES
                            if argv[3] == "--property=" + properties:
                                return self.run(argv, timeout=timeout)
                    if plan is not None:
                        for case_id in stop.CASES:
                            events = state.get(case_id, {})
                            if (
                                argv == validation.launch_command(plan, case_id)
                                and "synthetic_start_intent" in events
                            ):
                                if "launch_dispatched" in events:
                                    raise ReleaseError("retirement_stop_command_already_consumed")
                                events["launch_dispatched"] = True
                                return self.run(argv, timeout=timeout)
                            if (
                                argv == stop.observer_command(plan, case_id)
                                and "synthetic_observer_start_intent" in events
                            ):
                                if "observer_dispatched" in events:
                                    raise ReleaseError("retirement_stop_command_already_consumed")
                                events["observer_dispatched"] = True
                                return self.run(argv, timeout=timeout)
                            unit = probe._case(plan, case_id)["unit"]
                            observer = stop.observer_unit(plan, case_id)
                            if (
                                argv in [*stop.exec_array_commands(unit), stop.job_command(unit)]
                                and events.get("launch_dispatched") is True
                            ) or (
                                argv == stop.job_command(observer)
                                and events.get("observer_dispatched") is True
                            ):
                                return self.run(argv, timeout=timeout)
                            if argv == ["/usr/bin/systemctl", "stop", "--no-block", unit]:
                                if "synthetic_stop_committed" not in events or case_id in consumed:
                                    raise ReleaseError("retirement_stop_command_already_consumed")
                                consumed.add(case_id)  # Before uncertain delivery: never retry.
                                return self.run(argv, timeout=timeout)
                            if argv == ["/usr/bin/systemctl", "stop", "--no-block", observer]:
                                if (
                                    "synthetic_observer_release_intent" not in events
                                    or case_id in observer_consumed
                                ):
                                    raise ReleaseError("retirement_stop_command_already_consumed")
                                try:
                                    self.boundary("retirement_stop_release_observer_" + case_id)
                                except ReleaseError as error:
                                    if str(error) in {
                                        "operator_stop",
                                        "fresh_operator_permit_required",
                                        "permit_timeout",
                                        "controller_disconnected",
                                    }:
                                        cancelled = str(error)
                                    raise
                                active_baseline()
                                observer_consumed.add(case_id)
                                return self.run(argv, timeout=timeout)
                    raise ReleaseError("retirement_stop_command_rejected")

                summary = self.retirement_stop_summary(
                    stop.run_stop_validation(
                        bindings,
                        expected_python_sha256=RETIREMENT_SYSTEM_PYTHON_SHA256,
                        boundary=boundary,
                        run=run,
                        record=record,
                        **(
                            {"retained_observer": retained_observer["observation"]["observer"]}
                            if retained_observer is not None
                            else {}
                        ),
                    ),
                    bindings,
                    probe,
                    validation,
                    stop,
                )
                if recorded != summary:
                    raise ReleaseError("retirement_durable_summary_required")
                if cancelled is not None:
                    raise ReleaseError(cancelled)
                if self.retirement_baseline() != baseline:
                    raise ReleaseError("retirement_original_baseline_changed")
                after = True
                if (
                    not summary["retirement_stop_validation_passed"]
                    or not summary["synthetic_cleanup_verified"]
                ):
                    raise ReleaseError("legacy_stop_validation_failed")
                passed = True
            except Exception as error:
                error_code = failure_code(error)
                raise
            finally:
                os.close(fd)
                report = {
                    "attempt": attempt,
                    "signal_proof": proof,
                    "summary": summary,
                    "events": count,
                    "last_event_sha256": chain,
                    "production_verified": False,
                    "passed": passed,
                    "baseline_verified_after": after,
                    "failure_code": error_code,
                }
                self.write(directory / "receipt.json", encoded(report))
                self.action_report = report

    def seed_stage_probes(self, stage):
        """Create disposable owners only in the not-yet-started private DB copy."""
        expected = self.checked(self.operations / "staging" / self.request["release_id"])
        database = self.checked(stage / "data/chat.db")
        if stage != expected or database == self.production / "data/chat.db":
            raise ReleaseError("stage_probe_database_rejected")
        if not database.is_file() or (stage / "data/secret_key").exists():
            raise ReleaseError("stage_probe_copy_required")
        secret = secrets.token_hex(32)
        accounts = [
            {
                "username": "stage_" + secrets.token_hex(12),
                "password": secrets.token_urlsafe(32),
                "conversation": secrets.token_hex(12),
                "report": secrets.token_hex(12),
            }
            for _ in range(2)
        ]
        # This is the application's existing legacy hash contract. Real HTTP
        # login upgrades each synthetic hash to bcrypt inside the isolated app.
        # No candidate or inherited package is imported by this root process.
        self.write(stage / "data/secret_key", secret.encode())
        with closing(sqlite3.connect(database)) as db, db:
            for account in accounts:
                db.execute(
                    "INSERT INTO users(username,password_hash) VALUES(?,?)",
                    [account["username"], digest((account["password"] + secret).encode())],
                )
                db.execute(
                    "INSERT INTO conversations(id,username,title,skill) VALUES(?,?,?,'prism-research')",
                    [account["conversation"], account["username"], "Prism isolated acceptance"],
                )
                db.execute(
                    "INSERT INTO messages(conv_id,role,content) VALUES(?,'assistant',?)",
                    [account["conversation"], "Prism synthetic acceptance narrative"],
                )
                db.execute(
                    "INSERT INTO saved_reports(id,username,conv_id,title,report_json,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,1,1)",
                    [
                        account["report"],
                        account["username"],
                        account["conversation"],
                        "Prism isolated acceptance",
                        '{"summary":"Prism synthetic acceptance narrative"}',
                    ],
                )
        database.chmod(0o600)
        self.write(self.record / "stage-probe-credentials.json", encoded({"accounts": accounts}))
        return accounts

    def stage_http(self, stage, method, path, body=None, cookie=""):
        """Bounded stdlib HTTP over the fixed private socket; never TCP/proxies."""
        if (
            stage != self.operations / "staging" / self.request["release_id"]
            or method not in {"GET", "POST", "PUT", "DELETE"}
            or not re.fullmatch(r"/[a-zA-Z0-9/_\-.]+", path)
        ):
            raise ReleaseError("stage_http_target_rejected")
        connection = http.client.HTTPConnection("localhost", timeout=5)
        connection.auto_open = 0  # Never fall back to TCP if the fixed UDS closes.
        channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            channel.settimeout(5)
            channel.connect(str(self.stage_socket(stage)))
            connection.sock = channel
            headers = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
            if cookie:
                headers["Cookie"] = cookie
            payload = None if body is None else encoded(body)
            if payload is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ReleaseError("stage_http_response_limit")
            cookies = SimpleCookie()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie":
                    cookies.load(value)
            # Responses and cookie values never enter the public or private
            # result record. Retain only the application's auth cookie in memory.
            token = cookies.get("token")
            result_cookie = "token=" + token.value if token is not None and token.value else ""
            if result_cookie and not re.fullmatch(r"token=[0-9a-f]{64}", result_cookie):
                raise ReleaseError("stage_http_cookie_rejected")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                # Keep the actual status for a failed/non-JSON response without
                # retaining its body (which may contain provider diagnostics).
                data = {}
            return response.status, data, result_cookie
        finally:
            connection.close()
            channel.close()

    def stage_journeys(self, stage, accounts, application):
        """Exercise only synthetic owners through real authenticated routes."""
        checks = {}
        result = {
            "schema": 1,
            "commit": self.request["commit"],
            "served_commit": "unknown",
            "checked_at": int(self.now()),
            "synthetic_owners": len(accounts),
            "checks": checks,
            "passed": False,
            "external_integrations_verified": False,
            "http_status_by_check": {},
        }
        last_status = None

        def request(method, path, body=None, cookie=""):
            nonlocal last_status
            self.stop()
            response = self.stage_http(stage, method, path, body, cookie)
            last_status = response[0]
            return response

        def require(name, condition):
            checks[name] = condition is True
            if last_status is not None:
                result["http_status_by_check"][name] = last_status
            if not checks[name]:
                raise ReleaseError("stage_journey_failed")

        try:
            require("two_synthetic_owners", len(accounts) == 2)
            status, ready, _ = request("GET", "/health/ready")
            observed = ready.get("deployed_revision") if isinstance(ready, dict) else None
            if isinstance(observed, str) and SHA.fullmatch(observed):
                result["served_commit"] = observed
            require(
                "ready_exact_commit",
                status == 200
                and ready.get("status") == "ready"
                and ready.get("deployed_revision") == self.request["commit"],
            )
            require("anonymous_denied", request("GET", "/api/me")[0] == 401)
            for index, account in enumerate(accounts):
                prefix = "owner_" + str(index + 1) + "_"
                other = accounts[1 - index]
                status, _, cookie = request(
                    "POST",
                    "/api/login",
                    {
                        "username": account["username"],
                        "password": account["password"],
                    },
                )
                require(prefix + "login", status == 200 and bool(cookie))
                status, profile, _ = request("GET", "/api/me", cookie=cookie)
                require(
                    prefix + "identity",
                    status == 200
                    and profile.get("user") == account["username"]
                    and profile.get("is_admin") is False,
                )
                status, conversations, _ = request("GET", "/api/convs", cookie=cookie)
                require(
                    prefix + "conversation_list",
                    status == 200
                    and isinstance(conversations, list)
                    and [row.get("id") for row in conversations] == [account["conversation"]],
                )
                status, history, _ = request("GET", "/api/convs/" + account["conversation"], cookie=cookie)
                require(
                    prefix + "history",
                    status == 200
                    and history.get("id") == account["conversation"]
                    and any(
                        row.get("content") == "Prism synthetic acceptance narrative"
                        for row in history.get("messages", [])
                    ),
                )
                status, reports, _ = request("GET", "/api/reports/library", cookie=cookie)
                require(
                    prefix + "report_list",
                    status == 200
                    and [row.get("id") for row in reports.get("reports", [])] == [account["report"]],
                )
                status, report, _ = request("GET", "/api/reports/library/" + account["report"], cookie=cookie)
                require(
                    prefix + "report",
                    status == 200
                    and report.get("id") == account["report"]
                    and report.get("report", {}).get("summary") == "Prism synthetic acceptance narrative",
                )
                require(
                    prefix + "foreign_conversation",
                    request("GET", "/api/convs/" + other["conversation"], cookie=cookie)[0] == 404,
                )
                require(
                    prefix + "foreign_report",
                    request("GET", "/api/reports/library/" + other["report"], cookie=cookie)[0] == 404,
                )
                if application == "server.recovery:app":
                    for method, path in (
                        ("POST", "/api/convs"),
                        ("PUT", "/api/watchlist"),
                        ("DELETE", "/api/reports/library/" + account["report"]),
                    ):
                        require(prefix + "recovery_" + method, request(method, path, {}, cookie)[0] == 503)
                    require(
                        prefix + "shared_file_denied",
                        request(
                            "GET",
                            "/api/reports/" + account["conversation"] + "/stage-shared.html",
                            cookie=cookie,
                        )[0]
                        == 404,
                    )
                elif application == "server.main:app":
                    status, created, _ = request("POST", "/api/convs", {}, cookie)
                    created_id = created.get("id", "")
                    require(
                        prefix + "create", status == 200 and bool(re.fullmatch(r"[0-9a-f]{24}", created_id))
                    )
                    path = "/api/convs/" + created_id
                    require(prefix + "created_read", request("GET", path, cookie=cookie)[0] == 200)
                    require(
                        prefix + "update",
                        request("PUT", path + "/skill", {"skill": "prism-research"}, cookie)[0] == 200,
                    )
                    require(prefix + "delete", request("DELETE", path, cookie=cookie)[0] == 200)
                    require(prefix + "deleted_hidden", request("GET", path, cookie=cookie)[0] == 404)
                else:
                    raise ReleaseError("stage_application_rejected")
                require(prefix + "logout", request("POST", "/api/logout", {}, cookie)[0] == 200)
                require(prefix + "session_revoked", request("GET", "/api/me", cookie=cookie)[0] == 401)
            result["passed"] = True
        except Exception as error:
            # No HTTP body, exception message, synthetic password or cookie.
            result["check_count"] = len(checks)
            self.write(self.record / "stage-journeys.json", encoded(result))
            if isinstance(error, ReleaseError) and str(error) in {
                "operator_stop",
                "fresh_operator_permit_required",
                "permit_timeout",
            }:
                raise
            raise ReleaseError("stage_journey_failed_check_private_record") from None
        result["check_count"] = len(checks)
        self.write(self.record / "stage-journeys.json", encoded(result))
        return digest(encoded(result))

    def stage(self):
        self.verify_tree()
        stage = self.checked(self.operations / "staging" / self.request["release_id"])
        socket = self.stage_socket(stage)
        if stage.exists():
            raise ReleaseError("stage_already_exists")
        source_db = self.checked(self.production / "data/chat.db")
        self.space(self.operations, source_db.stat().st_size * 3)
        self.boundary("stage_private_copy")
        self.stage_account()
        self.public_directory(stage.parent)
        stage.mkdir(mode=0o700)
        data = stage / "data"
        data.mkdir(mode=0o700)
        with (
            closing(sqlite3.connect(source_db.as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(data / "chat.db")) as destination,
            destination,
        ):
            source.backup(destination)
            tables = {
                row[0] for row in destination.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "sessions" in tables:
                destination.execute("DELETE FROM sessions")
            if "capability_runs" in tables:
                destination.execute("UPDATE capability_runs SET revoked=1")
        accounts = self.seed_stage_probes(stage)
        # A copied DB is for migration/auth/history checks only. Runtime state is not copied.
        home = stage / "home"
        home.mkdir(mode=0o700)
        self.run(["/usr/bin/chown", "-R", "prism-stage:prism-stage", str(stage)])
        self.run(["/usr/bin/chown", "prism-stage:ubuntu", str(stage)])
        stage.chmod(0o750)
        self.boundary("stage_isolated_service")
        production_pid = self.process_id(SERVICE)
        nonce = secrets.token_hex(16)
        issued_at = int(self.now())
        application = self.application(self.release, self.record)
        self.run(
            [
                "/usr/bin/systemd-run",
                "--unit=prism-stage-" + self.request["commit"][:12],
                "--collect",
                "--uid=prism-stage",
                "--gid=prism-stage",
                "--property=WorkingDirectory=" + str(self.release),
                "--property=NoNewPrivileges=yes",
                "--property=PrivateNetwork=yes",
                "--property=ProtectHome=yes",
                "--property=ProtectProc=invisible",
                "--property=PrivateTmp=yes",
                "--property=ProtectSystem=strict",
                "--property=ReadWritePaths=" + str(stage),
                "--property=TemporaryFileSystem=/run:ro",
                "--property=MemoryMax=768M",
                "--property=TasksMax=64",
                "--property=CPUQuota=100%",
                "--property=RuntimeMaxSec=1800",
                "--property=KillMode=control-group",
                "--property=UMask=0077",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                "HOME=" + str(home),
                "STOCK_DATA_DIR=" + str(data),
                "STOCK_LOG_FILE=" + str(stage / "app.log"),
                "CLAUDE_BIN=/nonexistent/prism-stage-claude",
                "CLAUDE_SKILLS_HOME=" + str(home / "skills"),
                "PRISM_ASTOCK_PROJECT=/nonexistent/prism-stage-astock",
                "PRISM_VIBE_ENGINE_URL=http://127.0.0.1:9",
                "PRISM_INTERNAL_API_URL=http://127.0.0.1:18888",
                "PYTHONDONTWRITEBYTECODE=1",
                "PRISM_SINGLE_WORKER=1",
                "PRISM_ENVIRONMENT=staging",
                "PRISM_COOKIE_SECURE=0",
                str(self.release / ".venv/bin/python"),
                "-I",
                "-B",
                "-S",
                "-c",
                STAGE_BOOTSTRAP,
                self.request["commit"],
                self.request["release_id"],
                str(production_pid),
                nonce,
                str(issued_at),
                application,
            ]
        )
        deadline = time.monotonic() + 30
        while not socket.exists() and time.monotonic() < deadline:
            self.stop()
            time.sleep(0.1)
        if not self.is_socket(socket):
            raise ReleaseError("stage_socket_unavailable")
        identity = self.stage_process_identity(production_pid)
        if identity.get("read_only_private_runtime_mount") is not True:
            raise ReleaseError("stage_private_runtime_mount_required")
        proof = self.stage_proof(stage, nonce, issued_at, production_pid, identity["stage_pid"], application)
        self.write(self.record / "stage-isolation.json", encoded({"proof": proof, "identity": identity}))
        self.publish_stage_socket(socket)
        journeys_digest = self.stage_journeys(stage, accounts, application)
        self.write(
            self.record / "staged.json",
            encoded(
                {
                    "socket": str(socket),
                    "external_integrations_verified": False,
                    "isolation_proof_sha256": digest(encoded(proof)),
                    "journeys_sha256": journeys_digest,
                    "read_only_private_runtime_mount": True,
                }
            ),
        )

    def process_id(self, unit):
        value = self.run(["/usr/bin/systemctl", "show", "--value", "--property=MainPID", unit]).strip()
        if not re.fullmatch(rb"[1-9][0-9]{0,9}", value) or int(value) <= 1:
            raise ReleaseError("stage_process_identity_unavailable")
        return int(value)

    def stage_runtime_mount(self, process):
        """Inspect the verified PID's actual mount, never a systemd flag."""
        runtime = process / "root/run"  # /proc/PID/root is an intentional kernel link.
        try:
            descriptor = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                private = os.fstat(descriptor)
                filesystem = os.fstatvfs(descriptor)
                current = runtime.stat()
                host = (self.root / "run").stat()
                if (
                    not stat.S_ISDIR(private.st_mode)
                    or private.st_uid != 0
                    or private.st_mode & 0o022
                    or (private.st_dev, private.st_ino) == (host.st_dev, host.st_ino)
                    or (private.st_dev, private.st_ino) != (current.st_dev, current.st_ino)
                    or not filesystem.f_flag & os.ST_RDONLY
                ):
                    raise ReleaseError("stage_private_runtime_mount_rejected")
            finally:
                os.close(descriptor)
        except OSError:
            raise ReleaseError("stage_private_runtime_mount_unavailable") from None
        return True

    def stage_process_identity(self, production_pid):
        unit = "prism-stage-" + self.request["commit"][:12]
        stage_pid = self.process_id(unit)
        user = self.run(["/usr/bin/systemctl", "show", "--value", "--property=User", unit]).strip()
        process = self.root / "proc" / str(stage_pid)
        production = self.root / "proc" / str(production_pid)
        if (
            self.process_id(SERVICE) != production_pid
            or stage_pid == production_pid
            or user != b"prism-stage"
            or process.stat().st_uid != pwd.getpwnam("prism-stage").pw_uid
            or (process / "ns/net").readlink() == (production / "ns/net").readlink()
        ):
            raise ReleaseError("stage_process_identity_mismatch")
        runtime_verified = self.stage_runtime_mount(process)
        if runtime_verified is not True:
            raise ReleaseError("stage_private_runtime_mount_required")
        if (
            self.process_id(unit) != stage_pid
            or self.process_id(SERVICE) != production_pid
            or process.stat().st_uid != pwd.getpwnam("prism-stage").pw_uid
            or (process / "ns/net").readlink() == (production / "ns/net").readlink()
        ):
            raise ReleaseError("stage_process_identity_mismatch")
        return {
            "stage_pid": stage_pid,
            "production_pid": production_pid,
            "dedicated_user": True,
            "separate_network_namespace": True,
            "read_only_private_runtime_mount": runtime_verified,
            "checked_at": int(self.now()),
        }

    def stage_proof(self, stage, nonce, issued_at, production_pid, stage_pid, application):
        path = self.checked(stage / "isolation-proof.json")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or stat.S_IMODE(info.st_mode) != 0o600:
                raise ReleaseError("stage_isolation_proof_rejected")
            proof = json.loads(source.read(4097))
        if not isinstance(proof, dict):
            raise ReleaseError("stage_isolation_proof_rejected")
        expected = {
            "schema": 1,
            "commit": self.request["commit"],
            "release_id": self.request["release_id"],
            "nonce": nonce,
            "issued_at": issued_at,
            "production_pid": production_pid,
            "stage_pid": stage_pid,
            "application": application,
        }
        allowed = {
            "production_environment": {"permission_denied", "operation_denied", "not_visible"},
            "production_process_environment": {"permission_denied", "operation_denied", "not_visible"},
            "production_http": {
                "connection_refused",
                "network_unreachable",
                "host_unreachable",
                "permission_denied",
                "operation_denied",
            },
            "sidecar_http": {
                "connection_refused",
                "network_unreachable",
                "host_unreachable",
                "permission_denied",
                "operation_denied",
            },
            "candidate_write": {"permission_denied", "operation_denied", "read_only_filesystem"},
            "private_runtime_write": {"permission_denied", "operation_denied", "read_only_filesystem"},
        }
        checks = proof.get("checks", {})
        if (
            set(proof) != {*expected, "checked_at", "checks"}
            or any(
                type(proof.get(key)) is not type(value) or proof[key] != value
                for key, value in expected.items()
            )
            or type(proof.get("checked_at")) is not int
            or not issued_at <= proof["checked_at"] <= int(self.now()) <= issued_at + 120
            or not isinstance(checks, dict)
            or set(checks) != set(allowed)
            or any(
                not isinstance(checks[name], dict)
                or set(checks[name]) != {"blocked", "errno"}
                or checks[name]["blocked"] is not True
                or not isinstance(checks[name]["errno"], str)
                or checks[name]["errno"] not in categories
                for name, categories in allowed.items()
            )
        ):
            raise ReleaseError("stage_isolation_proof_rejected")
        return proof

    @staticmethod
    def stage_socket(stage):
        socket = stage / "stage.sock"
        if len(str(socket).encode()) >= 104:
            raise ReleaseError("stage_socket_path_too_long")
        return socket

    @staticmethod
    def is_socket(path):
        return stat.S_ISSOCK(path.lstat().st_mode)

    def publish_stage_socket(self, path):
        # The stage UID can replace this filename. Pin an inode with Linux O_PATH
        # and mutate only our own proc-fd reference, never the replaceable name.
        if not hasattr(os, "O_PATH"):
            raise ReleaseError("stage_socket_pinning_unavailable")
        descriptor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            original = os.fstat(descriptor)
            uid = pwd.getpwnam("prism-stage").pw_uid

            def same_socket():
                current = path.lstat()
                return stat.S_ISSOCK(current.st_mode) and (current.st_dev, current.st_ino) == (
                    original.st_dev,
                    original.st_ino,
                )

            if (
                not stat.S_ISSOCK(original.st_mode)
                or original.st_uid != uid
                or original.st_nlink != 1
                or not same_socket()
            ):
                raise ReleaseError("stage_socket_identity_rejected")
            pinned = "/proc/self/fd/" + str(descriptor)
            os.chown(pinned, uid, grp.getgrnam("ubuntu").gr_gid)
            os.chmod(pinned, 0o660)
            if not same_socket() or os.fstat(descriptor).st_nlink != 1:
                raise ReleaseError("stage_socket_identity_rejected")
        finally:
            os.close(descriptor)

    def switch(self):
        self.verify_tree()
        self.receipt("backup")
        self.receipt("compatibility")
        stage = self.receipt("stage")
        if (
            stage.get("isolated") is not True
            or stage.get("journeys_passed") is not True
            or not (self.record / "staged.json").exists()
        ):
            raise ReleaseError("stage_verification_required")
        self.assert_admission()
        expected = self.request["receipts"].get("expected_source_manifest_sha256")
        current = self.runtime()
        if current["fingerprint"] != self.request["receipts"].get("expected_current_fingerprint"):
            raise ReleaseError("current_runtime_changed")
        if (
            self.source_inventory(self.request.get("source_manifest", {}), Path(current["working_directory"]))
            != expected
        ):
            raise ReleaseError("source_manifest_receipt_mismatch")
        self.no_active_tasks()
        self.recovery_target()
        self.run(["/usr/bin/systemctl", "is-active", "--quiet", SERVICE])
        if (self.record / "previous.json").exists():
            raise ReleaseError("switch_already_attempted")
        previous = {
            "existed": self.checked(self.override).exists(),
            "content": base64.b64encode(self.override.read_bytes()).decode()
            if self.override.exists()
            else "",
        }
        self.boundary("production_override")
        # Must remain private: an old drop-in may contain credentials.
        self.write(self.record / "previous.json", encoded(previous))
        override = self.dropin(self.release)
        self.write(
            self.record / "cutover.json",
            encoded(
                {"at": int(self.now()), "previous_runtime": current, "override_sha256": digest(override)}
            ),
        )
        self.write(self.override, override, 0o600)
        self.journal("cutover_config_written", "completed")
        self.boundary("production_daemon_reload")
        self.run(["/usr/bin/systemctl", "daemon-reload"])
        self.boundary("production_restart")
        self.run(["/usr/bin/systemctl", "restart", SERVICE])
        self.write(self.record / "switched.json", encoded({"at": int(self.now())}))

    def application(self, release, record):
        manifest = json.loads((record / "manifest.json").read_bytes())
        if manifest.get("commit") != release.name:
            raise ReleaseError("application_identity_mismatch")
        if str(manifest.get("version", "")).endswith("-recovery"):
            if not any(item.get("path") == "server/recovery.py" for item in manifest.get("files", [])):
                raise ReleaseError("restricted_recovery_entry_required")
            return "server.recovery:app"
        return "server.main:app"

    def dropin(self, release):
        external = (release / ".engine-revision").read_text().strip()
        if not DIGEST.fullmatch(external):
            raise ReleaseError("fixed_engine_revision_required")
        engine = self.checked(self.root / "opt/prism/engines/astock" / external)
        recovery = self.request["receipts"].get("recovery", {})
        if release.name == self.request["commit"]:
            record = self.record
        elif release.name == recovery.get("commit"):
            record = self.checked(self.operations / recovery["release_id"])
        else:
            raise ReleaseError("application_identity_mismatch")
        application = self.application(release, record)
        cli = self.verify_cli(record, release)
        # systemd EnvironmentFile wins over Environment. /usr/bin/env assigns
        # these reviewed non-secret constraints after inherited configuration.
        paths = [
            release,
            release / ".venv/bin/python",
            cli,
            engine,
            self.production / ".env",
            self.production / "data",
            self.production / "app.log",
        ]
        if any(
            not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(path))
            or any(part in {".", ".."} for part in PurePosixPath(str(path)).parts)
            or "//" in str(path)
            for path in paths
        ):
            raise ReleaseError("systemd_fixed_path_rejected")
        constraints = {
            "CLAUDE_BIN": str(cli),
            "STOCK_DATA_DIR": str(self.production / "data"),
            "STOCK_LOG_FILE": str(self.production / "app.log"),
            "PRISM_INTERNAL_API_URL": "http://127.0.0.1:8088",
            "PRISM_ASTOCK_PROJECT": str(engine),
            "PRISM_SINGLE_WORKER": "1",
            "PRISM_ENVIRONMENT": "production",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        command = "/usr/bin/env " + " ".join(name + "=" + value for name, value in constraints.items())
        command += " " + str(release / ".venv/bin/python") + " -I -B -m uvicorn " + application
        command += " --app-dir " + str(release) + " --host 127.0.0.1 --port 8088 --workers 1"
        return (
            "[Service]\nNoNewPrivileges=yes\nUMask=0077\nWorkingDirectory="
            + str(release)
            + "\nExecStart=\nExecStart="
            + command
            + "\nEnvironmentFile="
            + str(self.production / ".env")
            + "\n"
        ).encode()

    def recovery_target(self):
        recovery = self.receipt("recovery")
        if (
            not SHA.fullmatch(recovery.get("commit", ""))
            or recovery["commit"] == self.request["commit"]
            or not RELEASE_ID.fullmatch(recovery.get("release_id", ""))
        ):
            raise ReleaseError("safe_recovery_identity_required")
        if not all(
            recovery.get(key) is True
            for key in ("security_verified", "journeys_passed", "database_forward_and_backward")
        ):
            raise ReleaseError("safe_recovery_acceptance_required")
        saved = self.release, self.record, self.request
        try:
            self.release = self.checked(self.releases / recovery["commit"])
            self.record = self.checked(self.operations / recovery["release_id"])
            self.request = dict(self.request, commit=recovery["commit"])
            identity = json.loads((self.record / "identity.json").read_bytes())
            if (
                any(
                    identity.get(key) != recovery.get(key)
                    for key in ("commit", "archive_sha256", "manifest_sha256")
                )
                or not (self.record / "staged.json").exists()
            ):
                raise ReleaseError("recovery_candidate_not_bound")
            self.verify_tree()
            if self.application(self.release, self.record) != "server.recovery:app":
                raise ReleaseError("restricted_recovery_entry_required")
            return self.release
        finally:
            self.release, self.record, self.request = saved

    def verify(self):
        if not (self.record / "switched.json").exists():
            raise ReleaseError("cutover_not_recorded")
        self.verify_tree()
        journey = self.receipt("journeys")
        if (
            journey.get("served_commit") != self.request["commit"]
            or journey.get("journeys_passed") is not True
        ):
            raise ReleaseError("core_journeys_required")
        data = self.ready()
        if (
            data.get("status") not in {"ok", "ready"}
            or data.get("deployed_revision") != self.request["commit"]
        ):
            raise ReleaseError("served_revision_not_verified")
        self.write(
            self.record / "verified.json",
            encoded(
                {
                    "commit": self.request["commit"],
                    "at": int(self.now()),
                    "receipt": self.request["receipts_sha256"],
                }
            ),
        )

    def nginx_paths(self):
        result = {}
        for name in NGINX_SITES:
            expected = self.root / "etc/nginx/sites-available" / name
            enabled = self.root / "etc/nginx/sites-enabled" / name
            if enabled.resolve(strict=True) != expected or self.checked(expected).stat().st_size > 65536:
                raise ReleaseError("nginx_fixed_target_mismatch")
            if self.root == Path("/") and expected.stat().st_uid != 0:
                raise ReleaseError("nginx_root_owner_required")
            result[name] = expected
        return result

    def probe_admission(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ReleaseError("admission_redirect_rejected")

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for url in ("https://doubleh.site/api/me", "http://124.223.168.224/api/me"):
            try:
                response = opener.open(url, timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                data = response.read(4097)
                if (
                    response.code != 503
                    or response.headers.get("Retry-After") != "60"
                    or len(data) > 4096
                    or json.loads(data).get("error") != HOLD_MESSAGE
                ):
                    raise ReleaseError("admission_hold_not_observed")

    def assert_admission(self):
        record = json.loads((self.record / "admission.json").read_bytes())
        if (self.record / "resumed.json").exists():
            raise ReleaseError("admission_already_resumed")
        for name, path in self.nginx_paths().items():
            if digest(path.read_bytes()) != record["files"][name]["hold_sha256"]:
                raise ReleaseError("admission_configuration_changed")
        self.probe_admission()

    def nginx_write(self, record, destination):
        paths = self.nginx_paths()
        allowed = {
            name: {item["original_sha256"], item["hold_sha256"]} for name, item in record["files"].items()
        }
        for name, path in paths.items():
            if digest(path.read_bytes()) not in allowed[name]:
                raise ReleaseError("nginx_configuration_changed")
        for name, path in paths.items():
            item = record["files"][name]
            content = base64.b64decode(item[destination], validate=True)
            if digest(content) != item[destination + "_sha256"]:
                raise ReleaseError("private_nginx_record_corrupted")
            self.write(path, content, item["mode"])
            self.run(
                ["/usr/bin/chown", "--no-dereference", str(item["uid"]) + ":" + str(item["gid"]), str(path)]
            )

    def quiesce(self):
        self.verify_tree()
        recovery = self.recovery_target()
        stage = self.receipt("stage")
        if (
            not stage.get("isolated")
            or not stage.get("journeys_passed")
            or not (self.record / "staged.json").exists()
        ):
            raise ReleaseError("stage_verification_required")
        self.boundary("stop_accepted_staging_services")
        for commit in (self.request["commit"], recovery.name):
            unit = "prism-stage-" + commit[:12] + ".service"
            state = self.run(
                ["/usr/bin/systemctl", "show", "--property=LoadState,ActiveState", unit]
            ).decode()
            values = dict(line.split("=", 1) for line in state.splitlines() if "=" in line)
            if values.get("ActiveState") in {"active", "activating", "deactivating", "reloading"}:
                self.run(["/usr/bin/systemctl", "stop", unit])
            elif (
                values.get("ActiveState") not in {"inactive", "failed"}
                and values.get("LoadState") != "not-found"
            ):
                raise ReleaseError("staging_stop_state_unknown")
        admission = self.receipt("admission")
        path = self.record / "admission.json"
        if path.exists():
            record = json.loads(path.read_bytes())
            if any(
                record["files"][name]["original_sha256"] != admission.get("config_sha256", {}).get(name)
                for name in NGINX_SITES
            ):
                raise ReleaseError("nginx_baseline_receipt_changed")
        else:
            record = {"runtime": self.runtime(), "files": {}}
            for name, target in self.nginx_paths().items():
                original = target.read_bytes()
                if digest(original) != admission.get("config_sha256", {}).get(name):
                    raise ReleaseError("nginx_baseline_drift")
                hold = admission_config(original)
                record["files"][name] = {
                    "original": base64.b64encode(original).decode(),
                    "hold": base64.b64encode(hold).decode(),
                    "original_sha256": digest(original),
                    "hold_sha256": digest(hold),
                    "mode": stat.S_IMODE(target.stat().st_mode),
                    "uid": target.stat().st_uid,
                    "gid": target.stat().st_gid,
                }
            self.write(path, encoded(record))  # root 0700 record; contents never public
        self.boundary("admission_hold_config")
        if (self.record / "resumed.json").exists():
            (self.record / "resumed.json").unlink()
        self.nginx_write(record, "hold")
        try:
            self.run(["/usr/sbin/nginx", "-t"])
        except Exception:
            self.nginx_write(record, "original")
            raise ReleaseError("nginx_hold_test_failed_original_restored") from None
        self.boundary("admission_hold_reload")
        self.run(["/usr/bin/systemctl", "reload", "nginx.service"])
        self.assert_admission()

    def resume(self):
        record = json.loads((self.record / "admission.json").read_bytes())
        if (self.record / "resumed.json").exists():
            raise ReleaseError("admission_already_resumed")
        if (self.record / "rollback.json").exists():
            receipt = self.receipt("recovery_journeys")
            expected = self.request["receipts"]["recovery"]["commit"]
            if receipt.get("served_commit") != expected or receipt.get("journeys_passed") is not True:
                raise ReleaseError("recovery_journeys_required")
        elif (self.record / "verified.json").exists():
            expected = self.request["commit"]
        elif (
            not (self.record / "cutover.json").exists()
            and self.runtime()["fingerprint"] == record["runtime"]["fingerprint"]
        ):
            expected = None  # aborted before any service cutover; legacy runtime unchanged
        else:
            raise ReleaseError("resume_verification_required")
        if expected:
            ready = self.ready()
            if ready.get("status") not in {"ok", "ready"} or ready.get("deployed_revision") != expected:
                raise ReleaseError("resume_revision_not_ready")
        self.boundary("admission_resume_config")
        self.nginx_write(record, "original")
        try:
            self.run(["/usr/sbin/nginx", "-t"])
        except Exception:
            self.nginx_write(record, "hold")
            raise ReleaseError("nginx_resume_test_failed_hold_preserved") from None
        try:
            self.boundary("admission_resume_reload")
            self.run(["/usr/bin/systemctl", "reload", "nginx.service"])
        except Exception as error:
            self.nginx_write(record, "hold")
            if isinstance(error, ReleaseError) and str(error) in {
                "operator_stop",
                "fresh_operator_permit_required",
                "permit_timeout",
            }:
                raise
            raise ReleaseError("nginx_resume_reload_unconfirmed_reconcile_required") from None
        self.write(self.record / "resumed.json", encoded({"at": int(self.now())}))

    def rollback(self):
        self.receipt("compatibility")
        self.assert_admission()
        if not (self.record / "cutover.json").exists():
            raise ReleaseError("cutover_not_attempted")
        cutover = json.loads((self.record / "cutover.json").read_bytes())
        if (self.record / "rollback.json").exists():
            raise ReleaseError("rollback_already_completed")
        recovery = self.recovery_target()
        recovery_override = self.dropin(recovery)
        intent = {
            "recovery_commit": recovery.name,
            "override_sha256": digest(recovery_override),
            "cutover_sha256": cutover["override_sha256"],
        }
        intent_path = self.record / "recovery-intent.json"
        if intent_path.exists() and json.loads(intent_path.read_bytes()) != intent:
            raise ReleaseError("recovery_intent_changed")
        if not self.checked(self.override).is_file() or digest(self.override.read_bytes()) not in {
            cutover.get("override_sha256"),
            digest(recovery_override),
        }:
            raise ReleaseError("rollback_configuration_changed")
        self.boundary("recovery_override")
        self.write(intent_path, encoded(intent))
        self.write(self.override, recovery_override, 0o600)
        self.journal("recovery_config_written", "completed")
        self.boundary("recovery_daemon_reload")
        self.run(["/usr/bin/systemctl", "daemon-reload"])
        self.boundary("recovery_restart")
        self.run(["/usr/bin/systemctl", "restart", SERVICE])
        self.write(
            self.record / "rollback.json",
            encoded({"at": int(self.now()), "recovery_commit": recovery.name, "verification_required": True}),
        )
        # A completed rollback action is not a verified release. No DB restore.

    def execute(self, request):
        if (
            request.get("action") not in ACTIONS
            or not SHA.fullmatch(request.get("commit", ""))
            or not RELEASE_ID.fullmatch(request.get("release_id", ""))
        ):
            raise ReleaseError("invalid_request")
        self.request = request
        self.action_report = None
        self.release = self.checked(self.releases / request["commit"])
        self.record = self.checked(self.operations / request["release_id"])
        if request["action"] == "inspect":
            return {
                "runtime": self.runtime(),
                "release_exists": self.release.exists(),
                "stop_requested": (self.operations / "STOP").exists(),
                "claude_binary_exists": (self.root / "home/ubuntu/.npm-global/bin/claude").is_file(),
                "production_verified": False,
            }
        self.receipt("backup")
        self.receipt("compatibility")
        self.boundary("begin_" + request["action"])
        self.public_directory(self.operations)
        lock = self.checked(self.operations / "writer.lock").open("a")
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReleaseError("another_writer_active") from None
            self.record.mkdir(mode=0o700, exist_ok=True)
            if self.root == Path("/") and self.record.stat().st_uid != 0:
                raise ReleaseError("root_owned_record_required")
            self.record.chmod(0o700)
            identity = self.record / "identity.json"
            bound = {key: request.get(key) for key in ("commit", "archive_sha256", "manifest_sha256")}
            if identity.exists() and json.loads(identity.read_bytes()) != bound:
                raise ReleaseError("release_identity_changed")
            if not identity.exists():
                self.write(identity, encoded(bound))
            self.journal(request["action"], "started")
            try:
                getattr(self, request["action"])()
                result = self.journal(
                    "verified" if request["action"] == "verify" else request["action"], "completed"
                )
                if request["action"] == "diagnose_build":
                    result["diagnostics"] = self.action_report
                elif request["action"] == "finish_stage":
                    result["stage_cleanup"] = self.action_report
                elif request["action"] in INTEGRATION_PROBES or request["action"] in {
                    "integration_live",
                    "integration_market",
                }:
                    result["integration"] = self.action_report
                elif request["action"] in {
                    "integration_live_network_preflight",
                    "integration_market_network_preflight",
                }:
                    result["network_preflight"] = self.action_report
                elif request["action"] == "validate_legacy_retirement":
                    result["legacy_retirement"] = self.action_report
                elif request["action"] == "validate_legacy_stop":
                    result["legacy_stop"] = self.action_report
                return result
            except Exception as error:
                self.journal(
                    request["action"],
                    "cancelled"
                    if isinstance(error, ReleaseError)
                    and str(error) in {"operator_stop", "fresh_operator_permit_required", "permit_timeout"}
                    else "failed",
                    failure=error,
                )
                raise
        finally:
            lock.close()


def main():
    if os.geteuid() != 0 or sys.platform != "linux":
        raise ReleaseError("root_executor_required")
    length = json.loads(sys.stdin.buffer.readline(128)).get("length", 0)
    if not isinstance(length, int) or not 0 < length <= MAX_INPUT:
        raise ReleaseError("input_limit")
    memory = Path("/proc/meminfo").read_text()
    available = int(re.search(r"^MemAvailable:\s+(\d+) kB$", memory, re.M)[1]) * 1024
    if available < length * 3 + 256 * 1024 * 1024:
        raise ReleaseError("insufficient_memory_reserve")
    raw = sys.stdin.buffer.read(length)
    if len(raw) != length or sys.stdin.buffer.read(1) != b"\n":
        raise ReleaseError("input_truncated")

    def read_control():
        line = sys.stdin.buffer.readline(1024)
        if not line:
            raise ReleaseError("controller_disconnected")
        return json.loads(line)

    def control():
        if select.select([sys.stdin.buffer], [], [], 0)[0]:
            if read_control().get("stop") is True:
                return True
            raise ReleaseError("unexpected_control_message")
        return False

    def permit(nonce, boundary):
        print(json.dumps({"permit_required": nonce, "boundary": boundary}), flush=True)
        if not select.select([sys.stdin.buffer], [], [], 30)[0]:
            raise ReleaseError("permit_timeout")
        return read_control().get("permit")

    result = RemoteRelease(permit=permit, control=control).execute(json.loads(raw))
    print(json.dumps({"result": result}), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps({"error": failure_code(error)}),
            flush=True,
        )
        raise SystemExit(1) from None
