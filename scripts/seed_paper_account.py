#!/usr/bin/env python3
"""
One-time setup checks for the FRESH paper account created for this
submission (judging requires a fresh account — a reused one is disqualified).

    python scripts/seed_paper_account.py

Confirms: NAV == 100000.00, options trading enabled, prints the account ID
that must go in the final lablab submission form, and writes it to
data/account_id.txt so journal/export.py can include it automatically.
"""
