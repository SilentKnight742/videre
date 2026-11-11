"""The planner against FakeProvider and a real (temporary) database. No network."""

import json
from typing import Any, NamedTuple

import pytest

from videre.agent.llm import FakeProvider, LLMRequest, LLMResponse, ProviderError
from videre.agent.llm.fake import scripted
from videre.agent.planner import PLAN_STEP_TITLE, REPAIR_STEP_TITLE, Planner
from videre.domain.cost import Prices, cost_micro
from videre.domain.events import EventType, Snapshot, apply_event, empty_snapshot
from videre.domain.states import can_transition
from videre.services.events import EventHub
from videre.services.runs import RunService
from videre.storage.db import Store

PROMPT = "build a website for my flower shop"
PRICES = Prices(in_per_mtok=750_000, out_per_mtok=3_750_000)


class Tool(NamedTuple):
    name: str
    description: str


TOOLS = [Tool("write_file", "Write a text file into the run workspace.")]


def task(key: str, *deps: str, subtasks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "title": f"Task {key}",
        "objective": f"{key} is finished.",
        "depends_on": list(deps),
        "subtasks": subtasks or [],
    }


# t1 -> t2, t3 -> t4, where t2 is composite (t2b needs t2a).
DIAMOND = {
    "title": "Flower shop website",
    "tasks": [
        task("t1"),
        task("t2", "t1", subtasks=[task("t2a"), task("t2b", "t2a")]),
        task("t3", "t1"),
        task("t4", "t2", "t3"),
    ],
}
CYCLE = {"title": "Loop", "tasks": [task("t1", "t3"), task("t2", "t1"), task("t3", "t2")]}


@pytest.fixture
def hub(store: Store) -> EventHub:
    return EventHub(store)


@pytest.fixture
def runs(hub: EventHub) -> RunService:
    return RunService(hub)


def planner(runs: RunService, provider: FakeProvider) -> Planner:
    return Planner(runs, provider, tools=TOOLS, prices=PRICES, max_output_tokens=8192)


async def replay(hub: EventHub, run_id: str) -> Snapshot:
    snap = empty_snapshot(run_id)
    for ev in await hub.read_after(run_id, 0):
        snap = apply_event(snap, ev)
    return snap


async def assert_consistent(runs: RunService, run_id: str) -> Snapshot:
    """Snapshot == replay of all events, and no illegal transition was emitted."""
    events = await runs.hub.read_after(run_id, 0)
    last: dict[str | None, str] = {}
    for ev in events:
        if ev.type == EventType.NODE_CREATED:
            last[ev.node_id] = ev.payload["status"]
        elif ev.type == EventType.NODE_STATUS:
            assert can_transition(last[ev.node_id], ev.payload["status"])
            last[ev.node_id] = ev.payload["status"]
    snap = await runs.snapshot(run_id)
    assert snap == await replay(runs.hub, run_id)
    return snap


async def io_of(runs: RunService, node_id: str) -> Any:
    return await runs.hub.store.fetch_one("SELECT * FROM node_io WHERE node_id = ?", (node_id,))


# ---- success ------------------------------------------------------------------------------------


async def test_a_valid_plan_becomes_the_task_tree(runs: RunService) -> None:
    provider = scripted([DIAMOND])
    run_id = await runs.create_run(PROMPT, provider="fake", model="fake-model")

    plan = await planner(runs, provider).plan(run_id)

    assert plan is not None
    assert [t.key for t in plan.tasks] == ["t1", "t2", "t3", "t4"]
    assert provider.call_count == 1

    snap = await assert_consistent(runs, run_id)
    # the planner leaves the run and the root for the executor to start
    assert (snap.run.status, snap.run.error, snap.run.title) == (
        "created",
        None,
        "Flower shop website",
    )
    by_path = {n.path: n for n in snap.nodes}
    assert sorted(by_path) == [
        "/t1",
        "/t1/s1",
        "/t1/t1",
        "/t1/t2",
        "/t1/t2/t1",
        "/t1/t2/t2",
        "/t1/t3",
        "/t1/t4",
    ]
    assert by_path["/t1"].status == "ready"
    assert by_path["/t1"].title == PROMPT  # the root keeps the shortened prompt

    step = by_path["/t1/s1"]
    assert (step.kind, step.step_type, step.title, step.actor, step.turn, step.status) == (
        "step",
        "llm",
        PLAN_STEP_TITLE,
        "planner",
        0,
        "done",
    )
    assert step.output_preview == "4 tasks: Task t1, Task t2, Task t3, Task t4"
    assert step.error is None

    tasks = {p: (n.title, n.status, n.depth, n.actor, n.objective) for p, n in by_path.items()}
    assert tasks["/t1/t1"] == ("Task t1", "ready", 1, "executor", "t1 is finished.")
    assert tasks["/t1/t2"] == ("Task t2", "blocked", 1, "executor", "t2 is finished.")
    assert tasks["/t1/t3"][1] == "blocked"
    assert tasks["/t1/t4"][1] == "blocked"
    assert tasks["/t1/t2/t1"] == ("Task t2a", "ready", 2, "executor", "t2a is finished.")
    assert tasks["/t1/t2/t2"] == ("Task t2b", "blocked", 2, "executor", "t2b is finished.")

    ids = {p: n.id for p, n in by_path.items()}
    assert {(e.task_id, e.depends_on_task_id) for e in snap.edges} == {
        (ids["/t1/t2"], ids["/t1/t1"]),
        (ids["/t1/t3"], ids["/t1/t1"]),
        (ids["/t1/t4"], ids["/t1/t2"]),
        (ids["/t1/t4"], ids["/t1/t3"]),
        (ids["/t1/t2/t2"], ids["/t1/t2/t1"]),
    }

    # the call is metered at list price and rolls up to the root
    expected = cost_micro(step.tokens_in, step.tokens_out, PRICES)
    assert step.tokens_in > 0
    assert expected > 0
    assert (step.spent_micro, by_path["/t1"].rollup_spent_micro) == (expected, expected)
    assert by_path["/t1"].rollup_calls == 1

    io = await io_of(runs, step.id)
    assert io["prompt"].startswith("[system]\nYou are the planner")
    assert f"[user]\n{PROMPT}" in io["prompt"]
    assert '"Flower shop website"' in io["response"]
    assert (io["model"], io["cached"], io["error"]) == ("fake-model", 0, None)


