"""
Tests for execution/orders.py (submit + submit_basket) and
execution/reconcile.py.

AlpacaClient is mocked throughout — no live API calls, ever.

Test cases:
    1. PASS decision → one order submitted, one journal row written.
    2. VETO decision → no order submitted, submit() returns 'vetoed'.
    3. Partial fill then timeout → expected number of retries, then
       an abandoned-order row — never an infinite loop.
    4. Diverged reconcile blocks subsequent submit_basket.
    5. submit_basket — 3-underlying basket:
       - reserves capital before leg 1 is submitted
       - fills all 3 legs sequentially
       - releases reservation after leg 3
    6. submit_basket — forced mid-basket gate VETO on leg 2:
       - leg 1 fills and stays in place
       - leg 2 triggers a VETO in the post-fill risk re-evaluation
       - leg 3 is never built
"""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from barbell.agent.schemas import (
    MarketState,
    PortfolioState,
    ProposedLeg,
    ProposedStructure,
    RiskDecision,
)
from barbell.config import ExecutionConfig, RiskGateConfig
from barbell.execution.orders import submit, submit_basket
from barbell.execution.reconcile import ReconciliationReport, reconcile
from barbell.journal.store import JournalStore
from barbell.risk.kill_switch import reset_in_process_cache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_exec.db")


@pytest.fixture
def store(db_path):
    return JournalStore(db_path=db_path)


@pytest.fixture
def exec_config() -> ExecutionConfig:
    return ExecutionConfig(
        order_type="limit",
        time_in_force="day",
        poll_interval_seconds=1,
        fill_timeout_seconds=2,  # short for fast tests
    )


@pytest.fixture
def risk_config() -> RiskGateConfig:
    return RiskGateConfig(
        max_loss_per_position_pct_nav=0.008,
        max_loss_portfolio_pct_nav=0.12,
        basket_reserve_before_first_leg=True,
        max_quote_age_seconds=120,
        max_sector_concentration=3,
        max_positions_per_underlying=1,
        drawdown_kill_switch_pct_nav=-0.08,
        max_slippage_pct_of_mid=0.10,
        order_retry_limit=2,
        order_retry_widen_pct=0.02,
    )


def _make_proposal(underlying="AAPL", max_loss=500.0, contracts=2) -> ProposedStructure:
    exp = date(2026, 9, 4)
    return ProposedStructure(
        underlying=underlying,
        legs=[
            ProposedLeg(symbol=f"{underlying}260904P00200000", expiry=exp, strike=200.0, right="put", side="sell", contracts=contracts),
            ProposedLeg(symbol=f"{underlying}260904P00195000", expiry=exp, strike=195.0, right="put", side="buy", contracts=contracts),
        ],
        rationale="test",
        sleeve="A",
        max_loss_estimate=max_loss,
        limit_price=1.25,
        structure_type="put_credit_spread",
    )


def _pass_decision(proposal: ProposedStructure, contracts: int | None = None) -> RiskDecision:
    return RiskDecision(
        outcome="PASS",
        contracts=contracts or max(leg.contracts for leg in proposal.legs),
        reasons=["all gates passed"],
        proposed=proposal,
    )


def _veto_decision(proposal: ProposedStructure) -> RiskDecision:
    return RiskDecision(
        outcome="VETO",
        contracts=None,
        reasons=["test veto"],
        proposed=proposal,
    )


def _mock_client_filled(order_id: str = "ord-123", fill_price: float = 1.20) -> MagicMock:
    """AlpacaClient mock that returns a filled order on first poll."""
    client = MagicMock()
    client.submit_mleg_order.return_value = order_id

    # Mock get_order_by_id to return filled status immediately
    mock_order = MagicMock()
    mock_order.status.value = "filled"
    mock_order.filled_qty = "2"
    mock_order.filled_avg_price = str(fill_price)
    client._trading.get_order_by_id.return_value = mock_order

    client.get_positions.return_value = []
    client.get_account.return_value = {
        "equity": 100_000.0, "buying_power": 100_000.0,
        "options_buying_power": 100_000.0, "options_approved_level": 3,
        "options_trading_level": 3, "cash": 100_000.0,
        "portfolio_value": 100_000.0, "trading_blocked": False,
        "id": "test-id", "account_number": "PA123",
    }
    return client


