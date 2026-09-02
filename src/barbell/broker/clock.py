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

All datetime comparisons are timezone-aware (America/New_York).  Calendar keys
in settings.yaml are ET times.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from barbell.config import get_settings

if TYPE_CHECKING:
    from barbell.broker.alpaca_client import AlpacaClient

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


def _now_et() -> datetime:
    """Return the current time as a timezone-aware ET datetime."""
    return datetime.now(tz=ET)


def _make_tz_aware(dt: datetime, tz: ZoneInfo = ET) -> datetime:
    """Attach tzinfo if the datetime is naive (e.g. loaded from YAML)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


# ---------------------------------------------------------------------------
# Market open / close
# ---------------------------------------------------------------------------


def is_market_open(client: AlpacaClient | None = None) -> bool:  # type: ignore[name-defined]
    """
    Return True if the market is currently open per Alpaca's clock API.

    Args:
        client: Optional AlpacaClient.  If None, one is constructed from
                settings (useful for one-shot checks; avoid in hot paths).
    """
    if client is None:
        from barbell.broker.alpaca_client import AlpacaClient
        client = AlpacaClient.from_settings()

    clock = client.get_clock()
    open_ = bool(clock["is_open"])
    log.debug("Market is_open=%s (next_close=%s)", open_, clock["next_close"])
    return open_


# ---------------------------------------------------------------------------
# Calendar window functions — all compare ET-aware dates
# ---------------------------------------------------------------------------


def is_carry_entry_window() -> bool:
    """
    Return True if today (ET) is on or before the last carry entry day
    from config/settings.yaml (calendar.last_carry_entry_day).
    """
    cfg = get_settings().calendar
    today = _now_et().date()
    result = today <= cfg.last_carry_entry_day
    log.debug(
        "is_carry_entry_window: today=%s <= last_carry_entry_day=%s → %s",
        today,
        cfg.last_carry_entry_day,
        result,
    )
    return result


def is_carry_unwind_day() -> bool:
    """
    Return True if today (ET) is the carry unwind day
    from config/settings.yaml (calendar.carry_unwind_day).
    """
    cfg = get_settings().calendar
    today = _now_et().date()
    result = today == cfg.carry_unwind_day
    log.debug(
        "is_carry_unwind_day: today=%s == carry_unwind_day=%s → %s",
        today,
        cfg.carry_unwind_day,
        result,
    )
    return result


def is_convexity_entry_window() -> bool:
    """
    Return True if today (ET) is the convexity entry day AND the current ET
    time is on or after convexity_entry_after_et (e.g. "14:30").
    """
    cfg = get_settings().calendar
    now = _now_et()
    today = now.date()

    if today != cfg.convexity_entry_day:
        return False

    # Parse "HH:MM" threshold
    h, m = (int(x) for x in cfg.convexity_entry_after_et.split(":"))
    threshold = now.replace(hour=h, minute=m, second=0, microsecond=0)
    result = now >= threshold
    log.debug(
        "is_convexity_entry_window: now=%s >= threshold=%s → %s", now, threshold, result
    )
    return result


def time_to_deadline() -> timedelta:
    """
    Return the time remaining until submission_deadline_et.

    Returns a negative timedelta if the deadline has already passed.
    All comparisons are ET-aware.
    """
    cfg = get_settings().calendar
    now = _now_et()
    deadline = _make_tz_aware(cfg.submission_deadline_et)
    remaining = deadline - now
    log.debug("time_to_deadline: %s remaining (deadline=%s)", remaining, deadline)
    return remaining


def must_be_flat_by() -> datetime:
    """
    Return the flatten deadline as a timezone-aware ET datetime.

    This is flatten_by_et from config/settings.yaml.  The endgame state
    machine compares the current ET time against this value to trigger the
    FLAT phase.
    """
    cfg = get_settings().calendar
    flat_dt = _make_tz_aware(cfg.flatten_by_et)
    return flat_dt


def is_past_flatten_deadline() -> bool:
    """
    Return True if the current ET time is at or past the flatten deadline.
    """
    return _now_et() >= must_be_flat_by()


def is_past_nfp() -> bool:
    """
    Return True if the current ET time is at or past the NFP release time.
    Used by endgame/schedule.py to gate Sleeve B convexity entry timing.
    """
    cfg = get_settings().calendar
    nfp_dt = _make_tz_aware(cfg.nfp_release_et)
    return _now_et() >= nfp_dt
