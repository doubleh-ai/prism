"""Private feedback workflow on synthetic users and isolated SQLite data."""

import json
import uuid

import pytest


@pytest.fixture(autouse=True)
def feedback_db(temp_data_dir, monkeypatch):
    from server import auth, feedback
    from server.database import create_session, get_db

    feedback.init_feedback_db()
    monkeypatch.setattr(auth, "ADMIN_USERS", {"reviewer"})
    with get_db() as db:
        for name in ("alice", "bob", "reviewer"):
            db.execute(
                "INSERT INTO users(username,password_hash) VALUES(?,?)", [name, "synthetic-not-a-password"]
            )
    return {name: {"token": create_session(name)} for name in ("alice", "bob", "reviewer")}


def report(**changes):
    return {
        "category": "bug",
        "title": "产业列表难以阅读",
        "description": "浅色主题下行业名称没有足够对比度，请核对。",
        "page": "/industry",
        **changes,
    }


@pytest.fixture
def worker_headers(monkeypatch):
    token = "synthetic-worker-token-for-offline-tests-only-0001"
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", token)
    return {"X-Prism-Feedback-Token": token}


async def submit(client, cookies, **changes):
    response = await client.post("/api/feedback", cookies=cookies, json=report(**changes))
    assert response.status_code == 200, response.text
    return response.json()["feedback"]


async def test_bug_immediate_triage_is_local_pending_work_not_fake_fix(client, feedback_db, monkeypatch):
    from server import feedback

    monkeypatch.setattr(feedback, "_read_deployed_revision", lambda: "a" * 40)
    item = await submit(client, feedback_db["alice"], title="页面出现500")
    assert item["status"] == "triaging" and item["triage"]["priority"] == "urgent"
    assert item["triage"]["automatically_fixed"] is False and item["triage"]["requires_review"] is True
    assert item["release"] == {"version": feedback.APP_VERSION, "revision": "a" * 12}
    assert item["updates"] == [] and "username" not in item and "releases" not in item


async def test_suggestions_enter_daily_shanghai_review_batch(client, feedback_db, monkeypatch):
    from server import feedback

    # 2026-01-01 17:00 UTC is the next Shanghai calendar day.
    monkeypatch.setattr(feedback.time, "time", lambda: 1767286800)
    item = await submit(client, feedback_db["alice"], category="suggestion")
    assert item["status"] == "pending_review" and item["triage"]["mode"] == "daily_review"
    assert item["review_batch_date"] == "2026-01-02"
    queue = await client.get(
        "/api/admin/feedback",
        cookies=feedback_db["reviewer"],
        params={"category": "suggestion", "status": "pending_review", "review_batch_date": "2026-01-02"},
    )
    assert [row["id"] for row in queue.json()["items"]] == [item["id"]]


async def test_owner_lists_details_and_admin_history_do_not_cross_users(client, feedback_db):
    alice = await submit(client, feedback_db["alice"])
    bob = await submit(client, feedback_db["bob"], title="其他用户的反馈")
    mine = await client.get("/api/feedback", cookies=feedback_db["alice"])
    assert [x["id"] for x in mine.json()["items"]] == [alice["id"]]
    for path in (f"/api/feedback/{bob['id']}", "/api/admin/feedback", f"/api/admin/feedback/{bob['id']}"):
        response = await client.get(path, cookies=feedback_db["alice"])
        assert response.status_code == (404 if path.startswith("/api/feedback/") else 403)
    detail = await client.get(f"/api/admin/feedback/{bob['id']}", cookies=feedback_db["reviewer"])
    assert detail.json()["feedback"]["username"] == "bob"


async def test_unauthenticated_and_missing_csrf_fail_without_creating_feedback(client, feedback_db):
    from httpx import ASGITransport, AsyncClient

    from server.database import get_db
    from server.main import app

    response = await client.get("/api/feedback")
    assert response.status_code == 401
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw:
        response = await raw.post("/api/feedback", json=report(), cookies=feedback_db["alice"])
        assert response.status_code == 403
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_items").fetchone()[0] == 0


