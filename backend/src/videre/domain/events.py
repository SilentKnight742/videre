"""Event vocabulary and the pure reducer that rebuilds a snapshot from events.

No I/O here. The reducer is idempotent (every payload carries absolute values), so replaying an
event the snapshot already includes is harmless. That is what lets a client fetch a snapshot and
then stream from its `seq` without a lock between the two reads.

Events and snapshots stay light: previews only. Full prompts, responses, thoughts and tool
results live in `node_io` and are served per node.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from videre.domain.states import NodeStatus

THOUGHT_EVENT_MAX = 500  # node.thought text
PREVIEW_MAX = 280  # node.io / node.output preview


def clip(text: str | None, limit: int) -> str | None:
    """Shorten `text` to at most `limit` characters, marking the cut with an ellipsis."""
    if text is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STATUS = "run.status"
    RUN_CLOSED = "run.closed"
    NODE_CREATED = "node.created"
    NODE_STATUS = "node.status"
    NODE_THOUGHT = "node.thought"
    NODE_IO = "node.io"
    NODE_OUTPUT = "node.output"
    NODE_COST = "node.cost"
    ARTIFACT_WRITTEN = "artifact.written"


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
    step_type: str | None = None
    title: str
    status: str
    path: str
    actor: str | None = None
    turn: int | None = None
    depth: int = 0
    objective: str | None = None
    thought_preview: str | None = None
    output_preview: str | None = None
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    est_total_micro: int = 0
    spent_micro: int = 0
    rollup_spent_micro: int = 0
    rollup_calls: int = 0
    seq_created: int = 0


class EdgeView(BaseModel):
    task_id: str
    depends_on_task_id: str


class ArtifactView(BaseModel):
    path: str
    bytes: int
    node_id: str | None = None


class RunView(BaseModel):
    id: str
    prompt: str
    title: str | None = None
    provider: str | None = None
    model: str | None = None
    status: str
    error: str | None = None


class Snapshot(BaseModel):
    """Everything a client needs to draw the graph, plus the seq to stream from."""

    run: RunView
    nodes: list[NodeView]
    edges: list[EdgeView]
    artifacts: list[ArtifactView] = Field(default_factory=list)
    seq: int


# A node.status reason is kept as the node's error only when it explains an end state.
ERROR_STATUSES = frozenset({NodeStatus.FAILED, NodeStatus.CANCELLED})


def empty_snapshot(run_id: str) -> Snapshot:
    return Snapshot(run=RunView(id=run_id, prompt="", status="created"), nodes=[], edges=[], seq=0)


def apply_event(snap: Snapshot, ev: Event) -> Snapshot:
    """Return `snap` advanced by `ev`. Unknown event types only advance `seq`."""
    nodes = {n.id: n for n in snap.nodes}
    edges = {(e.task_id, e.depends_on_task_id): e for e in snap.edges}
    artifacts = {a.path: a for a in snap.artifacts}
    run = snap.run
    p = ev.payload

    def update(node_id: str, **fields: Any) -> None:
        nodes[node_id] = nodes[node_id].model_copy(update=fields)

    match ev.type:
        case EventType.RUN_CREATED:
            run = run.model_copy(
                update={
                    "prompt": p["prompt"],
                    "title": p.get("title"),
                    "provider": p.get("provider"),
                    "model": p.get("model"),
                    "status": p["status"],
                }
            )
        case EventType.RUN_STATUS:
            fields = {"status": p["status"]}
            if p.get("error") is not None:
                fields["error"] = p["error"]
            run = run.model_copy(update=fields)
        case EventType.RUN_CLOSED:
            run = run.model_copy(update={"status": "closed"})
        case EventType.NODE_CREATED if ev.node_id:
            nodes[ev.node_id] = NodeView(
                id=ev.node_id,
                parent_id=p["parent_id"],
                kind=p["kind"],
                step_type=p.get("step_type"),
                title=p["title"],
                status=p["status"],
                path=p["path"],
                actor=p.get("actor"),
                turn=p.get("turn"),
                depth=p["depth"],
                objective=p.get("objective"),
                est_total_micro=p["est_total_micro"],
                seq_created=ev.seq,
            )
            for dep in p.get("depends_on", []):
                edges[(ev.node_id, dep)] = EdgeView(task_id=ev.node_id, depends_on_task_id=dep)
        case EventType.NODE_STATUS if ev.node_id in nodes:
            fields = {"status": p["status"]}
            if p.get("reason") is not None and p["status"] in ERROR_STATUSES:
                fields["error"] = p["reason"]
            update(ev.node_id, **fields)
        case EventType.NODE_THOUGHT if ev.node_id in nodes:
            update(ev.node_id, thought_preview=p["text"])
        case EventType.NODE_IO if ev.node_id in nodes:
            update(
                ev.node_id,
                tokens_in=p["tokens_in"],
                tokens_out=p["tokens_out"],
                output_preview=p.get("preview"),
            )
        case EventType.NODE_OUTPUT if ev.node_id in nodes:
            update(ev.node_id, output_preview=p["preview"])
        case EventType.NODE_COST if ev.node_id in nodes:
            update(
                ev.node_id,
                spent_micro=p["spent_micro"],
                rollup_spent_micro=p["rollup_spent_micro"],
                rollup_calls=p["rollup_calls"],
            )
        case EventType.ARTIFACT_WRITTEN:
            artifacts[p["path"]] = ArtifactView(
                path=p["path"], bytes=p["bytes"], node_id=p.get("node_id")
            )

    return Snapshot(
        run=run,
        nodes=sorted(nodes.values(), key=lambda n: n.seq_created),
        edges=list(edges.values()),
        artifacts=sorted(artifacts.values(), key=lambda a: a.path),
        seq=max(snap.seq, ev.seq),
    )
