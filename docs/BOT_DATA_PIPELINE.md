# Market Data and Feature Pipeline

## Sources and transport

The primary market integration is Delta Exchange v2. Public market data uses REST and public WebSocket endpoints configured by region in `app/config/settings.py`. Public market-data reads do not require API credentials. Authenticated endpoints require configured credentials and are used only by the contained testnet execution path in this release.

### DeltaClient

`app/data/delta_client.py` provides HTTP requests, Delta HMAC-SHA256 request signing, timeout/retry handling, response metadata, a TTL cache, token-bucket rate limiter and circuit breaker. Read fallback URLs are explicitly for public GET endpoints such as products, tickers and candles; order requests do not consult read fallback URLs.

### DeltaWebSocket

`app/data/websocket.py` normalizes current compact and legacy ticker/funding message formats into a common shape. It reconnects, resubscribes to configured channels, tracks received/processed/data messages, message types, handler errors, reconnects and last-message ages.

### MarketData

`app/data/market_data.py` combines the WebSocket ticker/funding feed with REST candle polling. It tracks ticker source, timestamps, candle buffer, poll failures, fallback use and data status. Freshness is timeframe-aware: a closed 1-hour candle can be legitimately older than a fixed five-minute freshness window.

## Candle quality

The feature pipeline requires time/open/high/low/close/volume. It rejects missing or non-numeric values, non-positive OHLC prices, negative volume, impossible high/low relationships, duplicate timestamps and non-ascending time. Separate data-quality/dataset modules retain candle source, venue, transport, fallback and quality flags.

## Feature pipeline

`app/features/pipeline.py` is the shared causal feature path for live signals, backtests and soak. Version: `phase2.v2`. It computes:
- EMA fast/slow/mid;
- RSI;
- MACD line/signal/histogram;
- ATR;
- ADX, +DI and -DI;
- rolling VWAP;
- SMA 20/50;
- Bollinger mid/upper/lower/width/position;
- volume SMA/ratio/z-score;
- realized volatility, volatility percentage and ATR percentage;
- trend and volatility regime labels.

The feature transformer uses only current/past rows. `prefix()` explicitly computes features through a selected row index. Feature pipeline version and EMA spans are attached to DataFrame attributes/output.

## Derivatives and news

Funding and open-interest data are normalized into derivatives features for the derivatives agent. The news module supplies headline strings to the sentiment agent; errors are handled as no-news input, which results in neutral sentiment rather than fabricated sentiment.

## Operational checks

Use:
- `GET /health` for readiness and data status;
- `GET /ticker` for current market snapshot;
- `GET /monitor` for multi-symbol health;
- WebSocket status fields for connection, message age and reconnects;
- data source/fallback metadata to identify provenance.

If data is stale, invalid, incomplete or from an unintended fallback source, do not override the quality gate. Investigate transport, REST poll errors, last candle age, source and quality flags before resuming paper activity.
