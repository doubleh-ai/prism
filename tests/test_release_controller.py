"""Offline tests: temp files, fake systemd/pip/HTTP; never contacts production."""

import base64
import io
import json
import os
import sqlite3
import stat
import tarfile
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / "scripts" / (name + ".py"))
    exec(compile(Path(module.__file__).read_bytes(), module.__file__, "exec"), module.__dict__)
    return module


remote = load("remote_release")
controller = load("release_controller")
COMMIT, RECOVERY, RELEASE = "a" * 40, "b" * 40, "release-test-001"
NOW = 1800000000


def archive(files=None, extra=None):
    files = files or {
        "server/main.py": b"# inert test candidate\n",
        "requirements.lock": b"example==1 --hash=sha256:" + b"1" * 64 + b"\n",
    }
    manifest = {
        "kind": "candidate",
        "deployed": False,
        "commit": COMMIT,
        "version": "v0.20.0",
        "files": [
            {"path": name, "sha256": remote.digest(content), "size": len(content), "mode": "0644"}
            for name, content in files.items()
        ],
    }
    manifest_raw = remote.encoded(manifest)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, content in dict(files, **{"release-manifest.json": manifest_raw}).items():
            item = tarfile.TarInfo(name)
            item.mode, item.size = 0o644, len(content)
            tar.addfile(item, io.BytesIO(content))
        if extra:
            tar.addfile(extra)
    return stream.getvalue(), manifest_raw, manifest, files


def request(action="prepare"):
    raw, manifest_raw, manifest, files = archive()
    receipts = {
        "commit": COMMIT,
        "archive_sha256": remote.digest(raw),
        "manifest_sha256": remote.digest(manifest_raw),
    }
    for name in ("backup", "compatibility", "activity", "stage", "journeys", "recovery", "admission"):
        receipts[name] = {"verified": True, "receipt_sha256": "c" * 64, "checked_at": NOW}
    receipts["backup"]["recovery_verified"] = True
    receipts["compatibility"]["database_forward_and_backward"] = True
    receipts["activity"]["admission_paused"] = True
    receipts["stage"].update(isolated=True, journeys_passed=True)
    receipts["journeys"].update(served_commit=COMMIT, journeys_passed=True)
    receipts["recovery"].update(
        commit=RECOVERY,
        release_id="safe-recovery-001",
        archive_sha256="d" * 64,
        manifest_sha256="e" * 64,
        security_verified=True,
        journeys_passed=True,
        database_forward_and_backward=True,
    )
    data = {
        "action": action,
        "commit": COMMIT,
        "release_id": RELEASE,
        "archive_sha256": receipts["archive_sha256"],
        "manifest_sha256": receipts["manifest_sha256"],
        "receipts": receipts,
        "archive": base64.b64encode(raw).decode(),
        "dependencies": [
            {
                "name": "core.lock",
                "sha256": remote.digest(files["requirements.lock"]),
                "content": base64.b64encode(files["requirements.lock"]).decode(),
            },
            {
                "name": "example-1-py3-none-any.whl",
                "sha256": remote.digest(b"inert-wheel"),
                "content": base64.b64encode(b"inert-wheel").decode(),
            },
        ],
    }
    receipts["dependencies"] = {"lock_sha256": remote.digest(files["requirements.lock"])}
    return bind(data)


