"""CW-010: oral digital-human domain recovery baseline on real PostgreSQL.

Migrated off the ``tmp_path`` SQLite lane onto a dedicated TEST-PG database with
an *independent multi-connection* baseline: every logical write/read block runs
in its own :func:`pg_transaction` (a separate pooled connection), so per-block
commit/rollback isolation, the oral clone/task lifecycle, the RESERVE/SETTLE/
RELEASE wallet deltas, the worker claim/lease/crash/timeout/SUBMISSION_UNCERTAIN
recovery paths, and the HTTP route contract are all asserted against real
PostgreSQL — never SQLite, never a mock-persisted store, never a missing-PG skip
(the CW-007 ``require_pg_or_explicit_skip`` hard gate fails closed when the
fixture is unreachable).

Vendor transport stays scripted and storage stays stubbed at the module seam
(``app.oral.storage_for_asset`` / ``app.oral.get_media_storage``): CW-010 allows
a controlled Provider double (real paid Provider calls are CW-050), so clone and
task flows exercise submission, polling, archival, and billing without network
or real buckets. The *database* is the only thing that must be real.

Migration findings (SQLite → PostgreSQL):

1. Placeholder syntax — the PG backend runs SQL verbatim (no ``?`` → ``%s``
   translation), so every direct-SQL statement in this module uses ``%s``.
2. Multi-connection isolation — the SQLite lane held one ``conn`` for a whole
   test; here each service call, raw read, and worker phase opens its own
   :func:`pg_transaction`. ``run_worker_once`` (SQLite, conn-bound) is replaced
   by ``run_pg_worker_once`` (keyword-only, pooled) exactly as the production PG
   worker loop drives it.
3. Deferred denial audit — on PG ``permissions._raise_denial_with_audit`` raises
   :class:`AuditedSecurityDenial` and rolls the business transaction back
   *without* an inline audit row; the fact is committed afterwards by
   :func:`persist_security_denial`. Service-level denial tests therefore persist
   the denial explicitly, and the route doubles mirror the production
   ``fenced_pg_transaction`` catch → persist → re-raise so both the 403 body and
   the durable ``security.role_denied`` audit survive.
4. Fair-queue cursor — ``claim_oral_work`` on PG takes a user queue slot
   (``user_queue_cursors.running_tasks_count``), so the scene seeds a cursor row
   per employee; without it a QUEUED oral claim fails closed.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Set the audit HMAC key before importing app modules (audit writers require it).
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-cw010-oral-domain-tests-minimum-48-bytes-long-1234",
)

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

from app.auth import CurrentUser, get_current_user
from app.character_image_generation import deterministic_png
from app.customer_fence import get_business_db
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.generation_worker import run_pg_worker_once
from app.hifly import HiflyClient, HiflyError
from app.internal_billing import (
    BillingInvariantError,
    finalize_oral_billing,
    reconcile_dangling_billing_reservations,
    reconcile_oral_billing_by_evidence,
)
from app.main import app
from app.media_tools import resolve_media_binary
from app.oral import (
    ORAL_CONSENT_TEXT_VERSION,
    OralConflictError,
    OralDomainError,
    OralResourceInUseError,
    OralResourceNotFoundError,
    cancel_oral_task,
    confirm_voice_clone,
    create_oral_consent,
    create_oral_task,
    delete_avatar_clone,
    delete_voice_clone,
    list_avatars,
    list_oral_consents,
    list_voices,
    oral_price_quote,
    oral_unit_price_fen,
    read_avatar_clone,
    read_voice_clone,
    refresh_avatar_clone,
    refresh_oral_task,
    refresh_voice_clone,
    start_avatar_clone,
    start_voice_clone,
)
from app.oral_routes import get_oral_vendor
from app.oral_worker import (
    OralLeaseLostError,
    OralWorkKind,
    OralWorkLease,
    OralWorkResult,
    claim_oral_work,
    discard_uncommitted_oral_asset,
    finalize_oral_work,
    perform_oral_work,
    prepare_oral_work,
    request_oral_archive_retry,
)
from app.permissions import AuditedSecurityDenial, persist_security_denial
from app.storage import StoredObject

CW010_ORAL_DB_NAME = "cw010_oral_test"

# Every table the oral scene touches. TRUNCATE runs under
# ``session_replication_role = replica`` with CASCADE so FK order is irrelevant;
# ``generation_tasks``/``generation_batches`` are included because
# ``wallet_transactions`` FK-references the generation ledger on PG.
_ORAL_TABLES = (
    "oral_tasks, oral_avatars, oral_voices, oral_consents, person_identities, "
    "user_queue_cursors, wallet_transactions, generation_tasks, generation_batches, "
    "assets, projects, wallets, runtime_settings, audit_logs, users"
)


@dataclass(frozen=True)
class OralTestMedia:
    image: bytes
    audio: bytes
    video: bytes


class FakeSourceStorage:
    def __init__(self, media: OralTestMedia) -> None:
        self.media = media
        self.objects: dict[str, bytes] = {}

    def get_object(self, key: str) -> bytes:
        if key in self.objects:
            return self.objects[key]
        if key.endswith(".png"):
            return self.media.image
        if key.endswith(".mp3"):
            return self.media.audio
        return self.media.video

    def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
        self.objects[key] = content
        return StoredObject(
            provider="fake",
            bucket="assets",
            key=key,
            uri=f"fake://assets/{key}",
            size=len(content),
            content_type=content_type,
            sha256=f"sha-{key}",
            updated_at=datetime.now(tz=UTC),
        )

    def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
        self.objects.pop(key, None)


@pytest.fixture(scope="session")
def oral_test_media(tmp_path_factory: pytest.TempPathFactory) -> OralTestMedia:
    directory = tmp_path_factory.mktemp("oral-media")
    audio_path = directory / "voice.mp3"
    video_path = directory / "avatar.mp4"
    ffmpeg = resolve_media_binary("ffmpeg")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=16000:cl=mono",
            "-t",
            "6",
            "-codec:a",
            "mp3",
            str(audio_path),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=10",
            "-t",
            "1",
            "-codec:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    return OralTestMedia(
        image=deterministic_png(b"oral-source"),
        audio=audio_path.read_bytes(),
        video=video_path.read_bytes(),
    )


@pytest.fixture()
def fake_source_storage(
    monkeypatch: pytest.MonkeyPatch,
    oral_test_media: OralTestMedia,
) -> FakeSourceStorage:
    storage = FakeSourceStorage(oral_test_media)
    monkeypatch.setattr("app.oral.storage_for_asset", lambda _conn, _uri: storage)
    return storage


def actor(user_id: str = "employee_1", role: str = "employee") -> CurrentUser:
    return CurrentUser(id=user_id, username=user_id, display_name=user_id, role=role)


class ScriptedVendorTransport:
    """Routes vendor calls by (method, url-prefix) with canned JSON bodies."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], bytes | Callable[[bytes | None], bytes]] = {}
        self.calls: list[tuple[str, str]] = []
        self.put_bodies: list[bytes] = []

    def on(
        self, method: str, prefix: str, responder: bytes | Callable[[bytes | None], bytes]
    ) -> None:
        self.routes[(method, prefix)] = responder

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> bytes:
        self.calls.append((method, url))
        if method == "PUT":
            self.put_bodies.append(body or b"")
            return b""
        path = url.split("?", 1)[0]
        for (route_method, prefix), responder in self.routes.items():
            if route_method == method and path.endswith(prefix):
                return responder(body) if callable(responder) else responder
        raise AssertionError(f"unexpected vendor call {method} {url}")


def envelope(data: dict[str, Any]) -> bytes:
    return json.dumps({"code": 0, "msg": "", "data": data}).encode()


def make_vendor() -> tuple[HiflyClient, ScriptedVendorTransport]:
    transport = ScriptedVendorTransport()
    return HiflyClient(api_key="test-key", transport=transport), transport


# --------------------------------------------------------------------------- #
# TEST-PG scaffolding: a dedicated migrated database + independent-connection
# helpers. Every logical block below opens its own ``pg_transaction`` (its own
# pooled connection), which is the multi-connection baseline CW-010 requires.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def oral_dsn() -> Iterator[str]:
    """A dedicated migrated PG database for the oral domain baseline.

    Created through the CW-007 kit helpers so ``assert_safe_test_database``
    guards the ``DROP ... WITH (FORCE)`` against the allowlisted name
    (registered in ``pg_test_kit.RECORDED_TEST_DATABASES``) and the kit resolves
    the admin DSN — no hand-rolled DSN concatenation lives in this file.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW010_ORAL_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW010_ORAL_DB_NAME)


@pytest.fixture()
def scene(oral_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the app PG pool at the oral database and re-seed for one test."""
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, oral_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    _seed_oral(oral_dsn)
    try:
        yield oral_dsn
    finally:
        close_pg_pool()


def _executemany(pg: psycopg.Connection, sql: str, rows: list[tuple[object, ...]]) -> None:
    """psycopg3 exposes ``executemany`` on the cursor, not the connection."""
    with pg.cursor() as cursor:
        cursor.executemany(sql, rows)


def _seed_oral(dsn: str) -> None:
    """TRUNCATE and re-seed the oral scene on real PostgreSQL.

    Raw autocommit connection (not the app pool): mirrors the wallet and
    independent baselines. Seeds the fair-queue cursor rows ``claim_oral_work``
    needs on PG and ``runtime_settings`` id=1 that the shared generation-capacity
    lock reads.
    """
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("UPDATE customer_credit_pricing SET version = 0, config_json = NULL")
        pg.execute("SET session_replication_role = replica")
        pg.execute(f"TRUNCATE {_ORAL_TABLES} CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")
        # Most lifecycle tests use an explicit low per-second tariff; absence is tested separately.
        pg.execute("TRUNCATE billing_tariffs")
        pg.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('oral',true,0.1,0.05)"
        )
        pg.execute(
            "UPDATE customer_credit_pricing SET version=1,config_json=%s",
            ('{"points_per_yuan":100}',),
        )
        pg.execute(
            "INSERT INTO runtime_settings "
            "(id, max_generation_count_per_batch, max_concurrent_h3_tasks, "
            " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen, "
            " fair_queue_enabled) "
            "VALUES (1, 4, 100, 1000, 10000, 1000, true)"
        )
        _executemany(
            pg,
            "INSERT INTO users (id, username, display_name, role) VALUES (%s, %s, %s, %s)",
            [
                ("employee_1", "employee_1", "Employee", "employee"),
                ("employee_2", "employee_2", "Other Employee", "employee"),
                ("admin_1", "admin_1", "Administrator", "admin"),
                ("auditor_1", "auditor_1", "Auditor", "auditor"),
            ],
        )
        _executemany(
            pg,
            "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES (%s, %s, 0)",
            [("employee_1", 20), ("employee_2", 20)],
        )
        pg.execute(
            "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
            ("oral-project", "employee_1", "Oral Project"),
        )
        pg.execute(
            "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
            "VALUES ('ident-1', 'employee_1', '张工', 'ACTIVE')"
        )
        _executemany(
            pg,
            "INSERT INTO user_queue_cursors (user_id, last_dispatched_at, running_tasks_count) "
            "VALUES (%s, now(), 0)",
            [("employee_1",), ("employee_2",)],
        )
        pg.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id"
            ") VALUES ("
            " 'asset-src', 'oral-project', 'source_video', 'local://assets/src.mp4',"
            " 'video-hash', 9, 'video/mp4', 'employee_1')"
        )
        pg.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id"
            ") VALUES ("
            " 'asset-image', 'oral-project', 'character_source_image',"
            " 'local://assets/src.png', 'image-hash', 9, 'image/png', 'employee_1')"
        )
        pg.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, metadata_json, created_by_user_id"
            ") VALUES ("
            " 'asset-audio', NULL, 'oral_audio', 'local://assets/v.mp3',"
            " 'audio-hash', 9, 'audio/mpeg',"
            ' \'{"audio_purpose":"voice_clone","duration_seconds":42,'
            "\"audio_duration_verified\":true}', 'employee_1')"
        )


# --------------------------------------------------------------------------- #
# Raw SQL helpers — each its own transaction/connection. ``_fetch``/``_fetchall``
# go through ``BusinessConnection.postgres`` so rows are ``_NamedRow`` (support
# both positional ``row[0]`` and named ``row["col"]`` access, as the assertions
# expect).
# --------------------------------------------------------------------------- #


def _exec(sql: str, params: tuple[object, ...] = ()) -> None:
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(sql, params)


def _fetch(sql: str, params: tuple[object, ...] = ()) -> Any:
    with pg_transaction() as raw:
        return BusinessConnection.postgres(raw).execute(sql, params).fetchone()


def _fetchall(sql: str, params: tuple[object, ...] = ()) -> list[Any]:
    with pg_transaction() as raw:
        return BusinessConnection.postgres(raw).execute(sql, params).fetchall()


def _count(sql: str, params: tuple[object, ...] = ()) -> int:
    row = _fetch(sql, params)
    assert row is not None
    return int(row[0])


def _wallet(user_id: str = "employee_1") -> tuple[int, int]:
    row = _fetch(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s",
        (user_id,),
    )
    assert row is not None
    return int(row["available_credits"]), int(row["reserved_credits"])


def _ledger(oral_task_id: str) -> list[str]:
    rows = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (oral_task_id,),
    )
    return [str(row["type"]) for row in rows]


# --------------------------------------------------------------------------- #
# Service wrappers — each opens one ``pg_transaction`` (one pooled connection),
# calls the oral service on a ``BusinessConnection.postgres`` facade, and lets
# any ``AuditedSecurityDenial``/domain error propagate (the transaction rolls
# back on the way out, exactly as the production route dependency does).
# --------------------------------------------------------------------------- #


