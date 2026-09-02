#!/usr/bin/env python3
"""
The seven-item Day-1 verification checklist from the strategy design,
made executable. Run this against the live paper API before writing or
trusting any strategy code — several downstream design choices (credit
spreads vs. cash-secured puts, Greeks source, staleness gate threshold)
branch on what this script finds.

    python scripts/verify_day1.py

Checks, each printing PASS/FAIL/UNKNOWN + evidence:
    1. Is the fresh paper account Level 3 (multi-leg) by default?
    2. Does a 1-lot single-name put credit spread actually fill?
    3. Does get_option_snapshot return Greeks + IV on the Basic data plan?
    4. How stale are indicative quotes vs. wall clock (sets gate G4)?
    5. Do SPX/XSP return market data quotes yet?
    6. How are multi-leg fills simulated — mid, far touch, or rejected?
    7. What are the real per-minute rate limits scanning ~25 chains?
"""
