from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.generation import build_h3_request
from app.settings import mask_config, normalize_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def _bash_path() -> str | None:
    # Resolve the interpreter explicitly: Windows' CreateProcess searches
    # System32 before PATH, so a bare "bash" argument can silently resolve
    # to the WSL launcher stub (System32\bash.exe / WindowsApps\bash.exe)
    # even when git-bash is first on PATH. The stub's exit status drifts with
    # the WSL service state and its UTF-16 diagnostics kill text-mode output
    # readers mid-decode, so the scan needs a native Windows POSIX shell
    # (git-bash) or a real POSIX system.
    bash = shutil.which("bash")
    if bash is None:
        return None
    if sys.platform == "win32":
        lowered = {part.lower() for part in Path(bash).parts}
        if "system32" in lowered or "windowsapps" in lowered:
            return None
    probe = subprocess.run(
        [bash, "-c", "exit 0"],
        check=False,
        capture_output=True,
        timeout=30,
    )
    return bash if probe.returncode == 0 else None


BASH = _bash_path()


def test_h3_provider_request_contract_does_not_embed_credentials() -> None:
    request = build_h3_request(
        prompt_text="只生成视频，不携带任何 Provider 凭证。",
        first_frame_url="local://first-frame.png",
        duration_seconds=10,
        resolution="768P",
    )
    serialized = json.dumps(request, ensure_ascii=False).lower()

    assert set(request) == {"model", "content", "resolution", "duration", "ratio"}
    assert "authorization" not in serialized
    assert "api_key" not in serialized
    assert "token" not in serialized
    assert "secret" not in serialized


def test_provider_config_normalization_masks_secret_like_fields() -> None:
    normalized = normalize_config(
        {
            "api_key": " metaso-secret-token ",
            "Authorization": " Bearer should-not-echo ",
            "access_key_id": " cloud-access-id ",
            "base_url": " https://metaso.example/api ",
        }
    )

    assert normalized == {
        "api_key": "metaso-secret-token",
        "Authorization": "Bearer should-not-echo",
        "access_key_id": "cloud-access-id",
        "base_url": "https://metaso.example/api",
    }
    assert mask_config(normalized) == {
        "api_key": "********oken",
        "Authorization": "********echo",
        "access_key_id": "********s-id",
        "base_url": "https://metaso.example/api",
    }


@pytest.mark.skipif(
    not (REPO_ROOT / "scripts" / "verify_no_secrets.sh").exists() or BASH is None,
    reason="secret scan script or a functional bash is not available",
)
def test_secret_scan_script_passes_on_repository_contract_surface() -> None:
    assert BASH is not None
    result = subprocess.run(
        [BASH, "scripts/verify_no_secrets.sh"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
    )

    # str() keeps the failure message renderable even when the shell emits
    # bytes that the ambient Windows code page cannot decode.
    assert result.returncode == 0, str(result.stdout) + str(result.stderr)


def test_no_sentry_sdk_enters_the_server_runtime() -> None:
    """CW-009/S2: the Sentry medium is structurally absent.

    The runtime contract carries no Sentry SDK and no DSN, so "credentials
    never reach Sentry" holds structurally. If an SDK is ever introduced,
    this contract fails and plaintext-leak assertions for captured events
    (CW-009 S2) must land in the same change.
    """
    pyproject = (REPO_ROOT / "server" / "pyproject.toml").read_text()
    assert "sentry" not in pyproject.lower()
    for path in sorted((REPO_ROOT / "server" / "app").rglob("*.py")):
        assert "sentry" not in path.read_text().lower(), str(path)
