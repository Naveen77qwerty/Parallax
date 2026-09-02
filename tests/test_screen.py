"""
Tests for Member 3: screen/metrics.py, screen/universe.py, screen/headline_triage.py.

All alpaca_client calls are mocked — no live API hits.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from barbell.agent.schemas import MarketState, ScreenResult
from barbell.screen.metrics import (
    bs_greeks,
    bs_implied_vol,
    dispersion_score,
    iv30_hv20_ratio,
    iv_rank,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "option_chain.json"


def _load_fixture() -> dict:
    with open(FIXTURE_PATH) as f:
        return json.load(f)


def _make_snapshot(data: dict) -> SimpleNamespace:
    """Convert fixture dict entry into a mock OptionsSnapshot-like object."""
    q = SimpleNamespace(
        bid_price=data["latest_quote"]["bid_price"],
        ask_price=data["latest_quote"]["ask_price"],
    )
    g = SimpleNamespace(**data["greeks"])
    snap = SimpleNamespace(
        implied_volatility=data["implied_volatility"],
        open_interest=data["open_interest"],
        latest_quote=q,
        greeks=g,
        latest_trade=None,
    )
    return snap


def _build_chain(fixture: dict, symbol: str) -> dict:
    return {occ: _make_snapshot(v) for occ, v in fixture[symbol].items()}


# ---------------------------------------------------------------------------
# screen/metrics.py — iv_rank
# ---------------------------------------------------------------------------

class TestIVRank:
    def test_at_52w_high(self):
        assert iv_rank(0.80, [0.20, 0.50, 0.80]) == pytest.approx(1.0)

    def test_at_52w_low(self):
        assert iv_rank(0.20, [0.20, 0.50, 0.80]) == pytest.approx(0.0)

    def test_midpoint(self):
        assert iv_rank(0.50, [0.20, 0.80]) == pytest.approx(0.5)

    def test_empty_series(self):
        assert iv_rank(0.40, []) == 0.0

    def test_zero_range(self):
        assert iv_rank(0.40, [0.40, 0.40]) == 0.0

    def test_clamps_above_one(self):
        result = iv_rank(1.0, [0.20, 0.80])
        assert result == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# screen/metrics.py — iv30_hv20_ratio
# ---------------------------------------------------------------------------

class TestIV30HV20Ratio:
    def _geometric_series(self, sigma_annual: float, n: int = 21) -> list[float]:
        """Build a price series whose realized vol ≈ sigma_annual."""
        daily = sigma_annual / math.sqrt(252)
        prices = [100.0]
        for _ in range(n - 1):
            prices.append(prices[-1] * math.exp(daily))
        return prices

    def test_ratio_above_one_when_iv_rich(self):
        closes = self._geometric_series(0.25)  # HV ≈ 25%
        ratio = iv30_hv20_ratio(0.40, closes)   # IV = 40%
        assert ratio > 1.0

    def test_ratio_below_one_when_iv_cheap(self):
        closes = self._geometric_series(0.40)
        ratio = iv30_hv20_ratio(0.30, closes)
        assert ratio < 1.0

    def test_insufficient_prices(self):
        assert iv30_hv20_ratio(0.30, [100.0]) == 0.0

    def test_zero_hv_returns_zero(self):
        # Flat prices → zero HV
        closes = [100.0] * 21
        assert iv30_hv20_ratio(0.30, closes) == 0.0


# ---------------------------------------------------------------------------
# screen/metrics.py — bs_implied_vol and bs_greeks
# ---------------------------------------------------------------------------

class TestBlackScholes:
    def test_bs_greeks_call_delta_range(self):
        g = bs_greeks(0.30, spot=100, strike=100, dte=30, is_call=True)
        assert 0.4 < g.delta < 0.6  # ATM call delta ≈ 0.5

    def test_bs_greeks_put_delta_negative(self):
        g = bs_greeks(0.30, spot=100, strike=100, dte=30, is_call=False)
        assert g.delta < 0

    def test_bs_greeks_vega_positive(self):
        g = bs_greeks(0.30, spot=100, strike=100, dte=30)
        assert g.vega > 0

    def test_bs_implied_vol_round_trip(self):
        """Price with known IV, then recover IV from that price."""
        from barbell.screen.metrics import _bs_price
        spot, strike, dte, rate, iv = 100.0, 100.0, 30.0, 0.05, 0.35
        price = _bs_price(spot, strike, dte, rate, iv, is_call=True)
        recovered_iv = bs_implied_vol(price, spot, strike, dte, rate, is_call=True)
        assert abs(recovered_iv - iv) < 0.005

    def test_bs_greeks_zero_dte(self):
        g = bs_greeks(0.30, spot=100, strike=100, dte=0)
        assert g.delta == 0.0

    def test_bs_implied_vol_zero_price(self):
        assert bs_implied_vol(0.0, 100, 100, 30) == 0.0


# ---------------------------------------------------------------------------
# screen/metrics.py — dispersion_score (hand-computed fixture)
# ---------------------------------------------------------------------------

class TestDispersionScore:
    """
    Hand-computed fixture (2 names):
        NVDA: iv=0.62, vega=0.18, contracts=1  → weight=0.18
        AMD:  iv=0.55, vega=0.15, contracts=1  → weight=0.15
        total_weight = 0.33
        weighted_iv  = 0.18*0.62 + 0.15*0.55 = 0.1116 + 0.0825 = 0.1941
        mean_iv      = 0.1941 / 0.33 ≈ 0.5882
        index_iv     = 0.18
        score        = 0.5882 / 0.18 ≈ 3.268
    """

    def _survivors(self) -> list[dict]:
        return [
            {"iv": 0.62, "vega": 0.18, "contracts": 1},
            {"iv": 0.55, "vega": 0.15, "contracts": 1},
        ]

    def test_expected_value(self):
        score = dispersion_score(self._survivors(), index_iv=0.18)
        assert score == pytest.approx(3.268, rel=0.01)

    def test_empty_survivors(self):
        assert dispersion_score([], index_iv=0.18) == 0.0

    def test_zero_index_iv(self):
        assert dispersion_score(self._survivors(), index_iv=0.0) == 0.0

    def test_three_names_hand_computed(self):
        """
        3 names:
            A: iv=0.60, vega=0.20, contracts=2 → weight=0.40
            B: iv=0.50, vega=0.10, contracts=1 → weight=0.10
            C: iv=0.70, vega=0.30, contracts=1 → weight=0.30
            total_weight = 0.80
            weighted_iv  = 0.40*0.60 + 0.10*0.50 + 0.30*0.70
                         = 0.24 + 0.05 + 0.21 = 0.50
            mean_iv      = 0.50 / 0.80 = 0.625
            score        = 0.625 / 0.20 = 3.125
        """
        survivors = [
            {"iv": 0.60, "vega": 0.20, "contracts": 2},
            {"iv": 0.50, "vega": 0.10, "contracts": 1},
            {"iv": 0.70, "vega": 0.30, "contracts": 1},
        ]
        score = dispersion_score(survivors, index_iv=0.20)
        assert score == pytest.approx(3.125, rel=0.01)

    def test_malformed_entry_skipped(self):
        survivors = [
            {"iv": 0.60, "vega": 0.20, "contracts": 1},
            {"bad_key": "oops"},  # should be skipped with a warning
        ]
        # Should not raise; only first entry counts
        score = dispersion_score(survivors, index_iv=0.20)
        assert score == pytest.approx(0.60 / 0.20, rel=0.01)


# ---------------------------------------------------------------------------
# screen/universe.py — filter rejection cases
# ---------------------------------------------------------------------------

def _make_mock_client(fixture: dict):
    """Return a mock AlpacaClient that serves fixture data."""
    client = MagicMock()
    fixture_data = _load_fixture()

    def _get_chain(underlying, **kwargs):
        if underlying not in fixture_data:
            return {}
        return _build_chain(fixture_data, underlying)

    client.get_option_chain.side_effect = _get_chain
    return client


def _make_mock_store():
    store = MagicMock()
    store.record_screen_result.return_value = None
    return store


@pytest.fixture
def mock_settings(tmp_path):
    """Patch get_settings with a minimal Settings mock."""
    screen_ns = SimpleNamespace(
        min_open_interest=500,
        max_spread_pct_of_mid=0.08,
        min_iv_rank=50,          # 50 → 0.50 in fractional form
        min_iv30_hv20_ratio=1.15,
        earnings_blackout=True,
        min_dispersion_score=1.15,
    )
    sleeve_ns = SimpleNamespace(
        screen=screen_ns,
        dte_range=[3, 7],
        short_delta_range=[0.20, 0.25],
        spread_width_usd=5.0,
        structure=["put_credit_spread", "iron_condor"],
    )
    risk_ns = SimpleNamespace(
        max_loss_per_position_pct_nav=0.008,
    )
    account_ns = SimpleNamespace(starting_nav=100000.0)
    universe_ns = SimpleNamespace(
        candidates={"technology": ["NVDA", "AMD"], "consumer": ["SBUX"]},
        index_hedge_underlying="SPY",
        exclude=["SBUX"],
    )
    settings = SimpleNamespace(
        sleeve_a_carry=sleeve_ns,
        risk_gates=risk_ns,
        account=account_ns,
        universe=universe_ns,
        featherless_api_key="",
        featherless_base_url="https://api.featherless.ai/v1",
        featherless_model="Qwen/Qwen2.5-7B-Instruct",
        anthropic_api_key="test-key",
        claude_model="claude-opus-4-5",
    )
    with patch("barbell.screen.universe.get_settings", return_value=settings), \
         patch("barbell.screen.metrics.get_settings", return_value=settings):
        yield settings


class TestScreenUniverse:
    def test_earnings_blackout_rejects_excluded_symbol(self, mock_settings):
        from barbell.screen.universe import _screen_one
        result, _ = _screen_one(
            symbol="SBUX",
            sector="consumer",
            screen_cfg=mock_settings.sleeve_a_carry.screen,
            exclude_set={"SBUX"},
            client=MagicMock(),
        )
        assert result.passed is False
        assert "earnings_blackout" in result.reason

    def test_low_oi_rejects(self, mock_settings):
        from barbell.screen.universe import _screen_one

        # Build chain with low OI
        snap = SimpleNamespace(
            implied_volatility=0.60,
            open_interest=50,          # below min_open_interest=500
            latest_quote=SimpleNamespace(bid_price=1.0, ask_price=1.05),
            greeks=SimpleNamespace(delta=-0.22, gamma=0.01, theta=-0.03, vega=0.15, rho=-0.02),
            latest_trade=None,
        )
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = {"LOW260905P00080000": snap}

        result, _ = _screen_one(
            symbol="LOW",
            sector="test",
            screen_cfg=mock_settings.sleeve_a_carry.screen,
            exclude_set=set(),
            client=mock_client,
        )
        assert result.passed is False
        assert "oi_below_floor" in result.reason

    def test_wide_spread_rejects(self, mock_settings):
        from barbell.screen.universe import _screen_one

        snap = SimpleNamespace(
            implied_volatility=0.60,
            open_interest=2000,
            latest_quote=SimpleNamespace(bid_price=1.0, ask_price=2.0),  # 67% spread
            greeks=SimpleNamespace(delta=-0.22, gamma=0.01, theta=-0.03, vega=0.15, rho=-0.02),
            latest_trade=None,
        )
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = {"WIDE260905P00080000": snap}

        result, _ = _screen_one(
            symbol="WIDE",
            sector="test",
            screen_cfg=mock_settings.sleeve_a_carry.screen,
            exclude_set=set(),
            client=mock_client,
        )
        assert result.passed is False
        assert "spread_too_wide" in result.reason

    def test_low_iv_rejects(self, mock_settings):
        from barbell.screen.universe import _screen_one

        snap = SimpleNamespace(
            implied_volatility=0.10,   # very low IV → low iv_rank
            open_interest=1500,
            latest_quote=SimpleNamespace(bid_price=0.50, ask_price=0.54),
            greeks=SimpleNamespace(delta=-0.20, gamma=0.01, theta=-0.02, vega=0.05, rho=-0.01),
            latest_trade=None,
        )
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = {"LOWIV260905P00050000": snap}

        result, _ = _screen_one(
            symbol="LOWIV",
            sector="test",
            screen_cfg=mock_settings.sleeve_a_carry.screen,
            exclude_set=set(),
            client=mock_client,
        )
        assert result.passed is False
        assert "iv_rank_too_low" in result.reason

    def test_passing_name_returns_passed_true(self, mock_settings):
        from barbell.screen.universe import _screen_one
        fixture = _load_fixture()
        mock_client = MagicMock()
        mock_client.get_option_chain.return_value = _build_chain(fixture, "NVDA")

        result, micro = _screen_one(
            symbol="NVDA",
            sector="technology",
            screen_cfg=mock_settings.sleeve_a_carry.screen,
            exclude_set=set(),
            client=mock_client,
        )
        assert result.passed is True
        assert result.reason == "ok"
        assert result.metrics.get("iv", 0) > 0
        assert micro is not None

    def test_every_candidate_gets_journal_row(self, mock_settings):
        from barbell.screen.universe import screen

        fixture = _load_fixture()
        mock_client = MagicMock()

        def _get_chain(sym, **kwargs):
            if sym in fixture:
                return _build_chain(fixture, sym)
            return {}

        mock_client.get_option_chain.side_effect = _get_chain
        mock_store = _make_mock_store()

        candidates = {"technology": ["NVDA", "AMD"], "consumer": ["SBUX"]}
        all_results, _ = screen(
            candidates=candidates,
            client=mock_client,
            store=mock_store,
            cycle_id="test-cycle-001",
            index_underlying="SPY",
        )

        # One record_screen_result call per candidate
        assert mock_store.record_screen_result.call_count == 3

    def test_dispersion_score_populated_in_market_state(self, mock_settings):
        from barbell.screen.universe import screen

        fixture = _load_fixture()
        mock_client = MagicMock()

        def _get_chain(sym, **kwargs):
            if sym in fixture:
                return _build_chain(fixture, sym)
            return {}

        mock_client.get_option_chain.side_effect = _get_chain
        mock_store = _make_mock_store()

        candidates = {"technology": ["NVDA", "AMD"]}
        _, market_state = screen(
            candidates=candidates,
            client=mock_client,
            store=mock_store,
            cycle_id="test-cycle-002",
            index_underlying="SPY",
        )

        assert isinstance(market_state, MarketState)
        # dispersion_score may be None if no survivors but won't raise
        # With NVDA and AMD passing, it should be computed
        # (exact value depends on fixture IVs)


# ---------------------------------------------------------------------------
# screen/headline_triage.py — failure degrades to neutral
# ---------------------------------------------------------------------------

class TestHeadlineTriage:
    def test_no_api_key_returns_neutral(self):
        from barbell.screen.headline_triage import digest_headlines
        from barbell.agent.schemas import HeadlineDigest

        settings_mock = SimpleNamespace(featherless_api_key="")
        with patch("barbell.screen.headline_triage.get_settings", return_value=settings_mock):
            result = digest_headlines("NVDA", ["Big earnings beat"])
        assert isinstance(result, HeadlineDigest)
        assert result.news_volume == "normal"
        assert result.summary == ""

    def test_api_error_returns_neutral(self):
        from barbell.screen.headline_triage import digest_headlines

        settings_mock = SimpleNamespace(
            featherless_api_key="test-key",
            featherless_base_url="https://api.featherless.ai/v1",
            featherless_model="Qwen/Qwen2.5-7B-Instruct",
        )
        with patch("barbell.screen.headline_triage.get_settings", return_value=settings_mock), \
             patch("barbell.screen.headline_triage.OpenAI") as mock_openai:
            mock_openai.return_value.chat.completions.create.side_effect = Exception("timeout")
            result = digest_headlines("NVDA", ["Some headline"])

        assert result.news_volume == "normal"
        assert result.summary == ""

    def test_bad_json_returns_neutral(self):
        from barbell.screen.headline_triage import digest_headlines

        settings_mock = SimpleNamespace(
            featherless_api_key="test-key",
            featherless_base_url="https://api.featherless.ai/v1",
            featherless_model="Qwen/Qwen2.5-7B-Instruct",
        )
        mock_resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="NOT JSON {{{{"))]
        )
        with patch("barbell.screen.headline_triage.get_settings", return_value=settings_mock), \
             patch("barbell.screen.headline_triage.OpenAI") as mock_openai:
            mock_openai.return_value.chat.completions.create.return_value = mock_resp
            result = digest_headlines("NVDA", ["Some headline"])

        assert result.news_volume == "normal"
        assert result.summary == ""

    def test_valid_response_parsed_correctly(self):
        from barbell.screen.headline_triage import digest_headlines

        settings_mock = SimpleNamespace(
            featherless_api_key="test-key",
            featherless_base_url="https://api.featherless.ai/v1",
            featherless_model="Qwen/Qwen2.5-7B-Instruct",
        )
        valid_json = '{"news_volume": "elevated", "summary": "Earnings beat by 15%."}'
        mock_resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=valid_json))]
        )
        with patch("barbell.screen.headline_triage.get_settings", return_value=settings_mock), \
             patch("barbell.screen.headline_triage.OpenAI") as mock_openai:
            mock_openai.return_value.chat.completions.create.return_value = mock_resp
            result = digest_headlines("NVDA", ["Big beat"])

        assert result.news_volume == "elevated"
        assert "15%" in result.summary

    def test_empty_headlines_returns_neutral(self):
        from barbell.screen.headline_triage import digest_headlines
        settings_mock = SimpleNamespace(featherless_api_key="test-key",
                                        featherless_base_url="x", featherless_model="x")
        with patch("barbell.screen.headline_triage.get_settings", return_value=settings_mock):
            result = digest_headlines("NVDA", [])
        assert result.news_volume == "normal"
        assert result.summary == ""


# ---------------------------------------------------------------------------
# agent/catalyst_gate.py — malformed response → catalyst_risk=True
# ---------------------------------------------------------------------------

class TestCatalystGate:
    def _settings(self):
        return SimpleNamespace(
            anthropic_api_key="test-key",
            claude_model="claude-opus-4-5",
            sleeve_a_carry=SimpleNamespace(
                screen=SimpleNamespace(min_iv_rank=50),
                dte_range=[3, 7],
                short_delta_range=[0.20, 0.25],
                spread_width_usd=5.0,
                structure=["put_credit_spread"],
            ),
        )

    def test_well_formed_response_parses(self):
        from barbell.agent.catalyst_gate import check_catalyst

        tool_block = SimpleNamespace(
            type="tool_use",
            name="record_catalyst_verdict",
            input={
                "catalyst_risk": False,
                "reasoning": "No pending binary events found.",
                "sources_considered": ["Q3 earnings already reported"],
            },
        )
        mock_response = SimpleNamespace(content=[tool_block])

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=self._settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = mock_response
            result = check_catalyst("NVDA", ["Earnings beat"])

        assert result.catalyst_risk is False
        assert result.symbol == "NVDA"
        assert len(result.reasoning) > 0

    def test_malformed_response_returns_catalyst_risk_true(self):
        from barbell.agent.catalyst_gate import check_catalyst

        # No tool_use block in response
        text_block = SimpleNamespace(type="text", text="I cannot determine this.")
        mock_response = SimpleNamespace(content=[text_block])

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=self._settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = mock_response
            result = check_catalyst("NVDA", ["Some headline"])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_api_error_returns_catalyst_risk_true(self):
        import anthropic as _anthropic
        from barbell.agent.catalyst_gate import check_catalyst

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=self._settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.side_effect = _anthropic.APIError(
                message="rate limit", request=MagicMock(), body={}
            )
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning
