"""
Pipeline Stage 2 — the first of two narrow LLM jobs (see docs/architecture.md
for why the LLM's role is split and kept out of order construction).

    check_catalyst(symbol: str, headlines: list[str], digest: HeadlineDigest | None) -> CatalystVerdict

Calls the Anthropic API with prompts/catalyst_gate.md, tool-forces a
CatalystVerdict-shaped response, and returns the parsed model. Anything that
fails schema validation is treated as catalyst_risk=True (fail closed, not open).

`digest` is the optional output of screen/headline_triage.py (Featherless,
open-source model) — included in the prompt as extra context ("a cheap
first pass flagged elevated news volume; verify why") but Claude alone
produces catalyst_risk. A missing digest changes nothing about this
function's behavior, by design.
"""

from __future__ import annotations

import logging
from pathlib import Path

import anthropic

from barbell.agent.schemas import CatalystVerdict, HeadlineDigest, ScreenResult
from barbell.config import get_settings

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "catalyst_gate.md"

# Tool schema that forces Claude to output a CatalystVerdict-shaped response
_TOOL_SCHEMA = {
    "name": "record_catalyst_verdict",
    "description": (
        "Submit your catalyst risk decision for this symbol. "
        "You MUST call this tool exactly once."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "catalyst_risk": {
                "type": "boolean",
                "description": (
                    "True if there is an active unscheduled/unpriced binary risk "
                    "that makes selling premium dangerous. False if IV is elevated "
                    "for a known, already-priced reason."
                ),
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "1–3 sentences explaining the specific evidence (or lack of it) "
                    "that drove your decision."
                ),
            },
            "sources_considered": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of specific headline items or data points you weighted most "
                    "heavily. Empty list is acceptable if no headlines were provided."
                ),
            },
        },
        "required": ["catalyst_risk", "reasoning"],
    },
}


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
        verdict = _call_anthropic(symbol, prompt_text)
        return verdict

    except anthropic.APIError as exc:
        log.error("check_catalyst(%s): Anthropic API error: %s", symbol, exc)
        return CatalystVerdict(
            symbol=symbol,
            catalyst_risk=True,
            reasoning=f"{fail_closed_prefix}: Anthropic API error — {exc}",
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


def _call_anthropic(symbol: str, prompt_text: str) -> CatalystVerdict:
    """
    Call the Anthropic API with tool-forcing and parse the CatalystVerdict.

    Raises ValueError if the tool response doesn't conform to the schema.
    Raises anthropic.APIError on API-level failures (callers handle these).
    """
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)

    response = client.messages.create(
        model=s.claude_model,
        max_tokens=512,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "record_catalyst_verdict"},
        messages=[
            {
                "role": "user",
                "content": prompt_text,
            }
        ],
    )

    # Extract tool use block
    tool_use_block = None
    for block in response.content:
        if block.type == "tool_use" and block.name == "record_catalyst_verdict":
            tool_use_block = block
            break

    if tool_use_block is None:
        raise ValueError(
            f"Claude did not call record_catalyst_verdict for {symbol}; "
            f"got content blocks: {[b.type for b in response.content]}"
        )

    inp = tool_use_block.input
    if not isinstance(inp, dict):
        raise ValueError(f"tool input is not a dict: {inp!r}")

    catalyst_risk = inp.get("catalyst_risk")
    if not isinstance(catalyst_risk, bool):
        raise ValueError(f"catalyst_risk must be bool, got {catalyst_risk!r}")

    reasoning = str(inp.get("reasoning", ""))
    if not reasoning:
        raise ValueError("reasoning is required and must be non-empty")

    sources = inp.get("sources_considered", [])
    if not isinstance(sources, list):
        sources = []

    verdict = CatalystVerdict(
        symbol=symbol,
        catalyst_risk=bool(catalyst_risk),
        reasoning=reasoning,
        sources_considered=[str(s_) for s_ in sources],
    )
    log.info(
        "check_catalyst(%s): catalyst_risk=%s — %s",
        symbol, verdict.catalyst_risk, verdict.reasoning[:80],
    )
    return verdict
