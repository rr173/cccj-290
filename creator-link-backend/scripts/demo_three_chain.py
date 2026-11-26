#!/usr/bin/env python3
"""Three-account chain and alternate-path component demo.

Topology exercised:

    Alice -- Bob -- Carol

Then after deleting Alice-Bob:

    Alice        Bob -- Carol

Finally create Alice-Carol and show an alternate path:

    Alice -- Carol -- Bob

Deleting Alice-Bob does not split because Alice-Carol-Bob remains; deleting
Alice-Carol does split Alice from Bob/Carol.

Usage:
    python scripts/demo_three_chain.py [http://127.0.0.1:8000]
"""

from __future__ import annotations

from wb_common import (
    BOLD,
    RESET,
    default_base_url,
    delete_edge_between,
    info,
    link_pair,
    ok,
    register,
    step,
    wait_for_health,
)


def main() -> None:
    base_url = default_base_url()
    wait_for_health(base_url)
    step(f"Using service {base_url}")

    alice = register(base_url, "alice3")
    bob = register(base_url, "bob3")
    carol = register(base_url, "carol3")

    step("Create projects at Alice and Carol")
    _, alice_project_data = alice.post(
        "/projects",
        json={"title": "Alice chain project", "content": "Reachable through Bob to Carol."},
        expected=201,
    )
    alice_project = alice_project_data["project"]

    _, carol_project_data = carol.post(
        "/projects",
        json={"title": "Carol chain project", "content": "Reachable through Bob to Alice."},
        expected=201,
    )
    carol_project = carol_project_data["project"]

    step("Build Alice-Bob-Carol chain with separate confirmations")
    link_pair(alice, bob)
    link_pair(bob, carol)
    ok("edges A-B and B-C are active")

    step("Transitive access works along the chain")
    carol.get(f"/projects/{alice_project['id']}", expected=200)
    ok("Carol can read Alice's project through A-B-C")
    alice.get(f"/projects/{carol_project['id']}", expected=200)
    ok("Alice can read Carol's project through C-B-A")

    _, carol_edit = carol.patch(
        f"/projects/{alice_project['id']}",
        json={"content": "Carol edited through the transitive chain."},
        expected=200,
    )
    assert carol_edit["project"]["owner_id"] == alice.account_id
    ok("Carol can write Alice's project; owner remains Alice")

    step("Delete A-B: Alice becomes isolated, Bob-Carol remains connected")
    delete_edge_between(alice, bob)

    bob.get(f"/projects/{alice_project['id']}", expected=403)
    carol.get(f"/projects/{alice_project['id']}", expected=403)
    ok("Bob and Carol immediately lose the known Alice project ID")

    bob.get(f"/projects/{carol_project['id']}", expected=200)
    carol.get(f"/projects/{carol_project['id']}", expected=200)
    alice.get(f"/projects/{alice_project['id']}", expected=200)
    ok("Bob-Carol sharing survives; Alice still owns her project")

    step("Reconnect A-B and add direct A-C, creating an alternate path")
    link_pair(alice, bob)
    link_pair(alice, carol)
    ok("triangle active: A-B, B-C, A-C")

    step("Delete A-B; A-C-B is another path, so access must remain")
    delete_edge_between(alice, bob)
    bob.get(f"/projects/{alice_project['id']}", expected=200)
    carol.get(f"/projects/{alice_project['id']}", expected=200)
    alice.get(f"/projects/{carol_project['id']}", expected=200)
    ok("alternate path A-C-B preserves all sharing")

    step("Delete A-C; now the alternate path is gone and A splits again")
    delete_edge_between(alice, carol)
    bob.get(f"/projects/{alice_project['id']}", expected=403)
    carol.get(f"/projects/{alice_project['id']}", expected=403)
    alice.get(f"/projects/{carol_project['id']}", expected=403)
    ok("removing the last connecting edge immediately denies direct IDs")

    step("History still identifies Carol as the chain actor")
    _, history = alice.get(
        f"/projects/{alice_project['id']}/history", expected=200
    )
    for item in history["history"]:
        info(
            f"r{item['revision']}: actor={item['actor']['username']} "
            f"owner={item['owner']['username']} shared={item['shared_modification']}"
        )
    assert history["history"][-1]["actor"]["id"] == carol.account_id
    assert history["history"][-1]["owner"]["id"] == alice.account_id

    print(f"\n{BOLD}Three-account chain demo passed.{RESET}")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\n\033[31mFAILED:\033[0m {exc}")
        raise SystemExit(1)
