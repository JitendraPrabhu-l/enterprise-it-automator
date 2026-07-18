#!/usr/bin/env bash
# Automated Postgres backup, deliberately isolated from the app's own
# credentials (see docs/RUNBOOKS.md's "Audit log integrity" /
# "Backup / restore" sections for why: an agent with valid, approved
# credentials can delete production data, and a backup plane that trusts
# the SAME credentials as the app it's protecting is not a real recovery
# plane).
#
# Reads ONLY BACKUP_DATABASE_URL — a separate connection string for a
# dedicated Postgres role, distinct from DATABASE_URL (the app's own
# read/write credential). This script never reads DATABASE_URL, API_KEY,
# or MCP_SERVER_TOKEN, and should be run from a process/identity that
# doesn't hold them either. The backup role should be granted SELECT only
# on the app's tables — no INSERT/UPDATE/DELETE/DROP — so a compromised or
# malicious agent action (which uses the APP's credential, never this
# script's) cannot also reach or destroy the backups this produces.
#
# Usage:
#   BACKUP_DATABASE_URL="postgresql://backup_ro:pw@host:5432/it_automator" \
#     ./scripts/backup_db.sh
#
# Optional:
#   BACKUP_OUTPUT_DIR   — where the dump file is written (default ./backups)
#   BACKUP_UPLOAD_CMD    — a shell command template to ship the dump
#                          off-host after it's written; {} is replaced with
#                          the dump file's path, e.g.:
#                            BACKUP_UPLOAD_CMD='aws s3 cp {} s3://my-bucket/'
#                            BACKUP_UPLOAD_CMD='rclone copy {} remote:backups/'
#                          Left unset: the dump is written locally only —
#                          deliberately no vendor SDK bundled here, since
#                          "where backups go" is an ops decision, not an
#                          application concern (same "runtime concern, not
#                          app concern" pattern as secrets management in
#                          README.md).
set -euo pipefail

: "${BACKUP_DATABASE_URL:?Set BACKUP_DATABASE_URL to a read-only Postgres role connection string — never DATABASE_URL}"

OUTPUT_DIR="${BACKUP_OUTPUT_DIR:-./backups}"
mkdir -p "$OUTPUT_DIR"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DUMP_FILE="${OUTPUT_DIR}/it_automator_${TIMESTAMP}.dump"

echo "Backing up to ${DUMP_FILE} ..."
# -Fc (custom format): compressed, and the format pg_restore expects —
# matches docs/RUNBOOKS.md's existing manual pg_dump/pg_restore guidance.
pg_dump -Fc "$BACKUP_DATABASE_URL" > "$DUMP_FILE"
echo "OK: $(du -h "$DUMP_FILE" | cut -f1) written."

if [ -n "${BACKUP_UPLOAD_CMD:-}" ]; then
  UPLOAD_CMD="${BACKUP_UPLOAD_CMD//\{\}/$DUMP_FILE}"
  echo "Uploading: ${UPLOAD_CMD}"
  eval "$UPLOAD_CMD"
  echo "OK: upload command completed."
fi
