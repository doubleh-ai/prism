"""_read_jsonl_messages uses aiofiles (non-blocking on event loop)."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_read_jsonl_is_async():
    """_read_jsonl_messages is `async def` (proves it uses aiofiles)."""
    from server import chat
    import inspect
    assert inspect.iscoroutinefunction(chat._read_jsonl_messages), \
        "_read_jsonl_messages must be async to use aiofiles"


def test_read_jsonl_parses_messages(tmp_path, monkeypatch):
    """Async reader parses user + assistant events from a jsonl file."""
    from server import chat
    # Build a jsonl with a user + assistant event
    p = tmp_path / "fake.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "text", "text": "hello back"},
                {"type": "tool_use", "name": "WebSearch", "input": {"q": "x"}},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    # Patch CLAUDE_PROJECTS so the reader looks in tmp_path
    monkeypatch.setattr(chat, "CLAUDE_PROJECTS", tmp_path)

    msgs = asyncio.run(chat._read_jsonl_messages("fake", "."))
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert "hi" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    # Content is JSON; parse and check chunks
    parsed = json.loads(msgs[1]["content"])
    assert any(c["t"] == "text" and "hello back" in c["c"] for c in parsed["chunks"])
    assert any(c["t"] == "tool" and c["c"] == "WebSearch" for c in parsed["chunks"])


def test_async_reader_strips_object_artifacts(tmp_path, monkeypatch):
    """The async chat saver should persist clean chunks, not JS object reprs."""
    from server import chat
    p = tmp_path / "artifact.jsonl"
    p.write_text(
        json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n"
        + json.dumps({
            "type": "assistant",
            "message": {"content": [
                {"type": "thinking", "thinking": "[object Object]\nreal thought"},
                {"type": "text", "text": "clean text [object Object]"},
            ]},
        }) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(chat, "CLAUDE_PROJECTS", tmp_path)

    msgs = asyncio.run(chat._read_jsonl_messages("artifact", "."))
    raw = json.dumps(msgs, ensure_ascii=False)
    assert "[object Object]" not in raw
    assert "real thought" in raw
    assert "clean text" in raw
