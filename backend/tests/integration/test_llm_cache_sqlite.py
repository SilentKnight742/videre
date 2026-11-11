from videre.agent.llm import CachingProvider, LLMRequest, Message, SqliteResponseStore
from videre.agent.llm.fake import scripted
from videre.storage.db import Store

REQ = LLMRequest(
    system="sys",
    messages=[Message(role="user", text="plan")],
    response_schema={"type": "object"},
    max_output_tokens=64,
)


async def test_sqlite_store_round_trips_a_response(store: Store) -> None:
    inner = scripted([{"title": "t"}])
    first = await CachingProvider(inner, SqliteResponseStore(store), replay=False).generate(REQ)

    replay_inner = scripted(["unused"])
    again = await CachingProvider(replay_inner, SqliteResponseStore(store), replay=True).generate(
        REQ
    )
    assert replay_inner.call_count == 0
    assert again.parsed == {"title": "t"}
    assert again.model_dump(exclude={"cached", "latency_ms"}) == first.model_dump(
        exclude={"cached", "latency_ms"}
    )
    row = await store.fetch_one("SELECT model, tokens_in, tokens_out FROM llm_cache")
    assert row is not None
    assert (row["model"], row["tokens_in"], row["tokens_out"]) == (
        first.model,
        first.tokens_in,
        first.tokens_out,
    )
