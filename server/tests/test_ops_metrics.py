from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.ops_metrics import (
    METRICS_TOKEN_FILE_ENV,
    REQUEST_ID_HEADER,
    metrics_response,
    render_metrics_document_for_tests,
    request_observability_middleware,
    reset_metrics_for_tests,
    set_current_result_code,
    set_current_trace_fields,
    unhandled_exception_response,
)


@pytest.fixture(autouse=True)
def clean_metrics_registry() -> Iterator[None]:
    reset_metrics_for_tests()
    yield
    reset_metrics_for_tests()


def _ops_app(*, ready: bool = True) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_response)
    app.middleware("http")(request_observability_middleware)

    @app.get("/echo")
    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"request_id": request.headers[REQUEST_ID_HEADER]})

    @app.get("/result")
    async def result() -> JSONResponse:
        set_current_result_code("CUSTOM_RESULT")
        return JSONResponse({"ok": True})

    @app.get("/trace")
    async def trace() -> JSONResponse:
        set_current_trace_fields(
            actor_id="admin-1",
            user_id="user-1",
            device_id="device-1",
            session_id="session-1",
            session_epoch=3,
            task_id="task-1",
            order_id="order-1",
            code_mask="XS04-****-LAST",
        )
        return JSONResponse({"ok": True})

    @app.get("/trace-poison")
    async def trace_poison(request: Request) -> JSONResponse:
        request.state.observability_fields = {
            "task_id": "task-safe",
            "authorization": "Bearer must-not-log",
        }
        return JSONResponse({"ok": True})

    @app.get("/tasks/{task_id}/orders/{order_no}")
    async def traced_resource(task_id: str, order_no: str) -> JSONResponse:
        return JSONResponse({"task_id": task_id, "order_no": order_no})

    @app.get("/replay")
    async def replay() -> JSONResponse:
        return JSONResponse(
            {"replayed": True},
            headers={REQUEST_ID_HEADER: "req-original-operation"},
        )

    @app.get("/invalid-replay")
    async def invalid_replay() -> JSONResponse:
        return JSONResponse(
            {"replayed": True},
            headers={REQUEST_ID_HEADER: "x" * 129},
        )

    @app.get("/metrics")
    def metrics(request: Request) -> JSONResponse:
        return metrics_response(request, readiness_check=lambda: ready)

    @app.get("/unhandled")
    async def unhandled() -> None:
        raise RuntimeError("test failure")

    return app


