from fastapi.testclient import TestClient

from target_app.main import app, reset_state

client = TestClient(app)
BODY = {"okta_id": "u1", "login": "a@x.com", "email": "a@x.com",
        "first_name": "A", "last_name": "B", "active": True}


def setup_function():
    reset_state()


def test_put_creates_then_updates():
    assert client.put("/users/u1", json=BODY).status_code == 201
    assert client.put("/users/u1", json={**BODY, "email": "new@x.com"}).status_code == 200
    assert client.get("/users/u1").json()["email"] == "new@x.com"
    assert len(client.get("/users").json()) == 1


def test_put_is_idempotent():
    client.put("/users/u1", json=BODY)
    client.put("/users/u1", json=BODY)
    assert len(client.get("/users").json()) == 1


def test_path_and_body_id_must_match():
    assert client.put("/users/other", json=BODY).status_code == 400


def test_deactivate_disables_but_keeps_the_user():
    client.put("/users/u1", json=BODY)
    assert client.post("/users/u1/deactivate").status_code == 200
    assert client.get("/users/u1").json()["active"] is False


def test_deactivate_unknown_user_is_404():
    assert client.post("/users/nope/deactivate").status_code == 404


def test_chaos_429_returns_retry_after(monkeypatch):
    monkeypatch.setenv("CHAOS_429_EVERY", "1")
    resp = client.get("/users")
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "1"
