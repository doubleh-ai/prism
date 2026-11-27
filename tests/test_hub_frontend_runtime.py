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
    assert "hub journeys ok: 15 scenarios" in result.stdout


def test_futures_panel_renders_dates_sources_and_reason_codes():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "tests/futures_panel_frontend.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "futures panel ok: 6 scenarios" in result.stdout


def test_standalone_futures_brief_and_explicit_interpretation():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    subprocess.run([node, "tests/futures_brief_runtime.cjs"], cwd=root, check=True, timeout=15)
