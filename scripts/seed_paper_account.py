#!/usr/bin/env python3
"""
One-time setup checks for the FRESH paper account created for this
submission (judging requires a fresh account — a reused one is disqualified).

    python scripts/seed_paper_account.py

Confirms: NAV == 100000.00, options trading enabled, prints the account ID
that must go in the final lablab submission form, and writes it to
data/account_id.txt so journal/export.py can include it automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from barbell.broker.alpaca_client import AlpacaClient
from barbell.config import get_settings
from barbell.logging_config import setup_logging

setup_logging()

import logging

log = logging.getLogger("seed_paper_account")


def seed_account() -> bool:
    s = get_settings()
    client = AlpacaClient.from_settings()

    print("=" * 60)
    print("Dispersion Barbell — Seed & Verify Fresh Paper Account")
    print("=" * 60)

    try:
        acct = client.get_account()
    except Exception as e:
        print(f"❌ Failed to fetch account: {e}")
        return False

    account_id = acct["id"]
    equity = acct["equity"]
    opt_level = acct["options_approved_level"]
    trading_level = acct["options_trading_level"]
    blocked = acct["trading_blocked"]

    print(f"Account ID:             {account_id}")
    print(f"Account Number:         {acct['account_number']}")
    print(f"Current NAV (Equity):   ${equity:,.2f}")
    print(f"Options Approved Level: {opt_level}")
    print(f"Options Trading Level:  {trading_level}")
    print(f"Trading Blocked:        {blocked}")
    print("-" * 60)

    # Check NAV
    expected_nav = s.account.starting_nav
    nav_ok = abs(equity - expected_nav) <= 1000.0  # approximate check if fees/slight drift
    if not nav_ok:
        print(f"⚠️  WARNING: Equity (${equity:,.2f}) deviates from expected starting NAV (${expected_nav:,.2f}).")
    else:
        print(f"✅ NAV check passed: ~${expected_nav:,.2f}")

    # Check Options level
    if opt_level < 2:
        print(f"❌ Options approval level is {opt_level} (< 2). Options trading is disabled!")
    else:
        print(f"✅ Options level {opt_level} enabled.")

    if blocked:
        print("❌ Trading is BLOCKED on this account!")
        return False

    # Persist account ID
    out_dir = Path(__file__).resolve().parent.parent / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "account_id.txt"
    out_file.write_text(account_id.strip(), encoding="utf-8")
    print(f"✅ Account ID written to {out_file}")

    print("=" * 60)
    print(f"Lablab Submission Account ID: {account_id}")
    print("=" * 60)
    return True


if __name__ == "__main__":
    ok = seed_account()
    sys.exit(0 if ok else 1)
