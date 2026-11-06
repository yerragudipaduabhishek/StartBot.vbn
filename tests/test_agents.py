import numpy as np
import pandas as pd
import pytest

from app.agents.decision_agent import DecisionAgent
from app.agents.derivatives_agent import DerivativesAgent
from app.agents.sentiment_agent import NeutralSentiment, SentimentAgent, VaderSentiment
from app.agents.structure_agent import StructureAgent
from app.agents.trend_agent import TrendAgent
from app.indicators.technical import add_all


def make_df(direction: str = "up", n: int = 120) -> pd.DataFrame:
    base = 50000 if direction == "up" else 70000
    step = 30 if direction == "up" else -30
    noise = np.random.default_rng(1).normal(0, 15, n)
    close = base + np.arange(n) * step + noise
    return pd.DataFrame(
        {
            "time": np.arange(n),
            "open": close,
            "high": close + 50,
            "low": close - 50,
            "close": close,
            "volume": np.full(n, 10.0),
        }
    )


def test_trend_agent_bullish():
    df = add_all(make_df("up"))
    v = TrendAgent().analyze(df)
    assert v.score > 0
    assert v.direction == "long"
    assert "EMA" in v.explanation


def test_trend_agent_bearish():
    df = add_all(make_df("down"))
    v = TrendAgent().analyze(df)
    assert v.score < 0
    assert v.direction == "short"


def test_structure_agent_bullish():
    df = add_all(make_df("up"))
    v = StructureAgent().analyze(df)
    assert v.score > 0


def test_sentiment_neutral_default():
    v = SentimentAgent(provider=NeutralSentiment()).analyze(None)
    assert v.score == 0.0
    assert v.direction == "neutral"


def test_vader_skipped_if_missing():
    try:
        provider = VaderSentiment()
    except RuntimeError:
        pytest.skip("vaderSentiment not installed")
    v = SentimentAgent(provider=provider).analyze(["great news, bitcoin rallies to all time high"])
    assert v.score > 0


def test_derivatives_agent_extreme_funding():
    ticker = {"funding_rate": 0.0005, "funding_rate_8h": 0.0012, "oi_contracts": "100", "oi_change_usd_6h": 0.0, "close": 50000, "oi_value_usd": "5000000"}
    v = DerivativesAgent().analyze(ticker)
    assert v.score < 0  # crowded longs -> bearish lean


def test_decision_agent_aggregates():
    from app.agents.base import Vote

    votes = [
        Vote(agent="trend", direction="long", score=1.0, confidence=0.9, explanation="x"),
        Vote(agent="structure", direction="long", score=0.5, confidence=0.7, explanation="y"),
        Vote(agent="sentiment", direction="neutral", score=0.0, confidence=0.0, explanation="z"),
        Vote(agent="derivatives", direction="neutral", score=0.0, confidence=0.0, explanation="w"),
    ]
    d = DecisionAgent().decide(votes)
    assert d.direction == "long"
    assert d.score > 0.5
    assert "trend" in d.details["contributions"]


def test_decision_agent_no_votes():
    d = DecisionAgent().decide([])
    assert d.direction == "neutral"
    assert d.score == 0.0