"""Decision agent: combines agent votes into a single directional call."""

from collections.abc import Sequence

from app.agents.base import Vote, score_to_direction
from app.strategy.scoring import weighted_score


class DecisionAgent:
    name = "decision"

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        self.weights = weights or {}

    def decide(self, votes: Sequence[Vote], threshold: float = 0.25) -> Vote:
        if not votes:
            return Vote(agent=self.name, direction="neutral", score=0.0, confidence=0.0, explanation="no agent votes", details={})

        score, confidence, contributions = weighted_score(votes, self.weights)
        direction = score_to_direction(score, threshold)
        explanation = "; ".join(f"{v.agent}={v.score:+.2f}" for v in votes)
        return Vote(
            agent=self.name,
            direction=direction,
            score=round(score, 3),
            confidence=round(confidence, 3),
            explanation=explanation,
            details={"contributions": contributions},
        )