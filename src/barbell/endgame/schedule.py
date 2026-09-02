"""
The dated state machine referenced throughout docs/architecture.md as the
"deadline-aware exit logic" — the part of the design that isn't required by
the hackathon rules but is arguably the most load-bearing piece of it.

    current_phase(clock: BrokerClock) -> Phase
        Phase = BUILD | CARRY_ACTIVE | UNWIND | CONVEXITY_ENTRY | HOLD_THROUGH_NFP
              | MONETIZE | FLAT | POST_DEADLINE

    allowed_actions(phase: Phase) -> set[str]
        e.g. CARRY_ACTIVE allows new Sleeve A entries; UNWIND allows only closes;
        FLAT allows nothing but reads.

run-cycle calls current_phase() first, every cycle, before doing anything
else — every other module receives the phase as a constraint, not a
suggestion. This is what guarantees the account is flat and fully realized
before scripts/verify submission, independent of what any LLM call returns
that day.
"""
