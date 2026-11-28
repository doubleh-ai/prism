"""Circle Intelligence Pool & Private Personal Agent Engine.

Provides four strictly separated capabilities for small-circle high-trust research:
1. Circle Intelligence Pool (`/api/intel/*`):
   - Zero-friction paste of group/street notes with deterministic structuring into
     conditional If-Then branches, verification checkpoints, TTL auto-expiry, and
     instant Market Reality Cross-Check (CSI 300 / ticker price & 5d heat check).
   - Strict public-vs-private isolation (`visibility='circle'` vs `'private'`),
     optional anonymous sharing (`anonymous=1`), peer endorsements, contributor
     verified hit-rate tracking, and post-expiry market reconciliation.
2. Layer-1 Private Position State (`/api/private-agent/state`):
   - Owner-only 10-second position & core holdings snapshot (strictly invisible to
     other circle members and excluded from admin tables).
3. Proactive Personal Watchman (`/api/private-agent/watchman/*`):
   - Natural-language one-sentence rule compiler + 0-token background polling over
     equities, indices, futures, and Radar/Circle keywords.
4. Per-User Private Notification Channel (`/api/private-agent/push/*`):
   - Lets each user bind their own Bark / WeChat / ntfy / IM webhook to receive
     personal Watchman alerts independently of global Ops broadcasts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import database, event_radar, public_network
from .auth import is_admin_user, require_account_context, require_auth, verify_csrf
from .database import check_rate_limit, get_db
from .work_lifecycle import LifecycleError, Manager

log = logging.getLogger("stock-app")
SHANGHAI = ZoneInfo("Asia/Shanghai")

intel_router = APIRouter(prefix="/api/intel", tags=["intel"])
private_router = APIRouter(prefix="/api/private-agent", tags=["private-agent"])

_INITIALIZED_DBS: set[str] = set()

IntelCategory = Literal["strategy", "rumor", "macro", "channel"]
IntelVisibility = Literal["circle", "private"]
WatchRuleKind = Literal[
    "price_above",
    "price_below",
    "change_pct_above",
    "change_pct_below",
    "keyword_hit",
]

CATEGORY_LABELS: dict[str, str] = {
    "strategy": "策略与仓位剧本",
    "rumor": "产业与订单线索",
    "macro": "宏观与政策预期",
    "channel": "调研与路演纪要",
}

DEFAULT_TTL_DAYS: dict[str, int] = {
    "strategy": 5,
    "macro": 5,
    "rumor": 7,
    "channel": 30,
}

_SECTOR_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AI算力与光模块", re.compile(r"光模块|算力|液冷|AI服务器|英伟达|NVDA|CPO|交换机|数据中心", re.I)),
    ("半导体与国产替代", re.compile(r"半导体|芯片|光刻|存储|HBM|晶圆|中芯|设备材料", re.I)),
    ("新能源与锂电", re.compile(r"锂电|碳酸锂|光伏|储能|固态电池|多晶硅|风电|宁王", re.I)),
    ("高股息与红利", re.compile(r"红利|高股息|银行|煤炭|电力|水电|央国企", re.I)),
    ("消费与医药", re.compile(r"白酒|消费|医药|创新药|食品|免税|猪周期", re.I)),
    ("地产与内需政策", re.compile(r"地产|楼市|化债|专项债|财政|消费补贴|降准|降息|平准", re.I)),
    ("有色与大宗商品", re.compile(r"黄金|铜|原油|铝|螺纹钢|铁矿|化工|焦煤|豆粕", re.I)),
    ("机器人与智驾", re.compile(r"机器人|人形|智驾|自动驾驶|低空经济|无人机", re.I)),
)

_FUTURES_NAME_MAP: dict[str, tuple[str, str]] = {
    "沪金": ("AU", "黄金"),
    "黄金": ("AU", "黄金"),
    "沪银": ("AG", "白银"),
    "白银": ("AG", "白银"),
    "沪铜": ("CU", "沪铜"),
    "螺纹钢": ("RB", "螺纹钢"),
    "螺纹": ("RB", "螺纹钢"),
    "铁矿石": ("I", "铁矿石"),
    "铁矿": ("I", "铁矿石"),
    "焦煤": ("JM", "焦煤"),
    "豆粕": ("M", "豆粕"),
    "原油": ("SC", "原油"),
    "碳酸锂": ("LC", "碳酸锂"),
    "多晶硅": ("PS", "多晶硅"),
    "纯碱": ("SA", "纯碱"),
    "玻璃": ("FG", "玻璃"),
    "欧线": ("EC", "集运欧线"),
}

_INDEX_ALIASES: dict[str, tuple[str, str]] = {
    "沪深300": ("sh000300", "沪深300"),
    "上证指数": ("sh000001", "上证指数"),
    "上证": ("sh000001", "上证指数"),
    "大盘": ("sh000001", "上证指数"),
    "创业板指": ("sz399006", "创业板指"),
    "科创50": ("sh000688", "科创50"),
    "恒生科指": ("hkHSTECH", "恒生科指"),
    "恒生科技": ("hkHSTECH", "恒生科指"),
    "纳斯达克": ("usIXIC", "纳斯达克"),
}


def init_private_agent_db(db=None) -> None:
    db_key = str(database.DB_PATH)
    if db is None and db_key in _INITIALIZED_DBS:
        return
    sql = """
    CREATE TABLE IF NOT EXISTS circle_intel_items (
        id TEXT PRIMARY KEY,
        author TEXT NOT NULL,
        visibility TEXT NOT NULL DEFAULT 'circle',
        anonymous INTEGER NOT NULL DEFAULT 0,
        category TEXT NOT NULL DEFAULT 'rumor',
        raw_text TEXT NOT NULL,
        headline TEXT NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'market',
        tickers_json TEXT NOT NULL DEFAULT '[]',
        sectors_json TEXT NOT NULL DEFAULT '[]',
        direction TEXT NOT NULL DEFAULT 'neutral',
        branches_json TEXT NOT NULL DEFAULT '[]',
        verify_signals_json TEXT NOT NULL DEFAULT '[]',
        market_check_json TEXT NOT NULL DEFAULT '{}',
        ttl_days INTEGER NOT NULL DEFAULT 5,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        upvotes_json TEXT NOT NULL DEFAULT '[]',
        reconcile_status TEXT NOT NULL DEFAULT 'pending',
        reconcile_summary TEXT NOT NULL DEFAULT '',
        reconciled_at INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_circle_intel_vis_exp
        ON circle_intel_items(visibility, expires_at DESC, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_circle_intel_author
        ON circle_intel_items(author, created_at DESC);

    CREATE TABLE IF NOT EXISTS user_position_state (
        username TEXT PRIMARY KEY,
        position_pct INTEGER NOT NULL DEFAULT 0,
        max_single_loss_pct REAL NOT NULL DEFAULT 8.0,
        cash_note TEXT NOT NULL DEFAULT '',
        holdings_json TEXT NOT NULL DEFAULT '[]',
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS user_watchman_rules (
        id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        rule_kind TEXT NOT NULL,
        target_code TEXT NOT NULL,
        target_name TEXT NOT NULL,
        threshold REAL NOT NULL DEFAULT 0.0,
        keyword TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        last_checked_at INTEGER,
        last_observed TEXT NOT NULL DEFAULT '',
        triggered_at INTEGER,
        trigger_summary TEXT NOT NULL DEFAULT '',
        push_delivered INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_watchman_owner
        ON user_watchman_rules(username, enabled DESC, created_at DESC);

    CREATE TRIGGER IF NOT EXISTS private_agent_user_cleanup AFTER DELETE ON users BEGIN
        DELETE FROM circle_intel_items WHERE author=OLD.username;
        DELETE FROM user_position_state WHERE username=OLD.username;
        DELETE FROM user_watchman_rules WHERE username=OLD.username;
    END;
    """
    if db is not None:
        db.executescript(sql)
        return
    with get_db() as conn:
        conn.executescript(sql)
    _INITIALIZED_DBS.add(db_key)


def _owner(request: Request, response: Response) -> str:
    response.headers["Cache-Control"] = "private, no-store"
    owner = require_auth(request)
    require_account_context(request, owner)
    return owner


Owner = Annotated[str, Depends(_owner)]


def _jload(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        val = json.loads(raw)
        return val if isinstance(val, type(fallback)) else fallback
    except Exception:
        return fallback


def _jdump(val: Any) -> str:
    return json.dumps(val, ensure_ascii=False, separators=(",", ":"))


# ── Live Quote Helper (0-Token Fast Quote for Market Reality Check & Watchman) ──


def _normalize_quote_code(code: str) -> str:
    c = str(code or "").strip()
    if not c:
        return ""
    low = c.lower()
    if low.startswith(("sh", "sz", "bj", "hk", "us", "wh")):
        return c
    if re.fullmatch(r"\d{6}", c):
        if c.startswith(("6", "5")):
            return "sh" + c
        if c.startswith(("4", "8", "92")):
            return "bj" + c
        return "sz" + c
    up = c.upper()
    if re.fullmatch(r"[A-Z]{1,3}0?", up):
        return up if up.endswith("0") else (up + "0")
    return c


def fetch_fast_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch instant quotes for equities/indices (Tencent) and futures (Sina) with 1.8s timeout."""
    out: dict[str, dict[str, Any]] = {}
    eq_codes: list[str] = []
    fut_codes: list[str] = []
    alias_back: dict[str, list[str]] = {}
    for raw in codes:
        norm = _normalize_quote_code(raw)
        if not norm:
            continue
        alias_back.setdefault(norm, []).append(raw)
        if re.fullmatch(r"[A-Z]{1,3}0", norm):
            if norm not in fut_codes:
                fut_codes.append(norm)
        else:
            if norm not in eq_codes:
                eq_codes.append(norm)

    if eq_codes:
        try:
            url = "https://qt.gtimg.cn/q=" + ",".join(eq_codes[:25])
            with public_network.sync_client(timeout=1.8) as client:
                resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code == 200:
                    txt = resp.content[:65536].decode("gbk", errors="replace")
                    for line in txt.splitlines():
                        if "=" not in line or "~" not in line:
                            continue
                        lhs, rhs = line.split("=", 1)
                        k = lhs.strip().removeprefix("v_")
                        parts = rhs.strip().strip('";').split("~")
                        if len(parts) < 5:
                            continue
                        try:
                            name = parts[1].strip()
                            price = float(parts[3])
                            prev = float(parts[4])
                            if price <= 0:
                                continue
                            chg_pct = round((price - prev) / prev * 100.0, 2) if prev > 0 else 0.0
                            qobj = {"code": k, "name": name, "price": price, "change_pct": chg_pct}
                            out[k] = qobj
                            for orig in alias_back.get(k, []):
                                out[orig] = qobj
                        except ValueError:
                            continue
        except Exception:
            pass

    if fut_codes:
        try:
            url = "https://hq.sinajs.cn/list=" + ",".join(fut_codes[:20])
            with public_network.sync_client(timeout=1.8) as client:
                resp = client.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
                )
                if resp.status_code == 200:
                    txt = resp.content[:65536].decode("gbk", errors="replace")
                    for line in txt.splitlines():
                        if "hq_str_" not in line or "=" not in line:
                            continue
                        lhs, rhs = line.split("=", 1)
                        k = lhs.split("hq_str_")[-1].strip()
                        parts = rhs.strip().strip('";').split(",")
                        if len(parts) < 9:
                            continue
                        try:
                            name = parts[0].strip()
                            prev = float(parts[5] or parts[7] or 0)
                            price = float(parts[8] or parts[6] or 0)
                            if price <= 0:
                                continue
                            chg_pct = round((price - prev) / prev * 100.0, 2) if prev > 0 else 0.0
                            qobj = {"code": k, "name": name, "price": price, "change_pct": chg_pct}
                            out[k] = qobj
                            for orig in alias_back.get(k, []):
                                out[orig] = qobj
                        except ValueError:
                            continue
        except Exception:
            pass
    return out


# ── 1. Circle Intelligence Structuring & Reality Cross-Check ──


def parse_intel_text(raw_text: str) -> dict[str, Any]:
    """Deterministically structure raw WeChat/circle chatter into actionable fields (<2ms)."""
    cleaned = re.sub(r"\r\n?", "\n", str(raw_text or "")).strip()
    compact = re.sub(r"\s+", " ", cleaned)

    # 1. Category detection
    has_pos_words = bool(
        re.search(r"底仓|空仓|重仓|轻仓|半仓|满仓|分批加|减仓|追涨|止盈|止损|回调机会", cleaned)
    )
    has_channel_words = bool(re.search(r"调研|纪要|路演|专家访谈|渠道反馈|排产|加单|稼动率|出货量", cleaned))
    has_macro_words = bool(
        re.search(r"央行|降准|降息|政治局|国常会|美联储|财政|政策预期|细则落地|关税", cleaned)
    )
    if has_pos_words:
        category: IntelCategory = "strategy"
    elif has_channel_words:
        category = "channel"
    elif has_macro_words:
        category = "macro"
    else:
        category = "rumor"

    # 2. Extract tickers (6-digit A-share / ETF codes + known futures names)
    tickers: list[str] = []
    for m in re.finditer(r"(?<!\d)([036]\d{5}|5[168]\d{4}|159\d{3}|8\d{5}|688\d{3})(?!\d)", cleaned):
        code = m.group(1)
        if code not in tickers:
            tickers.append(code)
    # Match stock names from cached stock list if available (bounded, instant memory check)
    try:
        from .market import akshare_provider as akp

        df = getattr(akp, "_stock_list_cache_data", None)
        if df is not None and not getattr(df, "empty", True) and len(tickers) < 5:
            for row in df.itertuples(index=False):
                nm = str(getattr(row, "name", "") or "").strip()
                cd = str(getattr(row, "code", "") or "").strip()
                if len(nm) >= 3 and nm in cleaned and cd not in tickers:
                    tickers.append(cd)
                    if len(tickers) >= 5:
                        break
    except Exception:
        pass

    for kw, (fcode, _fname) in _FUTURES_NAME_MAP.items():
        if kw in cleaned and fcode not in tickers and len(tickers) < 6:
            tickers.append(fcode)

    # 3. Extract sectors
    sectors: list[str] = []
    for s_name, pat in _SECTOR_KEYWORDS:
        if pat.search(cleaned) and s_name not in sectors:
            sectors.append(s_name)

    scope_type = (
        "equity"
        if any(re.fullmatch(r"\d{6}", t) for t in tickers)
        else ("futures" if tickers else ("sector" if sectors else "market"))
    )

    # 4. Direction / stance
    if re.search(r"不适合.*追涨|冲高分化|适当降低仓位|减仓|谨防|见顶|利好兑现|看空|回避", cleaned):
        direction = "cautious" if re.search(r"底仓|分批加|回调机会", cleaned) else "bearish"
    elif re.search(r"加单|超预期|大涨|满产|提价|涨价|看多|突破|主线", cleaned):
        direction = "bullish"
    else:
        direction = "neutral"

    # 5. Extract If-Then conditional playbook branches (like the user's exact noon example!)
    branches: list[dict[str, str]] = []
    clauses = [c.strip(" ，。；;！!？?•-") for c in re.split(r"[\n。；;！!]+", cleaned) if c.strip()]
    branch_patterns = [
        (r"已有底仓|有底仓|持仓者", "已有底仓 / 高风偏"),
        (r"空仓|未入场|场外", "空仓 / 轻仓观望者"),
        (r"仓位已经很重|重仓|满仓", "重仓账户"),
        (r"如果|若|倘若|一旦", "条件触发应对"),
    ]
    for cl in clauses:
        if len(cl) < 6:
            continue
        for pat, role_lbl in branch_patterns:
            if re.search(pat, cl):
                branches.append({"audience": role_lbl, "action": cl[:140]})
                break
        if len(branches) >= 4:
            break

    # 6. Extract verification signals (how to verify whether this chatter comes true)
    verify_signals: list[str] = []
    for cl in clauses:
        if re.search(r"看量能|量能|成交额|细则|落地|公告|验证|回调|突破|跌破|库存|排产|订单", cl):
            short = cl[:72]
            if short not in verify_signals:
                verify_signals.append(short)
        if len(verify_signals) >= 3:
            break
    if not verify_signals:
        verify_signals.append("观察未来 3 个交易日盘面量价配合及是否有公开公告/数据印证")

    # 7. Concise 1-line headline
    first_clause = clauses[0] if clauses else compact
    headline = re.sub(r"^[【\[].*?[】\]]\s*", "", first_clause).strip()
    if len(headline) > 54:
        headline = headline[:52] + "…"
    if not headline:
        headline = compact[:50]

    return {
        "category": category,
        "headline": headline,
        "scope_type": scope_type,
        "tickers": tickers[:6],
        "sectors": sectors[:4],
        "direction": direction,
        "branches": branches,
        "verify_signals": verify_signals,
        "default_ttl_days": DEFAULT_TTL_DAYS[category],
    }


def build_market_reality_check(parsed: dict[str, Any], *, fetch_live: bool = True) -> dict[str, Any]:
    """Cross-check submitted street chatter against real-time index/ticker prices."""
    now_iso = datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M")
    tickers = parsed.get("tickers") or []
    primary_eq = next((t for t in tickers if re.fullmatch(r"\d{6}", t)), None)
    primary_fut = next((t for t in tickers if re.fullmatch(r"[A-Z]{1,3}", t)), None)

    if not fetch_live:
        return {
            "checked_at": now_iso,
            "verdict": "neutral",
            "badge": "◎ 已记录基准时点",
            "summary": "已登记情报时点，后续可随时按最新行情对账。",
            "benchmark_code": primary_eq or primary_fut or "sh000300",
            "benchmark_price": None,
        }

    targets = ["sh000300"]
    if primary_eq:
        targets.insert(0, primary_eq)
    elif primary_fut:
        targets.insert(0, primary_fut)

    quotes = fetch_fast_quotes(targets)
    focus_key = primary_eq or primary_fut or "sh000300"
    q = quotes.get(focus_key) or quotes.get("sh000300")
    if not q:
        return {
            "checked_at": now_iso,
            "verdict": "neutral",
            "badge": "◎ 待盘中行情核验",
            "summary": "暂未获取到实时盘口快照，保留原始线索待核验。",
            "benchmark_code": focus_key,
            "benchmark_price": None,
        }

    chg = float(q.get("change_pct") or 0.0)
    price = float(q.get("price") or 0.0)
    name = str(q.get("name") or focus_key)
    direction = parsed.get("direction")

    if direction == "bullish" and chg >= 4.5:
        verdict = "warn_heated"
        badge = f"▲ 盘口已大涨 ({chg:+.2f}%) · 防利好兑现"
        summary = f"提交时 {name} 报价 {price}（当日 {chg:+.2f}%），短线涨幅已较显著，切忌盲目追高接盘。"
    elif abs(chg) >= 1.8 or direction == "cautious":
        verdict = "volatile"
        badge = f"◎ 盘面波动放大 ({name} {chg:+.2f}%)"
        summary = f"提交时 {name} 报价 {price}（当日 {chg:+.2f}%），盘面处于情绪波动窗口，宜按预设仓位纪律分批应对。"
    else:
        verdict = "calm"
        badge = f"✓ 盘口未明显透支 ({name} {chg:+.2f}%)"
        summary = f"提交时 {name} 报价 {price}（当日 {chg:+.2f}%），盘口尚未出现极端抢跑，可持续跟踪后续量能与催化剂验证。"

    return {
        "checked_at": now_iso,
        "verdict": verdict,
        "badge": badge,
        "summary": summary,
        "benchmark_code": focus_key,
        "benchmark_name": name,
        "benchmark_price": price,
        "benchmark_change_pct": chg,
    }


def _author_credibility_map(db) -> dict[str, dict[str, Any]]:
    rows = db.execute(
        """
        SELECT author,
               COUNT(*) AS total,
               SUM(CASE WHEN reconcile_status='supported' THEN 1 ELSE 0 END) AS sup,
               SUM(CASE WHEN reconcile_status='contradicted' THEN 1 ELSE 0 END) AS con
        FROM circle_intel_items
        WHERE visibility='circle' AND anonymous=0
        GROUP BY author
        """
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        sup = int(r["sup"] or 0)
        con = int(r["con"] or 0)
        decided = sup + con
        hit_rate = round(sup * 100.0 / decided, 0) if decided > 0 else None
        out[str(r["author"])] = {
            "shared_count": int(r["total"] or 0),
            "verified_count": decided,
            "hit_rate_pct": hit_rate,
        }
    return out


def _serialize_intel_row(
    row, viewer: str, cred_map: dict[str, dict[str, Any]], now_ts: int
) -> dict[str, Any]:
    is_mine = str(row["author"]) == viewer
    anon = bool(row["anonymous"])
    display_author = (
        (f"{row['author']}（我·匿名）" if is_mine else "匿名圈友") if anon else str(row["author"])
    )
    upvotes: list[str] = _jload(row["upvotes_json"], [])
    cred = (
        None
        if anon or row["visibility"] != "circle"
        else (
            cred_map.get(str(row["author"])) or {"shared_count": 1, "verified_count": 0, "hit_rate_pct": None}
        )
    )
    rem_sec = int(row["expires_at"]) - now_ts
    is_expired = rem_sec <= 0
    rem_days = max(0, round(rem_sec / 86400.0, 1)) if not is_expired else 0
    return {
        "id": row["id"],
        "visibility": row["visibility"],
        "anonymous": anon,
        "display_author": display_author,
        "is_mine": is_mine,
        "can_delete": is_mine or (row["visibility"] == "circle" and is_admin_user(viewer)),
        "category": row["category"],
        "category_label": CATEGORY_LABELS.get(row["category"], "圈内线索"),
        "raw_text": row["raw_text"],
        "headline": row["headline"],
        "scope_type": row["scope_type"],
        "tickers": _jload(row["tickers_json"], []),
        "sectors": _jload(row["sectors_json"], []),
        "direction": row["direction"],
        "branches": _jload(row["branches_json"], []),
        "verify_signals": _jload(row["verify_signals_json"], []),
        "market_check": _jload(row["market_check_json"], {}),
        "ttl_days": int(row["ttl_days"]),
        "created_at": int(row["created_at"]),
        "expires_at": int(row["expires_at"]),
        "is_expired": is_expired,
        "remaining_days": rem_days,
        "endorse_count": len(upvotes),
        "endorsed_by_me": viewer in upvotes,
        "author_credibility": cred,
        "reconcile_status": row["reconcile_status"],
        "reconcile_summary": row["reconcile_summary"] or "",
        "reconciled_at": row["reconciled_at"],
    }


def submit_circle_intel(
    author: str,
    *,
    raw_text: str,
    visibility: IntelVisibility = "circle",
    anonymous: bool = False,
    ttl_days: int | None = None,
    fetch_live: bool = True,
) -> dict[str, Any]:
    init_private_agent_db()
    text = str(raw_text or "").strip()
    if len(text) < 4:
        raise ValueError("内容太短，请至少粘贴一句完整信息")
    if len(text) > 4000:
        raise ValueError("单条情报请控制在 4000 字以内")

    parsed = parse_intel_text(text)
    eff_ttl = max(1, min(90, int(ttl_days or parsed["default_ttl_days"])))
    mcheck = build_market_reality_check(parsed, fetch_live=fetch_live)
    now_ts = int(time.time())
    exp_ts = now_ts + eff_ttl * 86400
    iid = secrets.token_hex(12)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO circle_intel_items(
                id, author, visibility, anonymous, category, raw_text, headline,
                scope_type, tickers_json, sectors_json, direction, branches_json,
                verify_signals_json, market_check_json, ttl_days, created_at, expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                iid,
                author,
                visibility,
                1 if anonymous else 0,
                parsed["category"],
                text,
                parsed["headline"],
                parsed["scope_type"],
                _jdump(parsed["tickers"]),
                _jdump(parsed["sectors"]),
                parsed["direction"],
                _jdump(parsed["branches"]),
                _jdump(parsed["verify_signals"]),
                _jdump(mcheck),
                eff_ttl,
                now_ts,
                exp_ts,
            ],
        )
        row = db.execute("SELECT * FROM circle_intel_items WHERE id=?", [iid]).fetchone()
        cred_map = _author_credibility_map(db)
    return _serialize_intel_row(row, author, cred_map, now_ts)


def list_visible_intel(
    viewer: str,
    *,
    scope: str = "active",
    query: str = "",
    category: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """List circle-shared items + viewer's own private items (strictly isolated)."""
    init_private_agent_db()
    now_ts = int(time.time())
    bounded = max(1, min(100, int(limit or 50)))
    q = str(query or "").strip().lower()

    with get_db() as db:
        # Strict SQL boundary: a user ONLY ever sees visibility='circle' OR author=viewer
        if scope == "mine":
            sql = "SELECT * FROM circle_intel_items WHERE author=? ORDER BY created_at DESC LIMIT 150"
            params: list[Any] = [viewer]
        elif scope == "private":
            sql = "SELECT * FROM circle_intel_items WHERE author=? AND visibility='private' ORDER BY created_at DESC LIMIT 150"
            params = [viewer]
        elif scope == "expired":
            sql = "SELECT * FROM circle_intel_items WHERE (visibility='circle' OR author=?) AND expires_at<=? ORDER BY created_at DESC LIMIT 150"
            params = [viewer, now_ts]
        elif scope == "all":
            sql = "SELECT * FROM circle_intel_items WHERE (visibility='circle' OR author=?) ORDER BY created_at DESC LIMIT 150"
            params = [viewer]
        else:
            # Default 'active': only unexpired items!
            sql = "SELECT * FROM circle_intel_items WHERE (visibility='circle' OR author=?) AND expires_at>? ORDER BY created_at DESC LIMIT 150"
            params = [viewer, now_ts]

        rows = db.execute(sql, params).fetchall()
        cred_map = _author_credibility_map(db)
        active_cnt = db.execute(
            "SELECT COUNT(*) FROM circle_intel_items WHERE (visibility='circle' OR author=?) AND expires_at>?",
            [viewer, now_ts],
        ).fetchone()[0]
        priv_cnt = db.execute(
            "SELECT COUNT(*) FROM circle_intel_items WHERE author=? AND visibility='private'",
            [viewer],
        ).fetchone()[0]

    items: list[dict[str, Any]] = []
    for r in rows:
        if category and r["category"] != category:
            continue
        item = _serialize_intel_row(r, viewer, cred_map, now_ts)
        if q:
            hay = " ".join(
                [
                    item["headline"],
                    item["raw_text"],
                    " ".join(item["tickers"]),
                    " ".join(item["sectors"]),
                ]
            ).lower()
            if q not in hay:
                continue
        items.append(item)
        if len(items) >= bounded:
            break

    # Sort active items by endorsement boost + recency
    if scope == "active":
        items.sort(key=lambda x: x["endorse_count"] * 7200 + x["created_at"], reverse=True)

    return {
        "items": items,
        "counts": {
            "active": int(active_cnt or 0),
            "private_mine": int(priv_cnt or 0),
            "returned": len(items),
        },
    }


def reconcile_intel_item(intel_id: str, viewer: str) -> dict[str, Any]:
    """Verify an intel item against latest market price vs benchmark price recorded at submission."""
    init_private_agent_db()
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM circle_intel_items WHERE id=? AND (visibility='circle' OR author=?)",
            [intel_id, viewer],
        ).fetchone()
    if not row:
        raise HTTPException(404, "情报不存在或无权访问")

    mcheck = _jload(row["market_check_json"], {})
    bcode = str(mcheck.get("benchmark_code") or "sh000300")
    ref_price = mcheck.get("benchmark_price")
    quotes = fetch_fast_quotes([bcode])
    q = quotes.get(bcode)
    if not q or not q.get("price"):
        raise HTTPException(503, "暂未获取到最新行情报价，稍后可重试对账")

    cur_price = float(q["price"])
    bname = str(q.get("name") or mcheck.get("benchmark_name") or bcode)
    now_ts = int(time.time())

    if ref_price and float(ref_price) > 0:
        move_pct = round((cur_price / float(ref_price) - 1.0) * 100.0, 2)
        direction = row["direction"]
        if direction == "bullish":
            status = (
                "supported"
                if move_pct >= 1.5
                else ("contradicted" if move_pct <= -2.5 else "expired_neutral")
            )
        elif direction in {"bearish", "cautious"}:
            status = (
                "supported" if move_pct <= 0.8 else ("contradicted" if move_pct >= 3.5 else "expired_neutral")
            )
        else:
            status = "expired_neutral"
        summary = f"对照基准 {bname}：入库价 {ref_price} → 现价 {cur_price}（区间变动 {move_pct:+.2f}%）。"
    else:
        status = "expired_neutral"
        summary = f"最新核验 {bname} 现价 {cur_price}（当日 {q.get('change_pct', 0):+.2f}%）。"

    with get_db() as db:
        db.execute(
            "UPDATE circle_intel_items SET reconcile_status=?, reconcile_summary=?, reconciled_at=? WHERE id=?",
            [status, summary, now_ts, intel_id],
        )
        updated = db.execute("SELECT * FROM circle_intel_items WHERE id=?", [intel_id]).fetchone()
        cred_map = _author_credibility_map(db)
    return _serialize_intel_row(updated, viewer, cred_map, now_ts)


def _has_table(db, name: str) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [name]).fetchone())


def build_agent_intel_digest_in_tx(db, viewer: str, page_context: dict | None = None) -> list[dict[str, Any]]:
    """Select up to 5 fresh, relevant Circle + Personal Intel items for Agent run snapshot."""
    if not _has_table(db, "circle_intel_items"):
        return []
    now_ts = int(time.time())
    rows = db.execute(
        """
        SELECT * FROM circle_intel_items
        WHERE (visibility='circle' OR author=?) AND expires_at>?
        ORDER BY created_at DESC LIMIT 40
        """,
        [viewer, now_ts],
    ).fetchall()
    if not rows:
        return []
    cred_map = _author_credibility_map(db)
    focus_ticker = str((page_context or {}).get("ticker") or (page_context or {}).get("symbol") or "").upper()
    focus_name = str((page_context or {}).get("name") or "")

    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        item = _serialize_intel_row(r, viewer, cred_map, now_ts)
        score = item["endorse_count"] * 3
        if focus_ticker and (
            focus_ticker in [t.upper() for t in item["tickers"]] or focus_ticker in item["raw_text"].upper()
        ):
            score += 50
        elif focus_name and len(focus_name) >= 2 and focus_name in item["raw_text"]:
            score += 40
        elif item["scope_type"] == "market" or item["category"] in {"strategy", "macro"}:
            score += 15
        scored.append((score, item))

    scored.sort(key=lambda pair: (pair[0], pair[1]["created_at"]), reverse=True)
    digest: list[dict[str, Any]] = []
    for _sc, it in scored[:5]:
        cred = it.get("author_credibility") or {}
        digest.append(
            {
                "id": it["id"],
                "source": "本人私密笔记"
                if it["visibility"] == "private"
                else f"圈内共享({it['display_author']})",
                "category": it["category_label"],
                "headline": it["headline"],
                "raw_text": it["raw_text"][:280],
                "branches": it["branches"],
                "verify_signals": it["verify_signals"],
                "market_check": it["market_check"].get("badge", ""),
                "endorse_count": it["endorse_count"],
                "author_hit_rate_pct": cred.get("hit_rate_pct"),
                "remaining_days": it["remaining_days"],
            }
        )
    return digest


# ── 2. Layer-1 Private Position State (Strictly Owner-Only) ──


class HoldingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(default="", max_length=32)
    cost_price: float | None = Field(default=None, gt=0, le=1_000_000)
    weight_note: str = Field(default="", max_length=32)


class PositionStateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    position_pct: int = Field(ge=0, le=100)
    max_single_loss_pct: float = Field(default=8.0, ge=1.0, le=50.0)
    cash_note: str = Field(default="", max_length=160)
    holdings: list[HoldingItem] = Field(default_factory=list, max_length=15)


def get_position_state_in_tx(db, owner: str) -> dict[str, Any]:
    row = (
        db.execute("SELECT * FROM user_position_state WHERE username=?", [owner]).fetchone()
        if _has_table(db, "user_position_state")
        else None
    )
    if not row:
        return {
            "configured": False,
            "position_pct": 0,
            "tier_label": "未设置底牌",
            "max_single_loss_pct": 8.0,
            "cash_note": "",
            "holdings": [],
            "updated_at": None,
        }
    pct = int(row["position_pct"])
    tier = (
        "空仓观望 (0%)"
        if pct == 0
        else "轻仓试错 (≤30%)"
        if pct <= 30
        else "中等底仓 (31%~65%)"
        if pct <= 65
        else "重仓博弈 (66%~85%)"
        if pct <= 85
        else "极高满仓 (>85%)"
    )
    return {
        "configured": True,
        "position_pct": pct,
        "tier_label": tier,
        "max_single_loss_pct": float(row["max_single_loss_pct"]),
        "cash_note": str(row["cash_note"] or ""),
        "holdings": _jload(row["holdings_json"], []),
        "updated_at": int(row["updated_at"]),
    }


def save_position_state(owner: str, body: PositionStateUpdate) -> dict[str, Any]:
    init_private_agent_db()
    now_ts = int(time.time())
    holdings = [h.model_dump() for h in body.holdings]
    with get_db() as db:
        db.execute(
            """
            INSERT INTO user_position_state(username, position_pct, max_single_loss_pct, cash_note, holdings_json, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET
                position_pct=excluded.position_pct,
                max_single_loss_pct=excluded.max_single_loss_pct,
                cash_note=excluded.cash_note,
                holdings_json=excluded.holdings_json,
                updated_at=excluded.updated_at
            """,
            [
                owner,
                body.position_pct,
                round(body.max_single_loss_pct, 1),
                body.cash_note,
                _jdump(holdings),
                now_ts,
            ],
        )
        return get_position_state_in_tx(db, owner)


# ── 3. Proactive Personal Watchman (0-Token Natural Language Sentinel + Personal Push) ──


def compile_watchman_sentence(raw_text: str) -> dict[str, Any]:
    """Compile a plain-Chinese watch instruction into a deterministic 0-token rule."""
    text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
    if len(text) < 3 or len(text) > 240:
        raise ValueError("请输入清晰的盯盘条件，例如：600519 跌破 1480 提醒我")

    # Identify target instrument (Index -> 6-digit Equity/ETF -> Futures code/name)
    target_code = ""
    target_name = ""
    for alias, (icode, iname) in _INDEX_ALIASES.items():
        if alias in text:
            target_code, target_name = icode, iname
            break
    if not target_code:
        m_eq = re.search(r"(?<!\d)([036]\d{5}|5[168]\d{4}|159\d{3}|8\d{5}|688\d{3})(?!\d)", text)
        if m_eq:
            target_code = m_eq.group(1)
            target_name = target_code
    if not target_code:
        for kw, (fcode, fname) in _FUTURES_NAME_MAP.items():
            if kw in text:
                target_code, target_name = fcode, fname
                break
    if not target_code:
        m_fut = re.search(r"\b([A-Za-z]{1,2})\b", text)
        if m_fut and m_fut.group(1).upper() in {v[0] for v in _FUTURES_NAME_MAP.values()}:
            target_code = m_fut.group(1).upper()
            target_name = target_code

    # Check percentage change rule first (e.g., 涨幅超过 5% / 跌超 3%)
    m_pct = re.search(r"(涨|拉升|跌|回撤|下杀)[^0-9\-+]{0,6}([+-]?\d+(?:\.\d+)?)\s*%", text)
    if m_pct and target_code:
        is_up = m_pct.group(1) in {"涨", "拉升"}
        val = abs(float(m_pct.group(2)))
        return {
            "rule_kind": "change_pct_above" if is_up else "change_pct_below",
            "target_code": target_code,
            "target_name": target_name,
            "threshold": val if is_up else -val,
            "keyword": "",
        }

    # Check price level rule (e.g., 跌破 4000 / 站上 1500 / 高于 / 低于 / 到)
    m_px = re.search(
        r"(跌破|低于|下破|回踩到|回调到|不破|突破|站上|高于|上穿|涨到|触及)\s*([0-9]+(?:\.[0-9]+)?)",
        text,
    )
    if m_px and target_code:
        verb = m_px.group(1)
        px = float(m_px.group(2))
        if px <= 0:
            raise ValueError("目标价格须大于 0")
        is_below = verb in {"跌破", "低于", "下破", "回踩到", "回调到"}
        return {
            "rule_kind": "price_below" if is_below else "price_above",
            "target_code": target_code,
            "target_name": target_name,
            "threshold": px,
            "keyword": "",
        }

    # Otherwise treat as Radar / Circle Intel keyword watch!
    kw_clean = re.sub(
        r"^(帮我盯着|帮我关注|盯着|关注|当|如果|一旦|风向雷达|风向|圈内情报|消息|命中|出现|有关)\s*[：:]*",
        "",
        text,
    )
    kw_clean = re.sub(r"(的时候)?(请)?(发微信|推送|通知|提醒|叫)我.*$", "", kw_clean).strip(" ，。！？“”\"'")
    if len(kw_clean) < 2:
        kw_clean = target_name or text[:24]
    return {
        "rule_kind": "keyword_hit",
        "target_code": target_code or "RADAR",
        "target_name": f"关键词「{kw_clean[:20]}」",
        "threshold": 0.0,
        "keyword": kw_clean[:32],
    }


def _describe_watch_rule(r) -> str:
    kind = r["rule_kind"]
    name = r["target_name"] or r["target_code"]
    th = float(r["threshold"] or 0.0)
    if kind == "price_above":
        return f"{name} ({r['target_code']}) 价格 ≥ {th:g}"
    if kind == "price_below":
        return f"{name} ({r['target_code']}) 价格 ≤ {th:g}"
    if kind == "change_pct_above":
        return f"{name} ({r['target_code']}) 当日涨幅 ≥ +{abs(th):g}%"
    if kind == "change_pct_below":
        return f"{name} ({r['target_code']}) 当日跌幅 ≤ -{abs(th):g}%"
    return f"风向雷达 / 圈内情报命中「{r['keyword']}」"


def create_watchman_rule(owner: str, raw_text: str) -> dict[str, Any]:
    init_private_agent_db()
    spec = compile_watchman_sentence(raw_text)
    now_ts = int(time.time())
    rid = secrets.token_hex(10)
    with get_db() as db:
        cnt = db.execute(
            "SELECT COUNT(*) FROM user_watchman_rules WHERE username=? AND enabled=1", [owner]
        ).fetchone()[0]
        if int(cnt or 0) >= 25:
            raise ValueError("同时启用的盯盘守望规则最多 25 条，请先清理旧规则")
        db.execute(
            """
            INSERT INTO user_watchman_rules(
                id, username, raw_text, rule_kind, target_code, target_name, threshold, keyword, enabled, created_at
            ) VALUES (?,?,?,?,?,?,?,?,1,?)
            """,
            [
                rid,
                owner,
                str(raw_text).strip()[:240],
                spec["rule_kind"],
                spec["target_code"],
                spec["target_name"],
                spec["threshold"],
                spec["keyword"],
                now_ts,
            ],
        )
        row = db.execute("SELECT * FROM user_watchman_rules WHERE id=?", [rid]).fetchone()
    return _serialize_watch_row(row)


def _serialize_watch_row(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "raw_text": row["raw_text"],
        "rule_kind": row["rule_kind"],
        "description": _describe_watch_rule(row),
        "target_code": row["target_code"],
        "target_name": row["target_name"],
        "threshold": float(row["threshold"] or 0.0),
        "keyword": row["keyword"],
        "enabled": bool(row["enabled"]),
        "created_at": int(row["created_at"]),
        "last_checked_at": row["last_checked_at"],
        "last_observed": row["last_observed"] or "",
        "triggered_at": row["triggered_at"],
        "trigger_summary": row["trigger_summary"] or "",
        "push_delivered": bool(row["push_delivered"]),
    }


def list_watchman_rules(owner: str) -> list[dict[str, Any]]:
    init_private_agent_db()
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM user_watchman_rules WHERE username=? ORDER BY enabled DESC, created_at DESC LIMIT 50",
            [owner],
        ).fetchall()
    return [_serialize_watch_row(r) for r in rows]