def _create_consent(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return create_oral_consent(BusinessConnection.postgres(raw), **kwargs)


def _list_consents(**kwargs: Any) -> list[dict[str, Any]]:
    with pg_transaction() as raw:
        return list_oral_consents(BusinessConnection.postgres(raw), **kwargs)


def _start_avatar(**kwargs: Any) -> Any:
    with pg_transaction() as raw:
        return start_avatar_clone(BusinessConnection.postgres(raw), **kwargs)


def _start_voice(**kwargs: Any) -> Any:
    with pg_transaction() as raw:
        return start_voice_clone(BusinessConnection.postgres(raw), **kwargs)


def _create_task(**kwargs: Any) -> Any:
    with pg_transaction() as raw:
        return create_oral_task(BusinessConnection.postgres(raw), **kwargs)


def _cancel_task(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return cancel_oral_task(BusinessConnection.postgres(raw), **kwargs)


def _refresh_task(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return refresh_oral_task(BusinessConnection.postgres(raw), **kwargs)


def _refresh_avatar(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return refresh_avatar_clone(BusinessConnection.postgres(raw), **kwargs)


def _refresh_voice(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return refresh_voice_clone(BusinessConnection.postgres(raw), **kwargs)


def _confirm_voice(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return confirm_voice_clone(BusinessConnection.postgres(raw), **kwargs)


def _list_avatars(**kwargs: Any) -> list[dict[str, Any]]:
    with pg_transaction() as raw:
        return list_avatars(BusinessConnection.postgres(raw), **kwargs)


def _read_avatar(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return read_avatar_clone(BusinessConnection.postgres(raw), **kwargs)


def _delete_avatar(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return delete_avatar_clone(BusinessConnection.postgres(raw), **kwargs)


def _list_voices(**kwargs: Any) -> list[dict[str, Any]]:
    with pg_transaction() as raw:
        return list_voices(BusinessConnection.postgres(raw), **kwargs)


def _read_voice(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return read_voice_clone(BusinessConnection.postgres(raw), **kwargs)


def _delete_voice(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return delete_voice_clone(BusinessConnection.postgres(raw), **kwargs)


def _oral_unit_price() -> int:
    with pg_transaction() as raw:
        return oral_unit_price_fen(BusinessConnection.postgres(raw))


def _price_quote() -> dict[str, Any]:
    with pg_transaction() as raw:
        return oral_price_quote(BusinessConnection.postgres(raw))


def _finalize_billing(**kwargs: Any) -> Any:
    with pg_transaction() as raw:
        return finalize_oral_billing(BusinessConnection.postgres(raw), **kwargs)


def _reconcile_dangling() -> Any:
    with pg_transaction() as raw:
        return reconcile_dangling_billing_reservations(BusinessConnection.postgres(raw))


def _reconcile_by_evidence(**kwargs: Any) -> Any:
    with pg_transaction() as raw:
        return reconcile_oral_billing_by_evidence(BusinessConnection.postgres(raw), **kwargs)


def _claim(worker_id: str, *, lease_seconds: int = 120) -> OralWorkLease | None:
    with pg_transaction() as raw:
        return claim_oral_work(
            BusinessConnection.postgres(raw), worker_id=worker_id, lease_seconds=lease_seconds
        )


def _prepare(lease: OralWorkLease) -> OralWorkLease:
    with pg_transaction() as raw:
        return prepare_oral_work(BusinessConnection.postgres(raw), lease)


def _finalize_work(*, lease: OralWorkLease, result: OralWorkResult) -> None:
    with pg_transaction() as raw:
        finalize_oral_work(BusinessConnection.postgres(raw), lease=lease, result=result)


def _request_archive_retry(**kwargs: Any) -> dict[str, Any]:
    with pg_transaction() as raw:
        return request_oral_archive_retry(BusinessConnection.postgres(raw), **kwargs)


def _run_oral_worker_step(
    *,
    vendor: HiflyClient,
    storage: FakeSourceStorage,
    worker_id: str = "oral-test-worker",
) -> OralWorkResult | None:
    """One oral worker phase on independent PG connections.

    Mirrors ``run_pg_worker_once``'s oral branch: claim commits in its own
    fenced transaction, prepare in a second, the paid vendor call runs outside
    any transaction, and finalize commits in a third. Returns the
    :class:`OralWorkResult` (or ``None`` when nothing was claimable) so tests can
    assert ``result.outcome`` exactly as the SQLite helper did.
    """
    with pg_transaction() as raw:
        lease = claim_oral_work(BusinessConnection.postgres(raw), worker_id=worker_id)
    if lease is None:
        return None
    with pg_transaction() as raw:
        prepared = prepare_oral_work(BusinessConnection.postgres(raw), lease)
    result = perform_oral_work(prepared, vendor=vendor, storage=storage)
    with pg_transaction() as raw:
        finalize_oral_work(BusinessConnection.postgres(raw), lease=prepared, result=result)
    return result


def _run_pg_worker(
    *,
    worker_id: str,
    storage: FakeSourceStorage,
    vendor: HiflyClient | None = None,
    max_tasks: int | None = None,
) -> int:
    """Drive the production PG worker loop (``run_pg_worker_once``)."""
    return run_pg_worker_once(
        worker_id=worker_id, storage=storage, oral_vendor=vendor, max_tasks=max_tasks
    )


def _consent_for(*, purpose: str, source_asset_id: str) -> str:
    return str(
        _create_consent(
            actor=actor(),
            identity_id="ident-1",
            source_asset_id=source_asset_id,
            purpose=purpose,
            consent_text_version=ORAL_CONSENT_TEXT_VERSION,
        )["id"]
    )


def _seed_ready_assets() -> tuple[str, str]:
    """Seed an already-READY avatar + confirmed voice pair (own transactions)."""
    avatar_consent = _consent_for(purpose="AVATAR_CLONE", source_asset_id="asset-src")
    voice_consent = _consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio")
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO assets ("
            " id, project_id, kind, storage_uri, sha256, size_bytes,"
            " content_type, created_by_user_id"
            ") VALUES ("
            " 'voice-demo-ready', NULL, 'oral_audio', 'local://assets/demo.mp3',"
            " 'demo-hash', 9, 'audio/mpeg', 'employee_1')"
        )
        conn.execute(
            "INSERT INTO oral_avatars ("
            " id, identity_id, owner_user_id, title, vendor_avatar_id,"
            " status, source_kind, source_asset_id, consent_id"
            ") VALUES ("
            " 'avatar-ready', 'ident-1', 'employee_1', '张工分身',"
            " 'vendor-avatar-9', 'READY', 'VIDEO', 'asset-src', %s)",
            (avatar_consent,),
        )
        conn.execute(
            "INSERT INTO oral_voices ("
            " id, identity_id, owner_user_id, title, vendor_voice_id,"
            " status, source_asset_id, consent_id, confirmed, demo_asset_id"
            ") VALUES ("
            " 'voice-ready', 'ident-1', 'employee_1', '张工声音',"
            " 'vendor-voice-9', 'READY', 'asset-audio', %s, 1, 'voice-demo-ready')",
            (voice_consent,),
        )
    return "avatar-ready", "voice-ready"


# --------------------------------------------------------------------------- #
# Route doubles. The oral *write* routes ride the session fence, which always
# yields a ``customer`` actor, so the route tests inject a chosen actor
# (employee / auditor / admin) by overriding ``get_business_db`` with a double
# that still persists to *real* PostgreSQL via ``pg_transaction`` and mirrors the
# production ``fenced_pg_transaction`` handling of ``AuditedSecurityDenial``
# (catch → persist → re-raise) so both the public 403 and the durable denial
# audit survive on PG. The *read* routes are deliberately NOT doubled: they ride
# the real ``auth.get_database`` PG branch (a pooled ``pg_transaction`` against
# the test DSN) and the real ``get_current_user`` dev-header path, so production
# read plumbing stays under test instead of being masked by a byte-copy.
# --------------------------------------------------------------------------- #


class _PgBusinessDb:
    """A ``BusinessDb`` double whose ``write()`` runs on a real PG transaction."""

    def __init__(self, current_actor: CurrentUser) -> None:
        self.current_actor = current_actor

    @contextmanager
    def write(self, *, isolation: Any = None) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        try:
            with pg_transaction() as raw:
                yield BusinessConnection.postgres(raw), self.current_actor
        except AuditedSecurityDenial as exc:
            persist_security_denial(exc)
            raise


def _read_actor_override(user_id: str = "employee_1", role: str = "employee") -> None:
    """Pin the read-owner dependency to a test actor.

    CW-026 removed the X-Dev-User-Id identity on the converged PG lane, so
    route-level tests that used to rely on it now seed the actor through the
    get_current_user dependency instead (auth itself is covered by
    test_cw026_converged_auth.py).
    """
    app.dependency_overrides[get_current_user] = lambda: actor(user_id, role)


def _make_business_db_override(
    current_actor: CurrentUser,
) -> tuple[Callable[[], _PgBusinessDb], _PgBusinessDb]:
    """Return ``(dependency_override, holder)``; mutate ``holder.current_actor``
    mid-test to change the acting user for subsequent requests."""
    holder = _PgBusinessDb(current_actor)
    return (lambda: holder), holder


def test_create_and_list_consent_snapshots_source_and_audits(scene: str) -> None:
    created = _create_consent(
        actor=actor(),
        identity_id="ident-1",
        source_asset_id="asset-audio",
        purpose="VOICE_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )

    assert created["owner_user_id"] == "employee_1"
    assert created["source_sha256"] == "audio-hash"
    assert created["purpose"] == "VOICE_CLONE"
    assert created["consented_at"]
    assert _list_consents(actor=actor(), identity_id="ident-1") == [created]
    audit = _fetch(
        "SELECT action, entity_id, metadata_json FROM audit_logs WHERE entity_id = %s",
        (created["id"],),
    )
    assert audit is not None
    assert audit["action"] == "oral.consent.create"
    assert json.loads(str(audit["metadata_json"])) == {
        "consent_text_version": ORAL_CONSENT_TEXT_VERSION,
        "identity_id": "ident-1",
        "purpose": "VOICE_CLONE",
        "source_asset_id": "asset-audio",
        "source_sha256": "audio-hash",
    }


def test_consent_read_keeps_foreign_and_missing_identity_indistinguishable(scene: str) -> None:
    with pytest.raises(OralDomainError) as foreign:
        _list_consents(actor=actor("employee_2"), identity_id="ident-1")
    with pytest.raises(OralDomainError) as missing:
        _list_consents(actor=actor("employee_2"), identity_id="missing")

    assert str(foreign.value) == str(missing.value)


def test_auditor_is_denied_from_every_oral_write_service(scene: str) -> None:
    vendor, _ = make_vendor()
    auditor = actor("auditor_1", "auditor")
    operations: list[tuple[str, Callable[[], object]]] = [
        (
            "oral.consent.create",
            lambda: _create_consent(
                actor=auditor,
                identity_id="ident-1",
                source_asset_id="asset-audio",
                purpose="VOICE_CLONE",
                consent_text_version=ORAL_CONSENT_TEXT_VERSION,
            ),
        ),
        (
            "oral.avatar.create",
            lambda: _start_avatar(
                actor=auditor,
                identity_id="ident-1",
                title="auditor avatar",
                source_asset_id="asset-image",
                source_kind="IMAGE",
                consent_id="missing",
                idempotency_key="auditor-avatar",
            ),
        ),
        (
            "oral.voice.create",
            lambda: _start_voice(
                actor=auditor,
                identity_id="ident-1",
                title="auditor voice",
                source_asset_id="asset-audio",
                consent_id="missing",
                idempotency_key="auditor-voice",
            ),
        ),
        (
            "oral.task.create",
            lambda: _create_task(
                actor=auditor,
                identity_id="ident-1",
                avatar_id="missing",
                voice_id=None,
                mode="AUDIO",
                title="auditor oral",
                script_text=None,
                audio_asset_id="asset-audio",
                subtitle=None,
                idempotency_key="auditor-oral-task",
            ),
        ),
        ("oral.task.cancel", lambda: _cancel_task(task_id="missing", actor=auditor)),
        (
            "oral.task.refresh",
            lambda: _refresh_task(task_id="missing", actor=auditor, vendor=vendor),
        ),
        (
            "oral.avatar.refresh",
            lambda: _refresh_avatar(avatar_id="missing", actor=auditor, vendor=vendor),
        ),
        (
            "oral.voice.refresh",
            lambda: _refresh_voice(voice_id="missing", actor=auditor, vendor=vendor),
        ),
        ("oral.voice.confirm", lambda: _confirm_voice(voice_id="missing", actor=auditor)),
    ]

    for _expected_action, operation in operations:
        with pytest.raises(HTTPException) as denied:
            operation()
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "ROLE_FORBIDDEN"
        # PG defers the denial audit: the business transaction rolled back, so
        # commit the fact separately exactly as the production dependency does.
        persist_security_denial(denied.value)  # type: ignore[arg-type]
    audited_actions = {
        json.loads(str(row["metadata_json"]))["attempted_action"]
        for row in _fetchall(
            """
            SELECT metadata_json FROM audit_logs
            WHERE actor_user_id = 'auditor_1' AND action = 'security.role_denied'
            """
        )
    }
    assert audited_actions == {action for action, _operation in operations}


def test_auditor_cannot_request_oral_archive_retry(scene: str) -> None:
    db_override, _holder = _make_business_db_override(actor("auditor_1", "auditor"))
    app.dependency_overrides[get_business_db] = db_override
    try:
        response = TestClient(app).post("/api/oral/tasks/missing/archive-retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "ROLE_FORBIDDEN"
    audit = _fetch(
        """
        SELECT metadata_json FROM audit_logs
        WHERE actor_user_id = 'auditor_1' AND action = 'security.role_denied'
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    assert audit is not None
    assert json.loads(str(audit["metadata_json"]))["attempted_action"] == (
        "oral.task.archive_retry"
    )


def test_auditor_cannot_write_oral_biometrics_even_as_resource_owner(scene: str) -> None:
    owner = actor()
    auditor = actor(role="auditor")
    avatar_consent = _create_consent(
        actor=owner,
        identity_id="ident-1",
        source_asset_id="asset-src",
        purpose="AVATAR_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )
    voice_consent = _create_consent(
        actor=owner,
        identity_id="ident-1",
        source_asset_id="asset-audio",
        purpose="VOICE_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )
    _exec(
        "INSERT INTO oral_voices ("
        "id, identity_id, owner_user_id, title, status, source_asset_id, consent_id"
        ") VALUES ('auditor-owned-ready', 'ident-1', 'employee_1', '待确认声音', "
        "'READY', 'asset-audio', %s)",
        (voice_consent["id"],),
    )

    operations = [
        lambda: _create_consent(
            actor=auditor,
            identity_id="ident-1",
            source_asset_id="asset-audio",
            purpose="VOICE_CLONE",
            consent_text_version=ORAL_CONSENT_TEXT_VERSION,
        ),
        lambda: _start_avatar(
            actor=auditor,
            identity_id="ident-1",
            title="审计员分身",
            source_asset_id="asset-src",
            source_kind="VIDEO",
            consent_id=str(avatar_consent["id"]),
            idempotency_key="auditor-avatar-write",
        ),
        lambda: _start_voice(
            actor=auditor,
            identity_id="ident-1",
            title="审计员声音",
            source_asset_id="asset-audio",
            consent_id=str(voice_consent["id"]),
            idempotency_key="auditor-voice-write",
        ),
        lambda: _confirm_voice(voice_id="auditor-owned-ready", actor=auditor),
    ]

    for operation in operations:
        with pytest.raises(HTTPException) as denied:
            operation()
        assert denied.value.status_code == 403
        assert denied.value.detail["code"] == "ROLE_FORBIDDEN"
        persist_security_denial(denied.value)  # type: ignore[arg-type]

    # ``persist_security_denial`` swallows write failures (logger.warning), so the
    # durable denial audit must be asserted, not assumed. The actor is employee_1
    # wearing the auditor role (the "even as resource owner" case), so the four
    # denial rows carry actor_user_id='employee_1' (audit_logs is scene-truncated).
    audited_actions = {
        json.loads(str(row["metadata_json"]))["attempted_action"]
        for row in _fetchall(
            """
            SELECT metadata_json FROM audit_logs
            WHERE actor_user_id = 'employee_1' AND action = 'security.role_denied'
            """
        )
    }
    assert audited_actions == {
        "oral.consent.create",
        "oral.avatar.create",
        "oral.voice.create",
        "oral.voice.confirm",
    }

    assert _count("SELECT COUNT(*) FROM oral_consents WHERE owner_user_id = 'employee_1'") == 2
    assert (
        _count("SELECT COUNT(*) FROM oral_avatars WHERE idempotency_key = 'auditor-avatar-write'")
        == 0
    )
    assert (
        _count("SELECT COUNT(*) FROM oral_voices WHERE idempotency_key = 'auditor-voice-write'")
        == 0
    )
    confirmed_row = _fetch("SELECT confirmed FROM oral_voices WHERE id = 'auditor-owned-ready'")
    assert confirmed_row is not None
    assert int(confirmed_row[0]) == 0


def test_customer_role_can_start_owned_oral_biometric_writes(scene: str) -> None:
    customer = actor(role="customer")

    avatar_consent = _create_consent(
        actor=customer,
        identity_id="ident-1",
        source_asset_id="asset-src",
        purpose="AVATAR_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )
    voice_consent = _create_consent(
        actor=customer,
        identity_id="ident-1",
        source_asset_id="asset-audio",
        purpose="VOICE_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )
    avatar = _start_avatar(
        actor=customer,
        identity_id="ident-1",
        title="客户分身",
        source_asset_id="asset-src",
        source_kind="VIDEO",
        consent_id=str(avatar_consent["id"]),
        idempotency_key="customer-avatar-write",
    )
    voice = _start_voice(
        actor=customer,
        identity_id="ident-1",
        title="客户声音",
        source_asset_id="asset-audio",
        consent_id=str(voice_consent["id"]),
        idempotency_key="customer-voice-write",
    )

    assert avatar.status == "PENDING"
    assert voice.status == "PENDING"
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        conn.execute(
            "INSERT INTO assets ("
            "id, project_id, kind, storage_uri, sha256, size_bytes, content_type, "
            "created_by_user_id"
            ") VALUES ('customer-voice-demo', NULL, 'oral_audio', "
            "'local://assets/customer-voice-demo.mp3', 'demo-hash', 8, 'audio/mpeg', "
            "'employee_1')"
        )
        conn.execute(
            "UPDATE oral_voices SET status = 'READY', demo_asset_id = 'customer-voice-demo' "
            "WHERE id = %s",
            (voice.task_id,),
        )
    confirmed = _confirm_voice(voice_id=voice.task_id, actor=customer)
    assert confirmed["confirmed"] == 1


def test_consent_and_voice_confirmation_routes(scene: str) -> None:
    vendor, _ = make_vendor()
    db_override, _holder = _make_business_db_override(actor())

    app.dependency_overrides[get_current_user] = actor
    app.dependency_overrides[get_business_db] = db_override
    app.dependency_overrides[get_oral_vendor] = lambda: vendor
    try:
        client = TestClient(app)
        created = client.post(
            "/api/oral/consents",
            json={
                "identity_id": "ident-1",
                "source_asset_id": "asset-audio",
                "purpose": "VOICE",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["purpose"] == "VOICE_CLONE"

        listed = client.get("/api/oral/consents", params={"identity_id": "ident-1"})
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()] == [created.json()["id"]]

        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id
                ) VALUES ('voice-route-demo', NULL, 'oral_audio',
                          'local://assets/route-demo.mp3', 'route-demo-hash', 9,
                          'audio/mpeg', 'employee_1')
                """
            )
            conn.execute(
                """
                INSERT INTO oral_voices (
                    id, identity_id, owner_user_id, title, vendor_voice_id,
                    status, source_asset_id, consent_id, confirmed, demo_asset_id
                ) VALUES (%s, 'ident-1', 'employee_1', '待确认声音', 'vendor-voice',
                          'READY', 'asset-audio', %s, 0, 'voice-route-demo')
                """,
                ("voice-route", created.json()["id"]),
            )
        confirmed = client.post("/api/oral/voices/voice-route/confirm")
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["confirmed"] == 1
        assert confirmed.json()["confirmed_by_user_id"] == "employee_1"
    finally:
        app.dependency_overrides.clear()


def test_delete_avatar_clone_soft_deletes_and_hides_from_list(scene: str) -> None:
    avatar_id, _ = _seed_ready_assets()
    assert avatar_id in [row["id"] for row in _list_avatars(actor=actor(), identity_id="ident-1")]

    result = _delete_avatar(avatar_id=avatar_id, actor=actor())
    assert result["id"] == avatar_id
    assert result["deleted_at"]

    # Row is retained (oral_tasks.avatar_id is ON DELETE RESTRICT); markers set.
    row = _fetch(
        "SELECT status, deleted_at, deleted_by_user_id FROM oral_avatars WHERE id = %s",
        (avatar_id,),
    )
    assert row is not None
    assert row["status"] == "READY"
    assert row["deleted_at"]
    assert row["deleted_by_user_id"] == "employee_1"

    # Hidden from the owner's clone list and unreadable by id.
    remaining = _list_avatars(actor=actor(), identity_id="ident-1")
    assert avatar_id not in [item["id"] for item in remaining]
    with pytest.raises(OralDomainError):
        _read_avatar(avatar_id=avatar_id, actor=actor())

    audit = _fetch(
        "SELECT action, actor_user_id FROM audit_logs WHERE entity_id = %s", (avatar_id,)
    )
    assert audit is not None
    assert audit["action"] == "oral.avatar.delete"
    assert audit["actor_user_id"] == "employee_1"


def test_delete_voice_clone_retains_source_and_demo_assets(scene: str) -> None:
    _, voice_id = _seed_ready_assets()
    _delete_voice(voice_id=voice_id, actor=actor())
    # Soft delete hides the clone but never touches the underlying demo asset.
    remaining = _list_voices(actor=actor(), identity_id="ident-1")
    assert voice_id not in [item["id"] for item in remaining]
    retained = _count(
        "SELECT 1 FROM oral_voices WHERE id = %s AND deleted_at IS NOT NULL", (voice_id,)
    )
    assert retained == 1
    assert _count("SELECT 1 FROM assets WHERE id = 'voice-demo-ready'") == 1


def test_delete_avatar_clone_blocked_while_clone_in_flight(scene: str) -> None:
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(
            "INSERT INTO oral_avatars ("
            " id, identity_id, owner_user_id, title, status, source_kind,"
            " source_asset_id, submission_state"
            ") VALUES ("
            " 'avatar-running', 'ident-1', 'employee_1', '制作中', 'RUNNING', 'VIDEO',"
            " 'asset-src', 'SUBMITTING')"
        )
    with pytest.raises(OralResourceInUseError):
        _delete_avatar(avatar_id="avatar-running", actor=actor())
    row = _fetch("SELECT deleted_at FROM oral_avatars WHERE id = 'avatar-running'")
    assert row is not None and row["deleted_at"] is None


def test_delete_avatar_clone_blocked_by_active_task_until_terminal(scene: str) -> None:
    avatar_id, _ = _seed_ready_assets()
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(
            "INSERT INTO oral_tasks ("
            " id, owner_user_id, identity_id, avatar_id, mode, title,"
            " estimated_cost_fen, idempotency_key, status"
            ") VALUES ("
            " 'task-active', 'employee_1', 'ident-1', %s, 'TTS', '进行中', 100,"
            " 'idem-task-active', 'RUNNING')",
            (avatar_id,),
        )
    with pytest.raises(OralResourceInUseError):
        _delete_avatar(avatar_id=avatar_id, actor=actor())
    # Once the referencing task reaches a terminal state the clone is deletable.
    _exec("UPDATE oral_tasks SET status = 'SUCCEEDED' WHERE id = 'task-active'")
    _delete_avatar(avatar_id=avatar_id, actor=actor())
    deleted = _fetch("SELECT deleted_at FROM oral_avatars WHERE id = %s", (avatar_id,))
    assert deleted is not None and deleted["deleted_at"]


def test_delete_avatar_clone_scoped_to_owner(scene: str) -> None:
    # An avatar owned by someone else is invisible to employee_1's delete.
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(
            "INSERT INTO oral_avatars ("
            " id, identity_id, owner_user_id, title, status, source_kind,"
            " source_asset_id, submission_state"
            ") VALUES ("
            " 'avatar-other', 'ident-1', 'employee_2', '别人的', 'READY', 'VIDEO',"
            " 'asset-src', 'SUBMITTED')"
        )
    with pytest.raises(OralResourceNotFoundError):
        _delete_avatar(avatar_id="avatar-other", actor=actor())
    row = _fetch("SELECT deleted_at FROM oral_avatars WHERE id = 'avatar-other'")
    assert row is not None and row["deleted_at"] is None


def test_delete_avatar_and_voice_routes(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, _ = make_vendor()
    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_current_user] = actor
    app.dependency_overrides[get_business_db] = db_override
    app.dependency_overrides[get_oral_vendor] = lambda: vendor
    try:
        client = TestClient(app)
        avatar_resp = client.delete(f"/api/oral/avatars/{avatar_id}")
        assert avatar_resp.status_code == 200, avatar_resp.text
        voice_resp = client.delete(f"/api/oral/voices/{voice_id}")
        assert voice_resp.status_code == 200, voice_resp.text

        avatars = client.get("/api/oral/avatars", params={"identity_id": "ident-1"})
        assert avatar_id not in [row["id"] for row in avatars.json()]
        voices = client.get("/api/oral/voices", params={"identity_id": "ident-1"})
        assert voice_id not in [row["id"] for row in voices.json()]

        missing = client.delete("/api/oral/avatars/does-not-exist")
        assert missing.status_code == 404, missing.text
        assert missing.json()["detail"]["code"] == "ORAL_RESOURCE_NOT_FOUND"
    finally:
        app.dependency_overrides.clear()


def test_delete_avatar_route_conflict_when_in_use(scene: str) -> None:
    _seed_ready_assets()
    with pg_transaction() as raw:
        BusinessConnection.postgres(raw).execute(
            "INSERT INTO oral_avatars ("
            " id, identity_id, owner_user_id, title, status, source_kind,"
            " source_asset_id, submission_state"
            ") VALUES ("
            " 'avatar-busy', 'ident-1', 'employee_1', '制作中', 'PENDING', 'VIDEO',"
            " 'asset-src', 'LOCAL_PENDING')"
        )
    vendor, _ = make_vendor()
    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_current_user] = actor
    app.dependency_overrides[get_business_db] = db_override
    app.dependency_overrides[get_oral_vendor] = lambda: vendor
    try:
        client = TestClient(app)
        resp = client.delete("/api/oral/avatars/avatar-busy")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["code"] == "ORAL_RESOURCE_IN_USE"
    finally:
        app.dependency_overrides.clear()


def test_clone_route_only_enqueues_before_returning_202(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/avatar",
                "content_type": "video/mp4",
                "file_id": "file-avatar",
            }
        ),
    )

    def uncertain(_body: bytes | None) -> bytes:
        raise HiflyError("连接中断")

    transport.on("POST", "/api/v2/hifly/avatar/create_by_video", uncertain)

    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    app.dependency_overrides[get_oral_vendor] = lambda: vendor
    try:
        client = TestClient(app)
        consent = client.post(
            "/api/oral/consents",
            json={
                "identity_id": "ident-1",
                "source_asset_id": "asset-src",
                "purpose": "AVATAR",
            },
        )
        response = client.post(
            "/api/oral/avatars",
            json={
                "identity_id": "ident-1",
                "title": "张工分身",
                "source_asset_id": "asset-src",
                "source_kind": "VIDEO",
                "consent_id": consent.json()["id"],
                "idempotency_key": "route-uncertain-key",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["status"] == "PENDING"
    assert transport.calls == []
    persisted = _fetch(
        "SELECT submission_state FROM oral_avatars WHERE idempotency_key = %s",
        ("route-uncertain-key",),
    )
    assert persisted is not None
    assert persisted["submission_state"] == "LOCAL_PENDING"


def test_oral_task_route_returns_202_without_vendor_and_cancel_is_idempotent(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()

    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    try:
        client = TestClient(app)
        response = client.post(
            "/api/oral/tasks",
            json={
                "identity_id": "ident-1",
                "avatar_id": avatar_id,
                "voice_id": voice_id,
                "mode": "TTS",
                "title": "排队口播",
                "script_text": "文案",
                "audio_asset_id": None,
                "subtitle": None,
                "idempotency_key": "route-queue-key",
            },
        )
        first_cancel = client.post(f"/api/oral/tasks/{response.json()['id']}/cancel")
        replay_cancel = client.post(f"/api/oral/tasks/{response.json()['id']}/cancel")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert first_cancel.status_code == replay_cancel.status_code == 200
    assert first_cancel.json()["status"] == replay_cancel.json()["status"] == "CANCELLED"
    assert _wallet("employee_1") == (20, 0)


def test_avatar_clone_start_then_refresh_to_ready(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {"upload_url": "https://up.example/1", "content_type": "video/mp4", "file_id": "file-1"}
        ),
    )
    transport.on("POST", "/api/v2/hifly/avatar/create_by_video", envelope({"task_id": "vt-1"}))
    transport.on(
        "GET", "/api/v2/hifly/avatar/task", envelope({"status": 3, "avatar_id": "vendor-avatar-1"})
    )

    started = _start_avatar(
        actor=actor(),
        identity_id="ident-1",
        title="张工口播分身",
        source_asset_id="asset-src",
        source_kind="VIDEO",
        consent_id=_consent_for(purpose="AVATAR_CLONE", source_asset_id="asset-src"),
        idempotency_key="avatar-clone-idem-1",
        vendor=vendor,
    )
    assert started.status == "PENDING"
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    _exec("UPDATE oral_avatars SET next_attempt_at = NULL WHERE id = %s", (started.task_id,))
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    refreshed = _fetch("SELECT * FROM oral_avatars WHERE id = %s", (started.task_id,))
    assert refreshed["status"] == "READY"
    assert refreshed["vendor_avatar_id"] == "vendor-avatar-1"
    # 上传走 PUT；创建与查询各一次 POST/GET。
    assert any(method == "PUT" for method, _ in transport.calls)


def test_avatar_clone_from_image_uses_image_provider_endpoint(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/image",
                "content_type": "image/png",
                "file_id": "file-image",
            }
        ),
    )
    transport.on(
        "POST",
        "/api/v2/hifly/avatar/create_by_image",
        envelope({"task_id": "image-task-1"}),
    )

    started = _start_avatar(
        actor=actor(),
        identity_id="ident-1",
        title="张工照片分身",
        source_asset_id="asset-image",
        source_kind="IMAGE",
        consent_id=_consent_for(purpose="AVATAR_CLONE", source_asset_id="asset-image"),
        idempotency_key="avatar-image-idem-1",
        vendor=vendor,
    )

    assert started.status == "PENDING"
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    assert any(url.endswith("/avatar/create_by_image") for _, url in transport.calls)
    assert not any(url.endswith("/avatar/create_by_video") for _, url in transport.calls)


@pytest.mark.parametrize(
    ("operation", "asset_id", "source_kind", "message"),
    [
        ("avatar", "asset-audio", "VIDEO", "素材类型"),
        ("avatar", "asset-src", "IMAGE", "素材类型"),
        ("voice", "asset-src", None, "音频素材"),
    ],
)
def test_clone_rejects_wrong_media_type(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    operation: str,
    asset_id: str,
    source_kind: str | None,
    message: str,
) -> None:
    vendor, _ = make_vendor()

    with pytest.raises(OralDomainError, match=message):
        if operation == "avatar":
            _start_avatar(
                actor=actor(),
                identity_id="ident-1",
                title="错误素材",
                source_asset_id=asset_id,
                source_kind=str(source_kind),
                consent_id=_consent_for(purpose="AVATAR_CLONE", source_asset_id=asset_id),
                idempotency_key=f"wrong-avatar-{asset_id}-{source_kind}",
                vendor=vendor,
            )
        else:
            _start_voice(
                actor=actor(),
                identity_id="ident-1",
                title="错误素材",
                source_asset_id=asset_id,
                consent_id=_consent_for(purpose="VOICE_CLONE", source_asset_id=asset_id),
                idempotency_key=f"wrong-voice-{asset_id}",
                vendor=vendor,
            )


def test_clone_rejects_inaccessible_and_incomplete_assets(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    _exec(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("other-project", "employee_2", "Other Project"),
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            "other-video",
            "other-project",
            "source_video",
            "local://assets/other.mp4",
            "other-hash",
            9,
            "video/mp4",
            "employee_2",
        ),
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            "other-audio",
            "other-project",
            "oral_audio",
            "local://assets/other.mp3",
            "other-audio-hash",
            9,
            "audio/mpeg",
            "employee_2",
        ),
    )
    _exec("UPDATE assets SET sha256 = '', size_bytes = 0 WHERE id = 'asset-audio'")
    vendor, _ = make_vendor()

    with pytest.raises(HTTPException) as inaccessible:
        _start_avatar(
            actor=actor(),
            identity_id="ident-1",
            title="他人素材",
            source_asset_id="other-video",
            source_kind="VIDEO",
            consent_id="consent-hidden",
            idempotency_key="hidden-avatar-key",
            vendor=vendor,
        )
    assert inaccessible.value.status_code == 404
    with pytest.raises(HTTPException) as inaccessible_audio:
        _start_voice(
            actor=actor(),
            identity_id="ident-1",
            title="他人音频",
            source_asset_id="other-audio",
            consent_id="consent-hidden",
            idempotency_key="hidden-voice-key",
            vendor=vendor,
        )
    assert inaccessible_audio.value.status_code == 404
    with pytest.raises(OralDomainError, match="音频素材.*失效"):
        _start_voice(
            actor=actor(),
            identity_id="ident-1",
            title="未完成素材",
            source_asset_id="asset-audio",
            consent_id="consent-hidden",
            idempotency_key="incomplete-voice-key",
            vendor=vendor,
        )


@pytest.mark.parametrize(("role", "expected_status"), [("admin", 404), ("auditor", 403)])
def test_privileged_roles_cannot_clone_another_users_biometric_asset(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    role: str,
    expected_status: int,
) -> None:
    user_id = f"{role}_1"
    privileged = actor(user_id, role)
    _exec(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
        "VALUES (%s, %s, '特权用户人物', 'ACTIVE')",
        (f"ident-{role}", user_id),
    )
    vendor, transport = make_vendor()

    with pytest.raises(HTTPException) as hidden:
        _start_avatar(
            actor=privileged,
            identity_id=f"ident-{role}",
            title="他人素材",
            source_asset_id="asset-src",
            source_kind="VIDEO",
            consent_id="not-reachable",
            idempotency_key=f"privileged-{role}-clone",
            vendor=vendor,
        )

    assert hidden.value.status_code == expected_status
    assert transport.calls == []


def test_clone_rejects_consent_mismatch_before_vendor_call(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    voice_consent = _consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio")
    avatar_consent = _consent_for(purpose="AVATAR_CLONE", source_asset_id="asset-src")
    _exec(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
        "VALUES ('ident-owned-2', 'employee_1', '同用户其他人物', 'ACTIVE'), "
        "('ident-foreign', 'employee_2', '其他用户人物', 'ACTIVE')"
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES ('asset-foreign-audio', NULL, 'oral_audio', "
        "'local://assets/foreign.mp3', 'foreign-audio-hash', 9, "
        "'audio/mpeg', 'employee_2')"
    )
    identity_mismatch = _create_consent(
        actor=actor(),
        identity_id="ident-owned-2",
        source_asset_id="asset-src",
        purpose="AVATAR_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )["id"]
    owner_mismatch = _create_consent(
        actor=actor("employee_2"),
        identity_id="ident-foreign",
        source_asset_id="asset-foreign-audio",
        purpose="VOICE_CLONE",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )["id"]

    for consent_id, source_asset_id in [
        (voice_consent, "asset-src"),
        (avatar_consent, "asset-image"),
        ("missing-consent", "asset-src"),
    ]:
        with pytest.raises(OralDomainError, match="授权"):
            _start_avatar(
                actor=actor(),
                identity_id="ident-1",
                title="无效授权",
                source_asset_id=source_asset_id,
                source_kind="VIDEO" if source_asset_id == "asset-src" else "IMAGE",
                consent_id=consent_id,
                idempotency_key=f"mismatch-avatar-{consent_id}",
                vendor=vendor,
            )
    with pytest.raises(OralDomainError, match="授权"):
        _start_avatar(
            actor=actor(),
            identity_id="ident-1",
            title="身份不匹配",
            source_asset_id="asset-src",
            source_kind="VIDEO",
            consent_id=str(identity_mismatch),
            idempotency_key="identity-mismatch-key",
            vendor=vendor,
        )
    with pytest.raises(OralDomainError, match="授权"):
        _start_voice(
            actor=actor(),
            identity_id="ident-1",
            title="用户不匹配",
            source_asset_id="asset-audio",
            consent_id=str(owner_mismatch),
            idempotency_key="owner-mismatch-key",
            vendor=vendor,
        )
    _exec("UPDATE assets SET sha256 = 'changed-hash' WHERE id = 'asset-src'")
    with pytest.raises(OralDomainError, match="授权"):
        _start_avatar(
            actor=actor(),
            identity_id="ident-1",
            title="素材内容已变化",
            source_asset_id="asset-src",
            source_kind="VIDEO",
            consent_id=avatar_consent,
            idempotency_key="hash-mismatch-key",
            vendor=vendor,
        )
    assert transport.calls == []


def test_voice_clone_ready_requires_explicit_confirmation(
    scene: str,
    fake_source_storage: FakeSourceStorage,
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/2",
                "content_type": "audio/mpeg",
                "file_id": "file-2",
            }
        ),
    )
    transport.on("POST", "/api/v2/hifly/voice/create", envelope({"task_id": "vt-2"}))
    transport.on(
        "GET",
        "/api/v2/hifly/voice/task",
        envelope(
            {
                "status": 3,
                "voice": "vendor-voice-2",
                "demo_url": "https://tmp.example/voice-demo.mp3",
            }
        ),
    )
    transport.on(
        "GET",
        "https://tmp.example/voice-demo.mp3",
        fake_source_storage.media.audio,
    )

    started = _start_voice(
        actor=actor(),
        identity_id="ident-1",
        title="张工声音",
        source_asset_id="asset-audio",
        consent_id=_consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio"),
        idempotency_key="voice-clone-idem-1",
        vendor=vendor,
    )
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    _exec("UPDATE oral_voices SET next_attempt_at = NULL WHERE id = %s", (started.task_id,))
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    refreshed = _fetch("SELECT * FROM oral_voices WHERE id = %s", (started.task_id,))
    assert refreshed["status"] == "READY"
    assert refreshed["vendor_voice_id"] == "vendor-voice-2"
    assert refreshed["confirmed"] == 0
    assert refreshed["confirmed_by_user_id"] is None
    assert refreshed["confirmed_at"] is None

    confirmed = _confirm_voice(voice_id=started.task_id, actor=actor())
    assert confirmed["confirmed"] == 1
    assert confirmed["confirmed_by_user_id"] == "employee_1"
    assert confirmed["confirmed_at"]
    audit = _fetch("SELECT action FROM audit_logs WHERE entity_id = %s", (started.task_id,))
    assert audit is not None
    assert audit["action"] == "oral.voice.confirm"


def test_voice_clone_rejects_undecodable_audio_before_provider_submission(
    scene: str,
    fake_source_storage: FakeSourceStorage,
) -> None:
    vendor, transport = make_vendor()
    fake_source_storage.objects["v.mp3"] = b"ID3-not-a-decodable-audio-file"
    started = _start_voice(
        actor=actor(),
        identity_id="ident-1",
        title="伪音频",
        source_asset_id="asset-audio",
        consent_id=_consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio"),
        idempotency_key="voice-invalid-media-key",
        vendor=vendor,
    )

    result = _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)

    assert result is not None and result.outcome == "failed"
    persisted = _fetch(
        "SELECT status, submission_state, error_message FROM oral_voices WHERE id = %s",
        (started.task_id,),
    )
    assert tuple(persisted) == (
        "FAILED",
        "FAILED",
        "声音素材需为 5 至 180 秒的有效音频",
    )
    assert transport.calls == []


@pytest.mark.parametrize(
    ("suffix", "audio_codec", "with_video"),
    [
        (".wav", "pcm_s16le", False),
        (".m4a", "aac", False),
        (".wma", "wmav2", False),
        (".wmv", "wmav2", True),
    ],
)
def test_voice_clone_normalizes_common_audio_formats_before_provider_upload(
    tmp_path: Any,
    fake_source_storage: FakeSourceStorage,
    suffix: str,
    audio_codec: str,
    with_video: bool,
) -> None:
    media_path = tmp_path / f"voice{suffix}"
    command = [
        resolve_media_binary("ffmpeg"),
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=16000",
    ]
    if with_video:
        command.extend(["-f", "lavfi", "-i", "color=c=black:s=32x32:r=5"])
    command.extend(["-t", "6", "-c:a", audio_codec])
    if with_video:
        command.extend(["-c:v", "msmpeg4v3"])
    command.append(str(media_path))
    subprocess.run(command, check=True, capture_output=True)
    fake_source_storage.objects[f"voice{suffix}"] = media_path.read_bytes()
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/normalized",
                "content_type": "audio/mpeg",
                "file_id": "normalized-file",
            }
        ),
    )
    transport.on("POST", "/api/v2/hifly/voice/create", envelope({"task_id": "voice-task"}))

    result = perform_oral_work(
        OralWorkLease(
            kind="voice_submit",
            record_id=f"voice-{suffix[1:]}",
            worker_id="format-worker",
            lease_token="format-lease",
            attempt_count=1,
            row={
                "source_storage_uri": f"fake://assets/voice{suffix}",
                "title": "张工声音",
            },
        ),
        vendor=vendor,
        storage=fake_source_storage,
    )

    assert result.outcome == "submitted"
    assert len(transport.put_bodies) == 1
    uploaded = transport.put_bodies[0]
    assert uploaded != media_path.read_bytes() or suffix == ".mp3"
    normalized_path = tmp_path / "normalized.mp3"
    normalized_path.write_bytes(uploaded)
    probe = subprocess.run(
        [
            resolve_media_binary("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=nw=1:nk=1",
            str(normalized_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == "mp3"


def test_voice_clone_transcode_failure_never_calls_provider(
    fake_source_storage: FakeSourceStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.media_tools import MediaToolFailed

    vendor, transport = make_vendor()

    def fail_normalization(*_args: Any, **_kwargs: Any) -> bytes:
        raise MediaToolFailed("forced transcode failure")

    monkeypatch.setattr("app.oral_worker.normalize_audio_to_mp3", fail_normalization)
    result = perform_oral_work(
        OralWorkLease(
            kind="voice_submit",
            record_id="voice-transcode-failure",
            worker_id="format-worker",
            lease_token="format-lease",
            attempt_count=1,
            row={"source_storage_uri": "fake://assets/voice.mp3", "title": "张工声音"},
        ),
        vendor=vendor,
        storage=fake_source_storage,
    )

    assert result.outcome == "failed"
    assert result.message == "媒体校验服务暂不可用，请稍后重试"
    assert transport.calls == []


def test_voice_audio_media_commands_restrict_protocols_and_fail_on_decode_errors(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import media_tools

    commands: list[list[str]] = []
    inspections: list[dict[str, Any]] = []

    def inspect(_content: bytes, **kwargs: Any) -> Any:
        inspections.append(kwargs)
        return object()

    def run(command: list[str]) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"ID3-normalized")

    monkeypatch.setattr(media_tools, "inspect_media_bytes", inspect)
    monkeypatch.setattr(media_tools, "resolve_media_binary", lambda _tool: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media_tools, "_run", run)

    normalized = media_tools.normalize_audio_to_mp3(
        b"RIFF-source",
        suffix=".wav",
        min_duration_seconds=5,
        max_duration_seconds=180,
    )

    assert normalized == b"ID3-normalized"
    assert len(commands) == 1
    command = commands[0]
    assert "-xerror" in command
    assert command[command.index("-protocol_whitelist") + 1] == "file,pipe"
    assert inspections == [
        {
            "suffix": ".wav",
            "expected_type": "audio",
            "min_duration_seconds": 5,
            "max_duration_seconds": 180,
            "local_input_only": True,
        },
        {
            "suffix": ".mp3",
            "expected_type": "audio",
            "min_duration_seconds": 5,
            "max_duration_seconds": 180,
            "local_input_only": True,
        },
    ]


def test_voice_clone_done_without_demo_stays_running(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/voice",
                "content_type": "audio/mpeg",
                "file_id": "file-voice",
            }
        ),
    )
    transport.on("POST", "/api/v2/hifly/voice/create", envelope({"task_id": "voice-task"}))
    transport.on(
        "GET",
        "/api/v2/hifly/voice/task",
        envelope({"status": 3, "voice": "vendor-voice", "demo_url": ""}),
    )
    started = _start_voice(
        actor=actor(),
        identity_id="ident-1",
        title="缺试听",
        source_asset_id="asset-audio",
        consent_id=_consent_for(purpose="VOICE", source_asset_id="asset-audio"),
        idempotency_key="voice-no-demo-key",
        vendor=vendor,
    )

    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    _exec("UPDATE oral_voices SET next_attempt_at = NULL WHERE id = %s", (started.task_id,))
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    refreshed = _fetch("SELECT * FROM oral_voices WHERE id = %s", (started.task_id,))

    assert refreshed["status"] == "RUNNING"
    assert refreshed["demo_asset_id"] is None


def test_clone_submission_uncertain_is_persisted_and_not_retried(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/avatar",
                "content_type": "video/mp4",
                "file_id": "file-avatar",
            }
        ),
    )

    def uncertain(_body: bytes | None) -> bytes:
        raise HiflyError("连接中断")

    transport.on("POST", "/api/v2/hifly/avatar/create_by_video", uncertain)
    consent_id = _consent_for(purpose="AVATAR", source_asset_id="asset-src")

    started = _start_avatar(
        actor=actor(),
        identity_id="ident-1",
        title="提交不确定",
        source_asset_id="asset-src",
        source_kind="VIDEO",
        consent_id=consent_id,
        idempotency_key="avatar-uncertain-key",
        vendor=vendor,
    )
    result = _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    assert result is not None and result.outcome == "uncertain"
    persisted = _fetch(
        "SELECT id, status, submission_state FROM oral_avatars WHERE idempotency_key = %s",
        ("avatar-uncertain-key",),
    )
    assert persisted["status"] == "FAILED"
    assert persisted["submission_state"] == "SUBMISSION_UNKNOWN"
    assert started.task_id == persisted["id"]
    assert _claim("second-worker") is None

    replayed = _start_avatar(
        actor=actor(),
        identity_id="ident-1",
        title="提交不确定",
        source_asset_id="asset-src",
        source_kind="VIDEO",
        consent_id=consent_id,
        idempotency_key="avatar-uncertain-key",
        vendor=vendor,
    )
    assert replayed.task_id == persisted["id"]
    assert replayed.submission_state == "SUBMISSION_UNKNOWN"
    assert replayed.replayed is True
    assert sum(1 for method, url in transport.calls if url.endswith("/avatar/create_by_video")) == 1


def test_voice_confirm_rejects_unready_and_foreign_as_missing(scene: str) -> None:
    _exec(
        "INSERT INTO oral_voices ("
        " id, identity_id, owner_user_id, title, status, source_asset_id"
        ") VALUES ('voice-running', 'ident-1', 'employee_1',"
        " '未完成声音', 'RUNNING', 'asset-audio')"
    )

    with pytest.raises(OralDomainError, match="就绪"):
        _confirm_voice(voice_id="voice-running", actor=actor())
    with pytest.raises(OralDomainError) as foreign:
        _confirm_voice(voice_id="voice-running", actor=actor("employee_2"))
    with pytest.raises(OralDomainError) as missing:
        _confirm_voice(voice_id="missing", actor=actor("employee_2"))
    assert str(foreign.value) == str(missing.value)


def test_create_oral_task_tts_queues_reserves_and_replays_idempotently(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on("POST", "/api/v2/hifly/video/create_by_tts", envelope({"task_id": "vt-task-1"}))

    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="乡墅口播",
        script_text="大家好，今天带大家看一套乡墅。",
        audio_asset_id=None,
        subtitle={"st_show": True},
        idempotency_key="idem-key-0001",
        vendor=vendor,
    )
    assert created.status == "QUEUED"
    assert created.estimated_cost_fen == 1
    assert created.replayed is False

    replayed = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="乡墅口播",
        script_text="大家好，今天带大家看一套乡墅。",
        audio_asset_id=None,
        subtitle={"st_show": True},
        idempotency_key="idem-key-0001",
        vendor=vendor,
    )
    assert replayed.task_id == created.task_id
    assert replayed.replayed is True
    assert not transport.calls
    assert _wallet("employee_1") == (19, 1)
    reserve = _fetch(
        "SELECT task_id, oral_task_id, type FROM wallet_transactions WHERE oral_task_id = %s",
        (created.task_id,),
    )
    assert dict(reserve) == {
        "task_id": None,
        "oral_task_id": created.task_id,
        "type": "RESERVE",
    }


def test_oral_task_same_owner_idempotency_key_rejects_changed_payload(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on("POST", "/api/v2/hifly/video/create_by_tts", envelope({"task_id": "vt-1"}))
    common = {
        "actor": actor(),
        "identity_id": "ident-1",
        "avatar_id": avatar_id,
        "voice_id": voice_id,
        "mode": "TTS",
        "title": "乡墅口播",
        "audio_asset_id": None,
        "subtitle": None,
        "idempotency_key": "same-owner-key",
        "vendor": vendor,
    }
    _create_task(script_text="版本一", **common)

    with pytest.raises(OralConflictError, match="幂等键"):
        _create_task(script_text="版本二", **common)

    assert transport.calls == []


def test_oral_task_idempotency_key_is_scoped_by_owner(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, _ = _seed_ready_assets()
    _exec(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("oral-project-2", "employee_2", "Other Oral Project"),
    )
    _exec(
        "INSERT INTO person_identities (id, owner_user_id, display_name, status) "
        "VALUES ('ident-2', 'employee_2', '李工', 'ACTIVE')"
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, metadata_json, created_by_user_id"
        ") VALUES "
        " ('asset-src-2', 'oral-project-2', 'source_video', "
        " 'local://assets/src-2.mp4', 'video-hash-2', 9, 'video/mp4', '{}', 'employee_2'), "
        " ('asset-audio-2', 'oral-project-2', 'oral_audio', "
        " 'local://assets/audio-2.mp3', 'audio-hash-2', 9, 'audio/mpeg', "
        ' \'{"audio_purpose":"oral_audio","duration_seconds":42,'
        "\"audio_duration_verified\":true}', 'employee_2')"
    )
    avatar_consent = _create_consent(
        actor=actor("employee_2"),
        identity_id="ident-2",
        source_asset_id="asset-src-2",
        purpose="AVATAR",
        consent_text_version=ORAL_CONSENT_TEXT_VERSION,
    )["id"]
    _exec(
        "INSERT INTO oral_avatars ("
        " id, identity_id, owner_user_id, title, vendor_avatar_id, status,"
        " source_kind, source_asset_id, consent_id"
        ") VALUES ('avatar-ready-2', 'ident-2', 'employee_2', '李工分身', "
        " 'vendor-avatar-2', 'READY', 'VIDEO', 'asset-src-2', %s)",
        (avatar_consent,),
    )
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/audio",
                "content_type": "audio/mpeg",
                "file_id": "file-audio",
            }
        ),
    )
    transport.on("POST", "/api/v2/hifly/video/create_by_audio", envelope({"task_id": "vt"}))
    _exec(
        "UPDATE assets SET metadata_json = %s WHERE id = 'asset-audio'",
        ('{"audio_purpose":"oral_audio","duration_seconds":42,"audio_duration_verified":true}',),
    )

    first = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=None,
        mode="AUDIO",
        title="用户一",
        script_text=None,
        audio_asset_id="asset-audio",
        subtitle=None,
        idempotency_key="shared-owner-key",
        vendor=vendor,
    )
    second = _create_task(
        actor=actor("employee_2"),
        identity_id="ident-2",
        avatar_id="avatar-ready-2",
        voice_id=None,
        mode="AUDIO",
        title="用户二",
        script_text=None,
        audio_asset_id="asset-audio-2",
        subtitle=None,
        idempotency_key="shared-owner-key",
        vendor=vendor,
    )

    assert first.task_id != second.task_id


@pytest.mark.parametrize("missing_consent", ["avatar", "voice"])
def test_historical_ready_clone_without_consent_cannot_create_oral_task(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    missing_consent: str,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    _exec(f"UPDATE oral_{missing_consent}s SET consent_id = NULL")
    vendor, transport = make_vendor()

    with pytest.raises(OralDomainError, match="授权"):
        _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id=avatar_id,
            voice_id=voice_id,
            mode="TTS",
            title="历史数据",
            script_text="文案",
            audio_asset_id=None,
            subtitle=None,
            idempotency_key=f"historical-{missing_consent}",
            vendor=vendor,
        )

    assert transport.calls == []


def test_create_oral_task_rejects_unready_assets(scene: str) -> None:
    vendor, _ = make_vendor()

    with pytest.raises(OralDomainError, match="就绪"):
        _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id="missing",
            voice_id=None,
            mode="TTS",
            title="t",
            script_text="文案",
            audio_asset_id=None,
            subtitle=None,
            idempotency_key="idem-key-0002",
            vendor=vendor,
        )


def test_oral_billing_cancel_releases_once_and_success_settles_once(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, _ = make_vendor()

    cancelled = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="取消任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-cancel-billing",
        vendor=vendor,
    )
    with pytest.raises(BillingInvariantError, match="remain frozen"):
        _finalize_billing(oral_task_id=cancelled.task_id)
    first_cancel = _cancel_task(task_id=cancelled.task_id, actor=actor())
    replay_cancel = _cancel_task(task_id=cancelled.task_id, actor=actor())
    assert first_cancel["status"] == replay_cancel["status"] == "CANCELLED"

    succeeded = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="成功任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-success-billing",
        vendor=vendor,
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES ('oral-final-result', NULL, 'oral_video', 'local://oral/final.mp4', "
        " 'final-hash', 9, 'video/mp4', 'employee_1')"
    )
    _exec(
        "UPDATE oral_tasks SET status = 'SUCCEEDED', duration_sec=1, result_asset_id = %s "
        "WHERE id = %s",
        ("oral-final-result", succeeded.task_id),
    )
    settled = _finalize_billing(oral_task_id=succeeded.task_id)
    replayed = _finalize_billing(oral_task_id=succeeded.task_id)

    assert settled.transaction_type == replayed.transaction_type == "SETTLE"
    assert _wallet("employee_1") == (19, 0)
    rows = _fetchall(
        "SELECT oral_task_id, type FROM wallet_transactions "
        "WHERE oral_task_id IN (%s, %s) ORDER BY oral_task_id, type",
        (cancelled.task_id, succeeded.task_id),
    )
    assert sorted(str(row["type"]) for row in rows) == [
        "RELEASE",
        "RESERVE",
        "RESERVE",
        "SETTLE",
    ]


@pytest.mark.parametrize("status", ["SUBMISSION_UNCERTAIN", "ARCHIVE_FAILED"])
def test_oral_billing_keeps_uncertain_and_archive_failed_reservations_frozen(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    status: str,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    task = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="冻结任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key=f"oral-frozen-{status}",
    )
    _exec("UPDATE oral_tasks SET status = %s WHERE id = %s", (status, task.task_id))

    with pytest.raises(BillingInvariantError, match="remain frozen"):
        _finalize_billing(oral_task_id=task.task_id)

    assert _wallet("employee_1") == (19, 1)


def test_create_oral_task_rejects_ready_but_unconfirmed_voice(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    _exec("UPDATE oral_voices SET confirmed = 0 WHERE id = %s", (voice_id,))
    vendor, transport = make_vendor()

    with pytest.raises(OralDomainError, match="确认"):
        _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id=avatar_id,
            voice_id=voice_id,
            mode="TTS",
            title="未试听声音",
            script_text="文案",
            audio_asset_id=None,
            subtitle=None,
            idempotency_key="unconfirmed-voice",
            vendor=vendor,
        )
    assert transport.calls == []


@pytest.mark.parametrize(
    ("audio_asset_id", "error_type"),
    [
        ("asset-src", OralDomainError),
        ("other-audio", HTTPException),
        ("pending-audio", OralDomainError),
    ],
)
def test_create_audio_oral_task_rejects_wrong_inaccessible_or_incomplete_asset(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    audio_asset_id: str,
    error_type: type[Exception],
) -> None:
    avatar_id, _ = _seed_ready_assets()
    _exec(
        "INSERT INTO projects (id, owner_user_id, name) VALUES (%s, %s, %s)",
        ("other-project", "employee_2", "Other Project"),
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES (%s, %s, 'oral_audio', %s, %s, %s, 'audio/mpeg', %s)",
        (
            "other-audio",
            "other-project",
            "local://assets/other.mp3",
            "other-audio-hash",
            9,
            "employee_2",
        ),
    )
    _exec(
        "INSERT INTO assets ("
        " id, project_id, kind, storage_uri, sha256, size_bytes,"
        " content_type, created_by_user_id"
        ") VALUES (%s, %s, 'oral_audio', %s, '', 0, 'audio/mpeg', %s)",
        ("pending-audio", "oral-project", "local://assets/pending.mp3", "employee_1"),
    )
    vendor, _ = make_vendor()

    with pytest.raises(error_type):
        _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id=avatar_id,
            voice_id=None,
            mode="AUDIO",
            title="音频口播",
            script_text=None,
            audio_asset_id=audio_asset_id,
            subtitle=None,
            idempotency_key=f"invalid-{audio_asset_id}",
            vendor=vendor,
        )


def test_oral_audio_and_voice_clone_do_not_cross_use_explicit_purpose(
    scene: str,
    fake_source_storage: FakeSourceStorage,
) -> None:
    avatar_id, _ = _seed_ready_assets()
    vendor, _ = make_vendor()

    _exec(
        "UPDATE assets SET metadata_json = %s WHERE id = 'asset-audio'",
        ('{"audio_purpose":"voice_clone","duration_seconds":42,"audio_duration_verified":true}',),
    )
    with pytest.raises(OralDomainError, match="用途"):
        _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id=avatar_id,
            voice_id=None,
            mode="AUDIO",
            title="用途错误",
            script_text=None,
            audio_asset_id="asset-audio",
            subtitle=None,
            idempotency_key="wrong-oral-purpose",
            vendor=vendor,
        )

    _exec(
        "UPDATE assets SET metadata_json = %s WHERE id = 'asset-audio'",
        ('{"audio_purpose":"oral_audio","duration_seconds":42,"audio_duration_verified":true}',),
    )
    voice_consent = _consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio")
    with pytest.raises(OralDomainError, match="用途"):
        _start_voice(
            actor=actor(),
            identity_id="ident-1",
            title="用途错误",
            source_asset_id="asset-audio",
            consent_id=voice_consent,
            idempotency_key="wrong-clone-purpose",
            vendor=vendor,
        )


