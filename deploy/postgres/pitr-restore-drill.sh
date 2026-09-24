#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pitr-preflight.sh
source "$SCRIPT_DIR/pitr-preflight.sh"

restore_fail() {
    printf 'PITR restore drill failed: %s\n' "$1" >&2
    exit 64
}

usage() {
    printf 'usage: %s --label LABEL --manifest PATH [--port PORT]\n' "$0" >&2
    exit 64
}

backup_label=''
manifest=''
port="${VIDEO_REPLICA_PG_PITR_DRILL_PORT:-55432}"
while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --label) backup_label="${2:-}"; shift 2 ;;
        --manifest) manifest="${2:-}"; shift 2 ;;
        --port) port="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done
[[ "$backup_label" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{2,80}$ ]] || restore_fail 'PITR backup label is invalid'
[[ "$manifest" = /* && -f "$manifest" ]] || restore_fail 'recovery manifest must be an existing absolute path'
[[ "$port" =~ ^[1-9][0-9]{3,4}$ && "$port" -le 65535 ]] || restore_fail 'recovery port is invalid'

: "${VIDEO_REPLICA_PITR_DRILL_ENV:?VIDEO_REPLICA_PITR_DRILL_ENV=staging is required}"
: "${VIDEO_REPLICA_PITR_DRILL_CONFIRM:?explicit restore confirmation is required}"
[[ "$VIDEO_REPLICA_PITR_DRILL_ENV" == 'staging' ]] || restore_fail 'PITR recovery drill may run only with VIDEO_REPLICA_PITR_DRILL_ENV=staging'
[[ "$VIDEO_REPLICA_PITR_DRILL_CONFIRM" == 'RESTORE_SYNTHETIC_STAGING_DATA' ]] || restore_fail 'PITR recovery drill requires VIDEO_REPLICA_PITR_DRILL_CONFIRM=RESTORE_SYNTHETIC_STAGING_DATA'

: "${VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT:?PITR recovery root is required}"
: "${VIDEO_REPLICA_PG_PITR_RECOVERY_DB:?recovery database name is required}"
: "${VIDEO_REPLICA_PG_PITR_RECOVERY_USER:?recovery role is required}"
[[ "$VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT" = /* && -d "$VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT" ]] || restore_fail 'PITR recovery root must be an existing absolute directory'
[[ "$VIDEO_REPLICA_PG_PITR_RECOVERY_DB" =~ ^[A-Za-z0-9_]+$ ]] || restore_fail 'recovery database name is invalid'
[[ "$VIDEO_REPLICA_PG_PITR_RECOVERY_USER" =~ ^[A-Za-z0-9_]+$ ]] || restore_fail 'recovery role is invalid'

pitr_preflight
pitr_require_command pg_ctl
pitr_require_command pg_isready

recovery_root="$(realpath -e -- "$VIDEO_REPLICA_PG_PITR_RECOVERY_ROOT")"
[[ "$recovery_root" != '/' ]] || restore_fail 'PITR recovery root must not be the filesystem root'
recovery_dir="$recovery_root/$backup_label"
[[ "$recovery_dir" == "$recovery_root"/* && "$recovery_dir" != "$recovery_root" ]] || restore_fail 'recovery directory escapes its root'
if [[ -e "$recovery_dir" ]]; then
    [[ ! -L "$recovery_dir" ]] || restore_fail 'recovery directory must not be a symlink'
    rm -rf -- "$recovery_dir"
fi
pg_isready --host=127.0.0.1 --port="$port" >/dev/null 2>&1 && restore_fail 'recovery port is already in use'

mkdir -m 0700 "$recovery_dir"
"$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" get-base "$backup_label" "$recovery_dir" >/dev/null
pg_verifybackup "$recovery_dir"
chmod 0700 "$recovery_dir"

python_bin="${VIDEO_REPLICA_PG_PITR_PYTHON:-/opt/video-replica/app/server/.venv/bin/python}"
[[ "$python_bin" = /* && -x "$python_bin" ]] || restore_fail 'protected application Python is unavailable'
server_dir="$(realpath -e -- "$SCRIPT_DIR/../../server")"
[[ -f "$server_dir/scripts/pitr_recovery_facts.py" ]] || restore_fail 'PITR recovery verifier is unavailable'
recovery_target_time="$(cd "$server_dir" && "$python_bin" -m scripts.pitr_recovery_facts target --manifest "$manifest")" || restore_fail 'recovery manifest is invalid'
fetch_wal="$SCRIPT_DIR/pitr-fetch-wal.sh"
printf "\nrestore_command = '%s %%f %%p'\nrecovery_target_time = '%s'\nrecovery_target_action = 'promote'\n" \
    "$fetch_wal" "$recovery_target_time" >>"$recovery_dir/postgresql.auto.conf"
touch "$recovery_dir/recovery.signal"

started=0
stop_recovery() {
    if [[ "$started" -eq 1 ]]; then
        pg_ctl -D "$recovery_dir" -m fast -w stop >/dev/null 2>&1 || true
    fi
}
trap stop_recovery EXIT
pg_ctl -D "$recovery_dir" -o "-p $port -c listen_addresses=127.0.0.1" -w start
started=1

psql --no-psqlrc --quiet --tuples-only --no-align \
    --host=127.0.0.1 --port="$port" --username="$VIDEO_REPLICA_PG_PITR_RECOVERY_USER" \
    --dbname="$VIDEO_REPLICA_PG_PITR_RECOVERY_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT CASE WHEN pg_is_in_recovery() THEN 'recovering' ELSE 'promoted' END" \
    | grep -qx 'promoted' || restore_fail 'recovery did not reach the requested PITR target'

(cd "$server_dir" && "$python_bin" -m scripts.pitr_recovery_facts verify --manifest "$manifest" \
    --conninfo "host=127.0.0.1 port=$port dbname=$VIDEO_REPLICA_PG_PITR_RECOVERY_DB user=$VIDEO_REPLICA_PG_PITR_RECOVERY_USER")
printf 'PITR staging recovery drill verified 100 cross-domain facts\n'
