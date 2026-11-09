"""Closed diagnostic hints, never source paths, user content or execution grants."""

from __future__ import annotations

import re
from typing import Literal

RepairScope = Literal[
    "unknown",
    "research-interface",
    "research-runtime",
    "knowledge-records",
    "research-excerpts",
    "futures-interface",
    "futures-fundamentals",
    "futures-soybean",
    "futures-gold",
    "futures-contracts",
    "work-runtime",
    "work-projects",
    "work-reminders",
    "work-calendar",
    "profile-risk",
    "industry-evidence",
    "market-data",
    "screening",
    "indicators",
    "single-stock",
    "deep-research",
    "admin-console",
]
SCOPE_FEATURES = {
    "research-interface": "chat",
    "research-runtime": "chat",
    "knowledge-records": "reports",
    "research-excerpts": "reports",
    "futures-interface": "futures",
    "futures-fundamentals": "futures",
    "futures-soybean": "futures",
    "futures-gold": "futures",
    "futures-contracts": "futures",
    "work-runtime": "work",
    "work-projects": "work",
    "work-reminders": "work",
    "work-calendar": "calendar",
    "profile-risk": "preferences",
    "industry-evidence": "industry",
    "market-data": "market",
    "screening": "screener",
    "indicators": "indicators",
    "single-stock": "analysis",
    "deep-research": "vibe",
    "admin-console": "admin",
}


def normalize_scope(value):
    return value if type(value) is str and value in SCOPE_FEATURES else "unknown"


def scope_from_api_route(route):
    """Return an enum only. Do not retain or return the route's identifiers."""
    if type(route) is not str or len(route) > 240 or "?" in route or "#" in route:
        return "unknown"
    if re.fullmatch(r"/api/futures/(?:AU|CU|RB|SC|M)/snapshot", route):
        return "futures-interface"
    if route == "/api/preferences/research" or re.fullmatch(r"/api/portfolio(?:/[a-z0-9_-]+)*", route):
        return "profile-risk"
    if re.fullmatch(r"/api/knowledge/(?:items|search)(?:/[A-Za-z0-9_-]+)*", route):
        return "knowledge-records"
    if re.fullmatch(r"/api/chat(?:/[A-Za-z0-9_-]+)*", route):
        return "research-runtime"
    return "unknown"


def signal_scope(value, *, feature=None):
    """Omit unknown/unattributed hints to preserve the legacy wire format."""
    scope = normalize_scope(value)
    if scope == "unknown" or (feature is not None and SCOPE_FEATURES[scope] != feature):
        return {}
    return {"repair_scope": scope}
