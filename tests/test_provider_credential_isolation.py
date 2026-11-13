"""Load the real startup configuration with synthetic per-provider credentials."""

import pytest

from scripts.check_provider_key_isolation import CASES, CONFIG, check_case


@pytest.mark.parametrize(
    "configured,expected",
    CASES,
)
def test_keys_stay_with_their_configured_provider(tmp_path, configured, expected):
    assert check_case(configured, expected, tmp_path) == {
        "provider_key_isolation": True,
        "network_requests": 0,
    }


def test_startup_gate_rejects_cross_region_credential_inheritance(tmp_path):
    bad = tmp_path / "cross-region-config.py"
    original = CONFIG.read_text()
    assert '"api_key": MINIMAX_CN_API_KEY' in original
    bad.write_text(original.replace('"api_key": MINIMAX_CN_API_KEY', '"api_key": MINIMAX_API_KEY'))
    configured, expected = CASES[-1]
    with pytest.raises(RuntimeError, match="provider_key_isolation_failed"):
        check_case(configured, expected, tmp_path, config=bad)
