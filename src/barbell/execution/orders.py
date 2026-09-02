"""
Builds and submits the actual multi-leg order once risk/engine.py has
returned PASS or RESIZE. This is the only module that calls
AlpacaClient.submit_mleg_order.

    submit(decision: RiskDecision, structure: ProposedStructure) -> OrderResult

Enforces, in code (not just config), that order_type is always "limit":
there is no code path here that can construct a market order for an option,
regardless of what config/settings.yaml says. Retries with a widening limit
up to order_retry_limit times (config), then abandons and logs — never
chases a fill past order_retry_widen_pct.
"""
