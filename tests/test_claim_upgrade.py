"""The wrap-up upgrade round fills source pages for server-registered numbers.

Coverage is server-owned, so the model only has to add ``source_locator`` and
``source`` for numbers the server already extracted. These tests pin the
``claim_key`` locator (one upgrade, never a duplicate), the original-page gate
that still applies, and the end-to-end runtime upgrade round after the report.
"""

import json

import pytest
from fastapi import HTTPException
from test_native_agent_runtime_review import done, events, execute, last_usage
from test_native_agent_runtime_review import engine as engine
from test_server_side_claims import _run, _seed_original, _upgrade

from server.agent import runtime
from server.research import (
    finalize_research_run,
    ledger,
    model_review,
    read_research_run,
    record_claims,
    register_report_claims,
    upgrade_candidates,
)

# ---- claim_key locator -----------------------------------------------------------------


def test_claim_key_locates_the_server_number_instead_of_inserting_a_duplicate():
    _run("run-upgrade-key")
    document, passage = _seed_original("run-upgrade-key")
    registered = register_report_claims("alice", "run-upgrade-key", "2025 年产能为 320 万吨。")
    server_claim = registered["claims"][0]
    assert server_claim["origin"] == "server_extract"
    assert server_claim["verification"] == "unverified"
    assert server_claim["claim_key"] == ["mass_t", None, "3200000"]

    upgrade = _upgrade(
        "2025 年产能为 320 万吨",
        "2025 年产能为 320 万吨",
        "320",
        "万吨",
        document=document,
        passage=passage,
    )
    upgrade["claim_key"] = list(server_claim["claim_key"])
    # Period and statement wording are not part of the locator: the claim_key
    # still finds the server row, so no duplicate claim is written.
    upgrade["period"] = "2025 财年"

    upgraded = record_claims("alice", "run-upgrade-key", [upgrade])["claims"]
    assert [claim["claim_id"] for claim in upgraded] == [server_claim["claim_id"]]
    assert upgraded[0]["verification"] == "verified_source"
    assert upgraded[0]["origin"] == "server_extract"
    assert upgraded[0]["source_locator"] == {"document_id": document["document_id"], "page": 1}

    stored = read_research_run("alice", "run-upgrade-key")["claims"]
    assert len(stored) == 1
    assert stored[0]["claim_id"] == server_claim["claim_id"]


def test_claim_key_upgrade_page_without_the_number_demotes_to_secondhand():
    _run("run-upgrade-page")
    document, passage = _seed_original("run-upgrade-page", excerpt="2025 年营业收入为 128.6 亿元。")
    registered = register_report_claims("alice", "run-upgrade-page", "2025 年产能为 320 万吨。")
    server_claim = registered["claims"][0]

    upgrade = _upgrade(
        "2025 年产能为 320 万吨",
        "2025 年产能为 320 万吨",
        "320",
        "万吨",
        document=document,
        passage=passage,
    )
    upgrade["claim_key"] = list(server_claim["claim_key"])
    upgraded = record_claims("alice", "run-upgrade-page", [upgrade])["claims"][0]

    # The declared page is real and original, but it does not contain 320: the
    # existing gate demotes instead of certifying, exactly as before.
    assert upgraded["verification_declared"] == "verified_source"
    assert upgraded["verification"] == "secondhand"
    assert upgraded["verification_reason"]["code"] == "value_not_in_page_text"
    assert "verification_not_supported" in upgraded["gaps"]
    stored = read_research_run("alice", "run-upgrade-page")["claims"]
    assert len(stored) == 1 and stored[0]["verification"] == "secondhand"


def test_claim_key_upgrade_without_an_original_document_is_rejected():
    _run("run-upgrade-no-original")
    registered = register_report_claims("alice", "run-upgrade-no-original", "2025 年产能为 320 万吨。")
    server_claim = registered["claims"][0]
    upgrade = {
        "kind": "fact",
        "statement": "2025 年产能为 320 万吨。",
        "period": "2025",
        "value": "320",
        "unit": "万吨",
        "verification": "verified_source",
        "source_locator": {"document_id": "doc_missing", "page": 1},
        "claim_key": list(server_claim["claim_key"]),
    }
    with pytest.raises(HTTPException) as error:
        record_claims("alice", "run-upgrade-no-original", [upgrade])
    assert error.value.status_code == 422
    assert getattr(error.value, "error_code", None) == "original_required"
    stored = read_research_run("alice", "run-upgrade-no-original")["claims"]
    assert len(stored) == 1 and stored[0]["verification"] == "unverified"


