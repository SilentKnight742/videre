import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from videre.api.runs import event_stream
from videre.domain.events import Event, EventType, apply_event, empty_snapshot
from videre.domain.states import NodeKind, NodeStatus, RunStatus
from videre.services.events import EventHub
from videre.services.runs import RunService
from videre.storage.db import Store


@pytest.fixture
def hub(store: Store) -> EventHub:
    return EventHub(store)


@pytest.fixture
def runs(hub: EventHub) -> RunService:
    return RunService(hub)


async def script_run(runs: RunService) -> str:
    """A small scripted run: two tasks (b depends on a), subtasks, calls, costs, close."""
    run_id = await runs.create_run("flower shop site")
    a = await runs.add_node(run_id, NodeKind.TASK, "Research", est_total_micro=5_000)
    b = await runs.add_node(
        run_id, NodeKind.TASK, "Build page", depends_on=[a], est_total_micro=9_000
    )
    sa = await runs.add_node(run_id, NodeKind.SUBTASK, "Survey florists", parent_id=a)
    ca = await runs.add_node(run_id, NodeKind.CALL, "gemini", parent_id=sa)
    await runs.set_run_status(run_id, RunStatus.RUNNING)
    await runs.set_node_status(a, NodeStatus.RUNNING)
    await runs.record_cost(ca, 1_200)
    await runs.record_cost(ca, 800)
    await runs.set_node_status(a, NodeStatus.DONE)
    await runs.set_node_status(b, NodeStatus.READY)
    await runs.set_run_status(run_id, RunStatus.CLOSED)
    return run_id


async def collect(stream: AsyncIterator[str]) -> list[Event]:
    events = []
    async for frame in stream:
        if frame.startswith(":"):
            continue
        data = next(line for line in frame.splitlines() if line.startswith("data: "))
        events.append(Event(**json.loads(data[6:])))
    return events


async def test_subscriber_sees_events_in_order_and_replay_equals_snapshot(
    hub: EventHub, runs: RunService
) -> None:
    run_id = await runs.create_run("x")  # stream needs a run to exist first
    reader = asyncio.create_task(collect(event_stream(hub, run_id, after=0)))
    await asyncio.sleep(0)

    a = await runs.add_node(run_id, NodeKind.TASK, "Research", est_total_micro=5_000)
    b = await runs.add_node(run_id, NodeKind.TASK, "Build", depends_on=[a])
    s = await runs.add_node(run_id, NodeKind.SUBTASK, "Survey", parent_id=a)
    c = await runs.add_node(run_id, NodeKind.CALL, "gemini", parent_id=s)
    await runs.set_node_status(a, NodeStatus.RUNNING)
    await runs.record_cost(c, 1_200)
    await runs.record_cost(c, 800)
    await runs.set_node_status(a, NodeStatus.DONE)
    await runs.set_node_status(b, NodeStatus.READY)
    await runs.set_run_status(run_id, RunStatus.CLOSED)

    events = await asyncio.wait_for(reader, timeout=5)
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    types = [e.type for e in events]
    assert types[:5] == [
        EventType.RUN_CREATED,
        EventType.NODE_CREATED,
        EventType.NODE_CREATED,
        EventType.NODE_CREATED,
        EventType.NODE_CREATED,
    ]
    assert types[-1] == EventType.RUN_CLOSED
    # a cost on the call is rolled up call -> subtask -> task: three events per record_cost
    assert types.count(EventType.NODE_COST) == 6

    snap = await runs.snapshot(run_id)
    rebuilt = empty_snapshot(run_id)
    for ev in events:
        rebuilt = apply_event(rebuilt, ev)
    assert rebuilt == snap
    nodes = {n.id: n for n in snap.nodes}
    assert nodes[a].rollup_spent_micro == 2_000
    assert nodes[a].rollup_calls == 2
    assert nodes[a].spent_micro == 0
    assert nodes[c].spent_micro == 2_000
    assert [(e.task_id, e.depends_on_task_id) for e in snap.edges] == [(b, a)]
    assert nodes[s].path == f"{nodes[a].path}/s1"


async def test_replay_is_idempotent_for_overlap(runs: RunService, hub: EventHub) -> None:
    run_id = await script_run(runs)
    events = await hub.read_after(run_id, 0)
    once = empty_snapshot(run_id)
    for ev in events:
        once = apply_event(once, ev)
    twice = once
    for ev in events:  # replaying events the snapshot already includes changes nothing
        twice = apply_event(twice, ev)
    assert twice == once == await runs.snapshot(run_id)


async def test_resume_after_seq_replays_only_the_tail(runs: RunService, hub: EventHub) -> None:
    run_id = await script_run(runs)
    everything = await collect(event_stream(hub, run_id, after=0))
    midpoint = everything[len(everything) // 2].seq
    tail = await collect(event_stream(hub, run_id, after=midpoint))
    assert [e.seq for e in tail] == [e.seq for e in everything if e.seq > midpoint]


async def test_rolled_back_change_emits_nothing(runs: RunService, hub: EventHub) -> None:
    run_id = await runs.create_run("x")
    async with hub.subscribe(run_id) as queue:
        with pytest.raises(RuntimeError, match="boom"):
            async with hub.transaction() as tx:
                await tx.emit(run_id, EventType.RUN_STATUS, status="running")
                raise RuntimeError("boom")
        assert queue.empty()
    assert [e.type for e in await hub.read_after(run_id, 0)] == [EventType.RUN_CREATED]


async def test_keepalive_when_idle(runs: RunService, hub: EventHub) -> None:
    run_id = await runs.create_run("x")
    stream = event_stream(hub, run_id, after=1, keepalive_s=0.01)
    assert await anext(stream) == ": keepalive\n\n"
    await stream.aclose()


async def test_invalid_graph_writes_are_rejected(runs: RunService) -> None:
    run_id = await runs.create_run("x")
    t = await runs.add_node(run_id, NodeKind.TASK, "t")
    with pytest.raises(ValueError, match="unknown dependency"):
        await runs.add_node(run_id, NodeKind.TASK, "u", depends_on=["nope"])
    with pytest.raises(ValueError, match="only tasks"):
        await runs.add_node(run_id, NodeKind.SUBTASK, "s", parent_id=t, depends_on=[t])
    with pytest.raises(ValueError, match="non-negative"):
        await runs.record_cost(t, -1)


def test_http_snapshot_and_stream(client: TestClient) -> None:
    runs: RunService = client.app.state.runs  # type: ignore[attr-defined]
    run_id = client.portal.call(script_run, runs)  # type: ignore[union-attr]

    snap = client.get(f"/api/runs/{run_id}").json()
    assert snap["run"]["status"] == "closed"
    assert len(snap["nodes"]) == 4 and snap["seq"] > 0

    with client.stream("GET", f"/api/runs/{run_id}/stream") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert body.startswith("id: 1\nevent: run.created\n")
    assert body.rstrip().endswith(f"event: run.closed\ndata: {body.rsplit('data: ', 1)[1].strip()}")

    # resume via query param and via Last-Event-ID both skip what the client already has
    with client.stream("GET", f"/api/runs/{run_id}/stream?after={snap['seq'] - 1}") as r:
        assert r.read().decode().count("event: ") == 1
    headers = {"Last-Event-ID": str(snap["seq"] - 1)}
    with client.stream("GET", f"/api/runs/{run_id}/stream", headers=headers) as r:
        assert r.read().decode().count("event: ") == 1


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/stream").status_code == 404
    assert client.get("/api/runs/x/stream?after=-1").status_code == 422
