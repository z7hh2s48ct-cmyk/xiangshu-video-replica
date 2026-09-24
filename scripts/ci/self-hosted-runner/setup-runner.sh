#!/usr/bin/env bash
# Provision a self-hosted GitHub Actions runner host for the Linux quality gate.
#
# OS-agnostic across the two supported hosts (both are Ubuntu 24.04 Linux):
#   - macOS: an OrbStack / Lima ubuntu:24.04 VM (arm64)   -> provision-vm.md
#   - Windows: WSL2 Ubuntu 24.04 (x64)                    -> provision-windows-wsl2.md
#
# Installs the SAME toolchain the ci.yml `quality-linux` job needs, plus Docker
# (the sharded pytest + customer E2E manage their own PostgreSQL containers), so
# a job dispatched here runs warm instead of cold-installing for 6-8 minutes.
#
# This script installs tooling ONLY. It never touches secrets or runner tokens —
# registration is a separate, interactive step (register-runner.sh).
#
# Idempotent: safe to re-run after an interruption or to top up a tool.

set -euo pipefail

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

NODE_MAJOR=24
PYTHON_VERSION=3.12
UV_VERSION=0.12.0

if [ "$(uname -s)" != "Linux" ]; then
  echo "ERROR: run this INSIDE the Linux host (OrbStack/Lima VM or WSL2), not on the macOS/Windows host." >&2
  exit 1
fi

# --- System packages -------------------------------------------------------
# Mirrors the ci.yml apt step (Tauri/webkit + ffmpeg + build tools) and adds the
# GitHub Actions runner's own runtime prerequisites (libicu, dotnet deps) and
# Docker CLI. `sudo` is used so the script works for a non-root dev user.
log "Installing system packages (apt)"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -o Acquire::Retries=3
sudo apt-get install -y -o Acquire::Retries=3 \
  build-essential curl wget git ca-certificates gnupg lsb-release \
  pkg-config python3 python3-venv python3-dev \
  ffmpeg \
  libayatana-appindicator3-dev librsvg2-dev libssl-dev \
  libwebkit2gtk-4.1-dev libxdo-dev \
  libicu-dev zlib1g-dev

# --- Docker ----------------------------------------------------------------
# Required by scripts/pg-fixture.sh (shard + E2E PostgreSQL containers). On WSL2
# with Docker Desktop, the CLI is provided by Desktop's WSL integration; this
# block is skipped when `docker` already works.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  log "Docker already available: $(docker --version)"
else
  log "Installing Docker Engine (get.docker.com)"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$(id -un)" || true
  echo "NOTE: log out/in (or run 'newgrp docker') so your user can reach the Docker socket."
fi

# --- Node.js 24 (NodeSource) ----------------------------------------------
if command -v node >/dev/null 2>&1 && [ "$(node -p 'process.versions.node.split(".")[0]')" = "${NODE_MAJOR}" ]; then
  log "Node.js ${NODE_MAJOR} already installed: $(node --version)"
else
  log "Installing Node.js ${NODE_MAJOR} (NodeSource)"
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
    | sudo gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg --yes
  echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
    | sudo tee /etc/apt/sources.list.d/nodesource.list >/dev/null
  sudo apt-get update -o Acquire::Retries=3
  sudo apt-get install -y nodejs
fi

# --- uv (pinned) -----------------------------------------------------------
if command -v uv >/dev/null 2>&1 && [ "$(uv --version | awk '{print $2}')" = "${UV_VERSION}" ]; then
  log "uv ${UV_VERSION} already installed"
else
  log "Installing uv ${UV_VERSION}"
  curl -LsSf "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-installer.sh" \
    | env INSTALLER_NO_MODIFY_PATH=1 sh
  # Ensure ~/.local/bin (uv's default) is on PATH for this and future shells.
  export PATH="${HOME}/.local/bin:${PATH}"
  grep -qs 'HOME/.local/bin' "${HOME}/.profile" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "${HOME}/.profile"
fi

# --- Rust (stable + rustfmt) ----------------------------------------------
if command -v rustup >/dev/null 2>&1; then
  log "Rust toolchain already present: $(rustc --version 2>/dev/null || echo unknown)"
  rustup toolchain install stable --profile minimal --component rustfmt
  rustup default stable
else
  log "Installing Rust (rustup, stable + rustfmt)"
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain stable --component rustfmt
  # shellcheck disable=SC1091
  . "${HOME}/.cargo/env"
  grep -qs '.cargo/env' "${HOME}/.profile" || echo '. "$HOME/.cargo/env"' >> "${HOME}/.profile"
fi

# --- Python 3.12 -----------------------------------------------------------
# Ubuntu 24.04 ships Python 3.12 as `python3`; just confirm and report.
log "Python: $(python3 --version)  (ci.yml pins ${PYTHON_VERSION}; Ubuntu 24.04 default matches)"

# --- Summary ---------------------------------------------------------------
log "Toolchain ready"
printf '  node    %s\n' "$(node --version 2>/dev/null || echo MISSING)"
printf '  npm     %s\n' "$(npm --version 2>/dev/null || echo MISSING)"
printf '  python3 %s\n' "$(python3 --version 2>/dev/null || echo MISSING)"
printf '  uv      %s\n' "$(uv --version 2>/dev/null || echo MISSING)"
printf '  rustc   %s\n' "$(rustc --version 2>/dev/null || echo MISSING)"
printf '  cargo   %s\n' "$(cargo --version 2>/dev/null || echo MISSING)"
printf '  ffmpeg  %s\n' "$(ffmpeg -version 2>/dev/null | head -1 || echo MISSING)"
printf '  docker  %s\n' "$(docker --version 2>/dev/null || echo MISSING)"

cat <<'EOF'

Next steps:
  1. register-runner.sh   (interactive: paste a runner registration token)
  2. runner-service.sh start
  3. Open a PR — select-runner dispatches the Linux gate here while it is online.

Cache warming happens on the first dispatched job (npm ci / uv sync / cargo /
playwright). Subsequent jobs reuse the persistent local caches and start warm.
EOF
