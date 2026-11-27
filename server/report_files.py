"""Explicitly owned, immutable report files; never infer ownership from a skill."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
import time
from pathlib import Path

from fastapi import HTTPException

from . import config
from .database import get_db

ALLOWED_EXTENSIONS = {".html", ".htm", ".pdf", ".csv", ".md", ".json"}
MAX_REPORT_BYTES = 50 * 1024 * 1024

# Enforced on the response as well as the iframe, including direct navigation.
# These are untrusted generated documents, not application pages.
REPORT_CSP = (
    "sandbox; default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "frame-ancestors 'self'; base-uri 'none'; form-action 'none'; object-src 'none'"
)


def _root() -> Path:
    return config.DATA_DIR / "report_files"


def _owned_conv(username: str, conv_id: str) -> None:
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
            [conv_id, username],
        ).fetchone()
    if not row:
        raise HTTPException(404, "会话不存在或不可访问")


def register_report_file(username: str, conv_id: str, run_id: str, path: Path) -> dict:
    """Copy an explicitly identified producer output to private immutable storage.

    Internal producers must supply the actual owner, conversation and research
    run. There is deliberately no HTTP endpoint for claiming arbitrary files.
    Legacy files stay untouched until their ownership can be established.
    """
    _owned_conv(username, conv_id)
    if not run_id or len(run_id) > 128:
        raise ValueError("report run is required")
    from .research import read_research_run

    run = read_research_run(username, run_id)["run"]
    if run.get("conv_id") != conv_id:
        raise HTTPException(404, "研究不存在或不可访问")
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS or path.is_symlink():
        raise ValueError("unsupported report file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
            raise ValueError("report file is not a bounded regular file")
        content = source.read(MAX_REPORT_BYTES + 1)
    if len(content) > MAX_REPORT_BYTES:
        raise ValueError("report file is too large")
    rid = secrets.token_hex(16)
    filename = rid + ext
    root = _root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("report storage must not be a symlink")
    destination = root / filename
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(content)
        now = int(time.time())
        with get_db() as db:
            db.execute(
                "INSERT INTO owned_report_files(id,username,conv_id,run_id,filename,title,sha256,size,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    rid,
                    username,
                    conv_id,
                    run_id,
                    filename,
                    path.name[:255],
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                    now,
                ],
            )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {
        "id": rid,
        "filename": filename,
        "title": path.name,
        "run_id": run_id,
        "size": len(content),
        "mtime": now,
    }


def list_report_files(username: str, conv_id: str) -> list[dict]:
    _owned_conv(username, conv_id)
    with get_db() as db:
        rows = db.execute(
            "SELECT id,run_id,filename,title,size,created_at AS mtime FROM owned_report_files "
            "WHERE username=? AND conv_id=? ORDER BY created_at DESC,id DESC LIMIT 100",
            [username, conv_id],
        ).fetchall()
    return [dict(row) for row in rows]


def read_report_file(username: str, conv_id: str, filename: str) -> tuple[dict, bytes]:
    _owned_conv(username, conv_id)
    if Path(filename).name != filename or ".." in filename or "\x00" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件路径")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM owned_report_files WHERE username=? AND conv_id=? AND filename=?",
            [username, conv_id, filename],
        ).fetchone()
    if not row:
        raise HTTPException(404, "报告不存在或不可访问")
    root = _root().resolve()
    path = root / filename
    try:
        path.resolve().relative_to(root)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
                raise HTTPException(413, "报告文件不可安全读取")
            content = source.read(MAX_REPORT_BYTES + 1)
    except (OSError, ValueError):
        raise HTTPException(404, "报告不存在或不可访问") from None
    if len(content) != row["size"] or hashlib.sha256(content).hexdigest() != row["sha256"]:
        raise HTTPException(409, "报告文件发生变化，暂不可读取")
    return dict(row), content
