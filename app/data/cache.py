"""Small, explicit resilience primitives for market-data reads.

These classes are intentionally dependency-free and deterministic so they can
be unit-tested with fake clocks. They are used only around public market-data
reads; order/account paths bypass the cache and are never served stale data.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable


class TTLCache:
    """Thread-safe bounded TTL cache with simple observable statistics."""

    def __init__(
        self,
        *,
        maxsize: int = 512,
        default_ttl: float = 2.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        if default_ttl < 0:
            raise ValueError("default_ttl must be non-negative")
        self.maxsize = int(maxsize)
        self.default_ttl = float(default_ttl)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._data: dict[Any, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: Any) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            expiry, value = entry
            if self._clock() >= expiry:
                self._data.pop(key, None)
                self.evictions += 1
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key: Any, value: Any, ttl: float | None = None) -> None:
        lifetime = self.default_ttl if ttl is None else float(ttl)
        if lifetime <= 0:
            return
        with self._lock:
            if key not in self._data and len(self._data) >= self.maxsize:
                oldest = min(self._data.items(), key=lambda item: item[1][0])[0]
                self._data.pop(oldest, None)
                self.evictions += 1
            self._data[key] = (self._clock() + lifetime, value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._data),
                "maxsize": self.maxsize,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }


class TokenBucket:
    """Token-bucket limiter; ``acquire`` blocks only as long as necessary."""

    def __init__(
        self,
        rate_per_second: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self.rate = float(rate_per_second)
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_second))
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._tokens = self.capacity
        self._updated = self._clock()
        self._lock = threading.Lock()
        self.waited_seconds = 0.0

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill(self._clock())
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0) -> float:
        """Wait for tokens and return the seconds actually waited."""
        with self._lock:
            now = self._clock()
            self._refill(now)
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            wait = (tokens - self._tokens) / self.rate
            self._tokens = 0.0
            self._updated = now + wait
        self.waited_seconds += wait
        self._sleep(wait)
        return wait

    def status(self) -> dict[str, float]:
        with self._lock:
            self._refill(self._clock())
            return {
                "rate_per_second": self.rate,
                "capacity": self.capacity,
                "tokens": round(self._tokens, 3),
                "waited_seconds": round(self.waited_seconds, 3),
            }


@dataclass
class CircuitState:
    failures: int = 0
    opened_at: float | None = None
    half_open_calls: int = 0
    successes: int = 0
    last_error: str = ""


class CircuitBreaker:
    """Per-host circuit breaker with closed/open/half-open states."""

    def __init__(
        self,
        *,
        failure_threshold: int = 4,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_timeout = float(recovery_timeout)
        self.half_open_max_calls = max(1, int(half_open_max_calls))
        self._clock = clock or time.monotonic
        self._hosts: dict[str, CircuitState] = {}
        self._lock = threading.Lock()

    def _state(self, host: str) -> CircuitState:
        return self._hosts.setdefault(host, CircuitState())

    def state(self, host: str) -> str:
        with self._lock:
            state = self._state(host)
            if state.opened_at is None:
                return "closed"
            if self._clock() - state.opened_at >= self.recovery_timeout:
                return "half_open"
            return "open"

    def allow_request(self, host: str) -> bool:
        with self._lock:
            state = self._state(host)
            if state.opened_at is None:
                return True
            if self._clock() - state.opened_at < self.recovery_timeout:
                return False
            if state.half_open_calls >= self.half_open_max_calls:
                return False
            state.half_open_calls += 1
            return True

    def record_success(self, host: str) -> None:
        with self._lock:
            self._hosts[host] = CircuitState(successes=self._state(host).successes + 1)

    def record_failure(self, host: str, error: str = "") -> None:
        with self._lock:
            state = self._state(host)
            state.failures += 1
            state.last_error = str(error)
            if state.failures >= self.failure_threshold or state.opened_at is not None:
                state.opened_at = self._clock()
                state.half_open_calls = 0

    def status(self) -> dict[str, dict]:
        with self._lock:
            now = self._clock()
            out: dict[str, dict] = {}
            for host, state in self._hosts.items():
                if state.opened_at is None:
                    circuit = "closed"
                    remaining = 0.0
                elif now - state.opened_at >= self.recovery_timeout:
                    circuit = "half_open"
                    remaining = 0.0
                else:
                    circuit = "open"
                    remaining = max(0.0, self.recovery_timeout - (now - state.opened_at))
                health = 1.0 if state.opened_at is None else (0.5 if circuit == "half_open" else 0.0)
                out[host] = {
                    "state": circuit,
                    "failures": state.failures,
                    "successes": state.successes,
                    "recovery_remaining_secs": round(remaining, 1),
                    "health": health,
                    "last_error": state.last_error,
                }
            return out


def retry_after_seconds(value: object) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(0.0, seconds)


def backoff_seconds(
    attempt: int,
    *,
    base: float = 0.25,
    cap: float = 4.0,
    jitter: float = 0.1,
    rng: random.Random | None = None,
) -> float:
    delay = min(cap, base * (2 ** max(0, attempt)))
    spread = delay * max(0.0, jitter)
    generator = rng or random
    return max(0.0, delay + generator.uniform(-spread, spread))


__all__ = [
    "CircuitBreaker",
    "TTLCache",
    "TokenBucket",
    "backoff_seconds",
    "retry_after_seconds",
]
