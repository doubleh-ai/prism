"""Scenario-specific verification bar for research reports.

A single-subject study and a cross-category synthesis do not carry the same share
of numbers that an original page states word for word: a synthesis adds derived
spreads, changes and background figures that no single source prints. One global
bar therefore either lets shallow single-subject work through or fails honest
synthesis. The bar is selected by an objective scenario classification instead.

The scenario is decided from server-created documents and the user's own question
text, never from model prose: the claim ``entities`` field is model-declared and
is not evidence. The same run therefore classifies the same way when replayed
from history. The classification and the gate are advisory: a report below its
scenario threshold is still produced, the run's gaps name the condition.
"""

from __future__ import annotations

import math
import re

SCHEMA = "prism.research-quality-gate.v1"
SINGLE_SUBJECT = "single_subject"
MULTI_SUBJECT = "multi_subject"
SCENARIOS = (SINGLE_SUBJECT, MULTI_SUBJECT)

# A single-subject study keeps most numbers on one company's or product's own
# pages. A cross-category synthesis derives spreads, changes and comparisons, so
# its verified share is structurally lower. Both lines are calibrated on real
# runs: single-subject filings research reached 0.89, the cross-category futures
# run 0.43.
SCENARIO_THRESHOLDS = {SINGLE_SUBJECT: 0.8, MULTI_SUBJECT: 0.4}

# Gap names that describe quality statistics only. They stay visible in the run's
# gaps, but they never block the report and never downgrade the acceptance status:
# the run finished, its numbers were checked, and the bar is a reporting target.
ADVISORY_GAPS = frozenset({"quality_below_scenario_threshold"})

# One market-data channel is one research category (quote, spread, inventory,
# basis, ...). Two or more channels over the same product mean the run synthesised
# several categories rather than reading one dataset.
CROSS_CATEGORY_CHANNELS = 2

# A-share/ETF security code: 000001, 600519, 688981, 830799 ... A year-month such
# as 202411 is not a code because the first digit is excluded.
_SECURITY_CODE = re.compile(r"(?<!\d)[03468]\d{5}(?!\d)")


def _snapshot_evidence(documents) -> tuple[set[str], set[str]]:
    """Distinct products and data channels among this run's market snapshots."""
    symbols: set[str] = set()
    channels: set[str] = set()
    for document in documents if isinstance(documents, (list, tuple)) else ():
        if not isinstance(document, dict) or document.get("source_kind") != "market_data_snapshot":
            continue
        request = document.get("request")
        if isinstance(request, dict):
            symbol = request.get("symbol")
            if isinstance(symbol, str) and symbol.strip():
                symbols.add(symbol.strip().upper())
        channel = document.get("access_method")
        if isinstance(channel, str) and channel.strip():
            channels.add(channel.strip())
    return symbols, channels


def _question_products(question: str) -> set[str]:
    """Futures products named in the user's question, from the public registry.

    Only product names are matched, not bare one-letter codes: an ``M`` or ``T``
    token would also match ordinary prose. A registry import failure degrades the
    heuristic, never the report.
    """
    if not isinstance(question, str) or not question:
        return set()
    try:
        from ..futures import PRODUCTS
    except Exception:  # noqa: BLE001 - classification must not cost the report
        return set()
    found: set[str] = set()
    for symbol, row in PRODUCTS.items():
        name = row[0] if isinstance(row, (list, tuple)) and row else None
        if isinstance(name, str) and name and name in question:
            found.add(symbol)
    return found


def scenario_facts(question: str, documents) -> dict:
    """Objective evidence behind one scenario decision, kept for audit."""
    symbols, channels = _snapshot_evidence(documents)
    products = _question_products(question)
    codes = {match.group(0) for match in _SECURITY_CODE.finditer(question or "")}
    subjects = sorted(symbols | products | codes)
    return {
        "subjects": subjects,
        "market_symbols": sorted(symbols),
        "data_channels": sorted(channels),
        "products": sorted(products),
        "security_codes": sorted(codes),
        "documents": len(documents) if isinstance(documents, (list, tuple)) else 0,
    }


def classify_scenario(question: str, documents) -> dict:
    """Classify one run from its receipts; returns the scenario and its facts.

    ``multi_subject`` covers multi-subject comparison and cross-category
    synthesis, which share the same structural effect on the verified share.
    """
    facts = scenario_facts(question, documents)
    multi = len(facts["subjects"]) >= 2 or len(facts["data_channels"]) >= CROSS_CATEGORY_CHANNELS
    return {"scenario": MULTI_SUBJECT if multi else SINGLE_SUBJECT, "facts": facts}


def scenario_threshold(scenario: str) -> float | None:
    """The verification bar for a scenario; unknown scenarios have no bar."""
    return SCENARIO_THRESHOLDS.get(scenario)


def quality_gate(scenario: str, verified_claim_ratio) -> dict:
    """Evaluate one run against its scenario bar.

    A missing ratio (blocked revision, degraded reconciliation) is
    ``not_applicable`` rather than a failure: no claim was evaluated.
    """
    threshold = SCENARIO_THRESHOLDS.get(scenario)
    ratio = verified_claim_ratio
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)):
        ratio = None
    else:
        ratio = float(ratio)
    gate = {
        "schema": SCHEMA,
        "scenario": scenario,
        "threshold": threshold,
        "verified_claim_ratio": ratio,
        "passed": None,
        "status": "unknown_scenario" if threshold is None else "not_applicable",
    }
    if threshold is not None and ratio is not None:
        gate["passed"] = ratio >= threshold
        gate["status"] = "ok"
    return gate


def is_below_bar(gate: dict) -> bool:
    """Whether an evaluated gate failed; advisory only, it never blocks a report."""
    return gate.get("status") == "ok" and gate.get("passed") is False
