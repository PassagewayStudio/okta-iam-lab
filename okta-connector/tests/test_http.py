"""Retry, rate-limit and timeout behaviour of the shared HTTP client. No real sleeping happens."""
import pytest
import requests
import responses

from connector.http import HttpClient, HttpError, RetryExhausted

URL = "https://example.okta.com/api/v1/users"


def make_client(**kwargs):
    sleeps = []
    kwargs.setdefault("rng", lambda: 1.0)  # no jitter reduction -> predictable delays
    client = HttpClient(requests.Session(), sleep=sleeps.append, **kwargs)
    return client, sleeps


@responses.activate
def test_429_honors_retry_after_header():
    responses.add(responses.GET, URL, status=429, headers={"Retry-After": "2"})
    responses.add(responses.GET, URL, json=[{"id": "1"}])
    client, sleeps = make_client()

    resp = client.request("GET", URL)

    assert resp.json() == [{"id": "1"}]
    assert sleeps == [2.0]
    assert len(responses.calls) == 2


@responses.activate
def test_429_uses_okta_rate_limit_reset_when_no_retry_after():
    # Okta documents X-Rate-Limit-Reset as UTC epoch seconds.
    responses.add(responses.GET, URL, status=429, headers={"X-Rate-Limit-Reset": "1005"})
    responses.add(responses.GET, URL, json=[])
    client, sleeps = make_client(now=lambda: 1000.0)

    client.request("GET", URL)

    assert sleeps == [6.0]  # 1005 - 1000, plus a 1s cushion


@responses.activate
def test_rate_limit_reset_prefers_server_clock_over_local_clock():
    # Date header says the server thinks it is epoch 1000; our local clock is badly skewed.
    responses.add(
        responses.GET, URL, status=429,
        headers={"X-Rate-Limit-Reset": "1005", "Date": "Thu, 01 Jan 1970 00:16:40 GMT"},
    )
    responses.add(responses.GET, URL, json=[])
    client, sleeps = make_client(now=lambda: 5_000_000.0)

    client.request("GET", URL)

    assert sleeps == [6.0]


@responses.activate
def test_retry_after_is_capped():
    responses.add(responses.GET, URL, status=429, headers={"Retry-After": "9999"})
    responses.add(responses.GET, URL, json=[])
    client, sleeps = make_client()

    client.request("GET", URL)

    assert sleeps == [60.0]


@responses.activate
def test_exponential_backoff_when_no_rate_limit_headers():
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, status=503)
    responses.add(responses.GET, URL, json=[])
    client, sleeps = make_client()

    client.request("GET", URL)

    assert sleeps == [0.5, 1.0, 2.0]


@responses.activate
@pytest.mark.parametrize(
    "exc", [requests.exceptions.ReadTimeout("slow"), requests.exceptions.ConnectionError("down")]
)
def test_timeouts_and_connection_errors_are_retried(exc):
    responses.add(responses.GET, URL, body=exc)
    responses.add(responses.GET, URL, json=[{"id": "1"}])
    client, sleeps = make_client()

    resp = client.request("GET", URL)

    assert resp.json() == [{"id": "1"}]
    assert sleeps == [0.5]


@responses.activate
def test_gives_up_after_max_retries_with_clear_error():
    responses.add(responses.GET, URL, status=503)
    client, sleeps = make_client(max_retries=3)

    with pytest.raises(RetryExhausted) as info:
        client.request("GET", URL)

    assert info.value.attempts == 4  # 1 try + 3 retries
    assert len(responses.calls) == 4
    assert len(sleeps) == 3
    assert "HTTP 503" in str(info.value)


@responses.activate
def test_persistent_timeout_raises_retry_exhausted():
    responses.add(responses.GET, URL, body=requests.exceptions.ConnectTimeout("no route"))
    client, _ = make_client(max_retries=2)

    with pytest.raises(RetryExhausted) as info:
        client.request("GET", URL)

    assert info.value.last_error == "ConnectTimeout"


@responses.activate
def test_401_is_not_retried():
    responses.add(responses.GET, URL, status=401, json={"errorCode": "E0000011"})
    client, sleeps = make_client()

    with pytest.raises(HttpError) as info:
        client.request("GET", URL)

    assert info.value.status == 401
    assert len(responses.calls) == 1
    assert sleeps == []


@responses.activate
def test_allowed_status_is_returned_not_raised():
    responses.add(responses.GET, URL, status=404)
    client, _ = make_client()

    assert client.request("GET", URL, allow={404}).status_code == 404
