"""Shipped browser scripts on a synthetic DOM, without network or model IO."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_feedback_and_diagnostics_actual_javascript_calls():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(ROOT / "tests/feedback_runtime.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "feedback frontend ok: 12 scenarios" in result.stdout
