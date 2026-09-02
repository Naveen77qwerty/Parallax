# Dispersion Barbell

Autonomous options agent for the Alpaca AI Trading Agents Hackathon. Short rich single-name volatility through defined-risk credit spreads; hold a small, cheap long-index-convexity hedge into the Sep 4 jobs print; flat and fully realized before the submission deadline.

Full design rationale: [strategy artifact](https://claude.ai/code/artifact/7cb083e5-37f7-4d0d-bbb3-22bff57e6c26) · engineering detail: [`docs/architecture.md`](docs/architecture.md).

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dashboard]"
cp .env.example .env   # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY / ANTHROPIC_API_KEY

python scripts/seed_paper_account.py   # confirms fresh $100k paper account, prints account ID
python scripts/verify_day1.py          # run this BEFORE trusting any strategy code

pytest                                  # risk gates + schedule boundaries first
barbell run-cycle                       # one manual pass
streamlit run dashboard/app.py          # optional live view
```

## Status

Scaffold only — see `docs/architecture.md` → "Build order" for what's implemented vs. stubbed.
