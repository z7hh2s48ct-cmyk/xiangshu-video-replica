#!/usr/bin/env bash
# Parallel pytest sharding for the Linux quality gate.
#
# Each shard runs against its OWN isolated PostgreSQL container (distinct
# container name + host port, same database name), so the ~30 PG-touching test
# files never collide on database names or contend on the shared-suite file
# lock (server/tests/pg_test_kit.py). Physical isolation == zero cross-shard
# interference.
#
# Usage:
#   bash scripts/ci/run-pytest-shards.sh [N]
#   CI_PYTEST_SHARDS=8 bash scripts/ci/run-pytest-shards.sh
#
# N defaults to 4 (overridable via arg or CI_PYTEST_SHARDS). When Docker is
# unavailable, or N == 1, the script falls back to a single sequential pytest
# pass using the ambient TEST_POSTGRESQL_URL — exactly the historical
# behaviour of `npm run check`, so the gate never loses coverage.
#
# Portable: bash 3.2 (macOS) safe — no mapfile/readarray, no associative arrays.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PG_FIXTURE="${REPO_ROOT}/scripts/pg-fixture.sh"
cd "${REPO_ROOT}"

SHARDS="${1:-${CI_PYTEST_SHARDS:-4}}"
COMMITTED_SHARD_DIR="${SCRIPT_DIR}/test-shards"
LOG_DIR="${CI_SHARD_LOG_DIR:-/tmp}"
BASE_PORT="${CI_SHARD_BASE_PORT:-5433}"
PG_USER="${PG_FIXTURE_USER:-testuser}"
PG_PASSWORD="${PG_FIXTURE_PASSWORD:-testpass}"
PG_DB="${PG_FIXTURE_DB:-customer_v3_test}"
DEFAULT_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${BASE_PORT}/${PG_DB}"

# pytest command mirroring the tail of `npm run check`.
UV_PYTEST=(uv --cache-dir .uv-cache run --project server --locked python -m pytest --rootdir server)

log() { printf '%s\n' "$*"; }

if ! [[ "${SHARDS}" =~ ^[1-9][0-9]*$ ]]; then
  log "ERROR: shard count must be a positive integer, got '${SHARDS}'" >&2
  exit 2
fi

docker_available() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# Sequential fallback: one pytest pass over the whole suite, ambient PG DSN.
# ---------------------------------------------------------------------------
run_sequential() {
  local reason="$1"
  log "==> Docker/sharding unavailable (${reason}); running the full suite sequentially."
  log "==> TEST_POSTGRESQL_URL=${TEST_POSTGRESQL_URL:-<unset, pytest uses pg_test_kit default>}"
  TEST_POSTGRESQL_URL="${TEST_POSTGRESQL_URL:-${DEFAULT_DSN}}" "${UV_PYTEST[@]}" server/tests
  return $?
}

# ---------------------------------------------------------------------------
# Resolve shard manifests. Prefer the committed, reviewed split when it matches
# the requested N; otherwise regenerate deterministically via build-test-shards.py.
# Sets SHARD_SRC_DIR to the directory holding shard-0.txt .. shard-(N-1).txt.
# ---------------------------------------------------------------------------
SHARD_SRC_DIR=""
TMP_SHARD_DIR=""
resolve_manifests() {
  local n="$1"
  local i ok=1
  for ((i = 0; i < n; i++)); do
    if [ ! -f "${COMMITTED_SHARD_DIR}/shard-${i}.txt" ]; then
      ok=0
      break
    fi
  done
  if [ "${ok}" -eq 1 ]; then
    # SH-6 fail-closed guard (CW-061): the committed manifests must cover every
    # discovered server/tests/test_*.py. A stale split — a new test file landed
    # without regenerating the manifests — would otherwise make CI silently skip
    # it (the CW-044 §18.2 fail-open gap: 13 files never ran). Refuse to adopt an
    # incomplete split; do NOT degrade to sequential here, because that would
    # mask the staleness and let the committed manifests rot. Force regeneration.
    if ! python3 "${SCRIPT_DIR}/build-test-shards.py" --check-coverage \
        --out-dir "${COMMITTED_SHARD_DIR}" >&2; then
      log "ERROR: committed shard manifests in ${COMMITTED_SHARD_DIR} do not cover every test file (see gaps above)." >&2
      log "ERROR: regenerate and commit them:  python3 scripts/ci/build-test-shards.py --shards ${n}" >&2
      exit 1
    fi
    SHARD_SRC_DIR="${COMMITTED_SHARD_DIR}"
    log "==> Using committed shard manifests in ${SHARD_SRC_DIR} (N=${n})."
    return 0
  fi

  log "==> Committed manifests do not cover N=${n}; regenerating via build-test-shards.py."
  TMP_SHARD_DIR="$(mktemp -d)"
  if python3 "${SCRIPT_DIR}/build-test-shards.py" --shards "${n}" --out-dir "${TMP_SHARD_DIR}" >&2; then
    SHARD_SRC_DIR="${TMP_SHARD_DIR}"
    return 0
  fi

  log "WARNING: shard regeneration failed; falling back to committed N." >&2
  SHARD_SRC_DIR=""
  return 1
}

