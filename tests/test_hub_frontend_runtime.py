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
    assert "hub journeys ok: 16 scenarios" in result.stdout


def test_research_library_stays_in_the_existing_workspace():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "tests/research_library_runtime.cjs"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "research library runtime ok" in result.stdout


def test_futures_entry_uses_the_native_send_flow_once():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [node, "tests/futures_research_frontend.cjs"], cwd=root, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "futures Native entry ok: 7 scenarios" in result.stdout
