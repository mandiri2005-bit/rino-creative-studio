#!/usr/bin/env bash
# Provision a disposable, PRODUCTION-EQUIVALENT database for the T72 suite.
#
# Production-equivalent means the migration chain runs as `neondb_owner` — a
# NON-superuser that owns the database and carries CREATEROLE + BYPASSRLS,
# matching Neon — and `app_user` is created by migration 0016 exactly as it is
# in production. The suite then connects as NEITHER of them.
#
# Destructive DROP DATABASE / DROP ROLE are confined to this disposable
# cluster. Never point this at production.
#
# Usage:  provision.sh [dbname]
set -euo pipefail

PGBIN="${T72_PGBIN:-/opt/homebrew/opt/postgresql@18/bin}"
PORT="${T72_PGPORT:-55488}"
HOST=127.0.0.1
DB="${1:-${T72_DB:-l2c_t72}}"

export PATH="$PGBIN:$PATH"
export LC_ALL=C LANG=C

sup() { psql -h "$HOST" -p "$PORT" -U postgres -d postgres -v ON_ERROR_STOP=1 "$@"; }

sup -qtAc "
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='neondb_owner') THEN
        CREATE ROLE neondb_owner LOGIN CREATEROLE CREATEDB BYPASSRLS;
    END IF;
END \$\$;" >/dev/null

sup -qtAc "DROP DATABASE IF EXISTS ${DB} WITH (FORCE);" >/dev/null
sup -qtAc "CREATE DATABASE ${DB} OWNER neondb_owner;" >/dev/null

# Roles are cluster-wide and are created by migrations (0016, 0074, 0088). Drop
# them so each provision starts genuinely clean and those migrations do the real
# work instead of finding the roles already present.
#
# 🔴 FAILURES ARE NOT SWALLOWED. An earlier version wrapped this in
#    `EXCEPTION WHEN OTHERS THEN NULL` plus `|| true`, so a role that could not
#    be dropped — because it still owned objects, or held privileges elsewhere —
#    left a CONTAMINATED cluster that still reported "fresh". Every subsequent
#    assertion would then be measuring residue from a previous run. Each drop is
#    attempted, and the POST-CONDITION is verified below; anything left standing
#    aborts the provision loudly.
ROLES="fx_t72_probe fx_t72_operator fx_t72_inheriting fx_rates_writer fx_rates_owner \
       g3_posting_engine g3_birth_definer platform_qc_reaper app_user"

for r in $ROLES; do
    if [ -n "$(sup -qtAc "SELECT 1 FROM pg_roles WHERE rolname='${r}'")" ]; then
        # DROP OWNED must run inside each database the role may own objects in.
        for d in $(sup -qtAc "SELECT datname FROM pg_database WHERE datallowconn AND datname <> 'template0'"); do
            psql -h "$HOST" -p "$PORT" -U postgres -d "$d" -qtAc \
                "DROP OWNED BY ${r} CASCADE" >/dev/null 2>&1 || true
        done
        sup -qtAc "DROP ROLE ${r}" >/dev/null 2>&1 || true
    fi
done

leftover=$(sup -qtAc "
SELECT coalesce(string_agg(rolname, ', ' ORDER BY rolname), '')
  FROM pg_roles WHERE rolname = ANY (string_to_array('$(echo $ROLES | tr -s ' ' ',')', ','));")
if [ -n "${leftover// /}" ]; then
    echo "provision: REFUSING — role(s) survived cleanup: ${leftover}" >&2
    echo "  The cluster is contaminated and would report 'fresh' anyway. Investigate what" >&2
    echo "  still owns objects for those roles, or destroy the cluster:" >&2
    echo "    tests/t72/harness.sh destroy" >&2
    exit 1
fi

echo "postgresql://neondb_owner@${HOST}:${PORT}/${DB}"