@pytest.mark.parametrize(
    "claim_key",
    [["mass_t"], ["mass_t", None, "not-a-number"], "mass_t", ["mass_t", None]],
)
def test_claim_key_must_be_the_research_read_shape(claim_key):
    _run("run-upgrade-bad-key")
    claim = {
        "kind": "fact",
        "statement": "2025 年产能为 320 万吨。",
        "value": "320",
        "unit": "万吨",
        "claim_key": claim_key,
    }
    with pytest.raises(HTTPException) as error:
        record_claims("alice", "run-upgrade-bad-key", [claim])
    assert error.value.status_code == 422
    assert "claim_key" in str(error.value.detail)


# ---- upgrade candidate list ------------------------------------------------------------


def test_upgrade_candidates_lists_only_numbers_missing_provenance():
    _run("run-upgrade-list")
    document, passage = _seed_original("run-upgrade-list")

    first = upgrade_candidates("alice", "run-upgrade-list", "2025 年产能为 320 万吨，营业收入 128.6 亿元。")
    assert first["claims_registered"] == 2
    assert [row["unit"] for row in first["claims"]] == ["万吨", "亿元"]
    # The rows carry the report's own writing so the model can echo a value the
    # original page actually contains instead of a converted magnitude.
    assert [row["value"] for row in first["claims"]] == ["320", "128.6"]
    for row in first["claims"]:
        assert set(row) == {
            "claim_key",
            "claim_id",
            "value",
            "unit",
            "currency",
            "period",
            "statement",
        }
        assert row["claim_key"] and row["statement"].startswith("报告正文数字：")

    # A blank report body never raises and never registers.
    assert upgrade_candidates("alice", "run-upgrade-list", "  ") == {
        "claims": [],
        "claims_registered": 0,
    }

    target = first["claims"][1]
    upgrade = _upgrade(
        "营业收入 128.6 亿元",
        "营业收入 128.6 亿元",
        "128.6",
        "亿元",
        document=document,
        passage=passage,
    )
    upgrade["claim_key"] = list(target["claim_key"])
    record_claims("alice", "run-upgrade-list", [upgrade])

    again = upgrade_candidates("alice", "run-upgrade-list", "2025 年产能为 320 万吨，营业收入 128.6 亿元。")
    assert [row["unit"] for row in again["claims"]] == ["万吨"]
    assert again["claims_registered"] == 2


# ---- runtime upgrade round after the report --------------------------------------------


def _seed_original_page(user, run_id, excerpt):
    """Insert one fetched original page for the evidence gate, for any user/run."""
    document = {
        "document_id": f"doc_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": 3,
        "gaps": [],
    }
    passage = {
        "passage_id": f"pass_{run_id}",
        "run_id": run_id,
        "document_id": document["document_id"],
        "excerpt": excerpt,
        "content_scope": "original_pdf_page",
        "locator": {"kind": "pdf_page", "page": 1, "section": None},
    }
    from server.database import get_db

    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [
                document["document_id"],
                run_id,
                f"receipt-{run_id}",
                json.dumps(document, ensure_ascii=False),
            ],
        )
        db.execute(
            "INSERT INTO research_passages VALUES (?,?,?,?)",
            [
                passage["passage_id"],
                run_id,
                document["document_id"],
                json.dumps(passage, ensure_ascii=False),
            ],
        )
    return document, passage


