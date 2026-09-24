#!/usr/bin/env bash
# Deploy one audited Git commit to the existing customer stack.
set -Eeuo pipefail

usage() {
  echo "usage: $0 --commit <40-character-git-sha>" >&2
  exit 2
}

RELEASE_SHA=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit)
      RELEASE_SHA="${2:-}"
      shift 2
      ;;
    *) usage ;;
  esac
done
[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]] || usage

ROOT="/opt/video-replica-candidate"
REPO_URL="${VIDEO_REPLICA_GIT_REPO_URL:-https://github.com/z7hh2s48ct-cmyk/xiangshu-video-replica}"
SOURCE="$ROOT/releases/$RELEASE_SHA"
SITE="/www/wwwroot/video.zszhj.cn"
# CW-019: the admin console is an independent build artifact (client/dist-admin,
# base /admin/). It is deployed beside the customer web root and must be replaced
# in the same run as $SITE, so the two artifacts never serve bytes from different
# release SHAs. Nginx serves it with
#   location ^~ /admin/ { alias <ADMIN_SITE>/; try_files $uri $uri/ /admin/index.html; }
# (deploy/nginx/customer.conf.example). That location block must already be in
# place before the first release built by this script is rolled out, otherwise
# the VERIFY step below fails on purpose and the run rolls back.
ADMIN_SITE="${SITE}-admin"
# CW-032: the compose topology is a registered in-repo artifact
# (deploy/customer/compose.yaml — the single default delivery package), not an
# unregistered /opt/video-replica-candidate host file. Legacy hosts may still
# redirect via CUSTOMER_COMPOSE=, but no longer need to.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Installed, reviewed topology is independent of the not-yet-fetched release.
COMPOSE="${CUSTOMER_COMPOSE:-$SCRIPT_DIR/customer/compose.yaml}"
COMPOSE_ENV="${CUSTOMER_COMPOSE_ENV:-/etc/video-replica/compose.env}"
IMAGE_OVERRIDE="$ROOT/app-image.override.json"
CUSTOMER_ENV="/etc/video-replica/customer.env"
SERVICE_USER="video-replica"
PUBLIC_ORIGIN="https://video.zszhj.cn"
NODE_BUILD_IMAGE="node:24-bookworm-slim"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
SHORT_SHA="${RELEASE_SHA:0:7}"
BACKUP="$ROOT/backups/git-$SHORT_SHA-$STAMP"
BUILD_CTX="$ROOT/build-git-$SHORT_SHA-$STAMP"
STAGE_SITE="$ROOT/site-git-$SHORT_SHA-$STAMP"
STAGE_ADMIN_SITE="$ROOT/admin-site-git-$SHORT_SHA-$STAMP"
LOG="$ROOT/deploy-git-$SHORT_SHA-$STAMP.log"
STATUS="$ROOT/deploy-git-$SHORT_SHA-$STAMP.status"
NEW_IMAGE="video-replica-rehearsal-app:$SHORT_SHA-git"
REQUIRED_SERVICES=(api-1 api-2 worker-1 worker-2 worker-3 worker-4)
OPTIONAL_SERVICES=(worker-viral worker-publish)
WORKER_SERVICES=(worker-1 worker-2 worker-3 worker-4)
SERVICES=("${REQUIRED_SERVICES[@]}")
ROLLBACK_SERVICES=("${REQUIRED_SERVICES[@]}")
ROLLOUT_STARTED=0
IMAGE_SWITCHED=0
umask 077

compose() {
  local files=(-f "$COMPOSE")
  if [[ -f "$IMAGE_OVERRIDE" ]]; then
    files+=(-f "$IMAGE_OVERRIDE")
  fi
  docker compose --env-file "$COMPOSE_ENV" "${files[@]}" "$@"
}

write_image_override() {
  python3 - "$@" <<'PY'
import json
import os
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
image = sys.argv[2]
services = sys.argv[3:]
if not services or any(not re.fullmatch(r"[a-z0-9-]+", s) for s in services):
    raise SystemExit("invalid application service inventory")
temporary = path.with_suffix(".tmp")
with temporary.open("w", encoding="utf-8") as stream:
    os.chmod(temporary, 0o600)
    json.dump({"services": {s: {"image": image} for s in services}}, stream)
    stream.write("\n")
temporary.replace(path)
PY
}

mkdir -p "$ROOT"
exec > >(tee -a "$LOG") 2>&1

mark() {
  printf '%s\n' "$1" > "$STATUS"
}

