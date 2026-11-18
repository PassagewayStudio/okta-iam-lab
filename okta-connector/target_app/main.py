"""Toy target application: a tiny users REST API the connector syncs Okta users into.

State is in memory, so restarting the app clears it (the next sync recreates everything).

Chaos switches (environment variables, read on every request) let you force the
failure modes the connector must survive, without touching Okta:

  CHAOS_429_EVERY=N     every Nth request returns 429 with Retry-After: 1
  CHAOS_SLOW_EVERY=N    every Nth request sleeps CHAOS_SLOW_SECONDS before answering
  CHAOS_SLOW_SECONDS=S  how long a slow request sleeps (default 15; client read timeout is 10)
"""
import asyncio
import os
import threading
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Toy target app")

_users: dict[str, dict] = {}
_lock = threading.Lock()
_request_count = 0


def reset_state() -> None:
    """Clear all users and the chaos request counter (used by tests)."""
    global _request_count
    with _lock:
        _users.clear()
        _request_count = 0


class UserIn(BaseModel):
    okta_id: str
    login: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    active: bool = True


def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@app.middleware("http")
async def chaos(request, call_next):
    global _request_count
    if request.url.path == "/health":
        return await call_next(request)
    with _lock:
        _request_count += 1
        n = _request_count
    every_429 = _env_int("CHAOS_429_EVERY")
    if every_429 and n % every_429 == 0:
        return JSONResponse(
            {"detail": "rate limited (chaos)"}, status_code=429, headers={"Retry-After": "1"}
        )
    every_slow = _env_int("CHAOS_SLOW_EVERY")
    if every_slow and n % every_slow == 0:
        await asyncio.sleep(_env_int("CHAOS_SLOW_SECONDS", 15))
    return await call_next(request)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/users")
def list_users():
    with _lock:
        return list(_users.values())


@app.get("/users/{okta_id}")
def get_user(okta_id: str):
    with _lock:
        user = _users.get(okta_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


@app.put("/users/{okta_id}")
def upsert_user(okta_id: str, body: UserIn, response: Response):
    """Create or update. PUT is idempotent, so the connector can retry it safely."""
    if body.okta_id != okta_id:
        raise HTTPException(status_code=400, detail="okta_id in body does not match path")
    record = body.model_dump()
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    with _lock:
        created = okta_id not in _users
        _users[okta_id] = record
    response.status_code = 201 if created else 200
    return record


@app.post("/users/{okta_id}/deactivate")
def deactivate_user(okta_id: str):
    """Disable (not delete) a user. Idempotent."""
    with _lock:
        user = _users.get(okta_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        user["active"] = False
        user["updated_at"] = datetime.now(timezone.utc).isoformat()
        return user
