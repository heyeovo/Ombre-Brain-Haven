#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

readonly BACKUP_BASE="/srv/ob-backups/haven"
readonly BACKUP_CONFIG="/etc/ombre-backup"
readonly HAVEN_ROOT="/srv/ob-data/haven-test"
readonly CLAUDE_ROOT="/srv/ob-data/claude"
readonly DASHBOARD_WORKSPACE="/srv/ob-workspaces/dashboard"
readonly HAVEN_WORKSPACE="/srv/ob-workspaces/haven"
readonly COOLIFY_SERVICE_ROOT="/data/coolify/services/5jhemgqroisbatkrbgbefueu"
readonly EXPECTED_DB_COUNT=13

mkdir -p "${BACKUP_BASE}/.restic-cache"
chmod 700 "${BACKUP_BASE}/.restic-cache"

exec 9>/run/lock/ombre-vps-backup.lock
flock -w 600 9

STAGE="$(mktemp -d "${BACKUP_BASE}/daily.XXXXXX")"
STAGE="$(readlink -f -- "$STAGE")"

case "$STAGE" in
  "${BACKUP_BASE}"/daily.*) ;;
  *) echo "INVALID_STAGE_PATH" >&2; exit 1 ;;
esac

cleanup() {
  unset B2_ACCOUNT_ID B2_ACCOUNT_KEY RESTIC_REPOSITORY_FILE RESTIC_PASSWORD_FILE RESTIC_CACHE_DIR
  if [[ -n "${STAGE:-}" && -d "$STAGE" ]]; then
    case "$STAGE" in
      "${BACKUP_BASE}"/daily.*) rm -rf --one-file-system -- "$STAGE" ;;
      *) echo "REFUSING_STAGE_CLEANUP: $STAGE" >&2 ;;
    esac
  fi
}
trap cleanup EXIT HUP INT TERM

for required_path in \
  "$HAVEN_ROOT/buckets" \
  "$HAVEN_ROOT/state" \
  "$HAVEN_ROOT/config" \
  "$CLAUDE_ROOT" \
  "$DASHBOARD_WORKSPACE" \
  "$HAVEN_WORKSPACE" \
  "$COOLIFY_SERVICE_ROOT/docker-compose.yml" \
  "$COOLIFY_SERVICE_ROOT/.env" \
  "$BACKUP_CONFIG/b2-key-id" \
  "$BACKUP_CONFIG/b2-application-key" \
  "$BACKUP_CONFIG/restic-password" \
  "$BACKUP_CONFIG/repository"; do
  [[ -e "$required_path" ]] || { echo "MISSING_REQUIRED_PATH: $required_path" >&2; exit 1; }
done

SENSITIVE_WORKSPACE_FILE="$(
  find "$DASHBOARD_WORKSPACE" "$HAVEN_WORKSPACE" -type f \
    \( -name '.env' \
       -o -name '.env.local' \
       -o -name '.env.production' \
       -o -name '.env.*.local' \
       -o -name '.npmrc' \
       -o -name '.pypirc' \
       -o -name 'id_rsa' \
       -o -name 'id_ed25519' \
       -o -name '*.pem' \
       -o -path '*/.ssh/*' \) \
    -print -quit
)"

if [[ -n "$SENSITIVE_WORKSPACE_FILE" ]]; then
  echo "SENSITIVE_WORKSPACE_FILE_BLOCKED: $SENSITIVE_WORKSPACE_FILE" >&2
  exit 1
fi

mkdir -p \
  "$STAGE/haven/buckets" \
  "$STAGE/haven/state" \
  "$STAGE/haven/config" \
  "$STAGE/claude" \
  "$STAGE/workspaces/dashboard" \
  "$STAGE/workspaces/haven" \
  "$STAGE/coolify/database" \
  "$STAGE/coolify/haven-service" \
  "$STAGE/verification"

tar -C "$HAVEN_ROOT/buckets" \
  --exclude='*.sqlite' --exclude='*.sqlite-wal' --exclude='*.sqlite-shm' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  -cf - . | tar -C "$STAGE/haven/buckets" -xf -

tar -C "$HAVEN_ROOT/state" \
  --exclude='*.sqlite' --exclude='*.sqlite-wal' --exclude='*.sqlite-shm' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='.migration-rollback' \
  -cf - . | tar -C "$STAGE/haven/state" -xf -

