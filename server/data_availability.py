"""Finite public-data failure metadata; never retain exception text or headers."""

import httpx


def source_failure(error):
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return {
            "error_code": "report_not_found"
            if status == 404
            else "access_denied"
            if status in {401, 403}
            else "http_error",
            "http_status": status,
        }
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return {"error_code": "source_timeout"}
    if isinstance(error, httpx.HTTPError):
        return {"error_code": "network_error"}
    return {"error_code": "invalid_source_data"}
