"""Scripted provider for tests. Never touches the network."""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from videre.agent.llm.base import LLMRequest, LLMResponse, ProviderError, parse_json_object

# A matcher is a substring of the request text (system + messages) or a predicate.
Matcher = str | Callable[[LLMRequest], bool]
# A reply is: a dict (sent as JSON), a str (sent verbatim), a full LLMResponse,
# an exception to raise, or a callable that builds one of those from the request.
Reply = dict[str, Any] | str | LLMResponse | BaseException | Callable[[LLMRequest], Any]


def request_text(req: LLMRequest) -> str:
    return "\n".join([req.system, *(m.text for m in req.messages)])


@dataclass
class _Rule:
    matcher: Matcher
    replies: list[Reply]
    used: int = 0

    def matches(self, req: LLMRequest) -> bool:
        if isinstance(self.matcher, str):
            return self.matcher in request_text(req)
        return self.matcher(req)

    def next_reply(self) -> Reply:
        # Replies are consumed in order; the last one repeats.
        reply = self.replies[min(self.used, len(self.replies) - 1)]
        self.used += 1
        return reply


@dataclass
class FakeProvider:
    """First matching rule wins. Unmatched requests raise `ProviderError`.

    Token counts are deterministic: ceil(chars / 4) for input and output.
    """

    name: str = "fake"
    model: str = "fake-model"
    calls: list[LLMRequest] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)

    def on(self, matcher: Matcher, *replies: Reply) -> "FakeProvider":
        if not replies:
            raise ValueError("a rule needs at least one reply")
        self._rules.append(_Rule(matcher, list(replies)))
        return self

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def generate(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        rule = next((r for r in self._rules if r.matches(req)), None)
        if rule is None:
            raise ProviderError(f"FakeProvider: no rule matches request {req.system[:60]!r}")
        return self._build(req, rule.next_reply())

    def _build(self, req: LLMRequest, reply: Reply) -> LLMResponse:
        if callable(reply) and not isinstance(reply, BaseException):
            reply = reply(req)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, LLMResponse):
            return reply
        if isinstance(reply, dict):
            text = json.dumps(reply)
        elif isinstance(reply, str):
            text = reply
        else:
            raise TypeError(f"unsupported fake reply {type(reply).__name__}")
        return LLMResponse(
            text=text,
            parsed=parse_json_object(text) if req.response_schema is not None else None,
            tokens_in=-(-req.input_chars() // 4),
            tokens_out=-(-len(text) // 4),
            latency_ms=1,
            model=self.model,
            finish_reason="STOP",
        )


def scripted(replies: Sequence[Reply]) -> FakeProvider:
    """A provider that answers every request with `replies` in order."""
    return FakeProvider().on(lambda _req: True, *replies)