@pytest.mark.parametrize("operation", ["oral", "voice_clone"])
def test_audio_without_explicit_purpose_is_rejected(
    scene: str,
    fake_source_storage: FakeSourceStorage,
    operation: str,
) -> None:
    avatar_id, _ = _seed_ready_assets()
    _exec("UPDATE assets SET metadata_json = '{}' WHERE id = 'asset-audio'")
    vendor, transport = make_vendor()

    with pytest.raises(OralDomainError, match="用途"):
        if operation == "oral":
            _create_task(
                actor=actor(),
                identity_id="ident-1",
                avatar_id=avatar_id,
                voice_id=None,
                mode="AUDIO",
                title="无用途音频",
                script_text=None,
                audio_asset_id="asset-audio",
                subtitle=None,
                idempotency_key="missing-purpose-oral",
                vendor=vendor,
            )
        else:
            _start_voice(
                actor=actor(),
                identity_id="ident-1",
                title="无用途声音",
                source_asset_id="asset-audio",
                consent_id=_consent_for(purpose="VOICE_CLONE", source_asset_id="asset-audio"),
                idempotency_key="missing-purpose-voice",
                vendor=vendor,
            )

    assert transport.calls == []


def test_refresh_oral_task_archives_result_asset(
    scene: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_source_storage: FakeSourceStorage,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on("POST", "/api/v2/hifly/video/create_by_tts", envelope({"task_id": "vt-task-2"}))
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="乡墅口播",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="idem-key-0003",
        vendor=vendor,
    )
    _exec(
        "UPDATE oral_tasks SET status = 'RUNNING', vendor_task_id = 'vt-task-2', "
        "submission_state = 'SUBMITTED' WHERE id = %s",
        (created.task_id,),
    )

    # 任务已 RUNNING：供应商查询 DONE 并返回临时视频地址 → 归档为平台资产。
    transport.on(
        "GET",
        "/api/v2/hifly/video/task",
        envelope({"status": 3, "video_Url": "https://tmp.example/v.mp4", "duration": 32}),
    )
    transport.on("GET", "https://tmp.example/v.mp4", fake_source_storage.media.video)

    class FakeResultStorage:
        def put_object(self, key: str, content: bytes, *, content_type: str) -> StoredObject:
            return StoredObject(
                provider="fake",
                bucket="assets",
                key=key,
                uri=f"fake://assets/{key}",
                size=len(content),
                content_type=content_type,
                sha256="hash-" + str(len(content)),
                updated_at=datetime.now(tz=UTC),
            )

    storage = FakeResultStorage()
    monkeypatch.setattr("app.oral.get_media_storage", lambda _conn: storage)

    refreshed = _refresh_task(task_id=created.task_id, actor=actor(), vendor=vendor)
    assert refreshed["status"] == "SUCCEEDED"
    assert refreshed["result_asset_id"]
    assert refreshed["duration_sec"] == 32

    asset = _fetch(
        "SELECT kind, project_id FROM assets WHERE id = %s", (refreshed["result_asset_id"],)
    )
    assert asset["kind"] == "oral_video"
    assert asset["project_id"] is None


