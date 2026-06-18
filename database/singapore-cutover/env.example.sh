#!/usr/bin/env bash
# Copy to env.sh and fill in. env.sh is gitignored — never commit real connection strings.
#
#   MODE=lab   -> drives the two local Docker Postgres 16 containers (Phase 0 rehearsal)
#   MODE=real  -> drives the actual Neon source/target (Phase 2 cutover). Handle with care.
export MODE=lab

# ---------- lab mode (Docker) ----------
export SRC_CONTAINER=pg_source
export TGT_CONTAINER=pg_target
export LAB_DB=rehearsal
export LAB_USER=postgres
export LAB_PASSWORD=rehearsal

# ---------- real mode (Neon) — fill ONLY when MODE=real ----------
# The owner/admin role MUST be BYPASSRLS (Neon's neondb_owner qualifies). NEVER use app_user:
# the subscription apply worker runs as the subscription owner, and a NOBYPASSRLS owner is
# hard-rejected by the 19 FORCE-RLS tables, stalling the slot and growing WAL on the source (prod).
# export SOURCE_URL='postgresql://neondb_owner:PW@ep-xxx.us-east-1.aws.neon.tech/rcs?sslmode=require'
# export TARGET_URL='postgresql://neondb_owner:PW@ep-yyy.ap-southeast-1.aws.neon.tech/rcs?sslmode=require'
# Connection string the TARGET subscription uses to reach the SOURCE:
# export SOURCE_SUBCONN='host=ep-xxx.us-east-1.aws.neon.tech port=5432 dbname=rcs user=neondb_owner password=PW sslmode=require'
# Failback: connection string the SOURCE subscription uses to reach the TARGET:
# export TARGET_SUBCONN='host=ep-yyy.ap-southeast-1.aws.neon.tech port=5432 dbname=rcs user=neondb_owner password=PW sslmode=require'

# ---------- object names (rarely changed) ----------
export PUB_NAME=mig_pub
export SUB_NAME=mig_sub
export FAILBACK_PUB=failback_pub
export FAILBACK_SUB=failback_sub
