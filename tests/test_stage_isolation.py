"""Exercise the fixed bootstrap's syscall decisions without Linux or a network."""

import builtins
import errno
import io
import json
import os
import stat
import types
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
remote = types.ModuleType("remote_stage_test")
exec(compile((ROOT / "scripts/remote_release.py").read_bytes(), "remote_release.py", "exec"), remote.__dict__)
COMMIT, RELEASE, NONCE, NOW = "a" * 40, "stage-test-001", "b" * 32, 1800000000


class ProofStream(io.StringIO):
    def close(self):
        pass  # The test inspects bytes that would be written to the private file.

    def fileno(self):
        return 99


def bootstrap(*, readable=None, writable=False, runtime_writable=False, connection_code=errno.ECONNREFUSED):
    module = types.ModuleType("isolated_stage_bootstrap_test")
    exec(compile(remote.STAGE_BOOTSTRAP, "fixed-stage-bootstrap", "exec"), module.__dict__)
    stream, events = ProofStream(), []

    def opened(path, flags, mode=0o600):
        events.append(("open", path, flags))
        if path.endswith("/isolation-proof.json"):
            return 99
        if (
            path == readable
            or (writable and "/.stage-write-probe-" in path)
            or (runtime_writable and path.startswith("/run/.prism-stage-write-probe-"))
        ):
            return 42
        raise PermissionError(errno.EACCES, "synthetic diagnostic must not be exposed")

    module.os = types.SimpleNamespace(
        **{
            name: getattr(os, name)
            for name in ("O_CLOEXEC", "O_NOFOLLOW", "O_WRONLY", "O_CREAT", "O_EXCL", "O_RDONLY")
        },
        open=mock.Mock(side_effect=opened),
        close=mock.Mock(),
        unlink=mock.Mock(),
        read=mock.Mock(side_effect=AssertionError("Probe must never read bytes")),
        getpid=lambda: 222,
        fdopen=mock.Mock(return_value=stream),
        fsync=mock.Mock(),
    )
    connection = mock.MagicMock()
    connection.__enter__.return_value = connection
    connection.connect_ex.side_effect = lambda address: events.append(("connect", address)) or connection_code
    module.socket = types.SimpleNamespace(AF_INET=2, SOCK_STREAM=1, socket=mock.Mock(return_value=connection))
    module.time = types.SimpleNamespace(time=lambda: NOW)
    module.sys = types.SimpleNamespace(path=[])
    uvicorn = types.SimpleNamespace(run=mock.Mock())
    site = types.SimpleNamespace(main=mock.Mock())
    original_import = builtins.__import__

    def controlled_import(name, *args, **kwargs):
        if name in {"site", "uvicorn"}:
            events.append(("import", name))
            assert json.loads(stream.getvalue())["checks"]  # Proof exists before environment imports.
            return site if name == "site" else uvicorn
        return original_import(name, *args, **kwargs)

    return module, stream, events, connection, site, uvicorn, controlled_import


def invoke(module, importer, *, application="server.main:app", issued_at=NOW):
    with mock.patch("builtins.__import__", side_effect=importer):
        module.main([COMMIT, RELEASE, "111", NONCE, str(issued_at), application])


@pytest.mark.parametrize("application", ["server.main:app", "server.recovery:app"])
def test_probes_precede_import_and_serve_same_pid_on_fixed_socket(application):
    module, stream, events, connection, site, uvicorn, importer = bootstrap()
    invoke(module, importer, application=application)
    proof = json.loads(stream.getvalue())
    assert proof["stage_pid"] == 222 and proof["production_pid"] == 111
    assert proof["nonce"] == NONCE and proof["commit"] == COMMIT
    assert all(item["blocked"] is True for item in proof["checks"].values())
    assert [event for event in events if event[0] == "connect"] == [
        ("connect", ("127.0.0.1", 8088)),
        ("connect", ("127.0.0.1", 8899)),
    ]
    assert events[-2:] == [("import", "site"), ("import", "uvicorn")]
    module.os.read.assert_not_called()
    site.main.assert_called_once_with()
    uvicorn.run.assert_called_once_with(
        application, uds=f"/var/lib/prism-operations/staging/{RELEASE}/stage.sock", workers=1
    )
    connection.settimeout.assert_called_with(0.5)
    assert not any(
        "/" in value or "synthetic" in value
        for item in proof["checks"].values()
        for value in item.values()
        if isinstance(value, str)
    )


