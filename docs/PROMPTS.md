# Claude Code Build Prompts — Dispersion Barbell

Fifteen prompts, one per phase, in the same order and with the same dependencies as the [implementation plan](https://claude.ai/code/artifact/ed1ac98d-647c-48e1-b5a7-a6afa5908746). Each is self-contained enough to paste into a fresh Claude Code session, but they're meant to be run **in order in the same session/repo** — later prompts assume earlier files exist and don't re-explain them.

## How to use this

1. Unzip the scaffold, `cd dispersion-barbell`, open it in Claude Code.
2. Paste the **CLAUDE.md block** below into `CLAUDE.md` at the repo root first — Claude Code loads it automatically every session, so the non-negotiable constraints don't need repeating in every prompt.
3. Fill in `.env` from `.env.example` with real keys before Phase 02.
4. Feed the phase prompts one at a time, in order. After each, actually run what it tells you to run before moving to the next — several later prompts depend on earlier ones having been verified against the real API, not just written.
5. If a session's context gets compacted or you start a new one mid-project, just say which phase you're resuming and point Claude Code at this file plus `docs/architecture.md`.

---

## Repo-root `CLAUDE.md` (paste once, before Phase 01)

```markdown
# Dispersion Barbell — project invariants

Autonomous options agent for the Alpaca AI Trading Agents Hackathon. Full
design: docs/architecture.md. Full phase plan: docs/PROMPTS.md.

## Non-negotiable, across every phase

- PAPER TRADING ONLY. Never write code that could plausibly submit to a live
  account. `ALPACA_PAPER_TRADE` must stay `true` everywhere it appears.
- The risk engine (`risk/engine.py`) may only PASS, RESIZE DOWN, or REJECT a
  proposed trade. No code path may ever increase a proposed size or clear a
  veto. This is tested by a property test in tests/test_risk_gates.py and
  that test must never be weakened to make a feature pass.
- Options orders are LIMIT ONLY, never MARKET. This is enforced in
  execution/orders.py in code, not just in config/settings.yaml.
- Every short option leg must be covered within the same multi-leg order —
  no naked shorts, ever, at any level.
- broker/alpaca_client.py is the ONLY module allowed to import alpaca-py.
  Everything else calls it through its interface so it stays mockable.
- agent/catalyst_gate.py and agent/structure_agent.py are the only two
  places an LLM decision has real authority (veto / sizing). Any other LLM
  call (e.g. screen/headline_triage.py) is informational only and must
  degrade to an empty/neutral result on failure — never block the pipeline.
- Every pipeline stage (screen, catalyst check, structure proposal, risk
  decision, order, reconciliation) writes to journal/store.py — passes and
  rejections both, always with a reason.
- Config values (gate thresholds, sizing, calendar dates) live in
  config/settings.yaml and config/universe.yaml. Don't hardcode numbers in
  Python that already have a home in config.
- Prefer small, directly testable functions. Anything touching money sizing
  or order construction needs a unit test in the same PR/commit that adds it.

## Stack

Python 3.11+, alpaca-py, anthropic SDK, openai SDK (pointed at Featherless
for screen/headline_triage.py only), pydantic v2, sqlmodel, APScheduler,
pyyaml + python-dotenv, pytest + freezegun, Streamlit (dashboard, optional).

## Style

Type-hint everything. Docstrings already exist on every stub file in this
repo — read the existing docstring before writing a function; it states the
exact contract. Don't change a function signature a docstring specifies
without saying why.
```

---

## Phase 01 — Foundation & environment

```
Set up the Python project so every later phase has somewhere to plug in.
This repo already has a scaffold with docstring-only stub files — read
docs/architecture.md first for the full picture, then:

1. Create and activate a venv, install the project in editable mode with
   dev extras (`pip install -e ".[dev,dashboard]"`), using the dependencies
   already listed in pyproject.toml. Fix any version conflicts you hit.

2. Write src/barbell/config.py — it doesn't exist yet and everything else
   will need it. It should:
   - Load config/settings.yaml and config/universe.yaml with pyyaml
   - Load .env with python-dotenv
   - Expose a single typed Settings object (pydantic BaseModel, nested
     models mirroring the YAML structure: AccountConfig, CalendarConfig,
     SleeveAConfig, SleeveBConfig, RiskGateConfig, ExecutionConfig,
     SchedulerConfig) via a `get_settings()` function, cached so it's only
     parsed once per process.
   - Fail loudly and specifically (not a generic KeyError) if a required
     env var is missing — this is the file every other module will import
     for config, so a bad error message here costs time everywhere else.

3. Write src/barbell/logging_config.py — a small setup using `rich` for
   readable console output, level from BARBELL_LOG_LEVEL in .env.

4. Confirm the CI workflow (.github/workflows/tests.yml) runs and passes
   with zero tests currently written.

5. Write one smoke test, tests/test_config.py, that loads Settings from the
   real config/settings.yaml and asserts a handful of values match what's
   actually in the YAML (e.g. account.starting_nav == 100000.0,
   risk_gates.drawdown_kill_switch_pct_nav == -0.08). This is the first
   real test in the repo — make sure `pytest` actually discovers and runs it.

Don't touch broker/, risk/, or agent/ yet — this phase is only the
scaffolding everything else imports.
```

---

## Phase 02 — Broker integration layer

```
Implement the only module allowed to talk to Alpaca directly, plus the
platform verification script that depends on it. Read the existing
docstrings in src/barbell/broker/alpaca_client.py and broker/clock.py first
— they already specify the exact method signatures to implement.

1. Implement broker/alpaca_client.py using alpaca-py's TradingClient and
   OptionHistoricalDataClient / StockHistoricalDataClient as needed,
   constructed from config.get_settings() (never read env vars directly in
   this file — go through Settings). Implement every method the docstring
   lists: get_account, get_positions, get_option_chain, submit_mleg_order,
   get_clock. submit_mleg_order must construct a proper Alpaca multi-leg
   order request — look at alpaca-py's actual request models for options
   multi-leg orders and use them correctly, don't invent a shape.

2. Implement broker/clock.py's functions against alpaca_client.get_clock()
   and the dates in config/settings.yaml's `calendar` section. Write it so
   every date comparison is timezone-aware (America/New_York), since the
   calendar keys are ET times.

3. Fully implement scripts/verify_day1.py — the 7-item checklist already
   described in its docstring. Each check should print a clear PASS / FAIL
   / UNKNOWN line with the evidence it saw, and the script should exit
   non-zero if any REQUIRED check (account level, order fill, quote
   staleness) fails, so it's usable as a gate in CI or a pre-flight script.

4. Run `python scripts/verify_day1.py` against the real paper account (keys
   already in .env). Report back what it finds for each of the 7 items —
   I need those answers to decide whether Sleeve A uses credit spreads as
   designed or falls back to cash-secured puts, and whether the Greeks
   fallback in screen/metrics.py (built in Phase 06) is actually needed.

5. Write tests/test_broker.py with alpaca-py calls mocked (use
   unittest.mock or a fixture) — no test in this repo should hit the live
   API. Cover at minimum: get_account parses correctly, submit_mleg_order
   rejects a leg list where a short isn't covered (this should raise before
   ever calling the SDK, not rely on Alpaca's API to catch it).

Do not implement broker/mcp_client.py's actual logic — it stays
documentation-only per its docstring; instead, write the
alpaca-mcp-server config block for Claude Desktop/Code as a snippet in
docs/architecture.md's MCP section, using the real ALPACA_API_KEY /
ALPACA_SECRET_KEY from .env (don't paste the actual secret into a
committed file — reference the env var names).
```

---

## Phase 03 — Risk engine & safety layer

```
This is the highest-stakes file in the repo — build it carefully and test
it harder than anything else. Read risk/gates.py, risk/engine.py, and
risk/kill_switch.py's docstrings first; they already name all 12 gates and
the exact non-negotiable behavior (can only tighten, never loosen).

1. Implement risk/gates.py as pure functions, one per gate, each with the
   signature (proposed: ProposedStructure, portfolio_state: PortfolioState,
   market_state: MarketState, config: RiskGateConfig) -> GateResult, where
   GateResult is PASS | RESIZE(contracts: int) | VETO(reason: str). Define
   PortfolioState and MarketState as small dataclasses/pydantic models in
   this file if they don't exist yet (current NAV, open positions, sector
   exposure, last quote timestamps, etc — whatever each gate actually
   needs). Implement all 12:
   per-position loss cap, portfolio loss cap, defined-risk-only (every
   short leg covered in the same order), quote staleness
   (max_quote_age_seconds), liquidity floor (min_open_interest,
   max_spread_pct_of_mid), earnings blackout, pre-NFP flatten requirement
   (query endgame phase — stub this dependency for now if endgame/schedule.py
   isn't built yet, and note the TODO), expiry-past-deadline rejection,
   concentration (max_positions_per_underlying, max_sector_concentration),
   drawdown kill-switch check (delegates to kill_switch.py), slippage cap
   (max_slippage_pct_of_mid), broker-state-reconciliation-required flag
   (delegates to execution/reconcile.py's last result — stub if not built
   yet, TODO it).

2. Implement risk/engine.py's evaluate() to run every gate in a fixed order,
   fold the results (a single VETO wins immediately; the smallest RESIZE
   wins if multiple gates resize; PASS only if every gate passes), and log
   the full breakdown. It must be structurally impossible for evaluate() to
   return a `contracts` value greater than what was proposed — write it so
   that's true by construction (e.g. start from proposed.contracts and only
   ever apply min()), not just by convention.

3. Implement risk/kill_switch.py: check_and_latch persists its state (use
   journal's DB if journal/store.py exists yet, otherwise a simple local
   SQLite table in this module for now and wire it to the shared DB in
   Phase 04) so a process restart doesn't un-latch it.

4. Write tests/test_risk_gates.py:
   - One PASS case, one exact-boundary case, and one VETO case per gate
   - A resize case for at least the per-position and portfolio caps
   - A property test (hypothesis, or a manual loop over randomized
     ProposedStructure/PortfolioState combinations) asserting
     engine.evaluate(...).contracts is never greater than the input's
     contracts, across at least 200 random cases
   - A kill-switch test: trip it, confirm a subsequent evaluate() call
     REJECTs regardless of what the individual gates say, confirm it stays
     tripped after re-instantiating the module (simulating a restart)

Run `pytest tests/test_risk_gates.py -v` and don't move to Phase 04 until
every case passes, including the property test.
```

---

## Phase 04 — Journal & persistence

```
Build the append-only journal now, before execution or the agent layer
exist, so every later phase can log to it from the moment it's written.
Read journal/store.py and journal/export.py's docstrings for the exact
table list.

1. Implement journal/store.py using sqlmodel: one model class per table
   already named in the docstring (screen_results, catalyst_verdicts,
   proposed_structures, risk_decisions, orders, positions_snapshot,
   kill_switch_events). Every table needs a cycle_id (str) and a ts
   (datetime, UTC) column. Provide a JournalStore class with one
   `record_*` method per table (e.g. record_screen_result,
   record_risk_decision) — no raw SQL calls from outside this module.
   DB path comes from Settings (BARBELL_DB_PATH). Create tables on first
   use if they don't exist.

2. Wire risk/kill_switch.py's persistence (stubbed in Phase 03) to a
   kill_switch_events row via JournalStore, so it survives restarts through
   the real shared DB instead of its own local table.

3. Implement journal/export.py:
   - export_trade_log_csv: dumps the orders table to CSV
   - export_writeup(db_path) -> str: renders docs/writeup_generated.md —
     pull recent risk_decisions (with pass/reject counts and top reasons),
     orders (realized P&L if closed), and a short narrative built from
     proposed_structures' rationale fields. Doesn't need to be beautiful
     yet — it needs to be non-empty and accurate against whatever's in the
     DB, since this becomes the actual submission write-up later.

4. Write tests/test_journal.py: use an in-memory or temp-file SQLite DB,
   insert a handful of rows across every table, confirm export_writeup
   produces non-empty markdown referencing the inserted data, confirm
   nothing in this module ever UPDATEs or DELETEs a row (append-only is a
   real invariant here, not just a docstring claim — test it if you can,
   e.g. by asserting there's no UPDATE/DELETE SQL anywhere reachable from
   the public API).
```

---

## Phase 05 — Execution & reconciliation

```
Wire the risk engine's decision to an actual paper order, and close the
loop back to reality. Read execution/orders.py and execution/reconcile.py's
docstrings.

1. Implement execution/orders.py's submit(): takes a RiskDecision (from
   Phase 03) and the original ProposedStructure, and if the decision isn't
   REJECT, builds and submits a limit multi-leg order via
   broker.alpaca_client.submit_mleg_order. Hard-code order_type to "limit"
   in this function's own logic — don't just trust
   config.execution.order_type, since this is one of the CLAUDE.md
   non-negotiables. Implement the retry-and-widen behavior from
   config/settings.yaml's execution and risk_gates.order_retry_* keys: on a
   timeout (fill_timeout_seconds) without a fill, widen the limit by
   order_retry_widen_pct and resubmit, up to order_retry_limit times, then
   give up and log rather than chase further. Every attempt (filled,
   retried, or abandoned) gets a journal.orders row via JournalStore.

2. Implement execution/reconcile.py's reconcile(): pulls
   broker.alpaca_client.get_positions() and get_account(), diffs against
   the journal's last positions_snapshot, writes a new snapshot either way,
   and returns a ReconciliationReport with a boolean `diverged` flag and a
   human-readable description of any mismatch. On divergence, this should
   be loud (log at CRITICAL, not just log) — later phases (the scheduler)
   will check this flag before allowing new entries.

3. Now go back to risk/gates.py's TODO for the broker-reconciliation-required
   gate from Phase 03 and wire it to reconcile()'s last result properly —
   remove the stub.

4. Write tests/test_execution.py with alpaca_client mocked: a PASS decision
   produces one submitted order; a partial-fill-then-timeout produces the
   expected number of widened retries and then an abandoned-order journal
   row, not an infinite loop; a divergent reconcile() result blocks a
   subsequent submit() call (add that check to orders.submit if it isn't
   already implied — new entries should refuse to proceed while
   reconciliation is in a diverged state).

5. Do one real end-to-end smoke test against the paper account: hand-build
   a single small ProposedStructure for a real optionable name, run it
   through risk.engine.evaluate() and then execution.orders.submit(), and
   confirm a real paper fill appears and a journal row was written. Report
   what happened.
```

---

## Phase 06 — Screening & data pipeline

```
Build the deterministic Stage 1 of the pipeline — no LLM calls in this
phase. Read screen/universe.py and screen/metrics.py's docstrings.

1. Implement screen/metrics.py:
   - iv_rank(current_iv, iv_52w_series) -> float
   - iv30_hv20_ratio(iv30, close_prices_20d) -> float (compute realized
     20-day historical vol from the closes yourself, annualized)
   - bs_implied_vol and bs_greeks: a dependency-free Black-Scholes solver
     (Newton-Raphson on price, standard normal cdf/pdf implemented inline
     or via `math.erf` — no scipy). Use this ONLY if Phase 02's
     verify_day1.py found the Basic data plan doesn't return usable
     Greeks/IV from get_option_snapshot — check what that script reported
     and tell me which path you're taking.

2. Implement screen/universe.py:
   - load_candidates(): reads config/universe.yaml's `candidates` dict
   - screen(candidates): for each ticker, pull the option chain via
     broker.alpaca_client.get_option_chain, apply every filter from
     config/settings.yaml's sleeve_a_carry.screen section in order
     (min_open_interest, max_spread_pct_of_mid, min_iv_rank,
     min_iv30_hv20_ratio, earnings_blackout against universe.yaml's
     `exclude` list), and return a list of ScreenResult (define this as a
     small model: symbol, passed: bool, reason: str, metrics: dict).
     Every candidate gets a ScreenResult whether it passed or not — write
     every one to journal.screen_results via JournalStore, not just the
     survivors.

3. Write tests/test_screen.py with alpaca_client mocked with fixture chain
   data (put a couple of representative fixture JSON files in
   tests/fixtures/): confirm each filter correctly rejects a case built to
   fail exactly that filter, confirm a name passing every filter is
   returned, confirm every candidate (pass or fail) produces a journal row.

4. Run the real screen against config/universe.yaml's candidate list
   through the live paper account's data feed and report how many survive
   and why the rest didn't — I want to see if the seed list needs pruning
   or expanding before Phase 07 spends LLM calls on it.
```

---

## Phase 07 — Agent / LLM layer

```
Both LLM stages, both schema-validated, both fail closed on the critical
one. Read agent/schemas.py, agent/catalyst_gate.py, agent/structure_agent.py,
and screen/headline_triage.py's docstrings — the schemas already exist
(CatalystVerdict, ProposedStructure, ProposedLeg, HeadlineDigest); extend
them only if a real gap shows up while implementing.

1. Implement screen/headline_triage.py's digest_headlines(symbol, headlines):
   use the `openai` SDK pointed at FEATHERLESS_BASE_URL /
   FEATHERLESS_API_KEY / FEATHERLESS_MODEL from Settings. Prompt it for a
   short JSON-ish summary and a news_volume label (low/normal/elevated),
   parse into HeadlineDigest. On ANY failure (timeout, bad JSON, API
   error), catch it, log a warning, and return an empty/neutral
   HeadlineDigest — this function must never raise past its own boundary,
   per CLAUDE.md.

2. Write agent/prompts/catalyst_gate.md: a prompt template that gives Claude
   the symbol, its recent headlines, and the optional HeadlineDigest as
   context, and asks it to decide whether elevated IV is explained by a
   scheduled/already-priced event or suggests an unscheduled binary risk —
   and to force its answer into the CatalystVerdict shape via tool use
   (define an Anthropic tool schema matching CatalystVerdict exactly).

3. Implement agent/catalyst_gate.py's check_catalyst(): calls the Anthropic
   API with that prompt and tool-forces a CatalystVerdict response. On a
   schema validation failure or any API error, return
   CatalystVerdict(catalyst_risk=True, reasoning="fail-closed: <error>") —
   never silently skip a name.

4. Write agent/prompts/structure_agent.md and implement
   agent/structure_agent.py's propose_structure(): give Claude the option
   chain data, screen metrics, and sleeve_a_carry config (width, delta
   range, DTE range from Settings), force a ProposedStructure response via
   tool use. This function must not call broker.alpaca_client or
   execution.orders at all — it returns data, nothing else.

5. Write tests/test_agent.py with both Anthropic and Featherless calls
   mocked: a well-formed model response parses correctly into each schema;
   a malformed catalyst-gate response results in catalyst_risk=True (test
   the fail-closed path explicitly, don't just assume it); a
   headline_triage failure doesn't raise and returns a neutral digest.

6. Run one real end-to-end call of both stages against a name that passed
   Phase 06's screen (small budget — a couple of real API calls is fine)
   and show me the actual CatalystVerdict and ProposedStructure it produced.
```

---

## Phase 08 — Endgame state machine

```
Implement the full dated logic for both sleeves — read endgame/schedule.py's
docstring for the Phase enum and function signatures.

1. Implement current_phase(clock) using broker.clock.py's queries against
   config/settings.yaml's `calendar` section: BUILD (before
   first_full_session) -> CARRY_ACTIVE (through last_carry_entry_day) ->
   UNWIND (carry_unwind_day) -> CONVEXITY_ENTRY (carry_unwind_day, after
   convexity_entry_after_et ET) -> HOLD_THROUGH_NFP -> MONETIZE (after
   nfp_release_et on submission day, before flatten_by_et) -> FLAT (after
   flatten_by_et) -> POST_DEADLINE (after submission_deadline_et). Get the
   exact boundary semantics right — the flatten_by_et and
   submission_deadline_et lines are the two the whole design depends on
   never being fuzzy.

2. Implement allowed_actions(phase) -> set[str] mapping each phase to
   what's permitted: CARRY_ACTIVE allows new Sleeve A entries and closes;
   UNWIND allows only Sleeve A closes; CONVEXITY_ENTRY allows a Sleeve B
   open (check sleeve_b_convexity.escalate_if_nav_above_start against
   current NAV vs account.starting_nav to decide base vs escalated risk
   size); MONETIZE allows only Sleeve B closes; FLAT and POST_DEADLINE
   allow nothing but reads.

3. Wire risk/gates.py's pre-NFP-flatten-requirement gate (stubbed in
   Phase 03) to call current_phase() and reject any new Sleeve A entry
   once phase is past CARRY_ACTIVE. Remove that TODO.

4. Write tests/test_schedule.py using freezegun, frozen at both sides of
   every boundary named in config/settings.yaml's calendar section:
   last_carry_entry_day 23:59 vs carry_unwind_day 00:01,
   convexity_entry_after_et minus 1 minute vs plus 1 minute,
   nfp_release_et minus 1 minute vs plus 1 minute, flatten_by_et minus 1
   vs plus 1, submission_deadline_et minus 1 vs plus 1. Every single one of
   these needs an explicit test on both sides — this is the file where an
   off-by-one actually costs the competition.
```

---

## Phase 09 — Scheduler / autonomous loop

```
Wire every prior phase into the process that runs unattended. Read
scheduler/loop.py and cli.py's docstrings.

1. Implement scheduler/loop.py using APScheduler: every
   scheduler.cycle_interval_minutes (Settings), if broker.clock.py says the
   market is open (or always, if scheduler.market_hours_only is false),
   run one cycle: current_phase() -> (if phase allows entries)
   screen.universe.screen() -> for survivors: headline_triage ->
   catalyst_gate -> (if not catalyst_risk) structure_agent ->
   risk.engine.evaluate() -> execution.orders.submit() -> always finish
   with execution.reconcile.reconcile(). Wrap each stage in its own
   try/except that logs and continues to the next candidate (or to
   reconcile, if the failure is pipeline-wide) — one bad candidate or one
   flaky API call must not take down the loop or skip reconciliation.

2. Implement cli.py's full dispatch (it's currently a stub with
   NotImplementedError): `run-cycle` calls the loop's single-cycle function
   once and exits; `status` prints current NAV, phase, open positions,
   kill-switch state; `flatten` forces every open position closed
   regardless of phase (an emergency override — this one should log loudly
   that it was manually invoked); `verify` runs scripts/verify_day1.py;
   `journal export` calls journal.export.export_writeup and
   export_trade_log_csv and prints where it wrote them.

3. Write tests/test_scheduler.py: mock every stage, confirm a cycle runs
   start to finish in the happy path; confirm a mocked failure in
   catalyst_gate for one candidate doesn't stop the cycle from processing
   the rest; confirm reconcile() always runs even if an earlier stage threw.

4. Run `barbell run-cycle` for real, once, against the live paper account
   in whatever phase current_phase() actually returns right now, and walk
   me through what happened at each stage using barbell status and the
   journal.
```

---

## Phase 10 — Dashboard & observability

```
Build this any time after Phase 04 — it's off the critical path and
doesn't block anything else. Read dashboard/app.py's docstring.

Implement it as a Streamlit app, read-only against the journal DB
(BARBELL_DB_PATH from Settings) — no writes, no calls to
broker.alpaca_client from this file. Show: NAV vs. the $100,000 starting
value as a line or delta metric; Sleeve A vs. Sleeve B realized/unrealized
P&L; a table of currently open positions from the latest
positions_snapshot; a live feed of the most recent risk_decisions rows
(symbol, outcome, reason); current kill-switch status; current
endgame phase from endgame.schedule.current_phase(); and a countdown to
config/settings.yaml's submission_deadline_et. Auto-refresh on a short
interval (st.rerun with a sleep, or Streamlit's built-in auto-refresh
pattern) so it's usable as a live monitor during market hours, not just a
one-time snapshot.

Run `streamlit run dashboard/app.py` against whatever's in the journal DB
right now and confirm it renders without errors.
```

---

## Phase 11 — Testing & verification

```
This is a verification pass, not new features — don't add functionality
here, find what's wrong with what exists.

1. Run the full suite: `pytest --cov=barbell tests/ -v`. Report the
   coverage numbers, called out separately for risk/ and endgame/ (these
   should be the highest of any package in the repo — if they're not,
   that's a problem to fix before anything else in this phase).

2. Do one full integration dry run: run `barbell run-cycle` once, watched
   manually end to end, confirming each journal table actually received
   the rows you'd expect for whatever the market did that cycle.

3. Deliberately break things to confirm the safety claims are real, not
   theoretical:
   - Manually trip risk.kill_switch (call check_and_latch with a NAV
     8%+ below start) and confirm a subsequent run-cycle rejects every
     new entry and that barbell status shows it latched
   - Feed risk.engine.evaluate() a ProposedStructure sized to breach the
     per-position cap and confirm it resizes down or vetoes, and that this
     shows up correctly in a risk_decisions row
   - Force execution.reconcile to return diverged=True (e.g. by
     manually inserting a fake position into the journal that doesn't
     match the broker) and confirm new entries are blocked until it clears

Report the results of all three as pass/fail, not just "looks fine" — I
need to know this actually works before it runs unattended.
```

---

## Phase 12 — Deployment & runtime

```
Get scheduler/loop.py running unattended and surviving a restart.

1. Write a systemd unit file (deploy/barbell.service) or, if we're running
   this on a plain machine instead of a VPS, a documented tmux/nohup
   invocation in docs/architecture.md's Runtime section — ask me which
   before picking one if it isn't already obvious from how .env is set up.

2. Confirm kill-switch and endgame-phase state both survive a process
   restart — restart the process mid-session (paper account, no open
   positions at risk) and confirm barbell status shows the same state as
   before the restart, sourced from the journal DB, not from memory.

3. If we're hosting the Streamlit dashboard at a public URL for the
   submission, wire that up too and confirm it's reachable from outside
   the host.

Report what you set up and how to restart/monitor it.
```

---

## Phase 13 — Live trading operation

```
Not a build task — write a short operational runbook,
docs/runbook.md, covering: what to check at market open each day
(barbell status, dashboard, verify_day1 items 4 and 7 haven't drifted);
what a normal cycle's journal output looks like versus a concerning one
(e.g. a sudden run of VETOs on the same gate suggests a miscalibrated
threshold, not a market event); how to manually invoke `barbell flatten`
if something looks wrong and you want a human-forced exit instead of
waiting for the endgame state machine; and what NAV trajectory would
trigger a mid-week reconsideration of sizing versus what's within the
expected range from the strategy design (roughly flat to +1.6% from Sleeve
A alone, per the original sizing math in docs/architecture.md).

This file is for a human glancing at the system during the week, not for
another Claude Code session — write it plainly.
```

---

## Phase 14 — Presentation assets

```
Generate the actual submission materials from what the system has really
done, not from a description of what it was supposed to do.

1. Run `barbell journal export` against the real, current journal DB and
   show me docs/writeup_generated.md. Check it actually covers the three
   things the hackathon rules ask for explicitly: AI logic, risk gates,
   Alpaca infrastructure — if journal.export's current template is missing
   any of the three, extend export_writeup() to cover it properly rather
   than me hand-editing the generated file every time the DB changes.

2. Write a small script, scripts/export_slide_stats.py, that pulls the
   handful of real numbers a slide deck would want (starting NAV, current
   NAV, per-sleeve realized P&L, number of trades placed, number of gate
   rejections and the top 2-3 reasons, number of catalyst-gate vetoes) into
   one clean block of text or a small JSON file — something to paste into
   slides without re-deriving numbers by hand each time.

Don't write the video script or the deck itself here — those are mine to
write from real content; this phase is just making sure the real content
is easy to pull out of the system accurately.
```

---

## Phase 15 — Submission

```
Final repo audit before submitting — read-only, report findings, don't
fix anything without telling me first.

Check: .env is git-ignored and was never committed (search git history,
not just the current .gitignore); no API key or secret appears anywhere in
tracked files; the README's quickstart commands actually work against a
clean checkout; CI is green on the current main branch; data/barbell.db
(or wherever BARBELL_DB_PATH points) is git-ignored so we're not
accidentally shipping the live trading journal into the public repo; the
account ID scripts/seed_paper_account.py wrote to data/account_id.txt
matches the account that actually has the trading history on it.

Report every finding as pass/fail with the specific evidence (file path,
line, or command output) — this is the last check before the repo goes
public and gets linked in the submission form.
```