wait_ready() {
  local service="$1"
  for _ in $(seq 1 90); do
    local cid state
    cid=$(compose ps -q "$service")
    if [[ -n "$cid" ]]; then
      state=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid")
      if [[ "$state" == "healthy" || "$state" == "running" ]]; then
        return 0
      fi
    fi
    sleep 2
  done
  return 1
}

rollback() {
  local code="$1"
  local failed_line="$2"
  local failed_command="$3"
  trap - ERR
  printf 'DEPLOY_FAILED exit=%s line=%s command=%q\n' "$code" "$failed_line" "$failed_command"
  mark ROLLING_BACK
  # Stop only newly introduced optional roles; restore those previously running.
  if [[ "$ROLLOUT_STARTED" == "1" ]]; then
    for service in "${OPTIONAL_SERVICES[@]}"; do
      if printf '%s\n' "${SERVICES[@]}" | grep -Fxq "$service" &&
         ! printf '%s\n' "${ROLLBACK_SERVICES[@]}" | grep -Fxq "$service"; then
        compose stop "$service" || true
      fi
    done
  fi
  if [[ "$IMAGE_SWITCHED" == "1" ]]; then
    if [[ -f "$BACKUP/image-before.json" ]]; then
      cp -a "$BACKUP/image-before.json" "$IMAGE_OVERRIDE"
    else
      rm -f -- "$IMAGE_OVERRIDE"
    fi
  fi
  if [[ -f "$BACKUP/site-before.tar.gz" ]]; then
    find "$SITE" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    tar -xzf "$BACKUP/site-before.tar.gz" -C "$SITE"
  fi
  # CW-019: restore the admin artifact only when this run actually backed one up.
  # The first release that introduces dist-admin has no prior admin site, so a
  # missing archive means "nothing to roll back to", not "skip silently".
  if [[ -f "$BACKUP/admin-site-before.tar.gz" ]]; then
    find "$ADMIN_SITE" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    tar -xzf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE"
  fi
  if [[ "$ROLLOUT_STARTED" == "1" ]]; then
    if ! compose up -d --no-deps "${ROLLBACK_SERVICES[@]}"; then
      mark FAILED_ROLLBACK_INCOMPLETE
      exit "$code"
    fi
    for service in "${ROLLBACK_SERVICES[@]}"; do
      if ! wait_ready "$service"; then
        mark FAILED_ROLLBACK_INCOMPLETE
        exit "$code"
      fi
    done
  fi
  mark FAILED_ROLLED_BACK
  printf 'ROLLBACK_IMAGE=%s\nDATABASE_BACKUP=%s\nDATABASE_HEAD_LEFT_FORWARD_COMPATIBLE=%s\n' \
    "${OLD_IMAGE:-unknown}" "$BACKUP/database-before.dump" "${IMAGE_DB_HEAD:-unknown}"
  exit "$code"
}
trap 'rollback "$?" "$LINENO" "$BASH_COMMAND"' ERR

require_command() {
  command -v "$1" >/dev/null || {
    echo "PRECHECK_FAILED: missing required command: $1" >&2
    exit 1
  }
}

