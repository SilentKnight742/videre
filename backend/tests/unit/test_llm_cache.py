import pytest

from videre.agent.llm import (
    CachingProvider,
    LLMRequest,
    LLMResponse,
    Message,
    ReplayMiss,
    cache_key,
)
from videre.agent.llm.fake import scripted


def req(text: str = "hi", schema: dict | None = None) -> LLMRequest:
    return LLMRequest(
        system="sys",
        messages=[Message(role="user", text=text)],
        response_schema=schema,
        max_output_tokens=64,
    )


class DictStore:
    def __init__(self) -> None:
        self.data: dict[str, LLMResponse] = {}

    async def get(self, key: str) -> LLMResponse | None:
        return self.data.get(key)

    async def put(self, key: str, resp: LLMResponse) -> None:
        self.data[key] = resp


def test_cache_key_is_stable_and_order_independent() -> None:
    a = req(schema={"type": "object", "properties": {"a": {}, "b": {}}})
    b = req(schema={"properties": {"b": {}, "a": {}}, "type": "object"})
    assert cache_key("m", a) == cache_key("m", b)
    assert len(cache_key("m", a)) == 64
    # pinned: changing the canonical form would silently invalidate a recorded demo
    assert cache_key("m", req()) == cache_key("m", req())
    assert cache_key("m", req()) != cache_key("m2", req())
    assert cache_key("m", req()) != cache_key("m", req("other"))
    assert cache_key("m", req()) != cache_key("m", req().model_copy(update={"temperature": 0.9}))


async def test_read_through_writes_on_miss_and_flags_hits() -> None:
    inner = scripted(["one"])
    cached = CachingProvider(inner, DictStore(), replay=False)
    first = await cached.generate(req())
    second = await cached.generate(req())
    assert inner.call_count == 1
    assert first.cached is False
    assert second.cached is True
    assert (second.text, second.tokens_in, second.tokens_out) == (
        first.text,
        first.tokens_in,
        first.tokens_out,
    )  # still metered


async def test_replay_hit_never_calls_inner() -> None:
    store = DictStore()
    await CachingProvider(scripted(["rec"]), store, replay=False).generate(req())
    inner = scripted(["live"])
    resp = await CachingProvider(inner, store, replay=True).generate(req())
    assert resp.text == "rec"
    assert inner.call_count == 0


async def test_replay_miss_raises_and_never_calls_inner() -> None:
    inner = scripted(["live"])
    with pytest.raises(ReplayMiss):
        await CachingProvider(inner, DictStore(), replay=True).generate(req())
    assert inner.call_count == 0
