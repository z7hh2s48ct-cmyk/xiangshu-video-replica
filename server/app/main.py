import ipaddress
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.account_admin_routes import router as account_admin_router
from app.account_migration_routes import router as account_migration_router
from app.activation_code_routes import router as customer_activation_router
from app.admin_activation_routes import router as admin_activation_router
from app.admin_audit_routes import router as admin_audit_router
from app.admin_auth_routes import router as admin_auth_router
from app.admin_customer_routes import router as admin_customer_router
from app.admin_dashboard_routes import router as admin_dashboard_router
from app.admin_device_routes import router as admin_device_router
from app.admin_first_frame_routes import router as admin_first_frame_router
from app.admin_runtime_routes import router as admin_runtime_router
from app.admin_session_routes import router as admin_session_router
from app.analysis_routes import router as analysis_router
from app.api_key_routes import router as api_key_router
from app.billing_routes import router as itemized_billing_router
from app.billing_viral_routes import router as billing_viral_router
from app.bootstrap import (
    check_customer_production_runtime_dependencies,
    customer_public_origin,
    is_customer_production,
    trusted_proxy_networks,
)
from app.character_contracts import character_domain_openapi_schemas
from app.character_generation_routes import router as character_generation_router
from app.character_identity_routes import router as character_identity_router
from app.character_reference_routes import router as character_reference_router
from app.character_routes import router as character_router
from app.control_routes import router as control_router
from app.credit_conversion import router as credit_conversion_router
from app.customer_auth_routes import CustomerBrowserTransport
from app.customer_auth_routes import router as customer_auth_router
from app.customer_device_routes import router as customer_device_router
from app.customer_pricing_routes import router as customer_pricing_router
from app.customer_security_routes import router as customer_security_router
from app.customer_session_routes import router as customer_session_router
from app.customer_sub_account_routes import router as customer_sub_account_router
from app.db_pg import DATABASE_URL_ENV, SQLITE_URL_SCHEMES, close_pg_pool
from app.export_controller import router as export_router
from app.first_frame_routes import router as first_frame_router
from app.generation_routes import router as generation_router
from app.independent_routes import router as independent_router
from app.logging_setup import configure_logging
from app.material_routes import router as material_router
from app.media_routes import router as media_router
from app.ops_metrics import (
    business_http_exception_handler,
    metrics_response,
    request_observability_middleware,
    set_current_result_code,
    unhandled_exception_response,
)
from app.oral_routes import router as oral_router
from app.payment_routes import router as payment_router
from app.prompt_optimizer_routes import router as prompt_optimizer_router
from app.publish_browser_routes import router as publish_browser_router
from app.publish_record_routes import router as publish_record_router
from app.publish_routes import router as publish_router
from app.rbac_routes import router as rbac_router
from app.recharge_package_routes import router as recharge_package_router
from app.recharge_routes import router as recharge_router
from app.script_from_audio_routes import router as script_from_audio_router
from app.settings import SettingsUnavailableError
from app.settings_routes import router as settings_router
from app.simple_character_routes import router as character_simple_router
from app.source_frame_routes import router as source_frame_router
from app.studio_draft_routes import router as studio_draft_router
from app.studio_routes import router as studio_router
from app.viral_import_routes import router as viral_import_router
from app.viral_routes import router as viral_router
from app.viral_search_refresh_routes import router as viral_search_refresh_router
from app.viral_search_routes import router as viral_search_router
from app.wallet_routes import router as wallet_router

# Configure application logging before the app serves traffic: uvicorn only
# configures uvicorn.* loggers, so without this every app.* INFO record is
# dropped and WARNING+ loses its timestamp format.
configure_logging()

# Non-loopback hosts that are still accepted: TestClient uses "testclient",
# and "localhost" is a loopback alias but not parseable as an IP address.
LOOPBACK_ALIASES = {"localhost", "testclient"}
logger = logging.getLogger(__name__)


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class ReadinessResponse(BaseModel):
    status: Literal["ready"]
    service: str
    # "internal" was the retired SQLite lane label (CW-042-b); kept only in
    # this comment — the field now always reports "postgresql".
    database: Literal["postgresql"]
    storage: Literal["local", "cos"]


