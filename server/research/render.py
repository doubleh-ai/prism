"""Additive verification rendering for research report narratives.

The stored revision keeps the model's narrative unchanged. This module derives a
separate view: key numbers in the text layer carry their claim's verification
level, and the claim evidence list exposes source, publication date and scope.

A number match only says that a registered claim normalized to the same
measurement; it is coverage, not certification. Missing provenance stays visible
as 未提供. Every stage degrades instead of raising, so a rendering failure always
falls back to the unmodified report text.
"""

from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata

from .reconciliation import (
    _HEADING,
    _PATTERNS,
    _RULE,
    _WHITESPACE,
    _claim_key,
    _covered,
    _mask,
    _match_key,
    _report_number,
)

SCHEMA = "prism.report-render.v1"
RENDER_VERSION = 1
VERIFICATION_LEVELS = ("verified_source", "secondhand", "inferred", "unverified")
DEFAULT_VERIFICATION = "unverified"
LEVEL_MARKS = {"verified_source": "✅", "secondhand": "◐", "inferred": "≈"}
LEVEL_LABELS = {
    "verified_source": "原文已核验",
    "secondhand": "二手来源",
    "inferred": "推算",
    "unverified": "待核验",
}
TRUST = {"verified_source": 3, "secondhand": 2, "inferred": 1, "unverified": 0}
MISSING = "未提供"
MAX_SOURCE_CHARACTERS = 120
MAX_SCOPE_CHARACTERS = 160
MAX_STATEMENT_CHARACTERS = 200
MAX_SENTENCE_NOTES = 3
VERIFICATION_RELEASE_LINE = 0.9
RELEASE_LINE_LABEL = "≥90%"
_MISSING_FIELDS = ("source", "published_at", "scope")
_PENDING = "【待核验：{display}】"
_SENTENCE_END = re.compile(r"[。；;！!？?]")
_URL_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_ISO_DATE = re.compile(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
_CHINESE_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)")


def render_report(claims, text, *, documents=None, passages=None) -> dict:
    """Build the additive verification view; no stage raises into the report path."""
    claim_list = [claim for claim in claims if isinstance(claim, dict)] if isinstance(claims, list) else []
    source_text = text if isinstance(text, str) else ""
    try:
        contexts = _claim_contexts(claim_list, documents, passages)
    except Exception:  # noqa: BLE001 - a derived view must never cost the report
        return unavailable_view(source_text)
    status = "ok"
    try:
        evidence = _evidence_payload(contexts)
    except Exception:  # noqa: BLE001 - keep the rendered narrative even without the list
        evidence = _unavailable_evidence()
        status = "partial"
    try:
        rendered = _render_narrative(contexts, source_text)
        narrative = {
            "status": "rendered",
            "changed": rendered != source_text,
            "text": rendered,
            "sha256": _digest(rendered),
        }
    except Exception:  # noqa: BLE001 - fall back to the original report text
        status = "degraded"
        narrative = _original_narrative(source_text)
    return {
        "schema": SCHEMA,
        "status": status,
        "scope": "additive_verification_view",
        "semantic_verification": False,
        "render_version": RENDER_VERSION,
        "narrative": narrative,
        "evidence": evidence,
    }


def unavailable_view(text) -> dict:
    """The conservative fallback used when the renderer itself cannot run."""
    source_text = text if isinstance(text, str) else ""
    return {
        "schema": SCHEMA,
        "status": "degraded",
        "scope": "additive_verification_view",
        "semantic_verification": False,
        "render_version": RENDER_VERSION,
        "narrative": _original_narrative(source_text),
        "evidence": _unavailable_evidence(),
    }


def _claim_contexts(claims, documents, passages) -> list[dict]:
    document_map = {
        item["document_id"]: item for item in _dicts(documents) if isinstance(item.get("document_id"), str)
    }
    passage_map = {
        item["passage_id"]: item for item in _dicts(passages) if isinstance(item.get("passage_id"), str)
    }
    contexts = []
    for claim in claims:
        level = claim.get("verification")
        if level not in VERIFICATION_LEVELS:
            level = DEFAULT_VERIFICATION
        try:
            key = _claim_key(claim)
        except Exception:  # noqa: BLE001 - malformed claim values stay claim-level gaps
            key = None
        try:
            provenance = _provenance(claim, document_map, passage_map)
        except Exception:  # noqa: BLE001
            provenance = _empty_provenance()
        contexts.append({"claim": claim, "verification": level, "key": key, "provenance": provenance})
    return contexts


