"""Offline parsing and fail-closed tests for the dated CZCE, CFFEX and GFEX adapters.

The report fragments below are verbatim rows of the real public releases fetched
for 2026-11-20 (38,872-byte CZCE UTF-8 text, 64,046-byte CFFEX GBK CSV and the
16,031-byte GFEX JSON body). No test in this module opens a network connection.
"""

import asyncio
import json
from datetime import date

import httpx
import pytest

from server import cffex_futures, czce_futures, exchange_futures, futures, gfex_futures

DAY = date(2026, 11, 20)

CZCE_SAMPLE = (
    "\t\t\t\t\t郑州商品交易所期货每日行情表(2026-11-20)\n"
    "合约代码|昨结算    |今开盘    |最高价    |最低价    |今收盘    |今结算    |涨跌1    |涨跌2    |成交量(手)|持仓量    |增减量    |成交额(万元)|交割结算价\n"
    "AP612 |6,597.00  |6,499.00  |6,645.00  |6,464.00  |6,611.00  |6,572.00  |14.00    |-25.00   |444       |1,951     |-27       |2,917.90    |\n"
    "MA612 |3,283.00  |3,299.00  |3,317.00  |3,255.00  |3,293.00  |3,286.00  |10.00    |3.00     |18,694    |23,905    |-2,772    |61,434.18   |\n"
    "MA701 |3,065.00  |3,094.00  |3,110.00  |3,042.00  |3,073.00  |3,071.00  |8.00     |6.00     |2,427,288 |818,424   |-34,292   |7,454,574.55|\n"
    "小计    |          |          |          |          |          |          |         |         |3,503,584 |1,958,702 |-25,090   |10,507,067.97|\n"
    "总计    |          |          |          |          |          |          |         |         |15,057,629|15,898,824|-85,648   |52,772,862.62|\n"
    "TA701 |5,830.00  |5,904.00  |5,972.00  |5,864.00  |5,950.00  |5,924.00  |120.00   |94.00    |845,425   |953,700   |-4,096    |2,503,937.56|\n"
    "RM701 |2,217.00  |2,228.00  |2,241.00  |2,196.00  |2,203.00  |2,220.00  |-14.00   |3.00     |469,264   |491,396   |-7,514    |1,041,689.91|\n"
    "OI701 |9,890.00  |9,891.00  |9,998.00  |9,850.00  |9,872.00  |9,917.00  |-18.00   |27.00    |238,549   |259,039   |-5,694    |2,365,605.96|\n"
    "PF701 |7,470.00  |7,508.00  |7,622.00  |7,476.00  |7,580.00  |7,536.00  |110.00   |66.00    |214,751   |184,562   |2,134     |809,144.06  |\n"
    "AP701 |5,797.00  |5,642.00  |5,735.00  |5,617.00  |5,666.00  |5,676.00  |-131.00  |-121.00  |268,476   |139,561   |-14,024   |1,523,842.71|\n"
).encode()

CFFEX_SAMPLE = (
    "合约代码,今开盘,最高价,最低价,成交量,成交金额,持仓量,持仓变化,今收盘,今结算,前结算,涨跌1,涨跌2,Delta\n"
    "IF2611,3928.4,3934.4,3892.4,15654,1838177.412,0,-21100,3919,3919.42,3961.6,-42.6,-42.18,--\n"
    "IF2612,3889.8,3889.8,3836,103707,12007759.926,188038,2992,3869,3861,3915.8,-46.8,-54.8,--\n"
    "T2612,110.16,110.18,110.05,155876,17163887.34,177040,-43122,110.075,110.08,110.13,-0.055,-0.05,--\n"
    "TF2612,106.83,106.85,106.765,126938,13557359.585,108912,-26341,106.78,106.79,106.82,-0.04,-0.03,--\n"
    "TS2612,102.662,102.676,102.64,58668,12045191.02,45984,-10668,102.64,102.646,102.664,-0.024,-0.018,--\n"
    "TS2703,102.682,102.69,102.668,27383,5623319.628,44586,7880,102.67,102.678,102.68,-0.01,-0.002,--\n"
    "TS2706,102.676,102.682,102.668,163,33471.76,1642,7,102.67,102.67,102.682,-0.012,-0.012,--\n"
    "HO2611-C-2450,272.8,278.2,272.8,21,60.55,0,-17,278.2,286.5,303.2,-25,-16.7,--\n"
    "小计,,,,86214,17701982.408,92212,-2781,,,,,,--\n"
    "合计,,,,2610374,197953441.715,2945574,-320662,,,,,,--\n"
).encode("gbk")

