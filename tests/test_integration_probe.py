"""Offline probe contracts. Fakes do not certify real Linux or Astock support."""

import contextlib
import hashlib
import json
import os
import ssl
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
probe = types.ModuleType("integration_probe")
probe.__file__ = str(ROOT / "scripts/integration_probe.py")
exec(compile(Path(probe.__file__).read_bytes(), probe.__file__, "exec"), probe.__dict__)
CANDIDATE, ENGINE, NONCE = "a" * 40, "b" * 64, "c" * 32


def arguments(work):
    return types.SimpleNamespace(
        candidate=CANDIDATE,
        engine=ENGINE,
        workdir=str(work),
        nonce=NONCE,
        production_pid=123,
        issued_at=1800000000,
        probe_sha256="d" * 64,
    )


@pytest.fixture
def work(tmp_path):
    path = tmp_path / "work"
    path.mkdir(mode=0o755)
    for name in ("output", "home", "tmp", "data"):
        (path / name).mkdir(mode=0o700)
    return path


def test_fixed_paths_only_accept_exact_controller_location():
    location = "/var/lib/prism-operations/integration/release-test-001/astock"
    candidate, engine, work = probe.fixed_paths(CANDIDATE, ENGINE, location)
    assert candidate == Path("/opt/prism/releases") / CANDIDATE
    assert engine == Path("/opt/prism/engines/astock") / ENGINE
    assert str(work) == location


@pytest.mark.parametrize(
    "location",
    [
        "/tmp/astock",
        "/home/ubuntu/stock-analyzer/data/astock",
        "/var/lib/prism-operations/integration/.hidden-one/astock",
        "/var/lib/prism-operations/integration/release-test-001/../astock",
        "/var/lib/prism-operations/integration/release-test-001/astock/",
        "/var/lib/prism-operations/integration/release-test-001/other",
    ],
)
def test_arbitrary_and_normalized_work_paths_are_rejected(location):
    with pytest.raises(probe.ProbeError):
        probe.fixed_paths(CANDIDATE, ENGINE, location)


def test_invalid_candidate_or_engine_is_rejected():
    for candidate, engine in [("a" * 39, ENGINE), (CANDIDATE, "B" * 64)]:
        with pytest.raises(probe.ProbeError):
            probe.fixed_paths(candidate, engine, "/var/lib/prism-operations/integration/release-001/astock")


@pytest.mark.parametrize(
    "wrong", ["platform", "isolated", "no_site", "bytecode", "interpreter", "root", "ubuntu"]
)
def test_runtime_requires_prepared_python_and_dedicated_user(work, monkeypatch, wrong):
    candidate = Path("/opt/prism/releases") / CANDIDATE
    runtime = types.SimpleNamespace(
        platform="darwin" if wrong == "platform" else "linux",
        executable="/usr/bin/python3" if wrong == "interpreter" else str(candidate / ".venv/bin/python"),
        flags=types.SimpleNamespace(
            isolated=wrong != "isolated",
            no_site=wrong != "no_site",
            dont_write_bytecode=wrong != "bytecode",
        ),
    )
    account = types.SimpleNamespace(
        pw_name="ubuntu" if wrong == "ubuntu" else "prism-integration",
        pw_uid=0 if wrong == "root" else 991,
        pw_gid=991,
        pw_shell="/usr/sbin/nologin",
    )
    monkeypatch.setattr(probe, "sys", runtime)
    monkeypatch.setattr(probe.pwd, "getpwuid", lambda _: account)
    monkeypatch.setattr(probe.os, "getgroups", lambda: [991])
    with pytest.raises(probe.ProbeError):
        probe.validate_runtime(candidate, Path("/engine"), work)


def test_unprivileged_owned_source_is_not_root_locked(work):
    if os.getuid() == 0:
        pytest.skip("this negative test requires the unprivileged offline runner")
    with pytest.raises(probe.ProbeError, match="unlocked_source_rejected"):
        probe.root_locked(work)


def test_probe_source_requires_exact_root_supplied_digest_and_filename(work, monkeypatch):
    source = work / "probe.py"
    source.write_bytes(b"inert synthetic source, never executed")
    monkeypatch.setattr(probe, "root_locked", lambda _: None)
    monkeypatch.setattr(probe, "__file__", str(source))
    probe.validate_probe_source(work, hashlib.sha256(source.read_bytes()).hexdigest())
    with pytest.raises(probe.ProbeError, match="trusted_probe_source_required"):
        probe.validate_probe_source(work, "e" * 64)


