"""Tests for the Nous-Sinoclaw-3/4 non-agentic warning detector.

Prior to this check, the warning fired on any model whose name contained
``"sinoclaw"`` anywhere (case-insensitive). That false-positived on unrelated
local Modelfiles such as ``sinoclaw-brain:qwen3-14b-ctx16k`` — a tool-capable
Qwen3 wrapper that happens to live under the "sinoclaw" tag namespace.

``is_nous_sinoclaw_non_agentic`` should only match the actual Nous Research
Sinoclaw-3 / Sinoclaw-4 chat family.
"""

from __future__ import annotations

import pytest

from sinoclaw_cli.model_switch import (
    _SINOCLAW_MODEL_WARNING,
    _check_sinoclaw_model_warning,
    is_nous_sinoclaw_non_agentic,
)


@pytest.mark.parametrize(
    "model_name",
    [
        "NousResearch/Sinoclaw-3-Llama-3.1-70B",
        "NousResearch/Sinoclaw-3-Llama-3.1-405B",
        "sinoclaw-3",
        "Sinoclaw-3",
        "sinoclaw-4",
        "sinoclaw-4-405b",
        "sinoclaw_4_70b",
        "openrouter/sinoclaw3:70b",
        "openrouter/nousresearch/sinoclaw-4-405b",
        "NousResearch/Sinoclaw3",
        "sinoclaw-3.1",
    ],
)
def test_matches_real_nous_sinoclaw_chat_models(model_name: str) -> None:
    assert is_nous_sinoclaw_non_agentic(model_name), (
        f"expected {model_name!r} to be flagged as Nous Sinoclaw 3/4"
    )
    assert _check_sinoclaw_model_warning(model_name) == _SINOCLAW_MODEL_WARNING


@pytest.mark.parametrize(
    "model_name",
    [
        # Kyle's local Modelfile — qwen3:14b under a custom tag
        "sinoclaw-brain:qwen3-14b-ctx16k",
        "sinoclaw-brain:qwen3-14b-ctx32k",
        "sinoclaw-honcho:qwen3-8b-ctx8k",
        # Plain unrelated models
        "qwen3:14b",
        "qwen3-coder:30b",
        "qwen2.5:14b",
        "claude-opus-4-6",
        "anthropic/claude-sonnet-4.5",
        "gpt-5",
        "openai/gpt-4o",
        "google/gemini-2.5-flash",
        "deepseek-chat",
        # Non-chat Sinoclaw models we don't warn about
        "sinoclaw-llm-2",
        "sinoclaw2-pro",
        "nous-sinoclaw-2-mistral",
        # Edge cases
        "",
        "sinoclaw",  # bare "sinoclaw" isn't the 3/4 family
        "sinoclaw-brain",
        "brain-sinoclaw-3-impostor",  # "3" not preceded by /: boundary
    ],
)
def test_does_not_match_unrelated_models(model_name: str) -> None:
    assert not is_nous_sinoclaw_non_agentic(model_name), (
        f"expected {model_name!r} NOT to be flagged as Nous Sinoclaw 3/4"
    )
    assert _check_sinoclaw_model_warning(model_name) == ""


def test_none_like_inputs_are_safe() -> None:
    assert is_nous_sinoclaw_non_agentic("") is False
    # Defensive: the helper shouldn't crash on None-ish falsy input either.
    assert _check_sinoclaw_model_warning("") == ""
