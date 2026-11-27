"""Pre-upgrade JM records and isolated multi-product research through the real routes."""

import json

from server import database
from server import futures_journal as journal


async def test_existing_jm_records_survive_init_and_use_the_original_review_rule(client, auth_cookies):
    # The pre-upgrade journal had no symbol, rule version or explicit review cutoff.
    historical = {
        "session": "2026-11-19",
        "question": "两份焦煤合约的价差会扩大吗？",
        "as_of": "2026-11-19T16:30:00+08:00",
        "headline": "跟踪两份固定合约",
        "explanation": "比较近月和远月收盘价之差。",
        "evidence": [],
        "choices": [
            {
                "id": "curve",
                "label": "近远月价差会扩大",
                "rule": "按原合约口径验证",
                "baseline": {
                    "key": "spread",
                    "period": "2026-11-19",
                    "target": "2026-11-20",
                    "value": 118.5,
                    "unit": "元/吨",
                    "legs": ["JM2701", "JM2705"],
                },
            }
        ],
    }
    snapshot = json.dumps(historical, ensure_ascii=False)
    finished = json.dumps({"status": "observed", "headline": "原结论", "evidence": []}, ensure_ascii=False)
    pending_id, final_id = "b" * 32, "e" * 32
    with database.get_db() as db:
        # Reproduce the old database with the legacy table populated before migration.
        db.execute("DROP TRIGGER futures_journal_account_delete")
        db.execute("DROP TABLE futures_research_journal")
        db.executemany(
            """INSERT INTO jm_research_journal(id,username,session,as_of,snapshot_json,choice_id,saved_at,review_json)
               VALUES (?,'testuser','2026-11-19',?,?,'curve','2026-11-25T12:00:00+08:00',?)""",
            [
                (record, historical["as_of"], snapshot, review)
                for record, review in ((pending_id, None), (final_id, finished))
            ],
        )
    database.init_db()
    database.init_db()
    url = "/api/futures/JM/judgments"
    page = await client.get(url, params={"session": "2026-11-19"}, cookies=auth_cookies)
    assert page.status_code == 200
    assert {r["id"] for r in page.json()["items"]} == {pending_id, final_id}
    pending = await client.get(f"{url}/{pending_id}/review", cookies=auth_cookies)
    assert pending.status_code == 200
    outcome = pending.json()
    assert outcome["status"] == "supported" and "11.5" in outcome["explanation"]
    assert outcome["as_of"] == "2026-11-20T16:30:00+08:00"
    assert (await client.get(f"{url}/{final_id}/review", cookies=auth_cookies)).json() == json.loads(finished)
    with database.get_db() as db:
        # Original snapshots are byte-for-byte intact, including after an upgrade/restart.
        assert [r[0] for r in db.execute("SELECT snapshot_json FROM jm_research_journal")] == [snapshot] * 2
        assert (
            db.execute("SELECT review_json FROM jm_research_journal WHERE id=?", (final_id,)).fetchone()[0]
            == finished
        )
        assert (
            json.loads(
                db.execute(
                    "SELECT review_json FROM jm_research_journal WHERE id=?", (pending_id,)
                ).fetchone()[0]
            )
            == outcome
        )


async def test_rb_real_research_save_review_and_account_symbol_isolation(client, auth_cookies):
    root = "/api/futures/RB"
    day = "2026-11-16"
    ask = {"session": day, "question": "螺纹钢去库是需求回暖还是钢厂减产？"}
    assert (await client.post(root + "/research", json=ask)).status_code == 401
    for code, status in (("JM", 422), ("ZZ", 404)):
        assert (
            await client.post(f"/api/futures/{code}/research", json=ask, cookies=auth_cookies)
        ).status_code == status
    response = await client.post(root + "/research", json=ask, cookies=auth_cookies)
    assert response.status_code == 200
    analysis = response.json()
    assert analysis["symbol"] == "RB" and analysis["rule_version"] == 1
    assert analysis["review_as_of"] == journal.REPLAY_WINDOWS["RB"][1].isoformat()
    key = analysis["analysis_id"]
    assert key and analysis["evidence"]
    choice = {"analysis_id": key, "choice_id": "society_down"}
    url = root + "/judgments"
    path = f"{url}/{key}"
    with database.get_db() as db:
        db.execute("INSERT INTO users(username,password_hash) VALUES ('observer','test-only')")
        other = {"token": database.create_session("observer")}
    assert (await client.post(url, json=choice, cookies=other)).status_code == 404
    assert (
        await client.post("/api/futures/JM/judgments", json=choice, cookies=auth_cookies)
    ).status_code == 404
    saved = await client.post(url, json=choice, cookies=auth_cookies)
    assert saved.status_code == 200
    same = await client.post(url, json=choice, cookies=auth_cookies)
    assert same.json() == saved.json()
    assert (await client.get(url, params={"session": day}, cookies=auth_cookies)).json()["items"] == [
        saved.json()
    ]
    assert (await client.get(url, params={"session": day}, cookies=other)).json()["items"] == []
    assert (await client.get(path + "/review", cookies=other)).status_code == 404
    assert (await client.delete(path, cookies=other)).status_code == 404
    assert (
        await client.get(f"/api/futures/JM/judgments/{key}/review", cookies=auth_cookies)
    ).status_code == 404
    assert (await client.delete(f"/api/futures/JM/judgments/{key}", cookies=auth_cookies)).status_code == 404
    with database.get_db() as db:
        assert not db.execute("SELECT 1 FROM jm_research_journal").fetchone()
        db.execute(
            """INSERT INTO futures_research_journal
            SELECT ?,username,'OTHER',session,as_of,snapshot_json,expires_at,choice_id,saved_at,review_json
            FROM futures_research_journal WHERE id=?""",
            ("d" * 32, key),
        )
    assert (await client.get(url, params={"session": day}, cookies=auth_cookies)).json()["items"] == [
        saved.json()
    ]
    foreign = url + "/" + "d" * 32
    assert (await client.get(foreign + "/review", cookies=auth_cookies)).status_code == 404
    assert (
        await client.get(url, params={"session": day, "cursor": "d" * 32}, cookies=auth_cookies)
    ).status_code == 404
    assert (await client.delete(foreign, cookies=auth_cookies)).status_code == 404
    assert (
        await client.post(url, json={**choice, "analysis_id": "d" * 32}, cookies=auth_cookies)
    ).status_code == 404
    review = await client.get(path + "/review", cookies=auth_cookies)
    assert review.status_code == 200 and review.json()["status"] == "supported"
    database.init_db()
    assert (await client.get(path + "/review", cookies=auth_cookies)).json() == review.json()
    assert (await client.delete(path, cookies=auth_cookies)).status_code == 204
    assert (await client.get(path + "/review", cookies=auth_cookies)).status_code == 404
    with database.get_db() as db:
        db.execute("DELETE FROM users WHERE username='testuser'")
        assert not db.execute("SELECT 1 FROM futures_research_journal").fetchone()
