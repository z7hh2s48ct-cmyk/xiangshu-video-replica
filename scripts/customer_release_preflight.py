#!/usr/bin/env python3
"""Fail-closed T45 checks for a customer production environment file.

The command deliberately reports variable names and file metadata only. It
never prints environment values or the metrics bearer token.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEV_AUTH_MODES = {"desktop", "development"}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def token_permissions_are_too_open(mode: int) -> bool:
    return bool(stat.S_IMODE(mode) & 0o077)


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment assignment at line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def release_preflight_errors(
    env_file: Path,
    *,
    service_uid: int | None = None,
    enforce_posix_permissions: bool | None = None,
) -> list[str]:
    errors: list[str] = []
    try:
        values = parse_env_file(env_file)
    except (OSError, UnicodeError, ValueError) as exc:
        return [f"cannot parse env file: {type(exc).__name__}"]

    if (
        values.get("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "").strip().lower()
        not in TRUTHY
    ):
        errors.append("VIDEO_REPLICA_CUSTOMER_PRODUCTION must be true")
    if values.get("VIDEO_REPLICA_AUTH_MODE", "").strip().lower() in DEV_AUTH_MODES:
        errors.append(
            "VIDEO_REPLICA_AUTH_MODE must not use a development identity mode"
        )
    if (
        values.get("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER", "").strip().lower()
        in TRUTHY
    ):
        errors.append("VIDEO_REPLICA_ALLOW_DEV_IDENTITY_HEADER must be disabled")
    if values.get("VIDEO_REPLICA_DESKTOP_USER_ID", "").strip():
        errors.append("VIDEO_REPLICA_DESKTOP_USER_ID must be unset")

    token_name = "VIDEO_REPLICA_METRICS_TOKEN_FILE"
    token_path_text = values.get(token_name, "").strip()
    if not token_path_text:
        errors.append(f"{token_name} must be configured")
        return errors
    token_path = Path(token_path_text)
    if not token_path.is_absolute():
        errors.append(f"{token_name} must be an absolute path")
        return errors
    try:
        token_stat = token_path.stat()
        token = token_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        errors.append("metrics token file must exist and be readable")
        return errors
    if not stat.S_ISREG(token_stat.st_mode):
        errors.append("metrics token path must be a regular file")
    if len(token) < 32:
        errors.append("metrics token must contain at least 32 characters")

    check_permissions = (
        os.name == "posix"
        if enforce_posix_permissions is None
        else enforce_posix_permissions
    )
    if check_permissions and token_permissions_are_too_open(token_stat.st_mode):
        errors.append("metrics token file must not grant group or other permissions")
    if service_uid is not None and token_stat.st_uid != service_uid:
        errors.append("metrics token file owner does not match the service account")
    return errors


def _service_uid(name: str | None) -> int | None:
    if name is None:
        return None
    if os.name != "posix":
        raise ValueError("--service-user is supported only on POSIX hosts")
    import pwd

    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError as exc:
        raise ValueError("service account does not exist") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate T45 customer release red lines"
    )
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--service-user")
    args = parser.parse_args(argv)
    try:
        uid = _service_uid(args.service_user)
    except ValueError as exc:
        print(f"T45_PREFLIGHT_FAIL: {exc}", file=sys.stderr)
        return 2
    errors = release_preflight_errors(args.env_file, service_uid=uid)
    if errors:
        for error in errors:
            print(f"T45_PREFLIGHT_FAIL: {error}", file=sys.stderr)
        return 1
    print("T45_PREFLIGHT_OK: customer identity and metrics-token checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
