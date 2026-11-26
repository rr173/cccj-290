"""End-to-end tests for limited-edition issues.

Each test module gets its own SQLite file (``WORKBENCH_DB`` is read at import
time by ``app.db``), so these tests never disturb ``test_workbench.py``.
"""

from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "WORKBENCH_DB", f"/tmp/workbench-issues-pytest-{uuid.uuid4().hex}.db"
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def register(client: TestClient, prefix: str = "user") -> SimpleNamespace:
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


def make_project(client, owner, *, title="Issue source", content="original body"):
    response = client.post(
        "/projects",
        headers=owner.headers,
        json={"title": title, "content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def publish_issue(client, owner, project, *, revision=1, quota=5):
    response = client.post(
        f"/projects/{project['id']}/issues",
        headers=owner.headers,
        json={
            "source_revision": revision,
            "total_quota": quota,
            "idempotency_key": uuid.uuid4().hex,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["issue"]


def invite(client, actor, issue, reader, *, channel_id=None, ttl=3600, key=None):
    if channel_id is None:
        path = f"/issues/{issue['id']}/invitations"
        body = {"reader_username": reader.username, "ttl_seconds": ttl}
    else:
        path = f"/issues/{issue['id']}/channels/{channel_id}/invitations"
        body = {"reader_username": reader.username, "ttl_seconds": ttl}
    if key is not None:
        body["idempotency_key"] = key
    return client.post(path, headers=actor.headers, json=body)


def allocate(client, actor, issue, manager, quota, *, parent=None, key=None):
    body = {"manager_username": manager.username, "quota": quota}
    if parent is not None:
        body["parent_channel_id"] = parent
    if key is not None:
        body["idempotency_key"] = key
    else:
        body["idempotency_key"] = uuid.uuid4().hex
    return client.post(
        f"/issues/{issue['id']}/channels", headers=actor.headers, json=body
    )


def quota(client, actor, issue):
    response = client.get(f"/issues/{issue['id']}/channels", headers=actor.headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_issue_snapshot_is_independent_of_later_edits(client):
    owner = register(client, "snap_owner")
    reader = register(client, "snap_reader")
    project = make_project(client, owner, title="Edition v1", content="body v1")

    issue = publish_issue(client, owner, project, revision=1, quota=2)
    assert issue["title"] == "Edition v1"
    assert issue["content"] == "body v1"
    assert issue["total_quota"] == 2

    # Continue editing the workbench project.
    edited = client.patch(
        f"/projects/{project['id']}",
        headers=owner.headers,
        json={"title": "Edition v2", "content": "body v2"},
    )
    assert edited.status_code == 200

    invitation = invite(client, owner, issue, reader).json()["invitation"]
    accept = client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )
    assert accept.status_code == 200, accept.text

    read = client.get(f"/issues/{issue['id']}/read", headers=reader.headers)
    assert read.status_code == 200, read.text
    snapshot = read.json()["issue"]
    assert snapshot["title"] == "Edition v1"
    assert snapshot["content"] == "body v1"


def test_reader_does_not_gain_project_or_history_access(client):
    owner = register(client, "iso_owner")
    reader = register(client, "iso_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=1)
    invitation = invite(client, owner, issue, reader).json()["invitation"]
    client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )

    # Claiming an issue grants no project/history/curation access.
    assert (
        client.get(f"/projects/{project['id']}", headers=reader.headers).status_code
        == 403
    )
    assert (
        client.get(
            f"/projects/{project['id']}/history", headers=reader.headers
        ).status_code
        == 403
    )
    # And no shared owner relationship: reader cannot see it in /projects.
    listed = client.get("/projects", headers=reader.headers).json()["projects"]
    assert listed == []


def test_owner_allocates_to_manager_who_allocates_downstream(client):
    owner = register(client, "chain_owner")
    manager = register(client, "chain_manager")
    sub_manager = register(client, "chain_sub")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=10)

    top = allocate(client, owner, issue, manager, 6).json()["channel"]
    assert top["available"] == 6
    assert top["manager"]["id"] == manager.id

    # Manager sees only its own channel and its balance.
    view = quota(client, manager, issue)
    assert [c["id"] for c in view["channels"]] == [top["id"]]
    assert view["quota"]["free"] == 10  # 4 owner + 6 manager

    child = allocate(client, manager, issue, sub_manager, 4, parent=top["id"]).json()[
        "channel"
    ]
    assert child["available"] == 4
    assert child["parent_id"] == top["id"]

    view = quota(client, manager, issue)
    by_id = {c["id"]: c for c in view["channels"]}
    assert by_id[top["id"]]["available"] == 2

    owner_view = quota(client, owner, issue)
    assert owner_view["unallocated_owner_quota"] == 4
    manager_ids = [c["id"] for c in owner_view["channels"]]
    assert top["id"] in manager_ids and child["id"] in manager_ids


def test_allocated_quota_cannot_be_used_at_two_levels(client):
    owner = register(client, "double_owner")
    manager = register(client, "double_manager")
    reader = register(client, "double_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=3)

    channel = allocate(client, owner, issue, manager, 3).json()["channel"]
    # Owner's unallocated pool is now empty.
    over = allocate(client, owner, issue, register(client, "double_extra"), 1)
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "INSUFFICIENT_QUOTA"

    # Manager can send exactly 3 invitations, not 4.
    for i in range(3):
        r = register(client, f"double_r{i}")
        assert invite(client, manager, issue, r, channel_id=channel["id"]).status_code == 201
    extra = register(client, "double_r_extra")
    blocked = invite(client, manager, issue, extra, channel_id=channel["id"])
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INSUFFICIENT_QUOTA"
    _ = reader


def test_one_live_invitation_per_reader_across_channels(client):
    owner = register(client, "uniq_owner")
    manager_a = register(client, "uniq_ma")
    manager_b = register(client, "uniq_mb")
    reader = register(client, "uniq_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=10)

    ch_a = allocate(client, owner, issue, manager_a, 3).json()["channel"]
    ch_b = allocate(client, owner, issue, manager_b, 3).json()["channel"]

    first = invite(client, manager_a, issue, reader, channel_id=ch_a["id"])
    assert first.status_code == 201
    first_id = first.json()["invitation"]["id"]

    # A different channel inviting the same reader must not take a second seat.
    second = invite(client, manager_b, issue, reader, channel_id=ch_b["id"])
    assert second.status_code == 201
    assert second.json()["invitation"]["id"] == first_id
    assert second.json()["reused_existing_invitation"] is True

    view = quota(client, owner, issue)
    assert view["quota"]["held_by_invitations"] == 1


def test_resent_invitation_keeps_deadline_and_does_not_take_seat(client):
    owner = register(client, "resend_owner")
    reader = register(client, "resend_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=2)

    first = invite(client, owner, issue, reader, ttl=1800, key="key-1")
    assert first.status_code == 201
    inv1 = first.json()["invitation"]

    # Retry with the same key and identical body: same invitation, no seat.
    replay = invite(client, owner, issue, reader, ttl=1800, key="key-1")
    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["invitation"]["id"] == inv1["id"]
    assert replay.json()["invitation"]["expires_at"] == inv1["expires_at"]

    # Re-sending without a key also reuses the live invitation, no extension.
    resent = invite(client, owner, issue, reader, ttl=999999)
    assert resent.json()["invitation"]["id"] == inv1["id"]
    assert resent.json()["invitation"]["expires_at"] == inv1["expires_at"]

    assert quota(client, owner, issue)["quota"]["held_by_invitations"] == 1


def test_only_named_reader_can_accept_and_accept_is_idempotent(client):
    owner = register(client, "acc_owner")
    reader = register(client, "acc_reader")
    other = register(client, "acc_other")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=2)
    invitation = invite(client, owner, issue, reader).json()["invitation"]

    denied = client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=other.headers
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "NOT_INVITED_READER"

    first = client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )
    assert first.status_code == 200
    entitlement_id = first.json()["entitlement"]["id"]

    again = client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["entitlement"]["id"] == entitlement_id

    mine = client.get("/me/issues", headers=reader.headers).json()["issues"]
    assert len(mine) == 1


def test_expired_invitation_releases_seat_without_manual_cleanup(client):
    owner = register(client, "exp_owner")
    patient = register(client, "exp_patient")
    late = register(client, "exp_late")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=1)

    invite(client, owner, issue, patient, ttl=1)

    # A second reader cannot claim the only seat while it is held.
    blocked = invite(client, owner, issue, late)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INSUFFICIENT_QUOTA"

    time.sleep(1.2)
    # Expiry is settled lazily on the next read, so the seat is already free.
    assert quota(client, owner, issue)["quota"]["free"] == 1
    refill = invite(client, owner, issue, late)
    assert refill.status_code == 201

    # The original reader can no longer claim.
    invitations = client.get(
        "/me/issue-invitations", headers=patient.headers
    ).json()["invitations"]
    expired = [i for i in invitations if i["status"] == "expired"]
    assert expired
    late_accept = client.post(
        f"/issues/invitations/{expired[0]['id']}/accept", headers=patient.headers
    )
    assert late_accept.status_code == 410
    assert late_accept.json()["error"]["code"] == "ISSUE_INVITATION_EXPIRED"


def test_owner_disable_cascades_and_releases_free_quota(client):
    owner = register(client, "dis_owner")
    manager = register(client, "dis_manager")
    sub = register(client, "dis_sub")
    reader = register(client, "dis_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=12)

    branch = allocate(client, owner, issue, manager, 8).json()["channel"]
    child = allocate(client, manager, issue, sub, 5, parent=branch["id"]).json()[
        "channel"
    ]
    # Spend one seat in the child so the branch has some held quota.
    live = invite(client, sub, issue, reader, channel_id=child["id"]).json()[
        "invitation"
    ]

    disabled = client.post(
        f"/issues/{issue['id']}/channels/{branch['id']}/disable",
        headers=owner.headers,
    )
    assert disabled.status_code == 200, disabled.text
    body = disabled.json()
    assert set(body["cascaded_channel_ids"]) == {child["id"]}
    # Free seats in the branch (8 - 5 allocated=3 at branch, 5-1=4 at child) = 7.
    assert body["released_to_parent"]["amount"] == 7
    # The live invitation's seat is NOT returned; it stays claimable.
    assert quota(client, owner, issue)["quota"]["held_by_invitations"] == 1

    # New allocation/invitation in the disabled subtree is blocked.
    assert invite(client, sub, issue, register(client, "dis_r2"), channel_id=child["id"]).status_code == 409

    # The already-issued invitation is still claimable, and its entitlement
    # is readable after the channel is disabled.
    accept = client.post(
        f"/issues/invitations/{live['id']}/accept", headers=reader.headers
    )
    assert accept.status_code == 200, accept.text
    read = client.get(f"/issues/{issue['id']}/read", headers=reader.headers)
    assert read.status_code == 200


def test_manager_can_only_disable_its_direct_child(client):
    owner = register(client, "perm_owner")
    manager = register(client, "perm_manager")
    sub = register(client, "perm_sub")
    outsider = register(client, "perm_outsider")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=12)

    branch = allocate(client, owner, issue, manager, 6).json()["channel"]
    child = allocate(client, manager, issue, sub, 3, parent=branch["id"]).json()[
        "channel"
    ]

    # Manager may disable its direct child.
    ok = client.post(
        f"/issues/{issue['id']}/channels/{child['id']}/disable",
        headers=manager.headers,
    )
    assert ok.status_code == 200, ok.text
    # But not its own channel (owned above it), nor a channel it did not create.
    self_disable = client.post(
        f"/issues/{issue['id']}/channels/{branch['id']}/disable",
        headers=manager.headers,
    )
    assert self_disable.status_code == 403
    outsider_try = client.post(
        f"/issues/{issue['id']}/channels/{branch['id']}/disable",
        headers=outsider.headers,
    )
    assert outsider_try.status_code == 403


def test_disabled_branch_expiry_returns_seat_to_nearest_active_ancestor(client):
    owner = register(client, "anc_owner")
    manager = register(client, "anc_manager")
    reader = register(client, "anc_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=5)

    branch = allocate(client, owner, issue, manager, 4).json()["channel"]
    # One pending invitation, then disable the branch.
    invite(client, manager, issue, reader, channel_id=branch["id"], ttl=1)
    client.post(
        f"/issues/{issue['id']}/channels/{branch['id']}/disable",
        headers=owner.headers,
    )
    # Free seats already returned to owner: 4 granted - 1 held = 3.
    assert quota(client, owner, issue)["quota"]["free"] == 1 + 3

    time.sleep(1.2)
    # When the held invitation expires, its seat goes to the nearest active
    # ancestor (the owner's root), not into limbo.
    totals = quota(client, owner, issue)["quota"]
    assert totals["free"] == 5
    assert totals["held_by_invitations"] == 0


def test_reallocating_to_same_manager_yields_new_channel(client):
    owner = register(client, "new_ch_owner")
    manager = register(client, "new_ch_manager")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=6)

    first = allocate(client, owner, issue, manager, 2).json()["channel"]
    second = allocate(client, owner, issue, manager, 2).json()["channel"]
    assert first["id"] != second["id"]
    # The old channel is still active and keeps its own quota.
    view = quota(client, manager, issue)
    ids = {c["id"] for c in view["channels"]}
    assert {first["id"], second["id"]} <= ids


def test_create_and_allocate_are_idempotent_on_retry(client):
    owner = register(client, "idem_owner")
    manager = register(client, "idem_manager")
    project = make_project(client, owner)

    key = uuid.uuid4().hex
    payload = {"source_revision": 1, "total_quota": 4, "idempotency_key": key}
    first = client.post(
        f"/projects/{project['id']}/issues", headers=owner.headers, json=payload
    )
    assert first.status_code == 201
    second = client.post(
        f"/projects/{project['id']}/issues", headers=owner.headers, json=payload
    )
    assert second.status_code == 201
    assert second.json()["idempotent_replay"] is True
    assert second.json()["issue"]["id"] == first.json()["issue"]["id"]

    issue = first.json()["issue"]
    ch_key = uuid.uuid4().hex
    ch1 = allocate(client, owner, issue, manager, 2, key=ch_key)
    ch2 = allocate(client, owner, issue, manager, 2, key=ch_key)
    assert ch1.status_code == 201 and ch2.status_code == 201
    assert ch2.json()["idempotent_replay"] is True
    assert ch1.json()["channel"]["id"] == ch2.json()["channel"]["id"]
    # Only one channel was created: owner 2 unallocated + child 2 = 4 free.
    assert quota(client, owner, issue)["quota"]["free"] == 4
    assert quota(client, owner, issue)["unallocated_owner_quota"] == 2


def test_failed_create_leaves_no_partial_issue(client):
    owner = register(client, "partial_owner")
    project = make_project(client, owner)
    before = client.get(
        f"/projects/{project['id']}/issues", headers=owner.headers
    ).json()["issues"]
    bad = client.post(
        f"/projects/{project['id']}/issues",
        headers=owner.headers,
        json={"source_revision": 99, "total_quota": 3, "idempotency_key": "x"},
    )
    assert bad.status_code == 404
    after = client.get(
        f"/projects/{project['id']}/issues", headers=owner.headers
    ).json()["issues"]
    assert len(after) == len(before)


def test_issue_quota_invariant_holds(client):
    owner = register(client, "inv_owner")
    manager = register(client, "inv_manager")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=9)

    channel = allocate(client, owner, issue, manager, 5).json()["channel"]
    readers = [register(client, f"inv_r{i}") for i in range(3)]
    invitations = []
    for reader in readers:
        body = invite(
            client, manager, issue, reader, channel_id=channel["id"]
        ).json()
        invitations.append(body["invitation"])

    # Claim one, leave the other two pending.
    accepted = client.post(
        f"/issues/invitations/{invitations[0]['id']}/accept",
        headers=readers[0].headers,
    )
    assert accepted.status_code == 200

    summary = quota(client, owner, issue)["quota"]
    assert summary["free"] + summary["held_by_invitations"] + summary["claimed"] == 9
    assert summary["claimed"] == 1
    assert summary["held_by_invitations"] == 2
    assert summary["free"] == 6


def test_disabling_a_channel_twice_is_idempotent(client):
    owner = register(client, "disidem_owner")
    manager = register(client, "disidem_manager")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=6)
    channel = allocate(client, owner, issue, manager, 4).json()["channel"]

    first = client.post(
        f"/issues/{issue['id']}/channels/{channel['id']}/disable",
        headers=owner.headers,
    )
    assert first.status_code == 200
    released_first = first.json()["released_to_parent"]["amount"]
    assert released_first == 4

    second = client.post(
        f"/issues/{issue['id']}/channels/{channel['id']}/disable",
        headers=owner.headers,
    )
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    # No double release: total free is still the full quota.
    assert quota(client, owner, issue)["quota"]["free"] == 6


def test_disabled_channel_blocks_downstream_allocation_and_invites(client):
    owner = register(client, "blk_owner")
    manager = register(client, "blk_manager")
    sub = register(client, "blk_sub")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=10)
    branch = allocate(client, owner, issue, manager, 6).json()["channel"]
    child = allocate(client, manager, issue, sub, 3, parent=branch["id"]).json()[
        "channel"
    ]

    # Manager disables its direct child; the whole subtree is now blocked.
    assert (
        client.post(
            f"/issues/{issue['id']}/channels/{child['id']}/disable",
            headers=manager.headers,
        ).status_code
        == 200
    )
    blocked_invite = invite(
        client, sub, issue, register(client, "blk_r1"), channel_id=child["id"]
    )
    assert blocked_invite.status_code == 409
    assert blocked_invite.json()["error"]["code"] == "CHANNEL_DISABLED"


