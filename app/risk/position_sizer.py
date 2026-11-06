"""Fixed-fractional position sizing: risk a fixed % of equity per trade."""

from app.contracts import PositionSize

__all__ = ["PositionSize", "size_position"]


def size_position(
    equity: float,
    entry: float,
    stop: float,
    risk_pct: float,
    contract_value: float = 0.001,
    max_leverage: float = 5.0,
    *,
    symbol: str | None = None,
    side: str | None = None,
) -> PositionSize | None:
    """Compute position size (contracts) such that hitting the stop loses `risk_pct` of equity."""
    if equity <= 0 or entry <= 0 or contract_value <= 0:
        return None
    if entry == stop:
        return None

    risk_amount = equity * risk_pct / 100.0
    stop_distance = abs(entry - stop)
    loss_per_contract = stop_distance * contract_value
    if loss_per_contract <= 0:
        return None

    contracts = risk_amount / loss_per_contract

    notional = contracts * contract_value * entry
    if notional > equity * max_leverage:
        contracts = (equity * max_leverage) / (contract_value * entry)
        notional = contracts * contract_value * entry

    margin = notional / max_leverage
    explanation = (
        f"risk ${risk_amount:,.2f} (equity ${equity:,.2f}) on stop distance ${stop_distance:.2f} "
        f"-> {contracts:.4f} contracts (~${notional:,.2f} notional, lev capped {max_leverage:.0f}x)"
    )
    return PositionSize(
        quantities=contracts,
        notional_usd=notional,
        margin_usd=margin,
        leverage=max_leverage,
        explanation=explanation,
        symbol=symbol,
        side=side,
        contract_value=contract_value,
    )
