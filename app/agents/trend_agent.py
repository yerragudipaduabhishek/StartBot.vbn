"""Trend agent: EMA alignment, directional movement, VWAP, MACD."""

import pandas as pd

from app.agents.base import Vote


class TrendAgent:
    name = "trend"

    def analyze(self, df: pd.DataFrame) -> Vote:
        """df: output of indicators.technical.add_all (must include a close row)."""
        last = df.iloc[-1]
        score = 0.0
        parts: list[str] = []

        ema_fast, ema_slow, ema_mid = last["ema_fast"], last["ema_slow"], last["ema_mid"]
        if ema_fast > ema_slow > ema_mid:
            score += 0.5
            parts.append("EMAs aligned bullish (fast>slow>mid)")
        elif ema_fast < ema_slow < ema_mid:
            score -= 0.5
            parts.append("EMAs aligned bearish (fast<slow<mid)")
        else:
            parts.append("EMAs mixed")

        plus_di, minus_di = last["plus_di"], last["minus_di"]
        if plus_di > minus_di:
            score += 0.2
            parts.append(f"+DI {plus_di:.1f} > -DI {minus_di:.1f}")
        else:
            score -= 0.2
            parts.append(f"-DI {minus_di:.1f} > +DI {plus_di:.1f}")

        close, vwap = last["close"], last["vwap"]
        if close > vwap:
            score += 0.15
            parts.append("price above VWAP")
        else:
            score -= 0.15
            parts.append("price below VWAP")

        if last["macd_hist"] > 0:
            score += 0.15
            parts.append("MACD histogram positive")
        else:
            score -= 0.15
            parts.append("MACD histogram negative")

        adj = max(-1.0, min(1.0, score))
        adx = float(last.get("adx", 0.0))
        confidence = min(1.0, max(0.0, (adx - 15.0) / 40.0))

        from app.agents.base import score_to_direction

        return Vote(
            agent=self.name,
            direction=score_to_direction(adj),
            score=round(adj, 3),
            confidence=round(confidence, 3),
            explanation="; ".join(parts) or "no clear trend",
            details={"adx": adx, "ema_fast": ema_fast, "ema_slow": ema_slow, "ema_mid": ema_mid},
        )