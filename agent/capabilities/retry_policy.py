"""RetryPolicy — configurable retry + review-gate policy for agent runs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Controls how many times GateRunner retries an agent after review failure."""

    max_retries: int = 3
    backoff_seconds: float = 0.0
    pass_threshold: float = 0.7

    def should_retry(self, attempt: int) -> bool:
        """Return True if another attempt is allowed.

        *attempt* is 1-based: after the first run ``attempt == 1``.
        """
        return attempt < self.max_retries