async def test_report_upgrade_round_fills_server_registered_source_pages(engine, monkeypatch):
    run_id = engine.run["run_id"]
    excerpt = "2025 年产能为 320 万吨，营业收入 128.6 亿元。"
    ledger.create_research_run(
        engine.user,
        engine.conv,
        engine.run["question"],
        runtime.build_research_context(run_id=run_id, engine="prism-native"),
    )
    document, passage = _seed_original_page(engine.user, run_id, excerpt)
    seen = {}

    async def script(messages, **kwargs):
        system = kwargs["system"]
        if "待补齐来源的数字（服务端已登记，按 claim_key 定位，不要新建重复）：" in system:
            marker = "待补齐来源的数字（服务端已登记，按 claim_key 定位，不要新建重复）：\n"
            body = system.split(marker, 1)[1].split("\n本轮进入来源补齐阶段", 1)[0]
            rows = json.loads(body)
            seen["rows"] = rows
            seen["prompt"] = system
            claim = {
                "kind": "fact",
                "statement": "2025 年产能为 320 万吨。",
                "supports": [passage["passage_id"]],
                "quotes": {passage["passage_id"]: "2025 年产能为 320 万吨"},
                "period": "2025",
                "value": rows[0]["value"],
                "unit": rows[0]["unit"],
                "currency": rows[0]["currency"],
                "claim_key": list(rows[0]["claim_key"]),
                "verification": "verified_source",
                "source_locator": {"document_id": document["document_id"], "page": 1},
                "source": "示例年报",
            }
            arguments = {"claims": [claim]}
            yield {"type": "usage", "input_tokens": 11, "output_tokens": 12}
            yield {
                "type": "tool_call",
                "id": "upgrade-claims",
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "upgrade-claims",
                        "name": "research_record_claims",
                        "input": arguments,
                    }
                ],
            )
            return
        yield {"type": "usage", "input_tokens": 10, "output_tokens": 10}
        yield {"type": "text_delta", "text": "2025 年产能为 320 万吨。"}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert outcome.status == "completed"
    assert "已为你登记好数字" in seen["prompt"]
    assert seen["rows"] and seen["rows"][0]["claim_key"] == ["mass_t", None, "3200000"]
    assert seen["rows"][0]["value"] == "320" and seen["rows"][0]["unit"] == "万吨"
    phases = [event.get("phase") for event in events(engine) if event.get("type") == "model_started"]
    assert phases == ["research", "claim_upgrade"]
    usage = last_usage(engine)
    assert usage["model_calls"] == 2 and usage["usage_complete"]
    # The last model_usage event is emitted before tool dispatch, so count the
    # executed tool call from its event instead.
    assert [e.get("tool") for e in events(engine) if e.get("type") == "tool_started"] == [
        "research_record_claims"
    ]

    stored = ledger.read_research_run(engine.user, run_id)
    claims = stored["claims"]
    assert len(claims) == 1
    claim = claims[0]
    assert claim["claim_id"] == seen["rows"][0]["claim_id"]
    assert claim["origin"] == "server_extract"
    assert claim["verification"] == "verified_source"
    assert claim["source_locator"]["document_id"] == document["document_id"]
    assert claim["source_locator"]["page"] == 1
    assert claim["source"] == "示例年报"

    revision = finalize_research_run(engine.user, run_id, "2025 年产能为 320 万吨。")
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0
    assert revision["claim_reconciliation"]["coverage"] == 1.0
    assert "structured_claims_missing" not in revision["gaps"]
    assert "semantic_review_pending" in revision["gaps"]


def _seed_reviewable_page(user, run_id, excerpt):
    """Insert one original page that also satisfies the excerpt-review binding.

    ``_seed_original_page`` documents carry no ``retrieved_body_sha256``, which is
    the review packet's proof that the excerpt came from a server fetch. A claim
    can only be review-eligible when that hash is present.
    """
    import hashlib

    from server.database import get_db

    document = {
        "document_id": f"doc_{run_id}",
        "run_id": run_id,
        "url": "https://example.test/annual",
        "title": "示例年报",
        "published_at": "2026-03-18T10:00:00+08:00",
        "publication_precision": "day",
        "original_obtained": True,
        "page_count": 3,
        "gaps": [],
        "retrieved_body_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
    }
    passage = {
        "passage_id": f"pass_{run_id}",
        "run_id": run_id,
        "document_id": document["document_id"],
        "excerpt": excerpt,
        "content_scope": "original_pdf_page",
        "locator": {"kind": "pdf_page", "page": 1, "section": None},
    }
    with get_db() as db:
        db.execute(
            "INSERT INTO research_documents VALUES (?,?,?,?)",
            [
                document["document_id"],
                run_id,
                f"receipt-{run_id}",
                json.dumps(document, ensure_ascii=False),
            ],
        )
        db.execute(
            "INSERT INTO research_passages VALUES (?,?,?,?)",
            [
                passage["passage_id"],
                run_id,
                document["document_id"],
                json.dumps(passage, ensure_ascii=False),
            ],
        )
    return document, passage


