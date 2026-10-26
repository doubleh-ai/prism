"""Pure historical-format parsing and offline public projection.

Historical project-directory discovery and application runtime file readers were
removed. Only the explicit staged importer may read these old data files.
"""

import json

import pytest

from server.legacy_history import _public_prefix
from server.research_context import parse_claude_jsonl_events


def user(content):
    return {"type": "user", "message": {"role": "user", "content": content}}


def answer(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_canonical_parser_retains_provenance_before_public_projection():
    events = [
        user("测试问题？"),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private provider reasoning"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "private tool command"}},
                    {"type": "text", "text": "这是一个测试回答。"},
                ]
            },
        },
    ]
    parsed = parse_claude_jsonl_events(events, session_id="synthetic-offline")
    assert len(parsed) == 2
    assert parsed[0] == {"role": "user", "content": "测试问题？"}
    payload = json.loads(parsed[1]["content"])
    assert payload["text"] == "这是一个测试回答。"
    assert {chunk["t"] for chunk in payload["chunks"]} == {"text", "tool", "think"}
    public = _public_prefix(events, "synthetic-offline")
    assert public == [user_message("测试问题？"), assistant_message("这是一个测试回答。")]
    assert "private" not in json.dumps(public)


def user_message(content):
    return {"role": "user", "content": content}


def assistant_message(content):
    return {"role": "assistant", "content": content}


def test_offline_projection_strips_legacy_object_artifacts_and_private_blocks():
    events = [
        user("question"),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "[object Object] private reasoning"},
                    {
                        "type": "tool_use",
                        "name": "Bash",
                        "input": {"command": "[object Object] private command"},
                    },
                    {"type": "text", "text": "answer [object Object] done"},
                ]
            },
        },
    ]
    messages = _public_prefix(events, "artifact-session")
    rendered = json.dumps(messages)
    assert "[object Object]" not in rendered and "private" not in rendered
    assert messages[-1]["content"] == "answer  done"


def test_empty_historical_events_have_empty_public_prefix():
    assert parse_claude_jsonl_events([], session_id="missing-session") == []
    assert _public_prefix([], "missing-session") == []


@pytest.mark.parametrize(
    "tool_wrapper",
    [
        "[{'type': 'tool_result', 'content': 'private output'}]",
        "[{'type': 'text', 'text': 'private skill content'}]",
        "[{'tool_use_id': 'call_123', 'type': 'tool_result', 'content': 'private output'}]",
    ],
)
def test_serialized_tool_wrappers_cannot_start_a_user_turn(tool_wrapper):
    events = [user("真实问题"), user(tool_wrapper), answer("回答内容")]
    parsed = parse_claude_jsonl_events(events, session_id="tool-filter")
    assert [item for item in parsed if item["role"] == "user"] == [user_message("真实问题")]
    assert _public_prefix(events, "tool-filter") == [user_message("真实问题"), assistant_message("回答内容")]


def test_structured_tool_result_cannot_create_user_turn():
    events = [
        user("实际问题"),
        user([{"type": "tool_result", "tool_use_id": "call_123", "content": "private output"}]),
        answer("公开回答"),
    ]
    parsed = parse_claude_jsonl_events(events, session_id="structured-tool")
    assert len([item for item in parsed if item["role"] == "user"]) == 1
    assert _public_prefix(events, "structured-tool") == [
        user_message("实际问题"),
        assistant_message("公开回答"),
    ]


def test_dict_messages_and_multiple_turn_boundaries_are_preserved():
    events = [user("dict格式问题"), answer("dict格式回答"), user("追问"), answer("追问回答")]
    messages = _public_prefix(events, "dict-format")
    assert messages == [
        user_message("dict格式问题"),
        assistant_message("dict格式回答"),
        user_message("追问"),
        assistant_message("追问回答"),
    ]


def test_credential_text_is_redacted_in_both_roles():
    messages = _public_prefix(
        [user("api_key=private-user-key 问题"), answer("password=private-password 回答")], "credentials"
    )
    assert "private-user-key" not in json.dumps(messages)
    assert "private-password" not in json.dumps(messages)
    assert "问题" in messages[0]["content"] and "回答" in messages[1]["content"]