def _deliver_private_watch_alert(owner: str, rule_desc: str, summary: str) -> bool:
    """Send triggered watchman alert strictly to `owner`'s personal push channel (if configured)."""
    try:
        with get_db() as db:
            ch = db.execute(
                "SELECT provider, endpoint_secret, enabled, bark_level FROM user_radar_channels WHERE username=?",
                [owner],
            ).fetchone()
            pos = get_position_state_in_tx(db, owner)
        if not ch or not ch["enabled"] or not ch["endpoint_secret"]:
            return False
        pos_hint = (
            f"\n你的当前仓位底牌：{pos['position_pct']}%（{pos['tier_label']}）"
            if pos.get("configured")
            else ""
        )
        ok, _status = event_radar.send_push_notification(
            str(ch["provider"]),
            str(ch["endpoint_secret"]),
            title=f"Prism 私人盯盘触发 · {rule_desc[:36]}",
            subtitle="个人守望条件已达成",
            body=f"{summary}{pos_hint}",
            bark_level=str(ch["bark_level"] or "timeSensitive"),
        )
        return bool(ok)
    except Exception as exc:
        log.warning("private_watchman_push_failed: %s", type(exc).__name__)
        return False


def evaluate_watchman_once(owner_filter: str | None = None) -> dict[str, int]:
    """0-token sweep across active watchman rules (owner-scoped when triggered by a user)."""
    init_private_agent_db()
    now_ts = int(time.time())
    with get_db() as db:
        if owner_filter:
            active_rows = db.execute(
                "SELECT * FROM user_watchman_rules WHERE username=? AND enabled=1 AND triggered_at IS NULL LIMIT 50",
                [owner_filter],
            ).fetchall()
        else:
            active_rows = db.execute(
                "SELECT * FROM user_watchman_rules WHERE enabled=1 AND triggered_at IS NULL LIMIT 200"
            ).fetchall()
    if not active_rows:
        return {"checked": 0, "triggered": 0}

    price_codes = list(
        {str(r["target_code"]) for r in active_rows if r["rule_kind"] != "keyword_hit" and r["target_code"]}
    )
    quotes = fetch_fast_quotes(price_codes) if price_codes else {}

    triggered_cnt = 0
    for r in active_rows:
        kind = r["rule_kind"]
        owner = str(r["username"])
        rid = str(r["id"])
        hit = False
        observed = ""
        summary = ""

        if kind in {"price_above", "price_below", "change_pct_above", "change_pct_below"}:
            q = quotes.get(str(r["target_code"]))
            if not q:
                continue
            px = float(q["price"])
            chg = float(q["change_pct"])
            nm = str(q.get("name") or r["target_name"] or r["target_code"])
            observed = f"{nm} 现价 {px} ({chg:+.2f}%)"
            th = float(r["threshold"])
            if kind == "price_above" and px >= th:
                hit = True
                summary = f"{nm} 最新价 {px}（当日 {chg:+.2f}%）已突破/站上设定阈值 {th:g}。"
            elif kind == "price_below" and px <= th:
                hit = True
                summary = f"{nm} 最新价 {px}（当日 {chg:+.2f}%）已触及/跌破设定阈值 {th:g}。"
            elif kind == "change_pct_above" and chg >= abs(th):
                hit = True
                summary = f"{nm} 当日涨幅达 {chg:+.2f}%（现价 {px}），超过设定阈值 +{abs(th):g}%。"
            elif kind == "change_pct_below" and chg <= -abs(th):
                hit = True
                summary = f"{nm} 当日跌幅达 {chg:+.2f}%（现价 {px}），触及设定阈值 -{abs(th):g}%。"
        elif kind == "keyword_hit":
            kw = str(r["keyword"] or "").strip()
            created_since = int(r["created_at"])
            if kw:
                with get_db() as db:
                    rad = db.execute(
                        "SELECT title, tier FROM radar_events WHERE created_at>=? AND (title LIKE ? OR body LIKE ?) ORDER BY created_at DESC LIMIT 1",
                        [created_since, f"%{kw}%", f"%{kw}%"],
                    ).fetchone()
                    intel = db.execute(
                        "SELECT headline FROM circle_intel_items WHERE created_at>=? AND (visibility='circle' OR author=?) AND raw_text LIKE ? ORDER BY created_at DESC LIMIT 1",
                        [created_since, owner, f"%{kw}%"],
                    ).fetchone()
                if rad:
                    hit = True
                    observed = f"风向[{rad['tier']}] {rad['title'][:32]}"
                    summary = f"风向雷达新快讯命中「{kw}」：[{rad['tier']}] {rad['title']}"
                elif intel:
                    hit = True
                    observed = f"圈内情报：{intel['headline'][:32]}"
                    summary = f"圈内新增情报命中「{kw}」：{intel['headline']}"
                else:
                    observed = "持续监听中"

        with get_db() as db:
            if hit:
                desc = _describe_watch_rule(r)
                pushed = _deliver_private_watch_alert(owner, desc, summary)
                db.execute(
                    """
                    UPDATE user_watchman_rules
                    SET enabled=0, last_checked_at=?, last_observed=?, triggered_at=?, trigger_summary=?, push_delivered=?
                    WHERE id=?
                    """,
                    [now_ts, observed, now_ts, summary, 1 if pushed else 0, rid],
                )
                triggered_cnt += 1
            else:
                db.execute(
                    "UPDATE user_watchman_rules SET last_checked_at=?, last_observed=? WHERE id=?",
                    [now_ts, observed, rid],
                )
    return {"checked": len(active_rows), "triggered": triggered_cnt}


