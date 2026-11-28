"""Explicit, versioned user investment persona, human-confirmed traits, and run snapshots."""

from __future__ import annotations

import json
import secrets
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import require_auth, verify_csrf
from .database import get_db
from .request_validation import read_model_body

router = APIRouter(prefix="/api/preferences", tags=["preferences"])

ArchetypeId = Literal["value", "cycle", "trend", "defensive", "custom"]
MarketFocus = Literal["ashare", "futures", "cross_asset"]
TraitCategory = Literal["discipline", "focus", "format", "avoid"]

ARCHETYPES: dict[str, dict] = {
    "value": {
        "id": "value",
        "name": "价值基本面派",
        "tagline": "重财报现金流、ROE 与估值安全边际，排斥无业绩支撑的概念炒作",
        "lens": (
            "优先核查公司财报原文（经营现金流净额、ROE、毛利率趋势、资产负债结构）与历史估值分位；"
            "对缺少基本面兑现的纯情绪题材保持审慎，强调长周期赔率与安全边际。"
        ),
        "defaults": {
            "style": "conservative",
            "horizon": "long",
            "max_position_pct": 25,
            "max_drawdown_pct": 15,
            "allow_leverage": False,
        },
    },
    "cycle": {
        "id": "cycle",
        "name": "产业周期与供需派",
        "tagline": "重上下游产能瓶颈、期现基差、库存去化与产业链利润传导",
        "lens": (
            "优先核查上下游供需格局、现货基差、交易所仓单与社会库存变化、盘面加工利润及股期联动映射；"
            "区分涨价能否向中下游顺价，关注中期供需错配拐点与催化剂兑现。"
        ),
        "defaults": {
            "style": "balanced",
            "horizon": "medium",
            "max_position_pct": 20,
            "max_drawdown_pct": 12,
            "allow_leverage": True,
        },
    },
    "trend": {
        "id": "trend",
        "name": "趋势动量与量化派",
        "tagline": "重均线量价共振、波动率带宽与右侧信号纪律，严格执行盈亏比与止损线",
        "lens": (
            "优先核查多周期均线结构、量价突破有效性、20日历史波动率热度分位及关键支撑阻力位；"
            "强调右侧确认与清晰的价格失效止损条件，回避逆势摸顶抄底。"
        ),
        "defaults": {
            "style": "aggressive",
            "horizon": "short",
            "max_position_pct": 15,
            "max_drawdown_pct": 8,
            "allow_leverage": True,
        },
    },
    "defensive": {
        "id": "defensive",
        "name": "稳健绝对收益 / 套利派",
        "tagline": "回避高分位拥挤追涨，偏好低分位安全垫、期现正反套、跨期月差与高股息",
        "lens": (
            "严控最大回撤，对处于近5年80%分位以上的拥挤品种主动提示追高风险；"
            "优先评估低估值/低分位安全垫、期现基差回归、跨期月差结构及下行保护。"
        ),
        "defaults": {
            "style": "conservative",
            "horizon": "medium",
            "max_position_pct": 15,
            "max_drawdown_pct": 6,
            "allow_leverage": False,
        },
    },
}


class ProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_version: int = Field(ge=0)
    style: Literal["conservative", "balanced", "aggressive"]
    horizon: Literal["short", "medium", "long"]
    max_position_pct: int = Field(ge=1, le=100)
    max_drawdown_pct: int = Field(ge=1, le=50)
    allow_leverage: bool = False
    archetype: ArchetypeId = "custom"
    focus_markets: list[MarketFocus] = Field(default_factory=lambda: ["ashare", "futures", "cross_asset"])
    negative_rules: list[str] = Field(default_factory=list, max_length=12)
    decision_style_note: str = Field(default="", max_length=300)

    @field_validator("negative_rules")
    @classmethod
    def _clean_negative_rules(cls, rules: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in rules:
            text = item.strip()
            if not text:
                continue
            if len(text) > 120:
                raise ValueError("单条负面清单不能超过 120 字")
            if text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("focus_markets")
    @classmethod
    def _clean_focus_markets(cls, markets: list[MarketFocus]) -> list[MarketFocus]:
        deduped = list(dict.fromkeys(markets))
        return deduped or ["ashare", "futures", "cross_asset"]


class TraitCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    trait_text: str = Field(min_length=2, max_length=200)
    category: TraitCategory = "focus"


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "dismiss"]


