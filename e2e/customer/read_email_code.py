"""E2E 的「收件箱替身」：从 customer_email_codes 反推最新一条未消费验证码。

生产环境里验证码只出现在邮件里；E2E 没有邮件沙箱，但码的空间只有 10^6 且
storage 里存的是无迭代的 keyed HMAC（``customer_device_service.keyed_digest``），
所以「收信」退化成一次本地穷举——只读，不写任何行。真实收信链路（签发、
冷却、消费、作废）全部照跑，唯一被替身顶掉的是邮件传输本身。

Usage:
    python e2e/customer/read_email_code.py <username> <purpose>

    purpose ∈ {bind_email, reset_password}

Reads env (the same values the API process was started with, see
setup-backend.mjs):
    CUSTOMER_E2E_DATABASE_URL
    VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY

Prints one line for the Playwright helper to parse:
    EMAIL_CODE=123456
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.customer_device_service import (  # noqa: E402
    highest_device_domain_key,
    keyed_digest,
)

PURPOSES = ("bind_email", "reset_password")


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[2] not in PURPOSES:
        raise SystemExit(f"usage: read_email_code.py <username> <{'|'.join(PURPOSES)}>")
    username, purpose = sys.argv[1], sys.argv[2]

    with psycopg.connect(os.environ["CUSTOMER_E2E_DATABASE_URL"]) as conn:
        row = conn.execute(
            "SELECT c.id, c.code_digest FROM customer_email_codes c "
            "JOIN users u ON u.id = c.user_id "
            "WHERE u.username = %s AND c.purpose = %s AND c.consumed_at IS NULL "
            "ORDER BY c.created_at DESC LIMIT 1",
            (username, purpose),
        ).fetchone()
    if row is None:
        raise SystemExit(f"no live {purpose} code for {username}")

    code_id, stored = str(row[0]), str(row[1])
    _, key = highest_device_domain_key()
    # Same shape as ``customer_email_routes._code_digest_value`` — reproduced
    # because importing that module drags the whole route layer in.
    prefix = f"email-code:{code_id}:"
    for value in range(1_000_000):
        candidate = f"{value:06d}"
        if keyed_digest(key, prefix + candidate) == stored:
            print(f"EMAIL_CODE={candidate}")
            return
    raise SystemExit("digest did not resolve; the code row shape changed")


if __name__ == "__main__":
    main()
