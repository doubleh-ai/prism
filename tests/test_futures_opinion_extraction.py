from datetime import date

import pytest

from server.futures_opinion_extraction import extract


def source(text, symbol="M"):
    return dict(
        symbol=symbol,
        institution="瑞达期货",
        title="晨会纪要",
        url="https://www.rdqh.com/content/show/61/206544",
        published_on="2026-11-26",
        available_at="2026-11-26T10:15:00+08:00",
        locator="晨会 · " + symbol,
        excerpt=text,
    )


def test_identifiable_view_has_verbatim_basis_horizon_condition_and_release_time():
    raw = source("国内豆粕库存减少。预计短期豆粕价格偏强。后续关注油厂开机率。")
    v = extract(raw)
    assert v.stance == "bullish" and v.anchor == "预计短期豆粕价格偏强。"
    assert v.summary == v.anchor and v.anchor in raw["excerpt"]
    assert v.horizon == "短期" and v.condition == "后续关注油厂开机率。"
    assert v.locator == raw["locator"] and v.report.visible_at().isoformat() == raw["available_at"]


@pytest.mark.parametrize(
    ("symbol", "text", "expected"),
    [
        ("EC", "机构预计短期欧线运价承压。", "bearish"),
        ("M", "库存下降，预计豆粕价格维持区间震荡。", "neutral"),
        ("JM", "焦煤库存明显偏低，焦煤现货价格昨日下跌。", "unclassified"),
        ("RB", "螺纹钢价格昨日上涨。", "unclassified"),
        ("M", "国内豆粕方面仍需关注开机。美豆短期震荡偏强。", "unclassified"),
        ("M", "豆油价格偏强。", "absent"),
        ("EC", "若复航提速，欧线运价或再下行。", "unclassified"),
        ("M", "不能简单认为豆粕价格偏强。", "unclassified"),
        ("RB", "预计短期螺纹钢价格偏强。预计中期螺纹钢价格偏弱。", "unclassified"),
        ("M", "豆粕开机率或回升，库存增速预计偏强。", "unclassified"),
        ("M", "豆粕价格下行，预计库存减少。", "unclassified"),
        ("EC", "预计欧线运价下跌风险有所缓和。", "unclassified"),
        ("RB", "预计螺纹钢价格上涨的可能性不大。", "unclassified"),
    ],
)
def test_direction_requires_a_single_unconditional_target_price_outlook(symbol, text, expected):
    v = extract(source(text, symbol))
    assert (v.stance if v else "absent") == expected
    if v:
        assert v.anchor in text


def test_pdf_publication_day_is_preserved_with_next_day_visibility():
    row = source("预计欧线运价偏弱。", "EC")
    row.update(
        institution="上海东亚期货",
        published_on="2026-11-23",
        available_at="2026-11-24T00:00:00+08:00",
        locator="第 3 页",
        url="https://www.eafutures.com/storage/file/20261123/ceb7a080aa1215c236488d711577e98f.pdf#page=3",
    )
    v = extract(row)
    assert v.report.published_on == date(2026, 11, 23) and v.report.published_time is None
    assert v.report.visible_at().isoformat() == row["available_at"]


def test_ambiguous_excerpt_is_not_exposed_as_a_bare_direction():
    raw = source("美豆预计偏强。国内豆粕供应短期仍然充足。")
    v = extract(raw)
    assert v.stance == "unclassified" and "美豆" not in v.anchor
    assert extract(source("免责声明。")) is None
