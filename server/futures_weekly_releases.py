"""Reviewed public research vintages. Times are conservative Shanghai availability bounds.

The week label identifies a survey edition. Ship arrivals are estimated from
vessel equivalents; a crush plan and its later realization have separate series.
"""

from typing import NamedTuple


class Release(NamedTuple):
    period: str
    known: str
    url: str
    values: dict[str, float]


SERIES = {
    "RB": {
        "output": ("螺纹产量", "137家螺纹钢厂", "万吨"),
        "mill": ("螺纹厂库", "137家螺纹钢厂", "万吨"),
        "society": ("螺纹社会库存", "全国35城", "万吨"),
        "purchases": ("终端采购", "西本沪市线螺", "万吨"),
    },
    "M": {
        "arrivals": ("大豆到港估量", "全国样本油厂，每船按6.5万吨折算", "万吨"),
        "crush": ("大豆实际压榨", "Mysteel全国动态全样本油厂", "万吨"),
        "crush_plan": ("大豆预估压榨", "Mysteel全国动态全样本油厂", "万吨"),
        "beans_stock": ("油厂原料豆库存", "Mysteel全国主要油厂动态样本", "万吨"),
        "meal_stock": ("油厂豆粕库存", "Mysteel全国主要油厂动态样本", "万吨"),
        "feed_days": ("豆粕物理库存天数", "全国50家饲料企业", "天"),
    },
    "EC": {
        "ec2611_settle": ("EC2611 日结算", "上期能源，2026年11月合约", "点"),
        "index": ("SCFIS欧洲航线", "上海出口至欧洲基本港，离港后结算", "点"),
    },
}

SCFIS = "https://www.sse.net.cn/index/scfisTrace"
CALENDAR = "https://www.sse.net.cn/cninfo/notice/index/202512/t20251205_1411580.jsp"
MATURITY = "https://www.ine.cn/reports/businessdata/prmsummary/"
SETTLEMENT_RULE = "https://www.ine.cn/products/futures/index_f/ec_f/manual/202308/P020250812538017993692.pdf"
NE3_NOTICE = "https://www.hapag-lloyd.com/en/services-information/news/2026/11/shipping-from-far-east-to-north-europe--here-s-a-schedule-update.html"

# Exchange-listed expiry and the last three scheduled publication days.
EC_WINDOW = ("EC2611", ("2026-11-16", "2026-11-23", "2026-11-30"), "ec2611_settle")
NE3_FIRST_VERIFIED = "2026-11-25T08:20"
NE3_SCHEDULED = "2026-12-15"

RELEASES = {
    "RB": (
        Release(
            "2026-11-12",
            "2026-11-16T14:28",
            "https://news.mysteel.com/a/26111614/B2AEA1D82FDEC51F.html",
            {"output": 182.82, "mill": 145.69, "society": 320.35},
        ),
        Release(
            "2026-11-19",
            "2026-11-19T14:16",
            "https://qhweb.eastmoney.com/news/202611193934977879.html",
            {"output": 182.64, "mill": 150.15, "society": 310.22},
        ),
        Release(
            "2026-11-13",
            "2026-11-14T06:53",
            "https://m.steelx2.com/news-content.aspx?id=649270",
            {"purchases": 1.40},
        ),
        Release(
            "2026-11-20",
            "2026-11-21T07:27",
            "https://m.steelx2.com/news-content.aspx?id=649563",
            {"purchases": 1.32},
        ),
    ),
    "M": (
        Release(
            "2026-11-13",
            "2026-11-16T16:27",
            "https://ncp.mysteel.com/a/26111616/202FA10468E86DF6.html",
            {"beans_stock": 795.39, "meal_stock": 125.23},
        ),
        Release(
            "2026-11-20",
            "2026-11-23T16:17",
            "https://ncp.mysteel.com/a/26112316/E34E3F115BCE38D8.html",
            {"beans_stock": 799.63, "meal_stock": 117.20},
        ),
        Release(
            "2026-11-13",
            "2026-11-13T17:56",
            "https://ncp.mysteel.com/a/26111317/99FD9AEB5E42D319.html",
            {"crush": 180.09},
        ),
        Release(
            "2026-11-20",
            "2026-11-13T17:56",
            "https://ncp.mysteel.com/a/26111317/99FD9AEB5E42D319.html",
            {"crush_plan": 205.53},
        ),
        Release(
            "2026-11-13",
            "2026-11-16T17:33",
            "https://ncp.mysteel.com/a/26111617/FBA8F248D9874638.html",
            {"arrivals": 193.7},
        ),
        Release(
            "2026-11-20",
            "2026-11-20T19:11",
            "https://ncp.mysteel.com/a/26112019/3064D7528E447500.html",
            {"crush": 180.26},
        ),
        Release(
            "2026-11-20",
            "2026-11-20T19:29",
            "https://ncp.mysteel.com/a/26112019/E3AF5AB2725EBD3B.html",
            {"feed_days": 7.3},
        ),
        Release(
            "2026-11-20",
            "2026-11-23T16:26",
            "https://ncp.mysteel.com/a/26112316/4CE00447638F02D2.html",
            {"arrivals": 184.6},
        ),
    ),
    # The history table identifies publication dates only. Use the next
    # midnight for replay rather than assuming the scheduled intraday minute.
    "EC": tuple(
        Release(day, known, SCFIS, {"index": value})
        for day, known, value in (
            ("2026-11-02", "2026-11-03T00:00", 2304.32),
            ("2026-11-09", "2026-11-10T00:00", 2198.66),
            ("2026-11-16", "2026-11-17T00:00", 2289.56),
            ("2026-11-23", "2026-11-24T00:00", 2201.88),
        )
    )
    + (
        Release(
            "2026-11-24",
            "2026-11-25T00:00",
            "https://www.ine.cn/data/tradedata/future/dailydata/kx20261124.dat",
            {"ec2611_settle": 2204.0},
        ),
    ),
}
