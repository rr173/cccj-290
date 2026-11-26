"""Shared HTTP helpers for runnable creator-workbench demos."""

from __future__ import annotations

import secrets
import sys
import time
from typing import Any, Iterable

import requests


RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
BOLD = "\033[1m"
RESET = "\033[0m"


def step(title: str) -> None:
    print(f"\n{BOLD}{BLUE}== {title} =={RESET}")


def ok(message: str) -> None:
    print(f"{GREEN}✓{RESET} {message}")


def info(message: str) -> None:
    print(f"{YELLOW}•{RESET} {message}")


def show_error(label: str, response: requests.Response, data: Any) -> None:
    code = None
    if isinstance(data, dict):
        code = data.get("error", {}).get("code")
    print(f"{RED}↯{RESET} {label}: HTTP {response.status_code} {code or ''}")
    print(f"  {response.text}")


def assert_status(
    response: requests.Response, expected: int | Iterable[int], context: str
) -> Any:
    allowed = {expected} if isinstance(expected, int) else set(expected)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    if response.status_code not in allowed:
        raise AssertionError(
            f"{context}: expected HTTP {sorted(allowed)}, got "
            f"HTTP {response.status_code}: {response.text}"
        )
    return data


def wait_for_health(base_url: str, timeout_seconds: float = 40.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url.rstrip('/')}/health", timeout=2)
            if response.status_code == 200:
                return
        except requests.RequestException as exc:
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"Service at {base_url} did not become healthy: {last_error}")


class Client:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.account_id: int | None = None
        self.username: str | None = None
        self.http = requests.Session()

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        expected: int | Iterable[int] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> tuple[requests.Response, Any]:
        merged_headers = self.headers()
        if headers:
            merged_headers.update(headers)
        response = self.http.request(
            method,
            f"{self.base_url}{path}",
            json=json,
            headers=merged_headers,
            timeout=timeout,
        )
        try:
            data: Any = response.json()
        except ValueError:
            data = {"raw": response.text}
        if expected is not None:
            assert_status(response, expected, f"{method} {path}")
        return response, data

    def get(self, path: str, **kwargs: Any):
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any):
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any):
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any):
        return self.request("DELETE", path, **kwargs)


def unique_username(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


def register(base_url: str, prefix: str) -> Client:
    username = unique_username(prefix)
    password = "demo-pass-123456"
    client = Client(base_url)
    response, data = client.post(
        "/auth/register",
        json={"username": username, "password": password},
        expected=201,
    )
    client.token = data["session"]["token"]
    client.account_id = data["account"]["id"]
    client.username = data["account"]["username"]
    ok(f"registered {client.username!r} (id={client.account_id}) with a new session")
    return client


def create_invitation(creator: Client, invitee: Client | None = None, ttl: int = 3600):
    body: dict[str, Any] = {"ttl_seconds": ttl}
    if invitee is not None:
        body["invitee_username"] = invitee.username
    response, data = creator.post("/invitations", json=body, expected=201)
    return data["token"], data["invitation"]


def confirm(client: Client, token: str, expected: int | Iterable[int] = 200):
    return client.post(f"/invitations/{token}/confirm", expected=expected)


def link_pair(a: Client, b: Client, ttl: int = 3600, verbose: bool = True):
    """Create one invitation and obtain explicit confirmation from both sides."""
    token, invitation = create_invitation(a, b, ttl=ttl)
    if verbose:
        info(
            f"invitation {invitation['id']} created by {a.username}; "
            f"both accounts must confirm"
        )

    _, first = confirm(a, token, expected=200)
    if verbose:
        ok(f"{a.username} confirmation -> {first['status']}")

    _, second = confirm(b, token, expected=200)
    if verbose:
        edge = second["edge"]
        ok(
            f"{b.username} confirmation -> {second['status']}; "
            f"edge {edge['id']} connects {edge['account_a']}-{edge['account_b']}"
        )
    return second


def active_edge_between(viewer: Client, peer_id: int) -> dict[str, Any] | None:
    _, data = viewer.get("/edges", expected=200)
    for edge in data["edges"]:
        if edge["peer_id"] == peer_id:
            return edge
    return None


def delete_edge_between(a: Client, b: Client) -> dict[str, Any]:
    edge = active_edge_between(a, b.account_id)
    if edge is None:
        raise AssertionError(f"No active edge between {a.username} and {b.username}")
    _, data = a.delete(f"/edges/{edge['id']}", expected=200)
    return data


def default_base_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1].rstrip("/")
    import os

    return os.environ.get("WORKBENCH_URL", "http://127.0.0.1:8000").rstrip("/")
