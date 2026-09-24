"""T15 / ACT-08 — shared multi-instance rate limiting and anti-enumeration.

Fail-first tests for the frozen files ``server/app/security_rate_limit.py``
and migration ``032_security_rate_limits``: the activation/login abuse
controls that must hold across *every* API instance behind the load
balancer (ACT-08 red line: in-process rate limiting is not a multi-API
answer — the counter state lives in PostgreSQL so a second API instance
sees the exact same budget).

Contract under test (task list §3 T15 / §12.2 ACT-08; acceptance spec §6):

- fixed-window counters keyed by dimension (``activate:ip``,
  ``activate:code``, ``login:ip``, ``login:account``) share one row per
  bucket in PG: two independent connections — two API instances — draw
  from the same budget atomically;
- exceeding a limit answers 429 ``RATE_LIMITED`` with a ``Retry-After``
  header in seconds until the window closes;
- the window resets after expiry, dimensions stay independent, and the
  identifier for the code dimension is the keyed digest — never the
  plaintext activation code;
- every code-side rejection is recorded as a security failure event
  (append-only), aggregatable into failure metrics and an alert
  threshold for operators;
- the unified rejection path pays a constant anti-enumeration delay so
  unknown, expired, suspended, revoked and already-active codes share
  one latency profile (timing side channel).
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from pg_test_kit import require_pg_or_explicit_skip
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.activation_code_service import (
    ACTIVATION_CODE_HMAC_KEY_ENV,
    compute_code_digest,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

T15_DB_NAME = "t15_customer_security_test"

TEST_KEY = secrets.token_urlsafe(48)  # code HMAC key (v1), never a real secret
TEST_FINGERPRINT_KEY = secrets.token_urlsafe(48)
TEST_ENVELOPE_AEAD_KEY = secrets.token_bytes(32)

ACTIVATE_PATH = "/api/customer/activate"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
FUTURE_EXPIRY = "2099-01-01T00:00:00+00:00"

# Canonical Crockford-shaped codes (prefix + 4 groups x 7 characters, no
# O/I/L/U confusables so normalization is the identity on them).
UNKNOWN_CODE = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
BURST_CODE = "XS04-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF"
UNKNOWN_ZZ_CODE = "XS04-ZZZZZZZ-ZZZZZZZ-ZZZZZZZ-ZZZZZZZ"
EXPIRED_CODE = "XS04-DEADBEE-DEADBEE-DEADBEE-DEADBEE"
MALFORMED_CODE = "not-a-code!!"

COUNTERS_TABLE = "security_rate_limit_counters"
FAILURES_TABLE = "security_auth_failures"


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


def _admin_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + "/postgres"


def _t15_dsn() -> str:
    return _pg_dsn().rsplit("/", 1)[0] + f"/{T15_DB_NAME}"


def _head_revision() -> str:
    from alembic.script import ScriptDirectory

    server_dir = Path(__file__).resolve().parent.parent
    head = ScriptDirectory(str(server_dir / "migrations")).get_current_head()
    assert head is not None
    return head


# ---------------------------------------------------------------------------
# Module-level units (no database) — these always run (T14 fixture-skip
# precedent: a machine without the fixture must not report vacuous green).
# ---------------------------------------------------------------------------


def _customer_ingress_app() -> FastAPI:
    from app.main import require_loopback_client
    from app.security_rate_limit import client_ip_from_request

    ingress_app = FastAPI()
    ingress_app.middleware("http")(require_loopback_client)

    @ingress_app.get("/probe")
    async def probe(request: Request) -> dict[str, str]:
        return {"client_ip": client_ip_from_request(request)}

    return ingress_app


def _customer_ingress_get(
    app: Any,
    *,
    peer: str,
    headers: list[tuple[str, str]] | None = None,
) -> Response:
    async def request() -> Response:
        transport = ASGITransport(app=app, client=(peer, 443))
        async with AsyncClient(
            transport=transport,
            base_url="https://app.example.test",
        ) as client:
            return await client.get("/probe", headers=headers)

    return asyncio.run(request())


def _configure_customer_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv("VIDEO_REPLICA_PUBLIC_ORIGIN", "https://app.example.test")
    monkeypatch.setenv("VIDEO_REPLICA_TRUSTED_PROXY_CIDRS", "10.20.0.0/24")


def test_customer_ingress_accepts_only_trusted_single_forwarded_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_customer_ingress(monkeypatch)
    response = _customer_ingress_get(
        _customer_ingress_app(),
        peer="10.20.0.8",
        headers=[
            ("Host", "app.example.test"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-For", "203.0.113.41"),
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"client_ip": "203.0.113.41"}


def test_customer_ingress_accepts_operational_loopback_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Nginx /live and /ready locations use a distinct loopback XFF so
    same-host monitoring does not look like an already-rewritten ASGI peer."""
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv("VIDEO_REPLICA_PUBLIC_ORIGIN", "https://app.example.test")
    monkeypatch.setenv("VIDEO_REPLICA_TRUSTED_PROXY_CIDRS", "127.0.0.1/32")

    response = _customer_ingress_get(
        _customer_ingress_app(),
        peer="127.0.0.1",
        headers=[
            ("Host", "app.example.test"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-For", "127.0.0.2"),
        ],
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"client_ip": "127.0.0.2"}


def test_customer_ingress_rejects_untrusted_peer_even_with_spoofed_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_customer_ingress(monkeypatch)
    response = _customer_ingress_get(
        _customer_ingress_app(),
        peer="203.0.113.99",
        headers=[
            ("Host", "app.example.test"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-For", "198.51.100.7"),
        ],
    )

    assert response.status_code == 403
    assert response.json()["code"] == "UNTRUSTED_PROXY"


def test_customer_ingress_rejects_already_rewritten_asgi_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed if an outer Uvicorn proxy middleware rewrote the raw peer."""
    _configure_customer_ingress(monkeypatch)
    app = ProxyHeadersMiddleware(_customer_ingress_app(), trusted_hosts="*")

    response = _customer_ingress_get(
        app,
        peer="203.0.113.99",
        headers=[
            ("Host", "app.example.test"),
            ("X-Forwarded-Proto", "https"),
            ("X-Forwarded-For", "10.20.0.8"),
        ],
    )

    assert response.status_code == 503
    assert response.json()["code"] == "PROXY_HEADER_REWRITE_DETECTED"


@pytest.mark.parametrize(
    "forwarded_for",
    [None, "not-an-ip", "203.0.113.1, 198.51.100.2"],
)
def test_customer_ingress_rejects_missing_malformed_or_chained_forwarded_ip(
    monkeypatch: pytest.MonkeyPatch,
    forwarded_for: str | None,
) -> None:
    _configure_customer_ingress(monkeypatch)
    headers = [
        ("Host", "app.example.test"),
        ("X-Forwarded-Proto", "https"),
    ]
    if forwarded_for is not None:
        headers.append(("X-Forwarded-For", forwarded_for))

    response = _customer_ingress_get(
        _customer_ingress_app(),
        peer="10.20.0.8",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "FORWARDED_CLIENT_INVALID"


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        (
            [
                ("Host", "evil.example.test"),
                ("X-Forwarded-Proto", "https"),
                ("X-Forwarded-For", "203.0.113.41"),
            ],
            421,
            "HOST_NOT_ALLOWED",
        ),
        (
            [
                ("Host", "app.example.test"),
                ("X-Forwarded-Proto", "http"),
                ("X-Forwarded-For", "203.0.113.41"),
            ],
            400,
            "HTTPS_REQUIRED",
        ),
    ],
)
def test_customer_ingress_rejects_wrong_host_or_forwarded_scheme(
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[str, str]],
    status: int,
    code: str,
) -> None:
    _configure_customer_ingress(monkeypatch)
    response = _customer_ingress_get(
        _customer_ingress_app(),
        peer="10.20.0.8",
        headers=headers,
    )

    assert response.status_code == status
    assert response.json()["code"] == code


def test_customer_public_origin_joins_exact_cors_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import _cors_origins

    _configure_customer_ingress(monkeypatch)

    origins = _cors_origins()
    assert "https://app.example.test" in origins
    assert "https://*.example.test" not in origins


def test_bucket_key_joins_dimension_and_identifier() -> None:
    from app.security_rate_limit import bucket_key

    key = bucket_key("activate:ip", "203.0.113.9")
    assert key == "activate:ip|203.0.113.9"
    assert bucket_key("login:account", "digest-abc") == "login:account|digest-abc"
    assert bucket_key("activate:ip", "a") != bucket_key("activate:ip", "b")


def test_fencing_audit_dimension_cannot_be_spent_as_a_rate_limit_bucket() -> None:
    from app.security_rate_limit import DIMENSION_SESSION_FENCING, consume_rate_limit

    with pytest.raises(ValueError, match="unknown rate-limit dimension"):
        consume_rate_limit(
            object(),  # type: ignore[arg-type]
            dimension=DIMENSION_SESSION_FENCING,
            identifier="digest",
            limit=1,
            window_seconds=60,
        )


def test_rate_limit_env_overrides_and_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.security_rate_limit import (
        DEFAULT_ACTIVATE_CODE_LIMIT,
        DEFAULT_ACTIVATE_IP_LIMIT,
        DEFAULT_FAILURE_ALERT_THRESHOLD,
        DEFAULT_WINDOW_SECONDS,
        activation_code_limit,
        activation_ip_limit,
        failure_alert_threshold,
        rate_limit_window_seconds,
    )

    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "3")
    assert activation_ip_limit() == 3
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "2")
    assert activation_code_limit() == 2
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "60")
    assert rate_limit_window_seconds() == 60
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_FAILURE_ALERT_THRESHOLD", "7")
    assert failure_alert_threshold() == 7

    # Non-numeric or non-positive values fall back to the safe defaults —
    # a typo in the environment must never disable the limits.
    for name in (
        "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP",
        "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE",
        "VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS",
        "VIDEO_REPLICA_RATE_LIMIT_FAILURE_ALERT_THRESHOLD",
    ):
        monkeypatch.setenv(name, "not-a-number")
    assert activation_ip_limit() == DEFAULT_ACTIVATE_IP_LIMIT
    assert activation_code_limit() == DEFAULT_ACTIVATE_CODE_LIMIT
    assert rate_limit_window_seconds() == DEFAULT_WINDOW_SECONDS
    assert failure_alert_threshold() == DEFAULT_FAILURE_ALERT_THRESHOLD
    for name in (
        "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP",
        "VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE",
        "VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS",
        "VIDEO_REPLICA_RATE_LIMIT_FAILURE_ALERT_THRESHOLD",
    ):
        monkeypatch.setenv(name, "0")
    assert activation_ip_limit() == DEFAULT_ACTIVATE_IP_LIMIT
    assert activation_code_limit() == DEFAULT_ACTIVATE_CODE_LIMIT
    assert rate_limit_window_seconds() == DEFAULT_WINDOW_SECONDS
    assert failure_alert_threshold() == DEFAULT_FAILURE_ALERT_THRESHOLD


def test_anti_enumeration_delay_costs_a_constant_baseline() -> None:
    """The unified rejection must burn measurable constant work (timing)."""
    from app.security_rate_limit import apply_anti_enumeration_delay

    started = time.perf_counter()
    apply_anti_enumeration_delay()
    first = time.perf_counter() - started
    started = time.perf_counter()
    apply_anti_enumeration_delay()
    second = time.perf_counter() - started
    # Both invocations clear a small floor and stay within a generous
    # ceiling: the point is a constant cost profile, not exact duration.
    assert first >= 0.002, f"delay too cheap to mask timing: {first}"
    assert second >= 0.002, f"delay too cheap to mask timing: {second}"
    assert first < 2.0 and second < 2.0


# ---------------------------------------------------------------------------
# PostgreSQL integration (dedicated migrated fixture database)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def security_dsn() -> Iterator[str]:
    from alembic import command
    from alembic.config import Config

    require_pg_or_explicit_skip(_pg_dsn())
    with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{T15_DB_NAME}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{T15_DB_NAME}"')
    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t15_dsn().replace("postgresql://", "postgresql+psycopg://")
    )
    command.upgrade(config, "head")
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
    try:
        yield _t15_dsn()
    finally:
        with psycopg.connect(_admin_dsn(), autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{T15_DB_NAME}" WITH (FORCE)')


@pytest.fixture()
def clean_counters(security_dsn: str) -> Iterator[str]:
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        # Counters are a cache (plain TRUNCATE), but the failures table is
        # append-only: 036 refuses its TRUNCATE, so the cleanup suspends
        # triggers via the replica role.
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {COUNTERS_TABLE}")
        conn.execute(f"TRUNCATE {FAILURES_TABLE}")
        conn.execute("SET session_replication_role = DEFAULT")
    yield security_dsn


def test_consume_window_allows_then_blocks(clean_counters: str) -> None:
    from app.security_rate_limit import consume_rate_limit

    with psycopg.connect(_t15_dsn()) as conn:
        for i in range(3):
            decision = consume_rate_limit(
                conn,
                dimension="activate:ip",
                identifier="198.51.100.7",
                limit=3,
                window_seconds=300,
            )
            assert decision.allowed, f"request {i + 1} must pass under the limit"
            assert decision.retry_after_seconds == 0
        blocked = consume_rate_limit(
            conn,
            dimension="activate:ip",
            identifier="198.51.100.7",
            limit=3,
            window_seconds=300,
        )
        assert not blocked.allowed
        assert blocked.retry_after_seconds > 0
        assert blocked.retry_after_seconds <= 300


def test_window_resets_after_expiry(clean_counters: str) -> None:
    from app.security_rate_limit import consume_rate_limit

    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        # Burn the whole budget inside a window that started long ago.
        past = now - timedelta(seconds=1000)
        for _ in range(3):
            decision = consume_rate_limit(
                conn,
                dimension="activate:ip",
                identifier="198.51.100.8",
                limit=3,
                window_seconds=300,
                now=past,
            )
            assert decision.allowed
        expired = consume_rate_limit(
            conn,
            dimension="activate:ip",
            identifier="198.51.100.8",
            limit=3,
            window_seconds=300,
            now=past,
        )
        assert not expired.allowed
        # The same bucket in the present must start a fresh window.
        fresh = consume_rate_limit(
            conn,
            dimension="activate:ip",
            identifier="198.51.100.8",
            limit=3,
            window_seconds=300,
            now=now,
        )
        assert fresh.allowed


def test_stale_counter_rows_are_purged_active_window_kept(
    clean_counters: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2 review M3: every identifier that ever attempted activation keeps
    its counter row forever without a sweep. Lapsed window rows must be
    deletable by the maintenance CLI while the still-active window keeps
    enforcing (counters are cache, not audit — the failure table carries
    the audit trail)."""
    from datetime import UTC, datetime, timedelta

    from app.security_rate_limit import (
        count_stale_counters,
        purge_stale_counters,
        rate_limit_window_seconds,
    )

    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "60")
    window = rate_limit_window_seconds()
    now = datetime.now(UTC)
    stale_start = (now - timedelta(seconds=2 * window)).replace(microsecond=0).isoformat()
    active_start = now.replace(microsecond=0).isoformat()
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
            "VALUES (%s, %s, 5, %s)",
            ("activate:ip|stale", stale_start, stale_start),
        )
        conn.execute(
            f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
            "VALUES (%s, %s, 1, %s)",
            ("activate:ip|active", active_start, active_start),
        )

        assert count_stale_counters(conn, now=now) == 1
        assert purge_stale_counters(conn, now=now) == 1
        rows = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                f"SELECT bucket_key, hit_count FROM {COUNTERS_TABLE}"
            ).fetchall()
        }
    assert rows == {"activate:ip|active": 1}, "only the active window row survives"