def test_manager_cannot_invite_from_a_channel_it_does_not_manage(client):
    owner = register(client, "scope_owner")
    manager_a = register(client, "scope_ma")
    manager_b = register(client, "scope_mb")
    reader = register(client, "scope_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=8)
    ch_a = allocate(client, owner, issue, manager_a, 3).json()["channel"]
    _ = allocate(client, owner, issue, manager_b, 3)

    forbidden = invite(client, manager_b, issue, reader, channel_id=ch_a["id"])
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "NOT_CHANNEL_MANAGER"


def test_manager_whitelist_of_own_channels_only(client):
    owner = register(client, "wl_owner")
    manager = register(client, "wl_manager")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=9)
    own = allocate(client, owner, issue, manager, 4).json()["channel"]
    allocate(client, owner, issue, register(client, "wl_other"), 2)

    view = quota(client, manager, issue)
    assert {c["id"] for c in view["channels"]} == {own["id"]}
    assert sum(c["available"] for c in view["channels"]) == 4


def test_reader_sees_expired_invitation_status(client):
    owner = register(client, "stat_owner")
    reader = register(client, "stat_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=1)
    invitation = invite(client, owner, issue, reader, ttl=1).json()["invitation"]
    time.sleep(1.2)

    mine = client.get("/me/issue-invitations", headers=reader.headers).json()
    match = [i for i in mine["invitations"] if i["id"] == invitation["id"]][0]
    assert match["status"] == "expired"
    assert mine["entitlements"] == []


def test_reallocation_preserves_live_invitation_on_old_channel(client):
    owner = register(client, "pres_owner")
    manager = register(client, "pres_manager")
    reader = register(client, "pres_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=6)

    old = allocate(client, owner, issue, manager, 3).json()["channel"]
    invitation = invite(client, manager, issue, reader, channel_id=old["id"]).json()[
        "invitation"
    ]

    # Re-allocating to the same manager creates a brand-new channel and does
    # not disturb the old one or its live invitation.
    new = allocate(client, owner, issue, manager, 2).json()["channel"]
    assert new["id"] != old["id"]
    old_view = {c["id"]: c for c in quota(client, owner, issue)["channels"]}
    assert old_view[old["id"]]["status"] == "active"

    accepted = client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )
    assert accepted.status_code == 200
    assert accepted.json()["entitlement"]["channel_id"] == old["id"]


def test_unclaimed_invitation_blocks_second_invitation_but_entitlement_allows_re_read(client):
    owner = register(client, "reread_owner")
    reader = register(client, "reread_reader")
    project = make_project(client, owner)
    issue = publish_issue(client, owner, project, quota=2)
    invitation = invite(client, owner, issue, reader).json()["invitation"]
    client.post(
        f"/issues/invitations/{invitation['id']}/accept", headers=reader.headers
    )

    # Once entitled, another invitation is refused (no double seat).
    second = invite(client, owner, issue, register(client, "reread_other_filler"))
    assert second.status_code == 201  # a different reader still has a seat

    same_reader = invite(client, owner, issue, reader)
    assert same_reader.status_code == 409
    assert same_reader.json()["error"]["code"] == "ALREADY_ENTITLED"

    # Re-reading the claimed issue is stable and still scoped to the reader.
    for _ in range(2):
        read = client.get(f"/issues/{issue['id']}/read", headers=reader.headers)
        assert read.status_code == 200


def test_non_owner_cannot_publish_issue(client):
    owner = register(client, "pub_owner")
    other = register(client, "pub_other")
    project = make_project(client, owner)
    response = client.post(
        f"/projects/{project['id']}/issues",
        headers=other.headers,
        json={"source_revision": 1, "total_quota": 2, "idempotency_key": "k"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "NOT_PROJECT_OWNER"


