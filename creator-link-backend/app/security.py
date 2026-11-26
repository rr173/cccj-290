"""Local password/session credential helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timezone

PBKDF2_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 30 * 24 * 3600


def now_ts() -> float:
    return time.time()


def iso_from_ts(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("ascii"), PBKDF2_ITERATIONS
    ).hex()
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_text, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("ascii"),
            int(iterations_text),
        ).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_invitation_token() -> str:
    return secrets.token_urlsafe(32)
