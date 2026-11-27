"""Prism Event Radar — Global + Domestic sentinel, Cross-Asset Tape, Gemini Search Grounding & Bark push.

Architecture guarantees:
1. Multi-region primary feeds (CLS telegraph, Sina 7x24, Eastmoney Global 7x24, Federal Reserve RSS,
   CNBC Global/Tech RSS, US SEC 8-K Atom) are fetched strictly via ``public_network`` with bounded memory
   and per-source timeout isolation.
2. Live Cross-Asset Tape Confirmation (USD/CNH, CSI 300, Hang Seng Tech, Nasdaq, NVIDIA, Gold, Copper,
   Crude Oil) provides 0-token real-money validation against intraday rumors.
3. Optional Google Gemini official API (``generativelanguage.googleapis.com`` + ``google_search``
   grounding) runs ONLY through a strict funnel + 6h cluster lock + durable SQLite daily circuit breaker
   (``daily_limit``), with ``x-goog-api-key`` header auth and masked secret storage.
4. SSRF-proof allowlisted mobile push with iOS Bark ``timeSensitive`` lock-screen delivery first-class.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import html
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from . import public_network
from .database import get_db
from .work_lifecycle import LifecycleError, Manager

log = logging.getLogger("stock-app")
SHANGHAI = ZoneInfo("Asia/Shanghai")

_CLS_URL = "https://www.cls.cn/api/cache?name=telegraph"
_SINA_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=30&page=1"
_EASTMONEY_URL = (
    "https://np-weblist.eastmoney.com/comm/web/getFastNewsList"
    "?client=web&biz=web_724&fastColumn=102&sortEnd=&pageSize=25&req_trace=1"
)
_FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
_CNBC_WORLD_RSS_URL = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114"
_CNBC_TECH_RSS_URL = "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910"
_SEC_8K_ATOM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcurrent&type=8-K&company=&dateb=&owner=include&start=0&count=25&output=atom"
)
_TENCENT_TAPE_URL = (
    "https://qt.gtimg.cn/q=whUSDCNH,sh000300,hkHSTECH,usIXIC,usNVDA,s_sh518880,s_sh512480,s_sz159980"
)
_SINA_FUTURES_TAPE_URL = "https://hq.sinajs.cn/list=AU0,CU0,SC0"

_MAX_INDEX_BYTES = 2 * 1024 * 1024
_MAX_STORED_EVENTS = 450
_CLUSTER_WINDOW_SECONDS = 6 * 3600

# ── Bilingual deterministic anti-noise & tier classification dictionaries ──
_NOISE_RE = re.compile(
    r"午评|收评|早评|盘中异动|直线拉升|快速跳水|涨停复盘|龙虎榜|机构点评|研报称|券商称|"
    r"投资者互动|互动平台|董秘回复|每日资金|主力资金净|收盘播报|开盘播报|"
    r"新闻精选|晚间新闻|早间必读|盘前必读|投资避雷针|每日复盘|一周大事|早报|晚报|新闻联播文字|日历|"
    r"\b(what to watch|stocks making the biggest moves|morning squawk|5 things to know|podcast|opinion:)\b",
    re.IGNORECASE,
)

_T0_AUTHORITY_RE = re.compile(
    r"国务院常务会议|国常会|中央政治局|政治局会议|中央经济工作会议|中央金融工作会议|"
    r"中国人民银行|央行|中国证监会|证监会|金融监管总局|国家金融监督管理总局|"
    r"财政部|国家发改委|商务部|国务院关税税则委员会|中央汇金|美联储|FOMC|"
    r"\b(Federal Reserve|FOMC|Jerome Powell|U\.S\. Treasury|Bureau of Industry and Security|"
    r"Department of Commerce|White House|USTR|OPEC\+|ECB|Bank of Japan)\b",
    re.IGNORECASE,
)

_T0_ACTION_RE = re.compile(
    r"平准基金|稳市机制|国家队|加力发挥|降准|降息|下调存款准备金|下调利率|印花税|"
    r"增量政策|逆周期调节|特别国债|专项债|增持ETF|增持A股|中长期资金入市|"
    r"出口管制|加征关税|关税反制|全面暂停|重大改革|资本市场高质量发展|降息\d+个基点|"
    r"\b(rate cut|rate hike|basis points|bps|interest rate decision|monetary policy|"
    r"tariff|export control|entity list|sanction|emergency liquidity|quantitative easing|"
    r"production cut|ceasefire)\b",
    re.IGNORECASE,
)

_T1_CATALYST_RE = re.compile(
    r"停产|不可抗力|涨价|提价|上调报价|业绩预增|净利润同比增长|大幅预增|扭亏为盈|"
    r"回购注销|大手笔回购|增持计划|重大合同|中标|反倾销|限产|减产|产能出清|"
    r"液冷|AI服务器|算力中心|光模块|固态电池|人形机器人|特高压|突破性进展|"
    r"\b(NVIDIA|NVDA|TSMC|Blackwell|Rubin|AI server|liquid cooling|optical transceiver|"
    r"HBM|CoWoS|guidance raised|beats estimates|record revenue|capex|data center|"
    r"force majeure|supply shortage|share buyback|Item 2\.02|Item 1\.01|Item 8\.01)\b",
    re.IGNORECASE,
)

_SEC_WATCH_COMPANY_RE = re.compile(
    r"\b(NVIDIA|MICROSOFT|APPLE|ALPHABET|GOOGLE|META PLATFORMS|AMAZON|BROADCOM|"
    r"ADVANCED MICRO DEVICES|AMD|SUPER MICRO|MICRON|INTEL|TESLA|ORACLE|TAIWAN SEMICONDUCTOR)\b",
    re.IGNORECASE,
)

_THEME_RULES: tuple[
    tuple[str, re.Pattern[str], tuple[str, ...], tuple[str, ...], list[dict[str, str]]], ...
] = (
    (
        "宏观稳市与宽基权重",
        re.compile(
            r"平准基金|国家队|中央汇金|稳市机制|资本市场|中长期资金|印花税|降准|降息|证监会|"
            r"\b(Federal Reserve|FOMC|rate cut|rate hike|Treasury|PBOC|stimulus)\b",
            re.IGNORECASE,
        ),
        ("AI算力与服务器", "白酒", "半导体"),
        ("AU", "CU"),
        [
            {"ticker": "510300", "name": "沪深300ETF", "role": "宽基平准先手锚"},
            {"ticker": "600030", "name": "中信证券", "role": "资本市场改革弹性标杆"},
            {"ticker": "601138", "name": "工业富联", "role": "大市值科技核心容量票"},
        ],
    ),
    (
        "AI算力与科技硬件",
        re.compile(
            r"算力|AI服务器|液冷|光模块|数据中心|英伟达|半导体|芯片|光刻|国产替代|大基金|"
            r"\b(NVIDIA|NVDA|TSMC|Blackwell|Rubin|AI server|data center|capex|semiconductor|chip|"
            r"export control|entity list|HBM|BROADCOM|AMD|MICROSOFT|META)\b",
            re.IGNORECASE,
        ),
        ("AI算力与服务器", "半导体"),
        ("CU", "SN"),
        [
            {"ticker": "601138", "name": "工业富联", "role": "AI服务器整机与液冷龙头"},
            {"ticker": "300308", "name": "中际旭创", "role": "高速光模块全球核心龙头"},
            {"ticker": "002463", "name": "沪电股份", "role": "AI高层高速PCB核心供应商"},
            {"ticker": "688012", "name": "中微公司", "role": "刻蚀设备国产替代龙头"},
        ],
    ),
    (
        "电网出海与算力电力",
        re.compile(
            r"特高压|电网|变压器|绿电|核电|储能|电力市场|虚拟电厂|"
            r"\b(power grid|transformer|nuclear power|data center power)\b",
            re.IGNORECASE,
        ),
        ("电网设备与算力电力", "光伏与储能"),
        ("CU", "AL"),
        [
            {"ticker": "600406", "name": "国电南瑞", "role": "电网自动化与特高压龙头"},
            {"ticker": "002028", "name": "思源电气", "role": "输变电设备出海标杆"},
            {"ticker": "300750", "name": "宁德时代", "role": "全球动力与储能电池龙头"},
        ],
    ),
    (
        "人形机器人与智造",
        re.compile(
            r"人形机器人|具身智能|丝杠|减速器|灵巧手|低空经济|自动驾驶|"
            r"\b(humanoid robot|Optimus|autonomous driving|robotaxi|robotics)\b",
            re.IGNORECASE,
        ),
        ("人形机器人与精密制造", "新能源汽车"),
        ("CU", "AL"),
        [
            {"ticker": "601689", "name": "拓普集团", "role": "执行器总成平台龙头"},
            {"ticker": "603728", "name": "鸣志电器", "role": "空心杯电机核心供应商"},
            {"ticker": "002594", "name": "比亚迪", "role": "智驾与整车制造龙头"},
        ],
    ),
    (
        "资源品、关税与大宗涨价",
        re.compile(
            r"黄金|铜|原油|停产|不可抗力|限产|减产|OPEC|涨价|提价|战略矿产|稀土|煤炭|关税|"
            r"\b(gold|copper|crude oil|OPEC|tariff|trade war|mine|smelter|force majeure|sanction)\b",
            re.IGNORECASE,
        ),
        ("铜金有色", "煤炭电力"),
        ("AU", "CU", "SC"),
        [
            {"ticker": "601899", "name": "紫金矿业", "role": "铜金矿产全球龙头"},
            {"ticker": "600547", "name": "山东黄金", "role": "纯正黄金弹性标的"},
            {"ticker": "601088", "name": "中国神华", "role": "高股息能源压舱石"},
        ],
    ),
)

_TICKER_IN_TEXT_RE = re.compile(r"(?<!\d)(60\d{4}|00\d{4}|30\d{4}|688\d{3}|51\d{4}|159\d{3})(?!\d)")
_BARK_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{12,64}$")
_NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{4,80}$")
_PUSHPLUS_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")
_SERVERCHAN_KEY_RE = re.compile(r"^SCT[A-Za-z0-9]{8,64}$")
_TELEGRAM_SECRET_RE = re.compile(r"^(\d{6,16}:[A-Za-z0-9_-]{20,64})@(-?\d{5,20})$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{12,180}$")
_GEMINI_KEY_RE = re.compile(r"^[A-Za-z0-9_.:+/=-]{16,256}$")
_INVISIBLE_UNICODE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\xa0]")
_EMBEDDED_GOOGLE_KEY_RE = re.compile(r"(AIza[A-Za-z0-9_.-]{25,120}|AQ\.[A-Za-z0-9_.:+/=-]{20,200})")
_HEADER_OR_ENV_KEY_RE = re.compile(
    r"(?:x-goog-api-key|api[_-]?key|Authorization)\s*[:=]\s*(?:Bearer\s+)?[\"'`]?([A-Za-z0-9_.:+/=-]{16,256})[\"'`]?",
    re.IGNORECASE,
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

PUSH_PROVIDER_CATALOG: tuple[dict[str, object], ...] = (
    {
        "id": "ntfy",
        "label": "ntfy · 全端开源锁屏 (安卓/iOS/桌面)",
        "os": "Android / iOS / Web / 桌面",
        "secret_label": "ntfy 专属订阅主题名 (Topic)",
        "placeholder": "如 prism_ops_你的随机串 或 https://ntfy.sh/...",
        "guide": "免注册零费用：Android / iOS / 桌面安装开源 ntfy 客户端，订阅同一专属随机主题名即可秒收高优锁屏提醒。",
        "mode_label": "全端唤醒优先级",
        "modes": [
            ["high", "high · 高优直达锁屏 (P4 推荐)"],
            ["urgent", "urgent · 最高强唤醒响铃 (P5)"],
            ["default", "default · 标准通知 (P3)"],
        ],
    },
    {
        "id": "pushplus",
        "label": "PushPlus · 个人微信直推 (免装App)",
        "os": "个人微信 (Android / 鸿蒙 / iOS)",
        "secret_label": "PushPlus 微信 Token",
        "placeholder": "粘贴 pushplus.plus 的 32 位个人 Token",
        "guide": "免装任何额外 App：用微信扫码登录 pushplus.plus 复制一对一 Token，风向战报直接推送至个人微信聊天窗。",
        "mode_label": "微信消息排版",
        "modes": [
            ["markdown", "Markdown 战报卡片 (推荐)"],
            ["compact", "精简纯文本摘要"],
        ],
    },
    {
        "id": "serverchan",
        "label": "Server酱 · 微信服务号 / 安卓直推",
        "os": "个人微信 / Android / iOS",
        "secret_label": "Server酱 SendKey",
        "placeholder": "粘贴 SCT 开头的 SendKey",
        "guide": "从 sct.ftqq.com 微信扫码获取 SCT 开头的 SendKey，支持微信服务号与 Server酱 App 双通道强提醒。",
        "mode_label": "消息排版模式",
        "modes": [
            ["markdown", "Markdown 详情战报 (推荐)"],
            ["compact", "精简摘要"],
        ],
    },
    {
        "id": "bark",
        "label": "Bark · 苹果原生锁屏 (iOS/macOS)",
        "os": "iOS / iPadOS / macOS",
        "secret_label": "Bark Device Key",
        "placeholder": "粘贴 Bark 设备 Key 或 https://api.day.app/...",
        "guide": "苹果 APNs 原生通道：支持 timeSensitive 穿破专注模式直达锁屏与 critical 紧急响铃。",
        "mode_label": "iOS / macOS 锁屏级别",
        "modes": [
            ["timeSensitive", "timeSensitive · 穿破专注锁屏"],
            ["critical", "critical · 紧急静音响铃"],
            ["active", "active · 标准亮屏通知"],
        ],
    },
    {
        "id": "feishu",
        "label": "飞书机器人 · 全端同步 (安卓/鸿蒙/iOS)",
        "os": "飞书全平台客户端",
        "secret_label": "飞书机器人 Webhook / Key",
        "placeholder": "粘贴 https://open.feishu.cn/open-apis/bot/v2/hook/...",
        "guide": "在飞书个人/投研群添加「自定义机器人」，复制完整官方 HTTPS Webhook 地址即可全端同步接收。",
        "mode_label": "群卡片格式",
        "modes": [
            ["markdown", "结构化全文推送 (推荐)"],
            ["compact", "精简纯文本"],
        ],
    },
    {
        "id": "wecom",
        "label": "企业微信机器人 · 可联动个人微信",
        "os": "企业微信 / 个人微信插件",
        "secret_label": "企微机器人 Webhook / Key",
        "placeholder": "粘贴 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...",
        "guide": "在企业微信群添加群机器人复制 Webhook；启用企微「微信插件」后可在个人微信直接收到提醒。",
        "mode_label": "消息排版格式",
        "modes": [
            ["markdown", "Markdown 高亮战报 (推荐)"],
            ["compact", "精简纯文本"],
        ],
    },
    {
        "id": "dingtalk",
        "label": "钉钉机器人 · 全端同步 (安卓/鸿蒙/iOS)",
        "os": "钉钉全平台客户端",
        "secret_label": "钉钉机器人 Webhook / Token",
        "placeholder": "粘贴 https://oapi.dingtalk.com/robot/send?access_token=...",
        "guide": "在钉钉群添加自定义机器人（安全关键词建议设「Prism」或「风向」），复制官方 Webhook 地址粘贴即可。",
        "mode_label": "消息排版格式",
        "modes": [
            ["markdown", "Markdown 战报卡片 (推荐)"],
            ["compact", "精简摘要"],
        ],
    },
    {
        "id": "telegram",
        "label": "Telegram Bot · 全端秒推 (安卓/iOS/桌面)",
        "os": "Telegram 全平台客户端",
        "secret_label": "BotToken@ChatID",
        "placeholder": "格式：123456789:AAH...@987654321",
        "guide": "向 @BotFather 获取 Bot Token 后拼接 @ 你的个人或频道 ChatID（例如 123456:AAH...@-100123456789）。",
        "mode_label": "消息排版格式",
        "modes": [
            ["markdown", "完整剧本详情 (推荐)"],
            ["compact", "精简快讯"],
        ],
    },
)

_SUPPORTED_PROVIDERS = {str(p["id"]) for p in PUSH_PROVIDER_CATALOG}
_BARK_LEVELS = {"timeSensitive", "critical", "active"}
_DELIVERY_MODES = {
    "timeSensitive",
    "critical",
    "active",
    "urgent",
    "high",
    "default",
    "markdown",
    "compact",
}
_MIN_TIERS = {"T0", "T1"}
_GEMINI_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
)
_GEMINI_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{3,80}$")
_AUTO_TIERS = {"T0_ONLY", "T0_WATCH", "T0_T1"}
SYSTEM_OWNER = "__system__"
FREE_MONTHLY_SEARCH_LIMIT = 5000

_last_poll_meta: dict[str, object] = {
    "last_poll_at": None,
    "last_poll_ok": False,
    "events_ingested": 0,
    "sources_ok": [],
    "interval_seconds": 30,
}
_last_manual_refresh_at: float = 0.0
_tape_cache: dict[str, object] = {"ts": 0.0, "items": []}


def init_event_radar_db() -> None:
    """Create Event Radar, Push, Gemini AI config, Daily Quota & Briefing tables idempotently."""
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS radar_events (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                tier TEXT NOT NULL,
                score INTEGER NOT NULL,
                theme TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                created_at REAL NOT NULL,
                cluster_key TEXT NOT NULL,
                echo_count INTEGER NOT NULL DEFAULT 1,
                window_tag TEXT NOT NULL DEFAULT '',
                playbook_json TEXT NOT NULL DEFAULT '{}',
                tickers_json TEXT NOT NULL DEFAULT '[]',
                futures_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_radar_events_created
                ON radar_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_radar_events_cluster
                ON radar_events(cluster_key, created_at DESC);

            CREATE TABLE IF NOT EXISTS user_radar_channels (
                username TEXT PRIMARY KEY,
                provider TEXT NOT NULL DEFAULT 'bark',
                endpoint_secret TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                min_tier TEXT NOT NULL DEFAULT 'T0',
                bark_level TEXT NOT NULL DEFAULT 'timeSensitive',
                notify_watchlist INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                last_sent_at REAL,
                last_status TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS radar_deliveries (
                event_id TEXT NOT NULL,
                username TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at REAL NOT NULL,
                PRIMARY KEY (event_id, username)
            );

            CREATE TABLE IF NOT EXISTS user_radar_ai_config (
                username TEXT PRIMARY KEY,
                api_key TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'gemini-2.5-flash',
                enabled INTEGER NOT NULL DEFAULT 1,
                use_google_search INTEGER NOT NULL DEFAULT 1,
                auto_enrich INTEGER NOT NULL DEFAULT 1,
                auto_tier TEXT NOT NULL DEFAULT 'T0_WATCH',
                auto_briefing INTEGER NOT NULL DEFAULT 1,
                daily_limit INTEGER NOT NULL DEFAULT 20,
                updated_at REAL NOT NULL,
                last_status TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS radar_ai_daily_usage (
                day TEXT NOT NULL,
                username TEXT NOT NULL,
                calls_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, username)
            );

            CREATE TABLE IF NOT EXISTS radar_briefings (
                id TEXT PRIMARY KEY,
                slot TEXT NOT NULL,
                title TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS radar_ai_call_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                month TEXT NOT NULL,
                username TEXT NOT NULL,
                model TEXT NOT NULL,
                call_kind TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                search_queries INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_radar_ai_ledger_month
                ON radar_ai_call_ledger(month, username);

            CREATE TABLE IF NOT EXISTS radar_discovered_models (
                model_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                input_limit INTEGER NOT NULL DEFAULT 0,
                output_limit INTEGER NOT NULL DEFAULT 0,
                input_usd_per_1m REAL NOT NULL DEFAULT 0.30,
                output_usd_per_1m REAL NOT NULL DEFAULT 2.50,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS radar_push_endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL DEFAULT '__system__',
                label TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL,
                endpoint_secret TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                min_tier TEXT NOT NULL DEFAULT 'T0',
                bark_level TEXT NOT NULL DEFAULT 'timeSensitive',
                notify_watchlist INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL,
                last_sent_at REAL,
                last_status TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_radar_push_endpoints_owner
                ON radar_push_endpoints(owner, id);

            CREATE TABLE IF NOT EXISTS radar_endpoint_deliveries (
                event_id TEXT NOT NULL,
                endpoint_key TEXT NOT NULL,
                status TEXT NOT NULL,
                sent_at REAL NOT NULL,
                PRIMARY KEY (event_id, endpoint_key)
            );
            """
        )
        # Idempotent schema migrations for Ultra credit tracking columns
        existing_cols = {str(r[1]) for r in db.execute("PRAGMA table_info(user_radar_ai_config)").fetchall()}
        for col_name, col_ddl in (
            ("monthly_credit_usd", "REAL NOT NULL DEFAULT 40.0"),
            ("prior_spend_usd", "REAL NOT NULL DEFAULT 0.0"),
            ("monthly_cap_usd", "REAL NOT NULL DEFAULT 38.0"),
        ):
            if col_name not in existing_cols:
                db.execute(f"ALTER TABLE user_radar_ai_config ADD COLUMN {col_name} {col_ddl}")


