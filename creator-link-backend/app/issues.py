"""Limited-edition issues: immutable snapshots, quota channel tree, claims.

Design summary
--------------

* An **issue** copies ``title``/``content`` from one existing project
  revision.  Later workbench edits never touch the issue; every reader sees
  the same snapshot.
* Every issue owns exactly one hidden **root channel** operated by the project
  owner.  Its ``available`` counter is the owner's *unallocated* quota.
* Allocating quota creates a brand-new child channel (never reuses or revives
  an old one) and immediately moves ``quota`` units from parent to child.
* Issuing an invitation immediately takes one unit out of the channel's
  ``available`` counter; the unit is held by the pending invitation.
* Accepting converts the held unit into a permanent entitlement (the seat is
  never returned).
* Expiry returns the held unit to the originating channel, or — if that
  channel is inside a disabled branch — to its nearest still-active ancestor.
  Settlement is lazy but runs at the start of every issue read/write, so a
  seat is usable the moment its deadline passes without a manual job.
* Disabling a channel cascades to its whole subtree, stops new allocations and
  invitations there, and moves all free seats in the subtree to the branch's
  direct parent.  Already-issued invitations stay claimable and already-claimed
  entitlements stay readable.

All mutations run under ``BEGIN IMMEDIATE`` on SQLite, which serializes every
writer, so quota counters, holds, and claims stay consistent under concurrent
allocation/invite/disable/accept and across restarts (all state is on disk).
Retries are made non-duplicating either by natural unique constraints (one live
invitation per reader per issue, one entitlement per reader per issue) or by the
``idempotency_records`` table written in the same transaction as its effect.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from .db import get_db, read_transaction, write_transaction
from .deps import get_current_account
from .errors import ApiError
from .schemas import (
    ChannelAllocateIn,
    IssueCreateIn,
    IssueInvitationCreateIn,
)
from .security import iso_from_ts, now_ts

router = APIRouter()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_lookup(
    conn: sqlite3.Connection,
    *,
    scope: str,
    actor_id: int,
    key: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM idempotency_records
         WHERE scope = ? AND actor_id = ? AND key = ?
        """,
        (scope, actor_id, key),
    ).fetchone()
    if row is None:
        return None
    if row["fingerprint"] != fingerprint:
        raise ApiError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used with a different request body.",
            {"scope": scope, "key": key},
        )
    return json.loads(row["response_json"])


