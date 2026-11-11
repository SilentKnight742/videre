"""Run and node writes. Every change emits its event in the same transaction.

This is the only writer of run/node state, so it is where the unit model (SPEC-A2 section 1,
ADR 0005) is enforced: tree shape, sibling-only dependencies and legal status transitions.
"""

import json
import uuid
from typing import Any

from fastapi import HTTPException

from videre.domain.events import (
    ERROR_STATUSES,
    PREVIEW_MAX,
    THOUGHT_EVENT_MAX,
    ArtifactView,
    EdgeView,
    EventType,
    NodeView,
    RunView,
    Snapshot,
    clip,
)
from videre.domain.states import (
    MAX_TASK_DEPTH,
    TERMINAL,
    NodeKind,
    NodeStatus,
    RunStatus,
    StepType,
    check_transition,
)
from videre.services.events import EventHub, Tx

_PATH_PREFIX = {NodeKind.TASK: "t", NodeKind.STEP: "s"}
# A node may be born in any non-terminal state except a pause (steps are created `running`).
_INITIAL = frozenset(
    {NodeStatus.BLOCKED, NodeStatus.WAITING_FUNDS, NodeStatus.READY, NodeStatus.RUNNING}
)
ROOT_TITLE_MAX = 60
# Actors: who executes a node.
PLANNER = "planner"
EXECUTOR = "executor"

_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def short_title(prompt: str, limit: int = ROOT_TITLE_MAX) -> str:
    """First line of the prompt, collapsed and cut to `limit` characters."""
    first = " ".join(prompt.strip().splitlines()[0].split()) if prompt.strip() else ""
    return clip(first, limit) or "Run"


async def _one(tx: Tx, sql: str, params: tuple[Any, ...]) -> Any:
    rows = await tx.conn.execute_fetchall(sql, params)
    return rows[0] if rows else None


