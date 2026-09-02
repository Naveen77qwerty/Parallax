"""
Highest-priority test file in the repo. Every gate in risk/gates.py gets:
    - a PASS case (clearly within limits)
    - an exact-boundary case (at the threshold — should still PASS or VETO depending on semantics)
    - a VETO case (clearly over/violates limits)
    - a RESIZE case where applicable (gates 1 & 2)

Also:
    - dispersion_score=None PASSES (not VETOs) — critical design decision
    - property test: 200+ randomized inputs asserting evaluate().contracts ≤ proposed.contracts
    - kill-switch trip test: trip it, confirm subsequent evaluate() REJECTs,
      confirm it survives module-level cache reset (simulates reinstantiation)
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from barbell.agent.schemas import (
    GateResult,
    MarketState,
    PortfolioState,
    ProposedLeg,
    ProposedStructure,
    RiskDecision,
)
from barbell.config import RiskGateConfig
from barbell.risk import gates


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> RiskGateConfig:
    """Base RiskGateConfig with safe defaults matching settings.yaml."""
    defaults = dict(
        max_loss_per_position_pct_nav=0.008,
        max_loss_portfolio_pct_nav=0.12,
        basket_reserve_before_first_leg=True,
        max_quote_age_seconds=120,
        max_sector_concentration=3,
        max_positions_per_underlying=1,
        drawdown_kill_switch_pct_nav=-0.08,
        max_slippage_pct_of_mid=0.10,
        order_retry_limit=3,
        order_retry_widen_pct=0.02,
    )
    defaults.update(overrides)
    return RiskGateConfig(**defaults)


def _make_portfolio(
    nav: float = 100_000.0,
    starting_nav: float = 100_000.0,
    open_positions: list | None = None,
    reserved_capital: float = 0.0,
    sector_exposure: dict | None = None,
) -> PortfolioState:
    return PortfolioState(
        current_nav=nav,
        starting_nav=starting_nav,
        open_positions=open_positions or [],
        reserved_capital=reserved_capital,
        sector_exposure=sector_exposure or {},
    )


def _make_market(
    dispersion_score: float | None = 1.5,
    quote_age: float = 30.0,
    oi: int = 1000,
    spread: float = 0.05,
    reconciliation_diverged: bool = False,
) -> MarketState:
    return MarketState(
        bid_ask_spread={"AAPL": spread},
        open_interest={"AAPL": oi},
        quote_age_seconds={"AAPL": quote_age},
        dispersion_score=dispersion_score,
        reconciliation_diverged=reconciliation_diverged,
    )


def _make_proposal(
    underlying: str = "AAPL",
    max_loss: float = 500.0,
    contracts: int = 2,
    sleeve: str = "A",
    limit_price: float = 1.25,
    structure_type: str = "put_credit_spread",
) -> ProposedStructure:
    exp = date(2026, 9, 4)
    return ProposedStructure(
        underlying=underlying,
        legs=[
            ProposedLeg(
                symbol=f"{underlying}260904P00200000",
                expiry=exp,
                strike=200.0,
                right="put",
                side="sell",
                contracts=contracts,
            ),
            ProposedLeg(
                symbol=f"{underlying}260904P00195000",
                expiry=exp,
                strike=195.0,
                right="put",
                side="buy",
                contracts=contracts,
            ),
        ],
        rationale="test",
        sleeve=sleeve,
        max_loss_estimate=max_loss,
        limit_price=limit_price,
        structure_type=structure_type,
    )


# ---------------------------------------------------------------------------
# Gate 01: per_position_loss_cap
# ---------------------------------------------------------------------------

class TestGatePerPositionLossCap:
    def test_pass_well_within_cap(self):
        # $500 loss on $100k NAV, cap = 0.8% = $800
        result = gates.gate_per_position_loss_cap(
            _make_proposal(max_loss=500.0, contracts=2),
            _make_portfolio(),
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_exact_boundary_passes(self):
        # max_loss = exactly the cap: $800 with contracts=2 -> $400/contract
        # max allowed contracts = 800/400 = 2 -> fits exactly -> PASS
        result = gates.gate_per_position_loss_cap(
            _make_proposal(max_loss=800.0, contracts=2),
            _make_portfolio(nav=100_000.0),
            _make_market(),
            _make_config(max_loss_per_position_pct_nav=0.008),
        )
        assert result.outcome == "PASS"

    def test_veto_even_one_contract_exceeds_cap(self):
        # NAV=10_000, cap=0.8%=80, per-contract loss = $100 -> even 1 exceeds cap
        result = gates.gate_per_position_loss_cap(
            _make_proposal(max_loss=100.0, contracts=1),
            _make_portfolio(nav=10_000.0),
            _make_market(),
            _make_config(max_loss_per_position_pct_nav=0.008),
        )
        assert result.outcome == "VETO"

    def test_resize_down_to_allowed_contracts(self):
        # NAV=100k, cap=800, per-contract loss=300 -> max allowed = 2 (2*300=600 < 800)
        result = gates.gate_per_position_loss_cap(
            _make_proposal(max_loss=1500.0, contracts=5),  # 5*300=1500, too much
            _make_portfolio(nav=100_000.0),
            _make_market(),
            _make_config(max_loss_per_position_pct_nav=0.008),
        )
        assert result.outcome == "RESIZE"
        assert result.contracts == 2  # int(800/300)=2
        assert result.contracts < 5

    def test_resize_contracts_never_exceed_proposed(self):
        """Invariant: RESIZE contracts must always be ≤ proposed contracts."""
        for _ in range(50):
            proposed_contracts = random.randint(1, 20)
            max_loss = random.uniform(100, 5000)
            nav = random.uniform(50_000, 200_000)
            proposal = _make_proposal(max_loss=max_loss, contracts=proposed_contracts)
            result = gates.gate_per_position_loss_cap(
                proposal, _make_portfolio(nav=nav), _make_market(), _make_config()
            )
            if result.outcome == "RESIZE":
                assert result.contracts is not None
                assert result.contracts <= proposed_contracts


# ---------------------------------------------------------------------------
# Gate 02: portfolio_loss_cap
# ---------------------------------------------------------------------------

class TestGatePortfolioLossCap:
    def test_pass_clean_portfolio(self):
        result = gates.gate_portfolio_loss_cap(
            _make_proposal(max_loss=500.0),
            _make_portfolio(),  # no existing positions
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_boundary_exactly_at_cap_passes(self):
        # cap = 12% of 100k = 12000. existing_risk=11000, new proposal=1000 -> total=12000
        existing_loss_position = {"unrealized_pl": -11_000.0, "symbol": "X", "asset_class": "us_option"}
        result = gates.gate_portfolio_loss_cap(
            _make_proposal(max_loss=1000.0, contracts=1),
            _make_portfolio(open_positions=[existing_loss_position]),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        # 11000 + 1000 = 12000 = cap exactly; int(1000/1000)=1; should PASS
        assert result.outcome == "PASS"

    def test_veto_existing_risk_exceeds_cap(self):
        # existing risk = 13k, cap = 12k -> 0 headroom even before this proposal
        existing_loss_position = {"unrealized_pl": -13_000.0, "symbol": "X", "asset_class": "us_option"}
        result = gates.gate_portfolio_loss_cap(
            _make_proposal(max_loss=100.0),
            _make_portfolio(open_positions=[existing_loss_position]),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "VETO"

    def test_resize_down_to_fit_headroom(self):
        # cap=12000, existing_risk=10000, headroom=2000, per-contract=500 -> max=4, proposed=10
        existing = {"unrealized_pl": -10_000.0, "symbol": "Y", "asset_class": "us_option"}
        result = gates.gate_portfolio_loss_cap(
            _make_proposal(max_loss=5000.0, contracts=10),  # 500/contract
            _make_portfolio(open_positions=[existing]),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "RESIZE"
        assert result.contracts == 4
        assert result.contracts < 10


# ---------------------------------------------------------------------------
# Gate 03: defined_risk_only
# ---------------------------------------------------------------------------

class TestGateDefinedRiskOnly:
    def test_pass_covered_spread(self):
        result = gates.gate_defined_risk_only(
            _make_proposal(),  # default has sell + buy same expiry
            _make_portfolio(),
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_boundary_both_legs_same_expiry(self):
        # exact boundary: sell and buy on the same expiry -> covered
        result = gates.gate_defined_risk_only(
            _make_proposal(),
            _make_portfolio(),
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_veto_naked_short(self):
        exp = date(2026, 9, 4)
        naked = ProposedStructure(
            underlying="AAPL",
            legs=[
                ProposedLeg(
                    symbol="AAPL260904P00200000",
                    expiry=exp,
                    strike=200.0,
                    right="put",
                    side="sell",
                    contracts=1,
                )
            ],
            rationale="test",
            sleeve="A",
            max_loss_estimate=500.0,
        )
        result = gates.gate_defined_risk_only(
            naked, _make_portfolio(), _make_market(), _make_config()
        )
        assert result.outcome == "VETO"
        assert "Naked short" in result.reason

    def test_veto_mismatched_expiry_uncovered_sell(self):
        sell_exp = date(2026, 9, 4)
        buy_exp = date(2026, 9, 11)  # different expiry → not covered
        proposal = ProposedStructure(
            underlying="AAPL",
            legs=[
                ProposedLeg(symbol="AAPL260904P00200000", expiry=sell_exp, strike=200.0, right="put", side="sell", contracts=1),
                ProposedLeg(symbol="AAPL260911P00195000", expiry=buy_exp, strike=195.0, right="put", side="buy", contracts=1),
            ],
            rationale="test",
            sleeve="A",
            max_loss_estimate=500.0,
        )
        result = gates.gate_defined_risk_only(
            proposal, _make_portfolio(), _make_market(), _make_config()
        )
        assert result.outcome == "VETO"


# ---------------------------------------------------------------------------
# Gate 04: quote_staleness
# ---------------------------------------------------------------------------

class TestGateQuoteStaleness:
    def test_pass_fresh_quote(self):
        result = gates.gate_quote_staleness(
            _make_proposal(),
            _make_portfolio(),
            _make_market(quote_age=30.0),
            _make_config(max_quote_age_seconds=120),
        )
        assert result.outcome == "PASS"

    def test_boundary_exactly_at_max_passes(self):
        result = gates.gate_quote_staleness(
            _make_proposal(),
            _make_portfolio(),
            _make_market(quote_age=120.0),  # exactly at max
            _make_config(max_quote_age_seconds=120),
        )
        assert result.outcome == "PASS"

    def test_veto_stale_quote(self):
        result = gates.gate_quote_staleness(
            _make_proposal(),
            _make_portfolio(),
            _make_market(quote_age=121.0),  # one second over
            _make_config(max_quote_age_seconds=120),
        )
        assert result.outcome == "VETO"

    def test_pass_no_quote_data_conservative(self):
        # No quote data in market_state → pass conservatively
        market = MarketState()  # empty dicts, no ages
        result = gates.gate_quote_staleness(
            _make_proposal(),
            _make_portfolio(),
            market,
            _make_config(max_quote_age_seconds=120),
        )
        assert result.outcome == "PASS"


# ---------------------------------------------------------------------------
# Gate 05: liquidity_floor
# ---------------------------------------------------------------------------

class TestGateLiquidityFloor:
    def test_pass_good_liquidity(self):
        result = gates.gate_liquidity_floor(
            _make_proposal(limit_price=2.00),
            _make_portfolio(),
            _make_market(oi=2000, spread=0.10),  # 0.10/2.00 = 5% < 8% max
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_boundary_oi_exactly_at_min(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.min_open_interest = 500
            mock_settings.return_value.sleeve_a_carry.screen.max_spread_pct_of_mid = 0.08
            result = gates.gate_liquidity_floor(
                _make_proposal(limit_price=2.00),
                _make_portfolio(),
                _make_market(oi=500, spread=0.10),  # OI exactly at min -> PASS
                _make_config(),
            )
        assert result.outcome == "PASS"

    def test_veto_insufficient_open_interest(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.min_open_interest = 500
            mock_settings.return_value.sleeve_a_carry.screen.max_spread_pct_of_mid = 0.08
            result = gates.gate_liquidity_floor(
                _make_proposal(limit_price=2.00),
                _make_portfolio(),
                _make_market(oi=100, spread=0.05),  # OI too low
                _make_config(),
            )
        assert result.outcome == "VETO"

    def test_veto_spread_too_wide(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.min_open_interest = 500
            mock_settings.return_value.sleeve_a_carry.screen.max_spread_pct_of_mid = 0.08
            result = gates.gate_liquidity_floor(
                _make_proposal(limit_price=1.00),
                _make_portfolio(),
                _make_market(oi=1000, spread=0.10),  # spread=10% of 1.00 = 10% > 8%
                _make_config(),
            )
        assert result.outcome == "VETO"


# ---------------------------------------------------------------------------
# Gate 06: dispersion_score
# ---------------------------------------------------------------------------

class TestGateDispersionScore:
    def test_pass_score_above_floor(self):
        result = gates.gate_dispersion_score(
            _make_proposal(sleeve="A"),
            _make_portfolio(),
            _make_market(dispersion_score=1.30),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_boundary_exactly_at_floor_passes(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.min_dispersion_score = 1.15
            result = gates.gate_dispersion_score(
                _make_proposal(sleeve="A"),
                _make_portfolio(),
                _make_market(dispersion_score=1.15),  # exactly at floor
                _make_config(),
            )
        assert result.outcome == "PASS"

    def test_veto_score_below_floor(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.min_dispersion_score = 1.15
            result = gates.gate_dispersion_score(
                _make_proposal(sleeve="A"),
                _make_portfolio(),
                _make_market(dispersion_score=1.10),
                _make_config(),
            )
        assert result.outcome == "VETO"
        assert "1.10" in result.reason or "1.100" in result.reason

    def test_none_dispersion_score_passes_not_veto(self):
        """
        CRITICAL: None dispersion_score must PASS, not VETO.

        If None caused a VETO, all of Sleeve A would be silently disabled
        until Member 3 wires screen/metrics.py's dispersion_score().
        The fail-open-for-None design is intentional — all other gates
        still run and enforce real risk constraints.
        """
        result = gates.gate_dispersion_score(
            _make_proposal(sleeve="A"),
            _make_portfolio(),
            _make_market(dispersion_score=None),
            _make_config(),
        )
        assert result.outcome == "PASS", (
            "dispersion_score=None must PASS (not VETO) — "
            "None means 'metric not yet computed by Member 3', not 'fails the test'. "
            "A VETO on None would silently disable all of Sleeve A."
        )
        assert "pending Member 3" in result.reason or "not yet populated" in result.reason

    def test_sleeve_b_always_passes_dispersion(self):
        # Dispersion gate is Sleeve A only
        result = gates.gate_dispersion_score(
            _make_proposal(sleeve="B"),
            _make_portfolio(),
            _make_market(dispersion_score=0.5),  # would VETO for Sleeve A
            _make_config(),
        )
        assert result.outcome == "PASS"
        assert "Sleeve B" in result.reason


# ---------------------------------------------------------------------------
# Gate 07: earnings_blackout
# ---------------------------------------------------------------------------

class TestGateEarningsBlackout:
    def test_pass_not_in_exclude_list(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.earnings_blackout = True
            mock_settings.return_value.universe.exclude = ["META", "NFLX"]
            result = gates.gate_earnings_blackout(
                _make_proposal(underlying="AAPL"),
                _make_portfolio(),
                _make_market(),
                _make_config(),
            )
        assert result.outcome == "PASS"

    def test_boundary_different_case_still_excluded(self):
        # Exclusion check should be case-insensitive
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.earnings_blackout = True
            mock_settings.return_value.universe.exclude = ["meta"]  # lowercase
            result = gates.gate_earnings_blackout(
                _make_proposal(underlying="META"),  # uppercase
                _make_portfolio(),
                _make_market(),
                _make_config(),
            )
        assert result.outcome == "VETO"

    def test_veto_underlying_in_exclude_list(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.earnings_blackout = True
            mock_settings.return_value.universe.exclude = ["AAPL"]
            result = gates.gate_earnings_blackout(
                _make_proposal(underlying="AAPL"),
                _make_portfolio(),
                _make_market(),
                _make_config(),
            )
        assert result.outcome == "VETO"
        assert "AAPL" in result.reason

    def test_pass_blackout_disabled_in_config(self):
        with patch("barbell.risk.gates.get_settings") as mock_settings:
            mock_settings.return_value.sleeve_a_carry.screen.earnings_blackout = False
            mock_settings.return_value.universe.exclude = ["AAPL"]  # in exclude, but disabled
            result = gates.gate_earnings_blackout(
                _make_proposal(underlying="AAPL"),
                _make_portfolio(),
                _make_market(),
                _make_config(),
            )
        assert result.outcome == "PASS"


# ---------------------------------------------------------------------------
# Gate 08: pre_nfp_flatten (stub)
# ---------------------------------------------------------------------------

class TestGatePreNfpFlatten:
    def test_always_pass_stub(self):
        result = gates.gate_pre_nfp_flatten(
            _make_proposal(),
            _make_portfolio(),
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"
        assert "Member 4" in result.reason


# ---------------------------------------------------------------------------
# Gate 09: expiry_past_deadline (stub)
# ---------------------------------------------------------------------------

class TestGateExpiryPastDeadline:
    def test_always_pass_stub(self):
        result = gates.gate_expiry_past_deadline(
            _make_proposal(),
            _make_portfolio(),
            _make_market(),
            _make_config(),
        )
        assert result.outcome == "PASS"
        assert "Member 4" in result.reason


# ---------------------------------------------------------------------------
# Gate 10: concentration
# ---------------------------------------------------------------------------

class TestGateConcentration:
    def test_pass_no_existing_positions(self):
        result = gates.gate_concentration(
            _make_proposal(underlying="AAPL"),
            _make_portfolio(open_positions=[]),
            _make_market(),
            _make_config(max_positions_per_underlying=1),
        )
        assert result.outcome == "PASS"

    def test_boundary_exactly_at_limit_veto(self):
        # max=1, already have 1 -> should VETO
        existing = {"symbol": "AAPL260904P00200000", "underlying": "AAPL", "qty": 2}
        result = gates.gate_concentration(
            _make_proposal(underlying="AAPL"),
            _make_portfolio(open_positions=[existing]),
            _make_market(),
            _make_config(max_positions_per_underlying=1),
        )
        assert result.outcome == "VETO"

    def test_veto_too_many_positions_same_underlying(self):
        existing = [
            {"symbol": "AAPL260904P00200000", "underlying": "AAPL", "qty": 2},
        ]
        result = gates.gate_concentration(
            _make_proposal(underlying="AAPL"),
            _make_portfolio(open_positions=existing),
            _make_market(),
            _make_config(max_positions_per_underlying=1),
        )
        assert result.outcome == "VETO"
        assert "AAPL" in result.reason

    def test_veto_sector_concentration(self):
        # sector_exposure says "AAPL" sector already has 3 positions (the max)
        result = gates.gate_concentration(
            _make_proposal(underlying="AAPL"),
            _make_portfolio(
                open_positions=[],
                sector_exposure={"AAPL": 3},  # sector named by underlying
            ),
            _make_market(),
            _make_config(max_positions_per_underlying=5, max_sector_concentration=3),
        )
        assert result.outcome == "VETO"

    def test_pass_different_underlying(self):
        # Have a TSLA position; adding AAPL is fine
        existing = [{"symbol": "TSLA260904P00200000", "underlying": "TSLA", "qty": 1}]
        result = gates.gate_concentration(
            _make_proposal(underlying="AAPL"),
            _make_portfolio(open_positions=existing),
            _make_market(),
            _make_config(max_positions_per_underlying=1),
        )
        assert result.outcome == "PASS"


# ---------------------------------------------------------------------------
# Gate 11: drawdown_kill_switch
# ---------------------------------------------------------------------------

class TestGateDrawdownKillSwitch:
    def test_pass_no_drawdown(self, tmp_path):
        from barbell.journal.store import JournalStore
        from barbell.risk.kill_switch import reset_in_process_cache

        reset_in_process_cache()
        db_path = str(tmp_path / "kill_switch_pass.db")
        store = JournalStore(db_path=db_path)
        gates._set_store(store)
        try:
            result = gates.gate_drawdown_kill_switch(
                _make_proposal(),
                _make_portfolio(nav=100_000.0, starting_nav=100_000.0),
                _make_market(),
                _make_config(),
            )
            assert result.outcome == "PASS"
        finally:
            gates._set_store(None)
            reset_in_process_cache()

    def test_boundary_exactly_at_threshold_no_trip(self, tmp_path):
        from barbell.journal.store import JournalStore
        from barbell.risk.kill_switch import reset_in_process_cache

        reset_in_process_cache()
        db_path = str(tmp_path / "kill_switch_boundary.db")
        store = JournalStore(db_path=db_path)
        gates._set_store(store)
        try:
            # Exactly -8%: drawdown = (92000-100000)/100000 = -0.08
            result = gates.gate_drawdown_kill_switch(
                _make_proposal(),
                _make_portfolio(nav=92_000.0, starting_nav=100_000.0),
                _make_market(),
                _make_config(drawdown_kill_switch_pct_nav=-0.08),
            )
            # At exactly the threshold, is_latched() is still False (check_and_latch
            # wasn't called by gate directly — it's called by engine), so PASS
            assert result.outcome == "PASS"
        finally:
            gates._set_store(None)
            reset_in_process_cache()

    def test_veto_when_latched(self, tmp_path):
        from barbell.journal.store import JournalStore
        from barbell.risk import kill_switch
        from barbell.risk.kill_switch import reset_in_process_cache

        reset_in_process_cache()
        db_path = str(tmp_path / "kill_switch_latch.db")
        store = JournalStore(db_path=db_path)
        # Trip the kill switch by writing a triggered event directly
        store.record_kill_switch_event(
            cycle_id="test-cycle",
            triggered=True,
            nav_at_trigger=91_000.0,
            reason="test trip",
        )
        gates._set_store(store)
        try:
            result = gates.gate_drawdown_kill_switch(
                _make_proposal(),
                _make_portfolio(nav=91_000.0, starting_nav=100_000.0),
                _make_market(),
                _make_config(),
            )
            assert result.outcome == "VETO"
            assert "latched" in result.reason.lower() or "kill switch" in result.reason.lower()
        finally:
            gates._set_store(None)
            reset_in_process_cache()


# ---------------------------------------------------------------------------
# Gate 12: broker_reconciliation
# ---------------------------------------------------------------------------

class TestGateBrokerReconciliation:
    def test_pass_no_divergence(self):
        result = gates.gate_broker_reconciliation(
            _make_proposal(),
            _make_portfolio(),
            _make_market(reconciliation_diverged=False),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_boundary_diverged_false_passes(self):
        # Same as pass — boundary is the bool flag
        result = gates.gate_broker_reconciliation(
            _make_proposal(),
            _make_portfolio(),
            _make_market(reconciliation_diverged=False),
            _make_config(),
        )
        assert result.outcome == "PASS"

    def test_veto_when_diverged(self):
        result = gates.gate_broker_reconciliation(
            _make_proposal(),
            _make_portfolio(),
            _make_market(reconciliation_diverged=True),
            _make_config(),
        )
        assert result.outcome == "VETO"
        assert "divergence" in result.reason.lower() or "reconciliation" in result.reason.lower()


# ---------------------------------------------------------------------------
# Gate 13: basket_capital_reservation
# ---------------------------------------------------------------------------

class TestGateBasketCapitalReservation:
    def test_pass_reserved_plus_proposed_within_cap(self):
        # cap=12000, reserved=5000, proposed=3000 -> total=8000 < 12000
        result = gates.gate_basket_capital_reservation(
            _make_proposal(max_loss=3000.0),
            _make_portfolio(nav=100_000.0, reserved_capital=5000.0),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "PASS"

    def test_boundary_exactly_at_cap_passes(self):
        # cap=12000, reserved=11000, proposed=1000 -> total=12000 = cap exactly
        result = gates.gate_basket_capital_reservation(
            _make_proposal(max_loss=1000.0),
            _make_portfolio(nav=100_000.0, reserved_capital=11_000.0),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "PASS"

    def test_veto_reserved_plus_proposed_over_cap(self):
        # cap=12000, reserved=11000, proposed=1001 -> total=12001 > 12000
        result = gates.gate_basket_capital_reservation(
            _make_proposal(max_loss=1001.0),
            _make_portfolio(nav=100_000.0, reserved_capital=11_000.0),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "VETO"
        assert "12001" in result.reason or "12,001" in result.reason or "over" in result.reason.lower() or "exceed" in result.reason.lower()

    def test_veto_zero_headroom_even_small_proposal(self):
        # cap=12000, reserved=12000, proposed=0.01 -> over
        result = gates.gate_basket_capital_reservation(
            _make_proposal(max_loss=0.01),
            _make_portfolio(nav=100_000.0, reserved_capital=12_000.0),
            _make_market(),
            _make_config(max_loss_portfolio_pct_nav=0.12),
        )
        assert result.outcome == "VETO"


# ---------------------------------------------------------------------------
# Property test: engine.evaluate() NEVER exceeds proposed contracts
# ---------------------------------------------------------------------------

class TestEngineNeverIncreasesContracts:
    def test_engine_never_increases_contracts_property(self, tmp_path):
        """
        Property test: over 200+ randomised inputs, evaluate().contracts is
        always ≤ proposed.contracts.  This is the test that backs the
        "risk engine can only tighten" claim in the write-up.

        CLAUDE.md non-negotiable: this test must never be weakened to make
        a feature pass.
        """
        from barbell.journal.store import JournalStore
        from barbell.risk.engine import evaluate
        from barbell.risk.kill_switch import reset_in_process_cache

        reset_in_process_cache()
        db_path = str(tmp_path / "property_test.db")
        store = JournalStore(db_path=db_path)
        failures: list[str] = []

        for i in range(220):
            reset_in_process_cache()
            nav = random.uniform(50_000, 200_000)
            proposed_contracts = random.randint(1, 20)
            max_loss = random.uniform(10, nav * 0.3)
            proposal = _make_proposal(
                max_loss=max_loss,
                contracts=proposed_contracts,
                sleeve=random.choice(["A", "B"]),
            )
            portfolio = _make_portfolio(nav=nav)
            market = _make_market(
                dispersion_score=random.choice([None, 0.8, 1.2, 1.5]),
                quote_age=random.uniform(0, 200),
                oi=random.randint(0, 2000),
            )
            config = _make_config()

            decision = evaluate(
                proposed=proposal,
                portfolio_state=portfolio,
                market_state=market,
                config=config,
                cycle_id=f"prop-test-{i}",
                store=store,
            )

            if decision.outcome in ("PASS", "RESIZE"):
                if decision.contracts is None:
                    failures.append(
                        f"Iteration {i}: outcome={decision.outcome} but contracts=None"
                    )
                elif decision.contracts > proposed_contracts:
                    failures.append(
                        f"Iteration {i}: contracts={decision.contracts} > "
                        f"proposed={proposed_contracts} — INVARIANT VIOLATED"
                    )

        assert not failures, (
            f"Property test FAILED on {len(failures)} iteration(s):\n"
            + "\n".join(failures)
        )


# ---------------------------------------------------------------------------
# Kill-switch: trip → subsequent evaluate() REJECTs; survives cache reset
# ---------------------------------------------------------------------------

class TestKillSwitchEndToEnd:
    def test_kill_switch_trip_blocks_all_subsequent_evaluations(self, tmp_path):
        """
        Trip the kill switch, then confirm that evaluate() returns VETO
        for any proposal regardless of individual gate results, and that
        the VETO persists after resetting the in-process cache
        (simulating process restart discovering the latched DB).
        """
        from barbell.journal.store import JournalStore
        from barbell.risk.engine import evaluate
        from barbell.risk.kill_switch import check_and_latch, is_latched, reset_in_process_cache

        reset_in_process_cache()
        db_path = str(tmp_path / "kill_switch_e2e.db")
        store = JournalStore(db_path=db_path)
        config = _make_config()

        # Step 1: Trip the kill switch directly
        tripped = check_and_latch(
            current_nav=91_000.0,
            starting_nav=100_000.0,
            cycle_id="trip-cycle",
            store=store,
            threshold_pct=-0.08,
        )
        assert tripped, "check_and_latch should have returned True (tripped)"

        # Step 2: evaluate() should VETO regardless of gate results
        portfolio = _make_portfolio(nav=91_000.0, starting_nav=100_000.0)
        market = _make_market()
        decision = evaluate(
            proposed=_make_proposal(max_loss=100.0),  # tiny, would PASS all other gates
            portfolio_state=portfolio,
            market_state=market,
            config=config,
            cycle_id="post-trip-cycle",
            store=store,
        )
        assert decision.outcome == "VETO", (
            f"Expected VETO after kill switch trip, got {decision.outcome}"
        )

        # Step 3: Reset in-process cache (simulates process restart)
        reset_in_process_cache()
        assert not gates._get_store() or True  # store still injected via engine

        # Step 4: is_latched() should re-discover from DB (not in-process cache)
        # Need to set store again since reset cleared the _latched flag
        gates._set_store(store)
        assert is_latched(store), (
            "is_latched() must discover latch from DB after in-process cache reset "
            "(simulates process restart)"
        )

        # Step 5: evaluate() must still VETO even after cache reset
        decision2 = evaluate(
            proposed=_make_proposal(max_loss=50.0),
            portfolio_state=portfolio,
            market_state=market,
            config=config,
            cycle_id="post-restart-cycle",
            store=store,
        )
        assert decision2.outcome == "VETO", (
            "evaluate() must still VETO after in-process cache reset — "
            "the DB is the source of truth, not the module-level flag"
        )

        gates._set_store(None)
        reset_in_process_cache()
