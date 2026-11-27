"""Server-side association of registered claims with obtained original pages.

Batch fourteen obtained two original documents (``original_obtained=true``) yet
finalized a run where none of its claims was verified: the model held the page
text but never linked a claim to one concrete page. Linking is mechanical, so the
server does it: after the report-body claims are registered and before the
evidence gate runs, every unverified or secondhand financial claim whose
measurement appears on an obtained original page gets that page as its
``source_locator``, the page passage as its ``supports``, and is promoted to
``verified_source``. Writing the support matters as much as the locator: the
ledger derives ``citation_status`` from ``gaps``, so a located claim without a
support keeps ``support_missing`` and a ``partial`` citation, and the run is
still reported as ``citation_checks_incomplete``.

The same number is often spelled differently in the report and on its table page:
``1,708.99 亿元`` against a bare ``170,899,152,276.34`` under a ``单位：元``
header, or ``(1,234.56) 万元`` for a loss. The matcher therefore normalizes both
sides onto the same ``quantities`` dimension. Bare table cells are read through
the page's declared ``单位``/``币种``, thousand separators and full-width digits
are folded (``NFKC``), a parenthesized value reads as negative, a point claim
matches a page value within ``TOLERANCE`` relative error, and an interval claim
matches any page value inside its bounds. The folded page text also keeps reusing
``reconciliation._report_numbers``, which folds adjacent bounds into one
``range`` key.

Report tables also state their unit in a header or a row label while the cells
stay bare: the 增减 column holds ``15.71`` under a ``(%)`` heading, and a per-share
row reads ``基本每股收益（元／股） 68.64 ...``. Those cells are read through the
page-level percentage context and through units stated inline on the same row, so
a claim such as ``+15.71%`` or ``68.64 元/股`` finds the page that printed it.
A claim whose unit is a bare Chinese magnitude (``万亿``/``亿``/``万``/``千``) is
keyed as an amount at full scale, matching the page's rounded restatements.

A claim that already carries a model-supplied ``source_locator`` is never
overwritten, and a claim no page covers keeps its level while recording
``locator_attempt="not_found"`` for the next round. No page text and no dollar of
matching is model-generated; the only thing left to the model is semantics. Page
provenance never loosens: only passages of this run's ``original_obtained``
documents count, and only at a page number the document declares. Every obtained
page of the run is normalized once, before any claim is matched; the page bound
below is a pathological-document safety valve, not a coverage window.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from .evidence_gate import _financial_key, claim_involves_financial_number
from .reconciliation import _covered_measurement, _measure_key, _report_numbers, claim_measure_key

_LOCATABLE_LEVELS = frozenset({"unverified", "secondhand"})
_RANGE = "range"
NOT_FOUND = "not_found"
ORIGIN = "server_locator"
# The claim schema bounds every passage-id list to this many entries.
_MAX_SUPPORTS = 20
# Safety valve for a pathological document, not a coverage window: a production
# annual report reaches 143 pages and a match on page 128 is real. Every obtained
# page is still normalized exactly once, before the claims are matched.
_MAX_LOCATE_PAGES = 600
# Relative tolerance between a report display and an exact page value: a report
# rounds to the digits it prints, so 1,708.99 亿元 must reach 170,899,152,276.34.
TOLERANCE = Decimal("0.005")
# A claim may state a bare magnitude (value "1.71", unit "万亿") instead of a
# currency unit; the page prints the matching rounded restatement.
_CLAIM_SCALES = {
    "万亿": Decimal("1000000000000"),
    "亿": Decimal("100000000"),
    "万": Decimal("10000"),
    "千": Decimal("1000"),
}

_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,80})(?:\.\d{1,24})?"
_BARE_NUMBER = re.compile(rf"(?<![\d.,])(?P<number>{_NUMBER})(?![\d]|\.\d)")
_PAREN_UNIT_ALTERNATION = (
    r"元/千瓦时|元/平方米|元/千克|元/股|元/吨"
    r"|(?:万亿|亿|万|千)?\s*(?:人民币|美元|港元|欧元|元)"
    r"|(?:万亿|亿|万|千)?\s*(?:吨|股|手|千克|公斤)"
)
# A loss is printed as "(1,234.56) 万元" or "(1,234.56 万元)", never with a sign.
_PAREN_NUMBER = re.compile(rf"[（(]\s*(?P<number>{_NUMBER})\s*(?P<unit>{_PAREN_UNIT_ALTERNATION})?\s*[)）]")
# A page states the unit of a whole table in its header, never on each cell.
_DECLARED_UNIT = re.compile(
    r"单位\s*[：:]\s*(?:(?P<currency>人民币|美元|港元|欧元)\s*)?"
    r"(?P<unit>"
    r"元/千瓦时|元/平方米|元/千克|元/股|元/吨"
    r"|万亿元|亿元|万元|千元|元"
    r"|万吨/年|万吨|千吨|吨/年|吨|千克|公斤"
    r")"
)
_DECLARED_CURRENCY = re.compile(
    r"币种\s*[：:]\s*(?P<currency>人民币|美元|港元|欧元|CNY|RMB|USD|HKD|EUR)",
    re.IGNORECASE,
)
# Units that must stay with the folded extractor: reading the same cell through
# the declared unit would apply a second, wrong scale.
_ATTACHED_SUFFIX = re.compile(
    r"\s*(?:"
    r"元/千瓦时|元/平方米|元/千克|元/股|元/吨"
    r"|(?:万亿|亿|万|千)?\s*(?:人民币|美元|港元|欧元|元)"
    r"|(?:万亿|亿|万|千)?\s*(?:吨|股|手|片|个|台|辆|座|套|桶|度|千克|公斤)"
    r"|(?:万亿|亿|万|千)(?![\u4e00-\u9fffA-Za-z%])"
    r"|[%‰]|个百分点|个基点|倍"
    r"|年|年度|月|日|号|家|名|人|户|位|只|支|条|项|次|份|笔|张"
    r"|公里|千米|平方米|公顷|亩|、|（|\("
    r")"
)
_RANGE_SIGNS = "-–—~至"
_PAREN_UNIT = re.compile(rf"\s*(?P<unit>{_PAREN_UNIT_ALTERNATION})")
_NON_MEASURE_AFTER = re.compile(r"\s*(?:[%‰]|个百分点|个基点|倍|年|年度|月|日|号|季度|季)")
# A table row may state the unit of its cells inline, in the row label or the
# column heading: "基本每股收益（元／股） 68.64 ..." or "加权平均净资产收益率（%） 36.02 ...".
_INLINE_UNIT = re.compile(
    r"(?:"
    r"元/千瓦时|元/平方米|元/千克|元/股|元/吨"
    r"|(?:万亿|亿|万|千)?\s*(?:人民币|美元|港元|欧元|元)"
    r"|(?:万亿|亿|万|千)?\s*(?:吨|股|手|千克|公斤|片)"
    r"|个百分点|个基点|%|‰"
    r")"
)
# A page whose table has a percentage column prints "本期比上年同期增减(%)" in
# the header and leaves the cells bare; the header statement covers the page.
_PERCENT_CONTEXT = re.compile(r"[%‰]|百分")


def auto_locate_claims(claims, documents, passages) -> list[dict]:
    """Associate financial claims with the original page that carries their number.

    Mutates the claim dicts in place and returns the changed ones so the caller
    can persist them. A run without an obtained original page is untouched: the
    evidence gate, not this function, decides what happens to that report.
    """
    pages = _original_pages(documents, passages)
    if not pages:
        return []
    changed = []
    for claim in claims:
        if not _locatable(claim):
            continue
        try:
            claim_keys = _claim_keys(claim)
        except Exception:  # noqa: BLE001 - one malformed claim never blocks the run
            continue
        if not claim_keys:
            continue
        located = next(
            (
                (document_id, page, passage_id)
                for document_id, page, page_numbers, page_points, passage_id in pages
                if _page_covers(page_numbers, page_points, claim_keys)
            ),
            None,
        )
        if located is None:
            claim["locator_attempt"] = NOT_FOUND
        else:
            document_id, page, passage_id = located
            claim["source_locator"] = {"document_id": document_id, "page": page}
            claim["verification"] = "verified_source"
            claim["origin"] = ORIGIN
            _attach_page_support(claim, passage_id)
            # A claim located on this pass no longer carries an earlier miss.
            claim.pop("locator_attempt", None)
        changed.append(claim)
    return changed


def _attach_page_support(claim: dict, passage_id: str) -> None:
    """Record the located page as a support and settle the claim's citation.

    The ledger only validates ``supports`` when a claim is registered; a claim
    that auto-location fixes afterwards must be completed here, or the stored
    claim keeps ``support_missing``/``search_snippet_only`` and the run's
    ``citation_checks_incomplete`` gap survives even though the page was read.
    The page passage goes first because it is the verified evidence; the model's
    own supports follow, deduplicated and bounded by the schema's 20-id cap.
    Only the gaps this page objectively resolves are removed: quote mismatches
    and the numeric-basis checks stay, so the citation never claims more than
    the evidence supports.
    """
    existing = claim.get("supports")
    supports = [pid for pid in existing if isinstance(pid, str)] if isinstance(existing, list) else []
    claim["supports"] = [passage_id, *[pid for pid in supports if pid != passage_id]][:_MAX_SUPPORTS]
    gaps = claim.get("gaps")
    if not isinstance(gaps, list):
        return
    gaps = [gap for gap in gaps if gap not in {"support_missing", "search_snippet_only"}]
    claim["gaps"] = gaps
    claim["citation_status"] = (
        "invalid" if "quote_not_in_passage" in gaps else ("partial" if gaps else "valid")
    )


def _locatable(claim) -> bool:
    """True for a claim with a page-locatable value that still lacks a source page.

    The money/trading families are attempted for every claim. A server-extracted
    report number is attempted beyond them as well: production runs lost
    ``ratio`` (``+15.71%``), ``unit:元/股``, ``unit:个百分点`` and ``mass_t``
    claims because a financial-only gate never looked for them, while the ledger
    still reported the run as ``citation_checks_incomplete``. Those claims carry
    the server's normalized ``claim_key``, so the locator can read them without
    re-parsing prose. A claim with no value, or one a model already located, is
    left alone.
    """
    if not isinstance(claim, dict):
        return False
    if claim.get("verification") not in _LOCATABLE_LEVELS:
        return False
    locator = claim.get("source_locator")
    if isinstance(locator, dict) and locator:
        return False
    if claim.get("value") is None:
        return False
    if claim_involves_financial_number(claim):
        return True
    try:
        if _financial_key(_value_measure_key(claim)):
            return True
    except Exception:  # noqa: BLE001 - an unreadable value is simply not locatable
        pass
    return claim.get("origin") == "server_extract" and isinstance(claim.get("claim_key"), list)


def _claim_keys(claim) -> list[tuple]:
    """Measurements one claim asserts: its point value plus any statement interval."""
    keys: list[tuple] = []
    try:
        primary = claim_measure_key(claim)
    except Exception:  # noqa: BLE001 - fall back to the server's own value parser
        primary = None
    if primary is None:
        primary = _value_measure_key(claim)
    if primary is not None:
        keys.append(_full_scale_key(primary))
    statement = claim.get("statement")
    if isinstance(statement, str) and statement.strip():
        try:
            statement_numbers = _report_numbers(statement)
        except Exception:  # noqa: BLE001 - a broken statement contributes no interval
            statement_numbers = {}
        # ``_stored_claim_key`` keeps only the low bound of a range, so the claim
        # text is the only source of the interval a report claim asserts.
        keys.extend(key for key in statement_numbers if _is_range(key))
    return keys


def _value_measure_key(claim) -> tuple | None:
    number = _claim_number(claim.get("value"))
    if number is None:
        return None
    try:
        key = _measure_key(number, claim.get("unit"), claim.get("currency"))
    except Exception:  # noqa: BLE001 - an unsupported unit is not a locatable measurement
        return None
    return _full_scale_key(key) if isinstance(key, tuple) else None


def _full_scale_key(key: tuple) -> tuple:
    """Key a bare magnitude (``1.71 万亿``) as an amount at full scale.

    The page prints the matching rounded restatement as an amount
    ("1.71 万亿元" / "1,710,000,000,000") even though the claim carried no
    currency unit of its own.
    """
    if _is_point(key) and isinstance(key[0], str) and key[0].startswith("unit:"):
        scale = _CLAIM_SCALES.get(key[0][len("unit:") :])
        if scale is not None:
            return ("amount", None, key[2] * scale)
    return key


def _claim_number(value) -> Decimal | None:
    """Parse a claim's stored value, including thousand separators and ``(x)``."""
    if isinstance(value, bool):
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    parenthesized = _PAREN_NUMBER.fullmatch(text)
    if parenthesized is not None:
        number = _numeric_value(parenthesized.group("number"))
        return -number if number is not None else None
    return _numeric_value(text)


