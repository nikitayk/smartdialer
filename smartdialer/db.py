"""SQLite is the single source of truth for the whole system.

There is deliberately no separate cache. That answers the assignment's
"DB says AVAILABLE but cache says RESERVED, which wins?" question directly:
there is no second copy of the truth to disagree with, so there is nothing
to reconcile. Every reader and writer goes through this one file.

Two pragmas do the heavy lifting for concurrency:
  - WAL: readers don't block the single writer, so worker loops and the
    sweeper can read freely while one writer commits.
  - busy_timeout: when two writers do collide, the loser waits and retries
    inside SQLite instead of immediately raising 'database is locked'. This
    is what lets many worker threads share one file without hand-rolled
    retry loops on every statement.
"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           TEXT PRIMARY KEY,
    state        TEXT NOT NULL,
    reserved_by  TEXT,
    reserved_at  REAL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS borrowers (
    id           TEXT PRIMARY KEY,
    phone        TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'QUEUED',  -- QUEUED | IN_FLIGHT | DONE
    attempts     INTEGER NOT NULL DEFAULT 0,
    locked_by    TEXT,
    locked_at    REAL,
    updated_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    id            TEXT PRIMARY KEY,
    borrower_id   TEXT NOT NULL,
    agent_id      TEXT,
    provider      TEXT,
    state         TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    answered_at   REAL,           -- set on ANSWERED; drives the fast abandon check
    FOREIGN KEY (borrower_id) REFERENCES borrowers(id)
);

-- Idempotency ledger for provider events. INSERT OR IGNORE here is the
-- dedup gate: a repeated provider_event_id can never apply twice, no matter
-- what state the call is in.
CREATE TABLE IF NOT EXISTS processed_events (
    provider_event_id TEXT PRIMARY KEY,
    call_id           TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    processed_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS anomaly_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type       TEXT,       -- 'agent' | 'call'
    entity_id         TEXT,
    attempted_event   TEXT,
    from_state        TEXT,
    worker_id         TEXT,
    provider_event_id TEXT,
    reason            TEXT,
    created_at        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS safety_decisions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    proposed_n     INTEGER NOT NULL,
    action         TEXT NOT NULL,
    approved_n     INTEGER NOT NULL,
    rule_triggered TEXT NOT NULL,
    reason         TEXT NOT NULL,
    created_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    name  TEXT PRIMARY KEY,
    value REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_state       ON agents(state);
CREATE INDEX IF NOT EXISTS idx_agents_reserved_at ON agents(reserved_at);
CREATE INDEX IF NOT EXISTS idx_calls_state        ON calls(state);
CREATE INDEX IF NOT EXISTS idx_calls_updated_at   ON calls(updated_at);
CREATE INDEX IF NOT EXISTS idx_borrowers_state    ON borrowers(state);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
