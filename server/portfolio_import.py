"""Bounded in-memory CSV preview; no file retention, model or database writes."""

from __future__ import annotations

import csv
import io

from fastapi import HTTPException
from pydantic import Field, ValidationError

from .portfolio import Currency, FuturesPosition, Position
from .productivity.models import ClosedModel

MAX_CSV_BYTES = 65536
FIELDS = {
    "asset_class": ("类型", "资产类型"),
    "asset_id": ("代码", "证券代码", "股票代码", "合约", "合约代码"),
    "quantity": ("数量", "持仓数量", "持仓量"),
    "price": ("价格", "最新价", "市价"),
    "currency": ("币种",),
    "price_as_of": ("价格时间",),
    "source_url": ("来源",),
    "side": ("方向",),
    "multiplier": ("合约乘数",),
    "broker_margin_rate_pct": ("保证金比例",),
    "terms_as_of": ("规则时间", "规则核对时间"),
    "last_trading_date": ("最后交易日",),
}
ALIASES = {alias: key for key, aliases in FIELDS.items() for alias in (key, *aliases)}


class ImportPreview(ClosedModel):
    csv_text: str = Field(min_length=1, max_length=MAX_CSV_BYTES)
    currency: Currency
    price_as_of: str | None = Field(default=None, max_length=40)


def parse_preview(body: ImportPreview):
    text = body.csv_text
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise HTTPException(422, "请使用UTF-8 CSV文本") from None
    if size > MAX_CSV_BYTES:
        raise HTTPException(413, "导入内容不能超过64KiB")
    text = text.removeprefix("\ufeff")
    if not text.strip():
        raise HTTPException(422, "没有可导入的持仓")
    if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise HTTPException(422, "导入内容含无效控制字符")
    delimiter = "\t" if "\t" in text.splitlines()[0] else ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    try:
        headers = next(reader)
        if not 3 <= len(headers) <= 32:
            raise HTTPException(422, "CSV需要代码、数量和价格列，最多32列")
        fields = [ALIASES.get(name.strip().casefold()) for name in headers]
        used = [name for name in fields if name is not None]
        if len(set(used)) != len(used):
            raise HTTPException(422, "同一字段重复出现，请保留一列")
        if not {"asset_id", "quantity", "price"}.issubset(used):
            raise HTTPException(422, "CSV缺少代码、数量或价格列；成本价不能替代当前价格")
        positions = []
        seen = set()
        for cells in reader:
            if not cells or all(not cell.strip() for cell in cells):
                continue
            if len(positions) >= 100:
                raise HTTPException(422, "每次最多导入100条持仓")
            if len(cells) != len(headers):
                raise HTTPException(422, f"第{reader.line_num}行列数与表头不一致")
            value = {
                name: cell.strip() for name, cell in zip(fields, cells, strict=True) if name and cell.strip()
            }
            if any(cell.startswith(("=", "+", "@")) for cell in value.values()):
                raise HTTPException(422, f"第{reader.line_num}行包含公式或无效数字；请粘贴原始值")
            kind = value.get("asset_class", "")
            kind = {"证券": "security", "股票": "security", "stock": "security", "期货": "futures"}.get(
                kind, kind
            )
            if not kind:
                kind = (
                    "futures"
                    if value.get("asset_id", "").upper().startswith(("SHFE:", "DCE:", "INE:"))
                    else "security"
                )
            value["asset_class"] = kind
            value.setdefault("currency", body.currency)
            value.setdefault("price_as_of", body.price_as_of)
            if "side" in value:
                value["side"] = {"多": "long", "多头": "long", "空": "short", "空头": "short"}.get(
                    value["side"], value["side"]
                )
            try:
                if kind not in {"security", "futures"}:
                    raise ValueError("unknown asset type")
                position = (FuturesPosition if kind == "futures" else Position).model_validate(value)
            except (ValidationError, ValueError):
                raise HTTPException(
                    422, f"第{reader.line_num}行的类型、数量、价格、时间或合约字段无效；请核对原始值"
                ) from None
            key = (position.asset_id.casefold(), getattr(position, "side", None))
            if key in seen:
                raise HTTPException(422, f"第{reader.line_num}行重复；同一合约同一方向请先合并核对")
            seen.add(key)
            positions.append(position.model_dump())
    except (csv.Error, StopIteration, IndexError):
        raise HTTPException(422, "CSV格式无效，请核对表头、引号与分隔符") from None
    if not positions:
        raise HTTPException(422, "没有可导入的持仓")
    futures = sum(p["asset_class"] == "futures" for p in positions)
    return {
        "positions": positions,
        "counts": {"security": len(positions) - futures, "futures": futures, "total": len(positions)},
        "ignored_columns": len(fields) - len(used),
        "input_basis": "user_supplied_preview",
    }
