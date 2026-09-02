"""
Deterministic Stage 1 of the pipeline (see docs/architecture.md). Pure
arithmetic on chain/bar data — no LLM call happens in this module, on purpose:
it's cheap, reproducible, and exactly reproducible in a demo ("run it twice,
get the same shortlist given the same market snapshot").

    load_candidates() -> from config/universe.yaml
    screen(candidates: list[str]) -> list[ScreenResult]
        applies, in order: liquidity floor (OI, spread% of mid), IV rank > 50,
        IV30/HV20 > min ratio, earnings blackout, price band.
        Every rejection is logged with its specific reason (journal/store.py) —
        "why didn't X get traded" is journal-legible, not silent.

Typically narrows ~25 seed names to 8-14 survivors on a real day; if it
returns 0, run-cycle should skip Sleeve A that cycle rather than relax the
screen — the screen thresholds are risk-relevant, not just filters.
"""
