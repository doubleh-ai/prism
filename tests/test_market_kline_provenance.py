"""Run actual market source in disposable copies with synthetic provider bodies.

Provider bodies below are synthetic fixtures. These tests exercise actual cache,
aggregation, auth, ASGI, owned capability and UI code, never public HTTP or models.
"""

import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "_market_provenance_runtime"
SINA = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


def copy_sources(destination):
    for source in (ROOT / "server").rglob("*.py"):
        if "__pycache__" not in source.parts:
            target = destination / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
    for name in ("static/js/app.market.js", "static/js/app.workbench.js", "static/css/app.css"):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    destination = tmp_path / "runtime"
    copy_sources(destination)
    before = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("server/market/models.py", "server/market/router.py", "static/js/app.market.js")
    }
    monkeypatch.setenv("STOCK_DATA_DIR", str(tmp_path / "data"))
    package = types.ModuleType(PREFIX)
    package.__path__ = [str(destination / "server")]
    monkeypatch.setitem(sys.modules, PREFIX, package)
    modules = {}
    for name in (
        "market.models",
        "market.chain",
        "market.cache",
        "market.resample",
        "market.tencent_provider",
        "market.router",
        "market.factory",
        "capabilities",
        "database",
    ):
        modules[name.rsplit(".", 1)[-1]] = importlib.import_module(PREFIX + "." + name)
    modules["database"].init_db()
    try:
        yield types.SimpleNamespace(**modules, directory=destination)
    finally:
        modules["database"].reset_connection()
        for name in list(sys.modules):
            if name.startswith(PREFIX + "."):
                del sys.modules[name]
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
            for name, digest in before.items()
        )


def bar(runtime, date="2026-10-19", *, provenance=None, opening=10, close=11, volume=100):
    from datetime import datetime, timedelta, timezone

    timestamp = int(datetime.fromisoformat(date).replace(tzinfo=timezone(timedelta(hours=8))).timestamp())
    return runtime.models.KLine(
        timestamp=timestamp,
        open=opening,
        high=max(opening, close) + 1,
        low=min(opening, close) - 1,
        close=close,
        volume=volume,
        turnover=1000,
        provenance=provenance,
    )


def recorded(runtime, **changes):
    return runtime.models.KLineProvenance(
        **{
            "source": "sina",
            "source_url": SINA + "?symbol=sh600519",
            "adapter": "tencent",
            "retrieved_at": 1792515000,
            **changes,
        }
    )


def make_provider(bars, name="tencent"):
    class Provider:
        calls = 0
        failing = False

        async def get_kline(self, ticker, period="1d", limit=120):
            self.calls += 1
            if self.failing:
                raise RuntimeError("synthetic provider unavailable")
            return bars

    provider = Provider()
    provider.name = name
    return provider


def cache_row(runtime, key):
    with runtime.database.get_db() as db:
        return dict(db.execute("SELECT * FROM market_cache WHERE cache_key=?", [key]).fetchone())


def expire(runtime, key):
    with runtime.database.get_db() as db:
        db.execute("UPDATE market_cache SET expires_at=? WHERE cache_key=?", [time.time() - 1, key])


@pytest.mark.parametrize("period", ["1d", "1w", "1m"])
async def test_sina_endpoint_is_origin_and_actual_response_records_acquisition(runtime, monkeypatch, period):
    provider = runtime.tencent_provider.TencentProvider()
    requests = []
    body = json.dumps(
        [
            {"day": "2026-10-19", "open": "10", "high": "13", "low": "9", "close": "12", "volume": "100"},
            {"day": "2026-10-20", "open": "12", "high": "15", "low": "11", "close": "14", "volume": "120"},
        ]
    )

    def fetch(request, timeout):
        requests.append(request.full_url)
        assert timeout == 12
        return body

    monkeypatch.setattr(provider, "_fetch_kline_sync", fetch)
    monkeypatch.setattr(runtime.tencent_provider, "time", types.SimpleNamespace(time=lambda: 1792600100))
    monkeypatch.setattr(runtime.chain, "time", types.SimpleNamespace(time=lambda: 1792600999))
    cache = runtime.cache.MarketCache()
    chain = runtime.chain.ProviderChain([provider], cache)
    bars, stale = await chain.get_kline("600519", period, 10)
    facts = runtime.models.describe_kline(bars, stale=stale)
    assert len(requests) == 1 and requests[0].startswith(SINA + "?symbol=sh600519&scale=240&ma=no&datalen=")
    assert facts["source"] == "sina" and facts["adapter"] == "tencent"
    assert facts["source_url"] == requests[0]
    assert facts["retrieved_at"] == 1792600100 != facts["data_timestamp"]
    assert facts["timestamp_kind"] == "bar_date" and facts["period_completeness"] == "unknown"
    assert facts["data_timestamp"] == bar(runtime, "2026-10-20").timestamp
    again, again_stale = await chain.get_kline("600519", period, 10)
    assert [b.model_dump() for b in again] == [b.model_dump() for b in bars]
    assert again_stale is False and len(requests) == 1


