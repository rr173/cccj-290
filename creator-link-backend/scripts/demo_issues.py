#!/usr/bin/env python3
"""Limited-edition issue demo: owner -> manager -> sub-manager -> reader.

Shows:
  * publishing an immutable snapshot of an existing project revision;
  * allocating quota down the channel tree;
  * issuing invitations and a reader claiming one;
  * that the reader gains no project/history access;
  * that further workbench edits do not change the issue.

Usage:
    python scripts/demo_issues.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

import uuid

from wb_common import (
    BOLD,
    RESET,
    default_base_url,
    info,
    ok,
    register,
    step,
    wait_for_health,
)


def main() -> None:
    base_url = default_base_url()
    wait_for_health(base_url)

    step("Register owner, channel manager, sub-manager, reader")
    owner = register(base_url, "owner")
    manager = register(base_url, "manager")
    sub = register(base_url, "submgr")
    reader = register(base_url, "reader")

    step("Owner creates a project and publishes revision 1 as a limited issue")
    _, data = owner.post(
        "/projects",
        json={"title": "Signed edition", "content": "the final text, v1"},
        expected=201,
    )
    project_id = data["project"]["id"]
    _, data = owner.post(
        f"/projects/{project_id}/issues",
        json={
            "source_revision": 1,
            "total_quota": 20,
            "idempotency_key": uuid.uuid4().hex,
        },
        expected=201,
    )
    issue = data["issue"]
    ok(f"issue {issue['id']} snapshots revision {issue['source_revision']} "
       f"with quota {issue['total_quota']}")

    step("Owner allocates 10 seats to manager, who allocates 4 to sub-manager")
    _, data = owner.post(
        f"/issues/{issue['id']}/channels",
        json={
            "manager_username": manager.username,
            "quota": 10,
            "idempotency_key": uuid.uuid4().hex,
        },
        expected=201,
    )
    manager_channel = data["channel"]
    _, data = manager.post(
        f"/issues/{issue['id']}/channels",
        json={
            "manager_username": sub.username,
            "quota": 4,
            "parent_channel_id": manager_channel["id"],
            "idempotency_key": uuid.uuid4().hex,
        },
        expected=201,
    )
    sub_channel = data["channel"]
    ok(f"manager channel {manager_channel['id']} -> sub channel {sub_channel['id']}")

    step("Sub-manager invites the reader")
    _, data = sub.post(
        f"/issues/{issue['id']}/channels/{sub_channel['id']}/invitations",
        json={"reader_username": reader.username, "ttl_seconds": 3600},
        expected=201,
    )
    invitation = data["invitation"]
    info(f"invitation {invitation['id']} holds one seat; deadline {invitation['expires_at']}")

    step("Reader sees the invitation and claims it")
    _, data = reader.get("/me/issue-invitations", expected=200)
    assert any(i["id"] == invitation["id"] for i in data["invitations"])
    _, data = reader.post(
        f"/issues/invitations/{invitation['id']}/accept", expected=200
    )
    ok(f"reader now holds entitlement {data['entitlement']['id']}")

    step("Reader reads the claimed issue but cannot open the project")
    _, data = reader.get(f"/issues/{issue['id']}/read", expected=200)
    ok(f"read title={data['issue']['title']!r} content={data['issue']['content']!r}")
    reader.get(f"/projects/{project_id}", expected=403)
    reader.get(f"/projects/{project_id}/history", expected=403)
    ok("reader has no project, history or curation access")

    step("Owner keeps editing the workbench; the issue stays frozen")
    owner.patch(
        f"/projects/{project_id}",
        json={"title": "Rewritten", "content": "completely different v2"},
        expected=200,
    )
    _, data = reader.get(f"/issues/{issue['id']}/read", expected=200)
    assert data["issue"]["title"] == "Signed edition"
    assert data["issue"]["content"] == "the final text, v1"
    ok("issue still serves the published revision")

    step("Owner views channel balances; manager views only its own channels")
    _, owner_view = owner.get(f"/issues/{issue['id']}/channels", expected=200)
    print(f"{BOLD}owner view{RESET}: unallocated={owner_view['unallocated_owner_quota']} "
          f"free={owner_view['quota']['free']} "
          f"held={owner_view['quota']['held_by_invitations']} "
          f"claimed={owner_view['quota']['claimed']}")
    _, manager_view = manager.get(f"/issues/{issue['id']}/channels", expected=200)
    ok(f"manager sees {len(manager_view['channels'])} of {len(owner_view['channels'])} channels")

    step("Owner disables the manager branch; free seats return upward")
    _, data = owner.post(
        f"/issues/{issue['id']}/channels/{manager_channel['id']}/disable", expected=200
    )
    ok(f"released {data['released_to_parent']['amount']} seats to parent "
       f"channel {data['released_to_parent']['channel_id']}")

    step("The reader's already-claimed entitlement still works")
    reader.get(f"/issues/{issue['id']}/read", expected=200)
    ok("entitlement survives channel disable")


if __name__ == "__main__":
    main()
