"""Prism V4.1 tests: Event Radar classifier, dedup, SSRF-proof Bark push, topbar chrome & Option C brand."""

from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from server import event_radar, public_network

SHANGHAI = ZoneInfo("Asia/Shanghai")


def test_state_council_midday_flash_scores_t0_and_builds_midday_playbook():
    title = "国常会：加力发挥“国家队”稳市作用 进一步优化中央汇金公司的类“平准基金”功能"
    body = (
        "财联社11月27日电，国务院常务会议指出，持续完善资本市场稳市机制，"
        "实施好存量政策，谋划出台增量政策，加大分红回购力度，加力发挥“国家队”稳市作用，"
        "进一步优化中央汇金公司的类“平准基金”功能，完善保险、社保等中长期资金入市政策。"
    )
    classified = event_radar.classify_event(title, body, cls_level="A")
    assert classified["tier"] == "T0"
    assert classified["score"] >= 85
    assert classified["theme"] == "宏观稳市与宽基权重"

    midday = datetime(2026, 11, 27, 12, 24, 51, tzinfo=SHANGHAI)
    playbook = event_radar.build_playbook(title, body, classified, now=midday)
    assert "午休黄金窗" in playbook["window_tag"]
    assert "36" in playbook["window_tag"] or "35" in playbook["window_tag"]
    tickers = [t["ticker"] for t in playbook["targets"]]
    assert "510300" in tickers and "601138" in tickers
    assert playbook["delta_points"]


def test_commentary_and_midday_recap_noise_demoted_out_of_t0():
    noise_title = "午评：沪指午后直线拉升，券商点评国常会平准基金稳市机制"
    classified = event_radar.classify_event(noise_title, noise_title, cls_level="B")
    assert classified["tier"] != "T0"

    digest_title = "财联社11月27日晚间新闻精选"
    digest_body = "国常会：加力发挥国家队稳市作用，进一步优化中央汇金类平准基金功能。"
    digest_classified = event_radar.classify_event(digest_title, digest_body, cls_level="A")
    assert digest_classified["tier"] == "T2"


def test_six_hour_topic_cluster_deduplicates_echoes():
    t0 = datetime(2026, 11, 27, 12, 24, 51, tzinfo=SHANGHAI)
    first = {
        "id": "cls-10001",
        "source": "财联社电报",
        "title": "国常会：加力发挥“国家队”稳市作用 优化中央汇金类“平准基金”功能",
        "body": "国务院常务会议部署资本市场稳市机制与类平准基金功能。",
        "url": "https://www.cls.cn/detail/10001",
        "published_at": t0.isoformat(timespec="seconds"),
        "created_at": t0.timestamp(),
        "cls_level": "A",
    }
    echo = {
        **first,
        "id": "sina-10002",
        "source": "新浪快讯",
        "created_at": t0.timestamp() + 180,
    }
    created_1 = event_radar.ingest_raw_items([first, echo], now=t0)
    assert len(created_1) == 1
    feed = event_radar.list_radar_feed("testuser", limit=10)
    matched = [ev for ev in feed["events"] if ev["id"] == "cls-10001"]
    assert matched and matched[0]["echo_count"] == 2


@pytest.mark.parametrize(
    "provider,bad",
    [
        ("ntfy", "http://127.0.0.1:8088/my_topic"),
        ("ntfy", "https://evil.example.com/my_topic"),
        ("pushplus", "https://evil.example.com/send?token=abcdef1234567890abcd"),
        ("serverchan", "https://evil.example.com/SCT1234567890.send"),
        ("bark", "http://127.0.0.1:8088/api/admin"),
        ("bark", "https://169.254.169.254/latest/meta-data/"),
        ("bark", "https://evil.example.com/mykey123456"),
        ("wecom", "https://evil.example.com/cgi-bin/webhook/send?key=123456789012"),
        ("feishu", "https://evil.example.com/open-apis/bot/v2/hook/123456789012"),
        ("dingtalk", "https://evil.example.com/robot/send?access_token=123456789012"),
        (
            "telegram",
            "https://evil.example.com/bot123456:AAH_valid_looking_token_12345/sendMessage?chat_id=123456",
        ),
    ],
)
def test_push_endpoint_rejects_ssrf_and_non_allowlisted_hosts(provider, bad):
    with pytest.raises(ValueError):
        event_radar.normalize_push_secret(provider, bad)


