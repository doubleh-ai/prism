"""Bounded, private project attachments in the same transactional backup store.

Bytes are untrusted and never rendered or passed to a model. IDs and current
project permissions, not URLs or filenames, authorize every download.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import PurePosixPath
from urllib.parse import quote, unquote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field, field_validator

from ..database import check_rate_limit, get_db
from . import collaboration, projects, store
from .models import ClosedModel, CreateSpace, clean_text
from .routes import _body, _id, _owner

router = APIRouter(prefix="/api/work/projects", tags=["work"])
MAX_FILE = 5 * 1024 * 1024
MAX_OWNER_BYTES = 100 * 1024 * 1024


class Upload(ClosedModel):
    request_id: str = Field(min_length=36, max_length=36)
    filename: str = Field(min_length=1, max_length=160)
    task_id: str | None = None
    _uuid = field_validator("request_id")(CreateSpace.request_uuid.__func__)

    @field_validator("task_id")
    @classmethod
    def task(cls, value):
        return CreateSpace.request_uuid(value) if value is not None else None

    @field_validator("filename")
    @classmethod
    def filename_text(cls, value):
        value = clean_text(value)
        if (
            any(c in value for c in "/\\\r\n\t")
            or re.search(r"[\u202a-\u202e\u2066-\u2069]", value)
            or value.startswith(".")
        ):
            raise ValueError("filename must be a plain name")
        if PurePosixPath(value).suffix.lower() not in {
            ".txt",
            ".md",
            ".csv",
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
        }:
            raise ValueError("unsupported file type")
        return value


class UploadFingerprint(Upload):
    sha256: str
    size: int


def init_attachments_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS work_project_attachments (
            id TEXT PRIMARY KEY,project_id TEXT NOT NULL,uploader TEXT NOT NULL,
            filename TEXT NOT NULL,mime TEXT NOT NULL,size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,data BLOB,version INTEGER NOT NULL,state TEXT NOT NULL,
            task_id TEXT,created_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS work_attachments_project ON work_project_attachments(project_id,state,created_at);
        CREATE TRIGGER IF NOT EXISTS work_attachments_project_delete AFTER DELETE ON work_projects BEGIN
            DELETE FROM work_project_attachments WHERE project_id=OLD.id;
        END;
        CREATE TRIGGER IF NOT EXISTS work_attachments_account_delete AFTER DELETE ON users BEGIN
            UPDATE work_project_attachments SET uploader='[removed]',data=NULL,state='removed',version=version+1 WHERE uploader=OLD.username;
        END;
        CREATE TRIGGER IF NOT EXISTS work_attachments_task_delete AFTER DELETE ON work_tasks BEGIN
            UPDATE work_project_attachments SET task_id=NULL,version=version+1 WHERE task_id=OLD.id;
        END;
        """)


def metadata(row):
    return {
        k: row[k]
        for k in (
            "id",
            "filename",
            "mime",
            "size",
            "sha256",
            "version",
            "state",
            "task_id",
            "uploader",
            "created_at",
        )
    }


def owned(db, actor, project_id, attachment_id):
    parent, role = collaboration.access(db, actor, project_id)
    row = db.execute(
        "SELECT * FROM work_project_attachments WHERE project_id=? AND id=?", [project_id, attachment_id]
    ).fetchone()
    if row is None:
        raise HTTPException(404, "附件不存在或不可访问")
    return parent, role, row


def listing(db, actor, project_id):
    parent, role = collaboration.access(db, actor, project_id)
    rows = db.execute(
        "SELECT id,filename,mime,size,sha256,version,state,task_id,uploader,created_at FROM work_project_attachments WHERE project_id=? AND state='active' ORDER BY created_at DESC,id LIMIT 31",
        [project_id],
    ).fetchall()
    return {
        "items": [metadata(r) for r in rows[:30]],
        "truncated": len(rows) > 30,
        "can_edit": role in {"owner", "editor"} and parent["status"] == "active",
    }


def _mime(filename, data):
    if not data or len(data) > MAX_FILE:
        raise HTTPException(413, "附件不能为空且不能超过5MiB")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(422, "文本附件需要UTF-8编码") from None
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", text):
            raise HTTPException(422, "文本附件包含无效控制字符")
        return "text/plain"
    signatures = {
        ".pdf": (b"%PDF-", "application/pdf"),
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    }
    prefix, mime = signatures[suffix]
    if not data.startswith(prefix):
        raise HTTPException(422, "附件类型与内容不一致")
    return mime


