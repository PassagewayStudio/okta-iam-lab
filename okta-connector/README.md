# okta-connector

A Python connector that reads users from the Okta API and syncs them into an application over REST: **create, update, deactivate**, with pagination, rate-limit handling, retries with backoff, and structured logging.

The "application" is a small FastAPI app in this repo (`target_app/`) that stands in for any system with a users API.

> **Status:** working. 44 automated tests pass (Okta mocked), and the main flows were run by hand against a real Okta developer tenant on 2026-11-18; see [Verification against a real Okta tenant](#verification-against-a-real-okta-tenant). One path is covered by unit tests only: handling of Okta's own `X-Rate-Limit-Reset` header on a 429. Groups and OAuth are not built yet (see the end of this file).

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
Each item below was run by hand against a real Okta developer tenant, with the target app running locally. Raw logs are not included here because they contain real user names and addresses.

Verified on a real Okta developer tenant, 2026-11-18:
- [x] `python -m connector sync --dry-run` lists the expected users without writing
- [x] A user created in Okta appears in the target after a sync
- [x] A user deactivated in Okta (Admin Console -> More Actions -> Deactivate) is disabled in the target after the next sync
- [x] Users that haven't changed are left alone (the target's server log showed only the one `POST .../deactivate` and no `PUT`s on the second run)
- [x] With `CHAOS_429_EVERY=3` on the target, a 429 mid-sync was retried after the `Retry-After` delay (log: `http_retry`, `status: 429`, `delay_s: 1.0`; server log: 429 then 201 for the same user) and the sync finished with `errors: 0`. This exercised the `Retry-After` path against the target app. Okta's own `X-Rate-Limit-Reset` path is covered by unit tests only, because I did not try to trigger a real Okta rate limit.
- [x] With `CHAOS_SLOW_EVERY=4 CHAOS_SLOW_SECONDS=15` on the target, a request that hung past the 10s read timeout was retried (log: `request_error_retrying`, `error: ReadTimeout`, `delay_s: 0.33`, logged 10.0s after the previous action) and the sync finished with `errors: 0`. The target held exactly 3 users afterwards, with no duplicates.
- [x] A wrong `OKTA_API_TOKEN` fails fast: one attempt and no retries, `HTTP 401` with Okta error code `E0000011` ("Invalid token provided"), "target not modified", exit code 1.
- [x] Real pagination with `PAGE_SIZE=2`: the main list took 2 pages (2 + 2 users) and the `DEPROVISIONED` search took 2 pages (2 + 1), 7 users in total, matching the unpaged runs. The `Link: rel="next"` header was followed on a real Okta tenant.
- [x] Target app unreachable (found by accident when it wasn't running): the connector fetched all Okta users first, then retried the target 5 times with exponential backoff (0.35s, 0.75s, 1.38s, 3.99s, 7.31s, about 14s in total), logged `retries_exhausted`, and aborted with "target not modified" and exit code 1.
- [x] A profile change in Okta updates the target: correcting a test user's name in Okta produced one `sync_action_done` with `action: updated`, `reason: profile_changed` (`updated: 1, unchanged: 3`). Only that user's `updated_at` changed in the target.

## What broke and how I fixed it
**1. The first real dry run crashed at the very end with `KeyError: "Attempt to overwrite 'created' in LogRecord"`.** Authentication, pagination and the planned actions had all worked and been logged. The crash came from the final `sync_complete` log call, which passed the result counts as `extra={"created": ..., "updated": ...}`. `created` is a built-in attribute of Python's `LogRecord` (the event timestamp), and logging refuses to overwrite it. The unit tests missed it because pytest leaves the root logger at WARNING, so `log.info(...)` calls never built a record at all. Fix: the counts are now nested under one `result` key, and `tests/conftest.py` runs every test with DEBUG logging so a bad log field fails in the tests instead of on the first real run.

**2. Okta's default user list hides deactivated users, confirmed on the real tenant.** On the first sync the default list returned 5 users and the `status eq "DEPROVISIONED"` search returned 2 (7 total, no overlap). After I deactivated a test user in Okta, the default list dropped to 4 and the search grew to 3. Without the second query the deactivation would never have been noticed. The next sync logged `deactivated ... reason: okta_status_deprovisioned`, the target's server log showed a single `POST /users/<id>/deactivate`, and the user was left in the target with `"active": false` (disabled, not deleted).

**3. Not every Okta user gets created.** On the first sync, 3 of the 7 Okta users were not `ACTIVE` and were skipped rather than created in the target, which is the intended behaviour (`created: 4, skipped: 3` in the `sync_complete` line).

## Not built yet
- **Groups and group membership.** Only users are synced.
- **OAuth 2.0 client credentials / private-key JWT.** Authentication is an API token (SSWS) for now.
- A guard that aborts if one run would deactivate an unusually large share of users.
