"""Exercise the actual async wrapper against a fake of upstream's global config."""

import asyncio
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import analysis_engine as engine
from server import config


@pytest.fixture
def upstream(monkeypatch, tmp_path):
    memory = tmp_path / "legacy-trading-memory.md"
    memory.write_text("Another owner's old private reasoning must never be read")
    defaults = {
        "results_dir": str(tmp_path / "legacy-results"),
        "data_cache_dir": str(tmp_path / "legacy-cache"),
        "memory_log_path": str(memory),
        "checkpoint_enabled": True,
    }
    module = types.ModuleType("tradingagents.default_config")
    module.DEFAULT_CONFIG = defaults
    monkeypatch.setitem(sys.modules, "tradingagents.default_config", module)
    monkeypatch.setattr(engine, "_ensure_astock", lambda: None)
    monkeypatch.setattr(engine, "_extract_structured", lambda *args: None)
    monkeypatch.setattr(engine, "ASTOCK_CACHE_TTL_SECONDS", 0)
    monkeypatch.setattr(engine, "_ENGINE_RUN_LOCK", threading.Lock())
    original_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        if path == memory:
            pytest.fail("Shared legacy memory must not be opened")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    yield SimpleNamespace(defaults=defaults, memory=memory)
    engine._store.clear()


def install_graph(monkeypatch, graph):
    module = types.ModuleType("tradingagents.graph.trading_graph")
    module.TradingAgentsGraph = graph
    monkeypatch.setitem(sys.modules, "tradingagents.graph.trading_graph", module)


async def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("Fake engine did not reach its expected state")


def test_config_paths_are_uuid_scoped_private_and_do_not_mutate_defaults_or_environment(
    upstream, monkeypatch
):
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", "legacy-global-results")
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", "legacy-global-cache")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-value")
    environment = dict(os.environ)
    original = dict(upstream.defaults)
    a = engine._build_config("deep", analysis_id="a" * 32)
    b = engine._build_config("deep", analysis_id="b" * 32)
    assert os.environ == environment and upstream.defaults == original
    assert a["results_dir"] != b["results_dir"] and a["data_cache_dir"] != b["data_cache_dir"]
    assert a["memory_log_path"] is b["memory_log_path"] is None
    assert a["checkpoint_enabled"] is b["checkpoint_enabled"] is False
    for key in ("results_dir", "data_cache_dir"):
        for value, uid in ((a[key], "a" * 32), (b[key], "b" * 32)):
            path = Path(value)
            assert path.is_relative_to(config.DATA_DIR / "analysis_engine" / uid)
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Legacy callers may inspect config without creating engine run directories.
    inspected = engine._build_config("fast")
    assert inspected["memory_log_path"] is None
    assert inspected["results_dir"] == engine.ASTOCK_RESULTS_DIR


@pytest.mark.parametrize("analysis_id", ["", "../other-owner", "alice", "1" * 12, "/absolute/path"])
def test_untrusted_or_missing_run_identifiers_cannot_choose_output_paths(upstream, analysis_id):
    with pytest.raises(ValueError):
        engine._build_config("deep", analysis_id=analysis_id)
    assert not (config.DATA_DIR / "analysis_engine").exists()


