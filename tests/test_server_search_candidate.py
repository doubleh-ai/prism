"""Apply a dormant patch ONLY to temp copies; synthetic official-schema fixtures.

These tests do not claim that fixed CLI 2.1.177 emits these DeepSeek frames.
No provider requests, user history, credentials or fetched article text are used.
"""

import copy
import hashlib
import json
import subprocess
import types
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "scripts/patches/deepseek-server-search-candidate.patch"
FIXTURE = ROOT / "scripts/fixtures/deepseek_server_search_synthetic.json"


@pytest.fixture
def candidate(tmp_path):
    paths = ("server/research_context.py", "server/chat.py")
    originals = {path: (ROOT / path).read_bytes() for path in paths}
    for path, content in originals.items():
        (tmp_path / path).parent.mkdir(exist_ok=True)
        (tmp_path / path).write_bytes(content)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }
    for option in ["--check", None]:
        argv = ["/usr/bin/git", "apply"] + ([option] if option else []) + [str(PATCH)]
        subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, check=True)
    modules = {}
    for path in paths:
        module = types.ModuleType("server._dormant_" + Path(path).stem)
        module.__file__ = str(tmp_path / path)
        module.__package__ = "server"
        exec(compile((tmp_path / path).read_bytes(), module.__file__, "exec"), module.__dict__)
        modules[Path(path).stem] = module
    modules["chat"].EvidenceTracker = modules["research_context"].EvidenceTracker
    yield modules
    assert {path: (ROOT / path).read_bytes() for path in paths} == originals


@pytest.fixture
def frames():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["authenticity"] == "synthetic_official_schema_not_captured_deepseek"
    return fixture


def tracker(candidate):
    return candidate["research_context"].EvidenceTracker("synthetic-run")


def test_documented_server_search_records_metadata_without_mutating_provider_blocks(candidate, frames):
    evidence = tracker(candidate)
    before = copy.deepcopy(frames)
    evidence.record_server_call(frames["call"])
    call, block, refs = evidence.record_server_result(
        frames["result"], retrieved_at="2026-10-22T01:00:00+08:00"
    )
    assert call["name"] == "WebSearch" and block["tool_use_id"] == call["id"]
    assert frames == before  # Ciphertext remains intact for upstream continuation.
    assert len(refs) == 1 and refs[0]["url"] == frames["result"]["content"][0]["url"]
    assert refs[0]["snippet"] == "" and refs[0]["published_at"] is None
    assert refs[0]["content_scope"] == "search_metadata_only"
    assert refs[0]["provider_transport"] == "server_tool_use"
    assert refs[0]["retrieved_at"] == "2026-10-22T01:00:00+08:00"
    assert "encrypted_content" not in json.dumps([block, refs])
    assert "page_age" not in json.dumps([block, refs])
    assert "SYNTHETIC_OPAQUE" not in json.dumps([block, refs])
    assert evidence.record_server_result(frames["result"]) is None


@pytest.mark.parametrize(
    "bad_content",
    [
        None,
        [],
        "markdown https://example.test/fake",
        [{"type": "text", "text": "https://example.test/fake"}],
        [{"type": "web_search_result"}, {"type": "web_search_tool_result_error"}],
    ],
)
def test_malformed_or_empty_success_cannot_create_evidence(candidate, frames, bad_content):
    evidence = tracker(candidate)
    evidence.record_server_call(frames["call"])
    assert evidence.record_server_result({**frames["result"], "content": bad_content}) is None
    assert evidence.record_server_result(frames["result"]) is None  # The failed call is consumed.


def test_http_200_error_or_orphan_result_cannot_create_evidence(candidate, frames):
    evidence = tracker(candidate)
    assert evidence.record_server_result(frames["result"]) is None
    evidence.record_server_call(frames["call"])
    assert evidence.record_server_result(frames["http_200_error_result"]) is None
    assert evidence.record_server_result(frames["result"]) is None


@pytest.mark.parametrize(
    "change",
    [
        {"name": "WebSearch"},
        {"name": "web_fetch"},
        {"type": "tool_use"},
        {"id": ""},
        {"id": "x" * 201},
        {"input": {"query": 1}},
    ],
)
def test_only_exact_documented_server_search_calls_are_accepted(candidate, frames, change):
    evidence = tracker(candidate)
    evidence.record_server_call({**frames["call"], **change})
    assert evidence.record_server_result(frames["result"]) is None


def test_tool_id_collision_cannot_turn_metadata_into_fetched_article(candidate, frames):
    evidence = tracker(candidate)
    evidence.record_server_call(frames["call"])
    evidence.record_call(
        {"id": frames["call"]["id"], "name": "WebFetch", "input": {"url": "https://example.test/fake"}}
    )
    assert evidence.record_server_result(frames["result"]) is None
    before = tracker(candidate)
    before.record_call({"id": frames["call"]["id"], "name": "WebSearch", "input": {}})
    before.record_server_call(frames["call"])
    assert before.record_server_result(frames["result"]) is None