async def test_upgrade_round_can_start_the_fallback_review_after_success(engine, monkeypatch):
    # Reachability for the review fallback: the earlier review runs before the
    # upgrade round, when model claims are still invalid. The upgrade round can
    # add a server page to a claim that stays partial (here: missing period), and
    # that claim is only reviewable after the model's successful claim call. The
    # runtime now starts exactly one review there and records the narrower scope.
    run_id = engine.run["run_id"]
    excerpt = "2025 年产能为 320 万吨，营业收入 128.6 亿元。"
    ledger.create_research_run(
        engine.user,
        engine.conv,
        engine.run["question"],
        runtime.build_research_context(run_id=run_id, engine="prism-native"),
    )
    document, passage = _seed_reviewable_page(engine.user, run_id, excerpt)
    seen = {}

    async def script(messages, **kwargs):
        system = kwargs["system"]
        if model_review.SYSTEM in system:
            packet = json.loads(messages[0]["content"])
            seen["packet"] = packet
            response = {
                "packet_id": packet["packet_id"],
                "assessments": [
                    {
                        "claim_id": claim["claim_id"],
                        "verdict": "supported",
                        "reason": "节选逐字支持该数字。",
                        "evidence_ids": [source["passage_id"] for source in claim["sources"]],
                    }
                    for claim in packet["claims"]
                ],
            }
            yield {"type": "usage", "input_tokens": 20, "output_tokens": 30}
            yield {"type": "text_delta", "text": json.dumps(response, ensure_ascii=False)}
            yield done()
            return
        if "待补齐来源的数字（服务端已登记，按 claim_key 定位，不要新建重复）：" in system:
            marker = "待补齐来源的数字（服务端已登记，按 claim_key 定位，不要新建重复）：\n"
            rows = json.loads(system.split(marker, 1)[1].split("\n本轮进入来源补齐阶段", 1)[0])
            seen["rows"] = rows
            claim = {
                "kind": "fact",
                "statement": "2025 年产能为 320 万吨。",
                "supports": [passage["passage_id"]],
                "quotes": {passage["passage_id"]: "2025 年产能为 320 万吨"},
                "period": None,
                "value": rows[0]["value"],
                "unit": rows[0]["unit"],
                "currency": rows[0]["currency"],
                "claim_key": list(rows[0]["claim_key"]),
                "verification": "verified_source",
                "source_locator": {"document_id": document["document_id"], "page": 1},
                "source": "示例年报",
            }
            arguments = {"claims": [claim]}
            yield {"type": "usage", "input_tokens": 11, "output_tokens": 12}
            yield {
                "type": "tool_call",
                "id": "upgrade-claims",
                "name": "research_record_claims",
                "arguments": arguments,
            }
            yield done(
                "tool_use",
                content=[
                    {
                        "type": "tool_use",
                        "id": "upgrade-claims",
                        "name": "research_record_claims",
                        "input": arguments,
                    }
                ],
            )
            return
        yield {"type": "usage", "input_tokens": 10, "output_tokens": 10}
        yield {"type": "text_delta", "text": "2025 年产能为 320 万吨。"}
        yield done()

    monkeypatch.setattr(runtime, "model_stream", script)
    outcome = await execute(engine)

    assert outcome.status == "completed" and outcome.error_code is None
    assert seen["rows"]
    phases = [event.get("phase") for event in events(engine) if event.get("type") == "model_started"]
    assert phases == ["research", "claim_upgrade", "evidence_review"]
    # The fallback review saw only the server-backed claim, not the model claim.
    packet = seen["packet"]
    assert packet["review_scope"] == "server_claims_only"
    assert [entry["kind"] for entry in packet["claims"]] == ["fact"]
    stored = ledger.read_research_run(engine.user, run_id)
    review = stored["run"]["model_review"]
    assert review["status"] == "completed"
    assert review["review_scope"] == "server_claims_only"
    assert review["claim_ids"] == [packet["claims"][0]["claim_id"]]
    # The partial claim is still stored as a server_extract claim: the review
    # degrades to the claim, it does not rewrite its origin or citation status.
    claim = stored["claims"][0]
    assert claim["origin"] == "server_extract"
    assert claim["citation_status"] == "partial"
    revision = finalize_research_run(engine.user, run_id, "2025 年产能为 320 万吨。")
    assert revision["claim_reconciliation"]["verified_claim_ratio"] == 1.0
    assert "semantic_review_pending" not in revision["gaps"]
    assert "model_review_incomplete" not in revision["gaps"]
