"""Gemini via google-genai. Never logs the API key or prompts."""

import re
import time
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from videre.agent.llm.base import (
    LLMRequest,
    LLMResponse,
    ProviderError,
    RateLimitError,
    parse_json_object,
)

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def retry_delay_seconds(details: Any) -> float | None:
    """Pull the server's suggested wait (google.rpc.RetryInfo, e.g. "17s") from an error body."""
    if isinstance(details, dict):
        details = details.get("error", details).get("details")
    if not isinstance(details, list):
        return None
    for item in details:
        if isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo"):
            match = _DURATION.match(str(item.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    return None


def _is_rate_limited(exc: errors.APIError) -> bool:
    return exc.code == 429 or exc.status == "RESOURCE_EXHAUSTED"


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        include_thoughts: bool = True,
        timeout_s: float = 90.0,
        client: genai.Client | None = None,
    ) -> None:
        if not model:
            raise ValueError("GEMINI_MODEL is not set")
        self.model = model
        self._include_thoughts = include_thoughts
        self._api_key = api_key
        self._timeout_ms = int(timeout_s * 1000)
        # Built on first use, so replay mode never needs a key.
        self._client = client

    def _get_client(self) -> genai.Client:
        if self._client is None:
            if not self._api_key:
                raise ProviderError("GEMINI_API_KEY is not set")
            self._client = genai.Client(
                api_key=self._api_key,
                # A request that never answers must not hang the run.
                http_options=types.HttpOptions(timeout=self._timeout_ms),
            )
        return self._client

    def _config(self, req: LLMRequest) -> types.GenerateContentConfig:
        config = types.GenerateContentConfig(
            system_instruction=req.system,
            max_output_tokens=req.max_output_tokens,
            temperature=req.temperature,
        )
        if req.response_schema is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = req.response_schema
        if self._include_thoughts:
            config.thinking_config = types.ThinkingConfig(include_thoughts=True)
        return config

    async def generate(self, req: LLMRequest) -> LLMResponse:
        contents = [
            types.Content(role=m.role, parts=[types.Part(text=m.text)]) for m in req.messages
        ]
        started = time.monotonic()
        try:
            resp = await self._get_client().aio.models.generate_content(
                model=self.model, contents=contents, config=self._config(req)
            )
        except errors.APIError as exc:
            if _is_rate_limited(exc):
                raise RateLimitError(
                    f"gemini {exc.code} {exc.status}", retry_delay_seconds(exc.details)
                ) from exc
            raise ProviderError(f"gemini {exc.code} {exc.status}: {exc.message}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini transport error: {type(exc).__name__}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return self._to_response(resp, req, latency_ms)

    def _to_response(
        self, resp: types.GenerateContentResponse, req: LLMRequest, latency_ms: int
    ) -> LLMResponse:
        text_parts: list[str] = []
        thought_parts: list[str] = []
        finish_reason = "UNKNOWN"
        if resp.candidates:
            cand = resp.candidates[0]
            if cand.finish_reason is not None:
                finish_reason = str(getattr(cand.finish_reason, "value", cand.finish_reason))
            for part in (cand.content.parts if cand.content else None) or []:
                if not part.text:
                    continue
                (thought_parts if part.thought else text_parts).append(part.text)
        elif resp.prompt_feedback and resp.prompt_feedback.block_reason:
            finish_reason = f"BLOCKED_{resp.prompt_feedback.block_reason}"

        usage = resp.usage_metadata
        tokens_in = (usage.prompt_token_count or 0) if usage else 0
        tokens_out = (
            (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0) if usage else 0
        )
        text = "".join(text_parts)
        return LLMResponse(
            text=text,
            parsed=parse_json_object(text) if req.response_schema is not None else None,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            model=resp.model_version or self.model,
            finish_reason=finish_reason,
            thought_summary="\n".join(thought_parts) or None,
        )
