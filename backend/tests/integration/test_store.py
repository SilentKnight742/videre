import sqlite3
from pathlib import Path

import pytest

from videre.storage.db import SCHEMA_VERSION, SchemaMismatch, Store

TABLES = {
    "runs",
    "nodes",
    "edges",
    "node_io",
    "artifacts",
    "ledger",
    "events",
    "settlements",
    "llm_cache",
}
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
        await tx.execute("INSERT INTO runs (id, prompt) VALUES ('r1', 'demo')")
    await first.close()

    second = Store(path)
    await second.open()  # schema is applied again; must be a no-op
    row = await second.fetch_one("SELECT status, wallet_authorized_micro FROM runs")
    await second.close()
    assert (row["status"], row["wallet_authorized_micro"]) == ("created", 0)


async def test_transaction_rolls_back_on_error(store: Store) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with store.transaction() as tx:
            await tx.execute("INSERT INTO runs (id, prompt) VALUES ('r1', 'demo')")
            raise RuntimeError("boom")
    assert await store.fetch_all("SELECT id FROM runs") == []
    # the writer lock was released and the next transaction works
    async with store.transaction() as tx:
        await tx.execute("INSERT INTO runs (id, prompt) VALUES ('r2', 'demo')")
    assert [r["id"] for r in await store.fetch_all("SELECT id FROM runs")] == ["r2"]


async def test_constraints_are_enforced(store: Store) -> None:
    async with store.transaction() as tx:
        await tx.execute("INSERT INTO runs (id, prompt) VALUES ('r1', 'demo')")
    bad = [
        "INSERT INTO runs (id, prompt, status) VALUES ('r2', 'demo', 'nonsense')",
        "INSERT INTO nodes (id, run_id, kind, title, path, depth)"
        " VALUES ('n', 'missing', 'task', 't', '/t', 0)",
        "INSERT INTO nodes (id, run_id, kind, title, path, depth, spent_micro)"
        " VALUES ('n', 'r1', 'task', 't', '/t', 0, -1)",
        # old v1 kinds are gone
        "INSERT INTO nodes (id, run_id, kind, title, path, depth)"
        " VALUES ('n', 'r1', 'subtask', 't', '/t', 0)",
        # a step must have a step_type, and only a step may have one
        "INSERT INTO nodes (id, run_id, kind, title, path, depth)"
        " VALUES ('n', 'r1', 'step', 's', '/s', 1)",
        "INSERT INTO nodes (id, run_id, kind, step_type, title, path, depth)"
        " VALUES ('n', 'r1', 'task', 'llm', 't', '/t', 0)",
        "INSERT INTO nodes (id, run_id, kind, title, path) VALUES ('n', 'r1', 'task', 't', '/t')",
    ]
    for sql in bad:
        with pytest.raises(sqlite3.IntegrityError):
            async with store.transaction() as tx:
                await tx.execute(sql)


async def test_closed_store_refuses_use(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="not open"):
        await Store(tmp_path / "x.db").fetch_one("SELECT 1")


async def test_fresh_database_is_stamped_with_schema_version(store: Store) -> None:
    assert (await store.fetch_one("PRAGMA user_version"))[0] == SCHEMA_VERSION == 2


async def test_other_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version = 3")
    with pytest.raises(SchemaMismatch, match=r"delete videre.db \(schema changed\)"):
        await Store(path).open()


async def test_unversioned_v1_database_is_refused(tmp_path: Path) -> None:
    """A1 databases had tables but no user_version; CREATE IF NOT EXISTS would keep v1 tables."""
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE runs (id TEXT PRIMARY KEY, scenario TEXT NOT NULL)")
    store = Store(path)
    with pytest.raises(SchemaMismatch, match="schema version 1"):
        await store.open()
    with pytest.raises(RuntimeError, match="not open"):
        await store.fetch_one("SELECT 1")
