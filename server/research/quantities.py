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


_QUARTER_NAMES = "一二三四"
# A claim often restates its period inside parentheses, e.g.
# "2024年度（2024-01-01至2024-12-31）". The restatement repeats the same period in
# another writing; the primary period before it is what the excerpt must state.
_PERIOD_PAREN = re.compile(r"[（(]")
_MONTH_RANGE = re.compile(r"(\d{4})年?(\d{1,2})[-—–~至到](\d{1,2})月")
_YEAR_ONLY = re.compile(r"(\d{4})(?:年|年度)?")


def period_matches(period, text) -> bool:
    """True when ``text`` states a claim's period, tolerating writing variants.

    A model writes a period as ``2024``, ``2024年``, ``2024Q3`` or ``2024-09-30``
    and may append its own restatement (``2024年度（2024-01-01至2024-12-31）``).
    Comparing that literal against an excerpt flags ``period_not_in_excerpt`` even
    when the page states 2024年. The matcher compares normalized period tokens
    (year / quarter / half / month range / exact date) against equivalent
    spellings on the page and keeps the literal substring rule for anything it
    cannot normalize, so a genuinely different period still fails.
    """
    if not isinstance(period, str) or not period.strip():
        return True
    if not isinstance(text, str) or not text.strip():
        return False
    if period.strip() in text:
        return True
    tokens = _period_tokens(period)
    if not tokens:
        return False
    page = unicodedata.normalize("NFKC", text)
    return all(_period_token_matches(token, page) for token in tokens)


def _period_tokens(period: str) -> list[tuple]:
    """Normalized tokens (all must appear) for one claim period."""
    normalized_period = unicodedata.normalize("NFKC", period)
    primary = _PERIOD_PAREN.split(normalized_period, maxsplit=1)[0].strip() or normalized_period
    span = period_span(primary)
    if span is not None:
        start, end = span
        if start == end:
            return [("date", start.year, start.month, start.day)]
        if (start.month, end.month) == (1, 12):
            return [("year", start.year)]
        if start.month in {1, 4, 7, 10} and end.month == start.month + 2:
            return [("quarter", start.year, (start.month - 1) // 3 + 1)]
        if (start.month, end.month) in {(1, 6), (7, 12)}:
            return [("half", start.year, 1 if start.month == 1 else 2)]
        return [
            ("date", start.year, start.month, start.day),
            ("date", end.year, end.month, end.day),
        ]
    raw = re.sub(r"\s+", "", primary)
    month_range = _MONTH_RANGE.fullmatch(raw)
    if month_range is not None:
        year, low, high = (int(group) for group in month_range.groups())
        if low > high or not 1 <= low <= high <= 12:
            return []
        if (low, high) == (1, 12):
            return [("year", year)]
        if (low, high) in {(1, 6), (7, 12)}:
            return [("half", year, 1 if low == 1 else 2, low, high)]
        return [("months", year, low, high)]
    year_only = _YEAR_ONLY.fullmatch(raw)
    if year_only is not None and 1900 <= int(year_only.group(1)) <= 2199:
        return [("year", int(year_only.group(1)))]
    return []


def _period_token_matches(token: tuple, text: str) -> bool:
    kind = token[0]
    if kind == "year":
        return _year_in_text(token[1], text)
    if kind == "date":
        return _date_in_text(token[1], token[2], token[3], text)
    if kind == "months":
        return _year_in_text(token[1], text) and _month_span_in_text(token[2], token[3], text)
    if kind == "quarter":
        return _year_in_text(token[1], text) and _quarter_in_text(token[2], text)
    if kind == "half":
        if not _year_in_text(token[1], text):
            return False
        if _half_in_text(token[2], text):
            return True
        return len(token) > 3 and _month_span_in_text(token[3], token[4], text)
    return False


def _year_in_text(year: int, text: str) -> bool:
    return re.search(rf"(?<!\d){year}(?!\d)", text) is not None


def _month_in_text(month: int, text: str) -> bool:
    return re.search(rf"(?<!\d)0?{month}\s*月", text) is not None


def _month_span_in_text(low: int, high: int, text: str) -> bool:
    if _month_in_text(low, text) and _month_in_text(high, text):
        return True
    return re.search(rf"(?<!\d)0?{low}\s*[-—–~至到]\s*0?{high}\s*月", text) is not None


def _date_in_text(year: int, month: int, day: int, text: str) -> bool:
    patterns = (
        rf"(?<!\d){year}\s*年\s*0?{month}\s*月\s*0?{day}\s*日",
        rf"(?<!\d){year}[-/.]0?{month}[-/.]0?{day}(?!\d)",
    )
    return any(re.search(pattern, text) is not None for pattern in patterns)


def _quarter_in_text(quarter: int, text: str) -> bool:
    named = f"{_QUARTER_NAMES[quarter - 1]}季度"
    patterns = (
        rf"(?i)(?<![a-z0-9])q{quarter}(?![0-9])",
        rf"第?{named}",
        rf"(?<!\d){quarter}\s*季度",
    )
    return any(re.search(pattern, text) is not None for pattern in patterns)


def _half_in_text(half: int, text: str) -> bool:
    named = "上半年" if half == 1 else "下半年"
    patterns = (rf"(?i)(?<![a-z0-9])h{half}(?![0-9])", named)
    return any(re.search(pattern, text) is not None for pattern in patterns)


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


def comparison_input(claim):
    """An explicit comparison key, value and gaps, without guessing synonyms."""
    reasons = []
    entities = claim.get("entities", [])
    entity = normalized(entities[0], 120) if isinstance(entities, list) and len(entities) == 1 else ""
    metric = normalized(claim.get("metric"), 80)
    basis = normalized(claim.get("accounting_basis"), 300)
    span = period_span(claim.get("period"))
    numeric, issue = quantity(claim.get("value"), claim.get("unit"), claim.get("currency"))
    if (
        claim.get("citation_status") != "valid"
        or not isinstance(claim.get("kind"), str)
        or claim["kind"] not in {"fact", "calculation"}
    ):
        reasons.append("citation_or_claim_kind_not_eligible")
    supports, quotes = claim.get("supports", []), claim.get("quotes", {})
    if claim.get("kind") == "fact" and not any(
        isinstance(pid, str)
        and isinstance(quotes, dict)
        and isinstance(quotes.get(pid), str)
        and quotes[pid].strip()
        for pid in (supports if isinstance(supports, list) else [])
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
        return None, None, reasons
    key = (entity, metric, basis, *[d.isoformat() for d in span], numeric["dimension"], numeric["currency"])
    return key, numeric["value"], []


def audit_claims(claims, *, excluded=()):
    groups, unassessed = defaultdict(list), []
    excluded = set(excluded)
    quantitative = [c for c in claims[:60] if c.get("value") is not None]
    for claim in quantitative:
        cid = claim["claim_id"]
        if cid in excluded:
            continue
        key, value, reasons = comparison_input(claim)
        if reasons:
            unassessed.append({"claim_id": cid, "reasons": reasons})
            continue
        groups[key].append({"claim_id": cid, "normalized_value": value})
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
