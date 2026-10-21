"""Claude Code CLI — interactive subprocess with resume + stdin support"""

import asyncio
import codecs
import contextlib
import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from ..config import (
    CHAT_STREAM_HEARTBEAT_SECONDS,
    CHAT_STREAM_IDLE_TIMEOUT_SECONDS,
    CLAUDE_BIN,
    DEFAULT_MODEL,
    MODELS,
    SKILL_FALLBACK_ROOTS,
    SKILL_MAP,
    SKILLS_HOME,
)
from ..proxy import get_proxy_config

log = logging.getLogger("stock-app")

# Per-conversation running processes: {conv_id: process}
_active_procs: dict = {}


def _skill_dir_name(skill_id_or_dir: str) -> str:
    """Return the on-disk directory name for a configured skill id."""
    return SKILL_MAP.get(skill_id_or_dir, {}).get("dir", skill_id_or_dir)


def _candidate_skill_dirs(dir_name: str) -> list[Path]:
    roots = [SKILLS_HOME, *SKILL_FALLBACK_ROOTS]
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        path = Path(root) / dir_name
        key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)
    return candidates


def _resolve_skill_dir(dir_name: str) -> tuple[Path | None, list[Path]]:
    """Resolve a skill directory, tolerating stale ~/.claude symlinks."""
    candidates = _candidate_skill_dirs(dir_name)
    for path in candidates:
        if path.exists() and path.is_dir():
            return path, candidates
    return None, candidates


async def run_claude_stream(
    question: str,
    skill_name: str = "bottleneck-hunter",
    user: str = "anon",
    model_cfg: dict | None = None,
    session_id: str | None = None,
    conv_id: str | None = None,
    extra_skills: list | None = None,
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

    Yields:
        Parsed JSON events from Claude's stdout
    """
    info = SKILL_MAP.get(skill_name, SKILL_MAP["bottleneck-hunter"])
    skill_dir_name = info["dir"]
    cwd, checked_paths = _resolve_skill_dir(skill_dir_name)

    if cwd is None:
        checked = ", ".join(str(path) for path in checked_paths)
        yield {"type": "error", "message": f"Skill目录不存在: {skill_dir_name}; 已检查: {checked}"}
        return

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
        "plan, tool output, or file-writing step. HTML/PDF artifacts are optional: "
        "create them only after the Markdown report, or when the user explicitly "
        "requests that format. If a source fails, disclose the gap and continue "
        "with verified evidence instead of repeatedly retrying the same source."
    )
    append_prompts.append(REPORT_FIRST_PROMPT)

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
    ]
    for ap in append_prompts:
        cmd.extend(["--append-system-prompt", ap])
    if session_id:
        cmd.extend(["--resume", session_id])

    # Dynamic proxy config
    proxy_config = await get_proxy_config()
    # Build env: never inherit API key/sensitive vars from parent unless
    # explicitly allowed. The Claude CLI subprocess should only see the
    # credentials we deliberately pass to it — not anything else sitting
    # in os.environ.
    sensitive_keys = {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "MINIMAX_API_KEY",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }
    env = {k: v for k, v in os.environ.items() if k not in sensitive_keys}
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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE,  # Open stdin for AskUserQuestion answers
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
    )
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
    try:
        while True:
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
                    yield ev
                except json.JSONDecodeError:
                    pass
        if not timed_out:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1)
            if proc.returncode not in (None, 0):
                err_text = ""
                try:
                    err_raw = await asyncio.wait_for(proc.stderr.read(4096), timeout=0.5)
                    err_text = err_raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    err_text = ""
                msg = err_text or f"Claude CLI exited with code {proc.returncode}"
                yield {"type": "error", "message": msg[:1000]}
    finally:
        if conv_id:
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
        log.info(f"Wrote answer to conv={conv_id}: {answer[:40]}...")
        return True
    except Exception as e:
        log.error(f"Failed to write to conv={conv_id}: {e}")
        return False


async def cancel_process(conv_id: str) -> bool:
    """Cancel (kill) a running Claude process for a conversation.

    Returns True if a process was found and killed.
    """
    proc = _active_procs.get(conv_id)
    if not proc:
        return False
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=3)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()
    _active_procs.pop(conv_id, None)
    log.info(f"Cancelled Claude process for conv={conv_id}")
    return True
