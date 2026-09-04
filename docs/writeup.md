# Parallax — Dispersion Barbell

**One-page write-up: AI logic, risk gates, and Alpaca infrastructure implementation**
Alpaca AI Trading Agents Hackathon · 28 Aug – 4 Sep 2026 · $100,000 paper account

---

## The strategy

Late-August 2026 had VIX at 2026 lows, implied correlation in the bottom 1% since 2005,
and single-stock volatility near the 98th percentile — index volatility cheap, single-name
volatility rich. That gap is the trade, and it fits inside Alpaca's options permission set.
Parallax runs it as a **dispersion barbell**: two sleeves with opposite signs on volatility.

- **Sleeve A — Carry.** Short idiosyncratic vol: $5-wide put credit spreads / iron condors
  on liquid single names with IV rank > 50 and IV30/HV20 > 1.15, 20–25Δ short strike, 3–7 DTE.
  Capped at 0.8% NAV risk per name.
- **Sleeve B — Convexity.** Long index vol: an SPY put debit spread (buy ~30Δ / sell ~10Δ),
  sized at 1.4% NAV base risk (2.8% escalated if NAV has grown past its starting value),
  held through the Sep 4 NFP print and monetized into the open.

The gap between the two sleeves isn't just a qualitative read — it's a computed number,
`dispersion_score` (vega-weighted single-name IV over index IV), logged every cycle and
used as a hard floor (1.15) on Sleeve A sizing. Classic institutional dispersion trading
delta-hedges a full constituent basket against the index; that's not implementable on a
15-minute-delayed feed in a multi-day window, so this design adapts the same signal into
outright defined-risk structures instead.

## AI logic — "AI proposes, code disposes"

The pipeline runs two LLM calls (Gemini), each with a narrow, specific job, wrapped
in a purely deterministic screen and risk engine on either side:

```
screen (deterministic: IV rank, IV30/HV20, liquidity, earnings blackout)
  -> catalyst gate (Gemini — veto power: "is this vol rich for a real reason,
     or is there an unpriced binary catalyst pending?")
  -> structure agent (Gemini — proposes the specific spread: strikes, expiry,
     limit price, schema-validated JSON)
  -> risk engine (deterministic, 13 gates — can only shrink or reject, never enlarge)
  -> execution (real multi-leg limit orders via alpaca-py)
```

Both LLM stages fail closed: a catalyst-gate error or a malformed response is treated as
`catalyst_risk=True` (veto), never as silent approval. A third, non-critical LLM call
(Featherless, open-source model) does headline pre-digest ahead of the catalyst gate —
informational context only, with no veto or sizing power and no path into the risk engine.

Sleeve B's proposal path is structurally identical but distinct end-to-end: its own prompt
targets a put *debit* spread with different delta targets and NAV-based sizing, and it
skips the catalyst gate by design — Sleeve B exists specifically to hold through a
scheduled macro catalyst (NFP), not to be vetoed for one.

## Risk gates — the part that has to be provably true, not just designed to be

Every proposed trade passes through 13 gates, in fixed order, before an order is ever
submitted:

`per-position loss cap` · `portfolio loss cap` · `defined-risk-only (no naked shorts)` ·
`quote staleness` · `liquidity floor` · `dispersion-score floor` · `earnings blackout` ·
`pre-NFP flatten` · `expiry-past-deadline` · `concentration limits` ·
`−8% drawdown kill switch` · `broker-reconciliation-is-truth` ·
`basket-capital-reservation`

The engine can only tighten a proposal (RESIZE down or VETO), never enlarge one — this is
enforced by construction (`min()` composition across gates) and backed by a property test
asserting it holds across 200+ randomized inputs, not just by convention. Every decision,
pass or reject, is written to an append-only journal with its reason, so "why didn't X
trade" is always answerable from the DB, never from memory.

Two defenses close gaps found in research rather than added for their own sake:

- **Broker-reconciliation-as-truth.** Every cycle (and after every basket leg fill),
  broker state is diffed against the journal; a divergence halts new entries and logs
  CRITICAL rather than trusting the agent's internal record of its own positions — a
  named defense against agents trading on falsified or drifted internal state.
- **Sequential basket entry with capital reservation.** Alpaca guarantees atomic fills
  only *within* one underlying's multi-leg order, never *across* underlyings. A basket
  spanning multiple names reserves its full max-loss capital in the journal before the
  first leg is submitted, enters one underlying at a time, and re-runs the risk engine
  against each actual fill — closing a real gap the naive "fire the whole basket in
  parallel" design left open.

## Alpaca infrastructure

`broker/alpaca_client.py` is the only module in the codebase that imports `alpaca-py`;
everything else calls through its interface, which is what kept the risk engine and
agents fully unit-testable against fakes (180+ tests, zero live calls in CI) while still
being verified against the real paper API before trusting any of it. That verification
surfaced two real, since-fixed bugs in `submit_mleg_order`: Alpaca rejects the `symbol`
field being set on an mleg order at all, and Alpaca's debit/credit sign convention for
`limit_price` is the *opposite* of this codebase's internal convention — both would have
made every real credit-spread order fail or price backwards, caught only because
`scripts/verify_day1.py`'s order-submission check was run for real against the live
account, not left as a mocked assumption. A second live-only finding: `alpaca-py`'s
option snapshot object carries no reliable open-interest field early in a session — the
liquidity screen now sources open interest from `get_option_contracts()` (the documented
contract-metadata endpoint) instead. Options orders are limit-only, enforced in code
(not just config); the CLI (`barbell run-cycle | status | flatten | verify | journal
export`) is the scheduled/unattended path, run every `cycle_interval_minutes` during
market hours via APScheduler.

A dated endgame state machine (`endgame/schedule.py`) gates which actions are even allowed
in a given phase — `CARRY_ACTIVE` allows Sleeve A opens/closes, `CONVEXITY_ENTRY` allows a
Sleeve B open, everything from NFP release through the submission deadline is closes-only
or read-only — independent of what any LLM call returns that day, so the account is
guaranteed flat and fully realized before judging regardless of model behavior.

## Results

As of this writing, the live window is still active. Starting NAV $100,000; current NAV
$100,000 (no fills yet). Three real cycles have run against the live paper account and
Gemini API (not mocks): 39 individual candidate screens across the seed universe, one
survivor (JPM, passing liquidity/IV-rank/IV30-HV20/earnings-blackout). That survivor's
catalyst-gate call hit a transient Gemini `503 UNAVAILABLE` and correctly failed closed
(`catalyst_risk=True`, trade skipped) — real evidence of the fail-closed design holding
under an actual API outage, not just a mocked test case. Kill switch clear, reconciliation
clean every cycle, zero divergence. Alpaca paper account ID: `PA3IC9W6B7VZ`. Final NAV,
fill count, and dispersion-score trend to be updated via `barbell journal export` once
the window closes.
