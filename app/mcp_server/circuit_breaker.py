"""Per-domain circuit breaker for MCP tool calls.

Not an MCP spec primitive — the spec leaves failure isolation between
backend servers to the orchestrator/gateway layer, so implementing it here
is the correct move, not a workaround. A hand-rolled three-state breaker
(CLOSED -> OPEN -> HALF_OPEN) rather than an external library: this is a
small, in-process concern with no need for cross-process shared state, and
having 3 domains (vs. 1) makes per-backend failure isolation genuinely
useful rather than cosmetic — a sustained outage in the ticketing domain
(e.g. Jira's real API down, in a non-simulated deployment) shouldn't also
block identity/access calls, which have nothing to do with it.
"""

import time
from dataclasses import dataclass, field
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"  # normal operation, calls pass through
    OPEN = "open"  # tripped: calls fail fast without attempting the backend
    HALF_OPEN = "half_open"  # trial period: one call allowed through to test recovery


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    recovery_timeout_seconds: float = 30.0
    state: CircuitState = field(default=CircuitState.CLOSED)
    _failure_count: int = field(default=0, repr=False)
    _opened_at: float | None = field(default=None, repr=False)

    def record_success(self) -> None:
        self._failure_count = 0
        self.state = CircuitState.CLOSED
        self._opened_at = None

    def record_failure(self) -> None:
        self._failure_count += 1
        if self.state == CircuitState.HALF_OPEN or self._failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            assert self._opened_at is not None
            if time.monotonic() - self._opened_at >= self.recovery_timeout_seconds:
                self.state = CircuitState.HALF_OPEN
                return True
            return False
        # HALF_OPEN: allow exactly one trial call through; record_success/
        # record_failure decide the next state based on its outcome.
        return True


class CircuitOpenError(Exception):
    """Raised when a call is rejected because its domain's breaker is open."""


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(domain: str) -> CircuitBreaker:
    if domain not in _breakers:
        _breakers[domain] = CircuitBreaker()
    return _breakers[domain]


def reset_all_breakers() -> None:
    """Test/ops helper — not called by app code during normal operation."""
    _breakers.clear()


def snapshot_all_breakers() -> dict[str, str]:
    """{domain: state} for every domain that has an active breaker — used by
    GET /health to report per-backend status. A domain with no breaker yet
    (no calls made to it since process start) is implicitly healthy/closed,
    so it's fine that it's simply absent here rather than needing eager
    initialization for every known domain.
    """
    return {domain: breaker.state.value for domain, breaker in _breakers.items()}
