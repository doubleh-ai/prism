"""Run the shipped Work script against private, deterministic browser scenarios."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_work_frontend_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the Work frontend runtime check")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "work_frontend_runtime.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "work frontend runtime ok: 23 scenarios" in result.stdout