def detect_trading_window(now: datetime | None = None) -> dict[str, object]:
    """Identify the current A-share trading window in Asia/Shanghai time."""
    dt = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    weekday = dt.weekday()
    hhmm = dt.hour * 60 + dt.minute

    if weekday >= 5:
        return {
            "tag": "周末全球发酵窗",
            "code": "weekend",
            "cadence_seconds": 90,
            "action_hint": "周末海内外重磅政策发酵期：结合周一早盘富时A50与汇率开盘定价，重点盯周一 09:15–09:25 集合竞价封单与承接。",
        }
    if 8 * 60 + 30 <= hhmm < 9 * 60 + 30:
        mins = 9 * 60 + 30 - hhmm
        return {
            "tag": f"盘前竞价窗（距开盘 {mins}m）",
            "code": "pre_open",
            "cadence_seconds": 15,
            "action_hint": "盯 09:15–09:25 集合竞价量能与高开幅度：核心龙头温和放量高开 2%~5% 优先观察承接，高开 >7% 谨防兑现。",
        }
    if 9 * 60 + 30 <= hhmm < 11 * 60 + 30:
        return {
            "tag": "早盘连续交易",
            "code": "morning",
            "cadence_seconds": 25,
            "action_hint": "盘中突发脉冲：先看前排宽基/龙头放量拉升后的首次分时回踩均价线承接，不追第一分钟无量跟风股。",
        }
    if 11 * 60 + 30 <= hhmm < 13 * 60:
        mins = 13 * 60 - hhmm
        return {
            "tag": f"午休黄金窗（距13:00开盘 {mins}m）",
            "code": "midday_break",
            "cadence_seconds": 15,
            "action_hint": (
                f"距 13:00 下午开盘还有 {mins} 分钟决策窗：优先核对离岸人民币与港股/A50午间脉冲，"
                "盯 13:00–13:10 核心容量龙头放量承接确认，缩量冲高回落则放弃追高。"
            ),
        }
    if 13 * 60 <= hhmm < 15 * 60:
        return {
            "tag": "午盘连续交易",
            "code": "afternoon",
            "cadence_seconds": 25,
            "action_hint": "午后交易时段：核对指数与核心标的量价共振，分时回踩均价线不破再考虑分批介入，尾盘偷袭需防次日分歧。",
        }
    if 15 * 60 <= hhmm < 21 * 60 + 30:
        return {
            "tag": "盘后与欧盘政策窗",
            "code": "after_hours",
            "cadence_seconds": 30,
            "action_hint": "盘后政策与欧美盘前披露窗：结合离岸人民币与国内商品夜盘（21:00开盘）反应，提前做好次日竞价预案。",
        }
    return {
        "tag": "美股与夜盘联动窗",
        "code": "overnight",
        "cadence_seconds": 60,
        "action_hint": "美股与美联储/SEC披露活跃时段：盯英伟达/纳指与黄金铜油夜盘定价幅度，评估次日 A 股映射溢价与高开兑现风险。",
    }


# ── Live Global Cross-Asset Tape Confirmation (0-Token Anti-Rumor Engine) ──


