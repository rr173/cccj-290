#!/usr/bin/env python3
"""Restart + HTTP concurrency verification.

This script starts a real uvicorn subprocess with a file-backed SQLite DB,
verifies sessions/edges survive process restart, then races Bob's project
writes against deletion of the A-B edge.  Finally it reconstructs the graph
at every revision's recorded graph version and proves that every committed
write was authorized by the graph state at that version.

It exits non-zero if any invariant fails.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from wb_common import (  # noqa: E402
    Client,
    active_edge_between,
    link_pair,
    register,
    wait_for_health,
)


def find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_server(db_path: Path, port: int, log_file: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["WORKBENCH_DB"] = str(db_path)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    log_handle = log_file.open("ab")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        wait_for_health(base_url, timeout_seconds=45)
    except Exception:
        process.terminate()
        try:
            output = log_file.read_text(errors="replace")[-4000:]
            print(output)
        except Exception:
            pass
        raise
    return process


def stop_server(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def make_client(base_url: str, token: str, account_id: int, username: str) -> Client:
    client = Client(base_url, token)
    client.account_id = account_id
    client.username = username
    return client


def verify_concurrent_invitation_and_delete_retries(
    base_url: str, db_path: Path
) -> None:
    print("Checking concurrent invitation confirmation retries...")
    users = [register(base_url, f"race_invite_{i}") for i in range(4)]
    creator = users[0]
    _, invitation_data = creator.post(
        "/invitations", json={"ttl_seconds": 3600}, expected=201
    )
    token = invitation_data["token"]
    invitation_id = invitation_data["invitation"]["id"]

    barrier = threading.Barrier(len(users) * 2)
    outcomes: list[tuple[int, str | None]] = []
    outcome_lock = threading.Lock()

    def confirm_once(user: Client) -> None:
        barrier.wait(timeout=30)
        response = requests.post(
            f"{base_url}/invitations/{token}/confirm",
            headers={"Authorization": f"Bearer {user.token}"},
            timeout=30,
        )
        consumed_edge = None
        try:
            body = response.json()
            consumed_edge = (body.get("edge") or {}).get("id")
        except ValueError:
            pass
        with outcome_lock:
            outcomes.append((response.status_code, consumed_edge))

    threads = []
    for user in users:
        # Two identical retries per account are released simultaneously.
        threads.append(threading.Thread(target=confirm_once, args=(user,)))
        threads.append(threading.Thread(target=confirm_once, args=(user,)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), "invitation confirmation thread timed out"

    statuses = [status for status, _ in outcomes]
    assert all(status in (200, 409) for status in statuses), statuses
    assert any(status == 200 for status in statuses)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        confirmation_count = conn.execute(
            "SELECT COUNT(*) AS n FROM invitation_confirmations WHERE invitation_id = ?",
            (invitation_id,),
        ).fetchone()["n"]
        distinct_count = conn.execute(
            "SELECT COUNT(DISTINCT account_id) AS n FROM invitation_confirmations WHERE invitation_id = ?",
            (invitation_id,),
        ).fetchone()["n"]
        invitation = conn.execute(
            "SELECT * FROM invitations WHERE id = ?", (invitation_id,)
        ).fetchone()
        assert confirmation_count == 2, confirmation_count
        assert distinct_count == 2, distinct_count
        assert invitation["consumed_at"] is not None
        assert invitation["consumed_edge_id"] is not None

        pair_count = conn.execute(
            """
            SELECT COUNT(*) AS n FROM association_edges
             WHERE invitation_id = ? AND deleted_version IS NULL
            """,
            (invitation_id,),
        ).fetchone()["n"]
        assert pair_count == 1, pair_count
    finally:
        conn.close()
    print("  exactly two distinct confirmations and one edge survived concurrent retries")

    print("Checking concurrent DELETE retries for one edge...")
    alice = register(base_url, "race_delete_a")
    bob = register(base_url, "race_delete_b")
    edge = link_pair(alice, bob, verbose=False)["edge"]
    delete_barrier = threading.Barrier(6)
    delete_results: list[bool] = []
    delete_statuses: list[int] = []
    delete_lock = threading.Lock()

    def delete_once() -> None:
        delete_barrier.wait(timeout=30)
        response = requests.delete(
            f"{base_url}/edges/{edge['id']}",
            headers={"Authorization": f"Bearer {alice.token}"},
            timeout=30,
        )
        with delete_lock:
            delete_statuses.append(response.status_code)
            if response.status_code == 200:
                delete_results.append(bool(response.json().get("idempotent_replay")))

    delete_threads = [threading.Thread(target=delete_once) for _ in range(6)]
    for thread in delete_threads:
        thread.start()
    for thread in delete_threads:
        thread.join(timeout=60)

    assert delete_statuses == [200] * 6, delete_statuses
    assert delete_results.count(False) == 1, delete_results
    assert delete_results.count(True) == 5, delete_results

    conn = sqlite3.connect(db_path)
    try:
        unlinked_events = conn.execute(
            "SELECT COUNT(*) AS n FROM graph_events WHERE edge_id = ? AND event_type = 'unlinked'",
            (edge["id"],),
        ).fetchone()[0]
        assert unlinked_events == 1, unlinked_events
    finally:
        conn.close()
    print("  one real deletion; five concurrent retries were idempotent replays")


def run_concurrency_cycles(
    alice: Client, bob: Client, project_id: int, cycles: int = 10
) -> dict[str, int]:
    total_200 = 0
    total_403 = 0
    total_unexpected = 0
    unexpected_samples: list[str] = []

    for cycle in range(cycles):
        if active_edge_between(alice, bob.account_id) is None:
            link_pair(alice, bob, verbose=False)
        edge = active_edge_between(alice, bob.account_id)
        assert edge is not None

        writer_count = 8
        writes_per_writer = 5
        barrier = threading.Barrier(writer_count + 1)
        statuses: list[int] = []
        status_lock = threading.Lock()

        def writer(index: int) -> None:
            barrier.wait(timeout=30)
            for write_index in range(writes_per_writer):
                marker = f"cycle-{cycle}-w{index}-{write_index}-{uuid.uuid4().hex}"
                try:
                    response = requests.patch(
                        f"{alice.base_url}/projects/{project_id}",
                        headers={"Authorization": f"Bearer {bob.token}"},
                        json={"content": marker},
                        timeout=30,
                    )
                    with status_lock:
                        statuses.append(response.status_code)
                except requests.RequestException:
                    with status_lock:
                        statuses.append(-1)

        def unlinker() -> None:
            barrier.wait(timeout=30)
            # Let a first wave enter the serialized write transaction; the
            # deletion then races the remaining waves.
            time.sleep(0.03)
            response = requests.delete(
                f"{alice.base_url}/edges/{edge['id']}",
                headers={"Authorization": f"Bearer {alice.token}"},
                timeout=30,
            )
            assert response.status_code == 200, response.text

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(writer_count)]
        unlink_thread = threading.Thread(target=unlinker)
        unlink_thread.start()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        unlink_thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads), "writer thread timed out"

        total_200 += statuses.count(200)
        total_403 += statuses.count(403)
        expected = writer_count * writes_per_writer
        if len(statuses) != expected:
            total_unexpected += expected - len(statuses)
        for status in statuses:
            if status not in (200, 403):
                total_unexpected += 1
                if len(unexpected_samples) < 5:
                    unexpected_samples.append(f"cycle={cycle} status={status}")

        # The DELETE response is sent only after its transaction commits.
        # Therefore every subsequent request must observe revoked access.
        bob.get(f"/projects/{project_id}", expected=403)
        bob.patch(
            f"/projects/{project_id}",
            json={"content": f"post-split-{uuid.uuid4().hex}"},
            expected=403,
        )

        if cycle < cycles - 1:
            link_pair(alice, bob, verbose=False)

    if total_unexpected:
        raise AssertionError(
            f"unexpected concurrency responses: {total_unexpected}; "
            f"samples={unexpected_samples}"
        )
    if total_200 == 0:
        raise AssertionError("concurrency test produced no successful shared writes")
    if total_403 == 0:
        raise AssertionError("concurrency test produced no denied racing writes")

    return {"successful_shared_writes": total_200, "denied_racing_writes": total_403}


def reachable_at_version(conn: sqlite3.Connection, start: int, version: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT account_a, account_b, created_version, deleted_version
          FROM association_edges
        """
    ).fetchall()
    adjacency: dict[int, set[int]] = {}
    for row in rows:
        active = row["created_version"] <= version and (
            row["deleted_version"] is None or row["deleted_version"] > version
        )
        if not active:
            continue
        a, b = row["account_a"], row["account_b"]
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop()
        for neighbor in adjacency.get(current, ()):  # type: ignore[arg-type]
            if neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def verify_database(db_path: Path, alice_id: int, bob_id: int, project_id: int) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        project = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        assert project is not None
        assert project["owner_id"] == alice_id, "project owner changed!"

        revisions = conn.execute(
            """
            SELECT r.*, p.owner_id
              FROM project_revisions r
              JOIN projects p ON p.id = r.project_id
             WHERE r.project_id = ?
             ORDER BY r.revision
            """,
            (project_id,),
        ).fetchall()
        assert revisions, "project has no history"

        shared_updates = 0
        for revision in revisions:
            reachable = reachable_at_version(
                conn, revision["actor_id"], revision["graph_version"]
            )
            assert revision["owner_id"] in reachable, (
                f"revision {revision['revision']} actor {revision['actor_id']} "
                f"could not reach owner {revision['owner_id']} at graph version "
                f"{revision['graph_version']}"
            )
            if revision["actor_id"] != revision["owner_id"]:
                shared_updates += 1

        active_edges = conn.execute(
            "SELECT COUNT(*) AS n FROM association_edges WHERE deleted_version IS NULL"
        ).fetchone()["n"]
        canonical_a, canonical_b = sorted((alice_id, bob_id))
        ab_active = conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM association_edges
             WHERE deleted_version IS NULL
               AND account_a = ?
               AND account_b = ?
            """,
            (canonical_a, canonical_b),
        ).fetchone()["n"]
        assert ab_active == 0, "final A-B edge should be deleted"

        duplicate_active = conn.execute(
            """
            SELECT account_a, account_b, COUNT(*) AS n
              FROM association_edges
             WHERE deleted_version IS NULL
             GROUP BY account_a, account_b
            HAVING n > 1
            """
        ).fetchall()
        assert not duplicate_active, "duplicate active undirected edges exist"

        confirmations = conn.execute(
            """
            SELECT invitation_id, COUNT(*) AS n
              FROM invitation_confirmations
             GROUP BY invitation_id
            HAVING n > 2
            """
        ).fetchall()
        assert not confirmations, "invitation consumed more than two confirmations"

        state_version = conn.execute(
            "SELECT version FROM graph_state WHERE id = 1"
        ).fetchone()["version"]
        event_versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM graph_events ORDER BY version"
            ).fetchall()
        ]
        assert state_version == (event_versions[-1] if event_versions else 0)
        assert event_versions == list(range(1, len(event_versions) + 1)), (
            "graph versions are not contiguous; a multi-statement graph change "
            "may have been only partially committed"
        )
        for event in conn.execute("SELECT * FROM graph_events").fetchall():
            edge = conn.execute(
                "SELECT * FROM association_edges WHERE id = ?",
                (event["edge_id"],),
            ).fetchone()
            assert edge is not None
            if event["event_type"] == "linked":
                assert edge["created_version"] == event["version"]
            else:
                assert edge["deleted_version"] == event["version"]

        return {
            "revisions_checked": len(revisions),
            "shared_revisions_checked": shared_updates,
            "final_active_edges": active_edges,
            "graph_events_checked": len(event_versions),
        }
    finally:
        conn.close()


def main() -> None:
    temp_dir = Path(tempfile.mkdtemp(prefix="workbench-verify-"))
    db_path = temp_dir / "verify.db"
    log_file = temp_dir / "uvicorn.log"
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    print(f"Starting uvicorn on {base_url}")
    print(f"Database: {db_path}")

    process: subprocess.Popen | None = None
    try:
        process = start_server(db_path, port, log_file)

        verify_concurrent_invitation_and_delete_retries(base_url, db_path)

        alice = Client(base_url)
        bob = Client(base_url)
        _, data = alice.post(
            "/auth/register",
            json={"username": f"verify_alice_{uuid.uuid4().hex[:8]}", "password": "verify-pass-123"},
            expected=201,
        )
        alice.token = data["session"]["token"]
        alice.account_id = data["account"]["id"]
        alice.username = data["account"]["username"]

        _, data = bob.post(
            "/auth/register",
            json={"username": f"verify_bob_{uuid.uuid4().hex[:8]}", "password": "verify-pass-123"},
            expected=201,
        )
        bob.token = data["session"]["token"]
        bob.account_id = data["account"]["id"]
        bob.username = data["account"]["username"]

        _, project_data = alice.post(
            "/projects",
            json={"title": "Restart/concurrency project", "content": "initial"},
            expected=201,
        )
        project_id = project_data["project"]["id"]

        link_pair(alice, bob, verbose=False)
        bob.patch(
            f"/projects/{project_id}",
            json={"content": "committed before restart"},
            expected=200,
        )
        alice_token = alice.token
        bob_token = bob.token

        print("Restart #1: stopping uvicorn and starting it against the same DB")
        stop_server(process)
        process = start_server(db_path, port, log_file)
        alice = make_client(base_url, alice_token, alice.account_id, alice.username)
        bob = make_client(base_url, bob_token, bob.account_id, bob.username)
        alice.get("/me", expected=200)
        bob.get("/me", expected=200)
        _, history = alice.get(f"/projects/{project_id}/history", expected=200)
        assert len(history["history"]) == 2
        bob.get(f"/projects/{project_id}", expected=200)
        print("  restart preserved sessions, edge, project, and actor history")

        print("Running write/unlink concurrency cycles...")
        stats = run_concurrency_cycles(alice, bob, project_id, cycles=10)
        print(f"  {stats}")

        print("Restart #2 after final split...")
        stop_server(process)
        process = start_server(db_path, port, log_file)
        alice = make_client(base_url, alice_token, alice.account_id, alice.username)
        bob = make_client(base_url, bob_token, bob.account_id, bob.username)
        alice.get(f"/projects/{project_id}", expected=200)
        bob.get(f"/projects/{project_id}", expected=403)
        bob.patch(
            f"/projects/{project_id}",
            json={"content": "denied after restart"},
            expected=403,
        )
        print("  denial persisted across restart; owner access remained")

        stop_server(process)
        process = None
        invariant = verify_database(db_path, alice.account_id, bob.account_id, project_id)
        print(f"Database graph/history invariant passed: {invariant}")
        print("\nPASS: restart, ownership, actor history, concurrency, and revocation checks")
    finally:
        stop_server(process)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
