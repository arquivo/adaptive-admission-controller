"""Test-only doubles shared across unit tests. Never referenced from
`config/backends.yaml` or env-var wiring — `RedisPenaltyStore` is the sole
production `PenaltyStore` (`docs/decision_log.md` D3).
"""

from __future__ import annotations

from app.interfaces import PenaltyStore


class FakePenaltyStore(PenaltyStore):
    """In-memory `key -> count` store with the same increment-then-conditionally
    -set-TTL semantics as `RedisPenaltyStore`, minus an actual TTL clock —
    tests that care about expiry drive it directly rather than sleeping.
    """

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def increment_and_get(self, key: str, ttl_seconds: int) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str) -> None:
        """Test helper: simulate the window rolling over."""
        self.counts.pop(key, None)
