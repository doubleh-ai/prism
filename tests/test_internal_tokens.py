"""Real private synthetic files and API boundaries; no live identity or network."""

import os
from datetime import datetime
from uuid import uuid4

import pytest

from server import internal_tokens as t

VALUES = {role: "synthetic-" + role + "-" + str(i) * 48 for i, role in enumerate(t.SLOTS, 1)}
HEADERS = {
    "monitor": "X-Prism-Monitor-Token",
    "review": "X-Prism-Review-Token",
    "progress": "X-Prism-Progress-Token",
    "worker": "X-Prism-Feedback-Token",
}
REVISION = "a" * 40


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    directory = tmp_path / "credentials"
    directory.mkdir(mode=0o700)
    for role, (env, name) in t.SLOTS.items():
        monkeypatch.setenv(env, "legacy-" + role + "-" + "x" * 40)
        path = directory / name
        path.write_bytes(VALUES[role].encode())
        path.chmod(0o400)
    monkeypatch.setenv(t.DIRECTORY_ENV, str(directory))
    return directory


def test_private_files_override_old_environment_as_one_explicit_source(credentials):
    assert t.load_tokens() == VALUES
    (credentials / t.SLOTS["worker"][1]).unlink()
    assert t.load_tokens() == {**VALUES, "worker": ""}


def test_legacy_environment_remains_supported_only_without_file_configuration(monkeypatch):
    monkeypatch.delenv(t.DIRECTORY_ENV, raising=False)
    for role, (env, _) in t.SLOTS.items():
        monkeypatch.setenv(env, VALUES[role])
    assert t.load_tokens() == VALUES
    monkeypatch.setenv(t.DIRECTORY_ENV, "")
    assert not any(t.load_tokens().values())


@pytest.mark.parametrize(
    "fault",
    [
        "missing_dir",
        "relative",
        "linked_dir",
        "linked_file",
        "hardlink",
        "public_dir",
        "public_file",
        "fifo",
        "too_long",
        "whitespace",
        "unicode",
        "double_newline",
        "empty",
    ],
)
def test_invalid_file_configuration_never_falls_back_to_environment(credentials, monkeypatch, fault):
    path = credentials / t.SLOTS["monitor"][1]
    if fault == "missing_dir":
        monkeypatch.setenv(t.DIRECTORY_ENV, str(credentials / "absent"))
    elif fault == "relative":
        monkeypatch.setenv(t.DIRECTORY_ENV, "credentials")
    elif fault == "linked_dir":
        alias = credentials.with_name("alias")
        alias.symlink_to(credentials, target_is_directory=True)
        monkeypatch.setenv(t.DIRECTORY_ENV, str(alias))
    elif fault == "linked_file":
        path.rename(credentials / "retained")
        path.symlink_to("retained")
    elif fault == "hardlink":
        os.link(path, credentials / "retained")
    elif fault == "public_dir":
        credentials.chmod(0o777)
    elif fault == "public_file":
        path.chmod(0o644)
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        path.chmod(0o600)
        raw = {
            "too_long": b"x" * 514,
            "whitespace": b" " + b"x" * 40,
            "unicode": "字".encode() * 40,
            "double_newline": b"x" * 40 + b"\n\n",
            "empty": b"",
        }[fault]
        path.write_bytes(raw)
        path.chmod(0o400)
    assert t.load_tokens() == dict.fromkeys(t.SLOTS, "")


def test_one_final_linefeed_is_supported_without_accepting_embedded_whitespace(credentials):
    path = credentials / t.SLOTS["monitor"][1]
    path.chmod(0o600)
    path.write_bytes(VALUES["monitor"].encode() + b"\n")
    path.chmod(0o400)
    assert t.load_tokens() == VALUES


def test_replaced_file_is_rejected_even_with_identical_bytes(credentials, monkeypatch):
    path = credentials / t.SLOTS["monitor"][1]
    actual = os.read
    changed = []

    def read(fd, count):
        raw = actual(fd, count)
        if raw == VALUES["monitor"].encode() and not changed:
            changed.append(True)
            path.rename(credentials / "retained")
            path.write_bytes(raw)
            path.chmod(0o400)
        return raw

    monkeypatch.setattr(t.os, "read", read)
    assert not any(t.load_tokens().values())
    assert changed == [True]