def _original_pages(documents, passages) -> list[tuple[str, int, tuple, tuple, str]]:
    """(document_id, page, folded page numbers, declared-unit points, passage id).

    Reads every obtained page of the run exactly once and normalizes it here, so a
    claim is matched against the whole document instead of the pages a model
    happened to quote. ``_MAX_LOCATE_PAGES`` only bounds a pathological document.
    """
    originals = {}
    for document in documents if isinstance(documents, (list, tuple)) else ():
        if not isinstance(document, dict) or document.get("original_obtained") is not True:
            continue
        document_id = document.get("document_id")
        page_count = document.get("page_count")
        if not isinstance(document_id, str):
            continue
        if isinstance(page_count, bool) or not isinstance(page_count, int) or not 1 <= page_count <= 2000:
            continue
        originals[document_id] = document
    pages = []
    for passage in passages if isinstance(passages, (list, tuple)) else ():
        if not isinstance(passage, dict):
            continue
        document = originals.get(passage.get("document_id"))
        if document is None:
            continue
        locator = passage.get("locator")
        page = locator.get("page") if isinstance(locator, dict) else None
        if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= document["page_count"]:
            continue
        excerpt = passage.get("excerpt")
        if not isinstance(excerpt, str) or not excerpt.strip():
            continue
        passage_id = passage.get("passage_id")
        if not isinstance(passage_id, str) or not passage_id:
            continue
        try:
            numbers = tuple(_report_numbers(excerpt))
            points = _page_points(excerpt)
        except Exception:  # noqa: BLE001 - an unparsable page is simply not a candidate
            continue
        if numbers or points:
            pages.append((document["document_id"], page, numbers, points, passage_id))
        if len(pages) >= _MAX_LOCATE_PAGES:
            break
    return pages


