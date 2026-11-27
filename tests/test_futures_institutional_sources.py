"""Synthetic pages following the two publishers' observed layouts; no external network."""

from datetime import datetime, timedelta

import httpx
import pytest

from server import futures_institutional_refresh as refresh
from server import futures_institutional_sources as sources
from server import futures_institutional_views as iv
from server.work_lifecycle import Manager

NOW = datetime.fromisoformat("2026-11-26T11:00:00+08:00")
RUI = "瑞达期货晨会纪要观点"
TITLE = RUI + "20261126"
URL = sources.RUIDA + "/content/show/61/206544"
CLOSE = "市场交投表现平稳，下一期实际报价还需要根据装载情况验证。"


def rd_index(*rows):
    return (
        '<ul class="contact_list">'
        + "".join(f'<li><em>{day}</em><p><a href="{link}">{label}</a></p></li>' for day, link, label in rows)
        + "</ul>"
    ).encode()


def rd_article(body, *, title=TITLE, published="2026-11-26 10:15"):
    return (
        f'<meta charset="utf-8"><div class="work_article"><h1>{title}</h1><p>'
        f'<span>时间：{published}</span></p></div><div class="work_article1">{body}</div>'
    ).encode()


def test_morning_report_validates_timestamp_symbol_and_source():
    body = rd_index(
        ("2026-11-26", "/content/show/61/206544", TITLE),
        ("2026-11-26", "https://example.com/other", TITLE),
        ("2026-11-25", "/content/show/61/206540", TITLE),
    )
    rows = sources._rd_editions(b'<meta charset="utf-8">' + body, NOW)
    assert rows == [(NOW.date(), TITLE, URL)]
    doc = rd_article(
        f"<p><strong>豆粕</strong></p><p>{CLOSE}</p>"
        "<p><strong>豆油</strong></p><p>隔壁品种的独立观点在此。</p>"
        f"<p><strong>集运指数(欧线)</strong></p><p>时段关注：{CLOSE}</p>"
    )
    parsed = sources._rd_sections(doc, *rows[0], NOW)
    assert [(v["symbol"], v["excerpt"]) for v in parsed] == [("M", CLOSE), ("EC", "时段关注：" + CLOSE)]
    assert parsed[0]["available_at"] == "2026-11-26T10:15:00+08:00"
    assert parsed[1]["locator"] == "晨会纪要 · 集运指数(欧线)"
    assert sources._rd_sections(doc, *rows[0], NOW.replace(hour=10, minute=14)) == []
    with pytest.raises(ValueError, match="conflicting"):
        sources._rd_sections(rd_article("", published="2026-11-25 10:15"), *rows[0], NOW)


def test_changed_or_ambiguous_article_layout_is_reported():
    head = "<p><strong>螺纹</strong></p>"
    for body in (
        head + "<p><strong>焦炭</strong></p><p>另一个品种的正文。</p>",
        (head + f"<p>{CLOSE}</p>") * 2,
    ):
        rows = sources._rd_sections(rd_article(body), NOW.date(), TITLE, URL, NOW)
        assert [(row["symbol"], row["url"], row["excerpt"]) for row in rows] == [("RB", URL, None)]
    with pytest.raises(ValueError, match="no identifiable"):
        sources._rd_sections(
            rd_article("<p>螺纹</p><p>只有文字而无品种标题。</p>"), NOW.date(), TITLE, URL, NOW
        )
    with pytest.raises(ValueError):
        sources._rd_editions(b"<main>No report list</main>", NOW)
    with pytest.raises(ValueError, match="unrecognized"):
        sources._rd_editions(rd_index(("2026-11-26", "/content/show/61/1", "Changed")), NOW)


def test_keep_complete_sentences_and_drop_unbounded_fragments():
    assert sources._excerpt("甲" * 300 + "。" + CLOSE) == CLOSE
    assert sources._excerpt("长" * 4001) is None
    assert sources._excerpt("长" * 300) is None


def ea_link(day, sym, number):
    title = {"M": "豆类周报-豆粕、菜粕", "EC": "上海东亚期货运价指数周报——集运（欧线）"}[sym]
    return (
        f'<a class="list" href="/preview?id={number}&amp;cid=41">'
        f'<div class="left">{title}20261120</div><div class="time">{day}</div></a>'
    )


