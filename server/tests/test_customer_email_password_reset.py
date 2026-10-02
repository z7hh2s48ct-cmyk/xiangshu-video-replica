"""客户邮箱绑定与邮箱找回密码.

锁住的契约：

- **绑定**只在验证码核对通过后写 ``users.email``；错码累加次数、5 次后作废；
  60 秒冷却；邮箱全局唯一；子账号不能绑定。
- **找回**对「账号不存在 / 没绑邮箱 / 子账号」一律回同一个 202 且不发信；
  按用户名或邮箱（大小写不敏感）都能定位。
- **重置**对未知账号与错码同答 400；成功后旧密码失效、新密码可登录、在线会话
  被撤销、验证码不能复用、写一行审计且给绑定邮箱发通知。
- 验证码只存摘要；发信客户端按模板发送，签名与错误映射稳定。

路由用例复用 ``test_customer_registration`` 的共享迁移库（``route_state``）。
"""

# Imported pytest fixtures are injected by parameter name.
# ruff: noqa: F811
import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import requests
from pg_test_kit import require_pg_or_explicit_skip
from test_customer_registration import (
    client as registration_client,  # noqa: F401
)
from test_customer_registration import (
    registration_dsn,  # noqa: F401
    route_state,  # noqa: F401
)

from app import customer_email_routes
from app.customer_email_routes import router as customer_email_router
from app.email_delivery import (
    EmailDeliveryError,
    EmailProviderTester,
    TemplateEmailSender,
    signed_headers,
    validate_email_config,
)
from app.password_hashing import hash_password

STATE_PATH = "/api/customer/account/email"
SEND_PATH = "/api/customer/account/email/send-code"
VERIFY_PATH = "/api/customer/account/email/verify"
FORGOT_PATH = "/api/customer/password/forgot"
RESET_PATH = "/api/customer/password/reset"

PASSWORD = "master-pass-9"
NEXT_PASSWORD = "master-pass-10"
DEFAULT_DSN = "postgresql://testuser:testpass@localhost:5433/customer_v3_test"

SESSION_GONE_CODES = {"SESSION_REPLACED", "SESSION_EXPIRED", "SESSION_TOKEN_REQUIRED"}


@dataclass
class FakeSender:
    codes: list[dict[str, Any]] = field(default_factory=list)
    notices: list[dict[str, Any]] = field(default_factory=list)

    def send_code(self, *, to: str, code: str, action: str, minutes: int) -> None:
        self.codes.append({"to": to, "code": code, "action": action, "minutes": minutes})

    def send_password_reset_notice(self, *, to: str, username: str, occurred_at: str) -> None:
        self.notices.append({"to": to, "username": username, "occurred_at": occurred_at})


@pytest.fixture()
def sender(monkeypatch: pytest.MonkeyPatch) -> FakeSender:
    fake = FakeSender()
    monkeypatch.setattr(customer_email_routes, "email_sender_from_settings", lambda conn: fake)
    return fake


@pytest.fixture()
def client(registration_client, monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("VIDEO_REPLICA_SETTINGS_KEY", Fernet.generate_key().decode("ascii"))
    registration_client.app.include_router(customer_email_router)
    return registration_client


def _pg_dsn() -> str:
    return os.environ.get("TEST_POSTGRESQL_URL", DEFAULT_DSN)


@pytest.fixture(scope="module", autouse=True)
def _require_pg() -> None:
    require_pg_or_explicit_skip(_pg_dsn())


def _username() -> str:
    return "mail" + uuid4().hex[:12]


def _login(client, username: str, password: str):
    return client.post(
        "/api/customer/login",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "username": username,
            "password": password,
            "device_fingerprint": "fp-" + uuid4().hex + uuid4().hex,
        },
    )


def _master(client, username: str) -> dict[str, str]:
    registered = client.post(
        "/api/customer/register", json={"username": username, "password": PASSWORD}
    )
    assert registered.status_code == 201, registered.text
    login = _login(client, username, PASSWORD)
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["session_token"]}


def _code(response) -> str:
    return response.json()["detail"]["code"]


def _bind(client, headers, sender: FakeSender, email: str) -> None:
    sent = client.post(SEND_PATH, headers=headers, json={"email": email})
    assert sent.status_code == 202, sent.text
    verified = client.post(
        VERIFY_PATH, headers=headers, json={"email": email, "code": sender.codes[-1]["code"]}
    )
    assert verified.status_code == 200, verified.text