def _page_points(excerpt: str) -> tuple[tuple, ...]:
    """Normalized table cells read through the page's declared or inline unit.

    Three writings of the same measurement are covered: a bare cell under the
    page's ``单位：``/``币种：`` header, a bare cell under an inline row/column
    unit, and a cell whose own value carries the unit (the folded extractor keeps
    that one). A page that prints a ``(%)`` heading reads its bare cells as
    percentages too, so ``+15.71%`` finds the column the header names.
    """
    text = unicodedata.normalize("NFKC", excerpt)
    units = _page_units(text)
    percent_context = _PERCENT_CONTEXT.search(text) is not None
    points: list[tuple] = []
    if units or percent_context:
        for match in _BARE_NUMBER.finditer(text):
            raw = match.group("number")
            number = _numeric_value(raw)
            start, end = match.span()
            if (
                number is None
                or _year_like(raw)
                or _ordinal_prefix(text, start)
                or _heading_number(text, start, end)
            ):
                continue
            if _inside_parentheses(text, start, end) or _range_bound(text, end):
                continue
            if _ATTACHED_SUFFIX.match(text, end):
                continue
            points.extend((dimension, currency, number * scale) for dimension, currency, scale in units)
            if percent_context:
                ratio = _measure_key(number, "%", None)
                if _is_point(ratio):
                    points.append(ratio)
    points.extend(_inline_unit_points(text))
    for match in _PAREN_NUMBER.finditer(text):
        number = _numeric_value(match.group("number"))
        if number is None or _ordinal_prefix(text, match.start()):
            continue
        unit = match.group("unit")
        if unit is None:
            after = _PAREN_UNIT.match(text, match.end())
            unit = after.group("unit") if after is not None else None
        if unit is not None:
            try:
                key = _measure_key(-number, unit, None)
            except Exception:  # noqa: BLE001 - an unsupported unit is a non-match
                continue
            if _is_point(key):
                points.append(key)
            continue
        if _NON_MEASURE_AFTER.match(text, match.end()) is not None:
            continue
        points.extend((dimension, currency, -number * scale) for dimension, currency, scale in units)
    return tuple(points)