def test_only_structured_safe_urls_count_not_links_in_titles_or_ciphertext(candidate, frames):
    evidence = tracker(candidate)
    evidence.record_server_call(frames["call"])
    row = frames["result"]["content"][0]
    rows = [
        {
            **row,
            "title": "[invented](https://example.test/injected)",
            "encrypted_content": "[hidden](https://example.test/cipher)",
        },
        {**row, "url": "https://example.test/leak?api_key=synthetic"},
        {**row, "url": "javascript:alert(1)"},
        row,
    ]
    _, _, refs = evidence.record_server_result({**frames["result"], "content": rows})
    assert [ref["url"] for ref in refs] == [row["url"]]
    assert refs[0]["snippet"] == "" and refs[0]["published_at"] is None


def test_legacy_reader_does_not_replay_previous_turn_result_or_invent_missing_clock(candidate, frames):
    def event(kind, content, **extra):
        return {"type": kind, "message": {"content": content}, **extra}

    events = [
        event("user", "first synthetic question"),
        event("assistant", [frames["call"], frames["result"]], timestamp="2026-10-21T16:00:00Z"),
        event("user", "second synthetic question"),
        event("assistant", [frames["result"]]),
        event("assistant", [{"type": "text", "text": "[unverified](https://example.test/model-link)"}]),
    ]
    messages = candidate["research_context"].parse_claude_jsonl_events(events, session_id="synthetic")
    old = json.loads(messages[1]["content"])["chunks"]
    new = json.loads(messages[3]["content"])["chunks"]
    assert [ref["url"] for ref in old if ref["t"] == "ref"] == [frames["result"]["content"][0]["url"]]
    assert old[0]["retrieved_at"] == "2026-10-22T00:00:00+08:00"
    assert not any(ref["t"] == "ref" for ref in new)
    events[1].pop("timestamp")
    no_clock = candidate["research_context"].parse_claude_jsonl_events(events[:2], session_id="synthetic")
    assert json.loads(no_clock[1]["content"])["chunks"][0]["retrieved_at"] is None


def test_actual_ledger_stores_no_passage_or_publication_from_encrypted_search_metadata(candidate, frames):
    from server.research import create_research_run, ingest_tool_result, read_research_run

    context = candidate["research_context"].build_research_context(
        run_id="synthetic-run", now=datetime(2026, 10, 22, tzinfo=UTC)
    )
    create_research_run("synthetic-owner", "", "public synthetic probe", context)
    evidence = tracker(candidate)
    evidence.record_server_call(frames["call"])
    call, block, refs = evidence.record_server_result(frames["result"])
    saved = ingest_tool_result("synthetic-owner", "synthetic-run", call, block, refs)
    result = read_research_run("synthetic-owner", "synthetic-run")
    assert len(saved) == 1 and result["passages"] == []
    (document,) = result["documents"]
    assert document["original_obtained"] is False and document["passage_ids"] == []
    assert document["published_at"] is None and document["publication_precision"] == "unknown"
    assert document["content_sha256"] == hashlib.sha256(b"").hexdigest()
    assert "SYNTHETIC_OPAQUE" not in json.dumps(result)
    with pytest.raises(HTTPException) as denied:
        read_research_run("foreign-owner", "synthetic-run")
    assert denied.value.status_code == 404


async def test_patched_chat_sse_and_private_ledger_receive_same_synthetic_metadata(
    candidate, frames, client, auth_cookies, monkeypatch
):
    from server import main
    from server.research import read_research_run

    contexts = []

    async def synthetic_cli(*args, **kwargs):
        contexts.append(kwargs["research_context"])
        yield {
            "type": "assistant",
            "message": {
                "content": [
                    frames["call"],
                    frames["result"],
                    {"type": "text", "text": "Synthetic metadata received; original remains unverified."},
                ]
            },
        }
        yield {"type": "result", "subtype": "success", "is_error": False}

    monkeypatch.setattr(candidate["chat"], "run_claude_stream", synthetic_cli)
    monkeypatch.setattr(main, "chat_stream", candidate["chat"].chat_stream)
    response = await client.post(
        "/api/chat/stream", cookies=auth_cookies, json={"question": "public synthetic question"}
    )
    assert response.status_code == 200
    events = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    refs = [event["ref"] for event in events if "ref" in event]
    assert len(refs) == 1 and refs[0]["url"] == frames["result"]["content"][0]["url"]
    assert refs[0]["passage_ids"] == [] and refs[0]["published_at"] is None
    ledger = read_research_run("testuser", contexts[0]["run_id"])
    assert ledger["documents"][0]["document_id"] == refs[0]["document_id"]
    assert ledger["passages"] == []
    assert "SYNTHETIC_OPAQUE" not in response.text
