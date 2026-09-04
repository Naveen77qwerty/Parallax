"""
Shared Gemini call wrapper for catalyst_gate.py and structure_agent.py — the
only two modules with real LLM decision authority (see CLAUDE.md).

Free-tier Gemini quota is enforced per-model, not per-account: exhausting
gemini-2.5-flash's daily/per-minute quota doesn't touch gemini-2.5-flash-lite
or gemini-2.0-flash. generate_content_with_fallback() tries
settings.gemini_models in order, advancing to the next model ONLY on a
quota/rate-limit error (HTTP 429 / RESOURCE_EXHAUSTED). Any other API error
(bad request, auth failure, schema issue) propagates immediately on the first
model tried — this helper never changes what counts as a failure, only which
model gets to answer. If every configured model is exhausted, the last
error propagates, and callers' existing fail-closed handling (catalyst_risk
=True / raise) is unchanged.

Configure the model list via the GEMINI_MODELS env var (comma-separated,
tried in order). With it unset, behavior is identical to before this module
existed: a single model, GEMINI_MODEL (default "gemini-2.5-flash").
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from barbell.config import get_settings

log = logging.getLogger(__name__)


def _is_quota_error(exc: genai_errors.APIError) -> bool:
    """True for HTTP 429 / RESOURCE_EXHAUSTED — the only case worth trying
    another model. Anything else (400s, auth, 5xx) is a real failure that a
    different model won't fix, so it should surface immediately."""
    if getattr(exc, "code", None) == 429:
        return True
    status = (getattr(exc, "status", None) or "").upper()
    return "RESOURCE_EXHAUSTED" in status


def generate_content_with_fallback(
    prompt_text: str,
    config: genai_types.GenerateContentConfig,
) -> genai_types.GenerateContentResponse:
    """
    Call Gemini's generate_content(), retrying across settings.gemini_models
    in order when a model's quota is exhausted.

    Returns the first successful response. Raises genai_errors.APIError if
    every configured model failed (a non-quota error on any model, or quota
    exhaustion on all of them) — callers already treat APIError as a
    fail-closed signal.
    """
    s = get_settings()
    client = genai.Client(api_key=s.gemini_api_key)
    models = s.gemini_models

    for i, model in enumerate(models):
        try:
            response = client.models.generate_content(
                model=model, contents=prompt_text, config=config,
            )
            if i > 0:
                log.warning(
                    "generate_content_with_fallback: %s was quota-exhausted, "
                    "succeeded on fallback model %s", models[0], model,
                )
            return response
        except genai_errors.APIError as exc:
            is_last = i == len(models) - 1
            if not _is_quota_error(exc) or is_last:
                raise
            log.warning(
                "generate_content_with_fallback: model %s quota/rate-limited "
                "(%s), trying next model %s", model, exc, models[i + 1],
            )

    raise AssertionError("unreachable: models list is validated non-empty by config.py")