def test_dimensions_are_independent(clean_counters: str) -> None:
    from app.security_rate_limit import consume_rate_limit

    with psycopg.connect(_t15_dsn()) as conn:
        for _ in range(3):
            assert consume_rate_limit(
                conn,
                dimension="activate:code",
                identifier="digest-shared",
                limit=3,
                window_seconds=300,
            ).allowed
        assert not consume_rate_limit(
            conn,
            dimension="activate:code",
            identifier="digest-shared",
            limit=3,
            window_seconds=300,
        ).allowed
        # The IP dimension is untouched by the exhausted code dimension...
        assert consume_rate_limit(
            conn,
            dimension="activate:ip",
            identifier="198.51.100.9",
            limit=3,
            window_seconds=300,
        ).allowed
        # ...and a different code identifier has its own budget.
        assert consume_rate_limit(
            conn,
            dimension="activate:code",
            identifier="digest-other",
            limit=3,
            window_seconds=300,
        ).allowed


def test_counter_shared_across_connections(clean_counters: str) -> None:
    """ACT-08 red line: two API instances share one budget in PG.

    Two independent pooled connections alternate the consumption for one
    bucket; with an in-process limiter each connection would own a private
    counter and the pair would allow 6 hits — the shared PG counter must
    block the 4th.

    Both connections are autocommit: each consumption is one complete
    transaction whose row lock releases on return, exactly like the
    production ``pg_transaction()`` scope. Without it the two open
    transactions would deadlock on the bucket's row lock.
    """
    from app.security_rate_limit import consume_rate_limit

    first = psycopg.connect(_t15_dsn(), autocommit=True)
    second = psycopg.connect(_t15_dsn(), autocommit=True)
    try:
        # Three consumptions alternate across the two connections (two API
        # instances) and all pass — the budget is shared, not per-connection.
        for index, conn in enumerate((first, second, first)):
            decision = consume_rate_limit(
                conn,
                dimension="login:account",
                identifier="user-42",
                limit=3,
                window_seconds=300,
            )
            assert decision.allowed, f"hit {index + 1} must share the budget"
        # The 4th hit — from the other connection — draws from the same
        # exhausted budget: an in-process limiter would have allowed it.
        blocked = consume_rate_limit(
            second,
            dimension="login:account",
            identifier="user-42",
            limit=3,
            window_seconds=300,
        )
        assert not blocked.allowed
    finally:
        first.close()
        second.close()


