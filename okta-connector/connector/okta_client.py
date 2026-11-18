"""Read-only Okta client: lists users, following pagination.

Two Okta behaviours this depends on (from Okta's API docs):
  * GET /api/v1/users returns everyone EXCEPT users with status DEPROVISIONED.
    Deactivated users must be requested separately with search=status eq "DEPROVISIONED".
  * Pagination is cursor-based: follow the `Link: <...>; rel="next"` response header.
    The maximum page size (limit) is 200.
"""
import logging

from .http import HttpClient

log = logging.getLogger("connector.okta")


class OktaError(Exception):
    pass


class OktaClient:
    def __init__(self, org_url: str, api_token: str, http: HttpClient, page_size: int = 200):
        self.base = org_url.rstrip("/")
        self.http = http
        self.page_size = min(page_size, 200)
        # Okta API tokens use the SSWS scheme. Never log this header.
        self._headers = {"Authorization": f"SSWS {api_token}", "Accept": "application/json"}

    def _paginate(self, path: str, params: dict | None):
        url = f"{self.base}{path}"
        page = 0
        while url:
            resp = self.http.request("GET", url, headers=self._headers, params=params)
            params = None  # the next-page link already carries the full query string
            data = resp.json()
            if not isinstance(data, list):
                raise OktaError(f"expected a JSON list from {url}, got {type(data).__name__}")
            page += 1
            log.info("okta_page_fetched", extra={"path": path, "page": page, "count": len(data)})
            yield from data
            url = resp.links.get("next", {}).get("url")

    def list_users(self) -> list[dict]:
        """All users except DEPROVISIONED ones (Okta's default behaviour)."""
        return list(self._paginate("/api/v1/users", {"limit": self.page_size}))

    def list_deprovisioned_users(self) -> list[dict]:
        params = {"search": 'status eq "DEPROVISIONED"', "limit": self.page_size}
        return list(self._paginate("/api/v1/users", params))

    def fetch_all_users(self) -> dict[str, dict]:
        """Every user, keyed by Okta id. Raises if any page fails, so callers never see a partial list."""
        users: dict[str, dict] = {}
        for user in self.list_users():
            users[user["id"]] = user
        for user in self.list_deprovisioned_users():
            users[user["id"]] = user
        log.info("okta_fetch_complete", extra={"users": len(users)})
        return users
