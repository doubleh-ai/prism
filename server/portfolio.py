"""Owner-scoped, user-supplied portfolio snapshots and deterministic risk rules.

No model, broker, FX guesses or external side effect. A missing, stale or mixed
currency input is unevaluated, never zero. Calculations are not price verification.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from .database import get_db
from .preferences import _owner, init_preferences_db, profile_in_transaction
from .productivity.models import ClosedModel, canonical_instant
from .productivity.routes import _body
from .source_urls import public_source_url

router = APIRouter(prefix="/api/preferences", tags=["preferences"])
Currency = Literal["CNY", "USD", "HKD"]
_DECIMAL = re.compile(r"(?:0|[1-9]\d{0,14})(?:\.\d{1,8})?\Z", re.ASCII)


def _amount(value, *, positive=False, signed=False):
    raw = value[1:] if signed and value.startswith("-") else value
    if not _DECIMAL.fullmatch(raw) or (positive and Decimal(value) <= 0):
        raise ValueError("bounded decimal string required")
    return value


class Position(ClosedModel):
    asset_class: Literal["security"] = "security"
    asset_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9._:/-]+$")
    quantity: str = Field(max_length=24)
    price: str = Field(max_length=24)
    currency: Currency
    price_as_of: str = Field(max_length=40)
    source_url: str | None = Field(default=None, max_length=2048)

    @field_validator("quantity", "price")
    @classmethod
    def positive_amount(cls, value):
        return _amount(value, positive=True)

    @field_validator("price_as_of")
    @classmethod
    def dated_price(cls, value):
        return canonical_instant(value, "UTC")

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value):
        if value is not None and (not value.startswith("https://") or public_source_url(value) != value):
            raise ValueError("public HTTPS source URL required")
        return value

    @field_validator("asset_id")
    @classmethod
    def security_code(cls, value):
        if value.upper().startswith(("SHFE:", "DCE:", "INE:")):
            raise ValueError("select futures for a futures contract")
        return value


class FuturesPosition(ClosedModel):
    asset_class: Literal["futures"]
    asset_id: str = Field(pattern=r"^(?:SHFE|DCE|INE):[A-Za-z]{1,3}[0-9]{4}$", max_length=40)
    quantity: str = Field(pattern=r"^[1-9][0-9]{0,8}$", max_length=9)
    price: str = Field(max_length=24)
    currency: Literal["CNY"]
    price_as_of: str = Field(max_length=40)
    source_url: str | None = Field(default=None, max_length=2048)
    side: Literal["long", "short"]
    multiplier: str = Field(max_length=24)
    broker_margin_rate_pct: str | None = Field(default=None, max_length=24)
    terms_as_of: str | None = Field(default=None, max_length=40)
    last_trading_date: str | None = Field(default=None, max_length=10)

    _positive = field_validator("price", "multiplier")(Position.positive_amount.__func__)
    _price_time = field_validator("price_as_of")(Position.dated_price.__func__)
    _public = field_validator("source_url")(Position.public_url.__func__)

    @field_validator("asset_id")
    @classmethod
    def concrete_contract(cls, value):
        if not 1 <= int(value[-2:]) <= 12:
            raise ValueError("concrete delivery month required")
        return value.upper()

    @field_validator("broker_margin_rate_pct")
    @classmethod
    def explicit_margin(cls, value):
        if value is not None:
            _amount(value, positive=True)
            if Decimal(value) > 100:
                raise ValueError("margin percentage exceeds 100")
        return value

    @field_validator("terms_as_of")
    @classmethod
    def terms_time(cls, value):
        return canonical_instant(value, "UTC") if value is not None else None

    @field_validator("last_trading_date")
    @classmethod
    def explicit_expiry(cls, value):
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("ISO calendar date required")
        return value


class PortfolioUpdate(ClosedModel):
    expected_version: int = Field(ge=0)
    currency: Currency
    observed_at: str = Field(max_length=40)
    cash: str = Field(max_length=24)
    debt: str = Field(max_length=24)
    positions: list[Position | FuturesPosition] = Field(max_length=100)
    futures_equity: str | None = Field(default=None, max_length=24)
    share_with_agent: bool = False
    equity_peak: str | None = Field(default=None, max_length=24)
    peak_as_of: str | None = Field(default=None, max_length=40)
    net_flows_since_peak: str = Field(default="0", max_length=25)

    @field_validator("cash", "debt")
    @classmethod
    def nonnegative_amount(cls, value):
        return _amount(value)

    @field_validator("net_flows_since_peak")
    @classmethod
    def signed_amount(cls, value):
        return _amount(value, signed=True)

    @model_validator(mode="after")
    def versioned_snapshot(self):
        self.observed_at = canonical_instant(self.observed_at, "UTC")
        if self.futures_equity is not None:
            _amount(self.futures_equity, signed=True)
        if (self.equity_peak is None) != (self.peak_as_of is None):
            raise ValueError("equity peak requires its observation time")
        if self.equity_peak is not None:
            _amount(self.equity_peak, positive=True)
            self.peak_as_of = canonical_instant(self.peak_as_of, "UTC")
            if self.peak_as_of > self.observed_at:
                raise ValueError("peak cannot follow portfolio observation")
        keys = [(p.asset_id.casefold(), getattr(p, "side", None)) for p in self.positions]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate asset")
        return self


def init_portfolio_db():
    init_preferences_db()
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            owner TEXT PRIMARY KEY, version INTEGER NOT NULL,
            payload TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS portfolio_history (
            owner TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(owner,version)
        );
        CREATE TRIGGER IF NOT EXISTS portfolio_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM portfolio_snapshots WHERE owner=OLD.username;
            DELETE FROM portfolio_history WHERE owner=OLD.username;
        END;
        """)