def _inline_unit_points(text: str) -> list[tuple]:
    """Points for cells read through a unit stated inline on the same row."""
    points: list[tuple] = []
    for match in _BARE_NUMBER.finditer(text):
        start, end = match.span()
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", end)
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end]
        position = start - line_start
        marker = None
        for unit_match in _INLINE_UNIT.finditer(line):
            if unit_match.end() > position:
                break
            marker = unit_match.group()
        if marker is None:
            continue
        raw = match.group("number")
        number = _numeric_value(raw)
        if (
            number is None
            or _year_like(raw)
            or _ordinal_prefix(text, start)
            or _heading_number(text, start, end)
            or _inside_parentheses(text, start, end)
            or _range_bound(text, end)
            or _ATTACHED_SUFFIX.match(text, end)
        ):
            continue
        try:
            key = _measure_key(number, marker.strip(), None)
        except Exception:  # noqa: BLE001 - an unsupported inline unit is a non-match
            continue
        if _is_point(key):
            points.append(key)
    return points


def _page_units(text: str) -> tuple[tuple, ...]:
    """Unit keys (dimension, currency, scale) declared by the page under ``单位``."""
    currency_match = _DECLARED_CURRENCY.search(text)
    declared_currency = currency_match.group("currency") if currency_match is not None else None
    units: list[tuple] = []
    for match in _DECLARED_UNIT.finditer(text):
        currency = match.group("currency") or declared_currency
        try:
            key = _measure_key(Decimal(1), match.group("unit"), currency)
        except Exception:  # noqa: BLE001 - an unsupported declared unit is ignored
            continue
        if _is_point(key):
            units.append(key)
    return tuple(dict.fromkeys(units))