def _provenance(claim, documents, passages) -> dict:
    """Read source/date/scope from claim fields, then from cited source documents."""
    source = _clean_text(claim.get("source"), 500)
    published_at = _publication_text(claim.get("published_at"))
    scope = _clean_text(claim.get("scope"), 300)
    period = _clean_text(claim.get("period"), 120)
    basis = _clean_text(claim.get("accounting_basis"), 300)
    source_url = _source_url(source)
    for passage_id in _claim_supports(claim):
        passage = passages.get(passage_id)
        document = documents.get(passage.get("document_id")) if isinstance(passage, dict) else None
        if not isinstance(document, dict):
            continue
        url = document.get("url") if isinstance(document.get("url"), str) else None
        if source is None:
            source = _clean_text(document.get("title"), 200) or _clean_text(url, 500)
        if source_url is None:
            source_url = _source_url(url)
        if published_at is None:
            published_at = _publication_text(document.get("published_at"))
    return {
        "source": source,
        "source_url": source_url,
        "published_at": published_at,
        "scope": scope or _scope_text(period, basis),
    }


def _empty_provenance() -> dict:
    return {"source": None, "source_url": None, "published_at": None, "scope": None}


def _claim_supports(claim) -> list[str]:
    supports = claim.get("supports")
    return [item for item in supports if isinstance(item, str)] if isinstance(supports, list) else []


def _scope_text(period, basis) -> str | None:
    parts = []
    if period:
        parts.append(period)
    if basis:
        parts.append("口径：" + basis)
    if not parts:
        return None
    return _clip(" · ".join(parts), MAX_SCOPE_CHARACTERS)


def _publication_text(value) -> str | None:
    """Normalize a trustworthy publication date; unknown or informal text is missing."""
    cleaned = _clean_text(value, 60)
    if not cleaned:
        return None
    normalized = unicodedata.normalize("NFKC", cleaned)
    chinese = _CHINESE_DATE.match(normalized)
    if chinese:
        year, month, day = chinese.groups()
        return _iso_date(year, month, day)
    normalized = normalized.replace("/", "-").replace(".", "-")
    match = _ISO_DATE.match(normalized)
    if not match:
        return None
    year, month, day = match.groups()
    return _iso_date(year, month, day)


def _iso_date(year: str, month: str | None, day: str | None) -> str | None:
    if month is None:
        return year
    month_number = int(month)
    if not 1 <= month_number <= 12:
        return None
    if day is None:
        return f"{year}-{month_number:02d}"
    day_number = int(day)
    if not 1 <= day_number <= 31:
        return None
    return f"{year}-{month_number:02d}-{day_number:02d}"


def _source_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not _URL_SCHEME.match(cleaned):
        return None
    return _clip(cleaned, 500)


def _dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _evidence_payload(contexts) -> dict:
    rows = []
    counts = dict.fromkeys(VERIFICATION_LEVELS, 0)
    for context in contexts:
        claim = context["claim"]
        provenance = context["provenance"]
        level = context["verification"]
        counts[level] += 1
        value = _plain_value(claim.get("value"))
        rows.append(
            {
                "claim_id": claim.get("claim_id") if isinstance(claim.get("claim_id"), str) else None,
                "statement": _clean_text(claim.get("statement"), MAX_STATEMENT_CHARACTERS),
                "metric": _clean_text(claim.get("metric"), 80),
                "value": value,
                "unit": _clean_text(claim.get("unit"), 80),
                "currency": _clean_text(claim.get("currency"), 30),
                "source": provenance["source"],
                "source_url": provenance["source_url"],
                "published_at": provenance["published_at"],
                "scope": provenance["scope"],
                "verification": level,
                "verification_label": LEVEL_LABELS[level],
                "missing": [field for field in _MISSING_FIELDS if not provenance[field]],
            }
        )
    total = len(rows)
    verified = counts["verified_source"]
    ratio = round(verified / total, 4) if total else None
    return {
        "status": "ok",
        "rows": rows,
        "total": total,
        "counts": dict(counts),
        "coverage": {
            "verified_claim_ratio": ratio,
            "threshold": VERIFICATION_RELEASE_LINE,
            "release_line": RELEASE_LINE_LABEL,
            "reached": None if ratio is None else ratio >= VERIFICATION_RELEASE_LINE,
        },
        "missing": {field: sum(1 for row in rows if field in row["missing"]) for field in _MISSING_FIELDS},
        "note": "证据清单只记录 claim 字段与支持资料，不构成独立事实认证。",
    }


def _plain_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _unavailable_evidence() -> dict:
    return {
        "status": "unavailable",
        "rows": [],
        "total": 0,
        "counts": dict.fromkeys(VERIFICATION_LEVELS, 0),
        "coverage": {
            "verified_claim_ratio": None,
            "threshold": VERIFICATION_RELEASE_LINE,
            "release_line": RELEASE_LINE_LABEL,
            "reached": None,
        },
        "missing": dict.fromkeys(_MISSING_FIELDS, 0),
        "note": "证据清单暂不可用；报告正文保持原样显示。",
    }


def _render_narrative(contexts, text: str) -> str:
    if not text:
        return text
    parts = re.split(r"(\r\n|\r|\n)", text)
    rendered = []
    for index, part in enumerate(parts):
        rendered.append(_render_line(part, contexts) if index % 2 == 0 else part)
    return "".join(rendered)


