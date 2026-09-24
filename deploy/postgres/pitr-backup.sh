#!/usr/bin/env bash
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pitr-preflight.sh
source "$SCRIPT_DIR/pitr-preflight.sh"

pitr_preflight

: "${VIDEO_REPLICA_PG_PITR_BACKUP_ROOT:?PITR backup root is required}"
[[ "$VIDEO_REPLICA_PG_PITR_BACKUP_ROOT" = /* && -d "$VIDEO_REPLICA_PG_PITR_BACKUP_ROOT" ]] || pitr_fail 'PITR backup root must be an existing absolute directory'
[[ ! -L "$VIDEO_REPLICA_PG_PITR_BACKUP_ROOT" ]] || pitr_fail 'PITR backup root must not be a symlink'
backup_root="$(realpath -e -- "$VIDEO_REPLICA_PG_PITR_BACKUP_ROOT")"
[[ "$backup_root" != '/' ]] || pitr_fail 'PITR backup root must not be the filesystem root'

backup_label="${VIDEO_REPLICA_PG_PITR_BACKUP_LABEL:-t38-$(date -u +%Y%m%dT%H%M%SZ)}"
[[ "$backup_label" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{2,80}$ ]] || pitr_fail 'PITR backup label is invalid'

lock_file="${VIDEO_REPLICA_PG_PITR_LOCK_FILE:-$backup_root/.backup.lock}"
[[ "$lock_file" = "$backup_root"/* && ! -L "$lock_file" ]] || pitr_fail 'PITR lock file must stay inside the backup root'
exec 9>"$lock_file"
flock -n 9 || pitr_fail 'another PITR base backup is already running'

final_dir="$backup_root/$backup_label"
staging_dir="$backup_root/.incoming-$backup_label"
[[ ! -e "$final_dir" && ! -e "$staging_dir" ]] || pitr_fail 'PITR backup label already exists'
mkdir -m 0700 "$staging_dir"
cleanup() {
    rm -rf -- "$staging_dir"
}
trap cleanup EXIT

pg_basebackup \
    --pgdata="$staging_dir" \
    --format=plain \
    --wal-method=stream \
    --checkpoint=fast \
    --manifest-checksums=SHA256 \
    --label="$backup_label"
pg_verifybackup "$staging_dir"

# The helper owns encryption, cross-region copy, object immutability, and
# credential handling. It receives a local protected directory only.
"$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" put-base "$backup_label" "$staging_dir" >/dev/null
"$VIDEO_REPLICA_PG_PITR_ARCHIVE_HELPER" assert-base "$backup_label" >/dev/null

mv -- "$staging_dir" "$final_dir"
trap - EXIT
printf 'PITR base backup published and externally verified: %s\n' "$backup_label"
