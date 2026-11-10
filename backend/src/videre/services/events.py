"""Event outbox and in-process fan-out.

`EventHub.transaction()` wraps `Store.transaction()`: events emitted inside are inserted in the
same transaction as the state change, and published to subscribers only after it commits. A
rolled-back change therefore never reaches a client.
"""

import asyncio
import json
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from videre.domain.events import Event
from videre.storage.db import Store


class Tx:
    """A write transaction that can emit events."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        self.events: list[Event] = []

    async def emit(
        self, run_id: str, type_: str, node_id: str | None = None, **payload: Any
    ) -> Event:
        async with self.conn.execute(
            "INSERT INTO events (run_id, type, node_id, payload_json) VALUES (?, ?, ?, ?)"
            " RETURNING seq, ts",
            (run_id, type_, node_id, json.dumps(payload)),
        ) as cur:
            row = await cur.fetchone()
        assert row is not None  # noqa: S101 - INSERT ... RETURNING always yields a row
        ev = Event(
            seq=row["seq"],
            run_id=run_id,
            ts=row["ts"],
            type=type_,
            node_id=node_id,
            payload=payload,
        )
        self.events.append(ev)
        return ev


def _row_to_event(row: aiosqlite.Row) -> Event:
    return Event(
        seq=row["seq"],
        run_id=row["run_id"],
        ts=row["ts"],
        type=row["type"],
        node_id=row["node_id"],
        payload=json.loads(row["payload_json"]),
    )


class EventHub:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        async with self.store.transaction() as conn:
            tx = Tx(conn)
            yield tx
        # Committed. Only now may anyone see the events.
        for ev in tx.events:
            for queue in list(self._subscribers[ev.run_id]):
                queue.put_nowait(ev)

    @asynccontextmanager
    async def subscribe(self, run_id: str) -> AsyncIterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[run_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[run_id].discard(queue)
            if not self._subscribers[run_id]:
                del self._subscribers[run_id]

    async def read_after(self, run_id: str, after: int) -> list[Event]:
        rows = await self.store.fetch_all(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, after)
        )
        return [_row_to_event(r) for r in rows]