def _expire_cooldown(dsn: str, username: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE customer_email_codes SET created_at = created_at - interval '2 minutes' "
            "WHERE user_id = (SELECT id FROM users WHERE username = %s)",
            (username,),
        )


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


# ---------------------------------------------------------------------------
# 绑定邮箱
# ---------------------------------------------------------------------------


def test_bind_email_requires_session(client, sender) -> None:
    response = client.post(SEND_PATH, json={"email": "a@example.com"})
    assert response.status_code == 401


def test_bind_email_happy_path_writes_only_after_verification(client, sender, route_state) -> None:
    username = _username()
    headers = _master(client, username)

    state = client.get(STATE_PATH, headers=headers)
    assert state.status_code == 200, state.text
    assert state.json() == {
        "email": None,
        "verified_at": None,
        "can_bind": True,
        "service_available": True,
    }

    sent = client.post(SEND_PATH, headers=headers, json={"email": "  Owner@Example.COM "})
    assert sent.status_code == 202, sent.text
    assert sent.json()["resend_after_seconds"] == 60
    assert len(sender.codes) == 1
    delivered = sender.codes[0]
    assert delivered["to"] == "owner@example.com"
    assert delivered["action"] == "绑定邮箱"
    assert len(delivered["code"]) == 6 and delivered["code"].isdigit()

    # 发码不等于绑定：核对通过前 users.email 仍为空。
    assert client.get(STATE_PATH, headers=headers).json()["email"] is None

    with psycopg.connect(route_state) as conn:
        stored = conn.execute(
            "SELECT code_digest FROM customer_email_codes "
            "WHERE user_id = (SELECT id FROM users WHERE username = %s)",
            (username,),
        ).fetchone()
    assert stored is not None and delivered["code"] not in stored[0]

    wrong = client.post(
        VERIFY_PATH,
        headers=headers,
        json={"email": "owner@example.com", "code": _wrong(delivered["code"])},
    )
    assert wrong.status_code == 400
    assert _code(wrong) == "INVALID_CODE"

    verified = client.post(
        VERIFY_PATH,
        headers=headers,
        json={"email": "OWNER@example.com", "code": delivered["code"]},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["email"] == "owner@example.com"
    assert verified.json()["verified_at"]

    state = client.get(STATE_PATH, headers=headers).json()
    assert state["email"] == "owner@example.com"

    reused = client.post(
        VERIFY_PATH,
        headers=headers,
        json={"email": "owner@example.com", "code": delivered["code"]},
    )
    assert reused.status_code == 400, "a consumed code must not verify twice"

    with psycopg.connect(route_state) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'customer.email.bound' "
            "AND actor_user_id = (SELECT id FROM users WHERE username = %s)",
            (username,),
        ).fetchall()
    assert len(audit) == 1
    assert delivered["code"] not in audit[0][0]


def test_bind_code_resend_has_cooldown(client, sender) -> None:
    headers = _master(client, _username())
    first = client.post(SEND_PATH, headers=headers, json={"email": "cool@example.com"})
    assert first.status_code == 202
    second = client.post(SEND_PATH, headers=headers, json={"email": "cool@example.com"})
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert len(sender.codes) == 1


def test_bind_code_is_scoped_to_its_target_email(client, sender) -> None:
    headers = _master(client, _username())
    client.post(SEND_PATH, headers=headers, json={"email": "mine@example.com"})
    swapped = client.post(
        VERIFY_PATH,
        headers=headers,
        json={"email": "other@example.com", "code": sender.codes[-1]["code"]},
    )
    assert swapped.status_code == 400
    assert _code(swapped) == "INVALID_CODE"


def test_bind_code_dies_after_max_attempts(client, sender) -> None:
    headers = _master(client, _username())
    client.post(SEND_PATH, headers=headers, json={"email": "tries@example.com"})
    code = sender.codes[-1]["code"]
    for _ in range(customer_email_routes.MAX_CODE_ATTEMPTS):
        wrong = client.post(
            VERIFY_PATH, headers=headers, json={"email": "tries@example.com", "code": _wrong(code)}
        )
        assert wrong.status_code == 400
    late = client.post(
        VERIFY_PATH, headers=headers, json={"email": "tries@example.com", "code": code}
    )
    assert late.status_code == 400, "the right code must not work after the attempt budget"


