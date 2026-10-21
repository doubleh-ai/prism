"""Claude Code CLI — interactive subprocess with resume + stdin support"""

import asyncio
import codecs
import contextlib
import json
import logging
import os
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

from ..capabilities import REGISTRY, issue_run_token, list_calls, revoke_run_token
from ..config import (
    CAPABILITY_INTERNAL_URL,
    CHAT_STREAM_HEARTBEAT_SECONDS,
    CHAT_STREAM_IDLE_TIMEOUT_SECONDS,
    CLAUDE_BIN,
    DEFAULT_MODEL,
    MODELS,
    PROJECT_ROOT,
    SKILL_FALLBACK_ROOTS,
    SKILL_MAP,
    SKILLS_HOME,
)
from ..proxy import get_proxy_config
from ..research_context import build_research_context, build_research_prompt

log = logging.getLogger("stock-app")

# Per-conversation running processes: {conv_id: process}
_active_procs: dict = {}
_cancelled_procs: set[int] = set()


def _skill_dir_name(skill_id_or_dir: str) -> str:
    """Translate only public skill IDs; user strings are never filesystem paths."""
    if skill_id_or_dir == "force-chinese":
        return "force-chinese"
    if skill_id_or_dir not in SKILL_MAP:
        raise ValueError("unregistered research method")
    return SKILL_MAP[skill_id_or_dir]["dir"]


def _candidate_skill_dirs(dir_name: str) -> list[Path]:
    allowed_dirs = {item["dir"] for item in SKILL_MAP.values()} | {"force-chinese"}
    if (
        dir_name not in allowed_dirs
        or Path(dir_name).name != dir_name
        or ".." in dir_name
        or "\\" in dir_name
    ):
        return []
    roots = [SKILLS_HOME, *SKILL_FALLBACK_ROOTS]
    if dir_name == "prism-research":
        roots.insert(0, PROJECT_ROOT / "skills")
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            resolved_root = Path(root).resolve()
            path = (resolved_root / dir_name).resolve()
        except (OSError, RuntimeError):
            continue
        if not path.is_relative_to(resolved_root):
            continue
        key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates


def _resolve_skill_dir(dir_name: str) -> tuple[Path | None, list[Path]]:
    """Only resolve directories and their instruction files within approved roots."""
    candidates = _candidate_skill_dirs(dir_name)
    for path in candidates:
        if path.exists() and path.is_dir():
            try:
                instructions = (path / "CLAUDE.md").resolve()
            except (OSError, RuntimeError):
                continue
            if not instructions.is_relative_to(path):
                continue
            return path, candidates
    return None, candidates


