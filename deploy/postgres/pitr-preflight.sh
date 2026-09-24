#!/usr/bin/env bash
set -euo pipefail

# T38 deliberately uses a privileged, protected libpq service instead of the
# application DSN. The service file points to a .pgpass/managed credential and
# grants only the backup/monitor privileges needed for this job.

pitr_fail() {
    printf 'PITR preflight failed: %s\n' "$1" >&2
    return 64
}

pitr_require_absolute_file() {
    local path="$1"
    [[ "$path" = /* && -f "$path" && -r "$path" ]] || pitr_fail 'required protected file is unavailable'
}

pitr_require_absolute_executable() {
    local path="$1"
    [[ "$path" = /* && -f "$path" && -x "$path" ]] || pitr_fail 'required archive helper is unavailable'
}

pitr_require_command() {
    command -v "$1" >/dev/null 2>&1 || pitr_fail "required PostgreSQL client command is unavailable"
}

pitr_is_positive_integer() {
    [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

pitr_configure_service() {
    : "${VIDEO_REPLICA_CUSTOMER_PRODUCTION:?VIDEO_REPLICA_CUSTOMER_PRODUCTION=true is required}"
    case "${VIDEO_REPLICA_CUSTOMER_PRODUCTION,,}" in
        1|true|yes|on) ;;
        *) pitr_fail 'customer PITR requires VIDEO_REPLICA_CUSTOMER_PRODUCTION=true' ;;
    esac

    : "${VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE:?backup libpq service file is required}"
    : "${VIDEO_REPLICA_PG_BACKUP_SERVICE:?backup libpq service name is required}"
    : "${VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER:?archive helper is required}"
    [[ "$VIDEO_REPLICA_PG_BACKUP_SERVICE" =~ ^[A-Za-z0-9_.-]+$ ]] || pitr_fail 'backup service name is invalid'
    pitr_require_absolute_file "$VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE"
    pitr_require_absolute_executable "$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER"
    pitr_require_command psql
    pitr_require_command pg_basebackup
    pitr_require_command pg_verifybackup

    # Do not add VIDEO_REPLICA_DATABASE_URL here. The application role must
    # remain unable to read archive status or initiate physical backups.
    export PGSERVICEFILE="$VIDEO_REPLICA_PG_BACKUP_SERVICE_FILE"
    export PGSERVICE="$VIDEO_REPLICA_PG_BACKUP_SERVICE"
}

pitr_preflight() {
    pitr_configure_service

    local settings
    settings="$(psql --no-psqlrc --quiet --tuples-only --no-align -v ON_ERROR_STOP=1 \
        -c "SELECT current_setting('server_version_num'), current_setting('wal_level'), current_setting('archive_mode'), CASE WHEN current_setting('archive_command') <> '' OR current_setting('archive_library') <> '' THEN 'configured' ELSE 'missing' END")" || pitr_fail 'cannot read PostgreSQL PITR settings'
    local version wal_level archive_mode archive_transport
    IFS='|' read -r version wal_level archive_mode archive_transport <<<"$settings"
    [[ "$version" =~ ^[0-9]+$ && "$version" -ge 160000 ]] || pitr_fail 'PostgreSQL 16 or later is required'
    [[ "$wal_level" == 'replica' || "$wal_level" == 'logical' ]] || pitr_fail 'wal_level must be replica or logical'
    [[ "$archive_mode" == 'on' || "$archive_mode" == 'always' ]] || pitr_fail 'archive_mode must be on or always'
    [[ "$archive_transport" == 'configured' ]] || pitr_fail 'archive_command or archive_library must be configured'

    local timeout_seconds="${VIDEO_REPLICA_PG_PITR_ARCHIVE_TIMEOUT_SECONDS:-300}"
    pitr_is_positive_integer "$timeout_seconds" || pitr_fail 'archive timeout must be a positive integer'
    (( timeout_seconds <= 900 )) || pitr_fail 'archive timeout exceeds the safe bound'

    local wal_name
    wal_name="$(psql --no-psqlrc --quiet --tuples-only --no-align -v ON_ERROR_STOP=1 \
        -c "SELECT pg_walfile_name(pg_switch_wal())")" || pitr_fail 'cannot force a WAL archive boundary'
    [[ "$wal_name" =~ ^[0-9A-F]{24}$ ]] || pitr_fail 'PostgreSQL returned an invalid WAL name'

    local attempts=$(( (timeout_seconds + 4) / 5 ))
    local attempt
    for ((attempt = 1; attempt <= attempts; attempt += 1)); do
        if "$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" assert-wal "$wal_name" >/dev/null; then
            printf 'PITR preflight passed: archived WAL is externally retrievable\n'
            return 0
        fi
        sleep 5
    done
    pitr_fail 'forced WAL is not retrievable from the protected archive'
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    pitr_preflight
fi