async def test_plain_text_secret_redaction_and_path_only_storage(client, feedback_db):
    from server.database import get_db

    secret = "sk-syntheticABCDEFG123456789"
    item = await submit(
        client,
        feedback_db["alice"],
        description=f"控制台报错。api_key={secret} password='synthetic-secret-value' Bearer synthetic-token-123456",
        page="/reports%3Ftoken%3Dsynthetic-query-secret%23private",
    )
    assert item["page"] == "/reports"
    raw = json.dumps(item, ensure_ascii=False)
    assert all(
        value not in raw
        for value in (secret, "synthetic-secret-value", "synthetic-token-123456", "synthetic-query-secret")
    )
    with get_db() as db:
        saved = str(tuple(db.execute("SELECT * FROM feedback_items").fetchone()))
        assert secret not in saved and "synthetic-query-secret" not in saved


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"description": " "},
        {"category": "unknown"},
        {"title": "x" * 121},
        {"description": "x" * 6001},
        {"page": "https://example.test/path?token=private"},
        {"page": "//example.test/path"},
        {"page": "//[malformed/path"},
        {"page": "/x\\secret"},
        {"request_id": "not-a-uuid"},
        {"description": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n-----END OPENSSH PRIVATE KEY-----"},
        {"description": 123},
        {"username": "bob"},
        {"feature": "conversation-private-id"},
        {"report": {"private": "do not collect"}},
    ],
)
async def test_invalid_sensitive_or_implicit_data_fields_rejected_without_echo(client, feedback_db, changes):
    response = await client.post("/api/feedback", cookies=feedback_db["alice"], json=report(**changes))
    assert response.status_code == 422
    assert "synthetic" not in response.text and "PRIVATE KEY" not in response.text


async def test_bounded_body_and_duplicate_json_keys(client, feedback_db):
    response = await client.post("/api/feedback", cookies=feedback_db["alice"], content=b"x" * 32769)
    assert response.status_code == 413
    response = await client.post(
        "/api/feedback",
        cookies=feedback_db["alice"],
        content=b'{"category":"bug","category":"suggestion","title":"a","description":"b"}',
    )
    assert response.status_code == 422


async def test_idempotency_owner_scope_and_conflict_without_overwriting(client, feedback_db):
    from server.database import get_db

    request_id = str(uuid.uuid4())
    first = await client.post(
        "/api/feedback", cookies=feedback_db["alice"], json=report(request_id=request_id)
    )
    second = await client.post(
        "/api/feedback", cookies=feedback_db["alice"], json=report(request_id=request_id)
    )
    assert first.json()["reused"] is False and second.json()["reused"] is True
    assert first.json()["feedback"]["id"] == second.json()["feedback"]["id"]
    conflict = await client.post(
        "/api/feedback", cookies=feedback_db["alice"], json=report(request_id=request_id, title="不同问题")
    )
    assert conflict.status_code == 409
    bob = await submit(client, feedback_db["bob"], request_id=request_id)
    assert bob["id"] != first.json()["feedback"]["id"]
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_items").fetchone()[0] == 2


async def test_user_cannot_write_administrator_reply(client, feedback_db):
    item = await submit(client, feedback_db["alice"])
    response = await client.post(
        f"/api/admin/feedback/{item['id']}/update",
        cookies=feedback_db["alice"],
        json={"status": "fixed", "public_reply": "越权修改"},
    )
    assert response.status_code == 403


