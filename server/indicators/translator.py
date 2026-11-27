"""Translate free-form indicator descriptions into the Prism indicator DSL."""

import json
import logging
import re

from .dsl import validate

log = logging.getLogger("prism.indicators")

_SCHEMA = """
你是量化指标翻译器。把用户的自然语言、通达信/同花顺风格公式、或简单口语描述
翻译成 Prism 指标 DSL。只输出 JSON，不要解释。

输出 schema:
{
  "name": "指标名",
  "kind": "plot|signal",
  "description": "一句话说明",
  "dsl": <表达式节点>,
  "params": {"items":[{"key":"window","label":"周期","value":20,"min":2,"max":250,"step":1,"unit":"日"}]},
  "plan": {"universe":"watchlist","period":"1d","lookback":120,"trigger_window":5,"combine":"all","score":"recent_signal","direction":"desc","markets":["SH","SZ","BJ"],"boards":["main","gem","star","bse"],"exclude_st":true,"min_turnover":0,"max_results":100,"steps":[],"risk_controls":[]},
  "visual": {"style":"signal_track|overlay|zone","color":"#60a5fa","marker":"▲","track":true,"zone":false}
}

DSL 节点必须是只含一个 key 的 dict:
基础指标: {"ma":N}, {"ema":N}, {"vol":0}, {"amount":0}, {"ma_vol":N},
{"price":"close|open|high|low"}, {"rsi":N}, {"kdj_k":N},
{"boll_upper":[N,K]}, {"boll_mid":N}, {"boll_lower":[N,K]},
{"macd_dif":[F,S]}, {"macd_dea":[F,S,SIG]}
基本面: {"fund":"pe|pb|roe|revenue_growth|market_cap|dividend_yield"}
常量: {"const":2}; 参数: {"param":["key", 默认值]}
算术: {"add":[a,b]}, {"sub":[a,b]}, {"mul":[a,b]}, {"div":[a,b]}
比较: {"gt":[a,b]}, {"lt":[a,b]}, {"ge":[a,b]}, {"le":[a,b]}, {"eq":[a,b]}
逻辑: {"and":[a,b]}, {"or":[a,b]}, {"not":[a]}
时序: {"cross_up":[a,b]}, {"cross_down":[a,b]}, {"highest":[a,N]},
{"lowest":[a,N]}, {"ref":[a,N]}, {"pct_change":[a,N]}, {"slope":[a,N]},
{"count":[cond,N]}, {"within":[cond,N]}, {"range_pct":[a,N]}

kind=plot 表示画线；kind=signal 表示触发条件。优先使用 param 节点，让用户可调。
“前 N 日均量”必须排除触发当日，使用 {"ref":[{"ma_vol":N},1]}；多个条件必须用嵌套 {"and":[a,b]} 合成一个 signal。
"""

