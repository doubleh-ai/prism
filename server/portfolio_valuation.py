"""Owned repricing with observed quotes, consent and no snapshot writes.

This is an unchanged-position estimate, never a broker account balance. Only
public instrument codes leave the service; quantities and cash stay local.
"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from . import exchange_futures, portfolio, public_network
from .database import check_rate_limit, get_db
from .market.factory import get_chain
from .preferences import profile_in_transaction, run_profile
from .productivity.store import _active

SHANGHAI = ZoneInfo("Asia/Shanghai")
SECONDS = 10
CONCURRENCY = 4
ASSUMPTIONS = [
    "按原快照持仓、现金、负债与其后净入金不变估算；不代表实际账户余额。",
    "期货仅按明确参考价格的权益加减价格变化，不重复计入合约名义金额；不含费用、滑点与资金变动。",
    "经纪商保证金、乘数和交割条件仍须另行核对；本次行情读取不验证这些规则。",
]


def _snapshot(owner, version, *, for_agent=False):
    portfolio.init_portfolio_db()
    with get_db() as db:
        _active(db, owner)
        row = db.execute("SELECT payload FROM portfolio_snapshots WHERE owner=?", [owner]).fetchone()
        value = json.loads(row[0]) if row else None
        if not value or value.get("version") != version:
            raise HTTPException(409, "持仓已变化，请重新读取后检查")
        if value.get("allow_market_pricing") is not True:
            raise HTTPException(403, "请先允许使用公开行情对价；只发送代码，不发送数量或资金")
        if for_agent and value.get("share_with_agent") is not True:
            raise HTTPException(403, "未允许向研究模型提供风险摘要")
        return value, profile_in_transaction(db, owner)


def agent_inputs(owner, run_id):
    """The current run must already hold the original explicit sharing consent."""
    from .agent.store import get_run

    run = get_run(owner, run_id)
    if run["status"] != "running" or run.get("cancel_requested"):
        raise HTTPException(409, "研究未在运行")
    with get_db() as db:
        frozen = run_profile(db, owner, run_id)
    previous = frozen.get("portfolio_risk")
    if not previous or type(previous.get("portfolio_version")) is not int:
        raise HTTPException(403, "本轮未获风险摘要分享许可，请保存偏好后新建研究")
    version = previous["portfolio_version"]
    _snapshot(owner, version, for_agent=True)
    return version, frozen


def _security_id(position):
    identity = portfolio.security_identity(position["asset_id"], position["currency"])
    # Existing market adapters identify these mainland share codes. They cannot
    # yet prove the exchange/currency identity of funds or foreign instruments.
    return identity[1] if identity and not identity[1].startswith(("1", "5")) else None


def _contract(position):
    match = re.fullmatch(r"(SHFE|INE|DCE):([A-Z]+)(\d{4})", position["asset_id"].upper())
    if not match:
        return None
    exchange, symbol, month = match.groups()
    expected = "INE" if symbol == "SC" else "SHFE"
    return (
        (symbol, symbol.lower() + month)
        if symbol in exchange_futures.HOSTS and exchange == expected
        else None
    )


async def _quote(code):
    quote, stale = await get_chain().get_quote(code)
    value = quote.model_dump()
    stamp, price = value.get("data_timestamp"), value.get("price")
    if stale or value.get("stale"):
        return {"status": "unavailable", "reason": "stale_cache"}
    if value.get("ticker") != code or value.get("data_kind") not in {"quote", "daily_close"}:
        return {"status": "unavailable", "reason": "instrument_or_basis_unknown"}
    if type(stamp) is not int or stamp <= 0 or value.get("timestamp_kind") != "trade":
        return {"status": "unavailable", "reason": "market_time_unknown"}
    if type(price) not in {int, float} or not math.isfinite(price) or not 0 < price < 10**15:
        return {"status": "unavailable", "reason": "invalid_price"}
    return {
        "status": "collected",
        "price": str(price),
        "price_as_of": datetime.fromtimestamp(stamp, UTC).isoformat(),
        "data_as_of": datetime.fromtimestamp(stamp, SHANGHAI).date().isoformat(),
        "basis": value["data_kind"],
        "precision": "date" if value["data_kind"] == "daily_close" else "instant",
        "source": value.get("source", "unknown"),
        "retrieved_at": value.get("retrieved_at"),
    }


async def _settlements(symbol, deadline):
    """One dated report per product; only a dated 404 permits an older date."""
    today = datetime.now(SHANGHAI).date()
    async with public_network.async_client(timeout=6) as client:
        for offset in range(3):
            day = today - timedelta(days=offset)
            value = await exchange_futures.read_settlements(client, symbol, day, deadline)
            if value["status"] != "unavailable" or value.get("error_code") != "report_not_found":
                return value
    return value


def _futures_price(report, contract):
    if report.get("status") != "collected":
        return {
            "status": "unavailable",
            "reason": report.get("error_code", report.get("reason", "source_unavailable")),
        }
    rows = [r for r in report.get("contracts", []) if r.get("contract") == contract]
    price = rows[0].get("settlement") if len(rows) == 1 else None
    if type(price) not in {int, float} or not math.isfinite(price) or price <= 0:
        return {"status": "unavailable", "reason": "concrete_contract_price_missing"}
    day = datetime.strptime(report["data_as_of"], "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    return {
        "status": "collected",
        "price": str(price),
        "price_as_of": day.isoformat(),
        "data_as_of": report["data_as_of"],
        "basis": "exchange_settlement",
        "precision": "date",
        "source": "exchange_daily",
        "source_url": report.get("source_url"),
        "body_sha256": report.get("body_sha256"),
    }


def _eligible(value, position, now):
    if value.get("status") != "collected":
        return value
    result = copy.deepcopy(value)
    observed = datetime.fromisoformat(result["price_as_of"])
    reference = datetime.fromisoformat(position["price_as_of"])
    if observed > now + timedelta(minutes=5) or observed < now - timedelta(days=7):
        result.update(status="unavailable", reason="market_time_out_of_range")
    elif (
        result["precision"] == "date"
        and observed.astimezone(SHANGHAI).date() <= reference.astimezone(SHANGHAI).date()
        or result["precision"] == "instant"
        and observed < reference
    ):
        result.update(status="unavailable", reason="observation_not_newer_than_reference")
    if result["status"] != "collected":
        result.pop("price", None)
    return result


def _calculation(profile, original, observations, *, now):
    def rule(ident, status, reason):
        return dict(id=ident, status=status, reason=reason, observed=None, limit=None, unit=None)

    missing = sum(v["status"] != "collected" for v in observations)
    futures = [p for p in original["positions"] if p.get("asset_class") == "futures"]
    basis = (
        not futures
        or original.get("futures_equity_basis") == "marked_to_reference_prices"
        and original.get("futures_equity") is not None
        and all(
            datetime.fromisoformat(p["price_as_of"]) == datetime.fromisoformat(original["observed_at"])
            for p in futures
        )
    )
    checks = [
        rule(
            "market_coverage",
            "not_evaluated" if missing else "pass",
            f"{missing} 项未取得可用于本次对价的具体标的行情"
            if missing
            else "已按具体标的取得报价或结算价，不代表交易可执行"
            if original["positions"]
            else "快照没有待对价持仓，资金仍按原记录计算",
        )
    ]
    if futures:
        checks.append(
            rule(
                "futures_equity_basis",
                "pass" if basis else "not_evaluated",
                "期货权益已明确包含原参考价格下盈亏，仅作持仓不变估算"
                if basis
                else "期货账户权益与参考价格时点尚未明确对应，未推算新权益",
            )
        )
        checks.append(
            rule(
                "broker_terms_verification",
                "not_evaluated",
                "未连接经纪商或独立核验本账户规则；保证金和交割资格不能确认",
            )
        )
    if missing or not basis:
        reference = portfolio.evaluate(profile, original, now=now.timestamp())
        result = {
            "overall": "not_evaluated",
            "rules": checks,
            "scenarios": [],
            "profile_version": profile.get("version", 0),
            "portfolio_version": original["version"],
            "reference_check": {
                "as_of": original["observed_at"],
                "overall": reference["overall"],
                "breaches": [r["id"] for r in reference["rules"] if r["status"] == "breach"],
            },
        }
    else:
        value = copy.deepcopy(original)
        delta = Decimal(0)
        with localcontext() as context:
            context.prec = 80
            for position, observation in zip(value["positions"], observations, strict=True):
                if position.get("asset_class") == "futures":
                    delta += (
                        (Decimal(observation["price"]) - Decimal(position["price"]))
                        * Decimal(position["quantity"])
                        * Decimal(position["multiplier"])
                        * (1 if position["side"] == "long" else -1)
                    )
                position.update(price=observation["price"], price_as_of=observation["price_as_of"])
            if futures:
                value["futures_equity"] = str(Decimal(value["futures_equity"]) + delta)
        result = portfolio.evaluate(profile, value, now=now.timestamp())
        instants = [datetime.fromisoformat(o["price_as_of"]) for o in observations]
        recent = all(
            now - instant <= timedelta(minutes=15)
            if o["basis"] == "quote"
            else (now.astimezone(SHANGHAI).date() - instant.astimezone(SHANGHAI).date()).days <= 1
            for instant, o in zip(instants, observations, strict=True)
        )
        if not recent:
            checks.append(
                rule("market_recency", "not_evaluated", "已取得的行情时点较早，不能表示当前市场风险")
            )
        dates = {o["data_as_of"] for o in observations}
        if len(dates) > 1:
            checks.append(
                rule(
                    "market_dates",
                    "not_evaluated",
                    "行情来自不同日期，只能作为异步估算，不能视为同日组合估值",
                )
            )
        result["rules"] = checks + result["rules"]
        if result["overall"] != "breach" and any(r["status"] == "not_evaluated" for r in result["rules"]):
            result["overall"] = "not_evaluated"
    result.update(
        input_basis="unchanged_snapshot_with_observed_market_prices_estimate",
        account_verified=False,
        assumptions=ASSUMPTIONS,
    )
    return result


async def reprice(owner, version, *, frozen_profile=None, authorize=None):
    original, profile = _snapshot(owner, version, for_agent=frozen_profile is not None)
    if not check_rate_limit("portfolio-reprice:" + owner, max_req=6, window=60):
        raise HTTPException(429, "行情对价较频繁，请稍后再试")
    profile = copy.deepcopy(frozen_profile if frozen_profile is not None else profile)
    loop = asyncio.get_running_loop()
    deadline, slots = loop.time() + SECONDS, asyncio.Semaphore(CONCURRENCY)
    requests, selections = {}, []
    for position in original["positions"]:
        if position.get("asset_class") == "futures":
            contract = _contract(position)
            key = ("futures", contract[0]) if contract else None
            selections.append((key, contract[1] if contract else None))
        else:
            code = _security_id(position)
            key = ("security", code) if code else None
            selections.append((key, None))
        if key is not None:
            requests[key] = None

    async def collect(key):
        async with slots:
            if loop.time() >= deadline:
                return {"status": "unavailable", "reason": "request_budget"}
            try:
                if authorize is not None:
                    authorize()
                async with asyncio.timeout_at(deadline):
                    return (
                        await _quote(key[1]) if key[0] == "security" else await _settlements(key[1], deadline)
                    )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return {"status": "unavailable", "reason": "source_timeout"}
            except Exception:
                return {"status": "unavailable", "reason": "source_unavailable"}

    values = await asyncio.gather(*(collect(key) for key in requests))
    if authorize is not None:
        authorize()
    requests.update(zip(requests, values, strict=True))
    now = datetime.now(UTC)
    observations = []
    for position, (key, contract) in zip(original["positions"], selections, strict=True):
        raw = (
            requests[key]
            if key is not None
            else {"status": "unavailable", "reason": "instrument_not_covered"}
        )
        value = _futures_price(raw, contract) if contract else raw
        observations.append(_eligible(value, position, now))
    current, latest_profile = _snapshot(owner, version, for_agent=frozen_profile is not None)
    if (
        current != original
        or frozen_profile is None
        and latest_profile.get("version") != profile.get("version")
    ):
        raise HTTPException(409, "检查期间持仓或偏好已变化，请重新读取")
    risk = _calculation(profile, original, observations, now=now)
    dates = sorted({v["data_as_of"] for v in observations if v["status"] == "collected"})
    return {
        "portfolio_version": version,
        "profile_version": profile.get("version", 0),
        "observed_at": now.isoformat(),
        "input_basis": "unchanged_saved_snapshot",
        "snapshot_as_of": original["observed_at"],
        "positions_changed": False,
        "status": "partial" if any(o["status"] != "collected" for o in observations) else "collected",
        "positions": [
            {"asset_id": p["asset_id"], "side": p.get("side"), **o}
            for p, o in zip(original["positions"], observations, strict=True)
        ],
        "market_basis": {
            "data_dates": dates,
            "collected_count": sum(v["status"] == "collected" for v in observations),
            "missing_count": sum(v["status"] != "collected" for v in observations),
            "latest_market_observation_verified": False,
        },
        "risk": risk,
    }
