"""CHARACTER-VIEW-APILIO 角色五视图生成链路测试。

单元层覆盖 ApilioCharacterImageProvider 的 prompt 组合、模型尺寸映射、错误码
映射、多格式输出校验与质检尺寸解析；PG 层覆盖注册表解析（未配置 503 / 配置
缺失回队列）、配置保存后的 worker 全链路（任务 → 质检 → 计费 → REVIEWING）
与终态失败的结算释放。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

# 审计写入器要求 HMAC key，必须在导入 app 模块之前设置。
os.environ.setdefault(
    "VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY",
    "test-key-for-character-provider-tests-minimum-48-bytes-long-key-1",
)

import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)
from psycopg.rows import dict_row

from app.auth import CurrentUser
from app.character_asset_quality import image_dimensions, inspect_character_asset
from app.character_identity import REQUIRED_CHARACTER_VIEW_TYPES, create_character_version
from app.character_image_generation import (
    CharacterGenerationTask,
    CharacterImageProvider,
    CharacterImageProviderFailed,
    CharacterImageRequest,
    CharacterImageResult,
    acquire_character_generation_task,
    character_provider_for_name,
    create_character_generation_tasks,
    resolve_character_image_provider,
    run_next_character_generation_task,
    validate_character_image_result,
)
from app.character_image_provider import (
    APILIO_CHARACTER_PROVIDER,
    CHARACTER_VIEW_PIXEL_SIZE,
    ApilioCharacterImageProvider,
    apilio_character_model_parameters,
    build_character_view_prompt,
    character_persona_prompt_lines,
    character_source_image,
)
from app.db_pg import DATABASE_URL_ENV, close_pg_pool, pg_transaction
from app.db_portable import BusinessConnection
from app.first_frames import (
    APILIO_DEFAULT_BASE_URL,
    ApilioImageProvider,
    ImageProviderFailed,
    RetryableImageProviderFailed,
)
from app.settings import SettingsRepository
from app.storage import FakeStorageAdapter, StorageAdapter

# ---------------------------------------------------------------------------
# Deterministic image builders (magic bytes only — enough for sniff + dims)
# ---------------------------------------------------------------------------


def png_with_dimensions(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
    )


def jpeg_with_dimensions(width: int, height: int) -> bytes:
    sof = (
        b"\xff\xc0"
        + (17).to_bytes(2, "big")
        + b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + (16).to_bytes(2, "big")
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + sof
        + b"\xff\xd9"
    )


def webp_vp8l_with_dimensions(width: int, height: int) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    return (
        b"RIFF"
        + (0).to_bytes(4, "little")
        + b"WEBP"
        + b"VP8L"
        + (5).to_bytes(4, "little")
        + b"\x2f"
        + bits.to_bytes(4, "little")
    )


PNG_VIEW = png_with_dimensions(1024, 1536)
_SOURCE_JPEG = jpeg_with_dimensions(940, 1672)


def _b64_response(content: bytes, *, mime_type: str | None = None) -> bytes:
    item: dict[str, str] = {"b64_json": base64.b64encode(content).decode("ascii")}
    if mime_type is not None:
        item["mime_type"] = mime_type
    return json.dumps({"data": [item]}).encode()


@dataclass
class FakeApilioTransport:
    response_body: bytes = b"{}"
    post_error: Exception | None = None
    requests: list[tuple[str, Mapping[str, str], bytes]] = field(default_factory=list)

    def post(
        self, url: str, *, headers: Mapping[str, str], body: bytes
    ) -> tuple[bytes, Mapping[str, str]]:
        self.requests.append((url, headers, body))
        if self.post_error is not None:
            raise self.post_error
        return self.response_body, {"content-type": "application/json"}


def _character_provider(transport: FakeApilioTransport) -> ApilioCharacterImageProvider:
    return ApilioCharacterImageProvider(
        client=ApilioImageProvider(api_key="test-key", transport=transport)
    )


def _view_request(**overrides: object) -> CharacterImageRequest:
    values: dict[str, object] = {
        "task_id": "task-1",
        "character_version_id": "version-1",
        "view_type": "FRONT_FACE",
        "candidate_number": 1,
        "attempt": 0,
        "model": "gpt-image-2",
        "source_sha256": hashlib.sha256(_SOURCE_JPEG).hexdigest(),
        "source_content": _SOURCE_JPEG,
        "persona_snapshot": {
            "name": "张工",
            "occupation": "设计师",
            "costume_description": "深色工装",
            "appearance_constraints_json": {"glasses": "none"},
        },
        "provider_parameters": {},
        "template_version": "character-prompt-v1",
        "template_hash": "hash",
    }
    values.update(overrides)
    return CharacterImageRequest(**values)  # type: ignore[arg-type]


class StubApilioProvider:
    """满足 CharacterImageProvider 协议的成功/失败替身，不发起任何网络调用。"""

    provider_name = APILIO_CHARACTER_PROVIDER

    def __init__(self, *, failures: list[CharacterImageProviderFailed] | None = None) -> None:
        self.failures = list(failures or [])
        self.requests: list[CharacterImageRequest] = []

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        return CharacterImageResult(
            content=PNG_VIEW,
            content_type="image/png",
            provider_task_id=f"stub-apilio-{request.task_id[:8]}",
            cost_amount=0.5,
        )


# ---------------------------------------------------------------------------
# Unit lane — prompt composition / size mapping / error mapping
# ---------------------------------------------------------------------------


def test_generate_view_uses_gpt_size_override_and_reports_result() -> None:
    transport = FakeApilioTransport(response_body=_b64_response(PNG_VIEW))
    provider = _character_provider(transport)

    result = provider.generate_view(_view_request(model="gpt-image-2"))

    assert result.content == PNG_VIEW
    assert result.content_type == "image/png"
    assert result.provider_task_id == (
        f"apilio-character-{hashlib.sha256(PNG_VIEW).hexdigest()[:24]}"
    )
    assert result.cost_amount == 0.0

    url, headers, body = transport.requests[0]
    assert url == "https://api.apilio.ai/v1/images/edits"
    assert headers["Authorization"] == "Bearer test-key"
    assert b'name="model"\r\n\r\ngpt-image-2' in body
    # gpt-image-2 只认 size：aspect_ratio 必须缺席，尺寸由 size 兑现。
    assert b'name="size"\r\n\r\n1024x1536' in body
    assert b'name="aspect_ratio"' not in body
    assert b'name="response_format"\r\n\r\nb64_json' in body
    assert b'name="n"\r\n\r\n1' in body
    assert b'filename="character-source.jpg"' in body
    # prompt = 视角片段 + persona 行 + 身份不变量。
    assert b"head-and-shoulders close-up" in body
    assert "Persona: 张工 — 设计师.".encode() in body
    assert "Costume: 深色工装".encode() in body
    assert b'Appearance constraints: {"glasses": "none"}' in body
    assert b"Identity invariants: the exact same person" in body


def test_generate_view_uses_nano_aspect_ratio_and_accepts_jpeg_output() -> None:
    view = jpeg_with_dimensions(1024, 1536)
    transport = FakeApilioTransport(response_body=_b64_response(view, mime_type="image/jpeg"))
    provider = _character_provider(transport)

    result = provider.generate_view(_view_request(model="nano-banana-pro-2k"))

    assert result.content == view
    assert result.content_type == "image/jpeg"
    body = transport.requests[0][2]
    assert b'name="aspect_ratio"\r\n\r\n2:3' in body
    assert b'name="image_size"\r\n\r\n2K' in body
    assert b'name="size"\r\n' not in body


def test_apilio_character_model_parameters_map_each_model() -> None:
    assert apilio_character_model_parameters("nano-banana-pro-2k") == ("2:3", None)
    assert apilio_character_model_parameters("gpt-image-2") == (None, CHARACTER_VIEW_PIXEL_SIZE)


def test_unsupported_model_is_rejected_before_transport() -> None:
    transport = FakeApilioTransport(response_body=_b64_response(PNG_VIEW))
    provider = _character_provider(transport)

    with pytest.raises(CharacterImageProviderFailed) as caught:
        provider.generate_view(_view_request(model="legacy-image-model"))

    assert caught.value.code == "CHARACTER_PROVIDER_MODEL_UNSUPPORTED"
    assert caught.value.retriable is False
    assert transport.requests == []


def test_invalid_source_bytes_are_rejected_before_transport() -> None:
    transport = FakeApilioTransport(response_body=_b64_response(PNG_VIEW))
    provider = _character_provider(transport)

    with pytest.raises(CharacterImageProviderFailed) as caught:
        provider.generate_view(_view_request(source_content=b"not-an-image"))

    assert caught.value.code == "CHARACTER_VERSION_SOURCE_INVALID"
    assert caught.value.retriable is False
    assert transport.requests == []


def test_provider_transport_failures_map_to_stable_codes() -> None:
    request = _view_request()

    retryable_transport = FakeApilioTransport(
        post_error=RetryableImageProviderFailed("Apilio returned HTTP 429")
    )
    with pytest.raises(CharacterImageProviderFailed) as retryable:
        _character_provider(retryable_transport).generate_view(request)
    assert retryable.value.code == "CHARACTER_PROVIDER_UNAVAILABLE"
    assert retryable.value.retriable is True
    assert retryable.value.http_status == 429

    terminal_transport = FakeApilioTransport(
        post_error=ImageProviderFailed("Apilio returned HTTP 400")
    )
    with pytest.raises(CharacterImageProviderFailed) as terminal:
        _character_provider(terminal_transport).generate_view(request)
    assert terminal.value.code == "CHARACTER_PROVIDER_FAILED"
    assert terminal.value.retriable is False
    assert terminal.value.http_status == 400


def test_character_source_image_labels_supported_formats() -> None:
    png = character_source_image(png_with_dimensions(9, 9))
    assert (png.content_type, png.filename) == ("image/png", "character-source.png")
    jpeg = character_source_image(jpeg_with_dimensions(9, 9))
    assert (jpeg.content_type, jpeg.filename) == ("image/jpeg", "character-source.jpg")
    webp = character_source_image(webp_vp8l_with_dimensions(9, 9))
    assert (webp.content_type, webp.filename) == ("image/webp", "character-source.webp")
    with pytest.raises(CharacterImageProviderFailed) as caught:
        character_source_image(b"not-an-image")
    assert caught.value.code == "CHARACTER_VERSION_SOURCE_INVALID"


def test_persona_prompt_lines_skip_empty_fields() -> None:
    assert character_persona_prompt_lines({}) == []
    assert character_persona_prompt_lines({"name": " ", "occupation": None}) == []
    lines = character_persona_prompt_lines(
        {
            "name": "张工",
            "occupation": "设计师",
            "scene_description": "工地现场",
            "appearance_constraints_json": {"beard": "none"},
        }
    )
    assert lines == [
        "Persona: 张工 — 设计师.",
        "Scene: 工地现场",
        'Appearance constraints: {"beard": "none"}',
    ]


def test_view_prompt_omits_persona_block_for_empty_snapshot() -> None:
    prompt = build_character_view_prompt(_view_request(persona_snapshot={}))
    assert "head-and-shoulders close-up" in prompt
    assert "Persona:" not in prompt
    assert "Scene:" not in prompt
    assert "Appearance constraints:" not in prompt
    assert prompt.rstrip().endswith("beauty-filter effects.")


def test_validate_character_image_result_accepts_allowed_formats_and_rejects_mismatch() -> None:
    allowed = (
        (PNG_VIEW, "image/png"),
        (jpeg_with_dimensions(4, 4), "image/jpeg"),
        (webp_vp8l_with_dimensions(4, 4), "image/webp"),
    )
    for content, content_type in allowed:
        validate_character_image_result(
            CharacterImageResult(
                content=content,
                content_type=content_type,
                provider_task_id="provider-1",
                cost_amount=0.0,
            )
        )

    with pytest.raises(CharacterImageProviderFailed) as mismatched:
        validate_character_image_result(
            CharacterImageResult(
                content=PNG_VIEW,
                content_type="image/jpeg",
                provider_task_id="provider-1",
                cost_amount=0.0,
            )
        )
    assert mismatched.value.code == "CHARACTER_PROVIDER_INVALID_RESPONSE"

    with pytest.raises(CharacterImageProviderFailed) as negative:
        validate_character_image_result(
            CharacterImageResult(
                content=PNG_VIEW,
                content_type="image/png",
                provider_task_id="provider-1",
                cost_amount=-1.0,
            )
        )
    assert negative.value.code == "CHARACTER_PROVIDER_INVALID_RESPONSE"


@pytest.mark.parametrize(
    "content",
    [
        png_with_dimensions(1024, 1536),
        jpeg_with_dimensions(1024, 1536),
        webp_vp8l_with_dimensions(1024, 1536),
    ],
)
def test_inspect_character_asset_parses_dimensions_per_format(content: bytes) -> None:
    assert image_dimensions(content) == (1024, 1536)
    report = inspect_character_asset(content, view_type="FRONT_FACE")
    assert report["dimensions"] == {"height": 1536, "width": 1024}
    assert cast(dict[str, object], report["checks"])["dimensions"] == "PASS"
    assert report["simulated"] is True


def test_inspect_character_asset_rejects_unknown_bytes() -> None:
    with pytest.raises(ValueError):
        inspect_character_asset(b"not-an-image", view_type="FRONT_FACE")


def test_resolve_character_image_provider_prefers_injection_and_rejects_mismatch() -> None:
    stub = StubApilioProvider()
    no_conn = cast(Any, None)

    assert (
        resolve_character_image_provider(
            no_conn, provider=stub, provider_name=APILIO_CHARACTER_PROVIDER
        )
        is stub
    )

    with pytest.raises(CharacterImageProviderFailed) as caught:
        resolve_character_image_provider(no_conn, provider=stub, provider_name="fake_character")
    assert caught.value.code == "CHARACTER_PROVIDER_MISMATCH"
    assert caught.value.retriable is False


def test_registry_keeps_fake_provider_and_rejects_unknown_names() -> None:
    fake = character_provider_for_name("fake_character")
    assert fake.provider_name == "fake_character"

    with pytest.raises(CharacterImageProviderFailed) as caught:
        character_provider_for_name("mystery_provider")
    assert caught.value.code == "CHARACTER_PROVIDER_NOT_CONFIGURED"
    assert caught.value.retriable is False


# ---------------------------------------------------------------------------
# PG lane — registry resolution + worker chain (dedicated migrated database)
# ---------------------------------------------------------------------------

CHARACTER_GENERATION_DB_NAME = "character_generation_test"

DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

_CLEANUP_ORDER = (
    "billing_operations",
    "operation_cost_records",
    "external_call_logs",
    "character_generation_tasks",
    "character_assets",
    "character_versions",
    "character_personas",
    "person_identities",
    "user_queue_cursors",
    "audit_logs",
    "assets",
    "wallet_transactions",
    "wallets",
    "provider_settings",
    "users",
    "runtime_settings",
    "billing_tariffs",
)

_ADMIN = CurrentUser(id="admin_1", username="admin_1", display_name="Admin One", role="admin")


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module")
def generation_dsn() -> Iterator[str]:
    require_pg_or_explicit_skip(_pg_dsn())
    dsn = create_test_database(CHARACTER_GENERATION_DB_NAME)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CHARACTER_GENERATION_DB_NAME)


@pytest.fixture()
def pg_state(generation_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    close_pg_pool()
    monkeypatch.setenv(DATABASE_URL_ENV, generation_dsn)
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    # Provider 配置加密 key：每个用例独立，避免跨用例复用密文。
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    yield generation_dsn
    close_pg_pool()


def _exec(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute(sql, params)


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as pg:
        return [dict(row) for row in pg.execute(sql, params).fetchall()]


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as pg:
        row = pg.execute(sql, params).fetchone()
        return None if row is None else row[0]


def _truncate(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET session_replication_role = replica")
        pg.execute("TRUNCATE " + ",".join(_CLEANUP_ORDER) + " CASCADE")
        pg.execute("SET session_replication_role = DEFAULT")


def _seed_base(dsn: str) -> None:
    _truncate(dsn)
    _exec(
        dsn,
        "INSERT INTO runtime_settings ("
        " id, max_generation_count_per_batch, max_concurrent_h3_tasks,"
        " internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen,"
        " fair_queue_enabled"
        ") VALUES (1, 4, 100, 1000, 10000, 1000, true)",
    )
    _exec(
        dsn,
        "INSERT INTO users (id, username, display_name, role) VALUES"
        " ('admin_1', 'admin_1', 'Admin One', 'admin')",
    )
    _exec(
        dsn,
        "INSERT INTO wallets (user_id, available_credits, reserved_credits) VALUES"
        " ('admin_1', 1000, 0)",
    )
    _exec(
        dsn,
        "INSERT INTO billing_tariffs (service, enabled, unit_credits, unit_cost_fen) "
        "VALUES ('character', true, 1, 5) ON CONFLICT (service) DO NOTHING",
    )


def _seed_character_graph(
    dsn: str,
    *,
    provider: str = APILIO_CHARACTER_PROVIDER,
    model: str = "gpt-image-2",
) -> dict[str, Any]:
    """播种身份 → 人设 → DRAFT 版本的完整前置，返回 storage 与 version_id。"""
    storage = FakeStorageAdapter(provider="cos", bucket="bucket")
    stored = storage.put_object(
        "users/admin_1/character-sources/source.jpg",
        _SOURCE_JPEG,
        content_type="image/jpeg",
    )
    _exec(
        dsn,
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES "
        "('asset-char-auth', NULL, 'character_source_image', 'cos://bucket/auth.jpg', "
        " %s, 17, 'image/jpeg', 'admin_1')",
        ("f" * 64,),
    )
    _exec(
        dsn,
        "INSERT INTO assets (id, project_id, kind, storage_uri, sha256, size_bytes, "
        "content_type, created_by_user_id) VALUES "
        "('asset-char-src', NULL, 'character_source_image', %s, %s, %s, 'image/jpeg', 'admin_1')",
        (stored.uri, stored.sha256, stored.size),
    )
    _exec(
        dsn,
        "INSERT INTO person_identities (id, owner_user_id, display_name, "
        "authorization_status, authorization_asset_id, source_asset_id, "
        "source_quality_status, created_by) VALUES "
        "('identity-1', 'admin_1', '张工', 'AUTHORIZED', 'asset-char-auth', "
        "'asset-char-src', 'PASSED', 'admin_1')",
    )
    _exec(
        dsn,
        "INSERT INTO character_personas (id, identity_id, name, occupation, "
        "costume_description, appearance_constraints_json, created_by) VALUES "
        "('persona-1', 'identity-1', '张工', '设计师', '深色工装', '{}', 'admin_1')",
    )
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        version = create_character_version(
            conn,
            actor=_ADMIN,
            persona_id="persona-1",
            provider=provider,
            model=model,
            generation_params_json={},
        )
    return {"storage": storage, "version_id": version.id}


def _save_apilio_config(dsn: str, *, api_key: str = "test-key") -> None:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        SettingsRepository(conn).save_provider_config(
            APILIO_CHARACTER_PROVIDER, {"api_key": api_key}, actor_user_id="admin_1"
        )


def _create_tasks(
    dsn: str,
    *,
    version_id: str,
    idempotency_key: str = "ik-character-views",
) -> list[CharacterGenerationTask]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return create_character_generation_tasks(
            conn,
            actor=_ADMIN,
            version_id=version_id,
            idempotency_key=idempotency_key,
            view_types=None,
            candidates_per_view=1,
        )


def _run_character_round(
    *,
    storage: StorageAdapter,
    provider: CharacterImageProvider | None,
    worker_id: str = "character-worker-1",
) -> CharacterGenerationTask | None:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        lease = acquire_character_generation_task(conn, worker_id=worker_id)
    if lease is None:
        return None
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        return run_next_character_generation_task(
            conn, worker_id=worker_id, storage=storage, provider=provider, lease=lease
        )


def test_character_tasks_require_saved_provider_config(pg_state: str) -> None:
    _seed_base(pg_state)
    graph = _seed_character_graph(pg_state)
    version_id = cast(str, graph["version_id"])

    with pytest.raises(HTTPException) as caught:
        _create_tasks(pg_state, version_id=version_id, idempotency_key="ik-blocked")
    assert caught.value.status_code == 503
    assert (
        cast(dict[str, object], caught.value.detail)["code"] == "CHARACTER_PROVIDER_NOT_CONFIGURED"
    )

    # 配置缺失拦在计费之前：没有任务、没有计费请求，版本保持 DRAFT。
    assert _one(pg_state, "SELECT count(*) FROM character_generation_tasks") == 0
    assert _one(pg_state, "SELECT count(*) FROM billing_operations") == 0
    assert (
        _one(pg_state, "SELECT status FROM character_versions WHERE id = %s", (version_id,))
        == "DRAFT"
    )

    # 注册表侧是「可恢复」错误：已入队任务会回队列等管理员补齐配置。
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        with pytest.raises(CharacterImageProviderFailed) as registry:
            character_provider_for_name(APILIO_CHARACTER_PROVIDER, conn=conn)
    assert registry.value.code == "CHARACTER_PROVIDER_NOT_CONFIGURED"
    assert registry.value.retriable is True


def test_saved_config_completes_registry_and_creates_billable_tasks(pg_state: str) -> None:
    _seed_base(pg_state)
    graph = _seed_character_graph(pg_state)
    version_id = cast(str, graph["version_id"])
    _save_apilio_config(pg_state)

    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        provider = character_provider_for_name(APILIO_CHARACTER_PROVIDER, conn=conn)
    assert isinstance(provider, ApilioCharacterImageProvider)
    assert provider.client.api_key == "test-key"
    assert provider.client.base_url == APILIO_DEFAULT_BASE_URL

    tasks = _create_tasks(pg_state, version_id=version_id)
    assert len(tasks) == len(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {task.view_type for task in tasks} == set(REQUIRED_CHARACTER_VIEW_TYPES)
    assert all(task.status == "PENDING" for task in tasks)
    assert all(task.provider == APILIO_CHARACTER_PROVIDER for task in tasks)
    assert (
        _one(pg_state, "SELECT status FROM character_versions WHERE id = %s", (version_id,))
        == "GENERATING"
    )
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM billing_operations WHERE service='character' AND state='PENDING'",
        )
        == 5
    )

    # 幂等重放：同一幂等键返回同一批任务，不重复计费。
    again = _create_tasks(pg_state, version_id=version_id)
    assert [task.id for task in again] == [task.id for task in tasks]
    assert _one(pg_state, "SELECT count(*) FROM billing_operations") == 5


def test_worker_full_chain_reaches_reviewing_with_assets_and_billing(pg_state: str) -> None:
    _seed_base(pg_state)
    graph = _seed_character_graph(pg_state)
    version_id = cast(str, graph["version_id"])
    _save_apilio_config(pg_state)
    tasks = _create_tasks(pg_state, version_id=version_id)
    storage = cast(StorageAdapter, graph["storage"])
    stub = StubApilioProvider()

    for _ in range(len(tasks)):
        result = _run_character_round(storage=storage, provider=stub)
        assert result is not None
        assert result.status == "SUCCEEDED"
    assert _run_character_round(storage=storage, provider=stub) is None
    assert len(stub.requests) == len(tasks)

    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM character_generation_tasks "
            "WHERE character_version_id = %s AND status = 'SUCCEEDED'",
            (version_id,),
        )
        == 5
    )
    assert (
        _one(pg_state, "SELECT status FROM character_versions WHERE id = %s", (version_id,))
        == "REVIEWING"
    )

    rows = _rows(
        pg_state,
        "SELECT ca.view_type, ca.review_status, ca.auto_quality_json, a.kind, "
        "a.content_type, a.sha256 FROM character_assets ca "
        "JOIN assets a ON a.id = ca.asset_id WHERE ca.character_version_id = %s",
        (version_id,),
    )
    assert len(rows) == 5
    assert {row["view_type"] for row in rows} == set(REQUIRED_CHARACTER_VIEW_TYPES)
    assert {row["review_status"] for row in rows} == {"NOT_REVIEWED"}
    assert {row["kind"] for row in rows} == {"character_generated_image"}
    assert {row["content_type"] for row in rows} == {"image/png"}
    assert {row["sha256"] for row in rows} == {hashlib.sha256(PNG_VIEW).hexdigest()}
    quality = json.loads(str(rows[0]["auto_quality_json"]))
    assert quality["dimensions"] == {"height": 1536, "width": 1024}
    assert quality["view_type"] == rows[0]["view_type"]

    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM operation_cost_records WHERE subject='character_sheet_image' "
            "AND status='ACTUAL' AND usage_amount=1 AND cost_fen=5",
        )
        == 5
    )
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM billing_operations WHERE service='character' "
            "AND state='SUCCEEDED' AND actual_units=1 AND charged_credits=1",
        )
        == 5
    )
    # 桩不发起网络调用，因此不会产生调用日志：调用日志只由真实的 recorded_urlopen
    # 入口写入（app.external_calls，ADMIN-P0-20260928 的调用日志收口）。此前这里断言
    # 的是 character_generation.py 的直写路径（已随该收口删除），故改为钉住「桩路径
    # 零写入」这一性质——它同时能抓到「桩路径意外走了网络」的回归。真实 HTTP 调用的
    # 落库与任务归属由 test_external_calls_pg.py 覆盖。
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM external_call_logs "
            "WHERE task_type = 'CHARACTER_VIEW_IMAGE' AND task_id IS NOT NULL",
        )
        == 0
    )
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM audit_logs WHERE action='character_generation.succeeded'",
        )
        == 5
    )
    assert _one(pg_state, "SELECT reserved_credits FROM wallets WHERE user_id='admin_1'") == 0


def test_missing_config_requeues_running_task_until_config_returns(pg_state: str) -> None:
    _seed_base(pg_state)
    graph = _seed_character_graph(pg_state)
    version_id = cast(str, graph["version_id"])
    _save_apilio_config(pg_state)
    _create_tasks(pg_state, version_id=version_id)
    storage = cast(StorageAdapter, graph["storage"])

    # 配置在任务创建后被清除（管理员误删 / 密钥轮换失配）：任务回队列等待补齐，
    # 而不是把已经计费的候选判死。
    _exec(pg_state, "DELETE FROM provider_settings WHERE provider = 'apilio'")
    stalled = _run_character_round(storage=storage, provider=None)
    assert stalled is not None
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM character_generation_tasks WHERE id = %s",
        (stalled.id,),
    )[0]
    assert row["status"] == "PENDING"
    assert row["error_code"] == "CHARACTER_PROVIDER_NOT_CONFIGURED"
    assert _one(pg_state, "SELECT count(*) FROM billing_operations WHERE state='PENDING'") == 5

    # 配置恢复后同一任务被重新领取并正常出图（其余任务置于退避期，隔离断言）。
    _save_apilio_config(pg_state)
    _exec(
        pg_state,
        "UPDATE character_generation_tasks SET next_poll_at = '2999-01-01T00:00:00+00:00' "
        "WHERE id <> %s",
        (stalled.id,),
    )
    _exec(
        pg_state,
        "UPDATE character_generation_tasks SET next_poll_at = NULL WHERE id = %s",
        (stalled.id,),
    )
    stub = StubApilioProvider()
    recovered = _run_character_round(storage=storage, provider=stub)
    assert recovered is not None
    assert recovered.id == stalled.id
    assert recovered.status == "SUCCEEDED"
    assert len(stub.requests) == 1


def test_terminal_provider_failure_settles_billing_and_fails_version_after_drain(
    pg_state: str,
) -> None:
    _seed_base(pg_state)
    graph = _seed_character_graph(pg_state)
    version_id = cast(str, graph["version_id"])
    _save_apilio_config(pg_state)
    _create_tasks(pg_state, version_id=version_id)
    storage = cast(StorageAdapter, graph["storage"])

    failing = StubApilioProvider(
        failures=[
            CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_FAILED",
                "character image provider rejected the request",
                retriable=False,
                http_status=400,
            )
        ]
    )
    result = _run_character_round(storage=storage, provider=failing)
    assert result is not None
    row = _rows(
        pg_state,
        "SELECT status, error_code FROM character_generation_tasks WHERE id = %s",
        (result.id,),
    )[0]
    assert row["status"] == "FAILED"
    assert row["error_code"] == "CHARACTER_PROVIDER_FAILED"

    # 终态失败按 0 单位结算并释放预留；其余任务仍在队列，版本保持 GENERATING。
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM billing_operations WHERE state='FAILED' AND actual_units=0",
        )
        == 1
    )
    assert (
        _one(pg_state, "SELECT status FROM character_versions WHERE id = %s", (version_id,))
        == "GENERATING"
    )
    assert (
        _one(
            pg_state,
            "SELECT count(*) FROM character_assets WHERE character_version_id = %s",
            (version_id,),
        )
        == 0
    )