async def run_claude_stream(
    question: str,
    skill_name: str = "prism-research",
    user: str = "anon",
    model_cfg: dict | None = None,
    session_id: str | None = None,
    conv_id: str | None = None,
    extra_skills: list | None = None,
    research_context: dict | None = None,
) -> AsyncGenerator[dict, None]:
    """Spawn Claude CLI and stream JSON events.

    Args:
        question: User question text
        skill_name: Primary skill ID (sets CWD, Claude loads its CLAUDE.md)
        user: Authenticated username
        model_cfg: Model configuration dict from MODELS
        session_id: If provided, use --resume to continue existing session
        conv_id: Conversation ID for tracking active process (for cancel/respond)
        extra_skills: Additional skill IDs to inject via --append-system-prompt
        research_context: Server-created clock and run identity for this turn

    Yields:
        Parsed JSON events from Claude's stdout
    """
    info = SKILL_MAP.get(skill_name)
    if info is None:
        yield {"type": "error", "message": "未找到所选研究方法，请重新选择"}
        return
    if extra_skills is not None and (
        not isinstance(extra_skills, list)
        or len(extra_skills) > 10
        or any(
            not isinstance(item, str) or item not in {*SKILL_MAP, "force-chinese"} for item in extra_skills
        )
    ):
        yield {"type": "error", "message": "附加研究方法无效，请从已有方法中选择"}
        return
    skill_dir_name = info["dir"]
    cwd, _ = _resolve_skill_dir(skill_dir_name)

    if cwd is None:
        cwd, _ = _resolve_skill_dir("prism-research")
        if cwd is None:
            yield {"type": "error", "message": "内置研究方法缺失，请检查应用安装"}
            return
        yield {
            "type": "status",
            "message": f"所选研究方法「{info['name']}」尚未安装，本轮使用内置 Prism 研究方法。",
        }

    if model_cfg is None:
        model_cfg = MODELS.get(DEFAULT_MODEL, list(MODELS.values())[0])

    # ── Build system prompts from auxiliary skills ──
    # force-chinese is always-on (enforces Chinese output at system level, not in question)
    system_skills = {"force-chinese"}  # Always-on base
    if extra_skills:
        system_skills.update(extra_skills)

    append_prompts = []
    for es in sorted(system_skills):
        es_dir_name = _skill_dir_name(es)
        es_dir, _ = _resolve_skill_dir(es_dir_name)
        if es_dir is None:
            continue
        es_md = es_dir / "CLAUDE.md"
        if es_md.exists():
            with contextlib.suppress(Exception):
                append_prompts.append(es_md.read_text(encoding="utf-8"))

    # v0.6 market-tag directive: tell the model to embed [[quote:TICKER]]
    # tokens in its output so the frontend can render live price chips.
    MARKET_TAG_PROMPT = (
        "When you mention a specific A-share, HK, or US ticker in your "
        "response, include the literal token [[quote:TICKER]] inline "
        "right after the company or ticker name, using the canonical "
        "ticker code (e.g. 600519, 00700, AAPL). Do not invent tickers. "
        "One token per distinct ticker."
    )
    append_prompts.append(MARKET_TAG_PROMPT)

    # Prism is a user-facing research surface, so a run is only useful when it
    # actually hands a readable conclusion back to the conversation.  Some
    # skills otherwise spend the whole provider window creating an optional
    # HTML artifact and finish before emitting their final answer.
    REPORT_FIRST_PROMPT = (
        "Prism delivery contract: prioritize a complete Chinese Markdown report "
        "in the conversation. Once the core evidence is sufficient, stop "
        "exploratory tool calls and synthesize the result. Start the final "
        "deliverable with the exact heading '## 报告已交付'. Do not end on a "
        "plan or tool output. This research session has no shell or file-writing "
        "capability; deliver Markdown directly. If a source fails, disclose the gap and continue "
        "with verified evidence instead of repeatedly retrying the same source."
    )
    append_prompts.append(REPORT_FIRST_PROMPT)
    research = research_context or build_research_context()
    append_prompts.append(build_research_prompt(research))
    append_prompts.append(
        "本次研究工具仅包括 WebSearch、WebFetch、AskUserQuestion 和显式提供的 Prism 平台能力。"
        "遵循技能的研究方法，但没有任意 shell、文件、SQL 或其他 MCP 访问能力。"
        "平台能力必须实际调用后再陈述结果，不得模拟工具调用。curated_unverified 是待核验背景，"
        "不可据此断言供货关系、市占率或置信度；prior_research 是既有研究，不是外部独立验证。"
        "仅在用户明确请求时启动分析、选股扫描或 Vibe；这些工具立即返回后台记录，"
        "请告知正在运行并给出对应入口，不能把已发起说成已完成，也不要持续轮询阻塞本轮交付。"
    )
    use_bridge = bool(conv_id and user != "anon")
    mcp_config = {"mcpServers": {}}
    if use_bridge:
        mcp_config["mcpServers"]["prism"] = {
            "type": "stdio",
            "command": sys.executable,
            "args": [str(Path(__file__).resolve().parents[1] / "capabilities_mcp.py")],
        }
    else:
        append_prompts.append("本轮没有已授权的持久会话，Prism 平台能力不可用，请勿声称调用成功。")
    native_tools = ["WebSearch", "WebFetch", "AskUserQuestion"]
    allowed_tools = native_tools + ([f"mcp__prism__{name}" for name in REGISTRY] if use_bridge else [])

    # Build command — clean question, system instructions via --append-system-prompt
    cmd = [
        CLAUDE_BIN,
        "-p",
        question,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model_cfg["model"],
        "--tools",
        ",".join(native_tools),
        "--allowedTools",
        ",".join(allowed_tools),
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp_config),
        "--setting-sources",
        "",
        "--append-system-prompt",
        "\n\n".join(append_prompts),
    ]
    if session_id:
        cmd.extend(["--resume", session_id])

    # Dynamic proxy config
    proxy_config = await get_proxy_config()
    # Build env: never inherit API key/sensitive vars from parent unless
    # explicitly allowed. The Claude CLI subprocess should only see the
    # credentials we deliberately pass to it — not anything else sitting
    # in os.environ.
    inherited_keys = {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TMP", "TEMP", "SystemRoot"}
    env = {k: v for k, v in os.environ.items() if k in inherited_keys}
    env.update(
        {
            "HOME": str(Path.home()),
            "NO_COLOR": "1",
            "ANTHROPIC_BASE_URL": model_cfg.get("base_url", ""),
            "ANTHROPIC_AUTH_TOKEN": model_cfg.get("api_key", ""),
        }
    )
    if proxy_config:
        env.update(proxy_config)
    bridge_token = ""
    if use_bridge:
        # The unguessable token exists only in process memory/env; argv and MCP
        # config contain no credential. Each request rechecks conversation ownership.
        try:
            bridge_token = issue_run_token(user, conv_id, research["run_id"])
        except Exception:
            yield {"type": "error", "message": "当前会话的研究授权不可用，请重新打开会话后重试"}
            return
        env["PRISM_CAPABILITY_TOKEN"] = bridge_token
        env["PRISM_CAPABILITY_URL"] = CAPABILITY_INTERNAL_URL
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,  # Open stdin for AskUserQuestion answers
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
    except BaseException:
        if use_bridge:
            revoke_run_token(research["run_id"])
        raise
    log.info(f"Claude PID={proc.pid} skill={skill_name} resume={bool(session_id)}")

    # Register for external control (cancel, respond)
    if conv_id:
        _active_procs[conv_id] = proc

    decoder = codecs.getincrementaldecoder("utf-8")()
    buf = ""
    seen = set()
    heartbeat_seconds = max(0.1, float(CHAT_STREAM_HEARTBEAT_SECONDS))
    idle_timeout_seconds = max(heartbeat_seconds, float(CHAT_STREAM_IDLE_TIMEOUT_SECONDS))
    last_activity = asyncio.get_running_loop().time()
    timed_out = False
    emitted_calls: dict[str, tuple] = {}
    last_call_poll = 0.0

    def pending_calls(force: bool = False) -> list[dict]:
        nonlocal last_call_poll
        now = asyncio.get_running_loop().time()
        if not use_bridge or (not force and now - last_call_poll < 0.25):
            return []
        last_call_poll = now
        result = []
        for call in list_calls(user, run_id=research["run_id"]):
            version = (call["status"], call["completed_at"])
            if emitted_calls.get(call["call_id"]) != version:
                emitted_calls[call["call_id"]] = version
                result.append({"type": "platform_call", "call": call})
        return result

    def scrub_event(value):
        if isinstance(value, dict):
            return {k: scrub_event(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub_event(v) for v in value]
        if isinstance(value, str):
            for credential in (bridge_token, model_cfg.get("api_key")):
                if credential:
                    value = value.replace(credential, "[redacted]")
        return value

    try:
        while True:
            for call_event in pending_calls():
                yield call_event
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=heartbeat_seconds)
            except TimeoutError:
                idle_for = asyncio.get_running_loop().time() - last_activity
                if idle_for >= idle_timeout_seconds:
                    timed_out = True
                    log.warning(
                        "Claude stream idle timeout conv=%s pid=%s idle=%.1fs",
                        conv_id,
                        proc.pid,
                        idle_for,
                    )
                    timeout_min = max(1, round(idle_timeout_seconds / 60))
                    yield {
                        "type": "error",
                        "message": (
                            f"生成超过 {timeout_min} 分钟没有新内容，已自动停止。"
                            "可以缩小问题范围、换 Fast 模型，或点击重生成再试。"
                        ),
                        "idle_timeout": True,
                    }
                    break
                yield {"type": "heartbeat", "idle_ms": int(idle_for * 1000)}
                continue
            if not chunk:
                break
            last_activity = asyncio.get_running_loop().time()
            buf += decoder.decode(chunk, final=False)
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    uid = ev.get("uuid", "")
                    if uid and uid in seen:
                        continue
                    if uid:
                        seen.add(uid)
                        if len(seen) > 1000:
                            seen.clear()
                    yield scrub_event(ev)
                except json.JSONDecodeError:
                    pass
        if not timed_out:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1)
            if id(proc) in _cancelled_procs:
                yield {"type": "cancelled", "message": "用户已取消本轮研究"}
            elif proc.returncode not in (None, 0):
                err_text = ""
                try:
                    err_raw = await asyncio.wait_for(proc.stderr.read(4096), timeout=0.5)
                    err_text = err_raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    err_text = ""
                # Third-party stderr may echo configuration; do not relay it.
                log.warning("Claude CLI exited code=%s stderr_present=%s", proc.returncode, bool(err_text))
                yield {
                    "type": "error",
                    "message": f"研究进程未完成（退出码 {proc.returncode}），请检查模型与研究引擎配置",
                }
        for call_event in pending_calls(force=True):
            yield call_event
    finally:
        _cancelled_procs.discard(id(proc))
        if use_bridge:
            revoke_run_token(research["run_id"])
        if conv_id and _active_procs.get(conv_id) is proc:
            _active_procs.pop(conv_id, None)
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()


async def respond_to_question(conv_id: str, answer: str) -> bool:
    """Write an answer to the running Claude process's stdin.

    Used for AskUserQuestion responses.
    Returns True if the process was found and written to.
    """
    proc = _active_procs.get(conv_id)
    if not proc or proc.stdin is None:
        log.warning(f"No active process for conv={conv_id}")
        return False
    try:
        proc.stdin.write((answer + "\n").encode())
        await proc.stdin.drain()
        log.info("Wrote answer to conv=%s", conv_id)
        return True
    except Exception:
        log.error("Failed to write answer to conv=%s", conv_id)
        return False


async def cancel_process(conv_id: str) -> bool:
    """Cancel (kill) a running Claude process for a conversation.

    Returns True if a process was found and killed.
    """
    proc = _active_procs.get(conv_id)
    if not proc:
        return False
    _cancelled_procs.add(id(proc))
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    if _active_procs.get(conv_id) is proc:
        _active_procs.pop(conv_id, None)
    log.info(f"Cancelled Claude process for conv={conv_id}")
    return True
