"""Run the actual recovery overlay, not a duplicate Python implementation."""

import shutil
import subprocess
from pathlib import Path


def test_actual_recovery_overlay():
    node = shutil.which("node")
    assert node is not None, "Node is required for the recovery UI regression"
    result = subprocess.run(
        [node, str(Path(__file__).with_name("recovery_frontend_runtime.cjs"))],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "recovery frontend ok" in result.stdout
