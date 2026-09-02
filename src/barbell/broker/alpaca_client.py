"""
Thin wrapper over alpaca-py (TradingClient + OptionHistoricalDataClient).

Owns ALL network calls to Alpaca. Nothing else in this codebase imports
alpaca-py directly — that keeps risk/ and agent/ testable with fakes, and
means there is exactly one place that needs updating if the SDK changes.

Responsibilities:
    - get_account() -> NAV, buying power, options level
    - get_positions() -> list of open positions (source of truth, see execution/reconcile.py)
    - get_option_chain(underlying) -> contracts + snapshots (greeks, IV, quotes)
    - submit_mleg_order(legs, limit_price, tif) -> order id
    - get_clock() -> is market open, next open/close (drives scheduler + endgame)

Day-1 verification target: confirm get_option_chain returns Greeks/IV on the
Basic data plan (scripts/verify_day1.py, item 3). If not, greeks fall back to
screen/metrics.py's local Black-Scholes solver.
"""


class AlpacaClient:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        raise NotImplementedError

    def get_account(self):
        raise NotImplementedError

    def get_positions(self):
        raise NotImplementedError

    def get_option_chain(self, underlying: str):
        raise NotImplementedError

    def submit_mleg_order(self, legs: list, limit_price: float, tif: str = "day"):
        raise NotImplementedError

    def get_clock(self):
        raise NotImplementedError
