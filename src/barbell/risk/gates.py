"""
The twelve deterministic gates from config/settings.yaml, as pure functions:
    (proposed_structure, portfolio_state, market_state, config) -> GateResult

Each gate returns PASS, RESIZE(new_contracts), or VETO(reason) — never a
larger size than it was given. This file has the highest test-coverage bar
in the repo (tests/test_risk_gates.py) because it is the one thing standing
between an LLM output and real (paper) capital.

Gates implemented here: per-position loss cap, portfolio loss cap,
defined-risk-only, quote staleness, liquidity floor, earnings blackout,
pre-NFP flatten requirement, expiry-past-deadline rejection, concentration
(per-underlying, per-sector), drawdown kill-switch latch, slippage cap,
broker-state-reconciliation check.
"""
