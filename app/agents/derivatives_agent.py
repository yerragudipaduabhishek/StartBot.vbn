"""Derivatives agent: funding + open interest positioning signals."""

from app.agents.base import Vote
from app.indicators.derivatives import derivatives_features


class DerivativesAgent:
    name = "derivatives"

    def analyze(self, ticker: dict) -> Vote:
        if not ticker:
            return Vote(agent=self.name, direction="neutral", score=0.0, confidence=0.0, explanation="no derivatives data", details={})

        f = derivatives_features(ticker)
        # Blend funding (contrarian lean) and OI (conviction) evenly.
        score = round(0.5 * f["funding_score"] + 0.5 * f["oi_score"], 3)
        # Modest score -> low confidence; extreme positioning -> higher confidence.
        confidence = round(min(1.0, abs(score)), 3)
        explanation = f"{f['funding_explanation']}; {f['oi_explanation']}"
        return Vote(
            agent=self.name,
            direction="long" if score > 0.25 else ("short" if score < -0.25 else "neutral"),
            score=score,
            confidence=confidence,
            explanation=explanation,
            details={
                "funding_8h": f["funding_8h"],
                "fuel_8h_pct": round(f["funding_8h"] * 100.0, 4),
                "oi_change_pct_6h": f["oi_change_pct_6h"],
            },
        )