def fetch_cross_market_tape(*, force: bool = False) -> list[dict[str, object]]:
    """Fetch sub-150ms live cross-market benchmarks via public_network with 20s TTL cache."""
    now = time.monotonic()
    cached = _tape_cache.get("items")
    if not force and isinstance(cached, list) and cached and now - float(_tape_cache.get("ts") or 0.0) < 20.0:
        return cached

    label_map = {
        "whUSDCNH": ("离岸人民币", "FX"),
        "sh000300": ("沪深300", "CN"),
        "hkHSTECH": ("恒生科指", "HK"),
        "usIXIC": ("纳斯达克", "US"),
        "usNVDA": ("英伟达", "AI"),
    }
    items: list[dict[str, object]] = []
    try:
        with public_network.sync_client(timeout=3.5) as client:
            resp = client.get(_TENCENT_TAPE_URL, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                text = resp.content[:65536].decode("gbk", errors="replace")
                for line in text.splitlines():
                    if "=" not in line or "~" not in line:
                        continue
                    lhs, rhs = line.split("=", 1)
                    code = lhs.strip().removeprefix("v_")
                    if code not in label_map:
                        continue
                    parts = rhs.strip().strip('";').split("~")
                    if len(parts) < 5:
                        continue
                    try:
                        price = float(parts[3])
                        prev = float(parts[4])
                        if price <= 0 or prev <= 0:
                            continue
                        chg_pct = round((price - prev) / prev * 100.0, 2)
                        name, group = label_map[code]
                        items.append(
                            {
                                "symbol": code,
                                "name": name,
                                "group": group,
                                "price": round(price, 4 if code == "whUSDCNH" else 2),
                                "change_pct": chg_pct,
                            }
                        )
                    except (ValueError, ZeroDivisionError):
                        continue
    except Exception:
        pass

    futures_map = {
        "AU0": ("沪金主力", "CMD"),
        "CU0": ("沪铜主力", "CMD"),
        "SC0": ("原油主力", "CMD"),
    }
    try:
        with public_network.sync_client(timeout=3.5) as client:
            resp = client.get(
                _SINA_FUTURES_TAPE_URL,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"},
            )
            if resp.status_code == 200:
                text = resp.content[:32768].decode("gbk", errors="replace")
                for line in text.splitlines():
                    if "=" not in line or "," not in line:
                        continue
                    lhs, rhs = line.split("=", 1)
                    sym = lhs.strip().rsplit("_", 1)[-1]
                    if sym not in futures_map:
                        continue
                    parts = rhs.strip().strip('";').split(",")
                    if len(parts) < 11:
                        continue
                    try:
                        last_p = float(parts[8])
                        settle_prev = float(parts[10]) or float(parts[5])
                        if last_p <= 0 or settle_prev <= 0:
                            continue
                        chg_pct = round((last_p - settle_prev) / settle_prev * 100.0, 2)
                        name, group = futures_map[sym]
                        items.append(
                            {
                                "symbol": sym,
                                "name": name,
                                "group": group,
                                "price": round(last_p, 2),
                                "change_pct": chg_pct,
                            }
                        )
                    except (ValueError, ZeroDivisionError):
                        continue
    except Exception:
        pass

    if items:
        _tape_cache["ts"] = now
        _tape_cache["items"] = items
    return items if items else (cached if isinstance(cached, list) else [])


def build_tape_verdict(theme: str, tape: list[dict[str, object]]) -> str:
    """Summarize whether live cross-asset prices confirm the catalyst theme."""
    if not tape:
        return ""
    by_sym = {str(it["symbol"]): it for it in tape}
    picks: list[dict[str, object]] = []
    if "AI" in theme or "科技" in theme or "机器人" in theme:
        for k in ("usNVDA", "usIXIC", "hkHSTECH", "CU0"):
            if k in by_sym:
                picks.append(by_sym[k])
    elif "资源" in theme or "大宗" in theme:
        for k in ("AU0", "CU0", "SC0", "whUSDCNH"):
            if k in by_sym:
                picks.append(by_sym[k])
    else:
        for k in ("whUSDCNH", "sh000300", "hkHSTECH", "AU0"):
            if k in by_sym:
                picks.append(by_sym[k])
    if not picks:
        picks = tape[:3]

    strong = any(abs(float(p["change_pct"])) >= (0.25 if p["symbol"] == "whUSDCNH" else 0.75) for p in picks)
    snap = " / ".join(
        f"{p['name']} {p['price']} ({'+' if float(p['change_pct']) >= 0 else ''}{p['change_pct']}%)"
        for p in picks[:3]
    )
    prefix = "✅ 跨市场活跃印证：" if strong else "⚖️ 跨市场实时锚："
    return f"{prefix}{snap}"


def _extract_delta_points(title: str, body: str) -> list[str]:
    text = f"{title}。{body}".strip()
    sentences = [s.strip("【】[] ") for s in re.split(r"[。；！\n]+|\.\s+", text) if len(s.strip()) >= 8]
    scored: list[tuple[int, str]] = []
    for s in sentences[:12]:
        pts = 0
        if _T0_ACTION_RE.search(s):
            pts += 3
        if _T0_AUTHORITY_RE.search(s):
            pts += 2
        if _T1_CATALYST_RE.search(s):
            pts += 2
        if pts > 0:
            scored.append((pts, s[:140]))
    scored.sort(key=lambda item: item[0], reverse=True)
    picked: list[str] = []
    for _, line in scored:
        if line not in picked:
            picked.append(line)
        if len(picked) >= 3:
            break
    if not picked and title:
        picked.append(title[:140])
    return picked


def classify_event(
    title: str, body: str, *, cls_level: str | None = None, region: str = "CN"
) -> dict[str, object]:
    """Deterministic zero-LLM bilingual event scoring & theme classification."""
    full = f"{title} {body}".strip()
    is_noise = bool(_NOISE_RE.search(title))
    has_authority = bool(_T0_AUTHORITY_RE.search(full))
    has_t0_action = bool(_T0_ACTION_RE.search(full))
    has_t1_catalyst = bool(_T1_CATALYST_RE.search(full))
    is_cls_red = str(cls_level or "").upper() in {"A", "B"}

    score = 25
    if is_cls_red:
        score += 18
    if has_authority:
        score += 34
    if has_t0_action:
        score += 36
    if has_t1_catalyst:
        score += 28
    if region == "GLOBAL" and (has_authority or has_t1_catalyst):
        score += 6
    if is_noise:
        score -= 45

    matched_theme = "全球宏观与市场风向" if region == "GLOBAL" else "综合市场风向"
    matched_categories: list[str] = []
    matched_futures: list[str] = []
    seed_targets: list[dict[str, str]] = []
    for theme_name, pattern, cats, futs, targets in _THEME_RULES:
        if pattern.search(full):
            matched_theme = theme_name
            matched_categories = list(cats)
            matched_futures = list(futs)
            seed_targets = list(targets)
            score += 8
            break

    explicit_tickers = list(dict.fromkeys(_TICKER_IN_TEXT_RE.findall(full)))[:6]
    score = max(0, min(99, score))

    if not is_noise and has_authority and has_t0_action and score >= 82:
        tier = "T0"
    elif not is_noise and (score >= 58 or (is_cls_red and has_t1_catalyst)):
        tier = "T1"
    else:
        tier = "T2"

    return {
        "tier": tier,
        "score": score,
        "region": region,
        "theme": matched_theme,
        "categories": matched_categories,
        "futures": matched_futures,
        "seed_targets": seed_targets,
        "explicit_tickers": explicit_tickers,
        "delta_points": _extract_delta_points(title, body),
    }


def _cluster_key(title: str, theme: str, tier: str) -> str:
    """Stable topic fingerprint for 6-hour echo deduplication across CN & EN wires."""
    tokens = sorted(
        set(
            [m.lower() for m in _T0_AUTHORITY_RE.findall(title) if isinstance(m, str) and m]
            + [m.lower() for m in _T0_ACTION_RE.findall(title) if isinstance(m, str) and m]
            + [m.lower() for m in _T1_CATALYST_RE.findall(title) if isinstance(m, str) and m]
            + _TICKER_IN_TEXT_RE.findall(title)
        )
    )
    if tokens:
        raw = f"{tier}|{theme}|{'|'.join(tokens[:6])}"
    else:
        cleaned = re.sub(r"[\W_]+", "", title.lower())[:28]
        raw = f"{tier}|{theme}|{cleaned}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _lookup_cached_quote_and_fund(ticker: str) -> dict[str, object]:
    """Non-blocking read from MarketCache for instant playbook grounding."""
    info: dict[str, object] = {}
    try:
        from .market.factory import get_chain

        chain = get_chain()
        for p in chain.providers:
            qval, _ = chain.cache.get_stale(chain._cache_key("quote", p.name, ticker))
            if isinstance(qval, dict) and qval.get("price") is not None:
                info["price"] = qval.get("price")
                info["change_pct"] = qval.get("change_pct")
                break
        for p in chain.providers:
            fval, _ = chain.cache.get_stale(chain._cache_key("fundamentals", p.name, ticker))
            if isinstance(fval, dict) and fval.get("pe_ttm") is not None:
                info["pe_ttm"] = fval.get("pe_ttm")
                break
    except Exception:
        pass
    return info


def build_playbook(
    title: str,
    body: str,
    classified: dict[str, object],
    *,
    now: datetime | None = None,
    tape: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Construct a structured, window-aware, cross-market-confirmed A-share & futures playbook."""
    win = detect_trading_window(now)
    seed_targets = list(classified.get("seed_targets") or [])
    explicit = list(classified.get("explicit_tickers") or [])

    targets: list[dict[str, object]] = []
    seen: set[str] = set()
    for t in seed_targets:
        tk = str(t["ticker"])
        if tk in seen:
            continue
        seen.add(tk)
        snap = _lookup_cached_quote_and_fund(tk)
        targets.append(
            {
                "ticker": tk,
                "name": t["name"],
                "role": t["role"],
                "price": snap.get("price"),
                "change_pct": snap.get("change_pct"),
                "pe_ttm": snap.get("pe_ttm"),
            }
        )

    if explicit:
        try:
            from .industry_seed import SEED_STOCK_TO_INDUSTRIES

            seed_names = {k: v[0][1] for k, v in SEED_STOCK_TO_INDUSTRIES.items() if v}
        except Exception:
            seed_names = {}
        for tk in explicit:
            if tk in seen:
                continue
            seen.add(tk)
            snap = _lookup_cached_quote_and_fund(tk)
            targets.insert(
                0,
                {
                    "ticker": tk,
                    "name": seed_names.get(tk, f"标的 {tk}"),
                    "role": "电报正文直接点名标的",
                    "price": snap.get("price"),
                    "change_pct": snap.get("change_pct"),
                    "pe_ttm": snap.get("pe_ttm"),
                },
            )

    tier = str(classified.get("tier") or "T2")
    region = str(classified.get("region") or "CN")
    theme = str(classified.get("theme") or "")
    if tier == "T0":
        invalidation = "若开盘后 15 分钟宽基/核心容量龙头高开低走跌破开盘首5分钟低点、且离岸人民币与A50同步回吐，说明市场借利好兑现，立即停止追涨。"
    elif tier == "T1":
        invalidation = (
            "若催化标的已提前大幅高开(>6%)或跨市场锚未共振确认，等待回踩分时均线验证承接，严防情绪接盘。"
        )
    else:
        invalidation = "常规快讯仅作产业线索观察，不单独构成盘中追涨依据。"

    tape_verdict = build_tape_verdict(theme, tape or []) if tier in {"T0", "T1"} else ""

    return {
        "region": region,
        "window_tag": win["tag"],
        "window_code": win["code"],
        "delta_points": classified.get("delta_points") or [title[:120]],
        "targets": targets[:5],
        "futures": list(classified.get("futures") or []),
        "tape_verdict": tape_verdict,
        "execution_rule": win["action_hint"],
        "invalidation_rule": invalidation,
    }


# ── Multi-Region Public Feed Fetchers (CN + Global Primary RSS/Atom) ──


def _fetch_bytes_public(
    url: str, *, timeout: float = 5.5, headers: dict[str, str] | None = None
) -> bytes | None:
    req_headers = {"User-Agent": "Mozilla/5.0 (compatible; PrismResearch/1.0)"}
    if headers:
        req_headers.update(headers)
    try:
        with (
            public_network.sync_client(timeout=timeout) as client,
            client.stream("GET", url, headers=req_headers) as resp,
        ):
            if resp.status_code != 200:
                return None
            parts, total = [], 0
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_INDEX_BYTES:
                    return None
                parts.append(chunk)
            return b"".join(parts)
    except Exception:
        return None


def _fetch_json_public(url: str, *, timeout: float = 5.5) -> dict | None:
    raw = _fetch_bytes_public(url, timeout=timeout, headers={"Accept": "application/json"})
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None


def _clean_html_text(text: str) -> str:
    if not text:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", html.unescape(text))
    return re.sub(r"\s+", " ", stripped).strip()


def _parse_pubdate(raw_date: str) -> tuple[str, float]:
    now_dt = datetime.now(SHANGHAI)
    s = (raw_date or "").strip()
    if not s:
        return now_dt.isoformat(timespec="seconds"), now_dt.timestamp()
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        sh = dt.astimezone(SHANGHAI)
        return sh.isoformat(timespec="seconds"), sh.timestamp()
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        sh = dt.astimezone(SHANGHAI)
        return sh.isoformat(timespec="seconds"), sh.timestamp()
    except Exception:
        return now_dt.isoformat(timespec="seconds"), now_dt.timestamp()


def _safe_parse_xml(raw_xml: bytes) -> ET.Element | None:
    if not raw_xml or b"<!ENTITY" in raw_xml[:4096].upper():
        return None
    try:
        return ET.fromstring(raw_xml)
    except Exception:
        return None


def _parse_cls_rows(payload: dict) -> list[dict[str, object]]:
    if not isinstance(payload, dict) or payload.get("errno") != 0:
        return []
    rows = (payload.get("data") or {}).get("roll_data")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for row in rows[:35]:
        if not isinstance(row, dict):
            continue
        eid = row.get("id")
        ctime = row.get("ctime")
        if type(eid) is not int or type(ctime) not in {int, str}:
            continue
        try:
            ts = int(ctime)
            pub = datetime.fromtimestamp(ts, UTC).astimezone(SHANGHAI).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            continue
        title = str(row.get("title") or "").strip()
        body = str(row.get("brief") or row.get("content") or "").strip()
        if not title:
            title = body[:90]
        if not title:
            continue
        out.append(
            {
                "id": f"cls-{eid}",
                "source": "财联社电报",
                "region": "CN",
                "title": title[:220],
                "body": body[:1200],
                "url": f"https://www.cls.cn/detail/{eid}",
                "published_at": pub,
                "created_at": float(ts),
                "cls_level": str(row.get("level") or ""),
            }
        )
    return out


def _parse_sina_rows(payload: dict) -> list[dict[str, object]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for row in rows[:22]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        url = str(row.get("url") or "").strip()
        ctime = row.get("ctime")
        if not title or not url.startswith("https://") or type(ctime) not in {int, str}:
            continue
        try:
            ts = int(ctime)
            pub = datetime.fromtimestamp(ts, UTC).astimezone(SHANGHAI).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            continue
        body = str(row.get("summary") or row.get("intro") or title).strip()
        doc_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        out.append(
            {
                "id": f"sina-{doc_hash}",
                "source": "新浪快讯",
                "region": "CN",
                "title": title[:220],
                "body": body[:1200],
                "url": url,
                "published_at": pub,
                "created_at": float(ts),
                "cls_level": "",
            }
        )
    return out


def _parse_eastmoney_rows(payload: dict) -> list[dict[str, object]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("fastNewsList") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for row in rows[:22]:
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        title = str(row.get("title") or "").strip()
        summary = str(row.get("summary") or title).strip()
        show_time = str(row.get("showTime") or "").strip()
        if not code or not title:
            continue
        pub, ts = _parse_pubdate(show_time.replace(" ", "T") + "+08:00" if " " in show_time else show_time)
        out.append(
            {
                "id": f"em-{code[:20]}",
                "source": "东财全球7×24",
                "region": "CN",
                "title": title[:220],
                "body": summary[:1200],
                "url": f"https://finance.eastmoney.com/a/{code}.html" if code.isdigit() else "",
                "published_at": pub,
                "created_at": ts,
                "cls_level": "",
            }
        )
    return out


def _parse_rss_feed(
    raw_xml: bytes, *, source_label: str, prefix: str, limit: int = 15
) -> list[dict[str, object]]:
    root = _safe_parse_xml(raw_xml)
    if root is None:
        return []
    out: list[dict[str, object]] = []
    # Standard RSS 2.0 <item>
    for item in root.findall(".//item")[:limit]:
        title = _clean_html_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        desc = _clean_html_text(item.findtext("description") or title)
        pub_raw = item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or ""
        if not title:
            continue
        pub, ts = _parse_pubdate(pub_raw)
        uid = hashlib.sha256((link or title).encode("utf-8")).hexdigest()[:12]
        out.append(
            {
                "id": f"{prefix}-{uid}",
                "source": source_label,
                "region": "GLOBAL",
                "title": title[:220],
                "body": desc[:1200],
                "url": link if link.startswith("https://") else "",
                "published_at": pub,
                "created_at": ts,
                "cls_level": "A" if prefix == "fed" else "",
            }
        )
    return out


def _parse_sec_8k_atom(raw_xml: bytes, *, limit: int = 20) -> list[dict[str, object]]:
    root = _safe_parse_xml(raw_xml)
    if root is None:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out: list[dict[str, object]] = []
    for entry in root.findall("a:entry", ns)[:limit]:
        title = _clean_html_text(entry.findtext("a:title", default="", namespaces=ns))
        if not title or not _SEC_WATCH_COMPANY_RE.search(title):
            continue
        summary = _clean_html_text(entry.findtext("a:summary", default=title, namespaces=ns))
        updated = entry.findtext("a:updated", default="", namespaces=ns)
        link_el = entry.find("a:link", ns)
        href = link_el.attrib.get("href", "").strip() if link_el is not None else ""
        pub, ts = _parse_pubdate(updated)
        uid = hashlib.sha256((href or title).encode("utf-8")).hexdigest()[:12]
        out.append(
            {
                "id": f"sec8k-{uid}",
                "source": "SEC 8-K 官方披露",
                "region": "GLOBAL",
                "title": title[:220],
                "body": summary[:1200],
                "url": href if href.startswith("https://") else "",
                "published_at": pub,
                "created_at": ts,
                "cls_level": "B",
            }
        )
    return out


def ingest_raw_items(
    items: list[dict[str, object]],
    *,
    now: datetime | None = None,
    tape: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Classify, deduplicate by 6-hour topic cluster, persist, and return newly stored alerts."""
    init_event_radar_db()
    dt_now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    now_ts = dt_now.timestamp()
    live_tape = tape if tape is not None else fetch_cross_market_tape()
    newly_created: list[dict[str, object]] = []

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            for item in items:
                eid = str(item["id"])
                if db.execute("SELECT 1 FROM radar_events WHERE id=?", [eid]).fetchone():
                    continue
                title = str(item["title"])
                body = str(item.get("body") or title)
                region = str(item.get("region") or "CN")
                classified = classify_event(
                    title, body, cls_level=str(item.get("cls_level") or ""), region=region
                )
                ckey = _cluster_key(title, str(classified["theme"]), str(classified["tier"]))

                existing_cluster = db.execute(
                    "SELECT id, echo_count FROM radar_events "
                    "WHERE cluster_key=? AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                    [ckey, now_ts - _CLUSTER_WINDOW_SECONDS],
                ).fetchone()
                if existing_cluster:
                    db.execute(
                        "UPDATE radar_events SET echo_count=echo_count+1 WHERE id=?",
                        [existing_cluster["id"]],
                    )
                    continue

                playbook = build_playbook(title, body, classified, now=dt_now, tape=live_tape)
                tickers = [t["ticker"] for t in playbook["targets"]]
                futures = list(playbook["futures"])
                event_row = {
                    "id": eid,
                    "source": str(item.get("source") or "公开快讯"),
                    "region": region,
                    "tier": str(classified["tier"]),
                    "score": int(classified["score"]),
                    "theme": str(classified["theme"]),
                    "title": title,
                    "body": body,
                    "url": str(item.get("url") or ""),
                    "published_at": str(item.get("published_at") or dt_now.isoformat(timespec="seconds")),
                    "created_at": float(item.get("created_at") or now_ts),
                    "cluster_key": ckey,
                    "echo_count": 1,
                    "window_tag": str(playbook["window_tag"]),
                    "playbook": playbook,
                    "tickers": tickers,
                    "futures": futures,
                }
                db.execute(
                    """INSERT INTO radar_events
                       (id, source, tier, score, theme, title, body, url, published_at, created_at,
                        cluster_key, echo_count, window_tag, playbook_json, tickers_json, futures_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        event_row["id"],
                        event_row["source"],
                        event_row["tier"],
                        event_row["score"],
                        event_row["theme"],
                        event_row["title"],
                        event_row["body"],
                        event_row["url"],
                        event_row["published_at"],
                        event_row["created_at"],
                        event_row["cluster_key"],
                        1,
                        event_row["window_tag"],
                        json.dumps(playbook, ensure_ascii=False),
                        json.dumps(tickers, ensure_ascii=False),
                        json.dumps(futures, ensure_ascii=False),
                    ],
                )
                newly_created.append(event_row)

            db.execute(
                "DELETE FROM radar_events WHERE id NOT IN "
                "(SELECT id FROM radar_events ORDER BY created_at DESC LIMIT ?)",
                [_MAX_STORED_EVENTS],
            )
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    return newly_created


# ── Google Gemini Official API + Google Search Grounding + Hard Daily Circuit Breaker ──


def _today_shanghai() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d")


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 6:
        return "****"
    return f"{secret[:3]}****{secret[-4:]}"


def _gemini_rate_for_model(model: str) -> tuple[float, float]:
    """Return official (input_usd_per_1m, output_usd_per_1m) for a Gemini model ID."""
    m = model.lower()
    if "flash-lite" in m:
        return (0.30, 2.50)
    if "2.5-flash" in m or "2.0-flash" in m:
        return (0.30, 2.50)
    if "flash" in m:
        return (0.75, 3.75)
    if "3." in m and "pro" in m:
        return (2.00, 12.00)
    if "pro" in m:
        return (1.25, 10.00)
    return (0.50, 3.00)


def _compute_gemini_billing(username: str, row) -> dict[str, object]:
    month = datetime.now(SHANGHAI).strftime("%Y-%m")
    today = _today_shanghai()
    monthly_credit = (
        float(row["monthly_credit_usd"]) if row and row["monthly_credit_usd"] is not None else 40.0
    )
    prior_spend = float(row["prior_spend_usd"]) if row and row["prior_spend_usd"] is not None else 0.0
    monthly_cap = float(row["monthly_cap_usd"]) if row and row["monthly_cap_usd"] is not None else 38.0

    with get_db() as db:
        m_agg = db.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(prompt_tokens), 0) AS in_tok,
                      COALESCE(SUM(output_tokens), 0) AS out_tok,
                      COALESCE(SUM(search_queries), 0) AS searches,
                      COALESCE(SUM(cost_usd), 0.0) AS cost_usd
               FROM radar_ai_call_ledger WHERE month=? AND username=?""",
            [month, username],
        ).fetchone()
        d_agg = db.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS cost_usd FROM radar_ai_call_ledger WHERE day=? AND username=?",
            [today, username],
        ).fetchone()

    prism_month_cost = round(float(m_agg["cost_usd"] or 0.0), 6)
    prism_today_cost = round(float(d_agg["cost_usd"] or 0.0), 6)
    searches_used = int(m_agg["searches"] or 0)
    # Add $0.014/query ONLY if monthly searches exceed Google's 5,000 free monthly pool
    billable_searches = max(0, searches_used - FREE_MONTHLY_SEARCH_LIMIT)
    search_overage_usd = round(billable_searches * 0.014, 4)

    total_month_spend = round(prior_spend + prism_month_cost + search_overage_usd, 4)
    credit_used = round(min(monthly_credit, total_month_spend), 4)
    credit_remaining = round(max(0.0, monthly_credit - total_month_spend), 4)
    out_of_pocket = round(max(0.0, total_month_spend - monthly_credit), 4)

    return {
        "month": month,
        "monthly_credit_usd": round(monthly_credit, 2),
        "prior_spend_usd": round(prior_spend, 4),
        "monthly_cap_usd": round(monthly_cap, 2),
        "prism_today_cost_usd": prism_today_cost,
        "prism_month_calls": int(m_agg["calls"] or 0),
        "prism_month_input_tokens": int(m_agg["in_tok"] or 0),
        "prism_month_output_tokens": int(m_agg["out_tok"] or 0),
        "prism_month_searches": searches_used,
        "prism_month_cost_usd": prism_month_cost,
        "search_overage_usd": search_overage_usd,
        "total_month_spend_usd": total_month_spend,
        "credit_used_usd": credit_used,
        "credit_remaining_usd": credit_remaining,
        "out_of_pocket_usd": out_of_pocket,
        "free_searches_limit": FREE_MONTHLY_SEARCH_LIMIT,
        "free_searches_remaining": max(0, FREE_MONTHLY_SEARCH_LIMIT - searches_used),
    }


def _list_available_gemini_models() -> list[dict[str, object]]:
    with get_db() as db:
        rows = db.execute(
            "SELECT model_id, display_name, input_limit, output_limit, input_usd_per_1m, output_usd_per_1m "
            "FROM radar_discovered_models ORDER BY model_id"
        ).fetchall()
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for r in rows:
        mid = str(r["model_id"])
        seen.add(mid)
        out.append(
            {
                "id": mid,
                "display_name": r["display_name"],
                "input_limit": int(r["input_limit"]),
                "output_limit": int(r["output_limit"]),
                "input_usd_per_1m": float(r["input_usd_per_1m"]),
                "output_usd_per_1m": float(r["output_usd_per_1m"]),
                "discovered": True,
            }
        )
    for preset in _GEMINI_MODELS:
        if preset not in seen:
            in_r, out_r = _gemini_rate_for_model(preset)
            out.append(
                {
                    "id": preset,
                    "display_name": preset,
                    "input_limit": 1048576,
                    "output_limit": 65536,
                    "input_usd_per_1m": in_r,
                    "output_usd_per_1m": out_r,
                    "discovered": False,
                }
            )
    return out


def discover_gemini_models(username: str = SYSTEM_OWNER) -> list[dict[str, object]]:
    """Query Google's official GET /v1beta/models endpoint to auto-discover live Gemini models."""
    init_event_radar_db()
    with get_db() as db:
        row = db.execute("SELECT api_key FROM user_radar_ai_config WHERE username=?", [username]).fetchone()
    if not row or not row["api_key"]:
        raise ValueError("gemini_key_missing")

    headers = {"x-goog-api-key": row["api_key"], "Accept": "application/json"}
    try:
        with public_network.sync_client(timeout=10.0) as client:
            resp = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models?pageSize=100",
                headers=headers,
            )
            if resp.status_code != 200:
                raise ValueError(f"gemini_models_http_{resp.status_code}")
            payload = resp.json()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"gemini_models_error:{type(exc).__name__}") from None

    raw_models = payload.get("models") if isinstance(payload, dict) else []
    if not isinstance(raw_models, list):
        return _list_available_gemini_models()

    now = time.time()
    discovered: list[tuple] = []
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        methods = item.get("supportedGenerationMethods") or []
        if "generateContent" not in methods:
            continue
        raw_name = str(item.get("name") or "").removeprefix("models/").strip()
        if not raw_name.startswith("gemini-") or not _GEMINI_MODEL_ID_RE.fullmatch(raw_name):
            continue
        # Skip embedding/tts/image/computer-use & internal arena codenames
        if any(
            skip in raw_name.lower()
            for skip in (
                "embedding",
                "tts",
                "image",
                "veo",
                "audio",
                "vision",
                "computer-use",
                "robotics",
                "live",
                "transcribe",
                "banana",
                "neon",
                "argon",
                "carbon",
                "hydrogen",
                "customtools",
                "gemma",
                "learnlm",
                "aqa",
            )
        ):
            continue
        in_r, out_r = _gemini_rate_for_model(raw_name)
        discovered.append(
            (
                raw_name,
                str(item.get("displayName") or raw_name)[:100],
                int(item.get("inputTokenLimit") or 0),
                int(item.get("outputTokenLimit") or 0),
                in_r,
                out_r,
                now,
            )
        )

    if discovered:
        with get_db() as db:
            db.executemany(
                """INSERT OR REPLACE INTO radar_discovered_models
                   (model_id, display_name, input_limit, output_limit,
                    input_usd_per_1m, output_usd_per_1m, updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                discovered,
            )
    return _list_available_gemini_models()


def get_user_ai_config(username: str) -> dict[str, object]:
    init_event_radar_db()
    day = _today_shanghai()
    with get_db() as db:
        row = db.execute(
            "SELECT api_key, model, enabled, use_google_search, auto_enrich, auto_tier, "
            "auto_briefing, daily_limit, monthly_credit_usd, prior_spend_usd, monthly_cap_usd, "
            "last_status FROM user_radar_ai_config WHERE username=?",
            [username],
        ).fetchone()
        usage = db.execute(
            "SELECT calls_used FROM radar_ai_daily_usage WHERE day=? AND username=?",
            [day, username],
        ).fetchone()
    used_today = int(usage["calls_used"]) if usage else 0
    models_detail = _list_available_gemini_models()
    model_ids = [str(m["id"]) for m in models_detail]
    billing = _compute_gemini_billing(username, row)
    if not row:
        return {
            "has_key": False,
            "masked_key": "",
            "model": "gemini-3.5-flash-lite",
            "available_models": model_ids,
            "models_detail": models_detail,
            "enabled": True,
            "use_google_search": True,
            "auto_enrich": True,
            "auto_tier": "T0_WATCH",
            "auto_briefing": True,
            "daily_limit": 20,
            "monthly_credit_usd": 40.0,
            "prior_spend_usd": 0.0,
            "monthly_cap_usd": 38.0,
            "used_today": used_today,
            "billing": billing,
            "last_status": "",
        }
    cur_model = str(row["model"] or "gemini-2.5-flash")
    if cur_model not in model_ids:
        model_ids.insert(0, cur_model)
    return {
        "has_key": bool(row["api_key"]),
        "masked_key": _mask_secret(row["api_key"]),
        "model": cur_model,
        "available_models": model_ids,
        "models_detail": models_detail,
        "enabled": bool(row["enabled"]),
        "use_google_search": bool(row["use_google_search"]),
        "auto_enrich": bool(row["auto_enrich"]),
        "auto_tier": row["auto_tier"] if row["auto_tier"] in _AUTO_TIERS else "T0_WATCH",
        "auto_briefing": bool(row["auto_briefing"]),
        "daily_limit": int(row["daily_limit"]),
        "monthly_credit_usd": float(row["monthly_credit_usd"]),
        "prior_spend_usd": float(row["prior_spend_usd"]),
        "monthly_cap_usd": float(row["monthly_cap_usd"]),
        "used_today": used_today,
        "billing": billing,
        "last_status": row["last_status"] or "",
    }


def normalize_gemini_api_key(raw_value: str) -> str:
    """Clean and validate a Google AI Studio / Cloud API key (or extract from pasted curl/env snippet)."""
    cleaned = _INVISIBLE_UNICODE_RE.sub("", str(raw_value or "")).strip()
    if not cleaned:
        raise ValueError("invalid_gemini_api_key")

    # Detect truncated masked preview copied from AI Studio table (e.g. "...abcd" or "AIza...abcd")
    if "..." in cleaned or "…" in cleaned or "***" in cleaned or "•" in cleaned:
        raise ValueError("masked_gemini_api_key")

    # If user pasted a curl command, header line, or GEMINI_API_KEY=... assignment, extract the key
    m_google = _EMBEDDED_GOOGLE_KEY_RE.search(cleaned)
    if m_google and (" " in cleaned or "\n" in cleaned or "=" in cleaned or ":" in cleaned):
        candidate = m_google.group(1)
    else:
        m_hdr = _HEADER_OR_ENV_KEY_RE.search(cleaned)
        if m_hdr and (
            " " in cleaned or "\n" in cleaned or cleaned.upper().startswith(("GEMINI_", "GOOGLE_", "EXPORT "))
        ):
            candidate = m_hdr.group(1)
        else:
            candidate = cleaned.strip("\"'` \t\r\n")
            if candidate.lower().startswith("bearer "):
                candidate = candidate[7:].strip("\"'` ")

    # Collapse accidental line-wrap whitespace inside otherwise clean single-token paste
    if "\n" in candidate or "\r" in candidate:
        candidate = re.sub(r"[\r\n\t ]+", "", candidate)

    if "..." in candidate or "…" in candidate or "***" in candidate:
        raise ValueError("masked_gemini_api_key")
    if not _GEMINI_KEY_RE.fullmatch(candidate):
        raise ValueError("invalid_gemini_api_key")
    return candidate


def save_user_ai_config(
    username: str,
    *,
    api_key: str | None,
    model: str = "gemini-2.5-flash",
    enabled: bool = True,
    use_google_search: bool = True,
    auto_enrich: bool = True,
    auto_tier: str = "T0_WATCH",
    auto_briefing: bool = True,
    daily_limit: int = 20,
    monthly_credit_usd: float = 40.0,
    prior_spend_usd: float = 0.0,
    monthly_cap_usd: float = 38.0,
) -> dict[str, object]:
    init_event_radar_db()
    clean_model = str(model or "").removeprefix("models/").strip()
    if not _GEMINI_MODEL_ID_RE.fullmatch(clean_model):
        raise ValueError("invalid_gemini_model")
    if auto_tier not in _AUTO_TIERS:
        raise ValueError("invalid_auto_tier")
    daily_limit = max(1, min(200, int(daily_limit)))
    monthly_credit_usd = max(0.0, min(5000.0, float(monthly_credit_usd)))
    prior_spend_usd = max(0.0, min(5000.0, float(prior_spend_usd)))
    monthly_cap_usd = max(0.0, min(5000.0, float(monthly_cap_usd)))

    with get_db() as db:
        existing = db.execute(
            "SELECT api_key FROM user_radar_ai_config WHERE username=?", [username]
        ).fetchone()
        if api_key is not None and api_key.strip():
            clean_key = normalize_gemini_api_key(api_key)
        elif existing and existing["api_key"]:
            clean_key = existing["api_key"]
        else:
            clean_key = ""

        db.execute(
            """INSERT INTO user_radar_ai_config
               (username, api_key, model, enabled, use_google_search, auto_enrich,
                auto_tier, auto_briefing, daily_limit, monthly_credit_usd,
                prior_spend_usd, monthly_cap_usd, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
                 api_key=excluded.api_key,
                 model=excluded.model,
                 enabled=excluded.enabled,
                 use_google_search=excluded.use_google_search,
                 auto_enrich=excluded.auto_enrich,
                 auto_tier=excluded.auto_tier,
                 auto_briefing=excluded.auto_briefing,
                 daily_limit=excluded.daily_limit,
                 monthly_credit_usd=excluded.monthly_credit_usd,
                 prior_spend_usd=excluded.prior_spend_usd,
                 monthly_cap_usd=excluded.monthly_cap_usd,
                 updated_at=excluded.updated_at""",
            [
                username,
                clean_key,
                clean_model,
                int(bool(enabled)),
                int(bool(use_google_search)),
                int(bool(auto_enrich)),
                auto_tier,
                int(bool(auto_briefing)),
                daily_limit,
                monthly_credit_usd,
                prior_spend_usd,
                monthly_cap_usd,
                time.time(),
            ],
        )
    return get_user_ai_config(username)


def _reserve_daily_gemini_call(username: str, daily_limit: int) -> bool:
    """Atomically check monthly Ultra credit cap AND reserve 1 daily call BEFORE network I/O."""
    init_event_radar_db()
    with get_db() as db:
        ai_row = db.execute("SELECT * FROM user_radar_ai_config WHERE username=?", [username]).fetchone()
    if ai_row:
        bill = _compute_gemini_billing(username, ai_row)
        cap = float(bill["monthly_cap_usd"])
        if cap > 0 and float(bill["total_month_spend_usd"]) >= cap:
            return False

    day = _today_shanghai()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            row = db.execute(
                "SELECT calls_used FROM radar_ai_daily_usage WHERE day=? AND username=?",
                [day, username],
            ).fetchone()
            used = int(row["calls_used"]) if row else 0
            if used >= daily_limit:
                db.execute("COMMIT")
                return False
            db.execute(
                """INSERT INTO radar_ai_daily_usage(day, username, calls_used) VALUES(?,?,1)
                   ON CONFLICT(day, username) DO UPDATE SET calls_used=calls_used+1""",
                [day, username],
            )
            db.execute("COMMIT")
            return True
        except BaseException:
            db.execute("ROLLBACK")
            raise


def _record_gemini_ledger(
    *,
    username: str,
    model: str,
    call_kind: str,
    prompt_tokens: int,
    output_tokens: int,
    search_queries: int,
) -> float:
    in_r, out_r = _gemini_rate_for_model(model)
    cost = round((prompt_tokens / 1_000_000.0) * in_r + (output_tokens / 1_000_000.0) * out_r, 6)
    dt = datetime.now(SHANGHAI)
    with get_db() as db:
        db.execute(
            """INSERT INTO radar_ai_call_ledger
               (day, month, username, model, call_kind, prompt_tokens, output_tokens,
                search_queries, cost_usd, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                dt.strftime("%Y-%m-%d"),
                dt.strftime("%Y-%m"),
                username,
                model,
                call_kind,
                max(0, int(prompt_tokens)),
                max(0, int(output_tokens)),
                max(0, int(search_queries)),
                cost,
                time.time(),
            ],
        )
    return cost


def _record_ai_status(username: str, status_msg: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE user_radar_ai_config SET last_status=?, updated_at=? WHERE username=?",
            [status_msg[:160], time.time(), username],
        )


def call_gemini_grounded(
    *,
    api_key: str,
    model: str,
    prompt: str,
    use_google_search: bool = True,
    timeout: float = 22.0,
    ledger_owner: str = SYSTEM_OWNER,
    call_kind: str = "enrich",
) -> tuple[bool, str, list[dict[str, str]]]:
    """Call official Google Gemini REST endpoint with header auth, search grounding & exact usage ledger."""
    clean_model = str(model or "").removeprefix("models/").strip()
    if not _GEMINI_MODEL_ID_RE.fullmatch(clean_model) or not _GEMINI_KEY_RE.fullmatch(api_key):
        return False, "invalid_gemini_configuration", []

    ai_studio_ep = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent"
    vertex_express_ep = (
        f"https://aiplatform.googleapis.com/v1/publishers/google/models/{clean_model}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    def _build_body(with_search: bool) -> dict[str, object]:
        body: dict[str, object] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 1200,
            },
        }
        if with_search:
            body["tools"] = [{"google_search": {}}]
        return body

    search_downgraded = False
    try:
        with public_network.sync_client(timeout=timeout) as client:
            resp = client.post(ai_studio_ep, json=_build_body(use_google_search), headers=headers)
            if resp.status_code in {400, 429} and use_google_search:
                alt_plain = client.post(ai_studio_ep, json=_build_body(False), headers=headers)
                if alt_plain.status_code == 200:
                    resp = alt_plain
                    search_downgraded = True
            if resp.status_code in {401, 403} and api_key.startswith("AQ."):
                alt_vx = client.post(vertex_express_ep, json=_build_body(use_google_search), headers=headers)
                if alt_vx.status_code == 200:
                    resp = alt_vx
            if resp.status_code != 200:
                detail_hint = ""
                with contextlib.suppress(Exception):
                    err_msg = str(((resp.json() or {}).get("error") or {}).get("message") or "")
                    if "no longer available" in err_msg:
                        detail_hint = ":model_retired_use_gemini_3_5_flash_lite"
                    elif "high demand" in err_msg:
                        detail_hint = ":high_demand_switch_to_gemini_3_5_flash_lite"
                return False, f"gemini_http_{resp.status_code}{detail_hint}", []
            data = resp.json()
    except Exception as exc:
        return False, f"gemini_error:{type(exc).__name__}", []

    candidates = data.get("candidates") if isinstance(data, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return False, "gemini_empty_candidates", []
    cand = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = ((cand.get("content") or {}).get("parts")) or []
    text_out = "\n".join(
        str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("text")
    ).strip()
    if not text_out:
        return False, "gemini_empty_text", []

    sources: list[dict[str, str]] = []
    gm = cand.get("groundingMetadata") if isinstance(cand.get("groundingMetadata"), dict) else {}
    chunks = gm.get("groundingChunks") if isinstance(gm.get("groundingChunks"), list) else []
    seen_urls: set[str] = set()
    for ch in chunks:
        web = ch.get("web") if isinstance(ch, dict) else None
        if not isinstance(web, dict):
            continue
        uri = str(web.get("uri") or "").strip()
        title = str(web.get("title") or uri).strip()
        if uri.startswith("https://") and uri not in seen_urls:
            seen_urls.add(uri)
            sources.append({"title": title[:80], "url": uri})
        if len(sources) >= 4:
            break

    usage_meta = data.get("usageMetadata") if isinstance(data.get("usageMetadata"), dict) else {}
    prompt_tok = int(usage_meta.get("promptTokenCount") or max(1, len(prompt) // 3))
    out_tok = int(usage_meta.get("candidatesTokenCount") or 0) + int(
        usage_meta.get("thoughtsTokenCount") or 0
    )
    if out_tok <= 0:
        out_tok = max(1, len(text_out) // 2)
    web_queries = gm.get("webSearchQueries")
    search_cnt = (
        0
        if search_downgraded
        else (len(web_queries) if isinstance(web_queries, list) else (1 if sources else 0))
    )
    with contextlib.suppress(Exception):
        _record_gemini_ledger(
            username=ledger_owner,
            model=clean_model,
            call_kind=call_kind,
            prompt_tokens=prompt_tok,
            output_tokens=out_tok,
            search_queries=search_cnt,
        )

    return True, text_out, sources


def _extract_json_object(raw_text: str) -> dict[str, object] | None:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            return None
    return None


def enrich_event_with_gemini(
    event_id: str,
    *,
    username: str,
    ignore_enabled_flag: bool = False,
) -> dict[str, object]:
    """Run Google Search grounded verification & A-share/futures synthesis on one radar event."""
    init_event_radar_db()
    with get_db() as db:
        ev_row = db.execute("SELECT * FROM radar_events WHERE id=?", [event_id]).fetchone()
        ai_row = db.execute("SELECT * FROM user_radar_ai_config WHERE username=?", [username]).fetchone()
        if username == SYSTEM_OWNER:
            watch_rows = db.execute("SELECT DISTINCT ticker, name FROM user_watchlist LIMIT 20").fetchall()
        else:
            watch_rows = db.execute(
                "SELECT ticker, name FROM user_watchlist WHERE username=?", [username]
            ).fetchall()

    if not ev_row:
        raise ValueError("event_not_found")
    if not ai_row or not ai_row["api_key"]:
        raise ValueError("gemini_key_missing")
    if not ignore_enabled_flag and not ai_row["enabled"]:
        raise ValueError("gemini_disabled")

    pb = json.loads(ev_row["playbook_json"] or "{}")
    if isinstance(pb.get("ai_enrichment"), dict) and not ignore_enabled_flag:
        return pb["ai_enrichment"]

    daily_limit = int(ai_row["daily_limit"] or 20)
    if not _reserve_daily_gemini_call(username, daily_limit):
        _record_ai_status(username, f"circuit_breaker_reached ({daily_limit}/day)")
        raise ValueError("gemini_daily_limit_reached")

    tape = fetch_cross_market_tape()
    tape_str = (
        "；".join(f"{t['name']} {t['price']} ({t['change_pct']:+.2f}%)" for t in tape[:6])
        or "暂无跨市场实时报价"
    )
    watch_str = "、".join(f"{r['name'] or ''}({r['ticker']})" for r in watch_rows[:12]) or "未设置自选"
    win = detect_trading_window()

    prompt = (
        "你是机构级全球宏观与A股/国内商品期货盘中风向研判官。请使用 Google Search 实时核实以下快讯的一手信源真伪与市场预期差，"
        "并输出严格合法的 JSON 对象（不要输出任何 Markdown 解释文字，只输出一个 JSON 对象）：\n"
        f"【快讯来源】：{ev_row['source']}\n"
        f"【快讯标题】：{ev_row['title']}\n"
        f"【快讯正文】：{ev_row['body']}\n"
        f"【当前交易窗口】：{win['tag']}\n"
        f"【全球跨市场实时价格锚】：{tape_str}\n"
        f"【用户自选股池】：{watch_str}\n\n"
        "请严格按以下 JSON 字段返回简体中文结果：\n"
        "{\n"
        '  "cn_title": "若原文为英文请专业精译为中文标题；若为中文则提炼28字内核心结论标题",\n'
        '  "verdict": "一手真伪与预期差定性（如：官方一手确认·超市场预期 / 旧闻重炒·预期已计价 / 传闻待证实）",\n'
        '  "delta_points": ["核心预期差增量1（含关键数字或政策实质）", "全球向A股及国内期货的传导路径2"],\n'
        '  "execution_rule": "结合当前交易时段与跨市场反应的挂单/竞价/盘中承接执行纪律（65字内）",\n'
        '  "invalidation_rule": "明确的证伪止损红线（55字内）"\n'
        "}"
    )

    ok, raw_text, sources = call_gemini_grounded(
        api_key=ai_row["api_key"],
        model=ai_row["model"],
        prompt=prompt,
        use_google_search=bool(ai_row["use_google_search"]),
    )
    if not ok:
        _record_ai_status(username, raw_text)
        raise ValueError(raw_text)

    parsed = _extract_json_object(raw_text) or {}
    enrichment = {
        "model": ai_row["model"],
        "grounded": bool(sources),
        "cn_title": str(parsed.get("cn_title") or "").strip()[:120],
        "verdict": str(parsed.get("verdict") or "已完成全球语义与盘面交叉推演").strip()[:120],
        "delta_points": [str(x).strip()[:180] for x in (parsed.get("delta_points") or []) if str(x).strip()][
            :3
        ]
        or [raw_text[:200]],
        "execution_rule": str(parsed.get("execution_rule") or "").strip()[:180],
        "invalidation_rule": str(parsed.get("invalidation_rule") or "").strip()[:180],
        "sources": sources,
        "enriched_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
    }

    pb["ai_enrichment"] = enrichment
    if enrichment["delta_points"]:
        pb["delta_points"] = enrichment["delta_points"]
    if enrichment["execution_rule"]:
        pb["execution_rule"] = enrichment["execution_rule"]
    if enrichment["invalidation_rule"]:
        pb["invalidation_rule"] = enrichment["invalidation_rule"]

    with get_db() as db:
        db.execute(
            "UPDATE radar_events SET playbook_json=? WHERE id=?",
            [json.dumps(pb, ensure_ascii=False), event_id],
        )
    _record_ai_status(username, f"ok ({ai_row['model']}, sources={len(sources)})")
    return enrichment


def generate_global_briefing(
    *, username: str | None = None, slot: str = "manual", force: bool = False
) -> dict[str, object]:
    """Generate the Morning (08:45) / Midday (12:45) Global Cross-Market Battle Briefing.

    Uses Gemini + Google Search when configured and within daily cap; otherwise builds a crisp
    0-token deterministic global briefing so the War Room always has a fresh overview card.
    """
    init_event_radar_db()
    dt_now = datetime.now(SHANGHAI)
    day = dt_now.strftime("%Y-%m-%d")
    brief_id = f"{day}-{slot}" if slot in {"morning", "midday"} else f"{day}-manual-{int(time.time())}"

    if not force and slot in {"morning", "midday"}:
        with get_db() as db:
            existing = db.execute("SELECT * FROM radar_briefings WHERE id=?", [brief_id]).fetchone()
        if existing:
            return json.loads(existing["summary_json"])

    tape = fetch_cross_market_tape()
    with get_db() as db:
        recent_rows = db.execute(
            "SELECT source, tier, theme, title, published_at FROM radar_events "
            "WHERE created_at >= ? ORDER BY score DESC, created_at DESC LIMIT 10",
            [time.time() - 14 * 3600],
        ).fetchall()
        ai_candidate = None
        if username:
            ai_candidate = db.execute(
                "SELECT * FROM user_radar_ai_config WHERE username=? AND api_key != ''", [username]
            ).fetchone()
        else:
            ai_candidate = db.execute(
                "SELECT * FROM user_radar_ai_config WHERE enabled=1 AND auto_briefing=1 AND api_key != '' "
                "ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()

    slot_label = (
        "08:45 盘前全球隔夜作战早报"
        if slot == "morning"
        else "12:45 午间政策与亚太作战午报"
        if slot == "midday"
        else "全球跨市场实时作战简报"
    )
    tape_summary = [f"{t['name']} {t['price']} ({t['change_pct']:+.2f}%)" for t in tape[:6]]
    top_headlines = [f"[{r['tier']}·{r['source']}] {r['title']}" for r in recent_rows[:6]]

    summary_obj: dict[str, object] = {
        "id": brief_id,
        "slot": slot,
        "title": slot_label,
        "generated_at": dt_now.isoformat(timespec="seconds"),
        "engine": "本地确定性跨市场合成（¥0 零消耗）",
        "overview": "已聚合近 14 小时海内外核心电报与全球跨市场实时价格锚，重点关注跨市场共振方向与开盘承接强度。",
        "bullets": top_headlines[:4] or ["暂无近14小时高优催化，建议以自选股均线承接与期货分位极值为准。"],
        "action_plan": detect_trading_window(dt_now)["action_hint"],
        "sources": [],
    }

    if ai_candidate and _reserve_daily_gemini_call(
        ai_candidate["username"], int(ai_candidate["daily_limit"] or 20)
    ):
        prompt = (
            f"你是机构级全球宏观与A股/国内商品期货投研总监。现在生成《{slot_label}》。"
            "请利用 Google Search 检索过去12小时全球最重要的宏观/美联储/关税/AI算力链(英伟达等)/大宗商品动态，"
            "结合以下已捕获快讯与跨市场报价，输出严格合法的 JSON 对象：\n"
            f"【跨市场实时报价】：{'；'.join(tape_summary) or '暂无'}\n"
            f"【已捕获高分快讯】：{' | '.join(top_headlines) or '暂无'}\n\n"
            "严格返回如下简体中文 JSON 结构：\n"
            "{\n"
            '  "overview": "一句话定调当前全球宏观与风险偏好温度（60字内）",\n'
            '  "bullets": ["核心催化与预期差1", "海外映射至A股/国内期货主线2", "潜在避雷风险点3"],\n'
            '  "action_plan": "下一交易窗口（竞价/午盘/夜盘）具体仓位与节奏纪律（70字内）"\n'
            "}"
        )
        ok, raw_text, sources = call_gemini_grounded(
            api_key=ai_candidate["api_key"],
            model=ai_candidate["model"],
            prompt=prompt,
            use_google_search=bool(ai_candidate["use_google_search"]),
        )
        if ok:
            parsed = _extract_json_object(raw_text) or {}
            summary_obj["engine"] = f"Gemini ({ai_candidate['model']}) + Google Search 全球溯源"
            if parsed.get("overview"):
                summary_obj["overview"] = str(parsed["overview"])[:200]
            if isinstance(parsed.get("bullets"), list) and parsed["bullets"]:
                summary_obj["bullets"] = [str(b)[:180] for b in parsed["bullets"][:4]]
            if parsed.get("action_plan"):
                summary_obj["action_plan"] = str(parsed["action_plan"])[:220]
            summary_obj["sources"] = sources
            _record_ai_status(ai_candidate["username"], f"briefing_ok ({slot})")

    with get_db() as db:
        db.execute(
            """INSERT OR REPLACE INTO radar_briefings(id, slot, title, summary_json, created_at)
               VALUES (?,?,?,?,?)""",
            [brief_id, slot, slot_label, json.dumps(summary_obj, ensure_ascii=False), time.time()],
        )
    return summary_obj


def get_latest_briefing() -> dict[str, object] | None:
    init_event_radar_db()
    with get_db() as db:
        row = db.execute(
            "SELECT summary_json FROM radar_briefings ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["summary_json"])
    except Exception:
        return None


def _auto_enrich_fresh_alerts(fresh_events: list[dict[str, object]]) -> None:
    """Automatically run Gemini Grounding on qualifying high-signal alerts within daily cap."""
    if not fresh_events:
        return
    with get_db() as db:
        ai_users = db.execute(
            "SELECT username, auto_tier FROM user_radar_ai_config "
            "WHERE enabled=1 AND auto_enrich=1 AND api_key != ''"
        ).fetchall()
        if not ai_users:
            return
        watch_by_user: dict[str, set[str]] = {}
        for urow in ai_users:
            u = urow["username"]
            wrows = db.execute("SELECT ticker FROM user_watchlist WHERE username=?", [u]).fetchall()
            watch_by_user[u] = {str(r["ticker"]) for r in wrows}

    for ev in fresh_events:
        tier = str(ev.get("tier") or "T2")
        if tier not in {"T0", "T1"}:
            continue
        ev_tickers = set(ev.get("tickers") or [])
        is_global = str(ev.get("region") or "CN") == "GLOBAL"
        for urow in ai_users:
            u = urow["username"]
            mode = urow["auto_tier"] or "T0_WATCH"
            should_run = (
                tier == "T0"
                or (mode == "T0_T1" and tier == "T1")
                or (
                    mode == "T0_WATCH" and tier == "T1" and (bool(ev_tickers & watch_by_user[u]) or is_global)
                )
            )
            if not should_run:
                continue
            try:
                enrichment = enrich_event_with_gemini(str(ev["id"]), username=u)
                if isinstance(ev.get("playbook"), dict):
                    ev["playbook"]["ai_enrichment"] = enrichment
                    if enrichment.get("delta_points"):
                        ev["playbook"]["delta_points"] = enrichment["delta_points"]
                    if enrichment.get("execution_rule"):
                        ev["playbook"]["execution_rule"] = enrichment["execution_rule"]
                break  # Shared event playbook is now enriched for all viewers!
            except ValueError:
                continue


def _maybe_run_scheduled_briefing(now: datetime | None = None) -> None:
    dt = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    if dt.weekday() >= 5:
        return
    hhmm = dt.hour * 60 + dt.minute
    slot = ""
    if 8 * 60 + 45 <= hhmm <= 9 * 60 + 10:
        slot = "morning"
    elif 12 * 60 + 45 <= hhmm <= 12 * 60 + 58:
        slot = "midday"
    if not slot:
        return
    brief_id = f"{dt.strftime('%Y-%m-%d')}-{slot}"
    with get_db() as db:
        if db.execute("SELECT 1 FROM radar_briefings WHERE id=?", [brief_id]).fetchone():
            return
    try:
        brief = generate_global_briefing(slot=slot, force=False)
        dispatch_briefing_to_channels(brief)
    except Exception:
        pass


def dispatch_briefing_to_channels(brief: dict[str, object]) -> int:
    """Push scheduled morning/midday briefing digest across all enabled multi-platform endpoints."""
    init_event_radar_db()
    _sync_legacy_system_channel_if_needed()
    with get_db() as db:
        eps = db.execute(
            "SELECT id, provider, endpoint_secret, bark_level "
            "FROM radar_push_endpoints WHERE enabled=1 AND endpoint_secret != ''"
        ).fetchall()
    sent = 0
    for ep in eps:
        ok, _ = send_push_notification(
            ep["provider"],
            ep["endpoint_secret"],
            title=f"🌐 Prism · {brief['title']}",
            subtitle=str(brief.get("engine") or "Prism 全球风向雷达"),
            body=f"{brief.get('overview', '')}\n纪律：{brief.get('action_plan', '')}",
            bark_level=ep["bark_level"],
        )
        if ok:
            sent += 1
    return sent


# ── Strict SSRF-proof Cross-Platform Push Validation & Multi-Channel Delivery ──


def list_push_providers() -> list[dict[str, object]]:
    return [dict(item) for item in PUSH_PROVIDER_CATALOG]


def _default_mode_for_provider(provider: str) -> str:
    if provider == "bark":
        return "timeSensitive"
    if provider == "ntfy":
        return "high"
    return "markdown"


def normalize_push_secret(provider: str, raw_value: str) -> str:
    """Validate and extract canonical token/topic/key for strictly allowlisted official HTTPS push gateways."""
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_push_provider")
    val = str(raw_value or "").strip()
    if not val:
        raise ValueError("empty_push_secret")

    if provider == "ntfy":
        if "://" in val:
            parsed = urlsplit(val)
            if parsed.scheme != "https" or parsed.hostname != "ntfy.sh" or parsed.port is not None:
                raise ValueError("unsupported_push_endpoint")
            segs = [s for s in parsed.path.split("/") if s]
            if len(segs) != 1 or not _NTFY_TOPIC_RE.fullmatch(segs[0]):
                raise ValueError("invalid_ntfy_topic")
            return segs[0]
        if not _NTFY_TOPIC_RE.fullmatch(val):
            raise ValueError("invalid_ntfy_topic")
        return val

    if provider == "pushplus":
        if "://" in val:
            parsed = urlsplit(val)
            if parsed.scheme != "https" or parsed.hostname != "www.pushplus.plus" or parsed.port is not None:
                raise ValueError("unsupported_push_endpoint")
            tok = (parse_qs(parsed.query).get("token") or [""])[0]
            if not _PUSHPLUS_TOKEN_RE.fullmatch(tok):
                raise ValueError("invalid_pushplus_token")
            return tok
        if not _PUSHPLUS_TOKEN_RE.fullmatch(val):
            raise ValueError("invalid_pushplus_token")
        return val

    if provider == "serverchan":
        if "://" in val:
            parsed = urlsplit(val)
            if parsed.scheme != "https" or parsed.hostname != "sctapi.ftqq.com" or parsed.port is not None:
                raise ValueError("unsupported_push_endpoint")
            key_seg = parsed.path.strip("/").removesuffix(".send")
            if not _SERVERCHAN_KEY_RE.fullmatch(key_seg):
                raise ValueError("invalid_serverchan_key")
            return key_seg
        val_clean = val.removesuffix(".send")
        if not _SERVERCHAN_KEY_RE.fullmatch(val_clean):
            raise ValueError("invalid_serverchan_key")
        return val_clean

    if provider == "bark":
        if "://" in val:
            parsed = urlsplit(val)
            if parsed.scheme != "https" or parsed.hostname != "api.day.app" or parsed.port is not None:
                raise ValueError("unsupported_push_endpoint")
            segs = [s for s in parsed.path.split("/") if s]
            if not segs or not _BARK_KEY_RE.fullmatch(segs[0]):
                raise ValueError("invalid_bark_device_key")
            return segs[0]
        if not _BARK_KEY_RE.fullmatch(val):
            raise ValueError("invalid_bark_device_key")
        return val

    if provider == "wecom":
        if "://" in val:
            parsed = urlsplit(val)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "qyapi.weixin.qq.com"
                or parsed.port is not None
                or parsed.path != "/cgi-bin/webhook/send"
            ):
                raise ValueError("unsupported_push_endpoint")
            key = (parse_qs(parsed.query).get("key") or [""])[0]
            if not _TOKEN_RE.fullmatch(key):
                raise ValueError("invalid_wecom_key")
            return key
        if not _TOKEN_RE.fullmatch(val):
            raise ValueError("invalid_wecom_key")
        return val

    if provider == "feishu":
        if "://" in val:
            parsed = urlsplit(val)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "open.feishu.cn"
                or parsed.port is not None
                or not parsed.path.startswith("/open-apis/bot/v2/hook/")
            ):
                raise ValueError("unsupported_push_endpoint")
            token = parsed.path.removeprefix("/open-apis/bot/v2/hook/").strip("/")
            if not _TOKEN_RE.fullmatch(token):
                raise ValueError("invalid_feishu_hook")
            return token
        if not _TOKEN_RE.fullmatch(val):
            raise ValueError("invalid_feishu_hook")
        return val

    if provider == "dingtalk":
        if "://" in val:
            parsed = urlsplit(val)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "oapi.dingtalk.com"
                or parsed.port is not None
                or parsed.path != "/robot/send"
            ):
                raise ValueError("unsupported_push_endpoint")
            token = (parse_qs(parsed.query).get("access_token") or [""])[0]
            if not _TOKEN_RE.fullmatch(token):
                raise ValueError("invalid_dingtalk_token")
            return token
        if not _TOKEN_RE.fullmatch(val):
            raise ValueError("invalid_dingtalk_token")
        return val

    if provider == "telegram":
        if "://" in val:
            parsed = urlsplit(val)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "api.telegram.org"
                or parsed.port is not None
                or not parsed.path.startswith("/bot")
            ):
                raise ValueError("unsupported_push_endpoint")
            bot_part = parsed.path.removeprefix("/bot").split("/")[0]
            chat_id = (parse_qs(parsed.query).get("chat_id") or [""])[0]
            combined = f"{bot_part}@{chat_id}"
            if not _TELEGRAM_SECRET_RE.fullmatch(combined):
                raise ValueError("invalid_telegram_secret")
            return combined
        normalized_tg = val.replace("|", "@")
        if not _TELEGRAM_SECRET_RE.fullmatch(normalized_tg):
            raise ValueError("invalid_telegram_secret")
        return normalized_tg

    raise ValueError("unsupported_push_provider")


def _sync_legacy_system_channel_if_needed(owner: str = SYSTEM_OWNER) -> None:
    """Ensure legacy single-channel config in user_radar_channels appears in radar_push_endpoints."""
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) FROM radar_push_endpoints WHERE owner=?", [owner]).fetchone()[0]
        if count > 0:
            return
        legacy = db.execute(
            "SELECT provider, endpoint_secret, enabled, min_tier, bark_level, notify_watchlist, "
            "updated_at, last_sent_at, last_status FROM user_radar_channels WHERE username=?",
            [owner],
        ).fetchone()
        if legacy and legacy["endpoint_secret"]:
            db.execute(
                """INSERT INTO radar_push_endpoints
                   (owner, label, provider, endpoint_secret, enabled, min_tier, bark_level,
                    notify_watchlist, updated_at, last_sent_at, last_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    owner,
                    "默认主设备",
                    legacy["provider"],
                    legacy["endpoint_secret"],
                    int(legacy["enabled"]),
                    legacy["min_tier"],
                    legacy["bark_level"],
                    int(legacy["notify_watchlist"]),
                    legacy["updated_at"] or time.time(),
                    legacy["last_sent_at"],
                    legacy["last_status"] or "",
                ],
            )


def _serialize_push_endpoint(row) -> dict[str, object]:
    meta = next((p for p in PUSH_PROVIDER_CATALOG if p["id"] == row["provider"]), None)
    return {
        "id": int(row["id"]),
        "label": row["label"] or (str(meta["label"]).split(" ")[0] if meta else row["provider"]),
        "provider": row["provider"],
        "provider_label": str(meta["label"]) if meta else row["provider"],
        "os": str(meta["os"]) if meta else "",
        "masked_secret": _mask_secret(row["endpoint_secret"]),
        "enabled": bool(row["enabled"]),
        "min_tier": row["min_tier"],
        "bark_level": row["bark_level"],
        "notify_watchlist": bool(row["notify_watchlist"]),
        "last_sent_at": row["last_sent_at"],
        "last_status": row["last_status"] or "",
    }


def list_ops_push_channels(owner: str = SYSTEM_OWNER) -> list[dict[str, object]]:
    init_event_radar_db()
    _sync_legacy_system_channel_if_needed(owner)
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM radar_push_endpoints WHERE owner=? ORDER BY id ASC",
            [owner],
        ).fetchall()
    return [_serialize_push_endpoint(r) for r in rows]


def add_ops_push_channel(
    owner: str = SYSTEM_OWNER,
    *,
    label: str = "",
    provider: str,
    secret: str,
    enabled: bool = True,
    min_tier: str = "T0",
    bark_level: str | None = None,
    notify_watchlist: bool = True,
) -> dict[str, object]:
    init_event_radar_db()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_push_provider")
    if min_tier not in _MIN_TIERS:
        raise ValueError("invalid_min_tier")
    mode = bark_level if bark_level in _DELIVERY_MODES else _default_mode_for_provider(provider)
    canonical = normalize_push_secret(provider, secret)
    clean_label = str(label or "").strip()[:40]
    if not clean_label:
        meta = next((p for p in PUSH_PROVIDER_CATALOG if p["id"] == provider), None)
        clean_label = str(meta["label"]).split(" ")[0] if meta else provider

    now = time.time()
    with get_db() as db:
        cnt = db.execute("SELECT COUNT(*) FROM radar_push_endpoints WHERE owner=?", [owner]).fetchone()[0]
        if int(cnt) >= 16:
            raise ValueError("push_channel_limit_exceeded")
        cur = db.execute(
            """INSERT INTO radar_push_endpoints
               (owner, label, provider, endpoint_secret, enabled, min_tier, bark_level,
                notify_watchlist, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                owner,
                clean_label,
                provider,
                canonical,
                int(bool(enabled)),
                min_tier,
                mode,
                int(bool(notify_watchlist)),
                now,
            ],
        )
        new_id = int(cur.lastrowid)
        row = db.execute("SELECT * FROM radar_push_endpoints WHERE id=?", [new_id]).fetchone()
    return _serialize_push_endpoint(row)


def update_ops_push_channel(
    channel_id: int,
    owner: str = SYSTEM_OWNER,
    *,
    label: str | None = None,
    provider: str | None = None,
    secret: str | None = None,
    enabled: bool | None = None,
    min_tier: str | None = None,
    bark_level: str | None = None,
    notify_watchlist: bool | None = None,
) -> dict[str, object]:
    init_event_radar_db()
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM radar_push_endpoints WHERE id=? AND owner=?",
            [int(channel_id), owner],
        ).fetchone()
        if not existing:
            raise ValueError("push_channel_not_found")
        prov = provider if provider is not None else str(existing["provider"])
        if prov not in _SUPPORTED_PROVIDERS:
            raise ValueError("unsupported_push_provider")
        tier = min_tier if min_tier is not None else str(existing["min_tier"])
        if tier not in _MIN_TIERS:
            raise ValueError("invalid_min_tier")
        mode = bark_level if bark_level is not None else str(existing["bark_level"])
        if mode not in _DELIVERY_MODES:
            mode = _default_mode_for_provider(prov)
        if secret is not None and secret.strip():
            canonical = normalize_push_secret(prov, secret)
        elif prov == existing["provider"]:
            canonical = str(existing["endpoint_secret"])
        else:
            raise ValueError("empty_push_secret")
        clean_label = (
            str(label).strip()[:40] if (label is not None and str(label).strip()) else str(existing["label"])
        )
        en = int(bool(existing["enabled"] if enabled is None else enabled))
        nw = int(bool(existing["notify_watchlist"] if notify_watchlist is None else notify_watchlist))
        db.execute(
            """UPDATE radar_push_endpoints
               SET label=?, provider=?, endpoint_secret=?, enabled=?, min_tier=?,
                   bark_level=?, notify_watchlist=?, updated_at=?
               WHERE id=? AND owner=?""",
            [clean_label, prov, canonical, en, tier, mode, nw, time.time(), int(channel_id), owner],
        )
        updated = db.execute("SELECT * FROM radar_push_endpoints WHERE id=?", [int(channel_id)]).fetchone()
    return _serialize_push_endpoint(updated)