def test_bind_rejects_email_owned_by_another_account(client, sender) -> None:
    owner = _master(client, _username())
    _bind(client, owner, sender, "shared@example.com")

    other = _master(client, _username())
    taken = client.post(SEND_PATH, headers=other, json={"email": "Shared@example.com"})
    assert taken.status_code == 409
    assert _code(taken) == "EMAIL_TAKEN"

    same = client.post(SEND_PATH, headers=owner, json={"email": "shared@example.com"})
    assert same.status_code == 409
    assert _code(same) == "EMAIL_ALREADY_BOUND"


def test_bind_rejects_malformed_email(client, sender) -> None:
    headers = _master(client, _username())
    response = client.post(SEND_PATH, headers=headers, json={"email": "not-an-email"})
    assert response.status_code == 400
    assert _code(response) == "INVALID_EMAIL"
    assert sender.codes == []


def test_unconfigured_mail_service_is_reported(client, monkeypatch) -> None:
    monkeypatch.setattr(customer_email_routes, "email_sender_from_settings", lambda conn: None)
    headers = _master(client, _username())

    assert client.get(STATE_PATH, headers=headers).json()["service_available"] is False
    send = client.post(SEND_PATH, headers=headers, json={"email": "x@example.com"})
    assert send.status_code == 503
    assert _code(send) == "EMAIL_SERVICE_UNAVAILABLE"
    forgot = client.post(FORGOT_PATH, json={"account": "anyone"})
    assert forgot.status_code == 503


# ---------------------------------------------------------------------------
# 找回密码
# ---------------------------------------------------------------------------


def test_forgot_answers_uniformly_and_only_mails_bound_masters(client, sender) -> None:
    unbound = _username()
    _master(client, unbound)

    responses = [
        client.post(FORGOT_PATH, json={"account": "nobody-" + uuid4().hex[:8]}),
        client.post(FORGOT_PATH, json={"account": unbound}),
        client.post(FORGOT_PATH, json={"account": "ghost@example.com"}),
    ]
    for response in responses:
        assert response.status_code == 202, response.text
    assert len({json.dumps(r.json(), sort_keys=True) for r in responses}) == 1
    assert responses[0].json()["message"] == customer_email_routes.FORGOT_ACCEPTED_MESSAGE
    assert sender.codes == []


def test_forgot_locates_account_by_username_or_email(client, sender, route_state) -> None:
    username = _username()
    headers = _master(client, username)
    _bind(client, headers, sender, "finder@example.com")
    sender.codes.clear()

    by_name = client.post(FORGOT_PATH, json={"account": username})
    assert by_name.status_code == 202
    assert [item["to"] for item in sender.codes] == ["finder@example.com"]
    assert sender.codes[0]["action"] == "重置密码"

    # 冷却期内再次请求：答复不变，但不再发信。
    again = client.post(FORGOT_PATH, json={"account": username})
    assert again.status_code == 202
    assert len(sender.codes) == 1

    _expire_cooldown(route_state, username)
    by_email = client.post(FORGOT_PATH, json={"account": " FINDER@example.com "})
    assert by_email.status_code == 202
    assert len(sender.codes) == 2


