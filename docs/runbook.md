# Dispersion Barbell — Operations Runbook

For the human monitoring the system during the judged trading window (Sep 1–4, 2026).
This is **not** a developer reference — see `docs/architecture.md` for the engineering
detail. This is what you look at before the open bell each morning and when something
looks wrong.

---

## Daily Market-Open Checklist

Run these three checks every morning before 09:30 ET, in order:

```bash
# 1. Confirm the process is alive and the current phase is correct
barbell status

# 2. Check that today's CI is green (no broken tests on main)
# → check https://github.com/Naveen77qwerty/Parallax/actions

# 3. Confirm no unreleased capital reservation from overnight (if any)
# barbell status will show "basket in-flight" if reserved_capital > 0
```

**What `barbell status` should show each morning:**

| Day | Expected Phase | Expected Actions |
|---|---|---|
| Sep 1 (Mon) | `CARRY_ACTIVE` | Sleeve A opens + closes |
| Sep 2 (Tue) | `CARRY_ACTIVE` | Sleeve A opens + closes |
| Sep 3 (Wed) before 14:30 ET | `UNWIND` | Sleeve A closes only |
| Sep 3 after 14:30 ET | `HOLD_THROUGH_NFP` or `CONVEXITY_ENTRY` | Sleeve B open |
| Sep 4 (Thu) before 08:30 ET | `HOLD_THROUGH_NFP` | Read-only |
| Sep 4 after 08:30 ET | `MONETIZE` | Sleeve B close |
| Sep 4 after 10:45 ET | `FLAT` | Read-only |
| Sep 4 after 11:00 ET | `POST_DEADLINE` | Read-only |

If the phase is wrong, **do not force-run anything** — check `endgame/schedule.py`
and `config/settings.yaml` calendar dates for a misconfiguration.

---

## What Normal Cycle Output Looks Like

A normal 30-minute cycle in `CARRY_ACTIVE` phase produces journal rows like:

```
[cycle-a3f2] Phase: CARRY_ACTIVE
[cycle-a3f2] Screen: 8 survivors / 25 candidates
[cycle-a3f2] NVDA: catalyst_risk=False
[cycle-a3f2] propose_structure(NVDA) → put_credit_spread
[cycle-a3f2] RiskDecision for NVDA: PASS (contracts=1, gates_run=13)
[cycle-a3f2] submit_basket: 1 leg, capital reserved → filled → released
[cycle-a3f2] Cycle complete — survivors=8 proposals=5 pass=3 resize=1 veto=1 orders=4
```

**Normal signals:**
- A mix of PASS, RESIZE, and VETO across proposals — this is correct operation
- `reconcile_diverged=False` every cycle
- Kill switch: `🟢 clear`
- Reserved capital: `$0.00` (no basket in-flight between cycles)

---

## Concerning vs. Expected Patterns

### 🔴 Immediate concern — stop and investigate

| Pattern | Likely cause | Action |
|---|---|---|
| `Kill switch: 🔴 LATCHED` | NAV dropped ≥ 8% from $100k | DO NOT reset; verify actual NAV; if confirmed, leave latched until you understand what happened |
| `reconcile_diverged=True` | Broker positions don't match journal | New entries are blocked automatically; check broker UI vs `barbell status` |
| `reserved_capital > 0` persisting across cycles | Mid-basket process crash | Check `capital_reservations` table for `status='reserved'` rows; resolve before restarting |
| Phase `FLAT` before 10:45 ET | Clock/config bug | Check `settings.yaml` calendar dates against current ET time |

### 🟡 Monitor — investigate before next cycle

| Pattern | Likely cause | Action |
|---|---|---|
| 5+ consecutive VETOs on the same gate | Gate threshold miscalibrated or market changed | Check which gate; `gate_dispersion_score` VETOs mean the carry trade is genuinely unfavourable today — that's correct behaviour, not a bug |
| All 25 candidates failing screen | IV rank collapsed across the universe | Confirm with `barbell journal export` — may be a market event |
| `propose_structure` failing for every survivor | Claude API issue | Check ANTHROPIC_API_KEY; `screen/headline_triage.py` will still run (Featherless) |
| No orders submitted in CARRY_ACTIVE for 2+ cycles | Risk engine VETOing everything | Run `barbell journal export` and read the VETO reasons table |

