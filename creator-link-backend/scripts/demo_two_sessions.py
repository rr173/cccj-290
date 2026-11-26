#!/usr/bin/env python3
"""Two-account/two-session end-to-end demo.

Demonstrates:
* independent local bearer sessions;
* project sharing after two explicit invitation confirmations;
* idempotent duplicate pending confirmation and consumed-token replay rejection;
* immutable owner and actor history;
* immediate direct-ID read/write denial after deleting the A-B edge.

Usage:
    python scripts/demo_two_sessions.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

import time

from wb_common import (
    BOLD,
    RESET,
    Client,
    confirm,
    create_invitation,
    default_base_url,
    delete_edge_between,
    info,
    ok,
    register,
    show_error,
    step,
    wait_for_health,
)


def expect_denied(client: Client, method: str, path: str, context: str, **kwargs):
    response, data = client.request(method, path, expected=403, **kwargs)
    show_error(context, response, data)
    return data


def main() -> None:
    base_url = default_base_url()
    wait_for_health(base_url)
    step(f"Using service {base_url}")

    alice = register(base_url, "alice")
    bob = register(base_url, "bob")

    step("Alice creates her original project")
    _, data = alice.post(
        "/projects",
        json={
            "title": "Alice launch plan",
            "content": "Owner remains Alice, even when Bob edits through sharing.",
        },
        expected=201,
    )
    alice_project = data["project"]
    project_id = alice_project["id"]
    ok(f"project {project_id} owner_id={alice_project['owner_id']}")

    step("Before linking, Bob knows the ID but has no access")
    expect_denied(bob, "GET", f"/projects/{project_id}", "Bob reads Alice's project")
    expect_denied(
        bob,
        "PATCH",
        f"/projects/{project_id}",
        "Bob tries to write Alice's project",
        json={"content": "stale qualification must never commit"},
    )

    step("One expiring invitation, confirmed separately by both sessions")
    token, invitation = create_invitation(alice, bob)
    info(f"raw invitation token is shown once: {token[:12]}...")

    _, first = confirm(alice, token, expected=200)
    assert first["status"] == "pending"
    assert first["invitation"]["confirmation_count"] == 1
    ok("Alice explicitly confirmed; invitation is still pending Bob")

    _, duplicate_pending = confirm(alice, token, expected=200)
    assert duplicate_pending["status"] == "pending"
    assert duplicate_pending["duplicate_confirmation"] is True
    assert duplicate_pending["invitation"]["confirmation_count"] == 1
    ok("Retrying Alice's pending confirmation did not insert a second confirmation")

    _, completed = confirm(bob, token, expected=200)
    assert completed["status"] == "consumed"
    edge_id = completed["edge"]["id"]
    ok(f"Bob explicitly confirmed; exactly one undirected edge {edge_id} was created")

    step("Consumed invitation cannot be replayed")
    response, replay = confirm(bob, token, expected=409)
    assert replay["error"]["code"] == "INVITATION_CONSUMED"
    assert replay["error"]["details"]["consumed_edge_id"] == edge_id
    show_error("Bob replays consumed confirmation", response, replay)

    step("Sharing works from either linked account")
    _, listed = bob.get("/projects", expected=200)
    assert any(p["id"] == project_id for p in listed["projects"])
    ok("Alice's project appears in Bob's accessible project list")

    _, read = bob.get(f"/projects/{project_id}", expected=200)
    assert read["project"]["access"] == "shared"

    _, edited = bob.patch(
        f"/projects/{project_id}",
        json={
            "title": "Alice launch plan (Bob pass)",
            "content": "Bob edited through the A-B share; owner must stay Alice.",
        },
        expected=200,
    )
    assert edited["project"]["owner_id"] == alice.account_id
    ok("Bob modified Alice's project; response still has owner_id=Alice")

    step("Bob creates a project during the association")
    _, bob_created = bob.post(
        "/projects",
        json={"title": "Bob shared-era draft", "content": "Created by Bob."},
        expected=201,
    )
    bob_project = bob_created["project"]
    bob_project_id = bob_project["id"]
    assert bob_project["owner_id"] == bob.account_id

    _, alice_edit = alice.patch(
        f"/projects/{bob_project_id}",
        json={"content": "Alice borrowed access to improve Bob's project."},
        expected=200,
    )
    assert alice_edit["project"]["owner_id"] == bob.account_id
    ok("Alice edited Bob's project; creation ownership remains Bob")

    step("Attempting to inject owner_id cannot rewrite ownership")
    _, injected = alice.patch(
        f"/projects/{bob_project_id}",
        json={"content": "Extra owner_id field is ignored.", "owner_id": alice.account_id},
        expected=200,
    )
    assert injected["project"]["owner_id"] == bob.account_id
    ok("unknown owner_id field was ignored")

    step("History records the real session actor and project owner")
    _, history_data = alice.get(f"/projects/{bob_project_id}/history", expected=200)
    for item in history_data["history"]:
        print(
            f"  r{item['revision']} {item['action']}: "
            f"actor={item['actor']['username']} owner={item['owner']['username']} "
            f"shared={item['shared_modification']} graph_v{item['graph_version']}"
        )
    actors = [(h["action"], h["actor"]["id"], h["owner"]["id"]) for h in history_data["history"]]
    assert ("created", bob.account_id, bob.account_id) in actors
    assert ("updated", alice.account_id, bob.account_id) in actors

    step("Delete A-B edge; known IDs must immediately deny direct read/write")
    deletion = delete_edge_between(alice, bob)
    assert deletion["deleted"] is True
    edge_id = deletion["edge"]["id"]
    ok(f"edge {edge_id} deleted")

    # Retrying DELETE is an idempotent deletion marker and creates no new
    # graph event/version.
    _, repeated_delete = alice.delete(f"/edges/{edge_id}", expected=200)
    assert repeated_delete["idempotent_replay"] is True
    ok("repeated edge DELETE returned idempotent_replay=true without a second graph change")

    expect_denied(
        bob,
        "GET",
        f"/projects/{project_id}",
        "Bob reads Alice's old project ID immediately after split",
    )
    expect_denied(
        bob,
        "PATCH",
        f"/projects/{project_id}",
        "Bob writes Alice's project ID immediately after split",
        json={"content": "must not commit after revocation"},
    )
    expect_denied(
        alice,
        "GET",
        f"/projects/{bob_project_id}",
        "Alice reads Bob's old project ID in the other direction",
    )

    _, alice_still = alice.get(f"/projects/{project_id}", expected=200)
    _, bob_still = bob.get(f"/projects/{bob_project_id}", expected=200)
    assert alice_still["project"]["owner_id"] == alice.account_id
    assert bob_still["project"]["owner_id"] == bob.account_id
    ok("each owner still has own project; ownership was never moved")

    history_response, history_error = bob.get(
        f"/projects/{project_id}/history", expected=403
    )
    show_error(
        "Bob reads Alice's history after split",
        history_response,
        history_error,
    )

    step("Sessions survive association deletion")
    alice.get("/me", expected=200)
    bob.get("/me", expected=200)
    ok("both original session tokens still authenticate")

    step("Expired invitation cannot be confirmed/replayed")
    expired_token, expired_invitation = create_invitation(alice, bob, ttl=1)
    info(f"invitation {expired_invitation['id']} expires in 1 second")
    time.sleep(1.15)
    response, expired_error = confirm(alice, expired_token, expected=410)
    assert expired_error["error"]["code"] == "INVITATION_EXPIRED"
    show_error("Alice confirms expired invitation", response, expired_error)

    print(f"\n{BOLD}Two-session demo passed.{RESET}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\n\033[31mFAILED:\033[0m {exc}")
        raise SystemExit(1)