def test_weekly_visibility_uses_publication_date_and_only_expected_directory():
    doc = (
        '<meta charset="utf-8">'
        + ea_link("2026.11.23", "M", 11)
        + ea_link("2026.11.26", "M", 12)
        + ea_link("2026.11.24", "EC", 13)
    ).encode()
    rows = sources._ea_entries(doc, ("M",), NOW)
    day, known, title, link = rows["M"]
    assert day.isoformat() == "2026-11-23" and title.endswith("20261120") and link.endswith("id=11&cid=41")
    assert known.isoformat() == "2026-11-24T00:00:00+08:00"
    assert set(rows) == {"M"}
    old = NOW.replace(month=12, day=5)
    assert sources._ea_entries(doc, ("M",), old) == {}
    with pytest.raises(ValueError, match="date after publication"):
        sources._ea_entries(doc.replace(b"20261120", b"20261130"), ("M",), NOW)


@pytest.mark.parametrize(
    ("symbol", "page", "next_subject"),
    [
        (
            "M",
            "M 豆粕\nS O Y B E A N M E A L\n主线 · 成本与库存\n本 周 逻 辑\n"
            + CLOSE
            + "\n基 本 面 信 息\n01 数据",
            "RM 菜粕",
        ),
        ("EC", "• 集运（欧线）\n• 【观点】\n" + CLOSE + "\n• 【基本面信息】", "集运其他航线"),
        ("JM", "五、后市展望（焦煤）\n综合评估：" + CLOSE + "\nP A R T  2", "第二部分 · 焦炭"),
        ("RB", "六、后市展望（螺纹钢）\n综合评估：" + CLOSE + "\n第七部分", "其他品种"),
    ],
)
def test_pdf_section_is_anchored_and_page_bound(symbol, page, next_subject):
    item = {"page": 7, "text": page + "\n" + next_subject + "\n不属于这个品种的后文。"}
    number, locator, text = sources._ea_section(symbol, [item])
    assert number == 7 and locator and CLOSE in text and next_subject not in text
    with pytest.raises(ValueError, match="ambiguous"):
        sources._ea_section(symbol, [item, item])
    with pytest.raises(ValueError):
        sources._ea_section(symbol, [{"page": 1, "text": "目录 · " + CLOSE}])


@pytest.mark.parametrize(
    "bad",
    [
        "https://example.com/storage/file/20261126/" + 32 * "a" + ".pdf",
        "/storage/file/20261120/" + 32 * "a" + ".pdf",
        "/storage/file/20261126/" + 32 * "a" + ".pdf?download=1",
    ],
)
async def test_weekly_preview_only_resolves_one_expected_public_original(bad):
    requests = []

    def respond(req):
        requests.append(req.url)
        return httpx.Response(302, headers={"location": bad})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            await sources._ea_pdf_url(client, sources.EAST_ORIGIN + "/preview?id=11&cid=41", NOW.date())
    assert len(requests) == 1


