import pytest

from videre.agent.llm import (
    LLMRequest,
    Message,
    ProviderError,
    RateLimitedProvider,
    RateLimitError,
    TokenBucket,
)
from videre.agent.llm.fake import scripted

REQ = LLMRequest(system="s", messages=[Message(role="user", text="u")], max_output_tokens=8)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_bucket_spaces_requests_evenly() -> None:
    clock = FakeClock()
    bucket = TokenBucket(10, clock=clock, sleep=clock.sleep)  # one every 6 s
    for _ in range(3):
        await bucket.acquire()
    assert clock.now == pytest.approx(12.0)
    assert clock.sleeps == [pytest.approx(6.0), pytest.approx(6.0)]


async def test_bucket_allows_a_burst_then_refills() -> None:
    clock = FakeClock()
    bucket = TokenBucket(60, burst=3, clock=clock, sleep=clock.sleep)  # one per second
    for _ in range(3):
        await bucket.acquire()
    assert clock.now == 0.0
    await bucket.acquire()
    assert clock.now == pytest.approx(1.0)
    clock.now += 100  # idle: refill caps at the burst size
    for _ in range(3):
        await bucket.acquire()
    assert clock.now == pytest.approx(101.0)


def make(fake, clock: FakeClock, rand: float = 0.5) -> RateLimitedProvider:
    return RateLimitedProvider(
        fake, rpm=6000, clock=clock, sleep=clock.sleep, rand=lambda: rand, base_delay=2.0
    )


async def test_429_is_retried_with_exponential_backoff_and_jitter() -> None:
    clock = FakeClock()
    fake = scripted([RateLimitError("429"), RateLimitError("429"), {"ok": 1}])
    resp = await make(fake, clock).generate(REQ)
    assert resp.text == '{"ok": 1}'
    assert fake.call_count == 3
    backoffs = [s for s in clock.sleeps if s > 0.5]  # ignore tiny bucket waits
    assert backoffs == [pytest.approx(2.0 + 1.0), pytest.approx(4.0 + 1.0)]


async def test_server_retry_delay_is_honoured() -> None:
    clock = FakeClock()
    fake = scripted([RateLimitError("429", retry_after=17.0), {"ok": 1}])
    await make(fake, clock).generate(REQ)
    assert pytest.approx(17.0) in clock.sleeps


async def test_gives_up_after_three_attempts() -> None:
    clock = FakeClock()
    fake = scripted([RateLimitError("429")])
    with pytest.raises(ProviderError, match="after 3 attempts") as info:
        await make(fake, clock).generate(REQ)
    assert not isinstance(info.value, RateLimitError)
    assert fake.call_count == 3


async def test_other_errors_are_not_retried() -> None:
    clock = FakeClock()
    fake = scripted([ProviderError("bad request")])
    with pytest.raises(ProviderError, match="bad request"):
        await make(fake, clock).generate(REQ)
    assert fake.call_count == 1


def test_backoff_is_capped() -> None:
    clock = FakeClock()
    p = RateLimitedProvider(
        scripted(["x"]), rpm=10, clock=clock, sleep=clock.sleep, rand=lambda: 0.0, max_delay=60.0
    )
    assert p.backoff(10, None) == 60.0
    assert p.backoff(1, 500.0) == 60.0
