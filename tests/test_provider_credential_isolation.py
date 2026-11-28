"""Load the real startup configuration with synthetic per-provider credentials."""

import runpy

import pytest

from scripts.check_provider_key_isolation import CASES, CONFIG, check_case


@pytest.mark.parametrize(
    "configured,expected",
    CASES,
)
def test_keys_stay_with_their_configured_provider(tmp_path, configured, expected):
    assert check_case(configured, expected, tmp_path) == {
        "provider_key_isolation": True,
        "network_requests": 0,
    }


def test_startup_gate_rejects_cross_region_credential_inheritance(tmp_path):
    bad = tmp_path / "cross-region-config.py"
    original = CONFIG.read_text()
    assert '"api_key": MINIMAX_CN_API_KEY' in original
    bad.write_text(original.replace('"api_key": MINIMAX_CN_API_KEY', '"api_key": MINIMAX_API_KEY'))
    configured, expected = CASES[-1]
    with pytest.raises(RuntimeError, match="provider_key_isolation_failed"):
        check_case(configured, expected, tmp_path, config=bad)


def test_domestic_catalog_selects_current_official_messages_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("STOCK_DATA_DIR", str(tmp_path))
    for name in ("LLM_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("MINIMAX_CN_API_KEY", "synthetic-domestic-key")
    monkeypatch.setenv("PRISM_DEFAULT_MODEL", "minimax-cn-m3")
    cfg = runpy.run_path(str(CONFIG))
    from server.agent.provider import configuration_status

    selected = cfg["MODELS"][cfg["DEFAULT_MODEL"]]
    assert selected["base_url"] == "https://api.minimax.cn/anthropic/v1/"
    assert selected["model"] == "MiniMax-M3"
    status = configuration_status(selected)
    assert status["configured"] and status["endpoint_host"] == "minimax-cn"


async def test_gemini_native_streaming_and_tool_call_adapter():
    import json

    import httpx

    from server.agent import provider
    from server.agent.provider_gemini import GeminiGenerateContentProvider

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["key"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content.decode())
        sse = (
            "data: "
            + json.dumps(
                {
                    "candidates": [
                        {
                            "content": {
                                "role": "model",
                                "parts": [
                                    {"text": "核心结论："},
                                    {"functionCall": {"name": "quote_get", "args": {"ticker": "601138"}}},
                                ],
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 18},
                },
                ensure_ascii=False,
            )
            + "\n\n"
        ).encode("utf-8")
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    cfg = {
        "protocol": "gemini_generate_content",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "api_key": "AQ.synthetic-gemini-ultra-key",
        "model": "gemini-3.8-flash",
    }
    st = provider.configuration_status(cfg)
    assert st["configured"] and st["endpoint_host"] == "gemini"

    p = GeminiGenerateContentProvider(transport=httpx.MockTransport(handler))
    events = [
        ev
        async for ev in p.stream(
            [{"role": "user", "content": "查一下工业富联"}],
            system="你是中文投研助手",
            tools=[
                {
                    "name": "quote_get",
                    "description": "获取实时行情",
                    "input_schema": {
                        "type": "object",
                        "properties": {"ticker": {"type": "string"}},
                        "additionalProperties": False,
                    },
                }
            ],
            model_cfg=cfg,
            max_tokens=1024,
            timeout_seconds=10,
        )
    ]
    assert captured["key"] == "AQ.synthetic-gemini-ultra-key"
    assert "/models/gemini-3.8-flash:streamGenerateContent?alt=sse" in captured["url"]
    assert any(ev["type"] == "text_delta" and ev["text"] == "核心结论：" for ev in events)
    assert any(
        ev["type"] == "tool_call" and ev["name"] == "quote_get" and ev["arguments"] == {"ticker": "601138"}
        for ev in events
    )
    done = [ev for ev in events if ev["type"] == "done"][-1]
    assert done["stop_reason"] == "tool_use"
    assert done["usage_complete"] is True


def test_kimi_glm_claude_gemini_status_recognition():
    from server.agent.provider import configuration_status

    cases = [
        ("https://api.moonshot.cn/anthropic", "anthropic_messages", "kimi-k2.5", "kimi"),
        ("https://open.bigmodel.cn/api/anthropic", "anthropic_messages", "glm-5", "glm"),
        ("https://api.anthropic.com/v1", "anthropic_messages", "claude-sonnet-5-5", "anthropic"),
        (
            "https://generativelanguage.googleapis.com/v1beta",
            "gemini_generate_content",
            "gemini-3.8-flash",
            "gemini",
        ),
    ]
    for base, proto, model, host in cases:
        st = configuration_status(
            {"base_url": base, "protocol": proto, "api_key": "synthetic-key-1234", "model": model}
        )
        assert st["configured"] and st["endpoint_host"] == host, (base, st)
