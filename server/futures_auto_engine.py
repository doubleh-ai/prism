"""Zero-touch background automation engine: judgment auto-reconciliation, event-gated pre-interpretation, and market radar."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from . import futures_margins, futures_ontology, futures_signals, model_routes
from .database import get_db
from .services.auxiliary_model import consume_text
from .work_lifecycle import LifecycleError

SHANGHAI = ZoneInfo("Asia/Shanghai")
_LOG = logging.getLogger(__name__)


def _encode(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def get_cached_interpretation(symbol: str, evidence_id: str) -> dict | None:
    """Return a pre-verified background interpretation iff ``(symbol, evidence_id)`` matches exactly."""
    if not symbol or not evidence_id:
        return None
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT result_json FROM futures_auto_interpretations WHERE symbol=? AND evidence_id=?",
                (symbol.upper(), evidence_id),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None
    except Exception:
        return None


def store_cached_interpretation(symbol: str, evidence_id: str, result: dict, now: datetime) -> None:
    """Persist a verified cited interpretation keyed by exact ``evidence_id``."""
    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO futures_auto_interpretations(symbol, evidence_id, result_json, created_at)
               VALUES (?, ?, ?, ?)""",
            (symbol.upper(), evidence_id, _encode(result), now.isoformat(timespec="seconds")),
        )


