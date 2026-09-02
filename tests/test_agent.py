"""
Tests for agent/catalyst_gate.py and agent/structure_agent.py.

Both Anthropic and Featherless are fully mocked — no live API calls.
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
        anthropic_api_key="test-key",
        claude_model="claude-opus-4-5",
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
        risk_gates=SimpleNamespace(max_loss_per_position_pct_nav=0.008),
        account=SimpleNamespace(starting_nav=100000.0),
    )


def _tool_response(name: str, input_dict: dict) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=name, input=input_dict)
    return SimpleNamespace(content=[block])


# ---------------------------------------------------------------------------
# catalyst_gate.py
# ---------------------------------------------------------------------------


class TestCatalystGateFull:
    """Extended tests for agent/catalyst_gate.py."""

    def test_catalyst_risk_false_parsed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _tool_response(
            "record_catalyst_verdict",
            {
                "catalyst_risk": False,
                "reasoning": "Q3 earnings already reported; IV resetting lower.",
                "sources_considered": ["earnings report headline"],
            },
        )
        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = check_catalyst("NVDA", ["Earnings beat"])

        assert isinstance(result, CatalystVerdict)
        assert result.catalyst_risk is False
        assert result.symbol == "NVDA"
        assert "earnings" in result.reasoning.lower()
        assert result.sources_considered == ["earnings report headline"]

    def test_catalyst_risk_true_parsed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _tool_response(
            "record_catalyst_verdict",
            {
                "catalyst_risk": True,
                "reasoning": "Active FDA PDUFA date in 5 days — binary outcome unpriced.",
                "sources_considered": ["FDA headline"],
            },
        )
        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = check_catalyst("BIOC", ["FDA decision imminent"])

        assert result.catalyst_risk is True

    def test_no_tool_block_fails_closed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        text_block = SimpleNamespace(type="text", text="I'm unsure.")
        resp = SimpleNamespace(content=[text_block])

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_api_exception_fails_closed(self):
        import anthropic as _ant
        from barbell.agent.catalyst_gate import check_catalyst

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.side_effect = _ant.APIError(
                message="503 Service Unavailable", request=MagicMock(), body={}
            )
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_missing_reasoning_field_fails_closed(self):
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _tool_response(
            "record_catalyst_verdict",
            {"catalyst_risk": False},  # missing "reasoning"
        )
        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = check_catalyst("NVDA", [])

        assert result.catalyst_risk is True
        assert "fail-closed" in result.reasoning

    def test_digest_context_included_when_provided(self):
        """Verify that a HeadlineDigest is accepted without raising."""
        from barbell.agent.catalyst_gate import check_catalyst

        resp = _tool_response(
            "record_catalyst_verdict",
            {
                "catalyst_risk": False,
                "reasoning": "No binary risk detected.",
                "sources_considered": [],
            },
        )
        digest = HeadlineDigest(symbol="AMD", news_volume="elevated", summary="Big move.")
        screen_r = ScreenResult(
            symbol="AMD", passed=True, reason="ok",
            metrics={"iv": 0.55, "iv_rank": 0.72},
        )

        with patch("barbell.agent.catalyst_gate.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.catalyst_gate.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = check_catalyst("AMD", ["Headline"], digest=digest, screen_result=screen_r)

        assert isinstance(result, CatalystVerdict)


# ---------------------------------------------------------------------------
# structure_agent.py
# ---------------------------------------------------------------------------


class TestStructureAgent:
    def _valid_legs(self):
        return [
            {
                "expiry": "2026-09-05",
                "strike": 110.0,
                "right": "put",
                "side": "sell",
                "contracts": 1,
                "ratio_qty": 1,
            },
            {
                "expiry": "2026-09-05",
                "strike": 105.0,
                "right": "put",
                "side": "buy",
                "contracts": 1,
                "ratio_qty": 1,
            },
        ]

    def _valid_tool_input(self) -> dict:
        return {
            "underlying": "NVDA",
            "sleeve": "A",
            "structure_type": "put_credit_spread",
            "rationale": "Put skew elevated; ATM put delta ≈ 0.22; dispersion score 3.2.",
            "max_loss_estimate": 500.0,
            "limit_price": 1.50,
            "legs": self._valid_legs(),
        }

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

        resp = _tool_response("record_proposed_structure", self._valid_tool_input())
        screen_r = ScreenResult(
            symbol="NVDA", passed=True, reason="ok",
            metrics={"iv": 0.62, "iv_rank": 0.75, "iv30_hv20_ratio": 1.3},
        )

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.structure_agent.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
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

        resp = _tool_response("record_proposed_structure", self._valid_tool_input())
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.structure_agent.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
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

        text_block = SimpleNamespace(type="text", text="Here is my analysis...")
        resp = SimpleNamespace(content=[text_block])
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.structure_agent.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            with pytest.raises(ValueError, match="record_proposed_structure"):
                propose_structure("NVDA", self._mock_chain(), screen_r)

    def test_empty_legs_raises_value_error(self):
        from barbell.agent.structure_agent import propose_structure

        bad_input = {**self._valid_tool_input(), "legs": []}
        resp = _tool_response("record_proposed_structure", bad_input)
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.structure_agent.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            with pytest.raises((ValueError, Exception)):
                propose_structure("NVDA", self._mock_chain(), screen_r)

    def test_dispersion_score_none_accepted(self):
        """dispersion_score=None should not cause an error."""
        from barbell.agent.structure_agent import propose_structure

        resp = _tool_response("record_proposed_structure", self._valid_tool_input())
        screen_r = ScreenResult(symbol="NVDA", passed=True, reason="ok", metrics={})

        with patch("barbell.agent.structure_agent.get_settings", return_value=_base_settings()), \
             patch("barbell.agent.structure_agent.anthropic.Anthropic") as mock_ant:
            mock_ant.return_value.messages.create.return_value = resp
            result = propose_structure("NVDA", self._mock_chain(), screen_r, dispersion_score=None)

        assert isinstance(result, ProposedStructure)