def test_engine_output_root_cannot_redirect_through_symlink(upstream, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    before = outside.stat().st_mode
    (config.DATA_DIR / "analysis_engine").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        engine._build_config(analysis_id="c" * 32)
    assert list(outside.iterdir()) == [] and outside.stat().st_mode == before


async def test_two_owners_same_stock_and_date_never_share_global_engine_configuration(upstream, monkeypatch):
    global_config, instances, writes = {}, [], []
    release = threading.Event()
    first_started = threading.Event()
    active = {"count": 0, "maximum": 0}

    class Graph:
        def __init__(self, *, config, **kwargs):
            self.cfg = config
            self.index = len(instances)
            self.graph = self
            instances.append(self)
            global_config.update(config)  # real upstream's dataflows.set_config
            active["count"] += 1
            active["maximum"] = max(active["maximum"], active["count"])
            assert self.cfg["memory_log_path"] is None
            assert self.cfg["checkpoint_enabled"] is False

        def assert_current(self):
            assert global_config["results_dir"] == self.cfg["results_dir"]
            assert global_config["data_cache_dir"] == self.cfg["data_cache_dir"]

        def prepare_graph_run(self, ticker, date):
            self.assert_current()
            return {"messages": [("human", ticker)]}, {}, None

        def stream(self, state, **kwargs):
            self.assert_current()
            assert state["messages"][0] == ("human", "600519")
            assert "本轮研究时间" in state["messages"][-1][1]
            if self.index == 0:
                first_started.set()
                assert release.wait(3)
            self.assert_current()
            yield {"final_trade_decision": "Observed fake report " + str(self.index)}

        def finalize_graph_run(self, ticker, date, state):
            self.assert_current()
            for key in ("results_dir", "data_cache_dir"):
                path = Path(global_config[key]) / f"{ticker}-{date}.json"
                path.write_text(str(self.index))
                writes.append(path)
            return "观察"

        def close_graph_run(self):
            self.assert_current()
            active["count"] -= 1

    install_graph(monkeypatch, Graph)
    environment = dict(os.environ)
    a = await engine.start_analysis("600519", date="2026-10-20", username="owner-a", force_refresh=True)
    await wait_for(first_started.is_set)
    b = await engine.start_analysis("600519", date="2026-10-20", username="owner-b", force_refresh=True)
    try:
        await wait_for(lambda: bool(engine._store[b]["events"]))
        assert len(instances) == 1
        assert engine._store[b]["stage"] == "queued"
        assert engine._store[b]["events"][0]["stage"] == "queued"
        assert all("progress_pct" not in event for event in engine._store[b]["events"])
    finally:
        release.set()
    await wait_for(
        lambda: (
            all(engine._store[aid]["status"] in {"done", "error"} for aid in (a, b))
            and not engine._ENGINE_RUN_LOCK.locked()
        )
    )
    assert engine._store[a]["status"] == engine._store[b]["status"] == "done"
    assert active["maximum"] == 1 and active["count"] == 0
    assert len({str(path) for path in writes}) == 4
    assert os.environ == environment
    for aid, owner in ((a, "owner-a"), (b, "owner-b")):
        report = engine.get_analysis_record(aid, owner)["report"]
        meta = report["_analysis_meta"]
        assert meta["evidence_status"] == "unverified_engine_report"
        assert meta["context_injection_scope"] == "first_analyst_message_only"
        assert meta["shared_memory_enabled"] is False
        assert engine.get_analysis_record(aid, "other-owner") is None
        assert all("progress_pct" not in event for event in engine._store[aid]["events"])


@pytest.mark.parametrize("failure", ["construct", "stream", "finalize", "close"])
async def test_failure_releases_entire_engine_lifetime_lock(upstream, monkeypatch, failure):
    instances = []

    class Graph:
        def __init__(self, **kwargs):
            self.index = len(instances)
            instances.append(self)
            self.graph = self
            if self.index == 0 and failure == "construct":
                raise RuntimeError("synthetic construction failure")

        def prepare_graph_run(self, *args):
            return {"messages": []}, {}, None

        def stream(self, *args, **kwargs):
            if self.index == 0 and failure == "stream":
                raise RuntimeError("synthetic stream failure")
            yield {"final_trade_decision": "A complete fake report"}

        def finalize_graph_run(self, *args):
            if self.index == 0 and failure == "finalize":
                raise RuntimeError("synthetic finalization failure")
            return "观察"

        def close_graph_run(self):
            if self.index == 0 and failure == "close":
                raise RuntimeError("synthetic cleanup failure")

    install_graph(monkeypatch, Graph)
    a = await engine.start_analysis("600519", username="owner-a", force_refresh=True)
    b = await engine.start_analysis("600519", username="owner-b", force_refresh=True)
    await wait_for(
        lambda: (
            all(engine._store[aid]["status"] in {"done", "error"} for aid in (a, b))
            and not engine._ENGINE_RUN_LOCK.locked()
        )
    )
    assert engine._store[b]["status"] == "done"
    assert engine._store[a]["status"] == ("done" if failure == "close" else "error")
    assert len(instances) == 2
    assert engine._ENGINE_RUN_LOCK.acquire(blocking=False)
    engine._ENGINE_RUN_LOCK.release()


async def test_missing_service_uuid_never_constructs_or_runs_external_graph(upstream, monkeypatch):
    graph = types.SimpleNamespace(calls=0)

    class ForbiddenGraph:
        def __init__(self, **kwargs):
            graph.calls += 1
            raise AssertionError("An invalid run must not construct the graph")

    install_graph(monkeypatch, ForbiddenGraph)
    monkeypatch.setattr(engine.uuid, "uuid4", lambda: SimpleNamespace(hex=""))
    aid = await engine.start_analysis("600519", username="owner", force_refresh=True)
    await wait_for(
        lambda: (
            engine._store[aid]["status"] == "error"
            and engine._store[aid]["events"][-1].get("type") == "error"
        )
    )
    assert graph.calls == 0
    assert not (config.DATA_DIR / "analysis_engine").exists()
    assert not any(event.get("stage") == "analysts" for event in engine._store[aid]["events"])


async def test_provider_exception_never_reaches_events_durable_records_or_logs(upstream, monkeypatch, caplog):
    secret = "synthetic-key-private-never-return"

    class Graph:
        def __init__(self, **kwargs):
            raise RuntimeError("provider authentication failed: " + secret)

    install_graph(monkeypatch, Graph)
    aid = await engine.start_analysis("600519", username="owner", force_refresh=True)
    await wait_for(lambda: engine._store[aid]["status"] == "error" and not engine._ENGINE_RUN_LOCK.locked())
    events = engine._store[aid]["events"]
    assert events[-1]["code"] == "engine_unavailable"
    assert secret not in str(events)
    assert secret not in str(engine.get_analysis_record(aid, "owner"))
    engine._store.pop(aid)
    assert secret not in str(engine.get_analysis_record(aid, "owner"))
    assert secret not in caplog.text and "RuntimeError" in caplog.text and aid in caplog.text