def init_preferences_db() -> None:
    with get_db() as db:
        if db.in_transaction:
            raise RuntimeError("profile schema requires its own transaction")
        db.executescript("""
        CREATE TABLE IF NOT EXISTS research_profiles (
            username TEXT PRIMARY KEY, version INTEGER NOT NULL,
            profile_json TEXT NOT NULL, updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS native_run_profiles (
            run_id TEXT PRIMARY KEY, username TEXT NOT NULL, profile_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_persona_traits (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            trait_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'focus',
            source TEXT NOT NULL DEFAULT 'manual',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_persona_traits_owner
            ON user_persona_traits(username, created_at DESC);
        CREATE TABLE IF NOT EXISTS user_persona_proposals (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            run_id TEXT,
            conv_id TEXT,
            trait_text TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'focus',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            decided_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_persona_proposals_owner
            ON user_persona_proposals(username, status, created_at DESC);
        DROP TRIGGER IF EXISTS profile_account_delete;
        CREATE TRIGGER profile_account_delete AFTER DELETE ON users BEGIN
            DELETE FROM research_profiles WHERE username=OLD.username;
            DELETE FROM native_run_profiles WHERE username=OLD.username;
            DELETE FROM user_persona_traits WHERE username=OLD.username;
            DELETE FROM user_persona_proposals WHERE username=OLD.username;
        END;
        """)


def _table_exists(db, name: str) -> bool:
    return (
        db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", [name]).fetchone() is not None
    )


def profile_in_transaction(db, owner: str) -> dict:
    row = db.execute("SELECT profile_json FROM research_profiles WHERE username=?", [owner]).fetchone()
    if not row:
        return {"configured": False, "version": 0}
    data = json.loads(row[0])
    return data if isinstance(data, dict) else {"configured": False, "version": 0}


def list_traits_in_transaction(db, owner: str) -> list[dict]:
    if not _table_exists(db, "user_persona_traits"):
        return []
    rows = db.execute(
        "SELECT id, trait_text, category, source, created_at FROM user_persona_traits "
        "WHERE username=? ORDER BY created_at DESC LIMIT 30",
        [owner],
    ).fetchall()
    return [dict(r) for r in rows]


def list_pending_proposals_in_transaction(db, owner: str) -> list[dict]:
    if not _table_exists(db, "user_persona_proposals"):
        return []
    rows = db.execute(
        "SELECT id, run_id, conv_id, trait_text, category, reason, status, created_at "
        "FROM user_persona_proposals WHERE username=? AND status='pending' "
        "ORDER BY created_at DESC LIMIT 10",
        [owner],
    ).fetchall()
    return [dict(r) for r in rows]