def bind(data):
    raw = remote.encoded(data["receipts"])
    data.update(receipts_raw=base64.b64encode(raw).decode(), receipts_sha256=remote.digest(raw))
    return data


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pr-")
        self.root = Path(self.temp.name).resolve()
        self.commands, self.boundaries = [], []
        self.engine = remote.RemoteRelease(
            self.root, runner=self.run_fake, permit=self.permit, now=lambda: NOW
        )
        self.engine.stage_account = lambda: None
        self.engine.is_socket = lambda path: path.is_file()  # fake transport, no socket/network syscall
        self.engine.publish_stage_socket = lambda path: path.chmod(0o660)  # fake file, not Linux O_PATH
        # Real synthetic seeding/HTTP contracts have dedicated stage tests.
        # This fixture has no application server; keep transport explicitly fake.
        self.engine.stage_journeys = lambda *args: "d" * 64
        self.engine.stage_socket = lambda stage: stage / "stage.sock"  # no real UDS address in this fake
        self.engine.stage_process_identity = lambda pid: {
            "stage_pid": 222,
            "production_pid": pid,
            "dedicated_user": True,
            "separate_network_namespace": True,
            "checked_at": NOW,
        }
        self.engine.old_venv.joinpath("lib/python3.12/site-packages").mkdir(parents=True)
        self.engine.old_venv.joinpath("lib/python3.12/site-packages/inherited.pth").write_text(
            "/reviewed/external/source\n"
        )
        self.engine.old_venv.joinpath("bin").mkdir()
        self.engine.old_venv.joinpath("bin/python").write_text("never executed")
        self.engine.production.joinpath("data").mkdir(parents=True)
        self.data = request()
        self.cli_source = self.root / remote.CLI_SOURCE
        self.cli_source.parent.mkdir(parents=True)
        self.cli_source.write_bytes(b"\x7fELFPrism inert test executable: never run\n")
        self.cli_source.chmod(0o755)
        self.cli_entry = self.root / remote.CLI_ENTRY
        self.cli_entry.parent.mkdir(parents=True)
        self.cli_entry.symlink_to(self.cli_source)
        self.cli_digest = remote.digest(self.cli_source.read_bytes())
        self.data["receipts"]["dependencies"]["cli_binary_sha256"] = self.cli_digest
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        external_root = self.root / "home/ubuntu/TradingAgents-astock"
        external_root.mkdir()
        source = external_root / "engine.py"
        source.write_bytes(b"# copied as data, never imported\n")
        source.chmod(0o664)  # observed upstream permissions; immutable copy is normalized
        external = {
            "source_root": "/home/ubuntu/TradingAgents-astock",
            "git_commit": "f" * 40,
            "files": [
                {
                    "path": "engine.py",
                    "sha256": remote.digest(source.read_bytes()),
                    "size": source.stat().st_size,
                    "mode": "0644",
                }
            ],
        }
        self.data["external_source_manifest"] = external
        self.data["receipts"]["dependencies"]["external_sources_sha256"] = remote.digest(
            remote.encoded(external)
        )
        bind(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def permit(self, nonce, label):
        self.boundaries.append(label)
        return nonce

    def run_fake(self, args, **kwargs):
        self.commands.append((args, kwargs))
        if args[:2] == ["/usr/bin/systemctl", "show"] and "--property=MainPID" in args:
            return b"111\n"
        if args[:2] == ["/usr/bin/systemctl", "show"] and args[-1].startswith("prism-stage-"):
            return b"LoadState=loaded\nActiveState=active\n"
        if "-m" in args and "venv" in args:
            target = Path(args[-1])
            target.joinpath("lib/python3.12/site-packages").mkdir(parents=True)
            target.joinpath("bin").mkdir()
            target.joinpath("bin/python").write_text("never executed")
        if args[0] == "/usr/bin/systemd-run" and remote.STAGE_BOOTSTRAP in args:
            commit, release_id, pid, nonce, issued_at, app = args[-6:]
            stage = self.engine.operations / "staging" / release_id
            (stage / "stage.sock").touch()
            proof = {
                "schema": 1,
                "commit": commit,
                "release_id": release_id,
                "production_pid": int(pid),
                "stage_pid": 222,
                "nonce": nonce,
                "issued_at": int(issued_at),
                "checked_at": NOW,
                "application": app,
                "checks": {
                    name: {"blocked": True, "errno": "permission_denied"}
                    for name in (
                        "production_environment",
                        "production_process_environment",
                        "production_http",
                        "sidecar_http",
                        "candidate_write",
                    )
                },
            }
            (stage / "isolation-proof.json").write_bytes(remote.encoded(proof))
            (stage / "isolation-proof.json").chmod(0o600)
        return b""

    def prepared(self):
        result = self.engine.execute(self.data)
        self.assertFalse(result["production_verified"])
        return self.engine.release

    def database(self):
        path = self.engine.production / "data/chat.db"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sessions (id TEXT)")
            db.execute("INSERT INTO sessions VALUES ('private-live-session')")
            db.execute("CREATE TABLE capability_runs (revoked INTEGER, expires_at INTEGER)")
            db.execute("INSERT INTO capability_runs VALUES (0, ?)", [NOW - 100])
            db.execute("CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT)")
            db.execute(
                "CREATE TABLE conversations (id TEXT PRIMARY KEY, username TEXT, title TEXT, skill TEXT)"
            )
            db.execute("CREATE TABLE messages (conv_id TEXT, role TEXT, content TEXT)")
            db.execute(
                "CREATE TABLE saved_reports (id TEXT PRIMARY KEY, username TEXT, conv_id TEXT, title TEXT, report_json TEXT, created_at INTEGER, updated_at INTEGER)"
            )
        return path

    def staged(self):
        release = self.prepared()
        self.database()
        self.engine.execute(dict(self.data, action="stage"))
        return release

    def safe_recovery(self, *, restricted=True):
        record = self.engine.operations / "safe-recovery-001"
        record.mkdir(mode=0o700)
        target = self.engine.releases / RECOVERY
        target.mkdir()
        (target / ".venv").mkdir()
        (target / ".deploy-revision").write_text(RECOVERY + "\n")
        engine_digest = self.data["receipts"]["dependencies"]["external_sources_sha256"]
        (target / ".engine-revision").write_text(engine_digest + "\n")
        (target / ".cli-revision").write_text(self.cli_digest + "\n")
        (record / "external-source-manifest.json").write_bytes(
            remote.encoded(self.data["external_source_manifest"])
        )
        source = target / "server" / ("recovery.py" if restricted else "main.py")
        source.parent.mkdir()
        source.write_bytes(b"# inert recovery fixture\n")
        source.chmod(0o644)
        manifest_raw = remote.encoded(
            {
                "commit": RECOVERY,
                "version": "v0.19.8-recovery" if restricted else "v0.19.8",
                "files": [
                    {
                        "path": source.relative_to(target).as_posix(),
                        "size": source.stat().st_size,
                        "mode": "0644",
                        "sha256": remote.digest(source.read_bytes()),
                    }
                ],
            }
        )
        self.data["receipts"]["recovery"]["manifest_sha256"] = remote.digest(manifest_raw)
        bind(self.data)
        (record / "identity.json").write_bytes(
            remote.encoded(
                {
                    key: self.data["receipts"]["recovery"][key]
                    for key in ("commit", "archive_sha256", "manifest_sha256")
                }
            )
        )
        (record / "manifest.json").write_bytes(manifest_raw)
        (record / "prepared.json").write_bytes(
            remote.encoded(
                {
                    "commit": RECOVERY,
                    "environment_sha256": remote.tree_digest(target / ".venv")[0],
                    "external_sources_sha256": engine_digest,
                    "cli_binary_sha256": self.cli_digest,
                }
            )
        )
        (record / "staged.json").write_text("{}")
        return target

    def switch_ready(self):
        self.staged()
        self.safe_recovery()
        (self.engine.production / "server").mkdir()
        code = self.engine.production / "server/main.py"
        code.write_text("# original")
        code.chmod(0o644)
        manifest = {
            "files": [
                {
                    "path": "server/main.py",
                    "sha256": remote.digest(code.read_bytes()),
                    "size": code.stat().st_size,
                    "mode": "0644",
                }
            ]
        }
        self.data["source_manifest"] = manifest
        self.data["receipts"]["expected_source_manifest_sha256"] = remote.digest(remote.encoded(manifest))
        self.data["receipts"]["expected_current_fingerprint"] = "f" * 64
        bind(self.data)
        self.engine.runtime = lambda: {
            "fingerprint": "f" * 64,
            "working_directory": str(self.engine.production),
            "served_revision": None,
        }
        self.nginx_fixture()
        self.engine.execute(dict(self.data, action="quiesce"))

    def nginx_fixture(self):
        available = self.root / "etc/nginx/sites-available"
        enabled = self.root / "etc/nginx/sites-enabled"
        available.mkdir(parents=True)
        enabled.mkdir()
        self.nginx_original = {}
        for name in remote.NGINX_SITES:
            content = b"server {\n    listen 80;\n    # comment with braces { } and /api\n    location / {\n        proxy_pass http://127.0.0.1:8088;\n    }\n}\n"
            (available / name).write_bytes(content)
            (enabled / name).symlink_to(available / name)
            self.nginx_original[name] = content
        self.data["receipts"]["admission"]["config_sha256"] = {
            name: remote.digest(value) for name, value in self.nginx_original.items()
        }
        bind(self.data)
        self.engine.probe_admission = lambda: None  # external HTTP is a separate real-world acceptance gate

    def test_prepare_binds_lock_and_inherited_environment(self):
        self.prepared()
        names = [" ".join(args) for args, _ in self.commands]
        self.assertTrue(any("--require-hashes --only-binary=:all:" in name for name in names))
        self.assertTrue(any("-m pip check" in name for name in names))
        self.assertFalse(any("restart" in name for name in names))
        prepared = json.loads((self.engine.record / "prepared.json").read_bytes())
        self.assertEqual(
            prepared["inherited_environment_sha256"],
            self.data["receipts"]["dependencies"]["base_environment_sha256"],
        )
        self.assertIn("offline_dependency_install", self.boundaries)
        builds = [args for args, _ in self.commands if args[0] == "/usr/bin/systemd-run"]
        self.assertTrue(builds)
        self.assertTrue(
            all("--uid=prism-stage" in args and "--property=PrivateNetwork=yes" in args for args in builds)
        )
        self.assertTrue(
            all(
                "--property=InaccessiblePaths=/run -" + str(self.engine.operations / "staging") in args
                for args in builds
            )
        )
        self.assertFalse(
            any(args[0] == str(self.engine.old_venv / "bin/python") for args, _ in self.commands)
        )
        self.assertFalse((self.engine.release / ".venv/lib/python3.12/site-packages/inherited.pth").exists())

    def test_cli_copy_is_bound_as_data_and_dropin_cannot_be_overridden_by_environment_file(self):
        before = self.cli_source.read_bytes()
        release = self.prepared()
        prepared = json.loads((self.engine.record / "prepared.json").read_bytes())
        target = self.engine.cli_target(self.cli_digest)
        self.assertEqual(prepared["cli_binary_sha256"], self.cli_digest)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertEqual((release / ".cli-revision").read_text().strip(), self.cli_digest)
        self.assertEqual(self.cli_source.read_bytes(), before)
        self.assertFalse(
            any(str(self.cli_source) in args or str(target) in args for args, _ in self.commands)
        )
        dropin = self.engine.dropin(release).decode()
        self.assertIn("ExecStart=/usr/bin/env CLAUDE_BIN=" + str(target) + " ", dropin)
        self.assertNotIn("Environment=CLAUDE_BIN=", dropin)
        self.assertNotIn(str(self.cli_entry), dropin)
        self.assertIn("prepare_fixed_cli_binary", self.boundaries)
        self.engine.verify_tree()
        target.write_bytes(b"\x7fELFtampered binary")
        with self.assertRaisesRegex(remote.ReleaseError, "cli_binary_drift"):
            self.engine.verify_tree()

    def test_cli_missing_digest_rejected_before_any_candidate_or_cli_copy(self):
        self.data["receipts"]["dependencies"].pop("cli_binary_sha256")
        bind(self.data)
        with self.assertRaisesRegex(remote.ReleaseError, "cli_binary_receipt_required"):
            self.engine.execute(self.data)
        self.assertFalse(self.engine.release.exists())
        self.assertFalse((self.root / "opt/prism/tools/claude").exists())

    def test_cli_entry_must_resolve_to_the_fixed_source(self):
        for kind in ("regular", "other_target"):
            with self.subTest(kind=kind):
                self.cli_entry.unlink()
                if kind == "regular":
                    self.cli_entry.write_bytes(self.cli_source.read_bytes())
                else:
                    other = self.cli_entry.parent / "other-claude"
                    other.write_bytes(self.cli_source.read_bytes())
                    self.cli_entry.symlink_to(other)
                self.engine.request = self.data
                with self.assertRaisesRegex(remote.ReleaseError, "fixed_cli_source_required"):
                    self.engine.prepare_cli()

    def test_cli_source_shell_setuid_hardlink_and_nonexecutable_rejected_without_execution(self):
        self.engine.request = self.data
        original = self.cli_source.read_bytes()
        for kind in ("shell", "setuid", "hardlink", "nonexecutable"):
            with self.subTest(kind=kind):
                self.cli_source.write_bytes(b"#!/bin/sh\nexit 9\n" if kind == "shell" else original)
                self.cli_source.chmod(
                    0o4755 if kind == "setuid" else 0o644 if kind == "nonexecutable" else 0o755
                )
                link = self.cli_source.with_name("linked-copy")
                if kind == "hardlink":
                    os.link(self.cli_source, link)
                try:
                    # Some macOS test filesystems strip setuid on chmod. Model
                    # that one fstat result explicitly; every other case uses
                    # real files, permissions, hard links and open descriptors.
                    context = nullcontext()
                    if kind == "setuid":
                        observed = self.cli_source.stat()
                        context = mock.patch.object(
                            remote.os,
                            "fstat",
                            return_value=types.SimpleNamespace(
                                st_mode=observed.st_mode | stat.S_ISUID,
                                st_nlink=observed.st_nlink,
                                st_size=observed.st_size,
                            ),
                        )
                    with context:
                        with self.assertRaisesRegex(remote.ReleaseError, "cli_source_not_regular_binary"):
                            self.engine.prepare_cli()
                finally:
                    if link.exists():
                        link.unlink()
        self.assertFalse(self.commands)

    def test_cli_source_or_entry_changed_at_copy_boundary_never_publishes_binary(self):
        self.engine.request = self.data
        original = self.cli_source.read_bytes()
        for kind in ("bytes", "entry"):
            with self.subTest(kind=kind):
                self.cli_source.write_bytes(original)
                self.cli_entry.unlink()
                self.cli_entry.symlink_to(self.cli_source)

                def change(nonce, label):
                    if label == "prepare_fixed_cli_binary":
                        if kind == "bytes":
                            self.cli_source.write_bytes(original + b"changed")
                        else:
                            self.cli_entry.unlink()
                            self.cli_entry.symlink_to(self.cli_source.with_name("not-the-original"))
                    return nonce

                self.engine.permit = change
                with self.assertRaises((remote.ReleaseError, FileNotFoundError)):
                    self.engine.prepare_cli()
                self.assertFalse(self.engine.cli_target(self.cli_digest).exists())

    def test_cli_existing_pending_is_preserved_and_existing_target_is_reverified(self):
        self.engine.request = self.data
        target = self.engine.cli_target(self.cli_digest)
        target.parent.mkdir(parents=True)
        pending = target.with_name("claude.pending")
        pending.write_bytes(b"previous failed copy, preserve for inspection")
        with self.assertRaises(FileExistsError):
            self.engine.prepare_cli()
        self.assertEqual(pending.read_bytes(), b"previous failed copy, preserve for inspection")
        self.assertFalse(target.exists())
        pending.unlink()
        target.write_bytes(self.cli_source.read_bytes())
        target.chmod(0o777)
        with self.assertRaisesRegex(remote.ReleaseError, "cli_binary_not_root_locked"):
            self.engine.prepare_cli()

    def test_prepare_drift_and_space_fail_before_candidate_write(self):
        self.engine.old_venv.joinpath("new.pth").write_text("changed")
        with self.assertRaisesRegex(remote.ReleaseError, "base_environment_changed"):
            self.engine.execute(self.data)
        self.assertFalse(self.engine.release.exists())
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        bind(self.data)
        with mock.patch.object(remote.shutil, "disk_usage", return_value=types.SimpleNamespace(free=1)):
            with self.assertRaisesRegex(remote.ReleaseError, "insufficient_disk_reserve"):
                self.engine.execute(self.data)
        self.assertFalse(self.engine.release.exists())

    def test_prepare_excludes_only_reviewed_ui_and_replaced_editable_roots(self):
        site = self.engine.old_venv / "lib/python3.12/site-packages"
        excluded = {
            "streamlit/__init__.py",
            "streamlit-1.58.0.dist-info/METADATA",
            "tradingagents_astock-0.2.16.dist-info/METADATA",
            "~radingagents_astock-0.2.15.dist-info/METADATA",
            "__editable__.tradingagents_astock-0.2.16.pth",
            "__editable___tradingagents_astock_0_2_16_finder.py",
            "~ttpx-0.25.2.dist-info/METADATA",
        }
        retained = {
            "streamlit_extra/__init__.py",
            "other_package/streamlit/__init__.py",
            "pydeck/__init__.py",
            "httpx-0.27.2.dist-info/METADATA",
            "unrelated-1.dist-info/METADATA",
        }
        for name in excluded | retained:
            path = site / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"raise RuntimeError('source must not execute during prepare')\n")
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        bind(self.data)
        before = remote.tree_digest(self.engine.old_venv)[0]
        self.prepared()
        copied = self.engine.release / ".venv/lib/python3.12/site-packages"
        self.assertTrue(all(not (copied / name).exists() for name in excluded))
        self.assertTrue(all((copied / name).is_file() for name in retained))
        self.assertEqual(before, remote.tree_digest(self.engine.old_venv)[0])
        self.assertTrue(all((site / name).is_file() for name in excluded))
        receipt = json.loads((self.engine.record / "prepared.json").read_bytes())
        projection = receipt["inherited_package_projection"]
        self.assertEqual(
            {item["name"] for item in projection["excluded_roots"]}, {name.split("/")[0] for name in excluded}
        )
        self.assertEqual(projection["replacement_engine_manifest_sha256"], receipt["external_sources_sha256"])
        self.engine.verify_tree()

    def test_excluded_ui_drift_is_still_checked_against_the_complete_source_tree(self):
        source = self.engine.old_venv / "lib/python3.12/site-packages/streamlit/__init__.py"
        source.parent.mkdir()
        source.write_bytes(b"# original excluded UI\n")
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        bind(self.data)

        def change_excluded_after_digest(args, **kwargs):
            result = self.run_fake(args, **kwargs)
            if "venv" in args:
                source.write_bytes(b"# changed excluded UI\n")
            return result

        self.engine.run = change_excluded_after_digest
        with self.assertRaisesRegex(remote.ReleaseError, "base_environment_changed_during_copy"):
            self.engine.execute(self.data)
        self.assertFalse(any("pip" in args for args, _ in self.commands))
        self.assertFalse((self.engine.record / "prepared.json").exists())

    def test_replaced_editable_uses_manifest_bound_graph_source_instead_of_a_hook(self):
        source = self.root / "home/ubuntu/TradingAgents-astock"
        graph_path = "tradingagents/graph/trading_graph.py"
        graph = source / graph_path
        graph.parent.mkdir(parents=True)
        graph.write_bytes(b"class TradingAgentsGraph: pass\n")
        manifest = self.data["external_source_manifest"]
        manifest["files"].append(
            {
                "path": graph_path,
                "sha256": remote.digest(graph.read_bytes()),
                "size": graph.stat().st_size,
                "mode": "0644",
            }
        )
        self.data["receipts"]["dependencies"]["external_sources_sha256"] = remote.digest(
            remote.encoded(manifest)
        )
        bind(self.data)
        self.prepared()
        target = self.engine.verify_external(self.engine.record)
        self.assertTrue((target / graph_path).is_file())
        self.assertIn("PRISM_ASTOCK_PROJECT=" + str(target), self.engine.dropin(self.engine.release).decode())
        self.assertFalse((self.engine.release / ".venv/lib/python3.12/site-packages/inherited.pth").exists())
        (target / graph_path).write_bytes(b"# tampered graph\n")
        with self.assertRaisesRegex(remote.ReleaseError, "external_source_drift"):
            self.engine.verify_tree()

    def test_failed_prepare_cannot_stage(self):
        def fail_pip(args, **kwargs):
            if "install" in args:
                raise remote.ReleaseError("command_failed")
            return self.run_fake(args, **kwargs)

        self.engine.run = fail_pip
        with self.assertRaisesRegex(remote.ReleaseError, "command_failed"):
            self.engine.execute(self.data)
        self.assertFalse((self.engine.record / "prepared.json").exists())
        with self.assertRaisesRegex(remote.ReleaseError, "prepare_not_completed"):
            self.engine.execute(dict(self.data, action="stage"))

    def test_inherited_package_changes_after_receipt_check_never_reach_pip(self):
        source = self.engine.old_venv / "lib/python3.12/site-packages/example.py"
        source.write_bytes(b"# reviewed source\n")
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        bind(self.data)

        def change_after_digest(args, **kwargs):
            result = self.run_fake(args, **kwargs)
            if "venv" in args:
                source.write_bytes(b"# modified source\n")
            return result

        self.engine.run = change_after_digest
        with self.assertRaisesRegex(remote.ReleaseError, "inherited_package_drift"):
            self.engine.execute(self.data)
        self.assertFalse(any("pip" in args for args, _ in self.commands))
        self.assertFalse((self.engine.record / "prepared.json").exists())

    def test_stage_is_network_isolated_and_revokes_copy_only(self):
        self.staged()
        stage = self.engine.operations / "staging" / RELEASE
        with sqlite3.connect(stage / "data/chat.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT revoked FROM capability_runs").fetchone()[0], 1)
        with sqlite3.connect(self.engine.production / "data/chat.db") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT revoked FROM capability_runs").fetchone()[0], 0)
        command = next(
            args
            for args, _ in self.commands
            if args[0] == "/usr/bin/systemd-run" and remote.STAGE_BOOTSTRAP in args
        )
        for item in [
            "--property=PrivateNetwork=yes",
            "--property=ProtectHome=yes",
            "--property=InaccessiblePaths=/run",
            "--property=MemoryMax=768M",
            "--property=TasksMax=64",
            "--property=CPUQuota=100%",
            "--property=RuntimeMaxSec=1800",
            "--property=KillMode=control-group",
            "-S",
            "-c",
            "-i",
        ]:
            self.assertIn(item, command)
        self.assertFalse(any("EnvironmentFile=" in arg or "API_KEY" in arg for arg in command))
        self.assertEqual(stage.stat().st_mode & 0o777, 0o750)
        self.assertIn("--uid=prism-stage", command)
        self.assertIn("--gid=prism-stage", command)
        self.assertEqual((stage / "data").stat().st_mode & 0o777, 0o700)
        self.assertEqual((stage / "stage.sock").stat().st_mode & 0o777, 0o660)
        proof_record = json.loads((self.engine.record / "stage-isolation.json").read_bytes())
        self.assertEqual(proof_record["proof"]["stage_pid"], 222)
        self.assertEqual(proof_record["proof"]["production_pid"], 111)
        self.assertEqual(proof_record["proof"]["application"], "server.main:app")

    def test_collected_stage_unit_is_allowed_but_unknown_command_failure_is_not(self):
        command = [
            "/usr/bin/systemctl",
            "show",
            "--property=LoadState,ActiveState",
            "prism-stage-" + COMMIT[:12] + ".service",
        ]
        with mock.patch.object(
            remote.subprocess,
            "run",
            return_value=types.SimpleNamespace(
                returncode=1, stdout=b"LoadState=not-found\nActiveState=inactive\n"
            ),
        ):
            self.assertIn(b"not-found", remote.run_command(command))
            with self.assertRaisesRegex(remote.ReleaseError, "command_failed"):
                remote.run_command(["/usr/bin/systemctl", "restart", remote.SERVICE])
        with mock.patch.object(
            remote.subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout=b"")
        ):
            with self.assertRaisesRegex(remote.ReleaseError, "command_failed"):
                remote.run_command(command)

    def test_stage_never_records_acceptance_for_a_failed_probe(self):
        self.prepared()
        self.database()

        def failed_probe(args, **kwargs):
            result = self.run_fake(args, **kwargs)
            if remote.STAGE_BOOTSTRAP in args:
                path = self.engine.operations / "staging" / RELEASE / "isolation-proof.json"
                proof = json.loads(path.read_bytes())
                proof["checks"]["production_environment"] = {"blocked": False, "errno": "opened"}
                path.write_bytes(remote.encoded(proof))
            return result

        self.engine.run = failed_probe
        with self.assertRaisesRegex(remote.ReleaseError, "stage_isolation_proof_rejected"):
            self.engine.execute(dict(self.data, action="stage"))
        self.assertFalse((self.engine.record / "staged.json").exists())
        self.assertFalse((self.engine.record / "stage-isolation.json").exists())

    def test_prepared_extra_source_and_environment_tampering_rejected(self):
        release = self.prepared()
        extra = release / "extra.py"
        extra.write_text("extra")
        with self.assertRaisesRegex(remote.ReleaseError, "unexpected_prepared_file"):
            self.engine.verify_tree()
        extra.unlink()
        (release / ".venv/new.pth").write_text("extra")
        with self.assertRaisesRegex(remote.ReleaseError, "prepared_environment_drift"):
            self.engine.verify_tree()

    def test_manifest_hash_cannot_be_replaced_with_matching_new_files(self):
        self.prepared()
        manifest = self.engine.record / "manifest.json"
        data = json.loads(manifest.read_bytes())
        data["files"] = []
        manifest.write_bytes(remote.encoded(data))
        with self.assertRaisesRegex(remote.ReleaseError, "prepared_manifest_not_bound"):
            self.engine.verify_tree()

    def test_external_source_is_fixed_root_owned_copy_and_reverified(self):
        self.prepared()
        target = self.engine.external_target(self.data["external_source_manifest"])
        self.assertEqual((target / "engine.py").read_bytes(), b"# copied as data, never imported\n")
        self.assertIn(str(target), self.engine.dropin(self.engine.release).decode())
        self.assertNotIn(
            "PRISM_ASTOCK_PROJECT=/home/ubuntu", self.engine.dropin(self.engine.release).decode()
        )
        (target / "engine.py").write_bytes(b"changed")
        with self.assertRaisesRegex(remote.ReleaseError, "external_source_drift"):
            self.engine.verify_tree()

    def test_extra_external_python_cannot_be_silently_ignored(self):
        (self.root / "home/ubuntu/TradingAgents-astock/untracked.py").write_text("# unexpected")
        with self.assertRaisesRegex(remote.ReleaseError, "unregistered_external_python"):
            self.engine.execute(self.data)
        self.assertFalse(self.engine.release.exists())

    def test_non_runtime_external_sources_are_not_copied_or_executed(self):
        source = self.root / "home/ubuntu/TradingAgents-astock"
        for name in ("tests", "examples", "scripts"):
            (source / name).mkdir()
            (source / name / "tracked_check.py").write_text("raise RuntimeError('never executed')")
        self.prepared()
        target = self.engine.external_target(self.data["external_source_manifest"])
        self.assertEqual({path.name for path in target.iterdir()}, {"engine.py"})
        with self.assertRaisesRegex(remote.ReleaseError, "stage_socket_path_too_long"):
            remote.RemoteRelease.stage_socket(Path("/" + "x" * 104))

    def test_tls_bypass_files_and_bytecode_are_not_inherited(self):
        site = self.engine.old_venv / "lib/python3.12/site-packages"
        (site / "__pycache__").mkdir()
        for name in (
            "sitecustomize.py",
            "patch_ssl.py",
            "usercustomize.py",
            "__pycache__/patch_ssl.cpython-312.pyc",
            "__pycache__/ordinary.cpython-312.pyc",
            "unrelated.pyc",
            "unrelated.pyo",
        ):
            (site / name).write_bytes(b"not executed")
        self.data["receipts"]["dependencies"]["base_environment_sha256"] = remote.tree_digest(
            self.engine.old_venv
        )[0]
        bind(self.data)
        self.prepared()
        names = [path.name for path in (self.engine.release / ".venv").rglob("*")]
        self.assertFalse(
            any(name.startswith(("sitecustomize", "patch_ssl", "usercustomize")) for name in names)
        )
        self.assertFalse(any(name == "__pycache__" or name.endswith((".pyc", ".pyo")) for name in names))

    def test_switch_requires_actual_runtime_and_admission_pause(self):
        self.switch_ready()
        self.data["receipts"]["expected_current_fingerprint"] = "wrong"
        bind(self.data)
        with self.assertRaisesRegex(remote.ReleaseError, "current_runtime_changed"):
            self.engine.execute(dict(self.data, action="switch"))
        self.data["receipts"]["expected_current_fingerprint"] = "f" * 64
        self.data["receipts"]["activity"]["admission_paused"] = False
        bind(self.data)
        with self.assertRaisesRegex(remote.ReleaseError, "reviewed_admission_pause_required"):
            self.engine.execute(dict(self.data, action="switch"))
        self.assertFalse(self.engine.override.exists())

    def test_stop_between_override_and_restart_preserves_failure_and_data(self):
        self.switch_ready()
        before = (self.engine.production / "data/chat.db").read_bytes()

        def permit(nonce, label):
            if label == "production_daemon_reload":
                return None
            return nonce

        self.engine.permit = permit
        with self.assertRaisesRegex(remote.ReleaseError, "fresh_operator_permit_required"):
            self.engine.execute(dict(self.data, action="switch"))
        self.assertTrue(self.engine.override.exists())
        self.assertIn("\nNoNewPrivileges=yes\nUMask=0077\n", self.engine.override.read_text())
        self.assertIn("uvicorn server.main:app", self.engine.override.read_text())
        self.assertIn(" --workers 1\n", self.engine.override.read_text())
        self.assertFalse(any("restart" in args for args, _ in self.commands))
        status = json.loads((self.engine.operations / "status.json").read_bytes())
        self.assertEqual(status["status"], "cancelled")
        self.assertTrue(status["cutover_occurred"])
        self.assertFalse(status["production_verified"])
        self.assertEqual(status["last_completed_stage"], "cutover_config_written")
        self.assertEqual((self.engine.production / "data/chat.db").read_bytes(), before)
        self.assertNotIn("private-live-session", json.dumps(status))
        self.assertNotIn(str(self.root), json.dumps(status))

    def test_rollback_broken_service_uses_safe_candidate_not_original(self):
        self.switch_ready()
        self.engine.execute(dict(self.data, action="switch"))
        self.engine.runtime = lambda: (_ for _ in ()).throw(
            AssertionError("broken current process must not be required")
        )
        self.engine.ready = self.engine.runtime
        before = (self.engine.production / "data/chat.db").read_bytes()
        result = self.engine.execute(dict(self.data, action="rollback"))
        self.assertIn(str(self.engine.releases / RECOVERY), self.engine.override.read_text())
        self.assertIn("uvicorn server.recovery:app", self.engine.override.read_text())
        self.assertIn("\nNoNewPrivileges=yes\nUMask=0077\n", self.engine.override.read_text())
        self.assertIn(" --workers 1\n", self.engine.override.read_text())
        self.assertFalse(result["production_verified"])
        self.assertEqual((self.engine.production / "data/chat.db").read_bytes(), before)
        with self.assertRaisesRegex(remote.ReleaseError, "rollback_already_completed"):
            self.engine.execute(dict(self.data, action="rollback"))

    def test_full_legacy_entry_cannot_be_used_as_restricted_recovery(self):
        self.prepared()
        self.safe_recovery(restricted=False)
        with self.assertRaisesRegex(remote.ReleaseError, "restricted_recovery_entry_required"):
            self.engine.recovery_target()

    def test_rollback_resumes_after_stop_or_failed_restart(self):
        self.switch_ready()
        self.engine.execute(dict(self.data, action="switch"))
        self.engine.permit = lambda nonce, label: None if label == "recovery_daemon_reload" else nonce
        with self.assertRaisesRegex(remote.ReleaseError, "fresh_operator_permit_required"):
            self.engine.execute(dict(self.data, action="rollback"))
        self.engine.permit = self.permit

        def fail_restart(args, **kwargs):
            if args[:2] == ["/usr/bin/systemctl", "restart"]:
                raise remote.ReleaseError("command_failed")
            return self.run_fake(args, **kwargs)

        self.engine.run = fail_restart
        with self.assertRaisesRegex(remote.ReleaseError, "command_failed"):
            self.engine.execute(dict(self.data, action="rollback"))
        self.engine.run = self.run_fake
        result = self.engine.execute(dict(self.data, action="rollback"))
        self.assertEqual(result["stage"], "rollback")
        self.assertFalse(result["production_verified"])

    def test_nginx_failed_test_restores_bytes_without_reload(self):
        self.staged()
        self.safe_recovery()
        self.nginx_fixture()
        self.engine.runtime = lambda: {"fingerprint": "baseline"}

        def failed(args, **kwargs):
            if args == ["/usr/sbin/nginx", "-t"]:
                raise remote.ReleaseError("command_failed")
            return self.run_fake(args, **kwargs)

        self.engine.run = failed
        with self.assertRaisesRegex(remote.ReleaseError, "nginx_hold_test_failed_original_restored"):
            self.engine.execute(dict(self.data, action="quiesce"))
        self.assertFalse(
            any(args == ["/usr/bin/systemctl", "reload", "nginx.service"] for args, _ in self.commands)
        )
        for name, path in self.engine.nginx_paths().items():
            self.assertEqual(path.read_bytes(), self.nginx_original[name])

    def test_nginx_resume_after_aborted_cutover_and_changed_target_rejection(self):
        self.switch_ready()
        self.engine.execute(dict(self.data, action="resume"))
        for name, path in self.engine.nginx_paths().items():
            self.assertEqual(path.read_bytes(), self.nginx_original[name])
        link = self.root / "etc/nginx/sites-enabled/stock-app"
        link.unlink()
        link.symlink_to(self.root / "etc/nginx/sites-available/stock-analyzer")
        with self.assertRaisesRegex(remote.ReleaseError, "nginx_fixed_target_mismatch"):
            self.engine.nginx_paths()

    def test_nginx_resume_failure_preserves_hold_and_starting_task_blocks(self):
        self.switch_ready()
        with sqlite3.connect(self.engine.production / "data/chat.db") as db:
            db.execute("CREATE TABLE analysis_runs(status TEXT)")
            db.execute("INSERT INTO analysis_runs VALUES('starting')")
        with self.assertRaisesRegex(remote.ReleaseError, "active_tasks"):
            self.engine.execute(dict(self.data, action="switch"))

        def failed(args, **kwargs):
            if args == ["/usr/sbin/nginx", "-t"]:
                raise remote.ReleaseError("command_failed")
            return self.run_fake(args, **kwargs)

        self.engine.run = failed
        with self.assertRaisesRegex(remote.ReleaseError, "nginx_resume_test_failed_hold_preserved"):
            self.engine.execute(dict(self.data, action="resume"))
        for path in self.engine.nginx_paths().values():
            self.assertIn("Prism controlled admission hold", path.read_text())

    def test_verify_requires_full_sha_and_journey_receipt(self):
        self.switch_ready()
        self.engine.execute(dict(self.data, action="switch"))
        self.engine.ready = lambda: {"status": "ready", "deployed_revision": COMMIT[:7]}
        with self.assertRaisesRegex(remote.ReleaseError, "served_revision_not_verified"):
            self.engine.execute(dict(self.data, action="verify"))
        self.engine.ready = lambda: {"status": "ready", "deployed_revision": COMMIT}
        result = self.engine.execute(dict(self.data, action="verify"))
        self.assertFalse(result["production_verified"])
        self.assertTrue(result["service_verified"])
        result = self.engine.execute(dict(self.data, action="resume"))
        self.assertTrue(result["production_verified"])
        stopped = [args[-1] for args, _ in self.commands if args[:2] == ["/usr/bin/systemctl", "stop"]]
        self.assertEqual(set(stopped), {"prism-stage-" + sha[:12] + ".service" for sha in (COMMIT, RECOVERY)})

    def test_receipt_content_digest_is_verified(self):
        self.data["receipts"]["backup"]["recovery_verified"] = False
        with self.assertRaisesRegex(remote.ReleaseError, "receipt_digest_mismatch"):
            self.engine.execute(self.data)
        self.assertFalse(self.engine.operations.exists())

    def test_legacy_runtime_does_not_fabricate_readiness(self):
        proc = self.root / "proc/42"
        proc.mkdir(parents=True)
        (proc / "cwd").symlink_to(self.engine.production)

        def systemd(args, **kwargs):
            return (
                str(self.engine.production).encode()
                if "--property=WorkingDirectory" in args
                else b"42"
                if "--property=MainPID" in args
                else b"old python command"
            )

        self.engine.run = systemd
        self.engine.ready = lambda: (_ for _ in ()).throw(OSError("legacy route missing"))
        value = self.engine.runtime()
        self.assertIsNone(value["served_revision"])
        self.assertEqual(value["working_directory"], str(self.engine.production))
        self.assertRegex(value["fingerprint"], "^[0-9a-f]{64}$")


