"""Per-tool rate limiting for MCP tool invocations.

MCP spec 2025-11-25's Tools security considerations: "Servers MUST: ...
rate limit tool invocations." Before this, the only rate limiting in this
codebase was `slowapi` on the FastAPI HTTP layer (POST /tickets,
POST /approvals/{id}/decide) — a caller that reaches the MCP gateway
directly (with a valid bearer token over streamable-HTTP, or over stdio)
bypassed that entirely and could invoke any tool at unlimited rate.

A hand-rolled token bucket, not an external library: this is a small,
single-process concern (mirrors app/mcp_server/circuit_breaker.py's own
reasoning for hand-rolling rather than reaching for a dependency), and a
bucket per TOOL NAME (not globally, and not per-caller — this MCP server
has no per-caller identity today, only a single shared bearer token) keeps
a burst against one tool from starving unrelated ones.
"""

import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    capacity: int
    refill_per_second: float
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.capacity)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        self._last_refill = now

    def try_consume(self) -> bool:
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


class RateLimitExceededError(Exception):
    """Raised when a tool call is rejected for exceeding its rate limit."""


_buckets: dict[str, TokenBucket] = {}

# Generous enough not to interfere with a single ticket's normal fan-out
# (Stage 1.3's concurrent batch execution can fire several calls to the
# same tool within milliseconds of each other) while still bounding a
# runaway/malicious caller hammering one tool directly against the
# gateway. Not configurable via .env today — this is a floor/backstop, not
# a tuning knob operators are expected to need per-deployment.
_DEFAULT_CAPACITY = 20
_DEFAULT_REFILL_PER_SECOND = 5.0


def get_bucket(tool_name: str) -> TokenBucket:
    if tool_name not in _buckets:
        _buckets[tool_name] = TokenBucket(
            capacity=_DEFAULT_CAPACITY, refill_per_second=_DEFAULT_REFILL_PER_SECOND
        )
    return _buckets[tool_name]


def check_rate_limit(tool_name: str) -> None:
    """Raises RateLimitExceededError if tool_name's bucket is exhausted;
    otherwise consumes one token and returns."""
    if not get_bucket(tool_name).try_consume():
        raise RateLimitExceededError(
            f"Rate limit exceeded for tool {tool_name!r} — too many calls in a short window."
        )


def reset_all_buckets() -> None:
    """Test/ops helper — not called by app code during normal operation."""
    _buckets.clear()
