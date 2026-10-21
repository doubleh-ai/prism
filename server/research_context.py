"""Per-turn research clock and provenance; retrieval is not factual verification."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

RESEARCH_TIMEZONE = "Asia/Shanghai"
_TZ = ZoneInfo(RESEARCH_TIMEZONE)
_CREDENTIAL_PARAMETERS = {
    "apikey",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "authtoken",
    "authorization",
    "password",
    "passwd",
    "secret",
    "clientsecret",
    "signature",
    "sig",
    "xamzsignature",
    "xamzcredential",
    "xgoogsignature",
    "xgoogcredential",
}


def build_research_context(
    *, run_id: str | None = None, now: datetime | None = None, engine: str = "chat"
) -> dict:
    current = now or datetime.now(_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_TZ)
    current = current.astimezone(_TZ)
    return {
        "schema_version": 1,
        "run_id": run_id or uuid.uuid4().hex,
        "as_of": current.isoformat(timespec="seconds"),
        "timezone": RESEARCH_TIMEZONE,
        "date": current.date().isoformat(),
        "engine": engine,
    }


def build_research_prompt(context: dict) -> str:
    return (
        f"本轮研究时间：{context.get('as_of') or '未知'}；时区：{RESEARCH_TIMEZONE}。\n"
        "这只是本轮观察时间，不代表数据已经更新至此。区分来源发布时间、数据所属期间和检索时间；"
        "未知日期写未知，不得用今天或模型记忆补齐。追问中的‘今天/最新’以本轮时间为准。\n"
        "用户明确要求历史信息截止时，有 research_scope 工具须在首次检索或登记结论之前调用，"
        "information_cutoff 填 YYYY-MM-DD 或带时区的 ISO 秒精度时间，可另填 requested_period。"
        "截止范围固定后不能改写；改变范围需要新一轮研究，as_of 始终是真实观察时点。"
        "工具不可用或范围未固定时不得声称已执行历史截断。发布日期未知的关键事实保留未知和缺口，"
        "不能默认其在截止前可用；日精度只按日对照，不能编造午夜或事件发生时间。\n"
        "涉及最新新闻、公告、财报或价格时，必须实际检索或调用可用数据工具；"
        "工具不可用、失败或来源过期时明确说明尚未验证。仅引用本轮真实返回的来源，"
        "历史来源要标明来自此前研究并重新检查时效。网页检索结果不等于事实已验证。\n"
        "来源优先追溯交易所披露、公司公告和投资者关系（IR）原文。"
        "财联社、金十、新浪财经可用于发现线索和交叉对照；关键数字和重大事件须追溯原始发布，"
        "同一原稿的转载不算独立来源。无法取得原文时明确缺口，不因来源名称自动判定可信。\n"
        "Prism 内部工具返回的平台数据、历史报告和行业图谱是研究线索，不能冒充外部原始披露。"
        "事实需有可追溯来源与数据日期；计算写明原始值、单位和口径；"
        "推断与假设分开，列出反方证据、缺口和可验证的失效条件。\n"
        "研究账本：成功的 WebSearch/WebFetch 返回会登记到本轮。可用时调用 research_read "
        "读取真实 document_id/passage_id，再用 research_record_claims 登记逐项候选结论。"
        "不得编造引用 ID。supports/opposes/background 使用 passage_id 列表；kind 区分 "
        "fact/calculation/inference/hypothesis，数字填写 value/unit/currency/period，"
        "推断填写 limitations（反证及失效条件）。计算 formula 只支持 operator "
        "add/subtract/multiply/divide 与两个已登记 input_claim_ids。"
        "搜索摘要只是线索，WebFetch 的工具摘要也不等于已获得完整原文。"
        "引用结构校验不能证明语义真实，报告保留待核验状态。"
    )


def source_timestamp(value: Any) -> str | None:
    """Convert a recorded timestamp, never substituting replay time for missing history."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(_TZ).isoformat(timespec="seconds")
    except (ValueError, OverflowError):
        return None


