"""Owner-scoped, user-supplied portfolio snapshots and deterministic risk rules.

No model, broker, FX guesses or external side effect. A missing, stale or mixed
currency input is unevaluated, never zero. Calculations are not price verification.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator, model_validator

from .database import get_db
from .preferences import _owner, init_preferences_db, profile_in_transaction
from .productivity.models import ClosedModel, canonical_instant
from .productivity.routes import _body
from .source_urls import public_source_url

router = APIRouter(prefix="/api/preferences", tags=["preferences"])
Currency = Literal["CNY", "USD", "HKD"]
_DECIMAL = re.compile(r"(?:0|[1-9]\d{0,14})(?:\.\d{1,8})?\Z")


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


class PortfolioUpdate(ClosedModel):
    expected_version: int = Field(ge=0)
    currency: Currency
    observed_at: str = Field(max_length=40)
    cash: str = Field(max_length=24)
    debt: str = Field(max_length=24)
    positions: list[Position] = Field(max_length=100)
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
        if (self.equity_peak is None) != (self.peak_as_of is None):
            raise ValueError("equity peak requires its observation time")
        if self.equity_peak is not None:
            _amount(self.equity_peak, positive=True)
            self.peak_as_of = canonical_instant(self.peak_as_of, "UTC")
            if self.peak_as_of > self.observed_at:
                raise ValueError("peak cannot follow portfolio observation")
        if len({p.asset_id.casefold() for p in self.positions}) != len(self.positions):
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
    values = [(p["asset_id"], Decimal(p["quantity"]) * Decimal(p["price"])) for p in positions]
    exposure = sum((v for _, v in values), Decimal(0))
    debt = Decimal(portfolio["debt"])
    equity = Decimal(portfolio["cash"]) + exposure - debt
    unit = portfolio["currency"]
    rule(
        "net_equity",
        "pass" if equity > 0 else "breach",
        "净权益＝现金＋持仓市值－负债",
        str(equity),
        "0",
        unit,
    )
    if equity > 0:
        largest = max((v for _, v in values), default=Decimal(0)) / equity * 100
        limit = Decimal(profile["max_position_pct"])
        rule(
            "concentration",
            "pass" if largest <= limit else "breach",
            "最大单一标的市值占净权益比例",
            str(largest.quantize(Decimal("0.0001"))),
            str(limit),
            "%",
        )
        leveraged = debt > 0 or exposure > equity
        rule(
            "leverage",
            "breach" if leveraged and not profile["allow_leverage"] else "pass",
            "按负债与总敞口核对杠杆偏好；未评估衍生品保证金",
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


@router.put("/portfolio")
async def save_portfolio(request: Request):
    from .auth import verify_csrf
    from .productivity.store import _active

    owner = _owner(request)
    verify_csrf(request)
    body = await _body(request, PortfolioUpdate)
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
