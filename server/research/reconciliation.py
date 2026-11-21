"""Bounded coverage check between report numbers and registered claims.

A match is coverage, not certification: it only says that a number with the same
normalized value and unit was registered as a claim for this run. Source truth,
extraction quality and economic meaning stay claim-level and model-review work.

The check never gates the report. Numbers that cannot be matched are reported for
operators; unexpected conditions degrade the item to ``unavailable`` instead of
raising. Claims are read from their payload JSON, so the three-positional-column
``research_claims`` INSERT and an application rollback stay intact.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .quantities import quantity

SCHEMA = "prism.claim-reconciliation.v1"
MAX_DETAILS = 20
MAX_SENTENCE_CHARACTERS = 300

_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,80})(?:\.\d{1,24})?"
_SCALE = {
    "万亿": Decimal("1000000000000"),
    "亿": Decimal("100000000"),
    "万": Decimal("10000"),
    "千": Decimal("1000"),
}
_CURRENCY_CODES = {
    "CNY": "CNY",
    "RMB": "CNY",
    "人民币": "CNY",
    "USD": "USD",
    "美元": "USD",
    "HKD": "HKD",
    "港元": "HKD",
    "EUR": "EUR",
    "欧元": "EUR",
}
_CURRENCY_IN_UNIT = (
    ("美元", "USD"),
    ("港元", "HKD"),
    ("欧元", "EUR"),
    ("人民币", "CNY"),
    ("元", "CNY"),
)
_MONETARY = re.compile(r"(万亿|亿|万|千)?(人民币|美元|港元|欧元|元)")

# Percentage-like units are extracted first because they never carry a scale.
_PERCENT_UNITS = ("个百分点", "个基点", "bps", "bp", "%", "倍")
# Quantity units follow the three number families from the quality bar (trading and
# holdings, industry data, production/capacity). Generic people/company counts
# (人/名/家/户/笔/张...) are deliberately absent: they are prose counts, not key numbers.
_QUANTITY_UNITS = (
    "元/千瓦时",
    "元/平方米",
    "元/千克",
    "元/千克时",
    "元/股",
    "元/吨",
    "美元/桶",
    "美元/吨",
    "吨/年",
    "千瓦时",
    "千瓦",
    "兆瓦",
    "吉瓦",
    "GWh",
    "MWh",
    "kWh",
    "GW",
    "MW",
    "kW",
    "吨",
    "千克",
    "公斤",
    "kg",
    "t",
    "手",
    "桶",
    "片",
    "台",
    "辆",
    "座",
    "亩",
    "公顷",
    "平方米",
    "平方公里",
    "公里",
    "千米",
    "度",
)
_QUANTITY_ALTERNATION = "|".join(re.escape(unit) for unit in sorted(_QUANTITY_UNITS, key=len, reverse=True))
_PERCENT_ALTERNATION = "|".join(re.escape(unit) for unit in _PERCENT_UNITS)
_AMOUNT_ALTERNATION = "|".join(re.escape(word) for word in ("人民币", "美元", "港元", "欧元", "元"))

_PATTERNS = (
    ("percent", re.compile(rf"(?<![\d.])({_NUMBER})\s*({_PERCENT_ALTERNATION})")),
    (
        "quantity",
        re.compile(rf"(?<![\d.])({_NUMBER})\s*(万亿|亿|万|千)?\s*({_QUANTITY_ALTERNATION})"),
    ),
    (
        "amount",
        re.compile(rf"(?<![\d.])({_NUMBER})\s*((?:万亿|亿|万|千)?\s*(?:{_AMOUNT_ALTERNATION}))"),
    ),
    (
        "bare_scale",
        re.compile(rf"(?<![\d.])({_NUMBER})\s*(万亿|亿|万|千)(?![\u4e00-\u9fffA-Za-z%])"),
    ),
)
_HEADING = re.compile(r"#{1,6}\s")
_RULE = re.compile(r"[-*_]{3,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[。；;！!？?])")
_URL = re.compile(r"https?://[^\s)\]}>」』]+", re.IGNORECASE)
_WHITESPACE = re.compile(r"\s+")
# "3%-5%" and "3亿-5亿" use a hyphen between bounds; a hyphen after one of these
# characters is a range separator, not a sign.
_SIGNABLE_PREVIOUS = set("0123456789%％亿万千百倍吨手桶片台辆座度亩hH")


def reconcile_report(claims, text) -> dict:
    """Compare report numbers with the run's claims and never raise.

    ``claims`` is the payload list read from ``research_claims``; ``text`` is the
    final report narrative. Any diagnostic failure degrades to ``unavailable`` so a
    report always reaches the user.
    """
    try:
        return _reconcile(claims, text)
    except Exception:  # noqa: BLE001 - a diagnostic must not cost the report
        return unavailable_result()


def unavailable_result() -> dict:
    """The conservative shape used when coverage numbers cannot be computed."""
    return {"schema": SCHEMA, "status": "unavailable"}


def _reconcile(claims, text) -> dict:
    claim_list = [claim for claim in claims if isinstance(claim, dict)] if isinstance(claims, list) else []
    report_numbers = _report_numbers(text if isinstance(text, str) else "")
    claim_keys = {key for claim in claim_list if (key := _claim_key(claim)) is not None}
    unsupported = []
    for key, (display, sentence) in report_numbers.items():
        if _covered(key, claim_keys):
            continue
        unsupported.append({"number": display, "sentence": _clip(sentence), "reason": "no_matching_claim"})
    if claim_list:
        verified = sum(1 for claim in claim_list if claim.get("verification") == "verified_source")
        ratio = round(verified / len(claim_list), 4)
    else:
        ratio = None
    return {
        "schema": SCHEMA,
        "status": "ok",
        "scope": "bounded_report_number_coverage",
        "semantic_verification": False,
        "verified_claim_ratio": ratio,
        "unsupported_claims": len(unsupported),
        "claims_checked": len(claim_list),
        "report_numbers_checked": len(report_numbers),
        "unsupported_numbers": unsupported[:MAX_DETAILS],
        "unsupported_numbers_truncated": len(unsupported) > MAX_DETAILS,
    }


def _report_numbers(text: str) -> dict:
    """Map distinct normalized measurements to their first report occurrence."""
    numbers: dict = {}
    for sentence in _sentences(text):
        masked = sentence
        taken: list = []
        for kind, pattern in _PATTERNS:
            for match in pattern.finditer(masked):
                start, end = match.span()
                if any(start < taken_end and taken_start < end for taken_start, taken_end in taken):
                    continue
                if masked[:start].rstrip().endswith("第"):
                    # Ordinals (第 3 名 / 第 5 家) are sequence numbers, not key numbers.
                    continue
                number, signless = _report_number(match, masked)
                if number is None:
                    continue
                taken.append((start, end))
                key = _match_key(kind, match, number)
                if key is None or key in numbers:
                    continue
                display = masked[start:end]
                if signless:
                    display = display.lstrip("+-")
                display = re.sub(r"\s+%", "%", _WHITESPACE.sub(" ", display).strip())
                numbers[key] = (display, sentence)
    return numbers


def _sentences(text: str):
    for raw_line in re.split(r"\r\n|\r|\n", text):
        # Table pipes are separators, not unit boundaries: treat them as spaces.
        line = raw_line.replace("|", " ").strip()
        if not line or _HEADING.match(line) or _RULE.fullmatch(line):
            continue
        for part in _SENTENCE_SPLIT.split(_mask(unicodedata.normalize("NFKC", line))):
            part = part.strip()
            if part:
                yield part


def _mask(sentence: str) -> str:
    """Blank URLs so their digits and query strings are never counted as report data."""
    return _URL.sub(lambda match: " " * len(match.group(0)), sentence)


def _report_number(match: re.Match, masked: str) -> tuple:
    """Read the number text, dropping a range hyphen that is not a sign."""
    raw = match.group(1)
    number = _decimal(raw)
    if number is None:
        return None, False
    if raw[0] in "+-" and match.start(1) > 0 and masked[match.start(1) - 1] in _SIGNABLE_PREVIOUS:
        return abs(number), True
    return number, False


def _match_key(kind: str, match: re.Match, number: Decimal) -> tuple | None:
    if kind == "percent":
        return _measure_key(number, match.group(2), None)
    if kind == "quantity":
        return _measure_key(number, (match.group(2) or "") + match.group(3), None)
    if kind == "amount":
        return _measure_key(number, match.group(2), None)
    return ("amount", None, number * _SCALE[match.group(2)])


def _claim_key(claim: dict) -> tuple | None:
    value = claim.get("value")
    if value is None or isinstance(value, bool):
        return None
    number = _decimal(value)
    if number is None:
        return None
    return _measure_key(number, claim.get("unit"), claim.get("currency"))


def _measure_key(number: Decimal, unit, currency) -> tuple | None:
    unit_text = _unit_text(unit)
    code = _currency_code(currency) or _currency_in_unit(unit_text)
    monetary = _MONETARY.fullmatch(unit_text)
    if monetary:
        return ("amount", code, number * _SCALE.get(monetary.group(1), Decimal(1)))
    formatted = _format_number(number)
    if formatted is None:
        return None
    normalized, _issue = quantity(formatted, unit_text, code)
    if normalized is None and code is not None:
        # Tolerate a currency attached to a ratio/count claim: the unit decides.
        normalized, _issue = quantity(formatted, unit_text, None)
    if normalized is None:
        return ("unit:" + unit_text, code, number)
    return (normalized["dimension"], normalized["currency"], Decimal(normalized["value"]))


def _covered(report_key: tuple, claim_keys: set) -> bool:
    if report_key in claim_keys:
        return True
    dimension, currency, magnitude = report_key
    if dimension != "amount":
        return False
    # A bare-scale amount ("市值 1200 亿") may omit the currency; accept it when one
    # side is currency-neutral and the normalized magnitude is identical.
    return any(
        other_dimension == "amount"
        and other_magnitude == magnitude
        and (currency is None or other_currency is None or currency == other_currency)
        for other_dimension, other_currency, other_magnitude in claim_keys
    )


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        raw = str(value)
    elif isinstance(value, str):
        raw = value.strip().replace(",", "")
    else:
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _format_number(number: Decimal) -> str | None:
    try:
        formatted = format(number, "f")
    except (InvalidOperation, ValueError):
        return None
    return formatted if re.fullmatch(_NUMBER, formatted, re.ASCII) else None


def _unit_text(unit) -> str:
    if not isinstance(unit, str):
        return ""
    text = _WHITESPACE.sub("", unicodedata.normalize("NFKC", unit)).casefold()
    return "bp" if text in {"个基点", "基点"} else text


def _currency_code(currency) -> str | None:
    if not isinstance(currency, str):
        return None
    return _CURRENCY_CODES.get(unicodedata.normalize("NFKC", currency).strip().upper())


def _currency_in_unit(unit_text: str) -> str | None:
    for word, code in _CURRENCY_IN_UNIT:
        if word in unit_text:
            return code
    return None


def _clip(sentence: str) -> str:
    collapsed = _WHITESPACE.sub(" ", sentence).strip()
    if len(collapsed) <= MAX_SENTENCE_CHARACTERS:
        return collapsed
    return collapsed[: MAX_SENTENCE_CHARACTERS - 1] + "…"
