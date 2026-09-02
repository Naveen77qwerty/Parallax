# Dispersion Barbell — 4-Person Build Prompts

Same project, same scaffold, same 15-phase plan as `docs/PROMPTS.md` — but
regrouped into **4 sequential handoffs** so four people can each own a slab
of the repo instead of one person running all 15 phases solo. This doc
supersedes `docs/PROMPTS.md` as the thing to actually hand to people; keep
`docs/PROMPTS.md` around as the phase-level reference if anyone wants finer
granularity than one of the four blocks below.

## How this is sequenced

**Strictly sequential, in this order: Member 1 → Member 2 → Member 3 → Member 4.**
Each member's block assumes every file from the previous members' blocks
already exists and works. Don't start Member 2's prompt until Member 1's
tests pass and their two live-account checks have been run and reported.

Why this order and not some other grouping: Member 1 builds the things
everyone else imports (`config.py`, the broker client, the journal, the
shared Pydantic schemas) — nothing else can be meaningfully built or even
mocked correctly until those exist. Member 2 (risk + execution) and Member 3
(screening + agents) don't strictly depend on each other's *internals*, only
on the schemas Member 1 already defined — if you have 2 and 3 available at
the same time and want to save calendar time, they *can* run in parallel
once Member 1 is done, each on their own branch, merging before Member 4
starts. But if you're running this as four people working one after
another (which is what "sequential" was asked for), the order above is
correct and each person should confirm the previous person's tests are
green before touching anything.

Member 4 cannot start meaningfully until 1, 2, and 3 are all done — they're
the one wiring the whole pipeline together end to end.

## One-time setup, before Member 1's prompt

1. Unzip the scaffold, `cd dispersion-barbell`, open it in Claude Code.
2. Paste the **CLAUDE.md block** below into `CLAUDE.md` at the repo root —
   every member's Claude Code session loads this automatically, so the
   non-negotiables don't need repeating in each prompt.
3. Fill in `.env` from `.env.example` with real keys before Member 1's
   prompt starts (Member 1 needs live paper-account keys for its
   verification step).
4. Each member should read `docs/architecture.md` in full before starting —
   every prompt below assumes that context and doesn't re-explain the
   overall design, only the specific files being built.

---

## Repo-root `CLAUDE.md` (paste once, before Member 1 starts)

```markdown
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

Python 3.11+, alpaca-py, anthropic SDK, openai SDK (pointed at Featherless
for screen/headline_triage.py only), pydantic v2, sqlmodel, APScheduler,
pyyaml + python-dotenv, pytest + freezegun, Streamlit (dashboard, optional).

## Style

Type-hint everything. Docstrings already exist on every stub file in this
repo — read the existing docstring before writing a function; it states the
exact contract. Don't change a function signature a docstring specifies
without saying why, and if you do change one, tell the next member in your
handoff notes (see the end of each block below).
```

---

# Member 1 — Foundation, Broker, Journal & Shared Schemas

**Owns:** `config.py`, `logging_config.py`, `broker/alpaca_client.py`,
`broker/clock.py`, `journal/store.py`, `journal/export.py` (initial version),
`agent/schemas.py` (the shared data contracts everyone else imports),
`scripts/verify_day1.py`, `scripts/seed_paper_account.py`.

**Blocks:** everyone. Nothing downstream can be honestly mocked until the
schemas and the journal tables exist, and nothing can run against the real
account until the broker client and verify_day1.py exist.

