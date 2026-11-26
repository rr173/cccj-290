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

-- One proposed replacement of [start, end) in the content of base_revision.
-- Submitting a proposal never touches projects/project_revisions; only the
-- owner's accept/withdraw decisions do, in the same transaction that records
-- the resulting project revision.
CREATE TABLE IF NOT EXISTS curation_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    proposer_id INTEGER NOT NULL REFERENCES accounts(id),
    base_revision INTEGER NOT NULL,
    start INTEGER NOT NULL,
    end INTEGER NOT NULL,
    original_text TEXT NOT NULL,
    replacement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'rejected', 'accepted', 'withdrawn')),
    accepted_start INTEGER,
    accepted_end INTEGER,
    created_at REAL NOT NULL,
    decided_at REAL,
    decided_by INTEGER REFERENCES accounts(id),
    accepted_revision INTEGER,
    withdrawn_at REAL,
    withdrawn_by INTEGER REFERENCES accounts(id),
    withdrawn_revision INTEGER,
    CHECK (start <= end),
    CHECK (end >= 0 AND start >= 0)
);
CREATE INDEX IF NOT EXISTS idx_curations_project
    ON curation_proposals(project_id, id);
CREATE INDEX IF NOT EXISTS idx_curations_proposer
    ON curation_proposals(proposer_id, id);

-- Exact span changed by a curation accept/withdraw.  The pre-existing PATCH
-- endpoint replaces the whole content and records no row here; an actual
-- content change through PATCH is therefore treated as a whole-document
-- rewrite when carrying a proposal range forward.
CREATE TABLE IF NOT EXISTS project_content_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    edit_start INTEGER NOT NULL,
    edit_end INTEGER NOT NULL,
    inserted_length INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(project_id, revision)
);

-- ---------------------------------------------------------------------------
-- Limited-edition issues
--
-- An issue is an immutable snapshot of one project revision plus a quota that
-- is distributed through a tree of channels.  ``issue_channels`` keeps one
-- mutable ``available`` counter per channel; moving quota between channels
-- never changes the sum of ``available``.  Held quota lives in pending
-- invitations, consumed quota lives in entitlements.  The invariant
--
--   total_quota == SUM(channels.available)
--                  + COUNT(pending invitations)
--                  + COUNT(accepted invitations)
--
-- therefore holds at every committed point (see issue_service for settlement).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    source_revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    total_quota INTEGER NOT NULL CHECK (total_quota > 0),
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project_id, id);
CREATE INDEX IF NOT EXISTS idx_issues_owner ON issues(owner_id, id);

CREATE TABLE IF NOT EXISTS issue_channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES issue_channels(id),
    manager_id INTEGER NOT NULL REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    granted INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    disabled_at REAL,
    disabled_by INTEGER REFERENCES accounts(id),
    CHECK (granted >= 0),
    CHECK (available >= 0)
);
-- Exactly one root (owner-operated) channel per issue.
CREATE UNIQUE INDEX IF NOT EXISTS uq_channel_root
    ON issue_channels(issue_id) WHERE parent_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_channels_issue ON issue_channels(issue_id, id);
CREATE INDEX IF NOT EXISTS idx_channels_parent ON issue_channels(parent_id, id);
CREATE INDEX IF NOT EXISTS idx_channels_manager ON issue_channels(manager_id, id);

CREATE TABLE IF NOT EXISTS issue_invitations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES issue_channels(id),
    reader_id INTEGER NOT NULL REFERENCES accounts(id),
    invited_by INTEGER NOT NULL REFERENCES accounts(id),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'accepted', 'expired')),
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    accepted_at REAL,
    settled_at REAL
);
-- A reader holds at most one *live* invitation per issue, regardless of which
-- channel issued it.  Expired rows are settled out of 'pending' first, so a
-- fresh invitation may reuse the slot afterwards.
CREATE UNIQUE INDEX IF NOT EXISTS uq_live_issue_invitation
    ON issue_invitations(issue_id, reader_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_inv_reader ON issue_invitations(reader_id, id);
CREATE INDEX IF NOT EXISTS idx_inv_issue ON issue_invitations(issue_id, id);
CREATE INDEX IF NOT EXISTS idx_inv_channel ON issue_invitations(channel_id, id);
CREATE INDEX IF NOT EXISTS idx_inv_pending_expiry
    ON issue_invitations(status, expires_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS issue_entitlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    reader_id INTEGER NOT NULL REFERENCES accounts(id),
    channel_id INTEGER NOT NULL REFERENCES issue_channels(id),
    invitation_id INTEGER NOT NULL UNIQUE REFERENCES issue_invitations(id),
    accepted_at REAL NOT NULL,
    UNIQUE(issue_id, reader_id)
);
CREATE INDEX IF NOT EXISTS idx_ent_reader ON issue_entitlements(reader_id, id);

-- Replay cache for at-least-once client retries.  A record is written in the
-- same transaction as the effect it describes, so a failure leaves neither.
CREATE TABLE IF NOT EXISTS idempotency_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    actor_id INTEGER NOT NULL REFERENCES accounts(id),
    key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(scope, actor_id, key)
);