def test_record_failure_and_metrics(clean_counters: str) -> None:
    from app.security_rate_limit import failure_metrics, record_auth_failure

    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        for i in range(4):
            record_auth_failure(
                conn,
                dimension="activate:code",
                identifier="digest-abc",
                request_id=f"req-{i}",
                now=now - timedelta(seconds=i * 10),
            )
        assert failure_metrics(conn, dimension="activate:code", window_seconds=300, now=now) == 4
        assert failure_metrics(conn, dimension="activate:ip", window_seconds=300, now=now) == 0


def test_failure_metrics_window_scoping(clean_counters: str) -> None:
    from app.security_rate_limit import failure_metrics, record_auth_failure

    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        record_auth_failure(
            conn,
            dimension="login:account",
            identifier="user-7",
            request_id="req-old",
            now=now - timedelta(seconds=1000),
        )
        record_auth_failure(
            conn,
            dimension="login:account",
            identifier="user-7",
            request_id="req-new",
            now=now - timedelta(seconds=30),
        )
        assert failure_metrics(conn, dimension="login:account", window_seconds=300, now=now) == 1


def test_failure_alert_threshold(clean_counters: str) -> None:
    from app.security_rate_limit import failure_alert_active, record_auth_failure

    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        for i in range(3):
            record_auth_failure(
                conn,
                dimension="activate:ip",
                identifier="203.0.113.77",
                request_id=f"req-{i}",
                now=now - timedelta(seconds=i),
            )
        assert not failure_alert_active(
            conn, dimension="activate:ip", window_seconds=300, threshold=5, now=now
        )
        for i in range(3, 5):
            record_auth_failure(
                conn,
                dimension="activate:ip",
                identifier="203.0.113.77",
                request_id=f"req-{i}",
                now=now - timedelta(seconds=i),
            )
        assert failure_alert_active(
            conn, dimension="activate:ip", window_seconds=300, threshold=5, now=now
        )


