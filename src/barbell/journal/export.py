"""
    export_writeup(db_path: str) -> str   # renders docs/writeup_generated.md
    export_trade_log_csv(db_path: str) -> str

Turns the append-only journal into the tables and narrative the one-page
write-up needs (AI logic examples, risk-gate trigger counts, final P&L,
account ID) without hand-transcription — run this the morning of Sep 4
rather than writing the write-up from memory.

Member 4 extended this to cover:
  1. All 13 risk gates, named individually with trigger counts
  2. Basket-reservation and dispersion-score gates called out specifically
     as the differentiated safety mechanisms
  3. Dispersion-score trend table across the week
  4. Basket-atomicity fix as a named defense paragraph
  5. AI logic section with real catalyst-gate reasoning examples
  6. Alpaca infrastructure section (MCP + CLI + alpaca-py)
"""

from __future__ import annotations

import csv
import io
import json
import logging
from collections import Counter
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
# Markdown write-up export (Member 4 extended version)
# ---------------------------------------------------------------------------

_GATE_NAMES = [
    "gate_per_position_loss_cap",
    "gate_portfolio_loss_cap",
    "gate_defined_risk_only",
    "gate_quote_staleness",
    "gate_liquidity_floor",
    "gate_dispersion_score",
    "gate_earnings_blackout",
    "gate_pre_nfp_flatten",
    "gate_expiry_past_deadline",
    "gate_concentration",
    "gate_drawdown_kill_switch",
    "gate_broker_reconciliation",
    "gate_basket_capital_reservation",
]

_DIFFERENTIATED_GATES = {
    "gate_basket_capital_reservation",
    "gate_dispersion_score",
}


def _parse_gate_breakdown(breakdown_json: str | None) -> list[str]:
    """Parse gate_breakdown_json from risk_decisions table."""
    if not breakdown_json:
        return []
    try:
        items = json.loads(breakdown_json)
        return [str(item) for item in items]
    except Exception:
        return []


def _build_gate_stats(risk_decisions: list[RiskDecisionRow]) -> dict[str, dict[str, int]]:
    """Count PASS/RESIZE/VETO per gate across all decisions."""
    stats: dict[str, dict[str, int]] = {
        g: {"PASS": 0, "RESIZE": 0, "VETO": 0} for g in _GATE_NAMES
    }
    for rd in risk_decisions:
        items = _parse_gate_breakdown(rd.gate_breakdown_json)
        for item in items:
            parts = item.split(":", 2)
            if len(parts) >= 2:
                gate_name = parts[0]
                outcome = parts[1]
                if gate_name in stats and outcome in stats[gate_name]:
                    stats[gate_name][outcome] += 1
    return stats


def _dispersion_trend(screens: list[ScreenResultRow]) -> list[tuple[str, float]]:
    """Extract dispersion_score readings across cycles (ts, score)."""
    seen_cycles: dict[str, float] = {}
    for s in screens:
        if not s.metrics:
            continue
        try:
            metrics = json.loads(s.metrics) if isinstance(s.metrics, str) else s.metrics
        except Exception:
            continue
        score = metrics.get("dispersion_score")
        if score is not None and s.cycle_id not in seen_cycles:
            seen_cycles[s.cycle_id] = float(score)
    return [(cid, score) for cid, score in sorted(seen_cycles.items())]


