# Demo Video Script — Parallax (Dispersion Barbell)

Read from this while recording your own screen. Keep two windows visible: this
script (or a second monitor/phone) and the terminal + browser you're demoing.
Everything referenced here is already running live — you're narrating over a
real system, not a mockup.

Before you hit record:
- Terminal open at the repo root, venv activated (`source .venv/bin/activate`)
- Browser tab open to `http://localhost:8501` (or the ngrok URL if you want to
  show the public link working)
- `docs/writeup.md` open in an editor tab, ready to flash on screen briefly

---

## 0:00 – 0:30 — Hook + what this is

**Say:**
"This is Parallax, an autonomous options trading agent built for the Alpaca
AI Trading Agents Hackathon. It runs a strategy called a dispersion barbell —
it sells rich single-name volatility while buying cheap index volatility as a
hedge, fully autonomously, through Alpaca's paper trading API. Everything
you're about to see is live against a real paper account, not a simulation."

**Show:** README.md top of file, or just talk to camera/screen with the repo
open.

---

## 0:30 – 1:30 — The strategy in one breath

**Say:**
"The thesis: single-stock volatility has been running hot while index
volatility has been unusually cheap. Sleeve A sells that expensive single-name
volatility through defined-risk put credit spreads — capped loss, no naked
shorts, ever. Sleeve B buys cheap index convexity — an SPY put debit spread —
as a hedge that pays off on a big market move, like the NFP jobs report.

The gap between the two isn't just a hunch — it's a real computed number,
called the dispersion score: vega-weighted single-name IV divided by index
IV. That number gates how much Sleeve A is allowed to size into, every single
cycle."

**Show:** README's Strategy Overview table, or `docs/writeup.md`'s "The
strategy" section.

---

## 1:30 – 3:00 — Architecture + safety, narrated over the pipeline diagram

**Say:**
"The pipeline is: a deterministic screen narrows about 25 candidate tickers
down to survivors on liquidity and IV metrics. Survivors go through a Gemini-
powered catalyst gate — that's an LLM with real veto power, checking whether
a name's volatility is rich for a good reason or because of some unpriced
binary risk like an earnings surprise. Anything that passes goes to a second
Gemini call, the structure agent, which proposes the actual spread — strikes,
expiry, limit price.

Here's the important part: neither of those AI calls can execute anything.
Everything they propose goes through a deterministic risk engine — thirteen
gates — that can only shrink a trade or reject it outright. It can never make
a position bigger than what was proposed. That's not just a design promise —
it's backed by a property test that runs the risk engine against two hundred
plus randomized inputs and asserts that invariant holds every time.

Both AI calls also fail closed — if the API errors out or returns something
that doesn't validate, the system treats that as a veto, not a silent
approval."

**Show:** the Architecture diagram in README.md (scroll to it), or the
pipeline ASCII diagram in `docs/writeup.md`.

---

## 3:00 – 4:30 — Live dashboard + a real cycle

**Say:**
"Let's look at the live dashboard."

**Show:** switch to the browser tab (`localhost:8501` or the ngrok URL).
Point out, in order:
- Current NAV vs. starting $100k
- Current phase (should read CARRY_ACTIVE or whatever the endgame state
  machine currently says)
- Kill switch status
- Dispersion score trend chart
- NAV history

**Say:**
"Now let's run a real cycle live, right now, against the actual market."

**Do:** switch to terminal, run:
```bash
python -m barbell.cli run-cycle
```

**Say while it runs:**
"Watch the terminal — it'll print the phase, how many of the ~25 candidates
survived the deterministic screen, then for each survivor the catalyst gate's
real verdict, then the risk engine's decision, then whether anything actually
got submitted as a real order."

**After it finishes, say (adapt to whatever actually happened):**
"[Read the actual output: e.g. 'Zero survivors this cycle — that's the
liquidity and IV filters correctly rejecting names that don't meet the
threshold right now, which is the system working as designed, not a bug.'
OR if something passed: 'X survived, here's what the catalyst gate decided
and why.']"

**Show:** flip back to the dashboard — point out it updated (NAV history,
dispersion score, or risk decision feed, whichever changed).

---

## 4:30 – 5:30 — What broke and got fixed live, and why that matters

**Say:**
"Something worth calling out: this was tested against the real Alpaca API,
not just mocks. That surfaced two real bugs — Alpaca rejects a symbol field
being set on multi-leg orders, and its debit/credit sign convention for limit
price is the opposite of what you'd assume — both would have made every real
order fail or price backwards. Those are fixed now, verified against the live
account. There's also a real example of the fail-closed design working: one
of the Gemini catalyst-gate calls hit an actual transient 503 error from
Google's side during this build, and the system correctly treated that as a
veto instead of crashing or silently approving the trade."

**Show:** optionally scroll to the "Alpaca infrastructure" section of
`docs/writeup.md` while saying this.

---

## 5:30 – 6:30 — Risk gates + safety invariants, quickly

**Say:**
"Every proposed trade — before anything is submitted — passes through
thirteen gates in a fixed order: per-position and portfolio loss caps, no
naked shorts, quote staleness, liquidity floor, the dispersion score floor,
earnings blackout, a pre-NFP flatten gate, an expiry-past-deadline gate,
concentration limits, a drawdown kill switch, broker reconciliation, and
capital reservation for multi-leg baskets. Two of those are worth a specific
mention: broker reconciliation compares the journal's record of positions
against what Alpaca actually reports, every cycle, and halts new entries on
any divergence — that's a defense against the agent trading on a false
picture of its own portfolio. And the basket capital reservation fixes a real
gap: Alpaca only guarantees atomic fills within one underlying's order, never
across underlyings, so a multi-name basket reserves its full max-loss capital
in the journal before the first leg ever goes out."

**Show:** `risk/gates.py` file briefly, or just keep talking over the
dashboard.

---

## 6:30 – 7:00 — Close

**Say:**
"Everything here — every decision, every gate, every rejection reason — is
written to an append-only journal, so the full trading history is always
answerable from the database. [Mention final NAV / trade count once you have
it.] The paper account ID for judging is PA3IC9W6B7VZ, the code is public on
GitHub, and the one-page write-up covering the AI logic, risk gates, and
infrastructure is in the repo. Thanks for watching."

**Show:** `docs/writeup.md` one more time, or the GitHub repo page.

---

## Notes for recording

- If a cycle shows 0 survivors when you run it live, don't panic-cut — narrate
  it honestly ("this is the screen correctly rejecting everything that
  doesn't meet the bar right now"). That's a real, defensible result, not a
  failure.
- If you want a cleaner visual moment, run `barbell status` right before and
  after the live cycle so the phase/NAV numbers are visible on screen.
- Keep the ngrok tab open in a second browser window if you want to prove the
  dashboard is genuinely publicly reachable, not just localhost.
