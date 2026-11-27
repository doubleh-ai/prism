"""Attach short verbatim evidence from a verified product section to an institutional view."""

import re
from datetime import date, datetime, time, timedelta
from urllib.parse import urlsplit

from .futures_institutional_views import SHANGHAI, Report, View

# A product section can discuss inputs, competing products and overseas prices.
# In those sentences the section heading alone does not identify a price call.
SUBJECTS = {
    "M": (r"豆粕|粕价|连粕|M\d{3,4}", r"美豆(?:粕)?|豆油|菜粕|油脂|大豆"),
    "EC": (r"欧线|欧洲线|欧洲航线|EC\d{3,4}|运价", r"美线|美西|地中海|美东|跨太平洋"),
    "JM": (r"焦煤|炼焦煤|煤价|JM\d{3,4}", r"焦炭|铁矿石|动力煤|焦价"),
    "RB": (r"螺纹(?:钢)?|螺纹价|RB\d{3,4}", r"热卷|铁矿石|钢坯|焦炭|焦煤"),
    "I": (r"铁矿(?:石)?|矿价|I\d{3,4}", r"螺纹|热卷|焦煤|焦炭"),
    "HC": (r"热卷|热轧(?:卷板)?|HC\d{3,4}", r"螺纹|铁矿|焦煤"),
    "SS": (r"不锈钢|SS\d{3,4}", r"沪镍|镍铁|铬铁"),
    "CU": (r"沪铜|铜价|精铜|CU\d{3,4}", r"伦铜|铝|锌|镍"),
    "AL": (r"沪铝|铝价|电解铝|AL\d{3,4}", r"氧化铝|伦铝|铜|锌"),
    "AO": (r"氧化铝|AO\d{3,4}", r"电解铝|沪铝|铝土矿"),
    "NI": (r"沪镍|镍价|精炼镍|NI\d{3,4}", r"不锈钢|硫酸镍"),
    "SN": (r"沪锡|锡价|SN\d{3,4}", r"伦锡|沪铜|沪镍"),
    "LC": (r"碳酸锂|锂价|LC\d{3,4}", r"工业硅|多晶硅|氢氧化锂"),
    "SI": (r"工业硅|硅价|SI\d{3,4}", r"多晶硅|碳酸锂"),
    "PS": (r"多晶硅|硅料|PS\d{3,4}", r"工业硅|碳酸锂"),
    "AU": (r"沪金|黄金|金价|AU\d{3,4}", r"白银|沪银|美债|原油"),
    "AG": (r"沪银|白银|银价|AG\d{3,4}", r"黄金|沪金|铂金"),
    "SC": (r"原油|油价|SC\d{3,4}", r"燃料油|沥青|液化气"),
    "PG": (r"液化(?:石油)?气|LPG|PG\d{3,4}", r"原油|天然气|丙烯"),
    "BU": (r"沥青|BU\d{3,4}", r"原油|燃料油"),
    "MA": (r"甲醇|郑醇|MA\d{3,4}", r"尿素|聚丙烯|乙二醇"),
    "TA": (r"PTA|精对苯二甲酸|TA\d{3,4}", r"乙二醇|短纤|原油|PX"),
    "PP": (r"聚丙烯|PP\d{3,4}", r"塑料|甲醇|PVC"),
    "V": (r"PVC|聚氯乙烯|V\d{3,4}", r"烧碱|纯碱|玻璃|电石"),
    "UR": (r"尿素|UR\d{3,4}", r"甲醇|纯碱|复合肥"),
    "SA": (r"纯碱|SA\d{3,4}", r"玻璃|烧碱|PVC"),
    "FG": (r"玻璃|FG\d{3,4}", r"纯碱|螺纹|PVC"),
    "SH": (r"烧碱|液碱|SH\d{3,4}", r"PVC|氧化铝|纯碱"),
    "RU": (r"天然橡胶|沪胶|天胶|RU\d{3,4}", r"20号胶|合成胶|丁二烯"),
    "NR": (r"20号胶|标胶|NR\d{3,4}", r"沪胶|合成胶"),
    "SP": (r"纸浆|浆价|SP\d{3,4}", r"白糖|棉花"),
    "A": (r"豆一|国产豆|A\d{3,4}", r"豆二|豆粕|美豆"),
    "B": (r"豆二|进口豆|B\d{3,4}", r"豆一|豆粕|豆油"),
    "RM": (r"菜(?:籽)?粕|RM\d{3,4}", r"豆粕|菜油|加菜籽"),
    "P": (r"棕榈油|连棕|棕油|P\d{3,4}", r"豆油|菜油|马棕"),
    "Y": (r"豆油|连豆油|Y\d{3,4}", r"棕榈油|菜油|豆粕|美豆油"),
    "OI": (r"菜(?:籽)?油|郑油|OI\d{3,4}", r"豆油|棕榈油|菜粕"),
    "PK": (r"花生|PK\d{3,4}", r"豆油|豆粕"),
    "C": (r"玉米|连玉米|玉米价格|C\d{3,4}", r"玉米淀粉|小麦|豆粕"),
    "CS": (r"玉米淀粉|淀粉|CS\d{3,4}", r"玉米价格|白糖"),
    "CF": (r"棉花|郑棉|棉价|CF\d{3,4}", r"棉纱|美棉|PTA"),
    "SR": (r"白糖|郑糖|糖价|SR\d{3,4}", r"原糖|棉花|淀粉"),
    "LH": (r"生猪|猪价|LH\d{3,4}", r"玉米|豆粕|鸡蛋"),
    "AP": (r"苹果|AP\d{3,4}", r"红枣|柑橘"),
    "CJ": (r"红枣|CJ\d{3,4}", r"苹果|花生"),
    "JD": (r"鸡蛋|蛋价|JD\d{3,4}", r"生猪|玉米|豆粕"),
}
OUTLOOK = re.compile(r"预计|预期|展望|后市|有望|或将|或仍|看好|看多|看空|或维持|仍将|大概率")
PRICE = re.compile(r"价格|期价|盘面|运价|粕价|煤价|螺纹价|合约|连粕")
NONPRICE = re.compile(
    r"库存|运力|开机|利润|成本|需求|供应|产量|成交量|基差|压榨|涨幅|跌幅|贴水|风险|概率|动能|动力|空间|幅度"
)
HISTORICAL = re.compile(r"昨日|上周|上月|前期|环比|同比|收涨|收跌")
CONDITIONAL = re.compile(
    r"若|倘|如果|一旦|除非|取决于|否则|不排除|尚难|未必|不能|难言|未能|并非|不会|不宜|难以|不再|不看|并不|不大|避免|难有|并未|不及|不足|没有|能否|是否"
)
OBSERVE = re.compile(r"留意|关注|观察|跟踪|取决于|若|如果|一旦")
HORIZON = re.compile(
    r"(?:\d{1,2}月|本月|下月)(?:上半月|下半月|上旬|中旬|下旬|初|底)?|短期|近期|中期|远期|长期|本周|下周|近月|远月"
)
DIRECTIONS = {
    "bullish": re.compile(r"看涨|看多|偏多|偏强|走强|回升|反弹|上行|上涨"),
    "bearish": re.compile(r"看跌|看空|偏空|偏弱|走弱|回落|下行|下跌|承压"),
    "neutral": re.compile(r"区间震荡|区间振荡|横盘|震荡整理|震荡运行"),
}
REPORT_URLS = {
    "瑞达期货": ("www.rdqh.com", r"/content/show/61/\d+"),
    "上海东亚期货": ("www.eafutures.com", r"/storage/file/\d{8}/[a-fA-F0-9]+\.pdf"),
}


