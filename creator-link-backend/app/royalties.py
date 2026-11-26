"""Owner-only sales, refunds, royalty balances, and finance settlements.

The module is intentionally separate from :mod:`issues`: it never changes
issue quotas, invitations, entitlements, or read access.

Money rules are all integer cents:

* A sale snapshots the collaborator account and basis points at sale time.
* The collaborator amount is ``floor(gross * bps / 10000)``; the owner gets the
  remainder.
* Refunds cumulatively recompute the collaborator's earning on the remaining
  received amount.  The refund then reverses the difference from each side's
  original/current earning, preserving the floor rounding.
* All issues owned by one account are combined by payee account.  Different
  owners are separate ledgers.
* A settlement creates one immutable pending transfer for every payee with no
  pending transfer and a positive net balance.  Confirmations only change the
  transfer result/status; successful transfers become paid, failed transfers
  release the occupation.

Every mutation uses ``BEGIN IMMEDIATE`` and natural unique constraints, so
concurrent duplicates/refunds/settlements cannot over-refund or pay twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Header

from .db import get_db, read_transaction, write_transaction
from .deps import get_current_account
from .errors import ApiError
from .security import iso_from_ts, now_ts

router = APIRouter()


# ---------------------------------------------------------------------------
# Request normalization and small utilities
# ---------------------------------------------------------------------------


def _fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unwrap_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for wrapper in ("sale", "royalty_sale", "payment", "refund", "royalty_refund"):
        value = payload.get(wrapper)
        if isinstance(value, dict):
            outer = {k: v for k, v in payload.items() if k != wrapper}
            return {**value, **outer}
    return dict(payload)


def _pick(data: dict[str, Any], *keys: str) -> tuple[Any, str | None]:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value, key
    return None, None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
    return None


def _require_ref(data: dict[str, Any], keys: tuple[str, ...], *, field: str) -> str:
    value, _ = _pick(data, *keys)
    if value is not None and not isinstance(value, (str, int)):
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{field} must be a non-empty string.",
            {"field": field},
        )
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{field} is required and must be non-empty.",
            {"field": field},
        )
    return text


def _optional_ref(data: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    value, _ = _pick(data, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _require_positive_int(
    data: dict[str, Any], keys: tuple[str, ...], *, field: str
) -> int:
    value, key_used = _pick(data, *keys)
    number = _as_int(value)
    if number is None or number <= 0:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            f"{field} must be a positive integer number of cents.",
            {"field": field, "received": value, "matched_key": key_used},
        )
    return number


def _optional_string(data: dict[str, Any], *keys: str) -> str | None:
    value = _optional_ref(data, keys)
    return value


def _basis_points(data: dict[str, Any]) -> int:
    value, key_used = _pick(
        data,
        "basis_points",
        "share_basis_points",
        "royalty_basis_points",
        "creator_share_basis_points",
        "author_share_basis_points",
        "share_rate_basis_points",
        "royalty_rate_basis_points",
        "percentage_basis_points",
        "bps",
        "share_bps",
        "royalty_bps",
        "rate_bps",
        "share_rate_bps",
        "royalty_rate_bps",
        "creator_share_bps",
        "author_share_bps",
        "split_bps",
    )
    bps = _as_int(value)
    if bps is not None:
        if not 0 <= bps <= 10000:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "Basis points must be an integer from 0 to 10000.",
                {"field": key_used, "value": value},
            )
        return bps

    # Convenience aliases for callers that express the same ratio as percent.
    percent, percent_key = _pick(
        data,
        "share_percent",
        "share_percentage",
        "creator_share_percent",
        "author_share_percent",
        "royalty_percent",
    )
    if percent is not None:
        try:
            percent_number = float(percent)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422, "VALIDATION_ERROR", "share percent must be numeric."
            ) from exc
        if not 0 <= percent_number <= 100:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "share percent must be between 0 and 100.",
                {"field": percent_key},
            )
        return round(percent_number * 100)

    ratio, ratio_key = _pick(
        data, "share_ratio", "royalty_ratio", "split_ratio", "ratio"
    )
    if ratio is not None:
        try:
            ratio_number = float(ratio)
        except (TypeError, ValueError) as exc:
            raise ApiError(
                422, "VALIDATION_ERROR", "share ratio must be numeric."
            ) from exc
        if 0 <= ratio_number <= 1:
            return round(ratio_number * 10000)
        if 0 <= ratio_number <= 10000 and float(ratio_number).is_integer():
            return int(ratio_number)
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "share ratio must be between 0 and 1, or basis points up to 10000.",
            {"field": ratio_key},
        )

    raise ApiError(
        422,
        "VALIDATION_ERROR",
        "basis_points (0..10000 integer) is required.",
        {"fields": ["basis_points", "share_bps", "bps"]},
    )


def _nested_account(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("creator", "author", "payee", "account", "user"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _resolve_account_input(
    conn: sqlite3.Connection, data: dict[str, Any]
) -> sqlite3.Row:
    nested = _nested_account(data)
    merged = {**nested, **data}

    id_value, id_key = _pick(
        merged,
        "creator_account_id",
        "author_account_id",
        "royalty_author_id",
        "royalty_creator_id",
        "payee_account_id",
        "account_id",
        "creator_id",
        "author_id",
        "user_id",
        "id",
    )
    username_value, username_key = _pick(
        merged,
        "creator_username",
        "author_username",
        "royalty_author_username",
        "royalty_creator_username",
        "payee_username",
        "account_username",
        "username",
        "user_username",
    )

    account: sqlite3.Row | None = None
    if id_value is not None:
        account_id = _as_int(id_value)
        if account_id is None:
            raise ApiError(
                422, "VALIDATION_ERROR", "Account id must be an integer."
            )
        account = conn.execute(
            "SELECT id, username FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise ApiError(
                404,
                "ACCOUNT_NOT_FOUND",
                "The specified existing account does not exist.",
                {"account_id": account_id, "field": id_key},
            )

    if username_value is not None:
        username = str(username_value).strip()
        by_name = conn.execute(
            "SELECT id, username FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        if by_name is None:
            raise ApiError(
                404,
                "ACCOUNT_NOT_FOUND",
                f"Account {username!r} does not exist.",
                {"username": username, "field": username_key},
            )
        if account is not None and account["id"] != by_name["id"]:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "Account id and username refer to different accounts.",
            )
        account = by_name

    if account is None:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "An existing creator account id or username is required.",
            {"fields": ["creator_account_id", "creator_username"]},
        )
    return account


def _request_identifier(
    data: dict[str, Any],
    *,
    idempotency_key_header: str | None,
    x_idempotency_key_header: str | None,
    request_id_header: str | None,
) -> tuple[str, bool]:
    value = _optional_ref(
        data,
        (
            "request_id",
            "settlement_request_id",
            "idempotency_key",
            "idempotency-key",
            "client_request_id",
            "request_uuid",
            "uuid",
            "key",
            "request_key",
        ),
    )
    generated = False
    if value is None:
        value = (
            idempotency_key_header
            or x_idempotency_key_header
            or request_id_header
        )
    if value is None or not value.strip():
        value = f"auto-{uuid.uuid4().hex}"
        generated = True
    return value.strip(), generated


# ---------------------------------------------------------------------------
# Domain helpers and serialization
# ---------------------------------------------------------------------------


def _require_issue_owned(
    conn: sqlite3.Connection, issue_id: int, actor_id: int
) -> sqlite3.Row:
    issue = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
    if issue is None:
        raise ApiError(404, "ISSUE_NOT_FOUND", "Issue was not found.")
    if issue["owner_id"] != actor_id:
        raise ApiError(
            403,
            "NOT_ISSUE_OWNER",
            "Only the original issue owner can manage sales and royalty settlements.",
            {"issue_id": issue_id},
        )
    return issue


def _require_owned_settlement(
    conn: sqlite3.Connection, settlement_id: int | str, actor_id: int
) -> sqlite3.Row:
    if isinstance(settlement_id, str) and not re.fullmatch(r"\d+", settlement_id):
        row = conn.execute(
            "SELECT * FROM royalty_settlements WHERE request_id = ?",
            (settlement_id,),
        ).fetchone()
        not_found_details: dict[str, Any] = {"request_id": settlement_id}
    else:
        numeric_id = int(settlement_id)
        row = conn.execute(
            "SELECT * FROM royalty_settlements WHERE id = ?", (numeric_id,)
        ).fetchone()
        not_found_details = {"settlement_id": numeric_id}
    if row is None:
        raise ApiError(
            404, "SETTLEMENT_NOT_FOUND", "Royalty settlement was not found.",
            not_found_details,
        )
    if row["owner_id"] != actor_id:
        raise ApiError(
            403,
            "ROYALTY_ACCESS_DENIED",
            "Only the original issue owner can manage this royalty ledger.",
            {"settlement_id": row["id"]},
        )
    return row


def _account_brief(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, username FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if row is None:  # Foreign keys make this unreachable for committed rows.
        return {"id": account_id, "username": None}
    return {"id": row["id"], "username": row["username"]}


def _share_dict(row: sqlite3.Row) -> dict[str, Any]:
    creator = {"id": row["creator_account_id"], "username": row["creator_username"]}
    return {
        "issue_id": row["issue_id"],
        "owner_id": row["owner_id"],
        "version": row["version"],
        "creator_account_id": row["creator_account_id"],
        "creator": creator,
        "author": creator,
        "payee": creator,
        "basis_points": row["basis_points"],
        "share_bps": row["basis_points"],
        "royalty_bps": row["basis_points"],
        "creator_share_basis_points": row["basis_points"],
        "created_at": iso_from_ts(row["created_at"]),
        "updated_at": iso_from_ts(row["updated_at"]),
    }


_SHARE_SELECT = """
SELECT s.*, a.username AS creator_username
  FROM issue_royalty_shares s
  JOIN accounts a ON a.id = s.creator_account_id
