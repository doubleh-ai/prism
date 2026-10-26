"""Real loopback HTTP journeys; temporary HOME/data and a scripted model only."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]

SERVER = r"""
import asyncio, json, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root = Path(sys.argv[2])
def no_external(*args, **kwargs):
    raise AssertionError('isolated HTTP journey forbids outgoing connections')
socket.socket.connect = no_external
socket.socket.connect_ex = no_external
from server import config
config.MODELS = {'mock': {'name':'合成模型', 'api_key':'synthetic-only', 'model':'mock',
                          'base_url':'https://api.deepseek.com/anthropic'}}
config.DEFAULT_MODEL = 'mock'
from server import main
from server.agent import runtime, tools
from server.auth import hash_password, require_auth
from server.database import get_db
from fastapi import Request
import uvicorn
state = {'model_calls':0, 'model_closed':0}
async def scripted(messages, **kwargs):
    state['model_calls'] += 1
    question = next(m['content'] for m in reversed(messages)
                    if m['role']=='user' and isinstance(m['content'],str))
    continuation = messages[-1]['role']=='user' and isinstance(messages[-1]['content'],list)
    try:
        yield {'type':'usage', 'input_tokens':8, 'output_tokens':4}
        if not continuation:
            calls = [
                {'type':'tool_use','id':'indicators','name':'indicator_list','input':{}},
                {'type':'tool_use','id':'source','name':'source_read',
                 'input':{'url':'https://www.sse.com.cn/disclosure/fixture'}}]
            for call in calls:
                yield {'type':'tool_call','id':call['id'],'name':call['name'],'arguments':call['input']}
            yield {'type':'done','stop_reason':'tool_use','content':calls,'usage_complete':True}
        else:
            yield {'type':'text_delta','text':'已保存的合成研究进度，公告节选仍待核验。'*50}
            if 'slow' in question:
                await asyncio.Event().wait()
            yield {'type':'text_delta','text':'最终合成结论。'}
            yield {'type':'done','stop_reason':'end_turn','content':[],'usage_complete':True}
    finally:
        state['model_closed'] += 1
def fetched(*args, **kwargs):
    return {'status':'ok','url':'https://www.sse.com.cn/disclosure/fixture',
            'title':'合成交易所公告','text':'合成公告中产能为120万吨。',
            'retrieved_at':'2026-10-25T18:00:00+08:00','published_at':'2026-10-20',
            'original_obtained':True,'original_preserved':False,
            'content_scope':'server_extracted_excerpt','text_truncated':False}
runtime.model_stream = scripted
tools._fetch_source = fetched
async def no_process(*args, **kwargs):
    raise AssertionError('native HTTP journey forbids CLI execution')
asyncio.create_subprocess_exec = no_process
with get_db() as db:
    for user in ('owner','peer'):
        db.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',
                   [user,hash_password('synthetic-local-test')])
@main.app.get('/__fixture/state')
async def fixture_state(request: Request):
    require_auth(request)
    return state
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
sock.bind(('127.0.0.1',0)); sock.listen(128)
port = sock.getsockname()[1]
(root/'listen.json').write_text(json.dumps({'port':port}))
uvicorn.Server(uvicorn.Config(main.app, log_level='critical', access_log=False,
                             timeout_graceful_shutdown=2)).run(sockets=[sock])
