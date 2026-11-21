import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_research_render_runtime_contract():
    """The research report view must render verification badges escaped, without executable tags."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "research_render_runtime.cjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "research render runtime ok" in result.stdout
