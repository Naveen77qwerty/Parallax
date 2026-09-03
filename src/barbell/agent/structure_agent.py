"""
Pipeline Stage 3 — proposes a specific options structure for a name that
passed both the numeric screen and the catalyst gate.

    propose_structure(symbol: str, chain: OptionChain, screen_result: ScreenResult) -> ProposedStructure

This is the ONLY module allowed to construct a candidate trade shape. It
does not touch AlpacaClient and cannot submit an order — its return value
goes straight into risk/engine.py, which is the sole path to execution/.

The structure agent receives the option chain data, screen metrics (including
the cycle's dispersion_score), and sleeve_a_carry config.  It uses Gemini
with a forced JSON response_schema to guarantee a ProposedStructure-shaped
response, which is then schema-validated before being returned.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from barbell.agent.schemas import ProposedLeg, ProposedStructure, ScreenResult
from barbell.config import get_settings

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "structure_agent.md"


class _ProposedLegSchema(BaseModel):
    """One LLM-facing leg — mirrors ProposedLeg minus the resolved OCC symbol."""

    expiry: str = Field(description="Expiration date in YYYY-MM-DD format.")
    strike: float = Field(description="Strike price in USD.")
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    contracts: int = Field(
        ge=1, description="Number of contracts (always 1 for initial proposal)."
    )
    ratio_qty: int = Field(default=1, description="Leg ratio (1 for standard spreads).")


class _ProposedStructureSchema(BaseModel):
    """LLM-facing response schema for propose_structure().

    Gemini only ever decides these fields — `underlying` is supplied by the
    caller as `symbol`, not trusted from the model's own output.
    """

    underlying: str = Field(description="The underlying ticker symbol.")
    sleeve: Literal["A", "B"] = Field(
        description="The sleeve this trade belongs to (A for carry, B for convexity)."
    )
    structure_type: str = Field(
        description="e.g. 'put_credit_spread', 'iron_condor', 'call_credit_spread'."
    )
    rationale: str = Field(
        description=(
            "2–3 sentences explaining structure choice, strike selection, "
            "and how dispersion score informed the decision."
        )
    )
    max_loss_estimate: float = Field(
        description=(
            "Your estimate of maximum dollar loss for this structure. "
            "The risk engine re-validates and can only lower size."
        )
    )
    limit_price: float = Field(
        description=(
            "Net credit (positive) or debit (negative) per spread in USD. "
            "Use the mid-price of each leg."
        )
    )
    legs: list[_ProposedLegSchema] = Field(
        min_length=2, description="All legs of the structure."
    )


def propose_structure(
    symbol: str,
    chain: dict[str, Any],
    screen_result: ScreenResult,
    dispersion_score: float | None = None,
) -> ProposedStructure:
    """
    Ask Gemini to propose an options spread structure for *symbol*.

    This function does NOT touch broker.alpaca_client or execution.orders —
    data in, data out, nothing else.  The returned ProposedStructure goes
    directly into risk/engine.py's evaluate() pipeline.

    On any failure (API error, schema validation failure), raises so the
    caller (scheduler/loop.py) can catch it, log it, and skip this name —
    this is the stage where we want a clean exception rather than a silent
    bad proposal reaching the risk engine.

    Args:
        symbol:           Ticker symbol.
        chain:            Dict from broker.alpaca_client.get_option_chain().
        screen_result:    ScreenResult from Stage 1 (has metrics: iv, iv_rank, etc.)
        dispersion_score: Vega-weighted single-name IV / index IV ratio from
                          screen/metrics.py (None if not computable this cycle).

    Returns:
        ProposedStructure validated against agent/schemas.py.

    Raises:
        ValueError:  if schema validation fails or Gemini returns a bad response.
        genai_errors.APIError: on API-level failures.
    """
    prompt_text = _build_prompt(symbol, chain, screen_result, dispersion_score)
    structure = _call_gemini(symbol, prompt_text)
    return structure


def _build_prompt(
    symbol: str,
    chain: dict[str, Any],
    screen_result: ScreenResult,
    dispersion_score: float | None,
) -> str:
    """Render the structure_agent.md prompt template with live data."""
    template = _PROMPT_PATH.read_text()

    s = get_settings()
    sleeve_cfg = s.sleeve_a_carry
    risk_cfg = s.risk_gates
    account_cfg = s.account

    # Screen metrics with safe defaults
    m = screen_result.metrics if screen_result.metrics else {}
    current_iv = float(m.get("iv", 0.0)) * 100
    iv_rank_val = float(m.get("iv_rank", 0.0))
    ratio = float(m.get("iv30_hv20_ratio", 0.0))
    nav_estimate = account_cfg.starting_nav

    # Format chain summary (top 10 contracts by open interest / spread)
    chain_summary = _format_chain_summary(chain, sleeve_cfg)

    # Dispersion score display
    d_score_str = f"{dispersion_score:.4f}" if dispersion_score is not None else "N/A (not computed this cycle)"

    delta_range = sleeve_cfg.short_delta_range
    delta_mid = (delta_range[0] + delta_range[1]) / 2

    rendered = (
        template
        .replace("{{symbol}}", symbol)
        .replace("{{current_iv}}", f"{current_iv:.1f}")
        .replace("{{iv_rank}}", f"{iv_rank_val:.2f}")
        .replace("{{iv30_hv20_ratio}}", f"{ratio:.2f}")
        .replace("{{dispersion_score}}", d_score_str)
        .replace("{{allowed_structures}}", ", ".join(sleeve_cfg.structure))
        .replace("{{spread_width}}", str(sleeve_cfg.spread_width_usd))
        .replace("{{delta_min}}", str(delta_range[0]))
        .replace("{{delta_max}}", str(delta_range[1]))
        .replace("{{delta_mid}}", str(delta_mid))
        .replace("{{dte_min}}", str(sleeve_cfg.dte_range[0]))
        .replace("{{dte_max}}", str(sleeve_cfg.dte_range[1]))
        .replace("{{max_loss_pct_nav}}", str(risk_cfg.max_loss_per_position_pct_nav * 100))
        .replace("{{nav_estimate}}", f"{nav_estimate:,.0f}")
        .replace("{{chain_summary}}", chain_summary)
    )
    return rendered


def _format_chain_summary(chain: dict[str, Any], sleeve_cfg: Any) -> str:
    """
    Format the option chain into a readable table for Gemini.

    Shows the most relevant contracts (near target delta range) for the
    DTE window.  Truncated to avoid exceeding context limits.
    """
    if not chain:
        return "(no chain data available)"

    rows = []
    for occ_sym, snap in chain.items():
        bid = ask = mid = iv = delta = 0.0

        if hasattr(snap, "latest_quote") and snap.latest_quote:
            q = snap.latest_quote
            bid = float(getattr(q, "bid_price", 0) or 0)
            ask = float(getattr(q, "ask_price", 0) or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0

        if hasattr(snap, "implied_volatility") and snap.implied_volatility:
            iv = float(snap.implied_volatility)

        if hasattr(snap, "greeks") and snap.greeks:
            delta = float(getattr(snap.greeks, "delta", 0) or 0)

        if mid > 0:
            spread_pct = (ask - bid) / mid if mid > 0 else 0
            rows.append({
                "symbol": occ_sym,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "iv": iv,
                "delta": delta,
                "spread_pct": spread_pct,
            })

    if not rows:
        return "(no liquid contracts found in chain)"

    # Sort by how close delta is to the target range midpoint
    target_delta = (sleeve_cfg.short_delta_range[0] + sleeve_cfg.short_delta_range[1]) / 2
    rows.sort(key=lambda r: abs(abs(r["delta"]) - target_delta))

    # Format top 15 rows as a compact table
    lines = [
        f"{'OCC Symbol':<25} {'Bid':>6} {'Ask':>6} {'Mid':>6} {'IV%':>6} {'Delta':>7} {'Spread%':>8}",
        "-" * 70,
    ]
    for r in rows[:15]:
        lines.append(
            f"{r['symbol']:<25} {r['bid']:>6.2f} {r['ask']:>6.2f} {r['mid']:>6.2f} "
            f"{r['iv']*100:>5.1f}% {r['delta']:>7.3f} {r['spread_pct']*100:>7.1f}%"
        )

    return "\n".join(lines)


def _call_gemini(symbol: str, prompt_text: str) -> ProposedStructure:
    """
    Call the Gemini API with a forced JSON schema and parse the ProposedStructure.

    Raises ValueError on schema failures, genai_errors.APIError on API failures.
    """
    s = get_settings()
    client = genai.Client(api_key=s.gemini_api_key)

    response = client.models.generate_content(
        model=s.gemini_model,
        contents=prompt_text,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_ProposedStructureSchema,
        ),
    )

    parsed = response.parsed
    if not isinstance(parsed, _ProposedStructureSchema):
        raise ValueError(
            f"Gemini did not return a schema-conformant response for {symbol}; "
            f"got: {getattr(response, 'text', None)!r}"
        )

    if not parsed.legs:
        raise ValueError(f"propose_structure({symbol}): no legs returned by Gemini")

    parsed_legs = []
    for i, leg in enumerate(parsed.legs):
        try:
            expiry = date.fromisoformat(leg.expiry)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"leg[{i}] has invalid expiry: {leg.expiry!r}") from exc

        parsed_legs.append(
            ProposedLeg(
                symbol="",  # resolved later by execution/orders.py
                expiry=expiry,
                strike=float(leg.strike),
                right=leg.right,
                side=leg.side,
                contracts=int(leg.contracts),
                ratio_qty=int(leg.ratio_qty),
            )
        )

    structure = ProposedStructure(
        underlying=symbol,
        legs=parsed_legs,
        rationale=parsed.rationale,
        sleeve=parsed.sleeve,
        max_loss_estimate=float(parsed.max_loss_estimate),
        structure_type=parsed.structure_type,
        limit_price=float(parsed.limit_price),
    )

    log.info(
        "propose_structure(%s): %s with %d legs, max_loss=$%.2f, limit=%.4f",
        symbol,
        structure.structure_type,
        len(structure.legs),
        structure.max_loss_estimate,
        structure.limit_price,
    )
    return structure