"""


@pytest.fixture
def live_native(tmp_path):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    data.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.defpath,
        "STOCK_DATA_DIR": str(data),
        "STOCK_LOG_FILE": str(tmp_path / "fixture.log"),
        "PRISM_COOKIE_SECURE": "0",
        "PRISM_SQLITE_MMAP_SIZE": "1048576",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    # No inherited provider keys, .env, user HOME or production database paths.
    with (tmp_path / "process.log").open("w+") as output:
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-c", SERVER, str(ROOT), str(tmp_path)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
        )
        try:
            for _ in range(200):
                if process.poll() is not None:
                    output.seek(0)
                    pytest.fail("isolated HTTP fixture failed: " + output.read()[-4000:])
                if (tmp_path / "listen.json").exists():
                    port = json.loads((tmp_path / "listen.json").read_text())["port"]
                    break
                time.sleep(0.025)
            else:
                pytest.fail("isolated HTTP listener did not start")
            clients = []
            for username in ("owner", "peer"):
                client = httpx.Client(
                    base_url=f"http://127.0.0.1:{port}",
                    trust_env=False,
                    timeout=5,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                result = client.post(
                    "/api/login", json={"username": username, "password": "synthetic-local-test"}
                )
                assert result.status_code == 200
                client.headers["X-CSRF-Token"] = client.cookies["csrf_token"]
                clients.append(client)
            yield clients
        finally:
            for client in locals().get("clients", []):
                client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def frames(response):
    assert response.status_code == 200
    for line in response.iter_lines():
        if line.startswith("data: ") and line != "data: [DONE]":
            value = json.loads(line[6:])
            if value.get("agent"):
                yield value


def test_real_http_refresh_cancel_reports_sources_and_owner_isolation(live_native):
    owner, peer = live_native
    conv = owner.post("/api/convs", json={"skill": "prism-research"}).json()["id"]
    body = {
        "question": "slow synthetic research",
        "conv_id": conv,
        "request_id": "http-refresh-owned",
        "model": "mock",
    }
    # Close the original POST reader once public progress has arrived. This
    # represents a reload; only GET replay may reconnect to the durable run.
    received = []
    with owner.stream("POST", "/api/agent/chat/stream", json=body) as response:
        for event in frames(response):
            received.append(event)
            if event.get("text"):
                break
    run_id = received[-1]["agent"]["run_id"]
    initial_calls = owner.get("/__fixture/state").json()["model_calls"]
    assert initial_calls == 2
    progress = owner.get(f"/api/convs/{conv}").json()["messages"]
    pending = json.loads(progress[-1]["content"])
    assert pending["native_agent"]["run_id"] == run_id
    assert pending["native_agent"]["status"] == "running"
    assert pending["text"] and pending["refs"]
    assert pending["refs"][0]["url"] == "https://www.sse.com.cn/disclosure/fixture"
    assert pending["refs"][0].get("semantic_status") != "verified"
    assert any(chunk["t"] == "platform_call" for chunk in pending["chunks"])

    with owner.stream("GET", f"/api/agent/runs/{run_id}/stream?after_seq=0") as response:
        replayed = []
        for event in frames(response):
            replayed.append(event)
            if event["agent"]["seq"] == received[-1]["agent"]["seq"]:
                break
    assert replayed == received
    assert owner.get("/__fixture/state").json()["model_calls"] == initial_calls
    call_id = next(event["platform_call"]["call_id"] for event in received if event.get("platform_call"))
    assert owner.get(f"/api/capabilities/calls/{call_id}").status_code == 200
    for path in (
        f"/api/convs/{conv}",
        f"/api/agent/runs/{run_id}",
        f"/api/agent/runs/{run_id}/events",
        f"/api/agent/runs/{run_id}/stream?after_seq=0",
        f"/api/capabilities/calls/{call_id}",
    ):
        assert peer.get(path).status_code == 404, path
    assert peer.post("/api/agent/chat/cancel", json={"conv_id": conv, "run_id": run_id}).status_code == 404
    ack = owner.post("/api/agent/chat/cancel", json={"conv_id": conv, "run_id": run_id})
    assert ack.status_code == 200 and ack.json()["ok"]
    for _ in range(100):
        run = owner.get(f"/api/agent/runs/{run_id}").json()["run"]
        if run["status"] == "cancelled":
            break
        time.sleep(0.02)
    assert run["status"] == "cancelled"
    assert owner.get("/__fixture/state").json() == {"model_calls": 2, "model_closed": 2}
    with owner.stream(
        "GET", f"/api/agent/runs/{run_id}/stream?after_seq={received[-1]['agent']['seq']}"
    ) as response:
        terminal = list(frames(response))
    assert terminal[-1]["agent"]["status"] == "cancelled"
    saved = json.loads(owner.get(f"/api/convs/{conv}").json()["messages"][-1]["content"])
    assert saved["native_agent"]["status"] == "cancelled"
    # The final cancellation flush also preserves text still in the runtime's
    # bounded public buffer when the original reader disconnected.
    assert saved["text"] == "已保存的合成研究进度，公告节选仍待核验。" * 50
    assert saved["text"].startswith(pending["text"])
    assert saved["refs"] == pending["refs"]
    reports = owner.get("/api/reports/library").json()["reports"]
    assert len(reports) == 1
    report_id = reports[0]["id"]
    report = owner.get(f"/api/reports/library/{report_id}")
    assert report.status_code == 200 and "合成" in report.text
    assert peer.get(f"/api/reports/library/{report_id}").status_code == 404
    assert peer.get("/api/reports/library").json()["reports"] == []