@pytest.mark.parametrize("readable", ["/home/ubuntu/stock-analyzer/.env", "/proc/111/environ"])
def test_readable_secret_is_closed_without_read_and_blocks_all_environment_imports(readable):
    module, stream, events, _, site, uvicorn, importer = bootstrap(readable=readable)
    with pytest.raises(RuntimeError, match="stage_isolation_probe_failed"):
        invoke(module, importer)
    module.os.read.assert_not_called()
    module.os.close.assert_called_once_with(42)
    assert not all(item["blocked"] for item in json.loads(stream.getvalue())["checks"].values())
    assert not any(event[0] == "import" for event in events)
    site.main.assert_not_called()
    uvicorn.run.assert_not_called()


@pytest.mark.parametrize("code", [0, errno.ETIMEDOUT, errno.EINVAL])
def test_reachable_or_unknown_network_result_blocks_start(code):
    module, _, _, _, _, uvicorn, importer = bootstrap(connection_code=code)
    with pytest.raises(RuntimeError, match="stage_isolation_probe_failed"):
        invoke(module, importer)
    uvicorn.run.assert_not_called()


def test_writable_candidate_only_creates_exclusive_canary_and_blocks_start():
    module, _, events, _, _, uvicorn, importer = bootstrap(writable=True)
    with pytest.raises(RuntimeError, match="stage_isolation_probe_failed"):
        invoke(module, importer)
    canary = f"/opt/prism/releases/{COMMIT}/.stage-write-probe-{NONCE}"
    flags = next(event[2] for event in events if event[:2] == ("open", canary))
    assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
    module.os.unlink.assert_called_once_with(canary)
    uvicorn.run.assert_not_called()


def test_writable_runtime_canary_blocks_import_without_reading_any_content():
    module, stream, events, _, site, uvicorn, importer = bootstrap(runtime_writable=True)
    with pytest.raises(RuntimeError, match="stage_isolation_probe_failed"):
        invoke(module, importer)
    canary = "/run/.prism-stage-write-probe-" + NONCE
    flags = next(event[2] for event in events if event[:2] == ("open", canary))
    assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
    module.os.unlink.assert_called_once_with(canary)
    module.os.read.assert_not_called()
    assert json.loads(stream.getvalue())["checks"]["private_runtime_write"]["blocked"] is False
    site.main.assert_not_called()
    uvicorn.run.assert_not_called()


@pytest.mark.parametrize("changed", [{"issued_at": NOW - 121}, {"application": "os:system"}])
def test_stale_or_arbitrary_entry_rejected_before_syscalls(changed):
    module, _, events, _, _, _, importer = bootstrap()
    with pytest.raises(RuntimeError, match="stage_probe_identity_rejected"):
        invoke(module, importer, **changed)
    assert not events


@pytest.mark.parametrize(
    "change",
    [
        {"nonce": "c" * 32},
        {"stage_pid": 333},
        {"checked_at": NOW - 1},
        {"production_pid": 444},
        {"commit": "c" * 40},
    ],
)
def test_parent_rejects_replayed_or_other_process_proof(tmp_path, change):
    module, stream, _, _, _, _, importer = bootstrap()
    invoke(module, importer)
    proof = json.loads(stream.getvalue())
    proof.update(change)
    path = tmp_path / "isolation-proof.json"
    path.write_bytes(remote.encoded(proof))
    path.chmod(0o600)
    engine = remote.RemoteRelease(tmp_path, now=lambda: NOW)
    engine.request = {"commit": COMMIT, "release_id": RELEASE}
    with pytest.raises(remote.ReleaseError, match="stage_isolation_proof_rejected"):
        engine.stage_proof(tmp_path, NONCE, NOW, 111, 222, "server.main:app")