tar -C "$HAVEN_ROOT/config" -cf - . | tar -C "$STAGE/haven/config" -xf -
tar -C "$CLAUDE_ROOT" -cf - . | tar -C "$STAGE/claude" -xf -

tar -C "$DASHBOARD_WORKSPACE" \
  --exclude='node_modules' --exclude='.next' --exclude='.turbo' --exclude='coverage' \
  -cf - . | tar -C "$STAGE/workspaces/dashboard" -xf -

tar -C "$HAVEN_WORKSPACE" \
  --exclude='node_modules' --exclude='.next' --exclude='.turbo' --exclude='coverage' \
  -cf - . | tar -C "$STAGE/workspaces/haven" -xf -

DB_COUNT=0
: > "$STAGE/verification/sqlite-integrity.txt"

while IFS= read -r -d '' source_db; do
  relative_db="${source_db#${HAVEN_ROOT}/}"
  destination_db="$STAGE/haven/$relative_db"
  mkdir -p "$(dirname "$destination_db")"

  sqlite3 "$source_db" ".timeout 30000" ".backup '$destination_db'"
  integrity_result="$(sqlite3 "$destination_db" 'PRAGMA integrity_check;')"

  [[ "$integrity_result" == "ok" ]] || {
    echo "SQLITE_INTEGRITY_FAILED: $relative_db" >&2
    exit 1
  }

  printf '%s: ok\n' "$relative_db" >> "$STAGE/verification/sqlite-integrity.txt"
  DB_COUNT=$((DB_COUNT + 1))
done < <(
  find "$HAVEN_ROOT/buckets" "$HAVEN_ROOT/state" \
    -type f \( -name '*.sqlite' -o -name '*.db' \) -print0
)

[[ "$DB_COUNT" -eq "$EXPECTED_DB_COUNT" ]] || {
  echo "DATABASE_COUNT_MISMATCH: expected=$EXPECTED_DB_COUNT actual=$DB_COUNT" >&2
  exit 1
}

cp -a \
  "$COOLIFY_SERVICE_ROOT/docker-compose.yml" \
  "$COOLIFY_SERVICE_ROOT/.env" \
  "$STAGE/coolify/haven-service/"
chmod 600 \
  "$STAGE/coolify/haven-service/docker-compose.yml" \
  "$STAGE/coolify/haven-service/.env"

docker exec coolify-db sh -lc \
  'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "$STAGE/coolify/database/coolify.dump"

[[ -s "$STAGE/coolify/database/coolify.dump" ]] || {
  echo "COOLIFY_DUMP_EMPTY" >&2
  exit 1
}

docker exec -i coolify-db pg_restore --list \
  < "$STAGE/coolify/database/coolify.dump" \
  > "$STAGE/verification/coolify-pgrestore-list.txt"
chmod 600 "$STAGE/coolify/database/coolify.dump"

{
  printf 'created_utc=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'host=%s\n' "$(hostname)"
  printf 'haven_root=%s\n' "$HAVEN_ROOT"
  printf 'database_count=%s\n' "$DB_COUNT"
  printf 'sqlite_integrity=ok\n'
  printf 'coolify_database_dump=custom-format-ok\n'
  printf 'coolify_archive_entries=%s\n' "$(wc -l < "$STAGE/verification/coolify-pgrestore-list.txt")"
  printf 'stage_bytes=%s\n' "$(du -sb "$STAGE" | awk '{print $1}')"
  restic version
} > "$STAGE/verification/backup-manifest.txt"

(
  cd "$STAGE"
  find haven claude workspaces coolify -type f -print0 \
    | sort -z \
    | xargs -0 sha256sum
) > "$STAGE/verification/payload-sha256.txt"

set +x
export B2_ACCOUNT_ID="$(< "$BACKUP_CONFIG/b2-key-id")"
export B2_ACCOUNT_KEY="$(< "$BACKUP_CONFIG/b2-application-key")"
export RESTIC_REPOSITORY_FILE="$BACKUP_CONFIG/repository"
export RESTIC_PASSWORD_FILE="$BACKUP_CONFIG/restic-password"
export RESTIC_CACHE_DIR="$BACKUP_BASE/.restic-cache"

restic backup "$STAGE" \
  --host "$(hostname)" \
  --tag ombre-vps-daily \
  --tag complete-vps-config

restic check
echo "OMBRE_VPS_BACKUP_OK"
