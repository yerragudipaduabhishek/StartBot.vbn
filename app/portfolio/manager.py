"""Small portfolio constraint layer used before order creation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortfolioLimits:
    max_positions: int = 1
    max_gross_notional: float = 25_000.0
    max_symbol_notional: dict[str, float] = field(default_factory=dict)


class PortfolioManager:
    def __init__(self, limits: PortfolioLimits | None = None) -> None:
        self.limits = limits or PortfolioLimits()

    def approve(self, *, symbol: str, notional: float, open_positions: int) -> tuple[bool, str]:
        if open_positions >= self.limits.max_positions:
            return False, "maximum open positions reached"
        if notional <= 0:
            return False, "notional must be positive"
        if notional > self.limits.max_gross_notional:
            return False, "gross notional limit exceeded"
        symbol_limit = self.limits.max_symbol_notional.get(symbol)
        if symbol_limit is not None and notional > symbol_limit:
            return False, "symbol notional limit exceeded"
        return True, "approved"
