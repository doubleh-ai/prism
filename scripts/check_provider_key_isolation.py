"""Fast stdlib-only startup gate with synthetic credentials and no network."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "server/config.py"
CASES = [
    ({"LLM_API_KEY": "synthetic-legacy"}, ["synthetic-legacy", "", "", ""]),
    (
        {"LLM_API_KEY": "synthetic-legacy", "DEEPSEEK_API_KEY": "synthetic-deepseek"},
        ["synthetic-deepseek", "", "", ""],
    ),
    (
        {"LLM_API_KEY": "synthetic-legacy", "MINIMAX_API_KEY": "synthetic-minimax"},
        ["synthetic-legacy", "synthetic-minimax", "", ""],
    ),
    ({"LLM_API_KEY": "synthetic-legacy", "MINIMAX_API_KEY": ""}, ["synthetic-legacy", "", "", ""]),
    (
        {"DEEPSEEK_API_KEY": "synthetic-deepseek", "OPENAI_API_KEY": "synthetic-openai"},
        ["synthetic-deepseek", "", "synthetic-openai", ""],
    ),
    ({"MINIMAX_CN_API_KEY": "synthetic-domestic"}, ["", "", "", "synthetic-domestic"]),
    (
        {"MINIMAX_CN_API_KEY": "synthetic-domestic", "MINIMAX_API_KEY": "synthetic-global"},
        ["", "synthetic-global", "", "synthetic-domestic"],
    ),
]
PROGRAM = """
import importlib.util,json,socket,sys
def forbidden(*args,**kwargs):raise RuntimeError('startup_network_forbidden')
socket.socket.connect=forbidden
socket.create_connection=forbidden
spec=importlib.util.spec_from_file_location('isolated_provider_config',sys.argv[1])
config=importlib.util.module_from_spec(spec);spec.loader.exec_module(config)
expected=json.loads(sys.argv[2])
actual=[config.DEEPSEEK_API_KEY,config.MINIMAX_API_KEY,config.OPENAI_API_KEY,config.MINIMAX_CN_API_KEY]
assert actual==expected
endpoints={'https://api.deepseek.com/anthropic':0,'https://api.minimax.io/anthropic/v1':1,
 'https://api.openai.com/v1':2,'https://api.minimaxi.com/anthropic/v1':3}
for model in config.MODELS.values():
 assert model['api_key']==expected[endpoints[model['base_url'].rstrip('/')]]
print(json.dumps({'provider_key_isolation':True,'network_requests':0}))
"""


def check_case(configured, expected, directory, *, config=CONFIG):
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", PROGRAM, str(config), json.dumps(expected)],
        cwd=directory,
        env={
            "PATH": os.defpath,
            "HOME": str(directory),
            "STOCK_DATA_DIR": str(directory / "synthetic-data"),
            **configured,
        },
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("provider_key_isolation_failed")
    observed = json.loads(result.stdout)
    if observed != {"provider_key_isolation": True, "network_requests": 0}:
        raise RuntimeError("provider_key_isolation_failed")
    return observed


def main():
    with tempfile.TemporaryDirectory(prefix="prism-config-check-") as name:
        for configured, expected in CASES:
            check_case(configured, expected, Path(name))
    print(json.dumps({"cases_passed": len(CASES), "network_requests": 0, "real_credentials_read": False}))


if __name__ == "__main__":
    main()