@pytest.mark.parametrize("adapter", ["akshare", "baostock", "tencent"])
async def test_chain_success_stamps_acquisition_but_never_guesses_origin(runtime, monkeypatch, adapter):
    provider = make_provider([bar(runtime)], adapter)
    monkeypatch.setattr(runtime.chain, "time", types.SimpleNamespace(time=lambda: 1792600555))
    chain = runtime.chain.ProviderChain([provider], runtime.cache.MarketCache())
    bars, stale = await chain.get_kline("600519", "1d", 10)
    facts = runtime.models.describe_kline(bars, stale=stale)
    assert facts["source"] == "unknown" and facts["source_url"] is None
    assert facts["adapter"] == adapter and facts["retrieved_at"] == 1792600555
    assert bars[0].provenance.retrieved_at != bars[0].timestamp


@pytest.mark.parametrize("legacy", [True, False])
async def test_real_sqlite_cache_replay_and_expired_fallback_do_not_rewrite_facts(
    runtime, monkeypatch, legacy
):
    bars = [bar(runtime, provenance=None if legacy else recorded(runtime))]
    provider = make_provider(bars)
    provider.failing = True
    cache = runtime.cache.MarketCache()
    chain = runtime.chain.ProviderChain([provider], cache)
    key = chain._cache_key("kline-v2", "tencent", "600519", "1d", "10")
    payload = [item.model_dump(exclude={"provenance"}) if legacy else item.model_dump() for item in bars]
    cache.set(key, payload, ttl=300)
    before = cache_row(runtime, key)

    def no_acquisition():
        pytest.fail("cache replay fabricated acquisition time")

    monkeypatch.setattr(runtime.chain, "time", types.SimpleNamespace(time=no_acquisition))
    fresh, stale = await chain.get_kline("600519", "1d", 10)
    assert stale is False and provider.calls == 0
    assert cache_row(runtime, key) == before
    expire(runtime, key)
    expired_before = cache_row(runtime, key)
    old, stale = await chain.get_kline("600519", "1d", 10)
    assert stale is True and provider.calls == 1
    assert cache_row(runtime, key) == expired_before
    assert [item.model_dump() for item in old] == [item.model_dump() for item in fresh]
    facts = runtime.models.describe_kline(old, stale=stale)
    assert facts["retrieved_at"] == (None if legacy else 1792515000)
    assert facts["source"] == ("unknown" if legacy else "sina")
    assert facts["adapter"] == (None if legacy else "tencent")
    assert facts["source_url"] is None if legacy else facts["source_url"].startswith(SINA)
    assert facts["data_timestamp"] == bars[0].timestamp


@pytest.mark.parametrize(
    "period,dates", [("1w", ("2026-10-19", "2026-10-20")), ("1m", ("2026-10-01", "2026-10-20"))]
)
@pytest.mark.parametrize("metadata", ["same", "mixed", "legacy"])
def test_aggregation_preserves_ohlcv_and_last_observed_date_without_completeness_claim(
    runtime, period, dates, metadata
):
    first = recorded(runtime) if metadata != "legacy" else None
    second = recorded(runtime, retrieved_at=1792516000) if metadata == "mixed" else first
    bars = [
        bar(runtime, dates[1], provenance=second, opening=11, close=14, volume=200),
        bar(runtime, dates[0], provenance=first),
    ]
    result = runtime.resample.resample_kline(bars, period, 1)
    assert len(result) == 1
    combined = result[0]
    assert (
        combined.open,
        combined.high,
        combined.low,
        combined.close,
        combined.volume,
        combined.turnover,
    ) == (10, 15, 9, 14, 300, 2000)
    assert combined.timestamp == bars[0].timestamp
    facts = runtime.models.describe_kline(result, stale=False)
    assert facts["period_completeness"] == "unknown" and facts["timestamp_kind"] == "bar_date"
    assert facts["retrieved_at"] == (1792515000 if metadata == "same" else None)
    assert facts["source"] == ("sina" if metadata == "same" else "unknown")