def test_failure_record_holds_no_plaintext_code(clean_counters: str) -> None:
    """The code-dimension identifier must be a digest, never the code."""
    from app.security_rate_limit import record_auth_failure

    plaintext = "XS04-AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD"
    digest = compute_code_digest(plaintext, key=TEST_KEY.encode("utf-8"))
    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        record_auth_failure(
            conn,
            dimension="activate:code",
            identifier=digest,
            request_id="req-1",
            now=now,
        )
        row = conn.execute(
            f"SELECT identifier FROM {FAILURES_TABLE} WHERE request_id = 'req-1'"
        ).fetchone()
        assert row is not None
        assert plaintext not in str(row[0])
        assert str(row[0]) == digest


def test_fencing_failure_audit_is_deduplicated_within_the_window(
    clean_counters: str,
) -> None:
    from app.security_rate_limit import record_auth_failure

    now = datetime.now(UTC).replace(microsecond=0)
    with psycopg.connect(_t15_dsn()) as conn:
        for index in range(5):
            record_auth_failure(
                conn,
                dimension="session:fencing",
                identifier="same-session-fencing-fact",
                request_id=f"fence-{index}",
                now=now + timedelta(seconds=index),
                dedupe_window_seconds=300,
            )
        count = conn.execute(
            f"SELECT count(*) FROM {FAILURES_TABLE} "
            "WHERE dimension = 'session:fencing' "
            "AND identifier = 'same-session-fencing-fact'"
        ).fetchone()

    assert count is not None and int(count[0]) == 1


