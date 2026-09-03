"""
Entrypoint for the autonomous / scheduled path.

    barbell run-cycle          # one full screen->gate->propose->risk->execute pass
    barbell status              # positions, NAV, gate state, kill-switch state
    barbell flatten             # emergency: close everything, cancel open orders
    barbell verify              # runs scripts/verify_day1.py checks
    barbell journal export      # dumps docs/writeup_generated.md from the DB

This is the CLI half of the "use MCP or CLI" requirement: it is what a cron /
scheduler invokes headlessly. The MCP server (see broker/mcp_client.py notes)
is the interactive half, driven live in the demo.

Every subcommand is a thin wrapper — real logic lives in the modules below,
never here, so it stays testable without a subprocess.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from pathlib import Path


def _get_store_and_client():
    """Construct JournalStore + AlpacaClient from settings (shared by most commands)."""
    from barbell.broker.alpaca_client import AlpacaClient
    from barbell.config import get_settings
    from barbell.journal.store import JournalStore
    from barbell.logging_config import setup_logging

    setup_logging()
    s = get_settings()
    store = JournalStore(db_path=str(s.barbell_db_path))
    client = AlpacaClient.from_settings()
    return client, store, s


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def _cmd_run_cycle(args: argparse.Namespace) -> int:
    """Run one full screening cycle and exit."""
    client, store, _ = _get_store_and_client()
    from barbell.scheduler.loop import run_one_cycle

    cycle_id = str(uuid.uuid4())[:8]
    log = logging.getLogger(__name__)
    log.info("barbell run-cycle — cycle_id=%s", cycle_id)

    summary = run_one_cycle(cycle_id, client, store)

    print(
        f"\n[run-cycle complete]\n"
        f"  cycle_id : {summary['cycle_id']}\n"
        f"  phase    : {summary['phase']}\n"
        f"  survivors: {summary['survivors']}\n"
        f"  proposals: {summary['proposals']}\n"
        f"  pass/resize/veto: {summary['decisions_pass']}/{summary['decisions_resize']}/{summary['decisions_veto']}\n"
        f"  orders   : {summary['orders_submitted']}\n"
        f"  reconcile_diverged: {summary['reconcile_diverged']}"
    )
    return 1 if summary["reconcile_diverged"] else 0


def _cmd_status(args: argparse.Namespace) -> int:
    """Print current NAV, phase, positions, kill-switch state, basket reservation."""
    client, store, s = _get_store_and_client()

    from barbell.endgame.schedule import current_phase
    from barbell.journal.store import CapitalReservationRow
    from barbell.risk.kill_switch import is_latched
    from sqlmodel import Session, create_engine, select

    # Phase
    try:
        phase = current_phase()
        phase_str = phase.name
    except Exception as e:
        phase_str = f"UNKNOWN ({e})"

    # Account
    try:
        account = client.get_account()
        nav = float(account.get("equity", 0))
        buying_power = float(account.get("buying_power", 0))
    except Exception as e:
        nav, buying_power = 0.0, 0.0
        print(f"[warning] Could not fetch account: {e}")

    # Positions
    try:
        positions = client.get_positions()
    except Exception as e:
        positions = []
        print(f"[warning] Could not fetch positions: {e}")

    # Kill switch
    try:
        kill_latched = is_latched(store=store)
    except Exception:
        kill_latched = False

    # Reserved capital (in-flight basket)
    reserved_capital = 0.0
    try:
        engine = create_engine(f"sqlite:///{s.barbell_db_path}", connect_args={"check_same_thread": False})
        with Session(engine) as session:
            open_reservations = session.exec(
                select(CapitalReservationRow).where(CapitalReservationRow.status == "reserved")  # type: ignore[arg-type]
            ).all()
            reserved_capital = sum(float(r.reserved_amount) for r in open_reservations)
    except Exception:
        pass

    print(f"""
╔══════════════════════════════════════════════════════╗
║          DISPERSION BARBELL — STATUS                 ║
╚══════════════════════════════════════════════════════╝
  Phase          : {phase_str}
  NAV            : ${nav:,.2f}
  Starting NAV   : ${s.account.starting_nav:,.2f}
  Buying power   : ${buying_power:,.2f}
  Kill switch    : {'🔴 LATCHED' if kill_latched else '🟢 clear'}
  Reserved cap.  : ${reserved_capital:,.2f} {'(basket in-flight)' if reserved_capital > 0 else ''}
  Open positions : {len(positions)}
