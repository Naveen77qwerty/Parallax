"""
Central configuration loader.

Loads config/settings.yaml and config/universe.yaml via PyYAML, loads .env
via python-dotenv, and exposes a single typed Settings object through
get_settings().  The result is cached so YAML is parsed exactly once per
process.

Usage anywhere in the codebase:
    from barbell.config import get_settings
    s = get_settings()
    print(s.risk_gates.max_quote_age_seconds)

Required environment variables (raises EnvironmentError with the exact var
name if missing, never a generic KeyError):
    ALPACA_API_KEY
    ALPACA_SECRET_KEY
    ALPACA_PAPER_TRADE
    GEMINI_API_KEY
    BARBELL_DB_PATH
    BARBELL_LOG_LEVEL

Optional (have defaults in .env.example):
    ALPACA_BASE_URL
    GEMINI_MODEL
    FEATHERLESS_API_KEY
    FEATHERLESS_BASE_URL
    FEATHERLESS_MODEL
    BARBELL_ENV
"""

from __future__ import annotations

import functools
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, field_validator, model_validator

# Project root is two levels up from this file: src/barbell/config.py
_ROOT = Path(__file__).resolve().parent.parent.parent


def _require_env(name: str) -> str:
    """Return os.environ[name] or raise EnvironmentError naming the variable."""
    val = os.environ.get(name)
    if not val:
        raise OSError(
            f"Required environment variable '{name}' is missing or empty. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


# ---------------------------------------------------------------------------
# Nested config models — mirror config/settings.yaml exactly
# ---------------------------------------------------------------------------


class AccountConfig(BaseModel):
    starting_nav: float


class CalendarConfig(BaseModel):
    first_full_session: date
    last_carry_entry_day: date
    carry_unwind_day: date
    convexity_entry_day: date
    convexity_entry_after_et: str          # "HH:MM" string — parsed by clock.py
    submission_deadline_et: datetime       # full ISO datetime string in YAML
    flatten_by_et: datetime
    nfp_release_et: datetime

    @field_validator("submission_deadline_et", "flatten_by_et", "nfp_release_et", mode="before")
    @classmethod
    def _parse_naive_dt(cls, v: Any) -> Any:
        """Accept both bare datetime objects from PyYAML and ISO strings."""
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            return datetime.fromisoformat(v)
        return v


class SleeveAScreenConfig(BaseModel):
    min_open_interest: int
    max_spread_pct_of_mid: float
    min_iv_rank: float
    min_iv30_hv20_ratio: float
    earnings_blackout: bool
    min_dispersion_score: float


class SleeveAConfig(BaseModel):
    enabled: bool
    structure: list[str]
    spread_width_usd: float
    short_delta_range: list[float]
    dte_range: list[int]
    target_names: int
    min_names: int
    profit_take_pct_of_credit: float
    screen: SleeveAScreenConfig


class SleeveBConfig(BaseModel):
    enabled: bool
    underlying: str
    structure: str
    long_delta_target: float
    short_delta_target: float
    expiry_offset_days_past_deadline: int
    base_risk_pct_nav: float
    escalated_risk_pct_nav: float
    escalate_if_nav_above_start: bool


class RiskGateConfig(BaseModel):
    max_loss_per_position_pct_nav: float
    max_loss_portfolio_pct_nav: float
    basket_reserve_before_first_leg: bool
    max_quote_age_seconds: int
    max_sector_concentration: int
    max_positions_per_underlying: int
    drawdown_kill_switch_pct_nav: float
    max_slippage_pct_of_mid: float
    order_retry_limit: int
    order_retry_widen_pct: float


class ExecutionConfig(BaseModel):
    order_type: str
    time_in_force: str
    poll_interval_seconds: int
    fill_timeout_seconds: int

    @model_validator(mode="after")
    def _enforce_limit_only(self) -> ExecutionConfig:
        if self.order_type != "limit":
            raise ValueError(
                f"execution.order_type must be 'limit', got '{self.order_type}'. "
                "Market orders are prohibited — see CLAUDE.md."
            )
        return self


class SchedulerConfig(BaseModel):
    cycle_interval_minutes: int
    market_hours_only: bool


class UniverseConfig(BaseModel):
    candidates: dict[str, list[str]]
    index_hedge_underlying: str
    exclude: list[str]


# ---------------------------------------------------------------------------
# Top-level Settings object — includes both YAML sections and env vars
# ---------------------------------------------------------------------------


class Settings(BaseModel):
    # YAML-sourced
    account: AccountConfig
    calendar: CalendarConfig
    sleeve_a_carry: SleeveAConfig
    sleeve_b_convexity: SleeveBConfig
    risk_gates: RiskGateConfig
    execution: ExecutionConfig
    scheduler: SchedulerConfig
    universe: UniverseConfig

    # Env-sourced — always present after get_settings() returns
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper_trade: bool
    alpaca_base_url: str

    gemini_api_key: str
    gemini_model: str

    featherless_api_key: str
    featherless_base_url: str
    featherless_model: str

    barbell_env: str
    barbell_db_path: Path
    barbell_log_level: str

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Parse config files and env vars exactly once per process.

    Call get_settings.cache_clear() in tests that need to swap env vars.
    """
    # Load .env (silently skipped if missing — CI sets vars directly)
    env_path = _ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)

    # Load YAML files
    settings_path = _ROOT / "config" / "settings.yaml"
    universe_path = _ROOT / "config" / "universe.yaml"

    with open(settings_path) as f:
        raw_settings: dict[str, Any] = yaml.safe_load(f)

    with open(universe_path) as f:
        raw_universe: dict[str, Any] = yaml.safe_load(f)

    # Required env vars — fail loudly with the exact name
    required = [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_PAPER_TRADE",
        "GEMINI_API_KEY",
        "BARBELL_DB_PATH",
        "BARBELL_LOG_LEVEL",
    ]
    for var in required:
        _require_env(var)

    paper_str = os.environ["ALPACA_PAPER_TRADE"].lower()
    alpaca_paper = paper_str in ("true", "1", "yes")

    universe_data = {
        "candidates": raw_universe.get("candidates", {}),
        "index_hedge_underlying": raw_universe.get("index_hedge_underlying", "SPY"),
        "exclude": raw_universe.get("exclude") or [],
    }

    return Settings(
        account=raw_settings["account"],
        calendar=raw_settings["calendar"],
        sleeve_a_carry=raw_settings["sleeve_a_carry"],
        sleeve_b_convexity=raw_settings["sleeve_b_convexity"],
        risk_gates=raw_settings["risk_gates"],
        execution=raw_settings["execution"],
        scheduler=raw_settings["scheduler"],
        universe=universe_data,
        # Env vars
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        alpaca_paper_trade=alpaca_paper,
        alpaca_base_url=os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        gemini_api_key=os.environ["GEMINI_API_KEY"],
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        featherless_api_key=os.environ.get("FEATHERLESS_API_KEY", ""),
        featherless_base_url=os.environ.get(
            "FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1"
        ),
        featherless_model=os.environ.get("FEATHERLESS_MODEL", "Qwen/Qwen2.5-7B-Instruct"),
        barbell_env=os.environ.get("BARBELL_ENV", "paper"),
        barbell_db_path=Path(os.environ["BARBELL_DB_PATH"]),
        barbell_log_level=os.environ["BARBELL_LOG_LEVEL"],
    )