def export_writeup(db_path: str | Path) -> str:
    """
    Render a markdown write-up from the journal DB.

    Writes the result to docs/writeup_generated.md and also returns it as a
    string so callers can pipe it to stdout or further processing.

    Covers all three hackathon criteria:
    1. AI logic (catalyst gate, structure agent) with real examples
    2. Risk gates — all 13 named with trigger counts; basket-reservation and
       dispersion gates called out as the differentiated mechanisms
    3. Alpaca infrastructure — MCP + CLI + alpaca-py usage

    Plus:
    - Dispersion score trend table across the trading week
    - Basket-atomicity fix as a named, specific defense paragraph

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
    gate_stats = _build_gate_stats(list(risk_decisions))
    dispersion_trend = _dispersion_trend(list(screens))

    # Veto reason breakdown
    veto_reasons: list[str] = []
    for rd in vetoed:
        items = _parse_gate_breakdown(rd.gate_breakdown_json)
        for item in items:
            if ":VETO:" in item:
                gate = item.split(":")[0]
                veto_reasons.append(gate)
    top_veto_reasons = Counter(veto_reasons).most_common(3)

    # Read account ID if available
    account_id_path = Path(db_path).parent / "account_id.txt"
    account_id = account_id_path.read_text().strip() if account_id_path.exists() else "NOT_SET"

    generated_at = datetime.now(UTC).isoformat()

    try:
        from barbell.config import get_settings
        starting_nav = get_settings().account.starting_nav
    except Exception:
        starting_nav = 100_000.0

    pnl = (latest_nav - starting_nav) if latest_nav else None
    pnl_pct = (pnl / starting_nav * 100) if pnl is not None else None

    lines: list[str] = [
        "# Dispersion Barbell — Trade Write-Up",
        "",
        f"> **Generated:** {generated_at}  ",
        f"> **Account ID:** {account_id}  ",
        f"> **DB path:** {db_path}",
        "",
        "---",
        "",
        "## Strategy Overview",
        "",
        "**Dispersion Barbell** is an autonomous options agent implementing a two-sleeve",
        "volatility barbell strategy for the Alpaca AI Trading Agents Hackathon.",
        "",
        "- **Sleeve A — Carry (short single-name vol):** Put credit spreads or iron condors",
        "  on high-IV, high-liquidity single-name equities. Profits when single-name IV",
        "  mean-reverts and the spread expires worthless or near worthless.",
        "",
        "- **Sleeve B — Convexity (long index vol):** SPY put debit spread entered on",
        "  Sep 3 after 14:30 ET and monetised into the Sep 4 NFP release open. Provides",
        "  convex upside if the market sells off sharply into the macro event.",
        "",
        "The core thesis: single-name implied volatility is structurally elevated relative",
        "to index IV (the dispersion trade), making it favourable to sell single-name",
        "premium while owning cheap index protection.",
        "",
        "---",
        "",
        "## Performance",
        "",
        f"| Metric | Value |",
        f"|---|---|",
        f"| Starting NAV | ${starting_nav:,.2f} |",
        f"| Latest NAV | {f'${latest_nav:,.2f}' if latest_nav else '_no snapshot_'} |",
        f"| P&L | {f'${pnl:+,.2f} ({pnl_pct:+.2f}%)' if pnl is not None else '_no data_'} |",
        f"| Filled orders | {len(filled_orders)} |",
        f"| Total orders | {len(orders)} |",
        f"| Kill switch | {'🔴 TRIGGERED' if kill_triggered else '🟢 Never triggered'} |",
        "",
        "---",
        "",
        "## AI Decision Logic",
        "",
        "Three AI-powered stages gate each candidate before any capital is committed:",
        "",
        "### Stage 1.5 — Headline Triage (Featherless AI / Open-Source LLM)",
        "",
        "Before the Claude catalyst gate, a lightweight open-source model (Qwen2.5-7B via",
        "Featherless AI) digests recent headlines for each screened symbol and produces a",
        "`HeadlineDigest` with a `news_volume` classification (low / normal / elevated) and",
        "a short summary. This call is informational only — a failure here returns an empty",
        "digest and never blocks the pipeline. It provides richer context to Claude's",
        "catalyst gate at minimal cost.",
        "",
        "### Stage 2 — Catalyst Gate (Claude — veto authority)",
        "",
        f"**Symbols evaluated this week:** {len(verdicts)}  ",
        f"**Catalyst risk flagged (veto):** {len(catalyst_blocked)}  ",
        "",
        "Claude determines whether elevated IV is explained by a *scheduled, already-priced*",
        "catalyst (earnings are announced, FDA date is set) versus an *unscheduled binary",
        "risk* that makes premium selling unreliable. Tool-use forces the response into the",
        "`CatalystVerdict` schema — if the API call fails or the response is malformed,",
        "the gate fails **closed** (`catalyst_risk=True`), blocking the trade.",
        "",
    ]

    if catalyst_blocked:
        lines += [
            "**Catalyst veto examples (real reasoning from this week's trading):**",
            "",
        ]
        for v in catalyst_blocked[:3]:
            lines.append(f"- **{v.symbol}** ({v.ts}):  ")
            lines.append(f"  > {v.reasoning[:200]}{'...' if len(v.reasoning) > 200 else ''}  ")
            lines.append("")

    lines += [
        "### Stage 3 — Structure Agent (Claude — sizes real risk)",
        "",
        f"**Structures proposed this week:** {len(structures)}  ",
        "",
        "Given the option chain, screen metrics (including the current `dispersion_score`),",
        "and Sleeve A config (spread width, delta range, DTE range), Claude proposes a",
        "concrete multi-leg structure as a validated `ProposedStructure`. The model's",
        "rationale references the dispersion score so the write-up is traceable.",
        "Tool-use enforces the schema; the function makes no broker calls (data-in, data-out).",
        "",
        "---",
        "",
        "## Risk Gate Audit",
        "",
        "All 13 deterministic gates run on every proposal in a fixed order. A single VETO",
        "stops evaluation immediately. Multiple RESIZEs take the minimum. The engine can",
        "only reduce contract count, never increase it — enforced by construction with `min()`.",
        "",
        "| # | Gate | PASS | RESIZE | VETO | Notes |",
        "|---|---|---|---|---|---|",
    ]

    gate_labels = {
        "gate_per_position_loss_cap":      "01 Per-position loss cap",
        "gate_portfolio_loss_cap":         "02 Portfolio loss cap",
        "gate_defined_risk_only":          "03 Defined-risk only (no naked shorts)",
        "gate_quote_staleness":            "04 Quote staleness",
        "gate_liquidity_floor":            "05 Liquidity floor",
        "gate_dispersion_score":           "06 **Dispersion score** ⭐",
        "gate_earnings_blackout":          "07 Earnings blackout",
        "gate_pre_nfp_flatten":            "08 Pre-NFP flatten",
        "gate_expiry_past_deadline":       "09 Expiry past deadline",
        "gate_concentration":              "10 Concentration",
        "gate_drawdown_kill_switch":       "11 Drawdown kill switch",
        "gate_broker_reconciliation":      "12 Broker reconciliation",
        "gate_basket_capital_reservation": "13 **Basket capital reservation** ⭐",
    }

    for gname in _GATE_NAMES:
        st = gate_stats[gname]
        label = gate_labels.get(gname, gname)
        note = "Differentiated mechanism — see below" if gname in _DIFFERENTIATED_GATES else ""
        lines.append(f"| {label} | {st['PASS']} | {st['RESIZE']} | {st['VETO']} | {note} |")

    if top_veto_reasons:
        lines += [
            "",
            "**Top veto reasons by gate:**",
            "",
        ]
        for gate, count in top_veto_reasons:
            lines.append(f"- `{gate}`: {count} veto(s)")

    lines += [
        "",
        "### ⭐ Differentiated Mechanism 1: Dispersion Score Gate",
        "",
        "Gate 06 computes a vega-weighted ratio of single-name IV to index (SPY) IV,",
        "called `dispersion_score`. This is not a standard position-sizing check — it",
        "gates whether the *market condition* (single names rich vs. index) actually",
        "supports Sleeve A's carry thesis at all. A score below `min_dispersion_score`",
        "(1.15) means the spread between single-name and index vol has collapsed, making",
        "the carry unfavorable regardless of individual position limits.",
        "",
        "**Design detail:** when `dispersion_score is None` (e.g. screen/metrics.py hasn't",
        "yet populated it), the gate returns PASS rather than VETO. This is intentional:",
        "VETO on None would silently disable all Sleeve A entries until Member 3's screen",
        "wired the value. The other 12 gates remain fully active in the interim.",
        "",
        "### ⭐ Differentiated Mechanism 2: Basket Capital Reservation Gate",
        "",
        "Gate 13 and the sequential basket execution architecture together solve a gap",
        "in naive multi-underlying option execution: **capital can be over-committed across",
        "sequential legs before earlier legs confirm filled.** Without this fix, a 3-name",
        "basket could reserve the full max-loss of each leg independently, while the broker",
        "only guarantees atomic fills within one underlying's multi-leg order, not across",
        "underlyings.",
        "",
        "The fix: before submitting the first leg of any basket, `execution/orders.py`",
        "writes a `capital_reservations` row with the **total** basket max-loss as",
        "`status='reserved'`. Gate 13 reads `portfolio_state.reserved_capital` and VETOs",
        "any proposal that would push `reserved + this_proposal_max_loss` over the",
        "portfolio loss cap. After all legs resolve, the reservation is released",
        "(`status='released'`). A process restart with an unreleased reservation is",
        "detected and refused until manually resolved — not silently forgotten.",
        "",
        "---",
        "",
        "## Dispersion Score Trend",
        "",
        "Dispersion score (`Σ w_i · IV_i / IV_index`, vega-weighted) across the trading week:",
        "",
        "| Cycle | Dispersion Score |",
        "|---|---|",
    ]

    if dispersion_trend:
        for cid, score in dispersion_trend[-20:]:
            lines.append(f"| {cid} | {score:.4f} |")
    else:
        lines.append("| — | _no data yet_ |")

    lines += [
        "",
        "_A score > 1.15 confirms the carry thesis: single-name IV is elevated relative to",
        "index IV. A score ≤ 1.15 triggers Gate 06 VETO, blocking new Sleeve A entries._",
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
        "## Technology Implementation (Alpaca Infrastructure)",
        "",
        "| Component | Role |",
        "|---|---|",
        "| `alpaca-py` SDK | All order construction and execution — the only module that imports alpaca-py is `broker/alpaca_client.py`. This isolation makes everything else testable without live API calls. |",
        "| `alpaca-mcp-server` | Interactive / demo path: Claude drives live session via MCP tool calls for real-time decision visibility during the demo video. |",
        "| `barbell` CLI | Headless / scheduler path: `barbell run-cycle` is what cron or systemd invokes. `barbell status` and `barbell flatten` are the two operational handles. |",
        "| `APScheduler` | Runs `run_one_cycle()` every 30 minutes during market hours. Single-instance job prevents overlapping cycles. |",
        "| SQLite journal | Append-only, 9 tables. Every pipeline stage writes — passes and rejections alike. This write-up is generated directly from the DB. |",
        "| GitHub Actions | CI: `pytest` on every push. Risk gates and schedule module have the highest coverage bar. |",
        "",
        "**Paper trading only.** `ALPACA_PAPER_TRADE=true` is enforced at the config layer",
        "and never toggled in any code path. `submit_mleg_order()` raises `NakedShortError`",
        "before any network call if a SELL leg isn't covered by a BUY leg same-expiry.",
        "",
        "---",
        "",
        "## Orders",
        "",
        "| Order ID | Symbol | Status | Fill Price | Timestamp |",
        "|---|---|---|---|---|",
    ]

    for o in orders[-20:]:
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
        "_This file is auto-generated by `journal/export.py`. Do not edit manually._",
        "",
    ]

    md = "\n".join(lines)

    # Write to docs/
    _WRITEUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _WRITEUP_PATH.write_text(md, encoding="utf-8")
    log.info("Writeup exported to %s (%d chars)", _WRITEUP_PATH, len(md))

    return md