class VideoReplicaAPI(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        schema = get_openapi(title=self.title, version=self.version, routes=self.routes)
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            character_domain_openapi_schemas()
        )
        self.openapi_schema = schema
        return schema


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """CW-025: 全环境 PG-only API lifespan（customer lane）。

    T09 / DB-08: fail the API process closed at startup when a customer-
    production boot still carries legacy single-admin mappings, dev identity,
    local assets or a missing admin-session key (uvicorn aborts on lifespan
    errors).

    M1 review H1: the lifespan must run the database-mode fail-closed check
    too, so a direct `uvicorn app.main:app` boot cannot reach the internal
    SQLite lane (bootstrap.main already validates; this closes the
    systemd/container entrypoint).

    CW-025 后 resolve_database_config() 全环境 fail-closed（不再抛
    MissingDatabaseConfigError），所以 try/except 吞掉逻辑已删除。
    历史 SQLite 工具走 CW-060 独立白名单，不经过在线 API 入口。

    CW-025 补充：internal lane（内部 P0 遗留逻辑，DATABASE_URL 未设置或为
    SQLite URL）在 DB_PATH 已配置时直接通过，不调用 resolve_database_config()。
    内部 P0 是"已完成收口"的独立线，不属于客户版 V3 的运行环境（dev/test/CI/
    staging/production），归 CW-030/CW-040 后续处理。若 DATABASE_URL 与
    DB_PATH 都缺失，仍视为"缺 DSN"全环境 fail-closed。
    """
    from app.bootstrap import assert_customer_production_security
    from app.db_pg import resolve_database_config, validate_customer_production

    # CW-042-b: the internal/desktop SQLite lane is physically retired —
    # a missing or SQLite DATABASE_URL fails closed in EVERY environment
    # (previously only customer production refused; the lane itself is gone).
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not url or url.startswith(SQLITE_URL_SCHEMES):
        raise RuntimeError(
            "PostgreSQL is required: "
            f"{DATABASE_URL_ENV} must be set to a postgresql:// DSN "
            f"(the internal SQLite lane is retired, CW-042-b)"
        )

    assert_customer_production_security()
    _database_config = resolve_database_config()
    validate_customer_production(_database_config)
    if is_customer_production():
        check_customer_production_runtime_dependencies()
    yield
    # M0 review M2: release the PG pool on shutdown so pooled connections
    # don't outlive the process.
    close_pg_pool()


async def _business_http_exception_dispatch(request: Request, error: Exception) -> Response:
    if not isinstance(error, HTTPException):
        raise error
    return await business_http_exception_handler(request, error)


app = VideoReplicaAPI(title="Video Replica API", version="2.2.0", lifespan=_lifespan)
app.add_exception_handler(HTTPException, _business_http_exception_dispatch)
app.add_exception_handler(Exception, unhandled_exception_response)


def _business_error_response(*, status_code: int, code: str, message: str) -> JSONResponse:
    set_current_result_code(code)
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "message": message},
    )


@app.exception_handler(SettingsUnavailableError)
async def settings_unavailable_handler(
    _: Request,
    error: SettingsUnavailableError,
) -> JSONResponse:
    logger.error("Local settings are unavailable: %s", type(error).__name__)
    set_current_result_code("SETTINGS_CONFIGURATION_UNAVAILABLE")
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "SETTINGS_CONFIGURATION_UNAVAILABLE",
                "message": (
                    "本地配置仍保存在数据库中，但当前主密钥缺失或不匹配；系统未覆盖已保存配置。"
                ),
            }
        },
    )


app.add_middleware(CustomerBrowserTransport)


