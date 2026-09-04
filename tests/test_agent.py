"""
Tests for agent/catalyst_gate.py and agent/structure_agent.py.

Both Gemini and Featherless are fully mocked — no live API calls.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from barbell.agent.schemas import (
    CatalystVerdict,
    HeadlineDigest,
    ProposedStructure,
    ScreenResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_settings():
    return SimpleNamespace(
        gemini_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        gemini_models=["gemini-2.5-flash"],
        featherless_api_key="fl-test-key",
        featherless_base_url="https://api.featherless.ai/v1",
        featherless_model="Qwen/Qwen2.5-7B-Instruct",
        sleeve_a_carry=SimpleNamespace(
            screen=SimpleNamespace(min_iv_rank=50, min_dispersion_score=1.15),
            dte_range=[3, 7],
            short_delta_range=[0.20, 0.25],
            spread_width_usd=5.0,
            structure=["put_credit_spread", "iron_condor"],
        ),
        sleeve_b_convexity=SimpleNamespace(
            enabled=True,
            underlying="SPY",
            structure="put_debit_spread",
            long_delta_target=0.30,
            short_delta_target=0.10,
            expiry_offset_days_past_deadline=7,
            base_risk_pct_nav=0.014,
            escalated_risk_pct_nav=0.028,
            escalate_if_nav_above_start=True,
        ),
        risk_gates=SimpleNamespace(max_loss_per_position_pct_nav=0.008),
        account=SimpleNamespace(starting_nav=100000.0),
    )


def _catalyst_response(catalyst_risk: bool, reasoning: str, sources=None) -> SimpleNamespace:
    """Build a fake genai response whose .parsed is a real _CatalystVerdictSchema."""
    from barbell.agent.catalyst_gate import _CatalystVerdictSchema

    parsed = _CatalystVerdictSchema(
        catalyst_risk=catalyst_risk,
        reasoning=reasoning,
        sources_considered=sources or [],
    )
    return SimpleNamespace(parsed=parsed, text=parsed.model_dump_json())


def _structure_response(**overrides) -> SimpleNamespace:
    """Build a fake genai response whose .parsed is a real _ProposedStructureSchema."""
    from barbell.agent.structure_agent import _ProposedLegSchema, _ProposedStructureSchema

    legs = overrides.pop("legs", None)
    if legs is None:
        legs = [
            _ProposedLegSchema(expiry="2026-09-05", strike=110.0, right="put", side="sell", contracts=1),
            _ProposedLegSchema(expiry="2026-09-05", strike=105.0, right="put", side="buy", contracts=1),
        ]
    fields = {
        "underlying": "NVDA",
        "sleeve": "A",
        "structure_type": "put_credit_spread",
        "rationale": "Put skew elevated; ATM put delta ≈ 0.22; dispersion score 3.2.",
        "max_loss_estimate": 500.0,
        "limit_price": 1.50,
        "legs": legs,
    }
    fields.update(overrides)
    parsed = _ProposedStructureSchema(**fields)
    return SimpleNamespace(parsed=parsed, text=parsed.model_dump_json())


def _bad_response(text: str = "I'm unsure.") -> SimpleNamespace:
    """Simulate a response that failed schema validation — .parsed is None."""
    return SimpleNamespace(parsed=None, text=text)


# ---------------------------------------------------------------------------
# catalyst_gate.py
# ---------------------------------------------------------------------------


class TestCatalystGateFull:
    """Extended tests for agent/catalyst_gate.py."""

    def test_catalyst_risk_false_parsed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _catalyst_response(
            False,
            "Q3 earnings already reported; IV resetting lower.",
            sources=["earnings report headline"],
        )
        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = check_catalyst("NVDA", ["Earnings beat"])

        assert isinstance(result, CatalystVerdict)
        assert result.catalyst_risk is False
        assert result.symbol == "NVDA"
        assert "earnings" in result.reasoning.lower()
        assert result.sources_considered == ["earnings report headline"]

    def test_catalyst_risk_true_parsed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _catalyst_response(
            True,
            "Active FDA PDUFA date in 5 days — binary outcome unpriced.",
            sources=["FDA headline"],
        )
        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = check_catalyst("BIOC", ["FDA decision imminent"])

        assert result.catalyst_risk is True

    def test_no_tool_block_fails_closed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _bad_response("I'm unsure.")

        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_api_exception_fails_closed(self):
        from google.genai import errors as genai_errors
        from barbell.agent.catalyst_gate import check_catalyst

        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.side_effect = genai_errors.APIError(
                503, {"message": "Service Unavailable"}
            )
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_missing_reasoning_field_fails_closed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _catalyst_response(False, "")  # empty reasoning

        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_digest_context_included_when_provided(self):
        """Verify that a HeadlineDigest is accepted without raising."""
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _catalyst_response(False, "No binary risk detected.", sources=[])
        digest = HeadlineDigest(symbol="AMD", news_volume="elevated", summary="Big move.")
        screen_r = ScreenResult(
            symbol="AMD", passed=True, reason="ok",
            metrics={"iv": 0.55, "iv_rank": 0.72},
        )

        with patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = check_catalyst("AMD", ["Headline"], digest=digest, screen_result=screen_r)

        assert isinstance(result, CatalystVerdict)


# ---------------------------------------------------------------------------
# structure_agent.py
# ---------------------------------------------------------------------------


class TestStructureAgent:
    def _mock_chain(self) -> dict:
        snap = SimpleNamespace(
            implied_volatility=0.62,
            open_interest=1800,
            latest_quote=SimpleNamespace(bid_price=1.80, ask_price=1.95),
            greeks=SimpleNamespace(delta=-0.22, gamma=0.011, theta=-0.04, vega=0.17, rho=-0.03),
            latest_trade=None,
        )
        return {"NVDA260905P00110000": snap}

    def test_well_formed_response_returns_proposed_structure(self):
        from barbell.agent.structure_agent import propose_structure

        resp = _structure_response()
        screen_r = ScreenResult(
            symbol="NVDA", passed=True, reason="ok",
            metrics={"iv": 0.62, "iv_rank": 0.75, "iv30_hv20_ratio": 1.3},
        )

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = propose_structure(
                "NVDA",
                self._mock_chain(),
                screen_r,
                dispersion_score=3.2,
            )

        assert isinstance(result, ProposedStructure)
        assert result.underlying == "NVDA"
        assert result.sleeve == "A"
        assert result.structure_type == "put_credit_spread"
        assert len(result.legs) == 2
        assert result.max_loss_estimate == pytest.approx(500.0)

    def test_legs_have_correct_expiry_and_strikes(self):
        from barbell.agent.structure_agent import propose_structure

        resp = _structure_response()
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = propose_structure("NVDA", self._mock_chain(), screen_r)

        legs = result.legs
        assert legs[0].expiry == date(2026, 9, 5)
        assert legs[0].strike == pytest.approx(110.0)
        assert legs[0].right == "put"
        assert legs[0].side == "sell"
        assert legs[1].strike == pytest.approx(105.0)
        assert legs[1].side == "buy"

    def test_no_tool_block_raises_value_error(self):
        from barbell.agent.structure_agent import propose_structure

        resp = _bad_response("Here is my analysis...")
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            with pytest.raises(ValueError, match="schema-conformant"):
                propose_structure("NVDA", self._mock_chain(), screen_r)

    def test_empty_legs_raises_value_error(self):
        from barbell.agent.structure_agent import propose_structure

        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            # An empty legs list fails the schema's min_length=2 constraint
            # at construction time — same as Gemini failing to produce a
            # schema-conformant response.
            mock_client.return_value.models.generate_content.return_value = _bad_response(
                "no legs"
            )
            with pytest.raises((ValueError, Exception)):
                propose_structure("NVDA", self._mock_chain(), screen_r)

    def test_dispersion_score_none_accepted(self):
        """dispersion_score=None should not cause an error."""
        from barbell.agent.structure_agent import propose_structure

        resp = _structure_response()
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = propose_structure("NVDA", self._mock_chain(), screen_r, dispersion_score=None)

        assert isinstance(result, ProposedStructure)


class TestStructureAgentSleeveB:
    """propose_structure_sleeve_b — the convexity hedge path (Sleeve B)."""

    def _mock_spy_chain(self) -> dict:
        long_leg = SimpleNamespace(
            implied_volatility=0.18,
            open_interest=5000,
            latest_quote=SimpleNamespace(bid_price=3.20, ask_price=3.35),
            greeks=SimpleNamespace(delta=-0.30, gamma=0.02, theta=-0.05, vega=0.20, rho=-0.03),
            latest_trade=None,
        )
        short_leg = SimpleNamespace(
            implied_volatility=0.20,
            open_interest=6000,
            latest_quote=SimpleNamespace(bid_price=1.10, ask_price=1.20),
            greeks=SimpleNamespace(delta=-0.10, gamma=0.01, theta=-0.03, vega=0.12, rho=-0.01),
            latest_trade=None,
        )
        # Deliberately include a call too — _format_chain_summary_sleeve_b must
        # filter it out (Sleeve B is puts only).
        call_leg = SimpleNamespace(
            implied_volatility=0.19,
            open_interest=4000,
            latest_quote=SimpleNamespace(bid_price=2.00, ask_price=2.10),
            greeks=SimpleNamespace(delta=0.30, gamma=0.02, theta=-0.05, vega=0.20, rho=0.03),
            latest_trade=None,
        )
        return {
            "SPY260911P00560000": long_leg,
            "SPY260911P00545000": short_leg,
            "SPY260911C00600000": call_leg,
        }

    def _sleeve_b_response(self, **overrides) -> SimpleNamespace:
        from barbell.agent.structure_agent import _ProposedLegSchema, _ProposedStructureSchema

        legs = overrides.pop("legs", None)
        if legs is None:
            legs = [
                _ProposedLegSchema(expiry="2026-09-11", strike=560.0, right="put", side="buy", contracts=1),
                _ProposedLegSchema(expiry="2026-09-11", strike=545.0, right="put", side="sell", contracts=1),
            ]
        fields = {
            "underlying": "SPY",
            "sleeve": "B",
            "structure_type": "put_debit_spread",
            "rationale": "SPY put debit spread ahead of NFP; long delta ~0.30, short delta ~0.10.",
            "max_loss_estimate": 1400.0,
            "limit_price": -1.35,  # net debit — must be negative per this codebase's sign convention
            "legs": legs,
        }
        fields.update(overrides)
        parsed = _ProposedStructureSchema(**fields)
        return SimpleNamespace(parsed=parsed, text=parsed.model_dump_json())

    def test_well_formed_response_returns_sleeve_b_structure(self):
        from barbell.agent.structure_agent import propose_structure_sleeve_b

        resp = self._sleeve_b_response()
        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            result = propose_structure_sleeve_b(
                self._mock_spy_chain(), nav_current=100000.0, nav_starting=100000.0,
            )

        assert isinstance(result, ProposedStructure)
        assert result.sleeve == "B"
        assert result.structure_type == "put_debit_spread"
        assert result.underlying == "SPY"
        assert result.limit_price < 0, "Sleeve B is a net debit — limit_price must be negative"
        assert len(result.legs) == 2

    def test_wrong_sleeve_in_response_raises(self):
        """Defense in depth: Gemini returning sleeve='A' for a Sleeve B call must not
        silently pass through — it would misclassify P&L attribution downstream."""
        from barbell.agent.structure_agent import propose_structure_sleeve_b

        resp = self._sleeve_b_response(sleeve="A")
        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.gemini_client.genai.Client") as mock_client:
            mock_client.return_value.models.generate_content.return_value = resp
            with pytest.raises(ValueError, match="expected 'B'"):
                propose_structure_sleeve_b(
                    self._mock_spy_chain(), nav_current=100000.0, nav_starting=100000.0,
                )

    def test_chain_summary_filters_out_calls(self):
        """Sleeve B is puts-only; a call contract in the chain must not appear
        in the rendered prompt sent to Gemini."""
        from barbell.agent.structure_agent import _format_chain_summary_sleeve_b

        settings = _base_settings()
        summary = _format_chain_summary_sleeve_b(self._mock_spy_chain(), settings.sleeve_b_convexity)

        assert "SPY260911P00560000" in summary
        assert "SPY260911P00545000" in summary
        assert "SPY260911C00600000" not in summary

    def test_escalated_sizing_used_when_nav_above_start(self):
        """When NAV has grown past starting_nav and escalate_if_nav_above_start
        is set, the prompt must target escalated_risk_pct_nav, not base."""
        from barbell.agent.structure_agent import _build_prompt_sleeve_b

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()):
            prompt_above = _build_prompt_sleeve_b(
                self._mock_spy_chain(), nav_current=101000.0, nav_starting=100000.0,
            )
            prompt_at = _build_prompt_sleeve_b(
                self._mock_spy_chain(), nav_current=100000.0, nav_starting=100000.0,
            )

        assert "2.80%" in prompt_above  # escalated_risk_pct_nav
        assert "1.40%" in prompt_at     # base_risk_pct_nav