def test_probe_source_never_follows_symlink(work):
    (work / "probe.py").symlink_to(work / "output/missing.py")
    with pytest.raises(probe.ProbeError, match="symlink_path_rejected"):
        probe.validate_probe_source(work, "e" * 64)


def test_private_write_is_exclusive_and_rejects_symlinks(work):
    output = work / "output/result.json"
    probe.write_private(output, {"passed": False})
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        probe.write_private(output, {"passed": True})
    link = work / "output/link.json"
    link.symlink_to(output)
    with pytest.raises(OSError):
        probe.write_private(link, {"passed": True})
    assert json.loads(output.read_bytes()) == {"passed": False}


def test_native_and_late_output_stays_in_private_log(work):
    source = """import os,pathlib,runpy,sys
module=runpy.run_path(sys.argv[1],run_name='probe_module')
summary=os.dup(1)
with module['private_log'](pathlib.Path(sys.argv[2])):
    print('synthetic import log')
os.write(1,b'late native log\\n')
os.write(summary,b'{\"passed\":false}\\n')
os.close(summary)
"""
    log = work / "output/private.log"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", source, probe.__file__, str(log)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(work / "home")},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == b'{"passed":false}\n' and not result.stderr
    assert "synthetic import log" in log.read_text() and "late native log" in log.read_text()


def test_open_canary_never_reads_successful_descriptor(work, monkeypatch):
    source = work / "source"
    source.write_text("synthetic unread content")
    monkeypatch.setattr(probe.os, "read", mock.Mock(side_effect=AssertionError("must never read")))
    assert probe.denied_open(source) is False
    canary = work / "new-canary"
    assert probe.denied_open(canary, writable=True) is False
    assert not canary.exists()
    assert source.read_text() == "synthetic unread content"


def test_bootstrap_proof_and_root_allow_are_exactly_bound(work, monkeypatch):
    args = arguments(work)
    monkeypatch.setattr(probe, "denied_open", lambda *a, **k: True)
    monkeypatch.setattr(probe, "denied_connect", lambda *a: True)
    real_fstat = probe.os.fstat

    def allow_created(_):
        proof = json.loads((work / "output/bootstrap-proof.json").read_bytes())
        allow = {
            "schema": 1,
            "candidate": CANDIDATE,
            "engine": ENGINE,
            "nonce": NONCE,
            "pid": os.getpid(),
            "probe_sha256": args.probe_sha256,
            "bootstrap_proof_sha256": hashlib.sha256(probe.encoded(proof)).hexdigest(),
            "allowed_at": args.issued_at,
        }
        (work / "root-allow.json").write_bytes(probe.encoded(allow))
        (work / "root-allow.json").chmod(0o444)

    def root_fstat(descriptor):
        info = real_fstat(descriptor)
        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        values["st_uid"] = 0
        return types.SimpleNamespace(**values)

    monkeypatch.setattr(probe.os, "fstat", root_fstat)
    probe.bootstrap_gate(
        args, Path("/opt/prism/releases") / CANDIDATE, work, now=lambda: args.issued_at, pause=allow_created
    )
    proof = json.loads((work / "output/bootstrap-proof.json").read_bytes())
    assert set(proof["checks"]) == {
        "production_environment_denied",
        "production_process_environment_denied",
        "production_http_denied",
        "sidecar_http_denied",
        "candidate_write_denied",
        "staging_directory_denied",
    }
    assert all(value is True for value in proof["checks"].values())
    assert proof["probe_sha256"] == args.probe_sha256