""")
    for p in positions:
        sym = p.get("symbol", "?")
        qty = p.get("qty", "?")
        pnl = p.get("unrealized_pl", "?")
        print(f"    {sym:30s}  qty={qty}  unrealized_pl={pnl}")

    return 0


def _cmd_flatten(args: argparse.Namespace) -> int:
    """Emergency: force-close all open positions regardless of phase."""
    import logging
    log = logging.getLogger(__name__)

    client, store, s = _get_store_and_client()

    log.critical(
        "barbell flatten MANUALLY INVOKED — force-closing all positions. "
        "This is an emergency override, not a normal exit."
    )
    print("⚠️  EMERGENCY FLATTEN — closing all open positions...")

    try:
        positions = client.get_positions()
    except Exception as e:
        print(f"[error] Could not fetch positions: {e}")
        return 1

    if not positions:
        print("No open positions to close.")
        return 0

    closed = 0
    failed = 0
    for pos in positions:
        sym = pos.get("symbol", "?")
        qty = pos.get("qty_available", pos.get("qty", 0))
        try:
            # Close by submitting a closing order via the broker
            client._trading_client.close_position(sym)  # type: ignore[attr-defined]
            log.critical("FLATTEN: closed position %s (qty=%s)", sym, qty)
            print(f"  ✓ Closed {sym} (qty={qty})")
            closed += 1
        except Exception as e:
            log.error("FLATTEN: failed to close %s: %s", sym, e)
            print(f"  ✗ Failed {sym}: {e}")
            failed += 1

    print(f"\nFlatten complete: {closed} closed, {failed} failed.")
    return 0 if failed == 0 else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run the Day-1 verification checklist (scripts/verify_day1.py)."""
    import importlib.util

    repo_root = Path(__file__).resolve().parent.parent.parent
    verify_path = repo_root / "scripts" / "verify_day1.py"

    if not verify_path.exists():
        print(f"[error] {verify_path} not found")
        return 1

    spec = importlib.util.spec_from_file_location("verify_day1", verify_path)
    if spec is None or spec.loader is None:
        print("[error] Could not load verify_day1.py")
        return 1

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    if hasattr(module, "main"):
        return module.main() or 0
    elif hasattr(module, "run_checks"):
        return module.run_checks() or 0
    else:
        print("[info] verify_day1.py loaded — no main()/run_checks() found, check output above")
        return 0


def _cmd_journal_export(args: argparse.Namespace) -> int:
    """Export write-up and trade log CSV from the journal DB."""
    client, store, s = _get_store_and_client()

    from barbell.journal.export import export_trade_log_csv, export_writeup

    db_path = s.barbell_db_path
    print(f"Exporting from {db_path}…")

    # Write-up
    writeup = export_writeup(db_path)
    writeup_path = Path(__file__).resolve().parent.parent.parent / "docs" / "writeup_generated.md"
    print(f"  ✓ Write-up: {writeup_path} ({len(writeup)} chars)")

    # CSV
    csv_str = export_trade_log_csv(db_path)
    csv_path = db_path.parent / "trade_log.csv"
    csv_path.write_text(csv_str, encoding="utf-8")
    print(f"  ✓ Trade log: {csv_path}")

    return 0


# ---------------------------------------------------------------------------
# Argument parser + dispatch
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="barbell",
        description="Dispersion Barbell — autonomous options agent CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run-cycle", help="Run one full cycle and exit")
    sub.add_parser("status", help="Print NAV, phase, positions, kill-switch state")
    sub.add_parser("flatten", help="Emergency: force-close all open positions")
    sub.add_parser("verify", help="Run scripts/verify_day1.py checks")

    journal_parser = sub.add_parser("journal", help="Journal management")
    journal_sub = journal_parser.add_subparsers(dest="journal_command", required=True)
    journal_sub.add_parser("export", help="Export write-up and trade log CSV")

    args = parser.parse_args()

    dispatch = {
        "run-cycle": _cmd_run_cycle,
        "status": _cmd_status,
        "flatten": _cmd_flatten,
        "verify": _cmd_verify,
    }

    if args.command == "journal":
        if args.journal_command == "export":
            sys.exit(_cmd_journal_export(args))
        else:
            parser.error(f"Unknown journal subcommand: {args.journal_command}")
    elif args.command in dispatch:
        sys.exit(dispatch[args.command](args))
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
