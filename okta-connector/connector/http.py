"""HTTP client with retries, rate-limit handling and timeouts, shared by the Okta and target clients.

Retried:     HTTP 429, 502, 503, 504, connection errors, and timeouts.
Not retried: any other 4xx (401 bad token, 403, 404, 400 ...). Retrying those cannot help.

How long to wait before a retry, in order of preference:
  1. Retry-After header (seconds or HTTP date), if present
  2. X-Rate-Limit-Reset header (UTC epoch seconds, what Okta documents for 429s)
  3. exponential backoff with jitter
The wait is capped so one bad header can never hang the connector.
"""
import logging
import random
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import requests

log = logging.getLogger("connector.http")

RETRY_STATUSES = {429, 502, 503, 504}


class HttpError(Exception):
    """A non-retryable error response (e.g. 401, 403, 404)."""

    def __init__(self, status: int, method: str, url: str, body: str):
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:200]}")
        self.status = status
        self.method = method
        self.url = url
        self.body = body


class RetryExhausted(Exception):
    """Every attempt failed with a retryable problem."""

    def __init__(self, method: str, url: str, attempts: int, last_error: str):
        super().__init__(f"{method} {url} failed after {attempts} attempts: {last_error}")
        self.method = method
        self.url = url
        self.attempts = attempts
        self.last_error = last_error


class HttpClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        timeout: tuple[float, float] = (3.05, 10.0),
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        max_retry_after: float = 60.0,
        sleep=time.sleep,
        now=time.time,
        rng=random.random,
    ):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.max_retry_after = max_retry_after
        # sleep/now/rng are injectable so tests run instantly and deterministically
        self.sleep = sleep
        self.now = now
        self.rng = rng

    def request(self, method: str, url: str, *, allow=(), **kwargs) -> requests.Response:
        """Send a request, retrying transient failures.

        `allow` is a set of status codes returned to the caller instead of raising (e.g. {404}).
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                error = type(exc).__name__
                if attempt > self.max_retries:
                    log.error(
                        "retries_exhausted",
                        extra={"method": method, "url": url, "attempts": attempt, "error": error},
                    )
                    raise RetryExhausted(method, url, attempt, error) from exc
                delay = self._backoff(attempt)
                log.warning(
                    "request_error_retrying",
                    extra={"method": method, "url": url, "attempt": attempt,
                           "delay_s": round(delay, 2), "error": error},
                )
                self.sleep(delay)
                continue

            if resp.status_code in RETRY_STATUSES:
                error = f"HTTP {resp.status_code}"
                if attempt > self.max_retries:
                    log.error(
                        "retries_exhausted",
                        extra={"method": method, "url": url, "attempts": attempt, "error": error},
                    )
                    raise RetryExhausted(method, url, attempt, error)
                delay = self._retry_delay(resp, attempt)
                log.warning(
                    "http_retry",
                    extra={"method": method, "url": url, "attempt": attempt,
                           "status": resp.status_code, "delay_s": round(delay, 2)},
                )
                self.sleep(delay)
                continue

            if resp.status_code >= 400 and resp.status_code not in allow:
                raise HttpError(resp.status_code, method, url, resp.text)
            return resp

    # -- wait-time helpers -------------------------------------------------

    def _backoff(self, attempt: int) -> float:
        base = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return base * (0.5 + self.rng() / 2)  # jitter: 50-100% of the base delay

    def _retry_delay(self, resp: requests.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            delay = self._parse_retry_after(retry_after)
            if delay is not None:
                return self._clamp(delay)

        reset = resp.headers.get("X-Rate-Limit-Reset")
        if reset:
            try:
                reset_epoch = float(reset)
            except ValueError:
                reset_epoch = None
            if reset_epoch is not None:
                # Prefer the server's own clock (Date header) so local clock skew doesn't matter.
                reference = self._server_time(resp) or self.now()
                return self._clamp(reset_epoch - reference + 1)  # +1s cushion past the reset

        return self._backoff(attempt)

    def _parse_retry_after(self, value: str) -> float | None:
        try:
            return float(value)
        except ValueError:
            pass
        try:  # HTTP-date form
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return when.timestamp() - self.now()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _server_time(resp: requests.Response) -> float | None:
        date = resp.headers.get("Date")
        if not date:
            return None
        try:
            return parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            return None

    def _clamp(self, delay: float) -> float:
        return max(0.0, min(delay, self.max_retry_after))
