"""
Session/calendar awareness. Wraps AlpacaClient.get_clock() plus the dates in
config/settings.yaml to answer the questions the scheduler and the endgame
state machine actually need:

    is_market_open() -> bool
    is_carry_entry_window() -> bool        # today <= last_carry_entry_day
    is_carry_unwind_day() -> bool          # today == carry_unwind_day
    is_convexity_entry_window() -> bool    # today == convexity_entry_day, after convexity_entry_after_et
    time_to_deadline() -> timedelta        # against submission_deadline_et
    must_be_flat_by() -> datetime          # flatten_by_et, tz-aware

This is the single place that knows "today" relative to the judged window —
endgame/schedule.py imports this instead of calling datetime.now() itself,
so the whole dated state machine is mockable in tests (see tests/test_schedule.py).
"""