def _render_line(line: str, contexts) -> str:
    stripped = line.strip()
    if not stripped or _HEADING.match(stripped) or _RULE.fullmatch(stripped):
        return line
    # Table pipes are separators, not unit boundaries; NFKC keeps extra digits visible.
    masked = line.replace("|", " ")
    normalized = unicodedata.normalize("NFKC", masked)
    work = _mask(normalized if len(normalized) == len(line) else masked)
    matches = _line_matches(work)
    if not matches:
        return line
    ends = _sentence_ends(line)
    edits: list = []
    notes: dict = {}
    table_line = "|" in line
    for item in matches:
        claim = _matching_claim(item["key"], contexts)
        level = claim["verification"] if claim else DEFAULT_VERIFICATION
        start, end = item["span"]
        if level == DEFAULT_VERIFICATION:
            if table_line and "|" in line[start:end]:
                span, display = item["number_span"], item["number_display"]
            else:
                span, display = item["span"], item["display"]
            edits.append((span[0], span[1], 0, len(edits), _PENDING.format(display=display)))
            continue
        marker_position = item["number_span"][1] if table_line else end
        edits.append((marker_position, marker_position, 0, len(edits), LEVEL_MARKS[level]))
        if table_line:
            # A source annotation inside a Markdown table row would create a stray cell.
            continue
        sentence = _sentence_index(ends, end)
        bucket = notes.setdefault(sentence, {"verified": [], "secondhand": False, "inferred": False})
        if level == "verified_source":
            note = _verified_note(claim)
            if note not in bucket["verified"] and len(bucket["verified"]) < MAX_SENTENCE_NOTES:
                bucket["verified"].append(note)
        elif level == "secondhand":
            bucket["secondhand"] = True
        elif level == "inferred":
            bucket["inferred"] = True
    for sentence, bucket in notes.items():
        parts = list(bucket["verified"])
        if bucket["secondhand"]:
            parts.append("（二手来源）")
        if bucket["inferred"]:
            parts.append("（推算）")
        if parts and sentence < len(ends):
            edits.append((ends[sentence], ends[sentence], 1, len(edits), "".join(parts)))
    if not edits:
        return line
    edits.sort(key=lambda edit: (edit[0], edit[1], edit[2]))
    result = line
    for start, end, _priority, _sequence, insertion in reversed(edits):
        result = result[:start] + insertion + result[end:]
    return result


def _line_matches(work: str) -> list[dict]:
    """Locate key numbers once each, reusing the reconciliation number grammar."""
    matches = []
    taken: list[tuple[int, int]] = []
    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(work):
            start, end = match.span()
            if any(start < taken_end and taken_start < end for taken_start, taken_end in taken):
                continue
            if work[:start].rstrip().endswith("第"):
                # Ordinals (第 3 名 / 第 5 家) are sequence numbers, not key numbers.
                continue
            number, signless = _report_number(match, work)
            if number is None:
                continue
            taken.append((start, end))
            key = _match_key(kind, match, number)
            if key is None:
                continue
            number_start, number_end = match.span(1)
            matches.append(
                {
                    "kind": kind,
                    "span": (start, end),
                    "number_span": (number_start, number_end),
                    "display": _display(work[start:end], signless),
                    "number_display": _display(work[number_start:number_end], signless),
                    "key": key,
                }
            )
    return matches


def _display(raw: str, signless: bool) -> str:
    text = raw.lstrip("+-") if signless else raw
    collapsed = _WHITESPACE.sub(" ", text).strip()
    return re.sub(r"\s+%", "%", collapsed)


def _matching_claim(key, contexts) -> dict | None:
    if key is None:
        return None
    matches = [
        context for context in contexts if context["key"] is not None and _covered(key, {context["key"]})
    ]
    if not matches:
        return None
    # Multiple claims can cover one number; the weakest level stays visible.
    return min(matches, key=lambda context: TRUST.get(context["verification"], 0))


def _verified_note(context) -> str:
    provenance = context["provenance"]
    source = _clip(provenance["source"], MAX_SOURCE_CHARACTERS) if provenance["source"] else MISSING
    published = provenance["published_at"] or "发布日期" + MISSING
    return f"（来源：{source}，{published}）"


def _sentence_ends(line: str) -> list[int]:
    ends = {match.end() for match in _SENTENCE_END.finditer(line)}
    ends.add(len(line))
    return sorted(ends)


def _sentence_index(ends: list[int], position: int) -> int:
    return min(bisect.bisect_left(ends, position), len(ends) - 1)


def _original_narrative(text: str) -> dict:
    return {"status": "original", "changed": False, "text": text, "sha256": _digest(text)}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean_text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    collapsed = _WHITESPACE.sub(" ", value).strip()
    if not collapsed:
        return None
    return _clip(collapsed, limit)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
