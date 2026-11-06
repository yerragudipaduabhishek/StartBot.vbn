from pydantic import BaseModel, Field


class RiskSettings(BaseModel):
    """Risk parameters. Every entry decision is fail-closed."""

    risk_per_trade_pct: float = Field(default=1.0, description="% of equity risked on the stop per trade")
    max_daily_loss_pct: float = Field(default=5.0, description="% of starting equity loss allowed per day")
    max_drawdown_pct: float = Field(default=20.0, description="% drawdown from peak equity that halts trading")
    max_leverage: float = Field(default=5.0)
    max_positions: int = Field(default=1)
    max_daily_trades: int = Field(default=10)
    max_order_notional_pct: float = Field(default=500.0, description="max order notional as % of equity")
    max_gross_exposure_pct: float = Field(default=500.0, description="max gross exposure as % of equity")
    max_symbol_exposure_pct: float = Field(default=500.0, description="max single-symbol exposure as % of equity")
    max_spread_bps: float = Field(default=10.0)
    max_slippage_bps: float = Field(default=20.0)
    allowed_sessions: tuple[str, ...] = Field(default=("00:00-23:59",), description="UTC sessions, HH:MM-HH:MM")
    require_streaming_data: bool = True
    max_candle_age_secs: int = Field(default=7200)
    sl_atr_multiple: float = Field(default=1.5)
    target_atr_multiple: float = Field(default=2.0)
    cooldown_bars: int = Field(default=5, description="bars to wait between trades")
    min_confidence: float = Field(default=0.4)
    min_equity: float = Field(default=100.0)
    volatility_skip_pct: float = Field(default=3.0, description="skip if ATR/price above this % (too choppy)")