def _idempotency_store(
    conn: sqlite3.Connection,
    *,
    scope: str,
    actor_id: int,
    key: str,
    fingerprint: str,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO idempotency_records(
            scope, actor_id, key, fingerprint, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (scope, actor_id, key, fingerprint, json.dumps(payload), now_ts()),
    )


def _require_account_by_username(
    conn: sqlite3.Connection, username: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, username FROM accounts WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "ACCOUNT_NOT_FOUND",
            f"Account {username!r} does not exist.",
            {"username": username},
        )
    return row


def _require_issue(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if row is None:
        raise ApiError(404, "ISSUE_NOT_FOUND", "Issue was not found.")
    return row


def _require_channel(
    conn: sqlite3.Connection, issue_id: int, channel_id: int
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM issue_channels WHERE id = ? AND issue_id = ?",
        (channel_id, issue_id),
    ).fetchone()
    if row is None:
        raise ApiError(
            404,
            "CHANNEL_NOT_FOUND",
            "Issue channel was not found.",
            {"issue_id": issue_id, "channel_id": channel_id},
        )
    return row


def _root_channel(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM issue_channels WHERE issue_id = ? AND parent_id IS NULL",
        (issue_id,),
    ).fetchone()
    if row is None:  # Defensive: every issue is created with a root channel.
        raise ApiError(500, "ISSUE_ROOT_MISSING", "Issue has no root channel.")
    return row


def _channel_username_row(
    conn: sqlite3.Connection, channel_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT c.*, m.username AS manager_username
          FROM issue_channels c
          JOIN accounts m ON m.id = c.manager_id
         WHERE c.id = ?
        """,
        (channel_id,),
    ).fetchone()


def _issue_actor_role(
    conn: sqlite3.Connection, issue: sqlite3.Row, actor_id: int
) -> str | None:
    if actor_id == issue["owner_id"]:
        return "owner"
    managed = conn.execute(
        "SELECT 1 FROM issue_channels WHERE issue_id = ? AND manager_id = ? LIMIT 1",
        (issue["id"], actor_id),
    ).fetchone()
    if managed is not None:
        return "manager"
    return None


def _nearest_active_channel(
    conn: sqlite3.Connection, channel_id: int
) -> int | None:
    """Walk up the original parent chain to the closest active channel."""
    current: int | None = channel_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        row = conn.execute(
            "SELECT id, parent_id, status FROM issue_channels WHERE id = ?",
            (current,),
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "active":
            return int(row["id"])
        current = row["parent_id"]
    return None


def _settle_expired(
    conn: sqlite3.Connection, issue_id: int, now: float
) -> int:
    """Release seats held by pending invitations whose deadline has passed."""
    rows = conn.execute(
        """
        SELECT * FROM issue_invitations
         WHERE issue_id = ? AND status = 'pending' AND expires_at <= ?
         ORDER BY id
        """,
        (issue_id, now),
    ).fetchall()
    for row in rows:
        target = _nearest_active_channel(conn, int(row["channel_id"]))
        if target is not None:
            conn.execute(
                "UPDATE issue_channels SET available = available + 1 WHERE id = ?",
                (target,),
            )
        conn.execute(
            """
            UPDATE issue_invitations
               SET status = 'expired', settled_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (now, row["id"]),
        )
    return len(rows)


def _quota_summary(conn: sqlite3.Connection, issue_id: int) -> dict[str, int]:
    free = conn.execute(
        "SELECT COALESCE(SUM(available), 0) AS n FROM issue_channels WHERE issue_id = ?",
        (issue_id,),
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM issue_invitations WHERE issue_id = ? AND status = 'pending'",
        (issue_id,),
    ).fetchone()["n"]
    claimed = conn.execute(
        "SELECT COUNT(*) AS n FROM issue_entitlements WHERE issue_id = ?",
        (issue_id,),
    ).fetchone()["n"]
    return {
        "free": int(free),
        "held_by_invitations": int(pending),
        "claimed": int(claimed),
    }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _issue_dict(
    conn: sqlite3.Connection, issue: sqlite3.Row, *, include_content: bool
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": issue["id"],
        "project_id": issue["project_id"],
        "owner_id": issue["owner_id"],
        "source_revision": issue["source_revision"],
        "title": issue["title"],
        "total_quota": issue["total_quota"],
        "created_at": iso_from_ts(issue["created_at"]),
        "quota": _quota_summary(conn, issue["id"]),
        "root_channel_id": _root_channel(conn, issue["id"])["id"],
    }
    if include_content:
        data["content"] = issue["content"]
    return data


def _channel_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "parent_id": row["parent_id"],
        "manager": {
            "id": row["manager_id"],
            "username": row["manager_username"],
        },
        "status": row["status"],
        "granted": row["granted"],
        "available": row["available"],
        "created_at": iso_from_ts(row["created_at"]),
        "disabled_at": iso_from_ts(row["disabled_at"]),
    }


_INVITATION_SELECT = """
SELECT i.*, r.username AS reader_username,
       issuer.username AS invited_by_username,
       iss.title AS issue_title,
       iss.source_revision AS issue_source_revision
  FROM issue_invitations i
  JOIN accounts r ON r.id = i.reader_id
  JOIN accounts issuer ON issuer.id = i.invited_by
  JOIN issues iss ON iss.id = i.issue_id
"""


def _invitation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "issue": {
            "id": row["issue_id"],
            "title": row["issue_title"],
            "source_revision": row["issue_source_revision"],
        },
        "channel_id": row["channel_id"],
        "reader_id": row["reader_id"],
        "reader_username": row["reader_username"],
        "invited_by": {
            "id": row["invited_by"],
            "username": row["invited_by_username"],
        },
        "status": row["status"],
        "expires_at": iso_from_ts(row["expires_at"]),
        "created_at": iso_from_ts(row["created_at"]),
        "accepted_at": iso_from_ts(row["accepted_at"]),
    }