_EXAMPLES = """
输入「MACD金叉」:
{"name":"MACD金叉","kind":"signal","description":"DIF 上穿 DEA","dsl":{"cross_up":[{"macd_dif":[{"param":["fast",12]},{"param":["slow",26]}]},{"macd_dea":[{"param":["fast",12]},{"param":["slow",26]},{"param":["signal",9]}]}]},"params":{"items":[{"key":"fast","label":"快线 EMA","value":12,"min":3,"max":30,"step":1},{"key":"slow","label":"慢线 EMA","value":26,"min":5,"max":80,"step":1},{"key":"signal","label":"信号线","value":9,"min":3,"max":30,"step":1}]},"plan":{},"visual":{"style":"signal_track","color":"#60a5fa","marker":"▲","track":true,"zone":false}}

输入「5日线上穿20日线且放量2倍」:
{"name":"放量均线金叉","kind":"signal","description":"短均线上穿长均线且成交量放大","dsl":{"and":[{"cross_up":[{"ma":{"param":["fast",5]}},{"ma":{"param":["slow",20]}}]},{"gt":[{"vol":0},{"mul":[{"param":["multiplier",2]},{"ref":[{"ma_vol":{"param":["vol_window",5]}},1]}]}]}]},"params":{"items":[{"key":"fast","label":"短均线","value":5,"min":2,"max":60,"step":1},{"key":"slow","label":"长均线","value":20,"min":5,"max":250,"step":1},{"key":"vol_window","label":"前序均量窗口","value":5,"min":2,"max":60,"step":1},{"key":"multiplier","label":"放量倍数","value":2,"min":1,"max":10,"step":0.1,"unit":"x"}]},"plan":{},"visual":{"style":"signal_track","color":"#22c55e","marker":"▲","track":true,"zone":false}}

输入「5日线上穿10日线，10日线上穿20日线，成交量大于前五日均量3倍」:
{"name":"多级均线放量共振(5/10/20)","kind":"signal","description":"两次均线上穿与前序量能同时确认","dsl":{"and":[{"and":[{"within":[{"cross_up":[{"ma":{"param":["fast",5]}},{"ma":{"param":["mid",10]}}]},{"param":["sync_window",1]}]},{"within":[{"cross_up":[{"ma":{"param":["mid",10]}},{"ma":{"param":["slow",20]}}]},{"param":["sync_window",1]}]}]},{"gt":[{"vol":0},{"mul":[{"param":["multiplier",3]},{"ref":[{"ma_vol":{"param":["baseline_window",5]}},1]}]}]}]},"params":{"items":[{"key":"fast","label":"短期均线","value":5,"min":2,"max":30,"step":1},{"key":"mid","label":"中期均线","value":10,"min":3,"max":120,"step":1},{"key":"slow","label":"长期均线","value":20,"min":5,"max":250,"step":1},{"key":"sync_window","label":"交叉共振窗口","value":1,"min":1,"max":10,"step":1},{"key":"baseline_window","label":"前序均量窗口","value":5,"min":2,"max":60,"step":1},{"key":"multiplier","label":"放量倍数","value":3,"min":1,"max":10,"step":0.1,"unit":"x"}]},"plan":{},"visual":{"style":"signal_track","color":"#d6332f","marker":"▲","track":true,"zone":false}}
"""


def _param(key, label, value, min_v, max_v, step=1, unit=""):
    return {
        "key": key,
        "label": label,
        "value": value,
        "min": min_v,
        "max": max_v,
        "step": step,
        "unit": unit,
    }


def _base_plan(kind="signal", **extra):
    plan = {
        "universe": "watchlist",
        "period": "1d",
        "lookback": 120,
        "trigger_window": 5,
        "combine": "all",
        "score": "recent_signal" if kind == "signal" else "latest_value",
        "direction": "desc",
        "markets": ["SH", "SZ", "BJ"],
        "boards": ["main", "gem", "star", "bse"],
        "exclude_st": True,
        "min_turnover": 0,
        "max_results": 100,
        "steps": [
            "先在当前 K 线验证触发点",
            "再用自选股扫描最近 5 根 K 线",
            "最后结合成交额、基本面和题材位置复核",
        ],
        "risk_controls": [
            "技术信号只作为筛选线索，不直接替代交易决策",
            "对流动性不足、财报异常和停牌数据保持谨慎",
        ],
    }
    plan.update(extra)
    return plan


def _visual(style="signal_track", color="#60a5fa", marker="▲"):
    return {
        "style": style,
        "color": color,
        "marker": marker,
        "track": style in {"signal_track", "zone"},
        "zone": style == "zone",
    }


def _param_values(params):
    if not isinstance(params, dict):
        return {}
    out = {}
    for item in params.get("items", []):
        if isinstance(item, dict) and item.get("key"):
            out[item["key"]] = item.get("value")
    return out


def _out(name, kind, description, dsl, params=None, plan=None, visual=None, source="rule"):
    params = params or {}
    if not validate(dsl, _param_values(params)):
        return None
    return {
        "name": name,
        "kind": kind,
        "description": description,
        "dsl": dsl,
        "params": params,
        "plan": plan or _base_plan(kind),
        "visual": visual or _visual("signal_track" if kind == "signal" else "overlay"),
        "source": source,
    }