GFEX_SAMPLE = json.dumps(
    {
        "code": "0",
        "msg": "接口访问成功！",
        "param": {"trade_date": ["20261120"], "trade_type": ["0"]},
        "data": [
            {
                "variety": "多晶硅",
                "varietyEn": "Poly Silicon",
                "diffI": -1952,
                "high": 33040,
                "turnover": 43689.3525,
                "diff": -760,
                "delta": None,
                "close": 32420,
                "diff1": -545,
                "lastClear": 33180,
                "open": 32555,
                "matchQtySum": 0,
                "delivMonth": "2612",
                "low": 32280,
                "clearPrice": 32635,
                "varietyOrder": "ps",
                "openInterest": 14575,
                "volumn": 4462,
            },
            {
                "variety": "多晶硅",
                "varietyEn": "Poly Silicon",
                "diffI": 2664,
                "high": 32580,
                "turnover": 346855.7145,
                "diff": -675,
                "delta": None,
                "close": 31890,
                "diff1": -440,
                "lastClear": 32565,
                "open": 32005,
                "matchQtySum": 0,
                "delivMonth": "2701",
                "low": 31855,
                "clearPrice": 32125,
                "varietyOrder": "ps",
                "openInterest": 81133,
                "volumn": 35986,
            },
            {
                "variety": "多晶硅小计",
                "varietyEn": "Poly Silicon SubTotal",
                "diffI": 2948,
                "high": None,
                "turnover": 497514.4155,
                "diff": None,
                "delta": None,
                "close": None,
                "diff1": None,
                "lastClear": None,
                "open": None,
                "matchQtySum": 0,
                "delivMonth": "",
                "low": None,
                "clearPrice": None,
                "varietyOrder": "ps",
                "openInterest": 155559,
                "volumn": 52077,
            },
        ],
        "time": 1795322280258,
    },
    ensure_ascii=False,
).encode("utf-8")


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _deadline():
    return asyncio.get_running_loop().time() + 5


def _czce_rows():
    return CZCE_SAMPLE.decode("utf-8").splitlines()


def _gfex_payload():
    payload = json.loads(GFEX_SAMPLE)
    assert isinstance(payload, dict)
    return payload


def _gfex_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_czce_real_text_snapshot_uses_dated_futures_contracts():
    value = czce_futures.parse_daily(CZCE_SAMPLE, "MA", DAY)
    assert value["status"] == "collected"
    assert value["date_provenance"] == "reported_date"
    assert value["source_method"] == "GET"
    assert value["quote_unit"] == "元/吨"
    assert [contract["contract"] for contract in value["contracts"]] == ["ma612", "ma701"]
    assert value["contracts"][0] == {
        "contract": "ma612",
        "delivery_month": "2026-12",
        "close": 3293.0,
        "settlement": 3286.0,
        "volume": 18694,
        "open_interest": 23905,
    }
    assert value["active_contract"] == "ma701"
    assert value["curve"]["shape"] == "backwardation"
    assert value["member_positioning"]["status"] == "not_covered"


def test_czce_added_contracts_parse_their_own_rows_and_units():
    for symbol, contract, month, settlement in (
        ("TA", "ta701", "2027-01", 5924.0),
        ("RM", "rm701", "2027-01", 2220.0),
        ("OI", "oi701", "2027-01", 9917.0),
        ("PF", "pf701", "2027-01", 7536.0),
        ("AP", "ap612", "2026-12", 6572.0),
    ):
        value = czce_futures.parse_daily(CZCE_SAMPLE, symbol, DAY)
        assert value["status"] == "collected"
        assert value["quote_unit"] == "元/吨"
        assert value["contracts"][0]["contract"] == contract
        assert value["contracts"][0]["delivery_month"] == month
        assert value["contracts"][0]["settlement"] == settlement
        assert value["member_positioning"]["status"] == "not_covered"


def test_czce_rows_for_other_products_are_not_borrowed():
    with pytest.raises(ValueError, match="missing CZCE contracts"):
        czce_futures.parse_daily(CZCE_SAMPLE, "CF", DAY)