def delete_ops_push_channel(channel_id: int, owner: str = SYSTEM_OWNER) -> bool:
    init_event_radar_db()
    with get_db() as db:
        cur = db.execute(
            "DELETE FROM radar_push_endpoints WHERE id=? AND owner=?",
            [int(channel_id), owner],
        )
    return cur.rowcount > 0


def get_user_push_config(username: str) -> dict[str, object]:
    init_event_radar_db()
    if username == SYSTEM_OWNER:
        channels = list_ops_push_channels(SYSTEM_OWNER)
        if channels:
            active = next((c for c in channels if c["enabled"]), channels[0])
            return {
                "provider": active["provider"],
                "has_secret": True,
                "masked_secret": active["masked_secret"],
                "enabled": any(bool(c["enabled"]) for c in channels),
                "min_tier": active["min_tier"],
                "bark_level": active["bark_level"],
                "notify_watchlist": active["notify_watchlist"],
                "last_sent_at": active["last_sent_at"],
                "last_status": active["last_status"],
                "channel_count": len(channels),
                "enabled_count": sum(1 for c in channels if c["enabled"]),
            }
    with get_db() as db:
        row = db.execute(
            "SELECT provider, endpoint_secret, enabled, min_tier, bark_level, notify_watchlist, "
            "updated_at, last_sent_at, last_status FROM user_radar_channels WHERE username=?",
            [username],
        ).fetchone()
    if not row:
        return {
            "provider": "ntfy",
            "has_secret": False,
            "masked_secret": "",
            "enabled": False,
            "min_tier": "T0",
            "bark_level": "high",
            "notify_watchlist": True,
            "last_sent_at": None,
            "last_status": "",
            "channel_count": 0,
            "enabled_count": 0,
        }
    return {
        "provider": row["provider"],
        "has_secret": bool(row["endpoint_secret"]),
        "masked_secret": _mask_secret(row["endpoint_secret"]),
        "enabled": bool(row["enabled"]),
        "min_tier": row["min_tier"],
        "bark_level": row["bark_level"],
        "notify_watchlist": bool(row["notify_watchlist"]),
        "last_sent_at": row["last_sent_at"],
        "last_status": row["last_status"] or "",
        "channel_count": 1 if row["endpoint_secret"] else 0,
        "enabled_count": 1 if (row["endpoint_secret"] and row["enabled"]) else 0,
    }


