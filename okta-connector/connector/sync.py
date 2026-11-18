"""Reconcile Okta users into the target app: create, update, deactivate.

Rules:
  * Okta user with status in ACTIVE_STATUSES  -> must exist and be active in the target.
  * Okta user with any other status (STAGED, PROVISIONED, SUSPENDED, DEPROVISIONED ...)
      -> deactivated in the target if present; never created.
  * Target user that no longer exists in Okta -> deactivated (disabled, not deleted).

Fail closed: the full Okta user list is fetched BEFORE anything is written. If that fails
(rate limit exhausted, network, bad token), the sync aborts with the target untouched, so a
partial Okta response can never cause users to be wrongly deactivated.
"""
import logging
from dataclasses import dataclass, field

from .http import HttpError, RetryExhausted

log = logging.getLogger("connector.sync")

ACTIVE_STATUSES = {"ACTIVE"}
COMPARED_FIELDS = ("login", "email", "first_name", "last_name", "active")


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "created": self.created, "updated": self.updated, "deactivated": self.deactivated,
            "unchanged": self.unchanged, "skipped": self.skipped, "errors": len(self.errors),
        }


def to_record(okta_user: dict) -> dict:
    profile = okta_user.get("profile") or {}
    return {
        "okta_id": okta_user["id"],
        "login": profile.get("login"),
        "email": profile.get("email"),
        "first_name": profile.get("firstName"),
        "last_name": profile.get("lastName"),
        "active": okta_user.get("status") in ACTIVE_STATUSES,
    }


def _differs(existing: dict, record: dict) -> bool:
    return any(existing.get(f) != record.get(f) for f in COMPARED_FIELDS)


def sync(okta, target, dry_run: bool = False) -> SyncResult:
    okta_users = okta.fetch_all_users()  # raises on failure, before any write
    target_users = target.list_users()
    result = SyncResult()

    for okta_id, okta_user in okta_users.items():
        record = to_record(okta_user)
        existing = target_users.get(okta_id)
        login = record["login"]

        if existing is None:
            if record["active"]:
                _apply(result, "created", okta_id, login, "new_in_okta", dry_run,
                       lambda r=record: target.upsert_user(r))
            else:
                result.skipped += 1  # inactive in Okta and never provisioned: nothing to do
        elif not record["active"]:
            if existing.get("active"):
                _apply(result, "deactivated", okta_id, login,
                       f"okta_status_{okta_user.get('status', 'unknown').lower()}", dry_run,
                       lambda i=okta_id: target.deactivate_user(i))
            else:
                result.unchanged += 1
        elif _differs(existing, record):
            _apply(result, "updated", okta_id, login, "profile_changed", dry_run,
                   lambda r=record: target.upsert_user(r))
        else:
            result.unchanged += 1

    for okta_id, existing in target_users.items():
        if okta_id not in okta_users and existing.get("active"):
            _apply(result, "deactivated", okta_id, existing.get("login"), "missing_from_okta",
                   dry_run, lambda i=okta_id: target.deactivate_user(i))

    # Nested under "result" because keys like "created" collide with built-in LogRecord attributes.
    log.info("sync_complete", extra={"result": result.summary(), "dry_run": dry_run})
    return result


def _apply(result: SyncResult, action: str, okta_id: str, login, reason: str, dry_run: bool, call):
    """Run one write; a failure on one user is recorded and the sync carries on with the rest."""
    fields = {"action": action, "okta_id": okta_id, "login": login, "reason": reason,
              "dry_run": dry_run}
    if dry_run:
        log.info("sync_action_planned", extra=fields)
        setattr(result, action, getattr(result, action) + 1)
        return
    try:
        call()
    except (HttpError, RetryExhausted) as exc:
        log.error("sync_action_failed", extra={**fields, "error": str(exc)})
        result.errors.append({"okta_id": okta_id, "action": action, "error": str(exc)})
        return
    log.info("sync_action_done", extra=fields)
    setattr(result, action, getattr(result, action) + 1)
