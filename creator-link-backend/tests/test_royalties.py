"""Regression tests for owner-scoped sales, refunds, and royalty settlements."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

os.environ["WORKBENCH_DB"] = f"/tmp/workbench-royalties-pytest-{uuid.uuid4().hex}.db"

from fastapi.testclient import TestClient  # noqa: E402
import pytest  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, prefix: str = "roy") -> SimpleNamespace:
    username = f"{prefix}_{uuid.uuid4().hex[:10]}"
    response = client.post(
        "/auth/register",
        json={"username": username, "password": "test-pass-123456"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return SimpleNamespace(
        id=body["account"]["id"],
        username=username,
        token=body["session"]["token"],
        headers={"Authorization": f"Bearer {body['session']['token']}"},
    )


def make_issue(client: TestClient, owner: SimpleNamespace, *, quota: int = 10):
    project = client.post(
        "/projects",
        headers=owner.headers,
        json={"title": f"Royalty issue {uuid.uuid4().hex[:6]}", "content": "final"},
    ).json()["project"]
    response = client.post(
        f"/projects/{project['id']}/issues",
        headers=owner.headers,
        json={
            "source_revision": 1,
            "total_quota": quota,
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["issue"]


def set_share(client, owner, issue, creator, bps, *, expected=200):
    response = client.put(
        f"/issues/{issue['id']}/royalty-share",
        headers=owner.headers,
        json={"creator_username": creator.username, "basis_points": bps},
    )
    assert response.status_code == expected, response.text
    return response


def sale(client, owner, issue, order_no, amount, *, expected=201):
    response = client.post(
        f"/issues/{issue['id']}/sales",
        headers=owner.headers,
        json={"external_order_no": order_no, "amount": amount},
    )
    assert response.status_code == expected, response.text
    return response


def refund(client, owner, payload, *, expected=201, path=None):
    url = path or "/royalties/refunds"
    response = client.post(url, headers=owner.headers, json=payload)
    assert response.status_code == expected, response.text
    return response


def balances(client, owner):
    response = client.get("/royalties/balances", headers=owner.headers)
    assert response.status_code == 200, response.text
    return {row["account_id"]: row for row in response.json()["balances"]}


def create_settlement(client, owner, request_id, *, expected=201):
    response = client.post(
        "/royalties/settlements",
        headers=owner.headers,
        json={"request_id": request_id},
    )
    assert response.status_code == expected, response.text
    return response


def test_share_validation_and_owner_only(client):
    owner = register(client, "share_owner")
    creator = register(client, "share_creator")
    manager = register(client, "share_manager")
    issue = make_issue(client, owner)

    # No sale before designating the split account.
    blocked = sale(client, owner, issue, "no-share-1", 100, expected=409)
    assert blocked.json()["error"]["code"] == "ROYALTY_SHARE_REQUIRED"

    ok = set_share(client, owner, issue, creator, 3330)
    assert ok.json()["royalty_share"]["basis_points"] == 3330
    assert ok.json()["royalty_share"]["creator"]["id"] == creator.id

    # Owner cannot be the split creator.
    self_share = client.put(
        f"/issues/{issue['id']}/royalty-share",
        headers=owner.headers,
        json={"creator_username": owner.username, "basis_points": 100},
    )
    assert self_share.status_code == 422

    # Basis points are an inclusive integer basis-point range.
    bad_bps = client.put(
        f"/issues/{issue['id']}/royalty-share",
        headers=owner.headers,
        json={"creator_username": creator.username, "basis_points": 10_001},
    )
    assert bad_bps.status_code == 422

    # Channel managers/readers do not inherit royalty ledger authority.
    allocated = client.post(
        f"/issues/{issue['id']}/channels",
        headers=owner.headers,
        json={
            "manager_username": manager.username,
            "quota": 1,
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert allocated.status_code == 201, allocated.text
    manager_allowed = client.get(
        f"/issues/{issue['id']}/sales", headers=manager.headers
    )
    assert manager_allowed.status_code == 403
    manager_write = client.put(
        f"/issues/{issue['id']}/royalty-share",
        headers=manager.headers,
        json={"creator_username": manager.username, "basis_points": 1},
    )
    assert manager_write.status_code == 403


def test_sale_snapshot_rate_change_replay_and_conflict(client):
    owner = register(client, "snap_owner")
    first = register(client, "snap_first")
    second = register(client, "snap_second")
    issue = make_issue(client, owner)
    set_share(client, owner, issue, first, 3330)

    order = f"ORDER-{uuid.uuid4().hex}"
    first_response = sale(client, owner, issue, order, 1000)
    data = first_response.json()
    assert data["idempotent_replay"] is False
    assert data["sale"]["creator"]["id"] == first.id
    assert data["sale"]["creator_amount"] == 333
    assert data["sale"]["owner_amount"] == 667

    # Changing collaborator/rate does not rewrite the old sale.
    set_share(client, owner, issue, second, 5000)
    new_order = f"ORDER-{uuid.uuid4().hex}"
    new_sale = sale(client, owner, issue, new_order, 1000).json()["sale"]
    assert new_sale["creator"]["id"] == second.id
    assert new_sale["creator_amount"] == 500
    old_sale = client.get(
        f"/issues/{issue['id']}/sales/{order}", headers=owner.headers
    ).json()["sale"]
    assert old_sale["creator"]["id"] == first.id
    assert old_sale["basis_points"] == 3330

    # Same external number + same parameters returns the original result.
    replay = sale(client, owner, issue, order, 1000)
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["sale"]["id"] == data["sale"]["id"]

    # Same external number with changed money is a conflict.
    conflict = sale(client, owner, issue, order, 1001, expected=409)
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    listed = client.get(f"/issues/{issue['id']}/sales", headers=owner.headers)
    assert listed.status_code == 200
    assert len(listed.json()["sales"]) == 2


def test_refunds_use_cumulative_floor_differences_and_cannot_over_refund(client):
    owner = register(client, "ref_owner")
    creator = register(client, "ref_creator")
    issue = make_issue(client, owner)
    set_share(client, owner, issue, creator, 3300)
    order = f"REF-SALE-{uuid.uuid4().hex}"
    original = sale(client, owner, issue, order, 100).json()["sale"]
    assert original["creator_amount"] == 33
    assert original["owner_amount"] == 67

    r1_no = f"R1-{uuid.uuid4().hex}"
    r1 = refund(
        client,
        owner,
        {
            "external_refund_no": r1_no,
            "sale_external_order_no": order,
            "amount": 1,
        },
    ).json()
    # floor(99 * 333 / 10000) == 32, so the first cent reverses creator only.
    assert r1["refund"]["creator_deduction"] == 1
    assert r1["refund"]["owner_deduction"] == 0
    assert r1["sale"]["creator_net_amount"] == 32
    assert r1["sale"]["owner_net_amount"] == 67

    # Repeated refund request returns the same refund.
    replay = refund(
        client,
        owner,
        {
            "external_refund_no": r1_no,
            "sale_external_order_no": order,
            "amount": 1,
        },
    )
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["refund"]["id"] == r1["refund"]["id"]

    # Same refund number with different amount conflicts.
    refund(
        client,
        owner,
        {
            "external_refund_no": r1_no,
            "sale_external_order_no": order,
            "amount": 2,
        },
        expected=409,
    )

    # 99 cents remain refundable after the 1-cent refund.
    over = refund(
        client,
        owner,
        {
            "external_refund_no": f"OVER-{uuid.uuid4().hex}",
            "sale_external_order_no": order,
            "amount": 100,
        },
        expected=409,
    )
    assert over.json()["error"]["code"] == "REFUND_EXCEEDS_SALE_AMOUNT"

    r2 = refund(
        client,
        owner,
        {
            "external_refund_no": f"R2-{uuid.uuid4().hex}",
            "sale_external_order_no": order,
            "amount": 50,
        },
    ).json()
    # Remaining 49: floor(49*3300/10000)=16. Cumulative creator reversal 17;
    # previous reversal was 1, so this refund reverses 16 creator / 34 owner.
    assert r2["refund"]["creator_deduction"] == 16
    assert r2["refund"]["owner_deduction"] == 34

    final_refund = refund(
        client,
        owner,
        {
            "external_refund_no": f"R3-{uuid.uuid4().hex}",
            "sale_external_order_no": order,
            "amount": 49,
        },
    ).json()
    assert final_refund["refund"]["creator_deduction"] == 16
    assert final_refund["refund"]["owner_deduction"] == 33
    final_sale = final_refund["sale"]
    assert final_sale["remaining_amount"] == 0
    assert final_sale["creator_net_amount"] == 0
    assert final_sale["owner_net_amount"] == 0

    # Once fully refunded there is no remaining refundable amount.
    refund(
        client,
        owner,
        {
            "external_refund_no": f"R4-{uuid.uuid4().hex}",
            "sale_external_order_no": order,
            "amount": 1,
        },
        expected=409,
    )


def test_owner_merges_issues_by_payee_and_confirmations(client):
    owner = register(client, "merge_owner")
    creator = register(client, "merge_creator")
    issue1 = make_issue(client, owner)
    issue2 = make_issue(client, owner)
    set_share(client, owner, issue1, creator, 3000)
    set_share(client, owner, issue2, creator, 2000)

    sale(client, owner, issue1, f"M1-{uuid.uuid4().hex}", 1000)
    sale(client, owner, issue2, f"M2-{uuid.uuid4().hex}", 500)

    before = balances(client, owner)
    assert before[creator.id]["earned_amount"] == 400
    assert before[creator.id]["net_balance"] == 400
    assert before[owner.id]["earned_amount"] == 1100
    assert before[owner.id]["net_balance"] == 1100

    req = f"SET-{uuid.uuid4().hex}"
    created = create_settlement(client, owner, req).json()
    settlement = created["settlement"]
    assert created["idempotent_replay"] is False
    assert settlement["item_count"] == 2
    assert settlement["total_amount"] == 1500
    by_account = {item["account_id"]: item for item in settlement["items"]}
    assert by_account[creator.id]["amount"] == 400
    assert by_account[owner.id]["amount"] == 1100

    after_pending = balances(client, owner)
    assert after_pending[creator.id]["net_balance"] == 0
    assert after_pending[creator.id]["pending_amount"] == 400
    assert after_pending[owner.id]["net_balance"] == 0

    # No eligible account while both transfers are pending.
    empty = create_settlement(
        client, owner, f"EMPTY-{uuid.uuid4().hex}", expected=409
    )
    assert empty.json()["error"]["code"] == "NO_PAYABLE_BALANCES"

    # Reusing the original request id returns the original order, not a second.
    replay = create_settlement(client, owner, req)
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["settlement"]["id"] == settlement["id"]

    # Same request id with changed parameters conflicts.
    changed = client.post(
        "/royalties/settlements",
        headers=owner.headers,
        json={"request_id": req, "note": "changed-parameter"},
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    creator_item = by_account[creator.id]
    owner_item = by_account[owner.id]

    success = client.post(
        f"/royalties/transfers/{creator_item['id']}/result",
        headers=owner.headers,
        json={"result": "success", "finance_reference": "bank-1"},
    )
    assert success.status_code == 200, success.text
    assert success.json()["transfer"]["status"] == "paid"
    assert success.json()["transfer"]["finance_reference"] == "bank-1"

    success_replay = client.post(
        f"/royalties/transfers/{creator_item['id']}/result",
        headers=owner.headers,
        json={"result": "success", "finance_reference": "bank-1"},
    )
    assert success_replay.json()["idempotent_replay"] is True

    opposite = client.post(
        f"/royalties/transfers/{creator_item['id']}/result",
        headers=owner.headers,
        json={"result": "failed"},
    )
    assert opposite.status_code == 409
    assert opposite.json()["error"]["code"] == "TRANSFER_RESULT_IMMUTABLE"

    failed = client.post(
        f"/royalties/transfers/{owner_item['id']}/result",
        headers=owner.headers,
        json={"status": "failed", "note": "bad account"},
    )
    assert failed.status_code == 200
    assert failed.json()["transfer"]["status"] == "failed"

    after_results = balances(client, owner)
    assert after_results[creator.id]["paid_amount"] == 400
    assert after_results[creator.id]["net_balance"] == 0
    # Failure released the pending occupation.
    assert after_results[owner.id]["pending_amount"] == 0
    assert after_results[owner.id]["net_balance"] == 1100

    second = create_settlement(client, owner, f"SET2-{uuid.uuid4().hex}").json()[
        "settlement"
    ]
    assert second["item_count"] == 1
    assert second["items"][0]["account_id"] == owner.id
    assert second["items"][0]["amount"] == 1100

    listing = client.get("/royalties/settlements", headers=owner.headers)
    assert listing.status_code == 200
    assert len(listing.json()["settlements"]) == 2


def test_pending_item_is_immutable_and_negative_balance_is_recouped(client):
    owner = register(client, "neg_owner")
    creator = register(client, "neg_creator")
    issue = make_issue(client, owner)
    set_share(client, owner, issue, creator, 10_000)
    order = f"NEG-{uuid.uuid4().hex}"
    sale(client, owner, issue, order, 1000)

    settlement = create_settlement(
        client, owner, f"NEG-SET-{uuid.uuid4().hex}"
    ).json()["settlement"]
    item = settlement["items"][0]
    assert item["account_id"] == creator.id
    assert item["amount"] == 1000

    # Refund while the transfer is pending: item amount/payee stay fixed.
    refund(
        client,
        owner,
        {
            "external_refund_no": f"NEG-R1-{uuid.uuid4().hex}",
            "sale_external_order_no": order,
            "amount": 300,
        },
    )
    unchanged = client.get(
        f"/royalties/settlements/{settlement['id']}", headers=owner.headers
    ).json()["settlement"]
    assert unchanged["items"][0]["amount"] == 1000
    assert unchanged["items"][0]["payee_account_id"] == creator.id

    row = balances(client, owner)[creator.id]
    assert row["earned_amount"] == 700
    assert row["pending_amount"] == 1000
    assert row["net_balance"] == -300
    assert row["settleable"] is False

    # Successful payment posts the full immutable instruction; the negative
    # net balance is later recovered from new income in the same account.
    confirm = client.post(
        f"/royalties/transfers/{item['id']}/result",
        headers=owner.headers,
        json={"result": "success"},
    )
    assert confirm.status_code == 200
    assert balances(client, owner)[creator.id]["net_balance"] == -300

    sale(client, owner, issue, f"NEG2-{uuid.uuid4().hex}", 500)
    recovered = balances(client, owner)[creator.id]
    assert recovered["earned_amount"] == 1200
    assert recovered["paid_amount"] == 1000
    assert recovered["net_balance"] == 200

    next_settlement = create_settlement(
        client, owner, f"NEG-SET2-{uuid.uuid4().hex}"
    ).json()["settlement"]
    assert len(next_settlement["items"]) == 1
    assert next_settlement["items"][0]["amount"] == 200


def test_royalty_ledger_is_isolated_between_owners(client):
    owner_a = register(client, "iso_owner_a")
    owner_b = register(client, "iso_owner_b")
    creator = register(client, "iso_creator")
    issue_a = make_issue(client, owner_a)
    set_share(client, owner_a, issue_a, creator, 5000)
    sale(client, owner_a, issue_a, f"A-{uuid.uuid4().hex}", 1000)
    settlement_a = create_settlement(
        client, owner_a, f"A-SET-{uuid.uuid4().hex}"
    ).json()["settlement"]

    # Another owner cannot read A's issue ledger or settlement.
    assert (
        client.get(f"/issues/{issue_a['id']}/sales", headers=owner_b.headers).status_code
        == 403
    )
    assert (
        client.get(
            f"/royalties/settlements/{settlement_a['id']}", headers=owner_b.headers
        ).status_code
        == 403
    )
    transfer_id = settlement_a["items"][0]["id"]
    assert (
        client.post(
            f"/royalties/transfers/{transfer_id}/result",
            headers=owner_b.headers,
            json={"result": "success"},
        ).status_code
        == 403
    )

    # B has an empty ledger and cannot accidentally offset against A.
    b_balances = balances(client, owner_b)
    assert b_balances == {}
    empty_b = create_settlement(
        client, owner_b, f"B-EMPTY-{uuid.uuid4().hex}", expected=409
    )
    assert empty_b.status_code == 409

    # The designated creator does not inherit owner authority on the issue.
    assert (
        client.get(f"/issues/{issue_a['id']}/sales", headers=creator.headers).status_code
        == 403
    )
