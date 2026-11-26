"""Creator workbench account-linking HTTP service.

Correctness design:

* Sessions are opaque local bearer tokens stored as SHA-256 hashes.
* An invitation needs one explicit confirmation from each of two accounts.
* Graph mutations and project writes use ``BEGIN IMMEDIATE``.  A project
  mutation therefore checks reachability and commits its revision while
  holding the same SQLite write lock that serializes edge changes.
* Edges are versioned in the same transaction as a graph-version bump.
  Project revisions record the graph version used for authorization.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .curation import map_through_edit
from .db import get_db, init_db, read_transaction, write_transaction
from .deps import get_current_account
from .errors import ApiError, error_body
from .issues import router as issues_router
from .royalties import router as royalties_router
from .schemas import (
    CredentialsIn,
    CurationCreateIn,
    InvitationCreateIn,
    ProjectCreateIn,
    ProjectUpdateIn,
)
from .security import (
    SESSION_TTL_SECONDS,
    hash_password,
    hash_secret,
    iso_from_ts,
    new_invitation_token,
    new_session_token,
    now_ts,
    verify_password,
)

# The first ? is the viewer account id.  The recursive term traverses both
# directions of every currently active undirected edge.
REACHABLE_CTE = """
WITH RECURSIVE reach(id) AS (
    SELECT ?
    UNION
    SELECT CASE WHEN e.account_a = reach.id
                THEN e.account_b
                ELSE e.account_a END
      FROM association_edges e
      JOIN reach ON reach.id IN (e.account_a, e.account_b)
     WHERE e.deleted_version IS NULL
)
"""


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Creator Workbench Account Link API",
    version="1.0.0",
    description="Local account sessions, expiring two-party invitations, transitive sharing, and safe edge removal.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(issues_router)
app.include_router(royalties_router)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_body(
            "VALIDATION_ERROR",
            "Request validation failed.",
            {"issues": jsonable_encoder(exc.errors())},
        ),
    )


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_operational_handler(
    _: Request, exc: sqlite3.OperationalError
) -> JSONResponse:
    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
        return JSONResponse(
            status_code=503,
            content=error_body(
                "DB_BUSY", "Database was busy; the request is safe to retry."
            ),
            headers={"Retry-After": "1"},
        )
    return JSONResponse(
        status_code=500,
        content=error_body("DATABASE_ERROR", "A database error occurred."),
    )


# ---------------------------------------------------------------------------
# Serialization and shared helpers
# ---------------------------------------------------------------------------


def account_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "username": row["username"]}


def session_dict(token: str, expires_at: float) -> dict[str, Any]:
    return {
        "token": token,
        "token_type": "Bearer",
        "expires_at": iso_from_ts(expires_at),
    }


def project_dict(row: sqlite3.Row, viewer_id: int | None = None) -> dict[str, Any]:
    owner_id = row["owner_id"]
    data = {
        "id": row["id"],
        "owner_id": owner_id,
        "owner": {
            "id": owner_id,
            "username": row["owner_username"] if "owner_username" in row.keys() else None,
        },
        "title": row["title"],
        "content": row["content"],
        "revision": row["revision"],
        "created_at": iso_from_ts(row["created_at"]),
        "updated_at": iso_from_ts(row["updated_at"]),
    }
    if viewer_id is not None:
        data["is_owner"] = viewer_id == owner_id
        data["access"] = "owner" if viewer_id == owner_id else "shared"
    return data


def edge_dict(
    row: sqlite3.Row, viewer_id: int | None = None
) -> dict[str, Any]:
    a = row["account_a"]
    b = row["account_b"]
    peer_id = None
    if viewer_id == a:
        peer_id = b
    elif viewer_id == b:
        peer_id = a
    return {
        "id": row["id"],
        "account_a": a,
        "account_b": b,
        "peer_id": peer_id,
        "account_a_username": row["account_a_username"]
        if "account_a_username" in row.keys()
        else None,
        "account_b_username": row["account_b_username"]
        if "account_b_username" in row.keys()
        else None,
        "invitation_id": row["invitation_id"],
        "created_version": row["created_version"],
        "created_at": iso_from_ts(row["created_at"]),
        "deleted": row["deleted_version"] is not None,
        "deleted_version": row["deleted_version"],
        "deleted_at": iso_from_ts(row["deleted_at"]),
    }


def invitation_status(row: sqlite3.Row) -> str:
    if row["consumed_at"] is not None:
        return "consumed"
    if row["expires_at"] <= now_ts():
        return "expired"
    return "pending"


def invitation_detail(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    confirmations = conn.execute(
        """
        SELECT c.account_id, c.confirmed_at, a.username
          FROM invitation_confirmations c
          JOIN accounts a ON a.id = c.account_id
         WHERE c.invitation_id = ?
         ORDER BY c.confirmed_at, c.id
        """,
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "status": invitation_status(row),
        "created_by": row["created_by"],
        "invitee_id": row["invitee_id"],
        "expires_at": iso_from_ts(row["expires_at"]),
        "created_at": iso_from_ts(row["created_at"]),
        "consumed_at": iso_from_ts(row["consumed_at"]),
        "consumed_edge_id": row["consumed_edge_id"],
        "confirmation_count": len(confirmations),
        "confirmations": [
            {
                "account_id": item["account_id"],
                "username": item["username"],
                "confirmed_at": iso_from_ts(item["confirmed_at"]),
            }
            for item in confirmations
        ],
    }


def current_graph_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT version FROM graph_state WHERE id = 1").fetchone()
    return int(row["version"])


# ---------------------------------------------------------------------------
# Health and local accounts/sessions
# ---------------------------------------------------------------------------


@app.get("/health")
def health(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, str]:
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "creator-workbench-account-link",
        "docs": "/docs",
        "health": "/health",
    }


@app.post("/auth/register", status_code=201)
def register(
    body: CredentialsIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    ts = now_ts()
    try:
        with write_transaction(conn):
            cursor = conn.execute(
                """
                INSERT INTO accounts(username, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (
                    body.username,
                    hash_password(body.password) if body.password else "local-session-only",
                    ts,
                ),
            )
            account_id = cursor.lastrowid
            token = new_session_token()
            conn.execute(
                """
                INSERT INTO sessions(token_hash, account_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    hash_secret(token),
                    account_id,
                    ts,
                    ts + SESSION_TTL_SECONDS,
                ),
            )
            account = conn.execute(
                "SELECT id, username, created_at FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise ApiError(
            409,
            "USERNAME_TAKEN",
            f"Username {body.username!r} is already registered.",
        ) from exc

    return {
        "account": account_dict(account),
        "session": session_dict(token, ts + SESSION_TTL_SECONDS),
    }


@app.post("/auth/login")
def login(
    body: CredentialsIn, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    account = conn.execute(
        "SELECT * FROM accounts WHERE username = ?", (body.username,)
    ).fetchone()
    # Same error whether the user or password is wrong.
    if account is None or not verify_password(body.password, account["password_hash"]):
        raise ApiError(401, "INVALID_CREDENTIALS", "Username or password is incorrect.")

    ts = now_ts()
    token = new_session_token()
    expires_at = ts + SESSION_TTL_SECONDS
    conn.execute(
        """
        INSERT INTO sessions(token_hash, account_id, created_at, expires_at)
        VALUES (?, ?, ?, ?)
        """,
        (hash_secret(token), account["id"], ts, expires_at),
    )
    return {
        "account": account_dict(account),
        "session": session_dict(token, expires_at),
    }


@app.get("/me")
def me(account: sqlite3.Row = Depends(get_current_account)) -> dict[str, Any]:
    return {"account": account_dict(account)}


@app.post("/auth/logout")
def logout(
    request: Request,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, bool]:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-session-token")
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_secret(token),))
    return {"logged_out": True}


# ---------------------------------------------------------------------------
# Two-party invitations and association edges
# ---------------------------------------------------------------------------


@app.post("/invitations", status_code=201)
def create_invitation(
    body: InvitationCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    invitee_id: int | None = None
    if body.invitee_username is not None:
        invitee = conn.execute(
            "SELECT id FROM accounts WHERE username = ?",
            (body.invitee_username,),
        ).fetchone()
        if invitee is None:
            raise ApiError(
                404,
                "INVITEE_NOT_FOUND",
                f"Invitee {body.invitee_username!r} does not exist.",
            )
        invitee_id = invitee["id"]

    token = new_invitation_token()
    ts = now_ts()
    expires_at = ts + body.ttl_seconds
    with write_transaction(conn):
        cursor = conn.execute(
            """
            INSERT INTO invitations(
                token_hash, created_by, invitee_id, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (hash_secret(token), account["id"], invitee_id, expires_at, ts),
        )
        invitation_id = cursor.lastrowid
        row = conn.execute(
            "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        detail = invitation_detail(conn, row)

    # Raw token is returned exactly once; only its SHA-256 hash is stored.
    return {"token": token, "invitation": detail}


@app.get("/invitations/{token}")
def get_invitation(
    token: str,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM invitations WHERE token_hash = ?", (hash_secret(token),)
    ).fetchone()
    if row is None:
        raise ApiError(404, "INVITATION_NOT_FOUND", "Invitation token was not found.")
    if (
        row["invitee_id"] is not None
        and account["id"] not in (row["created_by"], row["invitee_id"])
    ):
        raise ApiError(403, "NOT_INVITED_ACCOUNT", "This invitation is restricted to another pair.")
    return {"invitation": invitation_detail(conn, row)}


@app.post("/invitations/{token}/confirm")
def confirm_invitation(
    token: str,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]

    with write_transaction(conn):
        invitation = conn.execute(
            "SELECT * FROM invitations WHERE token_hash = ?",
            (hash_secret(token),),
        ).fetchone()
        if invitation is None:
            raise ApiError(404, "INVITATION_NOT_FOUND", "Invitation token was not found.")

        if (
            invitation["invitee_id"] is not None
            and viewer_id
            not in (invitation["created_by"], invitation["invitee_id"])
        ):
            raise ApiError(
                403,
                "NOT_INVITED_ACCOUNT",
                "This account is not a participant in the invitation.",
            )

        # Consumed tokens can never create another edge.
        if invitation["consumed_at"] is not None:
            own = conn.execute(
                """
                SELECT id FROM invitation_confirmations
                 WHERE invitation_id = ? AND account_id = ?
                """,
                (invitation["id"], viewer_id),
            ).fetchone()
            raise ApiError(
                409,
                "INVITATION_CONSUMED",
                "Invitation has already been consumed and cannot be replayed.",
                {
                    "consumed_edge_id": invitation["consumed_edge_id"],
                    "your_confirmation_exists": own is not None,
                },
            )

        if invitation["expires_at"] <= now_ts():
            raise ApiError(
                410,
                "INVITATION_EXPIRED",
                "Invitation has expired and cannot be confirmed.",
                {"expires_at": iso_from_ts(invitation["expires_at"])},
            )

        inserted = conn.execute(
            """
            INSERT OR IGNORE INTO invitation_confirmations(
                invitation_id, account_id, confirmed_at
            ) VALUES (?, ?, ?)
            """,
            (invitation["id"], viewer_id, now_ts()),
        )
        duplicate_confirmation = inserted.rowcount == 0

        confirmations = conn.execute(
            """
            SELECT account_id
              FROM invitation_confirmations
             WHERE invitation_id = ?
             ORDER BY confirmed_at, id
            """,
            (invitation["id"],),
        ).fetchall()
        count = len(confirmations)

        if count == 1:
            row = conn.execute(
                "SELECT * FROM invitations WHERE id = ?", (invitation["id"],)
            ).fetchone()
            return {
                "status": "pending",
                "duplicate_confirmation": duplicate_confirmation,
                "invitation": invitation_detail(conn, row),
            }

        if count != 2:
            # Defensive: the consumed check and unique confirmation constraint
            # make more than two pending confirmations unreachable.
            raise ApiError(
                409,
                "INVITATION_STATE_INVALID",
                "Invitation cannot be completed because its state is invalid.",
                {"confirmation_count": count},
            )

        first_id, second_id = sorted(item["account_id"] for item in confirmations)
        existing_edge = conn.execute(
            """
            SELECT * FROM association_edges
             WHERE account_a = ? AND account_b = ?
               AND deleted_version IS NULL
            """,
            (first_id, second_id),
        ).fetchone()

        edge_already_existed = existing_edge is not None
        if existing_edge is not None:
            edge_row = existing_edge
            edge_id = existing_edge["id"]
        else:
            conn.execute(
                "UPDATE graph_state SET version = version + 1 WHERE id = 1"
            )
            version = current_graph_version(conn)
            ts = now_ts()
            edge_cursor = conn.execute(
                """
                INSERT INTO association_edges(
                    account_a, account_b, invitation_id, created_by,
                    created_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    first_id,
                    second_id,
                    invitation["id"],
                    viewer_id,
                    version,
                    ts,
                ),
            )
            edge_id = edge_cursor.lastrowid
            conn.execute(
                """
                INSERT INTO graph_events(
                    version, event_type, edge_id, account_a, account_b,
                    actor_id, created_at
                ) VALUES (?, 'linked', ?, ?, ?, ?, ?)
                """,
                (version, edge_id, first_id, second_id, viewer_id, ts),
            )
            edge_row = conn.execute(
                "SELECT * FROM association_edges WHERE id = ?", (edge_id,)
            ).fetchone()

        conn.execute(
            """
            UPDATE invitations
               SET consumed_at = ?, consumed_edge_id = ?
             WHERE id = ?
            """,
            (now_ts(), edge_id, invitation["id"]),
        )
        invitation_row = conn.execute(
            "SELECT * FROM invitations WHERE id = ?", (invitation["id"],)
        ).fetchone()
        detailed_edge = conn.execute(
            """
            SELECT e.*, ua.username AS account_a_username,
                   ub.username AS account_b_username
              FROM association_edges e
              JOIN accounts ua ON ua.id = e.account_a
              JOIN accounts ub ON ub.id = e.account_b
             WHERE e.id = ?
            """,
            (edge_id,),
        ).fetchone()

        return {
            "status": "consumed",
            "duplicate_confirmation": False,
            "edge_already_existed": edge_already_existed,
            "edge": edge_dict(detailed_edge, viewer_id),
            "invitation": invitation_detail(conn, invitation_row),
        }


@app.get("/edges")
def list_edges(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT e.*, ua.username AS account_a_username,
               ub.username AS account_b_username
          FROM association_edges e
          JOIN accounts ua ON ua.id = e.account_a
          JOIN accounts ub ON ub.id = e.account_b
         WHERE e.deleted_version IS NULL
           AND ? IN (e.account_a, e.account_b)
         ORDER BY e.id
        """,
        (account["id"],),
    ).fetchall()
    return {"edges": [edge_dict(row, account["id"]) for row in rows]}


@app.delete("/edges/{edge_id}")
def delete_edge(
    edge_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    with write_transaction(conn):
        edge = conn.execute(
            """
            SELECT e.*, ua.username AS account_a_username,
                   ub.username AS account_b_username
              FROM association_edges e
              JOIN accounts ua ON ua.id = e.account_a
              JOIN accounts ub ON ub.id = e.account_b
             WHERE e.id = ?
            """,
            (edge_id,),
        ).fetchone()
        if edge is None:
            raise ApiError(404, "EDGE_NOT_FOUND", "Association edge was not found.")
        if viewer_id not in (edge["account_a"], edge["account_b"]):
            raise ApiError(
                403, "NOT_EDGE_PARTICIPANT", "Only a linked account can delete this edge."
            )

        if edge["deleted_version"] is not None:
            return {
                "deleted": True,
                "idempotent_replay": True,
                "edge": edge_dict(edge, viewer_id),
            }

        conn.execute("UPDATE graph_state SET version = version + 1 WHERE id = 1")
        version = current_graph_version(conn)
        ts = now_ts()
        conn.execute(
            """
            UPDATE association_edges
               SET deleted_version = ?, deleted_by = ?, deleted_at = ?
             WHERE id = ?
            """,
            (version, viewer_id, ts, edge_id),
        )
        conn.execute(
            """
            INSERT INTO graph_events(
                version, event_type, edge_id, account_a, account_b,
                actor_id, created_at
            ) VALUES (?, 'unlinked', ?, ?, ?, ?, ?)
            """,
            (
                version,
                edge_id,
                edge["account_a"],
                edge["account_b"],
                viewer_id,
                ts,
            ),
        )
        row = conn.execute(
            """
            SELECT e.*, ua.username AS account_a_username,
                   ub.username AS account_b_username
              FROM association_edges e
              JOIN accounts ua ON ua.id = e.account_a
              JOIN accounts ub ON ub.id = e.account_b
             WHERE e.id = ?
            """,
            (edge_id,),
        ).fetchone()

    return {"deleted": True, "idempotent_replay": False, "edge": edge_dict(row, viewer_id)}


# ---------------------------------------------------------------------------
# Projects, authorization, and actor history
# ---------------------------------------------------------------------------


@app.post("/projects", status_code=201)
def create_project(
    body: ProjectCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    title = body.title.strip()
    if not title:
        raise ApiError(422, "VALIDATION_ERROR", "title must not be blank.")

    viewer_id = account["id"]
    ts = now_ts()
    with write_transaction(conn):
        graph_version = current_graph_version(conn)
        cursor = conn.execute(
            """
            INSERT INTO projects(owner_id, title, content, revision, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            """,
            (viewer_id, title, body.content, ts, ts),
        )
        project_id = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO project_revisions(
                project_id, revision, action, actor_id, title, content,
                graph_version, created_at
            ) VALUES (?, 1, 'created', ?, ?, ?, ?, ?)
            """,
            (project_id, viewer_id, title, body.content, graph_version, ts),
        )
        row = conn.execute(
            """
            SELECT p.*, a.username AS owner_username
              FROM projects p JOIN accounts a ON a.id = p.owner_id
             WHERE p.id = ?
            """,
            (project_id,),
        ).fetchone()

    return {"project": project_dict(row, viewer_id)}


@app.get("/projects")
def list_projects(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    rows = conn.execute(
        REACHABLE_CTE
        + """
        SELECT p.*, a.username AS owner_username
          FROM projects p
          JOIN accounts a ON a.id = p.owner_id
         WHERE p.owner_id IN (SELECT id FROM reach)
         ORDER BY p.updated_at DESC, p.id DESC
        """,
        (viewer_id,),
    ).fetchall()
    return {"projects": [project_dict(row, viewer_id) for row in rows]}


def fetch_accessible_project(
    conn: sqlite3.Connection, viewer_id: int, project_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        REACHABLE_CTE
        + """
        SELECT p.*, a.username AS owner_username
          FROM projects p
          JOIN accounts a ON a.id = p.owner_id
         WHERE p.id = ?
           AND p.owner_id IN (SELECT id FROM reach)
        """,
        (viewer_id, project_id),
    ).fetchone()


def deny_if_project_inaccessible(
    conn: sqlite3.Connection, viewer_id: int, project_id: int
) -> None:
    exists = conn.execute(
        "SELECT 1 FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if exists is None:
        raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")
    raise ApiError(
        403,
        "PROJECT_ACCESS_DENIED",
        "Current account is not connected to the project owner's component.",
        {"project_id": project_id},
    )


@app.get("/projects/{project_id}")
def get_project(
    project_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    row = fetch_accessible_project(conn, viewer_id, project_id)
    if row is None:
        deny_if_project_inaccessible(conn, viewer_id, project_id)
    return {"project": project_dict(row, viewer_id)}


@app.patch("/projects/{project_id}")
def update_project(
    project_id: int,
    body: ProjectUpdateIn,
    if_match: str | None = Header(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip() if changes["title"] is not None else None
        if not changes["title"]:
            raise ApiError(422, "VALIDATION_ERROR", "title must not be blank.")
    if "content" in changes and changes["content"] is None:
        raise ApiError(422, "VALIDATION_ERROR", "content must be a string.")

    # BEGIN IMMEDIATE is the important concurrency boundary.  It waits for an
    # in-flight edge deletion/creation and then performs the reachability
    # check and revision insert in the same serialized transaction.
    with write_transaction(conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")

        accessible = fetch_accessible_project(conn, viewer_id, project_id)
        if accessible is None:
            raise ApiError(
                403,
                "PROJECT_ACCESS_DENIED",
                "Current account is not connected to the project owner's component.",
                {"project_id": project_id},
            )

        if if_match is not None:
            try:
                expected_revision = int(if_match.strip().strip('"'))
            except ValueError as exc:
                raise ApiError(
                    422,
                    "INVALID_IF_MATCH",
                    "If-Match must be an integer revision.",
                ) from exc
            if expected_revision != project["revision"]:
                raise ApiError(
                    409,
                    "REVISION_CONFLICT",
                    "Project revision does not match If-Match.",
                    {
                        "expected_revision": expected_revision,
                        "current_revision": project["revision"],
                    },
                )

        title = changes.get("title", project["title"])
        content = changes.get("content", project["content"])
        new_revision = int(project["revision"]) + 1
        graph_version = current_graph_version(conn)
        ts = now_ts()

        conn.execute(
            """
            UPDATE projects
               SET title = ?, content = ?, revision = ?, updated_at = ?
             WHERE id = ?
            """,
            (title, content, new_revision, ts, project_id),
        )
        # actor_id is always the authenticated session.  owner_id is absent
        # from this UPDATE and can never be rewritten by a collaborator.
        conn.execute(
            """
            INSERT INTO project_revisions(
                project_id, revision, action, actor_id, title, content,
                graph_version, created_at
            ) VALUES (?, ?, 'updated', ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                new_revision,
                viewer_id,
                title,
                content,
                graph_version,
                ts,
            ),
        )
        row = conn.execute(
            """
            SELECT p.*, a.username AS owner_username
              FROM projects p JOIN accounts a ON a.id = p.owner_id
             WHERE p.id = ?
            """,
            (project_id,),
        ).fetchone()

    return {"project": project_dict(row, viewer_id)}


@app.get("/projects/{project_id}/history")
def project_history(
    project_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]

    # Authorization and history read use one snapshot, avoiding a read variant
    # of check-then-revoke-then-read.
    with read_transaction(conn):
        accessible = fetch_accessible_project(conn, viewer_id, project_id)
        if accessible is None:
            deny_if_project_inaccessible(conn, viewer_id, project_id)

        rows = conn.execute(
            """
            SELECT r.revision, r.action, r.actor_id, r.title, r.content,
                   r.graph_version, r.created_at,
                   actor.username AS actor_username,
                   p.owner_id AS owner_id,
                   owner.username AS owner_username
              FROM project_revisions r
              JOIN accounts actor ON actor.id = r.actor_id
              JOIN projects p ON p.id = r.project_id
              JOIN accounts owner ON owner.id = p.owner_id
             WHERE r.project_id = ?
             ORDER BY r.revision ASC
            """,
            (project_id,),
        ).fetchall()

    history = []
    for row in rows:
        history.append(
            {
                "revision": row["revision"],
                "action": row["action"],
                "actor": {
                    "id": row["actor_id"],
                    "username": row["actor_username"],
                },
                "owner": {
                    "id": row["owner_id"],
                    "username": row["owner_username"],
                },
                "actor_is_owner": row["actor_id"] == row["owner_id"],
                "shared_modification": row["actor_id"] != row["owner_id"],
                "title": row["title"],
                "content": row["content"],
                "graph_version": row["graph_version"],
                "created_at": iso_from_ts(row["created_at"]),
            }
        )
    return {"project_id": project_id, "history": history}


# ---------------------------------------------------------------------------
# Curation review: collaborators propose, the owner decides
# ---------------------------------------------------------------------------

CURATION_APPROVAL_ACTIONS = ("accepted", "withdrawn")


def curation_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "base_revision": row["base_revision"],
        "start": row["start"],
        "end": row["end"],
        "original_text": row["original_text"],
        "replacement": row["replacement"],
        "operation": (
            "insert"
            if row["start"] == row["end"]
            else "delete"
            if row["replacement"] == ""
            else "replace"
        ),
        "status": row["status"],
        "proposer": {
            "id": row["proposer_id"],
            "username": row["proposer_username"],
        },
        "created_at": iso_from_ts(row["created_at"]),
        "decided_by": (
            {"id": row["decided_by"], "username": row["decided_by_username"]}
            if row["decided_by"] is not None
            else None
        ),
        "decided_at": iso_from_ts(row["decided_at"]),
        "accepted_range": (
            {"start": row["accepted_start"], "end": row["accepted_end"]}
            if row["accepted_start"] is not None
            else None
        ),
        "accepted_revision": row["accepted_revision"],
        "withdrawn_by": (
            {"id": row["withdrawn_by"], "username": row["withdrawn_by_username"]}
            if row["withdrawn_by"] is not None
            else None
        ),
        "withdrawn_at": iso_from_ts(row["withdrawn_at"]),
        "withdrawn_revision": row["withdrawn_revision"],
    }


CURATION_SELECT = """
SELECT c.*, proposer.username AS proposer_username,
       decider.username AS decided_by_username,
       withdrawer.username AS withdrawn_by_username
  FROM curation_proposals c
  JOIN accounts proposer ON proposer.id = c.proposer_id
  LEFT JOIN accounts decider ON decider.id = c.decided_by
  LEFT JOIN accounts withdrawer ON withdrawer.id = c.withdrawn_by
"""


def fetch_curation(conn: sqlite3.Connection, curation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        CURATION_SELECT + " WHERE c.id = ?", (curation_id,)
    ).fetchone()


def curation_visible(
    conn: sqlite3.Connection, viewer_id: int, project: sqlite3.Row, curation: sqlite3.Row
) -> bool:
    """Owner sees all of a project's proposals; others see only their own.

    Bystanders with access to the project never see another collaborator's
    proposal.  A proposer whose access has since been revoked cannot read the
    item any more either; the item itself stays for the owner to decide.
    """
    if viewer_id == project["owner_id"]:
        return True
    if viewer_id != curation["proposer_id"]:
        return False
    return fetch_accessible_project(conn, viewer_id, project["id"]) is not None


def project_content_at(
    conn: sqlite3.Connection, project_id: int, revision: int
) -> str | None:
    row = conn.execute(
        """
        SELECT content FROM project_revisions
         WHERE project_id = ? AND revision = ?
        """,
        (project_id, revision),
    ).fetchone()
    return None if row is None else row["content"]


def map_range_to_current(
    conn: sqlite3.Connection,
    *,
    project: sqlite3.Row,
    origin_revision: int,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    """Carry ``[start, end)`` from a content revision to the current content.

    Returns ``None`` when a later revision conflicts: an exact edit touched the
    range or an endpoint, or the pre-existing PATCH endpoint changed the
    content at all (treated as a whole-document rewrite).
    """
    origin_content = project_content_at(conn, project["id"], origin_revision)
    if origin_content is None:
        return None
    if start < 0 or end < start or end > len(origin_content):
        return None

    later = conn.execute(
        """
        SELECT r.revision, r.content, e.edit_start, e.edit_end, e.inserted_length
          FROM project_revisions r
          LEFT JOIN project_content_edits e
                 ON e.project_id = r.project_id AND e.revision = r.revision
         WHERE r.project_id = ? AND r.revision > ?
         ORDER BY r.revision ASC
        """,
        (project["id"], origin_revision),
    ).fetchall()

    previous = origin_content
    for row in later:
        current = row["content"]
        if current == previous:
            previous = current  # title-only or identical re-save
            continue
        if row["edit_start"] is None:
            # Content changed without a recorded exact edit (the direct PATCH
            # API): whole-document rewrite, so it always conflicts.
            return None
        mapped = map_through_edit(
            start,
            end,
            int(row["edit_start"]),
            int(row["edit_end"]),
            int(row["inserted_length"]),
        )
        if mapped is None:
            return None
        start, end = mapped
        previous = current

    if project["content"] != previous:
        # Defensive: a content change not represented in history at all.
        return None
    return start, end


def mapped_proposal_range(
    conn: sqlite3.Connection, curation: sqlite3.Row, project: sqlite3.Row
) -> tuple[int, int] | None:
    """Map the proposal's base range to the project's current content."""
    base_content = project_content_at(
        conn, project["id"], curation["base_revision"]
    )
    if base_content is None:
        return None
    start, end = int(curation["start"]), int(curation["end"])
    # A stale recorded original_text is itself an unsafe mismatch.
    if base_content[start:end] != curation["original_text"]:
        return None
    return map_range_to_current(
        conn,
        project=project,
        origin_revision=curation["base_revision"],
        start=start,
        end=end,
    )


def require_owner(project: sqlite3.Row, viewer_id: int) -> None:
    if viewer_id != project["owner_id"]:
        raise ApiError(
            403,
            "NOT_PROJECT_OWNER",
            "Only the project owner can decide curation proposals.",
            {"project_id": project["id"]},
        )


def insert_project_revision(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    revision: int,
    actor_id: int,
    title: str,
    content: str,
    ts: float,
    graph_version: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO project_revisions(
            project_id, revision, action, actor_id, title, content,
            graph_version, created_at
        ) VALUES (?, ?, 'updated', ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            revision,
            actor_id,
            title,
            content,
            current_graph_version(conn) if graph_version is None else graph_version,
            ts,
        ),
    )


def record_content_edit(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    revision: int,
    edit_start: int,
    edit_end: int,
    inserted_length: int,
    ts: float,
) -> None:
    conn.execute(
        """
        INSERT INTO project_content_edits(
            project_id, revision, edit_start, edit_end, inserted_length, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, revision, edit_start, edit_end, inserted_length, ts),
    )


@app.post("/projects/{project_id}/curations", status_code=201)
def submit_curation(
    project_id: int,
    body: CurationCreateIn,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    with write_transaction(conn):
        project = fetch_accessible_project(conn, viewer_id, project_id)
        if project is None:
            deny_if_project_inaccessible(conn, viewer_id, project_id)

        base_content = project_content_at(conn, project_id, body.base_revision)
        if base_content is None:
            raise ApiError(
                404,
                "REVISION_NOT_FOUND",
                "The referenced project revision does not exist.",
                {"base_revision": body.base_revision},
            )
        if body.end > len(base_content):
            raise ApiError(
                422,
                "RANGE_OUT_OF_BOUNDS",
                "Range end exceeds the revision content length.",
                {"base_revision": body.base_revision, "length": len(base_content)},
            )
        original = base_content[body.start : body.end]
        if original == body.replacement:
            raise ApiError(
                422,
                "NO_CHANGE",
                "The replacement is identical to the targeted text.",
            )

        ts = now_ts()
        cursor = conn.execute(
            """
            INSERT INTO curation_proposals(
                project_id, proposer_id, base_revision, start, end,
                original_text, replacement, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                project_id,
                viewer_id,
                body.base_revision,
                body.start,
                body.end,
                original,
                body.replacement,
                ts,
            ),
        )
        curation_id = cursor.lastrowid
        row = fetch_curation(conn, curation_id)

    return {"curation": curation_dict(row)}


@app.get("/projects/{project_id}/curations")
def list_curations(
    project_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    with read_transaction(conn):
        project = fetch_accessible_project(conn, viewer_id, project_id)
        if project is None:
            deny_if_project_inaccessible(conn, viewer_id, project_id)

        if viewer_id == project["owner_id"]:
            rows = conn.execute(
                CURATION_SELECT
                + """
                 WHERE c.project_id = ?
                 ORDER BY c.id ASC
                """,
                (project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                CURATION_SELECT
                + """
                 WHERE c.project_id = ? AND c.proposer_id = ?
                 ORDER BY c.id ASC
                """,
                (project_id, viewer_id),
            ).fetchall()

    return {
        "project_id": project_id,
        "viewer": "owner" if viewer_id == project["owner_id"] else "proposer",
        "curations": [curation_dict(row) for row in rows],
    }


@app.get("/projects/{project_id}/curations/{curation_id}")
def get_curation(
    project_id: int,
    curation_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    with read_transaction(conn):
        project = fetch_accessible_project(conn, viewer_id, project_id)
        if project is None:
            deny_if_project_inaccessible(conn, viewer_id, project_id)
        curation = fetch_curation(conn, curation_id)
        if curation is None or curation["project_id"] != project_id:
            raise ApiError(404, "CURATION_NOT_FOUND", "Curation proposal was not found.")
        if not curation_visible(conn, viewer_id, project, curation):
            raise ApiError(
                403,
                "NOT_CURATION_PARTICIPANT",
                "Only the project owner or this proposal's submitter can read it.",
                {"project_id": project_id, "curation_id": curation_id},
            )

    return {"curation": curation_dict(curation)}


def _decision_target(
    conn: sqlite3.Connection,
    *,
    project: sqlite3.Row,
    curation: sqlite3.Row,
    action: str,
) -> tuple[int, int]:
    """Resolve the span a decision edits on the current content.

    Accept starts from the proposal's base range.  Withdraw starts from the
    span recorded when the proposal was accepted.  Both are carried through
    later revisions and must not have been touched.
    """
    if action == "accepted":
        mapped = mapped_proposal_range(conn, curation, project)
        if mapped is not None:
            start, end = mapped
            if project["content"][start:end] != curation["original_text"]:
                mapped = None
        origin = curation["base_revision"]
    else:
        accepted_revision = curation["accepted_revision"]
        if (
            curation["accepted_start"] is None
            or curation["accepted_end"] is None
            or accepted_revision is None
        ):
            raise ApiError(
                409,
                "CURATION_NOT_ACCEPTED",
                "This proposal has no accepted replacement to withdraw.",
            )
        mapped = map_range_to_current(
            conn,
            project=project,
            origin_revision=accepted_revision,
            start=int(curation["accepted_start"]),
            end=int(curation["accepted_end"]),
        )
        if mapped is not None:
            start, end = mapped
            if project["content"][start:end] != curation["replacement"]:
                mapped = None
        origin = accepted_revision

    if mapped is None:
        raise ApiError(
            409,
            "CURATION_RANGE_CONFLICT",
            "Later edits overlap the targeted span or one of its endpoints.",
            {
                "curation_id": curation["id"],
                "origin_revision": origin,
                "current_revision": project["revision"],
            },
        )
    return mapped


def _apply_decision(
    conn: sqlite3.Connection,
    *,
    project: sqlite3.Row,
    curation: sqlite3.Row,
    viewer_id: int,
    action: str,
) -> sqlite3.Row:
    """Shared accept/withdraw core.  Caller holds ``BEGIN IMMEDIATE``.

    The decision either fully applies (proposal status + project content +
    project revision + exact edit record) or raises before changing anything.
    """
    if action == "accepted":
        if curation["status"] == "withdrawn":
            raise ApiError(
                409,
                "CURATION_ALREADY_WITHDRAWN",
                "A withdrawn proposal cannot be accepted again.",
            )
        if curation["status"] == "rejected":
            raise ApiError(
                409,
                "CURATION_REJECTED",
                "A rejected proposal cannot be accepted.",
            )
        if curation["status"] == "accepted":
            raise ApiError(
                409,
                "CURATION_ALREADY_DECIDED",
                "This proposal has already been accepted.",
                {"accepted_revision": curation["accepted_revision"]},
            )
        new_text_piece = curation["replacement"]
    else:  # withdrawn
        if curation["status"] == "withdrawn":
            raise ApiError(
                409,
                "CURATION_ALREADY_WITHDRAWN",
                "This proposal has already been withdrawn.",
                {"withdrawn_revision": curation["withdrawn_revision"]},
            )
        if curation["status"] != "accepted":
            raise ApiError(
                409,
                "CURATION_NOT_ACCEPTED",
                "Only an accepted proposal can be withdrawn.",
                {"status": curation["status"]},
            )
        new_text_piece = curation["original_text"]

    target_start, target_end = _decision_target(
        conn, project=project, curation=curation, action=action
    )

    content = project["content"]
    new_text = content[:target_start] + new_text_piece + content[target_end:]
    ts = now_ts()
    new_revision = int(project["revision"]) + 1
    conn.execute(
        """
        UPDATE projects
           SET content = ?, revision = ?, updated_at = ?
         WHERE id = ?
        """,
        (new_text, new_revision, ts, project["id"]),
    )
    insert_project_revision(
        conn,
        project_id=project["id"],
        revision=new_revision,
        actor_id=viewer_id,
        title=project["title"],
        content=new_text,
        ts=ts,
    )
    record_content_edit(
        conn,
        project_id=project["id"],
        revision=new_revision,
        edit_start=target_start,
        edit_end=target_end,
        inserted_length=len(new_text_piece),
        ts=ts,
    )
    if action == "accepted":
        conn.execute(
            """
            UPDATE curation_proposals
               SET status = 'accepted', decided_at = ?, decided_by = ?,
                   accepted_revision = ?, accepted_start = ?, accepted_end = ?
             WHERE id = ?
            """,
            (
                ts,
                viewer_id,
                new_revision,
                target_start,
                target_start + len(new_text_piece),
                curation["id"],
            ),
        )
    else:
        conn.execute(
            """
            UPDATE curation_proposals
               SET status = 'withdrawn', withdrawn_at = ?, withdrawn_by = ?,
                   withdrawn_revision = ?
             WHERE id = ?
            """,
            (ts, viewer_id, new_revision, curation["id"]),
        )
    return conn.execute(
        """
        SELECT p.*, a.username AS owner_username
          FROM projects p JOIN accounts a ON a.id = p.owner_id
         WHERE p.id = ?
        """,
        (project["id"],),
    ).fetchone()


def _decide(
    *,
    project_id: int,
    curation_id: int,
    action: str,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
    if_match: str | None,
) -> dict[str, Any]:
    """Owner-only decision.  Authorization, range check, edit and history
    commit in one ``BEGIN IMMEDIATE`` transaction; any conflict changes
    nothing."""
    viewer_id = account["id"]
    with write_transaction(conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")
        # Re-check dynamic access inside the transaction: a proposer who lost
        # access must not decide, and the owner remains the owner.
        if fetch_accessible_project(conn, viewer_id, project_id) is None:
            raise ApiError(
                403,
                "PROJECT_ACCESS_DENIED",
                "Current account is not connected to the project owner's component.",
                {"project_id": project_id},
            )
        require_owner(project, viewer_id)

        if if_match is not None:
            try:
                expected_revision = int(if_match.strip().strip('"'))
            except ValueError as exc:
                raise ApiError(
                    422,
                    "INVALID_IF_MATCH",
                    "If-Match must be an integer revision.",
                ) from exc
            if expected_revision != project["revision"]:
                raise ApiError(
                    409,
                    "REVISION_CONFLICT",
                    "Project revision does not match If-Match.",
                    {
                        "expected_revision": expected_revision,
                        "current_revision": project["revision"],
                    },
                )

        curation = fetch_curation(conn, curation_id)
        if curation is None or curation["project_id"] != project_id:
            raise ApiError(404, "CURATION_NOT_FOUND", "Curation proposal was not found.")

        row = _apply_decision(
            conn,
            project=project,
            curation=curation,
            viewer_id=viewer_id,
            action=action,
        )
        updated_curation = fetch_curation(conn, curation_id)

    return {
        "project": project_dict(row, viewer_id),
        "curation": curation_dict(updated_curation),
    }


@app.post("/projects/{project_id}/curations/{curation_id}/accept")
def accept_curation(
    project_id: int,
    curation_id: int,
    if_match: str | None = Header(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _decide(
        project_id=project_id,
        curation_id=curation_id,
        action="accepted",
        account=account,
        conn=conn,
        if_match=if_match,
    )


@app.post("/projects/{project_id}/curations/{curation_id}/reject")
def reject_curation(
    project_id: int,
    curation_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    viewer_id = account["id"]
    with write_transaction(conn):
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise ApiError(404, "PROJECT_NOT_FOUND", "Project was not found.")
        if fetch_accessible_project(conn, viewer_id, project_id) is None:
            raise ApiError(
                403,
                "PROJECT_ACCESS_DENIED",
                "Current account is not connected to the project owner's component.",
                {"project_id": project_id},
            )
        require_owner(project, viewer_id)

        curation = fetch_curation(conn, curation_id)
        if curation is None or curation["project_id"] != project_id:
            raise ApiError(404, "CURATION_NOT_FOUND", "Curation proposal was not found.")
        if curation["status"] == "accepted":
            raise ApiError(
                409,
                "CURATION_ALREADY_DECIDED",
                "An accepted proposal must be withdrawn, not rejected.",
                {"accepted_revision": curation["accepted_revision"]},
            )
        if curation["status"] == "withdrawn":
            raise ApiError(
                409,
                "CURATION_ALREADY_WITHDRAWN",
                "A withdrawn proposal cannot be rejected.",
            )
        if curation["status"] == "rejected":
            raise ApiError(
                409,
                "CURATION_ALREADY_DECIDED",
                "This proposal has already been rejected.",
            )

        ts = now_ts()
        conn.execute(
            """
            UPDATE curation_proposals
               SET status = 'rejected', decided_at = ?, decided_by = ?
             WHERE id = ?
            """,
            (ts, viewer_id, curation["id"]),
        )
        updated = fetch_curation(conn, curation_id)

    return {"curation": curation_dict(updated)}


@app.post("/projects/{project_id}/curations/{curation_id}/withdraw")
def withdraw_curation(
    project_id: int,
    curation_id: int,
    if_match: str | None = Header(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _decide(
        project_id=project_id,
        curation_id=curation_id,
        action="withdrawn",
        account=account,
        conn=conn,
        if_match=if_match,
    )


# Initialize on import as well as lifespan so TestClient and direct ASGI
# imports have a schema even if lifespan is not entered by a caller.
init_db()