def test_real_process_validation_rejects_shared_network_namespace(tmp_path, monkeypatch):
    engine = remote.RemoteRelease(tmp_path, now=lambda: NOW)
    engine.request = {"commit": COMMIT}
    engine.process_id = lambda unit: 111 if unit == remote.SERVICE else 222
    engine.run = lambda *args: b"prism-stage"
    engine.stage_runtime_mount = lambda process: True  # This test isolates netns validation.
    monkeypatch.setattr(remote.pwd, "getpwnam", lambda name: types.SimpleNamespace(pw_uid=os.getuid()))
    for pid in (111, 222):
        folder = tmp_path / "proc" / str(pid) / "ns"
        folder.mkdir(parents=True)
        (folder / "net").symlink_to("net:[1234]")
    with pytest.raises(remote.ReleaseError, match="stage_process_identity_mismatch"):
        engine.stage_process_identity(111)
    (tmp_path / "proc/222/ns/net").unlink()
    (tmp_path / "proc/222/ns/net").symlink_to("net:[5678]")
    assert engine.stage_process_identity(111)["separate_network_namespace"] is True


@pytest.mark.parametrize(
    "case",
    [
        "private_readonly",
        "host_mount",
        "writable",
        "foreign_owner",
        "world_writable",
        "changed_path",
        "unavailable",
    ],
)
def test_parent_uses_actual_pid_runtime_mount_identity_owner_and_readonly_flag(tmp_path, monkeypatch, case):
    engine = remote.RemoteRelease(tmp_path)
    process = tmp_path / "proc/222"
    runtime = process / "root/run"
    host = tmp_path / "run"
    private = types.SimpleNamespace(st_dev=2, st_ino=222, st_mode=stat.S_IFDIR | 0o755, st_uid=0)
    host_info = types.SimpleNamespace(st_dev=1, st_ino=111)
    current = types.SimpleNamespace(st_dev=2, st_ino=222)
    if case == "host_mount":
        host_info = types.SimpleNamespace(st_dev=2, st_ino=222)
    if case == "foreign_owner":
        private.st_uid = 999
    if case == "world_writable":
        private.st_mode = stat.S_IFDIR | 0o777
    if case == "changed_path":
        current.st_ino = 333
    opened = mock.Mock(side_effect=PermissionError if case == "unavailable" else None, return_value=99)

    def observed(path, *args, **kwargs):
        assert path in {runtime, host}
        return current if path == runtime else host_info

    with monkeypatch.context() as patched:
        patched.setattr(remote.os, "open", opened)
        patched.setattr(remote.os, "close", mock.Mock())
        patched.setattr(remote.os, "fstat", lambda descriptor: private)
        patched.setattr(
            remote.os,
            "fstatvfs",
            lambda descriptor: types.SimpleNamespace(f_flag=0 if case == "writable" else os.ST_RDONLY),
        )
        patched.setattr(Path, "stat", observed)
        if case == "private_readonly":
            assert engine.stage_runtime_mount(process) is True
            flags = opened.call_args.args[1]
            assert opened.call_args.args[0] == runtime
            assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        else:
            with pytest.raises(remote.ReleaseError, match="stage_private_runtime_mount"):
                engine.stage_runtime_mount(process)


@pytest.mark.parametrize(
    "case", ["success", "symlink", "hardlink", "foreign", "replace_before", "replace_after"]
)
def test_socket_publication_never_mutates_the_replaceable_path(tmp_path, case):
    engine = remote.RemoteRelease(tmp_path)
    inode = types.SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=1234, st_dev=1, st_ino=77, st_nlink=1)
    replacement = types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_dev=1, st_ino=88)
    if case == "symlink":
        inode.st_mode = stat.S_IFLNK | 0o777
    elif case == "hardlink":
        inode.st_nlink = 2
    elif case == "foreign":
        inode.st_uid = 0
    path = mock.Mock()
    path.lstat.side_effect = [
        replacement if case == "replace_before" else inode,
        replacement if case == "replace_after" else inode,
    ]
    with (
        mock.patch.object(remote.os, "O_PATH", 0o10000000, create=True),
        mock.patch.object(remote.os, "open", return_value=17) as opened,
        mock.patch.object(remote.os, "fstat", return_value=inode),
        mock.patch.object(remote.os, "chown") as chown,
        mock.patch.object(remote.os, "chmod") as chmod,
        mock.patch.object(remote.os, "close") as closed,
        mock.patch.object(remote.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=1234)),
        mock.patch.object(remote.grp, "getgrnam", return_value=types.SimpleNamespace(gr_gid=4567)),
    ):
        if case == "success":
            engine.publish_stage_socket(path)
        else:
            with pytest.raises(remote.ReleaseError, match="stage_socket_identity_rejected"):
                engine.publish_stage_socket(path)
        flags = opened.call_args.args[1]
        assert flags & os.O_NOFOLLOW and flags & remote.os.O_PATH
        closed.assert_called_once_with(17)
        if case in {"success", "replace_after"}:
            chown.assert_called_once_with("/proc/self/fd/17", 1234, 4567)
            chmod.assert_called_once_with("/proc/self/fd/17", 0o660)
        else:
            chown.assert_not_called()
            chmod.assert_not_called()


