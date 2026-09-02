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
"""