def _page_covers(page_numbers, page_points, claim_keys) -> bool:
    """True when one page measurement reaches at least one claim measurement."""
    for claim_key in claim_keys:
        if _is_range(claim_key):
            if _range_covered(claim_key, page_numbers, page_points):
                return True
        elif _point_covered(claim_key, page_numbers, page_points):
            return True
    return False


def _point_covered(claim_key, page_numbers, page_points) -> bool:
    if not _is_point(claim_key):
        return False
    for page_number in page_numbers:
        try:
            if _covered_measurement(page_number, {claim_key}):
                return True
        except Exception:  # noqa: BLE001 - a malformed page key is a non-match
            continue
        if _is_range(page_number):
            if _point_inside(claim_key, page_number):
                return True
        elif _near(page_number, claim_key):
            return True
    return any(_near(point, claim_key) for point in page_points)


def _range_covered(claim_range, page_numbers, page_points) -> bool:
    try:
        _tag, dimension, currency, low, high = claim_range
    except (TypeError, ValueError):
        return False
    if not isinstance(low, Decimal) or not isinstance(high, Decimal):
        return False
    for page_number in page_numbers:
        if _is_range(page_number):
            if _ranges_overlap(claim_range, page_number):
                return True
        elif (
            _is_point(page_number)
            and _same_measurement(page_number, dimension, currency)
            and low <= page_number[2] <= high
        ):
            return True
    return any(
        _same_measurement(point, dimension, currency) and low <= point[2] <= high for point in page_points
    )


