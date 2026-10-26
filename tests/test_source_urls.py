"""Public-source display boundaries; all URLs/credentials are synthetic.

No DNS, HTTP, CLI or model request is made. Literal rejection is not proof of
network isolation, and official-schema server-search frames are not live captures.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.source_urls import (
    HIDDEN_SOURCE_URL,
    public_source_url,
    redact_source_text,
    source_display_payload,
)

ROOT = Path(__file__).resolve().parents[1]
BAD_URLS = [
    "https://name:synthetic@example.test/a",
    "https://@example.test/a",
    "https://example.test/a?API-Key=SYNTHETIC_OTHER_KEY",
    "HTTPS://example.test/a?Access%5FToken=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?API%255fKEY=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?key=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?x=1&amp;Authorization=SYNTHETIC_OTHER_KEY",
    "https://example.test/a#token=SYNTHETIC_OTHER_KEY",
    "https://example.test/a#/oauth?Refresh-Token=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?X-Amz-Security-Token=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?SharedAccessSignature=SYNTHETIC_OTHER_KEY",
    "http://localhost/a",
    "https://foo.localhost/a",
    "http://machine/a",
    "https://metadata.google.internal/a",
    "http://node.local/a",
    "http://127.0.0.1/a",
    "http://127.1/a",
    "http://2130706433/a",
    "http://0x7f000001/a",
    "http://0177.0.0.1/a",
    "http://127%2e0%2e0%2e1/a",
    "http://10.1.2.3/a",
    "http://100.64.0.1/a",
    "http://169.254.169.254/a",
    "http://172.16.0.1/a",
    "http://192.168.0.1/a",
    "http://192.0.2.1/a",
    "http://198.18.0.1/a",
    "http://224.0.0.1/a",
    "http://255.255.255.255/a",
    "http://[::1]/a",
    "http://[::ffff:127.0.0.1]/a",
    "http://[::ffff:8.8.8.8]/a",
    "http://[fe80::1%25en0]/a",
    "http://[fc00::1]/a",
    "http://[2001:db8::1]/a",
    "http://[2002:7f00:1::]/a",
    "http://[3fff::1]/a",
    "https://example.test:99999/a",
    "https://%38.8.8.8/a",
    "https://8。8。8。8/a",
    "https://example.test/a?api&#95;key=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?api&lowbar;KEY=SYNTHETIC_OTHER_KEY",
    "https://example.test/a?api&ndash;key=SYNTHETIC_OTHER_KEY",
    "https&#58;//example.test/a?api_key=SYNTHETIC_OTHER_KEY",
    "https&colon;//example.test/a?api_key=SYNTHETIC_OTHER_KEY",
    "//example.test/a?api_key=SYNTHETIC_OTHER_KEY",
    "//127.0.0.1/a",
    "ftp://example.test/a?api_key=SYNTHETIC_OTHER_KEY",
    "file:///private/synthetic-history?api_key=SYNTHETIC_OTHER_KEY",
]
GOOD_URLS = [
    "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    "https://example.test/a?q=ordinary&symbol=600519#section-1",
    "HTTPS://Example.test/a?q=hello%20world",
    "https://example.test/%E5%85%AC%E5%91%8A?q=2026",
    "https://8.8.8.8/a",
    "https://[2001:4860:4860::8888]/a",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_unsafe_urls_are_not_public_sources_or_visible_text(url):
    assert public_source_url(url) is None
    clean = redact_source_text(f"before [source]({url}) after")
    assert url not in clean and "SYNTHETIC_OTHER_KEY" not in clean
    assert HIDDEN_SOURCE_URL in clean


@pytest.mark.parametrize("url", GOOD_URLS)
def test_public_sources_preserve_normal_query_and_encoded_paths(url):
    assert public_source_url(url) == url
    assert redact_source_text(f"[source]({url})") == f"[source]({url})"


def test_recursive_display_copy_preserves_real_arguments_and_legacy_escaped_url():
    url = BAD_URLS[2]
    raw = {"input": {"url": url}, "legacy": str({"url": url}), "escaped": url.replace("/", "\\/")}
    original = copy.deepcopy(raw)
    clean = source_display_payload(raw)
    assert raw == original
    assert "SYNTHETIC_OTHER_KEY" not in json.dumps(clean)
    assert clean["input"]["url"] == HIDDEN_SOURCE_URL


def test_nested_serialized_tool_json_stays_parseable_and_preserves_public_urls():
    raw = {"text": "", "chunks": [{"t": "tool", "d": json.dumps({"url": GOOD_URLS[0]})}]}
    stored = json.dumps(raw)
    cleaned = source_display_payload(stored)
    assert json.loads(json.loads(cleaned)["chunks"][0]["d"])["url"] == GOOD_URLS[0]
    raw["chunks"][0]["d"] = json.dumps({"url": BAD_URLS[2]})
    hidden = source_display_payload(json.dumps(raw))
    assert json.loads(json.loads(hidden)["chunks"][0]["d"])["url"] == HIDDEN_SOURCE_URL
    assert "SYNTHETIC_OTHER_KEY" not in json.dumps(source_display_payload({BAD_URLS[2]: "value"}))


def _event(kind, content, **extra):
    return {"type": kind, "message": {"content": content}, **extra}


def _node_review(payload):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(ROOT / "tests/source_urls_runtime.cjs")],
        cwd=ROOT,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "source URL runtime ok" in result.stdout


def test_shipped_javascript_matches_public_source_rules_and_hides_fallback_text():
    _node_review({"bad": BAD_URLS, "good": GOOD_URLS, "legacy": BAD_URLS[2]})


async def test_legacy_db_and_jsonl_responses_redact_without_rewriting_history(client, auth_cookies, tmp_path):
    from server.database import get_db
    from server.legacy_history import import_staged_history

    secret = BAD_URLS[2]
    variants = [secret, BAD_URLS[4], *BAD_URLS[-9:]]
    payload = json.dumps(
        {
            "text": "old " + secret,
            "chunks": [{"t": "tool", "c": "WebFetch", "d": str({"url": url})} for url in variants]
            + [{"t": "tool", "c": "WebFetch", "d": json.dumps({"url": GOOD_URLS[0]})}],
        }
    )
    events = [
        _event("user", "old question " + secret),
        _event(
            "assistant",
            [
                {"type": "thinking", "thinking": "PRIVATE_HISTORICAL_REASONING"},
                {"type": "tool_use", "name": "WebFetch", "input": {"url": secret}},
                {"type": "text", "text": "old " + secret + " public " + GOOD_URLS[0]},
            ],
        ),
    ]
    raw = "\n".join(json.dumps(event) for event in events)
    staged = tmp_path.resolve() / "staged-projects"
    project = staged / "legacy-project"
    project.mkdir(parents=True)
    history_path = project / "old.jsonl"
    history_path.write_text(raw)
    with get_db() as db:
        db.execute("INSERT INTO conversations(id,username,title) VALUES('legacy-url','testuser',?)", [secret])
        db.execute("INSERT INTO messages(conv_id,role,content) VALUES('legacy-url','assistant',?)", [payload])
        db.execute(
            "INSERT INTO conversations(id,username,session_id,project_dir) "
            "VALUES('legacy-jsonl','testuser','old','legacy-project')"
        )
    pending = await client.get("/api/convs/legacy-jsonl", cookies=auth_cookies)
    assert pending.status_code == 200
    assert pending.json()["history_migration"]["status"] == "pending_import"
    assert pending.json()["messages"] == []
    with get_db() as db:
        assert import_staged_history(db, staged, username="testuser")["imported"] == 1
    for conv in ("legacy-url", "legacy-jsonl"):
        result = await client.get(f"/api/convs/{conv}", cookies=auth_cookies)
        assert result.status_code == 200 and "SYNTHETIC_OTHER_KEY" not in result.text
        assert HIDDEN_SOURCE_URL in result.text
        assert "PRIVATE_HISTORICAL_REASONING" not in result.text
        if conv == "legacy-url":
            _node_review({"bad": BAD_URLS, "good": GOOD_URLS, "history": result.json()})
        else:
            assert result.json()["history_migration"]["status"] == "imported"
            assert GOOD_URLS[0] in result.text
    listing = await client.get("/api/convs", cookies=auth_cookies)
    assert "SYNTHETIC_OTHER_KEY" not in listing.text
    await client.delete("/api/convs/legacy-url", cookies=auth_cookies)
    trash = await client.get("/api/trash", cookies=auth_cookies)
    assert "SYNTHETIC_OTHER_KEY" not in trash.text and HIDDEN_SOURCE_URL in trash.text
    with get_db() as db:
        assert db.execute("SELECT content FROM messages WHERE conv_id='legacy-url'").fetchone()[0] == payload
    assert history_path.read_text() == raw


async def test_legacy_research_list_and_detail_redact_without_rewriting(client, auth_cookies):
    from server.database import get_db
    from server.research import create_research_run
    from server.research_context import build_research_context

    context = build_research_context(run_id="legacy-source-url")
    create_research_run("testuser", "", "synthetic", context)
    with get_db() as db:
        raw = json.loads(
            db.execute("SELECT payload FROM research_runs WHERE run_id=?", [context["run_id"]]).fetchone()[0]
        )
        raw["question"] = BAD_URLS[2]
        payload = json.dumps(raw)
        db.execute("UPDATE research_runs SET payload=? WHERE run_id=?", [payload, context["run_id"]])
    for path in ("/api/research/runs", "/api/research/runs/" + context["run_id"]):
        response = await client.get(path, cookies=auth_cookies)
        assert response.status_code == 200 and "SYNTHETIC_OTHER_KEY" not in response.text
        assert HIDDEN_SOURCE_URL in response.text
    with get_db() as db:
        assert (
            db.execute("SELECT payload FROM research_runs WHERE run_id=?", [context["run_id"]]).fetchone()[0]
            == payload
        )