def save_user_push_config(
    username: str,
    *,
    provider: str,
    secret: str | None,
    enabled: bool,
    min_tier: str = "T0",
    bark_level: str = "timeSensitive",
    notify_watchlist: bool = True,
) -> dict[str, object]:
    init_event_radar_db()
    if provider not in _SUPPORTED_PROVIDERS:
        raise ValueError("unsupported_push_provider")
    if min_tier not in _MIN_TIERS:
        raise ValueError("invalid_min_tier")
    if bark_level not in _DELIVERY_MODES:
        raise ValueError("invalid_bark_level")

    now = time.time()
    with get_db() as db:
        existing = db.execute(
            "SELECT endpoint_secret, provider FROM user_radar_channels WHERE username=?", [username]
        ).fetchone()
        if secret is not None and secret.strip():
            canonical = normalize_push_secret(provider, secret)
        elif existing and existing["endpoint_secret"] and existing["provider"] == provider:
            canonical = existing["endpoint_secret"]
        elif enabled:
            raise ValueError("empty_push_secret")
        else:
            canonical = ""

        db.execute(
            """INSERT INTO user_radar_channels
               (username, provider, endpoint_secret, enabled, min_tier, bark_level, notify_watchlist, updated_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
                 provider=excluded.provider,
                 endpoint_secret=excluded.endpoint_secret,
                 enabled=excluded.enabled,
                 min_tier=excluded.min_tier,
                 bark_level=excluded.bark_level,
                 notify_watchlist=excluded.notify_watchlist,
                 updated_at=excluded.updated_at""",
            [
                username,
                provider,
                canonical,
                int(bool(enabled)),
                min_tier,
                bark_level,
                int(bool(notify_watchlist)),
                now,
            ],
        )
        if username == SYSTEM_OWNER and canonical:
            first_ep = db.execute(
                "SELECT id FROM radar_push_endpoints WHERE owner=? ORDER BY id ASC LIMIT 1",
                [SYSTEM_OWNER],
            ).fetchone()
            meta = next((p for p in PUSH_PROVIDER_CATALOG if p["id"] == provider), None)
            lbl = str(meta["label"]).split(" ")[0] if meta else provider
            if first_ep:
                db.execute(
                    """UPDATE radar_push_endpoints
                       SET label=?, provider=?, endpoint_secret=?, enabled=?, min_tier=?,
                           bark_level=?, notify_watchlist=?, updated_at=?
                       WHERE id=?""",
                    [
                        lbl,
                        provider,
                        canonical,
                        int(bool(enabled)),
                        min_tier,
                        bark_level,
                        int(bool(notify_watchlist)),
                        now,
                        first_ep["id"],
                    ],
                )
            else:
                db.execute(
                    """INSERT INTO radar_push_endpoints
                       (owner, label, provider, endpoint_secret, enabled, min_tier, bark_level,
                        notify_watchlist, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [
                        SYSTEM_OWNER,
                        lbl,
                        provider,
                        canonical,
                        int(bool(enabled)),
                        min_tier,
                        bark_level,
                        int(bool(notify_watchlist)),
                        now,
                    ],
                )
    return get_user_push_config(username)


def send_push_notification(
    provider: str,
    secret: str,
    *,
    title: str,
    subtitle: str,
    body: str,
    bark_level: str = "timeSensitive",
    click_url: str | None = None,
) -> tuple[bool, str]:
    """Deliver one bounded HTTPS push notification to an allowlisted official cross-platform gateway."""
    canonical = normalize_push_secret(provider, secret)
    compact = bark_level == "compact"
    rendered_body = body[:240] if compact else body[:900]

    if provider == "ntfy":
        endpoint = "https://ntfy.sh"
        prio = 5 if bark_level in {"urgent", "critical"} else (3 if bark_level == "default" else 4)
        payload: dict[str, object] = {
            "topic": canonical,
            "title": f"{title} · {subtitle}"[:140],
            "message": rendered_body,
            "priority": prio,
            "tags": ["rotating_light" if prio >= 5 else "chart_with_upwards_trend"],
        }
        if click_url and click_url.startswith("https://"):
            payload["click"] = click_url
    elif provider == "pushplus":
        endpoint = "https://www.pushplus.plus/send"
        payload = {
            "token": canonical,
            "title": title[:96],
            "content": (
                f"{subtitle}\n{rendered_body}"
                if compact
                else f"**{subtitle}**\n\n{rendered_body.replace(chr(10), chr(10) + chr(10))}"
            ),
            "template": "txt" if compact else "markdown",
        }
    elif provider == "serverchan":
        endpoint = f"https://sctapi.ftqq.com/{canonical}.send"
        payload = {
            "title": title[:96],
            "desp": f"> {subtitle}\n\n{rendered_body}",
        }
    elif provider == "bark":
        endpoint = "https://api.day.app/push"
        level = bark_level if bark_level in _BARK_LEVELS else "timeSensitive"
        payload = {
            "device_key": canonical,
            "title": title[:120],
            "subtitle": subtitle[:100],
            "body": rendered_body[:600],
            "group": "Prism 风向雷达",
            "level": level,
        }
        if click_url and click_url.startswith("https://"):
            payload["url"] = click_url
    elif provider == "wecom":
        endpoint = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={canonical}"
        payload = (
            {"msgtype": "text", "text": {"content": f"{title}\n{subtitle}\n{rendered_body}"}}
            if compact
            else {
                "msgtype": "markdown",
                "markdown": {"content": f"**{title}**\n> {subtitle}\n\n{rendered_body}"},
            }
        )
    elif provider == "feishu":
        endpoint = f"https://open.feishu.cn/open-apis/bot/v2/hook/{canonical}"
        payload = {
            "msg_type": "text",
            "content": {"text": f"{title}\n{subtitle}\n{rendered_body}"},
        }
    elif provider == "dingtalk":
        endpoint = f"https://oapi.dingtalk.com/robot/send?access_token={canonical}"
        safe_title = title if ("Prism" in title or "风向" in title) else f"[Prism风向] {title}"
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": safe_title[:80],
                "text": f"### {safe_title}\n> {subtitle}\n\n{rendered_body}",
            },
        }
    elif provider == "telegram":
        m = _TELEGRAM_SECRET_RE.fullmatch(canonical)
        if not m:
            raise ValueError("invalid_telegram_secret")
        bot_token, chat_id = m.group(1), m.group(2)
        endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"{title}\n{subtitle}\n\n{rendered_body}",
            "disable_web_page_preview": True,
        }
    else:
        raise ValueError("unsupported_push_provider")

    try:
        with public_network.sync_client(timeout=7.0) as client:
            resp = client.post(endpoint, json=payload, headers={"Content-Type": "application/json"})
            if not (200 <= resp.status_code < 300):
                return False, f"http_{resp.status_code}"
            with contextlib.suppress(Exception):
                j = resp.json()
                if isinstance(j, dict):
                    if provider in {"bark", "pushplus"} and "code" in j and int(j["code"]) != 200:
                        return False, f"api_code_{j['code']}"
                    if provider == "serverchan" and "code" in j and int(j["code"]) not in {0, 200}:
                        return False, f"api_code_{j['code']}"
                    if provider in {"wecom", "dingtalk"} and "errcode" in j and int(j["errcode"]) != 0:
                        return False, f"errcode_{j['errcode']}"
                    if provider == "feishu" and "code" in j and int(j["code"]) != 0:
                        return False, f"feishu_code_{j['code']}"
                    if provider == "telegram" and "ok" in j and not bool(j["ok"]):
                        return False, f"tg_err_{j.get('error_code', 'unknown')}"
            return True, f"ok ({resp.status_code})"
    except Exception as exc:
        return False, type(exc).__name__


def format_event_push_copy(event: dict[str, object]) -> tuple[str, str, str]:
    tier = str(event.get("tier") or "T1")
    badge = "🔴 T0" if tier == "T0" else "🟠 T1"
    pb = event.get("playbook") if isinstance(event.get("playbook"), dict) else {}
    ai = pb.get("ai_enrichment") if isinstance(pb.get("ai_enrichment"), dict) else {}
    display_title = str(ai.get("cn_title") or event.get("title") or "")[:64]
    title = f"{badge} {display_title}"
    subtitle = f"{pb.get('window_tag') or event.get('window_tag') or '实时风向'} · {event.get('theme') or ''}"
    targets = pb.get("targets") or []
    t_str = " / ".join(f"{t.get('name')}({t.get('ticker')})" for t in targets[:3] if isinstance(t, dict))
    deltas = pb.get("delta_points") or []
    lead = str(deltas[0]) if deltas else str(event.get("body") or "")[:100]
    lines = []
    if ai.get("verdict"):
        lines.append(f"AI核验：{ai['verdict']}")
    lines.append(lead)
    if pb.get("tape_verdict"):
        lines.append(str(pb["tape_verdict"]))
    if t_str:
        lines.append(f"映射：{t_str}")
    if pb.get("execution_rule"):
        lines.append(f"节奏：{str(pb['execution_rule'])[:100]}")
    return title, subtitle, "\n".join(lines)


def dispatch_alerts_for_events(events: list[dict[str, object]]) -> int:
    """Send notifications across all enabled multi-platform Ops endpoints for newly ingested T0/T1 events."""
    if not events:
        return 0
    init_event_radar_db()
    _sync_legacy_system_channel_if_needed()
    sent_count = 0
    with get_db() as db:
        endpoints = db.execute(
            "SELECT id, owner, provider, endpoint_secret, min_tier, bark_level, notify_watchlist "
            "FROM radar_push_endpoints WHERE enabled=1 AND endpoint_secret != ''"
        ).fetchall()
        legacy_channels = db.execute(
            "SELECT username, provider, endpoint_secret, min_tier, bark_level, notify_watchlist "
            "FROM user_radar_channels WHERE enabled=1 AND endpoint_secret != '' AND username != ?",
            [SYSTEM_OWNER],
        ).fetchall()
        if not endpoints and not legacy_channels:
            return 0
        all_watch_rows = db.execute("SELECT DISTINCT ticker FROM user_watchlist").fetchall()
        global_watch = {str(r["ticker"]) for r in all_watch_rows}
        watchlists: dict[str, set[str]] = {SYSTEM_OWNER: global_watch}
        for ch in legacy_channels:
            u = ch["username"]
            rows = db.execute("SELECT ticker FROM user_watchlist WHERE username=?", [u]).fetchall()
            watchlists[u] = {str(r["ticker"]) for r in rows}

    for event in events:
        tier = str(event.get("tier") or "T2")
        if tier not in {"T0", "T1"}:
            continue
        ev_tickers = set(event.get("tickers") or [])
        title, subtitle, body = format_event_push_copy(event)

        for ep in endpoints:
            owner = ep["owner"]
            wset = global_watch if owner == SYSTEM_OWNER else watchlists.get(owner, global_watch)
            wants_tier = tier == "T0" or ep["min_tier"] == "T1"
            hits_watch = bool(ep["notify_watchlist"]) and bool(ev_tickers & wset)
            if not (wants_tier or hits_watch):
                continue
            ep_key = f"ep:{ep['id']}"
            with get_db() as db:
                if db.execute(
                    "SELECT 1 FROM radar_endpoint_deliveries WHERE event_id=? AND endpoint_key=?",
                    [event["id"], ep_key],
                ).fetchone():
                    continue
            ok, status_text = send_push_notification(
                ep["provider"],
                ep["endpoint_secret"],
                title=title,
                subtitle=subtitle,
                body=body,
                bark_level=ep["bark_level"],
            )
            now_ts = time.time()
            with get_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO radar_endpoint_deliveries(event_id, endpoint_key, status, sent_at) "
                    "VALUES(?,?,?,?)",
                    [event["id"], ep_key, status_text, now_ts],
                )
                db.execute(
                    "UPDATE radar_push_endpoints SET last_sent_at=?, last_status=? WHERE id=?",
                    [now_ts, status_text, ep["id"]],
                )
            if ok:
                sent_count += 1

        for ch in legacy_channels:
            u = ch["username"]
            wants_tier = tier == "T0" or ch["min_tier"] == "T1"
            hits_watch = bool(ch["notify_watchlist"]) and bool(ev_tickers & watchlists.get(u, set()))
            if not (wants_tier or hits_watch):
                continue
            with get_db() as db:
                if db.execute(
                    "SELECT 1 FROM radar_deliveries WHERE event_id=? AND username=?", [event["id"], u]
                ).fetchone():
                    continue
            ok, status_text = send_push_notification(
                ch["provider"],
                ch["endpoint_secret"],
                title=title,
                subtitle=subtitle,
                body=body,
                bark_level=ch["bark_level"],
            )
            now_ts = time.time()
            with get_db() as db:
                db.execute(
                    "INSERT OR IGNORE INTO radar_deliveries(event_id, username, status, sent_at) VALUES(?,?,?,?)",
                    [event["id"], u, status_text, now_ts],
                )
                db.execute(
                    "UPDATE user_radar_channels SET last_sent_at=?, last_status=? WHERE username=?",
                    [now_ts, status_text, u],
                )
            if ok:
                sent_count += 1
    return sent_count


def poll_once() -> dict[str, object]:
    """Parallel multi-region poll (CN + Global primary wires + live tape), auto-Gemini funnel & Bark push."""
    raw_items: list[dict[str, object]] = []
    sources_ok: list[str] = []

    def _job_cls():
        j = _fetch_json_public(_CLS_URL, timeout=4.5)
        return ("cls", _parse_cls_rows(j) if j else [])

    def _job_sina():
        j = _fetch_json_public(_SINA_URL, timeout=4.5)
        return ("sina", _parse_sina_rows(j) if j else [])

    def _job_em():
        j = _fetch_json_public(_EASTMONEY_URL, timeout=4.5)
        return ("eastmoney", _parse_eastmoney_rows(j) if j else [])

    def _job_fed():
        b = _fetch_bytes_public(_FED_RSS_URL, timeout=4.5)
        return (
            "fed",
            _parse_rss_feed(b, source_label="美联储官方 (Fed)", prefix="fed", limit=10) if b else [],
        )

    def _job_cnbc():
        b1 = _fetch_bytes_public(_CNBC_WORLD_RSS_URL, timeout=4.5)
        b2 = _fetch_bytes_public(_CNBC_TECH_RSS_URL, timeout=4.5)
        rows = []
        if b1:
            rows.extend(_parse_rss_feed(b1, source_label="CNBC 全球宏观", prefix="cnbcw", limit=12))
        if b2:
            rows.extend(_parse_rss_feed(b2, source_label="CNBC 全球科技链", prefix="cnbct", limit=12))
        return ("cnbc", rows)

    def _job_sec():
        b = _fetch_bytes_public(
            _SEC_8K_ATOM_URL,
            timeout=4.5,
            headers={"User-Agent": "PrismResearchWorkbench/1.0 (contact@prism.local)"},
        )
        return ("sec8k", _parse_sec_8k_atom(b, limit=20) if b else [])

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(fn) for fn in (_job_cls, _job_sina, _job_em, _job_fed, _job_cnbc, _job_sec)]
        tape_fut = pool.submit(fetch_cross_market_tape, force=True)
        for f in futs:
            try:
                name, rows = f.result(timeout=6.0)
                if rows:
                    sources_ok.append(name)
                    raw_items.extend(rows)
            except Exception:
                continue
        try:
            tape = tape_fut.result(timeout=4.5)
        except Exception:
            tape = []

    raw_items.sort(key=lambda item: float(item.get("created_at") or 0.0))
    created = ingest_raw_items(raw_items, tape=tape)

    fresh_cutoff = time.time() - 25 * 60
    fresh_alerts = [ev for ev in created if float(ev.get("created_at") or 0.0) >= fresh_cutoff]

    # Funnel Layer 2: Auto-trigger Gemini + Google Search on qualifying fresh alerts (capped by daily_limit)
    _auto_enrich_fresh_alerts(fresh_alerts)

    # Scheduled 08:45 / 12:45 Global Battle Briefing
    _maybe_run_scheduled_briefing()

    # Funnel Layer 3: Push enriched playbook to iOS Bark / allowlisted mobile webhooks
    pushed = dispatch_alerts_for_events(fresh_alerts)
    win = detect_trading_window()
    _last_poll_meta.update(
        {
            "last_poll_at": int(time.time()),
            "last_poll_ok": bool(sources_ok),
            "sources_ok": sources_ok,
            "events_ingested": len(created),
            "pushed": pushed,
            "interval_seconds": win["cadence_seconds"],
            "window_tag": win["tag"],
        }
    )
    return dict(_last_poll_meta)


def list_radar_feed(username: str, *, limit: int = 50, tier: str = "all") -> dict[str, object]:
    """Return personalized Global Event Radar feed, Cross-Asset Tape, Briefing, Push & Gemini status."""
    init_event_radar_db()
    limit = max(1, min(100, int(limit)))
    with get_db() as db:
        watch_rows = db.execute("SELECT ticker FROM user_watchlist WHERE username=?", [username]).fetchall()
        watch_set = {str(r["ticker"]) for r in watch_rows}
        if tier in {"T0", "T1", "T2"}:
            rows = db.execute(
                "SELECT * FROM radar_events WHERE tier=? ORDER BY created_at DESC LIMIT ?",
                [tier, limit],
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM radar_events ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()

    events: list[dict[str, object]] = []
    for row in rows:
        try:
            pb = json.loads(row["playbook_json"] or "{}")
        except Exception:
            pb = {}
        try:
            tickers = json.loads(row["tickers_json"] or "[]")
        except Exception:
            tickers = []
        try:
            futures = json.loads(row["futures_json"] or "[]")
        except Exception:
            futures = []
        watch_hits = [t for t in tickers if t in watch_set]
        region = str(
            pb.get("region") or ("GLOBAL" if str(row["id"]).startswith(("fed-", "cnbc", "sec8k-")) else "CN")
        )
        events.append(
            {
                "id": row["id"],
                "source": row["source"],
                "region": region,
                "tier": row["tier"],
                "score": row["score"],
                "theme": row["theme"],
                "title": row["title"],
                "body": row["body"],
                "url": row["url"],
                "published_at": row["published_at"],
                "created_at": row["created_at"],
                "echo_count": row["echo_count"],
                "window_tag": row["window_tag"],
                "playbook": pb,
                "tickers": tickers,
                "futures": futures,
                "watchlist_hits": watch_hits,
            }
        )

    now_ts = time.time()
    headline = next(
        (ev for ev in events if ev["tier"] == "T0" and now_ts - float(ev["created_at"]) <= 18 * 3600),
        None,
    ) or next(
        (ev for ev in events if ev["tier"] == "T1" and now_ts - float(ev["created_at"]) <= 6 * 3600),
        None,
    )
    win = detect_trading_window()
    sys_ai = get_user_ai_config(SYSTEM_OWNER)
    return {
        "events": events,
        "headline": headline,
        "counts": {
            "T0": sum(1 for ev in events if ev["tier"] == "T0"),
            "T1": sum(1 for ev in events if ev["tier"] == "T1"),
            "T2": sum(1 for ev in events if ev["tier"] == "T2"),
            "global": sum(1 for ev in events if ev["region"] == "GLOBAL"),
            "total": len(events),
        },
        "tape": fetch_cross_market_tape(),
        "briefing": get_latest_briefing(),
        "window": win,
        "collector": dict(_last_poll_meta),
        "ai_ready": bool(sys_ai["has_key"] and sys_ai["enabled"]),
    }


def get_radar_ops_summary() -> dict[str, object]:
    """Admin/Ops-only full control-plane telemetry & masked configuration."""
    init_event_radar_db()
    with get_db() as db:
        total = db.execute("SELECT COUNT(*) FROM radar_events").fetchone()[0]
        t0 = db.execute("SELECT COUNT(*) FROM radar_events WHERE tier='T0'").fetchone()[0]
        t1 = db.execute("SELECT COUNT(*) FROM radar_events WHERE tier='T1'").fetchone()[0]
    return {
        "collector": dict(_last_poll_meta),
        "window": detect_trading_window(),
        "counts": {"total": int(total), "T0": int(t0), "T1": int(t1)},
        "briefing": get_latest_briefing(),
        "ai": get_user_ai_config(SYSTEM_OWNER),
        "push": get_user_push_config(SYSTEM_OWNER),
        "push_channels": list_ops_push_channels(SYSTEM_OWNER),
        "push_providers": list_push_providers(),
    }


async def run_radar_collector_loop(manager: Manager) -> None:
    """Cooperative background sentinel owned by ``work_lifecycle``."""
    init_event_radar_db()
    await asyncio.sleep(4)
    while True:
        cadence = int(detect_trading_window()["cadence_seconds"])
        try:
            lease = manager.admit("auxiliary", persist_required=False)
        except LifecycleError:
            await asyncio.sleep(5)
            continue
        try:
            with lease:
                await manager.run_blocking("auxiliary", poll_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("event_radar_poll_failed: %s", type(exc).__name__)
        await asyncio.sleep(max(15, cadence))