def content_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\n{3,}", "\n\n", value.replace("[object Object]", "")).strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (content_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("text", "thinking", "content", "question", "summary"):
            if key in value:
                return content_text(value[key])
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _safe_url(url: Any) -> str | None:
    if not isinstance(url, str) or len(url) > 4000 or any(ord(char) < 32 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            return None
        # OAuth fragments and signed query parameters are credentials too. A
        # fragment may be a client-side route followed by its own query string.
        for parameters in (parsed.query, parsed.fragment, parsed.fragment.partition("?")[2]):
            names = parse_qs(parameters, keep_blank_values=True)
            if any(re.sub(r"[-_\s]", "", name).casefold() in _CREDENTIAL_PARAMETERS for name in names):
                return None
    except ValueError:
        return None
    return url


def _failed_tool_payload(text: str) -> bool:
    """Recognize result envelopes and plain failure notices, not quoted page examples."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        payload = None
    if isinstance(payload, dict) and (
        payload.get("ok") is False
        or payload.get("success") is False
        or payload.get("is_error") is True
        or payload.get("isError") is True
        or str(payload.get("status", "")).casefold() in {"error", "failed"}
    ):
        return True
    # The CLI's explicit API-error prefix can include a multiline response body.
    if re.match(r"(?i)^\s*API Error:\s*(?:HTTP\s*)?[45]\d{2}\b", text):
        return True
    if re.match(r"(?i)^\s*(?:HTTP\s+)?(?:401\s+Unauthorized|403\s+Forbidden|404\s+Not Found)\b", text):
        return True
    if re.search(r"(?i)<title>\s*(?:access denied|sign in|log in|403 forbidden)\s*</title>", text):
        return True
    # Other notices must be standalone transport errors, not arbitrary
    # occurrences of "error" inside fetched content or documentation examples.
    notice = text.strip()
    if "\n" in notice:
        return False
    return bool(
        re.match(
            r"(?i)^(?:error(?:[:：]|\s+(?:fetching|retrieving|searching)\b)|"
            r"failed to (?:fetch|retrieve|search|connect)\b|request failed\b|there was an error\b|"
            r"web (?:search|fetch) (?:is )?(?:unavailable|failed)|无法(?:获取|访问)|检索失败)",
            notice,
        )
    )


def extract_web_refs(text: str) -> list[dict]:
    """Parse returned links only. The caller must independently establish provenance."""
    refs: list[dict] = []
    seen: set[str] = set()

    def add(url, title, snippet="", published_at=None):
        url = _safe_url(url)
        if not url or url in seen:
            return
        seen.add(url)
        snippet = re.sub(r"^[\s\-•·–—]+", "", str(snippet)).strip()
        refs.append(
            {
                "url": url,
                "title": str(title or url),
                "snippet": snippet[:120],
                "snippet_truncated": len(snippet) > 120,
                "published_at": published_at if isinstance(published_at, str) else None,
            }
        )

    for match in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text):
        tail = text[match.end() :].split("\n\n", 1)[0]
        tail = re.split(r"\[[^\]]+\]\(https?://", tail, maxsplit=1)[0]
        add(match.group(2), match.group(1).strip(), tail[:160])

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if "url" in value:
                add(value["url"], value.get("title"), value.get("snippet", ""), value.get("published_at"))
            for key in ("links", "results", "sources"):
                if key in value:
                    visit(value[key])

    # Claude WebSearch also returns `Links: [{"title": ..., "url": ...}]`.
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.start() :])
        except (ValueError, TypeError):
            continue
        visit(value)
    return refs


def tool_source_kind(name: str) -> str | None:
    if name in {"WebSearch", "WebFetch"}:
        return "external_web"
    if name.startswith("mcp__prism__"):
        return (
            "historical_research" if any(part in name for part in ("report", "research")) else "platform_data"
        )
    return None


class EvidenceTracker:
    """Associate tool results with successful calls from exactly one user turn."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.calls: dict[str, dict] = {}
        self.completed: set[str] = set()
        self.seen: set[tuple[str, str]] = set()

    def record_call(self, call: dict) -> None:
        call_id = call.get("id") or call.get("tool_use_id")
        if call_id and isinstance(call.get("name"), str):
            self.calls[str(call_id)] = call

    def record_result(self, block: dict, *, retrieved_at: str | None = None) -> list[dict]:
        call_id = str(block.get("tool_use_id") or "")
        call = self.calls.get(call_id)
        if not call or call_id in self.completed:
            return []
        self.completed.add(call_id)
        name = call["name"]
        kind = tool_source_kind(name)
        text = content_text(block.get("content"))
        if not kind or block.get("is_error") or block.get("status") in {"error", "failed"} or not text:
            return []
        if _failed_tool_payload(text):
            return []
        if name == "WebFetch":
            inputs = call.get("input")
            url = _safe_url(inputs.get("url")) if isinstance(inputs, dict) else None
            refs = [{"url": url, "title": url, "snippet": ""}] if url else []
        else:
            refs = extract_web_refs(text)
        result = []
        for ref in refs:
            # Fetching a discovered URL is a new, richer observation. Do not
            # suppress it because the search already yielded the same link.
            key = (call_id, ref["url"])
            if key in self.seen:
                continue
            self.seen.add(key)
            result.append(
                {
                    **ref,
                    "run_id": self.run_id,
                    "tool": name,
                    "tool_call_id": call_id,
                    "source_kind": kind,
                    "evidence_status": "retrieved",
                    "retrieval_type": "fetched_page" if name == "WebFetch" else "tool_result",
                    "retrieved_at": retrieved_at,
                    "published_at": ref.get("published_at"),
                }
            )
        return result


def parse_claude_jsonl_events(events: list[dict], *, session_id: str = "") -> list[dict]:
    """Canonical legacy reader: a tool result cannot start a user turn or leak refs into another."""
    messages: list[dict] = []
    turn = 0
    chunks: list[dict] = []
    tracker = EvidenceTracker(f"legacy:{session_id}:0")

    def flush():
        if chunks:
            payload = {"text": "".join(c["c"] for c in chunks if c["t"] == "text"), "chunks": list(chunks)}
            messages.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})
            chunks.clear()

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        msg = event.get("message") or {}
        if kind == "user":
            if isinstance(msg, str) and msg.startswith(("[{'type':", "[{'tool_use_id':")):
                continue
            if not isinstance(msg, dict):
                try:
                    msg = json.loads(str(msg).replace("'", '"'))
                except (TypeError, ValueError):
                    msg = {"content": str(msg)}
            content = msg.get("content", "") if isinstance(msg, dict) else msg
            if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        for ref in tracker.record_result(
                            block, retrieved_at=source_timestamp(event.get("timestamp"))
                        ):
                            chunks.append({"t": "ref", **ref})
                continue
            user_text = content_text(content)
            if not user_text or user_text.startswith(("[{'type':", "[{'tool_use_id':")):
                continue
            flush()
            turn += 1
            tracker = EvidenceTracker(f"legacy:{session_id}:{turn}")
            messages.append({"role": "user", "content": user_text})
        elif kind == "assistant" and isinstance(msg, dict):
            for block in msg.get("content", []):
                if not isinstance(block, dict):
                    continue
                typ = block.get("type")
                if typ in {"text", "thinking"}:
                    text = content_text(block.get(typ))
                    if text:
                        chunks.append({"t": "text" if typ == "text" else "think", "c": text})
                elif typ == "tool_use":
                    tracker.record_call(block)
                    inp = block.get("input") or {}
                    chunks.append(
                        {"t": "tool", "c": block.get("name", "?"), "d": content_text(list(inp.values())[:1])}
                    )
    flush()
    return messages
