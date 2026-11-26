#!/usr/bin/env python3
"""Restart and HTTP concurrency checks for royalty ledger/settlements.

Starts a real uvicorn subprocess against a temporary SQLite file and races:

* identical sale retries (external order number uniqueness);
* identical refund retries and competing refunds that could over-refund;
* identical settlement request retries;
* repeated transfer success confirmations.

It then restarts uvicorn against the same file and confirms that confirmed
results, pending/failed state, and balances survive.  Finally it opens SQLite
offline and checks money and one-pending-transfer invariants.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from wb_common import Client, register  # noqa: E402
from verify_restart_concurrency import (  # noqa: E402
    find_free_port,
    start_server,
    stop_server,
)


def create_issue(base_url: str, owner: Client) -> dict:
    _, project_data = owner.post(
        "/projects",
        json={"title": "Royalty race source", "content": "final"},
        expected=201,
    )
    project_id = project_data["project"]["id"]
    _, issue_data = owner.post(
        f"/projects/{project_id}/issues",
        json={
            "source_revision": 1,
            "total_quota": 3,
            "idempotency_key": uuid.uuid4().hex,
        },
        expected=201,
    )
    return issue_data["issue"]


def set_share(owner: Client, issue_id: int, creator: Client, bps: int) -> None:
    owner.put(
        f"/issues/{issue_id}/royalty-share",
        json={"creator_username": creator.username, "basis_points": bps},
        expected=200,
    )


def create_sale(owner: Client, issue_id: int, order_no: str, amount: int):
    response, _ = owner.post(
        f"/issues/{issue_id}/sales",
        json={"external_order_no": order_no, "amount": amount},
        expected=201,
    )
    return response


def balances(owner: Client) -> dict[int, dict]:
    _, data = owner.get("/royalties/balances", expected=200)
    return {row["account_id"]: row for row in data["balances"]}


def barrier_map(workers: int, fn) -> list:
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def runner(index: int) -> None:
        barrier.wait(timeout=30)
        value = fn(index)
        with lock:
            outcomes.append(value)

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "royalty race thread timed out"
    return outcomes


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="royalty-verify-"))
    db_path = temp_dir / "royalty.db"
    log_file = temp_dir / "uvicorn.log"
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = start_server(db_path, port, log_file)

    try:
        owner = register(base_url, "roy_owner")
        creator = register(base_url, "roy_creator")
        issue = create_issue(base_url, owner)
        set_share(owner, issue["id"], creator, 3000)

        print("Checking concurrent identical sale registration...")
        sale_order = "RACE-SALE-" + uuid.uuid4().hex
        sale_outcomes = barrier_map(
            8,
            lambda _i: requests.post(
                f"{base_url}/issues/{issue['id']}/sales",
                headers=owner.headers(),
                json={"external_order_no": sale_order, "amount": 1000},
                timeout=30,
            ),
        )
        assert all(r.status_code == 201 for r in sale_outcomes), [
            (r.status_code, r.text) for r in sale_outcomes
        ]
        sale_ids = {r.json()["sale"]["id"] for r in sale_outcomes}
        assert len(sale_ids) == 1, sale_ids
        sale_id = sale_ids.pop()
        print("  eight concurrent retries produced one sale")

        print("Checking concurrent identical refund retry...")
        refund_no = "RACE-REFUND-DUP-" + uuid.uuid4().hex
        dup_refunds = barrier_map(
            8,
            lambda _i: requests.post(
                f"{base_url}/royalties/refunds",
                headers=owner.headers(),
                json={
                    "external_refund_no": refund_no,
                    "sale_external_order_no": sale_order,
                    "amount": 100,
                },
                timeout=30,
            ),
        )
        assert all(r.status_code == 201 for r in dup_refunds), [
            (r.status_code, r.text) for r in dup_refunds
        ]
        refund_ids = {r.json()["refund"]["id"] for r in dup_refunds}
        assert len(refund_ids) == 1, refund_ids
        print("  repeated refund request produced one refund")

        print("Checking competing refunds cannot exceed sale receipts...")
        over_order = "RACE-OVER-" + uuid.uuid4().hex
        create_sale(owner, issue["id"], over_order, 1000).json()
        refund_results = barrier_map(
            20,
            lambda i: requests.post(
                f"{base_url}/royalties/refunds",
                headers=owner.headers(),
                json={
                    "external_refund_no": f"RACE-REFUND-{uuid.uuid4().hex}-{i}",
                    "sale_external_order_no": over_order,
                    "amount": 100,
                },
                timeout=30,
            ),
        )
        statuses = [r.status_code for r in refund_results]
        assert statuses.count(201) == 10, statuses
        assert statuses.count(409) == 10, statuses
        _, sale_view = owner.get(
            f"/issues/{issue['id']}/sales/{over_order}", expected=200
        )
        assert sale_view["sale"]["refunded_amount"] == 1000
        assert sale_view["sale"]["remaining_amount"] == 0
        print("  exactly ten 100-cent refunds committed; ten over-refunds rejected")

        # Add a second unrefunded sale so settlement has merged balances.
        second_order = "RACE-SECOND-" + uuid.uuid4().hex
        create_sale(owner, issue["id"], second_order, 1000).json()

        print("Checking concurrent settlement request retries...")
        set_request_id = "RACE-SETTLEMENT-" + uuid.uuid4().hex
        settlement_responses = barrier_map(
            8,
            lambda _i: requests.post(
                f"{base_url}/royalties/settlements",
                headers=owner.headers(),
                json={"request_id": set_request_id},
                timeout=30,
            ),
        )
        assert all(r.status_code == 201 for r in settlement_responses), [
            (r.status_code, r.text) for r in settlement_responses
        ]
        settlement_ids = {
            r.json()["settlement"]["id"] for r in settlement_responses
        }
        assert len(settlement_ids) == 1, settlement_ids
        settlement = settlement_responses[0].json()["settlement"]
        assert settlement["item_count"] == 2
        by_account = {item["account_id"]: item for item in settlement["items"]}
        assert by_account[creator.account_id]["amount"] == 570
        assert by_account[owner.account_id]["amount"] == 1330
        creator_item = by_account[creator.account_id]
        owner_item = by_account[owner.account_id]
        print("  one merged settlement with fixed creator/owner transfers")

        print("Checking concurrent repeated success confirmation...")
        confirm_responses = barrier_map(
            8,
            lambda _i: requests.post(
                f"{base_url}/royalties/transfers/{creator_item['id']}/result",
                headers=owner.headers(),
                json={"result": "success", "finance_reference": "race-bank"},
                timeout=30,
            ),
        )
        assert all(r.status_code == 200 for r in confirm_responses), [
            (r.status_code, r.text) for r in confirm_responses
        ]
        assert all(
            r.json()["transfer"]["status"] == "paid" for r in confirm_responses
        )
        opposite = owner.post(
            f"/royalties/transfers/{creator_item['id']}/result",
            json={"result": "failed"},
        )
        assert opposite[0].status_code == 409, opposite[0].text
        bal = balances(owner)
        assert bal[creator.account_id]["paid_amount"] == 570, bal
        assert bal[creator.account_id]["net_balance"] == 0, bal
        print("  success was counted once and cannot be reversed")

        print("Restarting uvicorn and checking persisted royalty state...")
        stop_server(process)
        process = start_server(db_path, port, log_file)
        _, health = owner.get("/me", expected=200)
        assert health["account"]["id"] == owner.account_id

        _, fetched = owner.get(
            f"/royalties/settlements/{settlement['id']}", expected=200
        )
        persisted = fetched["settlement"]
        assert persisted["items"][0]["amount"] in (570, 1330)
        creator_after = next(
            item
            for item in persisted["items"]
            if item["account_id"] == creator.account_id
        )
        assert creator_after["status"] == "paid"
        assert creator_after["finance_reference"] == "race-bank"

        print("Checking failure releases occupation after restart...")
        response, data = owner.post(
            f"/royalties/transfers/{owner_item['id']}/result",
            json={"result": "failed", "note": "retry later"},
            expected=200,
        )
        assert data["transfer"]["status"] == "failed"
        bal = balances(owner)
        assert bal[owner.account_id]["pending_amount"] == 0
        assert bal[owner.account_id]["net_balance"] == 1330

        _, second_settlement_data = owner.post(
            "/royalties/settlements",
            json={"request_id": "AFTER-RESTART-" + uuid.uuid4().hex},
            expected=201,
        )
        second_settlement = second_settlement_data["settlement"]
        assert second_settlement["item_count"] == 1
        assert second_settlement["items"][0]["account_id"] == owner.account_id
        assert second_settlement["items"][0]["amount"] == 1330
        print("  failed transfer released; a new full settlement was possible")

        print("Offline SQLite money and payout invariant checks...")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            sales = conn.execute("SELECT * FROM royalty_sales").fetchall()
            assert len(sales) == 3
            for sale in sales:
                assert sale["creator_gross_amount"] + sale["owner_gross_amount"] == sale["gross_amount"]
                assert sale["creator_refunded_amount"] + sale["owner_refunded_amount"] == sale["refunded_amount"]
                assert sale["refunded_amount"] <= sale["gross_amount"]
                refund_sum = conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS n FROM royalty_refunds WHERE sale_id = ?",
                    (sale["id"],),
                ).fetchone()["n"]
                assert refund_sum == sale["refunded_amount"]

            over_sale = conn.execute(
                "SELECT * FROM royalty_sales WHERE external_order_no = ?",
                (over_order,),
            ).fetchone()
            assert over_sale["refunded_amount"] == 1000

            pending_dupes = conn.execute(
                """
                SELECT owner_id, payee_account_id, COUNT(*) AS n
                  FROM royalty_transfers
                 WHERE status = 'pending'
                 GROUP BY owner_id, payee_account_id
                HAVING COUNT(*) > 1
                """
            ).fetchall()
            assert not pending_dupes

            paid_transfers = conn.execute(
                "SELECT COUNT(*) AS n FROM royalty_transfers WHERE status = 'paid'"
            ).fetchone()["n"]
            assert paid_transfers == 1

            immutable = conn.execute(
                """
                SELECT amount, payee_account_id
                  FROM royalty_transfers
                 WHERE settlement_id = ?
                 ORDER BY id
                """,
                (settlement["id"],),
            ).fetchall()
            actual = {(row["payee_account_id"], row["amount"]) for row in immutable}
            assert actual == {(creator.account_id, 570), (owner.account_id, 1330)}
        finally:
            conn.close()

        print("\nPASS: royalty idempotency, refund limits, settlement exclusivity,")
        print("confirmation immutability, restart persistence, and DB invariants.")
    finally:
        stop_server(process)


if __name__ == "__main__":
    main()
