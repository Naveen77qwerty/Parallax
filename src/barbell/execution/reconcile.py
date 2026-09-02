"""
Runs at the start and end of every cycle. Pulls positions + buying power
from AlpacaClient (the broker is always the source of truth — see gate G12
in docs) and diffs against internal state in the journal DB.

    reconcile() -> ReconciliationReport

Any divergence (a position the DB doesn't know about, or vice versa) halts
new entries for that cycle and logs a CRITICAL journal entry. This is what
catches a partial fill, a missed webhook-equivalent, or a manual trade made
outside the agent during the demo.
"""