def evaluate(profile, portfolio, *, now=None):
    with localcontext() as context:
        context.prec = 80
        return _evaluate(profile, portfolio, now=now)


def _futures_checks(portfolio, positions, now, rule):
    """Linear domestic contracts only; no margin offsets or liquidation promise."""
    if not positions:
        return []
    notionals = [Decimal(p["quantity"]) * Decimal(p["price"]) * Decimal(p["multiplier"]) for p in positions]
    gross = sum(notionals, Decimal(0))
    unit = portfolio["currency"]
    balance = Decimal(portfolio["futures_equity"]) if portfolio.get("futures_equity") is not None else None
    rule(
        "futures_equity",
        "not_evaluated" if balance is None else "pass" if balance > 0 else "breach",
        "期货账户权益单独计入净权益，不把合约名义金额当作资产；须与现金分开填写",
        str(balance) if balance is not None else None,
        "0",
        unit,
    )
    dated = all(
        p.get("source_url")
        and p.get("terms_as_of")
        and now - timedelta(days=7) <= datetime.fromisoformat(p["terms_as_of"]) <= now + timedelta(minutes=5)
        for p in positions
    )
    rule(
        "futures_terms",
        "pass" if dated else "not_evaluated",
        "已填规则来源与7天内核对时间，仍是用户输入，未独立核验原文或经纪商条件"
        if dated
        else "缺少规则来源或近期核对时间，不能确认乘数及保证金条件仍适用",
    )
    rates_known = all(p.get("broker_margin_rate_pct") is not None for p in positions)
    required = (
        sum(
            (
                n * Decimal(p["broker_margin_rate_pct"]) / 100
                for p, n in zip(positions, notionals, strict=True)
            ),
            Decimal(0),
        )
        if rates_known
        else None
    )
    complete = dated and required is not None and balance is not None
    rule(
        "futures_margin",
        "breach" if complete and required > balance else "pass" if complete else "not_evaluated",
        "按填写的经纪商比例逐合约估算占用，不抵扣套利或对冲保证金；不代表经纪商实际可用资金或追保金额",
        str(required) if required is not None else None,
        str(balance) if balance is not None else None,
        unit,
    )
    today = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    dates = [date.fromisoformat(p["last_trading_date"]) for p in positions if p.get("last_trading_date")]
    expired = any(d < today for d in dates)
    near = any(d <= today + timedelta(days=7) for d in dates)
    rule(
        "futures_delivery",
        "breach"
        if expired
        else "not_evaluated"
        if near or len(dates) != len(positions) or not dated
        else "pass",
        "存在已经过最后交易日的合约，请核对实际持仓"
        if expired
        else "最后交易日缺失、临近或规则未核对；需人工核对持仓期限与交割资格"
        if near or len(dates) != len(positions) or not dated
        else "填写的最后交易日均在7天以后；仅日期检查，不证明交割资格",
    )
    scenarios = []
    for shock in (Decimal("5"), Decimal("10")):
        loss = gross * shock / 100
        remaining = balance - loss if balance is not None else None
        stressed_margin = (
            sum(
                (
                    n
                    * (1 - shock / 100 if p["side"] == "long" else 1 + shock / 100)
                    * Decimal(p["broker_margin_rate_pct"])
                    / 100
                    for p, n in zip(positions, notionals, strict=True)
                ),
                Decimal(0),
            )
            if rates_known
            else None
        )
        scenarios.append(
            {
                "adverse_move_pct": str(shock),
                "futures_loss": str(loss),
                "remaining_futures_equity": str(remaining) if remaining is not None else None,
                "estimated_margin": str(stressed_margin) if stressed_margin is not None else None,
                "margin_shortfall": str(max(Decimal(0), stressed_margin - remaining)) if complete else None,
                "unit": unit,
                "assumption": "所有期货分别向持仓不利方向变动；按线性乘数及现有保证金比例，不抵扣对冲，不含手续费、滑点、跳空及比例调整。不是概率预测或强平价格。",
            }
        )
    return scenarios


