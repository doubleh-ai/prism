"""Ontology-backed research and falsifiable judgment verification for the remaining 42 futures varieties.

Preserves the exact ``analyze(question, cutoff)`` and ``review(choice, cutoff)`` contract used by
``futures_journal.py`` so every product in the 46-variety workspace supports point-in-time question
verification, sealed baseline saving and subsequent automatic or manual reconciliation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import futures, futures_basis, futures_inventory, futures_ontology

SHANGHAI = ZoneInfo("Asia/Shanghai")
RULE_VERSION = "ontology-daily-v1"
FORBIDDEN_WORDS = ("买入", "卖出", "做多", "做空", "目标价", "止损", "满仓", "加仓", "抄底", "摸顶", "杠杆")


def _outcome(status: str, headline: str, explanation: str, evidence: list[dict] | None = None) -> dict:
    return {"status": status, "headline": headline, "explanation": explanation, "evidence": evidence or []}


class GenericOntologyEngine:
    """Point-in-time evidence reader and falsifiable baseline verifier for one futures symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol.strip().upper()

    async def analyze(self, question: str, cutoff: datetime) -> dict:
        if cutoff.utcoffset() is None:
            raise ValueError("cutoff requires a timezone")
        local = cutoff.astimezone(SHANGHAI)
        meta = futures_ontology.get(self.symbol)
        name = futures.PRODUCTS[self.symbol][0]
        base = {
            "question": question,
            "as_of": local.isoformat(),
            "method": f"依据已核对公开日线、现货基差与交易所仓单核验（{meta.archetype_label if meta else '商品期货'}）",
            "choices": [],
            "evidence": [],
        }
        if any(word in question for word in FORBIDDEN_WORDS):
            return {
                **base,
                "status": "unsupported",
                "headline": f"不提供交易指令；可核对{name}期现基差、仓单与趋势位置",
                "explanation": f"例如：{meta.presets[0][1]}"
                if meta and meta.presets
                else f"例如：{name}当前现货升贴水如何？",
            }

        evidence: list[dict] = []
        choices: list[dict] = []
        parts: list[str] = []

        # 1. Read continuous daily market snapshot
        snap = None
        try:
            snap = await futures.snapshot(self.symbol, workspace=True)
        except Exception:
            snap = None
        if snap and snap.get("series_status") == "collected" and snap.get("latest"):
            latest = snap["latest"]
            day = latest.get("date")
            close = latest.get("close")
            unit = futures._QUOTE_UNITS.get(self.symbol, "元/吨")
            src = snap.get("source") or {}
            if day and day <= local.date().isoformat() and isinstance(close, (int, float)):
                parts.append(f"{day} {name}连续序列收盘 {close} {unit}。")
                pct = (snap.get("workspace_percentiles") or {}).get("close_pct_5y")
                span = (snap.get("workspace_percentiles") or {}).get("span_label", "历史")
                if pct is not None:
                    parts.append(f"位于{span} {pct}% 分位。")
                evidence.append(
                    {
                        "label": f"{name}连续合约收盘价",
                        "value": f"{close} {unit}",
                        "data_date": day,
                        "as_of": snap.get("collected_at") or local.isoformat(),
                        "source_name": src.get("name") or "新浪财经",
                        "source_url": src.get("url")
                        or f"https://finance.sina.com.cn/futures/quotes/{self.symbol}0.shtml",
                    }
                )
                target = (date.fromisoformat(day) + timedelta(days=5)).isoformat()
                choices.append(
                    {
                        "id": "close_up",
                        "label": f"下一观测周期{name}收盘高于 {close} {unit}",
                        "rule": f"自 {target} 起首次取得的 {name} 连续收盘价须高于 {day} 的 {close} {unit}。",
                        "baseline": {
                            "version": RULE_VERSION,
                            "symbol": self.symbol,
                            "metric": "close",
                            "value": float(close),
                            "unit": unit,
                            "period": day,
                            "target": target,
                        },
                    }
                )
                choices.append(
                    {
                        "id": "close_down",
                        "label": f"下一观测周期{name}收盘低于 {close} {unit}",
                        "rule": f"自 {target} 起首次取得的 {name} 连续收盘价须低于 {day} 的 {close} {unit}。",
                        "baseline": {
                            "version": RULE_VERSION,
                            "symbol": self.symbol,
                            "metric": "close_down",
                            "value": float(close),
                            "unit": unit,
                            "period": day,
                            "target": target,
                        },
                    }
                )

        # 2. Read spot-futures basis if covered
        try:
            b_obs = await futures_basis.snapshot(self.symbol)
        except Exception:
            b_obs = None
        if (
            b_obs
            and b_obs.get("status") == "collected"
            and b_obs.get("data_as_of", "9999") <= local.date().isoformat()
        ):
            b_day = b_obs["data_as_of"]
            b_val = b_obs["basis"]
            b_unit = b_obs.get("quote_unit") or futures._QUOTE_UNITS.get(self.symbol, "元/吨")
            parts.append(
                f"{b_day} 现货 {b_obs['spot_price']} {b_unit}，主力 {b_obs['futures_contract']} 期价 {b_obs['futures_price']} {b_unit}，基差 {b_val:+.2f} {b_unit}。"
            )
            evidence.append(
                {
                    "label": f"{name}现期基差（现货−主力）",
                    "value": f"{b_val:+.2f} {b_unit}",
                    "data_date": b_day,
                    "as_of": b_obs.get("collected_at") or local.isoformat(),
                    "source_name": (b_obs.get("source") or {}).get("name") or futures_basis.SOURCE_NAME,
                    "source_url": (b_obs.get("source") or {}).get("url") or "",
                }
            )

        # 3. Read exchange warehouse warrant if covered
        try:
            inv_obs = await futures_inventory.snapshot(self.symbol)
        except Exception:
            inv_obs = None
        if (
            inv_obs
            and inv_obs.get("status") == "collected"
            and inv_obs.get("data_as_of", "9999") <= local.date().isoformat()
        ):
            i_day = inv_obs["data_as_of"]
            qty = inv_obs["quantity"]
            chg = inv_obs["change"]
            i_unit = inv_obs.get("unit") or "单位"
            parts.append(f"{i_day} 已披露仓单 {qty} {i_unit}（较前次 {chg:+d} {i_unit}）。")
            evidence.append(
                {
                    "label": f"{name}交易所披露仓单",
                    "value": f"{qty} {i_unit}（{chg:+d}）",
                    "data_date": i_day,
                    "as_of": inv_obs.get("retrieved_at") or local.isoformat(),
                    "source_name": (inv_obs.get("source") or {}).get("name") or "公开仓单源",
                    "source_url": (inv_obs.get("source") or {}).get("url") or "",
                }
            )
            target_inv = (date.fromisoformat(i_day) + timedelta(days=5)).isoformat()
            choices.append(
                {
                    "id": "inventory_down",
                    "label": f"后续披露{name}仓单较 {qty} {i_unit} 下降",
                    "rule": f"自 {target_inv} 起首次披露的同口径仓单须低于 {i_day} 的 {qty} {i_unit}。",
                    "baseline": {
                        "version": RULE_VERSION,
                        "symbol": self.symbol,
                        "metric": "inventory_down",
                        "value": float(qty),
                        "unit": i_unit,
                        "period": i_day,
                        "target": target_inv,
                    },
                }
            )

        if not evidence:
            return {
                **base,
                "status": "unavailable",
                "headline": f"截至当前尚未取得{name}可核对的公开观测数据",
                "explanation": "行情或交易所分项暂不可达，系统不编造基线数值。",
            }

        if meta:
            parts.append(f"产业核心跟踪指标：{'、'.join(meta.drivers)}。")

        return {
            **base,
            "status": "available",
            "headline": f"{name}期现、仓单与历史分位核对",
            "explanation": "".join(parts),
            "evidence": evidence,
            "choices": choices,
        }

    async def review(self, choice: dict, cutoff: datetime) -> dict:
        if cutoff.utcoffset() is None:
            raise ValueError("cutoff requires a timezone")
        local = cutoff.astimezone(SHANGHAI)
        base = choice.get("baseline") or {}
        if base.get("version") != RULE_VERSION or base.get("symbol") != self.symbol:
            return _outcome("unverifiable", "判断规则无法核对", "缺少匹配的品种判断基线版本。")
        target = base.get("target")
        if not isinstance(target, str) or local.date().isoformat() < target:
            return _outcome(
                "pending", "尚未到达目标核验日", f"等待 {target} 及之后的公开数据披露后自动对账。"
            )

        metric = base.get("metric")
        name = futures.PRODUCTS[self.symbol][0]
        if metric in {"close", "close_down"}:
            try:
                snap = await futures.snapshot(self.symbol, workspace=True)
            except Exception:
                snap = None
            latest = (snap or {}).get("latest") or {}
            obs_day = latest.get("date")
            obs_val = latest.get("close")
            if (
                not obs_day
                or obs_day < target
                or obs_day > local.date().isoformat()
                or not isinstance(obs_val, (int, float))
            ):
                return _outcome(
                    "pending", "等待目标日及之后的新日线", f"当前尚未收录不早于 {target} 的收盘数据。"
                )
            delta = round(float(obs_val) - float(base["value"]), 2)
            supported = delta > 0 if metric == "close" else delta < 0
            src = (snap or {}).get("source") or {}
            ev = {
                "label": f"{name}连续合约收盘价",
                "value": f"{obs_val} {base['unit']}",
                "data_date": obs_day,
                "as_of": (snap or {}).get("collected_at") or local.isoformat(),
                "source_name": src.get("name") or "新浪财经",
                "source_url": src.get("url") or "",
            }
            text = (
                f"{name}连续收盘由 {base['period']} 的 {base['value']} {base['unit']} "
                f"变为 {obs_day} 的 {obs_val} {base['unit']}（变化 {delta:+.2f} {base['unit']}）。"
            )
            return _outcome(
                "supported" if supported else "contradicted",
                "原判断得到支持" if supported else "原判断未得到支持",
                text,
                [ev],
            )

        if metric == "inventory_down":
            try:
                inv = await futures_inventory.snapshot(self.symbol)
            except Exception:
                inv = None
            obs_day = (inv or {}).get("data_as_of")
            obs_qty = (inv or {}).get("quantity")
            if (
                not inv
                or inv.get("status") != "collected"
                or not obs_day
                or obs_day < target
                or obs_day > local.date().isoformat()
                or not isinstance(obs_qty, (int, float))
            ):
                return _outcome(
                    "pending", "等待目标日后的仓单日报", f"当前尚未收录不早于 {target} 的同口径仓单。"
                )
            delta = round(float(obs_qty) - float(base["value"]), 2)
            supported = delta < 0
            src = inv.get("source") or {}
            ev = {
                "label": f"{name}交易所披露仓单",
                "value": f"{obs_qty} {base['unit']}",
                "data_date": obs_day,
                "as_of": inv.get("retrieved_at") or local.isoformat(),
                "source_name": src.get("name") or "交易所仓单日报",
                "source_url": src.get("url") or "",
            }
            text = (
                f"{name}同口径仓单由 {base['period']} 的 {base['value']} {base['unit']} "
                f"变为 {obs_day} 的 {obs_qty} {base['unit']}（变化 {delta:+.0f} {base['unit']}）。"
            )
            return _outcome(
                "supported" if supported else "contradicted",
                "原判断得到支持" if supported else "原判断未得到支持",
                text,
                [ev],
            )

        return _outcome("unverifiable", "未知指标类型", "无法识别封存的核验指标。")


def for_symbol(symbol: str) -> GenericOntologyEngine:
    return GenericOntologyEngine(symbol)
