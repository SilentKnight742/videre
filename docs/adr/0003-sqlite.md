# 3. SQLite in WAL mode

Status: accepted

## Context

Single user, single process, local demo, and a hard deadline. State changes and their events must commit atomically.

## Decision

One SQLite file in WAL mode via aiosqlite, with one writer lock in the `Store`.

## Consequences

Zero setup and real transactions. Write throughput is limited to one writer, which is far above what one agent run needs. Not suitable for multi-process deployment, which is out of scope.
