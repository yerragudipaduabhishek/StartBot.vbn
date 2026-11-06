"""Weighted ensemble scoring for agent votes."""

from collections.abc import Mapping, Sequence

from app.agents.base import Vote

DEFAULT_WEIGHTS: dict[str, float] = {
    "trend": 0.45,
    "structure": 0.25,
    "sentiment": 0.15,
    "derivatives": 0.15,
}


def weighted_score(votes: Sequence[Vote], weights: Mapping[str, float] | None = None) -> tuple[float, float, dict[str, float]]:
    table = dict(DEFAULT_WEIGHTS)
    if weights:
        table.update(weights)

    denom = sum(max(table.get(v.agent, 0.0), 0.0) for v in votes)
    if denom <= 1e-12:
        return 0.0, 0.0, {}

    total = 0.0
    conf_sum = 0.0
    contributions: dict[str, float] = {}
    for v in votes:
        w = max(table.get(v.agent, 0.0), 0.0)
        total += w * v.score
        conf_sum += w * v.confidence
        contributions[v.agent] = round(w * v.score / denom, 4)

    return round(total / denom, 3), round(min(1.0, conf_sum / denom), 3), contributions