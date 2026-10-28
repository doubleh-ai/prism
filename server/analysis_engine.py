"""Native-only compatibility admission and account-owned saved analysis reads.

The former automatic analysis executor has been removed. Existing reports are
read as stored data; reading a record never starts or resumes work.
"""

import json
from typing import NoReturn

from fastapi import HTTPException

from .database import get_db

ANALYSIS_EXECUTION_NOTICE = "旧版个股研究执行已停用：请使用 Prism 原生研究对话；已有研究记录仍可查看。"


def require_analysis_execution() -> NoReturn:
    """Reject the retired entry point without admitting work or reading data."""
    raise HTTPException(
        503, ANALYSIS_EXECUTION_NOTICE, headers={"X-Prism-Execution-Policy": "native-only-analysis-disabled"}
    )


async def start_analysis(
    ticker: str,
    name: str = "",
    date: str = "",
    username: str = "",
    mode: str = "deep",
    force_refresh: bool = False,
) -> str:
    """Compatibility signature only; parameters cannot enable execution."""
    require_analysis_execution()


def get_analysis_record(analysis_id: str, username: str) -> dict | None:
    """Read one owned SQLite record, preserving original report and dates."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM analysis_runs WHERE id=? AND username=?", [analysis_id, username]
        ).fetchone()
    if not row:
        return None
    record = dict(row)
    record.pop("username", None)
    try:
        report = json.loads(record.pop("report_json") or "null")
        if report is not None and not isinstance(report, dict):
            raise ValueError("invalid saved report")
        record["report"] = report
    except (ValueError, TypeError):
        record["report"] = None
        record["status"] = "error"
        record["error"] = "研究记录暂时无法读取"
    metadata = (record["report"] or {}).get("_analysis_meta")
    metadata = metadata if isinstance(metadata, dict) else {}
    record["research"] = metadata.get("research")
    timings = metadata.get("timings")
    record["timings"] = timings if isinstance(timings, dict) else {}
    record["elapsed"] = record["timings"].get("total_seconds")
    if record["status"] in {"starting", "running"}:
        record["status"] = "interrupted"
        record["error"] = "原研究进程已结束，保留已有内容；未自动重新启动。"
    return record


def _sse(data: dict) -> str:
    """Serialize one saved-history response without polling or model work."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
