#!/usr/bin/env bash
# T72 harness — provision a disposable PG18 cluster + database, apply the full
# migration chain, and run the suite or the mutation set. Reproducible from a
# clean checkout; nothing here depends on a session scratchpad.
#
#   tests/t72/harness.sh up        start the cluster (idempotent)
#   tests/t72/harness.sh db        provision + migrate; prints the DSN
#   tests/t72/harness.sh test      db, then run the T72 suite
#   tests/t72/harness.sh mutate    db, then run the mutation harness
#   tests/t72/harness.sh down      stop the cluster
#   tests/t72/harness.sh destroy   stop AND delete the data directory
#
# Overridable: T72_PGBIN, T72_PGPORT, T72_PGDATA, T72_DB, T72_NODE_PATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

PGBIN="${T72_PGBIN:-/opt/homebrew/opt/postgresql@18/bin}"
PORT="${T72_PGPORT:-55488}"
PGDATA="${T72_PGDATA:-${TMPDIR:-/tmp}/t72-pg18}"
DB="${T72_DB:-l2c_t72}"
HOST=127.0.0.1
MARKER=".t72-disposable"

export PATH="$PGBIN:$PATH"
export LC_ALL=C LANG=C

DSN="postgresql://neondb_owner@${HOST}:${PORT}/${DB}"
BG_DSN="postgresql://postgres@${HOST}:${PORT}/${DB}"

die() { echo "harness: $*" >&2; exit 1; }

