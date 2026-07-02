"""LLM translation: arbitrary indicator description -> validated DSL JSON."""
import logging
from server.analysis_engine import _parse_json_lenient  # reuse lenient JSON parser
from .dsl import DSLError, validate

log = logging.getLogger("prism.indicators")

_SCHEMA = (
    "你是量化指标翻译器。把用户的自然语言/公式描述翻译成结构化DSL JSON。"
    "严格只输出JSON，schema：{\"name\":\"指标名\",\"kind\":\"plot|signal\","
    "\"description\":\"一句话说明\",\"dsl\":<表达式节点>}。\n"
    "DSL规则（节点是只含一个键的dict）：\n"
    "基指标：{\"ma\":N} {\"ema\":N} {\"vol\":0} {\"ma_vol\":N} {\"price\":\"close|open|high|low\"} "
    "{\"rsi\":N} {\"kdj_k\":N} {\"boll_upper\":[N,K]} {\"boll_mid\":N} {\"boll_lower\":[N,K]} "
    "{\"macd_dif\":[F,S]} {\"macd_dea\":[F,S,SIG]}\n"
    "基本面(标量)：{\"fund\":\"pe|pb|roe|revenue_growth|market_cap|dividend_yield\"}\n"
    "常量：{\"const\":数值}\n"
    "算术：{\"add\":[a,b]} {\"sub\":[a,b]} {\"mul\":[a,b]} {\"div\":[a,b]}\n"
    "比较(产生布尔)：{\"gt\":[a,b]} {\"lt\":[a,b]} {\"ge\":[a,b]} {\"le\":[a,b]} {\"eq\":[a,b]}\n"
    "逻辑：{\"and\":[a,b]} {\"or\":[a,b]} {\"not\":[a]}\n"
    "时序事件：{\"cross_up\":[a,b]}(a上穿b) {\"cross_down\":[a,b]} {\"highest\":[a,N]} {\"lowest\":[a,N]} {\"ref\":[a,N]}\n"
    "kind=plot 表示画线类(返回数值序列)；kind=signal 表示条件/触发类(返回布尔)。\n"
    "只输出JSON，不要额外文字。"
)

_EXAMPLES = (
    "示例：\n"
    "输入『MACD金叉』→ {\"name\":\"MACD金叉\",\"kind\":\"signal\",\"description\":\"DIF上穿DEA\","
    "\"dsl\":{\"cross_up\":[{\"macd_dif\":[12,26]},{\"macd_dea\":[12,26,9]}]}}\n"
    "输入『5日线上穿20日线且量能放大2倍』→ {\"name\":\"放量金叉\",\"kind\":\"signal\","
    "\"description\":\"均线金叉且成交量放大\",\"dsl\":{\"and\":[{\"cross_up\":[{\"ma\":5},{\"ma\":20}]},"
    "{\"gt\":[{\"vol\":0},{\"mul\":[2,{\"ma_vol\":5}]}]}]}}\n"
    "输入『布林带上轨』→ {\"name\":\"BOLL上轨\",\"kind\":\"plot\",\"description\":\"20日布林上轨\","
    "\"dsl\":{\"boll_upper\":[20,2]}}\n"
    "输入『RSI14』→ {\"name\":\"RSI14\",\"kind\":\"plot\",\"description\":\"14日RSI\",\"dsl\":{\"rsi\":14}}\n"
)


def translate(user_input: str) -> dict:
    """Translate free-text → {name, kind, description, dsl}, validated.

    Returns None on any failure (LLM error, unparseable, or DSL fails the
    whitelist dry-run). The caller then asks the user to rephrase/edit.
    """
    import subprocess
    from server.config import CLAUDE_BIN

    prompt = _SCHEMA + "\n" + _EXAMPLES + "\n输入『" + user_input + "』→"
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt,
             "--model", "minimax-m2.7-fast",
             "--max-tokens", "800",
             "--output-format", "json"],
            capture_output=True, timeout=40,
        )
    except Exception as exc:
        log.warning("indicator translate subprocess failed: %s", exc)
        return None

    data = _parse_json_lenient(proc.stdout.decode("utf-8", errors="replace").strip())
    if not isinstance(data, dict):
        return None
    dsl = data.get("dsl")
    kind = data.get("kind")
    if not isinstance(dsl, dict) or kind not in ("plot", "signal"):
        return None
    # Whitelist dry-run — reject anything that won't evaluate.
    if not validate(dsl):
        log.warning("translated DSL failed validation: %r", dsl)
        return None
    return {
        "name": (data.get("name") or user_input[:24]).strip(),
        "kind": kind,
        "description": (data.get("description") or "").strip(),
        "dsl": dsl,
    }