"""


def _get_share(conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _SHARE_SELECT + " WHERE s.issue_id = ?", (issue_id,)
    ).fetchone()


_REFUND_SELECT = """
SELECT r.*, s.external_order_no AS sale_external_order_no,
       s.gross_amount AS sale_gross_amount
  FROM royalty_refunds r
  JOIN royalty_sales s ON s.id = r.sale_id
"""


def _refund_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "refund_id": row["id"],
        "issue_id": row["issue_id"],
        "sale_id": row["sale_id"],
        "sale_external_order_no": row["sale_external_order_no"],
        "external_order_no": row["sale_external_order_no"],
        "external_refund_no": row["external_refund_no"],
        "refund_no": row["external_refund_no"],
        "external_refund_id": row["external_refund_no"],
        "amount": row["amount"],
        "amount_cents": row["amount"],
        "refund_amount": row["amount"],
        "refund_amount_cents": row["amount"],
        "creator_deduction": row["creator_deduction"],
        "author_deduction": row["creator_deduction"],
        "creator_refunded_amount": row["creator_deduction"],
        "owner_deduction": row["owner_deduction"],
        "owner_refunded_amount": row["owner_deduction"],
        "deductions": {
            "creator": row["creator_deduction"],
            "author": row["creator_deduction"],
            "owner": row["owner_deduction"],
        },
        "reason": row["reason"],
        "created_at": iso_from_ts(row["created_at"]),
    }


_SALE_SELECT = """
SELECT s.*, ca.username AS creator_username,
       oa.username AS owner_username, i.title AS issue_title
  FROM royalty_sales s
  JOIN accounts ca ON ca.id = s.creator_account_id
  JOIN accounts oa ON oa.id = s.owner_id
  JOIN issues i ON i.id = s.issue_id
"""


def _sale_dict(
    conn: sqlite3.Connection, row: sqlite3.Row, *, include_refunds: bool = True
) -> dict[str, Any]:
    refunds: list[dict[str, Any]] = []
    if include_refunds:
        refund_rows = conn.execute(
            _REFUND_SELECT + " WHERE r.sale_id = ? ORDER BY r.id", (row["id"],)
        ).fetchall()
        refunds = [_refund_dict(item) for item in refund_rows]

    remaining = int(row["gross_amount"]) - int(row["refunded_amount"])
    creator_net = (
        int(row["creator_gross_amount"]) - int(row["creator_refunded_amount"])
    )
    owner_net = int(row["owner_gross_amount"]) - int(row["owner_refunded_amount"])
    creator = {
        "id": row["creator_account_id"],
        "username": row["creator_username"],
    }
    owner = {"id": row["owner_id"], "username": row["owner_username"]}
    return {
        "id": row["id"],
        "sale_id": row["id"],
        "issue_id": row["issue_id"],
        "issue_title": row["issue_title"],
        "owner_id": row["owner_id"],
        "owner": owner,
        "external_order_no": row["external_order_no"],
        "order_no": row["external_order_no"],
        "external_order_id": row["external_order_no"],
        "external_sale_id": row["external_order_no"],
        "basis_points": row["basis_points"],
        "share_bps": row["basis_points"],
        "creator_account_id": row["creator_account_id"],
        "creator": creator,
        "author": creator,
        "gross_amount": row["gross_amount"],
        "amount": row["gross_amount"],
        "amount_cents": row["gross_amount"],
        "received_amount": row["gross_amount"],
        "received_amount_cents": row["gross_amount"],
        "sale_amount": row["gross_amount"],
        "creator_gross_amount": row["creator_gross_amount"],
        "creator_amount": row["creator_gross_amount"],
        "creator_amount_cents": row["creator_gross_amount"],
        "creator_share": row["creator_gross_amount"],
        "creator_share_cents": row["creator_gross_amount"],
        "author_amount": row["creator_gross_amount"],
        "author_amount_cents": row["creator_gross_amount"],
        "author_share": row["creator_gross_amount"],
        "author_share_cents": row["creator_gross_amount"],
        "royalty_amount": row["creator_gross_amount"],
        "royalty_amount_cents": row["creator_gross_amount"],
        "owner_gross_amount": row["owner_gross_amount"],
        "owner_amount": row["owner_gross_amount"],
        "owner_amount_cents": row["owner_gross_amount"],
        "owner_share": row["owner_gross_amount"],
        "owner_share_cents": row["owner_gross_amount"],
        "refunded_amount": row["refunded_amount"],
        "refunded_amount_cents": row["refunded_amount"],
        "total_refunded_amount": row["refunded_amount"],
        "remaining_amount": remaining,
        "net_received_amount": remaining,
        "refundable_amount": remaining,
        "creator_refunded_amount": row["creator_refunded_amount"],
        "author_refunded_amount": row["creator_refunded_amount"],
        "owner_refunded_amount": row["owner_refunded_amount"],
        "creator_net_amount": creator_net,
        "creator_net_amount_cents": creator_net,
        "author_net_amount": creator_net,
        "author_net_amount_cents": creator_net,
        "owner_net_amount": owner_net,
        "owner_net_amount_cents": owner_net,
        "net_split": {"creator": creator_net, "author": creator_net, "owner": owner_net},
        "memo": row["memo"],
        "refund_count": len(refunds),
        "refunds": refunds,
        "created_at": iso_from_ts(row["created_at"]),
    }


def _sale_envelope(sale: dict[str, Any], *, replay: bool | None = None) -> dict[str, Any]:
    body: dict[str, Any] = dict(sale)
    body.update(
        {
            "sale": sale,
            "sales_order": sale,
            "sale_order": sale,
            "order": sale,
        }
    )
    if replay is not None:
        body["idempotent_replay"] = replay
    return body


def _find_sale_for_owner(
    conn: sqlite3.Connection,
    owner_id: int,
    *,
    issue_id: int | None,
    sale_ref: str | None,
    data: dict[str, Any],
) -> sqlite3.Row:
    body_ref = _optional_ref(
        data,
        (
            "sale_external_order_no",
            "external_order_no",
            "sale_order_no",
            "original_order_no",
            "order_no",
            "external_order_id",
            "sale_external_order_id",
            "sale_order_id",
            "original_order_id",
            "order_id",
            "sale_external_id",
            "external_sale_id",
        ),
    )
    internal_id = _as_int(_pick(data, "sale_id", "sale_pk", "id")[0])

    ref = sale_ref or body_ref
    if ref is not None:
        # External order numbers are unique per owner.  Try that first; a
        # numeric ref that is not an external number may also be an internal id.
        query = _SALE_SELECT + " WHERE s.owner_id = ? AND s.external_order_no = ?"
        params: list[Any] = [owner_id, ref]
        if issue_id is not None:
            query += " AND s.issue_id = ?"
            params.append(issue_id)
        row = conn.execute(query, params).fetchone()
        if row is not None:
            return row

        maybe_id = _as_int(ref)
        if maybe_id is not None:
            query = _SALE_SELECT + " WHERE s.owner_id = ? AND s.id = ?"
            params = [owner_id, maybe_id]
            if issue_id is not None:
                query += " AND s.issue_id = ?"
                params.append(issue_id)
            row = conn.execute(query, params).fetchone()
            if row is not None:
                return row
        raise ApiError(
            404,
            "SALE_NOT_FOUND",
            "The referenced original sale was not found in this owner ledger.",
            {"sale_ref": ref, "issue_id": issue_id},
        )

    if internal_id is not None:
        query = _SALE_SELECT + " WHERE s.owner_id = ? AND s.id = ?"
        params = [owner_id, internal_id]
        if issue_id is not None:
            query += " AND s.issue_id = ?"
            params.append(issue_id)
        row = conn.execute(query, params).fetchone()
        if row is None:
            raise ApiError(404, "SALE_NOT_FOUND", "Original sale was not found.")
        return row

    raise ApiError(
        422,
        "VALIDATION_ERROR",
        "A sale external order number or sale id is required.",
        {"fields": ["sale_external_order_no", "sale_id"]},
    )


# ---------------------------------------------------------------------------
# Balances and settlement serialization
# ---------------------------------------------------------------------------


def _balance_rows(conn: sqlite3.Connection, owner_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH earned_raw(account_id, earned_amount) AS (
            SELECT creator_account_id,
                   SUM(creator_gross_amount - creator_refunded_amount)
              FROM royalty_sales
             WHERE owner_id = ?
             GROUP BY creator_account_id
            UNION ALL
            SELECT owner_id,
                   SUM(owner_gross_amount - owner_refunded_amount)
              FROM royalty_sales
             WHERE owner_id = ?
             GROUP BY owner_id
        ),
        earned AS (
            SELECT account_id, SUM(earned_amount) AS earned_amount
              FROM earned_raw
             GROUP BY account_id
        ),
        paid AS (
            SELECT payee_account_id AS account_id, SUM(amount) AS paid_amount
              FROM royalty_transfers
             WHERE owner_id = ? AND status = 'paid'
             GROUP BY payee_account_id
        ),
        pending AS (
            SELECT payee_account_id AS account_id, SUM(amount) AS pending_amount
              FROM royalty_transfers
             WHERE owner_id = ? AND status = 'pending'
             GROUP BY payee_account_id
        ),
        account_ids AS (
            SELECT account_id FROM earned
            UNION SELECT account_id FROM paid
            UNION SELECT account_id FROM pending
        )
        SELECT a.id AS account_id,
               a.username AS username,
               COALESCE(e.earned_amount, 0) AS earned_amount,
               COALESCE(p.paid_amount, 0) AS paid_amount,
               COALESCE(h.pending_amount, 0) AS pending_amount,
               COALESCE(e.earned_amount, 0)
                 - COALESCE(p.paid_amount, 0)
                 - COALESCE(h.pending_amount, 0) AS net_balance
          FROM account_ids x
          JOIN accounts a ON a.id = x.account_id
          LEFT JOIN earned e ON e.account_id = x.account_id
          LEFT JOIN paid p ON p.account_id = x.account_id
          LEFT JOIN pending h ON h.account_id = x.account_id
         ORDER BY a.id
        """,
        (owner_id, owner_id, owner_id, owner_id),
    ).fetchall()


