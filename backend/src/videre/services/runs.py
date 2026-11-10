"""Run and node writes. Every change emits its event in the same transaction."""

import uuid

from fastapi import HTTPException

from videre.domain.events import EventType, NodeView, RunView, Snapshot
from videre.domain.states import NodeKind, NodeStatus, RunStatus
from videre.services.events import EventHub, Tx

_PATH_PREFIX = {NodeKind.TASK: "t", NodeKind.SUBTASK: "s", NodeKind.CALL: "c"}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class RunService:
    def __init__(self, hub: EventHub) -> None:
        self.hub = hub

    async def create_run(self, scenario: str) -> str:
        run_id = _new_id("run")
        async with self.hub.transaction() as tx:
            await tx.conn.execute(
                "INSERT INTO runs (id, scenario, status) VALUES (?, ?, ?)",
                (run_id, scenario, RunStatus.CREATED),
            )
            await tx.emit(
                run_id, EventType.RUN_CREATED, scenario=scenario, status=RunStatus.CREATED
            )
        return run_id

    async def set_run_status(self, run_id: str, status: RunStatus) -> None:
        async with self.hub.transaction() as tx:
            await tx.conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
            if status == RunStatus.CLOSED:
                await tx.conn.execute(
                    "UPDATE runs SET closed_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                    (run_id,),
                )
                await tx.emit(run_id, EventType.RUN_CLOSED)
            else:
                await tx.emit(run_id, EventType.RUN_STATUS, status=status)

    async def add_node(
        self,
        run_id: str,
        kind: NodeKind,
        title: str,
        *,
        parent_id: str | None = None,
        est_total_micro: int = 0,
        depends_on: list[str] | None = None,
        status: NodeStatus | None = None,
    ) -> str:
        depends_on = depends_on or []
        if depends_on and kind != NodeKind.TASK:
            raise ValueError("only tasks have dependencies")
        status = status or (NodeStatus.BLOCKED if depends_on else NodeStatus.READY)
        node_id = _new_id("n")
        async with self.hub.transaction() as tx:
            parent_path = ""
            if parent_id is not None:
                row = await tx.conn.execute_fetchall(
                    "SELECT path FROM nodes WHERE id = ? AND run_id = ?", (parent_id, run_id)
                )
                if not row:
                    raise ValueError(f"unknown parent {parent_id}")
                parent_path = row[0]["path"]
            for dep in depends_on:
                hit = await tx.conn.execute_fetchall(
                    "SELECT 1 FROM nodes WHERE id = ? AND run_id = ? AND kind = 'task'",
                    (dep, run_id),
                )
                if not hit:
                    raise ValueError(f"unknown dependency {dep}")
            count = await tx.conn.execute_fetchall(
                "SELECT COUNT(*) AS n FROM nodes WHERE run_id = ? AND parent_id IS ?",
                (run_id, parent_id),
            )
            path = f"{parent_path}/{_PATH_PREFIX[kind]}{count[0]['n'] + 1}"
            ev = await tx.emit(
                run_id,
                EventType.NODE_CREATED,
                node_id,
                parent_id=parent_id,
                kind=kind,
                title=title,
                status=status,
                path=path,
                est_total_micro=est_total_micro,
                depends_on=depends_on,
            )
            await tx.conn.execute(
                "INSERT INTO nodes (id, run_id, parent_id, kind, title, status, path,"
                " est_total_micro, seq_created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (node_id, run_id, parent_id, kind, title, status, path, est_total_micro, ev.seq),
            )
            for dep in depends_on:
                await tx.conn.execute(
                    "INSERT INTO edges (run_id, task_id, depends_on_task_id) VALUES (?, ?, ?)",
                    (run_id, node_id, dep),
                )
        return node_id

    async def set_node_status(self, node_id: str, status: NodeStatus) -> None:
        async with self.hub.transaction() as tx:
            row = await tx.conn.execute_fetchall(
                "SELECT run_id FROM nodes WHERE id = ?", (node_id,)
            )
            if not row:
                raise ValueError(f"unknown node {node_id}")
            await tx.conn.execute("UPDATE nodes SET status = ? WHERE id = ?", (status, node_id))
            await tx.emit(row[0]["run_id"], EventType.NODE_STATUS, node_id, status=status)

    async def record_cost(self, node_id: str, micro_usd: int, calls: int = 1) -> None:
        """Add metered spend to a node and roll it up its ancestors, in one transaction."""
        if micro_usd < 0 or calls < 0:
            raise ValueError("cost must be non-negative")
        async with self.hub.transaction() as tx:
            row = await tx.conn.execute_fetchall(
                "SELECT run_id FROM nodes WHERE id = ?", (node_id,)
            )
            if not row:
                raise ValueError(f"unknown node {node_id}")
            run_id = row[0]["run_id"]
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
                parent = await tx.conn.execute_fetchall(
                    "SELECT parent_id FROM nodes WHERE id = ?", (current,)
                )
                current = parent[0]["parent_id"]

    @staticmethod
    async def _emit_cost(tx: Tx, run_id: str, node_id: str) -> None:
        row = (
            await tx.conn.execute_fetchall(
                "SELECT spent_micro, rollup_spent_micro, rollup_calls FROM nodes WHERE id = ?",
                (node_id,),
            )
        )[0]
        await tx.emit(
            run_id,
            EventType.NODE_COST,
            node_id,
            spent_micro=row["spent_micro"],
            rollup_spent_micro=row["rollup_spent_micro"],
            rollup_calls=row["rollup_calls"],
        )

    async def snapshot(self, run_id: str) -> Snapshot:
        """Current state plus the seq to stream from.

        `seq` is read first, so the nodes are at least as new as it. Replaying the few events
        between the two is safe because the reducer is idempotent.
        """
        store = self.hub.store
        run = await store.fetch_one("SELECT id, scenario, status FROM runs WHERE id = ?", (run_id,))
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        seq_row = await store.fetch_one(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM events WHERE run_id = ?", (run_id,)
        )
        nodes = await store.fetch_all(
            "SELECT id, parent_id, kind, title, status, path, est_total_micro, spent_micro,"
            " rollup_spent_micro, rollup_calls, seq_created FROM nodes WHERE run_id = ?"
            " ORDER BY seq_created",
            (run_id,),
        )
        edges = await store.fetch_all(
            "SELECT task_id, depends_on_task_id FROM edges WHERE run_id = ?", (run_id,)
        )
        return Snapshot(
            run=RunView(**dict(run)),
            nodes=[NodeView(**dict(n)) for n in nodes],
            edges=[dict(e) for e in edges],  # type: ignore[misc]
            seq=seq_row["seq"],
        )
