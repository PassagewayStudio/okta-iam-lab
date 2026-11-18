"""Okta client: pagination, deactivated users, and rate limits in the middle of a listing."""
import pytest
import requests
import responses
from responses import matchers

from connector.http import HttpClient, HttpError
from connector.okta_client import OktaClient, OktaError

ORG = "https://dev-1.okta.com"
USERS = f"{ORG}/api/v1/users"


def make_okta(token="tok123"):
    sleeps = []
    http = HttpClient(requests.Session(), sleep=sleeps.append, rng=lambda: 1.0)
    return OktaClient(ORG, token, http), sleeps


@responses.activate
def test_follows_link_header_pagination_and_sends_ssws_token():
    next_url = f"{USERS}?after=abc123&limit=200"
    link = f'<{USERS}?limit=200>; rel="self", <{next_url}>; rel="next"'
    responses.add(
        responses.GET, USERS, json=[{"id": "1"}, {"id": "2"}], headers={"Link": link},
        match=[matchers.query_param_matcher({"limit": "200"})],
    )
    responses.add(
        responses.GET, USERS, json=[{"id": "3"}],
        match=[matchers.query_param_matcher({"after": "abc123", "limit": "200"})],
    )
    okta, _ = make_okta()

    users = okta.list_users()

    assert [u["id"] for u in users] == ["1", "2", "3"]
    assert len(responses.calls) == 2
    assert responses.calls[0].request.headers["Authorization"] == "SSWS tok123"


@responses.activate
def test_fetch_all_includes_deprovisioned_users_requested_separately():
    # Okta's default list omits DEPROVISIONED users, so they must be asked for explicitly.
    responses.add(
        responses.GET, USERS, json=[{"id": "A", "status": "ACTIVE"}],
        match=[matchers.query_param_matcher({"limit": "200"})],
    )
    responses.add(
        responses.GET, USERS, json=[{"id": "B", "status": "DEPROVISIONED"}],
        match=[matchers.query_param_matcher({"search": 'status eq "DEPROVISIONED"', "limit": "200"})],
    )
    okta, _ = make_okta()

    users = okta.fetch_all_users()

    assert set(users) == {"A", "B"}
    assert users["B"]["status"] == "DEPROVISIONED"


@responses.activate
def test_rate_limit_on_second_page_is_retried_and_listing_completes():
    next_url = f"{USERS}?after=abc123&limit=200"
    responses.add(
        responses.GET, USERS, json=[{"id": "1"}], headers={"Link": f'<{next_url}>; rel="next"'},
        match=[matchers.query_param_matcher({"limit": "200"})],
    )
    page2 = matchers.query_param_matcher({"after": "abc123", "limit": "200"})
    responses.add(responses.GET, USERS, status=429, headers={"Retry-After": "1"}, match=[page2])
    responses.add(responses.GET, USERS, json=[{"id": "2"}], match=[page2])
    okta, sleeps = make_okta()

    users = okta.list_users()

    assert [u["id"] for u in users] == ["1", "2"]
    assert sleeps == [1.0]


@responses.activate
def test_bad_api_token_fails_immediately():
    responses.add(responses.GET, USERS, status=401, json={"errorCode": "E0000011"})
    okta, sleeps = make_okta(token="wrong")

    with pytest.raises(HttpError) as info:
        okta.list_users()

    assert info.value.status == 401
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_unexpected_response_shape_raises_okta_error():
    responses.add(responses.GET, USERS, json={"errorCode": "weird"})
    okta, _ = make_okta()

    with pytest.raises(OktaError):
        okta.list_users()
