# 6. One provider interface, composed wrappers

Status: accepted

## Context

The agent needs Gemini now (free tier, strict rate limits) and Claude, OpenAI or external agents later. Tests must never touch the network, and the demo must be replayable without calling the model.

## Decision

- All model access goes through `LLMProvider.generate(LLMRequest) -> LLMResponse` (`agent/llm/base.py`). The request carries a system prompt, user/model messages, an optional JSON schema, a max output token count and a temperature. The response carries text, parsed JSON, token counts (thinking tokens count as output), latency, model, finish reason, a `cached` flag and an optional thought summary.
- Behaviour is added by wrapping, not by subclassing or merging: `CachingProvider(RateLimitedProvider(GeminiProvider))`.
  - `CachingProvider` keys on sha256 of the model plus the canonical JSON of the request. Normal mode reads through. Replay mode (`DEMO_REPLAY=1`) reads only, and a miss raises `ReplayMiss`.
  - `RateLimitedProvider` holds an async token bucket (`GEMINI_RPM`). It retries 429s with backoff and jitter, honouring the server's delay, at most 3 attempts.
  - Cache hits never spend a rate-limit token.
- `FakeProvider` gives scripted replies by matcher and counts calls; all offline tests use it.
- Cost is pure domain code in integer micro-USD: `ceil((in * price_in + out * price_out) / 1e6)`. Cached responses are still metered at list price but flagged `cached`.

## Consequences

- A new provider is one class implementing `generate`; caching, rate limiting and metering come for free.
- The demo replay is deterministic and provably offline (the inner provider's call counter stays at zero).
- Only 429s are retried. Other provider errors, including 5xx timeouts, fail the step, and the executor decides what happens next.
