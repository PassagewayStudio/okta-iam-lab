import argparse
import sys

import requests

from .config import ConfigError, load_config
from .http import HttpClient, HttpError, RetryExhausted
from .logging_setup import setup_logging
from .okta_client import OktaClient, OktaError
from .sync import sync
from .target_client import TargetClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="connector", description="Sync Okta users into the target app.")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("sync", help="reconcile Okta users into the target app")
    p.add_argument("--dry-run", action="store_true", help="log what would change, write nothing")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        args = build_parser().parse_args(["sync"])

    setup_logging(args.log_level)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    http = HttpClient(
        requests.Session(),
        timeout=(cfg.connect_timeout, cfg.read_timeout),
        max_retries=cfg.max_retries,
    )
    okta = OktaClient(cfg.okta_org_url, cfg.okta_api_token, http, cfg.page_size)
    target = TargetClient(cfg.target_url, http)

    try:
        result = sync(okta, target, dry_run=args.dry_run)
    except (HttpError, RetryExhausted, OktaError) as exc:
        # Raised before any write (the Okta fetch comes first), so the target is untouched.
        print(f"sync aborted, target not modified: {exc}", file=sys.stderr)
        return 1
    return 1 if result.errors else 0


if __name__ == "__main__":
    sys.exit(main())