# ── The deletion guard ──────────────────────────────────────────────────────
# T72_PGDATA is an env override, and an earlier version ran `rm -rf` on it with
# no validation at all: one typo or one inherited variable and the harness
# deletes something that matters. Nothing is ever removed unless it passes ALL
# of these, and a directory this harness did not create is never touched.
assert_safe_pgdata() {
    local p="$1"
    case "$p" in
        /*) ;;                       *) die "T72_PGDATA must be an absolute path: $p" ;;
    esac
    case "$p" in
        */..*|*/.) die "T72_PGDATA must not contain '..': $p" ;;
    esac
    [ "$(printf '%s' "$p" | awk -F/ '{print NF-1}')" -ge 2 ] \
        || die "T72_PGDATA is too shallow to be a scratch dir: $p"
    case "$p" in
        /|/Users|/home|/etc|/var|/usr|/opt|/System|/Library|"$HOME") die "refusing: $p" ;;
    esac
    # Must live under a temp root, so a real cluster path can never qualify.
    case "$p" in
        /tmp/*|/private/tmp/*|/var/folders/*|"${TMPDIR%/}"/*) ;;
        *) die "T72_PGDATA must live under a temp directory (/tmp, /private/tmp, \$TMPDIR): $p" ;;
    esac
}

# Only ever delete a directory carrying OUR marker (or an empty/absent one).
rm_pgdata() {
    local p="$1"
    assert_safe_pgdata "$p"
    [ -e "$p" ] || return 0
    [ -d "$p" ] || die "T72_PGDATA exists and is not a directory: $p"
    if [ -n "$(ls -A "$p" 2>/dev/null || true)" ] && [ ! -f "$p/$MARKER" ]; then
        die "refusing to delete $p — it is not empty and carries no $MARKER marker.
     This harness only removes data directories it created itself. Point
     T72_PGDATA somewhere disposable, or delete that directory by hand."
    fi
    rm -rf "$p"
}

# ── node's `pg` driver: resolved, never assumed ─────────────────────────────
resolve_node_path() {
    if [ -n "${T72_NODE_PATH:-}" ]; then
        [ -d "$T72_NODE_PATH/pg" ] || die "T72_NODE_PATH has no 'pg' module: $T72_NODE_PATH"
        echo "$T72_NODE_PATH"; return
    fi
    for cand in "$REPO/backend/node_modules" "$REPO/node_modules"; do
        [ -d "$cand/pg" ] && { echo "$cand"; return; }
    done
    die "cannot find the 'pg' module. database/migrate.js needs it.
     Run \`npm install\` in backend/, or set T72_NODE_PATH to a node_modules
     directory that contains it. (It is deliberately NOT defaulted to another
     checkout on this machine — that made the harness unreproducible.)"
}

up() {
    if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then return 0; fi
    if [ ! -s "$PGDATA/PG_VERSION" ]; then
        rm_pgdata "$PGDATA"
        mkdir -p "$PGDATA"
        command -v initdb >/dev/null || die "initdb not found; set T72_PGBIN (looked in $PGBIN)"
        # 🔴 THE MARKER IS WRITTEN AFTER initdb, NOT BEFORE. initdb refuses a
        # non-empty directory, so creating the marker first made bootstrapping
        # from an empty PGDATA impossible — the cluster could only ever be
        # brought up by hand. On failure the directory this invocation just
        # created is removed, because rm_pgdata verified it was empty or absent
        # moments ago and an unmarked half-initialised directory would then be
        # undeletable by the guard.
        # --locale=C: initdb rejects a macOS shell's default locale settings.
        if ! initdb -D "$PGDATA" -U postgres --auth-local=trust --auth-host=trust \
                    -E UTF8 --locale=C >/dev/null; then
            rm -rf "$PGDATA"
            die "initdb failed; removed the half-created $PGDATA"
        fi
        touch "$PGDATA/$MARKER"
        # unix_socket_directories='': a temp path easily exceeds the 103-byte
        # socket limit, so this cluster is TCP-only by construction.
        cat >> "$PGDATA/postgresql.conf" <<EOF
port = $PORT
listen_addresses = '$HOST'
unix_socket_directories = ''
fsync = off
full_page_writes = off
synchronous_commit = off
max_connections = 60
EOF
    fi
    pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" start >/dev/null
    for _ in $(seq 1 20); do
        psql -h "$HOST" -p "$PORT" -U postgres -d postgres -qtAc "SELECT 1" >/dev/null 2>&1 && return 0
        sleep 0.3
    done
    die "cluster did not come up; see $PGDATA/server.log"
}

db() {
    up
    "$HERE/provision.sh" "$DB" >/dev/null
    ( cd "$REPO" && NODE_PATH="$(resolve_node_path)" NODE_ENV=production PGSSLMODE=disable \
        DATABASE_URL="$DSN" node database/migrate.js >/dev/null )
    "$HERE/postmigrate.sh" "$DSN"
    echo "$DSN"
}

case "${1:-test}" in
    up)      up ;;
    db)      db ;;
    test)    db >/dev/null
             cd "$REPO"
             T72_DSN="$DSN" T72_BREAKGLASS_DSN="$BG_DSN" \
               python3 -m pytest tests/python/test_t72_fx_rates_population_contract.py \
               -p no:cacheprovider "${@:2}" ;;
    mutate)  db >/dev/null
             cd "$REPO"
             T72_DSN="$DSN" T72_BREAKGLASS_DSN="$BG_DSN" T72_HARNESS="$HERE/harness.sh" \
               python3 tests/t72/mutate.py "${@:2}" ;;
    preflight)
             # `0088`'s existing-role preflight cannot be covered by the
             # mutation harness: provision.sh drops the normative roles before
             # every run, so on a fresh cluster there is nothing to reject and
             # deleting the check is a no-op. This exercises the case that
             # actually matters — applying into a database WITH history.
             up
             SCRATCH="${DB}_preflight"
             SUP="postgresql://postgres@${HOST}:${PORT}/postgres"
             psql "$SUP" -qtAc "DROP DATABASE IF EXISTS ${SCRATCH} WITH (FORCE)" >/dev/null
             psql "$SUP" -qtAc "
DO \$\$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fx_rates_owner') THEN
    EXECUTE 'ALTER ROLE fx_rates_owner LOGIN';
  ELSE
    EXECUTE 'CREATE ROLE fx_rates_owner LOGIN NOBYPASSRLS';
  END IF;
END \$\$;" >/dev/null
             psql "$SUP" -qtAc "CREATE DATABASE ${SCRATCH} OWNER neondb_owner" >/dev/null
             set +e
             out=$( cd "$REPO" && NODE_PATH="$(resolve_node_path)" NODE_ENV=production \
                     PGSSLMODE=disable \
                     DATABASE_URL="postgresql://neondb_owner@${HOST}:${PORT}/${SCRATCH}" \
                     node database/migrate.js 2>&1 )
            rc=$?
             set -e
             psql "$SUP" -qtAc "DROP DATABASE IF EXISTS ${SCRATCH} WITH (FORCE)" >/dev/null
             psql "$SUP" -qtAc "ALTER ROLE fx_rates_owner NOLOGIN" >/dev/null 2>&1 || true
             if [ "$rc" -eq 0 ]; then
                 echo "PREFLIGHT FAILED: 0088 applied cleanly while fx_rates_owner already existed as LOGIN." >&2
                 echo "  A pre-existing privileged role was adopted silently." >&2
                 exit 1
             fi
             if ! printf '%s' "$out" | grep -q "role fx_rates_owner is LOGIN"; then
                 echo "PREFLIGHT FAILED: the migration aborted, but not on the NOLOGIN check:" >&2
                 printf '%s\n' "$out" | tail -5 >&2
                 exit 1
             fi
             echo "preflight OK — 0088 refuses a pre-existing LOGIN fx_rates_owner" ;;
    down)    pg_ctl -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true; echo "stopped" ;;
    destroy) pg_ctl -D "$PGDATA" stop -m fast >/dev/null 2>&1 || true
             rm_pgdata "$PGDATA"; echo "destroyed $PGDATA" ;;
    *)       die "usage: harness.sh {up|db|test|mutate|down|destroy}" ;;
esac