### 🟢 Fine — not a problem

- Zero survivors from screening: the market doesn't meet Sleeve A's criteria today. The system correctly does nothing.
- `dispersion_score` below 1.15: Gate 06 correctly blocks Sleeve A. Single-name IV is not elevated enough vs. index to make carry worthwhile.
- RESIZE decisions: the risk engine is doing its job — it's reducing contract count to stay within the per-position cap.

---

## NAV Trajectory — What to Expect

| Scenario | Expected NAV range by Sep 4 10:45 ET |
|---|---|
| Sleeve A working normally (carry realised) | +0.5% to +1.6% ($100,500–$101,600) |
| Sleeve A flat / no entries | ~$100,000 (no P&L either direction) |
| Sleeve B (convexity) pays off on NFP vol | Additional +0.5% to +5%+ depending on move |
| Both sleeves underperform | Drawdown limited to -8% by kill switch ($92,000 floor) |

**Key number**: the kill switch trips at **-8% of starting NAV** = below **$92,000**.
If you see NAV approaching $93,000–94,000, watch closely for the latch.

---

## How to Manually Force-Flatten

Use this **only** as an emergency override (e.g., kill switch didn't latch automatically,
or you need to exit all positions before submission deadline for any reason):

```bash
barbell flatten
```

This logs at CRITICAL level and closes all open positions via market close orders.
It **bypasses phase gating** — it will close positions regardless of what `current_phase()`
returns. After flattening, run:

```bash
barbell status
barbell journal export
```

to confirm positions are closed and the write-up reflects the final state.

---

## How to Export the Write-Up

On the morning of Sep 4 (or any time you want a current snapshot):

```bash
barbell journal export
```

This writes `docs/writeup_generated.md` (the hackathon write-up) and
`data/trade_log.csv` (the full order log). Both are generated from the
live journal DB — no manual editing required.

For slide stats:

```bash
python scripts/export_slide_stats.py
```

---

## Running the Dashboard

```bash
streamlit run dashboard/app.py
```

The dashboard auto-refreshes every 30 seconds (configurable via sidebar slider).
It reads from the journal DB directly and makes **no broker API calls**.

For a public URL (if needed for submission demo), run through ngrok:

```bash
ngrok http 8501
```

Or deploy to Streamlit Community Cloud:
- Push the repo to GitHub (ensure `.env` is **not** committed)
- Add secrets via Streamlit Cloud dashboard (ALPACA_API_KEY, etc.)
- Point the app to `dashboard/app.py`

---

## Running Under nohup / tmux (Windows alternative)

**On Windows (this machine)** — PowerShell background job:

```powershell
# Start the scheduler in the background (runs run_loop)
Start-Process -FilePath "python" `
    -ArgumentList "-c", "from barbell.scheduler.loop import run_loop; from barbell.broker.alpaca_client import AlpacaClient; from barbell.journal.store import JournalStore; from barbell.config import get_settings; s=get_settings(); run_loop(AlpacaClient.from_settings(), JournalStore(str(s.barbell_db_path)))" `
    -RedirectStandardOutput "logs\barbell.log" `
    -RedirectStandardError "logs\barbell_err.log" `
    -NoNewWindow
```

Or use the one-shot CLI on a Windows Task Scheduler cron:

```
# Task Scheduler: run every 30 minutes during market hours
barbell run-cycle >> logs\barbell.log 2>&1
```

**On Linux VPS** — see `deploy/barbell.service` for the systemd unit.

---

## Key Config File Locations

| File | What's in it |
|---|---|
| `config/settings.yaml` | All risk gate thresholds, calendar dates, sleeve parameters |
| `config/universe.yaml` | Candidate tickers for Sleeve A screening |
| `.env` | API keys (never committed to git) |
| `data/barbell.db` | The journal SQLite DB (append-only) |
| `data/account_id.txt` | The Alpaca paper account ID for submission |