@pytest.mark.parametrize(
    "raw",
    [
        CZCE_SAMPLE.replace(b"2026-11-20", b"2026-11-19"),
        CZCE_SAMPLE.replace("成交量(手)".encode(), "成交量".encode()),
    ],
)
def test_czce_mismatched_title_or_header_fails_closed(raw):
    with pytest.raises(ValueError):
        czce_futures.parse_daily(raw, "MA", DAY)


def test_czce_duplicate_contract_row_fails_closed():
    rows = _czce_rows()
    rows.insert(4, rows[3])
    with pytest.raises(ValueError, match="duplicate CZCE contract"):
        czce_futures.parse_daily("\n".join(rows).encode("utf-8"), "MA", DAY)


async def test_czce_missing_report_is_a_dated_not_found():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "www.czce.com.cn"
        assert request.url.path == "/cn/DFSStaticFiles/Future/2026/20261120/FutureDataDaily.txt"
        return httpx.Response(404)

    async with _client(handler) as client:
        value = await czce_futures.read_daily(client, "MA", DAY, _deadline())

    assert len(requests) == 1
    assert value["status"] == "unavailable"
    assert value["error_code"] == "report_not_found"
    assert value["http_status"] == 404
    assert value["member_positioning"]["status"] == "unavailable"


def test_cffex_two_year_treasury_contracts_parse_with_bond_units():
    value = cffex_futures.parse_daily(CFFEX_SAMPLE, "TS", DAY)
    assert value["status"] == "collected"
    assert value["quote_unit"] == "元"
    assert [contract["contract"] for contract in value["contracts"]] == ["ts2612", "ts2703", "ts2706"]
    assert value["contracts"][0] == {
        "contract": "ts2612",
        "delivery_month": "2026-12",
        "close": 102.64,
        "settlement": 102.646,
        "volume": 58668,
        "open_interest": 45984,
    }
    assert value["active_contract"] == "ts2612"
    assert value["member_positioning"]["status"] == "not_covered"


def test_cffex_gbk_report_decodes_chinese_header_and_contracts():
    raw = CFFEX_SAMPLE.decode("gbk")
    assert "今结算" in raw
    assert "合约代码" in raw
    value = cffex_futures.parse_daily(CFFEX_SAMPLE, "IF", DAY)
    assert [contract["contract"] for contract in value["contracts"]] == ["if2611", "if2612"]
    assert value["date_provenance"] == "request_parameter"
    assert value["quote_unit"] == "点"
    assert value["contracts"][1] == {
        "contract": "if2612",
        "delivery_month": "2026-12",
        "close": 3869.0,
        "settlement": 3861.0,
        "volume": 103707,
        "open_interest": 188038,
    }
    treasury = cffex_futures.parse_daily(CFFEX_SAMPLE, "T", DAY)
    assert treasury["quote_unit"] == "元"
    assert treasury["contracts"] == [
        {
            "contract": "t2612",
            "delivery_month": "2026-12",
            "close": 110.075,
            "settlement": 110.08,
            "volume": 155876,
            "open_interest": 177040,
        }
    ]


def test_cffex_duplicate_contract_row_fails_closed():
    rows = CFFEX_SAMPLE.decode("gbk").splitlines()
    rows.insert(2, rows[1])
    with pytest.raises(ValueError, match="duplicate CFFEX contract"):
        cffex_futures.parse_daily("\n".join(rows).encode("gbk"), "IF", DAY)


async def test_cffex_missing_report_redirect_is_a_dated_not_found():
    def handler(request):
        assert request.url.scheme == "http"
        assert request.url.host == "www.cffex.com.cn"
        return httpx.Response(
            302,
            headers={"location": "http://www.cffex.com.cn/error_page/error_404.html"},
        )

    async with _client(handler) as client:
        value = await cffex_futures.read_daily(client, "IF", DAY, _deadline())

    assert value["status"] == "unavailable"
    assert value["error_code"] == "report_not_found"
    assert value["http_status"] == 302


def test_gfex_real_json_snapshot_uses_chinese_product_and_settlements():
    value = gfex_futures.parse_daily(GFEX_SAMPLE, "PS", DAY)
    assert value["status"] == "collected"
    assert value["source_method"] == "POST"
    assert value["date_provenance"] == "request_parameter"
    assert [contract["contract"] for contract in value["contracts"]] == ["ps2612", "ps2701"]
    assert value["contracts"][0] == {
        "contract": "ps2612",
        "delivery_month": "2026-12",
        "close": 32420.0,
        "settlement": 32635.0,
        "volume": 4462,
        "open_interest": 14575,
    }
    assert value["active_contract"] == "ps2701"


