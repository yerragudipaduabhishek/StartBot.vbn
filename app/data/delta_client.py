import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

import requests

from app.config.settings import Settings, get_settings
from app.data.cache import CircuitBreaker, TTLCache, TokenBucket, backoff_seconds, retry_after_seconds

ORDER_TYPE_MARKET = "market"
ORDER_TYPE_LIMIT = "limit_order"
ORDER_TYPE_STOP_MARKET = "stop_market"
ORDER_TYPE_STOP_LIMIT = "stop_limit_order"

# Normalized order states used across the execution layer.
ORDER_OPEN = "open"
ORDER_FILLED = "filled"
ORDER_CANCELLED = "cancelled"
ORDER_REJECTED = "rejected"
ORDER_FINAL_STATES = {ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED}


class DeltaClient:
    """Signed REST client for the Delta Exchange v2 API.

    Public market data requires no credentials. Authenticated endpoints
    (orders/positions/balances) need api_key + api_secret and are only used
    when keys are configured.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        settings: Settings | None = None,
        timeout: int = 15,
        retries: int = 3,
        read_fallback_urls: tuple[str, ...] = (),
        cache_ttl_seconds: float = 2.0,
        rate_limit_per_second: float = 10.0,
        rate_limit_burst: float | None = None,
        circuit_failure_threshold: int = 4,
        circuit_recovery_seconds: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.settings = settings or get_settings()
        self.timeout = timeout
        self.retries = retries
        # Read-only fallback mirrors are used by *public* GET endpoints only
        # (products / tickers / candles); the order path never consults them.
        self.read_fallback_urls = tuple(u.rstrip("/") for u in read_fallback_urls)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "btc-ai-trading/0.1", "Content-Type": "application/json"})
        self.last_read_meta: dict = {}
        self.cache = TTLCache(maxsize=512, default_ttl=cache_ttl_seconds)
        self.rate_limiter = TokenBucket(rate_limit_per_second, capacity=rate_limit_burst)
        self.circuit_breaker = CircuitBreaker(
            failure_threshold=circuit_failure_threshold,
            recovery_timeout=circuit_recovery_seconds,
        )

    # ------------------------------------------------------------------ auth

    def _sign(self, method: str, path: str, query: str, body: str, timestamp: str) -> str:
        """Delta signature = HMAC_SHA256(secret, method + timestamp + path + query + body).

        The `timestamp` used inside the signed message must be byte-identical to
        the `timestamp` header, otherwise Delta rejects the request.
        """
        message = method + timestamp + path + query + body
        return hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()

    def _headers(self, method: str, path: str, query: str, body: str) -> dict:
        ts = str(int(time.time()))
        return {
            "api-key": self.api_key,
            "timestamp": ts,
            "signature": self._sign(method, path, query, body, ts),
        }

    def _request(
        self,
        method: str,
        path: str,
        query: dict | None = None,
        body: dict | None = None,
        base_urls: tuple[str, ...] | None = None,
        meta: dict | None = None,
    ) -> dict:
        query_str = ""
        if query:
            import urllib.parse

            query_str = "?" + urllib.parse.urlencode(query)
        body_str = json.dumps(body) if body is not None else ""
        bases = (self.base_url,) if base_urls is None else tuple(base_urls)

        last_exc: Exception | None = None
        for base in bases:
            if not self.circuit_breaker.allow_request(base):
                last_exc = RuntimeError(f"circuit open for {urlparse(base).netloc or base}")
                continue
            url = base + path + query_str
            for attempt in range(self.retries):
                fail_fast = False
                retry_after: float | None = None
                try:
                    self.rate_limiter.acquire()
                    headers = self._headers(method, path, query_str, body_str) if self.api_key and self.api_secret else {}
                    r = self.session.request(method, url, headers=headers, data=body_str if body is not None else None, timeout=self.timeout)
                    if r.status_code >= 500:
                        # server-side errors won't recover within a retry window:
                        # record and move to the next host immediately.
                        self.circuit_breaker.record_failure(base, f"status {r.status_code}")
                        fail_fast = True
                        raise requests.RequestException(f"status {r.status_code}")
                    if r.status_code == 429:
                        retry_after = retry_after_seconds(getattr(r, "headers", {}).get("Retry-After"))
                        raise requests.RequestException("status 429")
                    if not r.content or r.headers.get("content-type", "").startswith("text/html"):
                        self.circuit_breaker.record_failure(base, "non-JSON response")
                        raise requests.RequestException(f"non-JSON response (status {r.status_code})")
                    parsed = r.json()
                    if not isinstance(parsed, dict):
                        # HTTP 200 with a non-object body (e.g. JSON null) is a
                        # broken/garbage response - surface it honestly instead
                        # of crashing downstream readers with an obscure error.
                        self.circuit_breaker.record_failure(base, "non-object JSON")
                        raise requests.RequestException(f"non-object JSON payload (type={type(parsed).__name__})")
                    self.circuit_breaker.record_success(base)
                    if meta is not None:
                        meta.update(
                            {
                                "base_url": base,
                                "status_code": r.status_code,
                                "fetched_at": int(time.time()),
                            }
                        )
                    return parsed
                except (requests.RequestException, OSError, ValueError) as e:
                    last_exc = e
                    if retry_after is None and not fail_fast:
                        self.circuit_breaker.record_failure(base, str(e))
                    if fail_fast or attempt + 1 >= self.retries:
                        break
                    delay = retry_after if retry_after is not None else backoff_seconds(attempt)
                    time.sleep(min(4.0, max(0.0, delay)))
        raise RuntimeError(f"Delta request failed: {method} {path} after {len(bases)} host(s) ({last_exc})")

    def _read(self, method: str, path: str, query: dict | None = None) -> dict:
        """Public read with TTL cache, rate limiting and fallback mirrors.

        Order/account methods deliberately bypass this path, so an execution
        decision can never consume a cached/stale read.
        """
        cache_key = (
            method,
            path,
            tuple(sorted((str(k), str(v)) for k, v in (query or {}).items())),
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            payload, cached_meta = cached
            self.last_read_meta = {**dict(cached_meta), "cache_hit": True}
            return payload

        meta: dict = {"source": "delta", "transport": "rest", "endpoint": path}
        try:
            payload = self._request(
                method,
                path,
                query=query,
                body=None,
                base_urls=(self.base_url, *self.read_fallback_urls),
                meta=meta,
            )
        finally:
            base = str(meta.get("base_url") or self.base_url)
            meta["fallback_used"] = base.rstrip("/") != self.base_url.rstrip("/")
            try:
                meta["venue"] = urlparse(base).netloc or base
            except ValueError:
                meta["venue"] = base
            meta["cache_hit"] = False
            self.last_read_meta = meta
        self.cache.set(cache_key, (payload, dict(meta)))
        return payload

    def resilience_status(self) -> dict:
        return {
            "cache": self.cache.stats(),
            "rate_limiter": self.rate_limiter.status(),
            "circuits": self.circuit_breaker.status(),
        }

    # ------------------------------------------------------------ public data

    def get_products(self) -> list[dict]:
        return self._read("GET", "/v2/products").get("result", [])

    def find_product(self, symbol: str, contract_type: str = "perpetual_futures") -> dict:
        for p in self.get_products():
            if p.get("symbol") == symbol and p.get("contract_type") == contract_type:
                return p
        raise ValueError(f"Product not found: {symbol} ({contract_type})")

    def get_ticker(self, symbol: str) -> dict:
        return self._read("GET", f"/v2/tickers/{symbol}").get("result", {})

    def get_candles(
        self,
        symbol: str,
        resolution: str = "5m",
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        params = {"symbol": symbol, "resolution": resolution}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if limit is not None:
            params["limit"] = limit
        candles = self._read("GET", "/v2/history/candles", query=params).get("result", [])
        # Delta returns candles newest-first; normalize to ascending time.
        seen: set[int] = set()
        ordered: list[dict] = []
        for c in sorted(candles, key=lambda x: x["time"]):
            if c["time"] in seen:
                continue
            seen.add(c["time"])
            ordered.append(c)
        return ordered

    def get_candles_with_metadata(
        self,
        symbol: str,
        resolution: str = "5m",
        start: int | None = None,
        end: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Candle read whose provenance is available in ``last_read_meta``."""
        return self.get_candles(symbol, resolution=resolution, start=start, end=end, limit=limit)

    # ----------------------------------------------------------- auth endpoints

    def _check_auth(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Delta API key/secret not configured")

    def get_balances(self) -> list[dict]:
        self._check_auth()
        return self._request("GET", "/v2/wallet/balances").get("result", [])

    def get_positions(self, product_id: int | None = None) -> list[dict]:
        self._check_auth()
        query: dict = {"states": "open"}
        if product_id:
            query["product_id"] = product_id
        return self._request("GET", "/v2/positions", query=query).get("result", [])

    def get_orders(self, product_id: int, state: str = "open") -> list[dict]:
        self._check_auth()
        return self._request("GET", "/v2/orders", query={"product_id": product_id, "state": state}).get("result", [])

    def get_order(self, order_id: int) -> dict:
        self._check_auth()
        return self._request("GET", f"/v2/orders/{order_id}").get("result", {})

    def get_order_trades(self, order_id: int) -> list[dict]:
        self._check_auth()
        return self._request("GET", f"/v2/orders/{order_id}/trades").get("result", [])

    def place_order(
        self,
        product_id: int,
        size: float,
        side: str,
        order_type: str = ORDER_TYPE_MARKET,
        price: float | None = None,
        stop_price: float | None = None,
        reduce_only: bool = False,
        close_on_trigger: bool = False,
        client_order_id: str | None = None,
        time_in_force: str = "gtc",
    ) -> dict:
        self._check_auth()
        body: dict = {
            "product_id": product_id,
            "size": size,
            "side": side,
            "order_type": order_type,
            "time_in_force": time_in_force,
        }
        if price is not None:
            body["limit_price"] = price
        if stop_price is not None:
            body["stop_price"] = stop_price
        if reduce_only:
            body["reduce_only"] = True
        if close_on_trigger:
            body["close_on_trigger"] = True
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request("POST", "/v2/orders", body=body)

    def cancel_order(self, order_id: int) -> dict:
        self._check_auth()
        return self._request("POST", "/v2/orders/cancel", body={"id": order_id})

    def cancel_orders(self, order_ids: list[int]) -> dict:
        self._check_auth()
        return self._request("POST", "/v2/orders/cancel", body={"ids": order_ids})