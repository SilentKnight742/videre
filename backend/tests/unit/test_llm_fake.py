import pytest

from videre.agent.llm import FakeProvider, LLMRequest, LLMResponse, Message, ProviderError
from videre.agent.llm.fake import scripted

SCHEMA = {"type": "object"}


def req(text: str, system: str = "sys", schema: dict | None = SCHEMA) -> LLMRequest:
    return LLMRequest(
        system=system,
        messages=[Message(role="user", text=text)],
        response_schema=schema,
        max_output_tokens=256,
    )


async def test_first_matching_rule_wins_and_calls_are_counted() -> None:
    fake = FakeProvider().on("plan", {"title": "p"}).on(lambda r: True, {"other": 1})
    a = await fake.generate(req("please plan this"))
    b = await fake.generate(req("something else"))
    assert a.parsed == {"title": "p"}
    assert b.parsed == {"other": 1}
    assert fake.call_count == 2
    assert fake.calls[0].messages[0].text == "please plan this"


async def test_replies_are_consumed_in_order_and_the_last_repeats() -> None:
    fake = scripted(["not json", {"ok": True}])
    first = await fake.generate(req("x"))
    assert first.parsed is None
    assert first.text == "not json"
    assert (await fake.generate(req("x"))).parsed == {"ok": True}
    assert (await fake.generate(req("x"))).parsed == {"ok": True}


async def test_unmatched_request_raises() -> None:
    with pytest.raises(ProviderError):
        await FakeProvider().on("nope", "x").generate(req("hello"))


async def test_exceptions_and_callables_and_full_responses() -> None:
    full = LLMResponse(
        text="t", tokens_in=1, tokens_out=2, latency_ms=3, model="m", finish_reason="STOP"
    )
    fake = scripted([ProviderError("boom"), lambda r: {"echo": r.messages[0].text}, full])
    with pytest.raises(ProviderError, match="boom"):
        await fake.generate(req("a"))
    assert (await fake.generate(req("b"))).parsed == {"echo": "b"}
    assert await fake.generate(req("c")) == full


async def test_tokens_are_deterministic() -> None:
    resp = await scripted(["abcdefgh"]).generate(req("u" * 9, system="s" * 3, schema=None))
    assert resp.tokens_in == 3  # 12 chars / 4
    assert resp.tokens_out == 2
    assert resp.parsed is None  # no schema requested