@pytest.mark.parametrize("corruption", ["nonce", "pid", "hash", "owner", "mode", "extra", "time", "bool"])
def test_bootstrap_never_imports_after_invalid_root_allow(work, monkeypatch, corruption):
    args = arguments(work)
    monkeypatch.setattr(probe, "denied_open", lambda *a, **k: True)
    monkeypatch.setattr(probe, "denied_connect", lambda *a: True)
    real_fstat = probe.os.fstat

    def create(_):
        proof = json.loads((work / "output/bootstrap-proof.json").read_bytes())
        allow = {key: proof[key] for key in ("schema", "candidate", "engine", "nonce", "pid", "probe_sha256")}
        allow.update(
            bootstrap_proof_sha256=hashlib.sha256(probe.encoded(proof)).hexdigest(), allowed_at=args.issued_at
        )
        if corruption == "nonce":
            allow["nonce"] = "e" * 32
        elif corruption == "pid":
            allow["pid"] += 1
        elif corruption == "hash":
            allow["bootstrap_proof_sha256"] = "e" * 64
        elif corruption == "extra":
            allow["bypass"] = True
        elif corruption == "time":
            allow["allowed_at"] = args.issued_at + 1
        elif corruption == "bool":
            allow["schema"] = True
        (work / "root-allow.json").write_bytes(probe.encoded(allow))
        (work / "root-allow.json").chmod(0o644 if corruption == "mode" else 0o444)

    def fstat(descriptor):
        info = real_fstat(descriptor)
        values = {key: getattr(info, key) for key in dir(info) if key.startswith("st_")}
        values["st_uid"] = 999 if corruption == "owner" else 0
        return types.SimpleNamespace(**values)

    monkeypatch.setattr(probe.os, "fstat", fstat)
    with pytest.raises(probe.ProbeError, match="root_allow_rejected"):
        probe.bootstrap_gate(args, Path("/candidate"), work, now=lambda: args.issued_at, pause=create)


def test_bootstrap_unsafe_canary_fails_before_root_gate(work, monkeypatch):
    monkeypatch.setattr(probe, "denied_open", lambda *a, **k: False)
    monkeypatch.setattr(probe, "denied_connect", lambda *a: True)
    with pytest.raises(probe.ProbeError, match="bootstrap_isolation_rejected"):
        probe.bootstrap_gate(arguments(work), Path("/candidate"), work, now=lambda: 1800000000)
    assert not (work / "root-allow.json").exists()


@pytest.mark.parametrize("key", ["DEEPSEEK_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ADMIN_PASSWord", "LLM_API_KEY"])
def test_credential_environment_is_rejected_before_scrubbing(work, monkeypatch, key):
    monkeypatch.setenv(key, "synthetic-never-printed")
    with pytest.raises(probe.ProbeError, match="credential_environment_rejected"):
        probe.synthetic_environment(work, Path("/engine"))


def test_environment_has_only_synthetic_paths_and_no_inherited_proxy(work, monkeypatch):
    with mock.patch.dict(
        os.environ, {"HOME": "/home/ubuntu", "HTTPS_PROXY": "http://unused.invalid"}, clear=True
    ):
        monkeypatch.chdir(work)
        probe.synthetic_environment(work, Path("/engine"))
        assert os.environ["HOME"] == str(work / "home")
        assert os.environ["STOCK_DATA_DIR"] == str(work / "data")
        assert os.environ["PRISM_ASTOCK_PROJECT"] == "/engine"
        assert "HTTPS_PROXY" not in os.environ
        assert "API_KEY" not in " ".join(os.environ)


@pytest.mark.parametrize("name", ["bad.pth", "sitecustomize.py", "nested/patch_ssl.pyc", "usercustomize.py"])
def test_inherited_startup_hooks_are_rejected_without_importing(tmp_path, name):
    site = tmp_path / ".venv/lib/python3.12/site-packages"
    path = site / name
    path.parent.mkdir(parents=True)
    path.write_text("raise AssertionError('must not execute')")
    with pytest.raises(probe.ProbeError, match="startup_hook_rejected"):
        probe.startup_clean(tmp_path)


