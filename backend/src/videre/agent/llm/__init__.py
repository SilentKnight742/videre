"""Model providers: one interface, composable wrappers (SPEC-A2 section 4, ADR 0006).

Composition used by the app: Caching(RateLimited(Gemini)). Cache hits never spend a
rate-limit token, and in replay mode nothing reaches the network.
"""

from videre.agent.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    RateLimitError,
    ReplayMiss,
    estimate_request_max_micro,
    parse_json_object,
)
from videre.agent.llm.caching import CachingProvider, SqliteResponseStore, cache_key
from videre.agent.llm.fake import FakeProvider
from videre.agent.llm.ratelimit import RateLimitedProvider, TokenBucket
from videre.config import Settings
from videre.storage.db import Store

__all__ = [
    "CachingProvider",
    "FakeProvider",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "ProviderError",
    "RateLimitError",
    "RateLimitedProvider",
    "ReplayMiss",
    "SqliteResponseStore",
    "TokenBucket",
    "build_provider",
    "cache_key",
    "estimate_request_max_micro",
    "parse_json_object",
]


def build_provider(
    settings: Settings, store: Store, inner: LLMProvider | None = None
) -> CachingProvider:
    """The app's provider stack. `inner` defaults to Gemini (built lazily, so tests and
    DEMO_REPLAY never need an API key or the google-genai client)."""
    if inner is None:
        from videre.agent.llm.gemini import GeminiProvider

        inner = GeminiProvider(settings.gemini_api_key.get_secret_value(), settings.gemini_model)
    limited = RateLimitedProvider(inner, settings.gemini_rpm)
    return CachingProvider(limited, SqliteResponseStore(store), replay=settings.demo_replay)
