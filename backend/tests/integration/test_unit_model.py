"""RunService enforces the unit model (SPEC-A2 sections 1-3) and emits the v2 events."""

import pytest

from videre.domain.events import (
    PREVIEW_MAX,
    THOUGHT_EVENT_MAX,
    EventType,
    apply_event,
    empty_snapshot,
)
from videre.domain.states import NodeKind, NodeStatus, RunStatus, StepType, can_transition
from videre.services.events import EventHub
from videre.services.runs import RunService, short_title
from videre.storage.db import Store

T, S = NodeKind.TASK, NodeKind.STEP


@pytest.fixture
def hub(store: Store) -> EventHub:
    return EventHub(store)


@pytest.fixture
def runs(hub: EventHub) -> RunService:
    return RunService(hub)


async def run_with_root(runs: RunService) -> tuple[str, str]:
    run_id = await runs.create_run("build a website for my flower shop", provider="fake")
    return run_id, await runs.root_id(run_id)


async def step(runs: RunService, run_id: str, parent: str, turn: int = 0, **kw: object) -> str:
    return await runs.add_node(
        run_id, S, "call", parent_id=parent, step_type=StepType.LLM, turn=turn, **kw
    )


async def replay(hub: EventHub, run_id: str):  # type: ignore[no-untyped-def]
    snap = empty_snapshot(run_id)
    for ev in await hub.read_after(run_id, 0):
        snap = apply_event(snap, ev)
    return snap


# ---- run creation -----------------------------------------------------------------------------


async def test_create_run_makes_a_root_task(runs: RunService, hub: EventHub) -> None:
    run_id, root = await run_with_root(runs)
    events = await hub.read_after(run_id, 0)
    assert [e.type for e in events] == [EventType.RUN_CREATED, EventType.NODE_CREATED]
    assert events[0].payload["source"] == "native"
    assert events[0].payload["prompt"] == "build a website for my flower shop"
    snap = await runs.snapshot(run_id)
    (node,) = snap.nodes
    assert (node.id, node.kind, node.depth, node.path, node.actor, node.status) == (
        root,
        "task",
        0,
        "/t1",
        "planner",
        "ready",
    )
    assert node.title == "build a website for my flower shop"
    assert snap.run.provider == "fake"
    assert snap == await replay(hub, run_id)


async def test_empty_prompt_is_rejected(runs: RunService) -> None:
    with pytest.raises(ValueError, match="empty"):
        await runs.create_run("   ")


def test_short_title() -> None:
    assert short_title("  hello\n  world ") == "hello"
    long = short_title("x" * 100)
    assert len(long) == 60
    assert long.endswith("…")


# ---- tree shape -------------------------------------------------------------------------------


