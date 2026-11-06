"""Provider-agnostic LLM access for read-only research agents.

No agent, provider, or orchestrator may import execution, broker, order or risk
modules. Network adapters are opt-in and every call is budgeted; tests use the
offline stub and never touch the network.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class LLMBudgetExceeded(LLMError):
    pass


class LLMProviderError(LLMError):
    pass


class LLMTimeoutError(LLMProviderError):
    pass


@dataclass(frozen=True)
class LLMRequest:
    prompt: str
    system: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 1024
    response_format: str = "json"


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str = ""
    provider: str = ""
    request_id: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class LLMProvider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse: ...


class LLMBudget:
    def __init__(
        self,
        *,
        max_requests: int | None = None,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.max_tokens = max_tokens
        self.max_cost_usd = max_cost_usd
        self.requests = 0
        self.tokens = 0
        self.cost_usd = 0.0

    def check(self) -> None:
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise LLMBudgetExceeded("LLM request budget exhausted")
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise LLMBudgetExceeded("LLM token budget exhausted")
        if self.max_cost_usd is not None and self.cost_usd >= self.max_cost_usd:
            raise LLMBudgetExceeded("LLM cost budget exhausted")

    def add(self, response: LLMResponse) -> None:
        self.requests += 1
        self.tokens += response.usage.total_tokens
        self.cost_usd += float(response.cost_usd or 0.0)


class LLMClient:
    """Budgeted provider wrapper with retry and secret-safe logging."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        budget: LLMBudget | None = None,
        retries: int = 1,
        retry_delay: float = 0.0,
    ) -> None:
        self.provider = provider
        self.budget = budget or LLMBudget()
        self.retries = max(0, int(retries))
        self.retry_delay = max(0.0, float(retry_delay))

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.budget.check()
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            try:
                response = self.provider.complete(request)
            except Exception as exc:
                last = exc
                if attempt < self.retries:
                    if self.retry_delay:
                        time.sleep(self.retry_delay)
                    continue
                logger.warning(
                    "llm call failed provider=%s model=%s attempts=%d error=%s",
                    getattr(self.provider, "name", "unknown"),
                    request.model,
                    attempt + 1,
                    type(exc).__name__,
                )
                raise LLMProviderError(f"LLM provider failed: {exc}") from exc
            latency_ms = (time.monotonic() - started) * 1000.0
            response = LLMResponse(
                text=response.text,
                model=response.model or request.model,
                provider=response.provider or getattr(self.provider, "name", ""),
                request_id=response.request_id or uuid.uuid4().hex,
                usage=response.usage,
                cost_usd=response.cost_usd,
                latency_ms=latency_ms,
            )
            self.budget.add(response)
            logger.info(
                "llm call provider=%s model=%s tokens=%d cost_usd=%.6f latency_ms=%.1f request_id=%s",
                response.provider,
                response.model,
                response.usage.total_tokens,
                response.cost_usd,
                response.latency_ms,
                response.request_id,
            )
            return response
        raise LLMProviderError(f"LLM provider failed: {last}")


