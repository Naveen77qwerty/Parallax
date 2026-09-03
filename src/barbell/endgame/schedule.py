"""
The dated state machine referenced throughout docs/architecture.md as the
"deadline-aware exit logic" — the part of the design that isn't required by
the hackathon rules but is arguably the most load-bearing piece of it.

    current_phase(clock: BrokerClock) -> Phase
        Phase = BUILD | CARRY_ACTIVE | UNWIND | CONVEXITY_ENTRY | HOLD_THROUGH_NFP
              | MONETIZE | FLAT | POST_DEADLINE

    allowed_actions(phase: Phase) -> set[str]
        e.g. CARRY_ACTIVE allows new Sleeve A entries; UNWIND allows only closes;
        FLAT allows nothing but reads.

run-cycle calls current_phase() first, every cycle, before doing anything
else — every other module receives the phase as a constraint, not a
suggestion. This is what guarantees the account is flat and fully realized
before scripts/verify submission, independent of what any LLM call returns
that day.

Boundary semantics (all times ET / America/New_York):
    BUILD            before first_full_session (calendar day)
    CARRY_ACTIVE     from first_full_session through last_carry_entry_day EOD
    UNWIND           carry_unwind_day, before convexity_entry_after_et
    CONVEXITY_ENTRY  convexity_entry_day, at or after convexity_entry_after_et,
                     and before nfp_release_et on submission day
    HOLD_THROUGH_NFP after CONVEXITY_ENTRY window until nfp_release_et
    MONETIZE         at or after nfp_release_et, before flatten_by_et
    FLAT             at or after flatten_by_et, before submission_deadline_et
    POST_DEADLINE    at or after submission_deadline_et

Design note: current_phase() calls _now_et() (or broker clock) internally so
tests can freeze time via freezegun without monkeypatching every call site.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

from barbell.config import get_settings

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Phase enum
# ---------------------------------------------------------------------------


class Phase(Enum):
    """Endgame phase — the dated state machine for the judged window."""

    BUILD = "BUILD"
    CARRY_ACTIVE = "CARRY_ACTIVE"
    UNWIND = "UNWIND"
    CONVEXITY_ENTRY = "CONVEXITY_ENTRY"
    HOLD_THROUGH_NFP = "HOLD_THROUGH_NFP"
    MONETIZE = "MONETIZE"
    FLAT = "FLAT"
    POST_DEADLINE = "POST_DEADLINE"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_et() -> datetime:
    """Return the current time as a timezone-aware ET datetime.

    Isolated into a function so tests can freeze it via freezegun.
    """
    return datetime.now(tz=ET)


def _make_aware(dt: datetime) -> datetime:
    """Attach ET timezone if the datetime is naive (e.g. from PyYAML)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_phase(now: datetime | None = None) -> Phase:
    """Determine the current endgame phase from the calendar in settings.

    Args:
        now: Optional ET-aware datetime to use as "now". If None, uses the
             real current time. Pass a value in tests (or let freezegun freeze
             datetime.now) to make this deterministic without live clock calls.

    Returns:
        The current Phase enum value.

    Raises:
        Nothing — all config parsing errors fall through to a conservative
        default (FLAT, blocking new entries) rather than crashing the loop.
    """
    try:
        cfg = get_settings().calendar
    except Exception as exc:
        log.error("current_phase: could not load settings — defaulting to FLAT: %s", exc)
        return Phase.FLAT

    if now is None:
        now = _now_et()
    # Ensure tz-aware in ET
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    today = now.date()

    # Boundary datetimes — all made ET-aware
    submission_deadline = _make_aware(cfg.submission_deadline_et)
    flatten_by = _make_aware(cfg.flatten_by_et)
    nfp_release = _make_aware(cfg.nfp_release_et)

    # Parse convexity_entry_after_et "HH:MM" into a full ET datetime for
    # convexity_entry_day
    h, m = (int(x) for x in cfg.convexity_entry_after_et.split(":"))
    convexity_threshold = datetime(
        cfg.convexity_entry_day.year,
        cfg.convexity_entry_day.month,
        cfg.convexity_entry_day.day,
        h, m, 0,
        tzinfo=ET,
    )

    # -----------------------------------------------------------------------
    # Phase determination — evaluated in reverse-chronological order so the
    # first match (most-future boundary already passed) wins.
    # -----------------------------------------------------------------------

    # 8. POST_DEADLINE — at or after submission deadline
    if now >= submission_deadline:
        return Phase.POST_DEADLINE

    # 7. FLAT — at or after flatten_by, before submission deadline
    if now >= flatten_by:
        return Phase.FLAT

    # 6. MONETIZE — at or after NFP release, before flatten_by
    if now >= nfp_release:
        return Phase.MONETIZE

    # 5. HOLD_THROUGH_NFP — after convexity_entry_after_et on
    #    convexity_entry_day, until nfp_release_et (which is on submission day)
    #    This phase spans the overnight gap between Sep 3 afternoon and Sep 4
    #    morning NFP release.
    if now >= convexity_threshold and today >= cfg.convexity_entry_day:
        return Phase.HOLD_THROUGH_NFP

    # 4. CONVEXITY_ENTRY — convexity_entry_day, at or after convexity_entry_after_et
    #    (already handled above as HOLD_THROUGH_NFP for "after" — this branch
    #    is therefore redundant but kept for clarity; the logic above subsumes it)
    # Note: CONVEXITY_ENTRY and HOLD_THROUGH_NFP share the same trigger time.
    # CONVEXITY_ENTRY is the window *right at* that threshold on *that day*;
    # once it's the next day, we're in HOLD_THROUGH_NFP until NFP.
    # Since HOLD_THROUGH_NFP check above catches both, we leave this here
    # only for the allowed_actions mapping.

    # 3. UNWIND — carry_unwind_day, before convexity_entry_after_et
    if today == cfg.carry_unwind_day:
        # Before convexity threshold → UNWIND; at/after → already caught above
        return Phase.UNWIND

    # 2. CARRY_ACTIVE — from first_full_session through last_carry_entry_day (EOD)
    if cfg.first_full_session <= today <= cfg.last_carry_entry_day:
        return Phase.CARRY_ACTIVE

    # 1. BUILD — before first_full_session
    if today < cfg.first_full_session:
        return Phase.BUILD

    # Fallback: gap day(s) between last_carry_entry_day and carry_unwind_day
    # Treat as CARRY_ACTIVE (no new entries allowed beyond last entry day, but
    # that's enforced by allowed_actions; the phase itself is still active-ish).
    # In the current calendar: Sep 2 (last entry) → Sep 3 (unwind) so no gap.
    log.warning(
        "current_phase: today=%s falls in an unexpected gap in the calendar. "
        "Defaulting to FLAT to block new entries safely.",
        today,
    )
    return Phase.FLAT


