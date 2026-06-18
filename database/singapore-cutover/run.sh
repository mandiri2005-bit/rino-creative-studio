#!/usr/bin/env bash
# Parameterized driver for the Singapore Neon cutover — same commands for the Docker lab (MODE=lab)
# and the real Neon migration (MODE=real). See README.md. Never destructive against real prod
# except `cutover` (drops the subscription) and `failback`; everything else is read/replicate-only.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${REPO:-$(cd "$HERE/../.." && pwd)}"   # database/singapore-cutover -> repo root
# shellcheck disable=SC1091
if [ -f "$HERE/env.sh" ]; then source "$HERE/env.sh"; else source "$HERE/env.example.sh"; fi
# shellcheck disable=SC1091
source "$HERE/lib.sh"

# Lab helper: fully reset a container's db — drop subscriptions (orphan-safe) AND replication slots
# (a reverse-failback leaves a subscription on the source and a slot on the target), then recreate.
_lab_reset_db() {
  local C="$1"
  docker exec "$C" psql -U "$LAB_USER" -d "$LAB_DB" -c "DO \$\$ DECLARE s text; BEGIN FOR s IN SELECT subname FROM pg_subscription LOOP EXECUTE format('ALTER SUBSCRIPTION %I DISABLE', s); EXECUTE format('ALTER SUBSCRIPTION %I SET (slot_name=NONE)', s); EXECUTE format('DROP SUBSCRIPTION %I', s); END LOOP; END \$\$;" >/dev/null 2>&1 || true
  docker exec "$C" psql -U "$LAB_USER" -d postgres -tAc "SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots WHERE active_pid IS NOT NULL;" >/dev/null 2>&1 || true
  docker exec "$C" psql -U "$LAB_USER" -d postgres -tAc "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots;" >/dev/null 2>&1 || true
  docker exec "$C" psql -U "$LAB_USER" -d postgres -c "DROP DATABASE IF EXISTS $LAB_DB WITH (FORCE);" >/dev/null
  docker exec "$C" psql -U "$LAB_USER" -d postgres -c "CREATE DATABASE $LAB_DB;" >/dev/null
}