class StubProvider:
    """Deterministic offline provider for tests and local dry-runs."""

    name = "stub"

    def __init__(self, responses: list[str] | dict[str, str] | Callable[[LLMRequest], str] | str = "{}") -> None:
        if isinstance(responses, (list, tuple)):
            self._responses = list(responses)
        elif isinstance(responses, dict):
            self._responses = dict(responses)
        elif callable(responses):
            self._responses = responses
        else:
            self._responses = str(responses)
        self.calls: list[LLMRequest] = []
        self._counter = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if callable(self._responses):
            text = str(self._responses(request))
        elif isinstance(self._responses, list):
            if not self._responses:
                raise LLMProviderError("stub response list is empty")
            text = self._responses[min(self._counter, len(self._responses) - 1)]
        elif isinstance(self._responses, dict):
            text = self._responses.get(request.prompt, "{}")
        else:
            text = self._responses
        self._counter += 1
        return LLMResponse(
            text=text,
            model=request.model or "stub-model",
            provider=self.name,
            request_id=f"stub-{self._counter}",
            usage=LLMUsage(prompt_tokens=max(1, len(request.prompt) // 4), completion_tokens=max(1, len(text) // 4)),
        )


def _post_json(url: str, payload: dict, headers: dict[str, str], *, timeout: float) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except (TimeoutError, urllib.error.URLError) as exc:
        if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
            raise LLMTimeoutError(str(exc)) from exc
        raise LLMProviderError(str(exc)) from exc
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise LLMProviderError("provider returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMProviderError("provider returned a non-object response")
    return parsed


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, api_key: str | None = None, base_url: str = "https://api.openai.com/v1", timeout: float = 30.0) -> None:
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMProviderError("OPENAI_API_KEY is not configured")
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": request.model or "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if request.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        data = _post_json(
            f"{self.base_url}/chat/completions",
            payload,
            {"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError("provider response has no choices")
        text = str((choices[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=str(data.get("model") or request.model),
            provider=self.name,
            request_id=str(data.get("id") or ""),
            usage=LLMUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
            ),
        )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, base_url: str = "https://api.anthropic.com/v1", timeout: float = 30.0) -> None:
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMProviderError("ANTHROPIC_API_KEY is not configured")
        self.api_key = key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        data = _post_json(
            f"{self.base_url}/messages",
            {
                "model": request.model or "claude-3-5-haiku-latest",
                "system": request.system,
                "messages": [{"role": "user", "content": request.prompt}],
                "temperature": request.temperature,
                "max_tokens": max(1, request.max_tokens),
            },
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            timeout=self.timeout,
        )
        content = data.get("content") or []
        text = "".join(str(block.get("text") or "") for block in content if isinstance(block, dict))
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            model=str(data.get("model") or request.model),
            provider=self.name,
            request_id=str(data.get("id") or ""),
            usage=LLMUsage(
                prompt_tokens=int(usage.get("input_tokens") or 0),
                completion_tokens=int(usage.get("output_tokens") or 0),
            ),
        )


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, base_url: str = "http://127.0.0.1:11434", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def complete(self, request: LLMRequest) -> LLMResponse:
        data = _post_json(
            f"{self.base_url}/api/chat",
            {
                "model": request.model or "llama3.2",
                "stream": False,
                "options": {"temperature": request.temperature},
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.prompt},
                ],
            },
            {},
            timeout=self.timeout,
        )
        message = data.get("message") or {}
        return LLMResponse(
            text=str(message.get("content") or ""),
            model=str(data.get("model") or request.model),
            provider=self.name,
            request_id=str(data.get("created_at") or ""),
            usage=LLMUsage(
                prompt_tokens=int(data.get("prompt_eval_count") or 0),
                completion_tokens=int(data.get("eval_count") or 0),
            ),
        )


def provider_from_settings(settings: Any) -> LLMProvider:
    """Build the configured provider without ever defaulting to network access.

    Network providers are opt-in via ``LLM_ALLOW_NETWORK=true``. With the safe
    default the deterministic stub is used, so tests and paper deployments never
    make an outbound LLM call by accident.
    """
    name = str(getattr(settings, "llm_provider", "none") or "none").strip().lower()
    allow_network = bool(getattr(settings, "llm_allow_network", False))
    if name in {"none", "offline", "stub"}:
        return StubProvider()
    if not allow_network:
        logger.warning("llm provider %s requested but LLM_ALLOW_NETWORK is disabled; using stub", name)
        return StubProvider()
    timeout = float(getattr(settings, "llm_timeout_seconds", 30.0) or 30.0)
    base_url = str(getattr(settings, "llm_base_url", "") or "").strip()
    if name == "openai":
        kwargs: dict[str, Any] = {"api_key": getattr(settings, "openai_api_key", None), "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIProvider(**kwargs)
    if name == "anthropic":
        kwargs = {"api_key": getattr(settings, "anthropic_api_key", None), "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        return AnthropicProvider(**kwargs)
    if name in {"ollama", "local"}:
        return OllamaProvider(base_url=base_url or "http://127.0.0.1:11434", timeout=timeout)
    raise LLMProviderError(f"unknown LLM provider: {name}")


def client_from_settings(settings: Any, provider: LLMProvider | None = None) -> LLMClient:
    provider = provider or provider_from_settings(settings)
    max_requests = getattr(settings, "llm_max_requests_per_day", None)
    max_cost = getattr(settings, "llm_max_cost_usd_per_day", None)
    budget = LLMBudget(
        max_requests=int(max_requests) if max_requests is not None else None,
        max_cost_usd=float(max_cost) if max_cost is not None else None,
    )
    return LLMClient(provider, budget=budget)


__all__ = [
    "AnthropicProvider",
    "LLMBudget",
    "LLMBudgetExceeded",
    "LLMClient",
    "LLMError",
    "LLMProvider",
    "LLMProviderError",
    "LLMRequest",
    "LLMResponse",
    "LLMTimeoutError",
    "LLMUsage",
    "OllamaProvider",
    "OpenAIProvider",
    "StubProvider",
    "client_from_settings",
    "provider_from_settings",
]