def test_cross_platform_push_secret_normalization_accepts_all_official_providers():
    assert event_radar.normalize_push_secret("ntfy", "prism_android_topic_01") == "prism_android_topic_01"
    assert (
        event_radar.normalize_push_secret("ntfy", "https://ntfy.sh/prism_android_topic_01")
        == "prism_android_topic_01"
    )
    assert (
        event_radar.normalize_push_secret(
            "pushplus", "https://www.pushplus.plus/send?token=1234567890abcdef1234567890abcdef"
        )
        == "1234567890abcdef1234567890abcdef"
    )
    assert (
        event_radar.normalize_push_secret("serverchan", "https://sctapi.ftqq.com/SCT1234567890abcdef.send")
        == "SCT1234567890abcdef"
    )
    assert event_radar.normalize_push_secret("bark", "MyDeviceKey_98765432") == "MyDeviceKey_98765432"
    assert (
        event_radar.normalize_push_secret("bark", "https://api.day.app/MyDeviceKey_98765432/")
        == "MyDeviceKey_98765432"
    )
    assert (
        event_radar.normalize_push_secret(
            "telegram", "123456789:AAH_valid_bot_token_0123456789|-100987654321"
        )
        == "123456789:AAH_valid_bot_token_0123456789@-100987654321"
    )