def _evaluate(profile, portfolio, *, now=None):
    now = datetime.fromtimestamp(time.time() if now is None else now, UTC)
    rules = []

    def rule(name, status, reason, observed=None, limit=None, unit=None):
        rules.append(dict(id=name, status=status, reason=reason, observed=observed, limit=limit, unit=unit))

    empty = dict(profile_version=profile.get("version", 0), portfolio_version=portfolio.get("version", 0))
    if not profile.get("configured") or not portfolio.get("configured"):
        rule("inputs", "not_evaluated", "请先保存研究偏好和持仓快照")
        return {**empty, "overall": "not_evaluated", "rules": rules}
    positions = portfolio["positions"]
    times = [datetime.fromisoformat(portfolio["observed_at"])] + [
        datetime.fromisoformat(p["price_as_of"]) for p in positions
    ]
    freshness = all(now - timedelta(days=7) <= instant <= now + timedelta(minutes=5) for instant in times)
    same_currency = all(p["currency"] == portfolio["currency"] for p in positions)
    rule(
        "freshness",
        "pass" if freshness else "not_evaluated",
        "输入时点在7天内" if freshness else "输入过期或时间在未来",
    )
    rule(
        "currency",
        "pass" if same_currency else "not_evaluated",
        "同币种计算" if same_currency else "跨币种持仓缺少经确认的汇率",
    )
    if not freshness or not same_currency:
        return {**empty, "overall": "not_evaluated", "rules": rules}
    securities = [p for p in positions if p.get("asset_class", "security") == "security"]
    futures = [p for p in positions if p.get("asset_class") == "futures"]
    scenarios = _futures_checks(portfolio, futures, now, rule)
    if futures and portfolio.get("futures_equity") is None:
        return {
            **empty,
            "overall": "breach" if any(r["status"] == "breach" for r in rules) else "not_evaluated",
            "rules": rules,
            "scenarios": scenarios,
            "input_basis": "user_supplied_not_independently_verified",
        }
    securities_value = sum((Decimal(p["quantity"]) * Decimal(p["price"]) for p in securities), Decimal(0))
    values = [
        (
            re.sub(r"[0-9]{4}$", "", p["asset_id"]) if p.get("asset_class") == "futures" else p["asset_id"],
            Decimal(p["quantity"])
            * Decimal(p["price"])
            * (Decimal(p["multiplier"]) if p.get("asset_class") == "futures" else 1),
        )
        for p in positions
    ]
    exposure = sum((v for _, v in values), Decimal(0))
    debt = Decimal(portfolio["debt"])
    equity = (
        Decimal(portfolio["cash"]) + securities_value + Decimal(portfolio.get("futures_equity") or "0") - debt
    )
    unit = portfolio["currency"]
    rule(
        "net_equity",
        "pass" if equity > 0 else "breach",
        "净权益＝现金＋证券市值＋期货账户权益－负债（不计期货名义金额）",
        str(equity),
        "0",
        unit,
    )
    if equity > 0:
        grouped = {}
        for code, amount in values:
            key = code.casefold()
            grouped[key] = grouped.get(key, Decimal(0)) + amount
        largest = max(grouped.values(), default=Decimal(0)) / equity * 100
        limit = Decimal(profile["max_position_pct"])
        rule(
            "concentration",
            "pass" if largest <= limit else "breach",
            "最大单一证券或期货品种总敞口占净权益比例；期货跨到期月合并，多空不互相抵消",
            str(largest.quantize(Decimal("0.0001"))),
            str(limit),
            "%",
        )
        leveraged = debt > 0 or exposure > equity
        rule(
            "leverage",
            "breach" if leveraged and not profile["allow_leverage"] else "pass",
            "按负债与证券市值、期货名义总敞口核对杠杆偏好；不抵扣对冲",
            str((exposure / equity).quantize(Decimal("0.0001"))),
            None,
            "倍",
        )
        if portfolio.get("equity_peak"):
            peak = Decimal(portfolio["equity_peak"])
            adjusted_equity = equity - Decimal(portfolio.get("net_flows_since_peak", "0"))
            decline = max(Decimal(0), (peak - adjusted_equity) / peak * 100)
            drawdown_limit = Decimal(profile["max_drawdown_pct"])
            rule(
                "drawdown",
                "pass" if decline <= drawdown_limit else "breach",
                "相对填写的权益高点，扣除其后净入金计算当前回撤",
                str(decline.quantize(Decimal("0.0001"))),
                str(drawdown_limit),
                "%",
            )
        else:
            rule(
                "drawdown",
                "not_evaluated",
                "缺少权益高点和其后净入金，不能验证实际回撤",
                limit=str(profile["max_drawdown_pct"]),
                unit="%",
            )
    status = (
        "breach"
        if any(r["status"] == "breach" for r in rules)
        else ("not_evaluated" if any(r["status"] == "not_evaluated" for r in rules) else "pass")
    )
    return {
        **empty,
        "overall": status,
        "rules": rules,
        "scenarios": scenarios,
        "input_basis": "user_supplied_not_independently_verified",
    }


