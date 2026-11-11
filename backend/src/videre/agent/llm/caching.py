"""Read-through response cache, and the cache-only replay mode for the demo."""

import hashlib
import json
import time
from typing import Protocol

from videre.agent.llm.base import LLMProvider, LLMRequest, LLMResponse, ReplayMiss
from videre.storage.db import Store


def cache_key(model: str, req: LLMRequest) -> str:
    """sha256 of the model and the canonical JSON of the request. Stable across processes."""
    canonical = json.dumps(
        req.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(f"{model}\n{canonical}".encode()).hexdigest()


class ResponseStore(Protocol):
    async def get(self, key: str) -> LLMResponse | None: ...

    async def put(self, key: str, resp: LLMResponse) -> None: ...


class SqliteResponseStore:
    """Backed by the `llm_cache` table."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def get(self, key: str) -> LLMResponse | None:
        row = await self._store.fetch_one(
            "SELECT response FROM llm_cache WHERE prompt_hash = ?", (key,)
        )
        return None if row is None else LLMResponse.model_validate_json(row["response"])

    async def put(self, key: str, resp: LLMResponse) -> None:
        async with self._store.transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO llm_cache"
                " (prompt_hash, model, response, tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?)",
                (key, resp.model, resp.model_dump_json(), resp.tokens_in, resp.tokens_out),
            )


class CachingProvider:
    """Normal mode: read-through, write on miss. Replay mode: cache only; a miss raises ReplayMiss.

    A cached response keeps its token counts (it is still metered at list price) and is
    flagged `cached=True`; its latency is the lookup time.
    """

    def __init__(self, inner: LLMProvider, store: ResponseStore, *, replay: bool) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self._store = store
        self._replay = replay

    async def generate(self, req: LLMRequest) -> LLMResponse:
        key = cache_key(self.model, req)
        started = time.monotonic()
        hit = await self._store.get(key)
        if hit is not None:
            elapsed = int((time.monotonic() - started) * 1000)
            return hit.model_copy(update={"cached": True, "latency_ms": elapsed})
        if self._replay:
            raise ReplayMiss(f"DEMO_REPLAY=1 and no cached response for request {key[:12]}")
        resp = await self.inner.generate(req)
        await self._store.put(key, resp.model_copy(update={"cached": False}))
        return resp