def test_gfex_product_name_and_report_date_are_cross_checked():
    payload = _gfex_payload()
    payload["data"][0]["variety"] = "碳酸锂"
    with pytest.raises(ValueError, match="GFEX product mismatch"):
        gfex_futures.parse_daily(_gfex_bytes(payload), "PS", DAY)

    payload = _gfex_payload()
    payload["param"]["trade_date"] = ["20261119"]
    with pytest.raises(ValueError, match="GFEX report date"):
        gfex_futures.parse_daily(_gfex_bytes(payload), "PS", DAY)

    payload = _gfex_payload()
    payload["data"].insert(1, dict(payload["data"][0]))
    with pytest.raises(ValueError, match="duplicate GFEX contract"):
        gfex_futures.parse_daily(_gfex_bytes(payload), "PS", DAY)


async def test_gfex_read_daily_sends_bounded_public_post():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == "http://www.gfex.com.cn/u/interfacesWebTiDayQuotes/loadList"
        assert request.headers["User-Agent"].startswith("prism-futures/")
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert request.content == b"trade_date=20261120&trade_type=0"
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, content=GFEX_SAMPLE)

    async with _client(handler) as client:
        value = await gfex_futures.read_daily(client, "PS", DAY, _deadline())

    assert value["status"] == "collected"
    assert len(requests) == 1


async def test_gfex_edgeone_challenge_fails_closed_without_leaking_body():
    def handler(request):
        return httpx.Response(
            200,
            content=b"<html><script>function a(a){EO_Bot_Ssid='probe'}</script></html>",
            headers={"content-type": "text/html"},
        )

    async with _client(handler) as client:
        value = await gfex_futures.read_daily(client, "PS", DAY, _deadline())

    assert value["status"] == "unavailable"
    assert value["error_code"] == "automated_access_restricted"
    assert value["http_status"] == 200
    assert value["blocked_by"] == "anti_automation_challenge"
    serialized = json.dumps(value, ensure_ascii=False)
    assert "EO_Bot" not in serialized
    assert "function a(" not in serialized


async def test_gfex_empty_trading_day_is_a_dated_not_found():
    payload = _gfex_payload()
    payload["data"] = [
        {
            "variety": "总计",
            "varietyOrder": "",
            "delivMonth": "",
            "close": None,
            "clearPrice": None,
            "volumn": None,
            "openInterest": None,
        }
    ]
    body = _gfex_bytes(payload)

    def handler(request):
        return httpx.Response(200, content=body)

    async with _client(handler) as client:
        value = await gfex_futures.read_daily(client, "PS", DAY, _deadline())

    assert value["status"] == "unavailable"
    assert value["error_code"] == "report_not_found"
    assert value["http_status"] == 200


async def test_one_failed_exchange_report_does_not_block_other_products():
    def handler(request):
        if request.url.host == "www.czce.com.cn":
            return httpx.Response(500)
        if request.url.host == "www.cffex.com.cn":
            return httpx.Response(200, content=CFFEX_SAMPLE)
        return httpx.Response(200, content=GFEX_SAMPLE)

    async with _client(handler) as client:
        czce_value = await exchange_futures.read_settlements(client, "MA", DAY, _deadline())
        cffex_value = await exchange_futures.read_settlements(client, "IF", DAY, _deadline())
        gfex_value = await exchange_futures.read_settlements(client, "PS", DAY, _deadline())

    assert czce_value["status"] == "unavailable"
    assert czce_value["error_code"] == "http_error"
    assert cffex_value["status"] == "collected"
    assert gfex_value["status"] == "collected"


async def test_one_product_missing_from_a_report_does_not_block_another():
    def handler(request):
        return httpx.Response(200, content=CZCE_SAMPLE)

    async with _client(handler) as client:
        methanol = await exchange_futures.read_settlements(client, "MA", DAY, _deadline())
        cotton = await exchange_futures.read_settlements(client, "CF", DAY, _deadline())

    assert methanol["status"] == "collected"
    assert cotton["status"] == "unavailable"
    assert cotton["error_code"] == "invalid_source_data"


