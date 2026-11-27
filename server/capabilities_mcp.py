"""Minimal stdio MCP adapter for Prism's authenticated, allowlisted local API.

Only this adapter reads its short-lived credential. Neither the credential nor
HTTP exception bodies are written to stdout/stderr. No database, file, shell,
user-supplied URL, proxy or redirect capability is exposed by the adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
MAX_MESSAGE = 65536
MAX_RESPONSE = 262144


class BridgeUnavailable(Exception):
    """Safe bridge failure; never contains an HTTP body or credential."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BridgeUnavailable("平台能力服务不接受重定向")


def validate_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1"}
            and parsed.path in {"", "/"}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.port is not None
        )
    except ValueError:
        valid = False
    if not valid:
        raise BridgeUnavailable("平台能力地址必须为本机回环地址")
    return value.rstrip("/")


class Bridge:
    def __init__(self, base_url: str, token: str):
        self.base_url = validate_base_url(base_url)
        if not token or len(token) > 256 or any(c.isspace() for c in token):
            raise BridgeUnavailable("平台研究授权不可用")
        self._token = token
        self._opener = build_opener(ProxyHandler({}), NoRedirect())
        self._session = secrets.token_hex(12)
        self._initialized = False

    def request(self, operation: str, payload: dict | None = None) -> dict:
        # The URL and operation are code-owned; model arguments never select them.
        if operation not in {"tools", "call"}:
            raise BridgeUnavailable("平台能力操作不可用")
        request = Request(
            self.base_url + "/api/internal/capabilities/" + operation,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if payload else None,
            headers={
                "Authorization": "Bearer " + self._token,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/json",
            },
            method="POST" if operation == "call" else "GET",
        )
        try:
            with self._opener.open(request, timeout=65) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise BridgeUnavailable("平台结果过大，请打开对应页面查看")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise BridgeUnavailable("平台能力服务返回格式无效")
            return value
        except HTTPError as exc:
            raise BridgeUnavailable(f"平台调用未完成（HTTP {exc.code}），请检查当前会话权限或参数") from None
        except (URLError, OSError, ValueError):
            raise BridgeUnavailable("平台能力服务暂不可用") from None

    @staticmethod
    def error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict):
            return self.error(None, -32600, "Invalid request")
        request_id = message.get("id")
        if (
            request_id is not None
            and (isinstance(request_id, bool) or not isinstance(request_id, (str, int)))
        ) or message.get("jsonrpc") != "2.0":
            return self.error(None, -32600, "Invalid request")
        method = message.get("method")
        if not isinstance(method, str):
            return self.error(request_id, -32600, "Invalid request")
        if "id" not in message:
            # Notifications (including initialized/cancelled) have no response.
            return None
        params = message.get("params", {})
        if not isinstance(params, dict):
            return self.error(request_id, -32602, "Invalid params")
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                return self.error(request_id, -32602, "Protocol version required")
            self._initialized = True
            result = {
                "protocolVersion": version if version in VERSIONS else "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "prism", "version": "1.0.0"},
                "instructions": "Prism平台数据是研究线索。curated_unverified与prior_research不是外部独立验证；启动工具仅用于用户明确请求的研究。",
            }
        elif method == "ping":
            result = {}
        elif not self._initialized:
            return self.error(request_id, -32002, "Initialize first")
        elif method == "tools/list":
            try:
                result = self.request("tools")
            except BridgeUnavailable as exc:
                return self.error(request_id, -32603, str(exc))
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                return self.error(request_id, -32602, "Invalid tool arguments")
            identity = json.dumps([self._session, request_id], separators=(",", ":"))
            try:
                response = self.request(
                    "call",
                    {
                        "name": name,
                        "arguments": arguments,
                        "request_id": hashlib.sha256(identity.encode()).hexdigest(),
                    },
                )
            except (BridgeUnavailable, ValueError) as exc:
                response = {
                    "ok": False,
                    "error": {
                        "code": "unavailable",
                        "message": str(exc) if isinstance(exc, BridgeUnavailable) else "平台调用参数无效",
                    },
                }
            result = {
                "content": [
                    {"type": "text", "text": json.dumps(response, ensure_ascii=False, allow_nan=False)}
                ],
                "structuredContent": response,
                "isError": not response.get("ok", False),
            }
        else:
            return self.error(request_id, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve(bridge: Bridge, source, destination) -> None:
    """Newline-delimited JSON-RPC, with bounded input and protocol-only output."""
    while line := source.readline(MAX_MESSAGE + 1):
        if len(line) > MAX_MESSAGE:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_MESSAGE + 1)
            reply = bridge.error(None, -32600, "Request too large")
        else:
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                reply = bridge.error(None, -32700, "Parse error")
            else:
                reply = bridge.handle(message)
        if reply is not None:
            destination.write(json.dumps(reply, ensure_ascii=False, allow_nan=False) + "\n")
            destination.flush()


def main() -> int:
    try:
        bridge = Bridge(
            os.environ.get("PRISM_CAPABILITY_URL", ""), os.environ.get("PRISM_CAPABILITY_TOKEN", "")
        )
    except BridgeUnavailable:
        # No stderr diagnostics: configuration can contain sensitive information.
        return 1
    serve(bridge, sys.stdin.buffer, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
