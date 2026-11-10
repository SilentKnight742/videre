-- Videre schema (docs/PLAN-backend.md section 4). Idempotent: applied on every start.
-- All money columns are integer micro-USD (1 USD = 1_000_000); settlements are integer cents.

CREATE TABLE IF NOT EXISTS runs (
    id                      TEXT PRIMARY KEY,
    scenario                TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'awaiting_approval', 'funded', 'running', 'closed')),
    wallet_authorized_micro INTEGER NOT NULL DEFAULT 0 CHECK (wallet_authorized_micro >= 0),
    paypal_order_id         TEXT,
    paypal_auth_id          TEXT,
    created_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    closed_at               TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    parent_id          TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    kind               TEXT NOT NULL CHECK (kind IN ('task', 'subtask', 'call')),
    title              TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'blocked'
        CHECK (status IN ('blocked', 'waiting_funds', 'ready', 'running', 'paused_budget',
                          'done', 'failed', 'cancelled')),
    path               TEXT NOT NULL,              -- e.g. "/t3/s2/c1": subtree = prefix range scan
    allocated_micro    INTEGER NOT NULL DEFAULT 0 CHECK (allocated_micro >= 0),
    spent_micro        INTEGER NOT NULL DEFAULT 0 CHECK (spent_micro >= 0),
    reserved_micro     INTEGER NOT NULL DEFAULT 0 CHECK (reserved_micro >= 0),
    est_total_micro    INTEGER NOT NULL DEFAULT 0 CHECK (est_total_micro >= 0),
    rollup_spent_micro INTEGER NOT NULL DEFAULT 0 CHECK (rollup_spent_micro >= 0),
    rollup_calls       INTEGER NOT NULL DEFAULT 0 CHECK (rollup_calls >= 0),
    seq_created        INTEGER NOT NULL DEFAULT 0,
    started_at         TEXT,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_nodes_run_parent ON nodes(run_id, parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_run_path ON nodes(run_id, path);

-- Dependencies between tasks only.
CREATE TABLE IF NOT EXISTS edges (
    run_id             TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id            TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_task_id),
    CHECK (task_id <> depends_on_task_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_run ON edges(run_id);

-- Heavy text, fetched only when a node is opened.
CREATE TABLE IF NOT EXISTS node_io (
    node_id    TEXT PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    model      TEXT,
    prompt     TEXT,
    response   TEXT,
    tokens_in  INTEGER NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
    tokens_out INTEGER NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
    latency_ms INTEGER,
    extra_json TEXT
);

-- Append only.
CREATE TABLE IF NOT EXISTS ledger (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts                  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    node_id             TEXT REFERENCES nodes(id) ON DELETE CASCADE,
    type                TEXT NOT NULL
        CHECK (type IN ('fund', 'unfund', 'reserve', 'release', 'spend', 'capture', 'void',
                        'rounding')),
    amount_micro        INTEGER NOT NULL,
    balance_after_micro INTEGER NOT NULL,
    ref                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_ledger_run_ts ON ledger(run_id, ts);

-- Outbox: written in the same transaction as the state change it describes.
CREATE TABLE IF NOT EXISTS events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    type         TEXT NOT NULL,
    node_id      TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS settlements (
    id                TEXT PRIMARY KEY,
    run_id            TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id           TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    cents             INTEGER NOT NULL CHECK (cents >= 0),
    paypal_capture_id TEXT,
    invoice_id        TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_cache (
    prompt_hash TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    response    TEXT NOT NULL,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0
);