def fake_imports(candidate, engine, work, monkeypatch):
    site_dir = candidate / ".venv/lib/python3.12/site-packages"
    site_dir.mkdir(parents=True)
    base = candidate.parent / "python-base"
    base.mkdir()
    monkeypatch.setattr(sys, "base_prefix", str(base))
    monkeypatch.setattr(sys, "prefix", str(candidate / ".venv"))
    monkeypatch.setattr(sys, "path", [str(base / "lib/python3.12"), str(site_dir)])
    monkeypatch.setenv("HOME", str(work / "home"))
    monkeypatch.chdir(work)
    import site

    monkeypatch.setattr(site, "main", lambda: None)
    modules = {}
    for name in (
        "server",
        "server.analysis_engine",
        "server.config",
        "server.database",
        "tradingagents",
        "tradingagents.default_config",
        "tradingagents.graph.trading_graph",
    ):
        module = types.ModuleType(name)
        root = candidate if name.startswith("server") else engine
        module.__file__ = str(root / (name.replace(".", "/") + ".py"))
        modules[name] = module
    modules["server.analysis_engine"]._ensure_astock = mock.Mock()
    results, cache = work / "data/results", work / "data/cache"
    results.mkdir()
    cache.mkdir()
    build = mock.Mock(
        return_value={
            "results_dir": str(results),
            "data_cache_dir": str(cache),
            "checkpoint_enabled": False,
            "memory_log_path": None,
        }
    )
    modules["server.analysis_engine"]._build_config = build
    modules["tradingagents.default_config"].DEFAULT_CONFIG = {}

    class Graph:
        def __init__(self):
            raise AssertionError("a paid graph must never be constructed")

    modules["tradingagents.graph.trading_graph"].TradingAgentsGraph = Graph
    modules["server.config"].DATA_DIR = work / "data"
    modules["server.config"].ASTOCK_PROJECT = engine
    modules["server.config"].LOG_FILE = work / "output/probe.log"
    modules["server.database"].DB_PATH = work / "data/chat.db"
    cleaned = {
        key: value
        for key, value in sys.modules.items()
        if key.split(".")[0] not in {"server", "tradingagents"}
    }
    cleaned.update(modules)
    monkeypatch.setattr(sys, "modules", cleaned)
    monkeypatch.setattr(probe.importlib, "import_module", lambda name: modules[name])
    return modules


def test_astock_import_uses_frozen_modules_and_synthetic_config(work, tmp_path, monkeypatch):
    candidate, engine = tmp_path / "candidate", tmp_path / "engine"
    modules = fake_imports(candidate, engine, work, monkeypatch)
    probe.import_engine(candidate, engine, work)
    modules["server.analysis_engine"]._ensure_astock.assert_called_once_with()
    modules["server.analysis_engine"]._build_config.assert_called_once_with("fast", analysis_id="0" * 32)
    assert sys.path[:2] == [str(candidate), str(engine)]


def test_astock_wrong_module_origin_is_not_treated_as_success(work, tmp_path, monkeypatch):
    candidate, engine = tmp_path / "candidate", tmp_path / "engine"
    modules = fake_imports(candidate, engine, work, monkeypatch)
    modules["tradingagents.graph.trading_graph"].__file__ = "/home/ubuntu/old-engine.py"
    with pytest.raises(probe.ProbeError, match="module_source_mismatch"):
        probe.import_engine(candidate, engine, work)


@pytest.mark.parametrize(
    "event,args",
    [
        ("open", ("/home/ubuntu/secret", "r", 0)),
        ("open", ("/tmp/.env", "r", 0)),
        ("open", ("/tmp/real.db", "r", 0)),
        ("open", ("/opt/prism/candidate.py", "w", os.O_WRONLY)),
        ("sqlite3.connect", ("/home/ubuntu/chat.db",)),
        ("socket.connect", (None, ("127.0.0.1", 8088))),
        ("socket.connect", (None, ("1.1.1.1", 443))),
        ("subprocess.Popen", ("/bin/sh", ["/bin/sh"], None, {})),
    ],
)
def test_audit_guard_denies_source_reads_and_unbounded_execution(work, event, args):
    with pytest.raises(probe.ProbeError):
        probe.audit_policy(work, {"tls_port": 12345})(event, args)


def test_audit_guard_only_allows_private_writes_and_fixture_connection(work):
    guard = probe.audit_policy(work, {"tls_port": 12345})
    guard("open", (str(work / "data/secret_key"), "w", os.O_WRONLY))
    guard("open", ("/dev/null", "w", os.O_RDWR))
    guard("socket.connect", (None, ("127.0.0.1", 12345)))
    with pytest.raises(probe.ProbeError):
        guard("open", (str(work / "root-allow.json"), "w", os.O_WRONLY))


def test_only_real_certificate_verification_exception_counts():
    cert = ssl.SSLCertVerificationError(1, "synthetic certificate rejection")
    outer = RuntimeError("request failed", cert)
    assert probe.certificate_rejection(outer) is True
    assert probe.certificate_rejection(RuntimeError("CERTIFICATE_VERIFY_FAILED")) is False
    assert probe.certificate_rejection(TimeoutError()) is False