async def run_personal_watchman_loop(manager: Manager) -> None:
    """Cooperative 45-second 0-token personal sentinel loop."""
    init_private_agent_db()
    await asyncio.sleep(8)
    while True:
        try:
            lease = manager.admit("auxiliary", persist_required=False)
        except LifecycleError:
            await asyncio.sleep(5)
            continue
        try:
            with lease:
                await manager.run_blocking("auxiliary", evaluate_watchman_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("personal_watchman_loop_err: %s", type(exc).__name__)
        await asyncio.sleep(45)


# ── HTTP Endpoints: Circle Intel (`/api/intel/*`) ──


class IntelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    raw_text: str = Field(min_length=4, max_length=4000)
    visibility: IntelVisibility = "circle"
    anonymous: bool = False
    ttl_days: int | None = Field(default=None, ge=1, le=90)


@intel_router.get("/feed")
async def api_list_intel(
    owner: Owner,
    scope: str = "active",
    q: str = "",
    category: str = "",
    limit: int = 50,
):
    return await run_in_threadpool(
        list_visible_intel, owner, scope=scope, query=q, category=category, limit=limit
    )


@intel_router.post("")
async def api_create_intel(body: IntelCreateRequest, owner: Owner, request: Request):
    verify_csrf(request)
    if not check_rate_limit(f"intel_create:{owner}", max_req=20, window=60):
        raise HTTPException(429, "投喂过于频繁，请稍后再试")
    try:
        item = await run_in_threadpool(
            submit_circle_intel,
            owner,
            raw_text=body.raw_text,
            visibility=body.visibility,
            anonymous=body.anonymous,
            ttl_days=body.ttl_days,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"ok": True, "item": item}


@intel_router.post("/{intel_id}/endorse")
async def api_endorse_intel(intel_id: str, owner: Owner, request: Request):
    verify_csrf(request)
    init_private_agent_db()
    now_ts = int(time.time())
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM circle_intel_items WHERE id=? AND (visibility='circle' OR author=?)",
            [intel_id, owner],
        ).fetchone()
        if not row:
            raise HTTPException(404, "情报不存在或无权操作")
        upvotes: list[str] = _jload(row["upvotes_json"], [])
        if owner in upvotes:
            upvotes.remove(owner)
        else:
            upvotes.append(owner)
        db.execute(
            "UPDATE circle_intel_items SET upvotes_json=? WHERE id=?",
            [_jdump(upvotes), intel_id],
        )
        updated = db.execute("SELECT * FROM circle_intel_items WHERE id=?", [intel_id]).fetchone()
        cred_map = _author_credibility_map(db)
    return {"ok": True, "item": _serialize_intel_row(updated, owner, cred_map, now_ts)}


