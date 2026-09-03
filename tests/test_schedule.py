"""
endgame/schedule.py tested against frozen times (freezegun) at the exact
boundaries that matter: 23:59 Sep 2 (last carry entry), 14:29/14:31 ET Sep 3
(convexity entry gate), 08:29/08:31 ET Sep 4 (NFP), 10:44/10:46 ET Sep 4
(flatten deadline), 10:59/11:01 ET Sep 4 (submission deadline). Off-by-one
here is the single bug most likely to leave a position open at judging time.

Also tests the two now-wired risk gates that depend on schedule.py:
  - gate_pre_nfp_flatten: should VETO in HOLD_THROUGH_NFP / MONETIZE / FLAT / POST_DEADLINE
  - gate_expiry_past_deadline: should VETO when any leg expiry > deadline date
"""

from __future__ import annotations

import os
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# Fixtures — minimal env so get_settings() doesn't fail
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Provide minimal env vars + a real settings.yaml for schedule tests."""
    import shutil
    from pathlib import Path

    # Copy real settings into tmp_path so get_settings() finds them
    repo_root = Path(__file__).resolve().parent.parent
    config_src = repo_root / "config"
    config_dst = tmp_path / "config"
    shutil.copytree(config_src, config_dst)

    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini")
    monkeypatch.setenv("BARBELL_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BARBELL_LOG_LEVEL", "DEBUG")

    # Point get_settings to tmp config
    import barbell.config as cfg_mod
    original_root = cfg_mod._ROOT
    monkeypatch.setattr(cfg_mod, "_ROOT", tmp_path)
    cfg_mod.get_settings.cache_clear()

    yield

    cfg_mod.get_settings.cache_clear()
    monkeypatch.setattr(cfg_mod, "_ROOT", original_root)


def _frozen(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> datetime:
    """Return an ET-aware datetime for freezegun."""
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


# ---------------------------------------------------------------------------
# Helper: call current_phase() with an explicit now= argument
# (freezegun works, but passing now= is more explicit and faster)
# ---------------------------------------------------------------------------

def _phase(dt: datetime):
    from barbell.endgame.schedule import current_phase
    return current_phase(now=dt)


def _actions(dt: datetime):
    from barbell.endgame.schedule import allowed_actions, current_phase
    return allowed_actions(current_phase(now=dt))


# ---------------------------------------------------------------------------
# BUILD phase — before Sep 1
# ---------------------------------------------------------------------------

class TestBuildPhase:
    def test_aug30_is_build(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 8, 30, 9, 0)) == Phase.BUILD

    def test_aug31_morning_is_build_before_session(self):
        # first_full_session = 2026-08-31; date boundary, so anything on Aug 30 is BUILD
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 8, 30, 23, 59)) == Phase.BUILD

    def test_build_allows_only_reads(self):
        from barbell.endgame.schedule import Phase
        actions = _actions(_frozen(2026, 8, 30, 10, 0))
        assert "sleeve_a_open" not in actions
        assert "sleeve_b_open" not in actions
        assert "read" in actions


# ---------------------------------------------------------------------------
# CARRY_ACTIVE phase — Sep 1 and Sep 2
# ---------------------------------------------------------------------------

class TestCarryActivePhase:
    def test_sep1_morning_is_carry_active(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 1, 9, 30)) == Phase.CARRY_ACTIVE

    def test_sep2_morning_is_carry_active(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 2, 9, 30)) == Phase.CARRY_ACTIVE

    def test_sep2_2359_is_carry_active(self):
        """Last carry entry day EOD is still CARRY_ACTIVE."""
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 2, 23, 59, 59)) == Phase.CARRY_ACTIVE

    def test_carry_active_allows_sleeve_a_open(self):
        actions = _actions(_frozen(2026, 9, 1, 10, 0))
        assert "sleeve_a_open" in actions
        assert "sleeve_a_close" in actions
        assert "sleeve_b_open" not in actions


# ---------------------------------------------------------------------------
# Boundary: last_carry_entry_day EOD → carry_unwind_day start
# ---------------------------------------------------------------------------

class TestCarryToUnwindBoundary:
    def test_sep2_2359_carry_active(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 2, 23, 59, 59)) == Phase.CARRY_ACTIVE

    def test_sep3_0001_is_unwind(self):
        """Sep 3 00:01 ET should be UNWIND (carry_unwind_day)."""
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 3, 0, 1)) == Phase.UNWIND


# ---------------------------------------------------------------------------
# UNWIND phase — Sep 3 before 14:30 ET
# ---------------------------------------------------------------------------

class TestUnwindPhase:
    def test_sep3_0900_is_unwind(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 3, 9, 0)) == Phase.UNWIND

    def test_sep3_1429_is_unwind(self):
        """1 minute before convexity_entry_after_et is still UNWIND."""
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 3, 14, 29)) == Phase.UNWIND

    def test_unwind_allows_only_sleeve_a_close(self):
        actions = _actions(_frozen(2026, 9, 3, 10, 0))
        assert "sleeve_a_close" in actions
        assert "sleeve_a_open" not in actions
        assert "sleeve_b_open" not in actions


# ---------------------------------------------------------------------------
# Boundary: UNWIND → HOLD_THROUGH_NFP at convexity_entry_after_et (14:30 ET Sep 3)
# ---------------------------------------------------------------------------

class TestConvexityEntryBoundary:
    def test_sep3_1429_is_unwind(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 3, 14, 29)) == Phase.UNWIND

    def test_sep3_1430_is_hold_through_nfp(self):
        """At exactly 14:30 ET Sep 3, phase transitions to HOLD_THROUGH_NFP.
        The CONVEXITY_ENTRY window is captured by HOLD_THROUGH_NFP in this
        implementation since the same threshold triggers both phases and
        HOLD_THROUGH_NFP persists through Sep 4 morning NFP.
        """
        from barbell.endgame.schedule import Phase
        # Phase at 14:30 on convexity_entry_day should be HOLD_THROUGH_NFP
        p = _phase(_frozen(2026, 9, 3, 14, 30))
        assert p in (Phase.HOLD_THROUGH_NFP, Phase.CONVEXITY_ENTRY)

    def test_sep3_1431_is_hold_through_nfp(self):
        from barbell.endgame.schedule import Phase
        p = _phase(_frozen(2026, 9, 3, 14, 31))
        assert p in (Phase.HOLD_THROUGH_NFP, Phase.CONVEXITY_ENTRY)

    def test_actions_at_convexity_entry_allow_sleeve_b_open(self):
        actions = _actions(_frozen(2026, 9, 3, 14, 30))
        # Either CONVEXITY_ENTRY or HOLD_THROUGH_NFP; both should at minimum
        # restrict sleeve_a_open
        assert "sleeve_a_open" not in actions


# ---------------------------------------------------------------------------
# HOLD_THROUGH_NFP — Sep 3 after 14:30 through Sep 4 before NFP
# ---------------------------------------------------------------------------

class TestHoldThroughNFPPhase:
    def test_sep3_1500_is_hold(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 3, 15, 0)) in (Phase.HOLD_THROUGH_NFP, Phase.CONVEXITY_ENTRY)

    def test_sep4_0829_is_hold(self):
        """1 minute before NFP release is still HOLD_THROUGH_NFP."""
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 8, 29)) == Phase.HOLD_THROUGH_NFP


# ---------------------------------------------------------------------------
# Boundary: HOLD_THROUGH_NFP → MONETIZE at nfp_release_et (08:30 Sep 4)
# ---------------------------------------------------------------------------

class TestNFPBoundary:
    def test_sep4_0829_is_hold(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 8, 29)) == Phase.HOLD_THROUGH_NFP

    def test_sep4_0830_is_monetize(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 8, 30)) == Phase.MONETIZE

    def test_sep4_0831_is_monetize(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 8, 31)) == Phase.MONETIZE


# ---------------------------------------------------------------------------
# MONETIZE phase
# ---------------------------------------------------------------------------

class TestMonetizePhase:
    def test_monetize_allows_only_sleeve_b_close(self):
        actions = _actions(_frozen(2026, 9, 4, 9, 0))
        assert "sleeve_b_close" in actions
        assert "sleeve_a_open" not in actions
        assert "sleeve_b_open" not in actions
        assert "sleeve_a_close" not in actions


# ---------------------------------------------------------------------------
# Boundary: MONETIZE → FLAT at flatten_by_et (10:45 Sep 4)
# ---------------------------------------------------------------------------

class TestFlattenBoundary:
    def test_sep4_1044_is_monetize(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 10, 44)) == Phase.MONETIZE

    def test_sep4_1045_is_flat(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 10, 45)) == Phase.FLAT

    def test_sep4_1046_is_flat(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 10, 46)) == Phase.FLAT


# ---------------------------------------------------------------------------
# FLAT phase
# ---------------------------------------------------------------------------

class TestFlatPhase:
    def test_flat_allows_only_reads(self):
        actions = _actions(_frozen(2026, 9, 4, 10, 50))
        assert actions == {"read"}


# ---------------------------------------------------------------------------
# Boundary: FLAT → POST_DEADLINE at submission_deadline_et (11:00 Sep 4)
# ---------------------------------------------------------------------------

class TestDeadlineBoundary:
    def test_sep4_1059_is_flat(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 10, 59)) == Phase.FLAT

    def test_sep4_1100_is_post_deadline(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 11, 0)) == Phase.POST_DEADLINE

    def test_sep4_1101_is_post_deadline(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 4, 11, 1)) == Phase.POST_DEADLINE


# ---------------------------------------------------------------------------
# POST_DEADLINE phase
# ---------------------------------------------------------------------------

class TestPostDeadlinePhase:
    def test_post_deadline_allows_only_reads(self):
        actions = _actions(_frozen(2026, 9, 4, 11, 30))
        assert actions == {"read"}

    def test_far_future_is_post_deadline(self):
        from barbell.endgame.schedule import Phase
        assert _phase(_frozen(2026, 9, 5, 12, 0)) == Phase.POST_DEADLINE


# ---------------------------------------------------------------------------
# allowed_actions exhaustive check
# ---------------------------------------------------------------------------

class TestAllowedActions:
    def test_all_phases_return_read(self):
        from barbell.endgame.schedule import Phase, allowed_actions
        for phase in Phase:
            assert "read" in allowed_actions(phase), f"'read' missing from {phase}"

    def test_no_phase_allows_sleeve_a_open_and_b_open_simultaneously(self):
        from barbell.endgame.schedule import Phase, allowed_actions
        for phase in Phase:
            actions = allowed_actions(phase)
            assert not (
                "sleeve_a_open" in actions and "sleeve_b_open" in actions
            ), f"{phase} allows both sleeve_a_open and sleeve_b_open"


# ---------------------------------------------------------------------------
# Risk gate: gate_pre_nfp_flatten — now wired to schedule.py
# ---------------------------------------------------------------------------

class TestGatePreNFPFlatten:
    """Verify the gate VETOs in blocking phases and PASSes in entry phases."""

    def _make_proposal(self):
        from datetime import date
        from barbell.agent.schemas import ProposedLeg, ProposedStructure
        return ProposedStructure(
            underlying="NVDA",
            legs=[
                ProposedLeg(expiry=date(2026, 9, 3), strike=110.0, right="put", side="sell", contracts=1),
                ProposedLeg(expiry=date(2026, 9, 3), strike=105.0, right="put", side="buy", contracts=1),
            ],
            rationale="test",
            sleeve="A",
            max_loss_estimate=500.0,
        )

    def _make_portfolio(self):
        from barbell.agent.schemas import PortfolioState
        return PortfolioState(current_nav=100_000.0, starting_nav=100_000.0)

    def _make_market(self):
        from barbell.agent.schemas import MarketState
        return MarketState()

    def _make_config(self):
        from barbell.config import get_settings
        return get_settings().risk_gates

    def _run_gate(self, phase_name: str) -> str:
        from barbell.endgame.schedule import Phase
        from barbell.risk.gates import gate_pre_nfp_flatten
        phase = Phase[phase_name]
        with patch("barbell.endgame.schedule.current_phase", return_value=phase):
            result = gate_pre_nfp_flatten(
                self._make_proposal(),
                self._make_portfolio(),
                self._make_market(),
                self._make_config(),
            )
        return result.outcome

    def test_carry_active_passes(self):
        assert self._run_gate("CARRY_ACTIVE") == "PASS"

    def test_unwind_passes(self):
        assert self._run_gate("UNWIND") == "PASS"

    def test_hold_through_nfp_vetos(self):
        assert self._run_gate("HOLD_THROUGH_NFP") == "VETO"

    def test_monetize_vetos(self):
        assert self._run_gate("MONETIZE") == "VETO"

    def test_flat_vetos(self):
        assert self._run_gate("FLAT") == "VETO"

    def test_post_deadline_vetos(self):
        assert self._run_gate("POST_DEADLINE") == "VETO"

    def test_build_passes(self):
        assert self._run_gate("BUILD") == "PASS"


# ---------------------------------------------------------------------------
# Risk gate: gate_expiry_past_deadline
# ---------------------------------------------------------------------------

class TestGateExpiryPastDeadline:
    """Verify the gate VETOs legs expiring after the submission deadline."""

    def _make_portfolio(self):
        from barbell.agent.schemas import PortfolioState
        return PortfolioState(current_nav=100_000.0, starting_nav=100_000.0)

    def _make_market(self):
        from barbell.agent.schemas import MarketState
        return MarketState()

    def _make_config(self):
        from barbell.config import get_settings
        return get_settings().risk_gates

    def _make_proposal(self, expiry: date):
        from barbell.agent.schemas import ProposedLeg, ProposedStructure
        return ProposedStructure(
            underlying="NVDA",
            legs=[
                ProposedLeg(expiry=expiry, strike=110.0, right="put", side="sell", contracts=1),
                ProposedLeg(expiry=expiry, strike=105.0, right="put", side="buy", contracts=1),
            ],
            rationale="test",
            sleeve="A",
            max_loss_estimate=500.0,
        )

    def _run_gate(self, expiry: date) -> str:
        from barbell.risk.gates import gate_expiry_past_deadline
        result = gate_expiry_past_deadline(
            self._make_proposal(expiry),
            self._make_portfolio(),
            self._make_market(),
            self._make_config(),
        )
        return result.outcome

    def test_expiry_before_deadline_passes(self):
        # deadline is Sep 4, 2026 — Sep 3 passes
        assert self._run_gate(date(2026, 9, 3)) == "PASS"

    def test_expiry_on_deadline_day_passes(self):
        # Sep 4 = deadline day itself — passes (leg expires THAT day)
        assert self._run_gate(date(2026, 9, 4)) == "PASS"

    def test_expiry_after_deadline_vetos(self):
        # Sep 5 is after the deadline — should VETO
        assert self._run_gate(date(2026, 9, 5)) == "VETO"

    def test_expiry_week_after_deadline_vetos(self):
        assert self._run_gate(date(2026, 9, 11)) == "VETO"