class PureTests(unittest.TestCase):
    def test_nginx_parser_preserves_quotes_and_rejects_ambiguous_api_routes(self):
        config = (
            b'server {\n    set $x "quoted { } # text";\n    location / { proxy_pass http://localhost; }\n}\n'
        )
        changed = remote.admission_config(config)
        self.assertIn(b"location ^~ /api/", changed)
        self.assertIn(b"Retry-After 60 always", changed)
        for invalid in (
            changed,
            config + config,
            b"location / { x y; }",
            config.replace(b"location /", b"location /api/"),
        ):
            with self.assertRaises(remote.ReleaseError):
                remote.admission_config(invalid)

    def test_archive_digest_and_symlink_rejection(self):
        raw, manifest_raw, _, _ = archive()
        remote.validate_archive(raw, remote.digest(raw), remote.digest(manifest_raw))
        with self.assertRaisesRegex(remote.ReleaseError, "archive_digest_mismatch"):
            remote.validate_archive(raw + b"changed", remote.digest(raw), remote.digest(manifest_raw))
        symlink = tarfile.TarInfo("server/escape")
        symlink.type, symlink.linkname = tarfile.SYMTYPE, "/home/ubuntu/stock-analyzer/.env"
        raw, manifest_raw, _, _ = archive(extra=symlink)
        with self.assertRaisesRegex(remote.ReleaseError, "archive_member_rejected"):
            remote.validate_archive(raw, remote.digest(raw), remote.digest(manifest_raw))
        for path in ["../x", "/x", "server/../x", ".env", "server\\x"]:
            with self.assertRaises(remote.ReleaseError):
                remote.safe_path(path)

    def test_issue_fixed_public_identity_and_local_stop(self):
        issue = {
            "number": 9,
            "state": "open",
            "repository_url": "https://api.github.com/repos/doubleh-ai/prism",
            "user": {"login": "Huadous"},
        }

        class Response(io.BytesIO):
            status = 200

            def geturl(self):
                return controller.ISSUE

        class Opener:
            def open(self, req, timeout):
                self_request.append((req, timeout))
                return Response(json.dumps(issue).encode())

        self_request = []
        with tempfile.TemporaryDirectory() as tmp:
            flag = Path(tmp) / "STOP"
            controller.check_gate(flag, Opener())
            self.assertEqual(self_request[0][0].full_url, controller.ISSUE)
            self.assertFalse(self_request[0][0].has_header("Authorization"))
            issue["state"] = "closed"
            with self.assertRaisesRegex(controller.ControlError, "closed_or_unknown"):
                controller.check_gate(flag, Opener())
            flag.touch()
            with self.assertRaisesRegex(controller.ControlError, "local_stop"):
                controller.check_gate(flag, Opener())
        with self.assertRaisesRegex(controller.ControlError, "redirect_rejected"):
            controller.NoRedirect().redirect_request(None, None, None, None, None, "https://other.example")

    def test_ssh_pins_host_key_target_and_source(self):
        args = controller.ssh_argv(b'print("inert")')
        for value in [
            "StrictHostKeyChecking=yes",
            "UpdateHostKeys=no",
            "ForwardAgent=no",
            "BatchMode=yes",
            "ControlPath=/tmp/prism-release-ssh-%C",
            "ubuntu@124.223.168.224",
        ]:
            self.assertIn(value, args)
        self.assertIn("22", args)
        self.assertNotIn("StrictHostKeyChecking=no", args)

    def test_controller_rechecks_every_boundary_and_does_not_permit_closed_issue(self):
        class SpyInput(io.BytesIO):
            saved = b""

            def close(self):
                self.saved = self.getvalue()
                super().close()

        class Process:
            def __init__(self):
                read, write = os.pipe()
                self.stdout = os.fdopen(read, "rb")
                events = [
                    {"permit_required": "1" * 32},
                    {"permit_required": "2" * 32},
                    {"error": "fresh_operator_permit_required"},
                ]
                os.write(write, b"".join(json.dumps(event).encode() + b"\n" for event in events))
                os.close(write)
                self.stdin, self.code = SpyInput(), None

            def poll(self):
                return self.code

            def wait(self, timeout=None):
                self.code = 0
                return 0

            def terminate(self):
                self.code = 1

        process = Process()
        calls = []

        def gate(flag):
            calls.append(flag)
            if len(calls) == 3:
                raise controller.ControlError("operator_issue_closed_or_unknown")

        try:
            with self.assertRaises(controller.ControlError):
                controller.execute(
                    {"action": "switch"},
                    b"inert",
                    "/nonexistent/prism-test-stop",
                    popen=lambda *a, **k: process,
                    gate=gate,
                )
            sent = process.stdin.getvalue()
            self.assertIn(b'"permit": "' + b"1" * 32, sent)
            self.assertNotIn(b'"permit": "' + b"2" * 32, sent)
            self.assertIn(b'{"stop":true}', sent)
        finally:
            process.stdout.close()
            process.stdin.close()


if __name__ == "__main__":
    unittest.main()
