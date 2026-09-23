"""Seed a dedicated customer-E2E PostgreSQL database with admin + activation codes
and a throwaway ZPay merchant configuration.

Usage:
    python e2e/customer/seed_codes.py

Reads env (same variables the backend runner exports):
    CUSTOMER_E2E_DATABASE_URL   postgres DSN of the already-migrated E2E database
    VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY   plaintext HMAC key (>=32 bytes)

The ZPay seeding keeps the browser recharge E2E working: the wallet's 充值
button calls POST /api/customer/recharge-orders, which reads the ZPay merchant
config from provider_settings (decrypted with VIDEO_REPLICA_SETTINGS_KEY) and
the callback origin from PUBLIC_BASE_URL; the documented gateway is fixed in
code. This is the same shape
test_customer_recharge.py::recharge_config_fixture uses. Without it the route
answers 503 ZPAY_CONFIGURATION_INVALID.

The plaintext codes are printed once at the end so the Playwright spec can
drive the activation UI with them, and the Fernet key the ZPay config was
encrypted with is printed for the runner to pass to the API process. Never a
real key: the E2E runner generates a throwaway key for the run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.activation_code_service import (  # noqa: E402
    compute_code_digest,
    mask_activation_code,
)

E2E_CODES = [
    "XS04-ABCDEFG-HJKLMNP-QRSTVWX-YZ23456",
    "XS04-2345678-9ABCDEF-GHJKLMN-PQRSTVW",
    "XS04-XYZ2345-6789ABC-DEFGHJK-MNPQRST",
    "XS04-1234567-89ABCDE-FGHJKMN-PQRSTVW",
]

# Throwaway merchant credentials — never a real ZPay pid/secret.
ZPAY_MERCHANT_CONFIG = (
    b'{"pid":"merchant-123","key":"merchant-secret","enabled_channels":"alipay,wxpay"}'
)


def main() -> None:
    dsn = os.environ["CUSTOMER_E2E_DATABASE_URL"]
    key = os.environ["VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY"].encode("utf-8")
    if len(key) < 32:
        raise SystemExit("ACTIVATION_CODE_HMAC_KEY must be at least 32 bytes")

    settings_fernet_key = Fernet.generate_key()
    encrypted_config = Fernet(settings_fernet_key).encrypt(ZPAY_MERCHANT_CONFIG).decode("ascii")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (id, username, display_name, role) "
            "VALUES ('admin_e2e', 'admin_e2e', 'Admin E2E', 'admin') "
            "ON CONFLICT (id) DO NOTHING"
        )
        for i, code in enumerate(E2E_CODES, start=1):
            code_id = f"e2e-code-{i}"
            batch_id = f"e2e-batch-{i}"
            conn.execute(
                "INSERT INTO activation_code_batches ("
                " id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot,"
                " quantity, activation_expires_at, status, created_by_user_id, created_at)"
                " VALUES (%s, %s, 10000, 10000, 1000, 1, '2099-01-01T00:00:00+00:00',"
                " 'OPEN', 'admin_e2e', CURRENT_TIMESTAMP) ON CONFLICT (id) DO NOTHING",
                (batch_id, f"E2E Batch {i}"),
            )
            conn.execute(
                "INSERT INTO activation_codes ("
                " id, batch_id, code_digest, digest_key_version, masked_code,"
                " status, issued_at, activated_at)"
                " VALUES (%s, %s, %s, 1, %s, 'ISSUED', CURRENT_TIMESTAMP, NULL)"
                " ON CONFLICT (id) DO NOTHING",
                (
                    code_id,
                    batch_id,
                    compute_code_digest(code, key=key),
                    mask_activation_code(code),
                ),
            )
            print(f"SEEDED {code_id} {code}")
        conn.execute(
            """
            INSERT INTO provider_settings (
                provider, encrypted_config, updated_by_user_id, created_at, updated_at
            ) VALUES ('zpay', %s, 'admin_e2e', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(provider) DO UPDATE SET
                encrypted_config = excluded.encrypted_config,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = CURRENT_TIMESTAMP
            """,
            (encrypted_config,),
        )
        # 客户 E2E 没有 COS 凭据，素材/资产上传会 503 STORAGE_PROVIDER_FORBIDDEN。
        # 改用本地持久盘（配套 VIDEO_REPLICA_STORAGE_ROOT，见 setup-backend.mjs），
        # 让上传、归档等触达存储的链路在隔离环境里可跑。
        conn.execute(
            "UPDATE runtime_settings SET active_storage_provider = 'local' WHERE id = 1"
        )
        # 充值套餐（RECHARGE-PACKAGES-20260922）：充值对话框的档位由管理员配置的套餐
        # 提供（写死的 100/300/500 预设已移除）；recharge.spec.mjs 点选 100 元档位走
        # 真实套餐下单链路，需要一行启用套餐。名称以「100 元」开头以匹配该 spec 的
        # 档位断言；无折扣/赠送到账与基础汇率一致（1 元 = 10 积分）。
        conn.execute(
            "INSERT INTO recharge_packages ("
            " id, name, amount_fen, credits, discount_rate, discount_interfaces,"
            " sort_order, is_active, version, created_by_user_id, created_at, updated_at)"
            " VALUES ('e2e-package-100', '100 元档', 10000, 1000, NULL, '[]',"
            " 0, true, 0, 'admin_e2e', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            " ON CONFLICT (id) DO NOTHING"
        )

    print(f"SEEDED admin_e2e + {len(E2E_CODES)} codes into {dsn.split('@')[-1]}")
    # The runner captures this to set VIDEO_REPLICA_SETTINGS_KEY on the API.
    print(f"VIDEO_REPLICA_SETTINGS_KEY={settings_fernet_key.decode('ascii')}")


if __name__ == "__main__":
    main()
