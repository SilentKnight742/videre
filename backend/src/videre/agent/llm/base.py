"""Provider-neutral model interface (SPEC-A2 section 4)."""

import json
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from videre.domain.cost import Prices, estimate_max_micro


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "model"]
    text: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    messages: list[Message]
    # JSON schema; the provider must return JSON matching it.
    response_schema: dict[str, Any] | None = None
    max_output_tokens: int = Field(gt=0)
    temperature: float = 0.2

    def input_chars(self) -> int:
        return len(self.system) + sum(len(m.text) for m in self.messages)


class LLMResponse(BaseModel):
    text: str
    parsed: dict[str, Any] | None = None
    tokens_in: int = Field(ge=0)
    # Includes any thinking tokens the provider bills as output.
    tokens_out: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    model: str
    finish_reason: str
    cached: bool = False
    # Only set when the provider exposes one.
    thought_summary: str | None = None


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, req: LLMRequest) -> LLMResponse: ...


class ProviderError(Exception):
    """The provider could not produce a response."""


class RateLimitError(ProviderError):
    """The provider said "too many requests"; `retry_after` is its suggested wait, if any."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ReplayMiss(ProviderError):
    """DEMO_REPLAY is on and the request is not in the cache. Never falls back to the network."""


def estimate_request_max_micro(req: LLMRequest, prices: Prices) -> int:
    """Worst-case metered cost of `req` (phase B reserves this before the call)."""
    return estimate_max_micro(req.input_chars(), req.max_output_tokens, prices)


def render_request(req: LLMRequest) -> str:
    """The request as one readable text, for `node_io.prompt` (what a person sees per step)."""
    parts = [f"[system]\n{req.system}", *(f"[{m.role}]\n{m.text}" for m in req.messages)]
    return "\n\n".join(parts)


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Parse `text` as a JSON object; None if it is not one (the caller decides on repair)."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
