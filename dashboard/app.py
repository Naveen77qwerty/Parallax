"""
Streamlit dashboard reading directly from data/barbell.db (read-only) —
optional but cheap, and useful for both the demo video and live monitoring
during the window. Not on the order-submission path; deleting this file
changes nothing about how the agent trades.

    streamlit run dashboard/app.py

Shows: NAV vs. starting $100k, Sleeve A vs. Sleeve B P&L, open positions
table, live gate-decision feed (last N risk_decisions rows), kill-switch
status, and countdown to the Sep 4 11:00 ET submission deadline.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

# ---------------------------------------------------------------------------
# Page config — must be first Streamlit call
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Dispersion Barbell — Live Dashboard",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# DB path resolution
# ---------------------------------------------------------------------------


def _resolve_db() -> Path | None:
    """Find the journal DB — from env var or by searching common locations."""
    db_env = os.environ.get("BARBELL_DB_PATH")
    if db_env and Path(db_env).exists():
        return Path(db_env)
    # Fallback search relative to repo root
    candidates = [
        Path(__file__).resolve().parent.parent / "data" / "barbell.db",
        Path(__file__).resolve().parent.parent / "barbell.db",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


DB_PATH = _resolve_db()

# ---------------------------------------------------------------------------
# Data loading helpers (read-only SQLite, cached for performance)
# ---------------------------------------------------------------------------


@st.cache_resource
def _get_engine(db_path: str):
    from sqlalchemy import create_engine as _ce
    return _ce(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})


def _load_data(db_path: Path):
    """Load all relevant journal tables into dicts for display."""
    from sqlmodel import Session, select
    from barbell.journal.store import (
        BasketLegFillRow, CapitalReservationRow, KillSwitchEventRow,
        OrderRow, PositionsSnapshotRow, RiskDecisionRow, ScreenResultRow,
    )

    engine = _get_engine(str(db_path))
    with Session(engine) as session:
        orders = session.exec(select(OrderRow).order_by(OrderRow.ts.desc()).limit(100)).all()  # type: ignore[attr-defined]
        snapshots = session.exec(select(PositionsSnapshotRow).order_by(PositionsSnapshotRow.ts.desc())).all()  # type: ignore[attr-defined]
        kill_events = session.exec(select(KillSwitchEventRow)).all()
        risk_decisions = session.exec(select(RiskDecisionRow).order_by(RiskDecisionRow.ts.desc()).limit(50)).all()  # type: ignore[attr-defined]
        reservations = session.exec(select(CapitalReservationRow).order_by(CapitalReservationRow.ts.desc()).limit(20)).all()  # type: ignore[attr-defined]
        screens = session.exec(select(ScreenResultRow).order_by(ScreenResultRow.ts.desc()).limit(200)).all()  # type: ignore[attr-defined]

    return {
        "orders": orders,
        "snapshots": snapshots,
        "kill_events": kill_events,
        "risk_decisions": risk_decisions,
        "reservations": reservations,
        "screens": screens,
    }


# ---------------------------------------------------------------------------
# Derived stats
# ---------------------------------------------------------------------------


def _latest_nav(data: dict) -> float | None:
    snaps = data["snapshots"]
    return float(snaps[0].nav) if snaps else None


def _kill_latched(data: dict) -> bool:
    return any(k.triggered for k in data["kill_events"])


def _latest_dispersion_score(data: dict) -> float | None:
    for row in data["screens"]:
        if not row.metrics:
            continue
        try:
            m = json.loads(row.metrics) if isinstance(row.metrics, str) else row.metrics
            score = m.get("dispersion_score")
            if score is not None:
                return float(score)
        except Exception:
            continue
    return None


def _open_reservation(data: dict) -> float:
    for r in data["reservations"]:
        if r.status == "reserved":
            return float(r.reserved_amount)
    return 0.0


def _pnl_by_sleeve(data: dict) -> dict[str, float]:
    pnl: dict[str, float] = {"A": 0.0, "B": 0.0, "unknown": 0.0}
    for o in data["orders"]:
        if o.status != "filled" or not o.fill_price:
            continue
        legs = json.loads(o.legs_json) if o.legs_json else []
        sleeve_key = "A" if "A" in (o.symbol or "") else ("B" if "B" in (o.symbol or "") else "unknown")
        # Simple attribution: credit from sells, debit from buys
        for leg in legs:
            side = leg.get("side", "")
            price = float(leg.get("fill_price", 0) or 0)
            contracts = int(leg.get("contracts", 1) or 1)
            multiplier = 100
            if side == "sell":
                pnl[sleeve_key] += price * contracts * multiplier
            elif side == "buy":
                pnl[sleeve_key] -= price * contracts * multiplier
    return pnl


def _nav_history(data: dict) -> list[tuple[datetime, float]]:
    return [
        (snap.ts, float(snap.nav))
        for snap in reversed(data["snapshots"])
        if snap.ts and snap.nav
    ]


# ---------------------------------------------------------------------------
# Phase from live settings
# ---------------------------------------------------------------------------


def _current_phase_str() -> str:
    try:
        from barbell.endgame.schedule import current_phase
        return current_phase().name
    except Exception as e:
        return f"UNKNOWN ({e})"


def _deadline_countdown() -> str:
    try:
        from barbell.config import get_settings
        cfg = get_settings().calendar
        deadline = cfg.submission_deadline_et
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=ET)
        else:
            deadline = deadline.astimezone(ET)
        remaining = deadline - datetime.now(tz=ET)
        if remaining.total_seconds() < 0:
            return "⏱ DEADLINE PASSED"
        h, rem = divmod(int(remaining.total_seconds()), 3600)
        m, s = divmod(rem, 60)
        return f"⏱ {h:02d}:{m:02d}:{s:02d} until submission deadline"
    except Exception as e:
        return f"(deadline unknown: {e})"


# ---------------------------------------------------------------------------
# Main dashboard layout
# ---------------------------------------------------------------------------


def main():
    # --- Sidebar ---
    st.sidebar.title("🎯 Dispersion Barbell")
    st.sidebar.caption("Live monitoring dashboard — read-only")

    refresh_interval = st.sidebar.slider(
        "Auto-refresh (seconds)", min_value=10, max_value=120, value=30, step=10
    )

    if DB_PATH is None:
        st.error(
            "**Journal DB not found.**  \n"
            "Set `BARBELL_DB_PATH` environment variable or run `streamlit run dashboard/app.py` "
            "from the repo root after creating the DB with `barbell run-cycle`."
        )
        st.stop()

    st.sidebar.success(f"DB: `{DB_PATH.name}`")

    try:
        data = _load_data(DB_PATH)
    except Exception as e:
        st.error(f"Failed to load journal DB: {e}")
        st.stop()

    # --- Header row ---
    col_title, col_deadline = st.columns([3, 2])
    with col_title:
        st.title("Dispersion Barbell — Live Dashboard")
    with col_deadline:
        st.info(_deadline_countdown())

    # --- Key metrics row ---
    nav = _latest_nav(data)
    kill = _kill_latched(data)
    phase_str = _current_phase_str()
    disp_score = _latest_dispersion_score(data)
    reserved = _open_reservation(data)

    try:
        from barbell.config import get_settings
        starting_nav = get_settings().account.starting_nav
    except Exception:
        starting_nav = 100_000.0

    pnl_total = (nav - starting_nav) if nav is not None else None
    pnl_pct = (pnl_total / starting_nav * 100) if pnl_total is not None else None

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric(
        "Current NAV",
        f"${nav:,.2f}" if nav else "—",
        delta=f"{pnl_pct:+.2f}%" if pnl_pct is not None else None,
    )
    m2.metric("Starting NAV", f"${starting_nav:,.0f}")
    m3.metric("Phase", phase_str)
    m4.metric(
        "Kill Switch",
        "🔴 LATCHED" if kill else "🟢 Clear",
    )
    m5.metric(
        "Dispersion Score",
        f"{disp_score:.3f}" if disp_score is not None else "—",
        delta=f"{'✓ above floor' if disp_score and disp_score >= 1.15 else '✗ below floor'}" if disp_score else None,
    )

    # --- NAV history chart ---
    st.subheader("NAV History")
    nav_history = _nav_history(data)
    if nav_history:
        import pandas as pd
        df_nav = pd.DataFrame(nav_history, columns=["ts", "nav"])
        df_nav = df_nav.set_index("ts")
        df_nav["Starting NAV"] = starting_nav
        st.line_chart(df_nav)
    else:
        st.info("No NAV snapshots yet — run a cycle first.")

    # --- P&L by sleeve ---
    st.subheader("P&L by Sleeve (Filled Orders)")
    pnl_sleeves = _pnl_by_sleeve(data)
    c1, c2 = st.columns(2)
    c1.metric("Sleeve A (Carry)", f"${pnl_sleeves['A']:+,.2f}")
    c2.metric("Sleeve B (Convexity)", f"${pnl_sleeves['B']:+,.2f}")

    # --- Basket reservation status ---
    if reserved > 0:
        st.warning(
            f"🔒 **Basket in-flight:** ${reserved:,.2f} capital reserved "
            "(unreleased reservation in `capital_reservations` table)"
        )

    # --- Recent risk decisions ---
    st.subheader("Recent Risk Decisions")
    rd_rows = data["risk_decisions"]
    if rd_rows:
        import pandas as pd
        rows = []
        for rd in rd_rows[:20]:
            rows.append({
                "Symbol": rd.symbol,
                "Outcome": rd.outcome,
                "Contracts": rd.contracts or "—",
                "Reason": (rd.reason or "")[:80],
                "Timestamp": rd.ts,
            })
        df_rd = pd.DataFrame(rows)

        def _color_outcome(val: str) -> str:
            if val == "VETO":
                return "color: red; font-weight: bold"
            if val == "RESIZE":
                return "color: orange"
            if val == "PASS":
                return "color: green"
            return ""

        st.dataframe(
            df_rd.style.applymap(_color_outcome, subset=["Outcome"]),
            use_container_width=True,
        )
    else:
        st.info("No risk decisions recorded yet.")

    # --- Open positions ---
    st.subheader("Open Positions (from latest snapshot)")
    if data["snapshots"]:
        latest_snap = data["snapshots"][0]
        try:
            pos_data = json.loads(latest_snap.positions_json) if latest_snap.positions_json else []
            if pos_data:
                import pandas as pd
                st.dataframe(pd.DataFrame(pos_data), use_container_width=True)
            else:
                st.info("No open positions in latest snapshot.")
        except Exception as e:
            st.warning(f"Could not parse positions: {e}")
    else:
        st.info("No position snapshots yet.")

    # --- Recent orders ---
    st.subheader("Recent Orders")
    if data["orders"]:
        import pandas as pd
        order_rows = []
        for o in data["orders"][:15]:
            order_rows.append({
                "Order ID": str(o.order_id)[:8] + "…",
                "Symbol": o.symbol,
                "Status": o.status,
                "Fill Price": f"${o.fill_price:.4f}" if o.fill_price else "—",
                "Timestamp": o.ts,
            })
        st.dataframe(pd.DataFrame(order_rows), use_container_width=True)
    else:
        st.info("No orders recorded yet.")

    # --- Footer with refresh ---
    st.caption(f"Last refreshed: {datetime.now(ET).strftime('%H:%M:%S ET')} — auto-refreshing every {refresh_interval}s")
    time.sleep(refresh_interval)
    st.rerun()


if __name__ == "__main__" or True:
    main()