@intel_router.post("/{intel_id}/reconcile")
async def api_reconcile_intel(intel_id: str, owner: Owner, request: Request):
    verify_csrf(request)
    item = await run_in_threadpool(reconcile_intel_item, intel_id, owner)
    return {"ok": True, "item": item}


@intel_router.delete("/{intel_id}")
async def api_delete_intel(intel_id: str, owner: Owner, request: Request):
    verify_csrf(request)
    init_private_agent_db()
    with get_db() as db:
        row = db.execute(
            "SELECT author, visibility FROM circle_intel_items WHERE id=?", [intel_id]
        ).fetchone()
        if not row:
            raise HTTPException(404, "情报不存在")
        can_del = str(row["author"]) == owner or (row["visibility"] == "circle" and is_admin_user(owner))
        if not can_del:
            raise HTTPException(403, "只能删除本人发布的情报")
        db.execute("DELETE FROM circle_intel_items WHERE id=?", [intel_id])
    return {"ok": True}


# ── HTTP Endpoints: Private Personal Agent (`/api/private-agent/*`) ──


class WatchmanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    raw_text: str = Field(min_length=3, max_length=240)


class UserPrivatePushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    provider: str = Field(min_length=2, max_length=32)
    secret: str | None = Field(default=None, max_length=512)
    enabled: bool = True
    bark_level: str = Field(default="timeSensitive", max_length=32)


