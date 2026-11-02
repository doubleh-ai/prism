"""Run project collaboration UI with synthetic account and permission responses."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_projects_collaboration_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the project frontend runtime check")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "projects_collaboration_frontend.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "projects collaboration frontend ok" in result.stdout


def test_projects_attachments_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the project frontend runtime check")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "projects_attachments_frontend.cjs")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "projects attachments frontend ok" in result.stdout
