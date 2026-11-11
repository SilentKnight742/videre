"""The Store: one SQLite connection (WAL) with a single writer lock."""

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = resources.files("videre.storage").joinpath("schema.sql")
SCHEMA_VERSION = 2


class SchemaMismatch(RuntimeError):
    """The database file was made by another schema version. Dev only: no migrations."""


class Store:
    """Owns the database connection.

    Reads go straight through `fetch_one` / `fetch_all`. Every write happens inside
    `transaction()`, which holds the writer lock, so a state change and its event row
    (milestone A1) always commit together or not at all.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Store is not open")
        return self._conn

    async def open(self) -> None:
        if self._conn is not None:
            return
        # isolation_level=None: we issue BEGIN/COMMIT ourselves in transaction().
        conn = await aiosqlite.connect(self._path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        try:
            await self._check_version(conn)
            await conn.executescript(SCHEMA.read_text(encoding="utf-8"))
            await conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except BaseException:
            await conn.close()
            raise
        self._conn = conn

    async def _check_version(self, conn: aiosqlite.Connection) -> None:
        async with conn.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]  # type: ignore[index]
        if version == SCHEMA_VERSION:
            return
        # Version 0 is a fresh file, or one from before versioning (schema v1, which had tables).
        async with conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'") as cur:
            has_tables = (await cur.fetchone())[0] > 0  # type: ignore[index]
        if version != 0 or has_tables:
            raise SchemaMismatch(
                f"{self._path} has schema version {version or 1}, expected {SCHEMA_VERSION}:"
                " delete videre.db (schema changed)"
            )

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialised write transaction: commit on success, roll back on any error."""
        async with self._write_lock:
            conn = self.conn
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())