```
You're Member 1 of 4 building this project from scratch. Your job is the
foundation layer everyone else's Claude Code session will import — get the
contracts right, because changing a schema after Members 2–3 have built
against it is expensive. Read docs/architecture.md in full first.

1. Project setup. Create and activate a venv, install the project in
   editable mode with dev extras (`pip install -e ".[dev,dashboard]"`)
   using the dependencies already in pyproject.toml. Fix any version
   conflicts you hit. Confirm the CI workflow
   (.github/workflows/tests.yml) runs and passes with zero tests written.

2. src/barbell/config.py (doesn't exist yet, everything else needs it):
   - Load config/settings.yaml and config/universe.yaml with pyyaml.
   - Load .env with python-dotenv.
   - Expose one typed Settings object (pydantic BaseModel, nested models
     mirroring the YAML structure: AccountConfig, CalendarConfig,
     SleeveAConfig, SleeveBConfig, RiskGateConfig, ExecutionConfig,
     SchedulerConfig) via get_settings(), cached so it's parsed once per
     process.
   - Fail loudly and specifically (not a generic KeyError) if a required
     env var is missing.

3. src/barbell/logging_config.py: small `rich`-based console logging setup,
   level from BARBELL_LOG_LEVEL in .env.

4. agent/schemas.py — THE SHARED CONTRACT LAYER. Define every pydantic
   model that will cross a module boundary anywhere in this system, even
   though the modules that produce/consume some of them don't exist yet.
   Getting these right now is the whole point of your phase:
   - CatalystVerdict: catalyst_risk: bool, reasoning: str
   - ProposedLeg: symbol, expiry (date), strike (float), right ("call"/"put"),
     side ("buy"/"sell"), contracts (int)
   - ProposedStructure: underlying: str, legs: list[ProposedLeg],
     rationale: str, sleeve: Literal["A", "B"], max_loss_estimate: float
   - HeadlineDigest: symbol: str, news_volume: Literal["low","normal","elevated"],
     summary: str (empty string is the "neutral/failed" default, never None)
   - GateResult: outcome: Literal["PASS","RESIZE","VETO"], contracts: int | None,
     reason: str, gate_name: str
   - PortfolioState: current_nav, starting_nav, open_positions (list),
     sector_exposure (dict), last_quote_ts (dict[str, datetime]),
     reserved_capital: float (new — running total of capital reserved for
     an in-flight basket entry that hasn't finished reconciling yet; this
     is what the basket capital-reservation gate reads)
   - MarketState: whatever a gate actually needs beyond PortfolioState —
     quote ages, spreads, open interest per candidate, dispersion_score:
     float | None (new — vega-weighted single-name IV over index IV,
     computed in Phase/Member 3's screen/metrics.py, but the field belongs
     here since risk gates in Member 2 read it)
   - RiskDecision: outcome, contracts, reasons (list[str] — every gate that
     fired, not just the deciding one), proposed: ProposedStructure
   - ScreenResult: symbol, passed: bool, reason: str, metrics: dict
   Write these as the actual contract — Members 2 and 3 will import from
   this file and should not need to redefine or duplicate any of it. If a
   docstring elsewhere already implies a slightly different shape, this
   file wins; note any such conflicts you resolve in your handoff notes.

5. broker/alpaca_client.py — the only module allowed to import alpaca-py.
   Construct clients from config.get_settings() only, never read env vars
   directly here. Implement every method already named in its docstring:
   get_account, get_positions, get_option_chain, submit_mleg_order,
   get_clock. submit_mleg_order must build a real Alpaca multi-leg order
   request using alpaca-py's actual request models — look them up, don't
   invent a shape — and must itself raise before ever calling the SDK if a
   short leg in the list isn't covered by a corresponding long leg (no
   naked shorts, checked here as well as later in the risk engine —
   defense in depth on a CLAUDE.md non-negotiable).

6. broker/clock.py: implement its functions against
   alpaca_client.get_clock() and config/settings.yaml's `calendar` section.
   Every date comparison must be timezone-aware (America/New_York) since
   calendar keys are ET times.

7. journal/store.py using sqlmodel — one model class per table:
   screen_results, catalyst_verdicts, proposed_structures, risk_decisions,
   orders, positions_snapshot, kill_switch_events, and two NEW tables for
   the basket-execution fix: capital_reservations (cycle_id, basket_id,
   reserved_amount, status: "reserved"/"released"/"consumed", ts) and
   basket_leg_fills (basket_id, underlying, sequence_number, fill_status,
   ts) — Member 2 will write to both of these but the tables need to exist
   now. Every table needs cycle_id (str) and ts (datetime, UTC). Provide a
   JournalStore class with one record_* method per table — no raw SQL from
   outside this module, ever. DB path from Settings (BARBELL_DB_PATH).
   Create tables on first use if missing. Append-only: no method in this
   class may issue an UPDATE or DELETE.

8. journal/export.py: export_trade_log_csv (dumps orders table to CSV) and
   a first-pass export_writeup(db_path) -> str rendering
   docs/writeup_generated.md from whatever's in the DB (can be near-empty
   at this point — Member 4 will finish this in their phase, you're just
   making sure the plumbing works end to end).

9. scripts/verify_day1.py — the 7-item checklist already described in its
   docstring. Each check prints PASS / FAIL / UNKNOWN with the evidence it
   saw; exit non-zero if any REQUIRED check (account level, order fill,
   quote staleness) fails.

10. scripts/seed_paper_account.py: confirm a fresh $100k paper account,
    print and persist the account ID to data/account_id.txt.

11. Tests: tests/test_config.py (loads real settings.yaml, asserts a
    handful of real values), tests/test_broker.py (alpaca-py fully mocked
    — no test in this repo ever hits the live API — cover get_account
    parsing and the naked-short rejection), tests/test_journal.py (temp
    SQLite DB, insert a row per table including the two new ones, confirm
    export_writeup is non-empty, confirm no UPDATE/DELETE is reachable from
    the public API).

12. Run `python scripts/verify_day1.py` against the real paper account and
    report all 7 results — this decides whether Sleeve A uses credit
    spreads as designed or falls back to cash-secured puts, and whether
    Member 3 will need the Black-Scholes Greeks fallback.

13. Write handoff notes at the bottom of docs/architecture.md under a new
    "## Member 1 handoff" heading: exact final shape of every schema in
    agent/schemas.py (Members 2 and 3 should not have to open the file to
    know what's in it), what verify_day1.py found, and anything you had to
    deviate from the docstrings on and why.

Don't touch risk/, execution/, screen/, or agent/catalyst_gate.py /
structure_agent.py — those are Members 2 and 3's work, built against the
schemas and broker client you just finished.
```

