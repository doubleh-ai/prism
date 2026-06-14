"""Test Claude Code JSONL reading and parsing"""
import json
import pytest
from pathlib import Path


class TestJSONLReading:
    """Test _read_jsonl_messages and _read_jsonl_user_content"""

    def test_read_user_content(self, sample_jsonl):
        """Extract first user message for title."""
        sid, path = sample_jsonl
        from server.conversations import _read_jsonl_user_content
        content = _read_jsonl_user_content(path)
        assert content == "测试问题？"

    def test_read_jsonl_messages(self, sample_jsonl):
        """Read full messages from JSONL."""
        sid, path = sample_jsonl
        from server.conversations import _read_jsonl_messages
        messages = _read_jsonl_messages(sid, str(path.parent))

        assert len(messages) >= 1
        # User message
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) >= 1
        assert user_msgs[0]["content"] == "测试问题？"

        # Assistant message
        asst_msgs = [m for m in messages if m["role"] == "assistant"]
        assert len(asst_msgs) >= 1
        content = asst_msgs[0]["content"]
        assert "测试回答" in content
        # Verify chunks
        payload = json.loads(content)
        assert "chunks" in payload
        assert "text" in payload
        chunks = payload["chunks"]
        types = {c["t"] for c in chunks}
        assert "tool" in types
        assert "think" in types
        assert "text" in types

    def test_read_missing_jsonl(self):
        """Non-existent JSONL returns empty list."""
        from server.conversations import _read_jsonl_messages
        msgs = _read_jsonl_messages("nonexistent", "nonexistent")
        assert msgs == []

    def test_user_content_missing_jsonl(self):
        """Non-existent JSONL returns None."""
        from server.conversations import _read_jsonl_user_content
        c = _read_jsonl_user_content(Path("/nonexistent/file.jsonl"))
        assert c is None


class TestCwdToProjectDir:
    """Test cwd → project_dir conversion"""

    def test_standard_path(self):
        from server.chat import _cwd_to_project_dir
        result = _cwd_to_project_dir("/home/ubuntu/.claude/skills/serenity-bottleneck-hunter")
        # Hidden dirs: .claude becomes -claude → produces --claude
        assert result == "-home-ubuntu--claude-skills-serenity-bottleneck-hunter"

    def test_simple_path(self):
        from server.chat import _cwd_to_project_dir
        result = _cwd_to_project_dir("/tmp")
        assert result == "-tmp"

    def test_no_trailing_slash(self):
        from server.chat import _cwd_to_project_dir
        result = _cwd_to_project_dir("/home/user/project")
        assert result == "-home-user-project"

    def test_hidden_dir_conversion(self):
        from server.chat import _cwd_to_project_dir
        # .config should become -config
        result = _cwd_to_project_dir("/home/user/.config/app")
        assert result == "-home-user--config-app"


class TestJSONLMessageFiltering:
    """Test that tool_result messages are filtered out"""

    def test_filters_tool_results(self, tmp_path):
        """User messages starting with [{'type': should be skipped."""
        sid = "test-sid"
        proj_dir = str(tmp_path)
        jsonl = tmp_path / f"{sid}.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "真实问题"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "[{'type': 'tool_result', 'content': 'ls output...'}]"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "[{'type': 'text', 'text': 'skill content...'}]"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "回答内容"}
            ]}}),
        ]
        jsonl.write_text("\n".join(lines), encoding="utf-8")

        from server.conversations import _read_jsonl_messages
        messages = _read_jsonl_messages(sid, proj_dir)

        user_msgs = [m for m in messages if m["role"] == "user"]
        # Only the first "真实问题" should be included
        assert len(user_msgs) == 1
        assert user_msgs[0]["content"] == "真实问题"

    def test_handles_dict_message(self, tmp_path):
        """Handle user messages that are already dicts."""
        sid = "test-sid2"
        jsonl = tmp_path / f"{sid}.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "dict格式问题"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "dict格式回答"}
            ]}}),
        ]
        jsonl.write_text("\n".join(lines), encoding="utf-8")

        from server.conversations import _read_jsonl_messages
        messages = _read_jsonl_messages(sid, str(tmp_path))
        assert len(messages) == 2
        assert messages[0]["content"] == "dict格式问题"

    def test_filters_tool_use_id_results(self, tmp_path):
        """User messages starting with [{'tool_use_id': must be filtered out."""
        sid = "test-sid-tooluse"
        jsonl = tmp_path / f"{sid}.jsonl"
        lines = [
            json.dumps({"type": "user", "message": {"role": "user", "content": "实际问题"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": "[{'tool_use_id': 'call_123', 'type': 'tool_result', 'content': 'output...'}]"}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "回答"}
            ]}}),
        ]
        jsonl.write_text("\n".join(lines), encoding="utf-8")

        from server.conversations import _read_jsonl_messages
        messages = _read_jsonl_messages(sid, str(tmp_path))
        user_msgs = [m for m in messages if m["role"] == "user"]
        assert len(user_msgs) == 1  # Only the real question
        assert user_msgs[0]["content"] == "实际问题"

