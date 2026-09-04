"""
APScheduler-driven long-running process — what `barbell run-cycle` becomes
when left running unattended (systemd / tmux / nohup; see docs/architecture.md
"Runtime" section for why this isn't serverless).

Every `cycle_interval_minutes` (config), while broker/clock.py says the
market is open: pulls endgame phase -> runs screen (if phase allows entries)
-> catalyst gate -> structure agent -> risk engine -> execution -> reconcile
-> journal. A single stuck cycle must not take down the process — each stage
is wrapped so one failure logs and skips to reconcile, never crashes the loop.

Public API:
    run_one_cycle(cycle_id, client, store) -> dict   # one full pass, returns summary
    run_loop(client, store)                           # blocks; runs until killed
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from barbell.broker.alpaca_client import AlpacaClient
    from barbell.journal.store import JournalStore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Single-cycle runner (the atomic unit — also what `barbell run-cycle` calls)
# ---------------------------------------------------------------------------


def run_one_cycle(
    cycle_id: str,
    client: AlpacaClient,
    store: JournalStore,
) -> dict:
    """Run one full screening → gating → risk → execution → reconcile pass.

    Stage isolation: every stage is wrapped in its own try/except.  One bad
    candidate or flaky API call must not abort the cycle or skip reconcile.
    reconcile() is called in a finally block — it runs regardless.

    Returns:
        Summary dict with keys: cycle_id, phase, survivors, proposals,
        decisions_pass, decisions_veto, decisions_resize, orders_submitted,
        reconcile_diverged.
    """
    from barbell.agent.catalyst_gate import check_catalyst
    from barbell.agent.schemas import MarketState, PortfolioState
    from barbell.agent.structure_agent import propose_structure, propose_structure_sleeve_b
    from barbell.broker.clock import is_market_open
    from barbell.config import get_settings
    from barbell.endgame.schedule import Phase, allowed_actions, current_phase
    from barbell.execution.orders import submit_basket
    from barbell.execution.reconcile import reconcile
    from barbell.risk.engine import evaluate
    from barbell.screen.headline_triage import digest_headlines
    from barbell.screen.universe import load_candidates, screen

    s = get_settings()
    summary: dict = {
        "cycle_id": cycle_id,
        "phase": None,
        "survivors": 0,
        "proposals": 0,
        "decisions_pass": 0,
        "decisions_veto": 0,
        "decisions_resize": 0,
        "orders_submitted": 0,
        "reconcile_diverged": False,
    }

    # --- 1. Determine phase -------------------------------------------------
    try:
        phase = current_phase()
        summary["phase"] = phase.name
        log.info("[%s] Phase: %s", cycle_id, phase.name)
    except Exception as exc:
        log.error("[%s] current_phase() failed: %s — aborting cycle", cycle_id, exc)
        phase = Phase.FLAT  # safe default blocks new entries

    actions = allowed_actions(phase)

    # --- 2. Portfolio/market state snapshots (shared across all proposals) --
    portfolio_state = PortfolioState(
        current_nav=s.account.starting_nav,
        starting_nav=s.account.starting_nav,
    )
    market_state = MarketState()

    try:
        account = client.get_account()
        portfolio_state = PortfolioState(
            current_nav=float(account.get("equity", s.account.starting_nav)),
            starting_nav=s.account.starting_nav,
            open_positions=client.get_positions(),
        )
    except Exception as exc:
        log.warning("[%s] Could not fetch account/positions: %s — using defaults", cycle_id, exc)

    # Wire reconciliation_diverged into market_state below (after reconcile).

    # --- 3. Screening (only if entries are allowed) -------------------------
    all_results: list = []
    survivors: list = []
    dispersion_market_state = market_state  # may be updated with dispersion_score

    if "sleeve_a_open" in actions:
        try:
            candidates = load_candidates()
            all_results, dispersion_market_state = screen(candidates, client, store, cycle_id)
            survivors = [r for r in all_results if r.passed]
            summary["survivors"] = len(survivors)
            log.info("[%s] Screen: %d survivors / %d candidates", cycle_id, len(survivors), len(all_results))
            market_state = dispersion_market_state
        except Exception as exc:
            log.error("[%s] screen() failed: %s — no survivors this cycle", cycle_id, exc)
    else:
        log.info("[%s] Phase %s: skipping screen (entries not allowed)", cycle_id, phase.name)

    # --- 4. Per-survivor: headline triage → catalyst gate → structure -------
    proposals = []

    for result in survivors:
        symbol = result.symbol

        # 4a. Headline triage (Featherless — non-blocking, never raises past here)
        headlines: list = []  # TODO: wire real news API
        try:
            digest = digest_headlines(symbol, headlines)
        except Exception as exc:
            log.warning("[%s] digest_headlines(%s) raised unexpectedly: %s", cycle_id, symbol, exc)
            from barbell.agent.schemas import HeadlineDigest
            digest = HeadlineDigest(symbol=symbol, news_volume="normal", summary="")

        # 4b. Catalyst gate (Claude — veto authority, fail-closed on error)
        try:
            verdict = check_catalyst(symbol, headlines, digest, result)
            if verdict.catalyst_risk:
                log.info("[%s] %s: catalyst_risk=True — skipping", cycle_id, symbol)
                continue
        except Exception as exc:
            log.error("[%s] check_catalyst(%s) failed: %s — skipping (fail-closed)", cycle_id, symbol, exc)
            continue

        # 4c. Structure agent (Claude — propose spread, data-in data-out)
        try:
            chain = client.get_option_chain(symbol)
            structure = propose_structure(
                symbol,
                chain,
                result,
                dispersion_score=market_state.dispersion_score,
            )
            proposals.append(structure)
        except Exception as exc:
            log.warning("[%s] propose_structure(%s) failed: %s — skipping", cycle_id, symbol, exc)

    # --- 4b. Sleeve B: convexity hedge proposal (no per-name screen — single
    #     fixed index underlying from sleeve_b_convexity.underlying). No
    #     catalyst gate here: Sleeve B exists specifically to hold through a
    #     scheduled macro catalyst (e.g. NFP), not avoid one. ------------------
    if "sleeve_b_open" in actions and s.sleeve_b_convexity.enabled:
        try:
            b_underlying = s.universe.index_hedge_underlying
            b_chain = client.get_option_chain(b_underlying)
            b_structure = propose_structure_sleeve_b(
                b_chain,
                nav_current=portfolio_state.current_nav,
                nav_starting=portfolio_state.starting_nav,
            )
            proposals.append(b_structure)
            log.info("[%s] Sleeve B: proposed %s on %s", cycle_id, b_structure.structure_type, b_underlying)
        except Exception as exc:
            log.warning("[%s] propose_structure_sleeve_b failed: %s — skipping", cycle_id, exc)

    summary["proposals"] = len(proposals)

    # --- 5. Pre-filter proposals through risk engine ------------------------
    approved_proposals = []

    def _portfolio_state_fn() -> PortfolioState:
        """Fresh portfolio state for basket's mid-leg re-evaluation."""
        try:
            acct = client.get_account()
            return PortfolioState(
                current_nav=float(acct.get("equity", s.account.starting_nav)),
                starting_nav=s.account.starting_nav,
                open_positions=client.get_positions(),
                reserved_capital=portfolio_state.reserved_capital,
            )
        except Exception:
            return portfolio_state

    def _market_state_fn() -> MarketState:
        return market_state

    for proposal in proposals:
        try:
            decision = evaluate(
                proposal,
                portfolio_state,
                market_state,
                s.risk_gates,
                cycle_id=cycle_id,
                store=store,
            )
            if decision.outcome == "VETO":
                summary["decisions_veto"] += 1
            elif decision.outcome == "RESIZE":
                summary["decisions_resize"] += 1
                approved_proposals.append(proposal)
            else:
                summary["decisions_pass"] += 1
                approved_proposals.append(proposal)
        except Exception as exc:
            log.error("[%s] evaluate(%s) failed: %s — skipping proposal", cycle_id, proposal.underlying, exc)

    # --- 6. Sequential basket entry -----------------------------------------
    if approved_proposals and ("sleeve_a_open" in actions or "sleeve_b_open" in actions):
        try:
            results = submit_basket(
                approved_proposals,
                client=client,
                store=store,
                exec_config=s.execution,
                risk_config=s.risk_gates,
                engine_config=s.risk_gates,
                portfolio_state_fn=_portfolio_state_fn,
                market_state_fn=_market_state_fn,
                cycle_id=cycle_id,
            )
            summary["orders_submitted"] = len([r for r in results if r.get("status") not in ("abandoned", "skipped")])
        except Exception as exc:
            log.error("[%s] submit_basket() failed: %s", cycle_id, exc)

    # --- 7. Reconcile — always runs, even if earlier stages threw -----------
    try:
        recon = reconcile(client=client, store=store, cycle_id=cycle_id)
        summary["reconcile_diverged"] = recon.diverged
        if recon.diverged:
            log.critical("[%s] RECONCILIATION DIVERGED: %s", cycle_id, recon.description)
        # Feed diverged flag back into market_state for next cycle
        market_state.reconciliation_diverged = recon.diverged
    except Exception as exc:
        log.error("[%s] reconcile() failed: %s", cycle_id, exc)

    log.info(
        "[%s] Cycle complete — phase=%s survivors=%d proposals=%d "
        "pass=%d resize=%d veto=%d orders=%d diverged=%s",
        cycle_id,
        summary["phase"],
        summary["survivors"],
        summary["proposals"],
        summary["decisions_pass"],
        summary["decisions_resize"],
        summary["decisions_veto"],
        summary["orders_submitted"],
        summary["reconcile_diverged"],
    )
    return summary