def _ma_numbers(raw, compact):
    nums = [int(x) for x in re.findall(r"MA\s*\(\s*(?:CLOSE|C)\s*,\s*(\d+)\s*\)", compact)]
    nums += [int(x) for x in re.findall(r"\bMA\s*(\d+)\b", compact)]
    nums += [int(x) for x in re.findall(r"(\d+)\s*(?:日|天)?(?:均线|线)", raw)]
    seen = []
    for n in nums:
        if 1 < n <= 300 and n not in seen:
            seen.append(n)
    return seen


_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _small_number(token, default=None):
    text = str(token or "").strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    return _CN_DIGITS.get(text, default)


def _volume_terms(raw):
    upper = raw.upper()
    present = any(word in raw for word in ("成交量", "放量", "量能", "倍量")) or "VOL" in upper
    if not present:
        return False, 5, 2.0

    number_token = r"[0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]+"
    multiplier = 2.0
    multiples = re.findall(rf"({number_token})\s*倍", raw, re.I)
    if multiples:
        multiplier = float(_small_number(multiples[-1], 2.0))

    baseline_window = 5
    patterns = [
        rf"(?:前|近|最近|过去)\s*({number_token})\s*(?:日|天)",
        rf"({number_token})\s*(?:日|天)(?:均量|平均(?:成交)?量)",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw, re.I)
        if match:
            baseline_window = int(_small_number(match.group(1), 5))
            break
    return True, max(2, min(60, baseline_window)), max(1.0, min(10.0, multiplier))


def _ma_cross(left_key, left, right_key, right, sync_window=1):
    return {
        "within": [
            {
                "cross_up": [
                    {"ma": {"param": [left_key, left]}},
                    {"ma": {"param": [right_key, right]}},
                ]
            },
            {"param": ["sync_window", sync_window]},
        ]
    }


def _prior_volume_condition(window, multiplier):
    return {
        "gt": [
            {"vol": 0},
            {
                "mul": [
                    {"param": ["multiplier", multiplier]},
                    {
                        "ref": [
                            {"ma_vol": {"param": ["baseline_window", window]}},
                            1,
                        ]
                    },
                ]
            },
        ]
    }