# ---------------------------------------------------------------------------
# Route integration: the activation endpoint behind the shared limiter
# ---------------------------------------------------------------------------


@pytest.fixture()
def route_state(security_dsn: str) -> Iterator[str]:
    close_pg_pool()
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        # 036 refuses TRUNCATE of the append-only audit tables; the replica
        # role suspends triggers for this cleanup sweep only.
        conn.execute("SET session_replication_role = replica")
        conn.execute(
            "TRUNCATE customer_session_events, customer_session_state, "
            "customer_idempotency_envelopes, "
            "customer_devices, activation_code_events, activation_code_activations, "
            "activation_code_deliveries, activation_code_exports, activation_codes, "
            "activation_code_batches, admin_write_idempotency, admin_sessions, "
            "wallet_transactions, recharge_orders, wallets, users, "
            f"{COUNTERS_TABLE}, {FAILURES_TABLE} CASCADE"
        )
        conn.execute("SET session_replication_role = DEFAULT")
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_u', 'admin_u', 'Admin User', 'admin')"
        )
    yield security_dsn
    close_pg_pool()


@pytest.fixture()
def customer_app(monkeypatch: pytest.MonkeyPatch, route_state: str) -> Iterator[FastAPI]:
    from app.activation_code_routes import router as activation_code_router

    app = FastAPI()
    app.include_router(activation_code_router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    # Small budgets so the 429 path is reachable within a couple of calls.
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "2")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "300")
    # The anti-enumeration delay is monkeypatched to a no-op in the fast
    # route cases; its constant-cost property has its own unit test above.
    monkeypatch.setattr("app.activation_code_routes.apply_anti_enumeration_delay", lambda: None)
    yield app