def test_partial_mixed_and_empty_metadata_cannot_be_upgraded(runtime):
    one = bar(runtime, provenance=recorded(runtime))
    legacy = bar(runtime, "2026-10-20")
    unknown = bar(runtime, "2026-10-21", provenance=recorded(runtime, source="unknown"))
    for bars in ([], [one, legacy], [one, unknown]):
        facts = runtime.models.describe_kline(bars, stale=True)
        assert facts["source"] == "unknown" and facts["source_url"] is None
        assert facts["stale"] is True
    assert runtime.models.describe_kline([one, legacy], stale=False)["retrieved_at"] is None
    assert runtime.models.describe_kline([], stale=False)["data_timestamp"] is None


async def test_unavailable_provider_is_explicit_and_does_not_create_cache(runtime):
    provider = make_provider([])
    provider.failing = True
    chain = runtime.chain.ProviderChain([provider], runtime.cache.MarketCache())
    with pytest.raises(runtime.chain.NoProviderAvailable):
        await chain.get_kline("600519", "1d", 10)
    with runtime.database.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM market_cache").fetchone()[0] == 0


@pytest.mark.parametrize("bar_count", [1, 500])
async def test_real_protected_http_owned_capability_ledger_and_history_share_metadata(
    runtime, monkeypatch, bar_count, request
):
    limit = 500 if bar_count == 500 else 10
    first = bar(runtime, "2024-01-01", provenance=recorded(runtime))
    bars = [
        first.model_copy(update={"timestamp": first.timestamp + index * 86400}) for index in range(bar_count)
    ]
    provider = make_provider(bars)
    chain = runtime.chain.ProviderChain([provider], runtime.cache.MarketCache())
    monkeypatch.setattr(runtime.router, "get_chain", lambda: chain)
    monkeypatch.setattr(runtime.factory, "get_chain", lambda: chain)
    with runtime.database.get_db() as db:
        for user in ("owner", "other"):
            db.execute(
                "INSERT INTO users(username,password_hash) VALUES(?,?)", [user, "synthetic-unused-hash"]
            )
            db.execute("INSERT INTO conversations(id,username) VALUES(?,?)", [user + "-conv", user])
    owner = runtime.database.create_session("owner")
    other = runtime.database.create_session("other")
    parent = runtime.capabilities.get_manager().admit("agent", persist_required=False)
    request.addfinalizer(parent.release)
    request.addfinalizer(lambda: runtime.capabilities.revoke_run_token("provenance-run"))
    run = runtime.capabilities.issue_run_token("owner", "owner-conv", "provenance-run", parent_lease=parent)
    app = FastAPI()
    app.include_router(runtime.router.router)
    app.include_router(runtime.capabilities.router)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8088") as client:
        path = "/api/quote/600519/kline?period=1d&limit=" + str(limit)
        assert (await client.get(path)).status_code == 401 and provider.calls == 0
        client.cookies.set("token", owner)
        response = await client.get(path)
        assert response.status_code == 200 and provider.calls == 1
        api = response.json()
        request = {
            "name": "market_kline",
            "arguments": {"ticker": "600519", "period": "1d", "limit": limit},
            "request_id": "provenance-1",
        }
        result = await client.post(
            "/api/internal/capabilities/call", json=request, headers={"Authorization": "Bearer " + run}
        )
        assert result.status_code == 200 and result.json()["ok"] is True
        capability = result.json()["data"]
        assert len(capability["bars"]) == bar_count and "truncated" not in capability
        assert all("provenance" not in item for item in capability["bars"])
        assert len(json.dumps(capability, ensure_ascii=False)) < 120000
        for field in runtime.models.describe_kline(bars, stale=False):
            assert api[field] == capability[field]
        assert capability["bars"] == api["kline"] and provider.calls == 1
        call_id = result.json()["call"]["call_id"]
        history = await client.get("/api/capabilities/calls/" + call_id)
        assert history.status_code == 200 and history.json()["result"] == capability
        client.cookies.set("token", other)
        assert (await client.get("/api/capabilities/calls/" + call_id)).status_code == 404
        assert provider.calls == 1
        key = chain._cache_key("kline-v2", "tencent", "600519", "1d", str(limit))
        expire(runtime, key)
        provider.failing = True
        client.cookies.set("token", owner)
        fallback = await client.get(path)
        assert fallback.status_code == 200 and fallback.json()["stale"] is True
        assert fallback.json()["retrieved_at"] == api["retrieved_at"]
        assert (await client.get("/api/capabilities/calls/" + call_id)).json()["result"] == capability
        assert provider.calls == 2


