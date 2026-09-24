from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_preflight_module() -> ModuleType:
    script = Path(__file__).resolve().parents[2] / "scripts" / "customer_release_preflight.py"
    spec = importlib.util.spec_from_file_location("customer_release_preflight", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_customer_release_preflight_accepts_hardened_configuration(tmp_path: Path) -> None:
    preflight = load_preflight_module()
    token_file = tmp_path / "metrics.token"
    token_file.write_text("x" * 40, encoding="utf-8")
    token_file.chmod(0o600)
    env_file = tmp_path / "customer.env"
    env_file.write_text(
        "VIDEO_REPLICA_CUSTOMER_PRODUCTION=true\n"
        "VIDEO_REPLICA_AUTH_MODE=customer\n"
        "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=0\n"
        f"VIDEO_REPLICA_METRICS_TOKEN_FILE={token_file}\n",
        encoding="utf-8",
    )

    assert (
        preflight.release_preflight_errors(
            env_file,
            enforce_posix_permissions=False,
        )
        == []
    )


def test_customer_release_preflight_rejects_dev_identity_and_weak_metrics_file(
    tmp_path: Path,
) -> None:
    preflight = load_preflight_module()
    token_file = tmp_path / "metrics.token"
    token_file.write_text("do-not-print-this-token", encoding="utf-8")
    token_file.chmod(0o644)
    env_file = tmp_path / "customer.env"
    env_file.write_text(
        "VIDEO_REPLICA_CUSTOMER_PRODUCTION=true\n"
        "VIDEO_REPLICA_AUTH_MODE=desktop\n"
        "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER=yes\n"
        "VIDEO_REPLICA_DESKTOP_USER_ID=dev-user\n"
        f"VIDEO_REPLICA_METRICS_TOKEN_FILE={token_file}\n",
        encoding="utf-8",
    )

    errors = preflight.release_preflight_errors(
        env_file,
        enforce_posix_permissions=False,
    )

    assert errors == [
        "VIDEO_REPLICA_AUTH_MODE must not use a development identity mode",
        "VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER must be disabled",
        "VIDEO_REPLICA_DESKTOP_USER_ID must be unset",
        "metrics token must contain at least 32 characters",
    ]
    assert "do-not-print-this-token" not in "\n".join(errors)


def test_customer_release_preflight_rejects_group_or_world_token_permissions() -> None:
    preflight = load_preflight_module()

    assert preflight.token_permissions_are_too_open(0o100644) is True
    assert preflight.token_permissions_are_too_open(0o100600) is False
