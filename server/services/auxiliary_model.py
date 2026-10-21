"""Small, stateless model tasks with no research, filesystem or MCP tools."""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from .. import config

MAX_OUTPUT_BYTES = 256_000


class AuxiliaryModelUnavailable(RuntimeError):
    """A safe failure that never embeds prompts, credentials or child stderr."""


def _invocation(prompt: str, directory: str, output_format: str, model_id: str | None):
    model = config.MODELS.get(model_id or config.DEFAULT_MODEL)
    if not model or not model.get("api_key") or not model.get("model") or not model.get("base_url"):
        raise AuxiliaryModelUnavailable("辅助模型尚未配置")
    if output_format not in {"text", "json"}:
        raise ValueError("unsupported output format")
    # No user/project settings, saved sessions, hooks, extra MCP servers or
    # inherited provider/capability credentials. CLI support is pinned at release.
    cmd = [
        config.CLAUDE_BIN,
        "-p",
        prompt,
        "--model",
        model["model"],
        "--output-format",
        output_format,
        "--max-turns",
        "1",
        "--tools",
        "",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disallowedTools",
        "mcp__*",
        "--setting-sources",
        "",
        "--no-session-persistence",
    ]
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "SystemRoot",
        }
    }
    env.update(
        {
            "HOME": directory,
            "TMPDIR": directory,
            "TMP": directory,
            "TEMP": directory,
            "NO_COLOR": "1",
            "ANTHROPIC_AUTH_TOKEN": model["api_key"],
            "ANTHROPIC_BASE_URL": model["base_url"],
        }
    )
    return cmd, env


def _output(raw: bytes, returncode: int, credential: str) -> str:
    if returncode != 0 or len(raw) > MAX_OUTPUT_BYTES or credential.encode() in raw:
        raise AuxiliaryModelUnavailable("辅助模型未完成请求")
    return raw.decode("utf-8", errors="replace").strip()


def run_text(
    prompt: str, *, timeout: float = 40, output_format: str = "text", model_id: str | None = None
) -> str:
    with tempfile.TemporaryDirectory(prefix="prism-model-") as directory:
        Path(directory).chmod(0o700)
        cmd, env = _invocation(prompt, directory, output_format, model_id)
        try:
            proc = subprocess.run(
                cmd,
                cwd=directory,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise AuxiliaryModelUnavailable("辅助模型暂不可用") from None
        return _output(proc.stdout, proc.returncode, env["ANTHROPIC_AUTH_TOKEN"])


async def run_text_async(
    prompt: str, *, timeout: float = 15, output_format: str = "text", model_id: str | None = None
) -> str:
    with tempfile.TemporaryDirectory(prefix="prism-model-") as directory:
        Path(directory).chmod(0o700)
        cmd, env = _invocation(prompt, directory, output_format, model_id)
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=directory,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return _output(stdout, proc.returncode, env["ANTHROPIC_AUTH_TOKEN"])
        except (OSError, TimeoutError):
            raise AuxiliaryModelUnavailable("辅助模型暂不可用") from None
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
