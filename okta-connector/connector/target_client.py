"""Client for the target app's REST API (see target_app/main.py)."""
import logging

from .http import HttpClient

log = logging.getLogger("connector.target")


class TargetClient:
    def __init__(self, base_url: str, http: HttpClient):
        self.base = base_url.rstrip("/")
        self.http = http

    def list_users(self) -> dict[str, dict]:
        resp = self.http.request("GET", f"{self.base}/users")
        return {u["okta_id"]: u for u in resp.json()}

    def upsert_user(self, record: dict) -> bool:
        """Create or update. Returns True if the user was newly created.

        Uses PUT keyed by okta_id so a retried request cannot create a duplicate.
        """
        resp = self.http.request("PUT", f"{self.base}/users/{record['okta_id']}", json=record)
        return resp.status_code == 201

    def deactivate_user(self, okta_id: str) -> None:
        self.http.request("POST", f"{self.base}/users/{okta_id}/deactivate")
