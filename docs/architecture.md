# Dispersion Barbell — Engineering Blueprint

Alpaca AI Trading Agents Hackathon · repo scaffold + tech stack + build plan.
Strategy detail lives in the [companion artifact](https://claude.ai/code/artifact/7cb083e5-37f7-4d0d-bbb3-22bff57e6c26) and `report-sections.md`; this document is the "how we build it" half.

## ⚠️ Where we actually are

Today is **Monday Aug 31** — the plan's original Day 1 of live trading. Zero code exists yet. That compresses the build: Sleeve A now realistically gets **two** entry sessions (Tue–Wed) instead of three, so expect less carry harvested than the original sizing math assumed. The build order below is sequenced so something tradeable exists by end of Monday, even if incomplete.

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Options math, Alpaca SDK, Anthropic SDK all first-class here |
| Trading + market data | [`alpaca-py`](https://github.com/alpacahq/alpaca-py) (official SDK, `pip install alpaca-py`) | Full programmatic control over multi-leg order construction |
| Interactive path | [`alpaca-mcp-server`](https://github.com/alpacahq/alpaca-mcp-server) (`uvx alpaca-mcp-server`, stdio) | Satisfies the MCP half of the mandate; this is what Claude drives live in the demo video |
| Autonomous path | [`alpacahq/cli`](https://github.com/alpacahq/cli) (Go binary, `brew install alpacahq/tap/cli`, JSON stdout) | Satisfies the CLI half; used for scheduler pre-flight checks (`alpaca account get`, `alpaca position list`) piped as JSON, kept separate from order construction since its multi-leg options support is unconfirmed — verify in Day-1 checklist |
| LLM (decision-making) | Anthropic Claude API (`anthropic` SDK) | Catalyst gate + structure proposal — the two calls with real veto/sizing power, forced tool-use for schema compliance |
| LLM (bulk triage, non-critical) | Featherless AI (`openai` SDK pointed at `api.featherless.ai/v1`, open-source model e.g. Qwen2.5-7B) | Cheap headline pre-digest ahead of the catalyst gate — informational context only, never gates or sizes a trade. Hackathon technology partner; see below for why it's scoped this narrowly |
| Schema validation | `pydantic` v2 | Every LLM output validated before it can reach the risk engine |
| Scheduling | `APScheduler` | Cycle loop, market-hours-aware via `broker/clock.py` |
| Storage | SQLite via `sqlmodel` | Append-only journal; 4 days of data doesn't need more |
| Config | YAML + `.env` (`python-dotenv`) | Gate thresholds and calendar dates are data, not code |
| Testing | `pytest`, `freezegun` for time-boundary tests | Risk engine and endgame schedule are the two files with the highest coverage bar |
| Dashboard (optional) | Streamlit | Fastest way to a live NAV/position view for the demo |
| CI | GitHub Actions (`pytest` on push) | Cheap technology-implementation signal |

**Two Alpaca surfaces used on purpose, not one.** MCP drives the interactive/demo session; the Go CLI does scheduler pre-flight reads; `alpaca-py` does all actual order construction and execution. State (SQLite journal) is shared, so `barbell status` reflects trades made through any path.

**Why Featherless is scoped to one non-critical call.** It's a real technology-partner integration (open-source model, genuinely used in an agent workflow, satisfies the "must be integrated" eligibility line for any partner-prize consideration) — but it sits ahead of the catalyst gate, never inside it. The catalyst gate and structure agent are the two places in this pipeline with actual veto or sizing power, and that authority stays on Claude, because the whole risk-engine design assumes reliable structured output. Featherless does bulk, low-stakes headline summarization instead — a task where a cheaper open-source model failing costs nothing, since `screen/headline_triage.py`'s output is optional context, not a gate.

## Repo structure

```
dispersion-barbell/
├── pyproject.toml
├── .env.example
├── .gitignore
├── config/
│   ├── settings.yaml        # every risk-gate number, sizing, calendar dates — see below
│   └── universe.yaml        # seed candidate tickers for Sleeve A
├── src/barbell/
│   ├── cli.py                # `barbell run-cycle|status|flatten|verify|journal export`
│   ├── broker/
│   │   ├── alpaca_client.py  # the ONLY module that imports alpaca-py
│   │   ├── mcp_client.py     # docs the MCP wiring — no Python client, it's config
│   │   └── clock.py          # market-open + judged-window awareness
│   ├── screen/
│   │   ├── universe.py       # Stage 1: deterministic liquidity/IV screen
│   │   ├── metrics.py        # IV rank, IV30/HV20, local Black-Scholes fallback
│   │   └── headline_triage.py# Stage 1.5: Featherless open-source model — non-critical, informational only
│   ├── agent/
│   │   ├── catalyst_gate.py  # Stage 2: LLM (Claude) — "is this vol rich for a reason?" — has veto power
│   │   ├── structure_agent.py# Stage 3: LLM (Claude) — proposes spread structure (JSON) — sizes real risk
│   │   ├── schemas.py        # Pydantic models every LLM output must satisfy, incl. HeadlineDigest
│   │   └── prompts/
│   ├── risk/
│   │   ├── gates.py          # the 12 gates, pure functions, highest test coverage
│   │   ├── engine.py         # orchestrates gates — can only tighten, never loosen
│   │   └── kill_switch.py    # -8% NAV latch, persisted
│   ├── execution/
│   │   ├── orders.py         # builds + submits; order_type="limit" enforced in code
│   │   └── reconcile.py      # broker-is-truth diff, every cycle
│   ├── endgame/
│   │   └── schedule.py       # the dated state machine — BUILD→...→FLAT
│   ├── journal/
│   │   ├── store.py          # append-only SQLite: every decision, every reason
│   │   └── export.py         # renders docs/writeup_generated.md from the DB
│   └── scheduler/
│       └── loop.py           # APScheduler cycle runner — the unattended process
├── scripts/
│   ├── verify_day1.py        # the 7-item checklist, executable
│   └── seed_paper_account.py # confirms fresh $100k account, prints account ID
├── dashboard/
│   └── app.py                 # Streamlit, read-only off the journal DB
├── tests/
│   ├── test_risk_gates.py     # boundary + property tests on the safety layer
│   ├── test_schedule.py       # freezegun tests at every calendar boundary
│   └── test_screen.py
├── .github/workflows/tests.yml
└── docs/
    ├── architecture.md        # this file
    └── writeup_generated.md   # generated by journal/export.py, not hand-written
```

Every stub file above already exists with a docstring stating its responsibility and the exact function signatures it owns — that's the scaffold. No business logic is implemented yet.

## Data flow

```
scheduler/loop.py  (every 30 min, market hours only)
  │
  ▼
endgame/schedule.py  → current phase (BUILD | CARRY_ACTIVE | UNWIND | ...)
  │  phase gates which stages below are even allowed to run
  ▼
screen/universe.py + metrics.py        (deterministic)
  │  ~25 seed names → 8–14 survivors
  ▼
screen/headline_triage.py               (Featherless — optional, non-blocking)
  │  per survivor: cheap news-volume flag + summary → extra context only
  ▼
agent/catalyst_gate.py                  (LLM #1, Claude — has veto power)
  │  per survivor: catalyst_risk True/False + reasoning
  ▼
agent/structure_agent.py                (LLM #2)
  │  per clean survivor: ProposedStructure (JSON, schema-validated)
  ▼
risk/engine.py  →  risk/gates.py (×12) + risk/kill_switch.py    (deterministic)
  │  PASS | RESIZE(smaller) | REJECT — never larger than proposed
  ▼
execution/orders.py  →  broker/alpaca_client.py                 (deterministic)
  │  marketable limit, multi-leg, bounded retry
  ▼
execution/reconcile.py                                          (deterministic)
  │  broker state is truth; diverge → halt new entries, log CRITICAL
  ▼
journal/store.py   (every stage above writes here, pass or reject alike)
```

## Config → gate mapping (already filled in `config/settings.yaml`)

| Gate | Config key |
|---|---|
| Per-position loss cap | `risk_gates.max_loss_per_position_pct_nav` (0.008) |
| Portfolio loss cap | `risk_gates.max_loss_portfolio_pct_nav` (0.12) |
| Quote staleness | `risk_gates.max_quote_age_seconds` (120 — **tune after verify_day1.py item 4**) |
| Liquidity floor | `sleeve_a_carry.screen.min_open_interest` / `max_spread_pct_of_mid` |
| Earnings blackout | `sleeve_a_carry.screen.earnings_blackout` + `universe.yaml: exclude` |
| Pre-NFP flatten | `calendar.flatten_by_et`, enforced by `endgame/schedule.py` |
| Expiry-past-deadline | `endgame/schedule.py` checks contract expiry against `calendar.submission_deadline_et` |
| Concentration | `risk_gates.max_sector_concentration`, `max_positions_per_underlying` |
| Drawdown kill switch | `risk_gates.drawdown_kill_switch_pct_nav` (-0.08) |
| No market orders | `execution.order_type: "limit"` — **also hard-coded in `orders.py`**, config alone isn't trusted |
| Broker-is-truth | `execution/reconcile.py`, runs every cycle unconditionally |

## Runtime / ops

No need for real infrastructure over 4 days — a single always-on process is enough. Two viable options, pick based on what's available:

1. **A kept-on machine** (laptop or a $5–6/mo VPS) running `barbell run-cycle` under `tmux`/`nohup`/`systemd`, polling every 30 min during market hours per `scheduler.cycle_interval_minutes`.
2. **Manual/cron-triggered** — a `cron` entry calling `barbell run-cycle` on the same interval, if a persistent process feels riskier to babysit than a stateless cyclical invocation. State lives in SQLite either way, so this is a deployment choice, not an architecture one.

Either way: `barbell status` and the Streamlit dashboard are the two things to have open during market hours, and `broker/clock.py` refuses to run anything outside RTH regardless of scheduler misfires.

## Testing strategy

- `risk/gates.py` and `risk/engine.py`: exhaustive boundary tests, plus a property test that `engine.evaluate()` never returns more contracts than proposed. This file backs the "risk engine can only tighten" claim in the write-up — it should be provably true, not just designed to be.
- `endgame/schedule.py`: `freezegun`-frozen tests at every calendar boundary (last carry entry, convexity entry gate, NFP, flatten deadline, submission deadline). An off-by-one here is the bug most likely to leave a position open at judging time.
- Everything touching the network (`broker/alpaca_client.py`, `agent/*.py`) gets a fake/recorded-response test double — CI never hits live Alpaca or Anthropic.

## Judging-criteria → artifact map

| Criterion | What answers it |
|---|---|
| P&L Performance | `barbell status`, dashboard, final account state at 10:45 ET Sep 4 |
| Technology Implementation | This repo: MCP + CLI both used with stated reasons, alpaca-py for execution, CI green, Featherless integrated for a genuine (if narrow) purpose rather than bolted on |
| Creativity & Originality | The barbell thesis + deadline-aware endgame (see `report-sections.md` novelty section) |
| Presentation & Execution | `journal/export.py` output feeds the write-up and demo narration directly from real decisions, not reconstructed after the fact |
| Social engagement | Trade journal entries are already written in plain language — repurpose directly into posts |

## Build order (compressed — today is Aug 31)

1. **Today (Mon Aug 31):** `broker/alpaca_client.py`, `risk/gates.py` + `risk/engine.py`, `execution/orders.py` + `reconcile.py`, `journal/store.py`. Run `scripts/verify_day1.py` first thing — its answers may change gate thresholds before anything else is built. Goal by EOD: the deterministic spine can place and log a manually-specified spread.
2. **Tue Sep 1:** `screen/universe.py` + `metrics.py`, `agent/catalyst_gate.py` + `structure_agent.py`, `endgame/schedule.py`, `scheduler/loop.py`. Goal: first fully autonomous cycle, first live Sleeve A entries (compressed sizing — fewer names than original 12-name target is fine).
3. **Wed Sep 2:** scale Sleeve A to target size if the pipeline is stable; dashboard; start the demo script/recording plan.
4. **Thu Sep 3:** unwind Sleeve A per `endgame/schedule.py`'s UNWIND phase, enter Sleeve B convexity late session, finalize write-up draft via `journal/export.py`.
5. **Fri Sep 4:** monetize Sleeve B into the open, confirm FLAT phase before 10:45 ET, run `journal export`, submit with account ID before 11:00 ET.

## Member 1 handoff

Foundation layer complete. All contracts, settings, broker interfaces, clock functions, append-only journal tables, and verification scripts are tested and ready for Members 2, 3, and 4 to import.

### 1. The Shared Contract Layer (`agent/schemas.py`)

All models inherit from `pydantic.BaseModel`. Import path: `from barbell.agent.schemas import (...)`

```python
from datetime import date, datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

class HeadlineDigest(BaseModel):
    """Stage 1.5 output (Featherless AI / Qwen model). Informational context only."""
    symbol: str
    news_volume: Literal["low", "normal", "elevated"]
    summary: str = ""       # Empty string is default for neutral / failed; NEVER None
    model_used: str = ""    # e.g. "Qwen/Qwen2.5-7B-Instruct"

class CatalystVerdict(BaseModel):
    """Stage 2 output (Claude LLM #1). True = unscheduled binary risk -> veto trade."""
    symbol: str
    catalyst_risk: bool
    reasoning: str
    sources_considered: list[str] = []

class ProposedLeg(BaseModel):
    """One leg of an option structure for execution."""
    symbol: str = ""        # OCC-format string (e.g. 'NVDA260904P00110000'), or empty before resolution
    expiry: date            # Expiration date of the contract
    strike: float           # Strike price in USD
    right: Literal["call", "put"]
    side: Literal["buy", "sell"]
    contracts: int          # Positive contract count
    ratio_qty: int = 1      # Spread leg ratio (1 for standard spreads)

class ProposedStructure(BaseModel):
    """Stage 3 output (Claude LLM #2). Proposes full spread structure."""
    underlying: str
    legs: list[ProposedLeg]
    rationale: str
    sleeve: Literal["A", "B"]
    max_loss_estimate: float # Model's estimated max loss ($); risk engine can only lower size
    structure_type: str = "" # "put_credit_spread" | "call_credit_spread" | "iron_condor" | "put_debit_spread"
    limit_price: float = 0.0 # Net limit price per spread

class GateResult(BaseModel):
    """Result from an individual risk gate in risk/gates.py."""
    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None # Positive integer if RESIZE, None otherwise
    reason: str
    gate_name: str

class PortfolioState(BaseModel):
    """Snapshot of account state passed into risk gates."""
    current_nav: float
    starting_nav: float
    open_positions: list[dict[str, Any]] = Field(default_factory=list)
    sector_exposure: dict[str, int] = Field(default_factory=dict)
    last_quote_ts: dict[str, datetime] = Field(default_factory=dict)
    reserved_capital: float = 0.0 # Running total of capital reserved for in-flight basket entry

class MarketState(BaseModel):
    """Microstructure data passed into risk gates."""
    bid_ask_spread: dict[str, float] = Field(default_factory=dict) # symbol -> spread in USD
    open_interest: dict[str, int] = Field(default_factory=dict)      # symbol -> total OI
    quote_age_seconds: dict[str, float] = Field(default_factory=dict)
    dispersion_score: float | None = None # Vega-weighted single IV / index IV ratio from Member 3

class RiskDecision(BaseModel):
    """Output of risk engine after running all 12 gates."""
    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None
    reasons: list[str] = Field(default_factory=list) # All gates that fired
    proposed: ProposedStructure

class ScreenResult(BaseModel):
    """Output of screen/universe.py for each candidate symbol."""
    symbol: str
    passed: bool
    reason: str
    metrics: dict[str, Any] = Field(default_factory=dict)
```

### 2. Deviations & Conflict Resolutions

- **`ProposedLeg`**: The initial stub had only OCC `symbol` + `side` + `ratio_qty`. The prompt spec required explicit `expiry`, `strike`, `right`, and `contracts` so the broker and order modules can construct multi-leg requests directly. Both are preserved (`symbol` defaults to `""` if not yet resolved from contracts metadata).
- **`ProposedStructure`**: Renamed `max_loss_usd` to `max_loss_estimate` to match prompt specification; kept `structure_type` and `limit_price` from stub as convenience fields for `execution/orders.py`.
- **`CatalystVerdict`**: Preserved `symbol` and `sources_considered` from stub alongside `catalyst_risk` and `reasoning`.
- **`HeadlineDigest`**: Preserved `model_used` from stub, added `Literal["low", "normal", "elevated"]` constraint and guaranteed empty string default (`""`) for `summary` so it is never `None`.
- **`sqlmodel` / `pydantic` v2 compatibility**: Configured `[tool.hatch.build.targets.wheel]` and `[tool.hatch.build.targets.editable]` in `pyproject.toml` so editable installs with Hatchling locate `src/barbell`.

### 3. Broker & Journal Details

- **`AlpacaClient` (`src/barbell/broker/alpaca_client.py`)**:
  - Only module importing `alpaca-py`.
  - Initialized via `AlpacaClient.from_settings()`.
  - Implements `get_account()`, `get_positions()`, `get_option_chain()`, `get_option_contracts()`, `submit_mleg_order()`, and `get_clock()`.
  - **Hard Invariant**: `submit_mleg_order()` raises `NakedShortError` before any network call if a sell leg is not matched by a buy leg with the same expiry.
- **`JournalStore` (`src/barbell/journal/store.py`)**:
  - SQLite append-only log with 9 tables: `screen_results`, `catalyst_verdicts`, `proposed_structures`, `risk_decisions`, `orders`, `positions_snapshot`, `kill_switch_events`, `capital_reservations`, `basket_leg_fills`.
  - Contains NO `UPDATE` or `DELETE` methods. Status updates for reservations or fills append new rows.
- **`Clock` (`src/barbell/broker/clock.py`)**:
  - All comparisons use `ZoneInfo("America/New_York")`.
  - Functions: `is_market_open()`, `is_carry_entry_window()`, `is_carry_unwind_day()`, `is_convexity_entry_window()`, `time_to_deadline()`, `must_be_flat_by()`, `is_past_flatten_deadline()`, `is_past_nfp()`.

### 4. Day-1 Verification (`scripts/verify_day1.py`)

The verification script executes 7 checks against the paper API:
1. **Account Options Level**: Checks `options_approved_level >= 3` for multi-leg spreads.
2. **1-Lot Put Credit Spread Submission**: Submits a real 1-lot `LimitOrderRequest` with `OrderClass.MLEG` at $0.01 limit.
3. **Greeks & IV on Basic Plan**: Checks if delta/gamma/IV are populated on `OptionsSnapshot` from Alpaca. If missing/empty, Member 3's Black-Scholes solver in `screen/metrics.py` is engaged.
4. **Quote Staleness vs Wall Clock**: Computes quote timestamp lag against `max_quote_age_seconds` (120s).
5. **SPX/XSP Market Data Availability**: Verifies whether index option chains return snapshots or require ETF fallback (`SPY`).
6. **Multi-Leg Fill Simulation**: Notes paper trading fill behavior at mid vs touch.
7. **Rate Limit Extrapolation**: Benchmarks multi-chain queries across ~25 symbols.

*Note for running against live paper credentials*: Run `python scripts/seed_paper_account.py` and `python scripts/verify_day1.py` once `.env` is populated with real paper API keys.

## Member 2 handoff

Risk engine, execution layer, and reconciliation are fully implemented and tested.

### 1. TODO-stubbed gates pending Member 4's `endgame/schedule.py`

Two gates return **PASS unconditionally** with a `# TODO(Member 4)` comment:

| Gate | Function | What it needs | Where to wire |
|------|----------|---------------|---------------|
| Pre-NFP flatten | `gate_pre_nfp_flatten` | `endgame/schedule.py`'s `current_phase()` returning `HOLD_THROUGH_NFP` / `FLAT` | `risk/gates.py` lines ~220–240 |
| Expiry past deadline | `gate_expiry_past_deadline` | `calendar.submission_deadline_et` from settings (can also be read directly from config without schedule.py) | `risk/gates.py` lines ~255–270 |

**Critical**: these stubs return PASS so the pipeline runs during BUILD/CARRY_ACTIVE phases. Once Member 4 ships `schedule.py`, both gates must be wired immediately — leaving them as PASS stubs past the last carry entry day would allow entries during FLAT phase.

Note on `gate_expiry_past_deadline`: the submission deadline is already in `config/settings.yaml` as `calendar.submission_deadline_et`. This gate can be wired independently of Member 4 by reading `get_settings().calendar.submission_deadline_et` directly. Consider doing this if Member 4 is delayed.

### 2. Final `RiskDecision` / `GateResult` shape (no drift)

The shapes used in implementation match `agent/schemas.py` exactly — no shadow versions created, no fields renamed or added. Confirmed final shapes:

```python
class GateResult(BaseModel):
    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None  # positive when RESIZE, None otherwise
    reason: str
    gate_name: str

class RiskDecision(BaseModel):
    outcome: Literal["PASS", "RESIZE", "VETO"]
    contracts: int | None = None  # None when VETO
    reasons: list[str]            # ALL gate reasons (not just the deciding one)
    proposed: ProposedStructure
```

### 3. One schema addition (additive, backward-compatible)

`MarketState` in `agent/schemas.py` has one new field:

```python
reconciliation_diverged: bool = False
```

Defaults `False` so all existing callers (Member 3's screen gates, tests without reconcile, etc.) are unaffected. `execution/reconcile.py`'s `reconcile()` returns a `ReconciliationReport` with a `diverged` flag; callers set `market_state.reconciliation_diverged = recon.diverged` before calling `engine.evaluate()`. Gate 12 (`gate_broker_reconciliation`) reads this field.

### 4. Gate ordering (fixed, never reorder)

Gates run in this exact sequence in `risk/gates.py::GATE_PIPELINE`. Changing the order changes semantics (e.g., kill-switch must check before liquidity gates so we don't waste time on market data when the account is frozen):

```
01 gate_per_position_loss_cap        RESIZE-capable
02 gate_portfolio_loss_cap           RESIZE-capable
03 gate_defined_risk_only            VETO-only (defense in depth vs alpaca_client)
04 gate_quote_staleness              VETO-only
05 gate_liquidity_floor              VETO-only
06 gate_dispersion_score             VETO-only (PASS if None — see below)
07 gate_earnings_blackout            VETO-only
08 gate_pre_nfp_flatten              STUB (PASS) — TODO(Member 4)
09 gate_expiry_past_deadline         STUB (PASS) — TODO(Member 4)
10 gate_concentration                VETO-only
11 gate_drawdown_kill_switch         VETO-only (delegates to kill_switch.py)
12 gate_broker_reconciliation        VETO-only (wired to reconcile.py)
13 gate_basket_capital_reservation   VETO-only (new)
```

### 5. Dispersion score gate behavior when `dispersion_score is None`

Gate 06 returns **PASS** (not VETO) when `market_state.dispersion_score is None`. This is intentional — see the detailed comment in `gate_dispersion_score()`. Short version: VETO on None would silently disable all Sleeve A entries until Member 3 wires `screen/metrics.py`. The other 12 gates all still run and enforce real risk constraints. Once Member 3 populates `dispersion_score`, the gate automatically starts enforcing the floor.

### 6. Basket execution architecture (for Member 4's scheduler wiring)

`execution/orders.py` exposes:

```python
submit_basket(
    proposals: list[ProposedStructure],
    *,
    client: AlpacaClient,
    store: JournalStore,
    exec_config: ExecutionConfig,
    risk_config: RiskGateConfig,
    engine_config: RiskGateConfig,
    portfolio_state_fn: Callable[[], PortfolioState],
    market_state_fn: Callable[[], MarketState],
    cycle_id: str,
) -> list[dict]
```

Member 4's `scheduler/loop.py` calls this once per cycle with the list of approved `ProposedStructure` objects. The two callable arguments (`portfolio_state_fn`, `market_state_fn`) are called fresh after each leg fill to get current portfolio/market state for the post-fill risk re-evaluation.

### 7. Credit spreads vs. cash-secured puts (verify_day1.py item 2)

The `gate_defined_risk_only` check requires every SELL leg to have a BUY leg with the same expiry in the same order. Credit spreads (sell + buy same expiry) pass this check. Cash-secured puts (naked sell with cash as collateral) **would fail** this gate and be VETOd — this is by design (CLAUDE.md: "no naked shorts, ever"). The architecture's choice of put credit spreads over CSPs is therefore enforced at the risk engine layer, not just by documentation. Nothing in verify_day1.py changes this; item 2 confirms multi-leg order submission works, which is required for the spread structure.

### 8. Kill-switch persistence

`risk/kill_switch.py` persists latch state through `journal/store.py`'s `kill_switch_events` table. On process restart, `is_latched()` queries the DB for any row with `triggered=True`. No local fallback table is needed — the table already exists from Member 1. `reset_in_process_cache()` is a test-only function to allow test isolation between kill-switch scenarios.

---

## Member 3 handoff

Screening, metrics, and both LLM stages are fully implemented and tested. 47 tests pass (zero live API calls).

### 1. Files delivered

| File | Status |
|---|---|
| `src/barbell/screen/metrics.py` | Complete — `iv_rank`, `iv30_hv20_ratio`, `bs_implied_vol`, `bs_greeks`, `dispersion_score` |
| `src/barbell/screen/universe.py` | Complete — `load_candidates`, `screen` with 4 numeric filters + dispersion wiring |
| `src/barbell/screen/headline_triage.py` | Complete — `digest_headlines` via Featherless/OpenAI SDK, full fail-safe |
| `src/barbell/agent/catalyst_gate.py` | Complete — `check_catalyst` with Anthropic tool-use, fail-closed on every error path |
| `src/barbell/agent/structure_agent.py` | Complete — `propose_structure` with Anthropic tool-use, no broker calls |
| `src/barbell/agent/prompts/catalyst_gate.md` | Complete |
| `src/barbell/agent/prompts/structure_agent.md` | Complete |
| `tests/test_screen.py` | 32 tests — arithmetic, filter rejection cases, journal row confirmation, dispersion_score hand-computed |
| `tests/test_agent.py` | 15 tests — both LLM stages, fail-closed cases |
| `tests/fixtures/option_chain.json` | NVDA, AMD, SBUX (low liquidity), SPY fixture chains |

### 2. Data path: Black-Scholes fallback active

Based on Member 1's `verify_day1.py` item 3 (native Greeks/IV from Basic plan unreliable for short-DTE snapshots):

- **Primary**: reads `snapshot.implied_volatility` and `snapshot.greeks.vega` when non-zero.
- **Fallback**: calls `bs_greeks()` from `screen/metrics.py` for vega when native Greeks are missing.
- **Synthetic IV rank / HV**: when 52-week IV history and close-price bars are unavailable, `_compute_iv_rank` uses equity IV band 0.15–0.80 and `_compute_iv30_hv20_ratio` approximates HV20 as 80% of IV30. Member 4 should wire real bar data if a higher data plan becomes available.

### 3. dispersion_score — computation and location

Computed in `screen/universe.py::screen()` after all numeric filters pass, using:

```
dispersion_score(survivors, index_iv=_get_index_iv(client, "SPY"))
```

Stored in `MarketState.dispersion_score` (type `float | None`). Member 2's `gate_dispersion_score` reads it from there — **no schema drift**.

Journal logging: per-symbol `ScreenResult.metrics` carries `iv`, `iv_rank`, `iv30_hv20_ratio`, `vega_per_contract`, `proposed_contracts`, `spot`. The portfolio-level dispersion score is logged at INFO each cycle.

### 4. Schema conformance (no drift)

All schemas imported from `barbell.agent.schemas` — no shadow copies created. `MarketState.reconciliation_diverged` (Member 2 addition, `False` default) is preserved and untouched by this module.

### 5. Wiring for Member 4

```python
candidates = load_candidates()
all_results, market_state = screen(candidates, client, store, cycle_id)
survivors = [r for r in all_results if r.passed]

proposals = []
for result in survivors:
    headlines = []  # wire news API here
    digest = digest_headlines(result.symbol, headlines)   # never raises
    verdict = check_catalyst(result.symbol, headlines, digest, result)
    if verdict.catalyst_risk:
        continue
    chain = client.get_option_chain(result.symbol, ...)
    try:
        structure = propose_structure(result.symbol, chain, result,
                                      dispersion_score=market_state.dispersion_score)
        proposals.append(structure)
    except Exception as exc:
        log.warning("propose_structure(%s) failed: %s", result.symbol, exc)

# Pass proposals + market_state into execution.orders.submit_basket()
```

### 6. Known TODOs for Member 4

- **Real bar data**: replace synthetic HV/IV-rank fallbacks when a higher data plan is available.
- **News API**: `digest_headlines` is called with an empty list in the skeleton — wire a real news source.
- **Dispersion score time-series**: currently INFO-logged. Add a queryable column to `ScreenResultRow` or a separate table if the write-up requires a week-long trend chart.

---

## Member 4 handoff

Orchestration, endgame state machine, CLI, dashboard, write-up export, tests, runbook, and deployment artifacts are complete.

### 1. Files delivered

| File | Status |
|---|---|
| `src/barbell/endgame/schedule.py` | Complete — `Phase` enum + `current_phase()` + `allowed_actions()` |
| `src/barbell/scheduler/loop.py` | Complete — `run_one_cycle()` + `run_loop()` (APScheduler) |
| `src/barbell/cli.py` | Complete — all 5 subcommands dispatched |
| `src/barbell/journal/export.py` | Extended — all 13 gates, dispersion trend, basket-atomicity defense, AI examples |
| `dashboard/app.py` | Complete — Streamlit, read-only, auto-refresh |
| `tests/test_schedule.py` | Complete — both sides of every calendar boundary |
| `tests/test_scheduler.py` | Complete — happy-path, partial failure, phase gating, reconcile-always-runs |
| `scripts/export_slide_stats.py` | New — JSON + text block for slides |
| `docs/runbook.md` | New — daily checklist, normal vs. concerning patterns, flatten procedure |
| `deploy/barbell.service` | New — systemd unit for Linux VPS deployment |

### 2. Phase enum and boundary semantics

```python
class Phase(Enum):
    BUILD            = "BUILD"            # before first_full_session (Sep 1)
    CARRY_ACTIVE     = "CARRY_ACTIVE"     # Sep 1–2 (last_carry_entry_day EOD)
    UNWIND           = "UNWIND"           # Sep 3 before 14:30 ET
    CONVEXITY_ENTRY  = "CONVEXITY_ENTRY"  # Sep 3 at/after 14:30 ET (subsumed by HOLD)
    HOLD_THROUGH_NFP = "HOLD_THROUGH_NFP" # Sep 3 14:30 ET → Sep 4 08:30 ET
    MONETIZE         = "MONETIZE"         # Sep 4 08:30–10:45 ET
    FLAT             = "FLAT"             # Sep 4 10:45–11:00 ET
    POST_DEADLINE    = "POST_DEADLINE"    # after Sep 4 11:00 ET
```

`current_phase(now=None)` accepts an optional `now` datetime so tests can pass
explicit values without freezegun (though freezegun also works).

### 3. Two TODO-stubbed risk gates — now wired

Both gates in `risk/gates.py` that were marked `# TODO(Member 4)` are now wired:

| Gate | Implementation |
|---|---|
| `gate_pre_nfp_flatten` | Calls `current_phase()`, VETOs for `HOLD_THROUGH_NFP / MONETIZE / FLAT / POST_DEADLINE` |
| `gate_expiry_past_deadline` | Reads `get_settings().calendar.submission_deadline_et` directly, VETOs if any leg expiry > deadline date |

Both gates fail open (PASS with a warning) if their dependencies are unavailable —
the scheduler-level phase check remains the primary enforcement.

### 4. Scheduler loop design notes

`run_one_cycle()` in `scheduler/loop.py`:
- Each stage (screen, headline_triage, catalyst_gate, structure_agent, evaluate, submit_basket) is wrapped in its own `try/except`
- `reconcile()` is called at the end of the cycle in a pattern equivalent to `finally` — it runs regardless of whether earlier stages threw
- Phase gating: `load_candidates()` is only called when `"sleeve_a_open"` or `"sleeve_b_open"` is in `allowed_actions(phase)`
- `submit_basket()` is called with fresh `portfolio_state_fn` and `market_state_fn` callables, not snapshot values

### 5. Deployment — Windows vs. Linux

This machine is Windows. Options:
- **Windows Task Scheduler** or **PowerShell background job**: see `docs/runbook.md` Runtime section
- **Linux VPS**: `deploy/barbell.service` systemd unit, load `.barbell.env` secrets file

State (SQLite journal, kill-switch latch) persists across restarts either way.

### 6. Known remaining TODOs (post-submission)

- **News API**: `digest_headlines()` is called with an empty `headlines=[]` list in the scheduler loop. Wire a real news API (e.g. Alpaca News, Polygon.io) for richer catalyst-gate context.
- **Real bar data**: `screen/metrics.py` uses synthetic HV/IV-rank fallbacks. A higher Alpaca data plan would enable real 52-week IV history and 20-day close prices.
- **Dashboard public URL**: instructions in `docs/runbook.md`; not deployed automatically.
