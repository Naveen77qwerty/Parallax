"""
Unit tests for configuration loading and validation (src/barbell/config.py).
"""

from __future__ import annotations

import pytest

from barbell.config import get_settings


@pytest.fixture(autouse=True)
def clean_settings_cache():
    """Ensure cached settings do not leak across tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_env(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test_key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test_secret")
    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test_anthropic_key")
    monkeypatch.setenv("CLAUDE_MODEL", "claude-opus-4-5")
    monkeypatch.setenv("BARBELL_ENV", "paper")
    monkeypatch.setenv("BARBELL_DB_PATH", "data/test_barbell.db")
    monkeypatch.setenv("BARBELL_LOG_LEVEL", "INFO")


def test_load_settings_success(mock_env):
    """Test loading real settings.yaml and universe.yaml with valid env."""
    settings = get_settings()

    # Account config
    assert settings.account.starting_nav == 100000.0

    # Risk gate config
    assert settings.risk_gates.max_loss_per_position_pct_nav == 0.008
    assert settings.risk_gates.max_loss_portfolio_pct_nav == 0.12
    assert settings.risk_gates.max_quote_age_seconds == 120
    assert settings.risk_gates.drawdown_kill_switch_pct_nav == -0.08
    assert settings.risk_gates.basket_reserve_before_first_leg is True

    # Execution config
    assert settings.execution.order_type == "limit"

    # Universe config
    assert "technology" in settings.universe.candidates
    assert "NVDA" in settings.universe.candidates["technology"]
    assert settings.universe.index_hedge_underlying == "SPY"

    # Env vars
    assert settings.alpaca_api_key == "test_key"
    assert settings.alpaca_paper_trade is True
    assert settings.barbell_log_level == "INFO"


@pytest.mark.parametrize(
    "missing_var",
    [
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_PAPER_TRADE",
        "ANTHROPIC_API_KEY",
        "BARBELL_DB_PATH",
        "BARBELL_LOG_LEVEL",
    ],
)
def test_missing_required_env_var_raises(monkeypatch, missing_var):
    """Test that missing any required env var raises an EnvironmentError naming that var."""
    # Set all valid vars first
    base_env = {
        "ALPACA_API_KEY": "k",
        "ALPACA_SECRET_KEY": "s",
        "ALPACA_PAPER_TRADE": "true",
        "ANTHROPIC_API_KEY": "a",
        "BARBELL_DB_PATH": "data/barbell.db",
        "BARBELL_LOG_LEVEL": "INFO",
    }
    for k, v in base_env.items():
        monkeypatch.setenv(k, v)

    # Delete the target missing var
    monkeypatch.delenv(missing_var, raising=False)

    with pytest.raises(EnvironmentError, match=f"'{missing_var}'"):
        get_settings()
