"""Load the real startup configuration with synthetic per-provider credentials."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CONFIG = Path(__file__).resolve().parents[1] / "server/config.py"


@pytest.mark.parametrize(
    "configured,expected",
    [
        ({"LLM_API_KEY": "synthetic-legacy"}, ["synthetic-legacy", "", ""]),
        (
            {"LLM_API_KEY": "synthetic-legacy", "DEEPSEEK_API_KEY": "synthetic-deepseek"},
            ["synthetic-deepseek", "", ""],
        ),
        (
            {"LLM_API_KEY": "synthetic-legacy", "MINIMAX_API_KEY": "synthetic-minimax"},
            ["synthetic-legacy", "synthetic-minimax", ""],
        ),
        (
            {"LLM_API_KEY": "synthetic-legacy", "MINIMAX_API_KEY": ""},
            ["synthetic-legacy", "", ""],
        ),
        (
            {"DEEPSEEK_API_KEY": "synthetic-deepseek", "OPENAI_API_KEY": "synthetic-openai"},
            ["synthetic-deepseek", "", "synthetic-openai"],
        ),
    ],
)
def test_keys_stay_with_their_configured_provider(tmp_path, configured, expected):
    program = """
import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('isolated_provider_config',sys.argv[1])
config=importlib.util.module_from_spec(spec);spec.loader.exec_module(config)
expected=json.loads(sys.argv[2])
actual=[config.DEEPSEEK_API_KEY,config.MINIMAX_API_KEY,config.OPENAI_API_KEY]
assert actual==expected
for name,model in config.MODELS.items():
    key=expected[0 if name.startswith('deepseek-') else 1 if name.startswith('minimax-') else 2]
    assert model['api_key']==key
print(json.dumps({'provider_key_isolation':True,'network_requests':0}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", program, str(CONFIG), json.dumps(expected)],
        cwd=tmp_path,
        env={
            "PATH": os.defpath,
            "HOME": str(tmp_path),
            "STOCK_DATA_DIR": str(tmp_path / "synthetic-data"),
            **configured,
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"provider_key_isolation": True, "network_requests": 0}
