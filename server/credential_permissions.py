"""Private-file permissions, including the exact read-only systemd ACL form.

Ordinary files keep owner-only permission checks. The ACL exception requires
the active /run/credentials directory, root ownership, read-only filesystems,
and precisely root + this service UID, with no group or other authorization.
The byte layout is Linux POSIX_ACL_XATTR_VERSION=2, <HHI> entries.
"""

from __future__ import annotations

import os
import stat
import struct
import sys


def _stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _acl_bytes(uid, permission):
    undefined = 2**32 - 1
    rows = (
        (1, permission, undefined),
        (2, permission, uid),
        (4, 0, undefined),
        (16, permission, undefined),
        (32, 0, undefined),
    )
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *row) for row in rows)


def is_private_file(fd, directory_fd):
    """Check descriptor permissions; caller still binds names, size and reads."""
    if type(fd) is not int or type(directory_fd) is not int or min(fd, directory_fd) < 0:
        return False
    try:
        before = os.fstat(fd)
        uid = os.geteuid()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid not in {0, uid}:
            return False
        if not before.st_mode & 0o7077:
            return True
        if not (
            sys.platform == "linux"
            and 0 < uid < 2**32
            and before.st_uid == before.st_gid == 0
            and stat.S_IMODE(before.st_mode) == 0o440
        ):
            return False
        parent = os.fstat(directory_fd)
        if not (
            stat.S_ISDIR(parent.st_mode)
            and parent.st_uid == parent.st_gid == 0
            and stat.S_IMODE(parent.st_mode) == 0o550
        ):
            return False
        directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not (
            os.path.dirname(directory) == "/run/credentials"
            and os.path.normpath(directory) == directory
            and os.path.realpath(directory, strict=True) == directory
            and os.readlink(f"/proc/self/fd/{directory_fd}") == directory
        ):
            return False
        path = os.readlink(f"/proc/self/fd/{fd}")
        if os.path.dirname(path) != directory:
            return False
        name = os.path.basename(path)
        if not name or name in {".", ".."}:
            return False
        if not (os.fstatvfs(fd).f_flag & os.ST_RDONLY and os.fstatvfs(directory_fd).f_flag & os.ST_RDONLY):
            return False
        file_acl = os.getxattr(fd, "system.posix_acl_access")
        directory_acl = os.getxattr(directory_fd, "system.posix_acl_access")
        if file_acl != _acl_bytes(uid, 4) or directory_acl != _acl_bytes(uid, 5):
            return False
        if (
            os.getxattr(fd, "system.posix_acl_access") != file_acl
            or os.getxattr(directory_fd, "system.posix_acl_access") != directory_acl
        ):
            return False
        return _stamp(before) == _stamp(os.fstat(fd)) == _stamp(
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        ) and _stamp(parent) == _stamp(os.fstat(directory_fd)) == _stamp(
            os.stat(directory, follow_symlinks=False)
        )
    except (OSError, ValueError, AttributeError, RuntimeError):
        return False