def test_forgot_ignores_sub_accounts(client, sender, route_state) -> None:
    master_name = _username()
    _master(client, master_name)
    sub_name = _username()
    with psycopg.connect(route_state, autocommit=True) as conn:
        master_id = conn.execute(
            "SELECT id FROM users WHERE username = %s", (master_name,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, password_hash, "
            "registration_source, account_type, parent_user_id, email, email_verified_at) "
            "VALUES (%s, %s, %s, 'customer', %s, 'admin_create', 'SUB', %s, %s, now())",
            (
                str(uuid4()),
                sub_name,
                sub_name,
                hash_password(PASSWORD),
                master_id,
                "sub@example.com",
            ),
        )
    response = client.post(FORGOT_PATH, json={"account": sub_name})
    assert response.status_code == 202
    assert sender.codes == []


# ---------------------------------------------------------------------------
# 重置密码
# ---------------------------------------------------------------------------


def test_reset_password_end_to_end(client, sender, route_state) -> None:
    username = _username()
    headers = _master(client, username)
    _bind(client, headers, sender, "reset@example.com")
    sender.codes.clear()

    client.post(FORGOT_PATH, json={"account": "reset@example.com"})
    code = sender.codes[-1]["code"]

    wrong = client.post(
        RESET_PATH,
        json={"account": username, "code": _wrong(code), "new_password": NEXT_PASSWORD},
    )
    assert wrong.status_code == 400
    assert _code(wrong) == "INVALID_CODE"

    done = client.post(
        RESET_PATH, json={"account": username, "code": code, "new_password": NEXT_PASSWORD}
    )
    assert done.status_code == 200, done.text
    assert done.json() == {"reset": True, "sessions_revoked": 1}

    assert _login(client, username, PASSWORD).status_code == 401
    assert _login(client, username, NEXT_PASSWORD).status_code == 200

    stale = client.get(STATE_PATH, headers=headers)
    assert stale.status_code == 401
    assert _code(stale) in SESSION_GONE_CODES

    reused = client.post(
        RESET_PATH, json={"account": username, "code": code, "new_password": "master-pass-11"}
    )
    assert reused.status_code == 400, "a consumed reset code must not work twice"

    assert [item["to"] for item in sender.notices] == ["reset@example.com"]
    assert sender.notices[0]["username"] == username

    with psycopg.connect(route_state) as conn:
        audit = conn.execute(
            "SELECT metadata_json FROM audit_logs WHERE action = 'customer.password.reset' "
            "AND actor_user_id = (SELECT id FROM users WHERE username = %s)",
            (username,),
        ).fetchall()
        reasons = conn.execute(
            "SELECT reason FROM customer_session_events WHERE event = 'LOGOUT' "
            "AND user_id = (SELECT id FROM users WHERE username = %s)",
            (username,),
        ).fetchall()
    assert len(audit) == 1
    assert json.loads(audit[0][0])["channel"] == "email"
    assert ("password_reset",) in reasons


def test_reset_answers_unknown_accounts_like_wrong_codes(client, sender) -> None:
    unknown = client.post(
        RESET_PATH,
        json={"account": "nobody-" + uuid4().hex[:8], "code": "123456", "new_password": PASSWORD},
    )
    assert unknown.status_code == 400
    assert _code(unknown) == "INVALID_CODE"


def test_reset_rejects_weak_password_before_touching_the_code(client, sender) -> None:
    username = _username()
    headers = _master(client, username)
    _bind(client, headers, sender, "weak@example.com")
    client.post(FORGOT_PATH, json={"account": username})
    code = sender.codes[-1]["code"]

    weak = client.post(RESET_PATH, json={"account": username, "code": code, "new_password": "123"})
    assert weak.status_code == 400
    assert _code(weak) == "WEAK_PASSWORD"

    ok = client.post(
        RESET_PATH, json={"account": username, "code": code, "new_password": NEXT_PASSWORD}
    )
    assert ok.status_code == 200, "a policy rejection must not burn the emailed code"


def test_reset_rejects_malformed_code_shape(client, sender) -> None:
    response = client.post(
        RESET_PATH, json={"account": "someone", "code": "12ab56", "new_password": PASSWORD}
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 发信客户端（无数据库）
# ---------------------------------------------------------------------------

_CONFIG = {
    "secret_id": "AKIDexample",
    "secret_key": "example-secret-key",
    "region": "ap-guangzhou",
    "from_address": "账号服务 <noreply@mail.example.com>",
    "code_template_id": "1001",
    "notice_template_id": "1002",
}


class _FakeResponse:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body


def test_signed_headers_are_deterministic_and_scoped() -> None:
    kwargs = {
        "secret_id": "AKIDexample",
        "secret_key": "example-secret-key",
        "action": "SendEmail",
        "region": "ap-guangzhou",
        "payload": "{}",
        "timestamp": 1_790_000_000,
    }
    first = signed_headers(**kwargs)
    assert first == signed_headers(**kwargs)
    assert first["X-TC-Action"] == "SendEmail"
    assert first["X-TC-Version"] == "2020-10-02"
    assert first["X-TC-Region"] == "ap-guangzhou"
    assert first["Authorization"].startswith(
        "TC3-HMAC-SHA256 Credential=AKIDexample/2026-09-21/ses/tc3_request, "
        "SignedHeaders=content-type;host;x-tc-action, Signature="
    )
    assert "example-secret-key" not in json.dumps(first)
    changed = signed_headers(**{**kwargs, "payload": '{"a":1}'})
    assert changed["Authorization"] != first["Authorization"]


def test_template_sender_posts_template_payload() -> None:
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append({"url": url, **kwargs})
        return _FakeResponse({"Response": {"MessageId": "m-1", "RequestId": "r-1"}})

    sender = TemplateEmailSender(_CONFIG, post=post)
    sender.send_code(to="a@example.com", code="012345", action="重置密码", minutes=15)
    body = json.loads(calls[0]["data"].decode("utf-8"))
    assert body["Destination"] == ["a@example.com"]
    assert body["Template"]["TemplateID"] == 1001
    assert json.loads(body["Template"]["TemplateData"]) == {
        "action": "重置密码",
        "code": "012345",
        "minutes": "15",
    }
    assert body["TriggerType"] == 1

    sender.send_password_reset_notice(
        to="a@example.com", username="alice", occurred_at="2026-09-28 17:30"
    )
    notice = json.loads(calls[1]["data"].decode("utf-8"))
    assert notice["Template"]["TemplateID"] == 1002


def test_template_sender_skips_notice_without_template() -> None:
    calls: list[Any] = []
    config = {key: value for key, value in _CONFIG.items() if key != "notice_template_id"}
    sender = TemplateEmailSender(config, post=lambda url, **kw: calls.append(kw))
    sender.send_password_reset_notice(to="a@example.com", username="alice", occurred_at="now")
    assert calls == []


def test_alert_template_missing_is_explicit_and_never_calls_transport() -> None:
    calls: list[Any] = []
    config = {key: value for key, value in _CONFIG.items() if key != "notice_template_id"}
    sender = TemplateEmailSender(config, post=lambda url, **kw: calls.append(kw))
    assert sender.alert_digest_configured is False
    with pytest.raises(EmailDeliveryError):
        sender.send_alert_digest(
            to="synthetic@example.com",
            total=1,
            danger_count=0,
            items="高敏操作需要核对",
            generated_at="2026-10-02T08:00:00Z",
        )
    assert calls == []


def test_alert_template_contains_warn_headline_using_existing_template_keys() -> None:
    calls: list[dict[str, Any]] = []

    def post(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append(kwargs)
        return _FakeResponse({"Response": {"MessageId": "synthetic", "RequestId": "fake"}})

    sender = TemplateEmailSender(_CONFIG, post=post)
    assert sender.alert_digest_configured is True
    sender.send_alert_digest(
        to="synthetic@example.com",
        total=1,
        danger_count=0,
        items="高敏操作需要核对",
        generated_at="2026-10-02T08:00:00Z",
    )
    body = json.loads(calls[0]["data"].decode("utf-8"))
    data = json.loads(body["Template"]["TemplateData"])
    assert set(data) == {"username", "time"}
    assert data["username"] == "高敏操作需要核对"
    assert "0 条紧急 / 共 1 条" in body["Subject"]


def test_template_sender_maps_errors_without_leaking_transport_details() -> None:
    def api_error(url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(
            {"Response": {"Error": {"Code": "FailedOperation.TemplateNotApproved"}}}
        )

    with pytest.raises(EmailDeliveryError) as rejected:
        TemplateEmailSender(_CONFIG, post=api_error).check_template()
    assert rejected.value.code == "FailedOperation.TemplateNotApproved"

    def network_error(url: str, **kwargs: Any) -> _FakeResponse:
        raise requests.ConnectionError("https://upstream.example/ unreachable")

    with pytest.raises(EmailDeliveryError) as unreachable:
        TemplateEmailSender(_CONFIG, post=network_error).check_template()
    assert unreachable.value.code == "ConnectionError"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"region": "us-east-1"}, "发信地域"),
        ({"from_address": "noreply"}, "发信地址"),
        ({"code_template_id": "abc"}, "模板 ID"),
        ({"notice_template_id": "0"}, "模板 ID"),
    ],
)
def test_validate_email_config_rejects_bad_settings(patch: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_email_config({**_CONFIG, **patch})


def test_email_provider_tester_checks_template_without_sending() -> None:
    checked: list[str] = []

    class _Probe:
        def __init__(self, config: dict[str, str]) -> None:
            self.config = config

        def check_template(self) -> None:
            checked.append(self.config["code_template_id"])

    tester = EmailProviderTester(fallback=None, sender_factory=_Probe)
    result = tester.connection_test("ses", dict(_CONFIG))
    assert result.status == "ok"
    assert checked == ["1001"]
