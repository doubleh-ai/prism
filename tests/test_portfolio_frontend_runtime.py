import shutil
import subprocess
from pathlib import Path


def test_portfolio_frontend_runtime():
    node = "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else shutil.which("node")
    assert node, "Node.js is required for frontend runtime checks"
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [node, str(root / "tests/portfolio_frontend_runtime.cjs")], cwd=root, check=True, timeout=20
    )
