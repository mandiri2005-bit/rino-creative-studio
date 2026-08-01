#!/bin/sh
# Off-site logical backup of the Wimba production database.
#
# Runs as a Railway cron service, so it is independent of any laptop being awake.
# It dumps, VERIFIES the dump actually carries rows, uploads to object storage, then
# prunes old copies. Every failure exits nonzero so Railway marks the run failed instead
# of the job "succeeding" while producing nothing.
#
# Why the verification step exists: the backups that were on hand when this database was
# lost on 2026-07-25 were all schema-only — CREATE TABLE statements, zero COPY blocks.
# They looked like backups. A dump that restores an empty database is worse than no dump,
# because it is mistaken for protection.
set -eu

: "${BACKUP_DATABASE_URL:?BACKUP_DATABASE_URL is required}"
: "${BACKUP_REMOTE:?BACKUP_REMOTE is required, e.g. r2:wimba-backups}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-30}"
MIN_ROWS="${BACKUP_MIN_ROWS:-1}"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
DUMP="$WORK/wimba-$STAMP.dump"
trap 'rm -rf "$WORK"' EXIT INT TERM

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# ---------------------------------------------------------------------------
# 1. Dump. Custom format so pg_restore can filter, and so it is compressed.
# ---------------------------------------------------------------------------
log "dumping"
pg_dump -Fc -Z6 -d "$BACKUP_DATABASE_URL" -f "$DUMP"
BYTES="$(wc -c < "$DUMP" | tr -d ' ')"
log "dump written, $BYTES bytes"

# ---------------------------------------------------------------------------
# 2. Verify the dump carries DATA, not just DDL. This is the whole point.
# ---------------------------------------------------------------------------
TABLE_DATA="$(pg_restore --list "$DUMP" | grep -c ' TABLE DATA ' || true)"
log "TABLE DATA entries: $TABLE_DATA"
if [ "$TABLE_DATA" -lt 1 ]; then
  log "FATAL: archive contains no TABLE DATA — this is a schema-only dump"
  exit 3
fi

# Decode the data section and count real rows rather than trusting the table-of-contents.
pg_restore --data-only -f "$WORK/data.sql" "$DUMP"
ROWS="$(awk '
  /^COPY /      { inblock = 1; next }
  inblock && /^\\\.$/ { inblock = 0; next }
  inblock       { n++ }
  END           { print n + 0 }
' "$WORK/data.sql")"
log "rows in dump: $ROWS"
if [ "$ROWS" -lt "$MIN_ROWS" ]; then
  log "FATAL: only $ROWS rows, below BACKUP_MIN_ROWS=$MIN_ROWS — refusing to upload"
  exit 4
fi

# Cross-check against the live database, so a dump that silently loses tables is caught.
LIVE_TABLES="$(psql -Atqc "select count(*) from pg_tables where schemaname not in ('pg_catalog','information_schema')" "$BACKUP_DATABASE_URL")"
if [ "$TABLE_DATA" -ne "$LIVE_TABLES" ]; then
  log "FATAL: dump has $TABLE_DATA TABLE DATA entries but the database has $LIVE_TABLES tables"
  exit 5
fi
log "verified: $TABLE_DATA/$LIVE_TABLES tables, $ROWS rows"

# ---------------------------------------------------------------------------
# 3. Upload, then read it back. An upload that reports success but stores nothing is the
#    same failure class as a schema-only dump.
# ---------------------------------------------------------------------------
SUM="$(sha256sum "$DUMP" | cut -d' ' -f1)"
printf '%s  %s\n' "$SUM" "wimba-$STAMP.dump" > "$WORK/wimba-$STAMP.dump.sha256"

log "uploading to $BACKUP_REMOTE"
rclone copy "$DUMP" "$BACKUP_REMOTE/" --no-traverse
rclone copy "$WORK/wimba-$STAMP.dump.sha256" "$BACKUP_REMOTE/" --no-traverse

REMOTE_BYTES="$(rclone size "$BACKUP_REMOTE/wimba-$STAMP.dump" --json | sed -n 's/.*"bytes":\([0-9]*\).*/\1/p')"
if [ "$REMOTE_BYTES" != "$BYTES" ]; then
  log "FATAL: uploaded object is $REMOTE_BYTES bytes, expected $BYTES"
  exit 6
fi
log "upload verified: $BYTES bytes, sha256 $SUM"

# ---------------------------------------------------------------------------
# 4. Prune. Never prune before a verified upload — otherwise a broken run both fails to
#    add a backup and removes the old ones.
# ---------------------------------------------------------------------------
log "pruning copies older than ${RETAIN_DAYS}d"
rclone delete "$BACKUP_REMOTE/" --min-age "${RETAIN_DAYS}d" --include 'wimba-*.dump' --include 'wimba-*.dump.sha256'

REMAINING="$(rclone lsf "$BACKUP_REMOTE/" --include 'wimba-*.dump' | wc -l | tr -d ' ')"
log "done — $REMAINING dumps retained"
if [ "$REMAINING" -lt 1 ]; then
  log "FATAL: no dumps remain after prune"
  exit 7
fi