@private_router.get("/hub")
async def api_get_private_agent_hub(owner: Owner):
    """Return strictly owner-scoped position state, watchman rules, and personal push settings."""

    def _load():
        init_private_agent_db()
        with get_db() as db:
            pos = get_position_state_in_tx(db, owner)
        rules = list_watchman_rules(owner)
        push_cfg = event_radar.get_user_push_config(owner)
        providers = event_radar.list_push_providers()
        return {
            "position_state": pos,
            "watchman_rules": rules,
            "push": push_cfg,
            "push_providers": providers,
        }

    return await run_in_threadpool(_load)


@private_router.put("/state")
async def api_save_position_state(body: PositionStateUpdate, owner: Owner, request: Request):
    verify_csrf(request)
    state = await run_in_threadpool(save_position_state, owner, body)
    return {"ok": True, "position_state": state}


@private_router.post("/watchman")
async def api_create_watchman(body: WatchmanCreateRequest, owner: Owner, request: Request):
    verify_csrf(request)
    try:
        rule = await run_in_threadpool(create_watchman_rule, owner, body.raw_text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"ok": True, "rule": rule, "rules": await run_in_threadpool(list_watchman_rules, owner)}


@private_router.post("/watchman/check-now")
async def api_check_watchman_now(owner: Owner, request: Request):
    verify_csrf(request)
    summary = await run_in_threadpool(evaluate_watchman_once, owner)
    return {"ok": True, "summary": summary, "rules": await run_in_threadpool(list_watchman_rules, owner)}


