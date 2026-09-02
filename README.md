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
endgame/schedule.py       → current phase: BUILD | CARRY_ACTIVE | UNWIND | FLAT
  │
  ▼
screen/universe.py + metrics.py        (deterministic)
  │  ~25 seed names → 8–14 survivors (IV rank, IV30/HV20, liquidity)
  ▼
screen/headline_triage.py              (Featherless AI — optional, non-blocking)
  │  cheap news-volume flag; informational context only, never gates a trade
  ▼
agent/catalyst_gate.py                 (Claude LLM #1 — veto power)
  │  per survivor: catalyst_risk True/False + reasoning
  ▼
agent/structure_agent.py               (Claude LLM #2 — sizes real risk)
  │  per clean survivor: ProposedStructure (JSON, schema-validated)
  ▼
risk/engine.py  →  risk/gates.py (×12) + risk/kill_switch.py
  │  PASS | RESIZE(smaller) | REJECT — never larger than proposed
  ▼
execution/orders.py  →  broker/alpaca_client.py
  │  limit orders only, multi-leg, bounded retry
  ▼
execution/reconcile.py
  │  broker state is truth; diverge → halt new entries
  ▼
journal/store.py           (append-only — every decision logged)
```

### Key safety invariants

- **Risk engine can only tighten.** No gate may increase a proposed size or clear a veto. Enforced by a property test in `tests/test_risk_gates.py`.
- **Limit orders only.** `order_type="limit"` is enforced in `execution/orders.py` in code, not just config.
- **No naked shorts.** Every sell leg must be paired with a buy leg of the same expiry in the same multi-leg order, verified by `broker/alpaca_client.py` before any network call.
- **`broker/alpaca_client.py` is the only module that imports `alpaca-py`.** All other modules call through its interface for full mockability.
- **Kill switch.** An -8% NAV drawdown latches `risk/kill_switch.py` (persisted in the journal DB); it survives process restarts.

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
│   │   ├── catalyst_gate.py    # Stage 2: Claude — veto power
│   │   ├── structure_agent.py  # Stage 3: Claude — proposes spread structure
│   │   ├── schemas.py          # Pydantic models every LLM output must satisfy
│   │   └── prompts/
│   ├── risk/
│   │   ├── gates.py         # 12 gates, pure functions, highest test coverage
│   │   ├── engine.py        # orchestrates gates — can only tighten, never loosen
│   │   └── kill_switch.py   # -8% NAV latch, persisted in journal DB
│   ├── execution/
│   │   ├── orders.py        # builds + submits; limit-only enforced in code
│   │   └── reconcile.py     # broker-is-truth diff, every cycle
│   ├── endgame/
│   │   └── schedule.py      # dated state machine: BUILD→CARRY_ACTIVE→UNWIND→FLAT
│   ├── journal/
│   │   ├── store.py         # append-only SQLite: every decision + reason
│   │   └── export.py        # renders docs/writeup_generated.md from the DB
│   └── scheduler/
│       └── loop.py          # APScheduler cycle runner — unattended process
├── scripts/
│   ├── verify_day1.py       # 7-item checklist against paper API
│   └── seed_paper_account.py
├── dashboard/
│   └── app.py               # Streamlit read-only live view
├── tests/
│   ├── test_risk_gates.py   # boundary + property tests on the safety layer
│   ├── test_schedule.py     # freezegun tests at every calendar boundary
│   └── test_screen.py
└── docs/
    ├── architecture.md      # full engineering blueprint
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
#   ANTHROPIC_API_KEY                    — for catalyst gate + structure agent
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

## Tech Stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Trading + market data | [`alpaca-py`](https://github.com/alpacahq/alpaca-py) |
| Interactive / demo path | [`alpaca-mcp-server`](https://github.com/alpacahq/alpaca-mcp-server) (MCP, stdio) |
| LLM — decision-making | Anthropic Claude (`anthropic` SDK) — catalyst gate + structure proposal |
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
pytest                        # full suite
pytest tests/test_risk_gates.py   # risk engine only
pytest tests/test_schedule.py     # endgame schedule boundaries (freezegun)
pytest --cov=barbell --cov-report=term-missing
```

The risk gate suite includes a property test asserting `engine.evaluate()` never returns more contracts than proposed. The schedule suite freezes time at every critical calendar boundary (last carry entry, convexity entry gate, NFP, flatten deadline, submission deadline). CI never hits live Alpaca or Anthropic — all network calls are mocked.

---

## Endgame Schedule

| Phase | Trigger | Allowed actions |
|---|---|---|
| `BUILD` | Start | Broker verification, data checks |
| `CARRY_ACTIVE` | Market open Tue | Sleeve A entries, 30-min cycles |
| `UNWIND` | Thu session | Close all Sleeve A; enter Sleeve B |
| `HOLD_THROUGH_NFP` | Thu close | No new entries |
| `FLAT` | Fri 10:45 ET | All positions must be closed; submission |

The `gate_pre_nfp_flatten` and `gate_expiry_past_deadline` risk gates enforce phase boundaries at the execution layer — a trade that would survive all other gates is still vetoed if the schedule says positions must be flat.

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