async def test_new_exchange_products_never_request_member_rankings():
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "www.czce.com.cn":
            return httpx.Response(200, content=CZCE_SAMPLE)
        if request.url.host == "www.cffex.com.cn":
            return httpx.Response(200, content=CFFEX_SAMPLE)
        return httpx.Response(200, content=GFEX_SAMPLE)

    async with _client(handler) as client:
        for symbol in ("MA", "IF", "PS"):
            value = await exchange_futures.read_daily(client, symbol, DAY, _deadline())
            assert value["status"] == "collected"
            assert value["member_positioning"]["status"] == "not_covered"

    assert len(requested) == 3


def test_public_catalog_registers_every_covered_product():
    new_products = {
        "MA": ("甲醇", "CZCE"),
        "CF": ("棉花", "CZCE"),
        "SR": ("白糖", "CZCE"),
        "FG": ("玻璃", "CZCE"),
        "TA": ("精对苯二甲酸", "CZCE"),
        "RM": ("菜籽粕", "CZCE"),
        "OI": ("菜籽油", "CZCE"),
        "PF": ("涤纶短纤", "CZCE"),
        "AP": ("苹果", "CZCE"),
        "IF": ("沪深300股指期货", "CFFEX"),
        "IC": ("中证500股指期货", "CFFEX"),
        "T": ("10年期国债期货", "CFFEX"),
        "TF": ("5年期国债期货", "CFFEX"),
        "TS": ("2年期国债期货", "CFFEX"),
        "AL": ("铝", "SHFE"),
        "ZN": ("锌", "SHFE"),
        "NI": ("镍", "SHFE"),
        "AG": ("白银", "SHFE"),
        "RU": ("天然橡胶", "SHFE"),
        "BU": ("石油沥青", "SHFE"),
        "NR": ("20号胶", "INE"),
        "LU": ("低硫燃料油", "INE"),
        "SI": ("工业硅", "GFEX"),
        "LC": ("碳酸锂", "GFEX"),
        "PS": ("多晶硅", "GFEX"),
        "C": ("玉米", "DCE"),
        "I": ("铁矿石", "DCE"),
        "P": ("棕榈油", "DCE"),
    }
    assert len(futures.PRODUCTS) == 54
    for symbol, (name, exchange) in new_products.items():
        assert futures.PRODUCTS[symbol][0] == name
        assert futures.PRODUCTS[symbol][1] == exchange
        assert futures.catalog()[0] is not None
    assert futures._EXCHANGE_SOURCES["CZCE"] == "郑州商品交易所"
    assert futures._EXCHANGE_SOURCES["CFFEX"] == "中国金融期货交易所"
    assert futures._EXCHANGE_SOURCES["GFEX"] == "广州期货交易所"
    assert futures._QUOTE_UNITS["MA"] == "元/吨"
    assert futures._QUOTE_UNITS["C"] == futures._QUOTE_UNITS["I"] == futures._QUOTE_UNITS["P"] == "元/吨"
    assert futures._QUOTE_UNITS["IF"] == "点"
    assert futures._QUOTE_UNITS["T"] == futures._QUOTE_UNITS["TS"] == "元"
    assert futures._QUOTE_UNITS["AG"] == "元/千克"
    assert futures._QUOTE_UNITS["NR"] == futures._QUOTE_UNITS["LU"] == "元/吨"
    assert set(futures._QUOTE_UNITS) >= set(new_products)


def test_native_tool_accepts_new_symbols_and_rejects_unknown():
    from server import capabilities

    for symbol in ("MA", "IF", "SI"):
        assert capabilities.FuturesArgs(symbol=symbol).symbol == symbol
    with pytest.raises(ValueError):
        capabilities.FuturesArgs(symbol="ZZ")


async def test_evidence_document_uses_plain_daily_label_and_http_source():
    def handler(request):
        return httpx.Response(200, content=CFFEX_SAMPLE)

    async with _client(handler) as client:
        daily = await exchange_futures.read_settlements(client, "IF", DAY, _deadline())

    snapshot = {
        "symbol": "IF",
        "name": "沪深300股指期货",
        "exchange": "CFFEX",
        "collected_at": "2026-11-20T12:00:00+08:00",
        "status": "partial",
        "data_as_of": "2026-11-20",
        "source": {"name": "公开数据", "url": "http://www.cffex.com.cn/", "kind": "public"},
        "exchange_daily": daily,
    }
    document = futures.snapshot_document(snapshot)
    assert document is not None
    labels = [section["label"] for section in document["sections"]]
    assert labels == ["交易所结算日报"]
    section = document["sections"][0]
    assert section["url"].startswith("http://")
    assert "3861" in document["pages"][0]["text"]
