"""End to end: the real connector against the real target app (run in a background thread),
with Okta mocked. Covers the "done when" cases: create, deactivate, forced 429, forced timeout."""
import socket
import threading
import time

import pytest
import requests
import responses
import uvicorn
from responses import matchers

from connector.http import HttpClient
from connector.okta_client import OktaClient
from connector.sync import sync
from connector.target_client import TargetClient
from target_app.main import app, reset_state

ORG = "https://dev-1.okta.com"
USERS = f"{ORG}/api/v1/users"


def okta_user(uid, login, status="ACTIVE", email=None):
    return {"id": uid, "status": status,
            "profile": {"login": login, "email": email or login, "firstName": "F", "lastName": "L"}}


def mock_okta(active=(), deprovisioned=()):
    """(Re)register the two Okta list calls the connector makes."""
    responses.reset()
    responses.add_passthru("http://127.0.0.1")  # let calls to the local target app through
    responses.add(
        responses.GET, USERS, json=list(active),
        match=[matchers.query_param_matcher({"limit": "200"})],
    )
    responses.add(
        responses.GET, USERS, json=list(deprovisioned),
        match=[matchers.query_param_matcher({"search": 'status eq "DEPROVISIONED"', "limit": "200"})],
    )


@pytest.fixture(scope="module")
def target_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "target app did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    reset_state()
    for name in ("CHAOS_429_EVERY", "CHAOS_SLOW_EVERY", "CHAOS_SLOW_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def build(target_url, **http_kwargs):
    sleeps = []
    http = HttpClient(requests.Session(), sleep=sleeps.append, **http_kwargs)
    return OktaClient(ORG, "tok", http), TargetClient(target_url, http), sleeps


def target_state(target_url):
    return {u["okta_id"]: u for u in requests.get(f"{target_url}/users").json()}


@responses.activate
def test_user_lifecycle_create_update_deactivate(target_url):
    okta, target, _ = build(target_url)

    # 1. Two users appear in Okta -> both created in the target.
    mock_okta(active=[okta_user("u1", "alice@x.com"), okta_user("u2", "bob@x.com")])
    assert sync(okta, target).created == 2
    state = target_state(target_url)
    assert set(state) == {"u1", "u2"} and all(u["active"] for u in state.values())

    # 2. Nothing changed -> nothing written.
    assert sync(okta, target).unchanged == 2

    # 3. Alice's email changes, Bob is deactivated in Okta, Carol is new.
    mock_okta(
        active=[okta_user("u1", "alice@x.com", email="alice@new.com"), okta_user("u3", "carol@x.com")],
        deprovisioned=[okta_user("u2", "bob@x.com", status="DEPROVISIONED")],
    )
    result = sync(okta, target)
    assert (result.created, result.updated, result.deactivated) == (1, 1, 1)
    state = target_state(target_url)
    assert state["u1"]["email"] == "alice@new.com"
    assert state["u2"]["active"] is False  # disabled, not deleted
    assert state["u3"]["active"] is True

    # 4. Carol disappears from Okta entirely -> disabled in the target.
    mock_okta(active=[okta_user("u1", "alice@x.com", email="alice@new.com")],
              deprovisioned=[okta_user("u2", "bob@x.com", status="DEPROVISIONED")])
    assert sync(okta, target).deactivated == 1
    assert target_state(target_url)["u3"]["active"] is False


@responses.activate
def test_forced_429s_from_the_target_are_retried(target_url, monkeypatch):
    monkeypatch.setenv("CHAOS_429_EVERY", "2")  # every 2nd request gets 429 + Retry-After: 1
    okta, target, sleeps = build(target_url)
    mock_okta(active=[okta_user(f"u{i}", f"user{i}@x.com") for i in range(5)])

    result = sync(okta, target)

    monkeypatch.delenv("CHAOS_429_EVERY")  # so the checks below are not themselves rate limited
    assert result.created == 5 and result.errors == []
    assert len(target_state(target_url)) == 5
    assert sleeps and all(s == 1.0 for s in sleeps)  # waited as Retry-After instructed


@responses.activate
def test_forced_timeouts_are_retried_without_duplicates(target_url, monkeypatch):
    monkeypatch.setenv("CHAOS_SLOW_EVERY", "2")
    monkeypatch.setenv("CHAOS_SLOW_SECONDS", "1")
    # Read timeout (0.3s) is shorter than the forced 1s delay, so slow requests time out.
    okta, target, sleeps = build(target_url, timeout=(1.0, 0.3))
    mock_okta(active=[okta_user(f"u{i}", f"user{i}@x.com") for i in range(3)])

    result = sync(okta, target)

    monkeypatch.delenv("CHAOS_SLOW_EVERY")
    assert result.errors == []
    assert sleeps, "expected at least one timeout retry"
    time.sleep(1.2)  # let any abandoned slow request finish on the server
    state = target_state(target_url)
    assert len(state) == 3  # PUT is idempotent, so a retried request made no duplicates


@responses.activate
def test_okta_rate_limit_exhaustion_leaves_target_untouched(target_url):
    okta, target, _ = build(target_url, max_retries=2)
    mock_okta(active=[okta_user("u1", "alice@x.com")])
    sync(okta, target)  # seed one active user

    responses.reset()
    responses.add_passthru("http://127.0.0.1")
    responses.add(responses.GET, USERS, status=429, headers={"Retry-After": "1"})
    with pytest.raises(Exception):
        sync(okta, target)

    assert target_state(target_url)["u1"]["active"] is True  # not wrongly deactivated