def test_socket_pinning_failure_never_falls_back_to_path_mutation(tmp_path):
    engine = remote.RemoteRelease(tmp_path)
    with (
        mock.patch.object(remote.os, "O_PATH", 0o10000000, create=True),
        mock.patch.object(remote.os, "open", side_effect=PermissionError),
        mock.patch.object(remote.os, "chown") as chown,
        mock.patch.object(remote.os, "chmod") as chmod,
    ):
        with pytest.raises(PermissionError):
            engine.publish_stage_socket(tmp_path / "stage.sock")
        chown.assert_not_called()
        chmod.assert_not_called()


def seeded_stage(tmp_path):
    import sqlite3
    from contextlib import closing

    from server import database

    engine = remote.RemoteRelease(tmp_path, now=lambda: NOW)
    engine.request = {"commit": COMMIT, "release_id": RELEASE}
    engine.record = engine.operations / RELEASE
    engine.record.mkdir(parents=True, mode=0o700)
    source = engine.production / "data/chat.db"
    source.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database.DB_PATH)) as original, closing(sqlite3.connect(source)) as copied:
        original.backup(copied)
    stage = engine.operations / "staging" / RELEASE
    (stage / "data").mkdir(parents=True, mode=0o700)
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(stage / "data/chat.db")) as copied,
    ):
        original.backup(copied)
    before = remote.digest(source.read_bytes())
    accounts = engine.seed_stage_probes(stage)
    return engine, stage, accounts, before


def test_synthetic_seed_is_private_distinct_and_never_changes_original_database(tmp_path):
    import sqlite3
    from contextlib import closing

    engine, stage, accounts, before = seeded_stage(tmp_path)
    assert len({item["username"] for item in accounts}) == 2
    assert len({item["password"] for item in accounts}) == 2
    assert remote.digest((engine.production / "data/chat.db").read_bytes()) == before
    private = engine.record / "stage-probe-credentials.json"
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE((stage / "data/secret_key").stat().st_mode) == 0o600
    secret = (stage / "data/secret_key").read_text()
    with closing(sqlite3.connect(stage / "data/chat.db")) as db:
        for item in accounts:
            assert db.execute(
                "SELECT password_hash FROM users WHERE username=?", [item["username"]]
            ).fetchone()[0] == remote.digest((item["password"] + secret).encode())
            assert (
                db.execute(
                    "SELECT username FROM conversations WHERE id=?", [item["conversation"]]
                ).fetchone()[0]
                == item["username"]
            )
    with pytest.raises(remote.ReleaseError, match="stage_probe_database_rejected"):
        engine.seed_stage_probes(engine.production)
    with pytest.raises(remote.ReleaseError, match="stage_probe_copy_required"):
        engine.seed_stage_probes(stage)


