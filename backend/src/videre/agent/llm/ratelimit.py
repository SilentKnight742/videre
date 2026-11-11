"""Client-side rate limit (async token bucket) plus retry on provider 429s."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable

from videre.agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderError,
    RateLimitError,
)

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """`rate_per_min` tokens per minute, at most `burst` held (default 1: even spacing)."""

    def __init__(
        self,
        rate_per_min: float,
        *,
        burst: int = 1,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate_per_min <= 0:
            raise ValueError("rate_per_min must be positive")
        if burst < 1:
            raise ValueError("burst must be at least 1")
        self._per_sec = rate_per_min / 60.0
        self._burst = float(burst)
        self._tokens = float(burst)
        self._clock = clock
        self._sleep = sleep
        self._last = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._per_sec)
        self._last = now

    async def acquire(self) -> None:
        # The lock makes waiters queue in FIFO order instead of racing for each token.
        async with self._lock:
            self._refill()
            while self._tokens < 1.0:
                await self._sleep((1.0 - self._tokens) / self._per_sec)
                self._refill()
            self._tokens -= 1.0


class RateLimitedProvider:
    """Waits for a bucket token before every attempt; retries `RateLimitError` with
    exponential backoff + jitter (or the server's retry delay), at most `max_attempts`
    attempts in total, then raises `ProviderError`. Other errors are not retried."""

    def __init__(
        self,
        inner: LLMProvider,
        rpm: int,
        *,
        max_attempts: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.bucket = TokenBucket(rpm, clock=clock, sleep=sleep)
        self._max_attempts = max_attempts
        self._base = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._rand = rand

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        """Delay after failed attempt number `attempt` (1-based)."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self._max_delay)
        exp = self._base * 2 ** (attempt - 1)
        return min(exp + self._rand() * self._base, self._max_delay)

    async def generate(self, req: LLMRequest) -> LLMResponse:
        for attempt in range(1, self._max_attempts + 1):
            await self.bucket.acquire()
            try:
                return await self.inner.generate(req)
            except RateLimitError as exc:
                if attempt == self._max_attempts:
                    raise ProviderError(
                        f"rate limited by {self.name}; gave up after {attempt} attempts"
                    ) from exc
                await self._sleep(self.backoff(attempt, exc.retry_after))
        raise AssertionError("unreachable")  # pragma: no cover
