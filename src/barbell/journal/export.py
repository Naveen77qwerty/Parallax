"""
    export_writeup(db_path: str) -> str   # renders docs/writeup_generated.md
    export_trade_log_csv(db_path: str) -> str

Turns the append-only journal into the tables and narrative the one-page
write-up needs (AI logic examples, risk-gate trigger counts, final P&L,
account ID) without hand-transcription — run this the morning of Sep 4
rather than writing the write-up from memory.

Member 4 will expand the narrative sections; this first-pass version makes
the plumbing work end-to-end (non-empty output, correct section headers,
actual data from the DB).
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, create_engine, select

from barbell.journal.store import (
    BasketLegFillRow,
    CapitalReservationRow,
    CatalystVerdictRow,
    KillSwitchEventRow,
    OrderRow,
    PositionsSnapshotRow,
    ProposedStructureRow,
    RiskDecisionRow,
    ScreenResultRow,
)

log = logging.getLogger(__name__)

_WRITEUP_PATH = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "writeup_generated.md"


def _engine(db_path: str | Path):
    return create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def export_trade_log_csv(db_path: str | Path) -> str:
    """
    Dump the orders table to a CSV string.

    Columns: order_id, cycle_id, symbol, status, fill_price, ts, legs_json

    Returns:
        CSV string (empty header row if no orders recorded yet).
    """
    engine = _engine(db_path)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["order_id", "cycle_id", "symbol", "status", "fill_price", "ts", "legs"])

    with Session(engine) as session:
        rows = session.exec(select(OrderRow).order_by(OrderRow.ts)).all()

    for row in rows:
        legs = json.loads(row.legs_json) if row.legs_json else []
        writer.writerow(
            [
                row.order_id,
                row.cycle_id,
                row.symbol,
                row.status,
                row.fill_price or "",
                row.ts.isoformat() if row.ts else "",
                json.dumps(legs),
            ]
        )

    csv_str = buf.getvalue()
    log.info("export_trade_log_csv: %d orders exported", len(rows))
    return csv_str


# ---------------------------------------------------------------------------
# Markdown write-up export
# ---------------------------------------------------------------------------


def export_writeup(db_path: str | Path) -> str:
    """
    Render a markdown write-up from the journal DB.

    Writes the result to docs/writeup_generated.md and also returns it as a
    string so callers can pipe it to stdout or further processing.

    Member 4 will expand the narrative; this version ensures:
    - All section headers exist
    - Actual counts and data from the DB appear
    - The file is non-empty and syntactically valid markdown

    Returns:
        Markdown string.
    """
    engine = _engine(db_path)

    with Session(engine) as session:
        orders = session.exec(select(OrderRow).order_by(OrderRow.ts)).all()
        screens = session.exec(select(ScreenResultRow)).all()
        verdicts = session.exec(select(CatalystVerdictRow)).all()
        structures = session.exec(select(ProposedStructureRow)).all()
        risk_decisions = session.exec(select(RiskDecisionRow)).all()
        snapshots = session.exec(
            select(PositionsSnapshotRow).order_by(PositionsSnapshotRow.ts.desc())  # type: ignore[attr-defined]
        ).all()
        kill_events = session.exec(select(KillSwitchEventRow)).all()
        reservations = session.exec(select(CapitalReservationRow)).all()
        leg_fills = session.exec(select(BasketLegFillRow)).all()

    # Summary stats
    filled_orders = [o for o in orders if o.status == "filled"]
    vetoed = [r for r in risk_decisions if r.outcome == "VETO"]
    resized = [r for r in risk_decisions if r.outcome == "RESIZE"]
    catalyst_blocked = [v for v in verdicts if v.catalyst_risk]
    latest_nav = snapshots[0].nav if snapshots else None
    kill_triggered = any(k.triggered for k in kill_events)

    # Read account ID if available
    account_id_path = Path(db_path).parent / "account_id.txt"
    account_id = account_id_path.read_text().strip() if account_id_path.exists() else "NOT_SET"

    generated_at = datetime.now(UTC).isoformat()

    lines: list[str] = [
        "# Dispersion Barbell — Trade Write-Up",
        "",
        f"> **Generated:** {generated_at}  ",
        f"> **Account ID:** {account_id}  ",
        f"> **DB path:** {db_path}",
        "",
        "---",
        "",
        "## Strategy Summary",
        "",
        "Autonomous options agent implementing a dispersion barbell: short rich",
        "single-name premium (Sleeve A — put credit spreads/iron condors) and",
        "long cheap index convexity (Sleeve B — SPY put debit spreads).",
        "All decisions — screening, catalyst gating, structure proposal, risk",
        "evaluation — are journaled and reproducible from this DB.",
        "",
        "---",
        "",
        "## Performance",
        "",
        f"- **Latest NAV:** {f'${latest_nav:,.2f}' if latest_nav else '_no snapshot yet_'}",
        f"- **Filled orders:** {len(filled_orders)}",
        f"- **Total orders submitted:** {len(orders)}",
        f"- **Kill switch triggered:** {'YES ⚠️' if kill_triggered else 'No'}",
        "",
        "---",
        "",
        "## AI Decision Log",
        "",
        "### Screening",
        f"- Candidates screened: {len(screens)}",
        f"- Passed screen: {sum(1 for s in screens if s.passed)}",
        f"- Rejected by screen: {sum(1 for s in screens if not s.passed)}",
        "",
        "### Catalyst Gate (Claude LLM #1)",
        f"- Symbols evaluated: {len(verdicts)}",
        f"- Catalyst risk flagged (veto): {len(catalyst_blocked)}",
        "",
    ]

    # Show a few catalyst examples
    if catalyst_blocked:
        lines.append("**Catalyst vetoes (sample):**")
        lines.append("")
        for v in catalyst_blocked[:3]:
            lines.append(f"- **{v.symbol}** ({v.ts}): {v.reasoning[:120]}...")
        lines.append("")

    lines += [
        "### Structure Agent (Claude LLM #2)",
        f"- Structures proposed: {len(structures)}",
        "",
        "### Risk Engine",
        f"- Decisions: {len(risk_decisions)}",
        f"- PASS: {sum(1 for r in risk_decisions if r.outcome == 'PASS')}",
        f"- RESIZE: {len(resized)}",
        f"- VETO: {len(vetoed)}",
        "",
    ]

    if vetoed:
        lines.append("**Veto reasons (sample):**")
        lines.append("")
        for r in vetoed[:3]:
            breakdown = json.loads(r.gate_breakdown_json) if r.gate_breakdown_json else []
            lines.append(f"- **{r.symbol}** ({r.ts}): {r.reason}")
            if breakdown:
                lines.append(f"  - Gates fired: {'; '.join(breakdown[:3])}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Orders",
        "",
        "| Order ID | Symbol | Status | Fill Price | Timestamp |",
        "|---|---|---|---|---|",
    ]

    for o in orders[-20:]:  # last 20 orders
        fill = f"${o.fill_price:.4f}" if o.fill_price else "—"
        ts = o.ts.strftime("%Y-%m-%d %H:%M:%S UTC") if o.ts else ""
        oid_short = str(o.order_id)[:8]
        lines.append(f"| {oid_short}… | {o.symbol} | {o.status} | {fill} | {ts} |")

    if not orders:
        lines.append("| — | — | _no orders yet_ | — | — |")

    lines += [
        "",
        "---",
        "",
        "## Basket Execution",
        "",
        f"- Capital reservations recorded: {len(reservations)}",
        f"- Basket leg fill events: {len(leg_fills)}",
        "",
        "---",
        "",
        "## Risk Gate Audit",
        "",
        "_Full gate-by-gate breakdown available in the `risk_decisions` table._",
        "",
        f"- Gate-triggered resizes: {len(resized)}",
        f"- Gate-triggered vetoes: {len(vetoed)}",
        "",
        "---",
        "",
        "## Technology Implementation",
        "",
        "- **Broker:** Alpaca paper trading via `alpaca-py` SDK",
        "- **LLM decisions:** Anthropic Claude (catalyst gate + structure proposal)",
        "- **Bulk triage:** Featherless AI open-source model (non-critical headline pre-digest)",
        "- **MCP path:** `alpaca-mcp-server` — interactive / demo session",
        "- **CLI path:** `barbell` CLI — scheduler and headless execution",
        "- **Storage:** SQLite append-only journal (this DB)",
        "- **CI:** GitHub Actions — pytest on every push",
        "",
        "---",
        "",
        "_This file is auto-generated by `journal/export.py`. Do not edit manually._",
        "",
    ]

    md = "\n".join(lines)

    # Write to docs/
    _WRITEUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WRITEUP_PATH.write_text(md, encoding="utf-8")
    log.info("Writeup exported to %s (%d chars)", _WRITEUP_PATH, len(md))

    return md