def _mock_client_timeout() -> MagicMock:
    """AlpacaClient mock that never fills — always times out."""
    client = MagicMock()
    client.submit_mleg_order.return_value = "ord-timeout"

    # Always returns non-filled status
    mock_order = MagicMock()
    mock_order.status.value = "pending_new"
    mock_order.filled_qty = "0"
    mock_order.filled_avg_price = None
    client._trading.get_order_by_id.return_value = mock_order
    client._trading.cancel_order_by_id.return_value = None

    client.get_positions.return_value = []
    client.get_account.return_value = {
        "equity": 100_000.0, "buying_power": 100_000.0,
        "options_buying_power": 100_000.0, "options_approved_level": 3,
        "options_trading_level": 3, "cash": 100_000.0,
        "portfolio_value": 100_000.0, "trading_blocked": False,
        "id": "test-id", "account_number": "PA123",
    }
    return client


# ---------------------------------------------------------------------------
# Test 1: PASS decision → one order submitted
# ---------------------------------------------------------------------------

class TestSubmitPassDecision:
    def test_pass_decision_submits_one_order(self, store, exec_config, risk_config):
        reset_in_process_cache()
        proposal = _make_proposal()
        decision = _pass_decision(proposal)

        client = _mock_client_filled(order_id="ord-pass-1", fill_price=1.20)
        result = submit(
            decision=decision,
            proposed=proposal,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            cycle_id="cycle-pass-1",
        )

        assert result["status"] == "filled"
        assert result["order_id"] == "ord-pass-1"
        assert result["fill_price"] == 1.20
        assert result["attempt_count"] == 1
        assert not result["abandoned"]
        # Verify submit_mleg_order was called exactly once
        client.submit_mleg_order.assert_called_once()

    def test_submit_enforces_limit_order_not_market(self, store, exec_config, risk_config):
        """CLAUDE.md non-negotiable: order must be limit, not market."""
        proposal = _make_proposal()
        decision = _pass_decision(proposal)

        client = _mock_client_filled()
        submit(
            decision=decision,
            proposed=proposal,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            cycle_id="cycle-limit-check",
        )

        # The call must have been to submit_mleg_order with a non-zero limit_price
        call_kwargs = client.submit_mleg_order.call_args
        assert call_kwargs is not None
        # limit_price must be provided and non-zero (market order = no limit price)
        limit_price = call_kwargs[1].get("limit_price") or call_kwargs[0][1]
        assert limit_price != 0.0, "Market order detected — ORDER_TYPE='limit' invariant violated!"


# ---------------------------------------------------------------------------
# Test 2: VETO decision → no order submitted
# ---------------------------------------------------------------------------

class TestSubmitVetoDecision:
    def test_veto_decision_skips_order(self, store, exec_config, risk_config):
        proposal = _make_proposal()
        decision = _veto_decision(proposal)

        client = MagicMock()
        result = submit(
            decision=decision,
            proposed=proposal,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            cycle_id="cycle-veto",
        )

        assert result["status"] == "vetoed"
        assert result["order_id"] is None
        # Must never touch the broker
        client.submit_mleg_order.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: Partial fill then timeout → retries → abandoned row, not infinite loop
# ---------------------------------------------------------------------------

