"""Per-user live context layered on top of the curated industry graph.

The structural graph remains evidence-backed. This module reads other Prism
features and produces a transient signal layer without inventing supply-chain
edges from weak correlations.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from typing import Any

from .database import get_db

_TICKER_RE = re.compile(r"(?<!\d)([034689]\d{5})(?!\d)")
_SOURCE_ORDER = {"analysis": 0, "vibe": 1, "report": 2, "watchlist": 3}
_SOURCE_LABELS = {
    "analysis": "Astock 分析",
    "vibe": "Vibe 研究",
    "report": "报告库",
    "watchlist": "自选关注",
}


def _ticker(value: Any) -> str:
    match = _TICKER_RE.search(str(value or "").upper())
    return match.group(1) if match else ""


def _text(value: Any, limit: int = 180) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).replace("\x00", " ").split())
    return text[:limit]


def _tone(signal: str) -> str:
    value = (signal or "").strip().lower()
    if any(token in value for token in ("buy", "bull", "long", "买入", "看多", "增持")):
        return "up"
    if any(token in value for token in ("sell", "bear", "short", "卖出", "看空", "减持")):
        return "down"
    return "neutral"


def build_industry_context(username: str) -> dict[str, Any]:
    """Aggregate a user's recent cross-feature evidence by A-share ticker."""
    now = int(time.time())
    tickers: dict[str, dict[str, Any]] = {}
    source_totals = {key: 0 for key in _SOURCE_ORDER}

    def register(
        ticker: Any,
        *,
        name: Any = "",
        source: str,
        at: Any = 0,
        label: Any = "",
        detail: Any = "",
        signal: Any = "",
        target_id: Any = "",
    ) -> None:
        code = _ticker(ticker)
        if not code or source not in _SOURCE_ORDER:
            return
        timestamp = int(at or 0)
        item = tickers.setdefault(
            code,
            {
                "ticker": code,
                "name": "",
                "latest_at": 0,
                "latest_signal": "",
                "signal_tone": "neutral",
                "source_counts": defaultdict(int),
                "evidence": [],
            },
        )
        clean_name = _text(name, 80)
        if clean_name and (not item["name"] or timestamp >= item["latest_at"]):
            item["name"] = clean_name
        clean_signal = _text(signal, 32)
        item["source_counts"][source] += 1
        item["latest_at"] = max(item["latest_at"], timestamp)
        if clean_signal and timestamp >= int(item.get("latest_signal_at") or 0):
            item["latest_signal"] = clean_signal
            item["signal_tone"] = _tone(clean_signal)
            item["latest_signal_at"] = timestamp
        item["evidence"].append(
            {
                "source": source,
                "source_label": _SOURCE_LABELS[source],
                "label": _text(label, 100) or _SOURCE_LABELS[source],
                "detail": _text(detail, 280),
                "signal": clean_signal,
                "tone": _tone(clean_signal),
                "at": timestamp,
                "target_id": _text(target_id, 128),
            }
        )

    with get_db() as db:
        analyses = db.execute(
            """
            SELECT id, ticker, name, signal, mode, updated_at
            FROM analysis_cache
            WHERE username=?
            ORDER BY updated_at DESC
            LIMIT 120
            """,
            [username],
        ).fetchall()
        for row in analyses:
            source_totals["analysis"] += 1
            register(
                row["ticker"],
                name=row["name"],
                source="analysis",
                at=row["updated_at"],
                signal=row["signal"],
                target_id=row["id"],
                label=f"{row['mode'] or 'deep'} 分析",
                detail=f"最新评级：{row['signal'] or '未评级'}",
            )

        reports = db.execute(
            """
            SELECT id, ticker, name, title, signal, summary, updated_at
            FROM saved_reports
            WHERE username=? AND ticker IS NOT NULL AND ticker != ''
            ORDER BY updated_at DESC
            LIMIT 120
            """,
            [username],
        ).fetchall()
        for row in reports:
            source_totals["report"] += 1
            register(
                row["ticker"],
                name=row["name"],
                source="report",
                at=row["updated_at"],
                signal=row["signal"],
                target_id=row["id"],
                label=row["title"] or "已保存报告",
                detail=row["summary"],
            )

        plans = db.execute(
            """
            SELECT id, plan_json, created_at
            FROM vibe_plans
            WHERE username=?
            ORDER BY created_at DESC
            LIMIT 60
            """,
            [username],
        ).fetchall()
        for row in plans:
            try:
                plan = json.loads(row["plan_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if plan.get("status") != "complete":
                continue
            timestamp = int(plan.get("updated_at") or row["created_at"] or 0)
            for candidate in (plan.get("candidates") or [])[:20]:
                if not isinstance(candidate, dict) or not _ticker(candidate.get("ticker")):
                    continue
                source_totals["vibe"] += 1
                register(
                    candidate.get("ticker"),
                    name=candidate.get("name"),
                    source="vibe",
                    at=timestamp,
                    target_id=row["id"],
                    label=candidate.get("role") or plan.get("title") or "Vibe 候选",
                    detail=candidate.get("why") or plan.get("agent_summary") or "",
                )

        watch_rows = db.execute(
            """
            SELECT target_id, target_label, action, created_at
            FROM activity_events
            WHERE username=? AND feature='market'
              AND action IN ('watchlist_add', 'watchlist_remove')
            ORDER BY created_at DESC, id DESC
            LIMIT 500
            """,
            [username],
        ).fetchall()
        seen_watch: set[str] = set()
        for row in watch_rows:
            code = _ticker(row["target_id"])
            if not code or code in seen_watch:
                continue
            seen_watch.add(code)
            if row["action"] != "watchlist_add":
                continue
            source_totals["watchlist"] += 1
            register(
                code,
                name=row["target_label"],
                source="watchlist",
                at=row["created_at"],
                label="当前自选关注",
                detail="来自行情看板的自选操作",
            )

        link_rows = db.execute(
            """
            SELECT id, category, source_ticker
            FROM industry_nodes
            WHERE source_ticker IS NOT NULL AND source_ticker != ''
            """
        ).fetchall()

    links: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {
            "categories": set(),
            "node_ids": set(),
        }
    )
    for row in link_rows:
        code = _ticker(row["source_ticker"])
        if not code:
            continue
        links[code]["categories"].add(row["category"])
        links[code]["node_ids"].add(row["id"])

    recent_events: list[dict[str, Any]] = []
    category_context: dict[str, dict[str, Any]] = {}
    linked_count = 0
    for code, item in tickers.items():
        item["source_counts"] = dict(item["source_counts"])
        item["sources"] = sorted(item["source_counts"], key=lambda key: _SOURCE_ORDER[key])
        item["evidence"].sort(key=lambda event: event["at"], reverse=True)
        item["evidence"] = item["evidence"][:8]
        item.pop("latest_signal_at", None)
        linked = links.get(code)
        item["categories"] = sorted(linked["categories"]) if linked else []
        item["node_ids"] = sorted(linked["node_ids"]) if linked else []
        item["linked"] = bool(linked)
        if linked:
            linked_count += 1
            for category in item["categories"]:
                context = category_context.setdefault(
                    category,
                    {
                        "active_tickers": 0,
                        "latest_at": 0,
                        "source_counts": defaultdict(int),
                        "signals": [],
                    },
                )
                context["active_tickers"] += 1
                context["latest_at"] = max(context["latest_at"], item["latest_at"])
                for source, count in item["source_counts"].items():
                    context["source_counts"][source] += count
                if item["latest_signal"]:
                    context["signals"].append(
                        {
                            "ticker": code,
                            "name": item["name"],
                            "signal": item["latest_signal"],
                            "tone": item["signal_tone"],
                            "at": item["latest_at"],
                        }
                    )
        for event in item["evidence"][:3]:
            recent_events.append(
                {
                    **event,
                    "ticker": code,
                    "name": item["name"] or code,
                    "linked": item["linked"],
                    "categories": item["categories"],
                }
            )

    for context in category_context.values():
        context["source_counts"] = dict(context["source_counts"])
        context["signals"].sort(key=lambda item: item["at"], reverse=True)
        context["signals"] = context["signals"][:4]

    recent_events.sort(key=lambda event: event["at"], reverse=True)
    ordered_tickers = sorted(tickers.values(), key=lambda item: item["latest_at"], reverse=True)
    unlinked = [item for item in ordered_tickers if not item["linked"]][:20]
    coverage = round(linked_count * 100 / len(tickers)) if tickers else 0

    return {
        "generated_at": now,
        "auto_refresh_seconds": 90,
        "summary": {
            "active_tickers": len(tickers),
            "linked_tickers": linked_count,
            "unlinked_tickers": len(tickers) - linked_count,
            "coverage_pct": coverage,
            "source_totals": source_totals,
        },
        "tickers": {item["ticker"]: item for item in ordered_tickers},
        "categories": category_context,
        "recent_events": recent_events[:16],
        "unlinked": unlinked,
    }
