# Trading Logic and Strategy A

## Strategy identity

The strategy constants in `app/strategy/strategy_a.py` define Strategy A v1.0.0:

- Symbol: BTCUSDT
- Timeframe: 1h
- EMA spans: 8, 40 and 50
- Direction threshold: 0.25
- Strategy weights: trend 0.40, structure 0.00, sentiment 0.15, derivatives 0.15
- ATR stop multiple: 1.0
- ATR target multiple: 2.0
- Contract value: 0.001

The strategy profile is immutable. Strategy A rejects another symbol or timeframe. The README describes this 1H trend-regime strategy as the selected validated candidate; lower timeframe 5m/15m ensembles are reported as not showing measurable edge.

## Processing sequence

1. Collect OHLCV candles and a ticker snapshot.
2. Require sufficient candle history (the signal engine requires at least 30 candles).
3. Transform candles using the shared causal FeaturePipeline.
4. Run four directional agents.
5. Combine votes using normalized weighted scoring.
6. Compare the aggregate score to the configured threshold.
7. Calculate entry reference, ATR stop and target.
8. Ask RiskManager to approve and size the candidate.
9. Return an explained signal record; downstream validation/guarding is still required before execution.

## Weighted decision

Each agent emits a signed score in [-1, 1] and confidence in [0, 1]. `weighted_score` calculates the weighted average score and weighted confidence over the supplied votes, using non-negative weights. If the aggregate score is at or above +threshold the decision is long; at or below -threshold it is short; otherwise it is neutral.

Strategy A sets structure weight to zero. The structure agent still produces an explainable vote, but contributes no score under that profile. The sentiment and derivatives weights are present in the profile, but their information quality depends on available headlines/ticker fields.

## Trend inputs

The trend agent considers:
- EMA fast/slow/mid alignment: +0.5 for bullish alignment, -0.5 for bearish alignment.
- +DI versus -DI: +0.2 or -0.2.
- Close versus rolling VWAP: +0.15 or -0.15.
- MACD histogram sign: +0.15 or -0.15.

The combined score is clamped to [-1, 1]. Confidence is derived from ADX using the implemented mapping `(ADX - 15) / 40`, bounded to [0, 1].

## Structure inputs

Swing pivots are detected using a centered rolling window (default width 3 on each side). Rising pivot highs and lows indicate higher-high/higher-low structure; falling highs and lows indicate lower-high/lower-low structure. Mixed configurations receive a smaller directional score. If structure is indeterminate and enough bars exist, the last 30-bar range position can add a mild lean.

## Sentiment inputs

The SentimentAgent accepts headline strings through a provider interface. The VADER provider averages compound sentiment scores and estimates confidence from the number of non-neutral headlines. NeutralSentiment returns zero score/confidence when no provider or headlines are available. The signal engine attempts to construct VADER and falls back to neutral if unavailable.

## Derivatives inputs

The DerivativesAgent blends funding and open-interest scores equally. Funding extremes are contrarian: materially negative funding leans bullish; materially positive funding leans bearish. Open-interest change is normalized against notional and categorized by thresholds for increases/decreases over six hours. Missing ticker data yields a neutral vote with zero confidence.

## Stops, targets and sizing

For a long signal, the engine places the stop below entry by the configured ATR multiple and target above entry by the target multiple. Short signals invert those directions. Strategy A constants specify 1 ATR stop and 2 ATR target. The risk manager then calculates approved quantity from equity, risk percentage, stop distance and contract value, subject to leverage and other configured limits.

## No-trade conditions

The engine returns no directional signal when history is insufficient or aggregate score is below threshold. Risk can independently reject a directional candidate due to stale/non-streaming data, low confidence, invalid prices, exposure, loss/drawdown limits, trade counts, cooldown, volatility or kill-switch state. Invalid or incomplete signals must fail validation; no downstream component should treat a directional score as approval.

## Research caution

A strategy constant named “validated” or a README summary does not establish guaranteed profitability. Review the actual dataset, cost assumptions, sample sizes, walk-forward artifacts and current code version before drawing conclusions.