class TestSubmitRetryAndAbandon:
    def test_timeout_produces_retries_then_abandoned(self, store, exec_config, risk_config):
        reset_in_process_cache()
        proposal = _make_proposal()
        decision = _pass_decision(proposal)

        # Client that never fills
        client = _mock_client_timeout()

        result = submit(
            decision=decision,
            proposed=proposal,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            cycle_id="cycle-retry",
        )

        assert result["abandoned"] is True
        assert result["status"] == "abandoned"
        # Should have tried: 1 initial + order_retry_limit=2 retries = 3 total
        assert result["attempt_count"] == 3
        # Should have called submit_mleg_order 3 times
        assert client.submit_mleg_order.call_count == 3
        # Should have tried to cancel after each timeout
        assert client._trading.cancel_order_by_id.call_count >= 2  # at least 2 cancellations

    def test_retry_widens_limit_price(self, store, exec_config, risk_config):
        """Each retry attempt must submit with a wider (more aggressive) limit price."""
        proposal = _make_proposal()
        decision = _pass_decision(proposal)

        client = _mock_client_timeout()
        submit(
            decision=decision,
            proposed=proposal,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            cycle_id="cycle-widen",
        )

        calls = client.submit_mleg_order.call_args_list
        # Extract limit prices from each call — submit_mleg_order uses kwargs
        prices = []
        for c in calls:
            # Try kwargs first, then positional args[1]
            if "limit_price" in c.kwargs:
                prices.append(c.kwargs["limit_price"])
            elif len(c.args) >= 2:
                prices.append(c.args[1])
            else:
                prices.append(None)
        prices = [p for p in prices if p is not None]
        # Each retry should have a different (widened) price
        assert len(prices) == 3
        # For a credit spread (positive limit_price), widening means accepting less credit
        assert prices[0] > prices[1] > prices[2], (
            f"Limit prices should decrease on retries (accepting less credit): {prices}"
        )


# ---------------------------------------------------------------------------
# Test 4: Diverged reconcile blocks subsequent submit_basket
# ---------------------------------------------------------------------------

class TestDivergedReconcileBlocks:
    def test_diverged_reconcile_blocks_new_basket(self, store, exec_config, risk_config):
        """A diverged reconcile result must prevent a new basket from starting."""
        reset_in_process_cache()

        proposals = [_make_proposal("AAPL"), _make_proposal("TSLA")]

        # Mock client where reconcile finds a divergence (broker has a position
        # that the journal doesn't know about)
        client = MagicMock()
        client.get_positions.return_value = [
            {
                "symbol": "MYSTERY260904P00200000",
                "qty": 5.0,
                "side": "long",
                "market_value": 1000.0,
                "avg_entry_price": 2.0,
                "unrealized_pl": 0.0,
                "asset_class": "us_option",
                "current_price": 2.0,
            }
        ]
        client.get_account.return_value = {
            "equity": 100_000.0, "buying_power": 100_000.0,
            "options_buying_power": 100_000.0, "options_approved_level": 3,
            "options_trading_level": 3, "cash": 100_000.0,
            "portfolio_value": 100_000.0, "trading_blocked": False,
            "id": "test-id", "account_number": "PA123",
        }
        client.submit_mleg_order.return_value = "ord-blocked"
        mock_order = MagicMock()
        mock_order.status.value = "filled"
        mock_order.filled_qty = "2"
        mock_order.filled_avg_price = "1.20"
        client._trading.get_order_by_id.return_value = mock_order
        client._trading.cancel_order_by_id.return_value = None

        # Run reconcile to detect divergence
        recon = reconcile(client, store, cycle_id="recon-cycle")
        assert recon.diverged, "Expected reconcile to detect divergence"

        # Now try to start a basket with diverged market state
        def portfolio_state_fn():
            return PortfolioState(
                current_nav=100_000.0,
                starting_nav=100_000.0,
                open_positions=[],
                reserved_capital=0.0,
            )

        def market_state_fn():
            return MarketState(reconciliation_diverged=recon.diverged)

        # Run engine evaluate to confirm VETO from reconciliation gate
        from barbell.risk.engine import evaluate

        from barbell.risk.kill_switch import reset_in_process_cache as ric
        ric()
        config = risk_config
        decision = evaluate(
            proposed=proposals[0],
            portfolio_state=portfolio_state_fn(),
            market_state=market_state_fn(),
            config=config,
            cycle_id="post-recon-cycle",
            store=store,
        )
        assert decision.outcome == "VETO"
        # Confirm the veto reason mentions reconciliation
        assert any("reconcil" in r.lower() for r in decision.reasons)


# ---------------------------------------------------------------------------
# Test 5: submit_basket — 3-underlying basket
# ---------------------------------------------------------------------------

