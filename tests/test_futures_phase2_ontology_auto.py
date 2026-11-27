"""Tests for Phase 2: 46-product ontology, generic falsifiable research journal, extended institutional extraction, and zero-touch auto-reconciliation."""

from datetime import datetime

from server import (
    futures,
    futures_auto_engine,
    futures_basis,
    futures_institutional_sources,
    futures_institutional_views,
    futures_inventory,
    futures_ontology,
    futures_opinion_extraction,
    futures_workspace,
)
from server.work_lifecycle import Manager


def test_ontology_covers_exact_46_workspace_products_and_6_archetypes():
    assert set(futures_ontology.ONTOLOGY) == set(futures_workspace.UNIVERSE)
    assert len(futures_ontology.ONTOLOGY) == 46
    archetypes = {item.archetype for item in futures_ontology.ONTOLOGY.values()}
    assert archetypes == set(futures_ontology.ARCHETYPE_LABELS)
    assert len(archetypes) == 6
    for sym, meta in futures_ontology.ONTOLOGY.items():
        assert len(meta.drivers) >= 3, sym
        assert len(meta.presets) == 3, sym
        assert meta.seasonal_anchor, sym


def test_extended_institutional_opinion_extraction_covers_all_46_products():
    assert set(futures_opinion_extraction.SUBJECTS) == set(futures_workspace.UNIVERSE)
    sample = {
        "symbol": "LC",
        "institution": "瑞达期货",
        "title": "瑞达期货晨会纪要观点20261126",
        "url": "https://www.rdqh.com/content/show/61/206544",
        "published_on": "2026-11-26",
        "available_at": "2026-11-26T10:15:00+08:00",
        "locator": "晨会纪要 · 碳酸锂",
        "excerpt": "青海冬季盐湖减产。预计短期碳酸锂期价震荡偏强。后续关注下游正极材料排产情况。",
    }
    view = futures_opinion_extraction.extract(sample)
    assert view is not None
    assert view.stance == "bullish"
    assert view.horizon == "短期"
    assert "正极材料排产" in view.condition

    # Verify extended headings map LC and CU while default _rd_sections preserves legacy 4-symbol scope
    html = (
        b'<meta charset="utf-8"><div class="work_article"><h1>T</h1><p><span>'
        + "时间：2026-11-26 10:15".encode()
        + b'</span></p></div><div class="work_article1">'
        + "<p><strong>碳酸锂</strong></p><p>预计短期碳酸锂期价震荡偏强，后续关注正极排产。</p>".encode()
        + b"</div>"
    )
    now = datetime.fromisoformat("2026-11-26T11:00:00+08:00")
    ext_rows = futures_institutional_sources._rd_sections(
        html,
        now.date(),
        "T",
        "https://www.rdqh.com/content/show/61/206544",
        now,
        headings=futures_institutional_sources.EXTENDED_HEADINGS,
    )
    assert [r["symbol"] for r in ext_rows] == ["LC"]
    assert futures_institutional_views.COVERED_SYMBOLS == set(futures_workspace.UNIVERSE)


async def test_generic_research_journal_and_background_auto_reconciliation(client, auth_cookies, monkeypatch):
    # Test CU (one of the 42 newly enabled ontology varieties) end-to-end + background auto-reconciliation!
    day1 = "2026-11-20"
    day2 = "2026-11-26"
    current_close = [74200.0]
    current_day = [day1]

    async def fake_snapshot(symbol, *, workspace=False):
        return {
            "symbol": symbol,
            "series_status": "collected",
            "data_as_of": current_day[0],
            "collected_at": current_day[0] + "T15:30:00+08:00",
            "latest": {"date": current_day[0], "close": current_close[0]},
            "source": {"name": "新浪财经", "url": "https://finance.sina.com.cn/futures/quotes/CU0.shtml"},
            "workspace_percentiles": {"close_pct_5y": 68.5, "span_label": "近5年"},
        }

    async def fake_basis(symbol):
        return {
            "status": "collected",
            "data_as_of": current_day[0],
            "spot_price": 74350.0,
            "futures_contract": "cu2701",
            "futures_price": current_close[0],
            "basis": round(74350.0 - current_close[0], 2),
            "quote_unit": "元/吨",
            "collected_at": current_day[0] + "T15:35:00+08:00",
            "source": {"name": "生意社现期表", "url": "https://www.100ppi.com/sf/day-2026-11-20.html"},
        }

    async def fake_inv(symbol):
        return {"status": "inventory_unavailable", "reason": "测试未提供"}

    monkeypatch.setattr(futures, "snapshot", fake_snapshot)
    monkeypatch.setattr(futures_basis, "snapshot", fake_basis)
    monkeypatch.setattr(futures_inventory, "snapshot", fake_inv)

    # Unsupported trading instruction is cleanly refused
    bad = await client.post(
        "/api/futures/CU/research",
        json={"session": "current", "question": "现在应该满仓做多沪铜吗？"},
        cookies=auth_cookies,
    )
    assert bad.status_code == 200 and bad.json()["status"] == "unsupported"

    # Valid research inquiry returns evidence + falsifiable choices
    res = await client.post(
        "/api/futures/CU/research",
        json={"session": "current", "question": "沪铜现货基差和趋势位置如何？"},
        cookies=auth_cookies,
    )
    assert res.status_code == 200
    doc = res.json()
    assert doc["status"] == "available" and doc["analysis_id"]
    assert any(c["id"] == "close_up" for c in doc["choices"])

    # Save judgment
    saved = await client.post(
        "/api/futures/CU/judgments",
        json={"analysis_id": doc["analysis_id"], "choice_id": "close_up"},
        cookies=auth_cookies,
    )
    assert saved.status_code == 200
    jid = saved.json()["id"]

    # Before target date (Nov 25), background auto-reconciliation leaves it pending
    early = datetime.fromisoformat("2026-11-22T17:00:00+08:00")
    assert await futures_auto_engine.reconcile_pending_judgments(Manager(), now=early) == 0

    # Advance market to Nov 26 with higher close -> background auto-reconcile settles it automatically!
    current_day[0] = day2
    current_close[0] = 75100.0
    later = datetime.fromisoformat("2026-11-26T17:00:00+08:00")
    settled = await futures_auto_engine.reconcile_pending_judgments(Manager(), now=later)
    assert settled == 1

    # Listing judgments now carries auto_review without manual review click!
    listed = (await client.get("/api/futures/CU/judgments", cookies=auth_cookies)).json()["items"]
    assert len(listed) == 1 and listed[0]["id"] == jid
    assert listed[0]["auto_review"]["status"] == "supported"
    assert listed[0]["auto_review"]["auto_reconciled"] is True
    assert "75100" in listed[0]["auto_review"]["explanation"]