def test_file_rotation_is_used_next_request_without_an_auth_cache(credentials):
    assert t.load_tokens()["monitor"] == VALUES["monitor"]
    path = credentials / t.SLOTS["monitor"][1]
    replacement = credentials / "replacement"
    replacement.write_bytes(b"synthetic-rotated-" + b"r" * 48)
    replacement.chmod(0o400)
    os.replace(replacement, path)
    assert t.load_tokens()["monitor"] == "synthetic-rotated-" + "r" * 48


@pytest.fixture
def routes(credentials, monkeypatch):
    from server import feedback, feedback_progress, feedback_review

    feedback.init_feedback_db()
    feedback_review.init_review_db()
    feedback_progress.init_progress_db()
    monkeypatch.setattr(feedback_review, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(feedback_progress, "_read_deployed_revision", lambda: REVISION)
    monkeypatch.setattr(
        feedback_review, "_day", lambda: datetime(2026, 11, 3, 9, 30, tzinfo=feedback._SHANGHAI)
    )
    return {
        "monitor": "/api/internal/feedback-monitor/queue?limit=1",
        "review": "/api/internal/feedback-review/queue?batch_date=2026-11-03&limit=1",
        "progress": "/api/internal/feedback-progress/receipts/" + str(uuid4()),
        "worker": "/api/internal/feedback?limit=1",
    }


@pytest.mark.parametrize("role", list(t.SLOTS))
async def test_actual_role_routes_accept_only_their_own_private_file(client, credentials, routes, role):
    headers = {HEADERS[role]: VALUES[role], "X-Prism-Expected-Revision": REVISION}
    response = await client.get(routes[role], headers=headers)
    assert response.status_code == (404 if role == "progress" else 200), response.text
    assert not any(value in response.text for value in VALUES.values())
    for other in t.SLOTS:
        if other == role:
            continue
        wrong = await client.get(routes[role], headers={**headers, HEADERS[role]: VALUES[other]})
        assert wrong.status_code == 401
    legacy = "legacy-" + role + "-" + "x" * 40
    denied = await client.get(routes[role], headers={**headers, HEADERS[role]: legacy})
    assert denied.status_code == 401


@pytest.mark.parametrize("role", list(t.SLOTS))
async def test_shared_file_identity_cannot_gain_another_role(client, credentials, routes, role):
    other = next(key for key in t.SLOTS if key != role)
    path = credentials / t.SLOTS[other][1]
    path.chmod(0o600)
    path.write_bytes(VALUES[role].encode())
    path.chmod(0o400)
    response = await client.get(
        routes[role], headers={HEADERS[role]: VALUES[role], "X-Prism-Expected-Revision": REVISION}
    )
    assert response.status_code == 401


async def test_file_rotation_while_reading_a_write_body_rejects_before_commit(
    client, credentials, routes, auth_cookies, monkeypatch
):
    from server import feedback
    from server.database import get_db

    created = await client.post(
        "/api/feedback",
        cookies=auth_cookies,
        json={
            "category": "bug",
            "feature": "chat",
            "title": "Synthetic file-auth check",
            "description": "Synthetic reproducible check without private material.",
        },
    )
    assert created.status_code == 200
    item = created.json()["feedback"]
    original = feedback._payload

    async def rotate(request, model):
        body = await original(request, model)
        target = credentials / t.SLOTS["progress"][1]
        replacement = credentials / "replacement"
        replacement.write_bytes(b"synthetic-new-progress-" + b"p" * 40)
        replacement.chmod(0o400)
        os.replace(replacement, target)
        return body

    monkeypatch.setattr(feedback, "_payload", rotate)
    result = await client.post(
        "/api/internal/feedback-progress/" + str(item["id"]) + "/update",
        headers={HEADERS["progress"]: VALUES["progress"], "X-Prism-Expected-Revision": REVISION},
        json={
            "request_id": str(uuid4()),
            "expected_revision": item["revision_number"],
            "reported_revision": REVISION[:12],
            "stage": "triaging",
        },
    )
    assert result.status_code == 401
    with get_db() as db:
        row = db.execute(
            "SELECT status,revision_number FROM feedback_items WHERE id=?", [item["id"]]
        ).fetchone()
        assert dict(row) == {"status": item["status"], "revision_number": item["revision_number"]}
        assert db.execute("SELECT COUNT(*) FROM feedback_progress_receipts").fetchone()[0] == 0