def _balance_dict(row: sqlite3.Row) -> dict[str, Any]:
    account = {"id": row["account_id"], "username": row["username"]}
    net = int(row["net_balance"])
    pending_amount = int(row["pending_amount"])
    return {
        "account_id": row["account_id"],
        "username": row["username"],
        "account": account,
        "payee": account,
        "recipient": account,
        "earned_amount": int(row["earned_amount"]),
        "earned_amount_cents": int(row["earned_amount"]),
        "receivable_amount": int(row["earned_amount"]),
        "receivable_amount_cents": int(row["earned_amount"]),
        "current_earned_amount": int(row["earned_amount"]),
        "paid_amount": int(row["paid_amount"]),
        "paid_amount_cents": int(row["paid_amount"]),
        "pending_amount": pending_amount,
        "pending_amount_cents": pending_amount,
        "occupied_amount": pending_amount,
        "occupied_amount_cents": pending_amount,
        "net_balance": net,
        "net_balance_cents": net,
        "net_amount": net,
        "net_amount_cents": net,
        "balance": net,
        "balance_cents": net,
        "has_pending_transfer": pending_amount > 0,
        "settleable": pending_amount == 0 and net > 0,
        "can_create_transfer": pending_amount == 0 and net > 0,
    }


_TRANSFER_SELECT = """
SELECT t.*, a.username AS payee_username,
       st.request_id AS settlement_request_id
  FROM royalty_transfers t
  JOIN accounts a ON a.id = t.payee_account_id
  JOIN royalty_settlements st ON st.id = t.settlement_id
"""


def _transfer_dict(row: sqlite3.Row) -> dict[str, Any]:
    account = {"id": row["payee_account_id"], "username": row["payee_username"]}
    if row["status"] == "paid":
        outcome = "success"
        paid = True
    elif row["status"] == "failed":
        outcome = "failed"
        paid = False
    else:
        outcome = None
        paid = False
    confirmed = row["status"] != "pending"
    return {
        "id": row["id"],
        "transfer_id": row["id"],
        "item_id": row["id"],
        "settlement_id": row["settlement_id"],
        "settlement_request_id": row["settlement_request_id"],
        "owner_id": row["owner_id"],
        "account_id": row["payee_account_id"],
        "payee_id": row["payee_account_id"],
        "recipient_id": row["payee_account_id"],
        "payee_account_id": row["payee_account_id"],
        "recipient_account_id": row["payee_account_id"],
        "payee": account,
        "recipient": account,
        "account": account,
        "amount": row["amount"],
        "amount_cents": row["amount"],
        "transfer_amount": row["amount"],
        "currency_unit": "cent",
        "earned_amount_snapshot": row["earned_amount_snapshot"],
        "paid_amount_snapshot": row["paid_amount_snapshot"],
        "status": row["status"],
        "result_status": outcome,
        "result": outcome,
        "outcome": outcome,
        "finance_result": outcome,
        "successful": True if outcome == "success" else False if outcome == "failed" else None,
        "paid": paid,
        "confirmed": confirmed,
        "finance_reference": row["finance_reference"],
        "reference": row["finance_reference"],
        "note": row["result_note"],
        "result_note": row["result_note"],
        "confirmation_key": row["confirmation_key"],
        "created_at": iso_from_ts(row["created_at"]),
        "confirmed_at": iso_from_ts(row["confirmed_at"]),
        "confirmation": (
            {
                "status": outcome,
                "result": outcome,
                "finance_reference": row["finance_reference"],
                "note": row["result_note"],
                "confirmed_at": iso_from_ts(row["confirmed_at"]),
            }
            if confirmed
            else None
        ),
    }


_SETTLEMENT_SELECT = """
SELECT st.*, a.username AS owner_username
  FROM royalty_settlements st
  JOIN accounts a ON a.id = st.owner_id
"""


def _settlement_dict(
    conn: sqlite3.Connection, row: sqlite3.Row, *, include_items: bool = True
) -> dict[str, Any]:
    transfer_rows: list[sqlite3.Row] = []
    items: list[dict[str, Any]] = []
    if include_items:
        transfer_rows = conn.execute(
            _TRANSFER_SELECT + " WHERE t.settlement_id = ? ORDER BY t.id",
            (row["id"],),
        ).fetchall()
        items = [_transfer_dict(item) for item in transfer_rows]

    pending = [item for item in items if item["status"] == "pending"]
    paid = [item for item in items if item["status"] == "paid"]
    failed = [item for item in items if item["status"] == "failed"]
    pending_amount = sum(int(item["amount"]) for item in pending)
    paid_amount = sum(int(item["amount"]) for item in paid)
    failed_amount = sum(int(item["amount"]) for item in failed)

    if items and len(pending) == len(items):
        confirmation_status = "pending"
    elif pending:
        confirmation_status = "partially_confirmed"
    else:
        confirmation_status = "completed"

    owner = {"id": row["owner_id"], "username": row["owner_username"]}
    return {
        "id": row["id"],
        "settlement_id": row["id"],
        "owner_id": row["owner_id"],
        "owner": owner,
        "request_id": row["request_id"],
        "idempotency_key": row["request_id"],
        "note": row["note"],
        "total_amount": row["total_amount"],
        "total_amount_cents": row["total_amount"],
        "amount": row["total_amount"],
        "item_count": row["item_count"],
        "transfer_count": row["item_count"],
        "items": items,
        "transfers": items,
        "payouts": items,
        "pending_count": len(pending),
        "paid_count": len(paid),
        "success_count": len(paid),
        "failed_count": len(failed),
        "confirmed_count": len(paid) + len(failed),
        "pending_amount": pending_amount,
        "paid_amount": paid_amount,
        "success_amount": paid_amount,
        "failed_amount": failed_amount,
        "all_confirmed": not pending,
        "has_pending": bool(pending),
        "confirmation_status": confirmation_status,
        "status": confirmation_status,
        "document_status": "issued",
        "created_at": iso_from_ts(row["created_at"]),
    }


