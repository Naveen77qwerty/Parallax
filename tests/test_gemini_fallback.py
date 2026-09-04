"""
agent/gemini_client.py — the shared fallback wrapper used by catalyst_gate.py
and structure_agent.py. Confirms it only advances to the next model on a
quota/rate-limit error, and that everything else (non-quota errors, all
models exhausted) still propagates so callers' fail-closed handling is
unaffected.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from barbell.agent.gemini_client import generate_content_with_fallback


def _settings(models: list[str]) -> SimpleNamespace:
    return SimpleNamespace(gemini_api_key="test-key", gemini_models=models)


def _config() -> genai_types.GenerateContentConfig:
    return genai_types.GenerateContentConfig(response_mime_type="application/json")


def _quota_error() -> genai_errors.APIError:
    return genai_errors.APIError(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})


class TestGeminiFallback:
    def test_single_model_success_unchanged(self):
        with patch(
            "barbell.agent.gemini_client.get_settings",
            return_value=_settings(["gemini-2.5-flash"]),
        ), patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = "ok"
            result = generate_content_with_fallback("prompt", _config())

        assert result == "ok"
        mock_client.return_value.models.generate_content.assert_called_once()

    def test_falls_back_to_next_model_on_quota_error(self):
        with patch(
            "barbell.agent.gemini_client.get_settings",
            return_value=_settings(["gemini-2.5-flash", "gemini-2.5-flash-lite"]),
        ), patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = [
                _quota_error(),
                "ok-from-fallback",
            ]
            result = generate_content_with_fallback("prompt", _config())

        assert result == "ok-from-fallback"
        assert mock_client.return_value.models.generate_content.call_count == 2
        first_call, second_call = mock_client.return_value.models.generate_content.call_args_list
        assert first_call.kwargs["model"] == "gemini-2.5-flash"
        assert second_call.kwargs["model"] == "gemini-2.5-flash-lite"

    def test_non_quota_error_propagates_without_trying_next_model(self):
        with patch(
            "barbell.agent.gemini_client.get_settings",
            return_value=_settings(["gemini-2.5-flash", "gemini-2.5-flash-lite"]),
        ), patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = genai_errors.APIError(
                400, {"error": {"status": "INVALID_ARGUMENT"}}
            )
            with pytest.raises(genai_errors.APIError):
                generate_content_with_fallback("prompt", _config())

        mock_client.return_value.models.generate_content.assert_called_once()

    def test_all_models_exhausted_raises_last_error(self):
        with patch(
            "barbell.agent.gemini_client.get_settings",
            return_value=_settings(["gemini-2.5-flash", "gemini-2.5-flash-lite"]),
        ), patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = [
                _quota_error(),
                _quota_error(),
            ]
            with pytest.raises(genai_errors.APIError):
                generate_content_with_fallback("prompt", _config())

        assert mock_client.return_value.models.generate_content.call_count == 2
