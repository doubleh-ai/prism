"""Measured systemd ACL form and adversarial permission cases, no real secrets."""

import contextlib
import os
import stat
import struct
from types import SimpleNamespace as NS

import pytest

from server import credential_permissions as p

FILE, DIRECTORY, UID = 100001, 100002, 17001
ROOT = "/run/credentials/synthetic.service"
NAME = "feedback-monitor-token"


def acl(permission, uid=UID, *, group=0, other=0, extra=()):
    undefined = 2**32 - 1
    rows = [
        (1, permission, undefined),
        (2, permission, uid),
        *extra,
        (4, group, undefined),
        (16, permission, undefined),
        (32, other, undefined),
    ]
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *row) for row in rows)


@pytest.fixture
def systemd(monkeypatch):
    # UID, modes and ACL entries match the captured Linux systemd credential
    # projection. These syscall doubles are not a real Linux mount attestation.
    def info(kind, mode, inode):
        return NS(
            st_dev=1,
            st_ino=inode,
            st_mode=kind | mode,
            st_uid=0,
            st_gid=0,
            st_nlink=1,
            st_size=64,
            st_mtime_ns=1,
            st_ctime_ns=2,
        )

    state = NS(
        file=info(stat.S_IFREG, 0o440, 2),
        directory=info(stat.S_IFDIR, 0o550, 3),
        file_acl=acl(4),
        directory_acl=acl(5),
        file_ro=True,
        directory_ro=True,
        file_path=ROOT + "/" + NAME,
        directory_path=ROOT,
        reads=0,
        mutation=None,
    )
    monkeypatch.setattr(p.sys, "platform", "linux")
    monkeypatch.setattr(p.os, "geteuid", lambda: UID)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", ROOT)
    actual_stat, actual_fstat = os.stat, os.fstat
    actual_readlink, actual_realpath = os.readlink, os.path.realpath
    actual_vfs = os.fstatvfs

    def fstat(fd):
        return state.file if fd == FILE else state.directory if fd == DIRECTORY else actual_fstat(fd)

    def file_stat(path, *args, **kwargs):
        if path == ROOT:
            return state.directory
        if path == NAME and kwargs.get("dir_fd") == DIRECTORY:
            return state.file
        return actual_stat(path, *args, **kwargs)

    def link(path, *args, **kwargs):
        if path == f"/proc/self/fd/{FILE}":
            return state.file_path
        if path == f"/proc/self/fd/{DIRECTORY}":
            return state.directory_path
        return actual_readlink(path, *args, **kwargs)

    def xattr(fd, name):
        assert name == "system.posix_acl_access"
        state.reads += 1
        value = state.file_acl if fd == FILE else state.directory_acl
        if state.mutation and state.reads == 1:
            state.file = NS(**(vars(state.file) | {"st_ctime_ns": 3}))
        if value is None:
            raise OSError("synthetic ACL absent")
        return value

    monkeypatch.setattr(p.os, "fstat", fstat)
    monkeypatch.setattr(p.os, "stat", file_stat)
    monkeypatch.setattr(p.os, "readlink", link)
    monkeypatch.setattr(
        p.os.path,
        "realpath",
        lambda path, **kwargs: path if path == ROOT else actual_realpath(path, **kwargs),
    )
    monkeypatch.setattr(
        p.os,
        "fstatvfs",
        lambda fd: (
            NS(f_flag=os.ST_RDONLY if (state.file_ro if fd == FILE else state.directory_ro) else 0)
            if fd in {FILE, DIRECTORY}
            else actual_vfs(fd)
        ),
    )
    monkeypatch.setattr(p.os, "getxattr", xattr, raising=False)
    return state


def test_exact_measured_acl_is_accepted_without_granting_any_group(systemd):
    assert p.is_private_file(FILE, DIRECTORY)
    assert systemd.reads == 4


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_uid",
        "extra_user",
        "group_read",
        "other_read",
        "file_writable",
        "directory_writable",
        "file_path",
        "directory_path",
        "outside_directory",
        "gid",
        "file_owner",
        "directory_owner",
        "hardlink",
        "directory_mode",
        "file_mode",
        "missing_acl",
        "acl_version",
        "directory_acl",
        "identity_drift",
    ],
)
def test_acl_exception_is_closed_to_other_identities_paths_and_writable_files(systemd, monkeypatch, fault):
    s = systemd
    if fault == "wrong_uid":
        s.file_acl = acl(4, UID + 1)
    elif fault == "extra_user":
        s.file_acl = acl(4, extra=((2, 4, UID + 1),))
    elif fault == "group_read":
        s.file_acl = acl(4, group=4)
    elif fault == "other_read":
        s.file_acl = acl(4, other=4)
    elif fault in {"file_writable", "directory_writable"}:
        setattr(s, "file_ro" if fault == "file_writable" else "directory_ro", False)
    elif fault == "file_path":
        s.file_path = "/run/credentials/another.service/" + NAME
    elif fault == "directory_path":
        s.directory_path += "-replacement"
    elif fault == "outside_directory":
        monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/tmp/synthetic")
    elif fault in {"gid", "file_owner", "hardlink", "file_mode"}:
        key, value = {
            "gid": ("st_gid", UID),
            "file_owner": ("st_uid", UID),
            "hardlink": ("st_nlink", 2),
            "file_mode": ("st_mode", stat.S_IFREG | 0o640),
        }[fault]
        s.file = NS(**(vars(s.file) | {key: value}))
    elif fault in {"directory_owner", "directory_mode"}:
        key, value = ("st_uid", UID) if fault == "directory_owner" else ("st_mode", stat.S_IFDIR | 0o750)
        s.directory = NS(**(vars(s.directory) | {key: value}))
    elif fault == "missing_acl":
        s.file_acl = None
    elif fault == "acl_version":
        s.file_acl = struct.pack("<I", 3) + s.file_acl[4:]
    elif fault == "directory_acl":
        s.directory_acl = acl(5, other=5)
    else:
        s.mutation = True
    assert p.is_private_file(FILE, DIRECTORY) is False


@pytest.mark.parametrize(
    "mode,accepted", [(0o400, True), (0o600, True), (0o440, False), (0o644, False), (0o660, False)]
)
def test_real_ordinary_files_retain_owner_only_permissions(tmp_path, monkeypatch, mode, accepted):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    path = tmp_path / "value"
    path.write_bytes(b"synthetic")
    path.chmod(mode)
    with contextlib.ExitStack() as opened:
        parent = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        opened.callback(os.close, parent)
        opened.callback(os.close, fd)
        assert p.is_private_file(fd, parent) is accepted


def test_descriptor_inputs_cannot_be_coerced():
    for fd, parent in ((True, DIRECTORY), (FILE, False), (-1, DIRECTORY), ("1", DIRECTORY)):
        assert p.is_private_file(fd, parent) is False