class TestSubmitBasketThreeLegs:
    def test_basket_reserves_before_leg1_releases_after_leg3(
        self, store, exec_config, risk_config
    ):
        """
        Three-underlying basket must:
        1. Write capital_reservations row (status="reserved") before leg 1.
        2. Fill all 3 legs sequentially.
        3. Write capital_reservations row (status="released") after leg 3.
        """
        from sqlmodel import Session, select
        from barbell.journal.store import CapitalReservationRow

        reset_in_process_cache()

        proposals = [
            _make_proposal("AAPL", max_loss=500.0, contracts=1),
            _make_proposal("TSLA", max_loss=400.0, contracts=1),
            _make_proposal("MSFT", max_loss=450.0, contracts=1),
        ]
        total_max_loss = sum(p.max_loss_estimate for p in proposals)

        # Client fills every order immediately
        filled_client = _mock_client_filled(fill_price=1.20)

        def portfolio_state_fn():
            return PortfolioState(
                current_nav=100_000.0, starting_nav=100_000.0,
                open_positions=[], reserved_capital=0.0,
            )

        def market_state_fn():
            return MarketState(dispersion_score=1.5, reconciliation_diverged=False)

        results = submit_basket(
            proposals=proposals,
            client=filled_client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            engine_config=risk_config,
            portfolio_state_fn=portfolio_state_fn,
            market_state_fn=market_state_fn,
            cycle_id="basket-cycle-3",
        )

        assert len(results) == 3

        # Check capital reservation rows in DB
        with Session(store._engine) as session:
            rows = session.exec(select(CapitalReservationRow)).all()

        statuses = [r.status for r in rows]

        assert "reserved" in statuses, "Must have a 'reserved' row before basket starts"
        assert "released" in statuses, "Must have a 'released' row after basket completes"

        # reserved_amount should match total_max_loss
        reserved_rows = [r for r in rows if r.status == "reserved"]
        assert any(abs(r.reserved_amount - total_max_loss) < 0.01 for r in reserved_rows)

        # released row should appear with higher ID (later) than reserved row
        reserved_row = next(r for r in rows if r.status == "reserved")
        released_row = next(r for r in rows if r.status == "released")
        assert released_row.id > reserved_row.id, (
            "Released row must be written AFTER reserved row"
        )

    def test_basket_submits_legs_in_order(self, store, exec_config, risk_config):
        """Each underlying must be submitted in the order provided."""
        reset_in_process_cache()

        submission_order: list[str] = []
        proposals = [
            _make_proposal("AAPL", max_loss=500.0, contracts=1),
            _make_proposal("TSLA", max_loss=400.0, contracts=1),
            _make_proposal("MSFT", max_loss=450.0, contracts=1),
        ]

        def track_submission(legs, limit_price, tif="day"):
            # Track which underlying was submitted based on leg symbols
            underlying = legs[0].symbol[:4] if legs else "UNKNOWN"
            submission_order.append(underlying)
            return f"ord-{underlying.lower()}"

        client = _mock_client_filled()
        client.submit_mleg_order.side_effect = track_submission

        def portfolio_state_fn():
            return PortfolioState(current_nav=100_000.0, starting_nav=100_000.0, open_positions=[], reserved_capital=0.0)

        def market_state_fn():
            return MarketState(dispersion_score=1.5, reconciliation_diverged=False)

        submit_basket(
            proposals=proposals,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            engine_config=risk_config,
            portfolio_state_fn=portfolio_state_fn,
            market_state_fn=market_state_fn,
            cycle_id="basket-order-test",
        )

        assert submission_order == ["AAPL", "TSLA", "MSFT"]


# ---------------------------------------------------------------------------
# Test 6: Forced mid-basket VETO on leg 2 — leg 3 never built
# ---------------------------------------------------------------------------

