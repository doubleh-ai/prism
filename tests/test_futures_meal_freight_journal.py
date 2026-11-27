"""Follow the same signed-in research, save and replay path for meal and freight."""

from datetime import datetime

import pytest


@pytest.mark.parametrize(
    ("symbol", "other", "question", "day", "choice_id", "end_day", "outcome"),
    [
        (
            "M",
            "RB",
            "原料豆和豆粕库存变化一致吗？",
            "2026-11-16",
            "meal_stock_down",
            "2026-11-23",
            "supported",
        ),
        ("EC", "M", "欧线结算运价指数下期还会跌吗？", "2026-11-17", "index_up", "2026-11-24", "contradicted"),
    ],
)
async def test_historical_judgment_stays_frozen_and_isolated(
    client, auth_cookies, symbol, other, question, day, choice_id, end_day, outcome
):
    base = "/api/futures/" + symbol
    ask = {"session": day, "question": question}
    assert (await client.post(base + "/research", json=ask)).status_code == 401
    answer = await client.post(base + "/research", cookies=auth_cookies, json=ask)
    assert answer.status_code == 200
    draft = answer.json()
    assert draft["symbol"] == symbol and draft["rule_version"] == 1
    cutoff = datetime.fromisoformat(draft["as_of"])
    assert cutoff.date().isoformat() == day and draft["evidence"]
    assert all(datetime.fromisoformat(e["as_of"]) <= cutoff for e in draft["evidence"])
    assert {c["id"] for c in draft["choices"]} >= {choice_id}
    data = {"analysis_id": draft["analysis_id"], "choice_id": choice_id}
    assert (
        await client.post(f"/api/futures/{other}/judgments", cookies=auth_cookies, json=data)
    ).status_code == 404
    path = base + "/judgments"
    save = await client.post(path, cookies=auth_cookies, json=data)
    assert save.status_code == 200
    saved = save.json()
    assert saved["evidence"] == draft["evidence"] and saved["question"] == question
    rows = (await client.get(path, params={"session": day}, cookies=auth_cookies)).json()
    assert rows["items"] == [saved]
    key = saved["id"]
    assert (
        await client.get(f"/api/futures/{other}/judgments/{key}/review", cookies=auth_cookies)
    ).status_code == 404
    result = (await client.get(f"{path}/{key}/review", cookies=auth_cookies)).json()
    assert result["status"] == outcome and result["as_of"].startswith(end_day)
    assert result["evidence"]
    assert (await client.get(f"{path}/{key}/review", cookies=auth_cookies)).json() == result
    assert (await client.delete(f"{path}/{key}", cookies=auth_cookies)).status_code == 204
