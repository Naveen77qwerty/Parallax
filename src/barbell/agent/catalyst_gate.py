"""
Pipeline Stage 2 — the first of two narrow LLM jobs (see docs/architecture.md
for why the LLM's role is split and kept out of order construction).

    check_catalyst(symbol: str, headlines: list[str], digest: HeadlineDigest | None) -> CatalystVerdict

Calls the Gemini API with prompts/catalyst_gate.md, forces a
CatalystVerdict-shaped JSON response via response_schema, and returns the
parsed model. Anything that fails schema validation is treated as
catalyst_risk=True (fail closed, not open).

`digest` is the optional output of screen/headline_triage.py (Featherless,
open-source model) — included in the prompt as extra context ("a cheap
first pass flagged elevated news volume; verify why") but Gemini alone
produces catalyst_risk. A missing digest changes nothing about this
function's behavior, by design.
"""

from __future__ import annotations

import logging
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from barbell.agent.schemas import CatalystVerdict, HeadlineDigest, ScreenResult
from barbell.config import get_settings

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "catalyst_gate.md"


class _CatalystVerdictSchema(BaseModel):
    """LLM-facing response schema for check_catalyst().

    Gemini only ever decides these three fields — `symbol` is supplied by
    the caller, never asked of the model, so it can't invent the wrong one.
    """

    catalyst_risk: bool = Field(
        description=(
            "True if there is an active unscheduled/unpriced binary risk "
            "that makes selling premium dangerous. False if IV is elevated "
            "for a known, already-priced reason."
        )
    )
    reasoning: str = Field(
        description=(
            "1–3 sentences explaining the specific evidence (or lack of it) "
            "that drove your decision."
        )
    )
    sources_considered: list[str] = Field(
        default_factory=list,
        description=(
            "List of specific headline items or data points you weighted most "
            "heavily. Empty list is acceptable if no headlines were provided."
        ),
    )


def check_catalyst(
    symbol: str,
    headlines: list[str],
    digest: HeadlineDigest | None = None,
    screen_result: ScreenResult | None = None,
) -> CatalystVerdict:
    """
    Ask Claude whether elevated IV for *symbol* is explained by a known,
    priced-in event or by an active unscheduled binary risk.

    This function has real veto authority — a catalyst_risk=True result causes
    the symbol to be dropped from the current cycle without reaching the risk
    engine.  It NEVER silently succeeds on failure: any schema or API error
    returns catalyst_risk=True (fail closed, not open).

    Args:
        symbol:        Ticker symbol to evaluate.
        headlines:     List of recent headline strings for this symbol.
        digest:        Optional HeadlineDigest from screen/headline_triage.py.
                       Used as extra context in the prompt; does not change
                       the fail-closed behavior on its own.
        screen_result: Optional ScreenResult with metrics (IV, IV rank, etc.)
                       for richer prompt context.

    Returns:
        CatalystVerdict with symbol, catalyst_risk, reasoning, sources_considered.
    """
    fail_closed_prefix = "fail-closed"

    try:
        prompt_text = _build_prompt(symbol, headlines, digest, screen_result)
        verdict = _call_gemini(symbol, prompt_text)
        return verdict

    except genai_errors.APIError as exc:
        log.error("check_catalyst(%s): Gemini API error: %s", symbol, exc)
        return CatalystVerdict(
            symbol=symbol,
            catalyst_risk=True,
            reasoning=f"{fail_closed_prefix}: Gemini API error — {exc}",
            sources_considered=[],
        )
    except ValueError as exc:
        # Schema validation failure
        log.error("check_catalyst(%s): schema validation failed: %s", symbol, exc)
        return CatalystVerdict(
            symbol=symbol,
            catalyst_risk=True,
            reasoning=f"{fail_closed_prefix}: schema validation error — {exc}",
            sources_considered=[],
        )
    except Exception as exc:
        log.error("check_catalyst(%s): unexpected error: %s", symbol, exc, exc_info=True)
        return CatalystVerdict(
            symbol=symbol,
            catalyst_risk=True,
            reasoning=f"{fail_closed_prefix}: unexpected error — {type(exc).__name__}: {exc}",
            sources_considered=[],
        )


def _build_prompt(
    symbol: str,
    headlines: list[str],
    digest: HeadlineDigest | None,
    screen_result: ScreenResult | None,
) -> str:
    """
    Render the catalyst_gate.md prompt template with real values.
    """
    template = _PROMPT_PATH.read_text()

    # Screen metrics (with safe defaults)
    current_iv = 0.0
    iv_rank_val = 0.0
    if screen_result and screen_result.metrics:
        current_iv = float(screen_result.metrics.get("iv", 0.0)) * 100  # to %
        iv_rank_val = float(screen_result.metrics.get("iv_rank", 0.0))

    # Headline digest context
    news_volume = digest.news_volume if digest else "normal"
    headline_summary = digest.summary if digest and digest.summary else "(none)"

    # Format headlines block
    if headlines:
        headlines_block = "\n".join(f"  - {h}" for h in headlines[:15])
    else:
        headlines_block = "  (no headlines provided)"

    # Template substitution
    rendered = (
        template
        .replace("{{symbol}}", symbol)
        .replace("{{current_iv}}", f"{current_iv:.1f}")
        .replace("{{iv_rank}}", f"{iv_rank_val:.2f}")
        .replace("{{news_volume}}", news_volume)
        .replace("{{headline_summary}}", headline_summary)
        .replace("{{headlines_block}}", headlines_block)
    )
    return rendered


def _call_gemini(symbol: str, prompt_text: str) -> CatalystVerdict:
    """
    Call the Gemini API with a forced JSON schema and parse the CatalystVerdict.

    Raises ValueError if the response doesn't conform to the schema.
    Raises genai_errors.APIError on API-level failures (callers handle these).
    """
    s = get_settings()
    client = genai.Client(api_key=s.gemini_api_key)

    response = client.models.generate_content(
        model=s.gemini_model,
        contents=prompt_text,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_CatalystVerdictSchema,
        ),
    )

    parsed = response.parsed
    if not isinstance(parsed, _CatalystVerdictSchema):
        raise ValueError(
            f"Gemini did not return a schema-conformant response for {symbol}; "
            f"got: {getattr(response, 'text', None)!r}"
        )

    reasoning = str(parsed.reasoning or "")
    if not reasoning:
        raise ValueError("reasoning is required and must be non-empty")

    verdict = CatalystVerdict(
        symbol=symbol,
        catalyst_risk=bool(parsed.catalyst_risk),
        reasoning=reasoning,
        sources_considered=[str(s_) for s_ in (parsed.sources_considered or [])],
    )
    log.info(
        "check_catalyst(%s): catalyst_risk=%s — %s",
        symbol, verdict.catalyst_risk, verdict.reasoning[:80],
    )
    return verdict
