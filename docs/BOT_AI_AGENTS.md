# AI and Strategy Agents

This repository has two distinct agent families. The trading signal agents are deterministic feature-based components. The research agents are optional LLM-assisted, read-only analysts. They must not be conflated.

## 1. Trading signal agents

All signal agents return a Pydantic `Vote` with:
- `agent`: agent identifier
- `direction`: long, short or neutral
- `score`: signed value in [-1, 1]
- `confidence`: value in [0, 1]
- `explanation`: human-readable reasoning
- `details`: supporting fields

### TrendAgent

Uses EMA alignment, directional movement (+DI/-DI), price versus rolling VWAP and MACD histogram. It clamps the composite score and derives confidence from ADX. This is the primary contributor in Strategy A's configured weights.

### StructureAgent

Finds swing highs/lows from rolling pivots, compares pivot progression and labels higher highs/higher lows, lower highs/lower lows or mixed structure. It can use the last 30-bar range position as a mild lean when pivots do not establish direction.

### SentimentAgent

Uses an injectable `SentimentProvider`. `VaderSentiment` analyzes supplied headlines using VADER compound scores. `NeutralSentiment` returns neutral score and zero confidence. The signal engine attempts VADER and falls back to neutral if initialization fails. The presence of the agent does not mean a live news feed is configured or that headlines are always available.

### DerivativesAgent

Reads normalized ticker fields for funding and open interest. Funding is contrarian at extremes; open-interest change indicates positioning changes. It combines the two feature scores evenly. Missing derivatives data yields a neutral, zero-confidence vote.

### DecisionAgent

Calls `weighted_score`, calculates the normalized score/confidence and maps the score to long, short or neutral using the configured threshold. It does not place orders and is not a risk authority.

## 2. Research agents

`app/research/agents.py` defines strict output models for:
- Market analysis: summary, regime, data quality, observations and risks.
- Strategy research: hypotheses, parameter changes, expected effect and risks.
- Code review/proposals: patch descriptions, test plans and risk notes.
- Review analysis: approval/veto findings and required changes.
- Performance analysis: metrics assessment, diagnostics and recommendations.

Research agents use a shared LLM client. Their inputs are canonicalized and hashed; output schemas reject extra fields. The system prompt and agent contract treat headlines, URLs, numbers and tool results as untrusted data rather than instructions. A code proposal is never self-applying: `can_apply=true` is rejected by schema validation.

## 3. LLM client

`app/research/llm.py` provides a provider protocol, request/response records, a budget tracker and a retrying client. The budget can cap request count, tokens and USD cost. A deterministic stub provider supports offline tests and dry runs. Network access is opt-in through settings; provider failures are surfaced as typed errors and logs record provider/model, token count, cost, latency and request ID rather than request secrets.

Settings expose provider/model/base URL, timeout, temperature, max tokens, per-day request and cost limits, API key fields, and `LLM_ALLOW_NETWORK`. The default provider is `none`; no LLM is required for the normal trading signal path.

## 4. Governance boundary

Research outputs are proposals, not instructions to the trading system. Proposal lifecycle includes dataset identity, review, human approval, canary evidence, promotion and rollback. API mutations require admin authentication. Governance changes a paper strategy pin only; it does not enable live execution.

## 5. Explicit non-capabilities

- Research LLM agents cannot submit orders.
- They cannot edit risk limits or execution configuration.
- They cannot auto-apply code patches.
- They cannot approve their own promotion.
- They cannot override live-mode guards.
- External news or model output must never be treated as trusted control input.
