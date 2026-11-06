from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Delta regions -> (testnet REST, production REST)
TESTNET_BASE = {
    "global": "https://testnet-api.delta.exchange",
    "india": "https://cdn-ind.testnet.deltaex.org",
}
PROD_BASE = {
    "global": "https://api.delta.exchange",
    "india": "https://api.india.delta.exchange",
}

# Public WebSocket endpoints per region.  The legacy ``socket.*`` hosts are
# private/legacy transports; market-data consumers must use the public socket.
TESTNET_WS = {
    "global": "wss://socket-ind-pub.testnet.deltaex.org",
    "india": "wss://socket-ind-pub.testnet.deltaex.org",
}
PROD_WS = {
    "global": "wss://public-socket.india.delta.exchange",
    "india": "wss://public-socket.india.delta.exchange",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "BTC AI Trading"
    log_level: str = "INFO"
    # One of dev/test/paper/staging/live. Only dev/test skip fail-closed
    # startup validation; staging/live refuse to boot with unsafe settings.
    environment: str = "dev"
    log_file: str = ""
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5

    # Backend bind target (uvicorn). Loopback by default; a VPS/reverse-proxy
    # deployment must opt in explicitly with BACKEND_HOST.
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    # Required for watchlist mutations and kill-switch control via HTTP.
    # Empty disables those endpoints (they fail closed).
    admin_token: str = ""
    # Allow a non-loopback bind in staging/live (only behind a TLS proxy).
    allow_public_bind: bool = False
    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # In-memory per-client request budget and body cap (fail closed with 429/413).
    rate_limit_per_minute: int = 600
    max_request_bytes: int = 1_000_000
    trust_proxy_headers: bool = False

    delta_region: str = "global"
    delta_api_key: str = ""
    delta_api_secret: str = ""

    trading_symbol: str = "BTCUSDT"
    trading_timeframe: str = "1h"
    history_limit: int = 500

    # Optional explicit override for PUBLIC DATA reads only (e.g. historical
    # backfill from production while the account/order path stays on testnet).
    delta_data_url_override: str = ""

    # ---- execution safety ------------------------------------------------
    # Safety model:
    #   * LIVE_MODE=off                       -> real orders are impossible.
    #   * TESTNET_MODE=on                     -> every execution endpoint is
    #                                            resolved to the testnet host.
    #   * REAL_ORDER_CONFIRMATION=false       -> production orders additionally
    #                                            require this flag in settings
    #                                            AND an explicit per-call
    #                                            confirm_real_orders=True.
    live_mode: bool = False
    testnet_mode: bool = True
    real_order_confirmation: bool = False

    # ---- execution validation loop ---------------------------------------
    delta_execution_poll_secs: float = 2.0
    delta_execution_max_polls: int = 30
    delta_execution_state_file: str = "logs/testnet_state.json"
    delta_execution_journal: str = "logs/testnet_executions.jsonl"
    delta_client_id_prefix: str = "btcai-tn"
    delta_execution_native_sltp: bool = False  # when False, SL/TP are simulated locally

    database_url: str = "sqlite:///./data/trading.db"
    # Research datasets/experiments are isolated from account, order, position
    # and risk tables. Never point this at the trading database.
    research_database_url: str = "sqlite:///./data/research.db"

    llm_provider: str = "none"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_seconds: float = 30.0
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    llm_max_requests_per_day: int = 100
    llm_max_cost_usd_per_day: float = 5.0
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    # Optional local/self-hosted HTTPS endpoint. If set, it is the only
    # network destination used by the LLM layer (no vendor fallback).
    llm_allow_network: bool = False

    news_api_key: str = ""

    # ---- paper trading account ---------------------------------------------
    # The paper account is SIMULATED money, kept separate from the dashboard's
    # Trading Fund selector. Defaults mirror the fund presets (INR).
    paper_fund_inr: float = 5000.0  # default paper starting balance (₹)
    paper_risk_pct: float = 0.5  # risk per trade % applied by the risk engine
    paper_fee_pct: float = 0.05  # taker fee per side, % of notional
    paper_slippage_pct: float = 0.02  # adverse slippage per fill, % of price
    paper_contract_value: float = 0.001  # BTC per contract (Delta BTCUSDT)
    paper_symbol: str = "BTCUSDT"

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, value: str) -> str:
        normalized = str(value or "dev").strip().lower()
        allowed = {"dev", "test", "paper", "staging", "live"}
        if normalized not in allowed:
            raise ValueError(f"ENVIRONMENT must be one of {sorted(allowed)}")
        return normalized

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def production_profile(self) -> bool:
        return self.environment in {"paper", "staging", "live"}

    def startup_safety_problems(self) -> list[str]:
        """Return every reason this configuration must not serve traffic.

        The list is intentionally exhaustive so startup logs identify all
        problems at once. dev/test are allowed to run with an empty admin token
        because mutations still fail closed; staging/live are not.
        """
        problems: list[str] = []
        if self.live_mode:
            problems.append("LIVE_MODE=true: live execution is not authorized in this release")
        if self.real_order_confirmation:
            problems.append("REAL_ORDER_CONFIRMATION=true: real orders are disabled")
        if self.research_database_url.strip() == self.database_url.strip():
            problems.append("RESEARCH_DATABASE_URL must be separate from DATABASE_URL")
        if not self.testnet_mode:
            problems.append("TESTNET_MODE=false: the order path may resolve to production")
        loopback = {"127.0.0.1", "localhost", "::1"}
        if self.backend_host not in loopback and not self.allow_public_bind:
            problems.append("non-loopback BACKEND_HOST requires ALLOW_PUBLIC_BIND=true behind TLS")
        if self.production_profile:
            if not self.admin_token:
                problems.append("ADMIN_TOKEN is required in paper/staging/live")
            elif len(self.admin_token) < 24:
                problems.append("ADMIN_TOKEN must be at least 24 characters")
            if "*" in self.cors_origins_list:
                problems.append("CORS_ORIGINS must not contain '*' in paper/staging/live")
            if not self.cors_origins_list:
                problems.append("CORS_ORIGINS must name at least one dashboard origin")
        if self.rate_limit_per_minute <= 0:
            problems.append("RATE_LIMIT_PER_MINUTE must be positive")
        if self.max_request_bytes <= 0:
            problems.append("MAX_REQUEST_BYTES must be positive")
        return problems

    def validate_startup(self) -> None:
        """Raise when a production profile (or live mode) is unsafe to boot."""
        problems = self.startup_safety_problems()
        if not problems:
            return
        if self.live_mode or self.real_order_confirmation or self.production_profile:
            raise RuntimeError("unsafe startup configuration: " + "; ".join(problems))

    @property
    def _region(self) -> str:
        return self.delta_region if self.delta_region in TESTNET_BASE else "global"

    @property
    def delta_base_url(self) -> str:
        """REST base URL used by the ORDER path. Strictly testnet while
        TESTNET_MODE is on, production only when it is off (and live is on)."""
        if self.testnet_mode:
            return TESTNET_BASE[self._region]
        return PROD_BASE[self._region]

    @property
    def delta_market_data_url(self) -> str:
        """REST URL for public market/history data (never account/order data,
        so it cannot move funds).

        In testnet mode this defaults to the testnet host so that no production
        endpoint is touched at all while TESTNET_MODE=true. Operators who need
        production candle history for offline backfills can opt in explicitly via
        DELTA_DATA_URL_OVERRIDE; order placement is unaffected by that override.
        """
        if self.delta_data_url_override:
            return self.delta_data_url_override
        if self.testnet_mode:
            return TESTNET_BASE[self._region]
        return PROD_BASE[self._region]

    @property
    def delta_market_data_fallback_urls(self) -> tuple[str, ...]:
        """Read-only public-data mirrors tried when the primary market-data host
        fails (5xx / transport error) — e.g. Delta's global *testnet* REST host
        intermittently 502s on the candles and products routes while the public
        production mirror serves real candle history.

        This is strictly for public GET reads (products/tickers/candles). The
        order/account path never consults these URLs, so a fallback can never
        move funds. An explicit DELTA_DATA_URL_OVERRIDE is treated as fully
        authoritative and disables fallback.
        """
        if self.delta_data_url_override:
            return ()
        if self.testnet_mode:
            return (PROD_BASE["global"],)
        return ()

    @property
    def delta_ws_url(self) -> str:
        if self.testnet_mode:
            return TESTNET_WS[self._region]
        return PROD_WS[self._region]

    @property
    def execution_allowed(self) -> bool:
        """Orders may be placed against the contained venue only in testnet mode."""
        return not self.live_mode and self.testnet_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()
