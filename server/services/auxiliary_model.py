"""Bounded stateless model tasks through the native direct Messages transport.

No tools, subprocesses, sessions, skills, retries or alternate execution backend.
``json`` returns a single JSON object, without a CLI envelope or Markdown fence.
"""

from __future__ import annotations

import asyncio
import json
import math
from contextlib import aclosing, suppress

from .. import config
from ..agent import provider
from ..work_lifecycle import get_manager

MAX_OUTPUT_BYTES = 256_000
MAX_TOKENS = 16_384


class AuxiliaryModelUnavailable(RuntimeError):
    """A safe failure that never embeds prompts, credentials or upstream errors."""


def _model(model_id):
    if model_id is not None and not isinstance(model_id, str):
        raise AuxiliaryModelUnavailable("辅助模型尚未配置")
    model = config.MODELS.get(model_id or config.DEFAULT_MODEL)
    if not isinstance(model, dict) or not all(
        isinstance(model.get(key), str) and model[key] for key in ("api_key", "model", "base_url")
    ):
        raise AuxiliaryModelUnavailable("辅助模型尚未配置")
    return dict(model)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON member")
        value[key] = item
    return value


def _invalid_constant(_):
    raise ValueError("nonfinite JSON number")


def _output(text, credential, output_format):
    if (
        not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > MAX_OUTPUT_BYTES
        or credential in text
    ):
        raise AuxiliaryModelUnavailable("辅助模型未完成请求")
    text = text.strip()
    if output_format == "json":
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
        normalized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if credential in normalized:
            raise AuxiliaryModelUnavailable("辅助模型未完成请求")
        if not isinstance(value, dict):
            raise AuxiliaryModelUnavailable("辅助模型未完成请求")
    return text


async def _consume(prompt, model, output_format, timeout):
    pieces, byte_count = [], 0
    system = "Return only the requested final text. Do not call tools."
    if output_format == "json":
        system += " Return exactly one valid JSON object, without Markdown fences or surrounding prose."
    stream = provider.stream(
        [{"role": "user", "content": prompt}],
        system=system,
        tools=[],
        model_cfg=model,
        max_tokens=MAX_TOKENS,
        timeout_seconds=timeout,
    )
    async with aclosing(stream):
        async for event in stream:
            if not isinstance(event, dict):
                raise AuxiliaryModelUnavailable("辅助模型未完成请求")
            kind = event.get("type")
            if kind == "text_delta":
                text = event.get("text")
                if not isinstance(text, str):
                    raise AuxiliaryModelUnavailable("辅助模型未完成请求")
                byte_count += len(text.encode("utf-8"))
                if byte_count > MAX_OUTPUT_BYTES:
                    raise AuxiliaryModelUnavailable("辅助模型未完成请求")
                pieces.append(text)
            elif kind == "done":
                content = event.get("content")
                if (
                    event.get("stop_reason") != "end_turn"
                    or not isinstance(content, list)
                    or any(not isinstance(b, dict) or b.get("type") != "text" for b in content)
                    or any(not isinstance(b.get("text"), str) for b in content)
                ):
                    raise AuxiliaryModelUnavailable("辅助模型未完成请求")
                final = "".join(block["text"] for block in content)
                if final != "".join(pieces):
                    raise AuxiliaryModelUnavailable("辅助模型未完成请求")
                return _output(final, model["api_key"], output_format)
            elif kind != "usage":
                # Includes local/server tool calls and server-side tool results.
                raise AuxiliaryModelUnavailable("辅助模型未完成请求")
    raise AuxiliaryModelUnavailable("辅助模型未完成请求")


async def _owned_request(lease, *args):
    with lease:
        return await _consume(*args)


async def _cancel_and_close(task):
    # Cancel the request once. Further caller cancellations cannot interrupt its
    # transport cleanup or publish an empty work census while HTTP is still open.
    task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    with suppress(BaseException):
        task.result()


def run_text(
    prompt: str, *, timeout: float = 40, output_format: str = "text", model_id: str | None = None
) -> str:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise AuxiliaryModelUnavailable("同步辅助模型不能在事件循环中调用")
    return asyncio.run(
        run_text_async(prompt, timeout=timeout, output_format=output_format, model_id=model_id)
    )


async def run_text_async(
    prompt: str, *, timeout: float = 15, output_format: str = "text", model_id: str | None = None
) -> str:
    if output_format not in {"text", "json"}:
        raise ValueError("unsupported output format")
    task = lease = None
    try:
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 < timeout <= 600
        ):
            raise AuxiliaryModelUnavailable("辅助模型暂不可用")
        model = _model(model_id)
        lease = get_manager().admit("auxiliary", persist_required=False)
        task = asyncio.create_task(_owned_request(lease, prompt, model, output_format, timeout))
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except asyncio.CancelledError:
        if task is not None:
            await _cancel_and_close(task)
        raise
    except Exception:
        if task is not None and not task.done():
            await _cancel_and_close(task)
        raise AuxiliaryModelUnavailable("辅助模型暂不可用") from None
    finally:
        if lease is not None:
            # Also handles cancellation before the owned task first runs.
            lease.release()
