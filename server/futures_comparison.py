"""Two dated RB observations, preserving each source's contract and price convention."""

import asyncio
import math
import re
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException

from . import futures, futures_basis, futures_inventory, futures_spread, futures_weekly

UNIT = "元/吨"


def _contract(value):
    match = re.fullmatch(r"(?:RB)?(\d{2}(?:0[1-9]|1[0-2]))", str(value).upper())
    return "RB" + match[1] if match else None


def _price(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def _difference(later, earlier):
    return float(Decimal(str(later)) - Decimal(str(earlier)))


def _gap(day, reason):
    return {"date": day, "reason": reason}


def _issue(observation, day):
    if not isinstance(observation, dict):
        return "来源读取失败"
    if observation.get("status") != "collected":
        return observation.get("reason") or observation.get("error_code") or "所选日期没有可用记录"
    if observation.get("symbol") != "RB" or observation.get("data_as_of") != day:
        return "品种或披露日期与所选记录不一致"
    source = observation.get("source")
    if not isinstance(source, dict) or not source.get("name") or not source.get("url"):
        return "缺少可核对的来源"
    if observation.get("quote_unit") != UNIT:
        return "报价单位不能按元/吨比较"
    return None


def _basis_row(day, observation):
    if issue := _issue(observation, day):
        return _gap(day, issue)
    contract = _contract(observation.get("futures_contract"))
    spot, main = observation.get("spot_price"), observation.get("futures_price")
    if not contract or not (_price(spot) and _price(main)):
        return _gap(day, "缺少主力合约或同日现期报价")
    return {
        "date": day,
        "source": observation["source"],
        "contract": contract,
        "spot": spot,
        "main": main,
        "basis": _difference(spot, main),
    }


def _curve_row(day, observation):
    if issue := _issue(observation, day):
        return _gap(day, issue)
    calendar, report = observation.get("contract_months"), observation.get("settlement_report")
    if (
        not isinstance(calendar, dict)
        or not isinstance(report, dict)
        or not isinstance(calendar.get("months"), list)
    ):
        return _gap(day, "缺少挂牌合约清单或结算日报")
    if calendar.get("data_as_of") != day or report.get("data_as_of") != day:
        return _gap(day, "挂牌与结算资料日期不一致")
    contracts = {}
    for row in calendar["months"]:
        if not isinstance(row, dict):
            continue
        code, month, settlement = (
            _contract(row.get("contract")),
            row.get("delivery_month"),
            row.get("settlement"),
        )
        if code and month == f"20{code[2:4]}-{code[4:]}" and _price(settlement):
            if code in contracts:
                return _gap(day, "同一合约存在多条结算记录")
            contracts[code] = (month, settlement)
    return {
        "date": day,
        "source": observation["source"],
        "calendar_source": {"name": "挂牌合约清单", "url": calendar.get("source_url")},
        "contracts": contracts,
    }


def _warrant_row(day, observation):
    if not isinstance(observation, dict) or observation.get("status") != "collected":
        return _gap(day, "未取得该日仓单")
    unit, quantity = observation.get("unit"), observation.get("quantity")
    source = observation.get("source")
    if (
        observation.get("symbol") != "RB"
        or observation.get("data_as_of") != day
        or unit != "吨"
        or type(quantity) is not int
        or quantity < 0
        or not observation.get("segment")
        or not observation.get("basis")
        or not isinstance(source, dict)
        or not source.get("url")
        or not source.get("name")
    ):
        return _gap(day, "仓单的日期、范围、单位或来源无法核对")
    return {
        "date": day,
        "quantity": quantity,
        "segment": observation["segment"],
        "basis": observation["basis"],
        "unit": unit,
        "source": source,
    }


def _physical_assessment(changes, curve_change, warrants):
    """Test the short-end tightness hypothesis; prices alone cannot identify its cause."""
    before, after = warrants
    warrant_delta = None
    if (
        "quantity" in before
        and "quantity" in after
        and (before["segment"], before["basis"]) == (after["segment"], after["basis"])
    ):
        warrant_delta = after["quantity"] - before["quantity"]
    state = "insufficient"
    reading = "同合约基差、固定月差或同范围仓单缺项，暂缓判断近端供需。"
    if changes and changes["rolled"]:
        reading = "主力换月，先取得两日同一合约的现期报价，再判断近端强弱。"
    elif changes and curve_change is not None and warrant_delta is not None:
        if changes["spot"] > 0 and changes["basis"] > 0 and curve_change < 0 and warrant_delta < 0:
            state = "tightening_consistent"
            reading = "现货上涨且跑赢同一主力，近月相对走强，可交割仓单下降。三项观测支持近端趋紧。"
        elif changes["spot"] < 0 and changes["basis"] < 0 and curve_change > 0 and warrant_delta > 0:
            state = "loosening_consistent"
            reading = "现货下跌且跑输同一主力，远月相对走强，可交割仓单增加。三项观测支持近端转松。"
        else:
            state = "mixed"
            reading = "现期、固定月差与仓单没有共同指向近端趋紧或转松。暂缓据此选择方向。"
        if changes["basis"] > 0 and changes["spot"] <= 0:
            reading += "基差走强源于期价跌得更多，现货涨价尚未出现。"
    return {
        "state": state,
        "assessment": reading,
        "warrant_change_tonnes": warrant_delta,
        "next_check": "比较同期钢厂产量、建筑钢材成交与社会库存；产量收缩和终端补库都可能改变现货价格。核对仓单注销及到期日。",
        "recheck_condition": "跟踪原合约对和仓单段；现货相对强弱或近远月方向反转时，重新检验原判断。",
    }


async def compare(symbol: str, start: date, end: date):
    if symbol != "RB":
        raise HTTPException(422, "双日比较目前支持螺纹钢")
    if not start < end <= datetime.now(futures.SHANGHAI).date():
        raise HTTPException(422, "请选择先后两个日期，结束日期不能晚于上海今天")
    days = [start.isoformat(), end.isoformat()]
    raw = await asyncio.gather(
        *(
            reader(symbol, day)
            for reader in (futures_basis.snapshot, futures_spread.spread, futures_inventory.snapshot)
            for day in days
        ),
        return_exceptions=True,
    )
    for result in raw:
        if isinstance(result, asyncio.CancelledError):
            raise result
    basis = [_basis_row(day, obs) for day, obs in zip(days, raw[:2], strict=True)]
    first, last = basis
    changes = None
    if all("basis" in row for row in basis):
        changes = {key: _difference(last[key], first[key]) for key in ("spot", "main", "basis")}
        changes["rolled"] = first["contract"] != last["contract"]

    curves = [_curve_row(day, obs) for day, obs in zip(days, raw[2:4], strict=True)]
    pair, spread_change = None, None
    reason = "需要两个交易日的同口径结算资料"
    if all("contracts" in row for row in curves):
        before, after = (row["contracts"] for row in curves)
        common = sorted(
            (code for code in before.keys() & after.keys() if before[code][0] == after[code][0]),
            key=lambda code: before[code][0],
        )
        reason = "两日共同有结算价的交割月份不足两个"
        if len(common) >= 2:
            near, far = common[0], common[-1]
            pair = [near, far]
            for row in curves:
                prices = row["contracts"]
                row.update(
                    near=prices[near][1],
                    far=prices[far][1],
                    spread=_difference(prices[far][1], prices[near][1]),
                )
            spread_change = _difference(curves[1]["spread"], curves[0]["spread"])
    warrants = [_warrant_row(day, obs) for day, obs in zip(days, raw[4:], strict=True)]
    for row in curves:
        row.pop("contracts", None)
    return {
        "symbol": symbol,
        "start": days[0],
        "end": days[1],
        "unit": UNIT,
        "basis": basis,
        "changes": changes,
        "curves": curves,
        "pair": pair,
        "warrants": warrants,
        "weekly_research": futures_weekly.research(
            symbol,
            min(datetime.now(futures.SHANGHAI), datetime.combine(end, datetime.max.time(), futures.SHANGHAI)),
        ),
        "physical_assessment": _physical_assessment(changes, spread_change, warrants),
        "spread_change": spread_change,
        "spread_gap": None if pair else reason,
    }
