import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_workbench_real_javascript_behavior():
    """Run shipped UI code against out-of-order requests and isolated stream events."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "workbench_runtime.cjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "workbench runtime ok: 16 scenarios" in result.stdout
