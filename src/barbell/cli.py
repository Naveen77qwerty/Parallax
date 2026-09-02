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
import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="barbell")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run-cycle")
    sub.add_parser("status")
    sub.add_parser("flatten")
    sub.add_parser("verify")
    journal = sub.add_parser("journal")
    journal.add_subparsers(dest="journal_command")

    args = parser.parse_args()
    raise NotImplementedError(f"TODO: dispatch {args.command}")


if __name__ == "__main__":
    main()
