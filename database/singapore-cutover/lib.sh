#!/usr/bin/env bash
# Connection abstraction sourced by run.sh. Provides srcpsql / tgtpsql and the two subscription
# connection strings, switching transparently between the Docker lab and real Neon.
: "${MODE:=lab}"
: "${PUB_NAME:=mig_pub}"; : "${SUB_NAME:=mig_sub}"
: "${FAILBACK_PUB:=failback_pub}"; : "${FAILBACK_SUB:=failback_sub}"

if [ "$MODE" = lab ]; then
  : "${SRC_CONTAINER:=pg_source}"; : "${TGT_CONTAINER:=pg_target}"
  : "${LAB_DB:=rehearsal}"; : "${LAB_USER:=postgres}"; : "${LAB_PASSWORD:=rehearsal}"
  srcpsql() { docker exec -i "$SRC_CONTAINER" psql -U "$LAB_USER" -d "$LAB_DB" "$@"; }
  tgtpsql() { docker exec -i "$TGT_CONTAINER" psql -U "$LAB_USER" -d "$LAB_DB" "$@"; }
  # CRITICAL: subscription runs INSIDE the subscriber container, so it reaches the other peer by
  # docker-network hostname + internal port 5432 (NOT localhost, NOT the host-published 5433/5434).
  SUBCONN_FROM_TARGET="host=${SRC_CONTAINER} port=5432 user=${LAB_USER} password=${LAB_PASSWORD} dbname=${LAB_DB}"
  SUBCONN_FROM_SOURCE="host=${TGT_CONTAINER} port=5432 user=${LAB_USER} password=${LAB_PASSWORD} dbname=${LAB_DB}"
else
  : "${SOURCE_URL:?MODE=real requires SOURCE_URL}"
  : "${TARGET_URL:?MODE=real requires TARGET_URL}"
  : "${SOURCE_SUBCONN:?MODE=real requires SOURCE_SUBCONN (conn string target uses to reach source)}"
  : "${TARGET_SUBCONN:?MODE=real requires TARGET_SUBCONN (conn string source uses to reach target)}"
  srcpsql() { psql "$SOURCE_URL" "$@"; }
  tgtpsql() { psql "$TARGET_URL" "$@"; }
  SUBCONN_FROM_TARGET="$SOURCE_SUBCONN"
  SUBCONN_FROM_SOURCE="$TARGET_SUBCONN"
fi
