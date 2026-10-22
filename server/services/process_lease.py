"""Fixed local child identity and reap adapters; never a descendant/seal proof."""

from __future__ import annotations

import asyncio
import ctypes
import sys
from pathlib import Path

from ..work_lifecycle import LifecycleError


class ProcessIdentityUnknown(RuntimeError):
    """The real child birth identity could not be established."""


class _ProcBSDInfo(ctypes.Structure):
    # macOS SDK sys/proc_info.h proc_bsdinfo, MAXCOMLEN=16; libproc.h proc_pidinfo.
    _fields_ = [
        *[
            (name, ctypes.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ],
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        *[(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "tdev", "tpgid")],
        ("nice", ctypes.c_int32),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


def process_start_ticks(pid: int) -> int:
    """Kernel Linux start ticks, or macOS birth microseconds for development only."""
    if type(pid) is not int or pid < 2:
        raise ProcessIdentityUnknown("child_identity_unknown")
    try:
        if sys.platform == "linux":
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            prefix, separator, tail = raw.rpartition(") ")
            if not separator or not prefix.startswith(f"{pid} ("):
                raise ValueError
            fields = tail.split()
            value = int(fields[19])  # starttime is field 22; tail begins at field 3.
        elif sys.platform == "darwin":
            if ctypes.sizeof(_ProcBSDInfo) != 136 or _ProcBSDInfo.start_sec.offset != 120:
                raise ValueError
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            function = library.proc_pidinfo
            function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
            function.restype = ctypes.c_int
            info = _ProcBSDInfo()
            count = function(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if count != ctypes.sizeof(info) or info.pid != pid or info.start_usec >= 1_000_000:
                raise ValueError
            value = info.start_sec * 1_000_000 + info.start_usec
        else:
            raise ValueError
        if value <= 0:
            raise ValueError
        return value
    except (OSError, ValueError, IndexError, AttributeError):
        raise ProcessIdentityUnknown("child_identity_unknown") from None


def register_process(lease, process) -> bool:
    """Unknown identity blocks release, without discarding otherwise usable output."""
    try:
        lease.attach_process(process, start_ticks=process_start_ticks(process.pid))
    except (ProcessIdentityUnknown, LifecycleError, AttributeError):
        lease.block("process_unknown")
        return False
    return True


def start_process(lease, *argv, **options):
    """Own launch itself before its first await; cancellation cannot orphan spawn."""

    async def launch(_owner):
        process = await asyncio.create_subprocess_exec(*argv, **options)
        return process, register_process(lease, process)

    return lease.manager.start(lease.kind, launch, parent=lease)


async def wait_process(lease, process, registered: bool):
    """Consume an actual wait even when registration failed; unknown stays blocked."""
    if registered:
        return await lease.wait_process(process)
    if isinstance(process, asyncio.subprocess.Process):
        task = asyncio.create_task(process.wait())
        return await asyncio.shield(task)
    # A failed identity already prevents quiescence. Keep this real wait owned too.
    return await lease.manager.run_blocking("auxiliary", process.wait, parent=lease)


def wait_process_sync(lease, process, registered: bool):
    """Direct actual wait; no recursive pool submission from a pool worker."""
    if registered:
        return lease.wait_process_sync(process)
    return process.wait()
