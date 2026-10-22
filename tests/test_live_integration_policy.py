"""Offline policy checks only; no kernel proof, real provider or market acceptance."""

import copy
import json
from datetime import datetime

import pytest

from scripts import live_integration_policy as policy


@pytest.mark.parametrize(
    "url,role,operation",
    [
        ("https://api.deepseek.com/anthropic/v1/messages", "model", "messages"),
        ("https://api.deepseek.com/anthropic/v1/messages?beta=true", "model", "messages"),
        ("https://api.deepseek.com/anthropic/v1/messages/count_tokens", "model", "count_tokens"),
        ("https://www.sse.com.cn/aboutus/overview/", "fetch", "public_fetch"),
        ("https://qt.gtimg.cn/q=sh600519", "quote", "quote"),
        (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=10",
            "kline",
            "kline",
        ),
    ],
)
def test_only_fixed_public_routes_are_selected(url, role, operation):
    assert policy.https_target(url, role=role) == operation


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://[broken/",
        "https://api.deepseek.com:bad/anthropic/v1/messages",
        "http://api.deepseek.com/anthropic/v1/messages",
        "https://api.deepseek.com:8088/anthropic/v1/messages",
        "https://token@api.deepseek.com/anthropic/v1/messages",
        "https://api.deepseek.com/anthropic/v1/messages#credential",
        "https://api.deepseek.com/anthropic/v1/messages?redirect=https://evil.test",
        "https://api.deepseek.com.attacker.test/anthropic/v1/messages",
        "https://127.0.0.1:8088/anthropic/v1/messages",
        "https://169.254.169.254/latest/meta-data/",
        "https://api.deepseek.com/anthropic/v1/messages\n",
        "https://api.deepseek.com/anthropic/v1/messages/../other",
    ],
)
def test_bad_model_routes_are_denied(url):
    with pytest.raises(policy.PolicyError):
        policy.https_target(url, role="model")


@pytest.mark.parametrize(
    "url,role",
    [
        ("https://www.sse.com.cn/other", "fetch"),
        ("https://www.sse.com.cn/aboutus/overview/?next=other", "fetch"),
        ("https://qt.gtimg.cn/q=sh600000", "quote"),
        ("https://qt.gtimg.cn/?q=sh600519", "quote"),
        (
            "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol=sh600519&scale=240&ma=no&datalen=999",
            "kline",
        ),
        ("https://api.deepseek.com/anthropic/v1/messages", "connect"),
    ],
)
def test_public_routes_cannot_expand_the_scope(url, role):
    with pytest.raises(policy.PolicyError):
        policy.https_target(url, role=role)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "192.0.2.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fec0::1",
        "ff02::1",
        "::ffff:8.8.8.8",
        "2002:0808:0808::1",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "64:ff9b::808:808",
        "2606:4700:4700::1111%lo0",
        "not-an-ip",
        None,
        134744072,
    ],
)
def test_all_dns_answers_must_be_public_and_without_tunnels(address):
    with pytest.raises(policy.PolicyError, match="egress_address_rejected"):
        policy.public_peers("api.deepseek.com", ["8.8.8.8", address])


def test_dns_pinning_is_a_transport_requirement_and_not_a_dns_lookup():
    assert policy.public_peers("api.deepseek.com", ["8.8.8.8", "2606:4700:4700::1111", "8.8.8.8"]) == (
        "2606:4700:4700::1111",
        "8.8.8.8",
    )
    for host, peers in [("unknown.test", ["8.8.8.8"]), ("api.deepseek.com", [])]:
        with pytest.raises(policy.PolicyError, match="egress_host_rejected"):
            policy.public_peers(host, peers)


def test_failed_requests_consume_budget_and_expiry_denies_every_operation():
    now = [0.0]
    budget = policy.RequestBudget(clock=lambda: now[0])
    for _ in range(6):
        budget.reserve("messages")  # Reserve before I/O, including any failed response.
    with pytest.raises(policy.PolicyError, match="request_budget_exhausted"):
        budget.reserve("messages")
    budget.reserve("quote")
    with pytest.raises(policy.PolicyError):
        budget.reserve("quote")
    now[0] = 180.0
    for operation in ["kline", "count_tokens", "unknown"]:
        with pytest.raises(policy.PolicyError):
            budget.reserve(operation)
    assert budget.counts["messages"] == 6 and budget.counts["kline"] == 0


def test_explicit_probe_token_override_preserves_original_request():
    request = {
        "model": policy.MODEL,
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": "public synthetic probe"}],
    }
    original = copy.deepcopy(request)
    assert policy.bounded_model_body(request)["max_tokens"] == 1024
    assert request == original
    request["max_tokens"] = 20
    assert policy.bounded_model_body(request)["max_tokens"] == 20


@pytest.mark.parametrize(
    "extra",
    [
        {"max_tokens": True},
        {"max_tokens": 0},
        {"max_tokens": "10"},
        {"model": "other"},
        {"temperature": float("nan")},
        {"messages": "x" * (512 * 1024)},
        {"messages": b"bad"},
    ],
)
def test_model_request_limits_fail_closed(extra):
    with pytest.raises(policy.PolicyError, match="model_request_rejected"):
        policy.bounded_model_body({"model": policy.MODEL, "max_tokens": 10, **extra})


