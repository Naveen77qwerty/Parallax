# Dispersion Barbell — project invariants

Autonomous options agent for the Alpaca AI Trading Agents Hackathon. Full
design: docs/architecture.md. Full 4-person build plan: docs/TEAM_BUILD_PROMPTS.md.

## Non-negotiable, across every member's work

- PAPER TRADING ONLY. Never write code that could plausibly submit to a live
  account. `ALPACA_PAPER_TRADE` must stay `true` everywhere it appears.
- The risk engine (`risk/engine.py`) may only PASS, RESIZE DOWN, or REJECT a
  proposed trade. No code path may ever increase a proposed size or clear a
  veto. This is tested by a property test in tests/test_risk_gates.py and
  that test must never be weakened to make a feature pass.
- Options orders are LIMIT ONLY, never MARKET. Enforced in execution/orders.py
  in code, not just in config/settings.yaml.
- Every short option leg must be covered within the same multi-leg order —
  no naked shorts, ever, at any level.
- A basket spanning more than one underlying (Sleeve A's carry basket) is
  entered **sequentially, one underlying at a time**, with the full
  basket's max-loss capital **reserved in the journal before the first leg
  is submitted**, and portfolio risk **recomputed after every fill**, not
  just at basket start. Alpaca only guarantees atomic fills within one
  underlying's multi-leg order, never across underlyings — this is the fix
  for that gap and it is not optional.
- broker/alpaca_client.py is the ONLY module allowed to import alpaca-py.
  Everything else calls it through its interface so it stays mockable.
- agent/catalyst_gate.py and agent/structure_agent.py are the only two
  places an LLM decision has real authority (veto / sizing). Any other LLM
  call (e.g. screen/headline_triage.py) is informational only and must
  degrade to an empty/neutral result on failure — never block the pipeline.
- Every pipeline stage (screen, catalyst check, structure proposal, risk
  decision, order, reconciliation, capital reservation/release) writes to
  journal/store.py — passes and rejections both, always with a reason.
- Config values (gate thresholds, sizing, calendar dates) live in
  config/settings.yaml and config/universe.yaml. Don't hardcode numbers in
  Python that already have a home in config.
- **Feature Admission Protocol.** Don't add anything to `agent/`, `risk/`,
  or `execution/` beyond what your prompt below specifies without it
  answering, in one sentence each: (1) what specific edge or failure mode
  it addresses, (2) whether it fails closed automatically on error, (3) how
  it's removed with a single-point change if it turns out not to be worth
  it. If you think something's missing from your prompt, say so and ask
  rather than freelancing it in.
- Prefer small, directly testable functions. Anything touching money sizing
  or order construction needs a unit test in the same commit that adds it.

## Stack

Python 3.11+, alpaca-py, google-genai SDK (Gemini — catalyst_gate.py +
structure_agent.py, the two decisions with real veto/sizing power), openai
SDK (pointed at Featherless for screen/headline_triage.py only), pydantic
v2, sqlmodel, APScheduler, pyyaml + python-dotenv, pytest + freezegun,
Streamlit (dashboard, optional).

## Style

Type-hint everything. Docstrings already exist on every stub file in this
repo — read the existing docstring before writing a function; it states the
exact contract. Don't change a function signature a docstring specifies
without saying why, and if you do change one, tell the next member in your
handoff notes (see the end of each block below).
