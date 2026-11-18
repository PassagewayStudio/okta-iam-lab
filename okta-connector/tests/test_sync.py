"""Reconcile logic, using in-memory fakes for Okta and the target."""
import pytest

from connector.http import RetryExhausted
from connector.sync import sync


def okta_user(uid, login, status="ACTIVE", email=None, first="First", last="Last"):
    return {
        "id": uid,
        "status": status,
        "profile": {
            "login": login, "email": email or login, "firstName": first, "lastName": last,
        },
    }


def target_user(uid, login, active=True, email=None, first="First", last="Last"):
    return {
        "okta_id": uid, "login": login, "email": email or login,
        "first_name": first, "last_name": last, "active": active,
    }


class FakeOkta:
    def __init__(self, users=(), error=None):
        self.users = list(users)
        self.error = error

    def fetch_all_users(self):
        if self.error:
            raise self.error
        return {u["id"]: u for u in self.users}


class FakeTarget:
    def __init__(self, users=(), fail_on=()):
        self.users = {u["okta_id"]: dict(u) for u in users}
        self.fail_on = set(fail_on)
        self.calls = []

    def list_users(self):
        return {k: dict(v) for k, v in self.users.items()}

    def upsert_user(self, record):
        if record["okta_id"] in self.fail_on:
            raise RetryExhausted("PUT", "target", 6, "HTTP 503")
        self.calls.append(("upsert", record["okta_id"]))
        created = record["okta_id"] not in self.users
        self.users[record["okta_id"]] = dict(record)
        return created

    def deactivate_user(self, okta_id):
        self.calls.append(("deactivate", okta_id))
        self.users[okta_id]["active"] = False


def test_new_active_user_is_created():
    target = FakeTarget()
    result = sync(FakeOkta([okta_user("u1", "a@x.com")]), target)

    assert result.created == 1
    assert target.users["u1"]["login"] == "a@x.com"
    assert target.users["u1"]["active"] is True


def test_inactive_okta_user_never_provisioned_is_skipped():
    target = FakeTarget()
    result = sync(FakeOkta([okta_user("u1", "a@x.com", status="DEPROVISIONED")]), target)

    assert result.skipped == 1
    assert target.calls == []


def test_changed_profile_is_updated():
    target = FakeTarget([target_user("u1", "a@x.com", email="old@x.com")])
    result = sync(FakeOkta([okta_user("u1", "a@x.com", email="new@x.com")]), target)

    assert result.updated == 1
    assert target.users["u1"]["email"] == "new@x.com"


def test_identical_user_is_left_alone():
    target = FakeTarget([target_user("u1", "a@x.com")])
    result = sync(FakeOkta([okta_user("u1", "a@x.com")]), target)

    assert result.unchanged == 1
    assert target.calls == []


def test_deprovisioned_in_okta_is_deactivated_in_target():
    target = FakeTarget([target_user("u1", "a@x.com")])
    result = sync(FakeOkta([okta_user("u1", "a@x.com", status="DEPROVISIONED")]), target)

    assert result.deactivated == 1
    assert target.users["u1"]["active"] is False
    assert ("deactivate", "u1") in target.calls


def test_user_missing_from_okta_is_deactivated_not_deleted():
    target = FakeTarget([target_user("gone", "gone@x.com")])
    result = sync(FakeOkta([]), target)

    assert result.deactivated == 1
    assert target.users["gone"]["active"] is False  # still present, just disabled


def test_already_inactive_user_missing_from_okta_causes_no_write():
    target = FakeTarget([target_user("gone", "gone@x.com", active=False)])
    sync(FakeOkta([]), target)

    assert target.calls == []


def test_reactivated_okta_user_is_reactivated_in_target():
    target = FakeTarget([target_user("u1", "a@x.com", active=False)])
    result = sync(FakeOkta([okta_user("u1", "a@x.com")]), target)

    assert result.updated == 1
    assert target.users["u1"]["active"] is True


def test_dry_run_plans_but_writes_nothing():
    target = FakeTarget([target_user("old", "old@x.com")])
    result = sync(FakeOkta([okta_user("new", "new@x.com")]), target, dry_run=True)

    assert (result.created, result.deactivated) == (1, 1)
    assert target.calls == []
    assert target.users["old"]["active"] is True


def test_one_failing_user_does_not_stop_the_rest():
    target = FakeTarget(fail_on={"u1"})
    result = sync(FakeOkta([okta_user("u1", "a@x.com"), okta_user("u2", "b@x.com")]), target)

    assert result.created == 1
    assert len(result.errors) == 1
    assert result.errors[0]["okta_id"] == "u1"
    assert "u2" in target.users


def test_okta_failure_aborts_before_touching_the_target():
    # If the Okta listing is incomplete we must not deactivate anyone based on it.
    target = FakeTarget([target_user("u1", "a@x.com")])
    okta = FakeOkta(error=RetryExhausted("GET", "okta", 6, "HTTP 429"))

    with pytest.raises(RetryExhausted):
        sync(okta, target)

    assert target.calls == []
    assert target.users["u1"]["active"] is True


def test_second_run_is_a_no_op():
    okta = FakeOkta([okta_user("u1", "a@x.com"), okta_user("u2", "b@x.com")])
    target = FakeTarget()
    sync(okta, target)
    target.calls.clear()

    result = sync(okta, target)

    assert result.unchanged == 2
    assert target.calls == []
