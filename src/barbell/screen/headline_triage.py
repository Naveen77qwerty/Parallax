"""
Optional Stage 1.5 — a cheap, non-critical pre-digest of headlines for each
numeric-screen survivor, served by an open-source model through Featherless
AI (hackathon technology partner: OpenAI-compatible endpoint, $25/participant
credit). This is the one place that model is used, and deliberately so:

    digest_headlines(symbol: str, headlines: list[str]) -> HeadlineDigest

It flags rough news volume and produces a short summary — nothing more. The
output is fed as EXTRA CONTEXT into agent/catalyst_gate.py's prompt; it does
not itself decide catalyst_risk, does not gate which symbols proceed, and is
never seen by risk/engine.py. If this call fails, times out, or returns
something that doesn't parse into HeadlineDigest, the pipeline proceeds with
an empty digest — this stage can only add color, never block a cycle.

Rationale for keeping it this narrow: the catalyst gate is the one LLM call
in the pipeline with real veto power, and that stays on Claude, whose
structured-output reliability the rest of the safety design depends on.
Featherless is used where a less-proven model costs nothing if it's wrong —
bulk, low-stakes summarization — which is also genuinely representative of
what Featherless is for (cheap open-source inference in an agent workflow),
rather than a token integration bolted on for eligibility.

Config: FEATHERLESS_API_KEY, FEATHERLESS_BASE_URL (https://api.featherless.ai/v1),
FEATHERLESS_MODEL in .env. Uses the standard `openai` SDK client pointed at
Featherless's OpenAI-compatible endpoint — no separate SDK dependency.
"""
