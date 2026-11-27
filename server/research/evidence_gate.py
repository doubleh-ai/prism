"""Hard report-level gate: financial/trading numbers need an original document.

``reconciliation`` measures how far the report's numbers are covered by claims and
deliberately never gates the report. The gap that produced unreproducible runs was
the opposite: a run could register secondhand claims about financial numbers, skip
``source_original`` entirely, and still publish the report body. This module adds
the server-side constraint, outside the prompt: the finalizer withholds the report
body unless the run obtained at least one original document (``original_obtained``).

The predicate is deliberately narrow. It classifies the numbers a claim carries,
not the research topic: money amounts (元/万元/亿元/元每吨 ...), explicit currency
codes and share/lot trading quantities (股/手). A policy interpretation that only
cites rates, capacities, masses, dates or plain prose is not a financial report and
is never blocked by this gate.

The report body itself is a source of truth, not only the claims a model chose to
register: the finalizer extracts one server claim per unregistered report number
before freezing a report (see ``report_number_claims``), and the predicates accept
the body text so a run whose model never registered or upgraded a claim is still
withheld. The classification rule is shared with that server registration.
"""

from __future__ import annotations

import re

from .reconciliation import _claim_key, _report_numbers, _unit_text, report_number_claims

# ``quantities`` normalizes monetary units to dimensions named ``amount*``
# (amount, amount_per_t); an explicit currency code is equally decisive.
_MONEY_DIMENSION = "amount"
# Trading lots and share counts are transaction numbers even without a currency.
_TRADING_UNIT = re.compile(r"(?:万亿|亿|万|千)?(?:股|手)\Z")
_TRADING_NUMBER = re.compile(
    r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d{1,80})(?:\.\d{1,24})?\s*(?:万亿|亿|万|千)?\s*(?:股|手)"
)
_CURRENCY_NUMBER = re.compile(
    r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+|\d{1,80})(?:\.\d{1,24})?\s*(?:CNY|RMB|USD|HKD|EUR)\b",
    re.IGNORECASE,
)
_RANGE = "range"


def _financial_key(key) -> bool:
    """True when one normalized measurement key is monetary or a trading quantity."""
    if not isinstance(key, tuple) or len(key) < 2:
        return False
    if key[0] == _RANGE and len(key) == 5:
        dimension, currency = key[1], key[2]
    else:
        dimension, currency = key[0], key[1]
    if isinstance(dimension, str) and dimension.startswith(_MONEY_DIMENSION):
        return True
    if currency is not None:
        return True
    # An unsupported unit such as 手/股 keeps a ``unit:<text>`` dimension.
    return (
        isinstance(dimension, str)
        and dimension.startswith("unit:")
        and bool(_TRADING_UNIT.fullmatch(dimension[5:]))
    )


def claim_involves_financial_number(claim) -> bool:
    """True when one registered claim states a financial or trading number.

    Sources of truth, in order: the claim's own unit (trading lots/shares), its
    normalized numeric key (money dimension or currency), and, for value-less
    claims, the monetary/trading numbers found in its statement. Percentages,
    mass/capacity quantities, dates and prose do not qualify.
    """
    if not isinstance(claim, dict):
        return False
    if isinstance(claim.get("unit"), str) and _TRADING_UNIT.fullmatch(_unit_text(claim["unit"])):
        return True
    if _financial_key(_claim_key(claim)):
        return True
    statement = claim.get("statement")
    if not isinstance(statement, str):
        return False
    if _TRADING_NUMBER.search(statement) or _CURRENCY_NUMBER.search(statement):
        return True
    return any(_financial_key(key) for key in _report_numbers(statement))


def _report_body_claims(text, claims) -> list[dict]:
    """Server-extracted claims for the numbers already registered claims miss.

    This is the same rule the ledger uses to register report-body claims, so the
    gate classifies the same measurements the report will carry. Registration is
    best effort: a broken extractor must not block finalization, and the
    persisted claims still classify every number the model registered itself.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    registered = claims if isinstance(claims, (list, tuple)) else ()
    try:
        return report_number_claims(text, registered)
    except Exception:  # noqa: BLE001 - a broken extractor must not crash finalization
        return []


def report_needs_original(claims, text: str | None = None) -> bool:
    """True when the report states at least one financial or trading number.

    ``claims`` is the run's registered claim list; ``text`` is the final report
    body. The body's own numbers are classified with the server extraction rule,
    so a report that states a financial number is gated even when no claim was
    registered or upgraded for it before finalization.
    """
    if isinstance(claims, list) and any(claim_involves_financial_number(claim) for claim in claims):
        return True
    return any(claim_involves_financial_number(claim) for claim in _report_body_claims(text, claims))


def original_document_obtained(documents) -> bool:
    """True when this run holds at least one server-verified original document."""
    if not isinstance(documents, list):
        return False
    return any(
        document.get("original_obtained") is True for document in documents if isinstance(document, dict)
    )


def original_evidence_missing(claims, documents, text: str | None = None) -> bool:
    """The gate: a financial report without any obtained original is withheld.

    ``claims`` and ``documents`` are the run's persisted records at finalize
    time; ``text`` is the report body, whose numbers are classified even when the
    server registration has not run for them yet. The gate does not depend on a
    claim's declared verification level: secondhand and unverified claims count
    the same as first-hand claims, so a run that only cites web text cannot
    publish a financial report. When this returns True the finalizer must not
    freeze a report body; it records an evidence-insufficient result instead.
    """
    return report_needs_original(claims, text) and not original_document_obtained(documents)
