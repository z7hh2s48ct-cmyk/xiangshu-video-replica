#!/usr/bin/env bash
# CW-032 — build the FIRST customer app image on a blank host.
#
# deploy/customer-git-rollout.sh builds every subsequent release FROM the
# previous image (OLD_IMAGE); this script produces that first image from a
# clean base using only registered in-repo artifacts, so a blank isolated
# directory can rebuild API/Worker/Admin without any unregistered host file
# or pre-existing image.
#
# Usage:  bootstrap-base-image.sh <git-short-sha>
# Result: video-replica-rehearsal-app:<sha>  (then point APP_IMAGE at it in
#         /etc/video-replica/compose.env and `docker compose up -d`; see README.md)
#
# The in-image checks mirror the rollout's Dockerfile contract exactly:
# ffmpeg/ffprobe present, locked dependencies, python compileall, the four
# runtime imports, and the CW-060 historical-tooling physical scan.
set -euo pipefail

SHORT_SHA="${1:?usage: bootstrap-base-image.sh <git-short-sha>}"
[[ "$SHORT_SHA" =~ ^[A-Za-z0-9_.-]{1,64}$ ]] || {
    echo "invalid image tag fragment: $SHORT_SHA" >&2
    exit 1
}
SOURCE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
RELEASE_SHA=$(git -C "$SOURCE" rev-parse HEAD)
RELEASE_TREE=$(git -C "$SOURCE" rev-parse 'HEAD^{tree}')
git -C "$SOURCE" diff --quiet HEAD -- server
[[ -z "$(git -C "$SOURCE" ls-files --others --exclude-standard -- server)" ]]
# Read the frozen migration manifest without needing host Python packages.
EXPECTED_DB_HEAD=$(python3 - "$SOURCE/server/migrations/manifest.json" <<'PY'
import json
import sys
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
heads = manifest["graph"]["heads"]
if len(heads) != 1:
    raise SystemExit("expected exactly one migration head")
print(heads[0])
PY
)
BUILD_CTX="$(mktemp -d)"
trap 'rm -rf -- "$BUILD_CTX"' EXIT

mkdir -p "$BUILD_CTX/server"
cp -a "$SOURCE/server/app" "$BUILD_CTX/server/app"
cp -a "$SOURCE/server/migrations" "$BUILD_CTX/server/migrations"
cp -a "$SOURCE/server/alembic.ini" "$BUILD_CTX/server/alembic.ini"
cp -a "$SOURCE/server/pyproject.toml" "$SOURCE/server/uv.lock" "$BUILD_CTX/server/"
# CW-060 / PG-09: historical SQLite operator tooling never enters the
# customer image (defense in depth — the rollout applies the same scan).
rm -f "$BUILD_CTX/server/app/backup.py"

cat > "$BUILD_CTX/Dockerfile" <<'DOCKERFILE'
FROM python:3.12-slim
ARG EXPECTED_DB_HEAD
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv
COPY server /opt/video-replica/server
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/video-replica/browsers
RUN cd /opt/video-replica/server \
    && uv sync --locked --no-dev \
    && .venv/bin/python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/video-replica/browsers \
    && test "$(.venv/bin/python -c 'from alembic.config import Config; from alembic.script import ScriptDirectory; print(ScriptDirectory.from_config(Config("alembic.ini")).get_current_head())')" = "$EXPECTED_DB_HEAD" \
    && .venv/bin/python -m compileall -q app migrations \
    && command -v ffmpeg && command -v ffprobe && command -v node \
    && .venv/bin/python -c "import app.main, app.admin_customer_routes, app.customer_fence, app.generation_worker, app.publish_worker" \
    && ! test -e /opt/video-replica/server/app/backup.py \
    && ! test -e /opt/video-replica/server/scripts/sqlite_to_postgres.py \
    && ! test -e /opt/video-replica/server/scripts/reconcile_customer_billing.py \
    && .venv/bin/python -c "import pathlib, sys; forbidden = {'backup.py', 'sqlite_to_postgres.py', 'reconcile_customer_billing.py'}; found = [str(p) for p in pathlib.Path('/opt/video-replica/server').rglob('*') if p.is_file() and p.name in forbidden]; sys.exit('historical SQLite tooling in the customer image: ' + repr(found) if found else 0)"
ENV PATH="/opt/video-replica/server/.venv/bin:$PATH"
# Rollout and Compose migration commands use `sh -lc`; Debian /etc/profile
# resets PATH, so restore the same virtualenv for login shells as well.
RUN printf '%s\n' 'export PATH="/opt/video-replica/server/.venv/bin:$PATH"' \
    > /etc/profile.d/video-replica-venv.sh
WORKDIR /opt/video-replica/server
DOCKERFILE

docker build \
    --build-arg "EXPECTED_DB_HEAD=$EXPECTED_DB_HEAD" \
    --label "org.opencontainers.image.revision=$RELEASE_SHA" \
    --label "org.opencontainers.image.source-tree=$RELEASE_TREE" \
    --label "video-replica.database-head=$EXPECTED_DB_HEAD" \
    -t "video-replica-rehearsal-app:$SHORT_SHA" \
    "$BUILD_CTX"
printf 'base image ready: video-replica-rehearsal-app:%s\n' "$SHORT_SHA"
printf 'next: set APP_IMAGE in /etc/video-replica/compose.env, then docker compose up -d (see README.md)\n'
