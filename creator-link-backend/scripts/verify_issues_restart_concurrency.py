#!/usr/bin/env python3
"""Restart + concurrency verification for limited-edition issues.

Starts a real uvicorn subprocess against a file-backed SQLite DB, then:

1. publishes an issue snapshot and continues editing the project, asserting the
   issue text does not move;
2. races concurrent allocation of the same quota and asserts never-oversold;
3. races invitation sends for the same reader across channels and asserts a
   single live invitation / single held seat;
4. races concurrent accepts and asserts exactly one entitlement;
5. restarts the process and proves quotas, invitations, and entitlements
   survive while an expired invitation's seat is reclaimed without cleanup;
6. offline-checks the core invariant:
      total_quota == free + held + claimed
   on every issue.

Exits non-zero on any violation.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from wb_common import Client, register  # noqa: E402
from verify_restart_concurrency import (  # noqa: E402
    find_free_port,
    make_client,
    start_server,
    stop_server,
)


def publish(client: Client, project_id: int, *, revision=1, quota=10) -> dict:
    _, data = client.post(
        f"/projects/{project_id}/issues",
        json={
            "source_revision": revision,
            "total_quota": quota,
            "idempotency_key": uuid.uuid4().hex,
        },
        expected=201,
    )
    return data["issue"]


def allocate(client: Client, issue_id: int, manager: Client, quota: int,
             parent: int | None = None) -> dict:
    body = {"manager_username": manager.username, "quota": quota,
            "idempotency_key": uuid.uuid4().hex}
    if parent is not None:
        body["parent_channel_id"] = parent
    _, data = client.post(f"/issues/{issue_id}/channels", json=body, expected=201)
    return data["channel"]


def invite(client: Client, issue_id: int, reader: Client, *, channel_id=None,
           ttl=3600) -> dict:
    body = {"reader_username": reader.username, "ttl_seconds": ttl}
    if channel_id is None:
        path = f"/issues/{issue_id}/invitations"
    else:
        path = f"/issues/{issue_id}/channels/{channel_id}/invitations"
    _, data = client.post(path, json=body, expected=201)
    return data["invitation"]


def channels_view(client: Client, issue_id: int) -> dict:
    _, data = client.get(f"/issues/{issue_id}/channels", expected=200)
    return data


def test_snapshot_survives_editing(base_url, owner, project_id, reader):
    print("Checking immutable issue snapshot...")
    issue = publish(owner, project_id, revision=1, quota=5)
    owner.patch(
        f"/projects/{project_id}",
        json={"title": "changed after publish", "content": "changed after publish"},
        expected=200,
    )
    inv = invite(owner, issue["id"], reader)
    reader.post(f"/issues/invitations/{inv['id']}/accept", expected=200)
    _, read = reader.get(f"/issues/{issue['id']}/read", expected=200)
    assert read["issue"]["title"] == issue["title"]
    assert read["issue"]["content"] == issue["content"]
    print("  issue text stayed at the published revision")


def test_concurrent_allocate_never_oversells(base_url, owner, project_id):
    print("Checking concurrent allocation of the same quota...")
    issue = publish(owner, project_id, revision=1, quota=5)
    managers = [register(base_url, f"alloc_m{i}") for i in range(8)]
    barrier = threading.Barrier(len(managers))
    results: list[int] = []
    lock = threading.Lock()

    def allocate_once(manager: Client) -> None:
        barrier.wait(timeout=30)
        response = requests.post(
            f"{base_url}/issues/{issue['id']}/channels",
            headers={"Authorization": f"Bearer {owner.token}"},
            json={
                "manager_username": manager.username,
                "quota": 1,
                "idempotency_key": uuid.uuid4().hex,
            },
            timeout=30,
        )
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=allocate_once, args=(m,)) for m in managers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()

    assert results.count(201) == 5, results
    assert results.count(409) == 3, results
    view = channels_view(owner, issue["id"])
    assert view["unallocated_owner_quota"] == 0, view
    assert view["quota"]["free"] == 5, view
    print("  exactly five allocations succeeded; the rest were rejected")


def test_concurrent_invite_same_reader_one_seat(base_url, owner, project_id):
    print("Checking concurrent invitations for one reader across channels...")
    issue = publish(owner, project_id, revision=1, quota=6)
    managers = [register(base_url, f"inh_m{i}") for i in range(3)]
    channels = [allocate(owner, issue["id"], m, 2) for m in managers]
    reader = register(base_url, "inh_reader")
    barrier = threading.Barrier(len(channels))
    outcomes: list[tuple[int, int | None]] = []
    lock = threading.Lock()

    def invite_once(manager: Client, channel: dict) -> None:
        barrier.wait(timeout=30)
        response = requests.post(
            f"{base_url}/issues/{issue['id']}/channels/{channel['id']}/invitations",
            headers={"Authorization": f"Bearer {manager.token}"},
            json={"reader_username": reader.username, "ttl_seconds": 3600},
            timeout=30,
        )
        inv_id = None
        if response.status_code == 201:
            inv_id = response.json()["invitation"]["id"]
        with lock:
            outcomes.append((response.status_code, inv_id))

    threads = [
        threading.Thread(target=invite_once, args=(m, c))
        for m, c in zip(managers, channels)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert all(status == 201 for status, _ in outcomes), outcomes
    ids = {inv_id for _, inv_id in outcomes}
    assert len(ids) == 1, ids
    view = channels_view(owner, issue["id"])
    assert view["quota"]["held_by_invitations"] == 1, view
    print("  one live invitation for the reader; a single seat held")


def test_concurrent_accept_one_entitlement(base_url, owner, project_id):
    print("Checking concurrent accepts of one invitation...")
    issue = publish(owner, project_id, revision=1, quota=2)
    reader = register(base_url, "accept_reader")
    inv = invite(owner, issue["id"], reader)
    barrier = threading.Barrier(8)
    statuses: list[int] = []
    entitlement_ids: set[int] = set()
    lock = threading.Lock()

    def accept_once() -> None:
        barrier.wait(timeout=30)
        response = requests.post(
            f"{base_url}/issues/invitations/{inv['id']}/accept",
            headers={"Authorization": f"Bearer {reader.token}"},
            timeout=30,
        )
        with lock:
            statuses.append(response.status_code)
            if response.status_code == 200:
                entitlement_ids.add(response.json()["entitlement"]["id"])

    threads = [threading.Thread(target=accept_once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert statuses == [200] * 8, statuses
    assert len(entitlement_ids) == 1, entitlement_ids
    view = channels_view(owner, issue["id"])
    assert view["quota"]["claimed"] == 1, view
    print("  eight simultaneous accepts converged on one entitlement")


def verify_database(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        issues = conn.execute("SELECT * FROM issues").fetchall()
        checked = 0
        for issue in issues:
            free = conn.execute(
                "SELECT COALESCE(SUM(available), 0) AS n FROM issue_channels WHERE issue_id = ?",
                (issue["id"],),
            ).fetchone()["n"]
            held = conn.execute(
                "SELECT COUNT(*) AS n FROM issue_invitations WHERE issue_id = ? AND status = 'pending'",
                (issue["id"],),
            ).fetchone()["n"]
            claimed = conn.execute(
                "SELECT COUNT(*) AS n FROM issue_entitlements WHERE issue_id = ?",
                (issue["id"],),
            ).fetchone()["n"]
            assert free + held + claimed == issue["total_quota"], (
                issue["id"], free, held, claimed, issue["total_quota"]
            )
            dist = conn.execute(
                """
                SELECT COALESCE(SUM(granted), 0) AS g FROM issue_channels
                 WHERE issue_id = ? AND parent_id IS NOT NULL
                """,
                (issue["id"],),
            ).fetchone()["g"]
            root_granted = conn.execute(
                "SELECT granted FROM issue_channels WHERE issue_id = ? AND parent_id IS NULL",
                (issue["id"],),
            ).fetchone()["granted"]
            # Total granted into the tree must never exceed the total quota.
            assert root_granted <= issue["total_quota"]
            assert dist <= issue["total_quota"]
            checked += 1

        dup_live = conn.execute(
            """
            SELECT issue_id, reader_id, COUNT(*) AS n
              FROM issue_invitations WHERE status = 'pending'
             GROUP BY issue_id, reader_id HAVING n > 1
            """
        ).fetchall()
        assert not dup_live, "duplicate live invitations exist"
        dup_ent = conn.execute(
            """
            SELECT issue_id, reader_id, COUNT(*) AS n
              FROM issue_entitlements GROUP BY issue_id, reader_id HAVING n > 1
            """
        ).fetchall()
        assert not dup_ent, "duplicate entitlements exist"
        negative = conn.execute(
            "SELECT COUNT(*) AS n FROM issue_channels WHERE available < 0"
        ).fetchone()["n"]
        assert negative == 0
        return {
            "issues_checked": checked,
            "pending_invitations": conn.execute(
                "SELECT COUNT(*) AS n FROM issue_invitations WHERE status = 'pending'"
            ).fetchone()["n"],
            "entitlements": conn.execute(
                "SELECT COUNT(*) AS n FROM issue_entitlements"
            ).fetchone()["n"],
        }
    finally:
        conn.close()


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="workbench-issues-verify-"))
    db_path = temp_dir / "verify.db"
    log_file = temp_dir / "uvicorn.log"
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    print(f"Starting uvicorn on {base_url}")
    print(f"Database: {db_path}")

    process = start_server(db_path, port, log_file)
    try:
        owner = register(base_url, "iss_owner")
        reader = register(base_url, "iss_reader")
        _, proj = owner.post(
            "/projects",
            json={"title": "Edition A", "content": "first body"},
            expected=201,
        )
        project_id = proj["project"]["id"]

        test_snapshot_survives_editing(base_url, owner, project_id, register(base_url, "iss_r0"))
        test_concurrent_allocate_never_oversells(base_url, owner, project_id)
        test_concurrent_invite_same_reader_one_seat(base_url, owner, project_id)
        test_concurrent_accept_one_entitlement(base_url, owner, project_id)

        # One issue that will be restarted around.  Two long-lived seats prove
        # restart persistence; a third short-lived seat proves lazy expiry.
        issue = publish(owner, project_id, revision=1, quota=3)
        first_reader = register(base_url, "iss_first")
        second_reader = register(base_url, "iss_second")
        invite(owner, issue["id"], first_reader, ttl=3600)
        invite(owner, issue["id"], second_reader, ttl=3600)

        pre = channels_view(owner, issue["id"])["quota"]
        assert pre["held_by_invitations"] == 2, pre
        assert pre["free"] == 1, pre

        print("Restart #1: same port, same DB")
        stop_server(process)
        process = start_server(db_path, port, log_file)
        owner = make_client(base_url, owner.token, owner.account_id, owner.username)
        post = channels_view(owner, issue["id"])["quota"]
        assert post == pre, (pre, post)
        print("  sessions and held quota survived restart")

        # Expiry after restart: issue a short seat and let it lapse.
        short_reader = register(base_url, "iss_short")
        invite(owner, issue["id"], short_reader, ttl=1)
        assert channels_view(owner, issue["id"])["quota"]["held_by_invitations"] == 3
        time.sleep(1.2)
        # Expiry is settled lazily; reading the quota reclaims the short seat.
        after = channels_view(owner, issue["id"])["quota"]
        assert after["held_by_invitations"] == 2, after
        assert after["free"] == 1, after
        print("  expired seat reclaimed without any manual cleanup")

        print("Restart #2 after expiry...")
        stop_server(process)
        process = start_server(db_path, port, log_file)
        owner = make_client(base_url, owner.token, owner.account_id, owner.username)
        again = channels_view(owner, issue["id"])["quota"]
        assert again == after, (after, again)
        print("  reclaimed quota persisted across a second restart")

        stop_server(process)
        process = None
        stats = verify_database(db_path)
        print(f"Database quota invariant passed: {stats}")
        print("\nPASS: issue snapshot, quota, allocation, invite, accept, restart, expiry")
    finally:
        stop_server(process)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
