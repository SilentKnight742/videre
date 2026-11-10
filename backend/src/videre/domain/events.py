"""Event vocabulary and the pure reducer that rebuilds a snapshot from events.

No I/O here. The reducer is idempotent (every payload carries absolute values), so replaying an
event the snapshot already includes is harmless. That is what lets a client fetch a snapshot and
then stream from its `seq` without a lock between the two reads.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STATUS = "run.status"
    RUN_CLOSED = "run.closed"
    NODE_CREATED = "node.created"
    NODE_STATUS = "node.status"
    NODE_COST = "node.cost"


class Event(BaseModel):
    seq: int
    run_id: str
    ts: str
    type: str
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class NodeView(BaseModel):
    id: str
    parent_id: str | None
    kind: str
    title: str
    status: str
    path: str
    est_total_micro: int = 0
    spent_micro: int = 0
    rollup_spent_micro: int = 0
    rollup_calls: int = 0
    seq_created: int = 0


class EdgeView(BaseModel):
    task_id: str
    depends_on_task_id: str


class RunView(BaseModel):
    id: str
    scenario: str
    status: str


class Snapshot(BaseModel):
    """Everything a client needs to draw the graph, plus the seq to stream from."""

    run: RunView
    nodes: list[NodeView]
    edges: list[EdgeView]
    seq: int


def empty_snapshot(run_id: str) -> Snapshot:
    return Snapshot(
        run=RunView(id=run_id, scenario="", status="created"), nodes=[], edges=[], seq=0
    )


def apply_event(snap: Snapshot, ev: Event) -> Snapshot:
    """Return `snap` advanced by `ev`. Unknown event types only advance `seq`."""
    nodes = {n.id: n for n in snap.nodes}
    edges = {(e.task_id, e.depends_on_task_id): e for e in snap.edges}
    run = snap.run
    p = ev.payload

    match ev.type:
        case EventType.RUN_CREATED:
            run = run.model_copy(update={"scenario": p["scenario"], "status": p["status"]})
        case EventType.RUN_STATUS:
            run = run.model_copy(update={"status": p["status"]})
        case EventType.RUN_CLOSED:
            run = run.model_copy(update={"status": "closed"})
        case EventType.NODE_CREATED if ev.node_id:
            nodes[ev.node_id] = NodeView(
                id=ev.node_id,
                parent_id=p["parent_id"],
                kind=p["kind"],
                title=p["title"],
                status=p["status"],
                path=p["path"],
                est_total_micro=p["est_total_micro"],
                seq_created=ev.seq,
            )
            for dep in p.get("depends_on", []):
                edges[(ev.node_id, dep)] = EdgeView(task_id=ev.node_id, depends_on_task_id=dep)
        case EventType.NODE_STATUS if ev.node_id in nodes:
            nodes[ev.node_id] = nodes[ev.node_id].model_copy(update={"status": p["status"]})
        case EventType.NODE_COST if ev.node_id in nodes:
            nodes[ev.node_id] = nodes[ev.node_id].model_copy(
                update={
                    "spent_micro": p["spent_micro"],
                    "rollup_spent_micro": p["rollup_spent_micro"],
                    "rollup_calls": p["rollup_calls"],
                }
            )

    ordered = sorted(nodes.values(), key=lambda n: n.seq_created)
    return Snapshot(run=run, nodes=ordered, edges=list(edges.values()), seq=max(snap.seq, ev.seq))
