"""Real Gemini call. Skipped unless `pytest --live`; needs GEMINI_API_KEY and GEMINI_MODEL."""

import pytest

from videre.agent.llm import LLMRequest, Message
from videre.agent.llm.gemini import GeminiProvider
from videre.config import Settings

pytestmark = pytest.mark.live


async def test_structured_output_and_token_usage() -> None:
    s = Settings()
    if not (s.gemini_api_key.get_secret_value() and s.gemini_model):
        pytest.skip("GEMINI_API_KEY / GEMINI_MODEL not set")
    p = GeminiProvider(s.gemini_api_key.get_secret_value(), s.gemini_model)
    req = LLMRequest(
        system="You name small businesses. Reply in JSON.",
        messages=[Message(role="user", text="Name a flower shop.")],
        response_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
        max_output_tokens=1024,
    )
    resp = await p.generate(req)
    assert resp.parsed is not None, resp.finish_reason
    assert isinstance(resp.parsed["name"], str)
    assert resp.tokens_in > 0
    assert resp.tokens_out > 0
    print(
        f"\nfinish={resp.finish_reason} in={resp.tokens_in} out={resp.tokens_out} "
        f"thought_summary={'yes' if resp.thought_summary else 'no'}"
    )
