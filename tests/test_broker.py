"""
Unit tests for Alpaca broker client and clock functions with full mocking.
No live API calls are ever made.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from barbell.agent.schemas import ProposedLeg
from barbell.broker.alpaca_client import AlpacaClient, NakedShortError
from barbell.broker.clock import (
    is_market_open,
    must_be_flat_by,
    time_to_deadline,
)


@pytest.fixture
def mock_settings(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini_key")
    monkeypatch.setenv("BARBELL_DB_PATH", "data/test.db")
    monkeypatch.setenv("BARBELL_LOG_LEVEL", "INFO")


@pytest.fixture
def broker_client(mock_settings):
    with patch("barbell.broker.alpaca_client.TradingClient") as mock_trading, \
         patch("barbell.broker.alpaca_client.OptionHistoricalDataClient") as mock_data:
        client = AlpacaClient(api_key="k", secret_key="s", paper=True)
        client._mock_trading = mock_trading.return_value
        client._mock_data = mock_data.return_value
        yield client


def test_get_account_parsing(broker_client):
    """Test get_account parses TradeAccount model correctly into dictionary."""
    mock_acct = MagicMock()
    mock_acct.id = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    mock_acct.account_number = "PA12345678"
    mock_acct.equity = "100500.25"
    mock_acct.buying_power = "200000.00"
    mock_acct.options_buying_power = "100500.25"
    mock_acct.options_approved_level = 3
    mock_acct.options_trading_level = 3
    mock_acct.cash = "95000.00"
    mock_acct.portfolio_value = "100500.25"
    mock_acct.trading_blocked = False

    broker_client._trading.get_account.return_value = mock_acct

    res = broker_client.get_account()

    assert res["id"] == "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    assert res["account_number"] == "PA12345678"
    assert res["equity"] == 100500.25
    assert res["buying_power"] == 200000.00
    assert res["options_approved_level"] == 3
    assert res["trading_blocked"] is False


def test_get_positions_parsing(broker_client):
    """Test get_positions transforms Position objects into standardized dicts."""
    mock_pos = MagicMock()
    mock_pos.symbol = "SPY260904P00550000"
    mock_pos.qty = "2"
    mock_pos.side.value = "long"
    mock_pos.market_value = "450.00"
    mock_pos.avg_entry_price = "2.25"
    mock_pos.unrealized_pl = "25.00"
    mock_pos.asset_class.value = "us_option"
    mock_pos.current_price = "2.35"

    broker_client._trading.get_all_positions.return_value = [mock_pos]

    positions = broker_client.get_positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "SPY260904P00550000"
    assert positions[0]["qty"] == 2.0
    assert positions[0]["side"] == "long"
    assert positions[0]["market_value"] == 450.00
    assert positions[0]["asset_class"] == "us_option"


def test_submit_mleg_order_covered_spread_success(broker_client):
    """Test that a covered credit spread builds an OrderRequest with order_class=MLEG."""
    exp = date(2026, 9, 4)
    legs = [
        ProposedLeg(
            symbol="SPY260904P00550000",
            expiry=exp,
            strike=550.0,
            right="put",
            side="sell",
            contracts=1,
        ),
        ProposedLeg(
            symbol="SPY260904P00545000",
            expiry=exp,
            strike=545.0,
            right="put",
            side="buy",
            contracts=1,
        ),
    ]

    mock_order = MagicMock()
    mock_order.id = "order-uuid-1234"
    mock_order.status = "submitted"
    broker_client._trading.submit_order.return_value = mock_order

    order_id = broker_client.submit_mleg_order(legs, limit_price=1.25, tif="day")
    assert order_id == "order-uuid-1234"

    # Verify SDK was called with LimitOrderRequest containing MLEG class and OptionLegRequests
    assert broker_client._trading.submit_order.called
    req = broker_client._trading.submit_order.call_args[0][0]
    assert req.order_class.value == "mleg"
    # This codebase's convention (schemas.py) is positive=credit, negative=debit —
    # the OPPOSITE of Alpaca's mleg convention (positive=debit, negative=credit),
    # so a credit spread's limit_price must be sent to Alpaca negated.
    assert req.limit_price == -1.25
    assert req.symbol is None  # Alpaca rejects mleg orders with `symbol` set
    assert len(req.legs) == 2
    assert req.legs[0].symbol == "SPY260904P00550000"
    assert req.legs[0].side.value == "sell"
    assert req.legs[1].symbol == "SPY260904P00545000"
    assert req.legs[1].side.value == "buy"


def test_submit_mleg_order_naked_short_raises_before_sdk(broker_client):
    """Defense-in-depth: test that naked short leg immediately raises NakedShortError without calling SDK."""
    exp = date(2026, 9, 4)
    naked_legs = [
        ProposedLeg(
            symbol="AAPL260904C00230000",
            expiry=exp,
            strike=230.0,
            right="call",
            side="sell",
            contracts=1,
        )
    ]

    with pytest.raises(NakedShortError, match="Naked short detected"):
        broker_client.submit_mleg_order(naked_legs, limit_price=1.50)

    # SDK must never have been called
    assert not broker_client._trading.submit_order.called


def test_submit_mleg_order_uncovered_expiry_raises(broker_client):
    """Test short leg with one expiry and buy leg with different expiry triggers naked short error."""
    legs = [
        ProposedLeg(
            symbol="AAPL260904P00200000",
            expiry=date(2026, 9, 4),
            strike=200.0,
            right="put",
            side="sell",
            contracts=1,
        ),
        ProposedLeg(
            symbol="AAPL260911P00195000",
            expiry=date(2026, 9, 11),  # Different expiry
            strike=195.0,
            right="put",
            side="buy",
            contracts=1,
        ),
    ]

    with pytest.raises(NakedShortError, match="Naked short detected"):
        broker_client.submit_mleg_order(legs, limit_price=0.80)

    assert not broker_client._trading.submit_order.called


def test_submit_mleg_order_mismatched_right_raises(broker_client):
    """A SELL put + an unrelated BUY call at the same expiry is not a
    defined-risk structure — matching by expiry alone would have let this
    through as 'covered'."""
    legs = [
        ProposedLeg(
            symbol="AAPL260904P00200000",
            expiry=date(2026, 9, 4),
            strike=200.0,
            right="put",
            side="sell",
            contracts=1,
        ),
        ProposedLeg(
            symbol="AAPL260904C00210000",
            expiry=date(2026, 9, 4),  # same expiry, but a CALL — does not cover the put
            strike=210.0,
            right="call",
            side="buy",
            contracts=1,
        ),
    ]

    with pytest.raises(NakedShortError, match="Naked short detected"):
        broker_client.submit_mleg_order(legs, limit_price=0.80)

    assert not broker_client._trading.submit_order.called


def test_submit_mleg_order_wrong_side_strike_raises(broker_client):
    """A SELL put covered by a BUY put on the WRONG side of the strike (higher,
    not lower) does not actually bound the spread's max loss and must still
    be rejected, even though right and expiry both match."""
    legs = [
        ProposedLeg(
            symbol="AAPL260904P00200000",
            expiry=date(2026, 9, 4),
            strike=200.0,
            right="put",
            side="sell",
            contracts=1,
        ),
        ProposedLeg(
            symbol="AAPL260904P00205000",
            expiry=date(2026, 9, 4),
            strike=205.0,  # higher than the sell strike — wrong side for a put spread
            right="put",
            side="buy",
            contracts=1,
        ),
    ]

    with pytest.raises(NakedShortError, match="Naked short detected"):
        broker_client.submit_mleg_order(legs, limit_price=0.80)

    assert not broker_client._trading.submit_order.called


def test_submit_mleg_order_validation_errors(broker_client):
    """Test validation on empty legs or 0.0 limit price."""
    with pytest.raises(ValueError, match="at least one leg"):
        broker_client.submit_mleg_order([], limit_price=1.0)

    leg = ProposedLeg(
        symbol="SPY260904P00550000",
        expiry=date(2026, 9, 4),
        strike=550.0,
        right="put",
        side="buy",
        contracts=1,
    )
    with pytest.raises(ValueError, match="limit_price must be non-zero"):
        broker_client.submit_mleg_order([leg], limit_price=0.0)


def test_clock_functions(mock_settings):
    """Test calendar and market hours functions in broker/clock.py."""
    mock_client = MagicMock()
    mock_client.get_clock.return_value = {
        "is_open": True,
        "timestamp": datetime.now(UTC),
        "next_open": datetime.now(UTC),
        "next_close": datetime.now(UTC) + timedelta(hours=4),
    }

    assert is_market_open(mock_client) is True

    # Test time to deadline
    deadline_remaining = time_to_deadline()
    assert isinstance(deadline_remaining, timedelta)

    # Test flatten deadline
    flat_dt = must_be_flat_by()
    assert flat_dt.tzinfo is not None
