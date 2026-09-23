"""Personal center contract on real password sessions and PostgreSQL."""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import secrets
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
import pytest
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

from app.api_key_routes import router


@pytest.fixture()
def client(registration_client, monkeypatch):
    monkeypatch.setenv("VIDEO_REPLICA_API_KEY_HMAC_KEY", secrets.token_urlsafe(48))
    registration_client.app.include_router(router)
    return registration_client


def account(client, name="center_user"):
    response = client.post("/api/customer/register", json={"username": name, "password": "test-6"})
    assert response.status_code == 201
    response = client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": name,
            "password": "test-6",
            "device_fingerprint": str(uuid4()),
        },
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["session_token"]}, response.json()[
        "user_id"
    ]


def mutation(client, path, headers, body=None, key=None):
    return client.post(
        "/api/customer/api-keys" + path,
        headers={**headers, "Idempotency-Key": key or str(uuid4())},
        json=body or {},
    )


def token_headers(body):
    return {"Authorization": "Bearer " + body["plaintext"]}


def test_default_is_concurrent_idempotent_and_does_not_resurrect(client):
    headers, _ = account(client)
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=5) as pool:
        responses = list(
            pool.map(lambda _: mutation(client, "/default", headers, key=key), range(5))
        )
    assert all(r.status_code == 201 for r in responses), [r.status_code for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert len({r.json()["plaintext"] for r in responses}) == 1
    body = responses[0].json()
    assert body["is_default"] is True
    assert responses[0].headers["cache-control"] == "no-store"
    assert client.delete("/api/customer/api-keys/" + body["id"], headers=headers).status_code == 204
    again = mutation(client, "/default", headers).json()
    assert again["id"] == body["id"] and again["plaintext"] is None
    assert again["revoked_at"] is not None
    listing = client.get("/api/customer/api-keys", headers=headers)
    assert listing.json()["total"] == 1
    assert body["plaintext"] not in listing.text and "key_digest" not in listing.text


def test_rotation_preserves_group_history_and_other_keys(client, route_state):
    headers, user_id = account(client)
    first = mutation(client, "", headers, {"label": "工作电脑"})
    second = mutation(client, "", headers, {"label": "自动脚本"})
    assert first.status_code == second.status_code == 201
    a, b = first.json(), second.json()
    key = str(uuid4())
    rotated = mutation(client, "/" + a["id"] + "/rotate", headers, key=key)
    assert rotated.status_code == 201, rotated.text
    new = rotated.json()
    assert new["token_group_id"] == a["token_group_id"]
    assert new["credential_version"] == 2 and new["id"] != a["id"]
    assert mutation(client, "/" + a["id"] + "/rotate", headers, key=key).json() == new
    path = "/api/customer/recharge-orders"
    assert client.get(path, headers=token_headers(a)).status_code == 401
    assert client.get(path, headers=token_headers(new)).status_code == 200
    assert client.get(path, headers=token_headers(b)).status_code == 200
    assert client.get("/api/customer/api-keys", headers=headers).json()["total"] == 2
    with psycopg.connect(route_state) as conn:
        rows = conn.execute(
            "SELECT id, revoked_at FROM customer_api_keys WHERE user_id = %s", (user_id,)
        ).fetchall()
    assert len(rows) == 3 and next(r[1] for r in rows if r[0] == a["id"]) is not None


def test_key_management_is_scoped_and_disabled_accounts_cannot_use_tokens(client, route_state):
    headers, user_id = account(client)
    body = mutation(client, "", headers, {"label": "private"}).json()
    other, _ = account(client, "another_center_user")
    assert mutation(client, "/" + body["id"] + "/rotate", other).status_code == 404
    assert client.get("/api/customer/api-keys", headers=token_headers(body)).status_code == 401
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = %s", (user_id,))
    assert (
        client.get("/api/customer/recharge-orders", headers=token_headers(body)).status_code == 401
    )


def test_create_replay_conflict_and_summary_use_real_account(client):
    headers, user_id = account(client)
    key = str(uuid4())
    first = mutation(client, "", headers, {"label": "my token"}, key)
    assert first.status_code == 201
    assert mutation(client, "", headers, {"label": "my token"}, key).json() == first.json()
    assert mutation(client, "", headers, {"label": "changed"}, key).status_code == 409
    summary = client.get("/api/customer/center-summary", headers=headers)
    assert summary.status_code == 200, summary.text
    assert summary.json() == {
        "user_id": user_id,
        "available_credits": 0,
        "reserved_credits": 0,
        "total_consumed_credits": 0,
        "active_tokens": 1,
    }


def test_token_scopes_are_enforced_and_revocation_is_rechecked_at_write(client, route_state):
    from fastapi import HTTPException

    from app.customer_fence import ApiKeyUser, BusinessDb

    headers, user_id = account(client)
    body = mutation(client, "", headers, {"label": "restricted"}).json()
    with psycopg.connect(route_state) as conn:
        conn.execute("UPDATE customer_api_keys SET scopes = '[]' WHERE id = %s", (body["id"],))
    assert (
        client.get("/api/customer/recharge-orders", headers=token_headers(body)).status_code == 403
    )
    principal = ApiKeyUser(key_id=body["id"], user_id=user_id, scopes=("recharge",))
    business = BusinessDb(snapshot=None, authorization=None, dev_user_id=None, api_key=principal)
    client.delete("/api/customer/api-keys/" + body["id"], headers=headers)
    with pytest.raises(HTTPException) as rejected:
        with business.write():
            pytest.fail("A credential revoked after the early gate entered the business write")
    assert rejected.value.status_code == 401


def test_active_token_limit_does_not_block_rotation(client, monkeypatch):
    monkeypatch.setenv("VIDEO_REPLICA_MAX_CUSTOMER_API_KEYS", "1")
    headers, _ = account(client)
    body = mutation(client, "/default", headers).json()
    assert mutation(client, "", headers, {"label": "over limit"}).status_code == 409
    rotated = mutation(client, "/" + body["id"] + "/rotate", headers)
    assert rotated.status_code == 201
    assert rotated.json()["is_default"] is True
