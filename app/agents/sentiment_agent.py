"""Sentiment agent: scores news headlines via pluggable providers.

Default provider is neutral (no news source configured yet). A VADER provider
is available offline once headlines are supplied (e.g. from a news RSS feed).
"""

from abc import ABC, abstractmethod

from app.agents.base import Vote


class SentimentProvider(ABC):
    name: str = "provider"

    @abstractmethod
    def score_texts(self, texts: list[str]) -> tuple[float, float, str]:
        """Return (score in [-1,1], confidence 0..1, explanation)."""


class NeutralSentiment(SentimentProvider):
    name = "neutral"

    def score_texts(self, texts: list[str]) -> tuple[float, float, str]:
        return 0.0, 0.0, "no news source configured - sentiment treated as neutral"


class VaderSentiment(SentimentProvider):
    name = "vader"

    def __init__(self) -> None:
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

            self._analyzer = SentimentIntensityAnalyzer()
        except ImportError as e:
            raise RuntimeError("pip install vaderSentiment to use the VADER provider") from e

    def score_texts(self, texts: list[str]) -> tuple[float, float, str]:
        if not texts:
            return 0.0, 0.0, "no headlines to score"
        scores = [self._analyzer.polarity_scores(t)["compound"] for t in texts if t]
        if not scores:
            return 0.0, 0.0, "no headlines to score"
        avg = sum(scores) / len(scores)
        confidence = min(1.0, len([s for s in scores if abs(s) > 0.05]) / 10.0)
        label = "net positive headlines" if avg > 0.05 else ("net negative headlines" if avg < -0.05 else "headlines balanced")
        return round(avg, 3), round(confidence, 3), f"{label} ({len(scores)} headlines, mean compound {avg:+.3f})"


class SentimentAgent:
    name = "sentiment"

    def __init__(self, provider: SentimentProvider | None = None) -> None:
        self.provider = provider or NeutralSentiment()

    def analyze(self, headlines: list[str] | None = None) -> Vote:
        score, confidence, explanation = self.provider.score_texts(headlines or [])
        return Vote(
            agent=self.name,
            direction="long" if score > 0.25 else ("short" if score < -0.25 else "neutral"),
            score=score,
            confidence=confidence,
            explanation=explanation,
            details={"provider": self.provider.name, "headlines_scored": len(headlines or [])},
        )