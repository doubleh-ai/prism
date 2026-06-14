"""Prism — FastAPI 主入口"""
import logging
import sys
import os
from pathlib import Path
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from .config import TEMPLATE_DIR, STATIC_DIR, LOG_FILE, MODELS, SKILL_MAP, SKILLS_HOME, CLAUDE_BIN
from .database import init_db, get_db
from .auth import login as auth_login, logout as auth_logout, require_auth

# ── Logging ──
LOG_PATH = Path(LOG_FILE)
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO, datefmt="%H:%M:%S",
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(str(LOG_PATH)), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("stock-app")

# ── Init ──
init_db()

# ── FastAPI app ──
app = FastAPI(title="Prism")

# Static files (proper caching via StaticFiles)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Template helpers ──
def render_template(name: str) -> str:
    """Read and return a template file."""
    path = TEMPLATE_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ── Pages ──
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return render_template("login.html")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    u = require_auth(request, optional=True)
    if not u:
        return RedirectResponse("/login")
    return render_template("app.html")


# ── Auth ──
@app.post("/api/login")
async def api_login(request: Request):
    return await auth_login(request)


@app.post("/api/logout")
async def api_logout(request: Request):
    return auth_logout(request)


@app.get("/api/me")
async def api_me(request: Request):
    u = require_auth(request, optional=True)
    if not u:
        raise HTTPException(401)
    return {"user": u}


# ── Conversations ──
from .conversations import (
    list_convs, create_conv, get_conv, delete_conv, update_skill,
    restore_conv, permanent_delete_conv, list_trash, batch_delete_convs,
)


@app.get("/api/convs")
async def api_list_convs(request: Request):
    return list_convs(request)


@app.post("/api/convs")
async def api_create_conv(request: Request):
    return await create_conv(request)


@app.get("/api/convs/{cid}")
async def api_get_conv(cid: str, request: Request):
    return get_conv(cid, request)


@app.delete("/api/convs/{cid}")
async def api_delete_conv(cid: str, request: Request):
    return delete_conv(cid, request)


@app.post("/api/convs/{cid}/restore")
async def api_restore_conv(cid: str, request: Request):
    return restore_conv(cid, request)


@app.delete("/api/convs/{cid}/permanent")
async def api_permanent_delete_conv(cid: str, request: Request):
    return permanent_delete_conv(cid, request)


@app.put("/api/convs/{cid}/skill")
async def api_update_skill(cid: str, request: Request):
    return await update_skill(cid, request)


@app.get("/api/trash")
async def api_list_trash(request: Request):
    return list_trash(request)


@app.post("/api/convs/batch-delete")
async def api_batch_delete_convs(request: Request):
    return await batch_delete_convs(request)


# ── Chat ──
from .chat import chat_stream, chat_respond, chat_cancel


@app.get("/api/models")
async def api_models():
    return [{"id": k, "name": v["name"]} for k, v in MODELS.items()]


@app.get("/api/skills")
async def api_skills():
    return [{"id": k, "name": v["name"], "shortcut": "/" + k[:2]} for k, v in SKILL_MAP.items()]


@app.post("/api/chat/stream")
async def api_chat_stream(request: Request):
    return await chat_stream(request)


@app.post("/api/chat/respond")
async def api_chat_respond(request: Request):
    return await chat_respond(request)


@app.post("/api/chat/cancel")
async def api_chat_cancel(request: Request):
    return await chat_cancel(request)


# ── Report viewer ──
_REPORT_ALLOWED_EXT = {".html", ".htm", ".pdf", ".csv", ".md", ".json"}

# ── Term explanation cache ──
import asyncio
_term_cache: dict = {}

@app.post("/api/explain-term")
async def api_explain_term(request: Request):
    """Generate a brief AI explanation for a financial/professional term."""
    u = require_auth(request)
    body = await request.json()
    term = (body.get("term") or "").strip()
    context = (body.get("context") or "").strip()[:200]
    if not term or len(term) > 80:
        raise HTTPException(400, "无效术语")

    cache_key = term
    if cache_key in _term_cache:
        return {"term": term, "explanation": _term_cache[cache_key], "cached": True}

    prompt = f'请用1-2句中文简练解释金融/商业术语"{term}"。'
    if context:
        prompt += f"上下文：{context}。请结合上下文解释。"
    prompt += "只返回解释内容，不加任何前缀。"

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "-p", prompt,
            "--model", "minimax-m2.7-fast", "--max-tokens", "200",
            "--output-format", "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        explanation = stdout.decode("utf-8", errors="replace").strip()
        # Filter out common prefixes Claude adds
        for prefix in ["好的", "以下是", "这是", "解释：", "答案："]:
            if explanation.startswith(prefix):
                explanation = explanation[len(prefix):].strip()
        if explanation:
            _term_cache[cache_key] = explanation
            return {"term": term, "explanation": explanation, "cached": False}
    except asyncio.TimeoutError:
        pass
    except Exception:
        pass

    # Fallback: return a generic explanation hint
    return {"term": term, "explanation": f"暂无可用的AI解释，建议搜索「{term}」了解更多。", "cached": False}

@app.get("/api/reports/{conv_id}/{filename:path}")
async def api_view_report(conv_id: str, filename: str, request: Request):
    """Serve a generated report file for a conversation.

    Reports are stored under the skill's directory:
    ~/.claude/skills/{skill_dir}/reports/{filename}
    """
    from urllib.parse import unquote

    u = require_auth(request, optional=True)
    if not u:
        # Redirect to login page for browser access
        return RedirectResponse("/login")

    # URL-decode the filename (FastAPI's path converter may keep it encoded)
    filename = unquote(filename)

    # Prevent path traversal
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(400, "非法文件路径")

    ext = os.path.splitext(filename)[1].lower()
    if ext not in _REPORT_ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型: {ext}")

    # Resolve conversation → skill → skill directory
    with get_db() as db:
        row = db.execute(
            "SELECT skill FROM conversations WHERE id=? AND username=? AND deleted_at IS NULL",
            [conv_id, u]
        ).fetchone()
    if not row:
        return HTMLResponse("<h1>404</h1><p>对话不存在或已删除</p>", status_code=404)

    skill_info = SKILL_MAP.get(row["skill"])
    if not skill_info:
        return HTMLResponse("<h1>404</h1><p>技能未注册</p>", status_code=404)

    file_path = SKILLS_HOME / skill_info["dir"] / "reports" / filename
    log.info(f"Report request: conv={conv_id} skill={row['skill']} path={file_path} exists={file_path.exists()}")

    if not file_path.exists():
        return HTMLResponse(
            f"<h1>404</h1><p>报告文件不存在</p><pre>{filename}</pre>",
            status_code=404,
        )

    # Determine MIME type
    mime_map = {
        ".html": "text/html; charset=utf-8",
        ".htm": "text/html; charset=utf-8",
        ".pdf": "application/pdf",
        ".csv": "text/csv; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json",
    }
    mime = mime_map.get(ext, "application/octet-stream")
    # Don't set filename for HTML (avoids Content-Disposition encoding issues with Chinese chars)
    if ext in (".html", ".htm"):
        return FileResponse(str(file_path), media_type=mime)
    return FileResponse(str(file_path), media_type=mime, filename=filename)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server.main:app", host="127.0.0.1", port=8088, reload=True)
