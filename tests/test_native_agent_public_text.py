"""Public-answer projection uses only in-memory synthetic legacy structures."""

import json

import pytest

from server.agent.public_text import MAX_PUBLIC_CHARACTERS, public_answer_text


@pytest.mark.parametrize(
    "encode", [lambda value: value, json.dumps, lambda value: json.dumps(json.dumps(value))]
)
def test_private_blocks_do_not_become_public_in_direct_or_encoded_history(encode):
    value = [
        {
            "type": "thinking",
            "thinking": "private-reason",
            "text": "private-text",
            "summary": "private-summary",
        },
        {"type": "reasoning", "content": "private-reasoning-content"},
        {"type": "redacted_thinking", "data": "private-opaque"},
        {"type": "opaque_vendor_block", "text": "private-extension"},
        {"type": "text", "text": "public answer", "reasoning_content": "private-extra"},
    ]
    assert public_answer_text(encode(value)) == "public answer"


def test_pretty_printed_json_and_nested_business_records_preserve_only_answer():
    blocks = [{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "public"}]
    assert public_answer_text(json.dumps(blocks, indent=2)) == "public"
    assert public_answer_text({"final_decision": json.dumps(blocks), "thinking": "private"}) == "public"
    assert public_answer_text({"narrative": {"content": blocks}, "signature": "private"}) == "public"
    assert public_answer_text(json.dumps([None, 1, *blocks])) == "public"


def test_canonical_chat_text_does_not_repeat_chunks_or_expose_internal_fields():
    value = {
        "text": "public",
        "chunks": [{"t": "text", "c": "public"}, {"t": "think", "c": "private"}],
        "tool_calls": {"text": "private"},
        "signature": "private",
    }
    assert public_answer_text(value) == "public"
    assert (
        public_answer_text({"chunks": [{"t": "think", "c": "private"}, {"t": "text", "c": "public"}]})
        == "public"
    )


@pytest.mark.parametrize(
    "value",
    [
        {"type": "thinking", "text": "private"},
        {"TYPE": "thinking", "text": "private"},
        {"type": "text", "t": "thinking", "text": "private"},
        {"role": "system", "content": "private"},
        {"ROLE": "developer", "content": "private"},
        {"type": "tool_result", "content": "private"},
        {"metadata": {"text": "private"}},
        {"reasoning_content": "private"},
        '{"type":"thinking","text":"private"',
        '[\n {"type":"thinking","text":"private"}',
        '{"type":"thinking","type":"text","text":"private"}',
    ],
)
def test_unknown_internal_ambiguous_or_malformed_wrappers_fail_closed(value):
    assert public_answer_text(value) == ""


def test_over_encoded_private_or_public_json_is_not_released_as_literal_text():
    raw = [{"type": "thinking", "text": "private"}, {"type": "text", "text": "public"}]
    encoded = json.dumps(json.dumps(json.dumps(raw)))
    assert public_answer_text(encoded) == ""


def test_plain_markdown_remains_usable_and_known_private_tags_are_removed():
    text = "### 分析\n[资料](https://example.com/report)\n- 公开结论"
    assert public_answer_text(text) == text
    assert public_answer_text("[资料](https://example.com/report)") == "[资料](https://example.com/report)"
    assert public_answer_text("<think>private</think>public") == "public"
    assert public_answer_text("<reasoning>private</reasoning>public") == "public"
    assert public_answer_text("public\n<think>unterminated private") == "public"


def test_credentials_in_public_text_are_redacted_before_output_truncation():
    text = (
        "DEEPSEEK_API_KEY=synthetic-secret password='synthetic-password' "
        "Bearer synthetic-bearer sk-abcdefghijklmnopqrstuv "
        "https://example.com/?token=synthetic-link\n"
        "-----BEGIN PRIVATE KEY-----\nsynthetic-pem\n-----END PRIVATE KEY-----\npublic"
    )
    result = public_answer_text({"text": text})
    for marker in (
        "synthetic-secret",
        "synthetic-password",
        "synthetic-bearer",
        "sk-abcdefghijklmnopqrstuv",
        "synthetic-link",
        "synthetic-pem",
    ):
        assert marker not in result
    assert "public" in result


def test_recursion_cycles_node_count_and_total_output_are_bounded():
    cycle = {}
    cycle["content"] = cycle
    assert public_answer_text(cycle) == ""
    many = [{"type": "text", "text": "x" * 1000}] * 10000
    result = public_answer_text(many)
    assert len(result) <= MAX_PUBLIC_CHARACTERS
    assert public_answer_text("x" * 400001) == ""
    assert public_answer_text("\ud800") == ""
    assert public_answer_text({str(i): "private" for i in range(101)}) == ""


def test_business_answer_fields_remain_available_without_unknown_fields():
    value = {
        "question": "question",
        "thesis": "thesis",
        "narrative": "narrative",
        "final_decision": "answer",
        "_private": {"text": "private"},
        "reasoning": "private",
    }
    assert public_answer_text(value) == "question\nthesis\nnarrative\nanswer"
