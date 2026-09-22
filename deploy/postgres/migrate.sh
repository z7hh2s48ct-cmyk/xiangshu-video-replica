#!/usr/bin/env bash
set -euo pipefail

: "${VIDEO_REPLICA_DATABASE_URL:?VIDEO_REPLICA_DATABASE_URL is required}"
: "${VIDEO_REPLICA_CUSTOMER_PRODUCTION:?VIDEO_REPLICA_CUSTOMER_PRODUCTION=true is required}"

case "${VIDEO_REPLICA_CUSTOMER_PRODUCTION,,}" in
    1|true|yes|on) ;;
    *)
        echo "customer migration requires VIDEO_REPLICA_CUSTOMER_PRODUCTION=true" >&2
        exit 64
        ;;
esac

# This is a host-local concurrency guard. The runbook designates exactly one
# migration host, so two deploy sessions on that host cannot race Alembic.
# Do not run this script concurrently from multiple hosts.
LOCK_FILE="${VIDEO_REPLICA_MIGRATION_LOCK_FILE:-/var/lock/video-replica-pg-migrate.lock}"
cd /opt/video-replica/app/server
# Alembic reads the DSN directly, so run the same customer-production database
# gate before it can touch any target. This rejects SQLite and libpq modes that
# permit plaintext fallback before the first migration statement executes.
.venv/bin/python -c \
    'from app.db_pg import resolve_database_config, validate_customer_production; validate_customer_production(resolve_database_config())'

# DDL such as a CHECK rebuild takes an ACCESS EXCLUSIVE lock. Acquire it fast
# or fail fast: a migration queued behind peak wallet traffic blocks every
# wallet request behind itself for as long as it waits. PGOPTIONS reaches
# Alembic's libpq sessions without touching the DSN; re-run at low peak if
# this aborts on lock contention. Set the variable to 0 to opt out.
MIGRATION_LOCK_TIMEOUT="${VIDEO_REPLICA_MIGRATION_LOCK_TIMEOUT:-5s}"
if [ "$MIGRATION_LOCK_TIMEOUT" != "0" ]; then
    export PGOPTIONS="${PGOPTIONS:+$PGOPTIONS }-c lock_timeout=$MIGRATION_LOCK_TIMEOUT"
fi

# Not exec'd: a post-upgrade head check has to run in this same shell. `exec`
# replaces the process, so anything written after it would never execute.
flock -n "$LOCK_FILE" .venv/bin/alembic upgrade head

# Verify what is actually applied. `alembic upgrade head` exits 0 when the
# database is already at head, so a green run on its own cannot distinguish
# "just migrated" from "deployed new code against an old schema and did
# nothing" -- and the second case is the one that pages someone at 3am.
# Read-only, so it is safe to run after the lock is released.
#
# Count the heads instead of taking the first. `alembic heads` prints one
# "NNN_slug (head)" line per head, and the previous `awk 'NR == 1'` would
# silently pick one of them; `alembic current` likewise prints one line per row
# in alembic_version, so a database left at more than one revision could be
# compared on its first row alone and reported as "reached the expected head".
# customer-git-rollout.sh guards the same way; keep the two in step.
HEADS_OUTPUT="$(.venv/bin/alembic heads)"
HEAD_COUNT="$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {count++} END {print count + 0}')"
EXPECTED_HEAD="$(printf '%s\n' "$HEADS_OUTPUT" | awk '/\(head\)/ {print $1}' | tail -n 1)"
CURRENT_OUTPUT="$(.venv/bin/alembic current)"
CURRENT_COUNT="$(printf '%s\n' "$CURRENT_OUTPUT" | awk '/\(head\)/ {count++} END {print count + 0}')"
ACTUAL_HEAD="$(printf '%s\n' "$CURRENT_OUTPUT" | awk '/\(head\)/ {print $1}' | tail -n 1)"
if [ "$HEAD_COUNT" != "1" ] || [ -z "$EXPECTED_HEAD" ]; then
    echo "migration script tree must have exactly one head, found ${HEAD_COUNT}" >&2
    exit 70
fi
if [ "$CURRENT_COUNT" != "1" ] || [ "$ACTUAL_HEAD" != "$EXPECTED_HEAD" ]; then
    echo "migration did not reach the expected head: current='${ACTUAL_HEAD}' expected head='${EXPECTED_HEAD}'" >&2
    exit 70
fi
echo "migration verified at expected head: ${ACTUAL_HEAD}"
