# CapitalLens Architecture Redesign

## Scope

Full refactoring of the stock-analysis-project (CapitalLens) — addressing 6 user-reported issues plus 10 additional problems found during architecture review.

## Problems Addressed

### User-Reported
1. **对话记录溯源异常** → Duplicate chat.py/conversations.py modules, fragile JSONL sync timing
2. **偶尔"网络错误"** → Overbroad catch-all in frontend SSE error handling
3. **代理配置导致沙箱异常** → Hardcoded `http_proxy=http://127.0.0.1:7890` with no fallback
4. **思考过程不按markdown渲染** → `esc()` prevents `marked.parse()` on thinking content
5. **tmux残留** → tmux not required by application; deployment hygiene issue
6. **删除无回收站** → Hard DELETE, no soft delete / restore mechanism

### Architecture-Review Discovered
7. **AskUserQuestion单向缺陷** → `claude -p` is non-interactive; user answers never reach Claude
8. **多轮对话不连续** → Each message spawns new `claude -p` without `--resume`
9. **两套重复模块** → `server/chat.py` + `server/api/chat.py`, `server/conversations.py` + `server/api/conversations.py`
10. **JSONL正则提取脆弱** → `re.search(r"'content':\s*'([^']*)'")` breaks on single quotes
11. **静态文件内联注入** → CSS/JS read into memory, injected per-request, no browser caching
12. **死代码** → `server/skills.py`, `server/tools.py` unused since Claude CLI adoption
13. **DB连接管理不一致** → Mix of context manager and manual open/close patterns
14. **无停止生成按钮** → No AbortController for mid-stream cancellation
15. **代码块无复制按钮** → Missing UX for code blocks
16. **Streams注册表泄漏** → SSE disconnect without cleanup leaves orphaned stream objects
17. **智能滚动缺失** → Auto-scroll forces user to bottom even when reading history

## Target Architecture

```
stock-analysis-project/
├── server/
│   ├── main.py              # FastAPI entry (StaticFiles mount + route registration)
│   ├── config.py             # Env-based config
│   ├── database.py           # SQLite + soft-delete migration
│   ├── auth.py               # Auth (preserved, minor updates)
│   ├── chat.py               # Single SSE endpoint + stdin interaction
│   ├── conversations.py      # Single CRUD + recycle bin
│   ├── proxy.py              # NEW: proxy health check + smart fallback
│   └── services/
│       └── claude_runner.py  # Claude subprocess (stdin interaction + --resume)
├── static/                   # Served via FastAPI StaticFiles
│   ├── css/app.css
│   ├── js/app.js
│   └── lib/marked.min.js
├── templates/
│   ├── login.html
│   └── app.html
├── tests/
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_auth.py
│   ├── test_frontend.py
│   ├── test_integration.py
│   ├── test_jsonl.py
│   └── test_page.py
└── DELETED:
    ├── server/api/           # Entire directory (duplicate modules)
    ├── server/skills.py      # Dead code
    └── server/tools.py       # Dead code
```

## Key Design Decisions

### 1. Bidirectional Claude Interaction

**Flow:**
```
Browser ──SSE──→ Backend ──stdout──→ Claude subprocess
Browser ←──SSE── Backend ←──stdout── Claude subprocess
              [AskUserQuestion detected in stdout]
Browser ──POST──→ Backend ──stdin──→ Claude subprocess
Browser ←──SSE── Backend ←──stdout── Claude continues
```

**New endpoint:** `POST /api/chat/respond` — accepts `{conv_id, answer}` and writes to Claude stdin.

### 2. Multi-Turn Continuity

```
Turn 1: claude -p "msg1" --output-format stream-json --verbose
        → capture session_id from system/init event

Turn 2: claude --resume <session_id> -p "msg2" --output-format stream-json --verbose
        → Claude retains full context from Turn 1
```

### 3. Smart Proxy Detection

```python
# proxy.py
async def get_proxy_config(host="127.0.0.1", port=7890, timeout=2.0) -> dict:
    """TCP ping proxy; return config if reachable, empty dict if not."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        return {"http_proxy": f"http://{host}:{port}", "https_proxy": f"http://{host}:{port}"}
    except Exception:
        logging.getLogger("stock-app").warning("Proxy unreachable, using direct connection")
        return {}
```

### 4. Soft Delete / Recycle Bin

```sql
ALTER TABLE conversations ADD COLUMN deleted_at INTEGER;  -- NULL = active, unix timestamp = deleted
```

**Endpoints:**
- `DELETE /api/convs/{id}` → `UPDATE SET deleted_at = unixepoch()`
- `POST /api/convs/{id}/restore` → `UPDATE SET deleted_at = NULL`
- `DELETE /api/convs/{id}/permanent` → actual DELETE FROM both tables
- `GET /api/trash` → list conversations WHERE deleted_at IS NOT NULL

### 5. Static File Serving

Replace `_read()` + string injection with FastAPI `StaticFiles` mount:
```python
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
```
Templates use `<link rel="stylesheet" href="/static/css/app.css">` and `<script src="/static/js/app.js">`.

### 6. Smart Scroll Behavior

- Track `userScrolledUp` flag on the message list
- On `scroll` event: if user scrolls away from bottom (>80px threshold), set `true`; if back at bottom, set `false`
- In `renderOne()`: only `scrollTo(bottom)` if `!userScrolledUp`
- When a NEW assistant message starts streaming: reset `userScrolledUp = false` and scroll to bottom

### 7. Error Classification

| Error Type | Detection | Frontend Display |
|-----------|-----------|-----------------|
| NETWORK | `fetch()` throws TypeError | "网络连接失败，请检查网络后重试" |
| TIMEOUT | SSE silent > 120s | "响应超时，请重试" |
| SERVER | HTTP 500/502/503 | "服务异常，请稍后重试" |
| PARSE | JSON.parse throws on SSE data | Silent fallback (skip malformed event) |
| PROCESS | Claude process exits non-zero | "AI 引擎异常退出" |

### 8. Thinking Markdown Rendering

```javascript
// Before (broken):
html += '<details class="think-box" open><summary>Thinking</summary><p>'+esc(thinking)+'</p></details>';

// After (fixed):
html += '<details class="think-box" open><summary>Thinking</summary><div class="think-content">'+marked.parse(thinking)+'</div></details>';
```

## Database Migrations

```sql
-- conversations table: add deleted_at for soft delete
ALTER TABLE conversations ADD COLUMN deleted_at INTEGER;

-- messages table: add tool_calls column for structured tool data
-- (no schema change needed; tool data is stored in content JSON)
```

## UI Changes

1. **Stop button**: Appears during streaming, calls `reader.cancel()` + POST to kill backend process
2. **Code copy button**: Each `<pre>` block gets a copy icon in top-right corner
3. **Recycle bin**: Sidebar bottom shows "回收站 (N)" link when items exist
4. **Smart scroll**: No forced scrolling when user reads history
5. **Error states**: Distinct icons/messages for different error types
6. **Thinking blocks**: Full markdown rendering inside collapsible `<details>`