async def reconcile_pending_judgments(manager, *, now: datetime | None = None, limit: int = 24) -> int:
    """Automatically evaluate and settle due saved judgments (`review_json IS NULL`) in the background."""
    from . import futures_journal

    try:
        lease = manager.admit("market", persist_required=False)
    except LifecycleError:
        return 0

    current_time = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    settled_count = 0
    with lease:
        candidates: list[tuple[str, str, dict]] = []
        with get_db() as db:
            jm_rows = db.execute(
                """SELECT id, username, session, as_of, snapshot_json, choice_id, saved_at
                   FROM jm_research_journal
                   WHERE saved_at IS NOT NULL AND review_json IS NULL
                   ORDER BY saved_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in jm_rows:
                candidates.append(("jm_research_journal", "JM", dict(r)))
            fut_rows = db.execute(
                """SELECT id, username, symbol, session, as_of, snapshot_json, choice_id, saved_at
                   FROM futures_research_journal
                   WHERE saved_at IS NOT NULL AND review_json IS NULL
                   ORDER BY saved_at ASC LIMIT ?""",
                (limit,),
            ).fetchall()
            for r in fut_rows:
                candidates.append(("futures_research_journal", r["symbol"], dict(r)))

        for table, symbol, row in candidates[:limit]:
            try:
                snapshot = json.loads(row["snapshot_json"])
                engine = futures_journal.RULES.get(symbol, {}).get(snapshot.get("rule_version", 1))
                if engine is None:
                    continue
                is_current = row["session"] == "current"
                if is_current:
                    cutoff = current_time
                elif "review_as_of" in snapshot:
                    cutoff = datetime.fromisoformat(snapshot["review_as_of"])
                elif symbol == "JM" and row["session"] == "2026-11-19" and "rule_version" not in snapshot:
                    cutoff = datetime(2026, 11, 20, 16, 30, tzinfo=SHANGHAI)
                else:
                    continue
                choice = next((c for c in snapshot.get("choices", []) if c["id"] == row["choice_id"]), None)
                if choice is None:
                    continue
                tracked = {**choice, "tracking_started_at": row["saved_at"] if is_current else row["as_of"]}
                outcome = {
                    **(await engine.review(tracked, cutoff)),
                    "as_of": cutoff.isoformat(),
                    "auto_reconciled": True,
                }
                if outcome.get("status") in {"supported", "contradicted", "observed"}:
                    with get_db() as db:
                        updated = db.execute(
                            f"UPDATE {table} SET review_json=? WHERE id=? AND saved_at IS NOT NULL AND review_json IS NULL",
                            (_encode(outcome), row["id"]),
                        )
                        if updated.rowcount:
                            db.execute(
                                """INSERT OR REPLACE INTO futures_auto_reconcile_log(judgment_id, symbol, status, reconciled_at)
                                   VALUES (?, ?, ?, ?)""",
                                (
                                    row["id"],
                                    symbol,
                                    outcome["status"],
                                    current_time.isoformat(timespec="seconds"),
                                ),
                            )
                            settled_count += 1
            except Exception as exc:
                _LOG.warning("Auto-reconcile skipped %s/%s (%s)", symbol, row.get("id"), type(exc).__name__)
    return settled_count


def is_material_event(card: dict) -> bool:
    """Event Gate: return True only when a variety shows structural anomaly or fresh research updates."""
    brief = card.get("research_brief") or {}
    if any(not r.get("stale") for r in (brief.get("fundamentals") or {}).get("rows", [])):
        return True
    if (brief.get("institutions") or {}).get("status") == "available":
        return True
    pct = (card.get("percentiles") or {}).get("close_pct_5y")
    if isinstance(pct, (int, float)) and (pct >= 85.0 or pct <= 15.0):
        return True
    for m in card.get("margins") or []:
        mp = (m.get("values") or {}).get("pct_5y")
        if isinstance(mp, (int, float)) and (mp >= 85.0 or mp <= 15.0):
            return True
    return False


async def preinterpret_due_cards(manager, cards: list[dict], *, max_calls: int = 3) -> int:
    """Event-gated background pre-interpretation using the verified c321f1b4 attribution guard."""
    from . import futures_interpretation

    try:
        model_id, model, route = model_routes.resolve("research")
    except Exception:
        return 0

    generated = 0
    for raw_card in cards:
        if generated >= max_calls:
            break
        enriched = futures_interpretation.enrich(raw_card)
        brief = enriched.get("research_brief") or {}
        evidence_id = brief.get("evidence_id")
        if not brief.get("interpret_available") or not evidence_id:
            continue
        if get_cached_interpretation(enriched["symbol"], evidence_id) is not None:
            continue
        if not is_material_event(enriched):
            continue

        facts, _ = futures_interpretation.evidence(enriched["symbol"], brief)
        market = {k: v for k, v in facts.items() if k.startswith(("F", "T", "M"))}
        now = datetime.fromisoformat(enriched["generated_at"])
        if not market:
            # Institution-only reading needs zero LLM tokens!
            rendered = futures_interpretation.render(None, facts, enriched["generated_at"])
            store_cached_interpretation(
                enriched["symbol"], evidence_id, {**rendered, "auto_generated": True}, now
            )
            continue

        try:
            with manager.admit("auxiliary", persist_required=False):
                rid = f"futures-auto-{enriched['symbol'].lower()}-{evidence_id[:16]}"
                model_routes.reserve(
                    "research", "_system_auto", rid, model_id, expected_route_version=route["version"]
                )
                usage = {}
                try:
                    prompt = (
                        futures_interpretation.INSTRUCTIONS
                        + "\n"
                        + json.dumps(
                            {
                                "symbol": enriched["symbol"],
                                "as_of": enriched["generated_at"],
                                "facts": market,
                            },
                            ensure_ascii=False,
                        )
                    )
                    timeout = min(route.get("wall_seconds_max", 45), 45)
                    tokens = min(route.get("output_tokens_max", 1024), 1024)
                    raw = await consume_text(prompt, model, "json", timeout, max_tokens=tokens, usage=usage)
                    rendered = futures_interpretation.render(raw, facts, enriched["generated_at"])
                    store_cached_interpretation(
                        enriched["symbol"], evidence_id, {**rendered, "auto_generated": True}, now
                    )
                    generated += 1
                finally:
                    model_routes.finish(rid, **usage)
        except Exception as exc:
            _LOG.debug("Auto pre-interpret skipped %s: %s", enriched["symbol"], type(exc).__name__)
    return generated


def build_market_radar(
    histories: dict[str, list[dict] | None],
    names: dict[str, str],
    now: datetime,
    *,
    fresh_days: int = 4,
) -> dict:
    """Compute zero-network market-wide anomaly radar across all cached histories and margins."""
    today = now.date()
    high_extremes = []
    low_extremes = []
    roll_alerts = []

    for symbol, bars in histories.items():
        if not bars or len(bars) < 20:
            continue
        last_day = bars[-1].get("date")
        if not last_day or (today - datetime.strptime(last_day, "%Y-%m-%d").date()).days > fresh_days:
            continue
        pct = futures_signals.percentiles(bars)
        p5 = pct.get("close_pct_5y")
        chg_pct = (
            round((bars[-1]["close"] / bars[-2]["close"] - 1.0) * 100.0, 2)
            if len(bars) >= 2 and bars[-2].get("close")
            else None
        )
        meta = futures_ontology.get(symbol)
        item = {
            "symbol": symbol,
            "name": names.get(symbol, symbol),
            "archetype_label": meta.archetype_label if meta else "",
            "close": bars[-1]["close"],
            "change_pct": chg_pct,
            "pct_5y": p5,
            "span_label": pct.get("span_label", "历史"),
            "data_as_of": last_day,
        }
        if isinstance(p5, (int, float)):
            if p5 >= 82.0:
                high_extremes.append(item)
            elif p5 <= 18.0:
                low_extremes.append(item)
        diag = futures_signals.roll_diagnostics(bars)
        if diag.get("roll_suspect") and diag.get("roll_notice"):
            roll_alerts.append(
                {
                    "symbol": symbol,
                    "name": names.get(symbol, symbol),
                    "notice": diag["roll_notice"],
                    "data_as_of": last_day,
                }
            )

    high_extremes.sort(key=lambda r: -(r["pct_5y"] or 0))
    low_extremes.sort(key=lambda r: r["pct_5y"] if r["pct_5y"] is not None else 999)

    margin_alerts = []
    for m in futures_margins.all_margins(histories, today=today, fresh_days=fresh_days):
        if m["status"] != "collected" or not m["values"]:
            continue
        mv = m["values"]
        mp = mv.get("pct_5y") if mv.get("pct_5y") is not None else mv.get("pct_1y")
        if isinstance(mp, (int, float)) and (mp >= 78.0 or mp <= 22.0):
            margin_alerts.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "legs": m["legs"],
                    "primary_symbol": m["legs"][0],
                    "current": mv["current"],
                    "unit": m["unit"],
                    "pct": mp,
                    "span_label": mv["span_label"],
                    "hint": mv["hint"],
                    "data_as_of": m["data_as_of"],
                }
            )
    margin_alerts.sort(key=lambda r: -abs((r["pct"] or 50) - 50))

    reconciled_recent = 0
    supported_recent = 0
    try:
        with get_db() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS cnt FROM futures_auto_reconcile_log GROUP BY status"
            ).fetchall()
            for r in rows:
                reconciled_recent += int(r["cnt"])
                if r["status"] == "supported":
                    supported_recent += int(r["cnt"])
    except Exception:
        pass

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "observed_products": sum(1 for b in histories.values() if b),
        "high_extremes": high_extremes[:5],
        "low_extremes": low_extremes[:5],
        "margin_extremes": margin_alerts[:5],
        "roll_alerts": roll_alerts[:4],
        "reconciled_summary": {
            "total_auto_reconciled": reconciled_recent,
            "supported_count": supported_recent,
        },
    }
