#!/usr/bin/env bash
set -euo pipefail
umask 077

fetch_fail() {
    printf 'PITR WAL fetch failed: %s\n' "$1" >&2
    exit 64
}

: "${VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER:?archive helper is required}"
[[ "$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" = /* && -x "$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" ]] || fetch_fail 'archive helper is unavailable'
[[ "$#" -eq 2 ]] || fetch_fail 'expected a WAL file name and PostgreSQL destination'

wal_name="$1"
destination="$2"
[[ "$wal_name" =~ ^([0-9A-F]{24}|[0-9A-F]{8}\.history)$ ]] || fetch_fail 'WAL file name is invalid'
[[ "$destination" == "pg_wal/"* && "$destination" != *".."* ]] || fetch_fail 'PostgreSQL restore destination is invalid'

exec "$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" get-wal "$wal_name" "$destination"
