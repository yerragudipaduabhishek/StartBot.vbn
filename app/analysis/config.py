"""Analysis-layer constants: default instrument watchlist and sizing defaults.

Watchlist symbols are real Delta Exchange perpetual-futures instruments. The
backend is read-only: add/remove persistence happens client-side (localStorage).
"""

DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT"]

DEFAULT_SYMBOL = "BTCUSDT"

# Paper/test fund presets (INR, as the user-facing selector shows).
FUND_PRESETS_INR = [1000, 2500, 5000, 10000, 25000]

DEFAULT_FUND_INR = 5000

# Risk per trade % applied to the selected paper/test fund. This is the
# dashboard's read-only sizing assumption, not a change to app/risk defaults.
DEFAULT_RISK_PCT = 0.5

# 1h candle window for a single analysis request (EMA mids + window margin).
ANALYSIS_CANDLE_LIMIT = 200

# Seconds per 1h candle (used by the REST candle start/end window).
HOUR_SECONDS = 3600