@pytest.fixture()
def client(customer_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(customer_app) as test_client:
        yield test_client


def _activate_body(code: str = UNKNOWN_CODE, fingerprint: str = "fp-t15-1") -> dict[str, str]:
    return {
        "activation_code": code,
        "device_fingerprint": fingerprint,
        "device_name": "Test Device",
        "device_platform": "windows",
    }


def _activate(client: TestClient, code: str, fingerprint: str, key_suffix: str) -> object:
    return client.post(
        ACTIVATE_PATH,
        json=_activate_body(code, fingerprint),
        headers={IDEMPOTENCY_KEY_HEADER: f"idem-{key_suffix}"},
    )


def test_activate_returns_429_with_retry_after(client: TestClient) -> None:
    # Two failed attempts exhaust the IP budget of 2 (the TestClient host
    # is one fixed client address for every call). The code is well-formed
    # but unknown, so each attempt walks the full path — limiter, main
    # transaction, unified rejection — and actually spends the budget.
    for suffix in ("a", "b"):
        response = _activate(client, UNKNOWN_CODE, f"fp-{suffix}", suffix)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"
    blocked = _activate(client, UNKNOWN_CODE, "fp-c", "c")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"
    retry_after = blocked.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit()
    assert 0 < int(retry_after) <= 300


def test_malformed_requests_share_the_ip_budget(client: TestClient) -> None:
    """A malformed-code burst must not skirt the IP-dimension limiter.

    Normalization failures have no code digest, but they still spend the
    IP budget — otherwise format probing would bypass the abuse control
    entirely (each request below would early-exit before the limiter).
    """
    for suffix in ("m1", "m2"):
        response = _activate(client, MALFORMED_CODE, f"fp-malformed-{suffix}", suffix)
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "ACTIVATION_UNAVAILABLE"
    blocked = _activate(client, MALFORMED_CODE, "fp-malformed-3", "m3")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"


def test_activate_code_dimension_blocks_code_burst(customer_app: FastAPI) -> None:
    """One code hammered from distinct addresses hits its own budget.

    Each request below arrives from its own client address, so the IP
    dimension never trips (budget 2 per address, one hit each): only the
    shared code-dimension counter can produce the third 429 — which is
    exactly the multi-instance guarantee the red line demands.
    """
    addresses = ["203.0.113.10", "203.0.113.11", "203.0.113.12"]
    for index, address in enumerate(addresses):
        with TestClient(customer_app, client=(address, 51200 + index)) as scoped:
            response = _activate(scoped, BURST_CODE, f"fp-code-{index}", f"code-{index}")
            expected = 429 if index == 2 else 400
            assert response.status_code == expected, (
                f"request {index + 1} from {address}: expected {expected}, "
                f"got {response.status_code} {response.json()}"
            )


def test_unified_rejection_records_failure(client: TestClient) -> None:
    for suffix in ("a",):
        response = _activate(client, UNKNOWN_CODE, f"fp-fail-{suffix}", suffix)
        assert response.status_code == 400
    with psycopg.connect(_t15_dsn()) as conn:
        rows = conn.execute(f"SELECT dimension, identifier FROM {FAILURES_TABLE}").fetchall()
    assert rows, "the unified rejection must leave a failure event"
    code_rows = [row for row in rows if row[0] == "activate:code"]
    assert code_rows, "the code dimension must be recorded"
    for row in code_rows:
        assert "XS04" not in str(row[1])


def test_unified_rejection_applies_anti_enumeration_delay(
    monkeypatch: pytest.MonkeyPatch, route_state: str
) -> None:
    """Every unified-rejection path pays the constant delay (timing side channel)."""
    calls: list[float] = []

    def _fake_delay() -> None:
        calls.append(time.perf_counter())

    from app import activation_code_routes

    monkeypatch.setattr(activation_code_routes, "apply_anti_enumeration_delay", _fake_delay)
    app = FastAPI()
    app.include_router(activation_code_routes.router)
    monkeypatch.setenv(DATABASE_URL_ENV, route_state)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(ACTIVATION_CODE_HMAC_KEY_ENV, TEST_KEY)
    monkeypatch.setenv("VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY", TEST_FINGERPRINT_KEY)
    monkeypatch.setenv(
        "VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY",
        base64.urlsafe_b64encode(TEST_ENVELOPE_AEAD_KEY).decode("ascii").rstrip("="),
    )
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP", "10")
    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE", "10")

    with TestClient(app) as test_client:
        # Unknown (well-formed) code.
        first = _activate(test_client, UNKNOWN_ZZ_CODE, "fp-delay-1", "d1")
        assert first.status_code == 400
        # Malformed code (normalization failure).
        second = _activate(test_client, MALFORMED_CODE, "fp-delay-2", "d2")
        assert second.status_code == 400
        # An expired batch: seed a code whose batch already expired.
        with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO activation_code_batches "
                "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
                "quantity, activation_expires_at, status, created_by_user_id, created_at) "
                "VALUES ('batch-exp', 'expired', 1500, 1000, 100, 1, "
                "'2020-01-01T00:00:00+00:00', 'OPEN', 'admin_u', "
                "'2019-01-01T00:00:00+00:00')"
            )
            expired_digest = compute_code_digest(EXPIRED_CODE, key=TEST_KEY.encode("utf-8"))
            conn.execute(
                "INSERT INTO activation_codes "
                "(id, batch_id, code_digest, digest_key_version, masked_code, "
                "status, issued_at) "
                "VALUES ('code-exp', 'batch-exp', %s, 1, 'XS04-****', "
                "'ISSUED', '2019-06-01T00:00:00+00:00')",
                (expired_digest,),
            )
        third = _activate(test_client, EXPIRED_CODE, "fp-delay-3", "d3")
        assert third.status_code == 400

    assert len(calls) == 3, (
        "unknown, malformed and expired codes must all run the constant "
        f"anti-enumeration delay (got {len(calls)})"
    )


