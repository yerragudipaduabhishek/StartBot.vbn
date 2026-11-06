"""Host guards and domain errors for the execution layer."""

from urllib.parse import urlparse

# Production Delta REST hosts - never reachable from a testnet-mode client.
PROD_REST_HOSTS = {"api.delta.exchange", "api.india.delta.exchange"}
PROD_WS_HOSTS = {"socket.delta.exchange", "socket.india.delta.exchange"}

# Known testnet REST hosts (global + india).
TESTNET_REST_HOSTS = {"testnet-api.delta.exchange", "cdn-ind.testnet.deltaex.org"}
TESTNET_WS_HOSTS = {"socket.delta.exchange", "socket-ind.testnet.deltaex.org"}


class ExecutionSafetyError(RuntimeError):
    """Raised when the execution layer is asked to do something unsafe."""


class DuplicateOrderError(RuntimeError):
    """Raised when a client_order_id has already been used."""


def validate_execution_host(host: str, testnet_mode: bool, extra_allowed: set[str] | None = None) -> None:
    """Guard used by order clients.

    - While testnet_mode is on: production hosts are always forbidden, and an
      unknown/untested host is rejected unless explicitly allowed (test mocks).
    - While testnet_mode is off: the execution layer refuses to operate at all.
    """
    host = (host or "").lower()
    if not testnet_mode:
        raise ExecutionSafetyError("execution client refuses to run while TESTNET_MODE is off")
    if not host:
        raise ExecutionSafetyError("empty execution host")
    base = host.split(":")[0] if ":" in host else host
    if any(base == p or base.endswith("." + p) for p in PROD_REST_HOSTS):
        raise ExecutionSafetyError(f"production host {host} is forbidden while TESTNET_MODE=true")
    allowed = set(TESTNET_REST_HOSTS)
    allowed |= {h for h in (extra_allowed or set())}
    if any(base == a or base.endswith("." + a) for a in allowed):
        return
    raise ExecutionSafetyError(f"host {host} is not an approved testnet host")


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()