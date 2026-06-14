"""Claude Code CLI — interactive subprocess with resume + stdin support"""
import os
import json
import asyncio
import logging
import codecs
from pathlib import Path
from typing import AsyncGenerator, Optional

from ..config import CLAUDE_BIN, SKILLS_HOME, SKILL_MAP, MODELS, DEFAULT_MODEL
from ..proxy import get_proxy_config

log = logging.getLogger("stock-app")

# Per-conversation running processes: {conv_id: process}
_active_procs: dict = {}


async def run_claude_stream(
    question: str,
    skill_name: str = "bottleneck-hunter",
    user: str = "anon",
    model_cfg: Optional[dict] = None,
    session_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    extra_skills: Optional[list] = None,
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
    cwd = SKILLS_HOME / info["dir"]

    if not cwd.exists():
        yield {"type": "error", "message": f"Skill目录不存在: {cwd}"}
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
        es_dir = SKILLS_HOME / es
        es_md = es_dir / "CLAUDE.md"
        if es_md.exists():
            try:
                append_prompts.append(es_md.read_text(encoding="utf-8"))
            except Exception:
                pass

    # Build command — clean question, system instructions via --append-system-prompt
    cmd = [
        CLAUDE_BIN,
        "-p", question,
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--model", model_cfg["model"],
    ]
    for ap in append_prompts:
        cmd.extend(["--append-system-prompt", ap])
    if session_id:
        cmd.extend(["--resume", session_id])

    # Dynamic proxy config
    proxy_config = await get_proxy_config()
    env = {
        **os.environ,
        "HOME": str(Path.home()),
        "NO_COLOR": "1",
        "ANTHROPIC_BASE_URL": model_cfg.get("base_url", ""),
        "ANTHROPIC_AUTH_TOKEN": model_cfg.get("api_key", ""),
        # 抑制 SSL 警告（实际 SSL 处理由 patch_ssl.py auto-fallback 负责）
        "PYTHONWARNINGS": "ignore::urllib3.exceptions.InsecureRequestWarning",
    }
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
    try:
        while True:
            chunk = await proc.stdout.read(256)
            if not chunk:
                break
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
    finally:
        if conv_id:
            _active_procs.pop(conv_id, None)
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


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
        try:
            proc.kill()
        except Exception:
            pass
    _active_procs.pop(conv_id, None)
    log.info(f"Cancelled Claude process for conv={conv_id}")
    return True
