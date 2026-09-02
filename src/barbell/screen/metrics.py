"""
IV rank, IV30/HV20, and a local Black-Scholes IV/greeks solver — the fallback
path if scripts/verify_day1.py finds the Basic data plan doesn't return
Greeks from get_option_snapshot.

    iv_rank(current_iv, iv_52w_series) -> float
    iv30_hv20_ratio(iv30, close_prices_20d) -> float
    bs_implied_vol(mid_price, spot, strike, dte, rate) -> float   # Newton-Raphson
    bs_greeks(iv, spot, strike, dte, rate, is_call) -> Greeks

Keep this dependency-free (no scipy) — a plain Black-Scholes + Newton solve
in ~40 lines is one less thing that can be unavailable at 8am on Day 1.

Member 1 handoff note: verify_day1.py item 3 checks whether delta/gamma/IV are
populated from the Alpaca Basic plan.  If they are, screen/universe.py uses the
native values directly; if not, it calls bs_greeks here.  The dispersion_score()
function is always used (it needs contract vega regardless of data source).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Greeks dataclass — returned by bs_greeks()
# ---------------------------------------------------------------------------


@dataclass
class Greeks:
    delta: float
    gamma: float
    theta: float   # per day (annualised theta / 365)
    vega: float    # per 1% IV move (annualised vega / 100)
    rho: float


# ---------------------------------------------------------------------------
# IV rank
# ---------------------------------------------------------------------------


def iv_rank(current_iv: float, iv_52w_series: list[float]) -> float:
    """
    Return the IV rank of *current_iv* relative to the 52-week series.

    IV rank = (current_iv - 52w_low) / (52w_high - 52w_low)

    Returns 0.0 if the range is zero (all values are identical).

    Args:
        current_iv:      Today's implied volatility (0–1 scale, e.g. 0.35 = 35%).
        iv_52w_series:   Historical daily IVs for the past 252 trading days
                         (does NOT need to include today — just the lookback).

    Returns:
        float in [0, 1] where 1 = at 52-week high.
    """
    if not iv_52w_series:
        log.warning("iv_rank: empty series, returning 0.0")
        return 0.0

    low = min(iv_52w_series)
    high = max(iv_52w_series)

    if high == low:
        return 0.0

    rank = (current_iv - low) / (high - low)
    # Clamp to [0, 1] — current_iv may land outside the historical range
    return max(0.0, min(1.0, rank))


# ---------------------------------------------------------------------------
# IV30 / HV20 ratio
# ---------------------------------------------------------------------------


def iv30_hv20_ratio(iv30: float, close_prices_20d: list[float]) -> float:
    """
    Return the ratio of 30-day implied vol to 20-day realised historical vol.

    Signals whether options are pricing more risk than the underlying has
    been delivering — a core Sleeve A entry criterion.  Ratios > 1.15 indicate
    IV is rich relative to recent realised vol.

    Args:
        iv30:              30-day implied volatility (0–1 scale).
        close_prices_20d:  List of at least 21 consecutive close prices
                           (need 21 prices to compute 20 log-returns).

    Returns:
        iv30 / hv20.  Returns 0.0 on degenerate input (too few prices, zero HV).
    """
    if len(close_prices_20d) < 2:
        log.warning("iv30_hv20_ratio: need at least 2 prices, returning 0.0")
        return 0.0

    # 20-day realised vol: annualised std-dev of log returns
    prices = close_prices_20d[-21:]  # at most last 21 for 20 returns
    returns = [
        math.log(prices[i] / prices[i - 1])
        for i in range(1, len(prices))
        if prices[i - 1] > 0 and prices[i] > 0
    ]

    if len(returns) < 2:
        log.warning("iv30_hv20_ratio: not enough valid returns, returning 0.0")
        return 0.0

    n = len(returns)
    mean_r = sum(returns) / n
    variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    hv20 = math.sqrt(variance * 252)  # annualise: trading days per year

    if hv20 == 0.0:
        log.warning("iv30_hv20_ratio: HV20 is zero, returning 0.0")
        return 0.0

    return iv30 / hv20


# ---------------------------------------------------------------------------
# Black-Scholes — dependency-free (math.erf for N(d))
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF using math.erf — no scipy required."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(spot: float, strike: float, dte: float, rate: float, iv: float, is_call: bool) -> float:
    """Return Black-Scholes price for a European option."""
    if dte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0

    T = dte / 365.0
    sqrtT = math.sqrt(T)

    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT

    if is_call:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * T) * _norm_cdf(d2)
    else:
        return strike * math.exp(-rate * T) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def bs_implied_vol(
    mid_price: float,
    spot: float,
    strike: float,
    dte: float,
    rate: float = 0.05,
    is_call: bool = True,
    max_iter: int = 100,
    tol: float = 1e-6,
) -> float:
    """
    Compute implied volatility via Newton-Raphson iteration.

    This is the fallback path used when Alpaca's Basic data plan does not
    return native greeks/IV (confirmed by scripts/verify_day1.py item 3).

    Args:
        mid_price:  Option mid-price in USD.
        spot:       Underlying spot price.
        strike:     Option strike price.
        dte:        Days to expiry (calendar days).
        rate:       Risk-free rate (annualised, default 5%).
        is_call:    True for call, False for put.
        max_iter:   Newton-Raphson iteration limit.
        tol:        Convergence tolerance.

    Returns:
        Implied volatility (0–1 scale, e.g. 0.30 = 30%).
        Returns 0.0 on failure to converge or degenerate input.
    """
    if mid_price <= 0 or spot <= 0 or strike <= 0 or dte <= 0:
        return 0.0

    T = dte / 365.0
    # Initial guess: use simple approximation
    iv = math.sqrt(2 * math.pi / T) * mid_price / spot

    # Clamp initial guess to a sane range
    iv = max(0.01, min(5.0, iv))

    for _ in range(max_iter):
        price = _bs_price(spot, strike, dte, rate, iv, is_call)
        diff = price - mid_price

        if abs(diff) < tol:
            return iv

        sqrtT = math.sqrt(T)
        d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / (iv * sqrtT)
        vega_bs = spot * _norm_pdf(d1) * sqrtT  # vega w.r.t. sigma

        if abs(vega_bs) < 1e-10:
            break

        iv = iv - diff / vega_bs
        iv = max(0.001, min(10.0, iv))  # clamp to avoid divergence

    log.debug("bs_implied_vol: Newton-Raphson did not converge (returning last iv=%.4f)", iv)
    return iv


def bs_greeks(
    iv: float,
    spot: float,
    strike: float,
    dte: float,
    rate: float = 0.05,
    is_call: bool = True,
) -> Greeks:
    """
    Return Black-Scholes Greeks for a European option.

    Used as the fallback when Alpaca's Basic plan does not return native Greeks
    (see verify_day1.py item 3 and Member 1 handoff notes).

    Args:
        iv:       Implied volatility (0–1 scale).
        spot:     Underlying spot price.
        strike:   Strike price.
        dte:      Days to expiry.
        rate:     Risk-free rate (annualised).
        is_call:  True for call, False for put.

    Returns:
        Greeks dataclass with delta, gamma, theta (per day), vega (per 1% IV),
        and rho.
    """
    if dte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return Greeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, rho=0.0)

    T = dte / 365.0
    sqrtT = math.sqrt(T)

    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT

    nd1 = _norm_pdf(d1)
    Nd1 = _norm_cdf(d1)
    Nd2 = _norm_cdf(d2)

    if is_call:
        delta = Nd1
        rho = strike * T * math.exp(-rate * T) * Nd2 / 100.0
    else:
        delta = Nd1 - 1.0
        rho = -strike * T * math.exp(-rate * T) * _norm_cdf(-d2) / 100.0

    gamma = nd1 / (spot * iv * sqrtT)

    # Theta (annualised, then divided by 365 → per calendar day)
    theta_annual = (
        -(spot * nd1 * iv) / (2.0 * sqrtT)
        - rate * strike * math.exp(-rate * T) * (Nd2 if is_call else _norm_cdf(-d2))
    )
    theta = theta_annual / 365.0

    # Vega: per 1% move in IV (annualised vega / 100)
    vega = spot * nd1 * sqrtT / 100.0

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)


# ---------------------------------------------------------------------------
# Dispersion score — NEW for Member 3
# ---------------------------------------------------------------------------


def dispersion_score(
    survivors: list[dict],
    index_iv: float,
) -> float:
    """
    Compute the vega-weighted single-name IV over index IV ratio.

    Formula (from docs/architecture.md):
        dispersion_score = Σ(w_i · IV_i) / IV_index

    where w_i = estimated single-contract vega × proposed contract count
    (i.e. the dollar-vega weight for each survivor), normalised so weights
    sum to 1.

    A score > 1.15 means single names are pricing more vol than the index —
    the core dispersion thesis for Sleeve A.  The risk gate in risk/gates.py
    reads this value from MarketState.dispersion_score and VETOs Sleeve A
    entries if the score falls below sleeve_a_carry.screen.min_dispersion_score.

    Args:
        survivors: list of dicts, each with:
            "iv":        float  — this name's implied vol (0–1 scale)
            "vega":      float  — single-contract vega from bs_greeks (per 1% IV)
            "contracts": int    — proposed contract count (from ScreenResult.metrics)
        index_iv:  float — current SPY (index proxy) implied vol (0–1 scale)

    Returns:
        float dispersion score, or 0.0 on degenerate input (empty list, zero
        index IV, zero total weight).

    Note:
        Callers must ensure each survivor dict has "iv", "vega", and "contracts"
        keys.  Missing keys produce a log warning and that name is skipped.
    """
    if not survivors:
        log.warning("dispersion_score: empty survivors list, returning 0.0")
        return 0.0

    if index_iv <= 0:
        log.warning("dispersion_score: index_iv=%.4f <= 0, returning 0.0", index_iv)
        return 0.0

    weighted_iv_sum = 0.0
    total_weight = 0.0

    for s in survivors:
        try:
            iv = float(s["iv"])
            vega = float(s["vega"])
            contracts = int(s["contracts"])
        except (KeyError, TypeError, ValueError) as e:
            log.warning("dispersion_score: skipping malformed survivor entry (%s): %s", s, e)
            continue

        weight = vega * contracts
        if weight <= 0:
            continue

        weighted_iv_sum += weight * iv
        total_weight += weight

    if total_weight <= 0:
        log.warning("dispersion_score: total weight is zero, returning 0.0")
        return 0.0

    score = (weighted_iv_sum / total_weight) / index_iv
    log.info(
        "dispersion_score: weighted_iv=%.4f, index_iv=%.4f, score=%.4f",
        weighted_iv_sum / total_weight,
        index_iv,
        score,
    )
    return score