# ---------------------------------------------------------------------------
# Container lifecycle tracking + cleanup trap.
# ---------------------------------------------------------------------------
STARTED_CONTAINERS=""
cleanup() {
  local rc=$?
  if [ -n "${STARTED_CONTAINERS}" ]; then
    log "==> Tearing down shard PostgreSQL containers..."
    for name in ${STARTED_CONTAINERS}; do
      PG_FIXTURE_NAME="${name}" bash "${PG_FIXTURE}" stop >/dev/null 2>&1 || true
    done
  fi
  if [ -n "${TMP_SHARD_DIR}" ] && [ -d "${TMP_SHARD_DIR}" ]; then
    rm -rf "${TMP_SHARD_DIR}" 2>/dev/null || true
  fi
  exit "${rc}"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if [ "${SHARDS}" -eq 1 ]; then
  run_sequential "N=1 requested"
  exit $?
fi

if ! docker_available; then
  run_sequential "docker not reachable"
  exit $?
fi

if ! resolve_manifests "${SHARDS}"; then
  # Regeneration failed and committed manifests do not cover N: degrade safely.
  run_sequential "shard manifests unavailable for N=${SHARDS}"
  exit $?
fi

log "==> Running ${SHARDS} pytest shards in parallel, each with an isolated PostgreSQL container."

# 1. Start one PostgreSQL container per shard (sequential; each is quick).
dsn_list=()
for ((i = 0; i < SHARDS; i++)); do
  name="customer-v3-pg-test-shard${i}"
  port=$((BASE_PORT + i))
  log "    -> starting ${name} on port ${port}"
  if ! PG_FIXTURE_NAME="${name}" PG_FIXTURE_PORT="${port}" PG_FIXTURE_DB="${PG_DB}" \
      bash "${PG_FIXTURE}" start >&2; then
    log "ERROR: failed to start PostgreSQL fixture for shard ${i}" >&2
    exit 1
  fi
  STARTED_CONTAINERS="${STARTED_CONTAINERS} ${name}"
  dsn_list[$i]="postgresql://${PG_USER}:${PG_PASSWORD}@localhost:${port}/${PG_DB}"
done

# 2. Launch all shards in parallel.
pid_list=()
for ((i = 0; i < SHARDS; i++)); do
  manifest="${SHARD_SRC_DIR}/shard-${i}.txt"
  shard_log="${LOG_DIR}/ci-shard-${i}.log"

  files=""
  while IFS= read -r line; do
    [ -n "${line}" ] && files="${files} ${line}"
  done < "${manifest}"

  log "    -> shard ${i}: $(printf '%s' "${files}" | wc -w | tr -d ' ') test files, log ${shard_log}"
  # shellcheck disable=SC2086
  ( TEST_POSTGRESQL_URL="${dsn_list[$i]}" "${UV_PYTEST[@]}" ${files} ) > "${shard_log}" 2>&1 &
  pid_list[$i]=$!
done

# 3. Collect exit codes.
overall=0
rc_list=()
for ((i = 0; i < SHARDS; i++)); do
  wait "${pid_list[$i]}"
  rc=$?
  rc_list[$i]=${rc}
  shard_log="${LOG_DIR}/ci-shard-${i}.log"
  summary="$(grep -E '[0-9]+ (passed|failed|error)' "${shard_log}" 2>/dev/null | tail -1)"
  if [ "${rc}" -eq 0 ]; then
    log "    shard ${i}: PASS  ${summary}"
  else
    log "    shard ${i}: FAIL (rc=${rc})  ${summary}"
    overall=1
  fi
done

# 4. Surface full logs for any failing shard so CI shows the failure inline.
if [ "${overall}" -ne 0 ]; then
  for ((i = 0; i < SHARDS; i++)); do
    if [ "${rc_list[$i]}" -ne 0 ]; then
      shard_log="${LOG_DIR}/ci-shard-${i}.log"
      log ""
      log "==================== shard ${i} log (${shard_log}) ===================="
      cat "${shard_log}"
    fi
  done
  log ""
  log "==> Sharded pytest FAILED (see shard logs above)."
  exit 1
fi

log ""
log "==> All ${SHARDS} shards passed."
exit 0
