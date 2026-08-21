#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly BACKUP_BASE="/srv/ob-backups/haven"
readonly BACKUP_CONFIG="/etc/ombre-backup"

for required_path in \
  "$BACKUP_CONFIG/b2-key-id" \
  "$BACKUP_CONFIG/b2-application-key" \
  "$BACKUP_CONFIG/restic-password" \
  "$BACKUP_CONFIG/repository"; do
  [[ -f "$required_path" ]] || { echo "MISSING_REQUIRED_PATH: $required_path" >&2; exit 1; }
done

mkdir -p "$BACKUP_BASE/.restic-cache"
chmod 700 "$BACKUP_BASE/.restic-cache"

exec 9>/run/lock/ombre-vps-backup.lock
flock -w 3600 9

cleanup() {
  unset B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_REPOSITORY_FILE RESTIC_PASSWORD_FILE RESTIC_CACHE_DIR
}
trap cleanup EXIT HUP INT TERM

set +x
export B2_ACCOUNT_ID="$(< "$BACKUP_CONFIG/b2-key-id")"
export B2_ACCOUNT_KEY="$(< "$BACKUP_CONFIG/b2-application-key")"
export RESTIC_REPOSITORY_FILE="$BACKUP_CONFIG/repository"
export RESTIC_PASSWORD_FILE="$BACKUP_CONFIG/restic-password"
export RESTIC_CACHE_DIR="$BACKUP_BASE/.restic-cache"

restic check --read-data

restic forget \
  --host "$(hostname)" \
  --tag ombre-vps-daily \
  --group-by host,tags \
  --keep-daily 7 \
  --keep-weekly 4 \
  --keep-monthly 3 \
  --prune

echo "OMBRE_VPS_PRUNE_OK"
