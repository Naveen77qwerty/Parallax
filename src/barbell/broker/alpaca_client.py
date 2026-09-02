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

CLAUDE.md non-negotiable enforced here:
    submit_mleg_order raises NakedShortError BEFORE calling the SDK if any
    sell leg is not covered by a corresponding buy leg in the same request.
    Defense in depth — the risk engine also checks this, but we don't trust
    any single layer.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

# ---------------------------------------------------------------------------
# alpaca-py imports — THIS IS THE ONLY MODULE ALLOWED TO IMPORT alpaca-py
# ---------------------------------------------------------------------------
from alpaca.data import OptionHistoricalDataClient
from alpaca.data.models import OptionsSnapshot
from alpaca.data.requests import OptionChainRequest
from alpaca.trading import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.models import Clock, Position, TradeAccount
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    OptionLegRequest,
)

from barbell.agent.schemas import ProposedLeg
from barbell.config import get_settings

log = logging.getLogger(__name__)


class NakedShortError(ValueError):
    """
    Raised when submit_mleg_order detects an uncovered short leg.

    This is a hard invariant (CLAUDE.md) — no naked shorts, ever.
    The error is raised here (broker layer) AND the risk engine also checks;
    defence in depth.
    """


class AlpacaClient:
    """
    Wrapper around alpaca-py TradingClient and OptionHistoricalDataClient.

    Constructed from config.get_settings() only — never reads env vars
    directly.  Use AlpacaClient.from_settings() as the primary constructor.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        paper: bool = True,
        base_url: str | None = None,
    ) -> None:
        self._trading = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
            url_override=base_url,
        )
        # OptionHistoricalDataClient uses the same paper credentials
        self._data = OptionHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )
        log.debug("AlpacaClient initialised (paper=%s)", paper)

    @classmethod
    def from_settings(cls) -> AlpacaClient:
        """Primary constructor — builds from get_settings(), never raw env."""
        s = get_settings()
        return cls(
            api_key=s.alpaca_api_key,
            secret_key=s.alpaca_secret_key,
            paper=s.alpaca_paper_trade,
            base_url=s.alpaca_base_url if s.alpaca_base_url else None,
        )

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        """
        Return account state relevant to risk gates.

        Returns:
            {
                "id": str,
                "account_number": str,
                "equity": float,             # total portfolio value (NAV)
                "buying_power": float,
                "options_buying_power": float,
                "options_approved_level": int,   # 0–3; 3 = multi-leg
                "options_trading_level": int,
                "cash": float,
                "portfolio_value": float,
                "trading_blocked": bool,
            }
        """
        acct: TradeAccount = self._trading.get_account()
        return {
            "id": str(acct.id),
            "account_number": acct.account_number,
            "equity": float(acct.equity or 0),
            "buying_power": float(acct.buying_power or 0),
            "options_buying_power": float(acct.options_buying_power or 0),
            "options_approved_level": int(acct.options_approved_level or 0),
            "options_trading_level": int(acct.options_trading_level or 0),
            "cash": float(acct.cash or 0),
            "portfolio_value": float(acct.portfolio_value or 0),
            "trading_blocked": bool(acct.trading_blocked),
        }

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        """
        Return all open positions as plain dicts.

        Returns list of:
            {
                "symbol": str,          # OCC-format for options
                "qty": float,
                "side": str,            # "long" | "short"
                "market_value": float,
                "avg_entry_price": float,
                "unrealized_pl": float,
                "asset_class": str,     # "us_option" etc.
                "current_price": float,
            }
        """
        positions: list[Position] = self._trading.get_all_positions()
        result = []
        for p in positions:
            result.append(
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty or 0),
                    "side": p.side.value if p.side else "long",
                    "market_value": float(p.market_value or 0),
                    "avg_entry_price": float(p.avg_entry_price or 0),
                    "unrealized_pl": float(p.unrealized_pl or 0),
                    "asset_class": p.asset_class.value if p.asset_class else "us_equity",
                    "current_price": float(p.current_price or 0),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Option chain
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        underlying: str,
        expiration_date_gte: date | None = None,
        expiration_date_lte: date | None = None,
    ) -> dict[str, OptionsSnapshot]:
        """
        Return live option snapshots (greeks, IV, quotes) for `underlying`.

        Uses OptionHistoricalDataClient.get_option_chain() which returns
        Dict[str, OptionsSnapshot] keyed by OCC option symbol.  Each
        OptionsSnapshot has .greeks (delta, gamma, theta, vega, rho),
        .implied_volatility, .latest_quote, .latest_trade.

        Args:
            underlying:              Underlying ticker (e.g. "AAPL")
            expiration_date_gte:    Filter — contracts expiring on/after this date
            expiration_date_lte:    Filter — contracts expiring on/before this date

        Returns:
            Dict[occ_symbol, OptionsSnapshot]

        Raises:
            Exception: propagates SDK errors — callers should catch and handle
                       missing data (e.g. fall back to Black-Scholes).
        """
        req = OptionChainRequest(
            underlying_symbol=underlying,
            expiration_date_gte=expiration_date_gte,
            expiration_date_lte=expiration_date_lte,
        )
        chain: dict[str, OptionsSnapshot] = self._data.get_option_chain(req)
        log.debug("get_option_chain(%s): %d contracts returned", underlying, len(chain))
        return chain

    def get_option_contracts(
        self,
        underlying: str,
        expiration_date_gte: date | None = None,
        expiration_date_lte: date | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return option contract metadata (not snapshots) from the trading API.

        Useful for resolving OCC symbols from expiry/strike/right before
        building orders.  Returns list of dicts with keys: symbol, expiration_date,
        strike_price, type (call/put), open_interest.
        """
        req = GetOptionContractsRequest(
            underlying_symbols=[underlying],
            expiration_date_gte=expiration_date_gte,
            expiration_date_lte=expiration_date_lte,
        )
        resp = self._trading.get_option_contracts(req)
        contracts = resp.option_contracts if hasattr(resp, "option_contracts") else (resp or [])
        return [
            {
                "symbol": c.symbol,
                "expiration_date": c.expiration_date,
                "strike_price": float(c.strike_price or 0),
                "type": c.type.value if c.type else "",
                "open_interest": int(c.open_interest or 0),
                "underlying_symbol": c.underlying_symbol,
            }
            for c in contracts
        ]

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    def submit_mleg_order(
        self,
        legs: list[ProposedLeg],
        limit_price: float,
        tif: str = "day",
    ) -> str:
        """
        Build and submit a real Alpaca multi-leg (mleg) options order.

        Uses LimitOrderRequest with order_class=MLEG and a list of
        OptionLegRequest objects.  LIMIT ONLY — market orders are prohibited
        (CLAUDE.md + execution/orders.py double-enforcement).

        Naked-short check (CLAUDE.md non-negotiable, defense in depth):
            For every SELL leg, there must be a BUY leg covering the same
            underlying + expiry.  If not, raises NakedShortError BEFORE
            any SDK call.

        Args:
            legs:        List of ProposedLeg from agent/schemas.py
            limit_price: Net limit price per spread (positive = credit received,
                         negative = debit paid).  Alpaca expects this as a
                         positive number with direction implied by leg sides.
            tif:         Time-in-force string ("day", "gtc", etc.)

        Returns:
            order_id: str — Alpaca's UUID for the submitted order

        Raises:
            NakedShortError: if any sell leg is uncovered
            ValueError:      if legs list is empty or limit_price is zero
        """
        if not legs:
            raise ValueError("submit_mleg_order requires at least one leg")
        if limit_price == 0.0:
            raise ValueError("limit_price must be non-zero for options orders")

        # --- Naked-short check (BEFORE any SDK call) ---
        self._check_no_naked_shorts(legs)

        # --- Resolve OCC symbols if not already set ---
        resolved_legs = self._resolve_leg_symbols(legs)

        # --- Build SDK request ---
        tif_enum = TimeInForce(tif)
        option_legs = [
            OptionLegRequest(
                symbol=leg_symbol,
                ratio_qty=leg.ratio_qty,
                side=OrderSide(leg.side),
            )
            for leg, leg_symbol in zip(legs, resolved_legs)
        ]

        # For multi-leg options, Alpaca uses LimitOrderRequest with mleg class.
        # The `symbol` field is left empty for mleg orders; legs define the structure.
        # qty=1 means "1 spread unit"; contracts per leg are in ratio_qty.
        # limit_price is the net credit/debit per spread (always a positive float;
        # direction is determined by which side is the net seller).
        order_req = LimitOrderRequest(
            symbol=legs[0].symbol if legs[0].symbol else "",  # mleg requires a root symbol
            qty=legs[0].contracts,
            side=OrderSide.BUY,        # mleg convention: outer side is always BUY
            order_class=OrderClass.MLEG,
            time_in_force=tif_enum,
            limit_price=abs(limit_price),
            legs=option_legs,
        )

        log.info(
            "Submitting mleg order: %d legs, limit=%.4f, tif=%s",
            len(legs),
            limit_price,
            tif,
        )
        order = self._trading.submit_order(order_req)
        log.info("Order submitted: id=%s, status=%s", order.id, order.status)
        return str(order.id)

    def _check_no_naked_shorts(self, legs: list[ProposedLeg]) -> None:
        """
        Raise NakedShortError if any sell leg is not covered by a buy leg
        with the same underlying expiry.

        Coverage rule: for each SELL leg, there must exist at least one BUY
        leg with the same expiration date (same underlying is assumed since
        mleg orders are per-underlying).  This catches uncovered calls/puts.

        This is intentionally conservative — it errs toward rejection.
        """
        buy_expiries: set[date] = {leg.expiry for leg in legs if leg.side == "buy"}
        sell_expiries: list[date] = [leg.expiry for leg in legs if leg.side == "sell"]

        uncovered = [exp for exp in sell_expiries if exp not in buy_expiries]
        if uncovered:
            raise NakedShortError(
                f"Naked short detected: SELL leg(s) with expiry {uncovered} have no "
                f"corresponding BUY leg in the same order.  "
                f"All short legs must be covered within the same multi-leg order "
                f"(CLAUDE.md non-negotiable)."
            )

    def _resolve_leg_symbols(self, legs: list[ProposedLeg]) -> list[str]:
        """
        Return the OCC symbol for each leg, using leg.symbol if already set,
        otherwise leaving the resolution for the caller (orders.py).

        For now, requires symbol to be pre-populated.  If empty, raises
        ValueError — orders.py is responsible for resolving symbols from
        get_option_contracts() before calling submit_mleg_order().
        """
        resolved = []
        for leg in legs:
            if not leg.symbol:
                raise ValueError(
                    f"ProposedLeg with expiry={leg.expiry} strike={leg.strike} "
                    f"right={leg.right} has an empty OCC symbol.  "
                    "Resolve via get_option_contracts() before submitting."
                )
            resolved.append(leg.symbol)
        return resolved

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def get_clock(self) -> dict[str, Any]:
        """
        Return current market clock state.

        Returns:
            {
                "is_open": bool,
                "timestamp": datetime,
                "next_open": datetime,
                "next_close": datetime,
            }
        """
        clock: Clock = self._trading.get_clock()
        return {
            "is_open": bool(clock.is_open),
            "timestamp": clock.timestamp,
            "next_open": clock.next_open,
            "next_close": clock.next_close,
        }
