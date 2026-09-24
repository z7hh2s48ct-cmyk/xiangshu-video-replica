#!/usr/bin/env bash
# Register this machine as a self-hosted GitHub Actions runner for the repo.
#
# SECURITY (hard rule): the registration token is NEVER hardcoded and NEVER
# echoed. Provide it either interactively (prompted with echo disabled) or via
# the RUNNER_TOKEN environment variable for one shot:
#   RUNNER_TOKEN=AXXXX... bash register-runner.sh
# Generate a token in the repo UI:
#   Settings -> Actions -> Runners -> New self-hosted runner
# Tokens are short-lived (~1h); re-run this script with a fresh one if it lapses.
#
# The runner auto-registers its own OS/arch labels (self-hosted, linux, X64 or
# ARM64). We add exactly ONE custom label — `video-replica` — which is what
# ci.yml's select-runner job matches on. Deliberately NOT pinning arch here is
# what lets an arm64 macOS VM and an x64 Windows/WSL2 host both be selected by
# the same workflow.

set -euo pipefail

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_DIR="${RUNNER_DIR:-${HOME}/actions-runner}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
WORK_DIR="${WORK_DIR:-_work}"
EXTRA_LABELS="${RUNNER_LABELS:-video-replica}"

# --- Resolve target repo ---------------------------------------------------
REPO="${RUNNER_REPO:-}"
if [ -z "${REPO}" ]; then
  # Try to infer from the enclosing clone (works when run from inside the repo).
  origin_url="$(git config --get remote.origin.url 2>/dev/null || true)"
  case "${origin_url}" in
    git@github.com:*.git) REPO="$(printf '%s' "${origin_url}" | sed -E 's#git@github.com:(.*)\.git#\1#')" ;;
    https://github.com/*.git) REPO="$(printf '%s' "${origin_url}" | sed -E 's#https://github.com/(.*)\.git#\1#')" ;;
    https://github.com/*) REPO="$(printf '%s' "${origin_url}" | sed -E 's#https://github.com/(.*)#\1#')" ;;
  esac
fi
if [ -z "${REPO}" ]; then
  echo "ERROR: could not infer the repo. Set RUNNER_REPO=owner/name (or run from inside a clone)." >&2
  exit 2
fi
log "Target repo: ${REPO}"

# --- Detect arch -----------------------------------------------------------
case "$(uname -m)" in
  x86_64) ARCH=x64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "ERROR: unsupported arch $(uname -m)" >&2; exit 1 ;;
esac

# --- Resolve runner version (public actions/runner releases, no auth needed) --
VERSION="${RUNNER_VERSION:-}"
if [ -z "${VERSION}" ]; then
  log "Resolving latest actions/runner version"
  VERSION="$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest \
    | grep -m1 '"tag_name"' | sed -E 's/.*"v([^"]+)".*/\1/')"
fi
if [ -z "${VERSION}" ]; then
  echo "ERROR: could not resolve runner version. Set RUNNER_VERSION=x.y.z." >&2
  exit 1
fi
log "Runner version: v${VERSION} (${ARCH})"

# --- Read token (never echoed, never stored by this script) ----------------
TOKEN="${RUNNER_TOKEN:-}"
if [ -z "${TOKEN}" ]; then
  printf 'Paste the runner registration token (input hidden): ' >&2
  # read from the controlling terminal even when the script is piped.
  read -rs TOKEN </dev/tty || { echo "ERROR: no token provided" >&2; exit 2; }
  printf '\n' >&2
fi
if [ -z "${TOKEN}" ]; then
  echo "ERROR: empty token" >&2
  exit 2
fi

# --- Download + extract ----------------------------------------------------
mkdir -p "${RUNNER_DIR}"
cd "${RUNNER_DIR}"
TARBALL="actions-runner-linux-${ARCH}-${VERSION}.tar.gz"
URL="https://github.com/actions/runner/releases/download/v${VERSION}/${TARBALL}"
if [ ! -f "${RUNNER_DIR}/config.sh" ]; then
  log "Downloading runner package"
  curl -fsSL -o "${TARBALL}" "${URL}"
  log "Extracting into ${RUNNER_DIR}"
  tar xzf "${TARBALL}"
  rm -f "${TARBALL}"
else
  log "Runner files already present in ${RUNNER_DIR}; reconfiguring"
fi

# --- Configure -------------------------------------------------------------
# --unattended: no interactive prompts. --replace: re-register under the same
# name if it already exists. The token is consumed here and not persisted by us.
log "Configuring runner '${RUNNER_NAME}' with labels: ${EXTRA_LABELS}"
./config.sh \
  --url "https://github.com/${REPO}" \
  --token "${TOKEN}" \
  --name "${RUNNER_NAME}" \
  --labels "${EXTRA_LABELS}" \
  --work "${WORK_DIR}" \
  --unattended \
  --replace

# Drop the token from this shell's environment immediately.
unset TOKEN RUNNER_TOKEN

cat <<EOF

Runner configured in ${RUNNER_DIR}
  name:   ${RUNNER_NAME}
  repo:   ${REPO}
  labels: self-hosted, linux, ${ARCH}, ${EXTRA_LABELS}   (arch/OS added automatically)

Next: bash ${SCRIPT_DIR}/runner-service.sh start
      (or: cd ${RUNNER_DIR} && ./run.sh   for a foreground test run)
EOF