def upload(actor, project_id, body, data):
    mime = _mime(body.filename, data)
    signed = UploadFingerprint(**body.model_dump(), sha256=hashlib.sha256(data).hexdigest(), size=len(data))
    kind = "project_attachment_upload"
    fingerprint = store._fingerprint(kind, project_id, signed)
    with store._transaction(actor) as db:
        parent, role = collaboration.access(db, actor, project_id, write=True)
        if parent["status"] != "active":
            raise HTTPException(409, "请先激活项目再上传附件")
        old = store._replay(db, actor, body.request_id, fingerprint)
        if old:
            _, _, current = owned(db, actor, project_id, old["attachment"]["id"])
            return {"attachment": metadata(current), "replayed": True}
        if body.task_id:
            projects.project_task(db, actor, project_id, body.task_id, write=True)
        count = db.execute(
            "SELECT COUNT(*) FROM work_project_attachments WHERE project_id=? AND state='active'",
            [project_id],
        ).fetchone()[0]
        total = db.execute(
            "SELECT COALESCE(SUM(a.size),0) FROM work_project_attachments a JOIN work_projects p ON p.id=a.project_id WHERE p.owner=? AND a.state='active'",
            [parent["owner"]],
        ).fetchone()[0]
        if count >= 30 or total + len(data) > MAX_OWNER_BYTES:
            raise HTTPException(409, "附件额度已满：每项目30份、负责人所有项目合计100MiB")
        key = str(uuid4())
        db.execute(
            "INSERT INTO work_project_attachments VALUES(?,?,?,?,?,?,?,?,1,'active',?,?)",
            [
                key,
                project_id,
                actor,
                body.filename,
                mime,
                len(data),
                signed.sha256,
                data,
                body.task_id,
                int(time.time()),
            ],
        )
        response = {"attachment": metadata(owned(db, actor, project_id, key)[2])}
        store._record_request(db, actor, signed, kind, "", project_id, fingerprint, response)
        collaboration.audit(db, project_id, actor, "attachment_uploaded", key)
        return {**response, "replayed": False}


def remove(actor, project_id, attachment_id, body, *, transaction=None):
    with store._transaction(actor, transaction) as db:
        _, role, row = owned(db, actor, project_id, attachment_id)
        if role not in {"owner", "editor"} or (actor != row["uploader"] and role != "owner"):
            raise HTTPException(403, "只有上传者或项目负责人可以移除附件")
        fingerprint = store._fingerprint("project_attachment_remove", attachment_id, body)
        replay = store._replay(db, actor, body.request_id, fingerprint)
        if replay:
            return {**replay, "replayed": True}
        if row["version"] != body.expected_version or row["state"] != "active":
            raise HTTPException(409, "附件已有变化，请刷新核对")
        db.execute(
            "UPDATE work_project_attachments SET state='removed',data=NULL,version=version+1 WHERE id=?",
            [attachment_id],
        )
        response = {"attachment": metadata(owned(db, actor, project_id, attachment_id)[2])}
        collaboration.audit(db, project_id, actor, "attachment_removed", attachment_id)
        store._record_request(
            db, actor, body, "project_attachment_remove", "", attachment_id, fingerprint, response
        )
        return {**response, "replayed": False}


@router.get("/{project_id}/attachments")
async def list_attachments(project_id: str, request: Request):
    actor = _owner(request)
    with get_db() as db:
        return listing(db, actor, _id(project_id))


@router.post("/{project_id}/attachments")
async def post_attachment(project_id: str, request: Request):
    actor = _owner(request)
    project_id = _id(project_id)
    if not check_rate_limit("work-upload:" + actor, 10, 60):
        raise HTTPException(429, "上传较多，请稍后再试")
    header = request.headers.get("X-Prism-Upload", "")
    try:
        if len(header) > 8192 or re.search(r"%(?![0-9a-fA-F]{2})", header):
            raise ValueError()

        def unique(pairs):
            value = {}
            for k, v in pairs:
                if k in value:
                    raise ValueError()
                value[k] = v
            return value

        body = Upload.model_validate(json.loads(unquote(header, errors="strict"), object_pairs_hook=unique))
    except (ValueError, TypeError, RecursionError):
        raise HTTPException(422, "附件信息无效，请核对名称与请求") from None
    if request.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/octet-stream":
        raise HTTPException(415, "请使用二进制附件上传")
    with get_db() as db:
        collaboration.access(db, actor, project_id, write=True)
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_FILE:
            raise HTTPException(413, "附件不能超过5MiB")
        data.extend(chunk)
    if _owner(request) != actor:
        raise HTTPException(401)
    return upload(actor, project_id, body, bytes(data))


@router.get("/{project_id}/attachments/{attachment_id}/download")
async def download(project_id: str, attachment_id: str, request: Request):
    actor = _owner(request)
    with get_db() as db:
        row = owned(db, actor, _id(project_id), _id(attachment_id))[2]
        if row["state"] != "active" or row["data"] is None:
            raise HTTPException(404, "附件已移除")
        return Response(
            bytes(row["data"]),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["filename"], safe=""),
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "ETag": '"' + row["sha256"] + '"',
            },
        )


@router.post("/{project_id}/attachments/{attachment_id}/remove")
async def delete_attachment(project_id: str, attachment_id: str, request: Request):
    actor = _owner(request)
    body = await _body(request, collaboration.VersionRequest)
    if _owner(request) != actor:
        raise HTTPException(401)
    return remove(actor, _id(project_id), _id(attachment_id), body)
