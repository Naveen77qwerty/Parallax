"""
THE SHARED CONTRACT LAYER — every Pydantic model that crosses a module
boundary anywhere in this system lives here.

Members 2 and 3 import from this file; they do not redefine or duplicate
any of these shapes.  If a docstring elsewhere implies a slightly different
shape, THIS FILE WINS.  Conflicts resolved in Member 1 handoff notes at
the bottom of docs/architecture.md.

Import pattern:
    from barbell.agent.schemas import (
        CatalystVerdict, ProposedStructure, GateResult, RiskDecision, ...
    )
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Stage 1.5 — Headline triage (Featherless open-source model, non-critical)
# ---------------------------------------------------------------------------


class HeadlineDigest(BaseModel):
    """
    Output of screen/headline_triage.py (Featherless-hosted open-source model).

    Informational only — has NO veto power and never reaches the risk engine
    directly.  It is passed as extra context INTO the catalyst gate prompt;
    Claude makes the catalyst_risk call itself.

    A failed or missing digest degrades to HeadlineDigest(symbol=sym,
    news_volume="normal", summary="") — the empty-string summary signals
    "neutral or failed triage".  summary is NEVER None.
    """

    symbol: str
    news_volume: Literal["low", "normal", "elevated"]
    summary: str = ""         # empty string == neutral/failed, never None
    model_used: str = ""      # e.g. "Qwen/Qwen2.5-7B-Instruct" via Featherless


# ---------------------------------------------------------------------------
# Stage 2 — Catalyst gate (LLM #1, Claude — has veto power)
# ---------------------------------------------------------------------------


class CatalystVerdict(BaseModel):
    """
    Output of agent/catalyst_gate.py.  If schema validation fails the caller
    must treat it as catalyst_risk=True (fail closed, not open).
    """

    symbol: str
    catalyst_risk: bool         # True = unscheduled binary risk found → veto candidate
    reasoning: str
    sources_considered: list[str] = []


# ---------------------------------------------------------------------------
# Stage 3 — Structure proposal (LLM #2, Claude — sizes real risk)
# ---------------------------------------------------------------------------


class ProposedLeg(BaseModel):
    """
    One leg of a proposed multi-leg options structure.

    CONFLICT RESOLVED: The original stub used OCC-format `symbol` as the
    only identifier.  The prompt spec (and submit_mleg_order's actual needs)
    requires expiry, strike, right, and contracts so the broker client can
    build real Alpaca option contract identifiers without re-querying the
    chain.  Both representations are carried; `symbol` is the OCC-format
    string when available, else empty string.
    """

    symbol: str = ""            # OCC option symbol if already resolved; may be empty
    expiry: date                # expiration date of the contract
    strike: float               # strike price in USD
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    contracts: int              # number of contracts (always positive; side encodes direction)
    ratio_qty: int = 1          # for ratio spreads — 1 for standard structures


class ProposedStructure(BaseModel):
    """
    Output of agent/structure_agent.py.

    CONFLICT RESOLVED: stub had `max_loss_usd` — renamed to `max_loss_estimate`
    per prompt spec.  `structure_type` and `limit_price` are kept from the stub
    because structure_agent.py and execution/orders.py both need them.
    """

    underlying: str
    legs: list[ProposedLeg]
    rationale: str
    sleeve: Literal["A", "B"]
    max_loss_estimate: float     # model's own estimate; risk engine recomputes and can only lower

    # From original stub — kept because orders.py needs them
    structure_type: str = ""     # "put_credit_spread" | "call_credit_spread" | "iron_condor" | "put_debit_spread"
    limit_price: float = 0.0     # net debit (negative) or credit (positive) per contract

    @field_validator("legs")
    @classmethod
    def _at_least_one_leg(cls, v: list[ProposedLeg]) -> list[ProposedLeg]:
        if not v:
            raise ValueError("ProposedStructure must have at least one leg")
        return v


# ---------------------------------------------------------------------------
# Stage 4 — Risk engine
# ---------------------------------------------------------------------------


class GateResult(BaseModel):
    """
    Result from a single risk gate in risk/gates.py.

    outcome:   PASS = no change, RESIZE = reduce to `contracts`, VETO = reject
    contracts: new contract count when outcome==RESIZE; None for PASS/VETO
    gate_name: machine-readable identifier matching the gate function name
    reason:    human-readable explanation, always populated
    """

    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None
    reason: str
    gate_name: str

    @field_validator("contracts")
    @classmethod
    def _contracts_only_on_resize(cls, v: int | None, info: Any) -> int | None:
        # Can't validate cross-field in this hook easily in pydantic v2,
        # but we enforce at construction time from the engine.
        if v is not None and v <= 0:
            raise ValueError("contracts must be positive when set")
        return v


class PortfolioState(BaseModel):
    """
    Current account snapshot passed into every risk gate.

    `reserved_capital` is the running total of capital reserved for in-flight
    basket entries that have not finished reconciling yet.  The basket
    capital-reservation gate (risk/gates.py) reads this field to ensure the
    next basket entry doesn't exceed buying power that's already spoken for.
    This field is written by execution/orders.py via journal/store.py
    (capital_reservations table) before submitting the first leg.
    """

    current_nav: float
    starting_nav: float
    open_positions: list[dict[str, Any]] = Field(default_factory=list)
    sector_exposure: dict[str, int] = Field(default_factory=dict)
    last_quote_ts: dict[str, datetime] = Field(default_factory=dict)
    reserved_capital: float = 0.0    # NEW: capital spoken for by in-flight baskets

    model_config = {"arbitrary_types_allowed": True}


class MarketState(BaseModel):
    """
    Market microstructure data a gate needs beyond PortfolioState.

    `dispersion_score` is the vega-weighted single-name IV / index IV ratio,
    computed in screen/metrics.py (Phase/Member 3).  It lives HERE because
    risk/gates.py (Phase/Member 2) reads it for the Sleeve A dispersion gate.
    None means the metric wasn't computable (Member 3 handles the fallback
    logic; Member 2's gate should PASS conservatively when None and log why).
    """

    # Per-candidate microstructure
    bid_ask_spread: dict[str, float] = Field(default_factory=dict)    # symbol → spread in USD
    open_interest: dict[str, int] = Field(default_factory=dict)       # symbol → total OI
    quote_age_seconds: dict[str, float] = Field(default_factory=dict) # symbol → staleness in sec

    # Computed in screen/metrics.py — read by gates in risk/gates.py
    dispersion_score: float | None = None    # NEW: vega-weighted single-IV/index-IV

    model_config = {"arbitrary_types_allowed": True}


class RiskDecision(BaseModel):
    """
    Final output of risk/engine.py after running all 12 gates.

    `reasons` contains the reason string from EVERY gate that fired (PASS,
    RESIZE, or VETO) — not just the deciding one.  This is what gets written
    to the journal and drives the write-up's risk-gate section.
    """

    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None          # final approved contract count (None if VETO)
    reasons: list[str] = Field(default_factory=list)  # all gates that fired
    proposed: ProposedStructure


# ---------------------------------------------------------------------------
# Stage 1 — Screening
# ---------------------------------------------------------------------------


class ScreenResult(BaseModel):
    """
    Output of screen/universe.py for one candidate symbol.
    All survivors (passed=True) flow into the catalyst gate.
    All rejections are still written to the journal (reason required).
    """

    symbol: str
    passed: bool
    reason: str                             # "ok" on pass; specific gate failure on reject
    metrics: dict[str, Any] = Field(default_factory=dict)  # IV rank, spreads, OI etc.

    model_config = {"arbitrary_types_allowed": True}
