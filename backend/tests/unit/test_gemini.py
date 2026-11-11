"""GeminiProvider against a stub client: request mapping, response parsing, error mapping."""

from types import SimpleNamespace
from typing import Any

import pytest
from google.genai import errors, types

from videre.agent.llm import LLMRequest, Message, ProviderError, RateLimitError
from videre.agent.llm.gemini import GeminiProvider, retry_delay_seconds

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}}
REQ = LLMRequest(
    system="be brief",
    messages=[Message(role="user", text="hi"), Message(role="model", text="yo")],
    response_schema=SCHEMA,
    max_output_tokens=128,
)


class StubModels:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def generate_content(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def provider(result: Any, **kw: Any) -> tuple[GeminiProvider, StubModels]:
    models = StubModels(result)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    return GeminiProvider("", "gemini-test", client=client, **kw), models  # type: ignore[arg-type]


def response(parts: list[types.Part], **usage: int) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts),
                finish_reason=types.FinishReason.STOP,
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(**usage),
        model_version="gemini-test-001",
    )


async def test_request_is_mapped_to_the_sdk() -> None:
    p, models = provider(response([types.Part(text="{}")], prompt_token_count=1))
    await p.generate(REQ)
    assert models.kwargs["model"] == "gemini-test"
    contents = models.kwargs["contents"]
    assert [c.role for c in contents] == ["user", "model"]
    assert contents[0].parts[0].text == "hi"
    config: types.GenerateContentConfig = models.kwargs["config"]
    assert config.system_instruction == "be brief"
    assert config.max_output_tokens == 128
    assert config.temperature == 0.2
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA
    assert config.thinking_config is not None
    assert config.thinking_config.include_thoughts is True


async def test_no_schema_means_no_json_mode_and_thoughts_can_be_off() -> None:
    p, models = provider(response([types.Part(text="hello")]), include_thoughts=False)
    plain = REQ.model_copy(update={"response_schema": None})
    resp = await p.generate(plain)
    config = models.kwargs["config"]
    assert config.response_mime_type is None
    assert config.thinking_config is None
    assert resp.parsed is None
    assert resp.text == "hello"


async def test_response_text_tokens_and_thought_summary() -> None:
    parts = [
        types.Part(text="I should greet.", thought=True),
        types.Part(text='{"title": '),
        types.Part(text='"Hi"}'),
    ]
    p, _ = provider(
        response(parts, prompt_token_count=11, candidates_token_count=7, thoughts_token_count=30)
    )
    resp = await p.generate(REQ)
    assert resp.text == '{"title": "Hi"}'
    assert resp.parsed == {"title": "Hi"}
    assert resp.tokens_in == 11
    assert resp.tokens_out == 37  # thinking tokens are billed as output
    assert resp.thought_summary == "I should greet."
    assert resp.model == "gemini-test-001"
    assert resp.finish_reason == "STOP"
    assert resp.cached is False


async def test_invalid_json_is_returned_unparsed_for_the_caller_to_repair() -> None:
    p, _ = provider(response([types.Part(text="{not json")]))
    resp = await p.generate(REQ)
    assert resp.parsed is None
    assert resp.text == "{not json"


async def test_missing_usage_and_no_candidates() -> None:
    p, _ = provider(types.GenerateContentResponse())
    resp = await p.generate(REQ)
    assert (resp.text, resp.tokens_in, resp.tokens_out) == ("", 0, 0)
    assert resp.thought_summary is None


async def test_429_becomes_rate_limit_error_with_server_delay() -> None:
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}],
        }
    }
    p, _ = provider(errors.ClientError(429, body))
    with pytest.raises(RateLimitError) as info:
        await p.generate(REQ)
    assert info.value.retry_after == 17.0


async def test_other_api_errors_become_provider_errors() -> None:
    body = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad schema"}}
    p, _ = provider(errors.ClientError(400, body))
    with pytest.raises(ProviderError, match="bad schema") as info:
        await p.generate(REQ)
    assert not isinstance(info.value, RateLimitError)


async def test_missing_key_fails_on_use_not_on_construction() -> None:
    p = GeminiProvider("", "gemini-test")
    with pytest.raises(ProviderError, match="GEMINI_API_KEY"):
        await p.generate(REQ)


def test_model_is_required() -> None:
    with pytest.raises(ValueError, match="GEMINI_MODEL"):
        GeminiProvider("k", "")


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        ({"error": {"details": [{"@type": "x.RetryInfo", "retryDelay": "2.5s"}]}}, 2.5),
        ([{"@type": "x.RetryInfo", "retryDelay": "9s"}], 9.0),
        ({"error": {"details": [{"@type": "x.QuotaFailure"}]}}, None),
        ({"error": {}}, None),
        ("not json", None),
    ],
)
def test_retry_delay_parsing(details: Any, expected: float | None) -> None:
    assert retry_delay_seconds(details) == expected
