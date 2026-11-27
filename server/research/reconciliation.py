"""Bounded coverage check between report numbers and registered claims.

A match is coverage, not certification: it only says that a number with the same
normalized value and unit was registered as a claim for this run. Source truth,
extraction quality and economic meaning stay claim-level and model-review work.

The accounting is number-centric: every extracted report number is classified
exactly once as covered or uncovered, so ``claims_checked`` (the covered numbers)
plus ``unsupported_claims`` (the uncovered numbers) always equals
``report_numbers_checked``. The ledger also registers one unverified server claim
for every number the model left unregistered (see ``report_number_claims``), so
``claims_registered`` keeps up with ``report_numbers_checked`` instead of
depending on the optional model claim pass. Coverage is the covered share of
report numbers.

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
# A run registers one claim per report number; below this share of the report
# numbers the ledger reports ``claim_coverage_low`` for the operators. The gap is
# advisory: the report is still delivered.
MIN_COVERAGE = 0.8

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

# "170,899,152,276.34（约1708.99亿）" states one measurement twice: an exact
# amount immediately followed by its rounded display. It must count once, keyed by
# the exact value; otherwise the rounded annotation is read as a second, unclaimed
# number. A parenthetical whose display is not the same rounded magnitude (see
# ``_annotation_scale``) is left to the normal patterns.
_ANNOTATED = re.compile(rf"(?<![\d.])({_NUMBER})\s*[（(]\s*约\s*({_NUMBER})\s*(万亿|亿|万|千)\s*[)）]")

_PATTERNS = (
    ("annotated", _ANNOTATED),
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
# "3%-5%" / "100-120 万吨" / "40 至 50 亿元": one measurement with two bounds.
_RANGE_SEPARATOR = re.compile(r"^\s*[-–—~至到]\s*$")
_RANGE_SEPARATOR_LABEL = "-"
_RANGE_SIGNS = frozenset("-–—~")
_OPENING_BOUND = re.compile(r"(?<![\d.])(\d[\d,]*(?:\.\d+)?)\s*(万亿|亿|万|千)?\s*[-–—~至到]\s*$")
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


def report_number_claims(text, claims=()) -> list[dict]:
    """Build one server claim for every report number no existing claim covers.

    Registration is a fixed server action, not a model courtesy: the claim pass
    stays an optional upgrade path, so the ledger generates these candidates from
    the final report body before reconciling. A generated claim is ``unverified``
    and source-less; it keeps its normalized measurement key so a later model
    claim for the same number can upgrade it in place. Numbers the model already
    covered are skipped, which keeps one registered claim per report number.
    """
    report_numbers = _report_numbers(text if isinstance(text, str) else "")
    claim_list = [claim for claim in claims if isinstance(claim, dict)] if isinstance(claims, list) else []
    covered = {key for claim in claim_list if (key := _claim_key(claim)) is not None}
    return [
        _server_claim(key, display)
        for key, (display, _sentence) in report_numbers.items()
        if not _covered_measurement(key, covered)
    ]


def _reconcile(claims, text) -> dict:
    claim_list = [claim for claim in claims if isinstance(claim, dict)] if isinstance(claims, list) else []
    report_numbers = _report_numbers(text if isinstance(text, str) else "")
    claim_keys = {key for claim in claim_list if (key := _claim_key(claim)) is not None}
    covered_count = 0
    unsupported = []
    for key, entry in report_numbers.items():
        display, sentence = entry[0], entry[1]
        if _covered_measurement(key, claim_keys):
            covered_count += 1
            continue
        unsupported.append({"number": display, "sentence": _clip(sentence), "reason": "no_matching_claim"})
    report_count = len(report_numbers)
    # Every report number is classified exactly once. If this partition ever drifts,
    # the counters below would misstate the report, so the caller's guard degrades
    # the item to ``unavailable`` instead of persisting invented accounting.
    assert covered_count + len(unsupported) == report_count, "report numbers are not partitioned"
    if claim_list:
        verified = sum(1 for claim in claim_list if claim.get("verification") == "verified_source")
        ratio = round(verified / len(claim_list), 4)
    else:
        ratio = None
    # Coverage is the share of report numbers backed by a registered claim. A claim
    # that covers no report number cannot raise it; uncovered numbers are the ones
    # the operator-facing counters report.
    coverage = round(covered_count / report_count, 4) if report_count else None
    return {
        "schema": SCHEMA,
        "status": "ok",
        "scope": "bounded_report_number_coverage",
        "semantic_verification": False,
        "verified_claim_ratio": ratio,
        "coverage": coverage,
        "unsupported_claims": len(unsupported),
        "claims_checked": covered_count,
        "claims_registered": len(claim_list),
        "report_numbers_checked": report_count,
        "unsupported_numbers": unsupported[:MAX_DETAILS],
        "unsupported_numbers_truncated": len(unsupported) > MAX_DETAILS,
    }


def _report_numbers(text: str) -> dict:
    """Map distinct normalized measurements to their first report occurrence."""
    numbers: dict = {}
    for sentence in _sentences(text):
        masked = sentence
        taken: list = []
        entries: list = []
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
                if kind == "annotated" and _annotation_scale(match, number) is None:
                    # Not a rounded restatement: leave the span free so the exact
                    # number and the annotation are read as their normal families.
                    continue
                taken.append((start, end))
                key = _match_key(kind, match, number)
                if key is None:
                    continue
                display = masked[start:end]
                if kind == "annotated":
                    # The rounded annotation is a display aid, not a report number.
                    display = masked[match.start(1) : match.end(1)]
                if signless:
                    display = display.lstrip("+-")
                display = re.sub(r"\s+%", "%", _WHITESPACE.sub(" ", display).strip())
                entries.append((start, end, key, display))
        _merge_ranges(entries, masked, numbers, sentence)
    return numbers


def _merge_ranges(entries: list, masked: str, numbers: dict, sentence: str) -> None:
    """Fold ``3%-5%`` / ``100-120 万吨`` into one interval instead of two bounds.

    A range is a single reported measurement: a claim inside the interval covers
    it even when neither bound is claimed verbatim. Turning every bound into its
    own requirement produced false ``unsupported`` entries.
    """
    entries.sort(key=lambda item: item[0])
    paired: set = set()
    for index in range(len(entries) - 1):
        if index in paired or index + 1 in paired:
            continue
        first, second = entries[index], entries[index + 1]
        gap = masked[first[1] : second[0]]
        if not _RANGE_SEPARATOR.fullmatch(gap):
            # "-5%" is matched as a signed number, so the range hyphen lives
            # inside the second match instead of between the two matches.
            if second[0] != first[1] or masked[second[0] : second[0] + 1] not in _RANGE_SIGNS:
                continue
            gap = masked[first[1] : second[0] + 1]
            if not _RANGE_SEPARATOR.fullmatch(gap):
                continue
        if first[2][:2] != second[2][:2]:
            continue
        low, high = sorted((first[2][2], second[2][2]))
        display = f"{first[3]}{gap.strip()}{second[3]}"
        numbers[("range", first[2][0], first[2][1], low, high)] = (display, sentence)
        paired.update({index, index + 1})
    for index, (start, _end, key, display) in enumerate(entries):
        if index in paired:
            continue
        opening = _OPENING_BOUND.search(masked[:start])
        if opening is not None:
            low = _decimal(opening.group(1))
            if low is not None and low <= key[2] and low != key[2]:
                label = f"{opening.group(1)}{opening.group(2) or ''}{_RANGE_SEPARATOR_LABEL}{display}"
                numbers[("range", key[0], key[1], low, key[2])] = (label, sentence)
                continue
        if key in numbers:
            continue
        numbers[key] = (display, sentence)


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


def _annotation_scale(match: re.Match, number: Decimal) -> Decimal | None:
    """Return the display scale when ``（约X<scale>）`` restates ``number``.

    The parenthetical counts as the same measurement only when its rounded display
    is within one annotation unit of the exact value, so truncated displays also
    qualify but an unrelated parenthetical does not. Returns ``None`` otherwise.
    """
    rounded = _decimal(match.group(2))
    if rounded is None:
        return None
    scale = _SCALE[match.group(3)]
    exponent = rounded.as_tuple().exponent
    step = Decimal(1).scaleb(exponent) if isinstance(exponent, int) else Decimal(0)
    if abs(number - rounded * scale) > step * scale:
        return None
    return scale


def _match_key(kind: str, match: re.Match, number: Decimal) -> tuple | None:
    if kind == "percent":
        return _measure_key(number, match.group(2), None)
    if kind == "quantity":
        return _measure_key(number, (match.group(2) or "") + match.group(3), None)
    if kind == "amount":
        return _measure_key(number, match.group(2), None)
    if kind == "annotated":
        # The exact value is the measurement; the rounded annotation only displays it.
        return ("amount", None, number)
    return ("amount", None, number * _SCALE[match.group(2)])


def _stored_claim_key(claim: dict) -> tuple | None:
    """Read the normalized key a server-extracted claim stores in its payload.

    Range intervals and extractor-only unit spellings cannot be reproduced from
    value/unit/currency alone, so the server claim carries the authoritative key
    and the lookup prefers it over re-derivation.
    """
    raw = claim.get("claim_key")
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        return None
    dimension, currency, magnitude = raw
    if not isinstance(dimension, str) or (currency is not None and not isinstance(currency, str)):
        return None
    number = _decimal(magnitude)
    return (dimension, currency, number) if number is not None else None


def _claim_key(claim: dict) -> tuple | None:
    stored = _stored_claim_key(claim)
    if stored is not None:
        return stored
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


# Display units for the dimensions the extractor produces. The stored key stays
# authoritative for coverage; these fields only make the generated claim readable
# to claim lists, renders and audit views.
_SERVER_UNITS = {
    "ratio": ("%", Decimal("100")),
    "mass_t": ("吨", Decimal(1)),
    "mass_t_per_year": ("吨/年", Decimal(1)),
    "count_片": ("片", Decimal(1)),
    "count_个": ("个", Decimal(1)),
    "amount_per_t": ("元/吨", Decimal(1)),
}


def _server_claim(key: tuple, display: str) -> dict:
    """One source-less ``unverified`` claim for a report number."""
    if key[0] == "range":
        _tag, dimension, currency, magnitude, _high = key
    else:
        dimension, currency, magnitude = key
    unit, factor = _SERVER_UNITS.get(dimension, (None, Decimal(1)))
    if dimension == "amount":
        unit = "元"
    elif unit is None and dimension.startswith("unit:"):
        unit = dimension.split(":", 1)[1]
    elif unit is None:
        unit = dimension
    scaled = magnitude * factor
    return {
        "kind": "fact",
        "statement": f"报告正文数字：{display}",
        "supports": [],
        "opposes": [],
        "background": [],
        "quotes": {},
        "period": None,
        "value": _format_number(scaled) or format(scaled, "f"),
        "unit": unit,
        "currency": currency,
        "accounting_basis": None,
        "metric": None,
        "source": "",
        "published_at": None,
        "scope": None,
        "entities": [],
        "formula": None,
        "limitations": [],
        "citation_status": "partial",
        "semantic_status": "unreviewed",
        "verification": "unverified",
        "verification_declared": None,
        "verification_reason": None,
        "source_locator": None,
        "gaps": ["support_missing"],
        "origin": "server_extract",
        "claim_key": [dimension, currency, format(magnitude, "f")],
    }


_NUMBER_DISPLAY = re.compile(r"(?P<value>-?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[^\s].*)", re.ASCII)


def report_display_measurement(claim: dict) -> tuple[str, str] | None:
    """Split a server-extracted claim's report text into the page's own writing.

    The stored claim value is normalized to a base unit (``128.6 亿元`` becomes
    ``12860000000 元``), but the original-page gate compares the claim value with
    the literal numbers on the page. The wrap-up upgrade round therefore needs the
    report's own writing, so it can echo ``128.6``/``亿元`` instead of a converted
    magnitude the source page never contains.
    """
    statement = claim.get("statement") if isinstance(claim, dict) else None
    prefix = "报告正文数字："
    if not isinstance(statement, str) or not statement.startswith(prefix):
        return None
    match = _NUMBER_DISPLAY.match(statement[len(prefix) :].strip())
    if match is None:
        return None
    value = match.group("value").replace(",", "")
    unit = match.group("unit").strip()
    if not unit or len(value) > 100 or len(unit) > 80:
        return None
    return value, unit


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


def _covered_measurement(report_key: tuple, claim_keys: set) -> bool:
    """Coverage for one report measurement, including range intervals."""
    if report_key[0] == "range":
        _tag, dimension, currency, low, high = report_key
        return any(
            other[0] == dimension and other[1] == currency and low <= other[2] <= high for other in claim_keys
        )
    return _covered(report_key, claim_keys)


def same_measurement(left: dict, right: dict) -> bool:
    """True when two claims describe the same report measurement.

    The ledger uses this to upgrade an existing claim instead of inserting a
    duplicate: exact normalized keys match, and the bare-scale amount tolerance
    from ``_covered`` applies in both directions so a currency-less amount can be
    upgraded by its explicit currency form.
    """
    left_key = _claim_key(left)
    right_key = _claim_key(right)
    if left_key is None or right_key is None:
        return False
    return left_key == right_key or _covered(left_key, {right_key}) or _covered(right_key, {left_key})


def normalized_claim_key(value) -> tuple | None:
    """Canonicalize a model-supplied ``claim_key`` triple.

    ``research_read`` exposes the server-extracted measurement key as
    ``[dimension, currency, magnitude]``. The model may echo that key back to
    locate the claim the server already registered, so the same decimal parsing
    and shape checks apply before any lookup: a malformed key never matches.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    dimension, currency, magnitude = value
    if not isinstance(dimension, str) or not dimension.strip():
        return None
    if currency is not None and not isinstance(currency, str):
        return None
    number = _decimal(magnitude)
    if number is None:
        return None
    return (dimension.strip(), currency or None, number)


def claim_measure_key(claim: dict) -> tuple | None:
    """Return an existing claim's normalized measurement key (stored one preferred)."""
    return _claim_key(claim)


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
