"""JM research snapshots, ownership, and dated comparison through authenticated routes."""

import asyncio
from datetime import datetime, timedelta

import pytest

from server import database
from server import futures_journal as journal

ROOT = "/api/futures/JM"
HISTORY = "2026-11-19"
NOW = datetime(2026, 11, 25, 17, 15, tzinfo=journal.SHANGHAI)


@pytest.fixture
def observations(monkeypatch):
    calls = {"analysis": [], "review": [], "status": "pending"}

    async def analyze(question, cutoff):
        calls["analysis"].append(cutoff)
        return {
            "headline": "近月相对走强",
            "explanation": "固定合约收盘价之差。",
            "evidence": [{"label": "近月减远月", "value": "118.5", "as_of": cutoff.isoformat()}],
            "choices": [
                {"id": choice, "label": choice, "rule": "下期价差和已知数比较", "baseline": {"spread": 118.5}}
                for choice in ("curve", "wait")
            ],
        }

    async def review(choice, cutoff):
        calls["review"].append((choice, cutoff))
        return {"status": calls["status"], "headline": "月差核对", "explanation": "说明", "evidence": []}

    monkeypatch.setattr(journal, "_now", lambda: NOW)
    monkeypatch.setattr(journal.futures_jm_research, "analyze", analyze)
    monkeypatch.setattr(journal.futures_jm_research, "review", review)
    return calls


