"""
Optional Stage 1.5 — a cheap, non-critical pre-digest of headlines for each
numeric-screen survivor, served by an open-source model through Featherless
AI (hackathon technology partner: OpenAI-compatible endpoint, $25/participant
credit). This is the one place that model is used, and deliberately so:

    digest_headlines(symbol: str, headlines: list[str]) -> HeadlineDigest

It flags rough news volume and produces a short summary — nothing more. The
output is fed as EXTRA CONTEXT into agent/catalyst_gate.py's prompt; it does
not itself decide catalyst_risk, does not gate which symbols proceed, and is
never seen by risk/engine.py. If this call fails, times out, or returns
something that doesn't parse into HeadlineDigest, the pipeline proceeds with
an empty digest — this stage can only add color, never block a cycle.

Rationale for keeping it this narrow: the catalyst gate is the one LLM call
in the pipeline with real veto power, and that stays on Claude, whose
structured-output reliability the rest of the safety design depends on.
Featherless is used where a less-proven model costs nothing if it's wrong —
bulk, low-stakes summarization — which is also genuinely representative of
what Featherless is for (cheap open-source inference in an agent workflow),
rather than a token integration bolted on for eligibility.

Config: FEATHERLESS_API_KEY, FEATHERLESS_BASE_URL (https://api.featherless.ai/v1),
FEATHERLESS_MODEL in .env. Uses the standard `openai` SDK client pointed at
Featherless's OpenAI-compatible endpoint — no separate SDK dependency.
"""

from __future__ import annotations

import json
import logging

from openai import OpenAI

from barbell.agent.schemas import HeadlineDigest
from barbell.config import get_settings

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a concise financial news analyst. Given a stock ticker and a list of
recent headlines, output ONLY a valid JSON object with exactly these fields:
  - news_volume: one of "low", "normal", or "elevated"
  - summary: a single sentence (max 60 words) summarizing the key theme

Rules:
- "elevated" means ≥3 material headlines (earnings, FDA, legal, macro shock).
- "low" means ≤1 or only generic market noise.
- "normal" for everything else.
- The summary should describe WHAT is happening, not restate the volume label.
- Output ONLY the JSON object, no markdown fences, no extra text.

Example output:
{"news_volume": "elevated", "summary": "Company beat Q3 earnings by 12% and raised full-year guidance amid strong cloud revenue growth."}
"""


def digest_headlines(symbol: str, headlines: list[str]) -> HeadlineDigest:
    """
    Call Featherless (via OpenAI-compatible API) to pre-digest headlines.

    Produces a HeadlineDigest with news_volume and a short summary.
    On ANY failure (timeout, bad JSON, schema validation error, API error):
        - logs a WARNING
        - returns HeadlineDigest(symbol=symbol, news_volume="normal", summary="")
    This function NEVER raises past its own boundary — it has zero veto
    authority (CLAUDE.md invariant: informational only).

    Args:
        symbol:     Ticker symbol (for context and logging).
        headlines:  List of raw headline strings (empty list is valid — returns neutral).

    Returns:
        HeadlineDigest — always, even on failure.
    """
    neutral = HeadlineDigest(
        symbol=symbol,
        news_volume="normal",
        summary="",
        model_used="",
    )

    if not headlines:
        log.debug("digest_headlines(%s): no headlines provided, returning neutral", symbol)
        return neutral

    s = get_settings()

    # If Featherless is not configured, degrade gracefully
    if not s.featherless_api_key:
        log.warning(
            "digest_headlines(%s): FEATHERLESS_API_KEY not set — returning neutral digest",
            symbol,
        )
        return neutral

    try:
        client = OpenAI(
            api_key=s.featherless_api_key,
            base_url=s.featherless_base_url,
        )

        headlines_block = "\n".join(f"- {h}" for h in headlines[:20])  # cap at 20 headlines
        user_message = f"Ticker: {symbol}\n\nHeadlines:\n{headlines_block}"

        response = client.chat.completions.create(
            model=s.featherless_model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.1,
            max_tokens=150,
            timeout=15.0,
        )

        raw_text = response.choices[0].message.content or ""
        model_name = s.featherless_model

    except Exception as exc:
        log.warning("digest_headlines(%s): API call failed: %s", symbol, exc)
        return neutral

    # --- Parse JSON response ---
    try:
        raw_text = raw_text.strip()
        # Strip markdown fences if the model adds them despite the prompt
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()

        data = json.loads(raw_text)

        # Validate and coerce news_volume
        volume = data.get("news_volume", "normal")
        if volume not in ("low", "normal", "elevated"):
            log.warning(
                "digest_headlines(%s): unexpected news_volume=%r, defaulting to 'normal'",
                symbol, volume,
            )
            volume = "normal"

        summary = str(data.get("summary", ""))

        digest = HeadlineDigest(
            symbol=symbol,
            news_volume=volume,  # type: ignore[arg-type]
            summary=summary,
            model_used=model_name,
        )
        log.debug(
            "digest_headlines(%s): volume=%s, model=%s",
            symbol, digest.news_volume, digest.model_used,
        )
        return digest

    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        log.warning(
            "digest_headlines(%s): failed to parse response (%s): %r",
            symbol, exc, raw_text[:200],
        )
        return neutral
