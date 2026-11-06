from typing import Literal

from pydantic import BaseModel

Direction = Literal["long", "short", "neutral"]


class Vote(BaseModel):
    """An agent's scored opinion.

    score is signed: positive = bullish, negative = bearish, range [-1, 1].
    confidence is 0..1.
    """

    agent: str
    direction: Direction
    score: float
    confidence: float
    explanation: str
    details: dict = {}


def score_to_direction(score: float, threshold: float = 0.25) -> Direction:
    if score >= threshold:
        return "long"
    if score <= -threshold:
        return "short"
    return "neutral"