async def test_task_depth_is_at_most_three(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    d1 = await runs.add_node(run_id, T, "d1", parent_id=root)
    d2 = await runs.add_node(run_id, T, "d2", parent_id=d1)
    d3 = await runs.add_node(run_id, T, "d3", parent_id=d2)
    with pytest.raises(ValueError, match="at most depth 3"):
        await runs.add_node(run_id, T, "d4", parent_id=d3)
    s = await step(runs, run_id, d3)  # a step sits one level below its task
    snap = await runs.snapshot(run_id)
    assert {n.id: n.depth for n in snap.nodes}[s] == 4


async def test_a_task_holds_child_tasks_or_steps_not_both(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    leaf = await runs.add_node(run_id, T, "leaf", parent_id=root)
    await step(runs, run_id, leaf)
    with pytest.raises(ValueError, match="leaf"):
        await runs.add_node(run_id, T, "child", parent_id=leaf)

    composite = await runs.add_node(run_id, T, "composite", parent_id=root)
    await runs.add_node(run_id, T, "child", parent_id=composite)
    with pytest.raises(ValueError, match="composite"):
        await step(runs, run_id, composite)


async def test_root_holds_planner_steps_and_child_tasks(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    plan = await step(runs, run_id, root, actor="planner")
    task = await runs.add_node(run_id, T, "t", parent_id=root)
    with pytest.raises(ValueError, match="planner"):
        await step(runs, run_id, root, actor="executor")
    paths = {n.id: n.path for n in (await runs.snapshot(run_id)).nodes}
    assert (paths[plan], paths[task]) == ("/t1/s1", "/t1/t1")


async def test_steps_cannot_have_children_or_dependencies(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    leaf = await runs.add_node(run_id, T, "leaf", parent_id=root)
    s = await step(runs, run_id, leaf)
    with pytest.raises(ValueError, match="only tasks have children"):
        await runs.add_node(run_id, T, "x", parent_id=s)
    with pytest.raises(ValueError, match="no dependencies"):
        await step(runs, run_id, leaf, depends_on=[leaf])


async def test_step_and_task_fields_are_checked(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    leaf = await runs.add_node(run_id, T, "leaf", parent_id=root)
    with pytest.raises(ValueError, match="step_type"):
        await runs.add_node(run_id, S, "s", parent_id=leaf, turn=0)
    with pytest.raises(ValueError, match="turn"):
        await runs.add_node(run_id, S, "s", parent_id=leaf, step_type=StepType.TOOL)
    with pytest.raises(ValueError, match="only steps"):
        await runs.add_node(run_id, T, "t", parent_id=root, step_type=StepType.LLM)
    with pytest.raises(ValueError, match="only steps"):
        await runs.add_node(run_id, T, "t", parent_id=root, turn=1)
    with pytest.raises(ValueError, match="title"):
        await runs.add_node(run_id, T, " ", parent_id=root)
    with pytest.raises(ValueError, match="cannot start as"):
        await runs.add_node(run_id, T, "t", parent_id=root, status=NodeStatus.DONE)
    with pytest.raises(ValueError, match="unknown parent"):
        await runs.add_node(run_id, T, "t", parent_id="nope")


async def test_parallel_tool_steps_share_a_turn(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    leaf = await runs.add_node(run_id, T, "leaf", parent_id=root)
    await step(runs, run_id, leaf, turn=0)
    for _ in range(2):
        await runs.add_node(
            run_id, S, "write_file", parent_id=leaf, step_type=StepType.TOOL, turn=0
        )
    await step(runs, run_id, leaf, turn=1)
    nodes = [n for n in (await runs.snapshot(run_id)).nodes if n.parent_id == leaf]
    assert [(n.step_type, n.turn, n.path[-2:]) for n in nodes] == [
        ("llm", 0, "s1"),
        ("tool", 0, "s2"),
        ("tool", 0, "s3"),
        ("llm", 1, "s4"),
    ]


# ---- dependencies -----------------------------------------------------------------------------


async def test_dependencies_must_be_sibling_tasks(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    a = await runs.add_node(run_id, T, "a", parent_id=root)
    b = await runs.add_node(run_id, T, "b", parent_id=root)
    a1 = await runs.add_node(run_id, T, "a1", parent_id=a)
    b1 = await runs.add_node(run_id, T, "b1", parent_id=b)
    s = await step(runs, run_id, b1)

    c = await runs.add_node(run_id, T, "c", parent_id=root, depends_on=[a, b, a])
    with pytest.raises(ValueError, match="not a sibling"):  # cross-branch
        await runs.add_node(run_id, T, "b2", parent_id=b, depends_on=[a1])
    with pytest.raises(ValueError, match="not a sibling"):  # parent, not sibling
        await runs.add_node(run_id, T, "a2", parent_id=a, depends_on=[a])
    with pytest.raises(ValueError, match="not a sibling"):  # a step
        await runs.add_node(run_id, T, "b2", parent_id=b, depends_on=[s])
    with pytest.raises(ValueError, match="unknown dependency"):
        await runs.add_node(run_id, T, "d", parent_id=root, depends_on=["nope"])
    a2 = await runs.add_node(run_id, T, "a2", parent_id=a, depends_on=[a1])

    snap = await runs.snapshot(run_id)
    assert {(e.task_id, e.depends_on_task_id) for e in snap.edges} == {(c, a), (c, b), (a2, a1)}
    status = {n.id: n.status for n in snap.nodes}
    assert (status[c], status[a2], status[a]) == ("blocked", "blocked", "ready")


# ---- transitions ------------------------------------------------------------------------------


async def test_illegal_transitions_are_rejected_and_emit_nothing(
    runs: RunService, hub: EventHub
) -> None:
    run_id, root = await run_with_root(runs)
    a = await runs.add_node(run_id, T, "a", parent_id=root)
    before = await hub.read_after(run_id, 0)
    for bad in (NodeStatus.DONE, NodeStatus.BLOCKED, NodeStatus.PAUSED_BUDGET):
        with pytest.raises(ValueError, match="illegal node transition ready ->"):
            await runs.set_node_status(a, bad)
    assert await hub.read_after(run_id, 0) == before

    await runs.set_node_status(a, NodeStatus.RUNNING)
    await runs.set_node_status(a, NodeStatus.FAILED, reason="invalid JSON twice")
    with pytest.raises(ValueError, match="failed -> running"):
        await runs.set_node_status(a, NodeStatus.RUNNING)
    with pytest.raises(ValueError, match="unknown node"):
        await runs.set_node_status("nope", NodeStatus.RUNNING)


async def test_reason_becomes_error_only_for_end_states(runs: RunService, hub: EventHub) -> None:
    run_id, root = await run_with_root(runs)
    a = await runs.add_node(run_id, T, "a", parent_id=root)
    b = await runs.add_node(run_id, T, "b", parent_id=root, depends_on=[a])
    await runs.set_node_status(a, NodeStatus.RUNNING, reason="scheduled")
    await runs.set_node_status(a, NodeStatus.FAILED, reason="tool exploded")
    await runs.set_node_status(b, NodeStatus.CANCELLED, reason="dependency failed: a")
    nodes = {n.id: n for n in (await runs.snapshot(run_id)).nodes}
    assert nodes[a].error == "tool exploded"
    assert nodes[b].error == "dependency failed: a"
    ev = (await hub.read_after(run_id, 0))[-1]
    assert ev.payload == {"status": "cancelled", "reason": "dependency failed: a"}
    assert await runs.snapshot(run_id) == await replay(hub, run_id)


async def test_run_status_carries_an_error(runs: RunService, hub: EventHub) -> None:
    run_id, _ = await run_with_root(runs)
    await runs.set_run_status(run_id, RunStatus.RUNNING)
    await runs.set_run_status(run_id, RunStatus.FAILED, error="plan invalid twice: cycle t1->t2")
    snap = await runs.snapshot(run_id)
    assert (snap.run.status, snap.run.error) == ("failed", "plan invalid twice: cycle t1->t2")
    assert snap == await replay(hub, run_id)


# ---- content events and full replay ----------------------------------------------------------


async def test_every_event_type_replays_to_the_snapshot(runs: RunService, hub: EventHub) -> None:
    run_id, root = await run_with_root(runs)
    await runs.set_run_status(run_id, RunStatus.RUNNING)
    await runs.set_node_status(root, NodeStatus.RUNNING)
    leaf = await runs.add_node(
        run_id, T, "Write page", parent_id=root, objective="index.html exists", actor="planner"
    )
    await runs.set_node_status(leaf, NodeStatus.RUNNING)
    llm = await step(runs, run_id, leaf, actor="executor", status=NodeStatus.RUNNING)
    await runs.record_thought(llm, "t" * 900)
    await runs.record_io(
        llm,
        model="gemini-x",
        prompt="full prompt",
        response="r" * 5_000,
        preview="r" * 5_000,
        tokens_in=120,
        tokens_out=45,
        latency_ms=800,
        cached=True,
    )
    await runs.record_cost(llm, 300)
    await runs.set_node_status(llm, NodeStatus.DONE)
    tool = await runs.add_node(
        run_id,
        S,
        "write_file",
        parent_id=leaf,
        step_type=StepType.TOOL,
        turn=0,
        actor="executor",
        status=NodeStatus.RUNNING,
    )
    await runs.record_io(
        tool,
        tool_name="write_file",
        tool_args={"path": "index.html", "content": "<html>"},
        tool_result="wrote 6 bytes",
        preview="wrote index.html",
    )
    await runs.record_artifact(run_id, "index.html", size=6, sha256="ab" * 32, node_id=tool)
    await runs.record_artifact(run_id, "index.html", size=9, sha256="cd" * 32, node_id=tool)
    await runs.set_node_status(tool, NodeStatus.DONE)
    await runs.record_output(leaf, "the whole page", "Wrote index.html")
    await runs.set_node_status(leaf, NodeStatus.DONE)
    await runs.set_node_status(root, NodeStatus.DONE)
    await runs.set_run_status(run_id, RunStatus.COMPLETED)

    events = await hub.read_after(run_id, 0)
    types = {e.type for e in events} | {EventType.RUN_CLOSED}  # closing is phase B
    assert types == set(EventType)

    snap = await runs.snapshot(run_id)
    assert snap == await replay(hub, run_id)
    nodes = {n.id: n for n in snap.nodes}
    assert len(nodes[llm].thought_preview or "") == THOUGHT_EVENT_MAX
    assert len(nodes[llm].output_preview or "") == PREVIEW_MAX
    assert (nodes[llm].tokens_in, nodes[llm].tokens_out) == (120, 45)
    assert nodes[tool].output_preview == "wrote index.html"
    assert nodes[leaf].output_preview == "Wrote index.html"
    assert nodes[leaf].objective == "index.html exists"
    assert nodes[root].rollup_spent_micro == 300
    assert [(a.path, a.bytes, a.node_id) for a in snap.artifacts] == [("index.html", 9, tool)]
    assert snap.run.status == "completed"

    # heavy text stays out of events, but is kept in node_io
    io = await runs.hub.store.fetch_one(
        "SELECT thought, response, cached, tool_args_json FROM node_io WHERE node_id = ?", (llm,)
    )
    assert len(io["thought"]) == 900
    assert len(io["response"]) == 5_000
    assert io["cached"] == 1
    assert all(len(e.model_dump_json()) < 2_000 for e in events)
    out = await runs.hub.store.fetch_one("SELECT response FROM node_io WHERE node_id = ?", (leaf,))
    assert out["response"] == "the whole page"

    # no illegal transition was ever emitted (SPEC-A2 acceptance 5)
    last: dict[str, str] = {}
    for ev in events:
        if ev.type == EventType.NODE_CREATED:
            last[ev.node_id] = ev.payload["status"]  # type: ignore[index]
        elif ev.type == EventType.NODE_STATUS:
            assert can_transition(last[ev.node_id], ev.payload["status"])  # type: ignore[index]
            last[ev.node_id] = ev.payload["status"]  # type: ignore[index]


async def test_io_and_output_are_kind_specific(runs: RunService) -> None:
    run_id, root = await run_with_root(runs)
    leaf = await runs.add_node(run_id, T, "leaf", parent_id=root)
    s = await step(runs, run_id, leaf)
    with pytest.raises(ValueError, match="steps"):
        await runs.record_io(leaf, preview="x")
    with pytest.raises(ValueError, match="tasks"):
        await runs.record_output(s, "x", "x")
    with pytest.raises(ValueError, match="non-negative"):
        await runs.record_io(s, preview="x", tokens_in=-1)
    with pytest.raises(ValueError, match="non-negative"):
        await runs.record_cost(s, -1)
