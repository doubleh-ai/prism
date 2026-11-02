"""Bounded quantity/period comparisons, never semantic or source certification.

Metric, entity and accounting basis must be explicit. Normalizing a unit does
not establish that the model extracted the right number or economic meaning.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext

_NUMBER = re.compile(r"[+-]?(?:0|[1-9]\d{0,59})(?:\.\d{1,24})?\Z", re.ASCII)
_CURRENCY = {
    "CNY": "CNY",
    "人民币": "CNY",
    "USD": "USD",
    "美元": "USD",
    "HKD": "HKD",
    "港元": "HKD",
    "EUR": "EUR",
    "欧元": "EUR",
}
_UNITS = {
    "元": ("amount", "1"),
    "万元": ("amount", "10000"),
    "亿元": ("amount", "100000000"),
    "吨": ("mass_t", "1"),
    "万吨": ("mass_t", "10000"),
    "千克": ("mass_t", ".001"),
    "kg": ("mass_t", ".001"),
    "t": ("mass_t", "1"),
    "吨/年": ("mass_t_per_year", "1"),
    "万吨/年": ("mass_t_per_year", "10000"),
    "片": ("count_片", "1"),
    "万片": ("count_片", "10000"),
    "亿片": ("count_片", "100000000"),
    "个": ("count_个", "1"),
    "万个": ("count_个", "10000"),
    "亿个": ("count_个", "100000000"),
    "%": ("ratio", ".01"),
    "bp": ("ratio", ".0001"),
    "bps": ("ratio", ".0001"),
    "ratio": ("ratio", "1"),
    "元/吨": ("amount_per_t", "1"),
    "万元/吨": ("amount_per_t", "10000"),
    "元/千克": ("amount_per_t", "1000"),
}


def normalized(text, maximum):
    return (
        unicodedata.normalize("NFKC", text).strip().casefold()
        if isinstance(text, str) and len(text) <= maximum
        else ""
    )


def period_span(value):
    """Only explicit closed dates, calendar years/quarters/halves. No guessed dates."""
    raw = normalized(value, 200).replace(" ", "")
    match = re.fullmatch(
        r"(\d{4})(?:年)?(?:(?:q([1-4]))|(?:第?([一二三四1-4])季度)|(?:h([12]))|([上下])半年)?", raw
    )
    if match:
        year = int(match[1])
        if not 1900 <= year <= 2199:
            return None
        quarter = match[2] or match[3]
        if quarter:
            q = int(quarter) if quarter.isascii() else "一二三四".index(quarter) + 1
            start, end = q * 3 - 2, q * 3
        elif match[4] or match[5]:
            first = match[4] == "1" or match[5] == "上"
            start, end = (1, 6) if first else (7, 12)
        else:
            start, end = 1, 12
        return date(year, start, 1), date(year, end, calendar.monthrange(year, end)[1])
    try:
        parts = raw.split("/")
        if len(parts) not in {1, 2} or any(not re.fullmatch(r"\d{4}-\d{2}-\d{2}", p) for p in parts):
            return None
        start, end = date.fromisoformat(parts[0]), date.fromisoformat(parts[-1])
        return (start, end) if start <= end and 1900 <= start.year <= end.year <= 2199 else None
    except ValueError:
        return None


def quantity(value, unit, currency):
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None, "number_unbounded_or_missing"
    raw = str(value)
    if not _NUMBER.fullmatch(raw):
        return None, "number_unbounded_or_missing"
    key = normalized(unit, 80).replace(" ", "")
    if key not in _UNITS:
        return None, "unit_not_supported"
    dimension, scale = _UNITS[key]
    code = _CURRENCY.get(currency) if isinstance(currency, str) else None
    if dimension.startswith("amount") and code is None:
        return None, "currency_missing_or_unsupported"
    if not dimension.startswith("amount") and currency is not None and currency != "":
        return None, "currency_on_nonmonetary_quantity"
    try:
        with localcontext() as ctx:
            ctx.prec = 100
            number = Decimal(raw) * Decimal(scale)
            if not number.is_finite() or number.adjusted() > 90:
                return None, "number_unbounded_or_missing"
            result = "0" if number == 0 else format(number.normalize(), "f")
            return {"value": result, "dimension": dimension, "currency": code}, None
    except (InvalidOperation, ValueError):
        return None, "number_unbounded_or_missing"


def audit_claims(claims, *, excluded=()):
    groups, unassessed = defaultdict(list), []
    excluded = set(excluded)
    quantitative = [c for c in claims[:60] if c.get("value") is not None]
    for claim in quantitative:
        cid = claim["claim_id"]
        if cid in excluded:
            continue
        reasons = []
        entities = claim.get("entities", [])
        entity = normalized(entities[0], 120) if isinstance(entities, list) and len(entities) == 1 else ""
        metric = normalized(claim.get("metric"), 80)
        basis = normalized(claim.get("accounting_basis"), 300)
        span = period_span(claim.get("period"))
        numeric, issue = quantity(claim.get("value"), claim.get("unit"), claim.get("currency"))
        if claim.get("citation_status") != "valid" or claim.get("kind") not in {"fact", "calculation"}:
            reasons.append("citation_or_claim_kind_not_eligible")
        if claim.get("kind") == "fact" and not any(
            isinstance(claim.get("quotes"), dict) and claim["quotes"].get(pid)
            for pid in claim.get("supports", [])
        ):
            reasons.append("support_quote_missing")
        for missing, code in (
            (not entity, "single_entity_missing"),
            (not metric, "metric_missing"),
            (not basis, "accounting_basis_missing"),
            (span is None, "period_not_supported"),
        ):
            if missing:
                reasons.append(code)
        if issue:
            reasons.append(issue)
        if reasons:
            unassessed.append({"claim_id": cid, "reasons": reasons})
            continue
        key = (
            entity,
            metric,
            basis,
            *[d.isoformat() for d in span],
            numeric["dimension"],
            numeric["currency"],
        )
        groups[key].append({"claim_id": cid, "normalized_value": numeric["value"]})
    conflicts, comparable = [], 0
    for key, values in groups.items():
        if len(values) < 2:
            unassessed.append({"claim_id": values[0]["claim_id"], "reasons": ["matching_peer_missing"]})
            continue
        comparable += len(values)
        if len({v["normalized_value"] for v in values}) > 1:
            entity, metric, basis, start, end, dimension, currency = key
            conflicts.append(
                {
                    "status": "potential_value_conflict",
                    "entity": entity,
                    "metric": metric,
                    "accounting_basis": basis,
                    "period": {"start": start, "end": end},
                    "dimension": dimension,
                    "currency": currency,
                    "values": values,
                    "claim_ids": [v["claim_id"] for v in values],
                }
            )
    return {
        "schema": "prism.numeric-checks.v1",
        "scope": "bounded_within_run_numeric_consistency",
        "semantic_verification": False,
        "independent_sources_verified": False,
        "status": "conflicts_require_review"
        if conflicts
        else "bounded_checks_complete"
        if comparable
        else "insufficient_comparable_claims",
        "quantitative_claims": len(quantitative),
        "comparable_claims": comparable,
        "conflicts": conflicts,
        "not_comparable": unassessed[:20],
        "not_comparable_count": len(unassessed),
        "details_truncated": len(unassessed) > 20,
    }


def path_checks(relations):
    spans = [period_span(r.get("valid_period")) for r in relations]
    known = [span for span in spans if span is not None]
    overlap = (max(p[0] for p in known), min(p[1] for p in known)) if known else None
    if overlap and overlap[0] > overlap[1]:
        period = {"status": "nonoverlap"}
    elif not spans or len(known) != len(spans):
        period = {"status": "unknown"}
    else:
        period = {"status": "overlap", "start": overlap[0].isoformat(), "end": overlap[1].isoformat()}
    products = [
        {
            "at_entity": a["to_entity"],
            "input_product": a["product"],
            "output_product": b["product"],
            "status": "same_label_requires_semantic_review"
            if normalized(a["product"], 200) == normalized(b["product"], 200)
            else "conversion_evidence_required",
        }
        for a, b in zip(relations, relations[1:], strict=False)
    ]
    return {"period": period, "products": products, "semantic_verification": False}
