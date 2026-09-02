"""
Unit tests for the append-only journal store and export module.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from barbell.journal.export import export_trade_log_csv, export_writeup
from barbell.journal.store import JournalStore


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_journal.db"


@pytest.fixture
def journal_store(temp_db: Path) -> JournalStore:
    return JournalStore(db_path=temp_db)


def test_insert_all_tables_and_export(journal_store: JournalStore, temp_db: Path):
    """Insert a row in all 9 tables and verify export_writeup / export_trade_log_csv."""
    cycle_id = "cycle-20260901-001"

    # 1. Screen Result
    row_screen = journal_store.record_screen_result(
        cycle_id=cycle_id,
        symbol="NVDA",
        passed=True,
        reason="IV rank > 50, liquidity pass",
        metrics={"iv_rank": 65.0, "spread_pct": 0.03},
    )
    assert row_screen.id is not None
    assert row_screen.symbol == "NVDA"

    # 2. Catalyst Verdict
    row_catalyst = journal_store.record_catalyst_verdict(
        cycle_id=cycle_id,
        symbol="NVDA",
        catalyst_risk=False,
        reasoning="Earnings 3 weeks out; no unscheduled binary events detected",
        sources=["sec_filings", "news_digest"],
    )
    assert row_catalyst.id is not None
    assert row_catalyst.catalyst_risk is False

    # 3. Proposed Structure
    row_structure = journal_store.record_proposed_structure(
        cycle_id=cycle_id,
        symbol="NVDA",
        sleeve="A",
        structure={
            "underlying": "NVDA",
            "structure_type": "put_credit_spread",
            "contracts": 2,
            "limit_price": 0.85,
            "max_loss_estimate": 830.0,
        },
    )
    assert row_structure.id is not None
    assert row_structure.sleeve == "A"

    # 4. Risk Decision
    row_risk = journal_store.record_risk_decision(
        cycle_id=cycle_id,
        symbol="NVDA",
        outcome="PASS",
        contracts=2,
        reason="All 12 risk gates passed",
        gate_breakdown=["G1: PASS", "G2: PASS", "G3: PASS"],
    )
    assert row_risk.id is not None
    assert row_risk.outcome == "PASS"

    # 5. Order
    row_order = journal_store.record_order(
        order_id="alpaca-order-uuid-999",
        cycle_id=cycle_id,
        symbol="NVDA",
        legs=[
            {"symbol": "NVDA260904P00110000", "side": "sell", "contracts": 2},
            {"symbol": "NVDA260904P00105000", "side": "buy", "contracts": 2},
        ],
        status="filled",
        fill_price=0.85,
    )
    assert row_order.id is not None
    assert row_order.status == "filled"

    # 6. Positions Snapshot
    row_snapshot = journal_store.record_positions_snapshot(
        cycle_id=cycle_id,
        nav=100450.00,
        positions=[{"symbol": "NVDA", "market_value": 170.0}],
    )
    assert row_snapshot.id is not None
    assert row_snapshot.nav == 100450.00

    # 7. Kill Switch Event
    row_kill = journal_store.record_kill_switch_event(
        cycle_id=cycle_id,
        triggered=False,
        nav_at_trigger=100450.00,
        reason="Drawdown check ok",
    )
    assert row_kill.id is not None
    assert row_kill.triggered is False

    # 8. Capital Reservation (NEW table)
    row_res = journal_store.record_capital_reservation(
        cycle_id=cycle_id,
        basket_id="basket-abc-123",
        reserved_amount=830.0,
        status="reserved",
    )
    assert row_res.id is not None
    assert row_res.status == "reserved"

    # 9. Basket Leg Fill (NEW table)
    row_fill = journal_store.record_basket_leg_fill(
        cycle_id=cycle_id,
        basket_id="basket-abc-123",
        underlying="NVDA",
        sequence_number=1,
        fill_status="filled",
        fill_price=0.85,
    )
    assert row_fill.id is not None
    assert row_fill.sequence_number == 1

    # Verify export_trade_log_csv
    csv_out = export_trade_log_csv(temp_db)
    assert "order_id,cycle_id,symbol,status,fill_price,ts,legs" in csv_out
    assert "alpaca-order-uuid-999" in csv_out
    assert "NVDA" in csv_out
    assert "filled" in csv_out

    # Verify export_writeup
    writeup = export_writeup(temp_db)
    assert "# Dispersion Barbell — Trade Write-Up" in writeup
    assert "$100,450.00" in writeup
    assert "NVDA" in writeup
    assert "**Filled orders:** 1" in writeup
    assert "Capital reservations recorded: 1" in writeup
    assert "Basket leg fill events: 1" in writeup


def test_journal_store_append_only_invariants():
    """Verify that no update or delete methods are exposed on JournalStore."""
    public_methods = [
        name
        for name, func in inspect.getmembers(JournalStore, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]

    for m in public_methods:
        assert not m.startswith("update"), f"Forbidden UPDATE method found: {m}"
        assert not m.startswith("delete"), f"Forbidden DELETE method found: {m}"
        assert not m.startswith("remove"), f"Forbidden REMOVE method found: {m}"
        assert not m.startswith("drop"), f"Forbidden DROP method found: {m}"


def test_invalid_capital_reservation_status(journal_store: JournalStore):
    """Test that invalid capital reservation status raises ValueError."""
    with pytest.raises(ValueError, match="Invalid capital_reservation status"):
        journal_store.record_capital_reservation(
            cycle_id="c1",
            basket_id="b1",
            reserved_amount=100.0,
            status="invalid_status",
        )
