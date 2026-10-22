#!/usr/bin/env python3
"""Offline policy/design helpers; no transport, credential reader or paid runner.

Future reviewed controller: same-PID bootstrap -> real kernel/egress witnesses
-> release only DEEPSEEK_API_KEY to a separate broker's memory -> synthetic-token
runner. Pure JSON checks here do not create/attest those runtime witnesses.

Worker stays PrivateNetwork=yes. Its numeric-loopback HTTP relay reaches only
a root-owned Unix socket. A separate dedicated broker pins public DNS answers,
validates upstream HTTPS, denies private/metadata targets, counts requests and
injects the real key only for api.deepseek.com. CONNECT alone cannot count TLS
requests. Candidate APIs/MCP use a separate private gateway forwarding to the
real candidate UDS, exposing only owned market_quote/research_read calls.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import time
from datetime import datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

MODEL = "deepseek-v4-flash"  # Frozen f089 spelling; provider's legacy alias remains accepted.
MODEL_URL = "https://api.deepseek.com/anthropic"
PUBLIC_HOSTS = {"api.deepseek.com", "www.sse.com.cn", "qt.gtimg.cn", "money.finance.sina.com.cn"}
LIVE_EXECUTION_IMPLEMENTED = False
PRIMARY_SOURCES = (
    "https://code.claude.com/docs/en/cli-reference",
    "https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/",
    "https://api-docs.deepseek.com/guides/anthropic_api/",
    "https://api-docs.deepseek.com/quick_start/",
    "https://api-docs.deepseek.com/guides/search/",
    "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool",
)
LIMITS = {"messages": 6, "count_tokens": 2, "quote": 1, "kline": 1, "public_fetch": 1}
REQUIRED_WITNESSES = {
    "dedicated_worker_uid",
    "dedicated_broker_uid",
    "worker_private_network_namespace",
    "production_8088_denied",
    "vibe_8899_denied",
    "metadata_denied",
    "private_network_denied",
    "production_home_denied",
    "production_proc_denied",
    "candidate_write_denied",
    "config_write_denied",
    "egress_allowlist_enforced",
    "public_dns_pinned",
    "upstream_tls_verified",
    "synthetic_empty_data",
}


class PolicyError(RuntimeError):
    """Fixed safe categories; never retain a request, key or raw provider text."""


def public_peers(host, addresses):
    """Validate supplied DNS results. The transport must dial these pinned IPs."""
    if host not in PUBLIC_HOSTS or not isinstance(addresses, (list, tuple)) or not addresses:
        raise PolicyError("egress_host_rejected")
    peers = []
    for value in addresses:
        if not isinstance(value, str) or "%" in value:
            raise PolicyError("egress_address_rejected")
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise PolicyError("egress_address_rejected") from None
        if (
            not address.is_global
            or address.is_multicast
            or address.is_reserved
            or isinstance(address, ipaddress.IPv6Address)
            and (
                address.ipv4_mapped
                or address.sixtofour
                or address.teredo
                or address.is_site_local
                or address in ipaddress.ip_network("64:ff9b::/96")
            )
        ):
            raise PolicyError("egress_address_rejected")
        peers.append(str(address))
    return tuple(sorted(set(peers)))


def https_target(url, *, role):
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise PolicyError("egress_url_rejected")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme == "https" and parsed.port in {None, 443}
    except ValueError:
        valid = False
    if (
        not valid
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.hostname not in PUBLIC_HOSTS
    ):
        raise PolicyError("egress_url_rejected")
    if role == "model":
        paths = {"/anthropic/v1/messages": "messages", "/anthropic/v1/messages/count_tokens": "count_tokens"}
        if (
            parsed.hostname != "api.deepseek.com"
            or parsed.path not in paths
            or parsed.query not in {"", "beta=true"}
        ):
            raise PolicyError("model_route_rejected")
        return paths[parsed.path]
    if (
        role == "fetch"
        and parsed.hostname == "www.sse.com.cn"
        and parsed.path == "/aboutus/overview/"
        and not parsed.query
    ):
        return "public_fetch"
    if (
        role == "quote"
        and parsed.hostname == "qt.gtimg.cn"
        and parsed.path == "/q=sh600519"
        and not parsed.query
    ):
        return "quote"
    if (
        role == "kline"
        and parsed.hostname == "money.finance.sina.com.cn"
        and parsed.path == "/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
        and parse_qs(parsed.query, keep_blank_values=True)
        == {
            "symbol": ["sh600519"],
            "scale": ["240"],
            "ma": ["no"],
            "datalen": ["10"],
        }
    ):
        return "kline"
    raise PolicyError("public_route_rejected")


class RequestBudget:
    def __init__(self, *, clock=time.monotonic):
        self.clock, self.started = clock, clock()
        self.counts = dict.fromkeys(LIMITS, 0)

    def reserve(self, operation):
        # Reserve before upstream I/O; failed requests also consume a slot. No retry.
        if operation not in LIMITS or self.clock() - self.started >= 180:
            raise PolicyError("request_budget_exhausted")
        if self.counts[operation] >= LIMITS[operation]:
            raise PolicyError("request_budget_exhausted")
        self.counts[operation] += 1


def bounded_model_body(body):
    """Explicit probe-only override, not an identical production request claim."""
    if not isinstance(body, dict) or body.get("model") != MODEL or type(body.get("max_tokens")) is not int:
        raise PolicyError("model_request_rejected")
    try:
        size = len(json.dumps(body, ensure_ascii=True, allow_nan=False).encode())
    except (TypeError, ValueError, OverflowError):
        raise PolicyError("model_request_rejected") from None
    if body["max_tokens"] <= 0 or size > 512 * 1024:
        raise PolicyError("model_request_rejected")
    result = dict(body)
    result["max_tokens"] = min(body["max_tokens"], 1024)
    return result


def require_release_witnesses(witness, binding, *, now):
    """Structural check only; a reviewed root executor must establish actual facts."""
    expected = {"candidate": 40, "cli_sha256": 64, "probe_sha256": 64, "nonce": 32}
    if (
        not isinstance(binding, dict)
        or set(binding) != {*expected, "worker_pid", "broker_pid"}
        or any(
            not isinstance(binding.get(key), str)
            or not re.fullmatch("[0-9a-f]{" + str(length) + "}", binding[key])
            for key, length in expected.items()
        )
        or any(type(binding.get(key)) is not int or binding[key] <= 1 for key in ("worker_pid", "broker_pid"))
        or binding["worker_pid"] == binding["broker_pid"]
    ):
        raise PolicyError("credential_release_blocked")
    if (
        type(now) is not int
        or not isinstance(witness, dict)
        or witness.get("binding") != binding
        or type(witness.get("checked_at")) is not int
        or not 0 <= now - witness["checked_at"] <= 120
        or not isinstance(witness.get("checks"), dict)
        or set(witness["checks"]) != REQUIRED_WITNESSES
        or any(value is not True for value in witness["checks"].values())
    ):
        raise PolicyError("credential_release_blocked")


def owned_mcp_call(name, arguments, context):
    if (
        not isinstance(context, dict)
        or not isinstance(context.get("owner"), str)
        or not context["owner"]
        or context["owner"] != context.get("authenticated_owner")
        or context.get("synthetic_data_only") is not True
    ):
        raise PolicyError("owned_bridge_required")
    if name == "market_quote" and arguments == {"ticker": "600519"}:
        return
    if name == "research_read" and arguments == {} and context.get("run_id"):
        return
    if name == "indicator_list" and arguments == {}:
        return
    raise PolicyError("mcp_operation_rejected")


def shanghai_clock(context, started, finished):
    try:
        observed = datetime.fromisoformat(context["as_of"])
        epoch = observed.timestamp()
        expected = datetime.fromtimestamp(epoch, ZoneInfo("Asia/Shanghai"))
        return (
            context.get("timezone") == "Asia/Shanghai"
            and observed.utcoffset() == expected.utcoffset()
            and context.get("date") == expected.date().isoformat()
            and started - 5 <= epoch <= finished + 5
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def market_quote_receipt(value, started, finished):
    return (
        isinstance(value, dict)
        and value.get("ticker") == "600519"
        and value.get("source") == "tencent"
        and value.get("stale") is False
        and type(value.get("price")) in {int, float}
        and math.isfinite(value["price"])
        and value["price"] > 0
        and type(value.get("retrieved_at")) is int
        and started <= value["retrieved_at"] <= finished
        and type(value.get("data_timestamp")) is int
        and 0 < value["data_timestamp"] <= finished + 5
        and value.get("timestamp_kind") == "trade"
        and value.get("data_kind") == "quote"
    )


def plan():
    return {
        "kind": "live_integration_design_only",
        "live_execution_implemented": LIVE_EXECUTION_IMPLEMENTED,
        "credential_release_allowed": False,
        "production_verified": False,
        "model_url": MODEL_URL,
        "model": MODEL,
        "primary_sources": list(PRIMARY_SOURCES),
        "limits": {
            **LIMITS,
            "wall_seconds": 180,
            "upstream_timeout_seconds": 25,
            "cli_invocations": 2,
            "cli_turns_per_invocation": [4, 2],
            "max_tokens_per_message": 1024,
            "retries": 0,
        },
        "money_limit_is_estimate": True,
        "provider_internal_search_limit_verified": False,
        "native_webfetch_status": "blocked_pending_separate_connect_proof",
        "native_websearch_status": "unverified_until_matched_real_cli_frames",
        "transport_status": "design_only_no_broker_or_relay_implemented",
        "topology": "private worker loopback HTTP to UDS to dedicated broker; real key stays broker-only",
        "followup_connect_scope": {
            "implemented": False,
            "tls_mitm": False,
            "provider_host_forbidden": True,
            "requirements": [
                "exact official source hosts on port 443",
                "public DNS pinning per connection",
                "private and metadata denial",
                "connection byte and time bounds",
                "native CLI TLS verification",
                "unknown redirect hosts blocked",
            ],
        },
        "request_mutations_disclosed": [
            "local broker base_url",
            "synthetic runner token",
            "max_tokens <= 1024",
            "bounded CLI turn flags",
        ],
        "required_witnesses": sorted(REQUIRED_WITNESSES),
        "phases": [
            "fixed CLI protocol fixture",
            "same-PID kernel and egress proof",
            "public market HTTPS without key",
            "broker-only credential release",
            "owned authenticated /api/chat/stream",
            "matched native tool frames",
            "owned MCP roundtrip and foreign-owner 404",
            "Shanghai clock and private ledger readback",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(plan(), ensure_ascii=False, sort_keys=True))
