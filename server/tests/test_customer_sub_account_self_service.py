"""CW-062 desktop follow-up — the customer self-service sub-account lane.

``/api/customer/sub-accounts`` over the migrated PostgreSQL fixture with real
password sessions (the ``test_customer_center`` precedent):

- a master creates / lists / renames / disables / resets / deletes its own
  sub-accounts, and the created sub logs in immediately;
- login and profile both carry the sub identity (account_type / parent);
- a sub-account session is refused by the management lane, a foreign or
  unknown id is the single 404 (no IDOR oracle);
- deactivation and password rotation revoke the sub's live session at once,
  and deactivating the master freezes every session riding under it;
- DELETE answers ``deleted: true`` for a sub with no session history; a
  history (the append-only 029 event log) pins the account and the answer
  degrades to deactivation instead of failing.
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
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

from app.customer_sub_account_routes import router as customer_sub_account_router

SUB_ACCOUNTS_PATH = "/api/customer/sub-accounts"
PROFILE_PATH = "/api/customer/profile"

MASTER_PASSWORD = "master-pass-9"
SUB_PASSWORD = "sub-pass-9"


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    # The wallet read decrypts the customer billing settings through
    # SettingsRepository, so the Fernet root key joins the registration env.
    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    registration_client.app.include_router(customer_sub_account_router)
    return registration_client


def _fingerprint() -> str:
    return "fp-" + uuid4().hex + uuid4().hex


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": _fingerprint(),
        },
    )


def master_session(client, username: str):
    """Register a master and log it in; returns (headers, login body)."""
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": MASTER_PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    login = _login(client, username, MASTER_PASSWORD)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def sub_session(client, username: str, password: str = SUB_PASSWORD):
    """Log a sub-account in; returns (headers, login body)."""
    login = _login(client, username, password)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}, login.json()


def create_sub(
    client,
    headers,
    *,
    username: str,
    display_name: str = "子账号",
    password: str | None = SUB_PASSWORD,
):
    body: dict[str, str] = {"username": username, "display_name": display_name}
    if password is not None:
        body["password"] = password
    return client.post(SUB_ACCOUNTS_PATH, headers=headers, json=body)


def detail_code(response) -> str:
    return response.json()["detail"]["code"]


# ---------------------------------------------------------------------------
# Master life-cycle + sub identity
# ---------------------------------------------------------------------------


def test_lifecycle_create_login_identity_list_rename(client):
    master_headers, master = master_session(client, "org_master_01")
    assert master["account_type"] == "MASTER"
    assert master["parent_user_id"] is None

    created = create_sub(client, master_headers, username="assistant_01", display_name="助理一号")
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["account_type"] == "SUB"
    assert sub["parent_user_id"] == master["user_id"]
    assert sub["is_active"] is True
    assert sub["has_password"] is True
    assert sub["display_name"] == "助理一号"

    # The sub logs in immediately; its identity rides the login answer.
    sub_headers, login = sub_session(client, "assistant_01")
    assert login["account_type"] == "SUB"
    assert login["parent_user_id"] == master["user_id"]
    assert login["parent_display_name"] == master["display_name"]

    sub_profile = client.get(PROFILE_PATH, headers=sub_headers)
    assert sub_profile.status_code == 200, sub_profile.text
    assert sub_profile.json()["account_type"] == "SUB"
    assert sub_profile.json()["parent_user_id"] == master["user_id"]
    assert sub_profile.json()["parent_display_name"] == master["display_name"]
    master_profile = client.get(PROFILE_PATH, headers=master_headers)
    assert master_profile.json()["account_type"] == "MASTER"

    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_headers)
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["sub_accounts"]] == [sub["id"]]

    renamed = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub['id']}",
        headers=master_headers,
        json={"display_name": "夜班助理"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["display_name"] == "夜班助理"


def test_delete_removes_a_sub_without_session_history(client):
    master_headers, _ = master_session(client, "org_master_02")
    created = create_sub(client, master_headers, username="never_logged_in")
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]

    deleted = client.delete(f"{SUB_ACCOUNTS_PATH}/{sub_id}", headers=master_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"id": sub_id, "deleted": True, "is_active": False}
    assert client.get(SUB_ACCOUNTS_PATH, headers=master_headers).json()["sub_accounts"] == []


def test_delete_degrades_to_deactivation_when_a_session_history_pins(client):
    master_headers, _ = master_session(client, "org_master_03")
    created = create_sub(client, master_headers, username="weekend_worker")
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    sub_headers, _ = sub_session(client, "weekend_worker")
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 200

    deleted = client.delete(f"{SUB_ACCOUNTS_PATH}/{sub_id}", headers=master_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"id": sub_id, "deleted": False, "is_active": False}

    # The account survives, deactivated, and its live session is gone.
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 401
    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_headers).json()["sub_accounts"]
    assert [(item["id"], item["is_active"]) for item in listing] == [(sub_id, False)]


# ---------------------------------------------------------------------------
# Fence and scope
# ---------------------------------------------------------------------------


def test_sub_account_session_is_refused_by_the_management_lane(client):
    master_headers, _ = master_session(client, "org_master_04")
    assert create_sub(client, master_headers, username="regular_sub").status_code == 201
    sub_headers, _ = sub_session(client, "regular_sub")

    listed = client.get(SUB_ACCOUNTS_PATH, headers=sub_headers)
    assert listed.status_code == 403
    assert detail_code(listed) == "MASTER_ACCOUNT_REQUIRED"
    attempt = client.post(
        SUB_ACCOUNTS_PATH,
        headers=sub_headers,
        json={"username": "illegal_child", "display_name": "非法"},
    )
    assert attempt.status_code == 403
    assert detail_code(attempt) == "MASTER_ACCOUNT_REQUIRED"
    assert client.get(SUB_ACCOUNTS_PATH).status_code == 401


def test_foreign_and_unknown_ids_share_the_single_404(client):
    master_a_headers, _ = master_session(client, "org_master_a")
    master_b_headers, _ = master_session(client, "org_master_b")
    created = create_sub(client, master_b_headers, username="b_worker")
    assert created.status_code == 201, created.text
    b_sub = created.json()

    foreign_patch = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{b_sub['id']}",
        headers=master_a_headers,
        json={"display_name": "篡改"},
    )
    assert foreign_patch.status_code == 404
    assert detail_code(foreign_patch) == "SUB_ACCOUNT_NOT_FOUND"
    foreign_delete = client.delete(f"{SUB_ACCOUNTS_PATH}/{b_sub['id']}", headers=master_a_headers)
    assert foreign_delete.status_code == 404
    assert detail_code(foreign_delete) == "SUB_ACCOUNT_NOT_FOUND"
    foreign_password = client.post(
        f"{SUB_ACCOUNTS_PATH}/{b_sub['id']}/password",
        headers=master_a_headers,
        json={"password": "tamper-pass-9"},
    )
    assert foreign_password.status_code == 404
    unknown_patch = client.patch(
        f"{SUB_ACCOUNTS_PATH}/sub-does-not-exist",
        headers=master_a_headers,
        json={"display_name": "x"},
    )
    assert unknown_patch.status_code == 404
    assert detail_code(unknown_patch) == "SUB_ACCOUNT_NOT_FOUND"

    # The foreign sub is untouched and its own master still sees it.
    listing = client.get(SUB_ACCOUNTS_PATH, headers=master_b_headers).json()["sub_accounts"]
    assert [(item["id"], item["display_name"]) for item in listing] == [(b_sub["id"], "子账号")]


# ---------------------------------------------------------------------------
# Organisation wallet (T2.10 — the sub spends the master's credits)
# ---------------------------------------------------------------------------


def test_sub_account_reads_the_master_wallet_and_center_summary(client, route_state: str):
    master_headers, master = master_session(client, "org_master_11")
    assert create_sub(client, master_headers, username="spender").status_code == 201
    sub_headers, sub_login = sub_session(client, "spender")

    # The sub owns no wallet row: the organisation's credits are the master's.
    # A top-up written to the master's wallet is what the sub must read.
    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute(
            "UPDATE wallets SET available_credits = 120 WHERE user_id = %s",
            (master["user_id"],),
        )
        sub_wallet = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = %s", (sub_login["user_id"],)
        ).fetchone()
    assert sub_wallet is None

    wallet = client.get("/api/customer/wallet", headers=sub_headers)
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["available_credits"] == 120

    summary = client.get("/api/customer/center-summary", headers=sub_headers)
    assert summary.status_code == 200, summary.text
    assert summary.json()["user_id"] == sub_login["user_id"]
    assert summary.json()["available_credits"] == 120


# ---------------------------------------------------------------------------
# Session revocation (SES-03 propagation)
# ---------------------------------------------------------------------------


def test_deactivation_revokes_the_live_session_and_reenabling_restores_login(client):
    master_headers, _ = master_session(client, "org_master_06")
    created = create_sub(client, master_headers, username="temp_worker")
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    sub_headers, _ = sub_session(client, "temp_worker")
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 200

    disabled = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub_id}",
        headers=master_headers,
        json={"is_active": False},
    )
    assert disabled.status_code == 200 and disabled.json()["is_active"] is False
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 401
    assert _login(client, "temp_worker", SUB_PASSWORD).status_code == 401

    enabled = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{sub_id}",
        headers=master_headers,
        json={"is_active": True},
    )
    assert enabled.status_code == 200 and enabled.json()["is_active"] is True
    relogin_headers, _ = sub_session(client, "temp_worker")
    assert client.get(PROFILE_PATH, headers=relogin_headers).status_code == 200


def test_password_rotation_revokes_the_old_session_and_password(client):
    master_headers, _ = master_session(client, "org_master_07")
    created = create_sub(client, master_headers, username="rotate_worker")
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    old_headers, _ = sub_session(client, "rotate_worker")

    rotated = client.post(
        f"{SUB_ACCOUNTS_PATH}/{sub_id}/password",
        headers=master_headers,
        json={"password": "brand-new-pass-9"},
    )
    assert rotated.status_code == 200, rotated.text
    assert rotated.json() == {"id": sub_id, "has_password": True}
    assert client.get(PROFILE_PATH, headers=old_headers).status_code == 401
    assert _login(client, "rotate_worker", SUB_PASSWORD).status_code == 401

    new_headers, _ = sub_session(client, "rotate_worker", "brand-new-pass-9")
    assert client.get(PROFILE_PATH, headers=new_headers).status_code == 200


def test_deactivating_the_master_freezes_the_sub_session(client, route_state: str):
    master_headers, master = master_session(client, "org_master_10")
    assert create_sub(client, master_headers, username="night_shift").status_code == 201
    sub_headers, _ = sub_session(client, "night_shift")
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 200

    with psycopg.connect(route_state, autocommit=True) as conn:
        conn.execute("UPDATE users SET is_active = 0 WHERE id = %s", (master["user_id"],))

    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 401
    assert _login(client, "night_shift", SUB_PASSWORD).status_code == 401


# ---------------------------------------------------------------------------
# Password admission + stable error codes
# ---------------------------------------------------------------------------


def test_creation_without_password_defers_login_until_the_master_sets_one(client, route_state: str):
    master_headers, _ = master_session(client, "org_master_08")
    created = create_sub(client, master_headers, username="pending_worker", password=None)
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["has_password"] is False
    assert _login(client, "pending_worker", "anything-9").status_code == 401

    with psycopg.connect(route_state) as conn:
        row = conn.execute(
            "SELECT password_hash, registration_source, account_type FROM users WHERE id = %s",
            (sub["id"],),
        ).fetchone()
    assert row is not None and row[0] is None and row[1] is None and row[2] == "SUB"

    set_password = client.post(
        f"{SUB_ACCOUNTS_PATH}/{sub['id']}/password",
        headers=master_headers,
        json={"password": SUB_PASSWORD},
    )
    assert set_password.status_code == 200, set_password.text
    with psycopg.connect(route_state) as conn:
        source = conn.execute(
            "SELECT registration_source FROM users WHERE id = %s", (sub["id"],)
        ).fetchone()
    assert source is not None and source[0] == "admin_create"

    sub_headers, login = sub_session(client, "pending_worker")
    assert login["account_type"] == "SUB"
    assert client.get(PROFILE_PATH, headers=sub_headers).status_code == 200


def test_stable_error_codes_for_conflicts_and_policy(client):
    master_headers, _ = master_session(client, "org_master_09")
    assert create_sub(client, master_headers, username="duplicate_name").status_code == 201

    taken = create_sub(client, master_headers, username="duplicate_name")
    assert taken.status_code == 409
    assert detail_code(taken) == "USERNAME_TAKEN"

    weak = create_sub(client, master_headers, username="weak_worker", password="123")
    assert weak.status_code == 400
    assert detail_code(weak) == "WEAK_PASSWORD"

    blank_display = client.post(
        SUB_ACCOUNTS_PATH,
        headers=master_headers,
        json={"username": "blank_worker", "display_name": "   "},
    )
    assert blank_display.status_code == 400
    assert detail_code(blank_display) == "INVALID_DISPLAY_NAME"

    created = create_sub(client, master_headers, username="update_target")
    assert created.status_code == 201, created.text
    empty_update = client.patch(
        f"{SUB_ACCOUNTS_PATH}/{created.json()['id']}", headers=master_headers, json={}
    )
    assert empty_update.status_code == 400
    assert detail_code(empty_update) == "EMPTY_UPDATE"
