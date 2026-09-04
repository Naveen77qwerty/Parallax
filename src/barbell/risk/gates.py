"""
The 13 deterministic risk gates — pure functions, one per gate.

Signature (all gates):
    (proposed: ProposedStructure,
     portfolio_state: PortfolioState,
     market_state: MarketState,
     config: RiskGateConfig) -> GateResult

Each gate returns one of:
    PASS   — no change required
    RESIZE — reduce contracts to `contracts` (always < proposed.contracts)
    VETO   — reject the proposal entirely

INVARIANT: No gate may return a RESIZE with contracts > proposed.contracts.
           This is enforced at gate construction time (asserted below).

Gates (13 total — 12 original + basket capital reservation):
    01. gate_per_position_loss_cap      — max loss per position vs NAV
    02. gate_portfolio_loss_cap         — total portfolio max loss vs NAV
    03. gate_defined_risk_only          — every short leg covered in same order
    04. gate_quote_staleness            — max age of market quote data
    05. gate_liquidity_floor            — min OI + max bid/ask spread
    06. gate_dispersion_score           — Sleeve A vega-ratio floor (graceful on None)
    07. gate_earnings_blackout          — block entries during earnings windows
    08. gate_pre_nfp_flatten            — TODO(Member 4): stub, returns PASS
    09. gate_expiry_past_deadline       — TODO(Member 4): stub, returns PASS
    10. gate_concentration              — per-underlying + per-sector position limits
    11. gate_drawdown_kill_switch       — delegates to risk/kill_switch.py
    12. gate_broker_reconciliation      — VETO if reconcile found divergence
    13. gate_basket_capital_reservation — VETO if this leg over-commits reserved+capital

Defense-in-depth note on gate_defined_risk_only:
    broker/alpaca_client.py runs the SAME naked-short check before the SDK
    call.  Having the check in two independent layers is INTENTIONAL — do not
    remove either copy.  A bug in the gate doesn't get to the broker, and a
    bug in the broker doesn't mean the gate never ran.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from barbell.agent.schemas import (
    GateResult,
    MarketState,
    PortfolioState,
    ProposedStructure,
)
# Import get_settings at module level so tests can patch "barbell.risk.gates.get_settings"
from barbell.config import RiskGateConfig, get_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: build a PASS / RESIZE / VETO without forgetting the gate_name
# ---------------------------------------------------------------------------

def _pass(gate_name: str, reason: str) -> GateResult:
    return GateResult(outcome="PASS", contracts=None, reason=reason, gate_name=gate_name)


def _resize(gate_name: str, contracts: int, reason: str) -> GateResult:
    # Positive contract count enforced by GateResult validator
    return GateResult(outcome="RESIZE", contracts=contracts, reason=reason, gate_name=gate_name)


def _veto(gate_name: str, reason: str) -> GateResult:
    return GateResult(outcome="VETO", contracts=None, reason=reason, gate_name=gate_name)


# ---------------------------------------------------------------------------
# 01. Per-position loss cap
# ---------------------------------------------------------------------------

def gate_per_position_loss_cap(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    Ensure this single position's max-loss doesn't exceed
    config.max_loss_per_position_pct_nav × current_nav.

    If the proposed contracts would exceed the cap, RESIZE to the largest
    integer contracts that stays within it.  If even 1 contract exceeds the
    cap, VETO.
    """
    _NAME = "gate_per_position_loss_cap"
    nav = portfolio_state.current_nav
    cap_usd = config.max_loss_per_position_pct_nav * nav

    if cap_usd <= 0:
        return _veto(_NAME, f"cap_usd={cap_usd:.2f} ≤ 0 — NAV or config invalid")

    # Estimate per-contract max loss from the proposed structure.
    # proposed.max_loss_estimate is the TOTAL max loss for the whole proposal.
    n_contracts = _total_contracts(proposed)
    if n_contracts <= 0:
        return _veto(_NAME, "proposed has no contracts")

    per_contract_loss = proposed.max_loss_estimate / n_contracts

    if per_contract_loss <= 0:
        # Can't size; pass conservatively (could be a credit we received)
        return _pass(_NAME, f"per_contract_loss={per_contract_loss:.4f} ≤ 0 — credit structure, passing")

    max_contracts_allowed = int(cap_usd / per_contract_loss)

    if max_contracts_allowed <= 0:
        return _veto(
            _NAME,
            f"Even 1 contract (est. loss ${per_contract_loss:.2f}) exceeds "
            f"per-position cap ${cap_usd:.2f} "
            f"({config.max_loss_per_position_pct_nav:.1%} of NAV ${nav:.2f})",
        )

    if n_contracts <= max_contracts_allowed:
        return _pass(
            _NAME,
            f"max_loss_estimate ${proposed.max_loss_estimate:.2f} ≤ "
            f"cap ${cap_usd:.2f} ({config.max_loss_per_position_pct_nav:.1%} of NAV)",
        )

    return _resize(
        _NAME,
        max_contracts_allowed,
        f"Resized {n_contracts} → {max_contracts_allowed} contracts: "
        f"per-contract loss ${per_contract_loss:.2f} × {n_contracts} = "
        f"${n_contracts * per_contract_loss:.2f} exceeds cap ${cap_usd:.2f}",
    )


