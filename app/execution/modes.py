"""Execution-mode policy.

Mode checks are deliberately centralised here so no runner, backtest or
adapter can invent its own definition of "paper" or "testnet".
"""

from __future__ import annotations

from app.config.settings import Settings, get_settings
from app.contracts import ExecutionMode


class ExecutionModeError(ValueError):
    """Raised when an action is not allowed in the selected mode."""


class LiveTradingDisabled(ExecutionModeError):
    """Live order submission is intentionally unavailable."""


class ResearchBoundaryError(ExecutionModeError):
    """Research/backtest code attempted to mutate trading state."""


# Hard-coded, non-configurable live-trading kill switch. Environment flags can
# never turn this on; changing it requires a code change and review.
LIVE_TRADING_ENABLED: bool = False

_ORDER_MODES = {ExecutionMode.PAPER, ExecutionMode.TESTNET}
_PERSISTED_MODES = {ExecutionMode.PAPER, ExecutionMode.TESTNET}
_RESEARCH_MODES = {ExecutionMode.RESEARCH, ExecutionMode.BACKTEST}
_EXCHANGE_MODES = {ExecutionMode.TESTNET, ExecutionMode.LIVE}


def normalize_mode(value: ExecutionMode | str | None) -> ExecutionMode:
    if isinstance(value, ExecutionMode):
        return value
    try:
        return ExecutionMode(str(value or "paper").strip().lower())
    except ValueError as exc:
        raise ExecutionModeError(f"unsupported execution mode: {value!r}") from exc


def mode_from_settings(settings: Settings | None = None, requested: ExecutionMode | str | None = None) -> ExecutionMode:
    settings = settings or get_settings()
    if settings.live_mode:
        raise LiveTradingDisabled("LIVE_MODE is on; live execution is disabled")
    if requested is not None:
        mode = normalize_mode(requested)
        if mode == ExecutionMode.LIVE:
            raise LiveTradingDisabled("live mode is disabled")
        if mode == ExecutionMode.TESTNET and not settings.testnet_mode:
            raise ExecutionModeError("testnet execution requires TESTNET_MODE=on and LIVE_MODE=off")
        return mode
    if settings.testnet_mode:
        return ExecutionMode.TESTNET
    return ExecutionMode.PAPER


resolve_execution_mode = mode_from_settings


def mode_allows_orders(mode: ExecutionMode | str) -> bool:
    return normalize_mode(mode) in _ORDER_MODES


def mode_allows_persistence(mode: ExecutionMode | str) -> bool:
    return normalize_mode(mode) in _PERSISTED_MODES


def mode_is_research(mode: ExecutionMode | str) -> bool:
    return normalize_mode(mode) in _RESEARCH_MODES


def mode_uses_exchange(mode: ExecutionMode | str) -> bool:
    return normalize_mode(mode) in _EXCHANGE_MODES


def mode_is_live(mode: ExecutionMode | str) -> bool:
    return normalize_mode(mode) == ExecutionMode.LIVE


def live_trading_enabled() -> bool:
    """Live trading is permanently disabled unless the hard-coded flag is
    changed in source and reviewed."""
    return bool(LIVE_TRADING_ENABLED)


def assert_mode_can_submit(mode: ExecutionMode | str) -> None:
    normalized = normalize_mode(mode)
    if normalized == ExecutionMode.LIVE and not live_trading_enabled():
        raise LiveTradingDisabled("live mode is disabled in code")
    if normalized in _RESEARCH_MODES:
        raise ExecutionModeError(f"{normalized.value} mode cannot submit orders")
    if normalized not in _ORDER_MODES:
        raise ExecutionModeError(f"{normalized.value} mode cannot submit orders")


def assert_mode_can_persist(mode: ExecutionMode | str) -> None:
    normalized = normalize_mode(mode)
    if normalized in _RESEARCH_MODES:
        raise ResearchBoundaryError(f"{normalized.value} mode must not write trading state")
    if normalized == ExecutionMode.LIVE:
        raise LiveTradingDisabled("live mode is disabled")
    if normalized not in _PERSISTED_MODES:
        raise ExecutionModeError(f"{normalized.value} mode cannot write trading state")


def assert_testnet_ready(settings: Settings | None = None, *, has_credentials: bool | None = None) -> None:
    settings = settings or get_settings()
    if settings.live_mode:
        raise LiveTradingDisabled("LIVE_MODE must be off for testnet execution")
    if not settings.testnet_mode:
        raise ExecutionModeError("TESTNET_MODE must be on for testnet execution")
    if has_credentials is False:
        raise ExecutionModeError("testnet credentials are required")


def venue_for_mode(mode: ExecutionMode | str) -> str:
    normalized = normalize_mode(mode)
    if normalized == ExecutionMode.PAPER:
        return "paper"
    if normalized == ExecutionMode.TESTNET:
        return "delta-testnet"
    if normalized == ExecutionMode.LIVE:
        return "delta-live"
    return normalized.value