def _ops_headers(monkeypatch, tmp_path) -> dict[str, str]:
    import os

    from server.auth import hash_password
    from server.database import create_user, user_exists

    monkeypatch.setattr("server.auth.ADMIN_USERS", {"admin"})
    if not user_exists("admin"):
        create_user("admin", hash_password("adminpass12345"))
    secret = "ops_gateway_test_secret_0123456789abcdef"
    token_file = tmp_path / "ops_gateway_token"
    token_file.write_text(secret + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    monkeypatch.setenv("PRISM_OPS_GATEWAY_TOKEN_FILE", str(token_file))
    return {
        "X-Prism-Ops-Token": secret,
        "X-Prism-Ops-Principal": "ops-operator",
        "X-Requested-With": "XMLHttpRequest",
    }


@pytest.mark.asyncio
async def test_radar_push_config_is_ops_only_masks_secret_and_sends_bark_timesensitive_test(
    client, auth_cookies, monkeypatch, tmp_path
):
    sent_requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_requests.append(request)
        return httpx.Response(200, json={"code": 200, "message": "success"})

    class Adapter:
        def sync_transport(self):
            return httpx.MockTransport(handler)

        def async_transport(self):
            return httpx.MockTransport(handler)

        def connect_tls(self, *args):
            raise AssertionError("not used")

    monkeypatch.setattr(public_network, "_network", None)
    public_network.install(Adapter())

    # Regular user cookies MUST be rejected on Ops radar endpoints (403)!
    denied = await client.get(
        "/api/admin/radar-ops", cookies=auth_cookies, headers={"X-Requested-With": "XMLHttpRequest"}
    )
    assert denied.status_code == 403

    ops_hdrs = _ops_headers(monkeypatch, tmp_path)
    save_resp = await client.put(
        "/api/admin/radar-ops/push",
        headers=ops_hdrs,
        json={
            "provider": "bark",
            "secret": "https://api.day.app/BarkTestKey_12345678",
            "enabled": True,
            "min_tier": "T0",
            "bark_level": "timeSensitive",
            "notify_watchlist": True,
        },
    )
    assert save_resp.status_code == 200, save_resp.text
    saved = save_resp.json()
    assert saved["has_secret"] is True
    assert "BarkTestKey_12345678" not in save_resp.text
    assert saved["masked_secret"].endswith("5678")

    test_resp = await client.post("/api/admin/radar-ops/push/test", headers=ops_hdrs)
    assert test_resp.status_code == 200, test_resp.text
    assert len(sent_requests) == 1
    assert str(sent_requests[0].url) == "https://api.day.app/push"
    import json

    body = json.loads(sent_requests[0].content.decode("utf-8"))
    assert body["device_key"] == "BarkTestKey_12345678"
    assert body["level"] == "timeSensitive"
    assert body["group"] == "Prism 风向雷达"

    # Add Android ntfy + Personal WeChat PushPlus channels simultaneously & broadcast test!
    c_ntfy = await client.post(
        "/api/admin/radar-ops/push/channels",
        headers=ops_hdrs,
        json={
            "label": "主力安卓手机",
            "provider": "ntfy",
            "secret": "prism_android_topic_99",
            "bark_level": "urgent",
            "min_tier": "T0",
            "enabled": True,
            "notify_watchlist": True,
        },
    )
    assert c_ntfy.status_code == 200, c_ntfy.text
    assert "prism_android_topic_99" not in c_ntfy.text

    c_wx = await client.post(
        "/api/admin/radar-ops/push/channels",
        headers=ops_hdrs,
        json={
            "label": "个人微信免装App",
            "provider": "pushplus",
            "secret": "abcdef0123456789abcdef0123456789",
            "bark_level": "markdown",
            "min_tier": "T0",
            "enabled": True,
            "notify_watchlist": True,
        },
    )
    assert c_wx.status_code == 200, c_wx.text
    assert len(c_wx.json()["push_channels"]) == 3

    sent_requests.clear()
    multi_test = await client.post("/api/admin/radar-ops/push/test", headers=ops_hdrs)
    assert multi_test.status_code == 200, multi_test.text
    assert multi_test.json()["delivered"] == 3
    hosts = [r.url.host for r in sent_requests]
    assert hosts == ["api.day.app", "ntfy.sh", "www.pushplus.plus"]
    ntfy_payload = json.loads(sent_requests[1].content.decode("utf-8"))
    assert ntfy_payload["topic"] == "prism_android_topic_99"
    assert ntfy_payload["priority"] == 5


def test_top_level_shell_chrome_and_option_c_brand_contract():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    app_html = (root / "templates/app.html").read_text(encoding="utf-8")
    futures_html = (root / "templates/futures.html").read_text(encoding="utf-8")
    favicon_svg = (root / "static/favicon.svg").read_text(encoding="utf-8")
    mark_svg = (root / "static/prism-mark.svg").read_text(encoding="utf-8")

    assert 'class="sidebar-foot"' not in app_html
    for html in (app_html, futures_html):
        assert "favicon.svg?v=153" in html
        assert "radarTopbarPill" in html
        assert "topbar-account-btn" in html
        assert 'id="versionTag"' in html
    assert "#0E1B15" in favicon_svg
    assert 'id="mark"' in mark_svg and "fill-opacity" in mark_svg


def test_bilingual_global_feeds_fed_and_sec8k_and_xxe_guard():
    fed_cls = event_radar.classify_event(
        "Federal Reserve issues FOMC statement: lowers target range by 50 basis points",
        "The Federal Open Market Committee decided today to lower the target range for the federal funds rate.",
        cls_level="A",
        region="GLOBAL",
    )
    assert fed_cls["tier"] == "T0"
    assert fed_cls["region"] == "GLOBAL"

    nvda_cls = event_radar.classify_event(
        "8-K - NVIDIA CORP (0001045810) (Filer)",
        "Item 2.02 Results of Operations: Record AI server and Blackwell data center revenue beats estimates.",
        cls_level="B",
        region="GLOBAL",
    )
    assert nvda_cls["tier"] == "T1"
    assert nvda_cls["theme"] == "AI算力与科技硬件"

    # XXE entity payload must be rejected safely
    xxe_xml = b'<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&xxe;</title></item></channel></rss>'
    assert event_radar._parse_rss_feed(xxe_xml, source_label="Fed", prefix="fed") == []

    # Valid SEC 8-K Atom filtered to AI/tech bellwethers
    atom_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>8-K - NVIDIA CORP (0001045810) (Filer)</title>
        <link href="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000099/nvda-8k.htm"/>
        <summary>Item 2.02 Record data center demand</summary>
        <updated>2026-11-27T14:00:00Z</updated>
      </entry>
      <entry>
        <title>8-K - RANDOM SMALL BANCORP (0009999999)</title>
        <link href="https://www.sec.gov/Archives/edgar/data/999/small.htm"/>
        <summary>Routine bylaw amendment</summary>
        <updated>2026-11-27T14:00:00Z</updated>
      </entry>
    </feed>"""
    parsed_8k = event_radar._parse_sec_8k_atom(atom_xml)
    assert len(parsed_8k) == 1
    assert parsed_8k[0]["region"] == "GLOBAL"
    assert "NVIDIA" in str(parsed_8k[0]["title"])


@pytest.mark.asyncio
async def test_gemini_grounding_ops_only_header_auth_key_masking_and_daily_circuit_breaker(
    client, auth_cookies, monkeypatch, tmp_path
):
    import json

    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            payload = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "cn_title": "美联储紧急降息50基点超预期",
                                            "verdict": "官方一手确认·大幅超预期",
                                            "delta_points": [
                                                "降息幅度50bp超市场一致预期25bp",
                                                "离岸人民币与黄金同步急升",
                                            ],
                                            "execution_rule": "盯次日09:20宽基与黄金有色高开承接",
                                            "invalidation_rule": "若高开超6%且放量回落破均线则止盈",
                                        },
                                        ensure_ascii=False,
                                    )
                                }
                            ]
                        },
                        "groundingMetadata": {
                            "groundingChunks": [
                                {
                                    "web": {
                                        "uri": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20261127a.htm",
                                        "title": "Federal Reserve Board - FOMC statement",
                                    }
                                }
                            ]
                        },
                    }
                ]
            }
            return httpx.Response(200, json=payload)
        return httpx.Response(200, text="")

    class Adapter:
        def sync_transport(self):
            return httpx.MockTransport(handler)

        def async_transport(self):
            return httpx.MockTransport(handler)

        def connect_tls(self, *args):
            raise AssertionError("not used")

    monkeypatch.setattr(public_network, "_network", None)
    public_network.install(Adapter())

    # Seed one global event
    event_radar.ingest_raw_items(
        [
            {
                "id": "fed-test-50bp",
                "source": "美联储官方 (Fed)",
                "region": "GLOBAL",
                "title": "Federal Reserve lowers target range by 50 basis points",
                "body": "The Committee decided to lower the target range for the federal funds rate by 1/2 percentage point.",
                "url": "https://www.federalreserve.gov/newsevents",
                "published_at": "2026-11-27T22:00:00+08:00",
                "created_at": 1795788000.0,
                "cls_level": "A",
            }
        ]
    )

    ops_hdrs = _ops_headers(monkeypatch, tmp_path)
    user_hdrs = {"X-Requested-With": "XMLHttpRequest"}
    synthetic_key = "AIzaSySyntheticTestKeyForCircuitBreaker99"

    # Regular user feed does NOT expose ai/push credentials or quotas
    feed0 = await client.get("/api/radar/feed", cookies=auth_cookies)
    assert feed0.status_code == 200
    assert "ai" not in feed0.json() and "push" not in feed0.json()
    assert feed0.json()["ai_ready"] is False

    put_ai = await client.put(
        "/api/admin/radar-ops/ai",
        headers=ops_hdrs,
        json={
            "api_key": synthetic_key,
            "model": "gemini-2.5-flash",
            "enabled": True,
            "use_google_search": True,
            "auto_enrich": True,
            "auto_tier": "T0_WATCH",
            "auto_briefing": True,
            "daily_limit": 2,
        },
    )
    assert put_ai.status_code == 200, put_ai.text
    ai_body = put_ai.json()
    assert ai_body["has_key"] is True
    assert synthetic_key not in put_ai.text
    assert ai_body["daily_limit"] == 2

    # Call 1 (via Ops): Test Gemini connection
    r1 = await client.post("/api/admin/radar-ops/ai/test", headers=ops_hdrs)
    assert r1.status_code == 200, r1.text
    assert r1.json()["grounded"] is True

    # Verify API key is ONLY in x-goog-api-key header, NEVER in URL!
    gem_reqs = [r for r in captured_requests if r.url.host == "generativelanguage.googleapis.com"]
    assert gem_reqs
    assert synthetic_key not in str(gem_reqs[0].url)
    assert gem_reqs[0].headers.get("x-goog-api-key") == synthetic_key
    req_json = json.loads(gem_reqs[0].content.decode("utf-8"))
    assert {"google_search": {}} in req_json.get("tools", [])

    # Call 2 (User consuming platform-configured engine): Enrich the Fed event
    r2 = await client.post("/api/radar/events/fed-test-50bp/enrich", cookies=auth_cookies, headers=user_hdrs)
    assert r2.status_code == 200, r2.text
    assert r2.json()["enrichment"]["cn_title"] == "美联储紧急降息50基点超预期"

    # Call 3: Must hit hard daily circuit breaker (429)!
    r3 = await client.post("/api/admin/radar-ops/ai/test", headers=ops_hdrs)
    assert r3.status_code == 429

    # Verify Ultra credit ledger & billing-ops summary endpoints
    rops = await client.get("/api/admin/radar-ops", headers=ops_hdrs)
    assert rops.status_code == 200
    bill = rops.json()["ai"]["billing"]
    assert bill["monthly_credit_usd"] == 40.0
    assert bill["prism_month_calls"] == 2
    assert bill["out_of_pocket_usd"] == 0.0
    assert bill["credit_remaining_usd"] <= 40.0

    bops = await client.get("/api/admin/billing-ops/summary", headers=ops_hdrs)
    assert bops.status_code == 200
    bdata = bops.json()
    assert any(c["id"] == "deepseek-flash" for c in bdata["catalog"])
    assert any(c["id"] == "minimax-cn-m3.1-flash" for c in bdata["catalog"])
    assert {s["slot"] for s in bdata["provider_slots"]} == {
        "deepseek",
        "minimax_cn",
        "minimax",
        "openai",
    }

    # Verify smart Gemini API key parser accepts AQ. keys, curl snippets, invisible chars & rejects "..." masks
    assert (
        event_radar.normalize_gemini_api_key(
            '\u200bcurl -H "x-goog-api-key: AIzaSy_TestCurlExtract_1234567890" https://x'
        )
        == "AIzaSy_TestCurlExtract_1234567890"
    )
    assert (
        event_radar.normalize_gemini_api_key("AQ.Ab8RN6L_NewGoogleFormatKey+2026/xyz=")
        == "AQ.Ab8RN6L_NewGoogleFormatKey+2026/xyz="
    )
    # AQ. AI Studio keys route to generativelanguage.googleapis.com with x-goog-api-key header (never in URL)
    captured_requests.clear()
    event_radar.call_gemini_grounded(
        api_key="AQ.Ab8RN6L_NewGoogleFormatKey+2026/xyz=",
        model="gemini-3.5-flash-lite",
        prompt="ping",
        use_google_search=False,
    )
    assert captured_requests[0].url.host == "generativelanguage.googleapis.com"
    assert captured_requests[0].headers.get("x-goog-api-key") == "AQ.Ab8RN6L_NewGoogleFormatKey+2026/xyz="
    with pytest.raises(ValueError, match="masked_gemini_api_key"):
        event_radar.normalize_gemini_api_key("AIzaSy...abcd")

    # Verify Ops hot-reload provider credential slot (strictly isolated per provider, never returns raw key)
    put_slot = await client.put(
        "/api/admin/billing-ops/providers/minimax_cn",
        headers=ops_hdrs,
        json={"api_key": "sk-cn-synthetic-ops-key-99887766"},
    )
    assert put_slot.status_code == 200, put_slot.text
    assert "sk-cn-synthetic-ops-key-99887766" not in put_slot.text
    cn_slot = next(s for s in put_slot.json()["provider_slots"] if s["slot"] == "minimax_cn")
    assert cn_slot["configured"] is True
    assert cn_slot["source"] == "ops"
    assert cn_slot["masked_key"].endswith("7766")
