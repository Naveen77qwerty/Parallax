"""
Export key stats from the journal DB into a clean JSON + text block for
use in presentation slides and the hackathon write-up.

Usage:
    python scripts/export_slide_stats.py
    python scripts/export_slide_stats.py --db path/to/barbell.db
    python scripts/export_slide_stats.py --json   # JSON output only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

# Make sure the src/ package is importable when run directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))


def _resolve_db(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("BARBELL_DB_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
    for candidate in [
        _REPO_ROOT / "data" / "barbell.db",
        _REPO_ROOT / "barbell.db",
    ]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Journal DB not found. Set BARBELL_DB_PATH or pass --db path/to/barbell.db"
    )


def collect_stats(db_path: Path) -> dict:
    """Pull all stats needed for slides from the journal DB."""
    from sqlmodel import Session, create_engine, select

    from barbell.journal.store import (
        CatalystVerdictRow,
        KillSwitchEventRow,
        OrderRow,
        PositionsSnapshotRow,
        RiskDecisionRow,
        ScreenResultRow,
    )

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    with Session(engine) as session:
        orders = session.exec(select(OrderRow).order_by(OrderRow.ts)).all()
        snapshots = session.exec(
            select(PositionsSnapshotRow).order_by(PositionsSnapshotRow.ts.desc())
        ).all()
        risk_decisions = session.exec(select(RiskDecisionRow)).all()
        verdicts = session.exec(select(CatalystVerdictRow)).all()
        screens = session.exec(select(ScreenResultRow)).all()
        kill_events = session.exec(select(KillSwitchEventRow)).all()

    # --- NAV ---
    try:
        from barbell.config import get_settings
        starting_nav = get_settings().account.starting_nav
    except Exception:
        starting_nav = 100_000.0

    current_nav = float(snapshots[0].nav) if snapshots else starting_nav
    pnl = current_nav - starting_nav
    pnl_pct = pnl / starting_nav * 100

    # --- Per-sleeve P&L (from filled orders) ---
    sleeve_pnl: dict[str, float] = {"A": 0.0, "B": 0.0}
    filled_orders = [o for o in orders if o.status == "filled" and o.fill_price]
    for o in filled_orders:
        sleeve = "B" if (o.sleeve or "").upper() == "B" else "A"
        legs = json.loads(o.legs_json) if o.legs_json else []
        for leg in legs:
            side = leg.get("side", "")
            price = float(leg.get("fill_price", 0) or 0)
            contracts = int(leg.get("contracts", 1) or 1)
            if side == "sell":
                sleeve_pnl[sleeve] += price * contracts * 100
            elif side == "buy":
                sleeve_pnl[sleeve] -= price * contracts * 100

    # --- Gate rejection counts ---
    total_decisions = len(risk_decisions)
    vetoed = [r for r in risk_decisions if r.outcome == "VETO"]
    resized = [r for r in risk_decisions if r.outcome == "RESIZE"]
    passed = [r for r in risk_decisions if r.outcome == "PASS"]

    gate_veto_counter: Counter = Counter()
    for rd in vetoed:
        if rd.gate_breakdown_json:
            try:
                items = json.loads(rd.gate_breakdown_json)
                for item in items:
                    parts = str(item).split(":", 2)
                    if len(parts) >= 2 and parts[1] == "VETO":
                        gate_veto_counter[parts[0]] += 1
            except Exception:
                pass

    top_rejection_reasons = [
        {"gate": gate, "count": count}
        for gate, count in gate_veto_counter.most_common(3)
    ]

    # --- Catalyst gate ---
    catalyst_vetoes = sum(1 for v in verdicts if v.catalyst_risk)
    catalyst_total = len(verdicts)

    # --- Dispersion score (latest) ---
    latest_dispersion: float | None = None
    for s in screens:
        if not s.metrics:
            continue
        try:
            m = json.loads(s.metrics) if isinstance(s.metrics, str) else s.metrics
            score = m.get("dispersion_score")
            if score is not None:
                latest_dispersion = float(score)
                break
        except Exception:
            pass

    # --- Kill switch ---
    kill_triggered = any(k.triggered for k in kill_events)

    return {
        "starting_nav": starting_nav,
        "current_nav": current_nav,
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 4),
        "sleeve_a_pnl": round(sleeve_pnl["A"], 2),
        "sleeve_b_pnl": round(sleeve_pnl["B"], 2),
        "total_orders": len(orders),
        "filled_orders": len(filled_orders),
        "risk_decisions_total": total_decisions,
        "risk_decisions_pass": len(passed),
        "risk_decisions_resize": len(resized),
        "risk_decisions_veto": len(vetoed),
        "gate_rejection_count": len(vetoed),
        "top_rejection_reasons": top_rejection_reasons,
        "catalyst_gate_evaluated": catalyst_total,
        "catalyst_gate_vetoes": catalyst_vetoes,
        "kill_switch_triggered": kill_triggered,
        "latest_dispersion_score": latest_dispersion,
        "db_path": str(db_path),
    }


def format_text(stats: dict) -> str:
    """Render a human-readable text block for copy-paste into slides."""
    disp = (
        f"{stats['latest_dispersion_score']:.3f}"
        if stats["latest_dispersion_score"] is not None
        else "N/A"
    )
    top_reasons = ", ".join(
        f"{r['gate']} ({r['count']})" for r in stats["top_rejection_reasons"]
    ) or "N/A"

    return f"""
╔══════════════════════════════════════════════════════════════╗
║         DISPERSION BARBELL — SLIDE STATS                     ║
╚══════════════════════════════════════════════════════════════╝

  PERFORMANCE
  ───────────
  Starting NAV   : ${stats['starting_nav']:>12,.2f}
  Current NAV    : ${stats['current_nav']:>12,.2f}
  Total P&L      : ${stats['pnl_usd']:>+12,.2f}  ({stats['pnl_pct']:+.2f}%)
  Sleeve A P&L   : ${stats['sleeve_a_pnl']:>+12,.2f}
  Sleeve B P&L   : ${stats['sleeve_b_pnl']:>+12,.2f}

  TRADING ACTIVITY
  ────────────────
  Orders submitted : {stats['total_orders']}
  Orders filled    : {stats['filled_orders']}
  Kill switch      : {'TRIGGERED ⚠️' if stats['kill_switch_triggered'] else 'Never triggered ✓'}

  AI DECISION PIPELINE
  ────────────────────
  Catalyst gate evaluated  : {stats['catalyst_gate_evaluated']}
  Catalyst gate vetoes     : {stats['catalyst_gate_vetoes']}
  Risk engine decisions    : {stats['risk_decisions_total']}
    → PASS                 : {stats['risk_decisions_pass']}
    → RESIZE               : {stats['risk_decisions_resize']}
    → VETO                 : {stats['risk_decisions_veto']}
  Top rejection reasons    : {top_reasons}
  Latest dispersion score  : {disp}

  DB: {stats['db_path']}
"""


def main():
    parser = argparse.ArgumentParser(
        description="Export Dispersion Barbell stats for slides"
    )
    parser.add_argument("--db", help="Path to barbell.db (overrides BARBELL_DB_PATH)")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args()

    try:
        db_path = _resolve_db(args.db)
    except FileNotFoundError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)

    stats = collect_stats(db_path)

    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        print(format_text(stats))
        print("\n--- JSON ---\n")
        print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