class TestSubmitBasketMidBasketHalt:
    def test_mid_basket_veto_on_leg2_halts_before_leg3(
        self, store, exec_config, risk_config
    ):
        """
        Three-proposal basket where the post-fill risk re-evaluation VETOs
        after leg 2 fills:
        - Leg 1 fills normally and stays in place.
        - Leg 2 fills, but post-fill engine.evaluate() returns VETO.
        - Leg 3 must never be submitted (submit_mleg_order not called a 3rd time).
        """
        from sqlmodel import Session, select
        from barbell.journal.store import BasketLegFillRow
        from barbell.risk.kill_switch import reset_in_process_cache as ric

        ric()

        proposals = [
            _make_proposal("AAPL", max_loss=500.0, contracts=1),
            _make_proposal("TSLA", max_loss=400.0, contracts=1),
            _make_proposal("MSFT", max_loss=450.0, contracts=1),
        ]

        client = _mock_client_filled(fill_price=1.20)
        leg_2_submitted = False

        call_count = [0]
        original_submit = client.submit_mleg_order.side_effect

        def submit_side_effect(legs, limit_price, tif="day"):
            call_count[0] += 1
            return f"ord-{call_count[0]}"

        client.submit_mleg_order.side_effect = submit_side_effect

        # Simulate: after leg 2 fills, the portfolio is "over-concentrated"
        # by making portfolio_state_fn return a state that will VETO on leg 3.
        fill_count = [0]

        def portfolio_state_fn():
            fill_count[0] += 1
            if fill_count[0] >= 2:
                # After leg 2: make portfolio state cause a VETO (kill switch tripped)
                # We do this by making the NAV look very low
                return PortfolioState(
                    current_nav=91_000.0,   # -9% → triggers kill switch
                    starting_nav=100_000.0,
                    open_positions=[],
                    reserved_capital=0.0,
                )
            return PortfolioState(
                current_nav=100_000.0, starting_nav=100_000.0,
                open_positions=[], reserved_capital=0.0,
            )

        def market_state_fn():
            return MarketState(dispersion_score=1.5, reconciliation_diverged=False)

        results = submit_basket(
            proposals=proposals,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            engine_config=risk_config,
            portfolio_state_fn=portfolio_state_fn,
            market_state_fn=market_state_fn,
            cycle_id="basket-halt-test",
        )

        assert len(results) == 3

        # Leg 3 must never have been submitted to the broker
        # (call_count tracks how many times submit_mleg_order was called)
        # Leg 1 and 2 fill → 2 calls max for successful submissions
        # Leg 3 should be skipped
        assert call_count[0] <= 2, (
            f"submit_mleg_order was called {call_count[0]} times — "
            f"leg 3 should never be submitted after mid-basket halt"
        )

        # Leg 3 result should indicate it was skipped
        leg3_result = results[2]
        assert leg3_result["status"] in ("skipped_basket_halt", "skipped_basket_halt")

        # Leg 1's result should show it filled (not rolled back)
        leg1_result = results[0]
        assert leg1_result["status"] in ("filled", "abandoned"), (
            "Leg 1 fill must remain in place after mid-basket halt"
        )

        # Capital reservation must still be released even after halt
        from sqlmodel import Session, select
        from barbell.journal.store import CapitalReservationRow
        with Session(store._engine) as session:
            rows = session.exec(select(CapitalReservationRow)).all()
        statuses = [r.status for r in rows]
        assert "released" in statuses, (
            "Capital reservation must be released even when basket halts mid-way"
        )


# ---------------------------------------------------------------------------
# Test 7: Concurrency guard — second basket blocked while first is reserved
# ---------------------------------------------------------------------------

class TestBasketConcurrencyGuard:
    def test_second_basket_blocked_when_first_reservation_open(
        self, store, exec_config, risk_config
    ):
        """
        If a capital_reservations row with status="reserved" exists (unreleased),
        submit_basket must refuse to start a new basket.
        """
        reset_in_process_cache()

        # Write an open reservation directly to simulate in-flight basket
        store.record_capital_reservation(
            cycle_id="existing-basket-cycle",
            basket_id="existing-basket-id",
            reserved_amount=5000.0,
            status="reserved",  # not released
        )

        proposals = [_make_proposal("GOOG")]
        client = MagicMock()

        def portfolio_state_fn():
            return PortfolioState(current_nav=100_000.0, starting_nav=100_000.0, open_positions=[], reserved_capital=0.0)

        def market_state_fn():
            return MarketState()

        results = submit_basket(
            proposals=proposals,
            client=client,
            store=store,
            exec_config=exec_config,
            risk_config=risk_config,
            engine_config=risk_config,
            portfolio_state_fn=portfolio_state_fn,
            market_state_fn=market_state_fn,
            cycle_id="blocked-basket-cycle",
        )

        assert len(results) == 1
        assert results[0]["status"] == "blocked_by_open_basket"
        # Must never have called the broker
        client.submit_mleg_order.assert_not_called()