dependency_manifest_hash() {
  python3 -c '
import hashlib
import json
import sys
import tomllib

path = sys.argv[1]
format_path = sys.argv[2]
stream = sys.stdin.buffer if path == "-" else open(path, "rb")
with stream:
    document = tomllib.load(stream)
if format_path.endswith("pyproject.toml"):
    project = document["project"]
    manifest = {
        "requires-python": project["requires-python"],
        "dependencies": project.get("dependencies", []),
        "optional-dependencies": project.get("optional-dependencies", {}),
    }
else:
    manifest = {key: value for key, value in document.items() if key != "package"}
    manifest["package"] = [
        package for package in document.get("package", [])
        if package["name"] != "video-replica-api"
    ]
print(hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
' "$1" "$2"
}

cd "$ROOT"
mark PREFLIGHT
for command in git docker curl python3 flock; do
  require_command "$command"
done
exec 9>"$ROOT/deploy.lock"
flock -n 9 || { echo "PRECHECK_FAILED: another rollout is running" >&2; exit 1; }
[[ -f "$COMPOSE" && -r "$COMPOSE_ENV" && -d "$SITE" && -r "$CUSTOMER_ENV" ]]
[[ ! -e "$BUILD_CTX" && ! -e "$STAGE_SITE" && ! -e "$STAGE_ADMIN_SITE" && ! -e "$BACKUP" ]]
[[ "$(df -Pk "$ROOT" | awk 'NR == 2 {print $4}')" -gt 4194304 ]]
compose config --quiet
mapfile -t CONFIGURED_SERVICES < <(compose config --services)
for service in "${REQUIRED_SERVICES[@]}"; do
  printf '%s\n' "${CONFIGURED_SERVICES[@]}" | grep -Fxq "$service" || {
    echo "PRECHECK_FAILED: compose is missing required service: $service" >&2
    exit 1
  }
done
for service in "${OPTIONAL_SERVICES[@]}"; do
  if printf '%s\n' "${CONFIGURED_SERVICES[@]}" | grep -Fxq "$service"; then
    SERVICES+=("$service")
    WORKER_SERVICES+=("$service")
  fi
done
curl -fsS --max-time 20 "$PUBLIC_ORIGIN/health?preflight=$STAMP" >/dev/null
id "$SERVICE_USER" >/dev/null
docker image inspect "$NODE_BUILD_IMAGE" >/dev/null 2>&1 || docker pull "$NODE_BUILD_IMAGE"
docker run --rm --entrypoint node "$NODE_BUILD_IMAGE" -e \
  'process.exit(Number(process.versions.node.split(".")[0]) >= 24 ? 0 : 1)'

if [[ ! -d "$SOURCE/.git" ]]; then
  mkdir -p "$SOURCE"
  git -C "$SOURCE" init --quiet
  git -C "$SOURCE" remote add origin "$REPO_URL"
fi
[[ "$(git -C "$SOURCE" remote get-url origin)" == "$REPO_URL" ]]
git -C "$SOURCE" fetch --depth=1 origin "$RELEASE_SHA"
git -C "$SOURCE" checkout --detach --force FETCH_HEAD
[[ "$(git -C "$SOURCE" rev-parse HEAD)" == "$RELEASE_SHA" ]]
git -C "$SOURCE" diff --quiet
[[ -z "$(git -C "$SOURCE" status --porcelain)" ]]
RELEASE_TREE=$(git -C "$SOURCE" rev-parse 'HEAD^{tree}')

python3 "$SOURCE/scripts/customer_release_preflight.py" \
  --env-file "$CUSTOMER_ENV" \
  --service-user "$SERVICE_USER"
docker run --rm -v "$SOURCE:/workspace" -w /workspace \
  -e "VITE_API_BASE_URL=$PUBLIC_ORIGIN" \
  -e "VITE_CLOUD_ADMIN_ORIGIN=$PUBLIC_ORIGIN" "$NODE_BUILD_IMAGE" sh -lc \
  'npm ci --ignore-scripts && npm run build:all && npm run verify:customer-bundle'
[[ -s "$SOURCE/client/dist/index.html" && -d "$SOURCE/client/dist/assets" ]]
EXPECTED_ASSET=$(grep -oE 'assets/[^" ]+\.js' "$SOURCE/client/dist/index.html" | head -n 1)
[[ -n "$EXPECTED_ASSET" ]]
# CW-019: build:all also produces the admin artifact, and verify:customer-bundle
# (inside the same container) fails the run if any admin or internal entry leaked
# into client/dist. This is the only place the deployed bytes are produced, so the
# CW-019 exclusion evidence belongs to the release chain here, not just to CI.
[[ -s "$SOURCE/client/dist-admin/index.html" && -d "$SOURCE/client/dist-admin/assets" ]]
EXPECTED_ADMIN_ASSET=$(grep -oE 'assets/[^" ]+\.js' "$SOURCE/client/dist-admin/index.html" | head -n 1)
[[ -n "$EXPECTED_ADMIN_ASSET" ]]

API_CONTAINER=$(compose ps -q api-1)
DB_CONTAINER=$(compose ps -q db)
[[ -n "$API_CONTAINER" && -n "$DB_CONTAINER" ]]
OLD_IMAGE=$(docker inspect -f '{{.Config.Image}}' "$API_CONTAINER")
for service in "${OPTIONAL_SERVICES[@]}"; do
  if printf '%s\n' "${SERVICES[@]}" | grep -Fxq "$service"; then
    cid=$(compose ps -q "$service")
    if [[ -n "$cid" ]]; then
      [[ "$(docker inspect -f '{{.Config.Image}}' "$cid")" == "$OLD_IMAGE" ]]
      ROLLBACK_SERVICES+=("$service")
    fi
  fi
done
OLD_IMAGE_USER=$(docker image inspect -f '{{.Config.User}}' "$OLD_IMAGE")
OLD_IMAGE_DB_HEAD=$(docker image inspect -f '{{index .Config.Labels "video-replica.database-head"}}' "$OLD_IMAGE")
[[ -n "$OLD_IMAGE_DB_HEAD" ]]
[[ -z "$OLD_IMAGE_USER" || "$OLD_IMAGE_USER" =~ ^[A-Za-z0-9_.:-]+$ ]]
BUILD_BASE_IMAGE="${VIDEO_REPLICA_BUILD_BASE_IMAGE:-$OLD_IMAGE}"
docker image inspect "$BUILD_BASE_IMAGE" >/dev/null
BUILD_BASE_IMAGE_USER=$(docker image inspect -f '{{.Config.User}}' "$BUILD_BASE_IMAGE")
[[ -z "$BUILD_BASE_IMAGE_USER" || "$BUILD_BASE_IMAGE_USER" =~ ^[A-Za-z0-9_.:-]+$ ]]
for dependency_file in server/pyproject.toml server/uv.lock; do
  source_hash=$(dependency_manifest_hash "$SOURCE/$dependency_file" "$dependency_file")
  image_hash=$(docker run --rm --entrypoint sh "$BUILD_BASE_IMAGE" -c 'cat "$1"' sh "/opt/video-replica/$dependency_file" | dependency_manifest_hash - "$dependency_file")
  [[ "$source_hash" == "$image_hash" ]] || {
    echo "PRECHECK_FAILED: Python dependency change requires a base-image release: $dependency_file" >&2
    exit 1
  }
done
HEADS_OUTPUT=$(docker run --rm -v "$SOURCE/server:/source:ro" --entrypoint sh "$OLD_IMAGE" -lc \
  'cd /source && alembic heads')
HEAD_COUNT=$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {count++} END {print count + 0}')
EXPECTED_DB_HEAD=$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {print $1}' | tail -n 1)
[[ "$HEAD_COUNT" == "1" && -n "$EXPECTED_DB_HEAD" ]]

mark BACKUP
mkdir -p "$BACKUP"
cp -a "$COMPOSE" "$BACKUP/compose-before.yaml"
if [[ -f "$IMAGE_OVERRIDE" ]]; then
  cp -a "$IMAGE_OVERRIDE" "$BACKUP/image-before.json"
fi
# CW-032: pin the exact topology artifact this release rolled out with.
sha256sum "$COMPOSE" > "$BACKUP/compose.sha256"
printf '%s\n' "$OLD_IMAGE" > "$BACKUP/previous-image.txt"
printf '%s\n' "$RELEASE_SHA" > "$BACKUP/release-sha.txt"
printf '%s\n' "$RELEASE_TREE" > "$BACKUP/release-tree.txt"
compose ps > "$BACKUP/compose-before.txt"
tar -czf "$BACKUP/site-before.tar.gz" -C "$SITE" .
# CW-019: archive the admin artifact as well. Absent on the first release that
# introduces dist-admin; rollback() keys off this file's existence.
if [[ -d "$ADMIN_SITE" ]]; then
  tar -czf "$BACKUP/admin-site-before.tar.gz" -C "$ADMIN_SITE" .
fi
CURRENT_HEAD_BEFORE=$(compose exec -T db sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version"')
if [[ "$CURRENT_HEAD_BEFORE" != "$OLD_IMAGE_DB_HEAD" && "$CURRENT_HEAD_BEFORE" != "$EXPECTED_DB_HEAD" ]]; then
  echo "PRECHECK_FAILED: database revision is neither the active image head nor the target release head" >&2
  exit 1
fi
printf '%s\n' "$CURRENT_HEAD_BEFORE" > "$BACKUP/database-revision-before.txt"
compose exec -T db sh -lc \
  'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP/database-before.dump"
[[ -s "$BACKUP/database-before.dump" ]]
docker exec -i "$DB_CONTAINER" pg_restore --list < "$BACKUP/database-before.dump" >/dev/null
sha256sum "$BACKUP/site-before.tar.gz" "$BACKUP/database-before.dump" > "$BACKUP/BACKUP-SHA256SUMS"
if [[ -s "$BACKUP/admin-site-before.tar.gz" ]]; then
  sha256sum "$BACKUP/admin-site-before.tar.gz" >> "$BACKUP/BACKUP-SHA256SUMS"
fi

mark BUILD
mkdir -p "$BUILD_CTX/server" "$BUILD_CTX/scripts"
cp -a "$SOURCE/server/app" "$BUILD_CTX/server/app"
cp -a "$SOURCE/server/migrations" "$BUILD_CTX/server/migrations"
cp -a "$SOURCE/server/alembic.ini" "$BUILD_CTX/server/alembic.ini"
cp -a "$SOURCE/scripts/customer_release_preflight.py" "$BUILD_CTX/scripts/customer_release_preflight.py"
# CW-060 / PG-09: historical SQLite operator tooling never ships to customers.
# server/app/backup.py belongs to the isolated operator package and the
# legacy import/reconcile CLIs live only under server/scripts/ (never copied
# above); fail closed if either ever leaks into the customer build context.
rm -f "$BUILD_CTX/server/app/backup.py"
for forbidden_operator_path in server/app/backup.py server/scripts; do
  [[ ! -e "$BUILD_CTX/$forbidden_operator_path" ]] || {
    echo "PRECHECK_FAILED: historical SQLite tooling in the customer build context: $forbidden_operator_path" >&2
    exit 1
  }
done
{
  printf 'FROM %s\n' "$BUILD_BASE_IMAGE"
  cat <<'DOCKERFILE'
USER root
RUN rm -rf /opt/video-replica/server/app /opt/video-replica/server/migrations /opt/video-replica/server/scripts
COPY server/app /opt/video-replica/server/app
COPY server/migrations /opt/video-replica/server/migrations
COPY server/alembic.ini /opt/video-replica/server/alembic.ini
COPY scripts/customer_release_preflight.py /opt/video-replica/scripts/customer_release_preflight.py
RUN command -v ffmpeg \
    && command -v ffprobe \
    && command -v node \
    && chmod 0755 /opt/video-replica/scripts/customer_release_preflight.py \
    && python -m compileall -q /opt/video-replica/server/app /opt/video-replica/server/migrations \
    && cd /opt/video-replica/server \
    && python -c "import app.main, app.admin_customer_routes, app.customer_fence, app.generation_worker, app.publish_worker" \
    && ! test -e /opt/video-replica/server/app/backup.py \
    && ! test -e /opt/video-replica/server/scripts/sqlite_to_postgres.py \
    && ! test -e /opt/video-replica/server/scripts/reconcile_customer_billing.py \
    && python -c "import pathlib, sys; forbidden = {'backup.py', 'sqlite_to_postgres.py', 'reconcile_customer_billing.py'}; found = [str(p) for p in pathlib.Path('/opt/video-replica/server').rglob('*') if p.is_file() and p.name in forbidden]; sys.exit('historical SQLite tooling in the customer image: ' + repr(found) if found else 0)"
DOCKERFILE
  if [[ -n "$BUILD_BASE_IMAGE_USER" ]]; then
    printf 'USER %s\n' "$BUILD_BASE_IMAGE_USER"
  fi
} > "$BUILD_CTX/Dockerfile"

docker build \
  --label "org.opencontainers.image.revision=$RELEASE_SHA" \
  --label "org.opencontainers.image.source-tree=$RELEASE_TREE" \
  --label "video-replica.database-head=$EXPECTED_DB_HEAD" \
  -t "$NEW_IMAGE" \
  "$BUILD_CTX"
HEADS_OUTPUT=$(docker run --rm --entrypoint sh "$NEW_IMAGE" -lc 'cd /opt/video-replica/server && alembic heads')
HEAD_COUNT=$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {count++} END {print count + 0}')
IMAGE_DB_HEAD=$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {print $1}' | tail -n 1)
[[ "$HEAD_COUNT" == "1" && "$IMAGE_DB_HEAD" == "$EXPECTED_DB_HEAD" ]]

mark MIGRATE
MIGRATION_SERVICE=api-1
IMAGE_SERVICES=("${SERVICES[@]}")
if printf '%s\n' "${CONFIGURED_SERVICES[@]}" | grep -Fxq migrate; then
  IMAGE_SERVICES+=(migrate)
  MIGRATION_SERVICE=migrate
fi
write_image_override "$IMAGE_OVERRIDE" "$NEW_IMAGE" "${IMAGE_SERVICES[@]}"
IMAGE_SWITCHED=1
compose config --quiet
# Same fail-fast lock policy as deploy/postgres/migrate.sh: a DDL wait queued
# behind peak traffic blocks wallet requests behind itself. Re-run at low peak
# if this aborts on lock contention. The timeout variable expands inside the
# container, so set it in the compose env file, not the host shell.
compose run --rm --no-deps "$MIGRATION_SERVICE" \
  sh -lc 'cd /opt/video-replica/server && PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c lock_timeout=${VIDEO_REPLICA_MIGRATION_LOCK_TIMEOUT:-5s}" alembic upgrade head'

mark ROLL_API
ROLLOUT_STARTED=1
for service in api-1 api-2; do
  mark "ROLLING_$service"
  compose up -d --no-deps "$service"
  wait_ready "$service"
done

mark ROLL_WORKERS
for service in "${WORKER_SERVICES[@]}"; do
  mark "ROLLING_$service"
  compose up -d --no-deps "$service"
  wait_ready "$service"
done

CURRENT_DB_HEAD=$(compose exec -T api-1 \
  sh -lc 'cd /opt/video-replica/server && alembic current' \
  | awk '/\(head\)/ {print $1}' | tail -n 1)
[[ "$CURRENT_DB_HEAD" == "$IMAGE_DB_HEAD" ]]

mark SITE
mkdir -p "$STAGE_SITE" "$STAGE_ADMIN_SITE"
cp -a "$SOURCE/client/dist"/. "$STAGE_SITE"/
grep -q "$EXPECTED_ASSET" "$STAGE_SITE/index.html"
cp -a "$SOURCE/client/dist-admin"/. "$STAGE_ADMIN_SITE"/
grep -q "$EXPECTED_ADMIN_ASSET" "$STAGE_ADMIN_SITE/index.html"
find "$SITE" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$STAGE_SITE"/. "$SITE"/
# CW-019: replace the admin artifact immediately after the customer one, both from
# staging dirs built out of the same $RELEASE_SHA. Nginx serves /admin/ from
# $ADMIN_SITE, so leaving it on the previous SHA would mix two releases.
mkdir -p "$ADMIN_SITE"
find "$ADMIN_SITE" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
cp -a "$STAGE_ADMIN_SITE"/. "$ADMIN_SITE"/

mark VERIFY
for service in "${SERVICES[@]}"; do
  cid=$(compose ps -q "$service")
  [[ -n "$cid" ]]
  [[ "$(docker inspect -f '{{.Config.Image}}' "$cid")" == "$NEW_IMAGE" ]]
  [[ "$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$cid")" == "$RELEASE_SHA" ]]
done
grep -q "$EXPECTED_ASSET" "$SITE/index.html"
grep -q "$EXPECTED_ADMIN_ASSET" "$ADMIN_SITE/index.html"
curl -fsS --max-time 20 "$PUBLIC_ORIGIN/health?release=$SHORT_SHA" >/dev/null
CUSTOMER_HTML=$(curl -fsS --max-time 20 "$PUBLIC_ORIGIN/customer?release=$SHORT_SHA")
grep -q "$EXPECTED_ASSET" <<< "$CUSTOMER_HTML"
# CW-019: prove /admin is really served from the new admin artifact. Before this
# task the customer bundle itself rendered AdminApp at /admin; now it cannot, so a
# missing nginx `location ^~ /admin/` block or a missing dist-admin would otherwise
# degrade to the customer activation screen (or a 500 redirect cycle) while this
# script still reported SUCCESS.
ADMIN_HTML=$(curl -fsS --max-time 20 "$PUBLIC_ORIGIN/admin/?release=$SHORT_SHA")
grep -q "$EXPECTED_ADMIN_ASSET" <<< "$ADMIN_HTML"
compose exec -T api-1 sh -lc \
  "cd /opt/video-replica/server && python -c 'from app.main import app; assert \"/api/control/customer-sessions/live\" in app.openapi()[\"paths\"]'"

trap - ERR
mark SUCCESS
printf 'DEPLOYED=%s\nRELEASE_SHA=%s\nRELEASE_TREE=%s\nPREVIOUS_IMAGE=%s\nNEW_IMAGE=%s\nDATABASE_HEAD_BEFORE=%s\nDATABASE_HEAD_AFTER=%s\nCLIENT_ASSET=%s\nADMIN_SITE=%s\nADMIN_ASSET=%s\nDATABASE_BACKUP=%s\n' \
  "$STAMP" "$RELEASE_SHA" "$RELEASE_TREE" "$OLD_IMAGE" "$NEW_IMAGE" \
  "$CURRENT_HEAD_BEFORE" "$CURRENT_DB_HEAD" "$EXPECTED_ASSET" \
  "$ADMIN_SITE" "$EXPECTED_ADMIN_ASSET" "$BACKUP/database-before.dump"
