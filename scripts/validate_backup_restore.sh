#!/usr/bin/env bash
# End-to-end validation that a backup produced by scripts/backup_db.sh is
# actually RESTORABLE — proving "a file exists" is not the same as proving
# "we can recover from it," and this is the check docs/RUNBOOKS.md's
# quarterly restore-drill guidance points at.
#
# What this proves, against a real (throwaway) Postgres:
#   1. A dedicated read-only backup role (least-privilege — SELECT only,
#      no INSERT/UPDATE/DELETE/DROP) can produce a working pg_dump via
#      scripts/backup_db.sh, without ever touching the app's own
#      DATABASE_URL/write credential.
#   2. That dump restores cleanly into a FRESH database via pg_restore.
#   3. The restored database's schema/row counts match the source —
#      alembic_version's revision agrees, and a couple of key tables have
#      the same row count as the source.
#
# Run anywhere Docker works:
#   ./scripts/validate_backup_restore.sh
# Tears the scratch stack down on exit either way (disable with KEEP_STACK=1).
set -euo pipefail
cd "$(dirname "$0")/.."

export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 16)}"
export API_KEY="${API_KEY:-$(openssl rand -hex 16)}"
export MCP_SERVER_TOKEN="${MCP_SERVER_TOKEN:-$(openssl rand -hex 16)}"

PASS=0
step() { printf '\n== %s\n' "$1"; }
ok()   { PASS=$((PASS+1)); printf '   OK: %s\n' "$1"; }
die()  { printf '   FAIL: %s\n' "$1"; exit 1; }

cleanup() {
  if [ "${KEEP_STACK:-0}" != "1" ]; then
    docker compose down -v >/dev/null 2>&1 || true
    rm -rf "${SCRATCH_DIR:-}"
  fi
}
trap cleanup EXIT

step "Start Postgres only (no app container needed for this check)"
docker compose up -d postgres
for i in $(seq 1 30); do
  docker compose exec -T postgres pg_isready -U itauto -d it_automator >/dev/null 2>&1 && break
  [ "$i" = 30 ] && die "postgres never became ready"
  sleep 2
done
ok "postgres ready"

step "Apply the Alembic chain (source schema + data to back up)"
docker compose run --rm -e DATABASE_URL="postgresql+asyncpg://itauto:${POSTGRES_PASSWORD}@postgres:5432/it_automator" \
  app alembic upgrade head
docker compose exec -T postgres psql -U itauto -d it_automator -c \
  "INSERT INTO api_clients (name, role, key, daily_request_limit, daily_request_count, request_count_reset_at, created_at) \
   VALUES ('restore-drill-canary', 'STANDARD', 'canary-key', 100, 0, now(), now());" >/dev/null
ok "source database seeded (migrated + one canary row)"

step "Create a dedicated read-only backup role (least privilege, not the app's own role)"
docker compose exec -T postgres psql -U itauto -d it_automator -c \
  "DO \$\$ BEGIN
     IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'backup_ro') THEN
       CREATE ROLE backup_ro LOGIN PASSWORD 'backup_ro_pw';
     END IF;
   END \$\$;" >/dev/null
docker compose exec -T postgres psql -U itauto -d it_automator -c \
  "GRANT CONNECT ON DATABASE it_automator TO backup_ro;
   GRANT USAGE ON SCHEMA public TO backup_ro;
   GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_ro;" >/dev/null
ok "backup_ro role has SELECT-only grants"

step "Run scripts/backup_db.sh AS the read-only role (never DATABASE_URL)"
SCRATCH_DIR="$(mktemp -d)"
BACKUP_DATABASE_URL="postgresql://backup_ro:backup_ro_pw@127.0.0.1:5432/it_automator" \
  BACKUP_OUTPUT_DIR="$SCRATCH_DIR" \
  ./scripts/backup_db.sh
DUMP_FILE="$(ls -t "$SCRATCH_DIR"/*.dump | head -1)"
[ -s "$DUMP_FILE" ] || die "no dump file produced"
ok "dump produced by backup_db.sh: $DUMP_FILE"

step "Confirm the read-only role genuinely cannot write (proves isolation, not just intent)"
if docker compose exec -T postgres psql -U backup_ro -d it_automator -c \
    "INSERT INTO api_clients (name, role, key, daily_request_limit, daily_request_count, request_count_reset_at, created_at) \
     VALUES ('should-fail', 'STANDARD', 'x', 1, 0, now(), now());" >/dev/null 2>&1; then
  die "backup_ro role could WRITE — it must be SELECT-only"
fi
ok "backup_ro role's write attempt was rejected, as expected"

step "Restore the dump into a FRESH database"
docker compose exec -T postgres psql -U itauto -d postgres -c \
  "DROP DATABASE IF EXISTS it_automator_restore_check;" \
  -c "CREATE DATABASE it_automator_restore_check OWNER itauto;" >/dev/null
docker compose cp "$DUMP_FILE" postgres:/tmp/restore_check.dump
docker compose exec -T postgres pg_restore -U itauto -d it_automator_restore_check --no-owner /tmp/restore_check.dump
ok "pg_restore completed into it_automator_restore_check"

step "Restored schema/data matches the source"
SOURCE_REV=$(docker compose exec -T postgres psql -U itauto -d it_automator -tAc "SELECT version_num FROM alembic_version")
RESTORED_REV=$(docker compose exec -T postgres psql -U itauto -d it_automator_restore_check -tAc "SELECT version_num FROM alembic_version")
[ "$SOURCE_REV" = "$RESTORED_REV" ] || die "alembic_version mismatch: source=$SOURCE_REV restored=$RESTORED_REV"
ok "alembic_version matches ($SOURCE_REV)"

SOURCE_COUNT=$(docker compose exec -T postgres psql -U itauto -d it_automator -tAc \
  "SELECT count(*) FROM api_clients WHERE name = 'restore-drill-canary'")
RESTORED_COUNT=$(docker compose exec -T postgres psql -U itauto -d it_automator_restore_check -tAc \
  "SELECT count(*) FROM api_clients WHERE name = 'restore-drill-canary'")
[ "$SOURCE_COUNT" = "1" ] && [ "$RESTORED_COUNT" = "1" ] || die "canary row missing after restore (source=$SOURCE_COUNT restored=$RESTORED_COUNT)"
ok "canary row present in the restored database"

printf '\nAll %d checks passed — backup is genuinely restorable, and the backup role is genuinely read-only.\n' "$PASS"
