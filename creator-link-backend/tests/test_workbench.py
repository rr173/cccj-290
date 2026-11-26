from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault(
    "WORKBENCH_DB", f"/tmp/workbench-pytest-{uuid.uuid4().hex}.db"
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


def link(client: TestClient, a, b):
    response = client.post(
        "/invitations",
        headers=a.headers,
        json={"invitee_username": b.username, "ttl_seconds": 3600},
    )
    assert response.status_code == 201, response.text
    token = response.json()["token"]

    first = client.post(f"/invitations/{token}/confirm", headers=a.headers)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "pending"

    second = client.post(f"/invitations/{token}/confirm", headers=b.headers)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "consumed"
    return body["edge"]


def test_health_and_session_lifecycle(client):
    assert client.get("/health").json()["status"] == "ok"

    user = register(client, "session")
    me = client.get("/me", headers=user.headers)
    assert me.status_code == 200
    assert me.json()["account"]["username"] == user.username

    assert client.get("/me").status_code == 401
    bad = client.get("/me", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "INVALID_SESSION"


def test_duplicate_username_conflicts(client):
    username = f"dup_{uuid.uuid4().hex[:10]}"
    payload = {"username": username, "password": "test-pass-123456"}
    assert client.post("/auth/register", json=payload).status_code == 201
    duplicate = client.post("/auth/register", json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "USERNAME_TAKEN"


def test_two_party_invitation_ownership_history_and_revocation(client):
    alice = register(client, "alice")
    bob = register(client, "bob")

    created = client.post(
        "/projects",
        headers=alice.headers,
        json={"title": "Alice project", "content": "secret before link"},
    )
    assert created.status_code == 201, created.text
    project = created.json()["project"]
    pid = project["id"]
    assert project["owner_id"] == alice.id

    assert client.get(f"/projects/{pid}", headers=bob.headers).status_code == 403
    assert (
        client.patch(
            f"/projects/{pid}",
            headers=bob.headers,
            json={"content": "no stale write"},
        ).status_code
        == 403
    )

    edge = link(client, alice, bob)

    # Retry Alice's already-recorded pending confirmation semantics are only
    # possible before Bob completes. After completion every replay conflicts.
    invitation_response = client.post(
        "/invitations",
        headers=alice.headers,
        json={"invitee_username": bob.username, "ttl_seconds": 3600},
    )
    second_token = invitation_response.json()["token"]
    assert client.post(
        f"/invitations/{second_token}/confirm", headers=alice.headers
    ).status_code == 200
    duplicate = client.post(
        f"/invitations/{second_token}/confirm", headers=alice.headers
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["duplicate_confirmation"] is True

    consumed = client.post(
        f"/invitations/{second_token}/confirm", headers=bob.headers
    )
    assert consumed.status_code == 200
    replay = client.post(
        f"/invitations/{second_token}/confirm", headers=bob.headers
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "INVITATION_CONSUMED"

    read = client.get(f"/projects/{pid}", headers=bob.headers)
    assert read.status_code == 200
    assert read.json()["project"]["access"] == "shared"

    edited = client.patch(
        f"/projects/{pid}",
        headers=bob.headers,
        json={
            "title": "Bob changed title",
            "content": "Bob changed content",
            "owner_id": bob.id,  # unknown field must be ignored
        },
    )
    assert edited.status_code == 200
    assert edited.json()["project"]["owner_id"] == alice.id

    history = client.get(f"/projects/{pid}/history", headers=alice.headers)
    assert history.status_code == 200
    entries = history.json()["history"]
    assert entries[0]["action"] == "created"
    assert entries[0]["actor"]["id"] == alice.id
    assert entries[-1]["action"] == "updated"
    assert entries[-1]["actor"]["id"] == bob.id
    assert entries[-1]["owner"]["id"] == alice.id
    assert entries[-1]["shared_modification"] is True

    deleted = client.delete(f"/edges/{edge['id']}", headers=alice.headers)
    assert deleted.status_code == 200
    assert deleted.json()["idempotent_replay"] is False

    repeated = client.delete(f"/edges/{edge['id']}", headers=alice.headers)
    assert repeated.status_code == 200
    assert repeated.json()["idempotent_replay"] is True

    assert client.get(f"/projects/{pid}", headers=bob.headers).status_code == 403
    assert (
        client.patch(
            f"/projects/{pid}",
            headers=bob.headers,
            json={"content": "must fail after split"},
        ).status_code
        == 403
    )
    assert (
        client.get(f"/projects/{pid}/history", headers=bob.headers).status_code
        == 403
    )
    assert client.get(f"/projects/{pid}", headers=alice.headers).status_code == 200
    assert client.get("/me", headers=bob.headers).status_code == 200


def test_expired_invitation_is_rejected(client):
    alice = register(client, "expiry-a")
    bob = register(client, "expiry-b")
    created = client.post(
        "/invitations",
        headers=alice.headers,
        json={"invitee_username": bob.username, "ttl_seconds": 1},
    )
    token = created.json()["token"]
    time.sleep(1.1)
    response = client.post(f"/invitations/{token}/confirm", headers=alice.headers)
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "INVITATION_EXPIRED"


def test_chain_and_alternate_path(client):
    alice = register(client, "chain-a")
    bob = register(client, "chain-b")
    carol = register(client, "chain-c")

    pid = client.post(
        "/projects",
        headers=alice.headers,
        json={"title": "chain", "content": "chain"},
    ).json()["project"]["id"]

    ab = link(client, alice, bob)
    bc = link(client, bob, carol)

    assert client.get(f"/projects/{pid}", headers=carol.headers).status_code == 200

    client.delete(f"/edges/{ab['id']}", headers=alice.headers)
    assert client.get(f"/projects/{pid}", headers=carol.headers).status_code == 403
    assert client.get(f"/projects/{pid}", headers=bob.headers).status_code == 403

    # Reconnect A-B and add A-C; deleting A-B must leave A-C-B path.
    ab2 = link(client, alice, bob)
    ac = link(client, alice, carol)
    assert client.delete(f"/edges/{ab2['id']}", headers=alice.headers).status_code == 200
    assert client.get(f"/projects/{pid}", headers=bob.headers).status_code == 200
    assert client.get(f"/projects/{pid}", headers=carol.headers).status_code == 200

    assert client.delete(f"/edges/{ac['id']}", headers=alice.headers).status_code == 200
    assert client.get(f"/projects/{pid}", headers=bob.headers).status_code == 403
    assert client.get(f"/projects/{pid}", headers=carol.headers).status_code == 403
    assert bc["id"] is not None