async def test_events_arrive_in_order_title_before_tasks(runs: RunService, hub: EventHub) -> None:
    run_id = await runs.create_run(PROMPT)
    await planner(runs, scripted([DIAMOND])).plan(run_id)
    types = [e.type for e in await hub.read_after(run_id, 0)]
    assert types == [
        EventType.RUN_CREATED,
        EventType.NODE_CREATED,  # root
        EventType.NODE_CREATED,  # "Plan the work", born running
        EventType.NODE_IO,
        EventType.NODE_COST,  # step
        EventType.NODE_COST,  # root rollup
        EventType.NODE_STATUS,  # step done
        EventType.RUN_TITLE,
        *[EventType.NODE_CREATED] * 6,
    ]


async def test_tasks_are_created_in_topological_order(runs: RunService) -> None:
    out_of_order = {
        "title": "Site",
        "tasks": [task("build", "copy", "style"), task("style"), task("copy")],
    }
    run_id = await runs.create_run(PROMPT)
    await planner(runs, scripted([out_of_order])).plan(run_id)
    snap = await assert_consistent(runs, run_id)
    created = [(n.title, n.status) for n in snap.nodes if n.kind == "task" and n.depth == 1]
    assert created == [("Task style", "ready"), ("Task copy", "ready"), ("Task build", "blocked")]


async def test_a_thought_summary_is_recorded_when_the_provider_has_one(runs: RunService) -> None:
    reply = LLMResponse(
        text=json.dumps(DIAMOND),
        parsed=DIAMOND,
        tokens_in=900,
        tokens_out=400,
        latency_ms=1200,
        model="gemini-x",
        finish_reason="STOP",
        cached=True,
        thought_summary="A site needs copy and a layout before the page.",
    )
    run_id = await runs.create_run(PROMPT)
    await planner(runs, scripted([reply])).plan(run_id)
    snap = await assert_consistent(runs, run_id)
    step = next(n for n in snap.nodes if n.kind == "step")
    assert step.thought_preview == "A site needs copy and a layout before the page."
    assert (step.tokens_in, step.tokens_out) == (900, 400)
    assert step.spent_micro == cost_micro(900, 400, PRICES)  # cached calls are still metered
    io = await io_of(runs, step.id)
    assert (io["cached"], io["latency_ms"], io["model"]) == (1, 1200, "gemini-x")
    assert io["thought"] == "A site needs copy and a layout before the page."


# ---- repair -------------------------------------------------------------------------------------


async def test_an_invalid_plan_is_repaired_once(runs: RunService) -> None:
    provider = scripted([CYCLE, DIAMOND])
    run_id = await runs.create_run(PROMPT)

    plan = await planner(runs, provider).plan(run_id)

    assert plan is not None
    assert provider.call_count == 2
    snap = await assert_consistent(runs, run_id)
    first, second = (n for n in snap.nodes if n.kind == "step")
    assert (first.title, first.status, first.turn, first.path) == (
        PLAN_STEP_TITLE,
        "failed",
        0,
        "/t1/s1",
    )
    assert first.error == "invalid plan: dependency cycle among: t1, t2, t3"
    assert (second.title, second.status, second.turn, second.path, second.actor) == (
        REPAIR_STEP_TITLE,
        "done",
        0,
        "/t1/s2",
        "planner",
    )
    assert second.error is None
    assert (await io_of(runs, first.id))["error"] == "dependency cycle among: t1, t2, t3"

    # the repair request carries the rejected answer and the validator's errors
    repair: LLMRequest = provider.calls[1]
    assert [m.role for m in repair.messages] == ["user", "model", "user"]
    assert '"Loop"' in repair.messages[1].text
    assert "- dependency cycle among: t1, t2, t3" in repair.messages[2].text

    # both calls are metered; only the repaired plan's tasks exist
    root = next(n for n in snap.nodes if n.depth == 0)
    assert root.rollup_spent_micro == first.spent_micro + second.spent_micro
    assert root.rollup_calls == 2
    assert first.spent_micro > 0
    assert len([n for n in snap.nodes if n.kind == "task"]) == 1 + 6
    assert (snap.run.status, snap.run.title) == ("created", "Flower shop website")