# ---------------------------------------------------------------------------
# Long-running loop (APScheduler)
# ---------------------------------------------------------------------------


def run_loop(client: AlpacaClient, store: JournalStore) -> None:  # type: ignore[name-defined]
    """Start the APScheduler loop and block until the process is killed.

    Reads cycle_interval_minutes and market_hours_only from config.
    Each cycle invocation calls run_one_cycle() in a thread-pool executor
    (APScheduler's default) so a slow cycle doesn't delay the next tick.

    The scheduler never starts a new cycle while the previous one is still
    running (max_instances=1 on the job).
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
    except ImportError:
        log.error(
            "APScheduler not installed — run `pip install apscheduler` or "
            "`pip install -e '.[dev]'` from the repo root."
        )
        raise

    from barbell.broker.clock import is_market_open
    from barbell.config import get_settings

    s = get_settings()
    interval_min = s.scheduler.cycle_interval_minutes
    market_hours_only = s.scheduler.market_hours_only

    scheduler = BlockingScheduler(timezone="America/New_York")

    def _tick() -> None:
        if market_hours_only:
            try:
                if not is_market_open(client):
                    log.debug("Market closed — skipping cycle")
                    return
            except Exception as exc:
                log.warning("is_market_open() failed: %s — skipping cycle to be safe", exc)
                return

        cycle_id = str(uuid.uuid4())[:8]
        try:
            run_one_cycle(cycle_id, client, store)
        except Exception as exc:
            log.exception("Unhandled exception in run_one_cycle(%s): %s", cycle_id, exc)

    scheduler.add_job(
        _tick,
        trigger="interval",
        minutes=interval_min,
        max_instances=1,
        id="barbell_cycle",
    )

    log.info(
        "Scheduler starting — cycle every %d min, market_hours_only=%s",
        interval_min,
        market_hours_only,
    )
    scheduler.start()
