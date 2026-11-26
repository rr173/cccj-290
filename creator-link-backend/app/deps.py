"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3

from fastapi import Depends, Header

from .db import get_db
from .errors import ApiError
from .security import hash_secret, now_ts


def get_current_account(
    authorization: str | None = Header(default=None),
    x_session_token: str | None = Header(default=None),
    conn: sqlite3.Connection = Depends(get_db),
) -> sqlite3.Row:
    token: str | None = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token and x_session_token:
        token = x_session_token.strip()
    if not token:
        raise ApiError(
            401,
            "MISSING_SESSION",
            "Use 'Authorization: Bearer <session_token>' or X-Session-Token.",
        )

    row = conn.execute(
        """
        SELECT a.id, a.username, a.password_hash, a.created_at,
               s.expires_at AS session_expires_at
          FROM sessions s
          JOIN accounts a ON a.id = s.account_id
         WHERE s.token_hash = ?
        """,
        (hash_secret(token),),
    ).fetchone()
    if row is None:
        raise ApiError(401, "INVALID_SESSION", "Session token was not found.")
    if row["session_expires_at"] <= now_ts():
        raise ApiError(401, "SESSION_EXPIRED", "Session has expired; log in again.")
    return row
