"""CW-032 — the customer delivery package is a registered, rebuildable unit.

TEST-LOGIC (offline). The single default package lives in
``deploy/customer/``: ``compose.yaml`` (topology), ``README.md`` (rebuild
manual) and ``bootstrap-base-image.sh`` (first image on a blank host), with
``deploy/customer-git-rollout.sh`` consuming the in-repo compose instead of
an unregistered host file. The machine-checked contracts here:

1. **Topology** — the compose declares exactly the rollout's service set,
   adds the one-shot ``migrate`` role, and never lets api/worker execute
   schema DDL (single-migration execution; no multi-instance DDL races).
2. **Pool budget** — 2 API + 4 worker processes against the compose
   ``max_connections`` leaves the documented headroom, computed from the
   live defaults in ``app.db_pg`` (the numbers cannot drift silently).
3. **Wiring** — nginx upstreams, health checks and the rollout's image-bump
   service names stay consistent with the compose.
4. **Exclusions** — the formal package references no internal-P0/SQLite
   backup unit and no historical operator tooling (PG-08/PG-09); the
   registered package file list is closed.
5. **Fail-fast matrix** — every required dependency (PG DSN, root keys,
   private COS, ffmpeg/ffprobe) maps to the executable acceptance that
   fails fast, and those targets actually exist.

Blank-host rebuild execution itself needs a real Docker host; the command
contracts are pinned here and the real topology acceptance is CW-047.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIR = REPO_ROOT / "deploy" / "customer"
COMPOSE = PACKAGE_DIR / "compose.yaml"
README = PACKAGE_DIR / "README.md"
BOOTSTRAP = PACKAGE_DIR / "bootstrap-base-image.sh"
ROLLOUT = REPO_ROOT / "deploy" / "customer-git-rollout.sh"
NGINX = REPO_ROOT / "deploy" / "nginx" / "customer.conf.example"
LOGGING_YAML = REPO_ROOT / "docker-compose.logging.yml"

# The closed inventory of the formal delivery package.
PACKAGE_FILES = ("compose.yaml", "README.md", "bootstrap-base-image.sh", "healthcheck.py")

ROLLOUT_SERVICES = (
    "api-1",
    "api-2",
    "worker-1",
    "worker-2",
    "worker-3",
    "worker-4",
    "worker-viral",
    "worker-publish",
)

# Fail-fast matrix: dependency -> (executable acceptance target, marker).
FAIL_FAST_MATRIX = {
    "missing-or-invalid-pg-dsn": (
        "server/tests/test_bootstrap_all_env_pg_gate.py",
        "resolve_database_config",
    ),
    "missing-root-keys-production-security-gate": (
        "server/tests/test_customer_ha_smoke.py",
        "assert_customer_production_security",
    ),
    "missing-private-cos": (
        "server/tests/test_customer_ha_smoke.py",
        "check_customer_production_runtime_dependencies",
    ),
    "missing-ffprobe-media-probe": (
        "server/tests/test_customer_ha_smoke.py",
        "customer_runtime_dependency_gate_requires_ffprobe",
    ),
    # CW-042-b: test_script_from_audio.py retired with the SQLite lane;
    # the media-tools resolution surface (which raises MediaToolUnavailable on
    # a missing ffmpeg) stays pinned via test_oral_domain's resolve_media_binary
    # usage on the VIDEO_REPLICA_FFMPEG_DIR-marked CI lane.
    "ffmpeg-unavailable-media-tools": (
        "server/tests/test_oral_domain.py",
        "resolve_media_binary",
    ),
}


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service_block(service: str) -> str:
    """Return the raw YAML block of one top-level service (heuristic split)."""

    text = _compose_text()
    match = re.search(rf"(?ms)^  {re.escape(service)}:\n(.*?)(?=^  \S|\Z)", text)
    assert match is not None, f"service {service} missing from compose.yaml"
    return match.group(1)


def test_compose_declares_exactly_the_rollout_topology() -> None:
    section = _compose_text().split("services:")[1].split("\nvolumes:")[0]
    services = set(re.findall(r"(?m)^  ([a-z0-9-]+):\s*$", section))
    assert services == {*ROLLOUT_SERVICES, "db", "migrate"}
    rollout = ROLLOUT.read_text(encoding="utf-8")
    # worker-viral is declared by the customer compose but is not always present
    # in older stacks, so the rollout script keeps it in an optional list and only
    # appends it once `docker compose config --services` reports it. The required
    # plus optional lists must still cover exactly the closed rollout inventory.
    required_line = re.search(r"(?m)^REQUIRED_SERVICES=\(([^)]*)\)", rollout)
    assert required_line is not None
    optional_line = re.search(r"(?m)^OPTIONAL_SERVICES=\(([^)]*)\)", rollout)
    assert optional_line is not None
    assert required_line.group(1).split() + optional_line.group(1).split() == list(ROLLOUT_SERVICES)
    # Optional services are only rolled when the target compose declares them.
    assert "mapfile -t CONFIGURED_SERVICES < <(compose config --services)" in rollout
    assert "OPTIONAL_SERVICES=(" in rollout
    assert 'SERVICES+=("$service")' in rollout


def test_migrate_is_the_only_schema_ddl_role() -> None:
    migrate_block = _service_block("migrate")
    assert "python -m alembic upgrade head" in migrate_block
    assert 'restart: "no"' in migrate_block
    for service in ROLLOUT_SERVICES:
        block = _service_block(service)
        assert "alembic" not in block, f"{service} must not execute schema DDL"
        assert "condition: service_completed_successfully" in block, (
            f"{service} must wait for the one-shot migrate role"
        )


def test_connection_pool_budget_fits_the_database_limit() -> None:
    text = (REPO_ROOT / "server" / "app" / "db_pg.py").read_text(encoding="utf-8")
    default_max = int(re.search(r"DEFAULT_POOL_MAX = (\d+)", text).group(1))
    assert default_max == 8
    processes = sum(1 for service in ROLLOUT_SERVICES if service.startswith(("api", "worker")))
    assert processes == 8
    budget = processes * default_max
    compose = _compose_text()
    max_connections = int(re.search(r"max_connections=(\d+)", compose).group(1))
    reserved = int(re.search(r"superuser_reserved_connections=(\d+)", compose).group(1))
    # 64 + 1 transient migrate + 3 reserved <= declared limit (README §3).
    assert budget + 1 + reserved <= max_connections, (budget, max_connections)
    assert "VIDEO_REPLICA_PG_POOL_MAX" in compose


def test_api_wiring_matches_nginx_and_health_contract() -> None:
    compose = _compose_text()
    nginx = NGINX.read_text(encoding="utf-8")
    assert "127.0.0.1:8001:8000" in compose and "127.0.0.1:8002:8000" in compose
    assert "127.0.0.1:8001" in nginx and "127.0.0.1:8002" in nginx
    # Health checks: db pg_isready; api /health via the in-image python.
    assert "pg_isready" in compose
    assert compose.count('"/opt/video-replica/customer-healthcheck.py"') == 2
    # The health route itself exists (release VERIFY curls it through nginx).
    main_py = (REPO_ROOT / "server" / "app" / "main.py").read_text(encoding="utf-8")
    assert '@app.get("/health"' in main_py


def test_rollout_consumes_the_registered_package() -> None:
    rollout = ROLLOUT.read_text(encoding="utf-8")
    assert 'COMPOSE="${CUSTOMER_COMPOSE:-$SCRIPT_DIR/customer/compose.yaml}"' in rollout
    assert 'sha256sum "$COMPOSE" > "$BACKUP/compose.sha256"' in rollout
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    assert "uv sync --locked --no-dev" in bootstrap
    assert "python -m compileall -q app migrations" in bootstrap
    assert (
        "import app.main, app.admin_customer_routes, app.customer_fence, app.generation_worker, "
        "app.publish_worker" in bootstrap
    )
    # Douyin protocol signing runs a Node.js subprocess inside the publish worker.
    assert "ffmpeg ca-certificates nodejs" in bootstrap and "command -v node" in bootstrap
    assert "command -v node" in rollout
    assert "historical SQLite tooling in the customer image" in bootstrap


def test_formal_package_excludes_internal_and_sqlite_backup_surface() -> None:
    exclusion_markers = (
        "不属于本包",
        "排除",
        "never enters",
        "! test -e",
        "historical SQLite tooling in the customer image",
    )
    for name in PACKAGE_FILES:
        assert (PACKAGE_DIR / name).is_file(), name
        text = (PACKAGE_DIR / name).read_text(encoding="utf-8")
        for token in (
            "video-replica-backup",
            "internal-p0",
            "app.backup",
            "sqlite_to_postgres",
            "reconcile_customer_billing",
        ):
            for line in text.splitlines():
                if token in line:
                    # Only the documented exclusion statement may name them.
                    assert any(marker in line for marker in exclusion_markers), (
                        name,
                        token,
                        line.strip(),
                    )
    # The formal package directory inventory is closed.
    package_names = {path.name for path in PACKAGE_DIR.iterdir()}
    assert package_names == set(PACKAGE_FILES)


def test_readme_documents_rebuild_admin_backup_and_hashes() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "bootstrap-base-image.sh" in readme
    assert "provision-empty-customer" in readme and "--confirm-empty-database" in readme
    assert "pitr-backup.sh" in readme and "pitr-restore-drill.sh" in readme
    assert "max_connections=120" in readme
    assert "BACKUP-SHA256SUMS" in readme and "compose.sha256" in readme
    assert "service_completed_successfully" in readme


def test_fail_fast_matrix_targets_exist() -> None:
    for dependency, (test_file, marker) in FAIL_FAST_MATRIX.items():
        path = REPO_ROOT / test_file
        assert path.is_file(), (dependency, test_file)
        assert marker in path.read_text(encoding="utf-8"), (dependency, marker)


def test_customer_package_mounts_files_and_enables_postgres_tls() -> None:
    compose = _compose_text()
    assert "${CUSTOMER_ENV_FILE:-/etc/video-replica/customer.env}" in compose
    assert "target: /etc/video-replica/postgresql-ca.pem" in compose
    assert "target: /etc/video-replica/metrics.token" in compose
    assert "create_host_path: false" in compose
    db = _service_block("db")
    assert "ssl=on" in db and "ssl_cert_file=/etc/postgresql/tls/server.crt" in db
    assert "ssl_key_file=/etc/postgresql/tls/server.key" in db
    for service in ("api-1", "api-2"):
        assert "--no-proxy-headers" in _service_block(service)
    assert "--volume /etc/video-replica/cos-bootstrap.json:" in README.read_text(encoding="utf-8")


def test_postgres_tls_policy_preserves_physical_backup_connections() -> None:
    # PostgreSQL's database keyword "all" does not match physical replication.
    # The PITR pg_basebackup lane needs an explicit replication entry as well.
    policy = (REPO_ROOT / "deploy/postgres/customer-pg_hba.conf").read_text(encoding="utf-8")
    assert "hostssl replication all all scram-sha-256" in policy
    assert "hostnossl replication all all reject" in policy


def test_container_health_probe_preserves_ingress_headers_and_fails_on_not_ready() -> None:
    requests = []
    response_status = 200

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, dict(self.headers)))
            self.send_response(response_status)
            self.end_headers()

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {**os.environ, "VIDEO_REPLICA_PUBLIC_ORIGIN": "https://video.example.com"}
        command = [sys.executable, str(PACKAGE_DIR / "healthcheck.py"), str(server.server_port)]
        success = subprocess.run(command, cwd=REPO_ROOT / "server", env=env, capture_output=True)
        assert success.returncode == 0, success.stderr.decode()
        path, headers = requests[-1]
        assert path == "/ready"
        assert headers["Host"] == "video.example.com"
        assert headers["X-Forwarded-Proto"] == "https"
        assert headers["X-Forwarded-For"] == "127.0.0.2"
        response_status = 503
        failure = subprocess.run(command, cwd=REPO_ROOT / "server", env=env, capture_output=True)
        assert failure.returncode != 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_readiness_probe_headers_pass_production_boundary_without_bypassing_it(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app import main as main_module

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    monkeypatch.setenv("VIDEO_REPLICA_PUBLIC_ORIGIN", "https://video.example.com")
    monkeypatch.setenv("VIDEO_REPLICA_TRUSTED_PROXY_CIDRS", "127.0.0.1/32,172.30.42.1/32")
    monkeypatch.setattr(main_module, "check_customer_production_runtime_dependencies", lambda: None)
    headers = {
        "Host": "video.example.com",
        "X-Forwarded-Proto": "https",
        "X-Forwarded-For": "127.0.0.2",
    }
    client = TestClient(main_module.app, client=("127.0.0.1", 12345))
    assert client.get("/ready", headers=headers).status_code == 200
    assert client.get("/ready").status_code == 421
    untrusted = TestClient(main_module.app, client=("172.30.42.20", 12345))
    assert untrusted.get("/ready", headers=headers).status_code == 403

    def unavailable() -> None:
        raise RuntimeError("dependency unavailable")

    monkeypatch.setattr(main_module, "check_customer_production_runtime_dependencies", unavailable)
    assert client.get("/ready", headers=headers).status_code == 503


def test_compose_caps_container_logs_with_json_file_rotation() -> None:
    # P2-1 (BILLING-OBS): the application installs no file handler
    # (server/app/logging_setup.py keeps records on stderr), so container
    # stdout/stderr collected by docker's json-file driver is the only place
    # logs land on disk. An unbounded driver would let one busy worker fill
    # the host disk, so the shared anchor must cover every declared service.
    compose = _compose_text()
    anchor = re.search(r"(?ms)^x-logging: &default-logging$\n(.*?)(?=^\S)", compose)
    assert anchor is not None, "compose.yaml must define the shared x-logging anchor"
    anchor_block = anchor.group(1)
    assert "driver: json-file" in anchor_block
    assert 'max-size: "10m"' in anchor_block
    assert 'max-file: "5"' in anchor_block
    for service in ("db", "migrate", *ROLLOUT_SERVICES):
        assert "logging: *default-logging" in _service_block(service), (
            f"{service} must opt into the shared log-rotation anchor"
        )


def test_readme_and_logging_stack_document_rotation_and_retention() -> None:
    # The rotation parameters are only honest if the operator manual states
    # where rotated files live, how to query recent lines, and what the
    # retention boundary is (json-file keeps ~50 MB per container; long-term
    # retention is explicitly out of package scope).
    logging_yml = LOGGING_YAML.read_text(encoding="utf-8")
    assert logging_yml.count("logging: *default-logging") == 3  # loki/promtail/grafana
    readme = README.read_text(encoding="utf-8")
    assert "max-size" in readme and "max-file" in readme
    assert "/var/lib/docker/containers" in readme
    assert "logs --since" in readme and "logs -f" in readme