@pytest.mark.parametrize("failure", [None, "wrong_commit", "foreign_allowed", "raw_error"])
def test_real_main_routes_probe_login_history_foreign_denial_and_crud(tmp_path, monkeypatch, failure):
    import asyncio
    from http.cookies import SimpleCookie

    from httpx import ASGITransport, AsyncClient

    from server import config, database, main

    engine, stage, accounts, before = seeded_stage(tmp_path)
    database.reset_connection()
    monkeypatch.setattr(database, "DB_PATH", stage / "data/chat.db")
    monkeypatch.setattr(config, "SECRET_KEY", (stage / "data/secret_key").read_text())
    monkeypatch.setattr(
        main,
        "readiness_payload",
        lambda: {"status": "ready", "deployed_revision": "f" * 40 if failure == "wrong_commit" else COMMIT},
    )
    requests = []
    with asyncio.Runner() as runner:
        client = AsyncClient(transport=ASGITransport(app=main.app, client=None), base_url="http://stage.test")

        async def send(method, path, body, cookie):
            client.cookies.clear()
            headers = {"X-Requested-With": "XMLHttpRequest"}
            if cookie:
                headers["Cookie"] = cookie
            response = await client.request(method, path, json=body, headers=headers)
            cookies = SimpleCookie()
            for value in response.headers.get_list("set-cookie"):
                cookies.load(value)
            token = cookies.get("token")
            return (
                response.status_code,
                response.json(),
                "token=" + token.value if token and token.value else "",
            )

        def transport(area, method, path, body=None, cookie=""):
            assert area == stage
            requests.append((method, path))
            if failure == "raw_error" and path == "/api/login":
                raise RuntimeError("provider secret=" + accounts[0]["password"])
            result = runner.run(send(method, path, body, cookie))
            if failure == "foreign_allowed" and path == "/api/convs/" + accounts[1]["conversation"]:
                return 200, {"private": accounts[1]["password"]}, ""
            return result

        monkeypatch.setattr(engine, "stage_http", transport)
        try:
            if failure:
                with pytest.raises(remote.ReleaseError, match="stage_journey_failed_check_private_record"):
                    engine.stage_journeys(stage, accounts, "server.main:app")
            else:
                engine.stage_journeys(stage, accounts, "server.main:app")
        finally:
            runner.run(client.aclose())
    result = json.loads((engine.record / "stage-journeys.json").read_bytes())
    assert result["passed"] is (failure is None)
    assert result["synthetic_owners"] == 2 and result["commit"] == COMMIT
    assert result["served_commit"] == ("f" * 40 if failure == "wrong_commit" else COMMIT)
    assert result["http_status_by_check"]["ready_exact_commit"] == 200
    if failure == "foreign_allowed":
        assert result["http_status_by_check"]["owner_1_foreign_conversation"] == 200
        assert result["checks"]["owner_1_foreign_conversation"] is False
    assert result["check_count"] == len(result["checks"])
    assert all(isinstance(value, bool) for value in result["checks"].values())
    text = json.dumps(result)
    for account in accounts:
        assert account["password"] not in text and account["username"] not in text
    assert "provider secret" not in text
    assert remote.digest((engine.production / "data/chat.db").read_bytes()) == before
    assert not any("chat/stream" in path or "analysis" in path or "vibe" in path for _, path in requests)
    if failure is None:
        with database.get_db() as db:
            assert all(
                db.execute("SELECT password_hash FROM users WHERE username=?", [account["username"]])
                .fetchone()[0]
                .startswith("$2")
                for account in accounts
            )
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_stage_http_uses_only_fixed_unix_socket_and_ignores_redirect_targets(tmp_path, monkeypatch):
    engine = remote.RemoteRelease(tmp_path)
    engine.request = {"release_id": RELEASE}
    stage = engine.operations / "staging" / RELEASE
    monkeypatch.setattr(engine, "stage_socket", lambda area: Path("/tmp/fake-stage.sock"))
    channel = mock.Mock()
    response = mock.Mock(status=302)
    response.read.return_value = b"{}"
    response.getheaders.return_value = [
        ("Location", "http://127.0.0.1:8088/api/me"),
        ("Set-Cookie", 'token=""; Max-Age=0'),
    ]
    connection = mock.Mock()
    connection.getresponse.return_value = response
    monkeypatch.setattr(remote.socket, "socket", mock.Mock(return_value=channel))
    monkeypatch.setattr(remote.http.client, "HTTPConnection", mock.Mock(return_value=connection))
    assert engine.stage_http(stage, "GET", "/api/me") == (302, {}, "")
    remote.socket.socket.assert_called_once_with(remote.socket.AF_UNIX, remote.socket.SOCK_STREAM)
    channel.connect.assert_called_once_with(str(engine.stage_socket(stage)))
    assert connection.request.call_count == 1
    assert connection.auto_open == 0
    connection.close.assert_called_once()
    channel.close.assert_called_once()
    with pytest.raises(remote.ReleaseError, match="stage_http_target_rejected"):
        engine.stage_http(stage, "GET", "http://127.0.0.1:8088/api/me")