def witnesses():
    binding = {
        "candidate": "a" * 40,
        "cli_sha256": "b" * 64,
        "probe_sha256": "c" * 64,
        "nonce": "d" * 32,
        "worker_pid": 42,
        "broker_pid": 43,
    }
    witness = {
        "binding": copy.deepcopy(binding),
        "checked_at": 1000,
        "checks": dict.fromkeys(policy.REQUIRED_WITNESSES, True),
    }
    return witness, binding


def test_witness_validator_only_checks_structure_without_releasing_credentials():
    witness, binding = witnesses()
    assert policy.require_release_witnesses(witness, binding, now=1001) is None
    assert policy.LIVE_EXECUTION_IMPLEMENTED is False


@pytest.mark.parametrize(
    "defect",
    ["missing", "truthy", "extra", "future", "old", "pid", "nonce", "same-pid", "binding", "time-type"],
)
def test_incomplete_stale_or_unbound_witness_cannot_release_credentials(defect):
    witness, binding = witnesses()
    now = 1001
    if defect == "missing":
        witness["checks"].pop("upstream_tls_verified")
    elif defect == "truthy":
        witness["checks"]["upstream_tls_verified"] = 1
    elif defect == "extra":
        witness["checks"]["unreviewed_check"] = True
    elif defect == "future":
        witness["checked_at"] = 1002
    elif defect == "old":
        witness["checked_at"] = 880
    elif defect == "pid":
        binding["worker_pid"] = True
    elif defect == "nonce":
        binding["nonce"] = "bad"
    elif defect == "same-pid":
        binding["broker_pid"] = binding["worker_pid"]
    elif defect == "binding":
        witness["binding"]["candidate"] = "e" * 40
    else:
        now = "1001"
    with pytest.raises(policy.PolicyError, match="credential_release_blocked"):
        policy.require_release_witnesses(witness, binding, now=now)


@pytest.mark.parametrize(
    "name,arguments", [("market_quote", {"ticker": "600519"}), ("research_read", {}), ("indicator_list", {})]
)
def test_read_only_owned_mcp_allowlist(name, arguments):
    context = {
        "owner": "synthetic-owner",
        "authenticated_owner": "synthetic-owner",
        "synthetic_data_only": True,
        "run_id": "synthetic-run",
    }
    assert policy.owned_mcp_call(name, arguments, context) is None
    for bad_context in [
        None,
        {},
        {**context, "owner": ""},
        {**context, "authenticated_owner": "other"},
        {**context, "synthetic_data_only": 1},
    ]:
        with pytest.raises(policy.PolicyError, match="owned_bridge_required"):
            policy.owned_mcp_call(name, arguments, bad_context)
    for bad_name, bad_args in [
        ("research_start", {}),
        ("config_write", {}),
        ("research_read", {"id": "foreign"}),
        ("market_quote", {"ticker": "600519", "write": True}),
    ]:
        with pytest.raises(policy.PolicyError, match="mcp_operation_rejected"):
            policy.owned_mcp_call(bad_name, bad_args, context)


def test_server_clock_requires_observed_shanghai_date_and_real_window():
    epoch = int(datetime.fromisoformat("2026-10-22T00:00:01+08:00").timestamp())
    context = {"as_of": "2026-10-22T00:00:01+08:00", "timezone": "Asia/Shanghai", "date": "2026-10-22"}
    assert policy.shanghai_clock(context, epoch - 2, epoch + 2)
    for changes in [
        {"date": "2026-10-21"},
        {"timezone": "UTC"},
        {"as_of": "2026-10-21T16:00:01+00:00"},
        {"as_of": "2026-10-22T00:00:01"},
        {"as_of": "broken"},
    ]:
        assert not policy.shanghai_clock({**context, **changes}, epoch - 2, epoch + 2)
    assert not policy.shanghai_clock(context, epoch + 6, epoch + 10)


def test_market_receipt_keeps_retrieval_distinct_from_older_trade_time():
    quote = {
        "ticker": "600519",
        "source": "tencent",
        "stale": False,
        "price": 1500.5,
        "retrieved_at": 1001,
        "data_timestamp": 500,
        "timestamp_kind": "trade",
        "data_kind": "quote",
    }
    assert policy.market_quote_receipt(quote, 1000, 1002)
    for changes in [
        {"source": "model"},
        {"price": float("inf")},
        {"price": True},
        {"stale": True},
        {"retrieved_at": 999},
        {"data_timestamp": 1008},
        {"data_timestamp": 0},
        {"timestamp_kind": "retrieval"},
    ]:
        assert not policy.market_quote_receipt({**quote, **changes}, 1000, 1002)


def test_printable_plan_never_claims_live_execution_or_native_fetch_acceptance():
    plan = json.loads(json.dumps(policy.plan()))
    assert plan["live_execution_implemented"] is False
    assert plan["credential_release_allowed"] is False and plan["production_verified"] is False
    assert plan["native_webfetch_status"] == "blocked_pending_separate_connect_proof"
    assert plan["money_limit_is_estimate"] is True
    assert plan["provider_internal_search_limit_verified"] is False
    assert plan["followup_connect_scope"]["provider_host_forbidden"] is True
    assert plan["followup_connect_scope"]["tls_mitm"] is False