async def test_fixed_requires_specific_verification_and_version_then_is_visible_to_owner(client, feedback_db):
    item = await submit(client, feedback_db["alice"])
    path = f"/api/admin/feedback/{item['id']}/update"
    for fields in (
        {},
        {"verification": "已测试"},
        {"verification": "已用两个浅色行业页面核对对比度，截图测试通过", "target_version": "later"},
    ):
        response = await client.post(
            path,
            cookies=feedback_db["reviewer"],
            json={"status": "fixed", "public_reply": "修复说明", **fields},
        )
        assert response.status_code == 422
    response = await client.post(
        path,
        cookies=feedback_db["reviewer"],
        json={
            "status": "fixed",
            "public_reply": "浅色产业列表已修正，将随目标版本发布",
            "verification": "已验证浅色与深色主题、手机窄屏及行业列表两种状态",
            "target_version": "v0.21.0",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 200
    detail = (await client.get(f"/api/feedback/{item['id']}", cookies=feedback_db["alice"])).json()[
        "feedback"
    ]
    assert detail["status"] == "fixed" and detail["target_version"] == "v0.21.0" and detail["verification"]
    assert len(detail["updates"]) == 1 and "actor" not in detail["updates"][0]
    assert "username" not in detail
    conflict = await client.post(
        path,
        cookies=feedback_db["reviewer"],
        json={"status": "in_progress", "public_reply": "过期页面覆盖", "expected_revision": 1},
    )
    assert conflict.status_code == 409


async def test_suggestion_review_history_preserved_and_wrong_category_status_rejected(client, feedback_db):
    item = await submit(client, feedback_db["alice"], category="suggestion")
    path = f"/api/admin/feedback/{item['id']}/update"
    for status in ("accepted", "deferred", "declined"):
        response = await client.post(
            path,
            cookies=feedback_db["reviewer"],
            json={"status": status, "public_reply": "本轮统一评审结果：" + status},
        )
        assert response.status_code == 200
    response = await client.post(
        path, cookies=feedback_db["reviewer"], json={"status": "triaging", "public_reply": "错误类别状态"}
    )
    assert response.status_code == 422
    detail = (await client.get(f"/api/feedback/{item['id']}", cookies=feedback_db["alice"])).json()[
        "feedback"
    ]
    assert [x["status"] for x in detail["updates"]] == ["accepted", "deferred", "declined"]


async def test_queue_pagination_and_status_filters(client, feedback_db):
    items = [await submit(client, feedback_db["alice"], title="问题" + str(i)) for i in range(3)]
    first = (
        await client.get("/api/feedback?limit=2&category=bug&status=triaging", cookies=feedback_db["alice"])
    ).json()
    assert [x["id"] for x in first["items"]] == [items[2]["id"], items[1]["id"]]
    following = (
        await client.get(
            "/api/feedback",
            params={"before_id": first["next_before_id"], "limit": 2},
            cookies=feedback_db["alice"],
        )
    ).json()
    assert [x["id"] for x in following["items"]] == [items[0]["id"]] and following["next_before_id"] is None
    assert (await client.get("/api/feedback?status=made_up", cookies=feedback_db["alice"])).status_code == 422


async def test_submission_rate_limit_is_per_user(client, feedback_db):
    for _ in range(6):
        await submit(client, feedback_db["alice"])
    response = await client.post("/api/feedback", cookies=feedback_db["alice"], json=report())
    assert response.status_code == 429
    assert (await client.post("/api/feedback", cookies=feedback_db["bob"], json=report())).status_code == 200


async def test_deleted_account_feedback_and_updates_purged_by_persistent_trigger(client, feedback_db):
    from server.database import delete_user_account, get_db, init_db
    from server.feedback import init_feedback_db

    alice = await submit(client, feedback_db["alice"])
    bob = await submit(client, feedback_db["bob"])
    response = await client.post(
        f"/api/admin/feedback/{alice['id']}/update",
        cookies=feedback_db["reviewer"],
        json={"status": "in_progress", "public_reply": "已安排复现核对"},
    )
    assert response.status_code == 200
    init_db()
    init_feedback_db()
    # This core deletion is also the old c997 deletion behavior; the SQL trigger
    # persists independently of whether the running application knows feedback.
    assert delete_user_account("alice") is True
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_items WHERE username='alice'").fetchone()[0] == 0
        assert (
            db.execute("SELECT COUNT(*) FROM feedback_updates WHERE feedback_id=?", [alice["id"]]).fetchone()[
                0
            ]
            == 0
        )
        assert db.execute("SELECT id FROM feedback_items WHERE username='bob'").fetchone()[0] == bob["id"]


async def test_feature_is_plain_enum_and_can_filter_a_spa_root_page(client, feedback_db):
    item = await submit(client, feedback_db["alice"], page="/", feature="industry")
    assert item["page"] == "/" and item["feature"] == "industry"
    match = await client.get("/api/admin/feedback?feature=industry", cookies=feedback_db["reviewer"])
    assert [row["id"] for row in match.json()["items"]] == [item["id"]]
    other = await client.get("/api/feedback?feature=chat", cookies=feedback_db["alice"])
    assert other.json()["items"] == []


def test_schema_feature_upgrade_is_idempotent_and_preserves_existing_feedback():
    from server.database import get_db
    from server.feedback import init_feedback_db

    with get_db() as db:
        db.execute("DROP TRIGGER feedback_account_delete")
        db.execute("DROP TABLE feedback_updates")
        db.execute("DROP TABLE feedback_items")
        db.execute("""CREATE TABLE feedback_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL,
            category TEXT NOT NULL,title TEXT NOT NULL,description TEXT NOT NULL,page TEXT NOT NULL,
            status TEXT NOT NULL,version TEXT NOT NULL,revision TEXT NOT NULL,triage_json TEXT NOT NULL,
            review_batch_date TEXT NOT NULL,public_reply TEXT NOT NULL DEFAULT '',
            verification TEXT NOT NULL DEFAULT '',target_version TEXT NOT NULL DEFAULT '',
            revision_number INTEGER NOT NULL DEFAULT 1, request_id TEXT, request_fingerprint TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)""")
        db.execute("""INSERT INTO feedback_items(username,category,title,description,page,status,version,revision,triage_json,review_batch_date,created_at,updated_at)
            VALUES ('alice','bug','earlier synthetic title','earlier synthetic description','/','triaging','v0.21.0','','{}','2026-01-01',1,1)""")
    init_feedback_db()
    init_feedback_db()
    with get_db() as db:
        row = db.execute("SELECT * FROM feedback_items").fetchone()
        assert row["title"] == "earlier synthetic title" and row["feature"] == "unknown"
        assert [row[1] for row in db.execute("PRAGMA table_info(feedback_items)")].count("feature") == 1


@pytest.mark.parametrize("retire", [False, True])
async def test_admin_slow_body_cannot_write_after_actor_disabled_or_deleted(client, feedback_db, retire):
    from server.database import delete_user_account, get_db

    item = await submit(client, feedback_db["alice"])
    payload = json.dumps({"status": "in_progress", "public_reply": "此请求读取正文期间账号已被撤权"}).encode()

    async def slow_body():
        if retire:
            assert delete_user_account("reviewer") is True
        else:
            with get_db() as db:
                db.execute("UPDATE users SET disabled_at=1 WHERE username='reviewer'")
        yield payload

    response = await client.post(
        f"/api/admin/feedback/{item['id']}/update", cookies=feedback_db["reviewer"], content=slow_body()
    )
    assert response.status_code == 403
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_updates").fetchone()[0] == 0
        assert (
            db.execute("SELECT status FROM feedback_items WHERE id=?", [item["id"]]).fetchone()[0]
            == "triaging"
        )


async def test_worker_disabled_invalid_and_cookie_identity_never_falls_back(client, feedback_db, monkeypatch):
    monkeypatch.delenv("PRISM_FEEDBACK_WORKER_TOKEN", raising=False)
    for headers in ({}, {"X-Prism-Feedback-Token": "synthetic-invalid"}):
        response = await client.get(
            "/api/internal/feedback", cookies=feedback_db["reviewer"], headers=headers
        )
        assert response.status_code == 401 and response.json() == {"detail": "反馈处理身份无效"}
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", "too-short")
    assert (
        await client.get("/api/internal/feedback", headers={"X-Prism-Feedback-Token": "too-short"})
    ).status_code == 401
    monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", "synthetic-enabled-worker-token-only-0001")
    assert (await client.get("/api/internal/feedback", cookies=feedback_db["reviewer"])).status_code == 401


async def test_worker_queue_detail_are_scoped_and_cannot_call_other_admin_apis(
    client, feedback_db, worker_headers
):
    item = await submit(client, feedback_db["alice"], request_id=str(uuid.uuid4()), feature="industry")
    queue = await client.get("/api/internal/feedback?feature=industry&category=bug", headers=worker_headers)
    assert queue.status_code == 200
    assert [r["id"] for r in queue.json()["items"]] == [item["id"]]
    assert "username" not in queue.json()["items"][0] and "request_id" not in queue.json()["items"][0]
    detail = await client.get(f"/api/internal/feedback/{item['id']}", headers=worker_headers)
    assert detail.status_code == 200 and "username" not in detail.json()["feedback"]
    for path in ("/api/admin/feedback", "/api/admin/users", "/api/admin/diagnostics"):
        assert (await client.get(path, headers=worker_headers)).status_code == 401


async def test_only_exact_worker_post_uses_dedicated_header_no_cookie_or_csrf(
    client, feedback_db, worker_headers
):
    from httpx import ASGITransport, AsyncClient

    from server.database import get_db
    from server.main import app

    item = await submit(client, feedback_db["alice"])
    path = f"/api/internal/feedback/{item['id']}/update"
    payload = {
        "status": "in_progress",
        "public_reply": "已创建实际隔离排查任务，尚未修复或部署。",
        "expected_revision": 1,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as raw:
        result = await raw.post(path, headers=worker_headers, json=payload)
        assert result.status_code == 200 and "username" not in result.json()["feedback"]
        assert (await raw.post(path, json=payload, cookies=feedback_db["reviewer"])).status_code == 401
        assert (
            await raw.post(f"/api/admin/feedback/{item['id']}/update", headers=worker_headers, json=payload)
        ).status_code == 403
        assert (await raw.put(path, headers=worker_headers, json=payload)).status_code == 403
    with get_db() as db:
        assert (
            db.execute("SELECT actor FROM feedback_updates WHERE feedback_id=?", [item["id"]]).fetchone()[0]
            == "system:feedback-worker"
        )
        assert not db.execute("SELECT 1 FROM users WHERE username='system:feedback-worker'").fetchone()


async def test_worker_mandatory_cas_prevents_duplicate_or_racing_updates(client, feedback_db, worker_headers):
    item = await submit(client, feedback_db["alice"])
    path = f"/api/internal/feedback/{item['id']}/update"
    payload = {"status": "in_progress", "public_reply": "本次排查任务已建立。"}
    assert (await client.post(path, headers=worker_headers, json=payload)).status_code == 422
    payload["expected_revision"] = 1
    assert (await client.post(path, headers=worker_headers, json=payload)).status_code == 200
    assert (await client.post(path, headers=worker_headers, json=payload)).status_code == 409
    detail = (await client.get(f"/api/internal/feedback/{item['id']}", headers=worker_headers)).json()[
        "feedback"
    ]
    assert detail["revision_number"] == 2 and len(detail["updates"]) == 1


async def test_concurrent_worker_writers_only_one_can_accept_same_revision(
    client, feedback_db, worker_headers
):
    import asyncio

    item = await submit(client, feedback_db["alice"])
    path = f"/api/internal/feedback/{item['id']}/update"
    responses = await asyncio.gather(
        *(
            client.post(
                path,
                headers=worker_headers,
                json={"status": "in_progress", "public_reply": "隔离任务" + str(i), "expected_revision": 1},
            )
            for i in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 409]
    detail = (await client.get(f"/api/internal/feedback/{item['id']}", headers=worker_headers)).json()[
        "feedback"
    ]
    assert len(detail["updates"]) == 1 and detail["revision_number"] == 2


@pytest.mark.parametrize(
    "message",
    [
        "this." + "A" * 61 + " is not a function",
        "this.run(); publish_private_data is not a function",
        "prefix user text this.run is not a function",
    ],
)
def test_derived_method_code_never_returns_arbitrary_message(message):
    from server.feedback import _diagnostic_code

    assert _diagnostic_code("js_error", message) == {"error_code": "needs_review"}


async def test_worker_fixed_binds_running_version_and_full_deployed_revision(
    client, feedback_db, worker_headers, monkeypatch
):
    from server import feedback

    item = await submit(client, feedback_db["alice"])
    path = f"/api/internal/feedback/{item['id']}/update"
    payload = {
        "status": "fixed",
        "public_reply": "合成测试中的已验证发布说明。",
        "verification": "合成专用：真实行为测试中已核对对应问题场景与发布身份。",
        "target_version": feedback.APP_VERSION,
        "expected_revision": 1,
    }
    monkeypatch.setattr(feedback, "_read_deployed_revision", lambda: "a" * 40)
    for changes in (
        {},
        {"target_revision": "b" * 40},
        {"target_revision": "a" * 40, "target_version": "v99.0.0"},
    ):
        assert (
            await client.post(path, headers=worker_headers, json={**payload, **changes})
        ).status_code == 409
    assert (
        await client.post(
            path,
            headers=worker_headers,
            json={**payload, "target_revision": "a" * 40, "verification": "short"},
        )
    ).status_code == 422
    response = await client.post(path, headers=worker_headers, json={**payload, "target_revision": "a" * 40})
    assert response.status_code == 200 and response.json()["feedback"]["target_revision"] == "a" * 40


async def test_worker_token_rotation_during_slow_body_blocks_write(
    client, feedback_db, worker_headers, monkeypatch
):
    from server.database import get_db

    item = await submit(client, feedback_db["alice"])

    async def slow_body():
        monkeypatch.setenv("PRISM_FEEDBACK_WORKER_TOKEN", "synthetic-rotated-token-value-after-request-0002")
        yield json.dumps(
            {"status": "in_progress", "public_reply": "旧身份不能写入", "expected_revision": 1}
        ).encode()

    response = await client.post(
        f"/api/internal/feedback/{item['id']}/update", headers=worker_headers, content=slow_body()
    )
    assert response.status_code == 401
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM feedback_updates").fetchone()[0] == 0


async def test_worker_rate_bucket_checked_before_token_comparison(client, worker_headers, monkeypatch):
    from server import feedback

    monkeypatch.setattr(feedback, "check_rate_limit", lambda *args: False)
    monkeypatch.setattr(
        feedback.secrets,
        "compare_digest",
        lambda *args: pytest.fail("compared exhausted authentication bucket"),
    )
    assert (await client.get("/api/internal/feedback", headers=worker_headers)).status_code == 401


async def test_worker_diagnostics_uses_only_known_derived_codes_not_request_data(
    client, feedback_db, worker_headers
):
    from server.database import get_db
    from server.diagnostics import record_diagnostic

    record_diagnostic(
        username="alice",
        kind="js_error",
        component="private component",
        page="/private?token=hidden",
        message="TypeError: this.loadIndustries is not a function",
        stack="PRIVATE_STACK_AND_PROMPT",
        version="v0.20.0",
        context={"request_id": "PRIVATE_CONTEXT"},
    )
    record_diagnostic(
        username="bob",
        kind="server_exception",
        message="ValueError: PRIVATE_REQUEST_OR_USER_DATA",
        version="",
    )
    record_diagnostic(
        username="alice",
        kind="api_http_5xx",
        message="HTTP 503 from /private/user/history",
        version="v0.21.0",
    )
    response = await client.get("/api/internal/feedback/diagnostics?limit=100", headers=worker_headers)
    assert response.status_code == 200
    raw = json.dumps(response.json())
    assert all(
        word not in raw
        for word in (
            "alice",
            "bob",
            "PRIVATE_STACK",
            "PRIVATE_CONTEXT",
            "PRIVATE_REQUEST",
            "/private",
            "private component",
        )
    )
    events = response.json()["events"]
    assert any(
        r.get("method") == "loadIndustries" and r["error_code"] == "method_not_callable" for r in events
    )
    assert any(r.get("exception_class") == "ValueError" for r in events)
    assert any(r.get("http_status") == 503 for r in events)
    assert all("message" not in row and "username" not in row and "stack" not in row for row in events)
    with get_db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM diagnostic_events WHERE resolved_at IS NULL").fetchone()[0] == 3
        )
