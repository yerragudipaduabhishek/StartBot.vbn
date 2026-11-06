import pytest

from app.indicators.derivatives import derivatives_features, funding_features, oi_features


def test_funding_neutral():
    f = funding_features(0.0001)
    assert f["funding_score"] == 0.0


def test_funding_crowded_long():
    f = funding_features(0.0, funding_8h=0.0015)
    assert f["funding_score"] < 0


def test_funding_crowded_short():
    f = funding_features(0.0, funding_8h=-0.002)
    assert f["funding_score"] > 0


def test_oi_rising():
    o = oi_features("1000", 500000, 50000, 50000000)  # 1% of notional
    assert o["oi_score"] > 0
    assert "%" in o["oi_explanation"]


def test_oi_flat():
    o = oi_features("1000", 0.0, 50000, 50000000)
    assert o["oi_score"] == 0.0


def test_oi_change_pct_computed():
    o = oi_features("1000", 500000, 50000, 50000000)  # 1% of notional
    assert o["oi_change_pct_6h"] == pytest.approx(1.0)


def test_derivatives_features_combines():
    d = derivatives_features(
        {
            "funding_rate": 0.00005,
            "funding_rate_8h": 0.00005,
            "oi_contracts": "1000",
            "oi_change_usd_6h": 0.0,
            "oi_value_usd": "50000000",
            "close": 50000,
        }
    )
    assert "funding_score" in d and "oi_score" in d and "oi_change_pct_6h" in d
    assert d["oi_change_pct_6h"] == pytest.approx(0.0)