def _last_log_payload(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    assert caplog.records, "expected at least one structured request log"
    return json.loads(caplog.records[-1].getMessage())


def test_request_middleware_mints_request_id_and_redacts_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get(
                "/echo?signature=signed-url-secret",
                headers={"Authorization": "Bearer super-secret-token"},
            )

    assert response.status_code == 200
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id
    assert response.json()["request_id"] == request_id

    payload = _last_log_payload(caplog)
    assert payload["request_id"] == request_id
    assert payload["method"] == "GET"
    assert payload["route"] == "/echo"
    assert payload["operation"] == "GET /echo"
    assert payload["status"] == 200
    assert payload["result_code"] == "OK"
    assert isinstance(payload["latency_ms"], int)
    assert "super-secret-token" not in caplog.text
    assert "signed-url-secret" not in caplog.text
    assert "?signature=" not in caplog.text


def test_request_middleware_preserves_request_id_and_custom_result_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/result", headers={REQUEST_ID_HEADER: "req-42"})

    assert response.status_code == 200
    assert response.headers.get(REQUEST_ID_HEADER) == "req-42"

    payload = _last_log_payload(caplog)
    assert payload["request_id"] == "req-42"
    assert payload["route"] == "/result"
    assert payload["result_code"] == "CUSTOM_RESULT"


def test_cors_preflight_is_observed_like_every_other_http_request(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORS must not short-circuit request-id, metrics, or completion logs."""
    from app.main import app as main_app

    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        os.environ.get(
            "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
        ),
    )
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(main_app) as client:
            response = client.options(
                "/api/customer/activate",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "idempotency-key, x-request-id",
                },
            )

    assert response.status_code == 200, response.text
    request_id = response.headers.get(REQUEST_ID_HEADER)
    assert request_id
    payload = _last_log_payload(caplog)
    assert payload["request_id"] == request_id
    assert payload["method"] == "OPTIONS"
    assert payload["status"] == 200
    assert payload["route"] == "UNMATCHED"
    metrics = render_metrics_document_for_tests(is_ready=True)
    assert 'method="OPTIONS"' in metrics


def test_unhandled_500_keeps_the_request_id_for_log_correlation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/unhandled", headers={REQUEST_ID_HEADER: "req-unhandled"})

    assert response.status_code == 500
    assert response.headers[REQUEST_ID_HEADER] == "req-unhandled"
    payload = _last_log_payload(caplog)
    assert payload["request_id"] == "req-unhandled"
    assert payload["result_code"] == "UNHANDLED_EXCEPTION"


def test_shared_permission_denials_publish_the_business_result_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.permissions import forbidden

    app = FastAPI()
    app.middleware("http")(request_observability_middleware)

    @app.get("/forbidden")
    async def denied() -> None:
        raise forbidden("PROJECT_FORBIDDEN", "denied")

    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/forbidden")

    assert response.status_code == 403
    assert _last_log_payload(caplog)["result_code"] == "PROJECT_FORBIDDEN"


def test_shared_http_exception_handler_publishes_legacy_business_codes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.ops_metrics import business_http_exception_handler

    app = FastAPI()
    app.add_exception_handler(HTTPException, business_http_exception_handler)
    app.middleware("http")(request_observability_middleware)

    @app.get("/legacy-error")
    async def legacy_error() -> None:
        raise HTTPException(
            status_code=409,
            detail={"code": "LEGACY_BUSINESS_CONFLICT", "message": "conflict"},
        )

    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/legacy-error")

    assert response.status_code == 409
    assert _last_log_payload(caplog)["result_code"] == "LEGACY_BUSINESS_CONFLICT"


def test_direct_ingress_rejections_publish_the_business_result_code(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.main import require_loopback_client

    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")
    app = FastAPI()
    app.middleware("http")(require_loopback_client)
    app.middleware("http")(request_observability_middleware)

    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/probe")

    assert response.status_code == 403
    assert response.json()["code"] == "UNTRUSTED_PROXY"
    assert _last_log_payload(caplog)["result_code"] == "UNTRUSTED_PROXY"


def test_request_middleware_logs_only_the_explicit_trace_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get(
                "/trace",
                headers={"Authorization": "Bearer never-log-this"},
            )

    assert response.status_code == 200
    payload = _last_log_payload(caplog)
    assert (
        payload
        | {
            "actor_id": "admin-1",
            "user_id": "user-1",
            "device_id": "device-1",
            "session_id": "session-1",
            "session_epoch": 3,
            "task_id": "task-1",
            "order_id": "order-1",
            "code_mask": "XS04-****-LAST",
        }
        == payload
    )
    assert "never-log-this" not in caplog.text


def test_request_middleware_filters_unapproved_request_state_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/trace-poison")

    assert response.status_code == 200
    payload = _last_log_payload(caplog)
    assert payload["task_id"] == "task-safe"
    assert "authorization" not in payload
    assert "must-not-log" not in caplog.text


def test_request_middleware_adds_approved_path_business_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get("/tasks/task-42/orders/order-99")

    assert response.status_code == 200
    payload = _last_log_payload(caplog)
    assert payload["route"] == "/tasks/{task_id}/orders/{order_no}"
    assert payload["task_id"] == "task-42"
    assert payload["order_id"] == "order-99"


@pytest.mark.parametrize(
    "invalid_request_id",
    ["x" * 129, "contains\tcontrol"],
)
def test_request_middleware_replaces_invalid_request_ids(
    invalid_request_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get(
                "/echo",
                headers={REQUEST_ID_HEADER: invalid_request_id},
            )

    request_id = response.headers[REQUEST_ID_HEADER]
    assert request_id != invalid_request_id
    assert response.json()["request_id"] == request_id
    payload = _last_log_payload(caplog)
    assert payload["request_id"] == request_id
    assert invalid_request_id not in caplog.text


def test_request_middleware_logs_the_persisted_id_on_idempotent_replay(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get(
                "/replay",
                headers={REQUEST_ID_HEADER: "req-retry-attempt"},
            )

    assert response.headers[REQUEST_ID_HEADER] == "req-original-operation"
    payload = _last_log_payload(caplog)
    assert payload["request_id"] == "req-original-operation"


def test_request_middleware_rejects_an_invalid_persisted_replay_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _ops_app()
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(app) as client:
            response = client.get(
                "/invalid-replay",
                headers={REQUEST_ID_HEADER: "req-current-attempt"},
            )

    assert response.headers[REQUEST_ID_HEADER] == "req-current-attempt"
    assert _last_log_payload(caplog)["request_id"] == "req-current-attempt"
    assert "x" * 129 not in caplog.text


def test_customer_and_admin_routes_use_the_shared_request_id_source() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    # Customer adjustments delegate their write envelope to this helper, so
    # request IDs must remain centralized there after the admin refactor.
    customer_routes = (app_dir / "admin_customer_routes.py").read_text(encoding="utf-8")
    assert "from app.admin_write_contract import" in customer_routes
    for filename in (
        "activation_code_routes.py",
        "admin_activation_routes.py",
        "admin_write_contract.py",
        "customer_device_routes.py",
        "customer_session_routes.py",
    ):
        source = (app_dir / filename).read_text(encoding="utf-8")
        assert "get_or_create_request_id" in source, filename
        assert "request_id = str(uuid.uuid4())" not in source, filename
        assert (
            'request.headers.get(REQUEST_ID_HEADER, "").strip() or str(uuid.uuid4())' not in source
        ), filename


def test_metrics_fail_closed_when_token_file_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _ops_app()
    monkeypatch.delenv(METRICS_TOKEN_FILE_ENV, raising=False)
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "METRICS_UNAVAILABLE"
    assert "token" not in response.json()["detail"]["message"].lower()


def test_metrics_auth_failure_never_executes_collector(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "metrics.token"
    token_file.write_text("metrics-secret\n", encoding="utf-8")
    monkeypatch.setenv(METRICS_TOKEN_FILE_ENV, str(token_file))

    import app.ops_metrics as ops_metrics

    called = False

    def _boom(*, is_ready: bool) -> str:
        nonlocal called
        called = True
        raise AssertionError(f"collector should not run for unauthorized request: {is_ready}")

    monkeypatch.setattr(ops_metrics, "render_metrics_document", _boom)
    app = _ops_app()
    with TestClient(app) as client:
        response = client.get("/metrics", headers={"Authorization": "Bearer wrong-secret"})

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "METRICS_UNAUTHORIZED"
    assert called is False


def test_main_metrics_auth_precedes_readiness_probe(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token_file = tmp_path / "metrics.token"
    token_file.write_text("metrics-secret\n", encoding="utf-8")
    monkeypatch.setenv(METRICS_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)
    monkeypatch.setenv(
        "VIDEO_REPLICA_DATABASE_URL",
        os.environ.get(
            "TEST_POSTGRESQL_URL", "postgresql://testuser:testpass@localhost:5433/customer_v3_test"
        ),
    )

    from app import main

    called = False

    def forbidden_readiness_probe() -> bool:
        nonlocal called
        called = True
        raise AssertionError("unauthorized scrapes must not probe PostgreSQL/COS")

    monkeypatch.setattr(main, "_ready_status_for_metrics", forbidden_readiness_probe)
    with caplog.at_level(logging.INFO, logger="app.ops_metrics"):
        with TestClient(main.app, raise_server_exceptions=False) as client:
            response = client.get(
                "/metrics",
                headers={"Authorization": "Bearer wrong-secret"},
            )

    assert response.status_code == 401
    assert called is False
    assert _last_log_payload(caplog)["result_code"] == "METRICS_UNAUTHORIZED"


def test_metrics_require_exact_bearer_and_export_local_only_counters(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "metrics.token"
    token_file.write_text("metrics-secret\n", encoding="utf-8")
    monkeypatch.setenv(METRICS_TOKEN_FILE_ENV, str(token_file))

    app = _ops_app(ready=True)
    with TestClient(app) as client:
        wrong = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret-wrong"})
        assert wrong.status_code == 401

        echoed = client.get("/echo", headers={REQUEST_ID_HEADER: "req-metrics"})
        assert echoed.status_code == 200

        response = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    text = response.text
    assert 'video_replica_http_requests_total{method="GET",route="/echo",status="200"} 1' in text
    assert (
        "video_replica_http_request_duration_seconds_total"
        '{method="GET",route="/echo",status="200"} '
    ) in text
    assert 'video_replica_ready{service="video-replica-api"} 1' in text
    assert "request_id=" not in text
    assert "req-metrics" not in text


def test_unmatched_paths_share_one_bounded_metric_label(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "metrics.token"
    token_file.write_text("metrics-secret\n", encoding="utf-8")
    monkeypatch.setenv(METRICS_TOKEN_FILE_ENV, str(token_file))

    app = _ops_app()
    with TestClient(app) as client:
        assert client.get("/missing/attacker-value-a").status_code == 404
        assert client.get("/missing/attacker-value-b").status_code == 404
        metrics = client.get(
            "/metrics",
            headers={"Authorization": "Bearer metrics-secret"},
        ).text

    assert (
        'video_replica_http_requests_total{method="GET",route="UNMATCHED",status="404"} 2'
        in metrics
    )
    assert "attacker-value-a" not in metrics
    assert "attacker-value-b" not in metrics


def test_extension_methods_share_one_bounded_metric_label(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "metrics.token"
    token_file.write_text("metrics-secret\n", encoding="utf-8")
    monkeypatch.setenv(METRICS_TOKEN_FILE_ENV, str(token_file))

    app = _ops_app()
    with TestClient(app) as client:
        assert client.request("ATTACK1", "/echo").status_code == 405
        assert client.request("ATTACK2", "/echo").status_code == 405
        metrics = client.get(
            "/metrics",
            headers={"Authorization": "Bearer metrics-secret"},
        ).text

    assert 'method="OTHER"' in metrics
    assert "ATTACK1" not in metrics
    assert "ATTACK2" not in metrics


def test_rendered_metrics_can_report_not_ready() -> None:
    text = render_metrics_document_for_tests(is_ready=False)
    assert 'video_replica_ready{service="video-replica-api"} 0' in text
