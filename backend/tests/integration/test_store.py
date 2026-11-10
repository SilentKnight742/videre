import sqlite3
from pathlib import Path

import pytest

from videre.storage.db import Store

TABLES = {"runs", "nodes", "edges", "node_io", "ledger", "events", "settlements", "llm_cache"}
INDEXES = {
    "idx_nodes_run_parent",
    "idx_nodes_run_path",
    "idx_events_run_seq",
    "idx_ledger_run_ts",
}


async def names(store: Store, kind: str) -> set[str]:
    rows = await store.fetch_all("SELECT name FROM sqlite_master WHERE type = ?", (kind,))
    return {r["name"] for r in rows}


async def test_schema_creates_all_tables_and_indexes(store: Store) -> None:
    assert await names(store, "table") >= TABLES
    assert await names(store, "index") >= INDEXES


async def test_wal_and_foreign_keys_are_on(store: Store) -> None:
    assert (await store.fetch_one("PRAGMA journal_mode"))[0] == "wal"
    assert (await store.fetch_one("PRAGMA foreign_keys"))[0] == 1


async def test_reopening_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    first = Store(path)
    await first.open()
    async with first.transaction() as tx:
        await tx.execute("INSERT INTO runs (id, scenario) VALUES ('r1', 'demo')")
    await first.close()

    second = Store(path)
    await second.open()  # schema is applied again; must be a no-op
    row = await second.fetch_one("SELECT status, wallet_authorized_micro FROM runs")
    await second.close()
    assert (row["status"], row["wallet_authorized_micro"]) == ("created", 0)


async def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO runs (id, scenario) VALUES ('r1', 'demo')")
            raise RuntimeError("boom")
    assert await store.fetch_all("SELECT id FROM runs") == []
    # the writer lock was released and the next transaction works
    async with store.transaction() as tx:
        await tx.execute("INSERT INTO runs (id, scenario) VALUES ('r2', 'demo')")
    assert [r["id"] for r in await store.fetch_all("SELECT id FROM runs")] == ["r2"]


async def test_constraints_are_enforced(store: Store) -> None:
    async with store.transaction() as tx:
        await tx.execute("INSERT INTO runs (id, scenario) VALUES ('r1', 'demo')")
    bad = [
        "INSERT INTO runs (id, scenario, status) VALUES ('r2', 'demo', 'nonsense')",
        "INSERT INTO nodes (id, run_id, kind, title, path)"
        " VALUES ('n', 'missing', 'task', 't', '/t')",
        "INSERT INTO nodes (id, run_id, kind, title, path, spent_micro)"
        " VALUES ('n', 'r1', 'task', 't', '/t', -1)",
    ]
    for sql in bad:
        with pytest.raises(sqlite3.IntegrityError):
            async with store.transaction() as tx:
                await tx.execute(sql)


async def test_closed_store_refuses_use(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        await Store(tmp_path / "x.db").fetch_one("SELECT 1")
