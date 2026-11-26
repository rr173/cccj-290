"""SQLite connection, schema, and transaction helpers."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "workbench.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_account ON sessions(account_id);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id);

CREATE TABLE IF NOT EXISTS project_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('created', 'updated')),
    actor_id INTEGER NOT NULL REFERENCES accounts(id),
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    graph_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(project_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_revisions_project ON project_revisions(project_id, id);
CREATE INDEX IF NOT EXISTS idx_revisions_actor ON project_revisions(actor_id);

CREATE TABLE IF NOT EXISTS invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES accounts(id),
    invitee_id INTEGER REFERENCES accounts(id),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    consumed_at REAL,
    consumed_edge_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_invitations_token ON invitations(token_hash);

CREATE TABLE IF NOT EXISTS invitation_confirmations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invitation_id INTEGER NOT NULL REFERENCES invitations(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    confirmed_at REAL NOT NULL,
    UNIQUE(invitation_id, account_id)
);

-- Every edge version is retained. A deleted row has deleted_version set.
-- The partial unique index prevents two *active* edges for one pair.
CREATE TABLE IF NOT EXISTS association_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_a INTEGER NOT NULL REFERENCES accounts(id),
    account_b INTEGER NOT NULL REFERENCES accounts(id),
    invitation_id INTEGER REFERENCES invitations(id),
    created_by INTEGER NOT NULL REFERENCES accounts(id),
    created_version INTEGER NOT NULL,
    created_at REAL NOT NULL,
    deleted_version INTEGER,
    deleted_by INTEGER REFERENCES accounts(id),
    deleted_at REAL,
    CHECK (account_a < account_b)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_edge_pair
    ON association_edges(account_a, account_b)
    WHERE deleted_version IS NULL;
CREATE INDEX IF NOT EXISTS idx_edges_pair ON association_edges(account_a, account_b);

CREATE TABLE IF NOT EXISTS graph_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT INTO graph_state(id, version) VALUES (1, 0)
ON CONFLICT(id) DO NOTHING;

CREATE TABLE IF NOT EXISTS graph_events (
    version INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN ('linked', 'unlinked')),
    edge_id INTEGER NOT NULL,
    account_a INTEGER NOT NULL,
    account_b INTEGER NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("WORKBENCH_DB", str(DEFAULT_DB))).expanduser().resolve()


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # explicit BEGIN/COMMIT/ROLLBACK
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    # WAL lets readers not block writers; all authorization writes still use
    # BEGIN IMMEDIATE and therefore serialize against graph changes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Acquire the SQLite write lock before checking dynamic permissions."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Use one SQLite snapshot for authorization plus dependent reads."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
