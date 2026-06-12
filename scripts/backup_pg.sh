#!/usr/bin/env bash
# Daily logical backup of the LOCAL Label Studio Postgres (container label-studio-postgres).
# Custom-format (-Fc) dump => restore with: pg_restore --no-owner --no-acl -d <db> <file>
# Installed via the user crontab; see `crontab -l`. Backups live OUTSIDE ./pgdata so a
# corrupt data dir doesn't take the backups with it.
set -euo pipefail

CONTAINER=label-studio-postgres
DB=label-studio
DBUSER=labelstudio
BACKUP_DIR=/home/swjeon/docker/label-studio/backups
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$BACKUP_DIR/${DB}-${TS}.dump"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Bail if the DB container isn't up (don't create a 0-byte "backup").
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  log "ERROR: container $CONTAINER not running; skipping backup"
  exit 1
fi

start=$(date +%s)
# pg_dump over the container's trusted local socket (no password). Write to .tmp then
# atomically rename so a crash mid-dump never leaves a half-file that looks complete.
if docker exec "$CONTAINER" pg_dump -U "$DBUSER" -d "$DB" -Fc > "$OUT.tmp"; then
  mv "$OUT.tmp" "$OUT"
  size=$(du -h "$OUT" | cut -f1)
  log "OK: $OUT ($size, $(($(date +%s) - start))s)"
else
  rm -f "$OUT.tmp"
  log "ERROR: pg_dump failed"
  exit 1
fi

# Rotate: delete dumps older than retention window.
deleted=$(find "$BACKUP_DIR" -maxdepth 1 -name "${DB}-*.dump" -type f -mtime "+$RETENTION_DAYS" -print -delete | wc -l)
[ "$deleted" -gt 0 ] && log "rotated: removed $deleted dump(s) older than ${RETENTION_DAYS}d"
exit 0