def test_task_create_never_calls_vendor_inside_request_transaction(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/video/create_by_tts",
        json.dumps({"code": 1002, "msg": "credit", "data": {}}).encode(),
    )

    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="t",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="idem-key-0004",
        vendor=vendor,
    )
    assert created.status == "QUEUED"
    assert transport.calls == []
    row = _fetch(
        "SELECT status, submission_state, error_message FROM oral_tasks WHERE idempotency_key = %s",
        ("idem-key-0004",),
    )
    assert row["status"] == "QUEUED"
    assert row["submission_state"] == "LOCAL_PENDING"
    assert row["error_message"] is None

    result = _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    assert result is not None and result.outcome == "failed"
    failed = _fetch(
        "SELECT status, provider_charge_state FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert (failed["status"], failed["provider_charge_state"]) == (
        "FAILED",
        "NOT_CHARGED",
    )
    assert _wallet("employee_1") == (20, 0)


def test_oral_task_uncertain_submission_freezes_credit_and_never_retries(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()

    def uncertain(_body: bytes | None) -> bytes:
        raise HiflyError("connection dropped")

    transport.on("POST", "/api/v2/hifly/video/create_by_tts", uncertain)
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="不确定提交",
        script_text="禁止盲目重提。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-worker-uncertain-key",
    )
    result = _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    assert result is not None and result.outcome == "uncertain"
    task = _fetch(
        "SELECT status, provider_charge_state FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert (task["status"], task["provider_charge_state"]) == (
        "SUBMISSION_UNCERTAIN",
        "UNKNOWN",
    )
    assert _claim("second-worker") is None
    assert _wallet("employee_1") == (19, 1)
    assert sum(1 for _, url in transport.calls if url.endswith("video/create_by_tts")) == 1


