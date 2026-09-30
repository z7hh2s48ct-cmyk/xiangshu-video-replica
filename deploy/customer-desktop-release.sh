#!/usr/bin/env bash
# Publish (or roll back) a signed desktop release into the static self-update
# channel served by nginx at <origin>/downloads/customer-cloud/.
#
# The release directory is produced by scripts/release/build-customer-signed-
# release.ps1 on the signing machine (installer .exe, .exe.sig, stable.json,
# SHA256SUMS.txt, release-manifest.json) and copied to this host by any file
# transport; git never carries binaries. This script only validates, places
# the version directory, and atomically swaps stable.json — the one file every
# installed client reads — so a publish either fully switches the channel or
# leaves the previous version serving.
#
# Rollback keeps it equally simple: every swap archives the previous manifest
# as stable.json.<version>.bak, and --rollback restores one of those.
#
# Required on the host: nginx location blocks for /downloads/customer-cloud/
# must already be enabled (deploy/nginx/customer.conf.example) with the alias
# pointing at $VIDEO_REPLICA_SITE_ROOT/downloads/customer-cloud.
set -Eeuo pipefail

usage() {
  cat >&2 <<EOF
usage:
  $0 --release-dir <path/to/dist-release/customer-cloud/<version>>  publish
  $0 --rollback <version>                                           roll back the live manifest

environment:
  VIDEO_REPLICA_SITE_ROOT  web root holding the SPA (default /www/wwwroot/video.zszhj.cn,
                           same default as customer-git-rollout.sh); the channel lives at
                           \$VIDEO_REPLICA_SITE_ROOT/downloads/customer-cloud
EOF
  exit 2
}

SITE="${VIDEO_REPLICA_SITE_ROOT:-/www/wwwroot/video.zszhj.cn}"
CHANNEL="$SITE/downloads/customer-cloud"
SCRIPT_MODE=""
RELEASE_DIR=""
ROLLBACK_VERSION=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-dir)
      RELEASE_DIR="${2:-}"
      SCRIPT_MODE="publish"
      shift 2
      ;;
    --rollback)
      ROLLBACK_VERSION="${2:-}"
      SCRIPT_MODE="rollback"
      shift 2
      ;;
    *) usage ;;
  esac
done
[[ -n "$SCRIPT_MODE" ]] || usage

STAMP="$(date -u +%Y%m%d-%H%M%S)"
LOG="$CHANNEL/.publish-$STAMP.log"
SWAPPED=0
umask 022

mkdir -p "$CHANNEL"
exec > >(tee -a "$LOG") 2>&1

# stable_field <file> <dotted.path> — print one field of a stable.json document.
stable_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)
for key in sys.argv[2].split("."):
    value = value[key]
print(value)
PY
}

live_version() {
  [[ -f "$CHANNEL/stable.json" ]] || { echo ""; return; }
  stable_field "$CHANNEL/stable.json" version
}

restore_previous_manifest() {
  local backup="$1"
  cp "$backup" "$CHANNEL/.stable.json.restore-$STAMP"
  mv -f "$CHANNEL/.stable.json.restore-$STAMP" "$CHANNEL/stable.json"
}

on_error() {
  local code="$1" line="$2" command="$3"
  trap - ERR
  printf 'PUBLISH_FAILED exit=%s line=%s command=%q\n' "$code" "$line" "$command"
  if [[ "$SWAPPED" == "1" && -n "$PREVIOUS_BACKUP" && -f "$PREVIOUS_BACKUP" ]]; then
    printf 'restoring previous stable.json from %s\n' "$PREVIOUS_BACKUP"
    restore_previous_manifest "$PREVIOUS_BACKUP"
  fi
  exit "$code"
}
trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR
PREVIOUS_BACKUP=""

# Verify the live manifest over HTTPS after a swap: what clients will actually
# read, not what lies on disk.
verify_channel() {
  local expected_version="$1" endpoint served_file
  endpoint="$(stable_field "$CHANNEL/stable.json" platforms.windows-x86_64.url)"
  endpoint="$(dirname "$(dirname "$endpoint")")/stable.json"
  served_file="$(mktemp)"
  curl -fsS --max-time 30 "$endpoint" -o "$served_file"
  local served_version
  served_version="$(stable_field "$served_file" version)"
  rm -f "$served_file"
  [[ "$served_version" == "$expected_version" ]] || {
    printf 'served manifest version %s != expected %s\n' "$served_version" "$expected_version"
    return 1
  }
  printf 'verified %s -> %s\n' "$endpoint" "$served_version"
}