def test_stage_http_limits_untrusted_response_and_never_returns_cookie_on_oversize(tmp_path, monkeypatch):
    engine = remote.RemoteRelease(tmp_path)
    engine.request = {"release_id": RELEASE}
    monkeypatch.setattr(engine, "stage_socket", lambda area: Path("/tmp/fake-stage.sock"))
    connection = mock.Mock()
    connection.getresponse.return_value.read.return_value = b"x" * (1024 * 1024 + 1)
    monkeypatch.setattr(remote.socket, "socket", mock.Mock())
    monkeypatch.setattr(remote.http.client, "HTTPConnection", mock.Mock(return_value=connection))
    with pytest.raises(remote.ReleaseError, match="stage_http_response_limit"):
        engine.stage_http(engine.operations / "staging" / RELEASE, "GET", "/api/me")
    connection.close.assert_called_once()


@pytest.mark.parametrize("write_status", [503, 200])
def test_recovery_probe_requires_write_denial_and_does_not_start_research(
    tmp_path, monkeypatch, write_status
):
    engine, stage, accounts, _ = seeded_stage(tmp_path)
    calls, active = [], set()
    tokens = {"token=" + str(i + 1) * 64: account for i, account in enumerate(accounts)}

    def transport(area, method, path, body=None, cookie=""):
        calls.append((method, path))
        if path == "/health/ready":
            return 200, {"status": "ready", "deployed_revision": COMMIT}, ""
        if path == "/api/login":
            token = next(key for key, account in tokens.items() if account["username"] == body["username"])
            active.add(token)
            return 200, {}, token
        if cookie not in active:
            return 401, {}, ""
        account = tokens[cookie]
        if path == "/api/logout":
            active.remove(cookie)
            return 200, {}, ""
        if method != "GET":
            return write_status, {}, ""
        if path == "/api/me":
            return 200, {"user": account["username"], "is_admin": False}, ""
        if path == "/api/convs":
            return 200, [{"id": account["conversation"]}], ""
        if path == "/api/convs/" + account["conversation"]:
            return (
                200,
                {
                    "id": account["conversation"],
                    "messages": [{"content": "Prism synthetic acceptance narrative"}],
                },
                "",
            )
        if path == "/api/reports/library":
            return 200, {"reports": [{"id": account["report"]}]}, ""
        if path == "/api/reports/library/" + account["report"]:
            return (
                200,
                {"id": account["report"], "report": {"summary": "Prism synthetic acceptance narrative"}},
                "",
            )
        return 404, {}, ""

    monkeypatch.setattr(engine, "stage_http", transport)
    if write_status == 503:
        engine.stage_journeys(stage, accounts, "server.recovery:app")
    else:
        with pytest.raises(remote.ReleaseError, match="stage_journey_failed_check_private_record"):
            engine.stage_journeys(stage, accounts, "server.recovery:app")
    result = json.loads((engine.record / "stage-journeys.json").read_bytes())
    assert result["passed"] is (write_status == 503)
    assert not any("stream" in path or "analysis" in path or "vibe" in path for _, path in calls)
    assert not any(path.endswith("/skill") for _, path in calls)


def test_probe_stop_is_preserved_as_cancelled_not_masked_as_transport_error(tmp_path):
    engine, stage, accounts, _ = seeded_stage(tmp_path)
    (engine.operations / "STOP").write_text("stop")
    with pytest.raises(remote.ReleaseError, match="operator_stop"):
        engine.stage_journeys(stage, accounts, "server.main:app")
    result = json.loads((engine.record / "stage-journeys.json").read_bytes())
    assert result["passed"] is False
