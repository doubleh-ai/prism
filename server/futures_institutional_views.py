"""Dated institutional views selected from the publishers' original reports."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from urllib.parse import urldefrag
from zoneinfo import ZoneInfo

from . import futures_institutional_refresh

SHANGHAI = ZoneInfo("Asia/Shanghai")
WINDOW_DAYS = 7  # Publication recency on the card; a report's own horizon is separate.


@dataclass(frozen=True)
class Report:
    institution: str
    title: str
    url: str
    published_on: date
    published_time: time | None = None

    def visible_at(self) -> datetime:
        if self.published_time is not None:
            return datetime.combine(self.published_on, self.published_time, SHANGHAI)
        # A date without a release time cannot establish availability intraday.
        return datetime.combine(self.published_on + timedelta(days=1), time.min, SHANGHAI)


@dataclass(frozen=True)
class View:
    report: Report
    stance: Literal["bullish", "bearish", "neutral", "unclassified"]
    horizon: str
    summary: str
    condition: str
    anchor: str = ""
    locator: str = ""


# East Asia Futures' public index dates the meal and freight PDFs to Nov 23.
# The Nov 21 and Nov 20 cover dates are research dates, not publication times.
EAST = "上海东亚期货"
FILE = "https://www.eafutures.com/storage/file/"
MEAL = Report(
    EAST, "豆类周报", FILE + "20261123/df05cfc6cbb83da47c71ab92624c4251.pdf#page=3", date(2026, 11, 23)
)
FREIGHT = Report(
    EAST,
    "运价指数周报（集运欧线）",
    FILE + "20261123/ceb7a080aa1215c236488d711577e98f.pdf#page=3",
    date(2026, 11, 23),
)
STEEL = Report(
    EAST, "钢材产业周报", FILE + "20261120/b074f9ead37171c1d1580bbb3d1f7a6d.pdf#page=7", date(2026, 11, 20)
)
COAL = Report(
    EAST, "双焦市场周报", FILE + "20261120/41d71de8d4225ebd36aba90785195885.pdf#page=6", date(2026, 11, 20)
)
RUIDA = Report(
    "瑞达期货",
    "晨会纪要观点",
    "https://www.rdqh.com/content/show/61/206544",
    date(2026, 11, 26),
    time(10, 15),
)

VIEWS = {
    "M": (
        View(
            MEAL,
            "unclassified",
            "国内豆粕，周度",
            "外盘原料走弱，国内库存偏高，油厂出货主要靠刚需。",
            "跟踪油厂开机和南美大豆产区天气。",
        ),
        View(
            RUIDA,
            "unclassified",
            "国内豆粕，中期供应",
            "开机下降缓解短期压力，但港口原料充足，中期供应仍宽松。",
            "看十一至十二月大豆到港能否兑现，以及饲企是否增加采购。",
        ),
    ),
    "EC": (
        View(
            FREIGHT,
            "bullish",
            "十二月上半月，欧线现货运价",
            "船公司新一轮订舱已有提价成交，该机构据此看好运价回升。",
            "后续船期的实际装载和提价持续性仍待验证。",
        ),
        View(
            RUIDA,
            "bearish",
            "近期，欧线现货运价",
            "淡季货量偏低，亚欧航线逐步复航，机构预计运价中枢承压。",
            "若海峡风险加剧，能源成本上升可能推高运价。",
        ),
    ),
    "RB": (
        View(
            STEEL,
            "neutral",
            "螺纹钢，周度",
            "库存偏低、限产支撑下沿；需求转弱和成本下移限制上沿。",
            "观察厂库是否继续累积，以及冬储启动的时间与规模。",
        ),
        View(
            RUIDA,
            "unclassified",
            "近月 RB2701",
            "低产量带来去库，淡季削弱消费；近期原料走强和空头减仓支撑价格。",
            "留意持仓变化与期价上方阻力。",
        ),
    ),
    "JM": (
        View(
            COAL,
            "neutral",
            "焦煤，周度",
            "深贴水和低库存给价格支撑；铁水减少、矿山复产预期限制反弹。",
            "观察蒙煤通关与山西停产矿实际复产进度。",
        ),
        View(
            RUIDA,
            "unclassified",
            "近月 JM2701",
            "复产偏慢、进口补充收紧提供支撑；焦企利润薄，铁水低位抑制采购。",
            "看安监后煤矿的实际产量和下游冬储力度。",
        ),
    ),
}


# Exact verified product sections for the eight edited launch views.
# A changed section at the same publisher URL must be interpreted on its own merits.
REVIEWED_SECTIONS = {
    "81cd66f6f67bd11614a55548dc4035afc2ff2fda85e3b85725b51a94fd2a429a": "现货运价料低位运行",
    "4339db1360a914a884f081027f64bd7792701690e15f3a8d29845321211a231e": "中期豆粕供应宽松格局尚未扭转",
    "c0661d00b751e8e3e777b17d91d7d5a45159898d49dbf4a47cdec1be78398562": "供应恢复偏慢对盘面形成阶段性支撑",
    "0bd228438c5d576ebad62a3d1b4a0d32ed723dc37f41fe003cff20ed92d7b228": "消费淡季下游需求收缩",
    "6b0f94aa1f62635c6a3e49ad9ed6826b089382c178cff5728bd9ddb9b7907abc": "反弹高度取决于蒙煤通关与山西复产的实际兑现",
    "d2baae8e7b01351fb4d2f11e25c0bbb989d901557c0489de81662e74261d9756": "价格下沿由低库存与限产约束、上沿由淡季需求与成本下移牵制",
    "3e5677b5b5996d9ae2d1cf6069de6d8502020297f09897c0f440bd8b1bfb8e5c": "压榨回升但豆粕库存仍高、提货仅刚需",
    "9576cf30c63f676528042fe3a4f99b7292e61ab99dd3c604aef982f39028def9": "船公司陆续成交12月上半月运价，大多延续提涨的状态",
}


COVERED_SYMBOLS = frozenset(
    [
        "AU",
        "AG",
        "CU",
        "AL",
        "NI",
        "SN",
        "AO",
        "LC",
        "SI",
        "PS",
        "JM",
        "I",
        "RB",
        "HC",
        "SS",
        "C",
        "CS",
        "CF",
        "SR",
        "SP",
        "LH",
        "AP",
        "CJ",
        "JD",
        "SA",
        "FG",
        "SH",
        "MA",
        "TA",
        "RU",
        "NR",
        "PP",
        "V",
        "UR",
        "SC",
        "PG",
        "BU",
        "A",
        "B",
        "M",
        "RM",
        "P",
        "Y",
        "OI",
        "PK",
        "EC",
    ]
)


def views(symbol: str, as_of: datetime) -> dict:
    """Return the latest visible report per institution for this symbol."""
    if as_of.utcoffset() is None:
        raise ValueError("as_of requires a timezone")
    cutoff = as_of.astimezone(SHANGHAI)
    code = symbol.strip().upper()
    visible = futures_institutional_refresh.latest(code, cutoff) if code in COVERED_SYMBOLS else []
    captured_urls = {urldefrag(item["url"]).url for _, _, item in visible}
    for view in VIEWS.get(code, ()):
        report = view.report
        published = report.visible_at()
        if published <= cutoff and urldefrag(report.url).url not in captured_urls:
            item = dict(
                institution=report.institution,
                title=report.title,
                url=report.url,
                published_on=report.published_on.isoformat(),
                stance=view.stance,
                summary=view.summary,
                horizon=view.horizon,
                condition=view.condition,
            )
            visible.append((published, published, item))

    visible.sort(key=lambda row: (row[0], row[1]), reverse=True)
    latest = visible[0][2]["published_on"] if visible else None
    items, unclassified = [], []
    seen = set()
    for _, _, item in visible:
        institution = item["institution"]
        if institution in seen:
            continue
        seen.add(institution)
        if (cutoff.date() - date.fromisoformat(item["published_on"])).days >= WINDOW_DAYS:
            continue
        if "stance" in item:
            items.append(item)
        else:
            unclassified.append(item)
    present = {item["stance"] for item in items}
    return dict(
        status="available"
        if items
        else "unclassified"
        if unclassified
        else "stale"
        if latest
        else "unavailable",
        coverage="机构官网公开研报",
        window_days=WINDOW_DAYS,
        latest_published_on=latest,
        items=items,
        unclassified_items=unclassified,
        missing_sides=[side for side in ("bullish", "bearish") if side not in present],
    )