NODE_TEST = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
global.App = {switchMode(){}};
global.esc = global.escAttr = value => String(value == null ? '' : value).replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]));
vm.runInThisContext(fs.readFileSync(process.argv[2]+'/static/js/app.market.js', 'utf8'));
const mode=process.argv[3], base={source:'sina',source_url:'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519',adapter:'tencent',retrieved_at:1792600100,data_timestamp:1792425600,stale:false};
if(mode==='known') {
 const html=App._klineProvenanceHtml(base);
 for (const text of ['新浪财经','平台直连','最后数据日期','采集时间','有效期内','不代表行情新鲜度','完整性','noopener noreferrer']) assert(html.includes(text));
 assert(!html.includes('Tencent')); assert(!html.includes('行情时间'));
} else if(mode==='legacy') {
 const html=App._klineProvenanceHtml({bars:[{timestamp:1792425600}],stale:true});
 for (const text of ['原始数据源未确认','采集时间</span>未记录','已过期，使用备用记录','2026']) assert(html.includes(text));
 assert(!html.includes('href=') && !html.includes('获取渠道'));
} else if(mode==='unsafe') {
 for(const url of ['javascript:alert(1)','https://money.finance.sina.com.cn.evil.invalid/','https://user@money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData','https://money.finance.sina.com.cn:444/quotes_service/api/json_v2.php/CN_MarketData.getKLineData']) assert(!App._klineProvenanceHtml({...base,source_url:url}).includes('href='));
 const html=App._klineProvenanceHtml({...base,source:'<img src=x onerror=alert(1)>',adapter:'__proto__',retrieved_at:Infinity,data_timestamp:'<script>'});
 assert(!html.includes('<img') && !html.includes('<script>') && !html.includes('href=') && html.includes('原始数据源未确认'));
} else if(mode==='channels') {
 for(const adapter of ['akshare','baostock']) {
  const html=App._klineProvenanceHtml({...base,source:'unknown',adapter});
  assert(html.includes('原始数据源未确认') && html.includes('获取渠道') && !html.includes('href=') && !html.includes('新浪财经'));
 }
} else if(mode==='panel') {
 let calls=0,rendered=false; const children=[], empty={remove(){}};
 const wrap={innerHTML:'',querySelector:()=>empty,appendChild:child=>children.push(child)};
 global.document={getElementById:()=>({querySelector:()=>wrap}),createElement:()=>({innerHTML:''})};
 App._currentTicker='600519';App._invalidateKline=()=>{};
 App.api=async()=>{calls++;return {ok:true,json:async()=>({...base,kline:[{timestamp:1792425600,open:10}]})}};
 App.renderKlineChart=()=>{rendered=true};
 App._renderKline('600519','1d',120).then(()=>{assert(calls===1 && rendered);assert(children.length===2 && children[0].innerHTML.includes('新浪财经'));assert(children[0].innerHTML.includes('采集时间'));console.log('PASS')}).catch(()=>process.exit(1));
} else if(mode==='snapshot') {
 const body={innerHTML:''}, dialog={open:false,showModal(){this.open=true}}, title={textContent:''};
 global.document={getElementById:id=>({capabilityResultBody:body,capabilityResultDialog:dialog,capabilityResultTitle:title}[id]),addEventListener(){}};
 global.ChatRender={formatTime:()=> 'synthetic saved time'};
 vm.runInThisContext(fs.readFileSync(process.argv[2]+'/static/js/app.workbench.js','utf8'));
 App._closeWorkbenchPanel=()=>{};App._platformCalls={};
 App.api=async()=>({ok:true,json:async()=>({call:{capability_id:'market_kline',created_at:1},params:{ticker:'600519'},result:{...base,bars:[{timestamp:1792425600,open:10}]}})});
 App._openCapabilitySnapshot('synthetic-call').then(()=>{assert(body.innerHTML.includes('新浪财经'));assert(body.innerHTML.includes('采集时间'));assert(body.innerHTML.includes('此处读取已保存结果'));console.log('PASS');}).catch(()=>process.exit(1));
} else throw new Error('unknown test');
if(mode!=='snapshot' && mode!=='panel')console.log('PASS');
"""


@pytest.mark.parametrize("mode", ["known", "legacy", "unsafe", "channels", "panel", "snapshot"])
def test_actual_ui_metadata_and_saved_snapshot_render_without_requery(runtime, tmp_path, mode):
    node = shutil.which("node")
    assert node, "the repository's browser logic checks require Node"
    script = tmp_path / "render.js"
    script.write_text(NODE_TEST)
    result = subprocess.run(
        [node, str(script), str(runtime.directory), mode], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"
