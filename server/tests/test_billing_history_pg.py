"""BILLING-OBS P2-3：价目历史版本查询（管理端 + 用户端）的 PostgreSQL 契约。

Tests cover:
- GET /api/control/billing/tariff-history — 审计重建的科目版本序列 + 当前行兜底
- GET /api/control/billing/pricing-history — 充值换算配置的版本序列
- GET /api/customer/billing/price-version — 用户侧按版本回查当时价目
- customer_pricing.update 隐式上调科目时补写 billing.tariff.update 审计（历史断档修复）
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811

from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from test_customer_center import (  # noqa: F401
    account,
    client,
    mutation,
    registration_client,
    registration_dsn,
    route_state,
    token_headers,
)
from test_customer_pricing import admin_login, pricing_client  # noqa: F401

from app.billing_catalog import SERVICES


@pytest.fixture()
def history_client(pricing_client):
    from app.billing_routes import router

    pricing_client.app.include_router(router)
    return pricing_client


def publish(client, admin, service, tariff, expected_version, reason):
    return client.put(
        "/api/control/billing/tariff",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": reason,
            "service": service,
            "expected_version": expected_version,
            "tariff": tariff,
        },
    )


def update_pricing(client, admin, config_body, expected_version, reason):
    return client.put(
        "/api/control/settings/customer-pricing",
        headers={**admin, "Idempotency-Key": str(uuid4())},
        json={
            "confirm": True,
            "reason": reason,
            "expected_version": expected_version,
            "config": config_body,
        },
    )


def test_tariff_history_rebuilds_versions_from_audit_and_marks_current(history_client, route_state):
    client = history_client
    admin = admin_login(client, route_state)
    first = publish(
        client,
        admin,
        "analysis",
        {"enabled": True, "unit_credits": "2", "unit_cost_fen": "0.4"},
        0,
        "首版定价",
    )
    assert first.status_code == 200, first.text
    second = publish(
        client,
        admin,
        "analysis",
        {"enabled": True, "unit_credits": "3.5", "unit_cost_fen": "0.6"},
        1,
        "成本上调",
    )
    assert second.status_code == 200, second.text
    response = client.get("/api/control/billing/tariff-history?service=analysis", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["service"] == "analysis"
    assert body["name"] == SERVICES["analysis"].name
    assert body["unit"] == "call"
    assert body["current_version"] == 2
    items = body["items"]
    assert [item["version"] for item in items] == [2, 1]
    latest, previous = items
    assert latest["current"] is True and previous["current"] is False
    assert latest["source"] == "audit" and previous["source"] == "audit"
    assert latest["actor_username"] == "price_admin"
    assert latest["reason"] == "成本上调"
    assert Decimal(latest["unit_credits"]) == Decimal("3.5")
    assert Decimal(latest["unit_cost_fen"]) == Decimal("0.6")
    assert latest["unit_rounding"] == "ceil" and latest["enabled"] is True
    assert Decimal(previous["unit_credits"]) == Decimal("2")
    assert previous["reason"] == "首版定价"
    assert latest["effective_at"] and previous["effective_at"]


def test_tariff_history_falls_back_to_current_row_without_audit(history_client, route_state):
    client = history_client
    admin = admin_login(client, route_state)
    with psycopg.connect(route_state) as raw:
        raw.execute(
            "INSERT INTO billing_tariffs(service,enabled,unit_credits,unit_cost_fen) "
            "VALUES ('analysis',true,5,2.5)"
        )
    response = client.get("/api/control/billing/tariff-history?service=analysis", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_version"] == 1
    assert len(body["items"]) == 1
    entry = body["items"][0]
    assert entry["version"] == 1
    assert entry["source"] == "current" and entry["current"] is True
    assert entry["actor_user_id"] is None and entry["actor_username"] is None
    assert entry["reason"] is None
    assert Decimal(entry["unit_credits"]) == Decimal("5")
    assert entry["effective_at"]


def test_tariff_history_rejects_unknown_service_and_requires_admin(history_client, route_state):
    client = history_client
    admin = admin_login(client, route_state)
    rejected = client.get(
        "/api/control/billing/tariff-history?service=not_a_subject", headers=admin
    )
    assert rejected.status_code == 422
    assert "未知计费科目" in rejected.text
    client.cookies.clear()
    anonymous = client.get("/api/control/billing/tariff-history?service=analysis")
    assert anonymous.status_code in {401, 403}


def test_pricing_history_lists_audited_versions_and_marks_current(history_client, route_state):
    client = history_client
    admin = admin_login(client, route_state)
    updated = update_pricing(
        client,
        admin,
        {"points_per_yuan": 120, "discount_basis_points": 9000, "consumption_rounding": "floor"},
        0,
        "充值换算调整",
    )
    assert updated.status_code == 200, updated.text
    response = client.get("/api/control/billing/pricing-history", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_version"] == 1
    assert [item["version"] for item in body["items"]] == [1]
    entry = body["items"][0]
    assert entry["source"] == "audit" and entry["current"] is True
    assert entry["actor_username"] == "price_admin"
    assert entry["reason"] == "充值换算调整"
    assert entry["config"]["points_per_yuan"] == 120
    assert entry["config"]["consumption_rounding"] == "floor"
    assert entry["effective_at"]
    client.cookies.clear()
    assert client.get("/api/control/billing/pricing-history").status_code in {401, 403}


def test_pricing_history_marks_unpublished_current_row(history_client, route_state):
    client = history_client
    admin = admin_login(client, route_state)
    response = client.get("/api/control/billing/pricing-history", headers=admin)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_version"] == 0
    assert len(body["items"]) == 1
    entry = body["items"][0]
    assert entry["version"] == 0
    assert entry["source"] == "current" and entry["current"] is True
    assert entry["config"] is None and entry["actor_user_id"] is None


def test_customer_price_version_returns_historical_tariff_without_internals(
    history_client, route_state
):
    client = history_client
    admin = admin_login(client, route_state)
    assert (
        publish(
            client,
            admin,
            "analysis",
            {"enabled": True, "unit_credits": "2", "unit_cost_fen": "0.4"},
            0,
            "首版定价",
        ).status_code
        == 200
    )
    assert (
        publish(
            client,
            admin,
            "analysis",
            {"enabled": True, "unit_credits": "3.5", "unit_cost_fen": "0.6"},
            1,
            "成本上调",
        ).status_code
        == 200
    )
    headers, _ = account(client)
    response = client.get(
        "/api/customer/billing/price-version?service=analysis&version=1", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "service",
        "name",
        "unit",
        "version",
        "current_version",
        "found",
        "current",
        "enabled",
        "unit_credits",
        "unit_rounding",
        "effective_at",
    }
    assert body["found"] is True
    assert body["version"] == 1 and body["current_version"] == 2
    assert body["current"] is False and body["enabled"] is True
    assert Decimal(body["unit_credits"]) == Decimal("2")
    assert body["unit_rounding"] == "ceil" and body["effective_at"]
    assert "unit_cost_fen" not in response.text
    assert "price_admin" not in response.text
    assert "成本上调" not in response.text
    latest = client.get(
        "/api/customer/billing/price-version?service=analysis&version=2", headers=headers
    ).json()
    assert latest["current"] is True and Decimal(latest["unit_credits"]) == Decimal("3.5")
    missing = client.get(
        "/api/customer/billing/price-version?service=analysis&version=9", headers=headers
    )
    assert missing.status_code == 200
    assert missing.json()["found"] is False and missing.json()["current"] is False


def test_customer_price_version_rejects_platform_and_unknown_subjects(history_client, route_state):
    client = history_client
    headers, _ = account(client)
    platform = client.get(
        "/api/customer/billing/price-version?service=cos&version=1", headers=headers
    )
    assert platform.status_code == 422 and "不对用户计费" in platform.text
    unknown = client.get(
        "/api/customer/billing/price-version?service=not_a_subject&version=1", headers=headers
    )
    assert unknown.status_code == 422 and "未知计费科目" in unknown.text
    negative = client.get(
        "/api/customer/billing/price-version?service=analysis&version=-1", headers=headers
    )
    assert negative.status_code == 422 and "版本号" in negative.text


def test_customer_price_version_api_key_is_denied(history_client):
    client = history_client
    headers, _ = account(client)
    created = mutation(client, "/default", headers)
    assert created.status_code == 201, created.text
    denied = client.get(
        "/api/customer/billing/price-version?service=analysis&version=1",
        headers=token_headers(created.json()),
    )
    assert denied.status_code == 403
    assert "API_KEY_SCOPE_DENIED" in denied.text


def test_customer_pricing_update_audits_implicit_tariff_bumps(history_client, route_state):
    """P2-3 断档修复：全局换算保存时被隐式上调的三个科目必须留下审计。"""
    client = history_client
    admin = admin_login(client, route_state)
    assert (
        update_pricing(
            client, admin, {"video_768p": 3, "points_per_yuan": 100}, 0, "视频价目首版"
        ).status_code
        == 200
    )
    history = client.get(
        "/api/control/billing/tariff-history?service=video_768p", headers=admin
    ).json()
    assert history["current_version"] == 1
    assert [item["version"] for item in history["items"]] == [1]
    entry = history["items"][0]
    assert entry["source"] == "audit" and entry["current"] is True
    assert entry["actor_username"] == "price_admin"
    assert entry["reason"] == "视频价目首版"
    assert entry["enabled"] is True
    assert Decimal(entry["unit_credits"]) == Decimal("3")
    assert entry["unit_cost_fen"] is None and entry["unit_rounding"] == "ceil"
    assert (
        update_pricing(
            client, admin, {"video_768p": 4, "points_per_yuan": 100}, 1, "视频价目上调"
        ).status_code
        == 200
    )
    history = client.get(
        "/api/control/billing/tariff-history?service=video_768p", headers=admin
    ).json()
    assert history["current_version"] == 2
    assert [item["version"] for item in history["items"]] == [2, 1]
    assert Decimal(history["items"][0]["unit_credits"]) == Decimal("4")
    assert Decimal(history["items"][1]["unit_credits"]) == Decimal("3")
    unpublicised = client.get(
        "/api/control/billing/tariff-history?service=video_2k", headers=admin
    ).json()
    assert unpublicised["current_version"] == 0 and unpublicised["items"] == []
    with psycopg.connect(route_state) as raw:
        assert (
            raw.execute(
                "SELECT count(*) FROM audit_logs WHERE action='billing.tariff.update' "
                "AND entity_id='video_768p'"
            ).fetchone()[0]
            == 2
        )
