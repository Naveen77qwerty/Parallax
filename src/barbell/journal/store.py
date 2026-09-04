"""
SQLite-backed (via sqlmodel) append-only log. Tables:

    screen_results          symbol, cycle_id, passed, reason, metrics_json, ts
    catalyst_verdicts       symbol, cycle_id, catalyst_risk, reasoning, ts
    proposed_structures     cycle_id, symbol, structure_json, ts
    risk_decisions          cycle_id, symbol, outcome, reason, gate_breakdown_json, ts
    orders                  order_id, cycle_id, symbol, legs_json, status, fill_price, ts
    positions_snapshot      cycle_id, nav, positions_json, ts
    kill_switch_events      cycle_id, triggered, nav_at_trigger, ts
    capital_reservations    cycle_id, basket_id, reserved_amount, status, ts   [NEW]
    basket_leg_fills        basket_id, underlying, sequence_number, fill_status, ts, cycle_id [NEW]

Nothing is ever UPDATEd or DELETEd — this table set IS the audit trail for
the write-up's risk-gates section and the demo narration. journal/export.py
reads from here; nothing else does.

Usage:
    from barbell.journal.store import JournalStore
    store = JournalStore()   # uses BARBELL_DB_PATH from settings
    store.record_screen_result(cycle_id="c1", symbol="AAPL", passed=True, reason="ok", metrics={})
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine

from barbell.config import get_settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Table models — one SQLModel class per table, all append-only
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ScreenResultRow(SQLModel, table=True):
    __tablename__ = "screen_results"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    symbol: str = Field(index=True)
    passed: bool
    reason: str
    metrics_json: str = Field(default="{}")   # JSON-encoded metrics dict
    ts: datetime = Field(default_factory=_utcnow)


class CatalystVerdictRow(SQLModel, table=True):
    __tablename__ = "catalyst_verdicts"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    symbol: str = Field(index=True)
    catalyst_risk: bool
    reasoning: str
    sources_json: str = Field(default="[]")   # JSON list of sources considered
    ts: datetime = Field(default_factory=_utcnow)


class ProposedStructureRow(SQLModel, table=True):
    __tablename__ = "proposed_structures"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    symbol: str = Field(index=True)
    sleeve: str = Field(default="A")
    structure_json: str                       # full ProposedStructure JSON
    ts: datetime = Field(default_factory=_utcnow)


class RiskDecisionRow(SQLModel, table=True):
    __tablename__ = "risk_decisions"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    symbol: str = Field(index=True)
    outcome: str                              # "PASS" | "RESIZE" | "VETO"
    contracts: int | None = Field(default=None)
    reason: str                               # primary reason string
    gate_breakdown_json: str = Field(default="[]")  # all GateResult reasons
    ts: datetime = Field(default_factory=_utcnow)


class OrderRow(SQLModel, table=True):
    __tablename__ = "orders"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    order_id: str = Field(index=True)         # Alpaca order UUID
    cycle_id: str = Field(index=True)
    symbol: str = Field(index=True)           # underlying
    legs_json: str                            # list[ProposedLeg] JSON
    status: str                               # "submitted" | "filled" | "cancelled" | "expired"
    fill_price: float | None = Field(default=None)
    ts: datetime = Field(default_factory=_utcnow)


class PositionsSnapshotRow(SQLModel, table=True):
    __tablename__ = "positions_snapshot"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    nav: float
    positions_json: str                       # list of position dicts JSON
    ts: datetime = Field(default_factory=_utcnow)


class KillSwitchEventRow(SQLModel, table=True):
    __tablename__ = "kill_switch_events"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    triggered: bool
    nav_at_trigger: float
    reason: str = Field(default="")
    ts: datetime = Field(default_factory=_utcnow)


# NEW — basket execution capital reservation
class CapitalReservationRow(SQLModel, table=True):
    __tablename__ = "capital_reservations"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    basket_id: str = Field(index=True)        # UUID for the basket attempt
    reserved_amount: float
    status: str = Field(default="reserved")   # "reserved" | "released" | "consumed"
    ts: datetime = Field(default_factory=_utcnow)


# NEW — per-leg fill tracking for sequential basket execution
class BasketLegFillRow(SQLModel, table=True):
    __tablename__ = "basket_leg_fills"  # type: ignore[assignment]

    id: int | None = Field(default=None, primary_key=True)
    cycle_id: str = Field(index=True)
    basket_id: str = Field(index=True)
    underlying: str = Field(index=True)
    sequence_number: int                      # order of submission within the basket
    fill_status: str                          # "pending" | "filled" | "cancelled" | "failed"
    fill_price: float | None = Field(default=None)
    ts: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# JournalStore — the only class allowed to write to the DB
# ---------------------------------------------------------------------------


class JournalStore:
    """
    Append-only journal backed by SQLite (via sqlmodel).

    The DB path comes from Settings.barbell_db_path.  Tables are created on
    first use if missing.

    INVARIANT: No method in this class may issue an UPDATE or DELETE.
    The entire DB is an audit trail; overwriting any record would corrupt it.

    Usage:
        store = JournalStore()          # default path from settings
        store = JournalStore(db_path)   # explicit path (e.g. in tests)
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = get_settings().barbell_db_path
        self._db_path = Path(db_path)
        # Ensure parent directories exist
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            f"sqlite:///{self._db_path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        self._create_tables()
        log.debug("JournalStore initialised at %s", self._db_path)

    def _create_tables(self) -> None:
        """Create all tables if they don't exist yet."""
        SQLModel.metadata.create_all(self._engine)

    def close(self) -> None:
        """Dispose the underlying engine, closing its pooled DB connection."""
        self._engine.dispose()

    # ------------------------------------------------------------------
    # record_* methods — one per table, INSERT only, never UPDATE/DELETE
    # ------------------------------------------------------------------

    def record_screen_result(
        self,
        *,
        cycle_id: str,
        symbol: str,
        passed: bool,
        reason: str,
        metrics: dict[str, Any] | None = None,
    ) -> ScreenResultRow:
        row = ScreenResultRow(
            cycle_id=cycle_id,
            symbol=symbol,
            passed=passed,
            reason=reason,
            metrics_json=json.dumps(metrics or {}),
        )
        return self._insert(row)

    def record_catalyst_verdict(
        self,
        *,
        cycle_id: str,
        symbol: str,
        catalyst_risk: bool,
        reasoning: str,
        sources: list[str] | None = None,
    ) -> CatalystVerdictRow:
        row = CatalystVerdictRow(
            cycle_id=cycle_id,
            symbol=symbol,
            catalyst_risk=catalyst_risk,
            reasoning=reasoning,
            sources_json=json.dumps(sources or []),
        )
        return self._insert(row)

    def record_proposed_structure(
        self,
        *,
        cycle_id: str,
        symbol: str,
        sleeve: str,
        structure: dict[str, Any],
    ) -> ProposedStructureRow:
        row = ProposedStructureRow(
            cycle_id=cycle_id,
            symbol=symbol,
            sleeve=sleeve,
            structure_json=json.dumps(structure),
        )
        return self._insert(row)

    def record_risk_decision(
        self,
        *,
        cycle_id: str,
        symbol: str,
        outcome: str,
        contracts: int | None,
        reason: str,
        gate_breakdown: list[str] | None = None,
    ) -> RiskDecisionRow:
        row = RiskDecisionRow(
            cycle_id=cycle_id,
            symbol=symbol,
            outcome=outcome,
            contracts=contracts,
            reason=reason,
            gate_breakdown_json=json.dumps(gate_breakdown or []),
        )
        return self._insert(row)

    def record_order(
        self,
        *,
        order_id: str,
        cycle_id: str,
        symbol: str,
        legs: list[dict[str, Any]],
        status: str,
        fill_price: float | None = None,
    ) -> OrderRow:
        row = OrderRow(
            order_id=order_id,
            cycle_id=cycle_id,
            symbol=symbol,
            legs_json=json.dumps(legs),
            status=status,
            fill_price=fill_price,
        )
        return self._insert(row)

    def record_positions_snapshot(
        self,
        *,
        cycle_id: str,
        nav: float,
        positions: list[dict[str, Any]],
    ) -> PositionsSnapshotRow:
        row = PositionsSnapshotRow(
            cycle_id=cycle_id,
            nav=nav,
            positions_json=json.dumps(positions),
        )
        return self._insert(row)

    def record_kill_switch_event(
        self,
        *,
        cycle_id: str,
        triggered: bool,
        nav_at_trigger: float,
        reason: str = "",
    ) -> KillSwitchEventRow:
        row = KillSwitchEventRow(
            cycle_id=cycle_id,
            triggered=triggered,
            nav_at_trigger=nav_at_trigger,
            reason=reason,
        )
        return self._insert(row)

    def record_capital_reservation(
        self,
        *,
        cycle_id: str,
        basket_id: str,
        reserved_amount: float,
        status: str = "reserved",
    ) -> CapitalReservationRow:
        """
        Record a capital reservation for an in-flight basket entry.
        status: "reserved" | "released" | "consumed"
        APPEND ONLY — never updates an existing reservation row.
        To release or consume, insert a NEW row with the updated status.
        """
        if status not in ("reserved", "released", "consumed"):
            raise ValueError(f"Invalid capital_reservation status: {status!r}")
        row = CapitalReservationRow(
            cycle_id=cycle_id,
            basket_id=basket_id,
            reserved_amount=reserved_amount,
            status=status,
        )
        return self._insert(row)

    def record_basket_leg_fill(
        self,
        *,
        cycle_id: str,
        basket_id: str,
        underlying: str,
        sequence_number: int,
        fill_status: str,
        fill_price: float | None = None,
    ) -> BasketLegFillRow:
        """
        Record the fill status of one leg in a sequential basket execution.
        fill_status: "pending" | "filled" | "cancelled" | "failed"
        APPEND ONLY — insert new rows for status updates, never modify existing.
        """
        row = BasketLegFillRow(
            cycle_id=cycle_id,
            basket_id=basket_id,
            underlying=underlying,
            sequence_number=sequence_number,
            fill_status=fill_status,
            fill_price=fill_price,
        )
        return self._insert(row)

    # ------------------------------------------------------------------
    # Read helpers (used by export.py only)
    # ------------------------------------------------------------------

    def _session(self) -> Session:
        return Session(self._engine)

    def _insert(self, row: SQLModel) -> Any:
        with self._session() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
        return row