@private_router.delete("/watchman/{rule_id}")
async def api_delete_watchman(rule_id: str, owner: Owner, request: Request):
    verify_csrf(request)
    init_private_agent_db()
    with get_db() as db:
        cur = db.execute("DELETE FROM user_watchman_rules WHERE id=? AND username=?", [rule_id, owner])
    if not cur.rowcount:
        raise HTTPException(404, "盯盘规则不存在")
    return {"ok": True}


@private_router.put("/push")
async def api_save_private_push(body: UserPrivatePushRequest, owner: Owner, request: Request):
    verify_csrf(request)
    try:
        cfg = await run_in_threadpool(
            event_radar.save_user_push_config,
            owner,
            provider=body.provider,
            secret=body.secret,
            enabled=body.enabled,
            min_tier="T0",
            bark_level=body.bark_level,
            notify_watchlist=True,
        )
    except ValueError as exc:
        code = str(exc)
        msg_map = {
            "empty_push_secret": "请填写完整的个人推送凭证或 Webhook 链接",
            "unsupported_push_provider": "不支持该推送通道",
            "invalid_bark_level": "推送模式参数无效",
        }
        raise HTTPException(422, msg_map.get(code, f"推送配置校验未通过（{code}）")) from None
    return {"ok": True, "push": cfg}


@private_router.post("/push/test")
async def api_test_private_push(owner: Owner, request: Request):
    verify_csrf(request)

    def _send():
        with get_db() as db:
            row = db.execute(
                "SELECT provider, endpoint_secret, bark_level FROM user_radar_channels WHERE username=?",
                [owner],
            ).fetchone()
        if not row or not row["endpoint_secret"]:
            raise ValueError("尚未保存个人推送凭证")
        ok, status = event_radar.send_push_notification(
            str(row["provider"]),
            str(row["endpoint_secret"]),
            title="Prism 私人助理 · 通道连通测试",
            subtitle=f"专属账号：{owner}",
            body="你的私人盯盘守望通道已连通！当设定的价格、涨跌幅或情报关键词触发时，将第一时间推送至本设备。",
            bark_level=str(row["bark_level"] or "timeSensitive"),
        )
        if not ok:
            raise ValueError(f"发送未成功：{status}")
        return status

    try:
        st = await run_in_threadpool(_send)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"ok": True, "status": st}
