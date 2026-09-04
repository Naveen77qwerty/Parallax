# Parallax

Autonomous options agent for the [Alpaca AI Trading Agents Hackathon](https://alpaca.markets/learn/hackathon). Implements a **dispersion barbell** strategy: short rich single-name volatility through defined-risk credit spreads (Sleeve A), with a small cheap long-index-convexity hedge (Sleeve B). All positions are fully flat before the submission deadline.

> **Paper trading only.** `ALPACA_PAPER_TRADE=true` is a hard constraint enforced at every level of the stack. No code path can submit a live order.

Full engineering detail: [`docs/architecture.md`](docs/architecture.md)

---

## Strategy Overview

| | Sleeve A — Carry | Sleeve B — Convexity |
|---|---|---|
| **Instrument** | Put credit spreads / iron condors | SPY put debit spreads |
| **Thesis** | Short rich single-name IV; harvest premium where IV30 > HV20 and no binary catalyst is pending | Long cheap index tail insurance ahead of the Sep 4 NFP print |
| **Entry window** | Tue–Wed (screened each 30-min cycle) | Thu late session |
| **Exit** | Unwind phase begins Thu; hard flatten by Sep 4 10:45 ET | Monetize into the Sep 4 open |

The pipeline is fully autonomous: screen → catalyst gate → structure proposal → risk engine → execution → reconciliation. Every stage — passes and rejections alike — is written to an append-only SQLite journal.

---

## Architecture

```
scheduler/loop.py  (every 30 min, market hours only)
  │
  ▼
endgame/schedule.py       → current phase: BUILD | CARRY_ACTIVE | UNWIND |
  │                          CONVEXITY_ENTRY | HOLD_THROUGH_NFP | MONETIZE |
  │                          FLAT | POST_DEADLINE
  ▼
screen/universe.py + metrics.py        (deterministic)
  │  ~25 seed names → survivors (IV rank, IV30/HV20, liquidity)
  │  + dispersion_score() — vega-weighted single-name IV vs index IV
  ▼
screen/headline_triage.py              (Featherless AI — optional, non-blocking)
  │  cheap news-volume flag; informational context only, never gates a trade
  ▼
agent/catalyst_gate.py                 (Gemini LLM #1 — veto power)
  │  per survivor: catalyst_risk True/False + reasoning
  ▼
agent/structure_agent.py               (Gemini LLM #2 — sizes real risk)
  │  per clean survivor: ProposedStructure (JSON, schema-validated)
  ▼
risk/engine.py  →  risk/gates.py (×13) + risk/kill_switch.py
  │  PASS | RESIZE(smaller) | REJECT — never larger than proposed
  ▼
execution/orders.py  →  broker/alpaca_client.py
  │  limit orders only, multi-leg, bounded retry
  │  submit_basket() — sequential per-underlying entry with capital
  │  reserved in the journal before leg 1, re-evaluated after every fill
  ▼
execution/reconcile.py
  │  broker state is truth; diverge → halt new entries
  ▼
journal/store.py           (append-only — every decision logged)
```

### Key safety invariants

- **Risk engine can only tighten.** No gate may increase a proposed size or clear a veto. Enforced by a property test in `tests/test_risk_gates.py`.
- **Limit orders only.** `order_type="limit"` is enforced in `execution/orders.py` in code, not just config.
- **No naked shorts.** Every sell leg must be paired with a buy leg of the same expiry in the same multi-leg order, checked in both `broker/alpaca_client.py` (before any network call) and `risk/gates.py` (defense in depth).
- **`broker/alpaca_client.py` is the only module that imports `alpaca-py`.** All other modules call through its interface for full mockability.
- **Kill switch.** An NAV drawdown latches `risk/kill_switch.py` (persisted in the journal DB); it survives process restarts.
- **Basket capital reservation.** A multi-underlying Sleeve A basket reserves its full max-loss in the journal *before* the first leg is submitted, enters one underlying at a time, and re-runs the risk engine against each actual fill before building the next leg — the fix for the fact that Alpaca only guarantees atomic fills within one underlying's multi-leg order, never across underlyings.
- **Dispersion gate.** `gate_dispersion_score` vetoes new Sleeve A entries when vega-weighted single-name IV falls below index IV by too little to justify the trade; it passes (not vetoes) while `dispersion_score` hasn't been computed yet for the cycle, so absence of data never silently blocks the sleeve.

---

## Repo Structure

```
Parallax/
├── pyproject.toml
├── .env.example
├── config/
│   ├── settings.yaml        # all risk-gate thresholds, sizing, calendar dates
│   └── universe.yaml        # seed candidate tickers for Sleeve A
├── src/barbell/
│   ├── cli.py               # barbell run-cycle | status | flatten | verify | journal export
│   ├── broker/
│   │   ├── alpaca_client.py # ONLY module importing alpaca-py
│   │   ├── mcp_client.py    # MCP wiring docs (alpaca-mcp-server config)
│   │   └── clock.py         # market-hours + deadline awareness
│   ├── screen/
│   │   ├── universe.py      # Stage 1: deterministic liquidity/IV screen
│   │   ├── metrics.py       # IV rank, IV30/HV20, local Black-Scholes fallback
│   │   └── headline_triage.py # Stage 1.5: Featherless model — non-critical
│   ├── agent/
│   │   ├── catalyst_gate.py    # Stage 2: Gemini — veto power
│   │   ├── structure_agent.py  # Stage 3: Gemini — proposes spread structure
│   │   ├── schemas.py          # Pydantic models every LLM output must satisfy
│   │   └── prompts/
│   ├── risk/
│   │   ├── gates.py         # 13 gates, pure functions, highest test coverage
│   │   ├── engine.py        # orchestrates gates — can only tighten, never loosen
│   │   └── kill_switch.py   # NAV drawdown latch, persisted in journal DB
│   ├── execution/
│   │   ├── orders.py        # submit() + submit_basket(); limit-only, capital-reserved sequential entry
│   │   └── reconcile.py     # broker-is-truth diff, every cycle and after every basket leg fill
│   ├── endgame/
│   │   └── schedule.py      # dated state machine: BUILD→CARRY_ACTIVE→UNWIND→CONVEXITY_ENTRY→HOLD_THROUGH_NFP→MONETIZE→FLAT→POST_DEADLINE
│   ├── journal/
│   │   ├── store.py         # append-only SQLite: every decision + reason (9 tables, incl. capital_reservations / basket_leg_fills)
│   │   └── export.py        # renders docs/writeup_generated.md from the DB
│   └── scheduler/
│       └── loop.py          # APScheduler cycle runner — unattended process
├── scripts/
│   ├── verify_day1.py       # 7-item checklist against paper API
│   ├── seed_paper_account.py
│   └── export_slide_stats.py # NAV, P&L, gate stats, dispersion reading → JSON for slides
├── dashboard/
│   └── app.py               # Streamlit read-only live view
├── deploy/
│   └── barbell.service       # systemd unit for the unattended scheduler loop
├── tests/
│   ├── test_risk_gates.py   # boundary + property tests on the safety layer
│   ├── test_schedule.py     # calendar-boundary tests, both sides of every phase transition
│   ├── test_execution.py    # order submission + basket-atomicity tests
│   ├── test_scheduler.py    # full-cycle orchestration tests
│   └── test_screen.py
└── docs/
    ├── architecture.md      # full engineering blueprint + per-member handoff notes
    ├── runbook.md            # day-to-day operating guide
    └── writeup_generated.md # auto-generated by journal/export.py
```

---

## Quickstart

### 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dashboard]"
```

### 2. Configure

```bash
cp .env.example .env
# Fill in:
#   ALPACA_API_KEY / ALPACA_SECRET_KEY   — paper account credentials
#   GEMINI_API_KEY                        — for catalyst gate + structure agent
#   FEATHERLESS_API_KEY                  — for headline triage (non-critical)
```

### 3. Verify the paper account

```bash
python scripts/seed_paper_account.py   # confirms fresh $100k account, prints account ID
python scripts/verify_day1.py          # run BEFORE trusting any strategy code
```

`verify_day1.py` runs 7 checks: options approval level, multi-leg order submission, Greeks/IV availability on the basic plan, quote staleness, SPX/XSP data availability, fill simulation, and rate-limit headroom. Its output may require adjusting `config/settings.yaml` thresholds.

### 4. Run

```bash
pytest                        # risk gates + schedule boundary tests first
barbell run-cycle             # one manual pass through the full pipeline
barbell status                # current NAV, open positions, phase
streamlit run dashboard/app.py  # optional live view
```

---

## CLI Reference

| Command | Description |
|---|---|
| `barbell run-cycle` | One full screen→agent→risk→execution cycle |
| `barbell status` | Current NAV, positions, phase, kill-switch state |
| `barbell flatten` | Force-flatten all open positions (emergency) |
| `barbell verify` | Re-run the day-1 checklist |
| `barbell journal export` | Render `docs/writeup_generated.md` from the journal DB |

---

## Running a Live Demo

`pytest` proves the logic is correct against mocks — it never touches Alpaca or
Gemini. This section is the actual "watch it trade" walkthrough: every command
below hits the real paper account and the real Gemini API.

### 1. Confirm both APIs are actually reachable

```bash
source .venv/bin/activate
python scripts/verify_day1.py
```

Look for: `options_approved_level=3`, a real order reaching `ACCEPTED` status
(Check 2), and Greeks/IV present in the chain snapshot (Check 3). Check 4
(quote staleness) will legitimately fail outside 9:30–16:00 ET — that's stale
off-hours data, not a bug; rerun it during market hours to see it pass.

### 2. Start the visual dashboard

```bash
streamlit run dashboard/app.py
```

Open `http://localhost:8501`. This is the main thing to have on screen for
the demo: current NAV vs. the $100k start, phase, kill-switch state,
dispersion score, a live countdown to the submission deadline, and — once a
cycle has actually run — NAV history, P&L by sleeve, the risk-decision feed,
open positions, and recent orders. It auto-refreshes every 30s and makes no
broker calls itself (reads `data/barbell.db` directly), so it's safe to leave
open the whole time.

### 3. Run the pipeline for real

```bash
barbell status       # confirm current phase, NAV, kill-switch before starting
barbell run-cycle     # one real screen -> catalyst gate -> structure agent -> risk engine -> execution pass
```

Watch the terminal output: `Phase: <phase>` → `Screen: N survivors / 25
candidates` → per-survivor `catalyst_risk=True/False` → `RiskDecision: PASS
/ RESIZE / VETO` → `submit_basket` filling a real (paper) order. Each stage
writes to the journal as it happens, so the dashboard from step 2 updates
within its next 30s refresh.

Two things this pipeline depends on that no amount of retrying fixes:

- **Real market hours.** Outside 9:30–16:00 ET, option open-interest/quote
  data reads stale or zero, so the deterministic screen legitimately rejects
  every candidate (`oi_below_floor`) before any LLM call happens — that's
  real off-hours data, not a bug.
- **The endgame calendar in `config/settings.yaml`.** `barbell status`'s
  `Phase` line tells you what's currently allowed — `CARRY_ACTIVE` allows
  Sleeve A opens/closes, `CONVEXITY_ENTRY` allows a Sleeve B open, everything
  else is closes-only or read-only. If the phase doesn't match what you
  expect, check the calendar dates before assuming the code is broken.

For an unattended run instead of one-shot cycles, start the actual scheduler
(the same thing `barbell run-cycle` calls, but looped every
`cycle_interval_minutes` for as long as the market's open):

```bash
python3 -c "
from barbell.logging_config import setup_logging; setup_logging()
from barbell.scheduler.loop import run_loop
from barbell.broker.alpaca_client import AlpacaClient
from barbell.journal.store import JournalStore
from barbell.config import get_settings
s = get_settings()
run_loop(AlpacaClient.from_settings(), JournalStore(str(s.barbell_db_path)))
"
```

### 4. Cross-check against Alpaca's own paper dashboard

Log into [app.alpaca.markets/paper/dashboard/overview](https://app.alpaca.markets/paper/dashboard/overview)
with the same paper account and confirm the order/position from step 3 shows
up there too — this is independent proof the trade is real, not just a row
in our own journal DB.

### 5. Generate the write-up from whatever actually happened

```bash
barbell journal export
```

Renders `docs/writeup_generated.md` (AI logic + all 13 risk gates with real
trigger counts + Alpaca infrastructure) and `data/trade_log.csv` straight
from the journal — no hand-editing. Re-run any time to refresh it with the
latest trading activity.

### 6. Emergency stop, if needed

```bash
barbell flatten       # force-closes everything, bypasses phase gating entirely
barbell status        # confirm 0 open positions afterward
```

---

## Tech Stackbr

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Trading + market data | [`alpaca-py`](https://github.com/alpacahq/alpaca-py) |
| Interactive / demo path | [`alpaca-mcp-server`](https://github.com/alpacahq/alpaca-mcp-server) (MCP, stdio) |
| LLM — decision-making | Google Gemini (`google-genai` SDK) — catalyst gate + structure proposal |
| LLM — bulk triage | Featherless AI (`openai` SDK → `api.featherless.ai/v1`) — non-critical headline pre-digest only |
| Schema validation | Pydantic v2 |
| Scheduling | APScheduler |
| Storage | SQLite via SQLModel (append-only journal) |
| Config | YAML + `.env` (`python-dotenv`) |
| Testing | pytest + freezegun |
| Dashboard | Streamlit |
| CI | GitHub Actions (`pytest` on every push) |

---

## Testing

```bash
pytest                        # full suite — 180+ tests
pytest tests/test_risk_gates.py   # risk engine only
pytest tests/test_schedule.py     # endgame schedule boundaries
pytest tests/test_execution.py    # order submission + basket-atomicity
pytest --cov=barbell --cov-report=term-missing
```

The risk gate suite includes a 200+ case property test asserting `engine.evaluate()` never returns more contracts than proposed, plus a PASS/boundary/VETO case for all 13 gates. The schedule suite checks both sides of every calendar boundary (last carry entry, convexity entry, NFP release, flatten deadline, submission deadline). The execution suite covers `submit_basket()`'s capital-reservation lifecycle, including a mid-basket gate failure halting before the next leg is built. CI never hits live Alpaca or Gemini — all network calls are mocked; full-suite coverage currently runs ~79% overall, ~95% on `risk/`.

> **Note:** the live-account verification steps (`scripts/verify_day1.py` against a funded paper account, a real `submit_basket()` smoke test, a real screen against `config/universe.yaml`, and a real end-to-end LLM call) still need to be run with real credentials before this is submission-ready — see `docs/architecture.md`'s per-member handoff notes for what's been checked offline versus what still needs a live run.

---

## Endgame Schedule

Dates below are from `config/settings.yaml`'s `calendar` section for the current judged window; `endgame/schedule.py` computes the phase from these on every cycle.

| Phase | Trigger (ET) | Allowed actions |
|---|---|---|
| `BUILD` | before Aug 31 | Reads only — broker verification, data checks |
| `CARRY_ACTIVE` | Aug 31 – Sep 2 EOD | Sleeve A entries + closes, 30-min cycles |
| `UNWIND` | Sep 3, before 14:30 | Sleeve A closes only (routed through `submit_basket`) |
| `CONVEXITY_ENTRY` | Sep 3, at/after 14:30 | Sleeve B open (size scales with NAV vs. starting NAV) |
| `HOLD_THROUGH_NFP` | overnight through Sep 4 08:30 | No new entries, no closes |
| `MONETIZE` | Sep 4, 08:30 – 10:45 | Sleeve B closes only |
| `FLAT` | Sep 4, 10:45 – 11:00 | Reads only — everything must already be flat |
| `POST_DEADLINE` | Sep 4, at/after 11:00 | Reads only |

The `gate_pre_nfp_flatten` and `gate_expiry_past_deadline` risk gates enforce these phase boundaries at the execution layer too — a trade that would survive every other gate is still vetoed if the schedule says positions must be flat.

> **Known gap:** `current_phase()` currently folds `CONVEXITY_ENTRY` into `HOLD_THROUGH_NFP` at the trigger boundary, so `allowed_actions()` never actually returns `"sleeve_b_open"` in a live cycle — Sleeve B can't open automatically yet. Needs a fix in `endgame/schedule.py` before Sleeve B can trade unattended.

---

## Configuration

All gate thresholds, sizing parameters, and calendar dates live in `config/`:

| File | Contents |
|---|---|
| `config/settings.yaml` | Risk gate limits, cycle interval, execution settings, calendar dates |
| `config/universe.yaml` | Seed candidate tickers for Sleeve A screening |

Numbers are never hardcoded in Python that have a home in config.

---

## License

MIT
