"""The HTTP model listing, health check and research route agree on configured models."""

import json

import pytest
from fastapi import HTTPException

from server import config, health, model_routes


async def test_openai_configuration_and_public_model_metadata(client, auth_cookies, monkeypatch):
    cfg = {
        "protocol": "openai_responses",
        "name": "合成 GPT",
        "model": "synthetic-model",
        "base_url": "https://api.openai.com/v1",
        "api_key": "synthetic-isolated-responses",
    }
    models = {"openai": cfg, "unconfigured": {**cfg, "model": ""}}
    monkeypatch.setattr(config, "MODELS", models)
    monkeypatch.setattr(config, "DEFAULT_MODEL", "openai")
    model_routes.init_model_routes_db()
    snapshot = model_routes.snapshot()
    assert snapshot["models"][0]["endpoint_host"] == "openai"
    assert snapshot["models"][0]["protocol"] == "openai_responses"
    public_models = (await client.get("/api/models", cookies=auth_cookies)).json()
    assert [m["id"] for m in public_models if m["default"]] == ["openai"]
    assert {m["id"]: m["configured"] for m in public_models} == {"openai": True, "unconfigured": False}
    assert {m["id"]: m["configured"] for m in health._model_checks()} == {
        "openai": True,
        "unconfigured": False,
    }
    assert cfg["api_key"] not in json.dumps([snapshot, public_models])
    with pytest.raises(HTTPException) as missing:
        model_routes.resolve("research", explicit_model="unconfigured")
    assert missing.value.status_code == 503
