from pathlib import Path

import pytest

from videre.agent.llm import (
    CachingProvider,
    LLMRequest,
    Message,
    RateLimitedProvider,
    ReplayMiss,
    build_provider,
)
from videre.agent.llm.fake import scripted
from videre.agent.llm.gemini import GeminiProvider
from videre.config import Settings
from videre.storage.db import Store

REQ = LLMRequest(system="s", messages=[Message(role="user", text="u")], max_output_tokens=8)


def settings(tmp_path: Path, **kw: object) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "x.db", gemini_model="gemini-x", **kw)


def test_default_stack_is_cache_over_ratelimit_over_gemini(tmp_path: Path, store: Store) -> None:
    p = build_provider(settings(tmp_path, gemini_rpm=7), store)
    assert isinstance(p, CachingProvider)
    assert isinstance(p.inner, RateLimitedProvider)
    assert isinstance(p.inner.inner, GeminiProvider)
    assert (p.name, p.model) == ("gemini", "gemini-x")


async def test_replay_stack_never_reaches_inner(tmp_path: Path, store: Store) -> None:
    inner = scripted(["live"])
    p = build_provider(settings(tmp_path, demo_replay=True), store, inner=inner)
    with pytest.raises(ReplayMiss):
        await p.generate(REQ)
    assert inner.call_count == 0
