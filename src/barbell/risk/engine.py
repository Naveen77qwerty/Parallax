"""
Orchestrates gates.py in a fixed order and folds the results into a single
decision. This is the module referenced as "the risk engine can only
tighten, never loosen" — evaluate() below has no code path that increases
`contracts` or clears a VETO once set.

    evaluate(proposed: ProposedStructure, portfolio_state, market_state) -> RiskDecision
        RiskDecision = PASS(as-is) | RESIZE(smaller) | REJECT(reason)

Every call is logged to journal/store.py regardless of outcome — a REJECT
is exactly as important to the write-up as a PASS.
"""