def propose_persona_trait(
    username: str,
    run_id: str,
    conv_id: str,
    trait_text: str,
    category: str = "focus",
    reason: str = "",
) -> dict:
    """Create a pending human-confirmed trait proposal; never mutates active persona directly."""
    init_preferences_db()
    clean_text = " ".join(str(trait_text or "").split()).strip()[:200]
    clean_reason = " ".join(str(reason or "").split()).strip()[:240]
    cat = category if category in {"discipline", "focus", "format", "avoid"} else "focus"
    if len(clean_text) < 2:
        raise ValueError("invalid_trait_text")
    now = int(time.time())
    pid = secrets.token_hex(12)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [username]
            ).fetchone():
                raise ValueError("unavailable_owner")
            already_confirmed = db.execute(
                "SELECT id FROM user_persona_traits WHERE username=? AND trait_text=?",
                [username, clean_text],
            ).fetchone()
            if already_confirmed:
                db.execute("COMMIT")
                return {
                    "proposal_id": already_confirmed["id"],
                    "trait_text": clean_text,
                    "category": cat,
                    "reason": clean_reason,
                    "status": "already_in_persona",
                }
            existing_pending = db.execute(
                "SELECT id FROM user_persona_proposals WHERE username=? AND status='pending' AND trait_text=?",
                [username, clean_text],
            ).fetchone()
            if existing_pending:
                pid = existing_pending["id"]
            else:
                db.execute(
                    """INSERT INTO user_persona_proposals
                       (id, username, run_id, conv_id, trait_text, category, reason, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                    [pid, username, run_id, conv_id, clean_text, cat, clean_reason, now],
                )
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return {
        "proposal_id": pid,
        "trait_text": clean_text,
        "category": cat,
        "reason": clean_reason,
        "status": "pending_user_confirmation",
    }


def _sanitize_page_context(page_context: dict | None) -> dict | None:
    if not isinstance(page_context, dict) or not page_context:
        return None
    allowed_keys = (
        "surface",
        "ticker",
        "name",
        "symbol",
        "period",
        "workspace_tab",
        "summary",
        "response_mode",
    )
    cleaned: dict[str, str] = {}
    for key in allowed_keys:
        val = page_context.get(key)
        if isinstance(val, str) and val.strip():
            limit = 500 if key == "summary" else 64
            cleaned[key] = " ".join(val.split())[:limit]
    return cleaned or None


def snapshot_run(db, owner: str, run_id: str, *, page_context: dict | None = None) -> None:
    value = profile_in_transaction(db, owner)
    traits = list_traits_in_transaction(db, owner)
    if traits:
        value = {
            **value,
            "persona_traits": [{"category": t["category"], "text": t["trait_text"]} for t in traits[:20]],
        }
    try:
        from .journal_hub import summarize_user_journal_in_tx

        jstats = summarize_user_journal_in_tx(db, owner)
        if jstats.get("total", 0) > 0:
            value = {
                **value,
                "journal_track_record": {
                    "total": jstats["total"],
                    "supported": jstats["supported"],
                    "contradicted": jstats["contradicted"],
                    "win_rate_pct": jstats["win_rate_pct"],
                    "bias_hint": jstats["bias_hint"],
                },
            }
    except Exception:
        pass
    clean_ctx = _sanitize_page_context(page_context)
    if clean_ctx:
        value = {**value, "page_context": clean_ctx}
    try:
        from .private_agent import build_agent_intel_digest_in_tx, get_position_state_in_tx

        pos_state = get_position_state_in_tx(db, owner)
        if pos_state.get("configured"):
            value = {**value, "position_state": pos_state}
        intel_digest = build_agent_intel_digest_in_tx(db, owner, clean_ctx)
        if intel_digest:
            value = {**value, "circle_intel_digest": intel_digest}
    except Exception:
        pass
    if _table_exists(db, "portfolio_snapshots"):
        row = db.execute("SELECT payload FROM portfolio_snapshots WHERE owner=?", [owner]).fetchone()
        portfolio = json.loads(row[0]) if row else None
        if portfolio and portfolio.get("share_with_agent") is True:
            from .portfolio import evaluate

            # Freeze only the explicitly shared risk summary, never position rows.
            value = {**value, "portfolio_risk": evaluate(value, portfolio)}
    db.execute("INSERT INTO native_run_profiles VALUES(?,?,?)", [run_id, owner, json.dumps(value)])


def run_profile(db, owner: str, run_id: str) -> dict:
    row = db.execute(
        "SELECT profile_json FROM native_run_profiles WHERE run_id=? AND username=?", [run_id, owner]
    ).fetchone()
    return json.loads(row[0]) if row else {"configured": False, "version": 0}


def profile_prompt(value: dict) -> str:
    parts: list[str] = []
    if not value.get("configured"):
        parts.append("\n用户尚未设置显式研究偏好，不推断其风险承受能力；涉及个人仓位时先说明缺少风险约束。")
    else:
        core_view = {
            k: v
            for k, v in value.items()
            if k
            not in {
                "persona_traits",
                "journal_track_record",
                "page_context",
                "position_state",
                "circle_intel_digest",
            }
        }
        parts.append(
            "\n以下是用户主动保存、本轮固定的投研画像与风险约束（不是账户资金或持仓证明）："
            + json.dumps(core_view, ensure_ascii=False, sort_keys=True)
            + "。按这些约束比较情景；风险上限不能解释为可保证的止损或收益。"
            "缺少实际组合、可用资金、合约保证金或最新行情时，风险检查必须标记未评估。"
            "禁止因风格激进而跳过证据、忽略风险上限或建议超限；不允许杠杆时不要推荐杠杆交易。"
            "若含portfolio_risk，它仅为用户主动授权分享的本轮固定风险摘要；不是完整账户、实时报价或独立核验。"
            "摘要注明未评估或超限时保留该限制；不能据此推算真实持仓、下单数量、强平价格或保证止损。"
        )
        archetype = ARCHETYPES.get(str(value.get("archetype") or ""))
        if archetype:
            parts.append(f"用户选择的专属投资流派为【{archetype['name']}】：{archetype['lens']}")
        neg_rules = value.get("negative_rules")
        if isinstance(neg_rules, list) and neg_rules:
            parts.append(
                "用户设定的负面回避清单（分析与建议必须严格核对并遵守）：" + "；".join(neg_rules) + "。"
            )
        style_note = str(value.get("decision_style_note") or "").strip()
        if style_note:
            parts.append(f"用户指定的输出与决策偏好：{style_note}")

    traits = value.get("persona_traits")
    if isinstance(traits, list) and traits:
        formatted = "；".join(
            f"[{t.get('category', 'focus')}] {t.get('text', '')}" for t in traits if isinstance(t, dict)
        )
        if formatted:
            parts.append(f"用户本人已确认的长期个体化研究准则：{formatted}。")

    track = value.get("journal_track_record")
    if isinstance(track, dict) and track.get("total"):
        parts.append(
            f"用户历史判断复盘记录摘要：共登记 {track.get('total')} 笔，已印证 {track.get('supported', 0)} 笔，"
            f"触发证伪 {track.get('contradicted', 0)} 笔（{track.get('bias_hint', '')}）。在给出策略与失效条件时针对性协助其规避过往偏差。"
        )

    pos_state = value.get("position_state")
    if isinstance(pos_state, dict) and pos_state.get("configured"):
        holdings_txt = "、".join(
            f"{h.get('name') or h.get('code')}({h.get('code')})"
            + (f"成本{h.get('cost_price')}" if h.get("cost_price") else "")
            + (f"[{h.get('weight_note')}]" if h.get("weight_note") else "")
            for h in (pos_state.get("holdings") or [])
            if isinstance(h, dict)
        )
        parts.append(
            f"【私人底牌记忆（仅限本人可见，严禁外泄）】用户当前总仓位：{pos_state.get('position_pct')}%（{pos_state.get('tier_label')}），"
            f"单笔最大容忍止损：{pos_state.get('max_single_loss_pct')}%，"
            + (f"核心持仓：{holdings_txt}，" if holdings_txt else "暂未填具体持仓代码，")
            + (f"资金备注：{pos_state.get('cash_note')}。" if pos_state.get("cash_note") else "")
            + "当用户询问操作策略或解读多分支圈内应对剧本时，必须直接对号入座其当前真实仓位档位给出贴身行动建议，不要笼统把空仓/半仓/满仓所有话术抄一遍。"
        )

    intel_digest = value.get("circle_intel_digest")
    if isinstance(intel_digest, list) and intel_digest:
        parts.append(
            "【圈内鲜活情报池摘要（仅保留有效期内条目）】"
            + json.dumps(intel_digest, ensure_ascii=False)
            + "。【信源分层铁律】上述圈内情报属于熟人圈经验策略或未证实产业线索，绝不可冒充官方公告事实；"
            "若与当前问题相关，请单独以『【圈内情报交叉】』小节引用（注明剩余有效期及盘口体检提示），并与客观行情/财报数据交叉比对。"
        )

    parts.append(
        "【反回音室与红蓝军风控铁律】每当用户表现出追涨加仓冲动、重仓押注单一方向，或圈内情报一边倒看多时，"
        "必须在回答末尾或风险节独立列出简明的『【反方证伪视角】』：指出当前涨幅/分位是否已透支、潜在反证，以及明确的认错止损观察位。"
    )

    page_ctx = value.get("page_context")
    if isinstance(page_ctx, dict) and page_ctx:
        parts.append(
            "用户发起本轮提问时正在浏览的平台视口上下文："
            + json.dumps(page_ctx, ensure_ascii=False, sort_keys=True)
            + "。若用户使用‘这只票/当前品种/这个选股结果’等指代，直接结合该视口标的自主调用对应平台工具获取最新实证数据。"
        )

    parts.append(
        "【随行阅读与双速输出规范】用户在看盘工作台右侧伴随栏阅读，请优化信息密度与可读性："
        "1) 结论先行：回答第一段固定以 **【核心结论】** 开头，在 3~4 行内给出明确倾向、关键实证数字与失效条件，让用户在窄栏一眼抓住重点；"
        "2) 按需分流篇幅：若本轮为日常盘面快问、单点查证或简短异动扫描，采用紧凑要点列表（直奔主题，不写空泛宏观铺垫，控制在 400 字以内）；"
        "仅当用户明确要求全面深度拆解、诊股或生成报告时，才在核心结论下方展开分节论述与精炼对比表（单表列数尽量不超过 5 列，避免横向挤压）；"
        "3) 排版克制：严禁在标题、列表或正文中堆砌彩色表情符号（Emoji），全程保持干净、专业、去 AI 腔的机构投研行文风格。"
    )

    parts.append(
        "若用户在对话中明确提出了希望长期保留的个人分析习惯、纪律或研究偏好（而非单次查询指令），"
        "你可调用 persona_propose_trait 提交一条简短清晰的画像提议卡片供用户确认入库；普通问答无须调用。"
    )
    return "\n".join(parts)


def _owner(request: Request) -> str:
    owner = require_auth(request)
    if request.headers.get("X-Prism-Account", owner) != owner:
        raise HTTPException(409, "账号已经切换，请刷新页面")
    return owner


@router.get("/research")
async def read_profile(request: Request):
    owner = _owner(request)
    init_preferences_db()
    with get_db() as db:
        return {"profile": profile_in_transaction(db, owner), "portfolio_risk_status": "not_evaluated"}


@router.put("/research")
async def save_profile(request: Request):
    owner = _owner(request)
    verify_csrf(request)
    body = await read_model_body(request, ProfileUpdate)
    if require_auth(request) != owner:
        raise HTTPException(401, "请重新登录")
    init_preferences_db()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [owner]
            ).fetchone():
                raise HTTPException(401, "请重新登录")
            current = profile_in_transaction(db, owner)
            if current["version"] != body.expected_version:
                raise HTTPException(409, "偏好已在其他设备更新，请重新读取后修改")
            value = {
                **body.model_dump(exclude={"expected_version"}),
                "version": current["version"] + 1,
                "configured": True,
                "updated_at": int(time.time()),
            }
            db.execute(
                "INSERT INTO research_profiles VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET "
                "version=excluded.version,profile_json=excluded.profile_json,updated_at=excluded.updated_at",
                [owner, value["version"], json.dumps(value, ensure_ascii=False), value["updated_at"]],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return {"profile": value, "portfolio_risk_status": "not_evaluated"}


@router.get("/persona")
async def read_full_persona(request: Request):
    owner = _owner(request)
    init_preferences_db()
    from .journal_hub import summarize_user_journal_in_tx

    with get_db() as db:
        profile = profile_in_transaction(db, owner)
        traits = list_traits_in_transaction(db, owner)
        proposals = list_pending_proposals_in_transaction(db, owner)
        jstats = summarize_user_journal_in_tx(db, owner)
        watch_count = 0
        if _table_exists(db, "watchlists"):
            wrow = db.execute("SELECT items_json FROM watchlists WHERE username=?", [owner]).fetchone()
            if wrow and wrow[0]:
                try:
                    watch_count = len(json.loads(wrow[0]))
                except Exception:
                    watch_count = 0
    return {
        "profile": profile,
        "archetypes": list(ARCHETYPES.values()),
        "traits": traits,
        "pending_proposals": proposals,
        "journal_stats": jstats,
        "watchlist_count": watch_count,
        "portfolio_risk_status": "not_evaluated",
    }


@router.post("/traits")
async def create_trait(request: Request):
    owner = _owner(request)
    verify_csrf(request)
    body = await read_model_body(request, TraitCreate)
    init_preferences_db()
    now = int(time.time())
    tid = secrets.token_hex(12)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            if not db.execute(
                "SELECT 1 FROM users WHERE username=? AND disabled_at IS NULL", [owner]
            ).fetchone():
                raise HTTPException(401, "请重新登录")
            cnt = db.execute("SELECT COUNT(*) FROM user_persona_traits WHERE username=?", [owner]).fetchone()[
                0
            ]
            if cnt >= 30:
                raise HTTPException(409, "个人画像记忆条目已达 30 条上限，请先精简旧条目")
            dup = db.execute(
                "SELECT id, trait_text, category, source, created_at FROM user_persona_traits "
                "WHERE username=? AND trait_text=?",
                [owner, body.trait_text],
            ).fetchone()
            if dup:
                db.execute("COMMIT")
                return {"ok": True, "trait": dict(dup), "reused": True}
            db.execute(
                "INSERT INTO user_persona_traits(id, username, trait_text, category, source, created_at) "
                "VALUES (?, ?, ?, ?, 'manual', ?)",
                [tid, owner, body.trait_text, body.category, now],
            )
            row = db.execute(
                "SELECT id, trait_text, category, source, created_at FROM user_persona_traits WHERE id=?",
                [tid],
            ).fetchone()
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return {"ok": True, "trait": dict(row), "reused": False}


@router.delete("/traits/{trait_id}")
async def remove_trait(trait_id: str, request: Request):
    owner = _owner(request)
    verify_csrf(request)
    init_preferences_db()
    with get_db() as db:
        deleted = db.execute(
            "DELETE FROM user_persona_traits WHERE id=? AND username=?",
            [trait_id, owner],
        )
    if not deleted.rowcount:
        raise HTTPException(404, "画像条目不存在")
    return {"ok": True}


@router.post("/proposals/{proposal_id}/decide")
async def decide_proposal(proposal_id: str, request: Request):
    owner = _owner(request)
    verify_csrf(request)
    body = await read_model_body(request, ProposalDecision)
    init_preferences_db()
    now = int(time.time())
    saved_trait = None
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            prop = db.execute(
                "SELECT * FROM user_persona_proposals WHERE id=? AND username=?",
                [proposal_id, owner],
            ).fetchone()
            if not prop:
                raise HTTPException(404, "待确认画像提议不存在")
            next_status = "accepted" if body.decision == "accept" else "dismissed"
            db.execute(
                "UPDATE user_persona_proposals SET status=?, decided_at=? WHERE id=? AND username=?",
                [next_status, now, proposal_id, owner],
            )
            if body.decision == "accept":
                existing = db.execute(
                    "SELECT id, trait_text, category, source, created_at FROM user_persona_traits "
                    "WHERE username=? AND trait_text=?",
                    [owner, prop["trait_text"]],
                ).fetchone()
                if existing:
                    saved_trait = dict(existing)
                else:
                    cnt = db.execute(
                        "SELECT COUNT(*) FROM user_persona_traits WHERE username=?", [owner]
                    ).fetchone()[0]
                    if cnt >= 30:
                        raise HTTPException(409, "个人画像记忆条目已达 30 条上限，请先清理旧条目")
                    tid = secrets.token_hex(12)
                    db.execute(
                        "INSERT INTO user_persona_traits(id, username, trait_text, category, source, created_at) "
                        "VALUES (?, ?, ?, ?, 'agent_confirmed', ?)",
                        [tid, owner, prop["trait_text"], prop["category"], now],
                    )
                    saved_trait = {
                        "id": tid,
                        "trait_text": prop["trait_text"],
                        "category": prop["category"],
                        "source": "agent_confirmed",
                        "created_at": now,
                    }
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
    return {"ok": True, "status": next_status, "trait": saved_trait}