def report_from_source(row):
    institution = row["institution"]
    if institution not in REPORT_URLS:
        return None
    url = urlsplit(row["url"])
    host, path = REPORT_URLS[institution]
    if url.scheme != "https" or url.netloc != host or not re.fullmatch(path, url.path) or url.query:
        return None
    day = date.fromisoformat(row["published_on"])
    stamp = datetime.fromisoformat(row["available_at"])
    if stamp.utcoffset() is None:
        return None
    stamp = stamp.astimezone(SHANGHAI)
    if stamp.date() == day:
        minute = stamp.time()
    elif stamp.date() == day + timedelta(days=1) and stamp.time() == time.min:
        minute = None
    else:
        return None
    return Report(institution, row["title"], row["url"], day, minute)


def _bias(sentence):
    if not OUTLOOK.search(sentence) or CONDITIONAL.search(sentence) or HISTORICAL.search(sentence):
        return None
    directions = set()
    for clause in re.split("[，,；;]", sentence):
        if OUTLOOK.search(clause) and PRICE.search(clause) and not NONPRICE.search(clause):
            directions.update(k for k, pattern in DIRECTIONS.items() if pattern.search(clause))
    return directions.pop() if len(directions) == 1 else None


def extract(row):
    """Return an attributed quotation, or leave the publisher link for manual inspection."""
    symbol = row["symbol"]
    text = row["excerpt"]
    if symbol not in SUBJECTS or not isinstance(text, str) or not 0 < len(text) <= 4000:
        return None
    report = report_from_source(row)
    if not report or not row["locator"]:
        return None
    own, other = SUBJECTS[symbol]
    in_scope = True  # upstream located a single-product section
    eligible = []
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
        sentence = match.group().strip()
        if re.search(other, sentence, re.I):
            in_scope = False
            continue
        named = re.search(own, sentence, re.I)
        if named:
            in_scope = True
        if (
            in_scope
            and 4 <= len(sentence) <= 110
            and (named or PRICE.search(sentence) or OBSERVE.search(sentence))
        ):
            eligible.append((sentence, _bias(sentence)))
    if not eligible:
        return None
    calls = [(s, b) for s, b in eligible if b]
    stance = "unclassified"
    if calls and len({bias for _, bias in calls}) == 1:
        anchor, stance = calls[0]
    elif calls:
        start = text.index(calls[0][0])
        end = text.rindex(calls[-1][0]) + len(calls[-1][0])
        if end - start > 110:
            return None
        anchor = text[start:end]
    else:
        anchor = next((s for s, _ in eligible if OUTLOOK.search(s)), eligible[0][0])
    horizons = list(dict.fromkeys(HORIZON.findall(anchor)))
    condition = next((s for s, _ in eligible if OBSERVE.search(s) and s != anchor and len(s) <= 75), "")
    return View(report, stance, "、".join(horizons), anchor, condition, anchor, row["locator"])