def test_idempotent_replay_spends_no_rate_limit_budget(client: TestClient) -> None:
    """Session review P2: a legitimate idempotent retry replays without paying.

    The client lost the response of an already-successful activation and
    retries with the same Idempotency-Key: the sealed envelope answers the
    replay (201 + X-Idempotent-Replay) *before* the shared limiter runs,
    so no rate-limit budget is spent. With IP and code budgets of 2, three
    replays after one successful activation leave exactly one IP hit for
    a fresh request, and the one after that trips the limiter — proving
    the replays above drew nothing from the shared budget.
    """
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            "quantity, activation_expires_at, status, created_by_user_id) "
            "VALUES ('batch-replay', 'replay', 1500, 1000, 100, 1, "
            f"'{FUTURE_EXPIRY}', 'OPEN', 'admin_u')"
        )
        digest = compute_code_digest(BURST_CODE, key=TEST_KEY.encode("utf-8"))
        conn.execute(
            "INSERT INTO activation_codes "
            "(id, batch_id, code_digest, digest_key_version, masked_code, "
            "status, issued_at) "
            "VALUES ('code-replay', 'batch-replay', %s, 1, 'XS04-****', "
            "'ISSUED', '2026-01-01T00:00:00+00:00')",
            (digest,),
        )

    # One successful activation spends exactly one IP and one code hit.
    first = _activate(client, BURST_CODE, "fp-replay", "replay")
    assert first.status_code == 201, first.text
    assert "username" in first.json()
    assert "X-Idempotent-Replay" not in first.headers

    # Same key, same body: the sealed envelope answers, zero budget spent.
    for _ in range(3):
        replay = _activate(client, BURST_CODE, "fp-replay", "replay")
        assert replay.status_code == 201, replay.text
        assert replay.headers.get("X-Idempotent-Replay") == "true"

    # One IP hit remains out of the budget of 2: a fresh request passes
    # (and answers the unified 400 for an unknown code)…
    fresh = _activate(client, UNKNOWN_CODE, "fp-fresh", "fresh")
    assert fresh.status_code == 400
    # …and the next one trips the shared limiter.
    blocked = _activate(client, UNKNOWN_CODE, "fp-blocked", "blocked")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"


def test_blocked_ip_mints_no_code_counter_rows(client: TestClient) -> None:
    """PR #46 review P1: a blocked IP must stop minting code-dimension rows.

    The code-dimension identifier is attacker-controlled (a random
    well-shaped code's digest) and expired counter rows are never swept, so
    consuming the code dimension after the IP dimension already blocked
    would grow ``security_rate_limit_counters`` without bound — one
    permanent row per request from a client that is already dead.
    """
    # Two distinct unknown well-shaped codes burn the IP budget of 2; each
    # legitimately spends one code-dimension hit (one row each).
    for suffix, code in (("p1a", UNKNOWN_CODE), ("p1b", BURST_CODE)):
        response = _activate(client, code, f"fp-{suffix}", suffix)
        assert response.status_code == 400
    # The third request is IP-blocked: it must answer 429 *without*
    # creating a third code-dimension counter row.
    blocked = _activate(client, UNKNOWN_ZZ_CODE, "fp-p1c", "p1c")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "RATE_LIMITED"
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        row = conn.execute(
            f"SELECT count(*) FROM {COUNTERS_TABLE} WHERE bucket_key LIKE 'activate:code|%'"
        ).fetchone()
    assert row is not None and row[0] == 2, (
        "an IP-blocked request must not mint a new code-dimension counter row "
        f"(found {row[0]} rows; the two live rows belong to the pre-block requests)"
    )


