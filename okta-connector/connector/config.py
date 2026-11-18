"""Configuration from environment variables. Secrets never go in code or in the repo."""
import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    okta_org_url: str
    okta_api_token: str = field(repr=False)  # repr=False keeps the token out of logs/tracebacks
    target_url: str = "http://127.0.0.1:8000"
    connect_timeout: float = 3.05
    read_timeout: float = 10.0
    max_retries: int = 5
    page_size: int = 200


def load_config(env=None) -> Config:
    env = os.environ if env is None else env

    org_url = env.get("OKTA_ORG_URL", "").strip().rstrip("/")
    token = env.get("OKTA_API_TOKEN", "").strip()
    missing = [n for n, v in (("OKTA_ORG_URL", org_url), ("OKTA_API_TOKEN", token)) if not v]
    if missing:
        raise ConfigError(f"missing required environment variable(s): {', '.join(missing)}")
    if not org_url.startswith("https://"):
        raise ConfigError("OKTA_ORG_URL must start with https:// (e.g. https://dev-123456.okta.com)")

    try:
        return Config(
            okta_org_url=org_url,
            okta_api_token=token,
            target_url=env.get("TARGET_URL", "http://127.0.0.1:8000").strip().rstrip("/"),
            connect_timeout=float(env.get("CONNECT_TIMEOUT", 3.05)),
            read_timeout=float(env.get("READ_TIMEOUT", 10.0)),
            max_retries=int(env.get("MAX_RETRIES", 5)),
            page_size=min(int(env.get("PAGE_SIZE", 200)), 200),  # Okta's maximum page size is 200
        )
    except ValueError as exc:
        raise ConfigError(f"invalid numeric setting: {exc}") from exc