-- ---------------------------------------------------------------------------
-- Owner-scoped royalty ledger and finance settlement orders
--
-- Sales snapshot the collaborator and basis points in effect at sale time.
-- Refund allocations are recomputed cumulatively against that snapshot, so a
-- later collaborator/rate change never rewrites old sales.  Settlement items
-- are immutable money instructions; only their finance confirmation state
-- changes.  The partial unique index is the final guard against two pending
-- transfers for the same owner/payee account.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS issue_royalty_shares (
    issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    creator_account_id INTEGER NOT NULL REFERENCES accounts(id),
    basis_points INTEGER NOT NULL CHECK (basis_points BETWEEN 0 AND 10000),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_royalty_shares_owner
    ON issue_royalty_shares(owner_id);
CREATE INDEX IF NOT EXISTS idx_royalty_shares_creator
    ON issue_royalty_shares(creator_account_id);

CREATE TABLE IF NOT EXISTS issue_royalty_share_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    creator_account_id INTEGER NOT NULL REFERENCES accounts(id),
    basis_points INTEGER NOT NULL CHECK (basis_points BETWEEN 0 AND 10000),
    version INTEGER NOT NULL,
    changed_by INTEGER NOT NULL REFERENCES accounts(id),
    created_at REAL NOT NULL,
    UNIQUE(issue_id, version)
);

CREATE TABLE IF NOT EXISTS royalty_sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    external_order_no TEXT NOT NULL,
    gross_amount INTEGER NOT NULL CHECK (gross_amount > 0),
    memo TEXT,
    creator_account_id INTEGER NOT NULL REFERENCES accounts(id),
    basis_points INTEGER NOT NULL CHECK (basis_points BETWEEN 0 AND 10000),
    creator_gross_amount INTEGER NOT NULL CHECK (creator_gross_amount >= 0),
    owner_gross_amount INTEGER NOT NULL CHECK (owner_gross_amount >= 0),
    refunded_amount INTEGER NOT NULL DEFAULT 0 CHECK (refunded_amount >= 0),
    creator_refunded_amount INTEGER NOT NULL DEFAULT 0
        CHECK (creator_refunded_amount >= 0),
    owner_refunded_amount INTEGER NOT NULL DEFAULT 0
        CHECK (owner_refunded_amount >= 0),
    request_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(owner_id, external_order_no),
    CHECK (refunded_amount <= gross_amount),
    CHECK (creator_refunded_amount <= creator_gross_amount),
    CHECK (owner_refunded_amount <= owner_gross_amount),
    CHECK (creator_refunded_amount + owner_refunded_amount = refunded_amount),
    CHECK (creator_gross_amount + owner_gross_amount = gross_amount)
);
CREATE INDEX IF NOT EXISTS idx_royalty_sales_owner_issue
    ON royalty_sales(owner_id, issue_id, id);
CREATE INDEX IF NOT EXISTS idx_royalty_sales_creator
    ON royalty_sales(creator_account_id);

CREATE TABLE IF NOT EXISTS royalty_refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    sale_id INTEGER NOT NULL REFERENCES royalty_sales(id),
    external_refund_no TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK (amount > 0),
    creator_deduction INTEGER NOT NULL CHECK (creator_deduction >= 0),
    owner_deduction INTEGER NOT NULL CHECK (owner_deduction >= 0),
    reason TEXT,
    request_fingerprint TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(owner_id, external_refund_no),
    CHECK (creator_deduction + owner_deduction = amount)
);
CREATE INDEX IF NOT EXISTS idx_royalty_refunds_sale
    ON royalty_refunds(sale_id, id);
CREATE INDEX IF NOT EXISTS idx_royalty_refunds_owner_issue
    ON royalty_refunds(owner_id, issue_id, id);

CREATE TABLE IF NOT EXISTS royalty_settlements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    request_id TEXT NOT NULL,
    note TEXT,
    total_amount INTEGER NOT NULL CHECK (total_amount > 0),
    item_count INTEGER NOT NULL CHECK (item_count > 0),
    created_at REAL NOT NULL,
    UNIQUE(owner_id, request_id)
);
CREATE INDEX IF NOT EXISTS idx_royalty_settlements_owner
    ON royalty_settlements(owner_id, id);

CREATE TABLE IF NOT EXISTS royalty_settlement_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('issued', 'no_payable')),
    settlement_id INTEGER REFERENCES royalty_settlements(id),
    created_at REAL NOT NULL,
    UNIQUE(owner_id, request_id)
);

CREATE TABLE IF NOT EXISTS royalty_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id INTEGER NOT NULL
        REFERENCES royalty_settlements(id) ON DELETE CASCADE,
    owner_id INTEGER NOT NULL REFERENCES accounts(id),
    payee_account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount INTEGER NOT NULL CHECK (amount > 0),
    earned_amount_snapshot INTEGER NOT NULL,
    paid_amount_snapshot INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'failed')),
    result_status TEXT CHECK (result_status IN ('success', 'failed')),
    finance_reference TEXT,
    result_note TEXT,
    confirmation_key TEXT,
    created_at REAL NOT NULL,
    confirmed_at REAL,
    UNIQUE(settlement_id, payee_account_id),
    UNIQUE(owner_id, confirmation_key)
);
-- At most one outstanding instruction for one payee in one owner ledger.
CREATE UNIQUE INDEX IF NOT EXISTS uq_royalty_pending_transfer_per_account
    ON royalty_transfers(owner_id, payee_account_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_royalty_transfers_owner_payee
    ON royalty_transfers(owner_id, payee_account_id, id);
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
