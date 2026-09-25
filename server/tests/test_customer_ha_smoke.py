"""T36 deployment and readiness contracts for customer staging.

These tests deliberately prove the repository-owned topology without claiming
that a laptop is a real HA environment. Live two-API/four-worker/PG-HA/private-
COS evidence is recorded only after the deployment runbook is executed on the
staging hosts.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_w19_startup_ids_distinguish_identical_container_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import worker_identity

    monkeypatch.setattr(worker_identity.os, "getpid", lambda: 1)
    monkeypatch.setattr(worker_identity.socket, "gethostname", lambda: "same-host")
    ids = [worker_identity.new_worker_instance_id("generation-worker", "pool-a") for _ in range(4)]
    assert len(set(ids)) == 4
    assert all(value.startswith("pool-a:same-host:1:") for value in ids)
    unsafe = worker_identity.new_worker_instance_id("publish-worker", "line\n%(message)s" * 50)
    assert len(unsafe) <= 160
    assert "\n" not in unsafe and "%" not in unsafe


def test_w19_generation_cli_reuses_one_instance_id_and_changes_it_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app import bootstrap, generation_worker

    observed: list[str] = []
    closed: list[bool] = []
    monkeypatch.setattr(sys, "argv", ["worker", "--once", "--worker-id", "pool-a"])
    monkeypatch.setattr(
        generation_worker,
        "resolve_database_config",
        lambda: SimpleNamespace(mode=generation_worker.DatabaseMode.POSTGRESQL),
    )
    monkeypatch.setattr(generation_worker, "validate_customer_production", lambda _: None)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)
    monkeypatch.setattr(bootstrap, "is_customer_production", lambda: False)
    monkeypatch.setattr(
        generation_worker,
        "check_pg_ready",
        lambda: SimpleNamespace(pool_size=1, server_now=datetime.now(UTC)),
    )
    monkeypatch.setattr(generation_worker, "close_pg_pool", lambda: closed.append(True))

    def process(**kwargs: Any) -> int:
        observed.append(kwargs["worker_id"])
        return 1

    monkeypatch.setattr(generation_worker, "run_pg_worker_round", process)
    generation_worker.main()
    generation_worker.main()
    assert len(observed) == len(closed) == 2
    assert observed[0] != observed[1]
    assert all(value.startswith("pool-a:") for value in observed)


def test_liveness_and_readiness_are_separate_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    # CW-042-b: the lifespan requires a PG DSN in every environment; /ready
    # reports the postgresql database label with local dev storage.
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        os.environ.get(
            "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
        ),
    )
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/live").json() == {
            "status": "ok",
            "service": "video-replica-api",
        }
        assert client.get("/ready").json() == {
            "status": "ready",
            "service": "video-replica-api",
            "database": "postgresql",
            "storage": "local",
        }


def test_customer_runtime_dependency_gate_requires_private_cos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda command: "/usr/bin/ffprobe" if command == "ffprobe" else None,
    )
    ready = SimpleNamespace(pool_size=4)
    monkeypatch.setattr(bootstrap, "check_pg_ready", lambda: ready)

    @contextmanager
    def fake_transaction() -> Any:
        yield SimpleNamespace(row_factory=None)

    class MissingCosRepository:
        def __init__(self, _: object) -> None:
            pass

        def read_runtime_settings(self) -> dict[str, object]:
            return {"active_storage_provider": "cos"}

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "cos"
            return {}

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(bootstrap, "SettingsRepository", MissingCosRepository)

    def unexpected_storage(_: object) -> None:
        raise AssertionError("storage must not initialize with incomplete COS settings")

    monkeypatch.setattr(bootstrap, "create_storage_adapter", unexpected_storage)

    with pytest.raises(RuntimeError, match="private COS"):
        bootstrap.check_customer_production_runtime_dependencies()


def test_customer_runtime_dependency_gate_accepts_pg_and_cos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda command: "/usr/bin/ffprobe" if command == "ffprobe" else None,
    )
    ready = SimpleNamespace(pool_size=4)
    monkeypatch.setattr(bootstrap, "check_pg_ready", lambda: ready)

    events: list[str] = []

    @contextmanager
    def fake_transaction() -> Any:
        events.append("pg-enter")
        try:
            yield SimpleNamespace(row_factory=None)
        finally:
            events.append("pg-exit")

    class ConfiguredCosRepository:
        def __init__(self, _: object) -> None:
            pass

        def read_runtime_settings(self) -> dict[str, object]:
            return {"active_storage_provider": "cos"}

        def load_provider_config(self, provider: str) -> dict[str, str]:
            assert provider == "cos"
            return {
                "access_key_id": "placeholder-access-id",
                "secret_access_key": "placeholder-secret",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
            }

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(bootstrap, "SettingsRepository", ConfiguredCosRepository)

    probes: list[str] = []

    class ReachableStorage:
        def check_readiness(self) -> None:
            assert events == ["pg-enter", "pg-exit"]
            events.append("cos-bucket-head")
            probes.append("bucket")

        # CW-031: the readiness gate now also probes the formal-service write
        # path (put then delete under a reserved prefix) after the bucket HEAD.
        def put_object(self, key: str, content: bytes, *, content_type: str) -> None:
            assert key.startswith(".cw031-readiness/")
            assert events == ["pg-enter", "pg-exit", "cos-bucket-head"]
            probes.append("write-probe-put")

        def delete_object(self, key: str, *, actor_id: str | None = None) -> None:
            assert key.startswith(".cw031-readiness/")
            probes.append("write-probe-delete")

    monkeypatch.setattr(
        bootstrap,
        "create_storage_adapter",
        lambda _: ReachableStorage(),
    )

    assert bootstrap.check_customer_production_runtime_dependencies() is ready
    assert probes == ["bucket", "write-probe-put", "write-probe-delete"]
    assert events == ["pg-enter", "pg-exit", "cos-bucket-head"]


def test_customer_runtime_dependency_gate_requires_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)

    def unexpected_pg_probe() -> None:
        raise AssertionError("PostgreSQL must not be probed without ffprobe")

    monkeypatch.setattr(bootstrap, "check_pg_ready", unexpected_pg_probe)

    with pytest.raises(RuntimeError, match="requires ffprobe"):
        bootstrap.check_customer_production_runtime_dependencies()


def test_empty_customer_bootstrap_provisions_first_admin_and_cos_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        "postgresql://u:p@db.example.test/customer?sslmode=verify-full",
    )
    monkeypatch.setenv("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_V2", "k" * 64)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)
    monkeypatch.setattr(
        bootstrap,
        "issue_exchange_credential",
        lambda actor_id, **_: f"credential-for-{actor_id}",
    )

    events: list[tuple[str, tuple[object, ...]]] = []

    class Cursor:
        def __init__(self, *, row: tuple[object, ...] | None = None, rowcount: int = 1) -> None:
            self._row = row
            self.rowcount = rowcount

        def fetchone(self) -> tuple[object, ...] | None:
            return self._row

    class FakeBusinessConnection:
        def execute(self, sql: str, params: tuple[object, ...] = ()) -> Cursor:
            normalized = " ".join(sql.split())
            events.append((normalized, params))
            if "AS has_users" in normalized:
                return Cursor(row=(False, False, False, True))
            return Cursor()

    fake_conn = FakeBusinessConnection()

    @contextmanager
    def fake_transaction(**_: object) -> Any:
        yield object()

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(
        bootstrap,
        "BusinessConnection",
        SimpleNamespace(postgres=lambda _: fake_conn),
    )

    saved: list[tuple[dict[str, str], str]] = []

    class FakeSettingsRepository:
        def __init__(self, conn: object) -> None:
            assert conn is fake_conn

        def save_provider_config(
            self,
            provider: str,
            config: dict[str, str],
            *,
            actor_user_id: str,
        ) -> dict[str, object]:
            assert provider == "cos"
            saved.append((config, actor_user_id))
            return {"provider": "cos", "configured": True}

    monkeypatch.setattr(bootstrap, "SettingsRepository", FakeSettingsRepository)

    result = bootstrap.provision_empty_customer(
        admin_username="first-admin",
        admin_display_name="First Admin",
        cos_config={
            "access_key_id": "placeholder-access-id",
            "secret_access_key": "placeholder-secret",
            "bucket": "private-staging-bucket",
            "region": "ap-shanghai",
        },
        confirm_empty_database=True,
    )

    assert result.admin_user_id
    assert result.exchange_credential == f"credential-for-{result.admin_user_id}"
    assert saved == [
        (
            {
                "access_key_id": "placeholder-access-id",
                "secret_access_key": "placeholder-secret",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
            },
            result.admin_user_id,
        )
    ]
    assert any("pg_advisory_xact_lock" in sql for sql, _ in events)
    assert any("INSERT INTO users" in sql for sql, _ in events)
    assert any("INSERT INTO wallets" in sql for sql, _ in events)
    assert any("UPDATE runtime_settings" in sql for sql, _ in events)
    audit_params = next(params for sql, params in events if "INSERT INTO audit_logs" in sql)
    assert "placeholder-secret" not in repr(audit_params)


@pytest.mark.parametrize(
    "bootstrap_state",
    [
        (True, False, False, True),
        (False, True, False, True),
        (False, False, True, True),
        (False, False, False, False),
    ],
)
def test_empty_customer_bootstrap_refuses_a_nonpristine_database(
    monkeypatch: pytest.MonkeyPatch,
    bootstrap_state: tuple[object, ...],
) -> None:
    from app import bootstrap

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        "postgresql://u:p@db.example.test/customer?sslmode=verify-full",
    )
    monkeypatch.setenv("VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY_V2", "k" * 64)
    monkeypatch.delenv("VIDEO_REPLICA_DB_PATH", raising=False)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)

    minted: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "issue_exchange_credential",
        lambda actor_id, **_kwargs: minted.append(actor_id) or "secret",
    )

    class ExistingCursor:
        rowcount = 1

        def fetchone(self) -> tuple[object, ...]:
            return bootstrap_state

    class ExistingConnection:
        def execute(self, _sql: str, _params: tuple[object, ...] = ()) -> ExistingCursor:
            return ExistingCursor()

    @contextmanager
    def fake_transaction(**_: object) -> Any:
        yield object()

    monkeypatch.setattr(bootstrap, "pg_transaction", fake_transaction)
    monkeypatch.setattr(
        bootstrap,
        "BusinessConnection",
        SimpleNamespace(postgres=lambda _: ExistingConnection()),
    )

    with pytest.raises(RuntimeError, match="pristine, fully migrated PostgreSQL database"):
        bootstrap.provision_empty_customer(
            admin_username="first-admin",
            admin_display_name="First Admin",
            cos_config={
                "access_key_id": "placeholder-access-id",
                "secret_access_key": "placeholder-secret",
                "bucket": "private-staging-bucket",
                "region": "ap-shanghai",
            },
            confirm_empty_database=True,
        )
    assert minted == []


def test_customer_readiness_returns_generic_503_without_leaking_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")

    def unavailable() -> None:
        raise RuntimeError("postgresql://user:secret@db/private")

    monkeypatch.setattr(main, "check_customer_production_runtime_dependencies", unavailable)
    assert not inspect.iscoroutinefunction(main.ready)
    response = main.ready()

    assert response.status_code == 503
    assert json.loads(response.body) == {
        "status": "not_ready",
        "service": "video-replica-api",
        "code": "RUNTIME_DEPENDENCY_UNAVAILABLE",
    }
    assert b"secret" not in response.body


def test_direct_customer_worker_fails_before_entering_loop_when_cos_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import bootstrap, generation_worker

    monkeypatch.setattr(sys, "argv", ["generation-worker"])
    monkeypatch.setattr(
        generation_worker,
        "resolve_database_config",
        lambda: SimpleNamespace(mode=generation_worker.DatabaseMode.POSTGRESQL),
    )
    monkeypatch.setattr(generation_worker, "validate_customer_production", lambda _: None)
    monkeypatch.setattr(bootstrap, "assert_customer_production_security", lambda: None)
    monkeypatch.setattr(bootstrap, "is_customer_production", lambda: True)

    def unavailable() -> None:
        raise RuntimeError("customer production requires private COS")

    monkeypatch.setattr(
        bootstrap,
        "check_customer_production_runtime_dependencies",
        unavailable,
    )
    monkeypatch.setattr(
        generation_worker,
        "run_forever_pg",
        lambda **_: pytest.fail("worker loop must not start"),
    )

    with pytest.raises(RuntimeError, match="private COS"):
        generation_worker.main()


def test_nginx_contract_load_balances_two_apis_and_rewrites_forwarding_headers() -> None:
    nginx = _read("deploy/nginx/customer.conf.example")

    assert "server 127.0.0.1:8001" in nginx
    assert "server 127.0.0.1:8002" in nginx
    assert "zone video_replica_customer_api 64k;" in nginx
    assert "location = /health" in nginx
    assert "location = /live" in nginx
    assert "location = /ready" in nginx
    assert "location ^~ /api/" in nginx
    assert "try_files $uri $uri/ /index.html;" in nginx
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in nginx
    assert "proxy_set_header X-Forwarded-Proto https;" in nginx
    assert "proxy_set_header Host $host;" in nginx
    assert "$proxy_add_x_forwarded_for" not in nginx
    assert "proxy_next_upstream_tries 2;" in nginx
    assert nginx.count("max_fails=1 fail_timeout=30s") == 2
    live = nginx.split("location = /live", 1)[1].split("}", 1)[0]
    assert "proxy_set_header X-Forwarded-For 127.0.0.2;" in live
    health = nginx.split("location = /health", 1)[1].split("}", 1)[0]
    assert "proxy_pass http://video_replica_customer_api;" in health
    assert "proxy_set_header X-Forwarded-For 127.0.0.2;" in health
    ready = nginx.split("location = /ready", 1)[1].split("}", 1)[0]
    assert "allow 127.0.0.1;" in ready
    assert "allow ::1;" in ready
    assert "deny all;" in ready
    assert "proxy_set_header X-Forwarded-For 127.0.0.2;" in ready
    assert "proxy_set_header X-Forwarded-For $remote_addr;" not in ready
    assert "proxy_next_upstream error timeout http_502 http_503 http_504;" in ready
    api = nginx.split("location ^~ /api/", 1)[1].split("}", 1)[0]
    assert "proxy_next_upstream error timeout;" in api
    assert "http_502" not in api
    assert "http_503" not in api
    assert "http_504" not in api


def test_systemd_contract_has_two_api_ports_and_four_independent_workers() -> None:
    api = _read("deploy/systemd/video-replica-api@.service")
    worker = _read("deploy/systemd/video-replica-worker@.service")

    assert "EnvironmentFile=/etc/video-replica/customer.env" in api
    assert "ExecStartPre=/usr/bin/test -x /usr/bin/ffprobe" in api
    assert "python -m app.bootstrap" in api
    assert "--host 127.0.0.1 --port %i --no-proxy-headers" in api
    assert "EnvironmentFile=/etc/video-replica/customer.env" in worker
    assert "ExecStartPre=/usr/bin/test -x /usr/bin/ffprobe" in worker
    assert "python -m app.bootstrap" in worker
    assert "python -m app.generation_worker" in worker
    assert "--worker-id %H-worker-%i" in worker
    assert "Requires=video-replica-api" not in worker
    assert "PartOf=video-replica-api" not in worker


def test_maintenance_and_pg_migration_are_single_owner_fail_closed_jobs() -> None:
    maintenance = _read("deploy/systemd/video-replica-maintenance.service")
    migration = _read("deploy/postgres/migrate.sh")

    assert "Type=oneshot" in maintenance
    assert "EnvironmentFile=/etc/video-replica/customer.env" in maintenance
    assert maintenance.count("ExecStart=") >= 4
    assert "python -m scripts.reconcile_dangling_billing_reservations" in maintenance
    assert "flock -n" in migration
    assert "alembic upgrade head" in migration
    assert "VIDEO_REPLICA_DATABASE_URL" in migration
    assert "VIDEO_REPLICA_CUSTOMER_PRODUCTION" in migration
    assert migration.index("validate_customer_production") < migration.index("alembic upgrade head")
    assert "set -euo pipefail" in migration


def test_t37_metrics_and_cluster_alert_jobs_are_private_single_owner_contracts() -> None:
    nginx = _read("deploy/nginx/customer.conf.example")
    service = _read("deploy/systemd/video-replica-ops-alerts.service")
    timer = _read("deploy/systemd/video-replica-ops-alerts.timer")
    environment = _read("deploy/customer.env.example")
    probe = _read("server/scripts/check_ops_alerts.py")

    for instance, port in (("api-1", "8001"), ("api-2", "8002")):
        block = nginx.split(f"location = /internal/metrics/{instance}", 1)[1].split("}", 1)[0]
        assert f"proxy_pass http://127.0.0.1:{port}/metrics;" in block
        assert "allow 127.0.0.1;" in block
        assert "allow ::1;" in block
        assert "deny all;" in block
        assert "proxy_set_header Host $host;" in block
        assert "proxy_set_header X-Forwarded-Proto https;" in block
        assert "proxy_set_header X-Forwarded-For 127.0.0.2;" in block

    assert "VIDEO_REPLICA_METRICS_TOKEN_FILE=/etc/video-replica/metrics.token" in environment
    assert "EnvironmentFile=/etc/video-replica/customer.env" in service
    assert "python -m scripts.check_ops_alerts" in service
    assert "StateDirectory=" not in service
    assert "--state-file" not in service
    assert "OnUnitActiveSec=60s" in timer
    assert "RandomizedDelaySec" not in timer
    assert "pg_try_advisory_lock" in probe
    assert "pg_try_advisory_xact_lock" not in probe
    assert "ops_alert_state" in probe
    assert "FROM pg_stat" not in probe
    assert "JOIN pg_stat" not in probe


def test_customer_desktop_build_is_the_sole_default_target() -> None:
    # CW-020 made the customer cloud edition the *sole default* build target;
    # CW-021 went further and withdrew the internal edition entirely: the
    # opt-in overlay, the local-sidecar Cargo feature, and every internal
    # build/check script are gone. A bare `tauri build` / `npm run tauri:build`
    # produces the customer bundle and no internal release can be produced at
    # all, neither accidentally nor deliberately.
    package = _read("package.json")
    root_package = json.loads(package)
    client_package = json.loads(_read("client/package.json"))
    package_lock = json.loads(_read("package-lock.json"))
    cargo_toml = _read("client/src-tauri/Cargo.toml")
    cargo_lock = _read("client/src-tauri/Cargo.lock")
    base_config = json.loads(_read("client/src-tauri/tauri.conf.json"))
    customer_overlay = json.loads(_read("client/src-tauri/tauri.customer.conf.json"))
    workflow = _read(".github/workflows/ci.yml")
    origin_guard = _read("scripts/require_customer_api_base.mjs")
    customer_installer_hooks = _read("client/src-tauri/customer-installer-hooks.nsh")
    server_package = _read("server/pyproject.toml")
    server_main = _read("server/app/main.py")

    # --- base config IS the customer foundation (the sole default) ---
    assert base_config["identifier"] == "com.xiangshu.video-replica.customer"
    assert base_config["productName"] == "众墅之家 AI 爆款视频工具"
    assert base_config["app"]["windows"][0]["url"] == "customer"
    assert base_config["app"]["windows"][0]["title"] == ""
    # 2026-09-23 用户明确授权：客户云客户端带 ffmpeg，用于本地抽音轨（P2 本地
    # 缓存）。这是客户端自身能力，不是本地后端启动器——后者仍然一个都不许有，
    # 因此改为精确白名单而不是「必须为空」。
    assert base_config["bundle"]["resources"] == ["resources/ffmpeg/*"]
    assert base_config["bundle"]["publisher"] == "Xiangshu Video Replica"
    assert base_config["bundle"]["windows"]["nsis"]["startMenuFolder"] == "众墅之家 AI 爆款视频工具"
    # No local-API loopback in the default CSP: the customer WebView only ever
    # talks to an explicit HTTPS backend.
    assert "127.0.0.1:8000" not in base_config["app"]["security"]["csp"]
    # The legacy-internal migration hook is release-specific and must NOT live in
    # the base, else the internal overlay would silently inherit it; it stays in
    # the customer overlay.
    assert "installerHooks" not in base_config["bundle"]["windows"]["nsis"]

    # --- customer overlay is thin: only the release-specific installer hook ---
    assert (
        customer_overlay["bundle"]["windows"]["nsis"]["installerHooks"]
        == "customer-installer-hooks.nsh"
    )

    # --- CW-021: the internal edition is fully withdrawn ---
    # The opt-in internal overlay, the local-sidecar Cargo feature, and every
    # internal build/check script are gone; no internal edition can be built,
    # not even deliberately, and certainly not by accident.
    internal_overlay_path = REPO_ROOT / "client/src-tauri/tauri.internal.conf.json"
    assert not internal_overlay_path.exists(), (
        "CW-021 requires the internal edition overlay to be removed entirely"
    )

    # --- Cargo: the local-sidecar feature no longer exists at all ---
    cargo_features = tomllib.loads(cargo_toml).get("features", {})
    assert "local-sidecar" not in cargo_features, (
        "CW-021 removes the local-sidecar feature entirely"
    )
    assert "local-sidecar" not in cargo_toml
    assert "default = []" in cargo_toml or "[features]" not in cargo_toml

    # --- package.json: default build/check = customer; no internal scripts ---
    scripts = root_package["scripts"]
    assert '"check:tauri:customer"' not in package
    assert '"check:tauri:internal"' not in package
    assert '"tauri:build:internal"' not in package
    assert '"tauri:dev:internal"' not in package
    assert "local-sidecar" not in package
    assert "tauri.internal.conf.json" not in package
    assert "require:customer-api-base" in scripts["tauri:build"]
    assert "--no-default-features" in scripts["tauri:build"]
    assert "--config src-tauri/tauri.customer.conf.json" in scripts["tauri:build"]
    assert '"tauri:build:customer"' in package
    assert '"require:customer-api-base"' in package

    # --- address-failure guard (unchanged contract) ---
    assert "VITE_API_BASE_URL must be a routable, non-loopback HTTPS origin" in origin_guard
    assert 'addSubnet("127.0.0.0", 8, "ipv4")' in origin_guard
    assert 'addAddress("::1", "ipv6")' in origin_guard
    assert "url.port" in origin_guard

    # --- version chain (single source: the base config) ---
    assert base_config["version"] == "2.4.0"
    assert root_package["version"] == base_config["version"]
    assert client_package["version"] == base_config["version"]
    assert package_lock["version"] == base_config["version"]
    assert package_lock["packages"][""]["version"] == base_config["version"]
    assert package_lock["packages"]["client"]["version"] == base_config["version"]
    assert 'name = "video-replica-desktop"\nversion = "2.4.0"' in cargo_lock
    assert 'version = "2.4.0"' in cargo_toml.split("[lib]", maxsplit=1)[0]
    assert 'version = "2.4.0"' in server_package.split("[project]", maxsplit=1)[1]
    assert 'version="2.4.0"' in server_main

    # --- installer hook content (unchanged) ---
    assert "$LOCALAPPDATA\\短视频复刻工作台\\uninstall.exe" in customer_installer_hooks
    assert (
        "ExecWait '\"$LOCALAPPDATA\\短视频复刻工作台\\uninstall.exe\" /S'"
        in customer_installer_hooks
    )
    assert "IfErrors legacy_internal_failed" in customer_installer_hooks
    assert "IntCmp $0 0 legacy_internal_done" in customer_installer_hooks
    assert "Abort" in customer_installer_hooks

    # --- ci.yml only ever builds the customer cloud bundle (CW-021) ---
    assert "npm run check:tauri:internal" not in workflow
    assert "npm run tauri:build:internal" not in workflow
    assert "npm run tauri:build:customer" in workflow
    assert "npm run check:tauri:customer" not in workflow
    assert "VITE_API_BASE_URL: https://staging.example.invalid" in workflow
    assert "Archive unsigned internal NSIS installer locally" not in workflow
    assert "Archive unsigned customer cloud NSIS installer locally" in workflow
    assert "LOCAL_ARTIFACT_ROOT" in workflow
    assert "SHA256SUMS.txt" in workflow
    # CW-021 widened payload detection keeps enforcing the no-local-backend
    # contract at the artifact level (launchers, server/Python runtime, FFmpeg,
    # SQLite business database, boot command and business port markers).
    assert "Verify customer installer excludes local backend distribution" in workflow
    # Launcher names only appear in the payload gate's forbidden list.
    workflow_before_payload_gate = workflow.split(
        "Verify customer installer excludes local backend distribution", 1
    )[0]
    assert "start-backend" not in workflow_before_payload_gate
    assert "start-backend.bat" in workflow
    assert "start-backend.sh" in workflow
    assert "VIDEO_REPLICA_BOOT_COMMAND" in workflow
    assert "127.0.0.1:8000" in workflow


def test_release_desktop_uses_windows_gui_subsystem() -> None:
    desktop_entrypoint = _read("client/src-tauri/src/main.rs")

    assert desktop_entrypoint.startswith(
        '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]\n'
    )


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.2",
        "https://localhost.",
        "https://api.localhost",
        "https://[::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[::ffff:7f00:1]",
        "https://0.0.0.0",
        "https://[::]",
        "https://[::ffff:0.0.0.0]",
        "https://169.254.169.254",
        "https://224.0.0.1",
        "https://240.0.0.1",
        "https://192.0.2.1",
        "https://198.18.0.1",
        "https://[fe80::1]",
        "https://[ff02::1]",
        "https://[2001:db8::1]",
        "https://staging.example.invalid:8443",
    ],
)
def test_customer_cloud_build_rejects_non_destination_origins(origin: str) -> None:
    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = origin
    result = subprocess.run(
        ["node", "scripts/require_customer_api_base.mjs"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "non-loopback HTTPS origin" in result.stderr


def test_customer_cloud_build_accepts_a_remote_https_origin() -> None:
    env = os.environ.copy()
    env["VITE_API_BASE_URL"] = "https://staging.example.invalid"
    result = subprocess.run(
        ["node", "scripts/require_customer_api_base.mjs"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
