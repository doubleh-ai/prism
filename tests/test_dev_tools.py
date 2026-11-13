"""The documented development install must provide a working type checker."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("value,expected_code", [("1", 0), ('"wrong type"', 1)])
def test_development_mypy_checks_types(tmp_path, value, expected_code):
    source = tmp_path / "sample.py"
    source.write_text(f"count: int = {value}\n")
    config = tmp_path / "mypy.ini"
    config.write_text("[mypy]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--cache-dir",
            str(tmp_path / "cache"),
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "No module named" not in result.stderr
    assert result.returncode == expected_code, result.stdout + result.stderr
    if expected_code:
        assert "Incompatible types in assignment" in result.stdout
