"""
The -8% NAV drawdown latch (config: drawdown_kill_switch_pct_nav).

    check_and_latch(current_nav: float, starting_nav: float) -> bool

Once tripped, is_latched() stays True for the rest of the process lifetime
(persisted in the DB, not just in memory, so a restart doesn't reset it).
While latched: risk/engine.py rejects every new entry regardless of what
gates.py says, and execution/orders.py is only reachable for closing trades.
"""
