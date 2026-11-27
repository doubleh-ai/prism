"""Exercise current-card interpretation through the signed-in futures API with a synthetic provider."""

import json
from uuid import uuid4

import pytest

from server import futures_interpretation as interpretation
from server import futures_workspace, model_routes
from server.agent import provider


def example(symbol="M"):
    return {
        "generated_at": "2026-11-26T12:00:00+08:00",
        "symbol": symbol,
        "research_brief": {
            "technical": {
                "bias": "bearish",
                "data_as_of": "2026-11-25",
                "label": "偏空",
                "reason": "收盘低于两条均线",
                "close": 3000,
                "ema20": 3010,
                "ema60": 3020,
            },
            "fundamentals": {
                "rows": [
                    {
                        "label": "油厂库存",
                        "scope": "同一调查样本",
                        "current": 10,
                        "previous": 12,
                        "change": -2,
                        "data_as_of": "2026-11-23",
                        "previous_date": "2026-11-16",
                        "unit": "万吨",
                        "stale": False,
                    },
                    {"label": "expired observation", "stale": True},
                ]
            },
        },
        "owner_note": "synthetic-private-canary",
    }


@pytest.fixture
def source(monkeypatch):
    async def read(symbol):
        return example(symbol)

    monkeypatch.setattr(futures_workspace, "card", read)
    monkeypatch.setattr(
        interpretation.futures_institutional_views,
        "views",
        lambda s, t: {
            "status": "available",
            "items": [
                {
                    "institution": "样本研究所",
                    "published_on": "2026-11-23",
                    "title": "样本周报",
                    "stance": "neutral",
                    "summary": "等待下一次到港数据",
                    "horizon": "本周",
                }
            ],
        },
    )


async def test_current_card_and_one_cited_reading(client, auth_cookies, source, monkeypatch):
    calls = []

    async def stream(messages, *, tools, model_cfg, max_tokens, **kwargs):
        calls.append(messages)
        assert not tools and model_cfg["_model_purpose"] == "research" and max_tokens <= 1024
        prompt = messages[0]["content"]
        assert all(s in prompt for s in ["油厂库存", "ema20"])
        assert "样本研究所" not in prompt and "等待下一次到港数据" not in prompt
        assert "expired observation" not in prompt and "synthetic-private-canary" not in prompt
        text = json.dumps(
            {"points": [{"text": "库存下降与日线走弱并存，可跟踪下期库存。", "evidence": ["F1", "T1"]}]},
            ensure_ascii=False,
        )
        yield {"type": "usage", "input_tokens": 30, "output_tokens": 22}
        yield {"type": "text_delta", "text": text}
        yield {
            "type": "done",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage_complete": True,
        }

    monkeypatch.setattr(provider, "stream", stream)
    path = "/api/futures/M"
    card = (await client.get(path + "/card", cookies=auth_cookies)).json()
    headers = {
        "X-Prism-Account": "testuser",
        "X-Request-ID": str(uuid4()),
        "X-Futures-Evidence": card["research_brief"]["evidence_id"],
    }
    assert card["research_brief"]["interpret_available"] and not calls
    response = await client.post(path + "/interpret", cookies=auth_cookies, headers=headers)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert [r["id"] for r in body["references"]] == ["F1", "T1", "I1"]
    assert "[F1] [T1]" in body["summary"] and "样本研究所" in body["summary"]
    assert body["summary"].endswith("[I1]") and body["as_of"] == card["generated_at"]
    assert (await client.post(path + "/interpret", cookies=auth_cookies, headers=headers)).status_code == 409
    assert len(calls) == 1
    usage = model_routes.snapshot()["routes"][0]["usage"]
    assert usage == {"calls": 1, "unknown_calls": 0, "input_tokens": 30, "output_tokens": 22}


