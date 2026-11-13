"""The issued CSRF cookie must be echoed on browser writes."""

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from server.auth import verify_csrf


@pytest.mark.parametrize(
    "cookie,header", [(None, None), ("issued", None), (None, "issued"), ("issued", "different"), ("", "")]
)
def test_csrf_rejects_missing_or_mismatched_pair(cookie, header):
    headers = [(b"x-requested-with", b"XMLHttpRequest")]
    if cookie is not None:
        headers.append((b"cookie", ("csrf_token=" + cookie).encode()))
    if header is not None:
        headers.append((b"x-csrf-token", header.encode()))
    with pytest.raises(HTTPException) as error:
        verify_csrf(Request({"type": "http", "method": "POST", "headers": headers}))
    assert error.value.status_code == 403


def test_csrf_accepts_matching_pair():
    verify_csrf(
        Request(
            {
                "type": "http",
                "method": "POST",
                "headers": [
                    (b"x-requested-with", b"XMLHttpRequest"),
                    (b"cookie", b"csrf_token=issued"),
                    (b"x-csrf-token", b"issued"),
                ],
            }
        )
    )


def test_csrf_matching_pair_does_not_replace_requested_with():
    with pytest.raises(HTTPException) as error:
        verify_csrf(
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "headers": [(b"cookie", b"csrf_token=issued"), (b"x-csrf-token", b"issued")],
                }
            )
        )
    assert error.value.status_code == 403


async def test_login_token_is_required_by_real_http_writes(test_user):
    from httpx import ASGITransport, AsyncClient

    from server.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/login", json=test_user)
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert client.cookies["csrf_token"] == csrf
        headers = {"X-Requested-With": "XMLHttpRequest"}
        for bad in (None, "wrong"):
            supplied = headers if bad is None else {**headers, "X-CSRF-Token": bad}
            response = await client.post("/api/convs", headers=supplied, json={"title": "synthetic"})
            assert response.status_code == 403
        response = await client.post(
            "/api/convs", headers={**headers, "X-CSRF-Token": csrf}, json={"title": "synthetic"}
        )
        assert response.status_code == 200