async def test_text_that_is_not_json_is_repaired(runs: RunService) -> None:
    provider = scripted(["Sure! Here is a plan: ...", DIAMOND])
    run_id = await runs.create_run(PROMPT)
    assert await planner(runs, provider).plan(run_id) is not None
    snap = await assert_consistent(runs, run_id)
    first = next(n for n in snap.nodes if n.kind == "step")
    assert first.error == "invalid plan: the answer was not a JSON object (finish reason: STOP)"
    assert provider.calls[1].messages[1].text == "Sure! Here is a plan: ..."


async def test_a_truncated_answer_is_treated_as_invalid(runs: RunService) -> None:
    cut = LLMResponse(
        text='{"title": "Flower shop website", "tasks": [{"key": "t1", "ti',
        parsed=None,
        tokens_in=500,
        tokens_out=8192,
        latency_ms=9000,
        model="gemini-x",
        finish_reason="MAX_TOKENS",
    )
    provider = scripted([cut, DIAMOND])
    run_id = await runs.create_run(PROMPT)
    assert await planner(runs, provider).plan(run_id) is not None
    snap = await assert_consistent(runs, run_id)
    first = next(n for n in snap.nodes if n.kind == "step")
    assert first.status == "failed"
    assert "cut off at the output limit" in (first.error or "")
    assert "return a shorter plan" in provider.calls[1].messages[2].text


# ---- failure ------------------------------------------------------------------------------------


async def test_invalid_twice_fails_the_run_with_the_errors(runs: RunService, hub: EventHub) -> None:
    too_few = {"title": "Tiny", "tasks": [task("t1"), task("t2", "t9")]}
    provider = scripted([CYCLE, too_few, DIAMOND])  # a third answer must never be asked for
    run_id = await runs.create_run(PROMPT)

    assert await planner(runs, provider).plan(run_id) is None

    assert provider.call_count == 2
    snap = await assert_consistent(runs, run_id)
    assert snap.run.status == "failed"
    assert snap.run.error == (
        "planner: plan still invalid after one repair:"
        " tasks: need 3 to 7 top-level tasks, got 2;"
        " task 't2': depends_on names an unknown key 't9'"
    )
    assert snap.run.title is None
    last = (await hub.read_after(run_id, 0))[-1]
    assert last.type == EventType.RUN_STATUS
    assert last.payload == {"status": "failed", "error": snap.run.error}
    # no scripted fallback: nothing but the root and the two planner steps
    assert [(n.kind, n.status) for n in snap.nodes] == [
        ("task", "ready"),
        ("step", "failed"),
        ("step", "failed"),
    ]
    assert snap.edges == []


async def test_a_provider_error_fails_the_run_without_a_repair(runs: RunService) -> None:
    provider = scripted([ProviderError("gemini 503 UNAVAILABLE: overloaded"), DIAMOND])
    run_id = await runs.create_run(PROMPT)

    assert await planner(runs, provider).plan(run_id) is None

    assert provider.call_count == 1
    snap = await assert_consistent(runs, run_id)
    assert snap.run.status == "failed"
    assert snap.run.error == "planner: model call failed: gemini 503 UNAVAILABLE: overloaded"
    (step,) = (n for n in snap.nodes if n.kind == "step")
    assert (step.status, step.error) == (
        "failed",
        "model call failed: gemini 503 UNAVAILABLE: overloaded",
    )
    assert step.spent_micro == 0


async def test_a_provider_error_during_the_repair_fails_the_run(runs: RunService) -> None:
    provider = scripted([CYCLE, ProviderError("replay miss")])
    run_id = await runs.create_run(PROMPT)
    assert await planner(runs, provider).plan(run_id) is None
    snap = await assert_consistent(runs, run_id)
    assert snap.run.error == "planner: model call failed: replay miss"
    assert [n.status for n in snap.nodes if n.kind == "step"] == ["failed", "failed"]


async def test_long_errors_are_clipped_on_the_run_and_the_event(
    runs: RunService, hub: EventHub
) -> None:
    noisy = {
        "title": "Noisy",
        "tasks": [task(f"t{i}", *(f"missing-{j}" for j in range(5))) for i in range(7)],
    }
    run_id = await runs.create_run(PROMPT)
    assert await planner(runs, scripted([noisy])).plan(run_id) is None
    snap = await assert_consistent(runs, run_id)
    assert len(snap.run.error or "") == 500
    assert all(len(e.model_dump_json()) < 2_000 for e in await hub.read_after(run_id, 0))
    step = next(n for n in snap.nodes if n.kind == "step")
    assert len((await io_of(runs, step.id))["error"].splitlines()) == 20  # full list, per node


async def test_unknown_run(runs: RunService) -> None:
    with pytest.raises(ValueError, match="unknown run"):
        await planner(runs, scripted([DIAMOND])).plan("run_nope")
