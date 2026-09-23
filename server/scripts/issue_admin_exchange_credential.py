"""T09 / DB-08 — issue a single-use admin exchange credential.

Usage (server/ directory, key configured via the environment):

    VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY=<key> uv run python \
        -m scripts.issue_admin_exchange_credential --actor-user-id <id> \
        [--ttl-seconds 900] [--key-version 1]

The credential is printed once and never stored; the nonce digest becomes the
admin_sessions primary key when the operator exchanges it, so a replayed
credential can never mint a second session.
"""

from __future__ import annotations

import argparse
import sys

from app.admin_auth_routes import (
    DEFAULT_ADMIN_SESSION_TTL_SECONDS,
    issue_exchange_credential,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor-user-id", required=True, help="active admin/auditor user id")
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_ADMIN_SESSION_TTL_SECONDS // 8,
        help="credential validity in seconds (default: 15 minutes)",
    )
    parser.add_argument("--key-version", type=int, default=1)
    args = parser.parse_args(argv)

    credential = issue_exchange_credential(
        args.actor_user_id,
        ttl_seconds=args.ttl_seconds,
        key_version=args.key_version,
    )
    print(credential)
    print(
        "Single-use: exchanging it mints the admin cookie; keep it out of logs and chat channels.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