def test_uncertain_submit_releases_slot_only_after_successful_cas(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="不确定提交释放槽位",
        script_text="保留冻结金额但释放执行容量。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-worker-uncertain-slot-key",
    )
    lease = _claim("uncertain-slot-worker")
    assert lease is not None and lease.kind == "task_submit"
    _exec(
        "UPDATE oral_tasks SET queue_slot_acquired = 1 WHERE id = %s",
        (created.task_id,),
    )

    _finalize_work(
        lease=lease,
        result=OralWorkResult(outcome="uncertain", message="provider response unknown"),
    )
    row = _fetch(
        "SELECT status, submission_state, provider_charge_state, queue_slot_acquired "
        "FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert tuple(row) == ("SUBMISSION_UNCERTAIN", "SUBMISSION_UNKNOWN", "UNKNOWN", 0)
    assert _wallet("employee_1") == (19, 1)
    transactions = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [entry["type"] for entry in transactions] == ["RESERVE"]


def test_uncertain_submit_lease_loss_does_not_release_winner_slot(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="旧租约不得释放赢家槽位",
        script_text="CAS 失败必须无副作用。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-worker-uncertain-lease-lost-key",
    )
    lease = _claim("stale-worker")
    assert lease is not None and lease.kind == "task_submit"
    _exec(
        "UPDATE oral_tasks SET lease_owner = 'winner-token', queue_slot_acquired = 1 WHERE id = %s",
        (created.task_id,),
    )

    with pytest.raises(OralLeaseLostError):
        _finalize_work(
            lease=OralWorkLease(
                kind=lease.kind,
                record_id=lease.record_id,
                worker_id=lease.worker_id,
                lease_token=lease.lease_token,
                attempt_count=lease.attempt_count,
                row=lease.row,
            ),
            result=OralWorkResult(outcome="uncertain", message="stale result"),
        )
    row = _fetch(
        "SELECT status, lease_owner, queue_slot_acquired FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert tuple(row) == ("SUBMITTING", "winner-token", 1)
    assert _wallet("employee_1") == (19, 1)
    transactions = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [entry["type"] for entry in transactions] == ["RESERVE"]


def test_dangling_oral_reservation_reconciles_only_known_not_charged_failure(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="对账任务",
        script_text="仅明确未扣费可释放。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-dangling-key",
    )
    _exec(
        "UPDATE oral_tasks SET status = 'FAILED', submission_state = 'FAILED', "
        "provider_charge_state = 'NOT_CHARGED' WHERE id = %s",
        (created.task_id,),
    )

    result = _reconcile_dangling()
    assert (result.scanned, result.released, result.settled, result.failed) == (1, 1, 0, 0)
    assert _wallet("employee_1") == (20, 0)


def test_oral_unit_price_defaults_and_reads_settings(scene: str) -> None:
    _exec("DELETE FROM billing_tariffs WHERE service='oral'")
    assert _oral_unit_price() == 0
    _exec("UPDATE runtime_settings SET oral_unit_price_fen = %s WHERE id = 1", (1800,))
    assert _oral_unit_price() == 0  # The old runtime template cannot authorize a charge.


def test_oral_task_snapshots_configured_unit_price(scene: str) -> None:
    _seed_ready_assets()
    _exec("UPDATE billing_tariffs SET unit_credits=2,version=2 WHERE service='oral'")

    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id="avatar-ready",
        voice_id="voice-ready",
        mode="TTS",
        title="价格快照",
        script_text="测试口播价格快照",
        audio_asset_id=None,
        subtitle={"st_show": True},
        idempotency_key="oral-price-snapshot-key",
    )

    assert created.estimated_cost_fen == 8  # Four estimated seconds at two credits per second.
    assert _price_quote() == {"unit_price_fen": 2, "unit_credits": 2, "credit_price_version": 2}


def test_oral_credit_release_uses_reserved_price_after_repricing(scene: str) -> None:
    _seed_ready_assets()
    _exec(
        "UPDATE customer_credit_pricing SET version = 1, config_json = %s",
        (json.dumps({"video_768p": 3, "video_2k": 7, "oral": 11, "points_per_yuan": 100}),),
    )
    _exec("UPDATE billing_tariffs SET unit_credits=11 WHERE service='oral'")
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id="avatar-ready",
        voice_id="voice-ready",
        mode="TTS",
        title="积分冻结",
        script_text="测试",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-credit-snapshot",
    )
    assert _wallet() == (9, 11)
    _exec(
        "UPDATE customer_credit_pricing SET version = 2, config_json = %s",
        (json.dumps({"video_768p": 3, "video_2k": 7, "oral": 19, "points_per_yuan": 100}),),
    )
    _exec("UPDATE billing_tariffs SET unit_credits=19 WHERE service='oral'")
    _exec(
        "UPDATE oral_tasks SET status = 'FAILED', provider_charge_state = 'NOT_CHARGED' "
        "WHERE id = %s",
        (created.task_id,),
    )
    _finalize_billing(oral_task_id=created.task_id)
    _finalize_billing(oral_task_id=created.task_id)
    assert _wallet() == (20, 0)
    rows = _fetchall(
        "SELECT reserved_delta, pricing_snapshot_json FROM wallet_transactions "
        "WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [r["reserved_delta"] for r in rows] == [11, -11]
    assert rows[0]["pricing_snapshot_json"] == rows[1]["pricing_snapshot_json"]


def test_oral_clone_claim_is_exclusive_and_expired_submit_is_quarantined(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    started = _start_avatar(
        actor=actor(),
        identity_id="ident-1",
        title="多实例分身",
        source_asset_id="asset-src",
        source_kind="VIDEO",
        consent_id=_consent_for(purpose="AVATAR", source_asset_id="asset-src"),
        idempotency_key="exclusive-avatar-key",
    )
    first = _claim("worker-a", lease_seconds=120)
    assert first is not None and first.record_id == started.task_id
    assert _claim("worker-b", lease_seconds=120) is None

    _exec(
        "UPDATE oral_avatars SET lease_expires_at = %s WHERE id = %s",
        ("2000-01-01T00:00:00+00:00", started.task_id),
    )
    assert _claim("worker-b", lease_seconds=120) is None
    row = _fetch(
        "SELECT submission_state, attempt_count FROM oral_avatars WHERE id = %s",
        (started.task_id,),
    )
    assert (row["submission_state"], row["attempt_count"]) == (
        "SUBMISSION_UNKNOWN",
        1,
    )


@pytest.mark.parametrize("kind", ["avatar", "voice"])
@pytest.mark.parametrize("expired", [False, True])
def test_stopped_clone_refunds_budget_and_rejects_late_receipt(
    scene: str, kind: str, expired: bool
):
    from app.oral_worker import OralLeaseLostError, OralWorkResult, finalize_oral_work

    _exec(
        "INSERT INTO billing_tariffs(service,enabled,unit_credits) VALUES(%s,true,7)",
        (f"{kind}_clone",),
    )
    if kind == "avatar":
        started = _start_avatar(
            actor=actor(),
            identity_id="ident-1",
            title="clone",
            source_asset_id="asset-src",
            source_kind="VIDEO",
            consent_id=_consent_for(purpose="AVATAR", source_asset_id="asset-src"),
            idempotency_key="paid-clone",
        )
    else:
        started = _start_voice(
            actor=actor(),
            identity_id="ident-1",
            title="clone",
            source_asset_id="asset-audio",
            consent_id=_consent_for(purpose="VOICE", source_asset_id="asset-audio"),
            idempotency_key="paid-clone",
        )
    assert _wallet("employee_1") == (13, 7)
    lease = _claim("clone-worker")
    assert lease is not None
    if expired:
        _exec(
            f"UPDATE oral_{'avatars' if kind == 'avatar' else 'voices'} SET "
            "lease_expires_at='2000-01-01T00:00:00Z' WHERE id=%s",
            (started.task_id,),
        )
        assert _claim("recovery-worker") is None
    else:
        with pg_transaction() as raw:
            finalize_oral_work(
                BusinessConnection.postgres(raw), lease=lease, result=OralWorkResult("uncertain")
            )
    assert _wallet("employee_1") == (20, 0)
    with pytest.raises(OralLeaseLostError):
        with pg_transaction() as raw:
            finalize_oral_work(
                BusinessConnection.postgres(raw),
                lease=lease,
                result=OralWorkResult("submitted", provider_task_id="late-receipt"),
            )
    assert _wallet("employee_1") == (20, 0)


def test_expired_voice_poll_lease_cannot_delete_winner_demo(
    scene: str,
    fake_source_storage: FakeSourceStorage,
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/tool/create_upload_url",
        envelope(
            {
                "upload_url": "https://up.example/lease-voice",
                "content_type": "audio/mpeg",
                "file_id": "lease-file",
            }
        ),
    )
    transport.on("POST", "/api/v2/hifly/voice/create", envelope({"task_id": "lease-vt"}))
    transport.on(
        "GET",
        "/api/v2/hifly/voice/task",
        envelope(
            {
                "status": 3,
                "voice": "lease-vendor-voice",
                "demo_url": "https://tmp.example/lease-demo.mp3",
            }
        ),
    )
    transport.on(
        "GET",
        "https://tmp.example/lease-demo.mp3",
        fake_source_storage.media.audio,
    )
    started = _start_voice(
        actor=actor(),
        identity_id="ident-1",
        title="租约声音",
        source_asset_id="asset-audio",
        consent_id=_consent_for(purpose="VOICE", source_asset_id="asset-audio"),
        idempotency_key="voice-lease-fence-key",
    )
    assert _run_oral_worker_step(vendor=vendor, storage=fake_source_storage) is not None
    _exec("UPDATE oral_voices SET next_attempt_at = NULL WHERE id = %s", (started.task_id,))

    first = _claim("worker-a", lease_seconds=120)
    assert first is not None and first.kind == "voice_poll"
    first = _prepare(first)
    first_result = perform_oral_work(first, vendor=vendor, storage=fake_source_storage)
    assert first_result.stored is not None

    _exec(
        "UPDATE oral_voices SET lease_expires_at = %s WHERE id = %s",
        ("2000-01-01T00:00:00+00:00", started.task_id),
    )
    second = _claim("worker-b", lease_seconds=120)
    assert second is not None and second.kind == "voice_poll"
    second = _prepare(second)
    second_result = perform_oral_work(second, vendor=vendor, storage=fake_source_storage)
    assert second_result.stored is not None
    assert second.attempt_count == first.attempt_count + 1
    assert second.lease_token != first.lease_token
    assert first_result.stored.key != second_result.stored.key

    _finalize_work(lease=second, result=second_result)
    with pytest.raises(OralLeaseLostError):
        _finalize_work(lease=first, result=first_result)
    discard_uncommitted_oral_asset(
        fake_source_storage,
        result=first_result,
        actor_id="employee_1",
    )

    assert first_result.stored.key not in fake_source_storage.objects
    assert second_result.stored.key in fake_source_storage.objects
    winner = _fetch(
        "SELECT asset.storage_uri FROM oral_voices AS voice "
        "JOIN assets AS asset ON asset.id = voice.demo_asset_id WHERE voice.id = %s",
        (started.task_id,),
    )
    assert winner["storage_uri"] == second_result.stored.uri


# --------------------------------------------------------------------------- #
# Batch E — manual billing reconciliation, expired-submission slot release, the
# production PG worker loop (``run_pg_worker_once``), archive retry, and the
# customer-facing task routes (retry removed, billing/action projection, paging).
# Route tests inject the acting user through ``_make_business_db_override`` and
# drive the real ``get_current_user`` dev-header path, which resolves
# ``employee_1``/``employee_2`` against the seeded PG users (``users.is_active``
# defaults to 1, so ``authenticate_user`` succeeds).
# --------------------------------------------------------------------------- #


def test_oral_manual_billing_reconciliation_is_admin_only_audited_and_idempotent(
    scene: str,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    release_task = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="人工释放",
        script_text="供应商确认未扣费。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="manual-release-key",
    )
    _exec(
        "UPDATE oral_tasks SET status = 'SUBMISSION_UNCERTAIN', "
        "submission_state = 'SUBMISSION_UNKNOWN', provider_charge_state = 'UNKNOWN' "
        "WHERE id = %s",
        (release_task.task_id,),
    )
    with pg_transaction() as raw:
        _executemany(
            raw,
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (%s, NULL, 'document', %s, %s, 10, 'application/pdf', 'admin_1')
            """,
            [
                ("evidence-release", "local://evidence/release.pdf", "a" * 64),
                ("evidence-settle", "local://evidence/settle.pdf", "c" * 64),
            ],
        )

    db_override, holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    request = {
        "reconciliation_operation_id": "reconcile-release-001",
        "provider_outcome": "NOT_FOUND",
        "provider_charge_state": "NOT_CHARGED",
        "resolution": "RELEASE",
        "evidence_asset_id": "evidence-release",
        "reason": "供应商工单确认任务未创建",
    }
    try:
        client = TestClient(app)
        denied = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json=request,
        )
        holder.current_actor = actor("admin_1", "admin")
        released = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json=request,
        )
        replay = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json=request,
        )
        conflict = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json={
                **request,
                "provider_charge_state": "CHARGED",
                "resolution": "RELEASE",
                "evidence_asset_id": "evidence-settle",
            },
        )
        different_operation = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json={**request, "reconciliation_operation_id": "reconcile-release-002"},
        )
        unowned_evidence = client.post(
            f"/api/oral/tasks/{release_task.task_id}/billing-reconcile",
            json={
                **request,
                "reconciliation_operation_id": "reconcile-release-003",
                "evidence_asset_id": "asset-src",
            },
        )
        charged_task = _create_task(
            actor=actor(),
            identity_id="ident-1",
            avatar_id=avatar_id,
            voice_id=voice_id,
            mode="TTS",
            title="人工结算",
            script_text="供应商确认已扣费。",
            audio_asset_id=None,
            subtitle=None,
            idempotency_key="manual-settle-key",
        )
        _exec(
            "UPDATE oral_tasks SET status = 'FAILED', submission_state = 'FAILED', "
            "provider_charge_state = 'CHARGED' WHERE id = %s",
            (charged_task.task_id,),
        )
        settled = client.post(
            f"/api/oral/tasks/{charged_task.task_id}/billing-reconcile",
            json={
                "reconciliation_operation_id": "reconcile-settle-001",
                "provider_outcome": "FAILED",
                "provider_charge_state": "CHARGED",
                "resolution": "RELEASE",
                "evidence_asset_id": "evidence-settle",
                "reason": "供应商账单确认已产生扣费",
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert denied.status_code == 403
    assert released.status_code == replay.status_code == 200
    assert (
        released.json()
        == replay.json()
        == {
            "task_id": release_task.task_id,
            "billing_round": 1,
            "transaction_type": "RELEASE",
        }
    )
    assert "evidence_sha256" not in released.json()
    assert conflict.status_code == 409
    assert different_operation.status_code == 409
    assert unowned_evidence.status_code == 409
    assert settled.status_code == 200
    assert settled.json()["transaction_type"] == "RELEASE"
    assert _wallet("employee_1") == (20, 0)
    audit = _fetch(
        "SELECT metadata_json FROM audit_logs "
        "WHERE action = 'oral.billing.reconcile' AND entity_id = %s "
        "ORDER BY created_at LIMIT 1",
        (release_task.task_id,),
    )
    assert audit is not None
    assert json.loads(str(audit["metadata_json"])) == {
        "evidence_asset_id": "evidence-release",
        "evidence_sha256": "a" * 64,
        "provider_charge_state": "NOT_CHARGED",
        "provider_outcome": "NOT_FOUND",
        "reason": "供应商工单确认任务未创建",
        "reconciliation_operation_id": "reconcile-release-001",
        "resolution": "RELEASE",
    }


@pytest.mark.parametrize("status", ["RUNNING", "ARCHIVING", "SUCCEEDED"])
def test_manual_oral_reconciliation_rejects_non_attention_states(
    scene: str,
    status: str,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    task = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="不可人工对账",
        script_text="状态门禁",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key=f"reconcile-state-{status}",
    )
    _exec(
        "UPDATE oral_tasks SET status = %s, provider_charge_state = 'CHARGED' WHERE id = %s",
        (status, task.task_id),
    )
    with pytest.raises(BillingInvariantError, match="manual billing attention"):
        _reconcile_by_evidence(
            oral_task_id=task.task_id,
            reconciliation_operation_id=f"operation-{status}",
            provider_outcome="SUCCEEDED",
            provider_charge_state="CHARGED",
            resolution="SETTLE",
            reason="供应商账单人工复核",
            evidence_asset_id="asset-src",
            evidence_sha256="video-hash",
        )


def test_expired_oral_submission_releases_queue_slot_once(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    task = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="过期提交",
        script_text="过期提交释放槽位",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="expired-submit-slot-key",
    )
    _exec(
        "UPDATE oral_tasks SET status = 'SUBMITTING', submission_state = 'SUBMITTING', "
        "queue_slot_acquired = 1, "
        "lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE id = %s",
        (task.task_id,),
    )

    assert _claim("expiry-worker") is None
    assert _claim("expiry-worker-2") is None
    row = _fetch(
        "SELECT status, queue_slot_acquired FROM oral_tasks WHERE id = %s", (task.task_id,)
    )
    assert row is not None
    assert (row["status"], row["queue_slot_acquired"]) == ("SUBMISSION_UNCERTAIN", 0)


@pytest.mark.parametrize("kind", ["voice_poll", "task_archive"])
def test_oral_outputs_use_existing_authorized_cloud_namespaces(
    fake_source_storage: FakeSourceStorage, kind: OralWorkKind
) -> None:
    vendor, transport = make_vendor()
    transport.on(
        "GET",
        "/api/v2/hifly/voice/task",
        envelope(
            {
                "status": 3,
                "voice": "cloud-voice",
                "demo_url": "https://tmp.example/cloud-demo.mp3",
            }
        ),
    )
    transport.on("GET", "https://tmp.example/cloud-demo.mp3", fake_source_storage.media.audio)
    transport.on("GET", "https://tmp.example/cloud-result.mp4", fake_source_storage.media.video)
    original_put = fake_source_storage.put_object

    def restricted_put(key: str, content: bytes, *, content_type: str) -> StoredObject:
        if not key.startswith(("materials/", "generation-results/")):
            raise PermissionError("object is outside the configured business namespaces")
        return original_put(key, content, content_type=content_type)

    fake_source_storage.put_object = restricted_put  # type: ignore[method-assign]
    result = perform_oral_work(
        OralWorkLease(
            kind=kind,
            record_id="cloud-oral-record",
            worker_id="cloud-oral-worker",
            lease_token="cloud-oral-lease",
            attempt_count=1,
            row={
                "vendor_task_id": "cloud-oral-vendor",
                "provider_result_url": "https://tmp.example/cloud-result.mp4",
            },
        ),
        vendor=vendor,
        storage=fake_source_storage,
    )
    assert result.outcome == "ready"
    assert result.stored is not None
    assert result.stored.key in fake_source_storage.objects
    assert all(method == "GET" for method, _ in transport.calls)


def test_generation_worker_completes_oral_task_and_settles_once(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on("POST", "/api/v2/hifly/video/create_by_tts", envelope({"task_id": "oral-vt-1"}))
    transport.on(
        "GET",
        "/api/v2/hifly/video/task",
        envelope(
            {
                "status": 3,
                "video_url": "https://tmp.example/oral-result.mp4",
                "duration": 12,
            }
        ),
    )
    transport.on(
        "GET",
        "https://tmp.example/oral-result.mp4",
        fake_source_storage.media.video,
    )
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="Worker 口播",
        script_text="这是完整的异步口播。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-worker-success-key",
    )

    for index in range(3):
        if index == 1:
            _exec(
                "UPDATE oral_tasks SET next_attempt_at = NULL WHERE id = %s",
                (created.task_id,),
            )
        assert (
            _run_pg_worker(
                worker_id="generation-worker",
                storage=fake_source_storage,
                vendor=vendor,
                max_tasks=1,
            )
            == 1
        )

    task = _fetch(
        "SELECT status, result_asset_id, provider_charge_state FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert task is not None
    assert task["status"] == "SUCCEEDED"
    assert task["result_asset_id"]
    assert task["provider_charge_state"] == "CHARGED"
    assert _wallet("employee_1") == (19, 0)
    ledger = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [row["type"] for row in ledger] == ["RESERVE", "SETTLE"]
    assert sum(1 for _, url in transport.calls if url.endswith("video/create_by_tts")) == 1


def test_oral_worker_rejects_invalid_provider_video_without_settlement(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on(
        "POST",
        "/api/v2/hifly/video/create_by_tts",
        envelope({"task_id": "oral-invalid-result"}),
    )
    transport.on(
        "GET",
        "/api/v2/hifly/video/task",
        envelope(
            {
                "status": 3,
                "video_url": "https://tmp.example/invalid-result.mp4",
                "duration": 12,
            }
        ),
    )
    transport.on("GET", "https://tmp.example/invalid-result.mp4", b"not-a-video")
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="伪视频结果",
        script_text="供应商返回的内容必须先验真。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-invalid-result-key",
        vendor=vendor,
    )

    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    _exec("UPDATE oral_tasks SET next_attempt_at = NULL WHERE id = %s", (created.task_id,))
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)

    task = _fetch(
        "SELECT status, result_asset_id, error_message FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert task is not None
    assert tuple(task) == ("ARCHIVE_FAILED", None, "口播成片文件无效")
    assert _wallet("employee_1") == (19, 1)
    ledger = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [row["type"] for row in ledger] == ["RESERVE"]
    assert not any(
        key.startswith("generation-results/oral/") for key in fake_source_storage.objects
    )


def test_oral_archive_retry_reuses_result_without_resubmit_or_rereserve(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    vendor, transport = make_vendor()
    transport.on("POST", "/api/v2/hifly/video/create_by_tts", envelope({"task_id": "oral-vt-2"}))
    transport.on(
        "GET",
        "/api/v2/hifly/video/task",
        envelope({"status": 3, "video_url": "https://tmp.example/retry.mp4"}),
    )
    transport.on(
        "GET",
        "https://tmp.example/retry.mp4",
        fake_source_storage.media.video,
    )
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="归档重试",
        script_text="归档失败也不重复提交。",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-archive-retry-key",
    )
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    _exec("UPDATE oral_tasks SET next_attempt_at = NULL WHERE id = %s", (created.task_id,))
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)

    original_put = fake_source_storage.put_object
    failures = 0

    def fail_once(key: str, content: bytes, *, content_type: str) -> StoredObject:
        nonlocal failures
        if failures == 0 and key.startswith("generation-results/oral/"):
            failures += 1
            raise RuntimeError("temporary storage outage")
        return original_put(key, content, content_type=content_type)

    fake_source_storage.put_object = fail_once  # type: ignore[method-assign]
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    failed = _fetch(
        "SELECT status, provider_result_url FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert failed is not None
    assert failed["status"] == "ARCHIVE_FAILED"
    assert failed["provider_result_url"] == "https://tmp.example/retry.mp4"

    _request_archive_retry(task_id=created.task_id, owner_user_id="employee_1")
    _run_oral_worker_step(vendor=vendor, storage=fake_source_storage)
    succeeded = _fetch("SELECT status FROM oral_tasks WHERE id = %s", (created.task_id,))
    assert succeeded is not None
    assert succeeded["status"] == "SUCCEEDED"
    ledger = _fetchall(
        "SELECT type FROM wallet_transactions WHERE oral_task_id = %s ORDER BY ledger_sequence",
        (created.task_id,),
    )
    assert [row["type"] for row in ledger] == ["RESERVE", "SETTLE"]
    assert sum(1 for _, url in transport.calls if url.endswith("video/create_by_tts")) == 1


# --------------------------------------------------------------------------- #
# Customer retry for submission-uncertain tasks + billing/action projection
# --------------------------------------------------------------------------- #


def test_oral_task_retry_route_is_removed_and_keeps_uncertain_reservation(
    scene: str,
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="重试任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-retry-route-key",
    )
    # 模拟 worker 侧提交结果未知；普通用户不得再次发起付费 POST。
    _exec(
        """
        UPDATE oral_tasks
        SET status = 'SUBMISSION_UNCERTAIN', provider_charge_state = 'UNKNOWN',
            submission_state = 'SUBMISSION_UNKNOWN',
            queue_slot_acquired = 0
        WHERE id = %s
        """,
        (created.task_id,),
    )

    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    _read_actor_override()
    try:
        client = TestClient(app)
        headers: dict[str, str] = {}
        retried = client.post(f"/api/oral/tasks/{created.task_id}/retry", headers=headers)
        listing = client.get("/api/oral/tasks", headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert retried.status_code == 404
    row = _fetch(
        "SELECT status, submission_state, queue_slot_acquired FROM oral_tasks WHERE id = %s",
        (created.task_id,),
    )
    assert row is not None
    assert tuple(row) == ("SUBMISSION_UNCERTAIN", "SUBMISSION_UNKNOWN", 0)
    # 冻结的预留轮不动：仍然只有一笔 RESERVE，没有 SETTLE/RELEASE。
    assert _ledger(created.task_id) == ["RESERVE"]
    assert _wallet("employee_1") == (19, 1)
    listed = [entry for entry in listing.json()["items"] if entry["id"] == created.task_id]
    assert listed and listed[0]["status"] == "SUBMISSION_UNCERTAIN"
    assert listed[0]["billing_status"] == "RESERVED"
    assert listed[0]["available_actions"] == []


def test_oral_task_retry_route_is_absent_for_all_states(scene: str) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    created = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="排队任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-retry-reject-key",
    )

    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    _read_actor_override()
    try:
        client = TestClient(app)
        response = client.post(f"/api/oral/tasks/{created.task_id}/retry")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_openapi_does_not_advertise_uncertain_oral_resubmit() -> None:
    assert "/api/oral/tasks/{task_id}/retry" not in app.openapi()["paths"]


def test_oral_task_serialization_reports_billing_status_and_available_actions(
    scene: str, fake_source_storage: FakeSourceStorage
) -> None:
    avatar_id, voice_id = _seed_ready_assets()
    uncertain = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="序列化任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-serialize-a",
    )
    cancelled = _create_task(
        actor=actor(),
        identity_id="ident-1",
        avatar_id=avatar_id,
        voice_id=voice_id,
        mode="TTS",
        title="取消任务",
        script_text="文案",
        audio_asset_id=None,
        subtitle=None,
        idempotency_key="oral-serialize-b",
    )

    db_override, _holder = _make_business_db_override(actor())
    app.dependency_overrides[get_business_db] = db_override
    _read_actor_override()
    try:
        client = TestClient(app)
        headers: dict[str, str] = {}
        single = client.get(f"/api/oral/tasks/{uncertain.task_id}", headers=headers)
        assert single.status_code == 200
        assert single.json()["billing_status"] == "RESERVED"
        assert single.json()["available_actions"] == []
        app.dependency_overrides[get_current_user] = lambda: actor("employee_2")
        denied = client.get(f"/api/oral/tasks/{uncertain.task_id}")
        assert denied.status_code == 404
        assert denied.json()["detail"]["code"] == "ORAL_TASK_NOT_FOUND"
        # Restore the owner identity before the remaining serialization
        # assertions (the denial swap above must not leak into them).
        _read_actor_override()

        contract = client.get("/openapi.json").json()
        list_get = contract["paths"]["/api/oral/tasks"]["get"]
        detail_get = contract["paths"]["/api/oral/tasks/{task_id}"]["get"]
        list_schema = list_get["responses"]["200"]["content"]["application/json"]["schema"]
        detail_schema = detail_get["responses"]["200"]["content"]["application/json"]["schema"]
        assert list_schema["$ref"].endswith("/OralTaskPageResponse")
        assert detail_schema["$ref"].endswith("/OralTaskResponse")
        assert "404" in detail_get["responses"]

        _exec(
            """
            UPDATE oral_tasks SET status = 'SUBMISSION_UNCERTAIN',
                provider_charge_state = 'UNKNOWN' WHERE id = %s
            """,
            (uncertain.task_id,),
        )
        single = client.get(f"/api/oral/tasks/{uncertain.task_id}", headers=headers)
        assert single.json()["available_actions"] == []
        assert single.json()["billing_status"] == "RESERVED"

        _exec(
            """
            UPDATE oral_tasks SET status = 'ARCHIVE_FAILED',
                provider_result_url = 'https://tmp.example/a.mp4' WHERE id = %s
            """,
            (uncertain.task_id,),
        )
        single = client.get(f"/api/oral/tasks/{uncertain.task_id}", headers=headers)
        assert single.json()["available_actions"] == ["archive_retry"]

        # 归档失败但没有成片地址：与 archive-retry 路由守卫一致，不给动作。
        _exec(
            "UPDATE oral_tasks SET provider_result_url = NULL WHERE id = %s",
            (uncertain.task_id,),
        )
        single = client.get(f"/api/oral/tasks/{uncertain.task_id}", headers=headers)
        assert single.json()["available_actions"] == []

        cancel_response = client.post(f"/api/oral/tasks/{cancelled.task_id}/cancel")
        assert cancel_response.status_code == 200
        assert cancel_response.json()["billing_status"] == "RELEASED"

        _exec(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id
            ) VALUES (
                'oral-final-result', NULL, 'oral_video', 'local://assets/final.mp4',
                'final-hash', 9, 'video/mp4', 'employee_1'
            )
            """
        )
        _exec(
            "UPDATE oral_tasks SET status = 'SUCCEEDED', duration_sec=1, result_asset_id = "
            "'oral-final-result' "
            "WHERE id = %s",
            (uncertain.task_id,),
        )
        _finalize_billing(oral_task_id=uncertain.task_id)
        single = client.get(f"/api/oral/tasks/{uncertain.task_id}", headers=headers)
        assert single.json()["billing_status"] == "SETTLED"
        assert single.json()["available_actions"] == []

        listing = client.get("/api/oral/tasks", headers=headers).json()
        assert listing["total"] == 2
        by_id = {entry["id"]: entry for entry in listing["items"]}
        assert by_id[uncertain.task_id]["billing_status"] == "SETTLED"
        assert by_id[cancelled.task_id]["billing_status"] == "RELEASED"
        second_page = client.get(
            "/api/oral/tasks",
            params={"limit": 1, "offset": 1},
            headers=headers,
        ).json()
        assert second_page["total"] == 2
        assert second_page["limit"] == 1
        assert second_page["offset"] == 1
        assert len(second_page["items"]) == 1
    finally:
        app.dependency_overrides.clear()

    assert _wallet("employee_1") == (19, 0)


def test_new_avatar_request_accepts_video_only() -> None:
    from pydantic import ValidationError

    from app.oral_routes import AvatarCloneRequest

    fields = dict(
        identity_id="ident-1",
        title="视频分身",
        source_asset_id="asset-src",
        consent_id="consent-1",
        idempotency_key="video-only-key",
    )
    assert AvatarCloneRequest(**fields, source_kind="VIDEO").source_kind == "VIDEO"
    with pytest.raises(ValidationError):
        AvatarCloneRequest(**fields, source_kind="IMAGE")
