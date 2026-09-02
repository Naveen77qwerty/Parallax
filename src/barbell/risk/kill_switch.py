"""
The -8% NAV drawdown latch (config: risk_gates.drawdown_kill_switch_pct_nav).

    check_and_latch(current_nav, starting_nav, *, cycle_id, store) -> bool
        Returns True if the latch was just tripped or is already latched.
        Persists the trip to journal/store.py's kill_switch_events table so a
        process restart does NOT reset the latch.

    is_latched(store) -> bool
        Returns True if ANY previous kill_switch_events row has triggered=True.

Once latched, risk/engine.py rejects every new entry regardless of what
gates.py says. Only closing/unwind trades bypass this check.
"""

from __future__ import annotations

import logging

from barbell.journal.store import JournalStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-process cache — avoids a DB read on every single gate call.
# Set to True the moment the latch fires; never reset within a process.
# ---------------------------------------------------------------------------
_latched: bool = False


def is_latched(store: JournalStore) -> bool:
    """
    Return True if the kill switch has ever been tripped.

    Checks the in-process cache first (fast path), then falls back to a DB
    query — this ensures a fresh process that inherits a latched DB state
    correctly discovers the latch without requiring another -8% drawdown.

    Args:
        store: JournalStore instance for DB access.

    Returns:
        True if kill switch is active; False otherwise.
    """
    global _latched

    if _latched:
        return True  # fast path — already known latched in this process

    # Slow path: scan the DB for any prior triggered event
    from sqlmodel import Session, select

    from barbell.journal.store import KillSwitchEventRow

    with Session(store._engine) as session:
        stmt = select(KillSwitchEventRow).where(KillSwitchEventRow.triggered == True)  # noqa: E712
        row = session.exec(stmt).first()
        if row is not None:
            _latched = True
            log.warning(
                "Kill switch latch confirmed from DB (tripped at nav=%.2f, reason=%s)",
                row.nav_at_trigger,
                row.reason,
            )
            return True

    return False


def check_and_latch(
    current_nav: float,
    starting_nav: float,
    *,
    cycle_id: str,
    store: JournalStore,
    threshold_pct: float = -0.08,
) -> bool:
    """
    Evaluate the drawdown; latch and persist if the threshold is breached.

    This is called by risk/engine.py on every evaluate() call and also
    directly by the scheduler loop at cycle start.

    Args:
        current_nav:    Current portfolio NAV in USD.
        starting_nav:   NAV at the start of the contest (config.account.starting_nav).
        cycle_id:       Current cycle identifier (for journal logging).
        store:          JournalStore for writing kill_switch_events rows.
        threshold_pct:  Drawdown fraction that trips the switch (default -0.08 = -8%).
                        Callers should pass config.risk_gates.drawdown_kill_switch_pct_nav.

    Returns:
        True if the latch is now active (just tripped, or was already latched).
        False if the latch is not active.
    """
    global _latched

    # If already latched, no need to re-evaluate or re-persist.
    if is_latched(store):
        return True

    if starting_nav <= 0:
        log.error("check_and_latch: starting_nav=%.2f is invalid — skipping", starting_nav)
        return False

    drawdown_pct = (current_nav - starting_nav) / starting_nav

    if drawdown_pct <= threshold_pct:
        reason = (
            f"Drawdown {drawdown_pct:.2%} breached kill-switch threshold "
            f"{threshold_pct:.2%}. NAV={current_nav:.2f}, starting={starting_nav:.2f}."
        )
        log.critical("KILL SWITCH TRIPPED — %s", reason)

        # Persist to DB first — before setting in-process flag — so the record
        # survives even if the process dies immediately after.
        store.record_kill_switch_event(
            cycle_id=cycle_id,
            triggered=True,
            nav_at_trigger=current_nav,
            reason=reason,
        )
        _latched = True
        return True

    # Not triggered — record a non-triggered event so there's a heartbeat log.
    store.record_kill_switch_event(
        cycle_id=cycle_id,
        triggered=False,
        nav_at_trigger=current_nav,
        reason=f"Drawdown {drawdown_pct:.2%} within threshold {threshold_pct:.2%}.",
    )
    return False


def reset_in_process_cache() -> None:
    """
    Reset the module-level latch cache.

    TESTS ONLY — never call from production code.  Allows test isolation
    when running multiple kill-switch scenarios in sequence.
    """
    global _latched
    _latched = False
