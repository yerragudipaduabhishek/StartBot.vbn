"""Serializable risk-state snapshot."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class RiskStateSnapshot(BaseModel):
    day: date
    day_pnl: float
    peak_equity: float
    realized_pnl: float
    open_positions: int
    trades_today: int
    last_trade_bar_index: int | None
    trading_halted: bool
    halt_reason: str