async def investigate(client, auth_cookies, session="current", question="近月怎么样？"):
    response = await client.post(
        ROOT + "/research", cookies=auth_cookies, json={"session": session, "question": question}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def save(client, auth_cookies, key, choice="curve", **extra):
    return await client.post(
        ROOT + "/judgments", cookies=auth_cookies, json={"analysis_id": key, "choice_id": choice, **extra}
    )


async def test_only_a_supported_owned_snapshot_can_be_saved(client, auth_cookies, observations):
    assert (await client.post(ROOT + "/research", json={"question": "供需原因"})).status_code == 401
    assert (await client.get(ROOT + "/judgments")).status_code == 401
    for bad in ({"question": "   "}, {"question": "煤" * 141}, {"question": "焦煤", "session": "2099-01-01"}):
        assert (await client.post(ROOT + "/research", cookies=auth_cookies, json=bad)).status_code == 422
    for headers in ({"X-Requested-With": "invalid"}, {"X-Prism-Account": "other"}):
        response = await client.post(
            ROOT + "/research", cookies=auth_cookies, json={"question": "焦煤"}, headers=headers
        )
        assert response.status_code in (403, 409)
    assert observations["analysis"] == []

    analysis = await investigate(client, auth_cookies, HISTORY)
    assert observations["analysis"] == [journal.REPLAY_WINDOWS["JM"][0]]
    key = analysis["analysis_id"]
    assert (await save(client, auth_cookies, key, rule="forge")).status_code == 422
    assert (await save(client, auth_cookies, key, "invented")).status_code == 422
    with database.get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES('other','test-only')")
        other = {"token": database.create_session("other")}
    assert (await save(client, other, key)).status_code == 404
    assert (await client.get(ROOT + "/judgments", cookies=auth_cookies, params={"session": HISTORY})).json()[
        "items"
    ] == []

    responses = await asyncio.gather(*(save(client, auth_cookies, key) for _ in range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    record = responses[0].json()
    assert record == responses[1].json()
    assert record["evidence"] == analysis["evidence"] and record["question"] == analysis["question"]
    assert record["as_of"] == journal.REPLAY_WINDOWS["JM"][0].isoformat()
    assert record["saved_at"] == NOW.isoformat()
    assert (await save(client, auth_cookies, key, "wait")).status_code == 409
    path = ROOT + "/judgments/" + record["id"]
    assert (await client.get(path + "/review", cookies=other)).status_code == 404
    assert (await client.delete(path, cookies=other)).status_code == 404
    assert observations["review"] == []
    assert (await client.get(ROOT + "/judgments", cookies=other, params={"session": HISTORY})).json()[
        "items"
    ] == []
    assert (await client.get(ROOT + "/judgments", cookies=auth_cookies)).json()["items"] == []

    response = await client.get(ROOT + "/judgments", cookies=auth_cookies, params={"session": HISTORY})
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"items": [record], "next_cursor": None}
    observations["status"] = "contradicted"
    completed = await client.get(path + "/review", cookies=auth_cookies)
    assert completed.json()["status"] == "contradicted"
    evidence_choice, evaluation_time = observations["review"][0]
    assert evaluation_time == journal.REPLAY_WINDOWS["JM"][1]
    assert evidence_choice["baseline"] == {"spread": 118.5}
    assert evidence_choice["tracking_started_at"] == journal.REPLAY_WINDOWS["JM"][0].isoformat()
    observations["status"] = "supported"
    assert (await client.get(path + "/review", cookies=auth_cookies)).json() == completed.json()
    assert len(observations["review"]) == 1
    assert (await client.delete(path, cookies=auth_cookies)).status_code == 204
    assert (await client.get(path + "/review", cookies=auth_cookies)).status_code == 404
    assert (await save(client, auth_cookies, key)).status_code == 404


async def test_live_tracking_begins_at_save_and_pending_reviews_refresh(
    client, auth_cookies, observations, monkeypatch
):
    analysis = await investigate(client, auth_cookies)
    save_time = NOW + timedelta(minutes=10)
    monkeypatch.setattr(journal, "_now", lambda: save_time)
    recorded = (await save(client, auth_cookies, analysis["analysis_id"])).json()
    path = ROOT + "/judgments/" + recorded["id"] + "/review"
    assert (await client.get(path, cookies=auth_cookies)).json()["status"] == "pending"
    next_week = NOW + timedelta(days=7)
    monkeypatch.setattr(journal, "_now", lambda: next_week)
    observations["status"] = "supported"
    assert (await client.get(path, cookies=auth_cookies)).json()["status"] == "supported"
    choice, cutoff = observations["review"][-1]
    assert recorded["as_of"] == NOW.isoformat()
    assert choice["tracking_started_at"] == save_time.isoformat()
    assert cutoff == next_week
    assert (await client.get(ROOT + "/judgments", cookies=auth_cookies)).json()["items"][0] == recorded


async def test_drafts_expire_and_user_deletion_clears_private_records(
    client, auth_cookies, observations, monkeypatch
):
    first = await investigate(client, auth_cookies)
    second = await investigate(client, auth_cookies)
    assert (await save(client, auth_cookies, first["analysis_id"])).status_code == 200
    monkeypatch.setattr(journal, "_now", lambda: NOW + timedelta(hours=1))
    assert (await save(client, auth_cookies, second["analysis_id"])).status_code == 410
    third = await investigate(client, auth_cookies)
    assert (await save(client, auth_cookies, second["analysis_id"])).status_code == 404
    assert (await save(client, auth_cookies, first["analysis_id"])).status_code == 200
    assert (await save(client, auth_cookies, third["analysis_id"])).status_code == 200
    database.init_db()
    page = (await client.get(ROOT + "/judgments", params={"limit": 1}, cookies=auth_cookies)).json()
    assert page["items"][0]["id"] == third["analysis_id"]
    page2 = (
        await client.get(ROOT + "/judgments", params={"cursor": page["next_cursor"]}, cookies=auth_cookies)
    ).json()
    assert page2["items"][0]["id"] == first["analysis_id"] and page2["next_cursor"] is None
    with database.get_db() as db:
        db.execute("DELETE FROM users WHERE username='testuser'")
        assert db.execute("SELECT COUNT(*) FROM jm_research_journal").fetchone()[0] == 0


async def test_empty_choices_do_not_create_a_savable_result(client, auth_cookies, monkeypatch):
    async def unsupported(question, cutoff):
        return {
            "status": "unsupported",
            "headline": "范围之外",
            "explanation": "",
            "choices": [],
            "evidence": [],
        }

    monkeypatch.setattr(journal.futures_jm_research, "analyze", unsupported)
    result = await investigate(client, auth_cookies, question="豆粕怎么样？")
    assert result["status"] == "unsupported" and result["analysis_id"] is None
    with database.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM jm_research_journal").fetchone()[0] == 0


async def test_cancel_while_review_is_fetching_never_restores_record(
    client, auth_cookies, observations, monkeypatch
):
    analysis = await investigate(client, auth_cookies)
    key = analysis["analysis_id"]
    await save(client, auth_cookies, key)
    started = asyncio.Event()
    resume = asyncio.Event()

    async def detached_review(*args):
        started.set()
        await resume.wait()
        return {"status": "supported", "headline": "结果", "explanation": "说明", "evidence": []}

    monkeypatch.setattr(journal.futures_jm_research, "review", detached_review)
    path = ROOT + "/judgments/" + key
    result = asyncio.create_task(client.get(path + "/review", cookies=auth_cookies))
    await asyncio.wait_for(started.wait(), timeout=3)
    try:
        assert (await client.delete(path, cookies=auth_cookies)).status_code == 204
    finally:
        resume.set()
    assert (await result).status_code == 404
    assert (await client.get(ROOT + "/judgments", cookies=auth_cookies)).json()["items"] == []
