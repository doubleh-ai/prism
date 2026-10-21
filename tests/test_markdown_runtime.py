import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_bundled_marked_runtime_contract():
    """The bundled Marked version must render tokens without object coercion."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "markdown_renderer_runtime.cjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "markdown runtime ok" in result.stdout