class RunService:
    def __init__(self, hub: EventHub) -> None:
        self.hub = hub

    # ---- runs -------------------------------------------------------------------------------

    async def create_run(
        self,
        prompt: str,
        *,
        title: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> str:
        """Create a run and its root task (depth 0, actor `planner`) in one transaction."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        run_id = _new_id("run")
        root_title = short_title(prompt)
        async with self.hub.transaction() as tx:
            await tx.conn.execute(
                "INSERT INTO runs (id, prompt, title, provider, model, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, prompt, title, provider, model, RunStatus.CREATED),
            )
            await tx.emit(
                run_id,
                EventType.RUN_CREATED,
                prompt=prompt,
                title=title,
                provider=provider,
                model=model,
                source="native",
                status=RunStatus.CREATED,
            )
            await self._insert_node(
                tx,
                run_id,
                NodeKind.TASK,
                root_title,
                parent=None,
                status=NodeStatus.READY,
                actor=PLANNER,
                objective=prompt,
            )
        return run_id

    async def root_id(self, run_id: str) -> str:
        row = await self.hub.store.fetch_one(
            "SELECT id FROM nodes WHERE run_id = ? AND parent_id IS NULL", (run_id,)
        )
        if row is None:
            raise ValueError(f"unknown run {run_id}")
        return row["id"]

    async def prompt(self, run_id: str) -> str:
        row = await self.hub.store.fetch_one("SELECT prompt FROM runs WHERE id = ?", (run_id,))
        if row is None:
            raise ValueError(f"unknown run {run_id}")
        return row["prompt"]

    async def set_run_status(
        self, run_id: str, status: RunStatus, *, error: str | None = None
    ) -> None:
        async with self.hub.transaction() as tx:
            if status == RunStatus.CLOSED:
                await tx.conn.execute(
                    f"UPDATE runs SET status = ?, closed_at = {_NOW} WHERE id = ?",  # noqa: S608
                    (status, run_id),
                )
                await tx.emit(run_id, EventType.RUN_CLOSED)
                return
            await tx.conn.execute(
                "UPDATE runs SET status = ?, error = COALESCE(?, error) WHERE id = ?",
                (status, error, run_id),
            )
            extra = {"error": error} if error is not None else {}
            await tx.emit(run_id, EventType.RUN_STATUS, status=status, **extra)

    async def set_run_title(self, run_id: str, title: str) -> None:
        """Name the run (the planner does this from its plan). Emits `run.title`.

        The title is collapsed to one line and cut to 60 characters; the root task keeps its
        own title (the shortened prompt).
        """
        clean = clip(" ".join(title.split()), ROOT_TITLE_MAX)
        if not clean:
            raise ValueError("title must not be empty")
        async with self.hub.transaction() as tx:
            cur = await tx.conn.execute("UPDATE runs SET title = ? WHERE id = ?", (clean, run_id))
            if cur.rowcount == 0:
                raise ValueError(f"unknown run {run_id}")
            await tx.emit(run_id, EventType.RUN_TITLE, title=clean)

    # ---- nodes ------------------------------------------------------------------------------

    async def add_node(
        self,
        run_id: str,
        kind: NodeKind,
        title: str,
        *,
        parent_id: str,
        step_type: StepType | None = None,
        actor: str | None = None,
        turn: int | None = None,
        objective: str | None = None,
        est_total_micro: int = 0,
        depends_on: list[str] | None = None,
        status: NodeStatus | None = None,
    ) -> str:
        """Add a task or step under `parent_id` (the root task comes from `create_run`).

        Raises ValueError when the node would break the unit model: tasks at most depth 3;
        dependencies only between sibling tasks; a task holds child tasks or steps, never both
        (the root may also hold planner steps); steps have a step_type and a turn.
        """
        depends_on = list(dict.fromkeys(depends_on or []))
        if not title.strip():
            raise ValueError("title must not be empty")
        if kind == NodeKind.STEP:
            if step_type is None:
                raise ValueError("a step needs a step_type")
            if turn is None or turn < 0:
                raise ValueError("a step needs a turn >= 0")
            if depends_on:
                raise ValueError("steps have no dependencies; order them by turn")
        elif step_type is not None or turn is not None:
            raise ValueError("only steps have a step_type or a turn")
        status = status or (NodeStatus.BLOCKED if depends_on else NodeStatus.READY)
        if status not in _INITIAL:
            raise ValueError(f"a node cannot start as {status}")

        async with self.hub.transaction() as tx:
            parent = await _one(
                tx,
                "SELECT id, kind, depth, path, parent_id FROM nodes WHERE id = ? AND run_id = ?",
                (parent_id, run_id),
            )
            if parent is None:
                raise ValueError(f"unknown parent {parent_id}")
            if parent["kind"] != NodeKind.TASK:
                raise ValueError("only tasks have children")
            is_root = parent["parent_id"] is None
            child_kinds = {
                r["kind"]
                for r in await tx.conn.execute_fetchall(
                    "SELECT DISTINCT kind FROM nodes WHERE parent_id = ?", (parent_id,)
                )
            }
            if kind == NodeKind.TASK:
                if parent["depth"] + 1 > MAX_TASK_DEPTH:
                    raise ValueError(f"tasks may be at most depth {MAX_TASK_DEPTH}")
                if NodeKind.STEP in child_kinds and not is_root:
                    raise ValueError("a task with steps (a leaf) cannot get child tasks")
            else:
                if is_root and actor != PLANNER:
                    raise ValueError("only planner steps may sit directly under the root task")
                if NodeKind.TASK in child_kinds and not is_root:
                    raise ValueError("a task with child tasks (composite) cannot get steps")
            for dep in depends_on:
                hit = await _one(
                    tx,
                    "SELECT kind, parent_id FROM nodes WHERE id = ? AND run_id = ?",
                    (dep, run_id),
                )
                if hit is None:
                    raise ValueError(f"unknown dependency {dep}")
                if hit["kind"] != NodeKind.TASK or hit["parent_id"] != parent_id:
                    raise ValueError(f"dependency {dep} is not a sibling task")
            return await self._insert_node(
                tx,
                run_id,
                kind,
                title,
                parent=parent,
                status=status,
                step_type=step_type,
                actor=actor,
                turn=turn,
                objective=objective,
                est_total_micro=est_total_micro,
                depends_on=depends_on,
            )

    async def _insert_node(
        self,
        tx: Tx,
        run_id: str,
        kind: NodeKind,
        title: str,
        *,
        parent: Any,
        status: NodeStatus,
        step_type: StepType | None = None,
        actor: str | None = None,
        turn: int | None = None,
        objective: str | None = None,
        est_total_micro: int = 0,
        depends_on: list[str] | None = None,
    ) -> str:
        depends_on = depends_on or []
        parent_id = parent["id"] if parent is not None else None
        parent_path = parent["path"] if parent is not None else ""
        depth = parent["depth"] + 1 if parent is not None else 0
        count = await _one(
            tx,
            "SELECT COUNT(*) AS n FROM nodes WHERE run_id = ? AND parent_id IS ? AND kind = ?",
            (run_id, parent_id, kind),
        )
        path = f"{parent_path}/{_PATH_PREFIX[kind]}{count['n'] + 1}"
        node_id = _new_id("n")
        ev = await tx.emit(
            run_id,
            EventType.NODE_CREATED,
            node_id,
            parent_id=parent_id,
            kind=kind,
            step_type=step_type,
            title=title,
            status=status,
            path=path,
            actor=actor,
            turn=turn,
            depth=depth,
            objective=objective,
            est_total_micro=est_total_micro,
            depends_on=depends_on,
        )
        await tx.conn.execute(
            "INSERT INTO nodes (id, run_id, parent_id, kind, step_type, title, status, path,"
            " actor, turn, depth, objective, est_total_micro, seq_created, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
            " CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END)",
            (
                node_id,
                run_id,
                parent_id,
                kind,
                step_type,
                title,
                status,
                path,
                actor,
                turn,
                depth,
                objective,
                est_total_micro,
                ev.seq,
                status == NodeStatus.RUNNING,
            ),
        )
        for dep in depends_on:
            await tx.conn.execute(
                "INSERT INTO edges (run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                (run_id, node_id, dep),
            )
        return node_id

    async def set_node_status(
        self, node_id: str, status: NodeStatus, *, reason: str | None = None
    ) -> None:
        """Change a node's status. Raises ValueError for a transition the table forbids.

        `reason` travels on the event; for failed/cancelled it is also kept as the node's error.
        """
        async with self.hub.transaction() as tx:
            row = await _one(tx, "SELECT run_id, status FROM nodes WHERE id = ?", (node_id,))
            if row is None:
                raise ValueError(f"unknown node {node_id}")
            check_transition(row["status"], status)
            error = reason if status in ERROR_STATUSES else None
            stamps = ""
            if status == NodeStatus.RUNNING:
                stamps = f", started_at = COALESCE(started_at, {_NOW})"
            elif status in TERMINAL:
                stamps = f", finished_at = {_NOW}"
            await tx.conn.execute(
                f"UPDATE nodes SET status = ?, error = COALESCE(?, error){stamps}"  # noqa: S608
                " WHERE id = ?",
                (status, error, node_id),
            )
            extra = {"reason": reason} if reason is not None else {}
            await tx.emit(row["run_id"], EventType.NODE_STATUS, node_id, status=status, **extra)

    # ---- node content -----------------------------------------------------------------------

    async def _upsert_io(self, tx: Tx, node_id: str, fields: dict[str, Any]) -> None:
        cols = list(fields)
        await tx.conn.execute(
            f"INSERT INTO node_io (node_id, {', '.join(cols)})"  # noqa: S608 - column names are ours
            f" VALUES (?{', ?' * len(cols)})"
            f" ON CONFLICT(node_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}",
            (node_id, *fields.values()),
        )

    async def _node(self, tx: Tx, node_id: str) -> Any:
        row = await _one(tx, "SELECT run_id, kind FROM nodes WHERE id = ?", (node_id,))
        if row is None:
            raise ValueError(f"unknown node {node_id}")
        return row

    async def record_thought(self, node_id: str, text: str) -> None:
        """Full thought to node_io; a <= 500-char preview to the node and the event."""
        async with self.hub.transaction() as tx:
            node = await self._node(tx, node_id)
            preview = clip(text, THOUGHT_EVENT_MAX)
            await self._upsert_io(tx, node_id, {"thought": text})
            await tx.conn.execute(
                "UPDATE nodes SET thought_preview = ? WHERE id = ?", (preview, node_id)
            )
            await tx.emit(node["run_id"], EventType.NODE_THOUGHT, node_id, text=preview)

    async def record_io(
        self,
        node_id: str,
        *,
        preview: str | None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int | None = None,
        cached: bool = False,
        model: str | None = None,
        prompt: str | None = None,
        response: str | None = None,
        tool_name: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result: str | None = None,
        error: str | None = None,
    ) -> None:
        """Store a step's request/response and emit `node.io` with a <= 280-char preview."""
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("token counts must be non-negative")
        async with self.hub.transaction() as tx:
            node = await self._node(tx, node_id)
            if node["kind"] != NodeKind.STEP:
                raise ValueError("node.io is for steps; tasks use record_output")
            short = clip(preview, PREVIEW_MAX)
            await self._upsert_io(
                tx,
                node_id,
                {
                    "model": model,
                    "prompt": prompt,
                    "response": response,
                    "tool_name": tool_name,
                    "tool_args_json": None if tool_args is None else json.dumps(tool_args),
                    "tool_result": tool_result,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "latency_ms": latency_ms,
                    "cached": int(cached),
                    "error": error,
                },
            )
            await tx.conn.execute(
                "UPDATE nodes SET output_preview = ? WHERE id = ?", (short, node_id)
            )
            payload: dict[str, Any] = {
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "cached": cached,
                "preview": short,
            }
            if model is not None:
                payload["model"] = model
            if tool_name is not None:
                payload["tool_name"] = tool_name
            await tx.emit(node["run_id"], EventType.NODE_IO, node_id, **payload)

    async def record_output(self, node_id: str, output: str, summary: str) -> None:
        """A task's final output (full, in node_io.response) and its summary preview."""
        async with self.hub.transaction() as tx:
            node = await self._node(tx, node_id)
            if node["kind"] != NodeKind.TASK:
                raise ValueError("node.output is for tasks")
            short = clip(summary, PREVIEW_MAX)
            await self._upsert_io(tx, node_id, {"response": output})
            await tx.conn.execute(
                "UPDATE nodes SET output_preview = ? WHERE id = ?", (short, node_id)
            )
            await tx.emit(node["run_id"], EventType.NODE_OUTPUT, node_id, preview=short)

    async def record_artifact(
        self, run_id: str, path: str, *, size: int, sha256: str, node_id: str | None
    ) -> None:
        """Record a file the agent wrote (re-writing a path replaces the row)."""
        if size < 0:
            raise ValueError("size must be non-negative")
        async with self.hub.transaction() as tx:
            await tx.conn.execute(
                "INSERT INTO artifacts (run_id, path, bytes, sha256, node_id)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(run_id, path) DO UPDATE SET"
                " bytes = excluded.bytes, sha256 = excluded.sha256,"
                " node_id = excluded.node_id, created_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (run_id, path, size, sha256, node_id),
            )
            await tx.emit(
                run_id, EventType.ARTIFACT_WRITTEN, node_id, path=path, bytes=size, node_id=node_id
            )

    async def record_cost(self, node_id: str, micro_usd: int, calls: int = 1) -> None:
        """Add metered spend to a node and roll it up its ancestors, in one transaction."""
        if micro_usd < 0 or calls < 0:
            raise ValueError("cost must be non-negative")
        async with self.hub.transaction() as tx:
            run_id = (await self._node(tx, node_id))["run_id"]
            await tx.conn.execute(
                "UPDATE nodes SET spent_micro = spent_micro + ? WHERE id = ?", (micro_usd, node_id)
            )
            current: str | None = node_id
            while current is not None:
                await tx.conn.execute(
                    "UPDATE nodes SET rollup_spent_micro = rollup_spent_micro + ?,"
                    " rollup_calls = rollup_calls + ? WHERE id = ?",
                    (micro_usd, calls, current),
                )
                await self._emit_cost(tx, run_id, current)
                parent = await _one(tx, "SELECT parent_id FROM nodes WHERE id = ?", (current,))
                current = parent["parent_id"]

    @staticmethod
    async def _emit_cost(tx: Tx, run_id: str, node_id: str) -> None:
        row = await _one(
            tx,
            "SELECT spent_micro, rollup_spent_micro, rollup_calls FROM nodes WHERE id = ?",
            (node_id,),
        )
        await tx.emit(
            run_id,
            EventType.NODE_COST,
            node_id,
            spent_micro=row["spent_micro"],
            rollup_spent_micro=row["rollup_spent_micro"],
            rollup_calls=row["rollup_calls"],
        )

    # ---- reads ------------------------------------------------------------------------------

    async def snapshot(self, run_id: str) -> Snapshot:
        """Current state plus the seq to stream from.

        `seq` is read first, so the nodes are at least as new as it. Replaying the few events
        between the two is safe because the reducer is idempotent.
        """
        store = self.hub.store
        run = await store.fetch_one(
            "SELECT id, prompt, title, provider, model, status, error FROM runs WHERE id = ?",
            (run_id,),
        )
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        seq_row = await store.fetch_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?", (run_id,)
        )
        nodes = await store.fetch_all(
            "SELECT n.id, n.parent_id, n.kind, n.step_type, n.title, n.status, n.path, n.actor,"
            " n.turn, n.depth, n.objective, n.thought_preview, n.output_preview, n.error,"
            " COALESCE(io.tokens_in, 0) AS tokens_in, COALESCE(io.tokens_out, 0) AS tokens_out,"
            " n.est_total_micro, n.spent_micro, n.rollup_spent_micro, n.rollup_calls,"
            " n.seq_created FROM nodes n LEFT JOIN node_io io ON io.node_id = n.id"
            " WHERE n.run_id = ? ORDER BY n.seq_created",
            (run_id,),
        )
        edges = await store.fetch_all(
            "SELECT task_id, depends_on_task_id FROM edges WHERE run_id = ? ORDER BY rowid",
            (run_id,),
        )
        artifacts = await store.fetch_all(
            "SELECT path, bytes, node_id FROM artifacts WHERE run_id = ? ORDER BY path",
            (run_id,),
        )
        return Snapshot(
            run=RunView(**dict(run)),
            nodes=[NodeView(**dict(n)) for n in nodes],
            edges=[EdgeView(**dict(e)) for e in edges],
            artifacts=[ArtifactView(**dict(a)) for a in artifacts],
            seq=seq_row["seq"],
        )