cmd="${1:-help}"; shift || true
case "$cmd" in

  preflight)
    echo "MODE=$MODE"
    echo "[source] wal_level: $(srcpsql -tAc 'show wal_level')"
    echo "[source] reachable: $(srcpsql -tAc "select 'ok'")"
    echo "[target] reachable: $(tgtpsql -tAc "select 'ok'")"
    echo "[source] sequences needing advance at cutover: $(srcpsql -tAc "select coalesce(string_agg(relname,', ' order by relname),'(none)') from pg_class where relkind='S' and relnamespace='public'::regnamespace")"
    echo "[source] FORCE-RLS tables (apply-worker landmines): $(srcpsql -tAc "select count(*) from pg_class where relrowsecurity and relforcerowsecurity and relkind='r' and relnamespace='public'::regnamespace")"
    if [ "$MODE" = real ]; then
      echo "[target] current_user BYPASSRLS (MUST be t): $(tgtpsql -tAc 'select rolbypassrls from pg_roles where rolname=current_user')"
      echo "[source] max_replication_slots / max_wal_senders: $(srcpsql -tAc "select current_setting('max_replication_slots')||' / '||current_setting('max_wal_senders')")"
    fi
    ;;

  lab-up)
    [ "$MODE" = lab ] || { echo "lab-up only valid in MODE=lab"; exit 1; }
    docker network create pg_rehearsal >/dev/null 2>&1 || true
    for spec in "$SRC_CONTAINER:5433" "$TGT_CONTAINER:5434"; do
      name="${spec%%:*}"; port="${spec##*:}"
      docker rm -f "$name" >/dev/null 2>&1 || true
      docker run -d --name "$name" --network pg_rehearsal \
        -e POSTGRES_PASSWORD="$LAB_PASSWORD" -e POSTGRES_DB="$LAB_DB" -p "$port:5432" postgres:16 \
        -c wal_level=logical -c max_wal_senders=10 -c max_replication_slots=10 >/dev/null
    done
    echo "lab up: $SRC_CONTAINER (source, :5433), $TGT_CONTAINER (target, :5434)"
    ;;

  build-source-lab)
    [ "$MODE" = lab ] || { echo "lab only — in real mode the source is your live Neon"; exit 1; }
    _lab_reset_db "$SRC_CONTAINER"
    docker cp "$REPO/database/migrations" "$SRC_CONTAINER:/migrations" >/dev/null
    docker exec "$SRC_CONTAINER" bash -c "set -e; for f in \$(ls /migrations/0*.sql | sort); do psql -U $LAB_USER -d $LAB_DB -v ON_ERROR_STOP=1 -q -f \"\$f\" >/dev/null; done"
    echo "source built: $(srcpsql -tAc "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'") tables"
    ;;

  seed-lab)
    [ "$MODE" = lab ] || { echo "lab only — real mode gets data via replication"; exit 1; }
    docker cp "$HERE/sql/seed_lab.sql" "$SRC_CONTAINER:/seed_lab.sql" >/dev/null
    docker exec "$SRC_CONTAINER" psql -U "$LAB_USER" -d "$LAB_DB" -v ON_ERROR_STOP=1 -q -f /seed_lab.sql
    echo "seeded source: $(srcpsql -tAc 'select count(*) from credit_ledger') credit_ledger rows, SUM(delta)=$(srcpsql -tAc 'select coalesce(sum(delta),0) from credit_ledger')"
    ;;

  build-target)
    if [ "$MODE" = lab ]; then
      # Lab only: reset the target db for a clean restore (real mode targets a fresh Neon project).
      _lab_reset_db "$TGT_CONTAINER"
      echo "[target db reset]"
    fi
    # Pre-create app_user so the schema restore's GRANT ... TO app_user statements don't abort.
    tgtpsql -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_user') THEN CREATE ROLE app_user LOGIN NOBYPASSRLS; END IF; END \$\$;" >/dev/null
    echo "[app_user ensured on target]"
    if [ "$MODE" = lab ]; then
      docker exec "$SRC_CONTAINER" pg_dump -U "$LAB_USER" -d "$LAB_DB" --schema-only \
        | docker exec -i -e PGOPTIONS='-c client_min_messages=warning' "$TGT_CONTAINER" psql -U "$LAB_USER" -d "$LAB_DB" -v ON_ERROR_STOP=1 -q
    else
      pg_dump "$SOURCE_URL" --schema-only \
        | PGOPTIONS='-c client_min_messages=warning' psql "$TARGET_URL" -v ON_ERROR_STOP=1 -q
    fi
    echo "target schema restored: $(tgtpsql -tAc "select count(*) from information_schema.tables where table_schema='public' and table_type='BASE TABLE'") tables"
    ;;

  replicate)
    srcpsql -v ON_ERROR_STOP=1 -c "DROP PUBLICATION IF EXISTS $PUB_NAME; CREATE PUBLICATION $PUB_NAME FOR ALL TABLES;" >/dev/null
    echo "[source] publication $PUB_NAME created"
    # Orphan-safe teardown of any stale subscription (review #7): disable, detach slot, drop.
    tgtpsql -c "DO \$\$ BEGIN IF EXISTS (SELECT 1 FROM pg_subscription WHERE subname='$SUB_NAME') THEN EXECUTE 'ALTER SUBSCRIPTION $SUB_NAME DISABLE'; EXECUTE 'ALTER SUBSCRIPTION $SUB_NAME SET (slot_name=NONE)'; EXECUTE 'DROP SUBSCRIPTION $SUB_NAME'; END IF; END \$\$;" >/dev/null 2>&1 || true
    tgtpsql -v ON_ERROR_STOP=1 -c "CREATE SUBSCRIPTION $SUB_NAME CONNECTION '$SUBCONN_FROM_TARGET' PUBLICATION $PUB_NAME;"
    echo "[target] subscription $SUB_NAME created"
    echo "REMINDER: apply runs as the subscription owner — it MUST be BYPASSRLS (neondb_owner), never app_user."
    echo "Watch progress:  $0 lag"
    ;;

  lag)      tgtpsql < "$HERE/sql/lag.sql" ;;
  guard)    echo "[guard] zero rows = safe to resume writes:"; tgtpsql < "$HERE/sql/guard.sql" ;;
  advance)  echo "[advance] sequences on target:"; tgtpsql -v ON_ERROR_STOP=1 < "$HERE/sql/advance.sql" ;;

  reconcile)
    echo "================ SOURCE ================"; srcpsql < "$HERE/sql/reconcile.sql"
    echo "================ TARGET ================"; tgtpsql < "$HERE/sql/reconcile.sql"
    echo "(diff the two blocks — every row count and money aggregate must match)"
    ;;

  cutover)
    echo "PRECONDITION: source writes are quiesced (app read-only) and '$0 lag' shows bytes_behind ~0."
    echo "[1/4 guard] sequences behind max (must be empty):"; tgtpsql < "$HERE/sql/guard.sql"
    echo "[2/4 advance] advancing sequences on target:";       tgtpsql -v ON_ERROR_STOP=1 < "$HERE/sql/advance.sql"
    echo "[3/4 reconcile]";                                    "$0" reconcile
    echo "[4/4 drop subscription] target becomes authoritative:"; tgtpsql -v ON_ERROR_STOP=1 -c "DROP SUBSCRIPTION IF EXISTS $SUB_NAME;"
    echo "NEXT: set up failback ($0 failback), repoint app DATABASE_URL/DATABASE_POOL_URL to TARGET, then resume writes."
    ;;

  failback)
    tgtpsql -v ON_ERROR_STOP=1 -c "DROP PUBLICATION IF EXISTS $FAILBACK_PUB; CREATE PUBLICATION $FAILBACK_PUB FOR ALL TABLES;" >/dev/null
    echo "[target] failback publication $FAILBACK_PUB created"
    srcpsql -v ON_ERROR_STOP=1 -c "DROP SUBSCRIPTION IF EXISTS $FAILBACK_SUB;" >/dev/null 2>&1 || true
    srcpsql -v ON_ERROR_STOP=1 -c "CREATE SUBSCRIPTION $FAILBACK_SUB CONNECTION '$SUBCONN_FROM_SOURCE' PUBLICATION $FAILBACK_PUB WITH (copy_data=false);"
    echo "[source] failback subscription $FAILBACK_SUB created (copy_data=false)"
    echo "WARNING: reverse path does NOT carry DDL or advance sequences on source. Before trusting a"
    echo "failback, run '$0 reconcile' and confirm reverse bytes_behind ~0; otherwise post-cutover writes are lost."
    ;;

  lab-down)
    docker rm -f "$SRC_CONTAINER" "$TGT_CONTAINER" >/dev/null 2>&1 || true
    docker network rm pg_rehearsal >/dev/null 2>&1 || true
    echo "lab torn down"
    ;;

  *)
    cat <<EOF
Singapore Neon cutover driver. Edit env.sh (MODE=lab|real), then:

  LAB (Phase 0 rehearsal):
    ./run.sh lab-up                 start two PG16 containers
    ./run.sh build-source-lab       reset source + apply migrations 0001..0028
    ./run.sh seed-lab               load representative tenants/users/credit data
    ./run.sh build-target           dump source schema -> restore target (pre-creates app_user)
    ./run.sh replicate              publication + subscription (forward)
    ./run.sh lag                    watch initial copy / streaming lag
    ./run.sh reconcile              compare source vs target
    ./run.sh cutover                guard -> advance -> reconcile -> drop subscription
    ./run.sh failback               reverse replication target->source
    ./run.sh lab-down               tear down containers

  REAL (Phase 2 cutover): same commands with MODE=real + Neon URLs in env.sh.
  Always: preflight -> build-target -> replicate -> (wait lag~0) -> quiesce writes -> cutover -> failback.
EOF
    ;;
esac