def _rule_translate(user_input: str) -> dict | None:
    raw = (user_input or "").strip()
    upper = raw.upper()
    compact = re.sub(r"\s+", "", upper)

    if (
        "MACD" in compact
        and any(x in raw for x in ("金叉", "上穿", "上叉"))
        or "MACD" in compact
        and "CROSS" in compact
    ):
        params = {
            "items": [
                _param("fast", "快线 EMA", 12, 3, 30, 1, "日"),
                _param("slow", "慢线 EMA", 26, 5, 80, 1, "日"),
                _param("signal", "信号线", 9, 3, 30, 1, "日"),
            ]
        }
        dsl = {
            "cross_up": [
                {"macd_dif": [{"param": ["fast", 12]}, {"param": ["slow", 26]}]},
                {"macd_dea": [{"param": ["fast", 12]}, {"param": ["slow", 26]}, {"param": ["signal", 9]}]},
            ]
        }
        return _out(
            "MACD金叉",
            "signal",
            "DIF 上穿 DEA",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#60a5fa"),
        )

    ma_nums = _ma_numbers(raw, compact)
    wants_cross = (
        any(x in raw for x in ("金叉", "上穿", "上叉")) or "CROSS" in compact or "CROSSOVER" in compact
    )
    has_volume, baseline_window, volume_multiplier = _volume_terms(raw)
    if len(ma_nums) >= 3 and wants_cross:
        fast, mid, slow = sorted(ma_nums[:3])
        params = {
            "items": [
                _param("fast", "短期均线", fast, 2, 30, 1, "日"),
                _param("mid", "中期均线", mid, 3, 120, 1, "日"),
                _param("slow", "长期均线", slow, 5, 250, 1, "日"),
                _param("sync_window", "交叉共振窗口", 1, 1, 10, 1, "日"),
            ]
        }
        cross_pair = {
            "and": [
                _ma_cross("fast", fast, "mid", mid),
                _ma_cross("mid", mid, "slow", slow),
            ]
        }
        if has_volume:
            params["items"].extend(
                [
                    _param("baseline_window", "前序均量窗口", baseline_window, 2, 60, 1, "日"),
                    _param("multiplier", "放量倍数", volume_multiplier, 1, 10, 0.1, "x"),
                ]
            )
            dsl = {"and": [cross_pair, _prior_volume_condition(baseline_window, volume_multiplier)]}
            return _out(
                f"多级均线放量共振({fast}/{mid}/{slow})",
                "signal",
                "两次均线上穿与前序量能同时确认",
                dsl,
                params,
                _base_plan("signal", universe="market", lookback=180),
                _visual("signal_track", "#d6332f"),
            )
        return _out(
            f"多级均线共振({fast}/{mid}/{slow})",
            "signal",
            "短中长期均线依次上穿",
            cross_pair,
            params,
            _base_plan("signal", universe="market", lookback=180),
            _visual("signal_track", "#d6332f"),
        )

    if len(ma_nums) >= 2 and wants_cross:
        fast, slow = sorted(ma_nums[:2])
        params = {
            "items": [
                _param("fast", "短均线", fast, 2, 60, 1, "日"),
                _param("slow", "长均线", slow, 5, 250, 1, "日"),
            ]
        }
        dsl = {"cross_up": [{"ma": {"param": ["fast", fast]}}, {"ma": {"param": ["slow", slow]}}]}
        if has_volume:
            mult = volume_multiplier
            params["items"].extend(
                [
                    _param("vol_window", "均量窗口", baseline_window, 2, 60, 1, "日"),
                    _param("multiplier", "放量倍数", mult, 1, 10, 0.1, "x"),
                ]
            )
            dsl = {
                "and": [
                    dsl,
                    {
                        "gt": [
                            {"vol": 0},
                            {
                                "mul": [
                                    {"param": ["multiplier", mult]},
                                    {"ref": [{"ma_vol": {"param": ["vol_window", baseline_window]}}, 1]},
                                ]
                            },
                        ]
                    },
                ]
            }
            return _out(
                "放量均线金叉",
                "signal",
                "短均线上穿长均线且成交量放大",
                dsl,
                params,
                _base_plan("signal"),
                _visual("signal_track", "#22c55e"),
            )
        return _out(
            f"均线金叉({fast}/{slow})",
            "signal",
            "短均线上穿长均线",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#22c55e"),
        )

    if ma_nums and any(x in raw for x in ("站上", "突破", "高于")) or ma_nums and ">" in compact:
        n = ma_nums[0]
        params = {"items": [_param("window", "均线周期", n, 2, 250, 1, "日")]}
        dsl = {"gt": [{"price": "close"}, {"ma": {"param": ["window", n]}}]}
        return _out(
            f"收盘站上MA{n}",
            "signal",
            "收盘价高于指定均线",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#34d399"),
        )

    rsi = re.search(r"RSI\s*\(?\s*(\d+)?\s*\)?", compact)
    if rsi:
        n = int(rsi.group(1) or 14)
        threshold = 30 if any(x in raw for x in ("超卖", "低于", "小于")) or "<" in compact else 50
        params = {
            "items": [
                _param("window", "RSI周期", n, 2, 60, 1, "日"),
                _param("threshold", "阈值", threshold, 5, 95, 1),
            ]
        }
        if any(x in raw for x in ("上穿", "反弹", "金叉")) or "CROSS" in compact:
            dsl = {"cross_up": [{"rsi": {"param": ["window", n]}}, {"param": ["threshold", threshold]}]}
            name, desc = "RSI反弹", "RSI 上穿阈值"
        else:
            dsl = {"lt": [{"rsi": {"param": ["window", n]}}, {"param": ["threshold", threshold]}]}
            name, desc = "RSI低位", "RSI 低于阈值"
        return _out(
            name, "signal", desc, dsl, params, _base_plan("signal"), _visual("signal_track", "#f59e0b")
        )

    if has_volume or "MA(VOL" in compact:
        mult = volume_multiplier
        params = {
            "items": [
                _param("vol_window", "均量窗口", baseline_window, 2, 60, 1, "日"),
                _param("multiplier", "放量倍数", mult, 1, 10, 0.1, "x"),
            ]
        }
        dsl = {
            "gt": [
                {"vol": 0},
                {
                    "mul": [
                        {"param": ["multiplier", mult]},
                        {"ref": [{"ma_vol": {"param": ["vol_window", baseline_window]}}, 1]},
                    ]
                },
            ]
        }
        return _out(
            "放量突破",
            "signal",
            "成交量大于指定均量倍数",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#ef4444"),
        )

    high = re.search(r"(?:突破|新高|最高).{0,8}?(\d+)", raw)
    if high:
        n = int(high.group(1))
        params = {"items": [_param("window", "突破窗口", n, 5, 250, 1, "日")]}
        dsl = {"ge": [{"price": "close"}, {"highest": [{"price": "close"}, {"param": ["window", n]}]}]}
        return _out(
            f"{n}日收盘新高",
            "signal",
            "收盘价接近或突破窗口新高",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#8b5cf6"),
        )

    pct = re.search(r"(?:涨幅|收益|PCT).{0,8}?(\d+(?:\.\d+)?)\s*%", raw, re.I)
    if pct:
        threshold = float(pct.group(1))
        params = {
            "items": [
                _param("window", "回看窗口", 20, 2, 120, 1, "日"),
                _param("threshold", "涨幅阈值", threshold, 1, 80, 0.5, "%"),
            ]
        }
        dsl = {
            "gt": [
                {"pct_change": [{"price": "close"}, {"param": ["window", 20]}]},
                {"param": ["threshold", threshold]},
            ]
        }
        return _out(
            "阶段涨幅突破",
            "signal",
            "指定窗口涨幅超过阈值",
            dsl,
            params,
            _base_plan("signal"),
            _visual("signal_track", "#0ea5e9"),
        )

    if "BOLL" in compact or "布林" in raw:
        params = {
            "items": [
                _param("window", "布林周期", 20, 5, 120, 1, "日"),
                _param("std", "标准差倍数", 2, 1, 4, 0.1),
            ]
        }
        dsl = {"boll_upper": [{"param": ["window", 20]}, {"param": ["std", 2]}]}
        return _out(
            "BOLL上轨",
            "plot",
            "布林带上轨",
            dsl,
            params,
            _base_plan("plot"),
            _visual("overlay", "#f59e0b", ""),
        )

    return None


def translate(user_input: str) -> dict | None:
    """Translate free text into a validated indicator definition."""
    rule = _rule_translate(user_input)
    if rule:
        return rule

    from server.services.auxiliary_model import run_text

    prompt = f"{_SCHEMA}\n{_EXAMPLES}\n输入「{user_input}」:"
    try:
        output = run_text(prompt, timeout=40, output_format="json")
    except Exception as exc:
        log.warning("indicator translation unavailable (%s)", type(exc).__name__)
        return None

    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    dsl = data.get("dsl")
    kind = data.get("kind")
    if not isinstance(dsl, dict) or kind not in ("plot", "signal"):
        return None
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    if not validate(dsl, _param_values(params)):
        log.warning("translated DSL failed validation: %r", dsl)
        return None
    return {
        "name": (data.get("name") or user_input[:24]).strip(),
        "kind": kind,
        "description": (data.get("description") or "").strip(),
        "dsl": dsl,
        "params": params,
        "plan": data.get("plan") if isinstance(data.get("plan"), dict) else _base_plan(kind),
        "visual": data.get("visual")
        if isinstance(data.get("visual"), dict)
        else _visual("signal_track" if kind == "signal" else "overlay"),
        "source": "llm",
    }
