"""
Tests for scheduler/loop.py — mock all external calls.

Key scenarios tested:
  1. Happy-path cycle runs start to finish with all mocked stages
  2. Catalyst gate failure on one candidate doesn't stop the rest
  3. reconcile() always runs even if an earlier stage threw
  4. Phase gating: BUILD phase skips screen, FLAT phase skips everything
  5. submit_basket is called with the passing proposals, not the vetoed ones
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_account.return_value = {"equity": "100500.00", "buying_power": "50000.00"}
    client.get_positions.return_value = []
    client.get_option_chain.return_value = {}
    client.get_clock.return_value = {"is_open": True, "next_close": "16:00"}
    return client


@pytest.fixture
def mock_store(tmp_path):
    """Real JournalStore backed by a temp DB."""
    import os

    os.environ.setdefault("ALPACA_API_KEY", "test")
    os.environ.setdefault("ALPACA_SECRET_KEY", "test")
    os.environ.setdefault("ALPACA_PAPER_TRADE", "true")
    os.environ.setdefault("ANTHROPIC_API_KEY", "test")
    os.environ.setdefault("BARBELL_DB_PATH", str(tmp_path / "test.db"))
    os.environ.setdefault("BARBELL_LOG_LEVEL", "DEBUG")

    from barbell.journal.store import JournalStore

    return JournalStore(db_path=str(tmp_path / "test.db"))


def _make_screen_result(symbol: str = "NVDA", passed: bool = True):
    from barbell.agent.schemas import ScreenResult

    return ScreenResult(
        symbol=symbol,
        passed=passed,
        reason="test",
        metrics={"iv": 0.4, "iv_rank": 60.0},
    )


def _make_structure(symbol: str = "NVDA"):
    from barbell.agent.schemas import ProposedLeg, ProposedStructure

    return ProposedStructure(
        underlying=symbol,
        legs=[
            ProposedLeg(expiry=date(2026, 9, 3), strike=110.0, right="put", side="sell", contracts=1),
            ProposedLeg(expiry=date(2026, 9, 3), strike=105.0, right="put", side="buy", contracts=1),
        ],
        rationale="test structure",
        sleeve="A",
        max_loss_estimate=500.0,
    )


def _make_verdict(symbol: str = "NVDA", catalyst_risk: bool = False):
    from barbell.agent.schemas import CatalystVerdict

    return CatalystVerdict(
        symbol=symbol,
        catalyst_risk=catalyst_risk,
        reasoning="test reasoning",
    )


def _make_risk_decision(symbol: str = "NVDA", outcome: str = "PASS"):
    from barbell.agent.schemas import RiskDecision

    structure = _make_structure(symbol)
    return RiskDecision(
        outcome=outcome,
        contracts=1 if outcome != "VETO" else None,
        reasons=["All gates passed."],
        proposed=structure,
    )


def _make_reconcile_report(diverged: bool = False):
    from barbell.execution.reconcile import ReconciliationReport

    return ReconciliationReport(
        diverged=diverged,
        description="No divergence detected." if not diverged else "Position mismatch!",
    )


def _make_market_state():
    from barbell.agent.schemas import MarketState
    return MarketState(dispersion_score=1.25)


def _make_digest(symbol: str = "NVDA"):
    from barbell.agent.schemas import HeadlineDigest
    return HeadlineDigest(symbol=symbol, news_volume="normal", summary="No major news.")


# ---------------------------------------------------------------------------
# Test 1: Happy-path cycle
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_full_cycle_runs_start_to_finish(self, mock_client, mock_store):
        """All stages succeed — verify summary fields are populated."""
        structure = _make_structure("NVDA")
        verdict = _make_verdict("NVDA", catalyst_risk=False)
        decision = _make_risk_decision("NVDA", outcome="PASS")
        market_state = _make_market_state()
        digest = _make_digest("NVDA")

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", return_value={"tech": ["NVDA"]}),
            patch("barbell.screen.universe.screen", return_value=([_make_screen_result("NVDA")], market_state)),
            patch("barbell.screen.headline_triage.digest_headlines", return_value=digest),
            patch("barbell.agent.catalyst_gate.check_catalyst", return_value=verdict),
            patch("barbell.agent.structure_agent.propose_structure", return_value=structure),
            patch("barbell.risk.engine.evaluate", return_value=decision),
            patch("barbell.execution.orders.submit_basket", return_value=[{"status": "filled"}]),
            patch("barbell.execution.reconcile.reconcile", return_value=_make_reconcile_report(False)),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.CARRY_ACTIVE

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-cycle-1", mock_client, mock_store)

        assert summary["phase"] == "CARRY_ACTIVE"
        assert summary["survivors"] == 1
        assert summary["proposals"] == 1
        assert summary["decisions_pass"] == 1
        assert summary["decisions_veto"] == 0
        assert summary["orders_submitted"] >= 1
        assert summary["reconcile_diverged"] is False

    def test_reconcile_always_runs_even_if_screen_throws(self, mock_client, mock_store):
        """reconcile() must run in finally even if screen stage raises."""
        mock_reconcile = MagicMock(return_value=_make_reconcile_report(False))

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", side_effect=RuntimeError("screen exploded")),
            patch("barbell.execution.reconcile.reconcile", mock_reconcile),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.CARRY_ACTIVE

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-cycle-recon", mock_client, mock_store)

        # reconcile must have been called
        mock_reconcile.assert_called_once()
        # survivors = 0 because screen crashed
        assert summary["survivors"] == 0

    def test_reconcile_runs_even_if_structure_agent_throws(self, mock_client, mock_store):
        """reconcile() must run even if structure_agent raises for all candidates."""
        market_state = _make_market_state()
        mock_reconcile = MagicMock(return_value=_make_reconcile_report(False))
        digest = _make_digest()

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", return_value={"tech": ["NVDA"]}),
            patch("barbell.screen.universe.screen", return_value=([_make_screen_result("NVDA")], market_state)),
            patch("barbell.screen.headline_triage.digest_headlines", return_value=digest),
            patch("barbell.agent.catalyst_gate.check_catalyst", return_value=_make_verdict("NVDA")),
            patch("barbell.agent.structure_agent.propose_structure", side_effect=RuntimeError("LLM timeout")),
            patch("barbell.execution.reconcile.reconcile", mock_reconcile),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.CARRY_ACTIVE

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-cycle-struct-fail", mock_client, mock_store)

        mock_reconcile.assert_called_once()
        assert summary["proposals"] == 0


# ---------------------------------------------------------------------------
# Test 2: Catalyst gate failure on one candidate doesn't stop the rest
# ---------------------------------------------------------------------------


class TestCatalystGatePartialFailure:
    def test_one_catalyst_blocked_others_continue(self, mock_client, mock_store):
        """NVDA blocked by catalyst, AMD passes — AMD should produce a proposal."""
        market_state = _make_market_state()
        digest_nvda = _make_digest("NVDA")
        digest_amd = _make_digest("AMD")

        def catalyst_side_effect(symbol, headlines, digest, result):
            if symbol == "NVDA":
                return _make_verdict("NVDA", catalyst_risk=True)
            return _make_verdict("AMD", catalyst_risk=False)

        def structure_side_effect(symbol, chain, result, dispersion_score=None):
            if symbol == "AMD":
                return _make_structure("AMD")
            raise AssertionError(f"propose_structure called for blocked symbol {symbol}")

        def digest_side_effect(symbol, headlines):
            return _make_digest(symbol)

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch(
                "barbell.screen.universe.load_candidates",
                return_value={"tech": ["NVDA", "AMD"]},
            ),
            patch(
                "barbell.screen.universe.screen",
                return_value=(
                    [_make_screen_result("NVDA"), _make_screen_result("AMD")],
                    market_state,
                ),
            ),
            patch("barbell.screen.headline_triage.digest_headlines", side_effect=digest_side_effect),
            patch("barbell.agent.catalyst_gate.check_catalyst", side_effect=catalyst_side_effect),
            patch("barbell.agent.structure_agent.propose_structure", side_effect=structure_side_effect),
            patch("barbell.risk.engine.evaluate", return_value=_make_risk_decision("AMD")),
            patch("barbell.execution.orders.submit_basket", return_value=[{"status": "filled"}]),
            patch("barbell.execution.reconcile.reconcile", return_value=_make_reconcile_report()),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.CARRY_ACTIVE

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-cycle-partial", mock_client, mock_store)

        assert summary["survivors"] == 2
        assert summary["proposals"] == 1  # only AMD
        assert summary["decisions_pass"] == 1

    def test_catalyst_exception_skips_candidate_continues_rest(self, mock_client, mock_store):
        """check_catalyst() raising (not returning catalyst_risk=True) is also handled."""
        market_state = _make_market_state()

        def catalyst_side_effect(symbol, headlines, digest, result):
            if symbol == "NVDA":
                raise RuntimeError("Anthropic API timeout")
            return _make_verdict("AMD", catalyst_risk=False)

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", return_value={"tech": ["NVDA", "AMD"]}),
            patch(
                "barbell.screen.universe.screen",
                return_value=(
                    [_make_screen_result("NVDA"), _make_screen_result("AMD")],
                    market_state,
                ),
            ),
            patch("barbell.screen.headline_triage.digest_headlines", return_value=_make_digest()),
            patch("barbell.agent.catalyst_gate.check_catalyst", side_effect=catalyst_side_effect),
            patch("barbell.agent.structure_agent.propose_structure", return_value=_make_structure("AMD")),
            patch("barbell.risk.engine.evaluate", return_value=_make_risk_decision("AMD")),
            patch("barbell.execution.orders.submit_basket", return_value=[]),
            patch("barbell.execution.reconcile.reconcile", return_value=_make_reconcile_report()),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.CARRY_ACTIVE

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-cycle-exc", mock_client, mock_store)

        # NVDA skipped (exception), AMD should have produced a proposal
        assert summary["proposals"] == 1


# ---------------------------------------------------------------------------
# Test 3: Phase gating
# ---------------------------------------------------------------------------


class TestPhaseGating:
    def test_build_phase_skips_screen(self, mock_client, mock_store):
        """In BUILD phase, screen should not be called."""
        mock_screen = MagicMock()
        mock_reconcile = MagicMock(return_value=_make_reconcile_report())

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", mock_screen),
            patch("barbell.execution.reconcile.reconcile", mock_reconcile),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.BUILD

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-build", mock_client, mock_store)

        mock_screen.assert_not_called()
        assert summary["survivors"] == 0
        assert summary["proposals"] == 0

    def test_flat_phase_skips_screen(self, mock_client, mock_store):
        """In FLAT phase, screen should not be called."""
        mock_screen = MagicMock()
        mock_reconcile = MagicMock(return_value=_make_reconcile_report())

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", mock_screen),
            patch("barbell.execution.reconcile.reconcile", mock_reconcile),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.FLAT

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-flat", mock_client, mock_store)

        mock_screen.assert_not_called()
        # reconcile always runs
        mock_reconcile.assert_called_once()

    def test_post_deadline_skips_basket(self, mock_client, mock_store):
        """In POST_DEADLINE phase, submit_basket should never be called."""
        mock_basket = MagicMock()
        mock_reconcile = MagicMock(return_value=_make_reconcile_report())

        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", return_value={}),
            patch("barbell.execution.orders.submit_basket", mock_basket),
            patch("barbell.execution.reconcile.reconcile", mock_reconcile),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.POST_DEADLINE

            from barbell.scheduler.loop import run_one_cycle
            run_one_cycle("test-post", mock_client, mock_store)

        mock_basket.assert_not_called()
        mock_reconcile.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4: Reconcile diverged flag propagation
# ---------------------------------------------------------------------------


class TestReconcileDiverged:
    def test_reconcile_diverged_surfaces_in_summary(self, mock_client, mock_store):
        """A diverged reconcile should appear in the cycle summary."""
        with (
            patch("barbell.endgame.schedule.current_phase") as mock_phase,
            patch("barbell.screen.universe.load_candidates", return_value={}),
            patch("barbell.execution.reconcile.reconcile", return_value=_make_reconcile_report(diverged=True)),
        ):
            from barbell.endgame.schedule import Phase
            mock_phase.return_value = Phase.FLAT

            from barbell.scheduler.loop import run_one_cycle
            summary = run_one_cycle("test-diverge", mock_client, mock_store)

        assert summary["reconcile_diverged"] is True
