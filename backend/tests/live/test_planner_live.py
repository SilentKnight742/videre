"""One real planner call. Skipped unless `pytest --live`; needs GEMINI_API_KEY and GEMINI_MODEL."""

from typing import NamedTuple

import pytest

from videre.agent.llm import LLMRequest, LLMResponse
from videre.agent.llm.gemini import GeminiProvider
from videre.agent.planner import Planner
from videre.config import Settings
from videre.domain.cost import Prices
from videre.services.events import EventHub
from videre.services.runs import RunService
from videre.storage.db import Store

pytestmark = pytest.mark.live


class Tool(NamedTuple):
    name: str
    description: str


# The registry lands in A2d; these are its tools as SPEC-A2 section 7 describes them.
TOOLS = [
    Tool("write_file", "Write a text file (path, content) into the run workspace."),
    Tool("read_file", "Read a text file from the run workspace."),
    Tool("list_files", "List the files in the run workspace."),
    Tool("check_domain", "Mock: says whether a domain name is available and quotes a price."),
]


class Recording:
    """Keeps every response so the test can look at finish reasons."""

    def __init__(self, inner: GeminiProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.responses: list[LLMResponse] = []

    async def generate(self, req: LLMRequest) -> LLMResponse:
        resp = await self.inner.generate(req)
        self.responses.append(resp)
        return resp


async def test_a_real_planner_call_returns_a_valid_plan(store: Store) -> None:
    s = Settings()
    if not (s.gemini_api_key.get_secret_value() and s.gemini_model):
        pytest.skip("GEMINI_API_KEY / GEMINI_MODEL not set")
    provider = Recording(GeminiProvider(s.gemini_api_key.get_secret_value(), s.gemini_model))
    runs = RunService(EventHub(store))
    planner = Planner(
        runs,
        provider,
        tools=TOOLS,
        prices=Prices(s.price_in_per_mtok, s.price_out_per_mtok),
        max_output_tokens=s.max_output_tokens,
    )
    run_id = await runs.create_run("build a website for my flower shop", provider="gemini")

    plan = await planner.plan(run_id)

    snap = await runs.snapshot(run_id)
    assert provider.responses, snap.run.error  # the model call itself failed
    first = provider.responses[0]
    print(
        f"\nfinish={first.finish_reason} in={first.tokens_in} out={first.tokens_out}"
        f" calls={len(provider.responses)} title={snap.run.title!r}"
    )
    for node in snap.nodes:
        print(f"  {node.path:<12} {node.status:<8} {node.title}")
    # SPEC-A2 section 9: the output cap must not truncate the plan.
    assert first.finish_reason != "MAX_TOKENS"
    assert plan is not None, snap.run.error
    assert len(provider.responses) == 1, "the first answer needed a repair"
    assert 3 <= len(plan.tasks) <= 7
    assert snap.run.title
