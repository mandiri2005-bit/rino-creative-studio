#!/usr/bin/env bash
# Run AFTER database/migrate.js.
#
# 🔴 THIS SCRIPT MUST NEVER ALTER A NORMATIVE ROLE. An earlier version ran
#    `ALTER ROLE g3_posting_engine LOGIN` so the suite could read through it —
#    that is the harness editing the very contract under test, and it would have
#    made a NOLOGIN assertion unfalsifiable. `fx_rates_owner`,
#    `fx_rates_writer` and `g3_posting_engine` stay exactly as `0088` left them,
#    and the suite asserts all three are still NOLOGIN.
#
# Instead: create a RESTRICTED LOGIN PROBE that owns nothing and holds no
# privilege of its own, and give it SET-only membership of the reader role. It
# reads by doing `SET ROLE g3_posting_engine` — which is also what proves that
# role's SELECT grant is real, rather than the probe's own rights.
#
# The one grant that is genuine environment fidelity:
#   `neondb_owner` -> SET on `app_user`. In Neon, neondb_owner creates and
#   administers app_user, and the EXISTING suite
#   `test_live_g3_topup_quarantine_rejects_cross_tenant_access` does
#   `SET ROLE app_user` from the migration connection. A PG18 CREATEROLE
#   grantor gets ADMIN on roles it creates but NOT `SET`, so a bare local
#   cluster denies it and that test fails for an environment reason rather
#   than a code one. (That it needs this at all is evidence CI runs that suite
#   as a superuser.)
set -euo pipefail

PGBIN="${T72_PGBIN:-/opt/homebrew/opt/postgresql@18/bin}"
export PATH="$PGBIN:$PATH"
export LC_ALL=C LANG=C

DSN="${1:?usage: postmigrate.sh <dsn>}"
PROBE_ROLE="${T72_PROBE_ROLE:-fx_t72_probe}"

psql "$DSN" -v ON_ERROR_STOP=1 -qtAc "
DO \$\$
BEGIN
    -- environment fidelity, see header
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='app_user')
       AND NOT pg_has_role(current_user,'app_user','SET') THEN
        EXECUTE format('GRANT app_user TO %I WITH INHERIT FALSE, SET TRUE', current_user);
    END IF;

    -- the restricted read probe: LOGIN, but owns nothing and inherits nothing
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='${PROBE_ROLE}') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA public FROM ${PROBE_ROLE}';
        EXECUTE 'REVOKE g3_posting_engine FROM ${PROBE_ROLE}';
        EXECUTE 'DROP ROLE ${PROBE_ROLE}';
    END IF;
    EXECUTE 'CREATE ROLE ${PROBE_ROLE} LOGIN NOBYPASSRLS NOSUPERUSER';
    EXECUTE 'GRANT USAGE ON SCHEMA public TO ${PROBE_ROLE}';
    EXECUTE 'GRANT g3_posting_engine TO ${PROBE_ROLE} WITH INHERIT FALSE, SET TRUE';
END \$\$;" >/dev/null

# Post-condition: the three normative roles are untouched and still NOLOGIN. If
# anything in this script ever regresses to altering them, fail HERE rather than
# letting the suite assert against a contract the harness has already edited.
bad=$(psql "$DSN" -qtAc "
SELECT coalesce(string_agg(rolname, ', '), '')
  FROM pg_roles
 WHERE rolname IN ('fx_rates_owner','fx_rates_writer','g3_posting_engine')
   AND (rolcanlogin OR rolsuper OR rolbypassrls);")
if [ -n "${bad// /}" ]; then
    echo "postmigrate: REFUSING — normative role(s) are LOGIN/SUPERUSER/BYPASSRLS: ${bad}" >&2
    echo "  Two possible causes, both failures, and this check cannot tell them apart:" >&2
    echo "    (a) something in the harness altered them — fix the harness; or" >&2
    echo "    (b) 0088 shipped them that way — fix the migration." >&2
    echo "  test_c01c asserts the same property from inside the suite and attributes it to (b)." >&2
    exit 1
fi
