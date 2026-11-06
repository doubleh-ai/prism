"""Purpose-specific internal API tokens from private files or legacy settings.

Only the service operator chooses the directory. Requests and Agents cannot
choose filenames or paths. File mode has no environment fallback, including
missing, malformed or changed files. Never log returned values or read errors.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from .credential_permissions import is_private_file

DIRECTORY_ENV = "PRISM_INTERNAL_TOKEN_DIRECTORY"
SLOTS = {
    "monitor": ("PRISM_FEEDBACK_MONITOR_TOKEN", "feedback-monitor-token"),
    "review": ("PRISM_FEEDBACK_REVIEW_TOKEN", "feedback-review-token"),
    "progress": ("PRISM_FEEDBACK_PROGRESS_TOKEN", "feedback-progress-token"),
    "worker": ("PRISM_FEEDBACK_WORKER_TOKEN", "feedback-worker-token"),
}


def _valid(value):
    return type(value) is str and 32 <= len(value) <= 512 and all(33 <= ord(c) <= 126 for c in value)


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


def _files(directory):
    path = Path(directory)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ValueError()
    owners = {0, os.geteuid()}
    tokens = dict.fromkeys(SLOTS, "")
    with contextlib.ExitStack() as opened:
        parent = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened.callback(os.close, parent)
        original = os.fstat(parent)
        if not stat.S_ISDIR(original.st_mode) or original.st_uid not in owners or original.st_mode & 0o022:
            raise ValueError()
        files = []
        for role, (_, name) in SLOTS.items():
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
            except FileNotFoundError:
                continue
            opened.callback(os.close, fd)
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_uid not in owners
                or before.st_nlink != 1
                or not is_private_file(fd, parent)
                or not 32 <= before.st_size <= 513
            ):
                raise ValueError()
            raw = os.read(fd, 514)
            if len(raw) != before.st_size:
                raise ValueError()
            if raw.endswith(b"\n"):
                raw = raw[:-1]
            value = raw.decode("ascii")
            if not _valid(value):
                raise ValueError()
            tokens[role] = value
            files.append((name, fd, before))
        for name, fd, before in files:
            if _stamp(os.fstat(fd)) != _stamp(before) or _stamp(
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            ) != _stamp(before):
                raise ValueError()
        if _stamp(os.fstat(parent)) != _stamp(original) or _stamp(path.lstat()) != _stamp(original):
            raise ValueError()
        return tokens


def load_tokens():
    """Return only closed slots; invalid configuration disables machine auth."""
    directory = os.environ.get(DIRECTORY_ENV)
    if directory is None:
        return {
            role: value if _valid(value := os.environ.get(env, "")) else ""
            for role, (env, _) in SLOTS.items()
        }
    try:
        return _files(directory)
    except (OSError, ValueError, UnicodeError, RuntimeError):
        return dict.fromkeys(SLOTS, "")
