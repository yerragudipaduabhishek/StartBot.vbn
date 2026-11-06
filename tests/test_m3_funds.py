"""Milestone 3 validation: trading-fund selection, max-risk math, and the
dashboard's paper/test-balance vs trading-fund distinction.

The analysis layer takes a *selected trading fund* (INR) plus a risk %, and
must report max_risk_inr = fund x risk_pct / 100 deterministically. The paper
account (10k play balance) shown by the dashboard is a separate concept from the
selected trading fund — nothing here changes app/risk defaults.
"""

import pytest

from app.analysis.config import DEFAULT_FUND_INR, DEFAULT_RISK_PCT, FUND_PRESETS_INR
from app.analysis.service import AnalysisService
from app.risk.config import RiskSettings
from tests.m3_fixtures import SingleSymbolStub, make_candles

# Standard deterministic BTCUSDT fixtures (trending so a signal exists).
CANDLES = make_candles(200, 50000.0, 25.0, 0.005)
RISK_PCT = 0.5  # the dashboard's chosen default


def _analyze(fund: float, risk_pct: float = RISK_PCT) -> dict:
    return AnalysisService(client=SingleSymbolStub(candles=CANDLES)).analyze(
        "BTCUSDT", fund_inr=fund, risk_pct=risk_pct
    )


def test_fund_presets_match_requirement():
    assert FUND_PRESETS_INR == [1000, 2500, 5000, 10000, 25000]
    assert DEFAULT_FUND_INR == 5000
    assert DEFAULT_RISK_PCT == 0.5


@pytest.mark.parametrize(
    ("fund", "expected_max_risk"),
    [
        (1000, 5.0),
        (2500, 12.5),
        (5000, 25.0),
        (10000, 50.0),
        (25000, 125.0),
    ],
)
def test_max_risk_equals_fund_times_risk_pct(fund, expected_max_risk):
    out = _analyze(fund)
    assert out["fund_inr"] == fund
    assert out["risk_pct"] == RISK_PCT
    assert out["max_risk_inr"] == expected_max_risk
    # The trade-plan risk line carries the same number.
    assert out["plan"]["risk_line"].startswith(f"max risk {expected_max_risk:.2f}")


def test_custom_fund_is_supported():
    fund = 7777.0
    out = _analyze(fund)
    assert out["fund_inr"] == fund
    assert out["max_risk_inr"] == round(fund * RISK_PCT / 100.0, 2)


def test_default_fund_applied_when_not_specified():
    out = AnalysisService(client=SingleSymbolStub(candles=CANDLES)).analyze("BTCUSDT")
    assert out["fund_inr"] == DEFAULT_FUND_INR
    assert out["max_risk_inr"] == round(DEFAULT_FUND_INR * DEFAULT_RISK_PCT / 100.0, 2)


def test_risk_config_defaults_untouched():
    """The dashboard passes risk_pct per-call; app/risk defaults must stay as-designed."""
    s = RiskSettings()
    assert s.risk_per_trade_pct == 1.0
    assert s.max_leverage == 5.0
    assert s.max_daily_loss_pct == 5.0
    assert s.volatility_skip_pct == 3.0


def test_sizing_scales_with_fund_but_risk_budget_stays_honest():
    """Bigger fund -> bigger max-risk budget; the plan never exceeds that budget."""
    prev_notional = None
    for fund in (1000, 5000, 25000):
        out = _analyze(fund)
        plan = out["plan"]
        assert plan["position_size"] is not None
        # Largest allowed notional on this fund: equity x effective leverage.
        assert plan["notional"] <= fund * out["leverage"]["effective"] + 1e-6
        if prev_notional is not None:
            assert plan["notional"] > prev_notional
        prev_notional = plan["notional"]
        # A stop-hit must not cost more than fund x risk_pct.
        loss = plan["position_size"] * out["market"]["contract_value"] * abs(
            plan["entry"] - plan["stop_loss"]
        )
        assert loss <= fund * RISK_PCT / 100.0 + 1e-6


def test_fund_zero_rejected_by_sizer_without_crash():
    """edge fund values must not produce a fabricated size."""
    out = _analyze(0)
    assert out["max_risk_inr"] == 0.0
    assert out["plan"]["position_size"] is None