@pytest.mark.parametrize("bad_client", [None, "urllib", "requests", "httpx", "timeout"])
def test_tls_negative_control_rejects_accepted_and_unrelated_failures(work, monkeypatch, bad_client):
    def refused(_):
        raise RuntimeError("outer", ssl.SSLCertVerificationError(1, "synthetic"))

    clients = dict.fromkeys(probe.TLS_CLIENTS, refused)
    if bad_client == "timeout":
        clients["urllib"] = mock.Mock(side_effect=TimeoutError())
    elif bad_client:
        clients[bad_client] = lambda _: None
    monkeypatch.setattr(probe, "tls_clients", lambda: (True, clients))
    monkeypatch.setattr(probe, "tls_fixture", lambda *a: contextlib.nullcontext("https://127.0.0.1:12345/"))
    checks = probe.verify_tls(work, {})
    assert all(checks.values()) is (bad_client is None)


def test_synthetic_certificate_command_has_no_shell_or_inherited_environment(work, monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        for name in ("key.pem", "cert.pem"):
            (work / "output/tls" / name).write_text("synthetic fixture")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(probe, "root_locked", lambda _: None)
    monkeypatch.setattr(probe.subprocess, "run", run)
    state = {}
    cert, key = probe.make_certificate(work, state)
    assert captured["command"][:3] == ["/usr/bin/openssl", "req", "-x509"]
    assert captured["env"] == {
        "PATH": "/usr/bin:/bin",
        "HOME": str(work / "home"),
        "OPENSSL_CONF": str(work / "output/tls/openssl.cnf"),
    }
    assert "shell" not in captured
    assert "openssl_argv" not in state
    assert cert.stat().st_mode & 0o777 == key.stat().st_mode & 0o777 == 0o600


def mock_probe_runtime(work, monkeypatch):
    monkeypatch.setattr(probe, "fixed_paths", lambda *a: (Path("/candidate"), Path("/engine"), work))
    monkeypatch.setattr(probe, "validate_runtime", lambda *a: None)
    monkeypatch.setattr(probe, "validate_probe_source", lambda *a: None)
    monkeypatch.setattr(probe, "private_log", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(probe, "synthetic_environment", lambda *a: None)
    monkeypatch.setattr(probe, "bootstrap_gate", lambda *a: None)
    monkeypatch.setattr(probe.sys, "addaudithook", lambda *a: None)
    monkeypatch.setattr(probe, "import_engine", lambda *a: None)
    monkeypatch.setattr(
        probe,
        "verify_tls",
        lambda *a: {
            "default_tls_verification": True,
            **{"tls_" + name + "_rejected": True for name in probe.TLS_CLIENTS},
        },
    )


def test_success_records_only_its_limited_scope(work, monkeypatch):
    mock_probe_runtime(work, monkeypatch)
    result = probe.run_probe(arguments(work))
    assert result["passed"] is True and result["error_class"] is None
    assert len(result["checks"]) == 7 and all(result["checks"].values())
    assert result["network_namespace_verified"] is False
    assert result["live_model_verified"] is result["live_market_verified"] is False
    assert json.loads((work / "output/result.json").read_bytes()) == result


def test_failed_import_never_reports_success_or_raw_exception(work, monkeypatch):
    mock_probe_runtime(work, monkeypatch)
    monkeypatch.setattr(probe, "import_engine", mock.Mock(side_effect=ImportError("synthetic-never-printed")))
    result = probe.run_probe(arguments(work))
    assert result["passed"] is False
    assert "astock_import_and_fixed_sources" not in result["checks"]
    assert "synthetic-never-printed" not in json.dumps(result)
    assert result["error_class"] == "probe_execution_failed"


def test_failed_tls_is_nonzero_and_does_not_turn_timeout_into_cert_rejection(work, monkeypatch, capfd):
    mock_probe_runtime(work, monkeypatch)
    monkeypatch.setattr(probe, "verify_tls", lambda *a: {"tls_urllib_rejected": False})
    monkeypatch.setattr(probe, "run_probe", mock.Mock(wraps=probe.run_probe))
    code = probe.main(
        [
            "--candidate",
            CANDIDATE,
            "--engine",
            ENGINE,
            "--workdir",
            str(work),
            "--nonce",
            NONCE,
            "--probe-sha256",
            "d" * 64,
            "--production-pid",
            "123",
            "--issued-at",
            "1800000000",
        ]
    )
    result = json.loads(capfd.readouterr().out)
    assert code == 1 and result["passed"] is False
    assert result["error_class"] == "tls_verification_rejected"


def test_invalid_arguments_do_not_echo_arbitrary_values(capfd):
    assert probe.main(["--not-allowed", "synthetic-never-printed"]) == 1
    assert "synthetic-never-printed" not in capfd.readouterr().out