def _get_settlement_row(
    conn: sqlite3.Connection, settlement_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        _SETTLEMENT_SELECT + " WHERE st.id = ?", (settlement_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# Per-issue collaborator / basis-point designation
# ---------------------------------------------------------------------------


def _set_share_response(
    issue_id: int,
    payload: dict[str, Any] | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    data = _unwrap_payload(payload)
    actor_id = account["id"]
    with write_transaction(conn):
        issue = _require_issue_owned(conn, issue_id, actor_id)
        creator = _resolve_account_input(conn, data)
        if creator["id"] == actor_id or creator["id"] == issue["owner_id"]:
            raise ApiError(
                422,
                "ROYALTY_CREATOR_MUST_BE_DIFFERENT_ACCOUNT",
                "The royalty collaborator must be an existing account different from the issue owner.",
                {"owner_id": actor_id, "creator_account_id": creator["id"]},
            )
        bps = _basis_points(data)
        ts = now_ts()
        existing = _get_share(conn, issue_id)
        version = 1 if existing is None else int(existing["version"]) + 1

        conn.execute(
            """
            INSERT INTO issue_royalty_shares(
                issue_id, owner_id, creator_account_id, basis_points,
                version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(issue_id) DO UPDATE SET
                creator_account_id = excluded.creator_account_id,
                basis_points = excluded.basis_points,
                version = excluded.version,
                updated_at = excluded.updated_at
            """,
            (issue_id, actor_id, creator["id"], bps, version, ts, ts),
        )
        conn.execute(
            """
            INSERT INTO issue_royalty_share_history(
                issue_id, owner_id, creator_account_id, basis_points,
                version, changed_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (issue_id, actor_id, creator["id"], bps, version, actor_id, ts),
        )
        row = _get_share(conn, issue_id)
        assert row is not None
        changed = existing is None or (
            existing["creator_account_id"] != creator["id"]
            or existing["basis_points"] != bps
        )
        share = _share_dict(row)
        return {
            **share,
            "royalty_share": share,
            "royalty_recipient": share,
            "royalty_author": share,
            "royalty_creator": share,
            "collaborator": share,
            "share": share,
            "recipient": share,
            "changed": changed,
        }


@router.put("/issues/{issue_id}/royalty-share")
@router.patch("/issues/{issue_id}/royalty-share")
@router.post("/issues/{issue_id}/royalty-share")
@router.put("/issues/{issue_id}/royalty-shares")
@router.patch("/issues/{issue_id}/royalty-shares")
@router.post("/issues/{issue_id}/royalty-shares")
@router.put("/issues/{issue_id}/royalty-recipients")
@router.patch("/issues/{issue_id}/royalty-recipients")
@router.post("/issues/{issue_id}/royalty-recipients")
@router.put("/issues/{issue_id}/royalty-authors")
@router.patch("/issues/{issue_id}/royalty-authors")
@router.post("/issues/{issue_id}/royalty-authors")
@router.put("/issues/{issue_id}/collaborator")
@router.patch("/issues/{issue_id}/collaborator")
@router.post("/issues/{issue_id}/collaborator")
@router.put("/issues/{issue_id}/royalty-split")
@router.patch("/issues/{issue_id}/royalty-split")
@router.post("/issues/{issue_id}/royalty-split")
@router.put("/issues/{issue_id}/royalty")
@router.patch("/issues/{issue_id}/royalty")
@router.post("/issues/{issue_id}/royalty")
@router.put("/issues/{issue_id}/royalty-recipient")
@router.patch("/issues/{issue_id}/royalty-recipient")
@router.post("/issues/{issue_id}/royalty-recipient")
@router.put("/issues/{issue_id}/royalty-author")
@router.patch("/issues/{issue_id}/royalty-author")
@router.post("/issues/{issue_id}/royalty-author")
@router.put("/issues/{issue_id}/royalty-creator")
@router.patch("/issues/{issue_id}/royalty-creator")
@router.post("/issues/{issue_id}/royalty-creator")
@router.put("/issues/{issue_id}/split")
@router.patch("/issues/{issue_id}/split")
@router.post("/issues/{issue_id}/split")
def set_royalty_share(
    issue_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _set_share_response(issue_id, payload, account, conn)


def _get_share_response(
    issue_id: int, account: sqlite3.Row, conn: sqlite3.Connection
) -> dict[str, Any]:
    with read_transaction(conn):
        _require_issue_owned(conn, issue_id, account["id"])
        row = _get_share(conn, issue_id)
        history_rows = conn.execute(
            """
            SELECT h.*, a.username AS creator_username
              FROM issue_royalty_share_history h
              JOIN accounts a ON a.id = h.creator_account_id
             WHERE h.issue_id = ?
             ORDER BY h.version DESC
            """,
            (issue_id,),
        ).fetchall()
        history = [
            {
                "version": item["version"],
                "basis_points": item["basis_points"],
                "share_bps": item["basis_points"],
                "creator": {
                    "id": item["creator_account_id"],
                    "username": item["creator_username"],
                },
                "created_at": iso_from_ts(item["created_at"]),
            }
            for item in history_rows
        ]
        share = _share_dict(row) if row is not None else None
        response: dict[str, Any] = {
            "issue_id": issue_id,
            "royalty_share": share,
            "royalty_recipient": share,
            "royalty_author": share,
            "collaborator": share,
            "share": share,
            "configured": row is not None,
            "history": history,
        }
        if share is not None:
            response.update(share)
        return response


@router.get("/issues/{issue_id}/royalty-share")
@router.get("/issues/{issue_id}/royalty-shares")
@router.get("/issues/{issue_id}/royalty-recipients")
@router.get("/issues/{issue_id}/royalty-authors")
@router.get("/issues/{issue_id}/collaborator")
@router.get("/issues/{issue_id}/royalty-split")
@router.get("/issues/{issue_id}/royalty")
@router.get("/issues/{issue_id}/royalty-recipient")
@router.get("/issues/{issue_id}/royalty-author")
@router.get("/issues/{issue_id}/split")
def get_royalty_share(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _get_share_response(issue_id, account, conn)


# ---------------------------------------------------------------------------
# Sales registration and queries
# ---------------------------------------------------------------------------

SALE_AMOUNT_KEYS = (
    "amount_fen",
    "amount_cents",
    "received_amount_fen",
    "received_amount_cents",
    "gross_amount_fen",
    "gross_amount_cents",
    "paid_amount_fen",
    "paid_amount_cents",
    "sale_amount_fen",
    "sale_amount_cents",
    "price_fen",
    "price_cents",
    "amount",
    "received_amount",
    "gross_amount",
    "paid_amount",
    "sale_amount",
    "total_amount",
)

SALE_REF_KEYS = (
    "external_order_no",
    "sale_external_order_no",
    "external_order_id",
    "sale_external_id",
    "external_sale_id",
    "order_no",
    "order_id",
    "external_id",
    "out_trade_no",
    "out_order_no",
    "trade_no",
    "channel_order_no",
    "transaction_id",
)


def _create_sale_response(
    issue_id: int | None,
    payload: dict[str, Any] | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    data = _unwrap_payload(payload)
    actor_id = account["id"]

    if issue_id is None:
        issue_id = _as_int(
            _pick(data, "issue_id", "limited_issue_id", "edition_id")[0]
        )
        if issue_id is None:
            raise ApiError(
                422,
                "VALIDATION_ERROR",
                "issue_id is required when registering a sale outside an issue path.",
            )

    external_no = _require_ref(data, SALE_REF_KEYS, field="external_order_no")
    amount = _require_positive_int(data, SALE_AMOUNT_KEYS, field="amount")
    memo = _optional_string(data, "memo", "note", "description")
    normalized = {
        "issue_id": issue_id,
        "external_order_no": external_no,
        "amount": amount,
        "memo": memo,
    }
    fingerprint = _fingerprint(normalized)

    with write_transaction(conn):
        _require_issue_owned(conn, issue_id, actor_id)
        existing = conn.execute(
            "SELECT * FROM royalty_sales WHERE owner_id = ? AND external_order_no = ?",
            (actor_id, external_no),
        ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This external sale number was already used with different parameters.",
                    {
                        "external_order_no": external_no,
                        "existing_sale_id": existing["id"],
                    },
                )
            row = conn.execute(
                _SALE_SELECT + " WHERE s.id = ?", (existing["id"],)
            ).fetchone()
            assert row is not None
            return _sale_envelope(_sale_dict(conn, row), replay=True)

        share = _get_share(conn, issue_id)
        if share is None:
            raise ApiError(
                409,
                "ROYALTY_SHARE_REQUIRED",
                "Designate an existing royalty creator and basis points before registering sales.",
                {"issue_id": issue_id},
            )

        creator_id = int(share["creator_account_id"])
        bps = int(share["basis_points"])
        creator_amount = (amount * bps) // 10000
        owner_amount = amount - creator_amount
        ts = now_ts()
        try:
            cursor = conn.execute(
                """
                INSERT INTO royalty_sales(
                    owner_id, issue_id, external_order_no, gross_amount, memo,
                    creator_account_id, basis_points,
                    creator_gross_amount, owner_gross_amount,
                    request_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    issue_id,
                    external_no,
                    amount,
                    memo,
                    creator_id,
                    bps,
                    creator_amount,
                    owner_amount,
                    fingerprint,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Natural unique constraint backstop for racing retries.
            raced = conn.execute(
                "SELECT id FROM royalty_sales WHERE owner_id = ? AND external_order_no = ?",
                (actor_id, external_no),
            ).fetchone()
            if raced is not None:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This external sale number is already registered.",
                    {"external_order_no": external_no},
                ) from exc
            raise

        row = conn.execute(_SALE_SELECT + " WHERE s.id = ?", (cursor.lastrowid,)).fetchone()
        assert row is not None
        return _sale_envelope(_sale_dict(conn, row), replay=False)


@router.post("/issues/{issue_id}/sales", status_code=201)
@router.post("/issues/{issue_id}/sale", status_code=201)
@router.post("/issues/{issue_id}/royalty-sales", status_code=201)
@router.post("/issues/{issue_id}/royalties/sales", status_code=201)
@router.post("/issues/{issue_id}/royalty/sales", status_code=201)
@router.post("/issues/{issue_id}/payments", status_code=201)
@router.post("/issues/{issue_id}/incomes", status_code=201)
@router.post("/royalties/issues/{issue_id}/sales", status_code=201)
@router.post("/royalty/issues/{issue_id}/sales", status_code=201)
def create_issue_sale(
    issue_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _create_sale_response(issue_id, payload, account, conn)


@router.post("/royalties/sales", status_code=201)
@router.post("/royalty/sales", status_code=201)
@router.post("/sales", status_code=201)
def create_owner_sale(
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _create_sale_response(None, payload, account, conn)


def _list_sales_response(
    issue_id: int | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    query_issue_id: int | None = None,
) -> dict[str, Any]:
    actor_id = account["id"]
    with read_transaction(conn):
        if issue_id is not None:
            _require_issue_owned(conn, issue_id, actor_id)
        filter_issue = issue_id if issue_id is not None else query_issue_id
        sql = _SALE_SELECT + " WHERE s.owner_id = ?"
        params: list[Any] = [actor_id]
        if filter_issue is not None:
            sql += " AND s.issue_id = ?"
            params.append(filter_issue)
        sql += " ORDER BY s.id"
        rows = conn.execute(sql, params).fetchall()
        sales = [_sale_dict(conn, row) for row in rows]
        gross = sum(int(row["gross_amount"]) for row in rows)
        refunded = sum(int(row["refunded_amount"]) for row in rows)
        return {
            "owner_id": actor_id,
            "issue_id": filter_issue,
            "sales": sales,
            "sale_orders": sales,
            "orders": sales,
            "items": sales,
            "count": len(sales),
            "totals": {
                "gross_amount": gross,
                "refunded_amount": refunded,
                "net_received_amount": gross - refunded,
            },
        }


@router.get("/issues/{issue_id}/sales")
@router.get("/issues/{issue_id}/sale")
@router.get("/issues/{issue_id}/royalty-sales")
@router.get("/issues/{issue_id}/royalties/sales")
@router.get("/issues/{issue_id}/royalty/sales")
def list_issue_sales(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _list_sales_response(issue_id, account, conn)


@router.get("/royalties/sales")
@router.get("/royalty/sales")
@router.get("/sales")
def list_owner_sales(
    issue_id: int | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _list_sales_response(None, account, conn, query_issue_id=issue_id)


def _get_sale_response(
    issue_id: int | None,
    sale_ref: str,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    with read_transaction(conn):
        if issue_id is not None:
            _require_issue_owned(conn, issue_id, account["id"])
        row = _find_sale_for_owner(
            conn, account["id"], issue_id=issue_id, sale_ref=sale_ref, data={}
        )
        return _sale_envelope(_sale_dict(conn, row))


@router.get("/issues/{issue_id}/royalties")
@router.get("/issues/{issue_id}/royalty-report")
@router.get("/issues/{issue_id}/royalty-ledger")
def get_issue_royalty_report(
    issue_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    with read_transaction(conn):
        _require_issue_owned(conn, issue_id, account["id"])
        share_row = _get_share(conn, issue_id)
        sale_rows = conn.execute(
            _SALE_SELECT + " WHERE s.owner_id = ? AND s.issue_id = ? ORDER BY s.id",
            (account["id"], issue_id),
        ).fetchall()
        sales = [_sale_dict(conn, row) for row in sale_rows]
        totals = {
            "gross_amount": sum(int(row["gross_amount"]) for row in sale_rows),
            "refunded_amount": sum(int(row["refunded_amount"]) for row in sale_rows),
            "creator_amount": sum(
                int(row["creator_gross_amount"] - row["creator_refunded_amount"])
                for row in sale_rows
            ),
            "author_amount": sum(
                int(row["creator_gross_amount"] - row["creator_refunded_amount"])
                for row in sale_rows
            ),
            "owner_amount": sum(
                int(row["owner_gross_amount"] - row["owner_refunded_amount"])
                for row in sale_rows
            ),
        }
        totals["net_received_amount"] = (
            totals["gross_amount"] - totals["refunded_amount"]
        )
        return {
            "issue_id": issue_id,
            "royalty_share": _share_dict(share_row) if share_row is not None else None,
            "sales": sales,
            "refunds": [refund for sale in sales for refund in sale["refunds"]],
            "totals": totals,
        }


@router.get("/issues/{issue_id}/sales/{sale_ref}")
@router.get("/issues/{issue_id}/sale/{sale_ref}")
@router.get("/royalties/sales/{sale_ref}")
@router.get("/royalty/sales/{sale_ref}")
@router.get("/sales/{sale_ref}")
def get_sale(
    sale_ref: str,
    issue_id: int | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    # The issue route supplies issue_id; FastAPI leaves it None on owner routes.
    return _get_sale_response(issue_id, sale_ref, account, conn)


# ---------------------------------------------------------------------------
# Refund registration and queries
# ---------------------------------------------------------------------------

REFUND_REF_KEYS = (
    "external_refund_no",
    "refund_external_order_no",
    "external_refund_id",
    "refund_external_id",
    "out_refund_no",
    "refund_trade_no",
    "channel_refund_no",
    "refund_no",
    "refund_order_no",
    "refund_id",
    "id",
)
REFUND_AMOUNT_KEYS = (
    "refund_amount_fen",
    "refund_amount_cents",
    "amount_fen",
    "amount_cents",
    "refunded_amount_fen",
    "refunded_amount_cents",
    "refund_amount",
    "refunded_amount",
    "amount",
)


def _create_refund_response(
    issue_id: int | None,
    sale_ref: str | None,
    payload: dict[str, Any] | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
) -> dict[str, Any]:
    data = _unwrap_payload(payload)
    actor_id = account["id"]
    if issue_id is None:
        body_issue = _as_int(_pick(data, "issue_id", "limited_issue_id", "edition_id")[0])
        issue_id = body_issue

    external_refund_no = _require_ref(data, REFUND_REF_KEYS, field="external_refund_no")
    amount = _require_positive_int(data, REFUND_AMOUNT_KEYS, field="refund amount")
    reason = _optional_string(data, "reason", "note", "memo")

    with write_transaction(conn):
        if issue_id is not None:
            _require_issue_owned(conn, issue_id, actor_id)
        sale = _find_sale_for_owner(
            conn,
            actor_id,
            issue_id=issue_id,
            sale_ref=sale_ref,
            data=data,
        )
        issue_id = int(sale["issue_id"])
        normalized = {
            "sale_id": sale["id"],
            "external_refund_no": external_refund_no,
            "amount": amount,
            "reason": reason,
        }
        fingerprint = _fingerprint(normalized)

        existing = conn.execute(
            "SELECT * FROM royalty_refunds WHERE owner_id = ? AND external_refund_no = ?",
            (actor_id, external_refund_no),
        ).fetchone()
        if existing is not None:
            if (
                existing["sale_id"] != sale["id"]
                or int(existing["amount"]) != amount
                or existing["request_fingerprint"] != fingerprint
            ):
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This external refund number was already used with different parameters.",
                    {
                        "external_refund_no": external_refund_no,
                        "existing_refund_id": existing["id"],
                    },
                )
            refund_row = conn.execute(
                _REFUND_SELECT + " WHERE r.id = ?", (existing["id"],)
            ).fetchone()
            sale_row = conn.execute(
                _SALE_SELECT + " WHERE s.id = ?", (sale["id"],)
            ).fetchone()
            assert refund_row and sale_row
            refund_dict = _refund_dict(refund_row)
            return {
                **refund_dict,
                "refund": refund_dict,
                "refund_order": refund_dict,
                "sale": _sale_dict(conn, sale_row),
                "idempotent_replay": True,
            }

        already_refunded = int(sale["refunded_amount"])
        gross = int(sale["gross_amount"])
        remaining_refundable = gross - already_refunded
        if amount > remaining_refundable:
            raise ApiError(
                409,
                "REFUND_EXCEEDS_SALE_AMOUNT",
                "Cumulative refunds cannot exceed the original received amount.",
                {
                    "sale_id": sale["id"],
                    "external_order_no": sale["external_order_no"],
                    "gross_amount": gross,
                    "refunded_amount": already_refunded,
                    "remaining_refundable_amount": remaining_refundable,
                    "requested_refund_amount": amount,
                },
            )

        # Cumulative floor recomputation.  The refund reverses the difference
        # between the old and new creator entitlements; all remaining cents,
        # including floor rounding effects, reverse the owner side.
        old_creator_refunded = int(sale["creator_refunded_amount"])
        new_cumulative_refunded = already_refunded + amount
        remaining_received = gross - new_cumulative_refunded
        new_creator_entitlement = (
            remaining_received * int(sale["basis_points"])
        ) // 10000
        new_creator_refunded_cumulative = (
            int(sale["creator_gross_amount"]) - new_creator_entitlement
        )
        creator_deduction = new_creator_refunded_cumulative - old_creator_refunded
        owner_deduction = amount - creator_deduction
        if creator_deduction < 0 or owner_deduction < 0:
            raise ApiError(
                500,
                "ROYALTY_REFUND_ALLOCATION_ERROR",
                "Refund allocation produced a negative deduction.",
            )

        ts = now_ts()
        try:
            cursor = conn.execute(
                """
                INSERT INTO royalty_refunds(
                    owner_id, issue_id, sale_id, external_refund_no, amount,
                    creator_deduction, owner_deduction, reason,
                    request_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    actor_id,
                    issue_id,
                    sale["id"],
                    external_refund_no,
                    amount,
                    creator_deduction,
                    owner_deduction,
                    reason,
                    fingerprint,
                    ts,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raced = conn.execute(
                "SELECT id FROM royalty_refunds WHERE owner_id = ? AND external_refund_no = ?",
                (actor_id, external_refund_no),
            ).fetchone()
            if raced is not None:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This external refund number is already registered.",
                    {"external_refund_no": external_refund_no},
                ) from exc
            raise

        conn.execute(
            """
            UPDATE royalty_sales
               SET refunded_amount = refunded_amount + ?,
                   creator_refunded_amount = ?,
                   owner_refunded_amount = owner_refunded_amount + ?
             WHERE id = ?
            """,
            (
                amount,
                new_creator_refunded_cumulative,
                owner_deduction,
                sale["id"],
            ),
        )
        refund_row = conn.execute(
            _REFUND_SELECT + " WHERE r.id = ?", (cursor.lastrowid,)
        ).fetchone()
        sale_row = conn.execute(
            _SALE_SELECT + " WHERE s.id = ?", (sale["id"],)
        ).fetchone()
        assert refund_row and sale_row
        refund_dict = _refund_dict(refund_row)
        return {
            **refund_dict,
            "refund": refund_dict,
            "refund_order": refund_dict,
            "sale": _sale_dict(conn, sale_row),
            "idempotent_replay": False,
        }


@router.post("/issues/{issue_id}/refunds", status_code=201)
@router.post("/issues/{issue_id}/refund", status_code=201)
@router.post("/issues/{issue_id}/royalty-refunds", status_code=201)
@router.post("/issues/{issue_id}/sales/{sale_ref}/refunds", status_code=201)
@router.post("/issues/{issue_id}/sales/{sale_ref}/refund", status_code=201)
@router.post("/issues/{issue_id}/sale/{sale_ref}/refund", status_code=201)
@router.post("/royalties/issues/{issue_id}/refunds", status_code=201)
def create_issue_refund(
    issue_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    sale_ref: str | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _create_refund_response(issue_id, sale_ref, payload, account, conn)


@router.post("/royalties/refunds", status_code=201)
@router.post("/royalties/refund", status_code=201)
@router.post("/royalty/refunds", status_code=201)
@router.post("/refunds", status_code=201)
@router.post("/refund", status_code=201)
@router.post("/royalties/sales/{sale_ref}/refunds", status_code=201)
@router.post("/royalties/sales/{sale_ref}/refund", status_code=201)
@router.post("/royalty/sales/{sale_ref}/refunds", status_code=201)
@router.post("/sales/{sale_ref}/refunds", status_code=201)
@router.post("/sales/{sale_ref}/refund", status_code=201)
def create_owner_refund(
    sale_ref: str | None = None,
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    return _create_refund_response(None, sale_ref, payload, account, conn)


@router.get("/royalties/refunds")
@router.get("/royalty/refunds")
@router.get("/refunds")
def list_owner_refunds(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    with read_transaction(conn):
        rows = conn.execute(
            _REFUND_SELECT
            + " WHERE r.owner_id = ? ORDER BY r.id",
            (account["id"],),
        ).fetchall()
        refunds = [_refund_dict(row) for row in rows]
        return {
            "owner_id": account["id"],
            "refunds": refunds,
            "refund_orders": refunds,
            "items": refunds,
            "count": len(refunds),
        }


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def _assert_owner_path(owner_id: int | str | None, account: sqlite3.Row) -> None:
    if owner_id is None or str(owner_id) == "me":
        return
    numeric = _as_int(owner_id)
    if numeric != int(account["id"]):
        raise ApiError(
            403,
            "ROYALTY_ACCESS_DENIED",
            "Only the original issue owner can manage this royalty ledger.",
            {"requested_owner_id": owner_id, "actor_id": account["id"]},
        )


def _empty_balance_dict(account_id: int, username: str | None) -> dict[str, Any]:
    account = {"id": account_id, "username": username}
    return {
        "account_id": account_id,
        "username": username,
        "account": account,
        "payee": account,
        "recipient": account,
        "earned_amount": 0,
        "earned_amount_cents": 0,
        "receivable_amount": 0,
        "paid_amount": 0,
        "pending_amount": 0,
        "net_balance": 0,
        "net_balance_cents": 0,
        "net_amount": 0,
        "balance": 0,
        "has_pending_transfer": False,
        "settleable": False,
    }


@router.get("/royalties/balances")
@router.get("/royalties/balance")
@router.get("/royalty/balances")
@router.get("/royalty/balance")
@router.get("/royalties/net-balances")
@router.get("/royalties/accounts")
@router.get("/royalties/payees")
@router.get("/royalties/settlement-accounts")
@router.get("/balances")
@router.get("/balance")
@router.get("/me/royalties/balances")
@router.get("/me/royalty/balances")
@router.get("/me/royalty-balances")
@router.get("/me/balances")
@router.get("/owners/{owner_id}/royalties/balances")
@router.get("/owners/{owner_id}/balances")
def get_balances(
    account_id: int | None = None,
    owner_id: int | str | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _assert_owner_path(owner_id, account)
    with read_transaction(conn):
        rows = _balance_rows(conn, account["id"])
        balances = [_balance_dict(row) for row in rows]
        selected: dict[str, Any] | None = None
        if account_id is not None:
            selected = next(
                (item for item in balances if item["account_id"] == account_id),
                None,
            )
            if selected is None:
                account_row = conn.execute(
                    "SELECT id, username FROM accounts WHERE id = ?", (account_id,)
                ).fetchone()
                if account_row is None:
                    raise ApiError(
                        404, "ACCOUNT_NOT_FOUND", "Account was not found."
                    )
                selected = _empty_balance_dict(account_id, account_row["username"])
            balances = [selected]
        return {
            "owner_id": account["id"],
            "currency_unit": "cent",
            "balances": balances,
            "accounts": balances,
            "payees": balances,
            "balance": selected,
            "account": selected,
            "count": len(balances),
            "settleable_account_ids": [
                item["account_id"] for item in balances if item["settleable"]
            ],
        }


@router.get("/royalties/balances/{payee_account_id}")
@router.get("/royalties/accounts/{payee_account_id}")
@router.get("/royalties/accounts/{payee_account_id}/balance")
@router.get("/royalties/payees/{payee_account_id}")
@router.get("/royalties/payees/{payee_account_id}/balance")
def get_account_balance(
    payee_account_id: int,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    with read_transaction(conn):
        row = next(
            (
                item
                for item in _balance_rows(conn, account["id"])
                if item["account_id"] == payee_account_id
            ),
            None,
        )
        if row is None:
            account_row = conn.execute(
                "SELECT id, username FROM accounts WHERE id = ?",
                (payee_account_id,),
            ).fetchone()
            if account_row is None:
                raise ApiError(404, "ACCOUNT_NOT_FOUND", "Account was not found.")
            balance = _empty_balance_dict(
                payee_account_id, account_row["username"]
            )
        else:
            balance = _balance_dict(row)
        return {"owner_id": account["id"], "balance": balance, "account": balance}


@router.get("/royalties")
@router.get("/royalty")
def royalty_overview(
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    with read_transaction(conn):
        balances = [_balance_dict(row) for row in _balance_rows(conn, account["id"])]
        settlement_rows = conn.execute(
            _SETTLEMENT_SELECT + " WHERE st.owner_id = ? ORDER BY st.id DESC",
            (account["id"],),
        ).fetchall()
        settlements = [_settlement_dict(conn, row) for row in settlement_rows]
        return {
            "owner_id": account["id"],
            "balances": balances,
            "accounts": balances,
            "settlements": settlements,
            "latest_settlements": settlements[:10],
        }


# ---------------------------------------------------------------------------
# Settlement orders
# ---------------------------------------------------------------------------


def _create_settlement_response(
    payload: dict[str, Any] | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    idempotency_key_header: str | None,
    x_idempotency_key_header: str | None,
    request_id_header: str | None,
) -> tuple[int, dict[str, Any]]:
    data = _unwrap_payload(payload)
    actor_id = account["id"]
    request_id, generated = _request_identifier(
        data,
        idempotency_key_header=idempotency_key_header,
        x_idempotency_key_header=x_idempotency_key_header,
        request_id_header=request_id_header,
    )
    # Request identifier fields are transport/idempotency fields, not financial
    # parameters.  Any other body fields participate in conflict detection.
    params = {
        key: value
        for key, value in data.items()
        if key
        not in {
            "request_id",
            "settlement_request_id",
            "idempotency_key",
            "idempotency-key",
            "client_request_id",
            "request_uuid",
            "uuid",
            "key",
            "request_key",
        }
    }
    fingerprint = _fingerprint(params)

    no_payable = False
    result: dict[str, Any] | None = None
    status_code = 201

    with write_transaction(conn):
        prior_request = conn.execute(
            "SELECT * FROM royalty_settlement_requests WHERE owner_id = ? AND request_id = ?",
            (actor_id, request_id),
        ).fetchone()
        if prior_request is not None:
            if prior_request["fingerprint"] != fingerprint:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This settlement request id was already used with different parameters.",
                    {"request_id": request_id},
                )
            if prior_request["status"] == "no_payable":
                # The earlier request committed its request marker but no order.
                raise ApiError(
                    409,
                    "NO_PAYABLE_BALANCES",
                    "The original request found no account eligible for a transfer.",
                    {"request_id": request_id, "idempotent_replay": True},
                )
            settlement = _require_owned_settlement(
                conn, int(prior_request["settlement_id"]), actor_id
            )
            detailed = _get_settlement_row(conn, settlement["id"])
            assert detailed is not None
            result = {
                "settlement": _settlement_dict(conn, detailed),
                "idempotent_replay": True,
                "request_id": request_id,
                "generated_request_id": generated,
            }
        else:
            balances = [_balance_dict(row) for row in _balance_rows(conn, actor_id)]
            eligible = [item for item in balances if item["settleable"]]
            if not eligible:
                conn.execute(
                    """
                    INSERT INTO royalty_settlement_requests(
                        owner_id, request_id, fingerprint, status,
                        settlement_id, created_at
                    ) VALUES (?, ?, ?, 'no_payable', NULL, ?)
                    """,
                    (actor_id, request_id, fingerprint, now_ts()),
                )
                no_payable = True
            else:
                total_amount = sum(int(item["net_balance"]) for item in eligible)
                ts = now_ts()
                note = _optional_string(data, "note", "memo", "description")
                cursor = conn.execute(
                    """
                    INSERT INTO royalty_settlements(
                        owner_id, request_id, note, total_amount,
                        item_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        actor_id,
                        request_id,
                        note,
                        total_amount,
                        len(eligible),
                        ts,
                    ),
                )
                settlement_id = cursor.lastrowid
                for item in eligible:
                    account_id = int(item["account_id"])
                    amount = int(item["net_balance"])
                    earned = int(item["earned_amount"])
                    paid = int(item["paid_amount"])
                    conn.execute(
                        """
                        INSERT INTO royalty_transfers(
                            settlement_id, owner_id, payee_account_id, amount,
                            earned_amount_snapshot, paid_amount_snapshot,
                            status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            settlement_id,
                            actor_id,
                            account_id,
                            amount,
                            earned,
                            paid,
                            ts,
                        ),
                    )
                conn.execute(
                    """
                    INSERT INTO royalty_settlement_requests(
                        owner_id, request_id, fingerprint, status,
                        settlement_id, created_at
                    ) VALUES (?, ?, ?, 'issued', ?, ?)
                    """,
                    (actor_id, request_id, fingerprint, settlement_id, ts),
                )
                detailed = _get_settlement_row(conn, settlement_id)
                assert detailed is not None
                result = {
                    "settlement": _settlement_dict(conn, detailed),
                    "idempotent_replay": False,
                    "request_id": request_id,
                    "generated_request_id": generated,
                }

    if no_payable:
        # The write transaction has committed the request marker before we raise.
        raise ApiError(
            409,
            "NO_PAYABLE_BALANCES",
            "No account has a positive net balance without a pending transfer; no settlement was issued.",
            {
                "request_id": request_id,
                "balances": _read_balances_after_commit(conn, actor_id),
            },
        )
    assert result is not None
    return status_code, result


def _read_balances_after_commit(
    conn: sqlite3.Connection, owner_id: int
) -> list[dict[str, Any]]:
    # The caller has committed; use a short read snapshot for error details.
    with read_transaction(conn):
        return [_balance_dict(row) for row in _balance_rows(conn, owner_id)]


@router.post("/royalties/settlements", status_code=201)
@router.post("/royalties/settlement", status_code=201)
@router.post("/royalty/settlements", status_code=201)
@router.post("/royalty-settlements", status_code=201)
@router.post("/settlements", status_code=201)
@router.post("/settlement", status_code=201)
@router.post("/issues/{issue_id}/settlements", status_code=201)
@router.post("/issues/{issue_id}/royalties/settlements", status_code=201)
@router.post("/royalties/payout-runs", status_code=201)
@router.post("/payout-runs", status_code=201)
@router.post("/me/royalties/settlements", status_code=201)
@router.post("/me/settlements", status_code=201)
@router.post("/owners/me/settlements", status_code=201)
@router.post("/owners/{owner_id}/royalties/settlements", status_code=201)
@router.post("/owners/{owner_id}/settlements", status_code=201)
def create_settlement(
    issue_id: int | None = None,
    owner_id: int | str | None = None,
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    x_idempotency_key: str | None = Header(
        default=None, alias="X-Idempotency-Key"
    ),
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
) -> dict[str, Any]:
    _assert_owner_path(owner_id, account)
    if issue_id is not None:
        with read_transaction(conn):
            _require_issue_owned(conn, issue_id, account["id"])
    _, body = _create_settlement_response(
        payload,
        account,
        conn,
        idempotency_key_header=idempotency_key,
        x_idempotency_key_header=x_idempotency_key,
        request_id_header=x_request_id,
    )
    if "settlement" in body:
        settlement = body["settlement"]
        body["settlement_order"] = settlement
        body["statement"] = settlement
        body["payout_run"] = settlement
        body["order"] = settlement
        # Flat aliases for clients that expect the resource at top level.
        body["id"] = settlement["id"]
        body["settlement_id"] = settlement["id"]
        body["items"] = settlement["items"]
        body["transfers"] = settlement["transfers"]
        body["payouts"] = settlement["payouts"]
        body["total_amount"] = settlement["total_amount"]
        body["status"] = settlement["status"]
    return body


@router.get("/royalties/settlements")
@router.get("/royalties/settlement")
@router.get("/royalty/settlements")
@router.get("/royalty-settlements")
@router.get("/settlements")
@router.get("/issues/{issue_id}/settlements")
@router.get("/issues/{issue_id}/royalties/settlements")
@router.get("/me/royalties/settlements")
@router.get("/me/settlements")
@router.get("/owners/me/settlements")
@router.get("/owners/{owner_id}/royalties/settlements")
@router.get("/owners/{owner_id}/settlements")
def list_settlements(
    issue_id: int | None = None,
    owner_id: int | str | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _assert_owner_path(owner_id, account)
    with read_transaction(conn):
        if issue_id is not None:
            _require_issue_owned(conn, issue_id, account["id"])
        rows = conn.execute(
            _SETTLEMENT_SELECT + " WHERE st.owner_id = ? ORDER BY st.id DESC",
            (account["id"],),
        ).fetchall()
        settlements = [_settlement_dict(conn, row) for row in rows]
        return {
            "owner_id": account["id"],
            "issue_id": issue_id,
            "settlements": settlements,
            "settlement_orders": settlements,
            "statements": settlements,
            "payout_runs": settlements,
            "orders": settlements,
            "count": len(settlements),
        }


@router.get("/royalties/settlements/{settlement_id}")
@router.get("/royalties/settlement/{settlement_id}")
@router.get("/royalty/settlements/{settlement_id}")
@router.get("/royalty-settlements/{settlement_id}")
@router.get("/settlements/{settlement_id}")
@router.get("/me/royalties/settlements/{settlement_id}")
@router.get("/me/settlements/{settlement_id}")
@router.get("/owners/me/settlements/{settlement_id}")
@router.get("/owners/{owner_id}/royalties/settlements/{settlement_id}")
@router.get("/owners/{owner_id}/settlements/{settlement_id}")
def get_settlement(
    settlement_id: str,
    owner_id: int | str | None = None,
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, Any]:
    _assert_owner_path(owner_id, account)
    with read_transaction(conn):
        owned = _require_owned_settlement(conn, settlement_id, account["id"])
        row = _get_settlement_row(conn, int(owned["id"]))
        assert row is not None
        settlement = _settlement_dict(conn, row)
        return {
            "settlement": settlement,
            "settlement_order": settlement,
            "statement": settlement,
            "payout_run": settlement,
            "order": settlement,
            "id": settlement["id"],
            "settlement_id": settlement["id"],
            "items": settlement["items"],
            "transfers": settlement["transfers"],
            "payouts": settlement["payouts"],
            "total_amount": settlement["total_amount"],
            "status": settlement["status"],
        }


# ---------------------------------------------------------------------------
# Per-transfer finance results
# ---------------------------------------------------------------------------

_SUCCESS_VALUES = {
    "success",
    "successful",
    "succeeded",
    "ok",
    "paid",
    "confirmed",
    "complete",
    "completed",
}
_FAILURE_VALUES = {
    "failed",
    "failure",
    "fail",
    "rejected",
    "bounced",
    "error",
    "unsuccessful",
    "returned",
}


def _normalize_result(data: dict[str, Any], forced: str | None) -> str:
    if forced in {"success", "failed"}:
        return forced
    value, key_used = _pick(
        data,
        "result",
        "status",
        "outcome",
        "finance_result",
        "confirmation_status",
        "transfer_status",
        "payment_status",
        "finance_status",
        "state",
    )
    if isinstance(value, bool):
        return "success" if value else "failed"
    if isinstance(value, int):
        if value == 1:
            return "success"
        if value == 0:
            return "failed"
    text = str(value).strip().lower() if value is not None else ""
    if text in _SUCCESS_VALUES:
        return "success"
    if text in _FAILURE_VALUES:
        return "failed"

    success_bool, _ = _pick(data, "success", "successful", "paid", "is_paid")
    if isinstance(success_bool, bool):
        return "success" if success_bool else "failed"

    raise ApiError(
        422,
        "VALIDATION_ERROR",
        "A finance result of success/successful/paid or failed is required.",
        {"matched_key": key_used, "received": value},
    )


def _confirm_transfer_response(
    transfer_id: int,
    payload: dict[str, Any] | None,
    account: sqlite3.Row,
    conn: sqlite3.Connection,
    *,
    settlement_id: int | None = None,
    forced_result: str | None = None,
    idempotency_key_header: str | None = None,
) -> dict[str, Any]:
    data = _unwrap_payload(payload)
    outcome = _normalize_result(data, forced_result)
    actor_id = account["id"]
    confirmation_key = _optional_ref(
        data,
        (
            "confirmation_key",
            "result_idempotency_key",
            "idempotency_key",
            "result_key",
            "confirmation_id",
        ),
    ) or (idempotency_key_header.strip() if idempotency_key_header else None)
    finance_reference = _optional_ref(
        data,
        (
            "finance_reference",
            "bank_reference",
            "payout_reference",
            "transaction_reference",
            "reference",
            "external_reference",
        ),
    )
    note = _optional_string(data, "note", "result_note", "message", "reason")

    with write_transaction(conn):
        transfer = conn.execute(
            _TRANSFER_SELECT + " WHERE t.id = ?", (transfer_id,)
        ).fetchone()
        if transfer is None:
            raise ApiError(
                404, "TRANSFER_NOT_FOUND", "Royalty transfer item was not found."
            )
        if transfer["owner_id"] != actor_id:
            raise ApiError(
                403,
                "ROYALTY_ACCESS_DENIED",
                "Only the original issue owner can confirm this transfer.",
                {"transfer_id": transfer_id},
            )
        if settlement_id is not None and int(transfer["settlement_id"]) != settlement_id:
            raise ApiError(
                404,
                "TRANSFER_NOT_FOUND",
                "Transfer does not belong to the specified settlement.",
                {
                    "settlement_id": settlement_id,
                    "transfer_id": transfer_id,
                    "actual_settlement_id": transfer["settlement_id"],
                },
            )

        if confirmation_key is not None:
            keyed = conn.execute(
                "SELECT id, status, result_status FROM royalty_transfers WHERE owner_id = ? AND confirmation_key = ?",
                (actor_id, confirmation_key),
            ).fetchone()
            if keyed is not None and int(keyed["id"]) != transfer_id:
                raise ApiError(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "This confirmation key was already used for another transfer.",
                    {"confirmation_key": confirmation_key},
                )

        if transfer["status"] != "pending":
            existing_outcome = transfer["result_status"]
            if existing_outcome != outcome:
                raise ApiError(
                    409,
                    "TRANSFER_RESULT_IMMUTABLE",
                    "A confirmed transfer result cannot be reversed or changed.",
                    {
                        "transfer_id": transfer_id,
                        "existing_result": existing_outcome,
                        "requested_result": outcome,
                    },
                )
            row = conn.execute(
                _TRANSFER_SELECT + " WHERE t.id = ?", (transfer_id,)
            ).fetchone()
            settlement = _get_settlement_row(conn, int(transfer["settlement_id"]))
            assert row is not None and settlement is not None
            transfer_dict = _transfer_dict(row)
            return {
                "transfer": transfer_dict,
                "item": transfer_dict,
                "id": transfer_dict["id"],
                "transfer_id": transfer_dict["id"],
                "status": transfer_dict["status"],
                "result": transfer_dict["result"],
                "amount": transfer_dict["amount"],
                "settlement": _settlement_dict(conn, settlement),
                "idempotent_replay": True,
            }

        new_status = "paid" if outcome == "success" else "failed"
        conn.execute(
            """
            UPDATE royalty_transfers
               SET status = ?, result_status = ?, finance_reference = ?,
                   result_note = ?, confirmation_key = COALESCE(?, confirmation_key),
                   confirmed_at = ?
             WHERE id = ? AND status = 'pending'
            """,
            (
                new_status,
                outcome,
                finance_reference,
                note,
                confirmation_key,
                now_ts(),
                transfer_id,
            ),
        )
        row = conn.execute(
            _TRANSFER_SELECT + " WHERE t.id = ?", (transfer_id,)
        ).fetchone()
        settlement = _get_settlement_row(conn, int(transfer["settlement_id"]))
        assert row is not None and settlement is not None
        transfer_dict = _transfer_dict(row)
        return {
            "transfer": transfer_dict,
            "item": transfer_dict,
            "id": transfer_dict["id"],
            "transfer_id": transfer_dict["id"],
            "status": transfer_dict["status"],
            "result": transfer_dict["result"],
            "amount": transfer_dict["amount"],
            "settlement": _settlement_dict(conn, settlement),
            "idempotent_replay": False,
        }


@router.post("/royalties/settlements/{settlement_id}/confirm")
@router.post("/royalties/settlements/{settlement_id}/result")
@router.post("/royalty/settlements/{settlement_id}/confirm")
@router.post("/settlements/{settlement_id}/confirm")
def confirm_settlement_item_from_body(
    settlement_id: int,
    payload: dict[str, Any] | None = Body(default=None),
    account: sqlite3.Row = Depends(get_current_account),
    conn: sqlite3.Connection = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    data = _unwrap_payload(payload)
    transfer_id = _as_int(
        _pick(data, "transfer_id", "item_id", "payout_id", "transferId", "itemId", "id")[0]
    )
    if transfer_id is None:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "transfer_id/item_id is required; each settlement item is confirmed separately.",
        )
    return _confirm_transfer_response(
        transfer_id,
        payload,
        account,
        conn,
        settlement_id=settlement_id,
        idempotency_key_header=idempotency_key,
    )


_RESULT_ROUTES = (
    "/royalties/transfers/{transfer_id}/result",
    "/royalties/transfers/{transfer_id}/confirm",
    "/royalties/items/{transfer_id}/result",
    "/royalties/items/{transfer_id}/confirm",
    "/royalties/settlement-items/{transfer_id}/result",
    "/royalties/settlement-items/{transfer_id}/confirm",
    "/royalties/payout-items/{transfer_id}/result",
    "/royalties/payout-items/{transfer_id}/confirm",
    "/royalties/payouts/{transfer_id}/result",
    "/royalties/payouts/{transfer_id}/confirm",
    "/royalty/transfers/{transfer_id}/result",
    "/royalty/transfers/{transfer_id}/confirm",
    "/royalty/settlement-items/{transfer_id}/result",
    "/royalty/payouts/{transfer_id}/result",
    "/settlement-items/{transfer_id}/result",
    "/settlement-items/{transfer_id}/confirm",
    "/payout-items/{transfer_id}/result",
    "/payout-items/{transfer_id}/confirm",
    "/transfers/{transfer_id}/result",
    "/transfers/{transfer_id}/confirm",
    "/payouts/{transfer_id}/result",
    "/payouts/{transfer_id}/confirm",
)


def _register_result_routes() -> None:
    # Programmatic registration keeps the many compatibility aliases in one
    # place without stacking a dozen decorators on each wrapper.
    def result_endpoint(
        transfer_id: int,
        payload: dict[str, Any] | None = Body(default=None),
        account: sqlite3.Row = Depends(get_current_account),
        conn: sqlite3.Connection = Depends(get_db),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> dict[str, Any]:
        return _confirm_transfer_response(
            transfer_id,
            payload,
            account,
            conn,
            idempotency_key_header=idempotency_key,
        )

    def success_endpoint(
        transfer_id: int,
        payload: dict[str, Any] | None = Body(default=None),
        account: sqlite3.Row = Depends(get_current_account),
        conn: sqlite3.Connection = Depends(get_db),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> dict[str, Any]:
        return _confirm_transfer_response(
            transfer_id,
            payload,
            account,
            conn,
            forced_result="success",
            idempotency_key_header=idempotency_key,
        )

    def fail_endpoint(
        transfer_id: int,
        payload: dict[str, Any] | None = Body(default=None),
        account: sqlite3.Row = Depends(get_current_account),
        conn: sqlite3.Connection = Depends(get_db),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> dict[str, Any]:
        return _confirm_transfer_response(
            transfer_id,
            payload,
            account,
            conn,
            forced_result="failed",
            idempotency_key_header=idempotency_key,
        )

    for path in _RESULT_ROUTES:
        router.add_api_route(
            path, result_endpoint, methods=["POST", "PUT"], status_code=200
        )
        action_base = path
        for suffix in ("/result", "/confirm"):
            if action_base.endswith(suffix):
                action_base = action_base[: -len(suffix)]
                break
        router.add_api_route(
            action_base + "/success",
            success_endpoint,
            methods=["POST", "PUT"],
            status_code=200,
        )
        router.add_api_route(
            action_base + "/succeed",
            success_endpoint,
            methods=["POST"],
            status_code=200,
        )
        router.add_api_route(
            action_base + "/paid",
            success_endpoint,
            methods=["POST"],
            status_code=200,
        )
        router.add_api_route(
            action_base + "/fail",
            fail_endpoint,
            methods=["POST", "PUT"],
            status_code=200,
        )
        router.add_api_route(
            action_base + "/failed",
            fail_endpoint,
            methods=["POST"],
            status_code=200,
        )

    settlement_paths = (
        "/royalties/settlements/{settlement_id}/items/{transfer_id}/result",
        "/royalties/settlements/{settlement_id}/items/{transfer_id}/confirm",
        "/royalties/settlements/{settlement_id}/transfers/{transfer_id}/result",
        "/royalties/settlements/{settlement_id}/transfers/{transfer_id}/confirm",
        "/settlements/{settlement_id}/items/{transfer_id}/result",
        "/settlements/{settlement_id}/transfers/{transfer_id}/result",
    )

    def settlement_result_endpoint(
        settlement_id: int,
        transfer_id: int,
        payload: dict[str, Any] | None = Body(default=None),
        account: sqlite3.Row = Depends(get_current_account),
        conn: sqlite3.Connection = Depends(get_db),
        idempotency_key: str | None = Header(
            default=None, alias="Idempotency-Key"
        ),
    ) -> dict[str, Any]:
        return _confirm_transfer_response(
            transfer_id,
            payload,
            account,
            conn,
            settlement_id=settlement_id,
            idempotency_key_header=idempotency_key,
        )

    for path in settlement_paths:
        router.add_api_route(
            path,
            settlement_result_endpoint,
            methods=["POST", "PUT"],
            status_code=200,
        )


_register_result_routes()