def test_cors_exposes_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR #46 review P2: browser JS must be able to read the 429 backoff hint.

    CORS hides unlisted response headers from JavaScript; without
    ``Retry-After`` in ``expose_headers`` the WebView/Tauri client cannot
    honour the documented backoff after a 429 and retries blind against
    the shared PG-backed budget.
    """
    from app.main import app as main_app

    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(
        DATABASE_URL_ENV,
        os.environ.get(
            "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
        ),
    )
    with TestClient(main_app) as main_client:
        # A validation failure still passes through the CORS middleware, so
        # the status does not matter — only the readable header contract.
        actual = main_client.post(
            ACTIVATE_PATH,
            headers={
                "Origin": "http://localhost:5173",
                IDEMPOTENCY_KEY_HEADER: "key-cors-p2",
            },
        )
        assert actual.status_code == 422, actual.text
    exposed = actual.headers.get("access-control-expose-headers", "").lower()
    assert "retry-after" in exposed, (
        "Retry-After must be CORS-exposed for the browser client to back off"
    )


def test_downgrade_refuses_once_failures_exist(security_dsn: str) -> None:
    """Session review P3: the 032 downgrade guards the audit trail.

    ``security_auth_failures`` is append-only audit evidence — once any
    event exists, the downgrade must refuse loudly (026/028 guard
    precedent) instead of dropping the table and destroying the record.
    Runs last in this module: it cycles the shared fixture database
    through a downgrade/upgrade round-trip.
    """
    from alembic import command
    from alembic.config import Config

    server_dir = Path(__file__).resolve().parent.parent
    config = Config(str(server_dir / "alembic.ini"))
    config.set_main_option("script_location", str(server_dir / "migrations"))
    config.set_main_option(
        "sqlalchemy.url", _t15_dsn().replace("postgresql://", "postgresql+psycopg://")
    )

    # One audited failure event is enough to block the downgrade.
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        conn.execute(
            f"INSERT INTO {FAILURES_TABLE} "
            "(id, dimension, identifier, request_id, occurred_at) "
            "VALUES ('fail-guard', 'activate:code', 'digest-guard', 'req-guard', "
            "'2026-08-23T00:00:00+00:00')"
        )
    with pytest.raises(RuntimeError, match="cannot downgrade 032_security_rate_limits"):
        command.downgrade(config, "029_customer_sessions_and_idempotency")

    # The refusal left the schema untouched at head.
    with psycopg.connect(_t15_dsn()) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _head_revision()

    # TRUNCATE only bypasses the row-level append-only trigger (it fires on
    # UPDATE/DELETE); 036 added a statement-level TRUNCATE guard, so the
    # cleanup suspends triggers via the replica role. With an empty audit
    # trail the downgrade is symmetric, and upgrading back restores the
    # security tables.
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        conn.execute("SET session_replication_role = replica")
        conn.execute(f"TRUNCATE {FAILURES_TABLE}")
        conn.execute("SET session_replication_role = DEFAULT")
    command.downgrade(config, "029_customer_sessions_and_idempotency")
    with psycopg.connect(_t15_dsn()) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        }
    assert version == "029_customer_sessions_and_idempotency"
    assert COUNTERS_TABLE not in tables
    assert FAILURES_TABLE not in tables

    command.upgrade(config, "head")
    with psycopg.connect(_t15_dsn()) as conn:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert version == _head_revision()


# ---------------------------------------------------------------------------
# Maintenance sweep CLI (scripts/purge_stale_rate_limit_counters.py, M2 M3)
# ---------------------------------------------------------------------------


def test_cli_sweep_uses_postgres_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep's cutoff must come from ``SELECT now()`` on the opened
    connection, never the maintenance host's clock: a host clock ahead of
    PostgreSQL would delete counters that ``consume_rate_limit()`` still
    considers active, bypassing the shared abuse budget (Codex P2)."""
    import scripts.purge_stale_rate_limit_counters as purge_cli

    pg_now = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)  # far from the host clock

    class _Cursor:
        def fetchone(self) -> tuple[datetime]:
            return (pg_now,)

    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def execute(self, _sql: str) -> _Cursor:
            return _Cursor()

        def transaction(self) -> nullcontext[None]:
            return nullcontext()

    captured: dict[str, object] = {}

    def _fake_count(conn: object, *, now: datetime) -> int:
        captured["now"] = now
        return 0

    monkeypatch.setattr(purge_cli.psycopg, "connect", lambda _dsn: _FakeConn())
    monkeypatch.setattr(purge_cli, "count_stale_counters", _fake_count)
    assert purge_cli.main(["--database-url", "postgresql://u:p@host/db", "--dry-run"]) == 0
    assert captured["now"] == pg_now, "sweep cutoff must be the PostgreSQL clock"


def test_cli_purges_stale_rows_active_window_kept(
    clean_counters: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the CLI deletes only fully lapsed window rows, keeping the
    still-active window's counter so the next request cannot mint a fresh
    bucket (counters are cache, not audit)."""
    from scripts.purge_stale_rate_limit_counters import main as purge_main

    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "60")
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        now = conn.execute("SELECT now()").fetchone()[0]
        stale_start = (now - timedelta(seconds=120)).replace(microsecond=0).isoformat()
        active_start = now.replace(microsecond=0).isoformat()
        conn.execute(
            f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
            "VALUES (%s, %s, 5, %s)",
            ("activate:ip|cli-stale", stale_start, stale_start),
        )
        conn.execute(
            f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
            "VALUES (%s, %s, 1, %s)",
            ("activate:ip|cli-active", active_start, active_start),
        )

    assert purge_main(["--database-url", _t15_dsn()]) == 0

    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        rows = {
            str(row[0]): int(row[1])
            for row in conn.execute(
                f"SELECT bucket_key, hit_count FROM {COUNTERS_TABLE}"
            ).fetchall()
        }
    assert rows == {"activate:ip|cli-active": 1}, "only the active window row survives"


def test_cli_dry_run_reports_without_deleting(
    clean_counters: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.purge_stale_rate_limit_counters import main as purge_main

    monkeypatch.setenv("VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS", "60")
    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        now = conn.execute("SELECT now()").fetchone()[0]
        stale_start = (now - timedelta(seconds=120)).replace(microsecond=0).isoformat()
        conn.execute(
            f"INSERT INTO {COUNTERS_TABLE} (bucket_key, window_start, hit_count, updated_at) "
            "VALUES (%s, %s, 5, %s)",
            ("activate:ip|cli-dry", stale_start, stale_start),
        )

    assert purge_main(["--database-url", _t15_dsn(), "--dry-run"]) == 0

    with psycopg.connect(_t15_dsn(), autocommit=True) as conn:
        hit_count = conn.execute(
            f"SELECT hit_count FROM {COUNTERS_TABLE} WHERE bucket_key = %s",
            ("activate:ip|cli-dry",),
        ).fetchone()[0]
    assert hit_count == 5


def test_cli_requires_a_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.purge_stale_rate_limit_counters import main as purge_main

    monkeypatch.delenv("VIDEO_REPLICA_DATABASE_URL", raising=False)
    assert purge_main([]) == 1
