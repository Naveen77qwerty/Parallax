"""
Pipeline Stage 2 — the first of two narrow LLM jobs (see docs/architecture.md
for why the LLM's role is split and kept out of order construction).

    check_catalyst(symbol: str, headlines: list[str], digest: HeadlineDigest | None) -> CatalystVerdict

Calls the Anthropic API with prompts/catalyst_gate.md, tool-forces a
CatalystVerdict-shaped response, and returns the parsed model. Anything that
fails schema validation is treated as catalyst_risk=True (fail closed, not open).

`digest` is the optional output of screen/headline_triage.py (Featherless,
open-source model) — included in the prompt as extra context ("a cheap
first pass flagged elevated news volume; verify why") but Claude alone
produces catalyst_risk. A missing digest changes nothing about this
function's behavior, by design.
"""
