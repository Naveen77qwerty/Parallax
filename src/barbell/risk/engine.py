"""
Orchestrates risk/gates.py in a fixed order and folds the results into a
single RiskDecision.

The guarantee this module upholds (tested in tests/test_risk_gates.py with
a property test over 200+ randomised inputs):

    evaluate().contracts can NEVER exceed proposed.contracts.

This is enforced BY CONSTRUCTION:
    1. `current_contracts` starts at proposed.contracts.
    2. Every RESIZE gate applies min(current_contracts, gate_result.contracts).
    3. `min()` is the ONLY operation ever applied — there is no addition,
       multiplication, or any other operation that could increase contracts.
    4. A VETO sets the outcome to VETO and stops evaluation (returns None
       contracts).

Every call is written to journal/store.py regardless of outcome — a VETO
is exactly as important to the write-up as a PASS.

Usage:
    from barbell.risk.engine import evaluate
    decision = evaluate(proposed, portfolio_state, market_state, config,
                        cycle_id="c1", store=journal_store)
"""

from __future__ import annotations

import logging

from barbell.agent.schemas import (
    GateResult,
    MarketState,
    PortfolioState,
    ProposedStructure,
    RiskDecision,
)
from barbell.config import RiskGateConfig
from barbell.journal.store import JournalStore
from barbell.risk import gates as _gates

log = logging.getLogger(__name__)


def evaluate(
    proposed: ProposedStructure,
    portfolio_state: PortfolioState,
    market_state: MarketState,
    config: RiskGateConfig,
    *,
    cycle_id: str,
    store: JournalStore,
) -> RiskDecision:
    """
    Run all 13 gates in the fixed order defined by gates.GATE_PIPELINE.

    Fold rules (applied in this exact priority):
        1. First VETO wins — returns immediately (no remaining gates run).
        2. Multiple RESIZE results — take the minimum contracts.
        3. PASS only if every single gate returned PASS.

    `contracts` in the returned RiskDecision:
        - PASS:   proposed.contracts (unchanged)
        - RESIZE: min across all RESIZE results
        - VETO:   None

    Invariant: returned contracts is always ≤ proposed.contracts.
    This is guaranteed by using only min() — never any operation that
    could increase the value.

    Args:
        proposed:         The LLM-proposed trade structure.
        portfolio_state:  Current account snapshot.
        market_state:     Market microstructure data (incl. reconciliation_diverged).
        config:           RiskGateConfig from settings (thresholds + limits).
        cycle_id:         Current cycle ID for journal logging.
        store:            JournalStore for persisting the decision.

    Returns:
        RiskDecision with outcome PASS / RESIZE / VETO and full gate breakdown.
    """
    # --- Pre-step: update kill-switch latch ---
    # check_and_latch writes a DB row and sets the module-level flag.
    # Gates run AFTER this so gate_drawdown_kill_switch sees the current state.
    from barbell.risk.kill_switch import check_and_latch

    check_and_latch(
        current_nav=portfolio_state.current_nav,
        starting_nav=portfolio_state.starting_nav,
        cycle_id=cycle_id,
        store=store,
        threshold_pct=config.drawdown_kill_switch_pct_nav,
    )

    # Inject store into gates module so gate_drawdown_kill_switch can read it.
    _gates._set_store(store)

    # --- Run gates ---
    # `current_contracts` starts at proposed.contracts.
    # It can only decrease via min() — never increase.
    n_proposed = max(leg.contracts for leg in proposed.legs) if proposed.legs else 0
    current_contracts: int = n_proposed

    gate_results: list[GateResult] = []
    final_outcome: str = "PASS"
    veto_reason: str = ""

    for gate_fn in _gates.GATE_PIPELINE:
        result: GateResult = gate_fn(proposed, portfolio_state, market_state, config)
        gate_results.append(result)

        log.debug(
            "Gate %s: %s — %s",
            result.gate_name,
            result.outcome,
            result.reason,
        )

        if result.outcome == "VETO":
            # First VETO wins — stop immediately.
            final_outcome = "VETO"
            veto_reason = result.reason
            log.warning("VETO from %s: %s", result.gate_name, result.reason)
            break  # Do not evaluate remaining gates.

        if result.outcome == "RESIZE":
            assert result.contracts is not None, (
                f"Gate {result.gate_name} returned RESIZE with contracts=None"
            )
            # BY CONSTRUCTION: only min() is applied here.
            current_contracts = min(current_contracts, result.contracts)
            if final_outcome == "PASS":
                final_outcome = "RESIZE"

    # --- Build RiskDecision ---
    all_reasons = [r.reason for r in gate_results]

    if final_outcome == "VETO":
        decision = RiskDecision(
            outcome="VETO",
            contracts=None,
            reasons=all_reasons,
            proposed=proposed,
        )
    elif final_outcome == "RESIZE":
        # Guarantee: current_contracts ≤ n_proposed (enforced by min() above)
        assert current_contracts <= n_proposed, (
            f"BUG: engine produced contracts={current_contracts} > proposed={n_proposed}. "
            "This violates the fundamental invariant. File a bug."
        )
        assert current_contracts > 0, (
            f"BUG: engine resized to 0 contracts without a VETO. Gate logic error."
        )
        decision = RiskDecision(
            outcome="RESIZE",
            contracts=current_contracts,
            reasons=all_reasons,
            proposed=proposed,
        )
    else:
        decision = RiskDecision(
            outcome="PASS",
            contracts=n_proposed,
            reasons=all_reasons,
            proposed=proposed,
        )

    # --- Persist to journal ---
    primary_reason = veto_reason if final_outcome == "VETO" else "; ".join(
        r.reason for r in gate_results if r.outcome != "PASS"
    ) or "All gates passed."

    store.record_risk_decision(
        cycle_id=cycle_id,
        symbol=proposed.underlying,
        outcome=decision.outcome,
        contracts=decision.contracts,
        reason=primary_reason,
        gate_breakdown=[f"{r.gate_name}:{r.outcome}:{r.reason}" for r in gate_results],
    )

    log.info(
        "RiskDecision for %s: %s (contracts=%s, gates_run=%d)",
        proposed.underlying,
        decision.outcome,
        decision.contracts,
        len(gate_results),
    )

    return decision