def _point_inside(point, page_range) -> bool:
    """True when one claim point falls inside a page range (currency-tolerant)."""
    if not _is_point(point) or not _is_range(page_range):
        return False
    dimension, currency, low, high = page_range[1:]
    if not isinstance(low, Decimal) or not isinstance(high, Decimal):
        return False
    if not _same_measurement(point, dimension, currency):
        return False
    return low <= point[2] <= high


def _ranges_overlap(left, right) -> bool:
    try:
        _tag, dimension, currency, low, high = left
        _tag, other_dimension, other_currency, other_low, other_high = right
    except (TypeError, ValueError):
        return False
    if not all(isinstance(bound, Decimal) for bound in (low, high, other_low, other_high)):
        return False
    if dimension != other_dimension or not _currencies_compatible(currency, other_currency):
        return False
    return low <= other_high and other_low <= high


def _same_measurement(key, dimension, currency) -> bool:
    return key[0] == dimension and _currencies_compatible(key[1], currency)


def _near(left, right) -> bool:
    """True when two point measurements differ by at most TOLERANCE relative error."""
    if not _is_point(left) or not _is_point(right):
        return False
    if left[0] != right[0] or not _currencies_compatible(left[1], right[1]):
        return False
    left_value, right_value = left[2], right[2]
    if left_value == right_value:
        return True
    scale = max(abs(left_value), abs(right_value))
    return scale > 0 and abs(left_value - right_value) <= TOLERANCE * scale


def _currencies_compatible(left, right) -> bool:
    return left is None or right is None or left == right


def _is_point(key) -> bool:
    return (
        isinstance(key, tuple)
        and len(key) == 3
        and isinstance(key[0], str)
        and (key[1] is None or isinstance(key[1], str))
        and isinstance(key[2], Decimal)
    )


def _is_range(key) -> bool:
    return isinstance(key, tuple) and len(key) == 5 and key[0] == _RANGE


def _numeric_value(raw: str) -> Decimal | None:
    try:
        number = Decimal(raw.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _year_like(raw: str) -> bool:
    if not raw.isdigit() or len(raw) != 4:
        return False
    return 1900 <= int(raw) <= 2199


def _ordinal_prefix(text: str, start: int) -> bool:
    return text[:start].rstrip().endswith("第")


def _heading_number(text: str, start: int, end: int) -> bool:
    """True for a section number such as ``3.1`` that opens a text line."""
    prefix = text[text.rfind("\n", 0, start) + 1 : start]
    if prefix.strip():
        return False
    after = text[end:].lstrip()
    return after[:1].isalpha() and "," not in text[start:end]


def _range_bound(text: str, end: int) -> bool:
    """True for a range bound such as the ``1`` and ``3`` in ``(1-3 月份)``."""
    following = text[end : end + 1]
    return bool(following) and following in _RANGE_SIGNS


def _inside_parentheses(text: str, start: int, end: int) -> bool:
    return text[:start].rstrip().endswith(("(", "（")) and text[end:].lstrip().startswith((")", "）"))