def _reply(db, owner):
    row = db.execute("SELECT payload FROM portfolio_snapshots WHERE owner=?", [owner]).fetchone()
    portfolio = json.loads(row[0]) if row else {"configured": False, "version": 0}
    return {"portfolio": portfolio, "risk": evaluate(profile_in_transaction(db, owner), portfolio)}


@router.get("/portfolio")
async def read_portfolio(request: Request):
    owner = _owner(request)
    init_portfolio_db()
    with get_db() as db:
        from .productivity.store import _active

        _active(db, owner)
        return _reply(db, owner)


@router.post("/portfolio/import-preview")
async def preview_portfolio(request: Request):
    from .auth import verify_csrf
    from .portfolio_import import ImportPreview, parse_preview
    from .productivity.store import _active

    owner = _owner(request)
    verify_csrf(request)
    body = await _body(request, ImportPreview, maximum=131072)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    with get_db() as db:
        _active(db, owner)
    return parse_preview(body)


@router.put("/portfolio")
async def save_portfolio(request: Request):
    from .auth import verify_csrf
    from .productivity.store import _active

    owner = _owner(request)
    verify_csrf(request)
    body = await _body(request, PortfolioUpdate, maximum=131072)
    if _owner(request) != owner:
        raise HTTPException(401, "请重新登录")
    init_portfolio_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            _active(db, owner)
            current = db.execute("SELECT version FROM portfolio_snapshots WHERE owner=?", [owner]).fetchone()
            if (current[0] if current else 0) != body.expected_version:
                raise HTTPException(409, "持仓已在其他设备更新，请重新读取后修改")
            value = {
                **body.model_dump(exclude={"expected_version"}),
                "version": body.expected_version + 1,
                "configured": True,
                "updated_at": int(time.time()),
                "input_basis": "user_supplied",
            }
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
            db.execute(
                "INSERT INTO portfolio_snapshots VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE SET "
                "version=excluded.version,payload=excluded.payload,updated_at=excluded.updated_at",
                [owner, value["version"], raw, value["updated_at"]],
            )
            db.execute("INSERT INTO portfolio_history VALUES(?,?,?)", [owner, value["version"], raw])
            db.execute(
                "DELETE FROM portfolio_history WHERE owner=? AND version<=?", [owner, value["version"] - 100]
            )
            result = _reply(db, owner)
            db.execute("COMMIT")
            return result
        except BaseException:
            db.execute("ROLLBACK")
            raise
