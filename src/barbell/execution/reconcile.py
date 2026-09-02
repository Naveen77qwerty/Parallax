"""
Broker-is-truth reconciliation — runs at the start/end of every cycle AND
after every basket leg fill.

    reconcile(client, store, *, cycle_id) -> ReconciliationReport

Pulls positions + account state from AlpacaClient (broker is always the
source of truth), diffs against the last positions_snapshot in the journal,
writes a new snapshot either way (for the audit trail), and returns a
ReconciliationReport.

On divergence: logs at CRITICAL, sets reconciliation_diverged=True on the
returned report.  Callers should propagate this into market_state before
calling risk/engine.evaluate() — gate_broker_reconciliation reads
market_state.reconciliation_diverged to block new entries.

Called from two places:
    1. scheduler/loop.py — at the start of every cycle (Member 4 wires this).
    2. execution/orders.py submit_basket() — after every basket leg fill.
Same function, called from two places, always writes a snapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from barbell.broker.alpaca_client import AlpacaClient
from barbell.journal.store import JournalStore

log = logging.getLogger(__name__)


@dataclass
class ReconciliationReport:
    """
    Result of a single reconciliation run.

    diverged:            True if broker state doesn't match journal state.
    description:         Human-readable summary of what was found / diffed.
    broker_positions:    Raw positions from get_positions() — broker is truth.
    journal_positions:   Positions from the last snapshot in the DB.
    broker_nav:          Current NAV from get_account().
    """
    diverged: bool
    description: str
    broker_positions: list[dict[str, Any]] = field(default_factory=list)
    journal_positions: list[dict[str, Any]] = field(default_factory=list)
    broker_nav: float = 0.0


def reconcile(
    client: AlpacaClient,
    store: JournalStore,
    *,
    cycle_id: str,
) -> ReconciliationReport:
    """
    Pull broker state, diff against the last journal snapshot, write a new
    snapshot, and return a ReconciliationReport.

    Divergence criteria:
        - A position exists at the broker but not in the last journal snapshot.
        - A position exists in the last journal snapshot but not at the broker.
        - The quantity (qty) for a symbol differs between broker and snapshot.

    The journal snapshot is keyed by the OCC option symbol.  Non-option
    positions (equities, cash) are ignored for divergence purposes — this
    system only manages options.

    Args:
        client:    AlpacaClient — the ONLY module allowed to call alpaca-py.
        store:     JournalStore for reading the last snapshot and writing a new one.
        cycle_id:  Current cycle ID for journal row attribution.

    Returns:
        ReconciliationReport with diverged flag and human-readable description.

    Side effects:
        - Always writes a new positions_snapshot row to the journal.
        - Logs CRITICAL if diverged=True.
    """
    # --- Pull broker state ---
    try:
        account = client.get_account()
        broker_nav = account.get("equity", 0.0)
        broker_positions_raw = client.get_positions()
    except Exception as exc:
        # Can't reach broker — this IS a divergence from a safety perspective.
        report = ReconciliationReport(
            diverged=True,
            description=f"Broker API call failed: {exc}. Cannot confirm position state.",
            broker_positions=[],
            journal_positions=[],
            broker_nav=0.0,
        )
        log.critical(
            "RECONCILE: broker API unreachable — %s. Halting new entries.", exc
        )
        return report

    # Filter to options positions only for diff purposes
    broker_options = {
        p["symbol"]: p
        for p in broker_positions_raw
        if p.get("asset_class", "") == "us_option"
    }

    # --- Pull last journal snapshot ---
    journal_positions = _get_last_snapshot_positions(store)
    journal_options = {
        p["symbol"]: p
        for p in journal_positions
        if p.get("asset_class", "") == "us_option"
    }

    # --- Compute diff ---
    broker_symbols = set(broker_options.keys())
    journal_symbols = set(journal_options.keys())

    in_broker_not_journal = broker_symbols - journal_symbols
    in_journal_not_broker = journal_symbols - broker_symbols

    qty_mismatches: list[str] = []
    for sym in broker_symbols & journal_symbols:
        broker_qty = float(broker_options[sym].get("qty", 0))
        journal_qty = float(journal_options[sym].get("qty", 0))
        if abs(broker_qty - journal_qty) > 0.001:  # float tolerance
            qty_mismatches.append(
                f"{sym}: broker_qty={broker_qty}, journal_qty={journal_qty}"
            )

    diverged = bool(in_broker_not_journal or in_journal_not_broker or qty_mismatches)

    # --- Build description ---
    lines: list[str] = []
    if in_broker_not_journal:
        lines.append(
            f"Broker has positions not in journal: {sorted(in_broker_not_journal)}"
        )
    if in_journal_not_broker:
        lines.append(
            f"Journal has positions not at broker: {sorted(in_journal_not_broker)}"
        )
    if qty_mismatches:
        lines.append(f"Quantity mismatches: {qty_mismatches}")
    if not diverged:
        lines.append(
            f"Reconciliation clean: {len(broker_symbols)} option position(s) match. "
            f"NAV=${broker_nav:.2f}"
        )
    description = " | ".join(lines)

    # --- Log on divergence ---
    if diverged:
        log.critical(
            "RECONCILE DIVERGENCE DETECTED (cycle=%s): %s",
            cycle_id,
            description,
        )
    else:
        log.info("Reconciliation clean (cycle=%s): %s", cycle_id, description)

    # --- Write new snapshot to journal (always — diverged or clean) ---
    all_positions = broker_positions_raw  # full list, not just options
    store.record_positions_snapshot(
        cycle_id=cycle_id,
        nav=broker_nav,
        positions=all_positions,
    )

    return ReconciliationReport(
        diverged=diverged,
        description=description,
        broker_positions=broker_positions_raw,
        journal_positions=journal_positions,
        broker_nav=broker_nav,
    )


# ---------------------------------------------------------------------------
# Internal helper: read last snapshot from DB
# ---------------------------------------------------------------------------

def _get_last_snapshot_positions(store: JournalStore) -> list[dict[str, Any]]:
    """
    Return the positions from the most recent positions_snapshot row.

    Returns an empty list if no snapshot exists yet (fresh DB).
    """
    from sqlmodel import Session, select

    from barbell.journal.store import PositionsSnapshotRow

    with Session(store._engine) as session:
        # Get the most recent row by ID (rows are append-only, so highest ID = latest)
        stmt = (
            select(PositionsSnapshotRow)
            .order_by(PositionsSnapshotRow.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = session.exec(stmt).first()
        if row is None:
            return []
        try:
            return json.loads(row.positions_json)
        except Exception:
            log.warning("Could not parse positions_json from last snapshot — treating as empty.")
            return []