async def test_morning_transport_checks_official_edition_and_size(monkeypatch):
    received = []

    def respond(req):
        url = str(req.url)
        received.append(url)
        if url == sources.RD_INDEX:
            doc = b'<meta charset="utf-8">' + rd_index(("2026-11-26", "/content/show/61/206544", TITLE))
        elif url == URL:
            doc = rd_article(f"<p><strong>豆粕</strong></p><p>{CLOSE}</p>")
        else:
            raise AssertionError("unexpected destination")
        return httpx.Response(200, content=doc, headers={"content-type": "text/html"})

    monkeypatch.setattr(
        sources.public_network,
        "async_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    assert [r["symbol"] for r in await sources.collect("ruida", NOW)] == ["M"]
    assert received == [sources.RD_INDEX, URL]
    monkeypatch.setattr(sources, "HTML_LIMIT", 3)
    with pytest.raises(ValueError, match="size"):
        await sources.collect("ruida", NOW)


async def test_one_unavailable_weekly_sector_preserves_another_reports_verified_section(monkeypatch):
    url = sources.EAST_ORIGIN + "/storage/file/20261123/" + "1" * 32 + ".pdf"
    raw = b"%PDF-1.7 synthetic"

    def respond(request):
        if request.url.path == "/yjzx.html":
            if request.url.params["breed"] != "71":
                return httpx.Response(503)
            doc = '<meta charset="utf-8">' + ea_link("2026.11.23", "M", 11)
            return httpx.Response(200, text=doc, headers={"Content-Type": "text/html"})
        if request.url.path == "/preview":
            return httpx.Response(302, headers={"Location": url})
        assert str(request.url) == url
        return httpx.Response(200, content=raw, headers={"Content-Type": "application/pdf"})

    def extract(value, *, first_page, page_count):
        assert value == raw and (first_page, page_count) == (1, 9)
        text = f"M 豆粕\nSOYBEAN MEAL\n主线 · 成本\n本 周 逻 辑\n{CLOSE}\n基本面信息"
        return {"status": "ok", "pages": [{"page": 3, "text": text}]}

    monkeypatch.setattr(sources, "extract_pdf", extract)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        rows = await sources._read_east(client, NOW)
    assert [(r["symbol"], r["url"], r["excerpt"]) for r in rows] == [("M", url + "#page=3", CLOSE)]


async def test_response_size_enforced_without_content_length(monkeypatch):
    async def data():
        yield b"1234"
        yield b"5678"

    def respond(request):
        return httpx.Response(200, content=data(), headers={"Content-Type": "text/html"})

    monkeypatch.setattr(sources, "HTML_LIMIT", 5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="size"):
            await sources._get(client, sources.RD_INDEX)


async def test_verified_issue_with_unreadable_product_body_supersedes_only_from_capture(monkeypatch):
    clock = [NOW]
    site_offline = [False]
    file_offline = [True]
    # Reuse the original PDF from the curated edition, whose old quote links to page 3.
    pdf = iv.MEAL.url.split("#")[0]
    header = "<p><strong>豆粕</strong></p>"

    def respond(req):
        if site_offline[0]:
            return httpx.Response(503)
        if str(req.url) == sources.RD_INDEX:
            doc = b'<meta charset="utf-8">' + rd_index(("2026-11-26", "/content/show/61/206544", TITLE))
        elif str(req.url) == URL:
            doc = rd_article(header + "<p><strong>豆油</strong></p><p>邻近品种的观点。</p>")
        elif req.url.path == "/yjzx.html":
            if req.url.params["breed"] != "71":
                return httpx.Response(503)
            doc = ('<meta charset="utf-8">' + ea_link("2026.11.23", "M", 11)).encode()
        elif req.url.path == "/preview":
            return httpx.Response(302, headers={"Location": pdf})
        else:
            assert str(req.url) == pdf
            if file_offline[0]:
                return httpx.Response(503)
            return httpx.Response(
                200, content=b"%PDF-1.7 synthetic", headers={"Content-Type": "application/pdf"}
            )
        return httpx.Response(200, content=doc, headers={"Content-Type": "text/html"})

    monkeypatch.setattr(refresh, "_now", lambda: clock[0])
    monkeypatch.setattr(
        sources.public_network,
        "async_client",
        lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(
        sources,
        "extract_pdf",
        lambda *args, **kwargs: {
            "status": "ok",
            "pages": [{"page": 1, "text": "仅有目录，无可核对的品种章节。"}],
        },
    )
    before = NOW - timedelta(seconds=1)
    assert len(iv.views("M", before)["items"]) == 2
    await refresh.refresh_due(Manager())
    # Temporary unavailability of the very same verified PDF does not revoke its archived quote.
    first = iv.views("M", NOW)
    assert [r["url"] for r in first["items"]] == [iv.MEAL.url]
    assert [r["url"] for r in first["unclassified_items"]] == [URL]
    file_offline[0] = False
    clock[0] += timedelta(hours=6)
    await refresh.refresh_due(Manager())
    current = iv.views("M", clock[0])
    assert current["items"] == [] and current["status"] == "unclassified"
    assert {i["url"] for i in current["unclassified_items"]} == {URL, pdf}
    assert all("anchor" not in i and "stance" not in i for i in current["unclassified_items"])
    assert len(iv.views("M", before)["items"]) == 2
    site_offline[0] = True
    clock[0] += timedelta(hours=6)
    await refresh.refresh_due(Manager())
    assert iv.views("M", clock[0]) == current
