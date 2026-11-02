import shutil
import subprocess
from pathlib import Path

import pytest


def test_hub_handlers_preserve_identity_and_drafts():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "tests/hub_frontend_runtime.cjs"], cwd=root, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hub journeys ok: 3 scenarios" in result.stdout