def _entitlement_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "issue_id": row["issue_id"],
        "issue": {
            "id": row["issue_id"],
            "title": row["issue_title"],
            "source_revision": row["issue_source_revision"],
        },
        "channel_id": row["channel_id"],
        "reader_id": row["reader_id"],
        "accepted_at": iso_from_ts(row["accepted_at"]),
    }


_ENTITLEMENT_SELECT = """
SELECT e.*, iss.title AS issue_title, iss.source_revision AS issue_source_revision
  FROM issue_entitlements e
  JOIN issues iss ON iss.id = e.issue_id
"""


def _fetch_invitation(
    conn: sqlite3.Connection, invitation_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        _INVITATION_SELECT + " WHERE i.id = ?", (invitation_id,)
    ).fetchone()


def _fetch_entitlement(
    conn: sqlite3.Connection, issue_id: int, reader_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        _ENTITLEMENT_SELECT + " WHERE e.issue_id = ? AND e.reader_id = ?",
        (issue_id, reader_id),
    ).fetchone()


# ---------------------------------------------------------------------------
# Issue creation and viewing (owner / manager)
# ---------------------------------------------------------------------------


@router.post("/projects/{project_id}/issues", status_code=201)
def create_issue(
    project_id: int,
    body: IssueCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    fingerprint = _fingerprint(
        {
            "project_id": project_id,
            "source_revision": body.source_revision,
            "total_quota": body.total_quota,
        }
    )

    with write_transaction(conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")
        if project["owner_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_PROJECT_OWNER",
                "Only the project owner can publish a limited issue.",
                {"project_id": project_id},
            )

        replay = _idempotency_lookup(
            conn,
            scope="issue.create",
            actor_id=actor_id,
            key=body.idempotency_key,
            fingerprint=fingerprint,
        )
        if replay is not None:
            _settle_expired(conn, int(replay["issue_id"]), now_ts())
            issue = _require_issue(conn, int(replay["issue_id"]))
            return {
                "issue": _issue_dict(conn, issue, include_content=True),
                "idempotent_replay": True,
            }

        revision = conn.execute(
            """
            SELECT * FROM project_revisions
             WHERE project_id = ? AND revision = ?
            """,
            (project_id, body.source_revision),
        ).fetchone()
        if revision is None:
            raise ApiError(
                404,
                "REVISION_NOT_FOUND",
                "The selected project revision does not exist.",
                {"project_id": project_id, "revision": body.source_revision},
            )

        ts = now_ts()
        cursor = conn.execute(
            """
            INSERT INTO issues(
                project_id, owner_id, source_revision, title, content,
                total_quota, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                actor_id,
                body.source_revision,
                revision["title"],
                revision["content"],
                body.total_quota,
                ts,
            ),
        )
        issue_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO issue_channels(
                issue_id, parent_id, manager_id, status, granted, available, created_at
            ) VALUES (?, NULL, ?, 'active', ?, ?, ?)
            """,
            (issue_id, actor_id, body.total_quota, body.total_quota, ts),
        )
        _idempotency_store(
            conn,
            scope="issue.create",
            actor_id=actor_id,
            key=body.idempotency_key,
            fingerprint=fingerprint,
            payload={"issue_id": issue_id},
        )
        issue = _require_issue(conn, issue_id)

    return {
        "issue": _issue_dict(conn, issue, include_content=True),
        "idempotent_replay": False,
    }


@router.get("/projects/{project_id}/issues")
def list_project_issues(
    project_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")
        if project["owner_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_PROJECT_OWNER",
                "Only the project owner can list limited issues.",
                {"project_id": project_id},
            )
        rows = conn.execute(
            "SELECT * FROM issues WHERE project_id = ? ORDER BY id", (project_id,)
        ).fetchall()
        now = now_ts()
        for row in rows:
            _settle_expired(conn, row["id"], now)
        return {
            "project_id": project_id,
            "issues": [
                _issue_dict(conn, row, include_content=True) for row in rows
            ],
        }


@router.get("/issues/{issue_id}")
def get_issue(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        role = _issue_actor_role(conn, issue, actor_id)
        if role is None:
            raise ApiError(
                403,
                "ISSUE_ACCESS_DENIED",
                "Only the project owner or a channel manager can inspect this issue.",
                {"issue_id": issue_id},
            )
        _settle_expired(conn, issue_id, now_ts())
        return {
            "issue": _issue_dict(
                conn, issue, include_content=(role == "owner")
            ),
            "viewer_role": role,
        }


# ---------------------------------------------------------------------------
# Quota allocation
# ---------------------------------------------------------------------------


def _resolve_source_channel(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row,
    actor_id: int,
    requested_parent: int | None,
) -> sqlite3.Row:
    if requested_parent is not None:
        parent = _require_channel(conn, issue["id"], requested_parent)
        if parent["manager_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_CHANNEL_MANAGER",
                "Only the manager of a channel can allocate from it.",
                {"channel_id": requested_parent},
            )
        return parent

    if actor_id == issue["owner_id"]:
        return _root_channel(conn, issue["id"])

    candidates = conn.execute(
        """
        SELECT c.*, m.username AS manager_username
          FROM issue_channels c
          JOIN accounts m ON m.id = c.manager_id
         WHERE c.issue_id = ? AND c.manager_id = ? AND c.status = 'active'
         ORDER BY c.id
        """,
        (issue["id"], actor_id),
    ).fetchall()
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ApiError(
            409,
            "NO_USABLE_CHANNEL",
            "You do not manage an active channel in this issue.",
            {"issue_id": issue["id"]},
        )
    raise ApiError(
        409,
        "MULTIPLE_SOURCE_CHANNELS",
        "You manage several active channels; specify parent_channel_id.",
        {"issue_id": issue["id"], "channel_ids": [c["id"] for c in candidates]},
    )


@router.post("/issues/{issue_id}/channels", status_code=201)
def allocate_channel(
    issue_id: int,
    body: ChannelAllocateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    fingerprint = _fingerprint(
        {
            "issue_id": issue_id,
            "parent_channel_id": body.parent_channel_id,
            "manager_username": body.manager_username,
            "quota": body.quota,
        }
    )

    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        replay = _idempotency_lookup(
            conn,
            scope="issue.allocate",
            actor_id=actor_id,
            key=body.idempotency_key,
            fingerprint=fingerprint,
        )
        if replay is not None:
            _settle_expired(conn, issue_id, now_ts())
            row = _channel_username_row(conn, int(replay["channel_id"]))
            return {"channel": _channel_dict(row), "idempotent_replay": True}

        _settle_expired(conn, issue_id, now_ts())
        parent = _resolve_source_channel(
            conn,
            issue=issue,
            actor_id=actor_id,
            requested_parent=body.parent_channel_id,
        )
        if parent["status"] != "active":
            raise ApiError(
                409,
                "CHANNEL_DISABLED",
                "Cannot allocate from a disabled channel.",
                {"channel_id": parent["id"]},
            )
        if parent["available"] < body.quota:
            raise ApiError(
                409,
                "INSUFFICIENT_QUOTA",
                "The source channel does not have enough available quota.",
                {"channel_id": parent["id"], "available": parent["available"]},
            )
        manager = _require_account_by_username(conn, body.manager_username)

        ts = now_ts()
        conn.execute(
            "UPDATE issue_channels SET available = available - ? WHERE id = ?",
            (body.quota, parent["id"]),
        )
        # A fresh channel row every time, even for the same manager account.
        cursor = conn.execute(
            """
            INSERT INTO issue_channels(
                issue_id, parent_id, manager_id, status, granted, available, created_at
            ) VALUES (?, ?, ?, 'active', ?, ?, ?)
            """,
            (issue_id, parent["id"], manager["id"], body.quota, body.quota, ts),
        )
        channel_id = cursor.lastrowid
        _idempotency_store(
            conn,
            scope="issue.allocate",
            actor_id=actor_id,
            key=body.idempotency_key,
            fingerprint=fingerprint,
            payload={"issue_id": issue_id, "channel_id": channel_id},
        )
        row = _channel_username_row(conn, channel_id)

    return {"channel": _channel_dict(row), "idempotent_replay": False}


# ---------------------------------------------------------------------------
# Channel listing (owner sees all, manager sees own)
# ---------------------------------------------------------------------------


_CHANNEL_SELECT = """
SELECT c.*, m.username AS manager_username
  FROM issue_channels c
  JOIN accounts m ON m.id = c.manager_id
"""


@router.get("/issues/{issue_id}/channels")
def list_channels(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        role = _issue_actor_role(conn, issue, actor_id)
        if role is None:
            raise ApiError(
                403,
                "ISSUE_ACCESS_DENIED",
                "Only the project owner or a channel manager can list channels.",
                {"issue_id": issue_id},
            )
        _settle_expired(conn, issue_id, now_ts())
        if role == "owner":
            # Exclude the hidden root pool; its balance is reported separately
            # as ``unallocated_owner_quota``.
            rows = conn.execute(
                _CHANNEL_SELECT
                + " WHERE c.issue_id = ? AND c.parent_id IS NOT NULL ORDER BY c.id",
                (issue_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                _CHANNEL_SELECT
                + " WHERE c.issue_id = ? AND c.manager_id = ? AND c.parent_id IS NOT NULL"
                " ORDER BY c.id",
                (issue_id, actor_id),
            ).fetchall()
        return {
            "issue_id": issue_id,
            "viewer_role": role,
            "unallocated_owner_quota": _root_channel(conn, issue_id)["available"],
            "channels": [_channel_dict(row) for row in rows],
            "quota": _quota_summary(conn, issue_id),
        }


# ---------------------------------------------------------------------------
# Disabling channels
# ---------------------------------------------------------------------------


def _assert_can_disable(
    conn: sqlite3.Connection, issue: sqlite3.Row, actor_id: int, channel: sqlite3.Row
) -> None:
    if channel["parent_id"] is None:
        raise ApiError(
            409,
            "ROOT_CHANNEL_NOT_DISABLEABLE",
            "The owner's unallocated pool cannot be disabled.",
            {"channel_id": channel["id"]},
        )
    if actor_id == issue["owner_id"]:
        return
    parent = conn.execute(
        "SELECT * FROM issue_channels WHERE id = ?", (channel["parent_id"],)
    ).fetchone()
    if parent is None or parent["manager_id"] != actor_id:
        raise ApiError(
            403,
            "CANNOT_DISABLE_CHANNEL",
            "A manager may only disable a child channel they directly allocated.",
            {"channel_id": channel["id"]},
        )


@router.post("/issues/{issue_id}/channels/{channel_id}/disable")
def disable_channel(
    issue_id: int,
    channel_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        channel = _require_channel(conn, issue_id, channel_id)
        _assert_can_disable(conn, issue, actor_id, channel)
        _settle_expired(conn, issue_id, now_ts())

        if channel["status"] == "disabled":
            row = _channel_username_row(conn, channel_id)
            return {
                "channel": _channel_dict(row),
                "cascaded_channel_ids": [],
                "released_to_parent": 0,
                "idempotent_replay": True,
            }

        # Collect the whole subtree (including this channel).
        subtree = conn.execute(
            """
            WITH RECURSIVE sub(id) AS (
                SELECT id FROM issue_channels WHERE id = ?
                UNION
                SELECT c.id FROM issue_channels c JOIN sub ON c.parent_id = sub.id
            )
            SELECT id FROM sub
            """,
            (channel_id,),
        ).fetchall()
        ids = [int(r["id"]) for r in subtree]
        free_total = conn.execute(
            f"SELECT COALESCE(SUM(available), 0) AS n FROM issue_channels "
            f"WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchone()["n"]
        ts = now_ts()
        for cid in ids:
            conn.execute(
                """
                UPDATE issue_channels
                   SET status = 'disabled',
                       available = 0,
                       disabled_at = COALESCE(disabled_at, ?),
                       disabled_by = COALESCE(disabled_by, ?)
                 WHERE id = ?
                """,
                (ts, actor_id, cid),
            )
        # Free seats in the disabled branch return to its direct parent.
        conn.execute(
            "UPDATE issue_channels SET available = available + ? WHERE id = ?",
            (int(free_total), channel["parent_id"]),
        )
        row = _channel_username_row(conn, channel_id)
        cascaded = [cid for cid in ids if cid != channel_id]
        parent_row = _channel_username_row(conn, int(channel["parent_id"]))
        return {
            "channel": _channel_dict(row),
            "cascaded_channel_ids": cascaded,
            "released_to_parent": {
                "channel_id": parent_row["id"],
                "amount": int(free_total),
                "available": parent_row["available"],
            },
            "idempotent_replay": False,
        }


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


def _issue_scope_for_channel(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row,
    actor_id: int,
    channel_id: int | None,
) -> sqlite3.Row:
    if channel_id is not None:
        channel = _require_channel(conn, issue["id"], channel_id)
        if channel["manager_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_CHANNEL_MANAGER",
                "Only the manager of a channel can invite from it.",
                {"channel_id": channel_id},
            )
        return channel
    return _resolve_source_channel(
        conn, issue=issue, actor_id=actor_id, requested_parent=None
    )


def _create_invitation_core(
    conn: sqlite3.Connection,
    *,
    issue: sqlite3.Row,
    actor_id: int,
    channel: sqlite3.Row,
    reader_username: str,
    ttl_seconds: int,
    key: str | None,
    fingerprint: str | None,
) -> dict[str, Any]:
    if channel["status"] != "active":
        raise ApiError(
            409,
            "CHANNEL_DISABLED",
            "Cannot invite from a disabled channel.",
            {"channel_id": channel["id"]},
        )
    reader = _require_account_by_username(conn, reader_username)

    existing_entitlement = _fetch_entitlement(conn, issue["id"], reader["id"])
    if existing_entitlement is not None:
        raise ApiError(
            409,
            "ALREADY_ENTITLED",
            "This reader already holds an entitlement for the issue.",
            {
                "issue_id": issue["id"],
                "reader_id": reader["id"],
                "entitlement_id": existing_entitlement["id"],
            },
        )

    existing_pending = conn.execute(
        """
        SELECT id FROM issue_invitations
         WHERE issue_id = ? AND reader_id = ? AND status = 'pending'
        """,
        (issue["id"], reader["id"]),
    ).fetchone()
    if existing_pending is not None:
        # Re-sending must neither take another seat nor extend the deadline.
        row = _fetch_invitation(conn, existing_pending["id"])
        if key is not None:
            _idempotency_store(
                conn,
                scope="issue.invite",
                actor_id=actor_id,
                key=key,
                fingerprint=fingerprint or "",
                payload={
                    "issue_id": issue["id"],
                    "invitation_id": existing_pending["id"],
                },
            )
        return {
            "invitation": _invitation_dict(row),
            "idempotent_replay": True,
            "reused_existing_invitation": True,
        }

    if channel["available"] < 1:
        raise ApiError(
            409,
            "INSUFFICIENT_QUOTA",
            "The channel has no available quota for another invitation.",
            {"channel_id": channel["id"], "available": channel["available"]},
        )

    ts = now_ts()
    expires_at = ts + ttl_seconds
    conn.execute(
        "UPDATE issue_channels SET available = available - 1 WHERE id = ?",
        (channel["id"],),
    )
    cursor = conn.execute(
        """
        INSERT INTO issue_invitations(
            issue_id, channel_id, reader_id, invited_by, status,
            expires_at, created_at
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (issue["id"], channel["id"], reader["id"], actor_id, expires_at, ts),
    )
    invitation_id = cursor.lastrowid
    if key is not None:
        _idempotency_store(
            conn,
            scope="issue.invite",
            actor_id=actor_id,
            key=key,
            fingerprint=fingerprint or "",
            payload={"issue_id": issue["id"], "invitation_id": invitation_id},
        )
    row = _fetch_invitation(conn, invitation_id)
    return {
        "invitation": _invitation_dict(row),
        "idempotent_replay": False,
        "reused_existing_invitation": False,
    }


def _invite(
    *,
    issue_id: int,
    body: IssueInvitationCreateIn,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
    forced_channel_id: int | None,
) -> dict[str, Any]:
    actor_id = account["id"]
    requested_channel = forced_channel_id if forced_channel_id is not None else body.channel_id
    fingerprint = (
        _fingerprint(
            {
                "issue_id": issue_id,
                "channel_id": requested_channel,
                "reader_username": body.reader_username,
                "ttl_seconds": body.ttl_seconds,
            }
        )
        if body.idempotency_key is not None
        else None
    )

    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        if body.idempotency_key is not None:
            replay = _idempotency_lookup(
                conn,
                scope="issue.invite",
                actor_id=actor_id,
                key=body.idempotency_key,
                fingerprint=fingerprint or "",
            )
            if replay is not None:
                _settle_expired(conn, issue_id, now_ts())
                row = _fetch_invitation(conn, int(replay["invitation_id"]))
                return {
                    "invitation": _invitation_dict(row),
                    "idempotent_replay": True,
                    "reused_existing_invitation": False,
                }

        _settle_expired(conn, issue_id, now_ts())
        channel = _issue_scope_for_channel(
            conn, issue=issue, actor_id=actor_id, channel_id=requested_channel
        )
        return _create_invitation_core(
            conn,
            issue=issue,
            actor_id=actor_id,
            channel=channel,
            reader_username=body.reader_username,
            ttl_seconds=body.ttl_seconds,
            key=body.idempotency_key,
            fingerprint=fingerprint,
        )


@router.post("/issues/{issue_id}/invitations", status_code=201)
def invite_from_issue(
    issue_id: int,
    body: IssueInvitationCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _invite(
        issue_id=issue_id,
        body=body,
        account=account,
        conn=conn,
        forced_channel_id=None,
    )


@router.post("/issues/{issue_id}/channels/{channel_id}/invitations", status_code=201)
def invite_from_channel(
    issue_id: int,
    channel_id: int,
    body: IssueInvitationCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _invite(
        issue_id=issue_id,
        body=body,
        account=account,
        conn=conn,
        forced_channel_id=channel_id,
    )


@router.get("/issues/{issue_id}/channels/{channel_id}/invitations")
def list_channel_invitations(
    issue_id: int,
    channel_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        issue = _require_issue(conn, issue_id)
        channel = _require_channel(conn, issue_id, channel_id)
        if channel["manager_id"] != actor_id and issue["owner_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_CHANNEL_MANAGER",
                "Only the channel manager or the project owner can list invitations.",
                {"channel_id": channel_id},
            )
        _settle_expired(conn, issue_id, now_ts())
        rows = conn.execute(
            _INVITATION_SELECT + " WHERE i.channel_id = ? ORDER BY i.id",
            (channel_id,),
        ).fetchall()
        return {
            "issue_id": issue_id,
            "channel_id": channel_id,
            "invitations": [_invitation_dict(row) for row in rows],
        }


# ---------------------------------------------------------------------------
# Reader: invitations, acceptance, reading the issue
# ---------------------------------------------------------------------------


@router.get("/me/issue-invitations")
def my_issue_invitations(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    reader_id = account["id"]
    with write_transaction(conn):
        issue_ids = conn.execute(
            """
            SELECT DISTINCT issue_id FROM issue_invitations
             WHERE reader_id = ? AND status = 'pending' AND expires_at <= ?
            """,
            (reader_id, now_ts()),
        ).fetchall()
        for row in issue_ids:
            _settle_expired(conn, int(row["issue_id"]), now_ts())
        rows = conn.execute(
            _INVITATION_SELECT + " WHERE i.reader_id = ? ORDER BY i.id",
            (reader_id,),
        ).fetchall()
        entitlements = conn.execute(
            _ENTITLEMENT_SELECT + " WHERE e.reader_id = ? ORDER BY e.id",
            (reader_id,),
        ).fetchall()
        return {
            "reader_id": reader_id,
            "invitations": [_invitation_dict(row) for row in rows],
            "entitlements": [_entitlement_dict(row) for row in entitlements],
        }


@router.post("/issues/invitations/{invitation_id}/accept")
def accept_issue_invitation(
    invitation_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with write_transaction(conn):
        invitation = conn.execute(
            "SELECT * FROM issue_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        if invitation is None:
            raise ApiError(
                404, "ISSUE_INVITATION_NOT_FOUND", "Issue invitation was not found."
            )
        if invitation["reader_id"] != actor_id:
            raise ApiError(
                403,
                "NOT_INVITED_READER",
                "Only the invited reader account can claim this invitation.",
                {"invitation_id": invitation_id},
            )

        _settle_expired(conn, int(invitation["issue_id"]), now_ts())
        invitation = conn.execute(
            "SELECT * FROM issue_invitations WHERE id = ?", (invitation_id,)
        ).fetchone()

        # Repeated acceptance returns the original claim result.
        if invitation["status"] == "accepted":
            entitlement = _fetch_entitlement(
                conn, int(invitation["issue_id"]), actor_id
            )
            return {
                "status": "accepted",
                "idempotent_replay": True,
                "entitlement": _entitlement_dict(entitlement),
                "invitation": _invitation_dict(_fetch_invitation(conn, invitation_id)),
            }

        if invitation["status"] == "expired" or invitation["expires_at"] <= now_ts():
            # Defensive: settle would already have flipped it, but keep the
            # explicit rejection before any entitlement is created.
            if invitation["status"] == "pending":
                _settle_expired(conn, int(invitation["issue_id"]), now_ts())
            raise ApiError(
                410,
                "ISSUE_INVITATION_EXPIRED",
                "The claim deadline has passed; the seat was released.",
                {
                    "invitation_id": invitation_id,
                    "expires_at": iso_from_ts(invitation["expires_at"]),
                },
            )

        # A reader may hold at most one entitlement per issue.
        existing = _fetch_entitlement(conn, int(invitation["issue_id"]), actor_id)
        if existing is not None:
            raise ApiError(
                409,
                "ALREADY_ENTITLED",
                "This reader already holds an entitlement for the issue.",
                {"entitlement_id": existing["id"]},
            )

        ts = now_ts()
        conn.execute(
            """
            UPDATE issue_invitations
               SET status = 'accepted', accepted_at = ?, settled_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (ts, ts, invitation_id),
        )
        cursor = conn.execute(
            """
            INSERT INTO issue_entitlements(
                issue_id, reader_id, channel_id, invitation_id, accepted_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                invitation["issue_id"],
                actor_id,
                invitation["channel_id"],
                invitation_id,
                ts,
            ),
        )
        entitlement = conn.execute(
            _ENTITLEMENT_SELECT + " WHERE e.id = ?", (cursor.lastrowid,)
        ).fetchone()
        return {
            "status": "accepted",
            "idempotent_replay": False,
            "entitlement": _entitlement_dict(entitlement),
            "invitation": _invitation_dict(_fetch_invitation(conn, invitation_id)),
        }


@router.get("/me/issues")
def my_claimed_issues(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    reader_id = account["id"]
    with read_transaction(conn):
        rows = conn.execute(
            _ENTITLEMENT_SELECT + " WHERE e.reader_id = ? ORDER BY e.id",
            (reader_id,),
        ).fetchall()
        return {
            "reader_id": reader_id,
            "issues": [
                {
                    **_entitlement_dict(row),
                    "content_available": True,
                }
                for row in rows
            ],
        }


@router.get("/issues/{issue_id}/read")
def read_claimed_issue(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    actor_id = account["id"]
    with read_transaction(conn):
        issue = conn.execute(
            "SELECT * FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        if issue is None:
            raise ApiError(404, "ISSUE_NOT_FOUND", "Issue was not found.")
        entitlement = _fetch_entitlement(conn, issue_id, actor_id)
        if entitlement is None:
            raise ApiError(
                403,
                "ISSUE_NOT_CLAIMED",
                "An accepted entitlement is required to read this issue.",
                {"issue_id": issue_id},
            )
        return {
            "issue": {
                "id": issue["id"],
                "project_id": issue["project_id"],
                "source_revision": issue["source_revision"],
                "title": issue["title"],
                "content": issue["content"],
                "created_at": iso_from_ts(issue["created_at"]),
            },
            "entitlement": _entitlement_dict(entitlement),
        }
