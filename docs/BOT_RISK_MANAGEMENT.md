# Risk Management

## Design principle

Risk decisions are deterministic and fail-closed. A strategy agent can recommend a direction; only the risk manager can produce an approved risk decision. The execution guard and adapter revalidate the approval before an order is accepted.

## Position sizing

`app/risk/position_sizer.py` uses fixed-fractional sizing. It calculates the cash amount at risk as equity multiplied by risk percentage, divides by stop-distance loss per contract, and caps notional exposure at equity times maximum leverage. It returns quantity, notional, margin, leverage, contract value and an explanation. Invalid equity, price, contract value or zero stop distance returns no size.

Formula:

`risk_amount = equity × risk_pct / 100`

`loss_per_contract = abs(entry - stop) × contract_value`

`contracts = risk_amount / loss_per_contract`

The leverage cap may reduce the raw risk-based quantity.

## Configurable risk settings

The `RiskSettings` model declares:
- risk per trade percentage;
- maximum daily loss and peak-to-equity drawdown;
- maximum leverage;
- maximum open positions and daily trade count;
- maximum order, gross and per-symbol exposure percentages;
- maximum spread and slippage basis points;
- allowed UTC sessions;
- requirement for streaming data and maximum candle age;
- ATR stop/target multiples;
- cooldown bars;
- minimum confidence and minimum equity;
- volatility skip threshold.

The defaults in `app/risk/config.py` are configuration defaults, not a claim that every deployment has identical effective values. The active TradingProfile binds the risk configuration to the selected mode and strategy.

## Risk lifecycle and state

RiskManager tracks current/peak equity, realized and daily P&L, open positions, trades today, last trade bar, halt status and persistence errors. It can restore and persist state through the SQLAlchemy session factory. Research mode uses an independent non-persisting copy and is prevented from writing trading state.

## Pre-trade execution guard

`TradeGuard`:
1. validates the signal against the immutable TradingProfile;
2. rejects neutral/no-trade signals;
3. rejects if the persistent kill switch is active;
4. asks RiskManager for a structured decision and size;
5. checks portfolio position count and notional limits;
6. checks that signal accepted size equals the risk-approved quantity;
7. rejects any caller-requested size that differs from approved size.

The execution adapter repeats request/decision consistency checks, including mode, approved status, quantity, signal key, symbol, timeframe, strategy version, expected price and protective levels.

## Kill switch

The kill switch is separate from risk halts and persists its state. A human/operator can halt new entries with a reason. A failed state read fails closed. Resume requires an explicit confirmation in the CLI. The switch blocks new entries; it does not automatically flatten an existing paper position. The runner continues managing an existing paper position.

CLI examples:

```bash
python -m app.risk.kill_switch --status
python -m app.risk.kill_switch --halt --reason "operator halt"
python -m app.risk.kill_switch --resume --confirm
```

HTTP halt/resume endpoints require `X-Admin-Token`.

## Operational interpretation

- A signal may be long/short while risk rejects it.
- Missing risk approval or missing size must never be interpreted as permission.
- Do not restart the process to clear a halt or bypass a loss limit.
- Review the risk state, paper account and audit records before resuming.
- Risk defaults are not a substitute for a documented deployment-specific risk policy.

## Important boundary

This risk subsystem is part of a paper/testnet development platform. Its presence does not make the system approved for live trading. The live execution mode is disabled in code and the separate live-readiness review is NOT APPROVED.