if [[ "$SCRIPT_MODE" == "rollback" ]]; then
  [[ "$ROLLBACK_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
  BACKUP="$CHANNEL/stable.json.$ROLLBACK_VERSION.bak"
  [[ -f "$BACKUP" ]] || { printf 'no archived manifest for %s\n' "$ROLLBACK_VERSION" >&2; exit 1; }
  PREVIOUS_BACKUP="$CHANNEL/stable.json.$(live_version).bak"
  [[ -f "$PREVIOUS_BACKUP" ]] || PREVIOUS_BACKUP=""
  SWAPPED=1
  restore_previous_manifest "$BACKUP"
  verify_channel "$ROLLBACK_VERSION"
  printf 'rolled back to %s\n' "$ROLLBACK_VERSION"
  exit 0
fi

# --- publish -----------------------------------------------------------------

RELEASE_DIR="$(cd -- "$RELEASE_DIR" && pwd)"
VERSION="$(basename "$RELEASE_DIR")"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  printf 'release directory must be named <version>: %s\n' "$RELEASE_DIR" >&2
  exit 1
}

INSTALLER="$(stable_field "$RELEASE_DIR/stable.json" platforms.windows-x86_64.url)"
INSTALLER="$(basename "$INSTALLER")"
REQUIRED_FILES=(stable.json SHA256SUMS.txt release-manifest.json "$INSTALLER" "$INSTALLER.sig")
for name in "${REQUIRED_FILES[@]}"; do
  [[ -f "$RELEASE_DIR/$name" ]] || {
    printf 'missing %s in %s\n' "$name" "$RELEASE_DIR" >&2
    exit 1
  }
done

# The signed release already recorded both hashes; re-verify here so a
# corrupted upload can never become the self-update payload.
(cd "$RELEASE_DIR" && sha256sum -c SHA256SUMS.txt)

MANIFEST_VERSION="$(stable_field "$RELEASE_DIR/stable.json" version)"
[[ "$MANIFEST_VERSION" == "$VERSION" ]] || {
  printf 'stable.json version %s != directory %s\n' "$MANIFEST_VERSION" "$VERSION" >&2
  exit 1
}
MANIFEST_URL_PATH="/downloads/customer-cloud/$VERSION/$INSTALLER"
URL_PATH="$(stable_field "$RELEASE_DIR/stable.json" platforms.windows-x86_64.url)"
[[ "${URL_PATH#https://*}" == */downloads/customer-cloud/* ]] || {
  printf 'download url is not under /downloads/customer-cloud/: %s\n' "$URL_PATH" >&2
  exit 1
}
[[ "${URL_PATH#*://}" == "$MANIFEST_URL_PATH" ]] || {
  printf 'download url path mismatch: %s != %s\n' "${URL_PATH#*://}" "$MANIFEST_URL_PATH" >&2
  exit 1
}

if [[ "$(live_version)" == "$VERSION" ]]; then
  printf 'version %s is already live\n' "$VERSION"
  exit 0
fi

# Place the version directory via a staging rename so a partially uploaded
# tree is never reachable, then swap the manifest atomically.
STAGE="$CHANNEL/.staging-$STAMP"
mkdir -p "$STAGE/$VERSION"
for name in "${REQUIRED_FILES[@]}"; do
  cp "$RELEASE_DIR/$name" "$STAGE/$VERSION/$name"
done
chmod 755 "$STAGE" "$STAGE/$VERSION"
chmod 644 "$STAGE/$VERSION"/*
rm -rf "$CHANNEL/$VERSION"
mv "$STAGE/$VERSION" "$CHANNEL/$VERSION"
rmdir "$STAGE"

if [[ -f "$CHANNEL/stable.json" ]]; then
  PREVIOUS_VERSION="$(live_version)"
  PREVIOUS_BACKUP="$CHANNEL/stable.json.$PREVIOUS_VERSION.bak"
  cp "$CHANNEL/stable.json" "$PREVIOUS_BACKUP"
fi
cp "$RELEASE_DIR/stable.json" "$CHANNEL/.stable.json.next-$STAMP"
chmod 644 "$CHANNEL/.stable.json.next-$STAMP"
mv -f "$CHANNEL/.stable.json.next-$STAMP" "$CHANNEL/stable.json"
SWAPPED=1

verify_channel "$VERSION"
printf 'published %s (%s)\n' "$VERSION" "$INSTALLER"
printf 'rollback with: %s --rollback %s\n' "$0" "${PREVIOUS_VERSION:-<previous>}"
