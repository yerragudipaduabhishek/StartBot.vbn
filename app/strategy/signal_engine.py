"""Signal engine: assemble agents -> decision -> explained signal block.

Runs on a candle snapshot (indicator dataframe + live ticker) and produces the
fully-explained LONG / SHORT / NO TRADE output the pipeline is designed for.
"""

import logging
from datetime import datetime

import pandas as pd

from app.agents.base import Vote
from app.agents.decision_agent import DecisionAgent
from app.agents.derivatives_agent import DerivativesAgent
from app.agents.sentiment_agent import NeutralSentiment, SentimentAgent, VaderSentiment
from app.agents.structure_agent import StructureAgent
from app.agents.trend_agent import TrendAgent
from app.config.trading import TradingProfile
from app.data.market_data import MarketData
from app.features.pipeline import FeaturePipeline, ensure_features
from app.risk.risk_manager import RiskManager
from app.strategy.strategy_a import (
    STRATEGY_A_CONTRACT_VALUE,
    STRATEGY_A_ID,
    STRATEGY_A_PARAMS,
    STRATEGY_A_VERSION,
)
from app.strategy.timeframes import infer_timeframe

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(
        self,
        trend_agent: TrendAgent | None = None,
        structure_agent: StructureAgent | None = None,
        sentiment_agent: SentimentAgent | None = None,
        derivatives_agent: DerivativesAgent | None = None,
        decision_agent: DecisionAgent | None = None,
        weights: dict[str, float] | None = None,
        risk_manager: RiskManager | None = None,
        contract_value: float = STRATEGY_A_CONTRACT_VALUE,
        decision_threshold: float = float(STRATEGY_A_PARAMS["threshold"]),
        profile: TradingProfile | None = None,
    ) -> None:
        self.profile = profile
        if profile is not None:
            weights = weights if weights is not None else dict(profile.weights)
            contract_value = contract_value or profile.contract_value
            decision_threshold = profile.decision_threshold
            if risk_manager is None:
                risk_manager = RiskManager(
                    settings=profile.risk,
                    execution_mode=profile.mode,
                )
        self.trend_agent = trend_agent or TrendAgent()
        self.structure_agent = structure_agent or StructureAgent()
        if sentiment_agent is None:
            try:
                provider = VaderSentiment()
            except RuntimeError:
                provider = NeutralSentiment()
            sentiment_agent = SentimentAgent(provider=provider)
        self.sentiment_agent = sentiment_agent
        self.derivatives_agent = derivatives_agent or DerivativesAgent()
        self.decision_agent = decision_agent or DecisionAgent(weights=weights)
        self.risk_manager = risk_manager
        self.contract_value = contract_value
        self.decision_threshold = decision_threshold
        ema_spans = profile.ema_spans if profile is not None else (12, 26, 50)
        self.feature_pipeline = FeaturePipeline(ema_spans=ema_spans)

    def generate(self, market: MarketData, headlines: list[str] | None = None) -> dict:
        candles = market.candles
        if len(candles) < 30:
            return {
                "direction": "none",
                "reason": "insufficient candle history",
                **{k: None for k in ("score", "confidence", "entry", "stop_loss", "target")},
                "accepted_size": None,
                "risk_allowed": False,
                "risk_reason": "insufficient candle history",
                "strategy_id": STRATEGY_A_ID,
                "strategy_version": STRATEGY_A_VERSION,
                "votes": [],
                "time": None,
            }

        df = self.feature_pipeline.transform(pd.DataFrame(candles))
        sig = self.analyze_candle(df, market.ticker or {}, headlines)
        sig["time"] = candles[-1]["time"]
        sig["feature_pipeline_version"] = self.feature_pipeline.version
        return sig

    def analyze_candle(
        self,
        df: pd.DataFrame,
        ticker: dict | None = None,
        headlines: list[str] | None = None,
        end_idx: int | None = None,
        equity: float | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Core per-candle analysis. `df` must already have indicator columns
        (see indicators.technical.add_all). No lookahead: only the rows up to
        `end_idx` (or the final row) are used. Passing `end_idx` avoids the
        per-bar DataFrame copy that index-based slicing would create. ``equity``
        lets a replay size against mark-to-market equity instead of realized
        equity only."""
        if end_idx is None:
            end_idx = len(df) - 1
        df = ensure_features(df, ema_spans=self.feature_pipeline.ema_spans)
        lo = max(0, end_idx - 119)
        window = df.iloc[lo : end_idx + 1]
        ticker = ticker or {}

        votes: list[Vote] = [
            self.trend_agent.analyze(window),
            self.structure_agent.analyze(window),
            self.sentiment_agent.analyze(headlines),
            self.derivatives_agent.analyze(ticker),
        ]
        decision = self.decision_agent.decide(votes, self.decision_threshold)

        last = window.iloc[-1]
        entry = float(last["close"])
        atr = float(last["atr"])

        sl_long = entry - self.risk_sl_multiple() * atr if decision.direction == "long" else None
        sl_short = entry + self.risk_sl_multiple() * atr if decision.direction == "short" else None
        target_long = entry + self.risk_target_multiple() * atr if decision.direction == "long" else None
        target_short = entry - self.risk_target_multiple() * atr if decision.direction == "short" else None

        risk_line = "  Risk:         N/A (risk unavailable)"
        accepted_size = None
        risk_allowed = False
        risk_reason = "risk manager unavailable"
        if decision.direction == "neutral":
            risk_reason = "neutral direction"
        elif self.risk_manager is not None:
            prev = window["close"].iloc[-2] if len(window) >= 2 else 0.0
            candle_time = int(last["time"]) if "time" in window.columns else None
            bar_seconds = (
                float(candle_time) - float(window["time"].iloc[-2])
                if candle_time is not None and len(window) >= 2 and "time" in window.columns
                else None
            )
            candidate = {
                "direction": decision.direction,
                "entry": entry,
                "stop_loss": sl_long or sl_short,
                "target": target_long or target_short,
                "confidence": decision.confidence,
                "prev_close": prev,
                "candle_time": candle_time,
                "timeframe": infer_timeframe(bar_seconds) if bar_seconds else None,
                "strategy_id": STRATEGY_A_ID,
                "strategy_version": STRATEGY_A_VERSION,
            }
            allowed, sizing, reason = self.risk_manager.evaluate(
                candidate,
                equity=self.risk_equity() if equity is None else float(equity),
                contract_value=self.contract_value,
                now=now,
            )
            risk_line = f"  Risk:         {'ACCEPTABLE' if allowed else 'REJECTED'} - {reason}"
            accepted_size = sizing.quantities if sizing else None
            risk_allowed = allowed
            risk_reason = reason

        explanation = "\n".join(
            [
                f"  Trend:        {self._vote_line(self._vote(votes, 'trend'))}",
                f"  Structure:    {self._vote_line(self._vote(votes, 'structure'))}",
                f"  Sentiment:    {self._vote_line(self._vote(votes, 'sentiment'))}",
                f"  Derivatives:  {self._vote_line(self._vote(votes, 'derivatives'))}",
                f"  Decision:     {decision.direction.upper()} (score {decision.score:+.2f}, conf {decision.confidence:.2f})",
                risk_line,
                f"  Reason:       {decision.explanation}",
            ]
        )

        return {
            "direction": decision.direction,
            "score": decision.score,
            "confidence": decision.confidence,
            "entry": round(entry, 2),
            "stop_loss": round(sl_long or sl_short or 0.0, 2) if decision.direction != "neutral" else None,
            "target": round(target_long or target_short or 0.0, 2) if decision.direction != "neutral" else None,
            "accepted_size": accepted_size,
            "risk_allowed": risk_allowed,
            "risk_reason": risk_reason,
            "strategy_id": STRATEGY_A_ID,
            "strategy_version": STRATEGY_A_VERSION,
            "votes": [v.model_dump() for v in votes],
            "explanation": explanation,
        }

    def risk_sl_multiple(self) -> float:
        if self.risk_manager is not None:
            return self.risk_manager.settings.sl_atr_multiple
        return float(STRATEGY_A_PARAMS["sl_atr"])

    def risk_target_multiple(self) -> float:
        if self.risk_manager is not None:
            return self.risk_manager.settings.target_atr_multiple
        return float(STRATEGY_A_PARAMS["target_atr"])

    def risk_equity(self) -> float:
        return self.risk_manager.current_equity

    @staticmethod
    def _vote(votes: list[Vote], name: str) -> Vote:
        return next(v for v in votes if v.agent == name)

    @staticmethod
    def _vote_line(v: Vote) -> str:
        return f"{v.direction.upper()} (score {v.score:+.2f}): {v.explanation}"