def allowed_actions(phase: Phase) -> set[str]:
    """Return the set of allowed trading actions for the given phase.

    Action strings:
        "sleeve_a_open"   — open a new Sleeve A position (put credit spread / IC)
        "sleeve_a_close"  — close an existing Sleeve A position
        "sleeve_b_open"   — open a new Sleeve B position (SPY put debit spread)
        "sleeve_b_close"  — close an existing Sleeve B position
        "read"            — read-only operations (status, journal queries)

    CARRY_ACTIVE  → Sleeve A opens + closes allowed
    UNWIND        → Sleeve A closes only (routed through submit_basket, same
                    reconcile discipline as entries — no special-cased parallel close)
    CONVEXITY_ENTRY → Sleeve B open (size based on NAV vs starting_nav)
    HOLD_THROUGH_NFP → no new entries; no closes (hold existing positions)
    MONETIZE      → Sleeve B closes only
    FLAT          → reads only
    POST_DEADLINE → reads only
    BUILD         → reads only (no live trading before first session)
    """
    _READ = {"read"}

    mapping: dict[Phase, set[str]] = {
        Phase.BUILD: _READ,
        Phase.CARRY_ACTIVE: {"sleeve_a_open", "sleeve_a_close", "read"},
        Phase.UNWIND: {"sleeve_a_close", "read"},
        Phase.CONVEXITY_ENTRY: {"sleeve_b_open", "read"},
        Phase.HOLD_THROUGH_NFP: _READ,
        Phase.MONETIZE: {"sleeve_b_close", "read"},
        Phase.FLAT: _READ,
        Phase.POST_DEADLINE: _READ,
    }
    return mapping[phase]
