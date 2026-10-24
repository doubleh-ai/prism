"""Exercise saved-report Markdown with the actual bundled browser scripts."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_report_renderer_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the report renderer runtime check")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "report_renderer_runtime.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "report renderer runtime ok" in result.stdout
