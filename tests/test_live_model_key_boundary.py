"""Synthetic parser cases only; no real environment/configuration is read."""

import pytest
import test_release_controller as existing

remote = existing.remote
SYNTHETIC = "synthetic-model-token-0123456789"
FALLBACK = "synthetic-legacy-token-0123456789"


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_only_literal_existing_provider_key_is_selected(quote):
    raw = (
        "MINIMAX_API_KEY=unrelated-value-must-not-be-selected\n"
        f"LLM_API_KEY={FALLBACK}\n"
        f"export DEEPSEEK_API_KEY = {quote}{SYNTHETIC}{quote} # synthetic comment\n"
    ).encode()
    assert remote.selected_model_key(raw) == SYNTHETIC


def test_candidate_legacy_fallback_is_used_only_when_provider_specific_is_absent():
    assert remote.selected_model_key(f"LLM_API_KEY={FALLBACK}\n".encode()) == FALLBACK
    with pytest.raises(remote.ReleaseError, match="^live_model_credential_unavailable$"):
        remote.selected_model_key(f"DEEPSEEK_API_KEY=\nLLM_API_KEY={FALLBACK}\n".encode())


@pytest.mark.parametrize(
    "raw",
    [
        b"MINIMAX_API_KEY=synthetic-unrelated-token-0123456789\n",
        b"DEEPSEEK_API_KEY=$(synthetic-command-never-executed)\n",
        b"DEEPSEEK_API_KEY=`synthetic-command-never-executed`\n",
        b"DEEPSEEK_API_KEY=${SYNTHETIC_OTHER_KEY}\n",
        b"DEEPSEEK_API_KEY='synthetic-token-with-\\-escape'\n",
        b'DEEPSEEK_API_KEY="synthetic-token-with-\nmultiline-value"\n',
        f"DEEPSEEK_API_KEY={SYNTHETIC}\nDEEPSEEK_API_KEY={FALLBACK}\n".encode(),
        f"LLM_API_KEY={SYNTHETIC}\nLLM_API_KEY={FALLBACK}\n".encode(),
        f"DEEPSEEK_API_KEY={SYNTHETIC}\0".encode(),
        b"\xff",
        b"#" * 65537,
    ],
)
def test_unknown_or_ambiguous_configuration_fails_without_returning_text(raw):
    with pytest.raises(remote.ReleaseError) as caught:
        remote.selected_model_key(raw)
    assert str(caught.value) == "live_model_credential_unavailable"