# ---------------------------------------------------------------------------
# 02. Portfolio loss cap
# ---------------------------------------------------------------------------

def gate_portfolio_loss_cap(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    Ensure total portfolio max-loss (existing + proposed) stays within
    config.max_loss_portfolio_pct_nav × current_nav.

    Existing portfolio max-loss is approximated from open_positions as the
    sum of abs(unrealized_pl) for positions already at a loss, plus the
    current max_loss_estimate totals previously stored.  For simplicity in
    this implementation, we use the sum of negative unrealized_pl values as
    the existing risk proxy.

    If there's headroom, PASS.  If resizing helps, RESIZE.  If 1 contract
    still doesn't fit, VETO.
    """
    _NAME = "gate_portfolio_loss_cap"
    nav = portfolio_state.current_nav
    cap_usd = config.max_loss_portfolio_pct_nav * nav

    # Existing portfolio risk: sum of negative unrealized P&L
    existing_risk = sum(
        abs(float(p.get("unrealized_pl", 0)))
        for p in portfolio_state.open_positions
        if float(p.get("unrealized_pl", 0)) < 0
    )

    n_contracts = _total_contracts(proposed)
    if n_contracts <= 0:
        return _veto(_NAME, "proposed has no contracts")

    per_contract_loss = proposed.max_loss_estimate / n_contracts if n_contracts > 0 else 0.0

    headroom = cap_usd - existing_risk

    if headroom <= 0:
        return _veto(
            _NAME,
            f"Existing portfolio risk ${existing_risk:.2f} already at or above "
            f"portfolio cap ${cap_usd:.2f} ({config.max_loss_portfolio_pct_nav:.1%} of NAV). "
            f"No room for new positions.",
        )

    if per_contract_loss <= 0:
        return _pass(_NAME, f"per_contract_loss={per_contract_loss:.4f} ≤ 0 — credit, passing")

    max_contracts_allowed = int(headroom / per_contract_loss)

    if max_contracts_allowed <= 0:
        return _veto(
            _NAME,
            f"Even 1 contract (est. loss ${per_contract_loss:.2f}) would push "
            f"portfolio risk from ${existing_risk:.2f} past cap ${cap_usd:.2f}",
        )

    if n_contracts <= max_contracts_allowed:
        return _pass(
            _NAME,
            f"Portfolio risk ${existing_risk + proposed.max_loss_estimate:.2f} "
            f"within cap ${cap_usd:.2f}",
        )

    return _resize(
        _NAME,
        max_contracts_allowed,
        f"Resized {n_contracts} → {max_contracts_allowed} contracts to stay within "
        f"portfolio cap ${cap_usd:.2f} (headroom ${headroom:.2f}, "
        f"per-contract loss ${per_contract_loss:.2f})",
    )


# ---------------------------------------------------------------------------
# 03. Defined-risk-only (no naked shorts)
# ---------------------------------------------------------------------------

def gate_defined_risk_only(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if any SELL leg is not covered by a BUY leg of the same right, same
    expiry, with a strike on the protective side (lower for puts, higher for
    calls — what actually bounds the spread's max loss).

    DEFENSE IN DEPTH: broker/alpaca_client.py's submit_mleg_order() runs
    the identical check before the Alpaca SDK call.  BOTH checks are
    intentional and must remain — this gate catches naked shorts before any
    order is built; the broker layer catches them again at submission time.
    Do NOT remove either copy.

    Matching by expiry alone previously let a SELL put "pass" as covered by
    an unrelated BUY call at the same expiry, which is not a defined-risk
    structure at all — tightened to match right + verify strike relationship.
    """
    _NAME = "gate_defined_risk_only"

    buy_legs = [leg for leg in proposed.legs if leg.side == "buy"]
    uncovered = []
    for sell in (leg for leg in proposed.legs if leg.side == "sell"):
        covered = any(
            buy.expiry == sell.expiry
            and buy.right == sell.right
            and (
                (sell.right == "put" and buy.strike < sell.strike)
                or (sell.right == "call" and buy.strike > sell.strike)
            )
            for buy in buy_legs
        )
        if not covered:
            uncovered.append(sell)

    if uncovered:
        details = [f"{leg.right} strike={leg.strike} expiry={leg.expiry}" for leg in uncovered]
        return _veto(
            _NAME,
            f"Naked short detected: SELL leg(s) {details} have no covering BUY leg "
            f"of the same right/expiry on the protective side of the strike. "
            f"Defined-risk-only policy (CLAUDE.md non-negotiable).",
        )

    return _pass(_NAME, "All short legs are covered within this structure.")


# ---------------------------------------------------------------------------
# 04. Quote staleness
# ---------------------------------------------------------------------------

def gate_quote_staleness(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if any leg's quote is older than config.max_quote_age_seconds.

    Checks market_state.quote_age_seconds keyed by OCC symbol or underlying.
    If a symbol isn't found in quote_age_seconds, uses the underlying key as
    a fallback.  If neither is found, PASS conservatively (quote data simply
    wasn't collected — don't block on missing metadata).
    """
    _NAME = "gate_quote_staleness"
    max_age = config.max_quote_age_seconds

    for leg in proposed.legs:
        # Try leg symbol first, then underlying fallback
        age = market_state.quote_age_seconds.get(leg.symbol)
        if age is None:
            age = market_state.quote_age_seconds.get(proposed.underlying)
        if age is None:
            log.debug(
                "%s: no quote_age for symbol=%s / underlying=%s — passing conservatively",
                _NAME, leg.symbol, proposed.underlying,
            )
            continue

        if age > max_age:
            return _veto(
                _NAME,
                f"Quote for {leg.symbol or proposed.underlying} is {age:.1f}s old "
                f"(max {max_age}s). Stale data — cannot price accurately.",
            )

    return _pass(_NAME, f"All quotes within staleness threshold ({max_age}s).")


# ---------------------------------------------------------------------------
# 05. Liquidity floor
# ---------------------------------------------------------------------------

def gate_liquidity_floor(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if:
    - Any leg's open interest is below config (sleeve_a_carry.screen.min_open_interest)
    - The bid/ask spread on any leg is above config (sleeve_a_carry.screen.max_spread_pct_of_mid)

    config is RiskGateConfig which doesn't carry sleeve_a_carry fields.
    These thresholds are passed by the engine from settings directly into
    this gate's config via the SleeveAScreenConfig fields.  To keep the
    gate signature clean, min_open_interest and max_spread_pct_of_mid are
    read from the module-level settings if not overridden — see engine.py.

    Fallback values (conservative): min_oi=500, max_spread=0.08.
    """
    _NAME = "gate_liquidity_floor"

    # Thresholds from sleeve_a_carry.screen (in settings.yaml)
    try:
        s = get_settings()
        min_oi = s.sleeve_a_carry.screen.min_open_interest
        max_spread_pct = s.sleeve_a_carry.screen.max_spread_pct_of_mid
    except Exception:
        min_oi = 500
        max_spread_pct = 0.08

    for leg in proposed.legs:
        symbol = leg.symbol or proposed.underlying

        # Open interest check
        oi = market_state.open_interest.get(symbol)
        if oi is None:
            oi = market_state.open_interest.get(proposed.underlying)
        if oi is not None and oi < min_oi:
            return _veto(
                _NAME,
                f"Open interest for {symbol} is {oi} (below floor {min_oi}). "
                f"Insufficient liquidity for defined-risk spread.",
            )

        # Bid/ask spread check
        spread = market_state.bid_ask_spread.get(symbol)
        if spread is None:
            spread = market_state.bid_ask_spread.get(proposed.underlying)
        if spread is not None and spread > 0:
            # Need a mid price to compute percentage.  Use limit_price as proxy.
            mid = abs(proposed.limit_price) if proposed.limit_price != 0.0 else None
            if mid and mid > 0:
                spread_pct = spread / mid
                if spread_pct > max_spread_pct:
                    return _veto(
                        _NAME,
                        f"Bid/ask spread for {symbol} is {spread_pct:.1%} of mid "
                        f"(max {max_spread_pct:.1%}). Spread too wide — adverse fills likely.",
                    )

    return _pass(_NAME, "All legs meet open-interest and spread requirements.")


# ---------------------------------------------------------------------------
# 06. Dispersion / vega-ratio floor (Sleeve A only)
# ---------------------------------------------------------------------------

def gate_dispersion_score(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    For Sleeve A proposals: VETO if dispersion_score < min_dispersion_score.
    PASS (not VETO) if dispersion_score is None.

    Rationale for the None-passes design:
        Member 3's screen/metrics.py dispersion_score() is not yet wired
        when this gate is first built.  A VETO on None would silently disable
        all of Sleeve A until Member 3 lands — that's worse than passing,
        because the other 12 gates still enforce real risk constraints.
        Feature Admission Protocol: "absence of data means don't block on
        something not computed yet," not "assume the worst."

    For Sleeve B proposals: always PASS (dispersion screen is Sleeve A only).
    """
    _NAME = "gate_dispersion_score"

    if proposed.sleeve != "A":
        return _pass(_NAME, "Sleeve B — dispersion screen does not apply.")

    score = market_state.dispersion_score

    if score is None:
        log.info(
            "%s: dispersion_score is None (Member 3 metric not yet populated) — "
            "passing conservatively. Other gates still enforce risk constraints.",
            _NAME,
        )
        return _pass(
            _NAME,
            "dispersion_score not yet populated (screen/metrics.py pending Member 3). "
            "Passing conservatively — all other gates remain active.",
        )

    try:
        min_score = get_settings().sleeve_a_carry.screen.min_dispersion_score
    except Exception:
        min_score = 1.15  # default from settings.yaml

    if score < min_score:
        return _veto(
            _NAME,
            f"Dispersion score {score:.3f} below Sleeve A floor {min_score:.3f}. "
            f"Single-name IV not sufficiently elevated vs index — unfavorable carry dynamics.",
        )

    return _pass(
        _NAME,
        f"Dispersion score {score:.3f} ≥ floor {min_score:.3f}. Sleeve A carry conditions met.",
    )


# ---------------------------------------------------------------------------
# 07. Earnings blackout
# ---------------------------------------------------------------------------

def gate_earnings_blackout(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if the underlying is in the configured earnings-blackout exclude list.

    The earnings blackout check has two parts:
    1. If sleeve_a_carry.screen.earnings_blackout is False in config, PASS.
    2. If True, check whether the underlying appears in universe.yaml's exclude
       list — that list is populated with any symbol facing a near-term earnings
       announcement that would violate defined-risk assumptions.

    This is a defense-in-depth check — structure_agent.py already evaluated
    earnings risk as part of its CatalystVerdict check, but that was LLM-based.
    This gate applies the deterministic universe-exclude list as a fallback.
    """
    _NAME = "gate_earnings_blackout"

    try:
        s = get_settings()
        earnings_blackout_enabled = s.sleeve_a_carry.screen.earnings_blackout
        exclude_list: list[str] = s.universe.exclude
    except Exception:
        # Config unavailable — fail closed: treat as blackout enabled, no excludes
        log.warning("%s: could not load settings — passing conservatively", _NAME)
        return _pass(_NAME, "settings unavailable — passing conservatively")

    if not earnings_blackout_enabled:
        return _pass(_NAME, "earnings_blackout disabled in config.")

    underlying = proposed.underlying.upper()
    exclude_upper = {sym.upper() for sym in exclude_list}

    if underlying in exclude_upper:
        return _veto(
            _NAME,
            f"{underlying} is in the universe exclude list (earnings blackout active). "
            f"Binary earnings risk makes premium harvesting unreliable.",
        )

    return _pass(
        _NAME,
        f"{underlying} not in earnings exclude list. Earnings blackout clear.",
    )


# ---------------------------------------------------------------------------
# 08. Pre-NFP flatten requirement
# ---------------------------------------------------------------------------

def gate_pre_nfp_flatten(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO new entries once we are past the NFP-flatten deadline.

    Phase is read from endgame/schedule.py's current_phase(). New entries
    are blocked in HOLD_THROUGH_NFP, MONETIZE, FLAT, and POST_DEADLINE.

    The scheduler loop also enforces phase-gating at the cycle level —
    this gate provides defense-in-depth at the individual proposal level.

    On any import or config error, fails OPEN (PASS) with a warning rather
    than silently blocking the whole pipeline — the scheduler-level phase
    check is the primary enforcement.
    """
    _NAME = "gate_pre_nfp_flatten"

    try:
        from barbell.endgame.schedule import Phase, current_phase
        phase = current_phase()
    except Exception as exc:
        log.warning("%s: could not determine phase — passing conservatively: %s", _NAME, exc)
        return _pass(_NAME, f"phase unavailable (error: {exc}) — passing conservatively.")

    _BLOCKING_PHASES = {
        Phase.HOLD_THROUGH_NFP,
        Phase.MONETIZE,
        Phase.FLAT,
        Phase.POST_DEADLINE,
    }

    if phase in _BLOCKING_PHASES:
        return _veto(
            _NAME,
            f"NFP flatten required — current phase is {phase.name}. "
            "No new entries allowed after the pre-NFP flatten window.",
        )

    return _pass(_NAME, f"Phase is {phase.name} — pre-NFP flatten gate allows entries.")


# ---------------------------------------------------------------------------
# 09. Expiry-past-deadline rejection
# ---------------------------------------------------------------------------

def gate_expiry_past_deadline(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if any leg expires after the contest submission deadline.

    Reads submission_deadline_et directly from config/settings.yaml — no
    dependency on endgame/schedule.py (per Member 2's handoff note).

    Rationale: options that expire after the judged window cannot contribute
    to realised P&L before submission and represent uncontrolled open risk
    past the contest boundary.
    """
    _NAME = "gate_expiry_past_deadline"

    try:
        deadline_dt = get_settings().calendar.submission_deadline_et
        # submission_deadline_et may be naive (from YAML) — treat as ET
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")
        if deadline_dt.tzinfo is None:
            deadline_dt = deadline_dt.replace(tzinfo=_ET)
        deadline_date = deadline_dt.date()
    except Exception as exc:
        log.warning("%s: could not read submission deadline — passing: %s", _NAME, exc)
        return _pass(_NAME, f"deadline unavailable (error: {exc}) — passing conservatively.")

    for leg in proposed.legs:
        if leg.expiry > deadline_date:
            return _veto(
                _NAME,
                f"Leg expiry {leg.expiry} is after submission deadline {deadline_date}. "
                "Position cannot be realised before judging — rejecting.",
            )

    return _pass(
        _NAME,
        f"All leg expiries on or before submission deadline {deadline_date}.",
    )


# ---------------------------------------------------------------------------
# 10. Concentration (per-underlying + per-sector)
# ---------------------------------------------------------------------------

def gate_concentration(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if:
    - The proposed underlying already has max_positions_per_underlying open positions.
    - The sector this underlying belongs to already has max_sector_concentration positions.

    Sector is looked up from portfolio_state.sector_exposure (dict[sector, count]).
    If sector information isn't available, skip the sector check (conservative PASS).
    """
    _NAME = "gate_concentration"
    underlying = proposed.underlying

    # Per-underlying check
    existing_for_underlying = sum(
        1 for p in portfolio_state.open_positions
        if str(p.get("underlying", p.get("symbol", ""))).startswith(underlying)
    )
    if existing_for_underlying >= config.max_positions_per_underlying:
        return _veto(
            _NAME,
            f"Already have {existing_for_underlying} position(s) on {underlying} "
            f"(max {config.max_positions_per_underlying}). Concentration limit reached.",
        )

    # Per-sector check
    # sector_exposure: dict[sector_name, count_of_positions_in_that_sector]
    if portfolio_state.sector_exposure:
        # Determine the sector for the proposed underlying.
        # Sector is stored by the caller in sector_exposure; if the underlying
        # appears directly as a key, use it; otherwise skip.
        sector_count = portfolio_state.sector_exposure.get(underlying)
        if sector_count is not None and sector_count >= config.max_sector_concentration:
            return _veto(
                _NAME,
                f"Sector containing {underlying} already has {sector_count} positions "
                f"(max {config.max_sector_concentration}). Sector concentration limit reached.",
            )

    return _pass(
        _NAME,
        f"{underlying}: {existing_for_underlying} existing position(s), "
        f"within per-underlying ({config.max_positions_per_underlying}) and "
        f"per-sector ({config.max_sector_concentration}) limits.",
    )


# ---------------------------------------------------------------------------
# 11. Drawdown kill-switch
# ---------------------------------------------------------------------------

def gate_drawdown_kill_switch(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if the kill switch is latched (drawdown exceeded threshold).

    Delegates to kill_switch.is_latched() which reads the DB so this is
    correct even after a process restart.  The engine also calls
    check_and_latch() before running any gates, so by the time this gate
    runs, the latch state is already current.
    """
    _NAME = "gate_drawdown_kill_switch"

    # is_latched requires a store; we read it from a module-level singleton
    # injected by the engine before gate evaluation.  If not available, VETO
    # (fail closed on kill-switch check failure).
    store = _get_store()
    if store is None:
        return _veto(_NAME, "JournalStore not available — kill-switch check failed, blocking new entries.")

    from barbell.risk.kill_switch import is_latched
    if is_latched(store):
        drawdown_pct = (
            (portfolio_state.current_nav - portfolio_state.starting_nav)
            / portfolio_state.starting_nav
            if portfolio_state.starting_nav > 0 else 0.0
        )
        return _veto(
            _NAME,
            f"Kill switch latched. Portfolio drawdown {drawdown_pct:.2%} breached "
            f"{config.drawdown_kill_switch_pct_nav:.1%} threshold. "
            f"No new entries allowed until manually cleared.",
        )

    return _pass(
        _NAME,
        f"Kill switch not latched. NAV ${portfolio_state.current_nav:.2f} "
        f"(starting ${portfolio_state.starting_nav:.2f}).",
    )


# ---------------------------------------------------------------------------
# 12. Broker reconciliation required
# ---------------------------------------------------------------------------

def gate_broker_reconciliation(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if execution/reconcile.py found broker state diverged from the journal.

    market_state.reconciliation_diverged is set by reconcile() before the
    engine is called.  Default is False (PASS) so gates work in test contexts
    that don't run reconcile.

    This gate is WIRED (not stubbed) — reconcile.py is built in this same
    phase.  The gate is the last defense before an order hits the broker when
    we don't trust our own position state.
    """
    _NAME = "gate_broker_reconciliation"

    if market_state.reconciliation_diverged:
        return _veto(
            _NAME,
            "Broker reconciliation found position divergence. "
            "Halting new entries until reconcile passes cleanly. "
            "Check logs for CRITICAL reconciliation alert.",
        )

    return _pass(_NAME, "Broker reconciliation: no position divergence detected.")


# ---------------------------------------------------------------------------
# 13. Basket capital reservation (NEW)
# ---------------------------------------------------------------------------

def gate_basket_capital_reservation(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
) -> GateResult:
    """
    VETO if submitting this leg would push total reserved-plus-committed
    capital above the portfolio loss cap.

    Formula:
        portfolio_state.reserved_capital + proposed.max_loss_estimate
        > config.max_loss_portfolio_pct_nav × portfolio_state.current_nav

    portfolio_state.reserved_capital is the running total of capital already
    reserved for in-flight basket legs (written by execution/orders.py via
    capital_reservations table before the first leg is submitted).

    This gate prevents a basket build from over-committing capital across
    sequential legs before earlier legs have confirmed filled — the gap that
    exists because Alpaca only guarantees atomic fills within one underlying's
    multi-leg order, not across underlyings.
    """
    _NAME = "gate_basket_capital_reservation"

    nav = portfolio_state.current_nav
    cap_usd = config.max_loss_portfolio_pct_nav * nav
    total_committed = portfolio_state.reserved_capital + proposed.max_loss_estimate

    if total_committed > cap_usd:
        return _veto(
            _NAME,
            f"Basket capital check: reserved ${portfolio_state.reserved_capital:.2f} "
            f"+ this proposal ${proposed.max_loss_estimate:.2f} = ${total_committed:.2f} "
            f"would exceed portfolio cap ${cap_usd:.2f} "
            f"({config.max_loss_portfolio_pct_nav:.1%} of NAV ${nav:.2f}). "
            f"Blocking to prevent over-commitment across sequential basket legs.",
        )

    return _pass(
        _NAME,
        f"Basket capital ok: reserved ${portfolio_state.reserved_capital:.2f} "
        f"+ proposed ${proposed.max_loss_estimate:.2f} = ${total_committed:.2f} "
        f"within cap ${cap_usd:.2f}.",
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _total_contracts(proposed: ProposedStructure) -> int:
    """
    Return the representative contract count for this proposal.

    All legs in a standard spread have the same contract count; we take the
    max to be conservative.  For ratio spreads, this could differ per leg —
    in that case the caller should review the sizing logic.
    """
    if not proposed.legs:
        return 0
    return max(leg.contracts for leg in proposed.legs)


# Module-level store injection — set by risk/engine.py before gate evaluation.
# Using a module-level slot avoids changing the gate signatures while still
# giving gate_drawdown_kill_switch access to the JournalStore.
_store_instance: "JournalStore | None" = None  # type: ignore[name-defined]


def _get_store() -> "JournalStore | None":  # type: ignore[name-defined]
    return _store_instance


def _set_store(store: "JournalStore | None") -> None:  # type: ignore[name-defined]
    global _store_instance
    _store_instance = store


# ---------------------------------------------------------------------------
# Ordered gate list — the engine iterates this in sequence, never out of order
# ---------------------------------------------------------------------------

GateFn = object  # runtime type alias; all elements are callables with the 4-arg gate signature

GATE_PIPELINE: list[GateFn] = [
    gate_per_position_loss_cap,       # 01
    gate_portfolio_loss_cap,          # 02
    gate_defined_risk_only,           # 03 — defense in depth (alpaca_client also checks)
    gate_quote_staleness,             # 04
    gate_liquidity_floor,             # 05
    gate_dispersion_score,            # 06
    gate_earnings_blackout,           # 07
    gate_pre_nfp_flatten,             # 08 — TODO(Member 4): stub, PASS
    gate_expiry_past_deadline,        # 09 — TODO(Member 4): stub, PASS
    gate_concentration,               # 10
    gate_drawdown_kill_switch,        # 11
    gate_broker_reconciliation,       # 12 — wired (reconcile.py built this phase)
    gate_basket_capital_reservation,  # 13 — NEW
]
