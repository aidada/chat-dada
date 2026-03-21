from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 2
    backoff_seconds: float = 0.0

    def should_retry(self, attempt: int, error: Exception | None = None) -> bool:
        return attempt < self.max_attempts