---

# Member 2 — Risk Engine & Execution (with basket-atomicity fix)

**Owns:** `risk/gates.py`, `risk/engine.py`, `risk/kill_switch.py`,
`execution/orders.py`, `execution/reconcile.py`.

**Depends on:** Member 1's `config.py`, `broker/alpaca_client.py`,
`journal/store.py`, `agent/schemas.py` — read Member 1's handoff notes in
`docs/architecture.md` before starting.

**Blocks:** Member 4 (nothing can run end to end without this). Does not
block Member 3, who can build in parallel on a separate branch if you have
the people available — but per the sequential plan, Member 3 starts after
you're done.

```
You're Member 2 of 4. This is the highest-stakes code in the repo — the
layer that can only make a proposed trade smaller or reject it, never
bigger. Read docs/architecture.md in full, including the "Multi-leg basket
execution ordering" and "Config → gate mapping" sections, and read Member
1's handoff notes for the exact final shape of agent/schemas.py before
writing anything.

1. risk/gates.py — pure functions, one per gate, signature:
   (proposed: ProposedStructure, portfolio_state: PortfolioState,
   market_state: MarketState, config: RiskGateConfig) -> GateResult.
   Implement all 13 (12 original + the new basket-reservation gate):
   - per-position loss cap
   - portfolio loss cap
   - defined-risk-only (every short leg covered in the same order — same
     check as Member 1's alpaca_client, duplicated here on purpose as
     defense in depth, don't remove either copy)
   - quote staleness (max_quote_age_seconds)
   - liquidity floor (min_open_interest, max_spread_pct_of_mid)
   - dispersion/vega-ratio floor: VETO if market_state.dispersion_score is
     not None and falls below sleeve_a_carry.screen.min_dispersion_score;
     PASS (not VETO) if dispersion_score is None — Member 3 hasn't wired
     screen/metrics.py's dispersion_score() yet when you're building this,
     so this gate must degrate gracefully until that field starts arriving
     populated, per the Feature Admission Protocol's fail-closed rule
     applied sensibly: absence of data here means "don't block on
     something not computed yet," not "assume the worst," since a VETO on
     None would silently disable Sleeve A entirely until Member 3 lands.
   - earnings blackout
   - pre-NFP flatten requirement (queries endgame phase — endgame/schedule.py
     is Member 4's, not built yet — stub this dependency for now, return
     PASS unconditionally with a `# TODO(Member 4)` comment, and say so
     explicitly in your handoff notes so it doesn't get missed)
   - expiry-past-deadline rejection (same TODO/stub situation as above)
   - concentration (max_positions_per_underlying, max_sector_concentration)
   - drawdown kill-switch check (delegates to kill_switch.py)
   - slippage cap (max_slippage_pct_of_mid)
   - broker-reconciliation-required flag (delegates to this same phase's
     execution/reconcile.py result — this one you ARE building this phase,
     wire it for real, no stub)
   - **basket capital reservation gate (new):** VETO if submitting this
     leg would push total reserved-plus-committed capital
     (portfolio_state.reserved_capital + this proposal's max_loss_estimate)
     over risk_gates.max_loss_portfolio_pct_nav × current_nav. This is
     what stops a basket build from over-committing capital across
     sequential legs before earlier legs have actually confirmed filled.

2. risk/engine.py's evaluate(): run every gate in a fixed order, fold
   results (a single VETO wins immediately; smallest RESIZE wins if
   multiple gates resize; PASS only if every gate passes), log the full
   breakdown as a RiskDecision. Write it so evaluate().contracts can never
   exceed proposed.contracts BY CONSTRUCTION — start from proposed and only
   ever apply min(), never anything that could increase it.

3. risk/kill_switch.py: check_and_latch persists through journal/store.py's
   kill_switch_events table (it exists now, from Member 1 — no local
   fallback table needed) so a process restart doesn't un-latch it.

4. execution/orders.py — implement submit() AND the new sequential-basket
   entry logic:
   - submit(decision: RiskDecision, proposed: ProposedStructure): if not
     REJECT, build and submit a limit multi-leg order via
     broker.alpaca_client.submit_mleg_order. Hard-code order_type="limit"
     in this function's own logic, don't just trust config — this is a
     CLAUDE.md non-negotiable. Implement retry-and-widen from
     config/settings.yaml's execution/risk_gates.order_retry_* keys: on
     timeout without a fill, widen by order_retry_widen_pct and resubmit,
     up to order_retry_limit times, then give up and log. Every attempt
     gets a journal.orders row.
   - NEW: submit_basket(proposals: list[ProposedStructure]) — the
     sequential-entry orchestrator described in architecture.md. For each
     underlying in order: (a) on the FIRST call for this basket, compute
     total max-loss across all proposals and write a capital_reservations
     row via JournalStore (status="reserved") BEFORE calling submit() for
     the first leg; (b) call submit() for this one underlying only; (c)
     call execution.reconcile.reconcile() and confirm the fill (or log the
     rejection) before moving to the next underlying — never build the
     next order until this one is resolved; (d) after each fill, re-run
     risk.engine.evaluate() against the ACTUAL filled position (not the
     original proposal) for portfolio-level gates, and if that now fails,
     halt the basket for this cycle, write a basket_leg_fills row noting
     the halt, and leave remaining names for the next cycle; (e) when the
     whole basket finishes (all legs resolved, halted, or the last one
     done), release the capital reservation (status="released") and write
     that to the journal too. submit_basket must refuse to start a new
     basket while an old one's reservation is still "reserved" (unreleased)
     — that's the concurrency guard mentioned in architecture.md.

5. execution/reconcile.py's reconcile(): pulls
   broker.alpaca_client.get_positions() and get_account(), diffs against
   the journal's last positions_snapshot, writes a new snapshot either way,
   returns a ReconciliationReport with `diverged: bool` and a
   human-readable description. On divergence, log at CRITICAL. This runs
   both on the normal per-cycle cadence (Member 4 wires that) AND after
   every basket leg fill (you just wired that above) — same function,
   called from two places.

6. Tests:
   - tests/test_risk_gates.py: one PASS, one exact-boundary, one VETO case
     per gate (all 13, including the new basket-reservation gate and the
     dispersion-score-is-None-passes-not-vetoes case), a resize case for
     per-position and portfolio caps, a property test (200+ randomized
     cases) asserting evaluate().contracts never exceeds the input, a
     kill-switch test (trip it, confirm a subsequent evaluate() REJECTs
     regardless of individual gates, confirm it survives module
     reinstantiation).
   - tests/test_execution.py: alpaca_client mocked — a PASS decision
     produces one order; a partial-fill-then-timeout produces the expected
     retries then an abandoned-order row, not an infinite loop; a diverged
     reconcile() blocks a subsequent submit(); AND new cases for
     submit_basket: a 3-underlying basket reserves capital before leg 1,
     releases it after leg 3, and a forced mid-basket gate failure on leg 2
     halts before leg 3 ever gets built while leg 1's fill stays in place.

7. Do one real end-to-end smoke test against the paper account: hand-build
   a small 2-underlying list of ProposedStructures, run submit_basket() for
   real, and report what happened at each step (reservation written,
   sequence order, both fills, reservation released) using the journal.

8. Handoff notes in docs/architecture.md under "## Member 2 handoff": which
   gates are still TODO-stubbed pending Member 4's endgame/schedule.py,
   confirm the exact final RiskDecision/GateResult shape used (in case it
   drifted from Member 1's schema during implementation — if it did, you
   must have gotten Member 1's schema file to actually match, not created
   a second shadow version), and the real verify_day1.py-informed answer
   on credit spreads vs. cash-secured puts if that changed anything here.
```

---

# Member 3 — Screening, Metrics & AI Agents (with dispersion score)

**Owns:** `screen/universe.py`, `screen/metrics.py`, `screen/headline_triage.py`,
`agent/catalyst_gate.py`, `agent/structure_agent.py`, `agent/prompts/*.md`.

**Depends on:** Member 1's `config.py`, `broker/alpaca_client.py`,
`journal/store.py`, `agent/schemas.py`. Does not depend on Member 2's
internals, only on the same schemas Member 2 also built against — if
running in parallel with Member 2, coordinate only on agent/schemas.py.

**Blocks:** Member 4.

```
You're Member 3 of 4. You own everything that decides WHAT to trade, before
Member 2's risk engine decides how much of it (if any) is allowed. Read
docs/architecture.md in full, especially the "Dispersion / vega-ratio
metric" section, and read Member 1's (and Member 2's, if done by now)
handoff notes in docs/architecture.md before starting.

1. screen/metrics.py:
   - iv_rank(current_iv, iv_52w_series) -> float
   - iv30_hv20_ratio(iv30, close_prices_20d) -> float (compute realized
     20-day historical vol from the closes yourself, annualized)
   - bs_implied_vol and bs_greeks: dependency-free Black-Scholes (Newton-
     Raphson, math.erf for the normal cdf — no scipy). Use this ONLY if
     Member 1's verify_day1.py found the Basic data plan doesn't return
     usable Greeks/IV directly — check their handoff notes and say which
     path you're taking.
   - NEW: dispersion_score(survivors: list[ScreenResult with per-name IV
     and proposed vega], index_iv: float) -> float — implements
     `(Σ w_i · IV_i) / IV_index` from architecture.md, weighted by each
     survivor's proposed position vega (use each candidate's estimated
     single-contract vega from bs_greeks × proposed contract count as the
     weight). Compute this once per screening cycle, over whatever set of
     names passed Stage 1's other filters, against the current SPY (index)
     IV pulled from broker.alpaca_client.get_option_chain. Log the value to
     the journal every cycle regardless of whether it ends up vetoing
     anything (screen_results or a dedicated field — check what Member 1's
     schema supports and extend ScreenResult.metrics dict with it if there
     isn't a dedicated column, rather than adding a new table for one
     number).

2. screen/universe.py:
   - load_candidates(): reads config/universe.yaml's `candidates` dict.
   - screen(candidates): for each ticker, pull the option chain, apply
     every filter from config/settings.yaml's sleeve_a_carry.screen section
     in order (min_open_interest, max_spread_pct_of_mid, min_iv_rank,
     min_iv30_hv20_ratio, earnings_blackout against universe.yaml's exclude
     list), THEN compute dispersion_score() across whatever survives those
     filters and attach it to MarketState (per Member 1's schema) so
     Member 2's dispersion gate can read it — this is the wiring that turns
     that gate from always-PASS (Member 2's None-safe default) into a real
     check. Every candidate gets a ScreenResult whether it passed or not,
     written to journal.screen_results via JournalStore — not just
     survivors.

3. screen/headline_triage.py's digest_headlines(symbol, headlines): use the
   `openai` SDK pointed at FEATHERLESS_BASE_URL / FEATHERLESS_API_KEY /
   FEATHERLESS_MODEL from Settings. Prompt for a short summary and a
   news_volume label, parse into HeadlineDigest. On ANY failure (timeout,
   bad JSON, API error): catch it, log a warning, return an empty/neutral
   HeadlineDigest. This function must never raise past its own boundary —
   it has zero authority, per CLAUDE.md.

4. agent/prompts/catalyst_gate.md: prompt template giving Claude the
   symbol, recent headlines, and the optional HeadlineDigest as context,
   asking it to decide whether elevated IV is explained by a
   scheduled/already-priced event versus an unscheduled binary risk, forced
   into CatalystVerdict via an Anthropic tool-use schema matching Member
   1's schema exactly.

5. agent/catalyst_gate.py's check_catalyst(): calls the Anthropic API with
   that prompt, tool-forces a CatalystVerdict. On schema validation failure
   or ANY API error: return
   CatalystVerdict(catalyst_risk=True, reasoning="fail-closed: <error>") —
   this is the one place in your whole block that has real veto authority,
   so it never silently skips a name on failure.

6. agent/prompts/structure_agent.md and agent/structure_agent.py's
   propose_structure(): give Claude the option chain data, screen metrics
   (including this cycle's dispersion_score, so the model's rationale text
   can reference it), and sleeve_a_carry config (width, delta range, DTE
   range), force a ProposedStructure response via tool use. This function
   must not call broker.alpaca_client or execution.orders at all — data in,
   data out, nothing else.

7. Tests:
   - tests/test_screen.py: alpaca_client mocked with fixture chain data
     (tests/fixtures/*.json) — confirm each filter rejects a case built to
     fail exactly that filter; confirm a fully-passing name is returned;
     confirm dispersion_score() produces the expected number on a small
     hand-computed fixture (2–3 names, known IVs, known index IV — check
     the arithmetic by hand, don't just assert it runs); confirm every
     candidate produces a journal row regardless of pass/fail.
   - tests/test_agent.py: both Anthropic and Featherless mocked — a
     well-formed response parses correctly into each schema; a malformed
     catalyst-gate response results in catalyst_risk=True (test this
     explicitly); a headline_triage failure doesn't raise and returns a
     neutral digest.

8. Run the real screen against config/universe.yaml's candidate list
   through the live paper account's data feed. Report how many survive,
   why the rest didn't, and the real dispersion_score value this
   produces right now — that number is itself informative (confirms or
   undermines the "single names rich, index cheap" thesis with a real
   figure instead of the qualitative read the design started with).

9. Run one real end-to-end call of both LLM stages against a name that
   passed the screen (a couple of real API calls is fine) and show the
   actual CatalystVerdict and ProposedStructure produced.

10. Handoff notes under "## Member 3 handoff" in docs/architecture.md:
    real dispersion_score reading and what it implies, confirm final
    MarketState/ScreenResult shape matches what Member 2 built the
    dispersion gate against (flag any drift), and which data path
    (native Greeks vs. Black-Scholes fallback) you ended up on.
```

---

# Member 4 — Endgame, Orchestration, Dashboard, Verification & Submission

**Owns:** `endgame/schedule.py`, `scheduler/loop.py`, `cli.py`,
`dashboard/app.py`, final `journal/export.py`, all integration testing,
deployment, the runbook, presentation assets, and the pre-submission audit.

**Depends on:** everything from Members 1–3. This is the wiring, testing,
and shipping phase — nothing here should require touching risk/gates.py's
actual logic or agent/'s prompts, only calling into them correctly and
proving the whole thing works end to end.

```
You're Member 4 of 4 — you're closing the loop, verifying the safety
claims are real rather than theoretical, and getting this to a submittable
state. Read docs/architecture.md in full including all three prior
members' handoff notes before starting; you need to know what's stubbed
(the two risk gates Member 2 left as TODOs pending your endgame/schedule.py)
and what the real numbers looked like from Members 2 and 3's live checks.

1. endgame/schedule.py:
   - current_phase(clock): BUILD (before first_full_session) ->
     CARRY_ACTIVE (through last_carry_entry_day) -> UNWIND
     (carry_unwind_day) -> CONVEXITY_ENTRY (carry_unwind_day, after
     convexity_entry_after_et ET) -> HOLD_THROUGH_NFP -> MONETIZE (after
     nfp_release_et on submission day, before flatten_by_et) -> FLAT (after
     flatten_by_et) -> POST_DEADLINE (after submission_deadline_et). Get
     the boundary semantics exactly right — flatten_by_et and
     submission_deadline_et are the two lines the whole design depends on
     never being fuzzy.
   - allowed_actions(phase) -> set[str]: CARRY_ACTIVE allows new Sleeve A
     entries and closes; UNWIND allows only Sleeve A closes (routed through
     Member 2's submit_basket() the same as entries — closing a basket is
     still sequential-per-underlying with the same reconcile-between-legs
     discipline, don't special-case it into a parallel-close path); 
     CONVEXITY_ENTRY allows a Sleeve B open (check
     sleeve_b_convexity.escalate_if_nav_above_start against current NAV vs.
     starting_nav for base vs. escalated size); MONETIZE allows only Sleeve
     B closes; FLAT and POST_DEADLINE allow nothing but reads.
   - Go back to risk/gates.py's two TODO-stubbed gates from Member 2
     (pre-NFP flatten requirement, expiry-past-deadline) and wire them to
     current_phase() for real. Remove both TODOs — this is the one place
     you do touch risk/gates.py, and only to complete an already-specified
     stub, not to change gate logic.
   - tests/test_schedule.py with freezegun, frozen at BOTH sides of every
     boundary in config/settings.yaml's calendar section (last_carry_entry_day
     23:59 vs carry_unwind_day 00:01, convexity_entry_after_et ±1 min,
     nfp_release_et ±1 min, flatten_by_et ±1 min, submission_deadline_et
     ±1 min). This is the file where an off-by-one actually costs the
     competition — every boundary needs an explicit test on both sides.

2. scheduler/loop.py using APScheduler: every
   scheduler.cycle_interval_minutes, if the market's open (or always, if
   scheduler.market_hours_only is false), run one cycle: current_phase() ->
   (if phase allows entries) screen.universe.screen() -> for survivors:
   headline_triage -> catalyst_gate -> (if not catalyst_risk)
   structure_agent -> collect all ProposedStructures for this cycle ->
   risk.engine.evaluate() per proposal to pre-filter -> hand whatever
   passes to execution.orders.submit_basket() (Member 2's sequential
   entry point — do not call submit() directly per name from the
   scheduler, submit_basket() is what owns the capital-reservation
   ordering) -> always finish with execution.reconcile.reconcile() even if
   an earlier stage threw. Wrap each stage in its own try/except that logs
   and continues (one bad candidate or flaky API call must not take down
   the loop or skip the final reconcile).

3. cli.py's full dispatch: `run-cycle` runs one cycle and exits; `status`
   prints NAV, phase, open positions, kill-switch state, current
   reserved_capital if a basket is mid-flight; `flatten` force-closes every
   open position regardless of phase (log loudly that it was manually
   invoked — this is an emergency override); `verify` runs
   scripts/verify_day1.py; `journal export` calls export_writeup and
   export_trade_log_csv and prints where it wrote them.

4. Finish journal/export.py's export_writeup(): Member 1 built a first
   pass: extend it to definitely cover the three things the hackathon
   rules ask for explicitly — AI logic, risk gates (all 13, name the
   basket-reservation and dispersion gates specifically as the
   differentiated ones), Alpaca infrastructure — plus a section
   summarizing the dispersion_score trend across the week (pull it from
   wherever Member 3 logged it) and a short, specific paragraph on the
   basket-atomicity fix as a named defense, not just "risk-managed."

5. dashboard/app.py — Streamlit, read-only against the journal DB, no
   broker calls from this file. Show: NAV vs. $100,000 starting value;
   Sleeve A vs. Sleeve B realized/unrealized P&L; open positions table;
   live feed of recent risk_decisions (symbol, outcome, reason); current
   kill-switch status; current endgame phase; current basket-reservation
   status if one is in flight (from capital_reservations); the latest
   dispersion_score reading; a countdown to submission_deadline_et.
   Auto-refresh on a short interval.

6. tests/test_scheduler.py: mock every stage, confirm a happy-path cycle
   runs start to finish; confirm a mocked catalyst_gate failure on one
   candidate doesn't stop the rest of the cycle; confirm reconcile() always
   runs even if an earlier stage threw.

7. Full verification pass — not new features, find what's wrong:
   - `pytest --cov=barbell tests/ -v`. Report coverage, called out
     separately for risk/ and endgame/ (should be the highest of any
     package — fix first if not).
   - One full integration dry run: `barbell run-cycle` once, watched
     manually, confirming each journal table (including the two new
     capital_reservations / basket_leg_fills tables) got the rows you'd
     expect for whatever the market did that cycle.
   - Deliberately break things to confirm the safety claims are real:
     trip the kill switch and confirm a subsequent run-cycle rejects
     everything; feed the risk engine a proposal sized to breach the
     per-position cap and confirm resize/veto; force reconcile() to return
     diverged=True and confirm new entries are blocked; start a
     submit_basket() call, kill the process mid-basket (simulate — don't
     actually crash a real order), restart, and confirm the leftover
     "reserved" capital_reservations row is either correctly resumed or
     safely refused rather than silently forgotten (this is the basket
     fix's own restart-safety case — Parallax's rigor is only real if
     this actually holds).
   Report all of these as pass/fail with evidence, not "looks fine."

8. Deployment: systemd unit (deploy/barbell.service) or a documented
   tmux/nohup invocation in docs/architecture.md's Runtime section — ask
   which if not obvious. Confirm kill-switch and endgame-phase state
   survive a real process restart (paper account, no open positions at
   risk), sourced from the journal DB not memory. Wire up the dashboard at
   a public URL if needed for the submission.

9. docs/runbook.md: what to check at market open each day; what a normal
   cycle's journal output looks like versus concerning (e.g. a sudden run
   of VETOs on one gate suggests miscalibration, not a market event); how
   to manually `barbell flatten`; what NAV trajectory is within the
   expected range (roughly flat to +1.6% from Sleeve A alone) versus
   warrants reconsidering sizing. Written for a human glancing at the
   system during the week, not another Claude Code session.

10. scripts/export_slide_stats.py: pull starting NAV, current NAV,
    per-sleeve realized P&L, trade count, gate-rejection count and top 2-3
    reasons, catalyst-gate veto count, and the final dispersion_score
    reading into one clean JSON/text block for slides.

11. Final pre-submission audit, read-only, report don't fix without
    asking: .env is git-ignored and was never committed (check git
    history, not just current .gitignore); no API key/secret anywhere in
    tracked files; README's quickstart works on a clean checkout; CI is
    green on main; the DB file is git-ignored; the account ID in
    data/account_id.txt matches the account with the real trading history.
    Report every item pass/fail with specific evidence.
```
