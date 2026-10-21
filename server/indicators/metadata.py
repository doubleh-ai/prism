"""Semantic UI metadata for configurable indicators.

The calculation DSL only needs parameter values.  The workbench needs more
context so a period, threshold and multiplier do not all become the same
anonymous slider.  This module enriches both new definitions and legacy rows
without changing the stored DSL contract.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any


_PARAM_SPECS: dict[str, dict[str, Any]] = {
    "fast": {
        "control": "period",
        "group": "计算周期",
        "tone": "trend",
        "help": "短周期越小，信号越灵敏，也更容易出现噪声。",
        "quick_values": [3, 5, 8, 10, 12, 20],
    },
    "slow": {
        "control": "period",
        "group": "计算周期",
        "tone": "trend",
        "help": "长周期越大，趋势过滤越稳健，但信号会更滞后。",
        "quick_values": [10, 20, 26, 30, 60, 120],
    },
    "mid": {
        "control": "period",
        "group": "计算周期",
        "tone": "trend",
        "help": "中期均线连接短期与长期趋势，建议保持短期 < 中期 < 长期。",
        "quick_values": [5, 10, 20, 30, 60],
    },
    "sync_window": {
        "control": "period",
        "group": "共振规则",
        "tone": "trend",
        "help": "两次均线上穿允许相隔的交易日数；1 表示必须同日发生。",
        "quick_values": [1, 2, 3, 5, 10],
    },
    "baseline_window": {
        "control": "period",
        "group": "量能基准",
        "tone": "volume",
        "help": "计算触发日前多少个完整交易日的平均成交量，不包含当天。",
        "quick_values": [3, 5, 10, 20, 30, 60],
    },
    "signal": {
        "control": "period",
        "group": "信号平滑",
        "tone": "momentum",
        "help": "用于平滑 MACD 信号线；数值越小，交叉反应越快。",
        "quick_values": [5, 7, 9, 12, 15],
    },
    "window": {
        "control": "period",
        "group": "计算周期",
        "tone": "trend",
        "help": "决定指标参考多少根 K 线；短周期更敏感，长周期更平滑。",
        "quick_values": [5, 10, 14, 20, 30, 60],
    },
    "vol_window": {
        "control": "period",
        "group": "量能基准",
        "tone": "volume",
        "help": "用最近多少个交易日的平均成交量作为放量基准。",
        "quick_values": [3, 5, 10, 20, 30, 60],
    },
    "threshold": {
        "control": "threshold",
        "group": "触发门槛",
        "tone": "momentum",
        "help": "只有越过该边界才触发；门槛越极端，信号通常越少。",
    },
    "multiplier": {
        "control": "ratio",
        "group": "量能门槛",
        "tone": "volume",
        "help": "当前成交量相对均量的倍数；越高代表放量要求越严格。",
        "quick_values": [1.2, 1.5, 2, 2.5, 3],
    },
    "std": {
        "control": "ratio",
        "group": "波动带宽",
        "tone": "volatility",
        "help": "布林带使用的标准差倍数；越大，上下轨距离中轨越远。",
        "quick_values": [1, 1.5, 2, 2.5, 3],
    },
}


def _dsl_text(dsl: Any) -> str:
    try:
        return json.dumps(dsl or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(dsl or "")


def _root_op(dsl: Any) -> str:
    if isinstance(dsl, dict) and len(dsl) == 1:
        return str(next(iter(dsl)))
    return ""


def _family(name: str, dsl: Any, keys: set[str]) -> str:
    text = f"{name} {_dsl_text(dsl)}".lower()
    if {"fast", "mid", "slow", "sync_window", "baseline_window", "multiplier"}.issubset(keys):
        return "ma_stack_volume"
    if {"fast", "mid", "slow", "sync_window"}.issubset(keys):
        return "ma_stack"
    if {"fast", "slow", "vol_window", "multiplier"}.issubset(keys):
        return "ma_volume_cross"
    if "macd" in text:
        return "macd"
    if {"fast", "slow"}.issubset(keys):
        return "ma_cross"
    if "rsi" in text:
        return "rsi"
    if {"vol_window", "multiplier"}.issubset(keys):
        return "volume_breakout"
    if "boll" in text or {"window", "std"}.issubset(keys):
        return "bollinger"
    if "pct_change" in text:
        return "momentum"
    if "highest" in text or "lowest" in text:
        return "price_breakout"
    return "custom"


_FAMILY_LABELS = {
    "ma_stack_volume": "多级均线 + 前序量能",
    "ma_stack": "多级均线共振",
    "ma_volume_cross": "趋势 + 量能",
    "ma_cross": "双均线趋势",
    "macd": "MACD 动量",
    "rsi": "RSI 超买超卖",
    "volume_breakout": "成交量异动",
    "bollinger": "布林波动率",
    "momentum": "价格动量",
    "price_breakout": "价格突破",
    "custom": "自定义逻辑",
}


def _logic_template(family: str, root: str, kind: str, description: str) -> str:
    if family == "ma_stack_volume":
        return "在 {sync_window} 个交易日内 MA{fast} 上穿 MA{mid} 且 MA{mid} 上穿 MA{slow}，并且当日成交量 > 前 {baseline_window} 日均量 x {multiplier}；三个条件全部满足才触发"
    if family == "ma_stack":
        return "在 {sync_window} 个交易日内 MA{fast} 上穿 MA{mid} 且 MA{mid} 上穿 MA{slow}；两个条件全部满足才触发"
    if family == "ma_volume_cross":
        direction = "下穿" if root == "cross_down" else "上穿"
        return f"MA{{fast}} {direction} MA{{slow}}，且成交量超过 {{vol_window}} 日均量的 {{multiplier}} 倍时触发"
    if family == "ma_cross":
        direction = "下穿" if root == "cross_down" else "上穿"
        return f"MA{{fast}} {direction} MA{{slow}} 时触发"
    if family == "macd":
        if kind == "signal":
            return "DIF(EMA{fast} - EMA{slow}) 上穿 DEA({signal}) 时触发"
        return "绘制 DIF：EMA{fast} - EMA{slow}"
    if family == "rsi":
        action = "上穿" if root == "cross_up" else "低于"
        return f"RSI({{window}}) {action} {{threshold}} 时触发"
    if family == "volume_breakout":
        return "成交量超过 {vol_window} 日均量的 {multiplier} 倍时触发"
    if family == "bollinger":
        return "绘制布林上轨：MA{window} + {std}σ"
    if family == "momentum":
        return "{window} 日涨幅超过 {threshold}% 时触发"
    if family == "price_breakout":
        return "收盘价突破最近 {window} 日价格区间时触发"
    return description or "调整参数后，计算逻辑会同步更新"


def _preset(label: str, note: str, **values: float) -> dict[str, Any]:
    return {"label": label, "note": note, "values": values}


def _presets(family: str, keys: set[str]) -> list[dict[str, Any]]:
    candidates: dict[str, list[dict[str, Any]]] = {
        "ma_stack_volume": [
            _preset("同日严格", "两次上穿必须同日且成交量显著放大", fast=5, mid=10, slow=20, sync_window=1, baseline_window=5, multiplier=3),
            _preset("三日共振", "允许两次上穿在三个交易日内先后完成", fast=5, mid=10, slow=20, sync_window=3, baseline_window=5, multiplier=2),
            _preset("中期趋势", "使用更慢均线过滤短期噪声", fast=10, mid=20, slow=60, sync_window=5, baseline_window=10, multiplier=2),
        ],
        "ma_stack": [
            _preset("同日", "两次上穿必须在同一个交易日发生", fast=5, mid=10, slow=20, sync_window=1),
            _preset("三日", "允许两次上穿在三个交易日内完成", fast=5, mid=10, slow=20, sync_window=3),
            _preset("中期", "使用更慢均线过滤短期噪声", fast=10, mid=20, slow=60, sync_window=5),
        ],
        "ma_cross": [
            _preset("灵敏", "更快捕捉短线转折", fast=3, slow=10),
            _preset("经典", "常用的波段组合", fast=5, slow=20),
            _preset("稳健", "过滤更多短期噪声", fast=20, slow=60),
        ],
        "ma_volume_cross": [
            _preset("短线", "快速趋势与温和放量", fast=3, slow=10, vol_window=5, multiplier=1.5),
            _preset("标准", "经典趋势与明确放量", fast=5, slow=20, vol_window=5, multiplier=2),
            _preset("严格", "中期趋势与强放量", fast=10, slow=60, vol_window=20, multiplier=3),
        ],
        "rsi": [
            _preset("激进", "更早捕捉超卖反弹", window=6, threshold=35),
            _preset("经典", "常用 RSI 超卖设置", window=14, threshold=30),
            _preset("严格", "只关注更极端的超卖", window=21, threshold=25),
        ],
        "volume_breakout": [
            _preset("温和", "捕捉初步量能变化", vol_window=5, multiplier=1.5),
            _preset("标准", "常用放量确认", vol_window=5, multiplier=2),
            _preset("异常", "只保留显著放量", vol_window=20, multiplier=3),
        ],
        "macd": [
            _preset("快速", "缩短周期提高灵敏度", fast=6, slow=13, signal=5),
            _preset("经典", "标准 MACD 参数", fast=12, slow=26, signal=9),
            _preset("稳健", "降低短期波动影响", fast=19, slow=39, signal=9),
        ],
        "bollinger": [
            _preset("紧凑", "对短期波动更敏感", window=10, std=1.5),
            _preset("经典", "标准布林带设置", window=20, std=2),
            _preset("宽松", "减少触轨频率", window=40, std=2.5),
        ],
    }
    presets = []
    for item in candidates.get(family, []):
        values = {k: v for k, v in item["values"].items() if k in keys}
        if values:
            presets.append({**item, "values": values})
    return presets


def _infer_spec(item: dict[str, Any]) -> dict[str, Any]:
    key = str(item.get("key") or "")
    spec = dict(_PARAM_SPECS.get(key, {}))
    lower = key.lower()
    if not spec:
        if any(part in lower for part in ("period", "window", "lookback", "length")):
            spec = dict(_PARAM_SPECS["window"])
        elif any(part in lower for part in ("threshold", "limit", "level")):
            spec = dict(_PARAM_SPECS["threshold"])
        elif any(part in lower for part in ("ratio", "multiple", "multiplier", "std")):
            spec = dict(_PARAM_SPECS["multiplier"])
        else:
            spec = {
                "control": "number",
                "group": "其他参数",
                "tone": "neutral",
                "help": "直接调整该参数的计算值。",
            }
    quick = []
    for value in spec.get("quick_values", []):
        if item.get("min") is not None and value < item["min"]:
            continue
        if item.get("max") is not None and value > item["max"]:
            continue
        quick.append(value)
    spec["quick_values"] = quick
    return spec


def enrich_params(
    params: dict[str, Any] | None,
    *,
    name: str = "",
    dsl: Any = None,
    kind: str = "signal",
    description: str = "",
) -> dict[str, Any]:
    """Return a UI-rich copy of a parameter definition.

    Existing metadata wins, so user-created or future controls remain forward
    compatible.  Legacy rows receive inferred metadata at response time.
    """
    out = deepcopy(params) if isinstance(params, dict) else {}
    raw_items = out.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    enriched_items = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("key"):
            continue
        item = deepcopy(raw)
        spec = _infer_spec(item)
        for key, value in spec.items():
            item.setdefault(key, value)
        enriched_items.append(item)
    out["items"] = enriched_items

    keys = {str(item.get("key")) for item in enriched_items if item.get("key")}
    family = _family(name, dsl, keys)
    ui = deepcopy(out.get("ui")) if isinstance(out.get("ui"), dict) else {}
    ui.setdefault("family", family)
    ui.setdefault("family_label", _FAMILY_LABELS.get(family, _FAMILY_LABELS["custom"]))
    ui.setdefault("logic_template", _logic_template(family, _root_op(dsl), kind, description))
    if family == "ma_stack_volume" and not isinstance(ui.get("conditions"), list):
        ui["conditions"] = [
            "MA{fast} 上穿 MA{mid}（最近 {sync_window} 日内）",
            "MA{mid} 上穿 MA{slow}（最近 {sync_window} 日内）",
            "当日成交量 > 前 {baseline_window} 日均量 x {multiplier}",
        ]
    if family == "ma_stack" and not isinstance(ui.get("conditions"), list):
        ui["conditions"] = [
            "MA{fast} 上穿 MA{mid}（最近 {sync_window} 日内）",
            "MA{mid} 上穿 MA{slow}（最近 {sync_window} 日内）",
        ]
    if not isinstance(ui.get("presets"), list):
        ui["presets"] = _presets(family, keys)
    out["ui"] = ui
    return out


def enrich_indicator(indicator: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(indicator)
    out["params"] = enrich_params(
        out.get("params"),
        name=str(out.get("name") or ""),
        dsl=out.get("dsl"),
        kind=str(out.get("kind") or "signal"),
        description=str(out.get("description") or ""),
    )
    return out
