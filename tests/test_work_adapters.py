"""Real request lifetime and bound-manager admission regression tests."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from server.work_adapters import WorkAdmissionMiddleware, produces_work
from server.work_lifecycle import KINDS, LifecycleError, Manager, bind_manager, get_manager


def application_manager():
    manager = Manager()
    for kind in KINDS:
        manager.declare(kind, enabled=kind != "vibe")
    app = SimpleNamespace(state=SimpleNamespace())
    bind_manager(app, manager)
    return app, manager


def scope(app, path, method="GET"):
    return {"type": "http", "method": method, "path": path, "app": app}


async def receive():
    return {"type": "http.request", "body": b"", "more_body": False}


@pytest.mark.parametrize(
    "method,path,expected",
    [
        ("GET", "/api/indicators/universe/summary", True),
        ("GET", "/api/quote/600519", True),
        ("GET", "/api/admin/diagnostics", False),
        ("GET", "/api/conversations", False),
        ("POST", "/api/chat/stream", True),
        ("POST", "/api/chat/cancel", False),
        ("POST", "/api/chat/respond", False),
        ("POST", "/api/internal/capabilities/call", False),
    ],
)
def test_finite_work_routes(method, path, expected):
    assert produces_work(method, path) is expected


@pytest.mark.asyncio
async def test_fence_rejects_new_work_but_keeps_bound_history():
    app, manager = application_manager()
    manager.begin_drain(manager.ticket())
    sent, called = [], []

    async def inner(_scope, _receive, send):
        called.append(True)
        assert get_manager() is manager
        with pytest.raises(LifecycleError, match="admission_closed"):
            get_manager().admit("market")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"history"})

    async def send(message):
        sent.append(message)

    middleware = WorkAdmissionMiddleware(inner)
    await middleware(scope(app, "/api/quote/600519"), receive, send)
    assert not called and sent[0]["status"] == 503
    sent.clear()
    await middleware(scope(app, "/api/conversations"), receive, send)
    assert called and sent[0]["status"] == 200
    manager.close_development()


@pytest.mark.asyncio
async def test_stream_response_owns_request_until_last_body():
    app, manager = application_manager()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def inner(_scope, _receive, send):
        assert get_manager() is manager
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        entered.set()
        await finish.wait()
        await send({"type": "http.response.body", "body": b"saved", "more_body": False})

    async def send(_message):
        pass

    task = asyncio.create_task(
        WorkAdmissionMiddleware(inner)(scope(app, "/api/chat/stream", "POST"), receive, send)
    )
    await entered.wait()
    manager.begin_drain(manager.ticket())
    assert sum(manager.observe()["leases"].values()) == 1
    finish.set()
    await task
    assert sum(manager.observe()["leases"].values()) == 0
    manager.close_development()


@pytest.mark.asyncio
async def test_disconnected_request_retains_actual_worker_thread():
    app, manager = application_manager()
    entered, finish = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        assert finish.wait(5)
        return "saved"

    async def inner(_scope, _receive, _send):
        await get_manager().run_blocking("market", blocked)

    async def send(_message):
        pass

    task = asyncio.create_task(WorkAdmissionMiddleware(inner)(scope(app, "/api/quote/600519"), receive, send))
    try:
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        manager.begin_drain(manager.ticket())
        assert manager.observe()["pending_cf"] == 1
        with pytest.raises(LifecycleError):
            manager.close_development()
    finally:
        finish.set()
        for _ in range(200):
            if sum(manager.observe()["leases"].values()) == 0:
                break
            await asyncio.sleep(0.005)
    assert sum(manager.observe()["leases"].values()) == 0
    manager.close_development()


@pytest.mark.asyncio
async def test_application_lifespan_binds_one_manager_and_closes_before_replacing(monkeypatch):
    from server.main import lifespan

    monkeypatch.delenv("PRISM_WORK_CONTROL", raising=False)
    application = SimpleNamespace(state=SimpleNamespace())
    async with lifespan(application):
        first = get_manager({"app": application})
        assert first is application.state.work_runtime.manager
        assert first.observe()["producer_unknown"] == 0
        assert application.state.work_runtime.identity is None
        with pytest.raises(LifecycleError, match="work_kind_rejected"):
            first.admit("vibe")
    assert first.observe()["state"] == "closed"
    async with lifespan(application):
        second = get_manager({"app": application})
        assert second is not first
    assert second.observe()["state"] == "closed"


@pytest.mark.asyncio
async def test_observed_history_cannot_grant_business_parent_or_hide_pending_response():
    _app, manager = application_manager()
    manager.begin_drain(manager.ticket())
    generation = manager.observe()["generation"]
    observation = manager.track_request()
    assert manager.observe()["pending_requests"] == 1
    assert manager.observe()["generation"] > generation
    with pytest.raises(LifecycleError, match="foreign_or_finished_parent"):
        manager.admit("market", parent=observation)
    with pytest.raises(TypeError):
        with observation:
            pass
    with pytest.raises(LifecycleError, match="admission_closed"):
        manager.admit("market")
    assert await manager.wait_drained(0.01) is False
    with pytest.raises(LifecycleError):
        manager.close_development()
    observation.release()
    observation.release()
    assert manager.observe()["pending_requests"] == 0
    assert await manager.wait_drained(0.01) is True
    manager.close_development()


@pytest.mark.asyncio
async def test_outer_response_observation_covers_activity_write_and_blocks_readiness_after_close():
    app, manager = application_manager()
    response_finished, release_activity = asyncio.Event(), asyncio.Event()
    written, sent = [], []

    async def activity_app(_scope, _receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"history"})
        response_finished.set()
        await release_activity.wait()
        written.append("usage persisted")

    async def send(message):
        sent.append(message)

    middleware = WorkAdmissionMiddleware(activity_app)
    request = asyncio.create_task(middleware(scope(app, "/api/convs"), receive, send))
    await response_finished.wait()
    manager.begin_drain(manager.ticket())
    assert manager.observe()["pending_requests"] == 1 and not written
    assert await manager.wait_drained(0.01) is False
    release_activity.set()
    await request
    assert written and manager.observe()["pending_requests"] == 0
    manager.close_development()
    sent.clear()
    await middleware(scope(app, "/health/ready"), receive, send)
    assert sent[0]["status"] == 503 and len(written) == 1


def test_main_installs_admission_outside_activity():
    from server.activity import ActivityMiddleware
    from server.main import app

    classes = [item.cls for item in app.user_middleware]
    assert classes.index(WorkAdmissionMiddleware) < classes.index(ActivityMiddleware)