async def test_stale_or_mismatched_evidence_prevents_model_calls(client, auth_cookies, source, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("unverified evidence reached a model")
        yield

    monkeypatch.setattr(provider, "stream", forbidden)
    path = "/api/futures/M/interpret"
    assert (await client.post(path)).status_code == 401
    headers = {"X-Prism-Account": "testuser", "X-Request-ID": str(uuid4()), "X-Futures-Evidence": "0" * 64}
    assert (await client.post(path, cookies=auth_cookies, headers=headers)).status_code == 409
    old = example()
    old["research_brief"] = {
        "fundamentals": {"rows": [{"stale": True}]},
        "technical": {"bias": "unavailable"},
    }
    monkeypatch.setattr(
        interpretation.futures_institutional_views, "views", lambda s, t: {"status": "stale", "items": []}
    )

    async def stale(symbol):
        return old

    monkeypatch.setattr(futures_workspace, "card", stale)
    brief = (await client.get("/api/futures/M/card", cookies=auth_cookies)).json()["research_brief"]
    assert not brief["interpret_available"]
    headers["X-Futures-Evidence"] = brief["evidence_id"]
    assert (await client.post(path, cookies=auth_cookies, headers=headers)).status_code == 422
    assert model_routes.snapshot()["routes"][0]["usage"]["calls"] == 0


@pytest.mark.parametrize("ids", [[], ["F99"], ["F1", "F1"]])
def test_each_interpretation_point_needs_real_distinct_references(ids):
    with pytest.raises(ValueError):
        interpretation.render(
            json.dumps({"points": [{"text": "库存下降", "evidence": ids}]}), {"F1": {}}, "now"
        )


async def test_provider_failure_is_one_consumed_reservation(client, auth_cookies, source, monkeypatch):
    calls = []

    async def incomplete(*args, **kwargs):
        calls.append(True)
        yield {"type": "usage", "input_tokens": 11}
        raise RuntimeError("synthetic upstream error")

    monkeypatch.setattr(provider, "stream", incomplete)
    path = "/api/futures/M"
    brief = (await client.get(path + "/card", cookies=auth_cookies)).json()["research_brief"]
    headers = {
        "X-Prism-Account": "testuser",
        "X-Request-ID": str(uuid4()),
        "X-Futures-Evidence": brief["evidence_id"],
    }
    result = await client.post(path + "/interpret", cookies=auth_cookies, headers=headers)
    assert result.status_code == 502 and "upstream" not in result.text
    assert (await client.post(path + "/interpret", cookies=auth_cookies, headers=headers)).status_code == 409
    assert len(calls) == 1
    assert model_routes.snapshot()["routes"][0]["usage"] == {
        "calls": 1,
        "unknown_calls": 1,
        "input_tokens": 11,
        "output_tokens": None,
    }


def test_displayed_evidence_is_bound_to_the_original_source(source):
    card = example()
    first = interpretation.enrich(card)["research_brief"]["evidence_id"]
    card["generated_at"] = "2026-11-26T12:01:00+08:00"
    assert interpretation.enrich(card)["research_brief"]["evidence_id"] == first
    card["research_brief"]["fundamentals"]["rows"][0]["source"] = {"name": "经订正的来源"}
    assert interpretation.enrich(card)["research_brief"]["evidence_id"] != first


async def test_interpretation_charges_only_the_account_that_opened_the_page(client, auth_cookies, source):
    path = "/api/futures/M/interpret"
    for headers in ({}, {"X-Prism-Account": "previous-user"}):
        assert (await client.post(path, cookies=auth_cookies, headers=headers)).status_code == 409
    assert model_routes.snapshot()["routes"][0]["usage"]["calls"] == 0


def receipt_facts(symbol):
    from datetime import datetime

    card = example(symbol)
    brief = card["research_brief"]
    cutoff = datetime.fromisoformat(card["generated_at"])
    brief["institutions"] = interpretation.futures_institutional_views.views(symbol, cutoff)
    return interpretation.evidence(symbol, brief)[0]


def test_meal_replay_keeps_each_source_with_its_horizon():
    # From the Nov 26 live acceptance: the model reversed the two horizons and invented disagreement.
    wrong = (
        "基本面与技术判断存在分歧：上海东亚期货指出外盘原料走弱、国内库存偏高且出货主要靠刚需，"
        "可解释日线偏空；瑞达期货则认为开机下降缓解短期压力但中期供应仍宽松，"
        "分歧在于压力主要落在短期缓解还是中期宽松的延续，期限分别为中期供应和周度。"
    )
    raw = json.dumps(
        {
            "points": [
                {"text": "油厂库存下降，日线仍然偏空。", "evidence": ["F1", "T1"]},
                {"text": wrong, "evidence": ["I1", "I2", "T1"]},
            ]
        }
    )
    result = interpretation.render(raw, receipt_facts("M"), "now")
    text = result["summary"]
    assert "油厂库存下降，日线仍然偏空。" in text
    assert not any(s in text for s in ("分别为", "分歧", "共识", "unclassified"))
    lines = text.splitlines()
    assert any(
        "瑞达期货" in line and "中期供应" in line and "[I1]" in line and "[I2]" not in line for line in lines
    )
    assert any(
        "上海东亚期货" in line and "周度" in line and "[I2]" in line and "[I1]" not in line for line in lines
    )
    assert [r["id"] for r in result["references"]] == ["F1", "T1", "I1", "I2"]


def test_real_fundamental_and_technical_divergence_is_still_explainable():
    reading = "库存下降但日线偏空，基本面与技术走势出现分歧；库存变化的负值表示去库。"
    raw = json.dumps({"points": [{"text": reading, "evidence": ["F1", "T1"]}]})
    result = interpretation.render(raw, receipt_facts("M"), "now")
    assert reading in result["summary"]
    assert "[F1] [T1]" in result["summary"]


@pytest.mark.parametrize(
    "fabrication",
    [
        "机构一致认为期价即将下跌。",
        "瑞达期货预计期价即将下跌。",
        "东亚认为期价即将下跌。",
    ],
)
def test_market_references_cannot_be_used_to_attribute_a_forecast(fabrication):
    raw = json.dumps({"points": [{"text": fabrication, "evidence": ["T1"]}]})
    result = interpretation.render(raw, receipt_facts("M"), "now")
    assert fabrication not in result["summary"]
    assert "数值解读未通过" in result["summary"]
    assert "[I1]" in result["summary"] and "[I2]" in result["summary"]


async def test_freight_institutions_only_keeps_both_spot_horizons_without_model(
    client, auth_cookies, monkeypatch
):
    data = example("EC")
    data["research_brief"].update(fundamentals={"rows": []}, technical={"bias": "unavailable"})

    async def card(_symbol):
        return data

    def unexpected(*args, **kwargs):
        pytest.fail("institution-only interpretation reached a model or quota lookup")

    monkeypatch.setattr(futures_workspace, "card", card)
    monkeypatch.setattr(model_routes, "resolve", unexpected)
    monkeypatch.setattr(model_routes, "reserve", unexpected)
    uri = "/api/futures/EC"
    brief = (await client.get(uri + "/card", cookies=auth_cookies)).json()["research_brief"]
    response = await client.post(
        uri + "/interpret",
        cookies=auth_cookies,
        headers={
            "X-Prism-Account": "testuser",
            "X-Request-ID": str(uuid4()),
            "X-Futures-Evidence": brief["evidence_id"],
        },
    )
    assert response.status_code == 200
    result = response.json()
    lines = result["summary"].splitlines()
    assert any(
        "瑞达期货" in line and "近期" in line and "现货" in line and "偏空" in line and "[I1]" in line
        for line in lines
    )
    assert any(
        "上海东亚期货" in line
        and "十二月上半月" in line
        and "现货" in line
        and "偏多" in line
        and "[I2]" in line
        for line in lines
    )
    assert [r["id"] for r in result["references"]] == ["I1", "I2"]
    assert model_routes.snapshot()["routes"][0]["usage"]["calls"] == 0