@app.middleware("http")
async def require_loopback_client(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Enforce the internal loopback or customer trusted-proxy boundary.

    Loopback is one layer of the desktop threat model, not an authentication
    mechanism. Release requests use the server-configured desktop identity;
    X-Dev-User-Id is accepted only when development identity mode is explicitly
    enabled. Customer production instead requires a raw peer in the configured
    proxy CIDRs, an exact public Host, HTTPS, and one proxy-overwritten client IP.
    Uvicorn must keep proxy-header parsing disabled so ``request.client`` remains
    the raw peer used to enforce this trust boundary.
    """
    host = request.client.host if request.client is not None else ""
    if is_customer_production():
        try:
            peer_ip = ipaddress.ip_address(host)
        except ValueError:
            return _business_error_response(
                status_code=403,
                code="UNTRUSTED_PROXY",
                message="The request did not arrive through a trusted proxy.",
            )
        try:
            proxy_networks = trusted_proxy_networks()
            public_origin = customer_public_origin()
        except ValueError:
            return _business_error_response(
                status_code=503,
                code="INGRESS_CONFIGURATION_INVALID",
                message="Customer ingress security is not configured correctly.",
            )
        if not any(peer_ip in network for network in proxy_networks):
            return _business_error_response(
                status_code=403,
                code="UNTRUSTED_PROXY",
                message="The request did not arrive through a trusted proxy.",
            )

        expected_host = public_origin.removeprefix("https://")
        host_values = request.headers.getlist("host")
        if len(host_values) != 1 or host_values[0].strip().casefold() != expected_host.casefold():
            return _business_error_response(
                status_code=421,
                code="HOST_NOT_ALLOWED",
                message="The request Host is not configured for this service.",
            )

        proto_values = request.headers.getlist("x-forwarded-proto")
        if len(proto_values) != 1 or proto_values[0].strip().casefold() != "https":
            return _business_error_response(
                status_code=400,
                code="HTTPS_REQUIRED",
                message="Customer requests must arrive through HTTPS.",
            )

        forwarded_values = request.headers.getlist("x-forwarded-for")
        forwarded = forwarded_values[0].strip() if len(forwarded_values) == 1 else ""
        if not forwarded or "," in forwarded:
            return _business_error_response(
                status_code=400,
                code="FORWARDED_CLIENT_INVALID",
                message="The trusted proxy must provide exactly one client IP.",
            )
        try:
            forwarded_ip = ipaddress.ip_address(forwarded)
        except ValueError:
            return _business_error_response(
                status_code=400,
                code="FORWARDED_CLIENT_INVALID",
                message="The trusted proxy must provide exactly one client IP.",
            )
        if forwarded_ip == peer_ip:
            # Uvicorn's ProxyHeadersMiddleware replaces scope["client"] with
            # the single X-Forwarded-For address. Equality therefore proves
            # that the raw last-hop peer was lost before this boundary ran.
            # Fail closed even when the forged/re-written address happens to
            # land inside the trusted proxy CIDR.
            return _business_error_response(
                status_code=503,
                code="PROXY_HEADER_REWRITE_DETECTED",
                message=(
                    "The ASGI server rewrote the proxy peer; disable proxy-header "
                    "parsing for customer production."
                ),
            )
        request.state.client_ip = str(forwarded_ip)
        return await call_next(request)

    try:
        allowed = ipaddress.ip_address(host).is_loopback or host in LOOPBACK_ALIASES
    except ValueError:
        allowed = host in LOOPBACK_ALIASES
    if not allowed:
        return _business_error_response(
            status_code=403,
            code="LOOPBACK_ONLY",
            message="API 仅允许本机访问。",
        )
    request.state.client_ip = host
    return await call_next(request)


def _cors_origins() -> list[str]:
    origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://tauri.localhost",
        "tauri://localhost",
    ]
    if is_customer_production():
        try:
            origins.append(customer_public_origin())
        except ValueError:
            # The lifespan gate aborts startup with the actionable error. Keep
            # module import deterministic so tooling can still load OpenAPI.
            pass
    return origins


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    # PR #44 review P1: first activation is called from the WebView/browser
    # client with a mandatory Idempotency-Key (plus X-Request-Id); without
    # them in allow_headers the CORS preflight fails before the handler runs.
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Dev-User-Id",
        "X-Admin-CSRF",
        "Idempotency-Key",
        "X-Request-Id",
    ],
    # The same review's other half: browser JS must be able to read the
    # replay marker and the echoed request id on the activation response.
    # PR #46 review P2: Retry-After joins the exposed list — the browser/
    # Tauri client must read the 429 backoff hint, or it retries blind and
    # keeps burning the (shared, PG-backed) abuse budget.
    expose_headers=[
        "X-Request-Id",
        "X-Idempotent-Replay",
        "Retry-After",
        "X-Export-Total",
        "X-Export-Returned",
        "X-Export-Truncated",
    ],
)
# Starlette applies the last registered middleware first. Keep observability
# outside CORS so direct OPTIONS responses also receive a request id, log and
# bounded HTTP metric instead of being silently short-circuited.
app.middleware("http")(request_observability_middleware)
app.include_router(generation_router)
app.include_router(studio_router)
app.include_router(studio_draft_router)
app.include_router(publish_router)
app.include_router(publish_browser_router)
app.include_router(publish_record_router)
app.include_router(material_router)
app.include_router(script_from_audio_router)
app.include_router(oral_router)
app.include_router(independent_router)
app.include_router(rbac_router)
app.include_router(payment_router)
app.include_router(control_router)
app.include_router(admin_auth_router)
app.include_router(admin_dashboard_router)
app.include_router(admin_customer_router)
app.include_router(admin_session_router)
app.include_router(admin_first_frame_router)
app.include_router(customer_pricing_router)
app.include_router(itemized_billing_router)
app.include_router(billing_viral_router)
app.include_router(account_admin_router)
app.include_router(account_migration_router)
app.include_router(credit_conversion_router)
app.include_router(admin_runtime_router)
app.include_router(admin_audit_router)
app.include_router(customer_auth_router)
app.include_router(customer_activation_router)
app.include_router(customer_device_router)
app.include_router(customer_session_router)
app.include_router(customer_sub_account_router)
app.include_router(customer_security_router)
app.include_router(api_key_router)
app.include_router(admin_activation_router)
app.include_router(admin_device_router)
app.include_router(recharge_router)
app.include_router(recharge_package_router)
app.include_router(wallet_router)
app.include_router(export_router)  # Admin report export functionality
app.include_router(settings_router)
app.include_router(media_router)
app.include_router(analysis_router)
app.include_router(viral_router)
app.include_router(viral_import_router)
app.include_router(viral_search_router)
app.include_router(viral_search_refresh_router)
app.include_router(prompt_optimizer_router)
app.include_router(character_router)
app.include_router(character_identity_router)
app.include_router(character_generation_router)
app.include_router(character_reference_router)
app.include_router(source_frame_router)
app.include_router(first_frame_router)
app.include_router(character_simple_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", service="video-replica-api")


@app.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """Process liveness only; dependency failures belong to ``/ready``."""
    return HealthResponse(status="ok", service="video-replica-api")


@app.get("/ready", response_model=None)
def ready() -> ReadinessResponse | JSONResponse:
    """Return ready only while every customer runtime dependency is usable.

    This is deliberately synchronous: FastAPI runs it in its worker thread
    pool, so a slow PostgreSQL/COS probe cannot block the ASGI event loop or
    prevent the dependency-free liveness endpoint from responding.
    """
    if not is_customer_production():
        # CW-042-b: every lane is PostgreSQL now; dev/test/CI only differ in
        # using local object storage instead of COS.
        return ReadinessResponse(
            status="ready",
            service="video-replica-api",
            database="postgresql",
            storage="local",
        )
    try:
        check_customer_production_runtime_dependencies()
    except Exception as exc:
        logger.error("Runtime readiness check failed: %s", type(exc).__name__)
        set_current_result_code("RUNTIME_DEPENDENCY_UNAVAILABLE")
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "video-replica-api",
                "code": "RUNTIME_DEPENDENCY_UNAVAILABLE",
            },
        )
    return ReadinessResponse(
        status="ready",
        service="video-replica-api",
        database="postgresql",
        storage="cos",
    )


def _ready_status_for_metrics() -> bool:
    if not is_customer_production():
        return True
    try:
        check_customer_production_runtime_dependencies()
    except Exception:
        return False
    return True


@app.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    return metrics_response(request, readiness_check=_ready_status_for_metrics)
