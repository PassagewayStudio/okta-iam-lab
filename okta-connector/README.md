# okta-connector

A Python connector that reads users from the Okta API and syncs them into an application over REST: **create, update, deactivate**, with pagination, rate-limit handling, retries with backoff, and structured logging.

The "application" is a small FastAPI app in this repo (`target_app/`) that stands in for any system with a users API.

> **Status:** built and unit/integration tested against a mocked Okta. The checklist under [Verification against a real Okta tenant](#verification-against-a-real-okta-tenant) has **not** been completed yet. Don't treat it as proven until it is.

## How it works

```
Okta  --GET /api/v1/users (paged)-->  connector  --PUT /users/{okta_id}-->  target app
      --GET ...?search=status eq "DEPROVISIONED"-->        --POST /users/{id}/deactivate-->
```

1. Fetch **every** Okta user (follow `Link: rel="next"` pagination, 200 per page).
2. Fetch the target app's current users.
3. Reconcile:
   | Situation | Action |
   |---|---|
   | Active in Okta, not in target | create |
   | Active in Okta, profile differs in target (or target copy is inactive) | update |
   | Not `ACTIVE` in Okta (STAGED, SUSPENDED, DEPROVISIONED ...), active in target | deactivate |
   | In target, gone from Okta | deactivate (disabled, **not** deleted) |
   | Already matches | leave alone |

### Design decisions
- **Deactivated users need a separate query.** Okta's default `GET /api/v1/users` excludes `DEPROVISIONED` users, so the connector also asks for `search=status eq "DEPROVISIONED"`. Without that, a user deactivated in Okta would look like any other user and never be noticed.
- **Fail closed.** The full Okta listing is fetched before anything is written. If it fails partway (rate limit exhausted, network, bad token) the run aborts with the target untouched, so a partial listing can never cause wrongful deactivations.
- **Idempotent writes.** Create and update are one `PUT /users/{okta_id}`, so a retried request can't create a duplicate. Deactivate is also safe to repeat.
- **Retry policy** (`connector/http.py`): retries 429, 502, 503, 504, connection errors and timeouts. Wait time is `Retry-After` if present, else Okta's documented `X-Rate-Limit-Reset` (epoch seconds, compared against the server's `Date` header so local clock skew doesn't matter), else exponential backoff with jitter. Waits are capped at 60s. Other 4xx errors (e.g. 401 bad token) are **not** retried.
- **One bad user doesn't stop the run.** Per-user write failures are logged and counted; the exit code is non-zero if any occurred.
- **Secrets.** The API token comes from an environment variable, is never logged, and is excluded from the config's `repr`. `.env` is gitignored.
- **Only `ACTIVE` counts as active.** Change `ACTIVE_STATUSES` in `connector/sync.py` if you want, say, `PASSWORD_EXPIRED` users treated as active.

## Setup

Requires Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate          # Git Bash on Windows: source .venv/Scripts/activate
pip install -r requirements-dev.txt
```

### Okta API token (lab tenant only)
Admin Console -> Security -> API -> Tokens -> Create token. The token carries the permissions of the admin who creates it, so create it as an admin with a **read-only administrator** role; this connector only reads.

```bash
cp .env.example .env               # then edit .env; it is gitignored
set -a; source .env; set +a        # load it into your shell
```

## Run

Terminal 1, the target app:
```bash
uvicorn target_app.main:app --port 8000
```

Terminal 2, the connector:
```bash
python -m connector sync --dry-run   # log what would change, write nothing
python -m connector sync
```
Logs are JSON lines, one event per line, e.g. `{"event": "sync_action_done", "action": "created", "login": "...", "reason": "new_in_okta", ...}`. Exit codes: `0` ok, `1` some users failed or the run aborted, `2` configuration error.

Check the result: `curl http://127.0.0.1:8000/users`

### Forcing failures (no Okta needed)
Restart the target app with chaos switches, then run a sync:
```bash
CHAOS_429_EVERY=3 uvicorn target_app.main:app --port 8000              # every 3rd request -> 429 + Retry-After
CHAOS_SLOW_EVERY=4 CHAOS_SLOW_SECONDS=15 uvicorn target_app.main:app --port 8000   # every 4th request hangs past the 10s read timeout
```
Watch for `http_retry` / `request_error_retrying` warnings in the connector's log, and for the sync still finishing with the right result.

## Tests
```bash
python -m pytest
```
44 tests: pagination via `Link` header, deactivated users, 429 with `Retry-After` / `X-Rate-Limit-Reset` / neither, timeouts, retry exhaustion, bad token not retried, every sync rule, fail-closed behaviour, and end-to-end runs against the real target app with forced 429s and forced timeouts.

## Verification against a real Okta tenant
Tick these only after running them on a real dev tenant, and paste the real output into the section below.

- [ ] `python -m connector sync --dry-run` lists the expected users without writing
- [ ] A user created in Okta appears in the target after a sync
- [ ] A user deactivated in Okta (Admin Console -> More Actions -> Deactivate) is disabled in the target after the next sync
- [ ] A profile change in Okta (e.g. name) updates the target
- [ ] With `CHAOS_429_EVERY` set, the sync completes and the log shows the retries
- [ ] With `CHAOS_SLOW_EVERY` set, a timeout is retried and the sync completes with no duplicates
- [ ] A wrong `OKTA_API_TOKEN` fails fast (HTTP 401, one attempt, non-zero exit, target untouched)

## What broke and how I fixed it
_Fill this in from real runs. Include the symptom, the log line or command output that showed it, the root cause, and the fix. Anything that behaved differently from what the docs or this README predicted is the most valuable thing to write down._

## Not built yet
- **Groups and group membership.** Only users are synced.
- **OAuth 2.0 client credentials / private-key JWT.** Authentication is an API token (SSWS) for now.
- A guard that aborts if one run would deactivate an unusually large share of users.
