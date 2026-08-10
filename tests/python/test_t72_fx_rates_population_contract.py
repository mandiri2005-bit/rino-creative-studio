"""
T72 — `fx_rates_population_contract`.

NORMATIVE SOURCE: `PLAN-046 §A/S10.G3.3-c`. `MATRIX-046 T72` is the ACCEPTANCE
for that contract, not its specification. Assertions below cite `G3.3-c(n)`.

═══════════════════════════════════════════════════════════════════════════════
VERDICT: **T72 PASS** — Gate 4 CLOSED 2026-08-10.
═══════════════════════════════════════════════════════════════════════════════

`G3.3-c(3)`'s deadlock-freedom proof has two halves. The corrector half — at
most one `fx_rates` row lock per transaction, because the second call fails at
the XID guard before locking — is implemented and executed here. The other half
— the engine takes all its `FOR SHARE` birth locks FIRST, ordered ascending by
`(currency_pair, rate_date)`, and never acquires an `fx_rates` lock after it
begins writing — belongs to the Gate 2/3 birth path. `0089` SHIPPED THAT PATH,
and `test_c11b` now asserts the lock phase against the real `g3_birth_lots`
rather than against a stand-in.

A hand-rolled `SELECT ... FOR UPDATE` holder standing in for that engine would
exercise this file's own code path and nothing else. It would not touch the
birth path, and calling the resulting green a PASS on the correction-vs-birth
race is precisely the "test that cannot fail" this workstream has already paid
for once. So clause (11) was carried as an OPEN DEPENDENCY with a tripwire, and
the row was reported PARTIAL until the engine existed.

🔴 THAT DEPENDENCY IS NOW CLOSED, AND THE VERDICT CHANGED FOR ONE REASON ONLY:
   the identity. `0089`/`0090` shipped the birth path and `0091` made
   `g3_birth_lots` its single executable entrypoint, owned by the NOLOGIN
   boundary role `g3_birth_definer` and reachable only by `g3_posting_engine`.
   Every birth in this suite now runs through a restricted LOGIN probe that
   assumes that role — NOBYPASSRLS, holding EXECUTE on the entrypoint and on
   nothing else, with no table privilege on `credit_lots` at all. The bare probe
   is refused `42501`, and the assertion runs inside the transaction that calls
   the function, so a `SET ROLE` that silently did not take cannot pass unnoticed.

   PARTIAL was never about coverage. It was about the suite measuring the birth
   path through `neondb_owner` — `BYPASSRLS`, privileged on everything, unable to
   fail a privilege check or an RLS policy even in principle. That is what
   changed. Nothing here is a PASS because more tests were written.

═══════════════════════════════════════════════════════════════════════════════
THIS SUITE HARD-FAILS. IT NEVER SKIPS.
═══════════════════════════════════════════════════════════════════════════════

  * `T72_DSN` unset                            -> failure (collection error)
  * `asyncpg` not importable                   -> failure (collection error)
  * the operator connection is superuser       -> failure
  * the operator owns fx_rates / credit_lots   -> failure
  * `credit_lots` lacks ENABLE+FORCE RLS       -> failure
  * `fx_rates_owner` is superuser or BYPASSRLS -> failure

CONNECTIONS, and why there are five. After `0088` the migration role holds NO
privilege on `fx_rates` at all — ownership moved to `fx_rates_owner` and the
temporary membership was revoked (`G3.3-c(8)(v)`). That is the contract working,
and it means read-back and owner-level DML need distinct identities:

  admin      `neondb_owner`      — owns credit_lots, BYPASSRLS; builds fixtures
  operator   `fx_t72_operator`   — restricted; SET-only on fx_rates_writer.
                                   Makes every API call. Holds NO table privilege.
  reader     `g3_posting_engine` — the normative SELECT-only role; every data
                                   read-back goes through it, which also proves
                                   that grant is real
  app        `app_user`          — denial + RLS assertions
  breakglass `postgres`          — the "acknowledged, documented break-glass" of
                                   `G3.3-c(1)`, used ONLY where a test must act
                                   AS the table owner to prove a TRIGGER rather
                                   than a privilege error. Never used for a
                                   privilege assertion; the guards below prove
                                   the assertion identity is not superuser.

ENVIRONMENT (`MATRIX-046 T72` environment cell): PostgreSQL 18 disposable real
database, complete migration chain applied by `database/migrate.js`,
production-equivalent restricted roles, no production credentials.

    T72_DSN=postgresql://neondb_owner@127.0.0.1:55488/l2c_t72 \
    T72_BREAKGLASS_DSN=postgresql://postgres@127.0.0.1:55488/l2c_t72 \
        python3 -m pytest tests/python/test_t72_fx_rates_population_contract.py -q
"""
import asyncio
import datetime
import decimal
import itertools
import json
import os
import pathlib
import time
import uuid

import pytest

try:
    import asyncpg
except ImportError as exc:  # pragma: no cover - environment defect
    raise RuntimeError(
        "T72 PREREQUISITE FAILED: asyncpg is not importable. This is a FAILED run, not a "
        "skip — a privilege contract that silently does not execute is indistinguishable "
        "from one that does not hold."
    ) from exc

ADMIN_DSN = os.getenv("T72_DSN")
if not ADMIN_DSN:
    raise RuntimeError(
        "T72 PREREQUISITE FAILED: T72_DSN is not set. This is a FAILED run, not a skip. "
        "Point it at a DISPOSABLE PostgreSQL 18 database — never at production."
    )
BREAKGLASS_DSN = os.getenv("T72_BREAKGLASS_DSN")
if not BREAKGLASS_DSN:
    raise RuntimeError(
        "T72 PREREQUISITE FAILED: T72_BREAKGLASS_DSN is not set. After 0088 the migration "
        "role holds no privilege on fx_rates (G3.3-c(8)(v) revokes the temporary "
        "membership), so the owner-level trigger tests need the acknowledged break-glass "
        "connection of G3.3-c(1). This is a FAILED run, not a skip."
    )

REPO = pathlib.Path(__file__).resolve().parents[2]

OPERATOR_ROLE = "fx_t72_operator"
INHERITING_ROLE = "fx_t72_inheriting"   # negative control for clause (1)
READER_ROLE = "g3_posting_engine"       # normative, NOLOGIN — never altered
ENGINE_ROLE = READER_ROLE               # same principal: it reads evidence AND posts the lot
PROBE_ROLE = "fx_t72_probe"             # restricted LOGIN probe; SET-only on READER_ROLE
NORMATIVE_NOLOGIN_ROLES = ("fx_rates_owner", "fx_rates_writer", "g3_posting_engine")

# The REAL ops/migration role — the identity `db-backup` connects as. The ops
# membership must exist on THIS role, not on a synthetic one the fixture made.
OPS_ROLE = ADMIN_DSN.split("://", 1)[1].split("@", 1)[0]

USD, JPY = "USD/IDR", "JPY/IDR"

RESTRICT_VIOLATION = "23001"
CHECK_VIOLATION = "23514"
NOT_NULL_VIOLATION = "23502"
INSUFFICIENT_PRIVILEGE = "42501"
LOCK_NOT_AVAILABLE = "55P03"

# fx_rates rows are immutable and undeletable by design, so a suite cannot clean
# up after itself. Each run works in its own date range.
_RUN = uuid.uuid4().hex[:8]
_BASE = datetime.date(2035, 1, 1) + datetime.timedelta(days=uuid.uuid4().int % 12000)


def _d(n: int) -> datetime.date:
    return _BASE + datetime.timedelta(days=n)


def _dsn_as(role: str) -> str:
    head, tail = ADMIN_DSN.split("://", 1)
    return f"{head}://{role}@{tail.split('@', 1)[1]}"


def run(coro):
    return asyncio.run(coro)


async def _connect(dsn):
    return await asyncpg.connect(dsn)


def hard_require(condition, message):
    if not condition:
        raise AssertionError(
            "T72 NON-VACUITY GUARD UNMET -> THIS RUN IS FAILED, NOT SKIPPED: " + message)


async def sqlstate(conn, sql, *args):
    """Run `sql`; return the SQLSTATE it raised, or None if it succeeded."""
    try:
        await conn.execute(sql, *args)
        return None
    except asyncpg.PostgresError as e:
        return e.sqlstate


def _char(v):
    return v.decode() if isinstance(v, (bytes, bytearray)) else v


# ═══════════════════════════════════════════════════════════════════════════
# FIXTURE
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="session", autouse=True)
def provision():
    async def _go():
        admin = await _connect(ADMIN_DSN)
        bg = await _connect(BREAKGLASS_DSN)
        try:
            # Guard BEFORE touching anything: if the operator identity names an
            # existing privileged role the whole suite is vacuous and must fail
            # HERE, with that reason. This is the T79 defect as a precondition.
            for role in (OPERATOR_ROLE, INHERITING_ROLE):
                attrs = await admin.fetchrow(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=$1", role)
                if attrs is not None:
                    hard_require(not attrs["rolsuper"],
                                 f"the configured operator identity {role!r} is a SUPERUSER role. "
                                 "Superuser satisfies every privilege assertion here by "
                                 "definition — this is how T79 reported 35/35 against a "
                                 "SECURITY INVOKER writer.")
                    hard_require(not attrs["rolbypassrls"],
                                 f"the operator identity {role!r} has BYPASSRLS; G3.3-c(4) "
                                 "would pass without any policy existing.")

            for role in (OPERATOR_ROLE, INHERITING_ROLE):
                if await admin.fetchval("SELECT 1 FROM pg_roles WHERE rolname=$1", role):
                    await admin.execute(f"REVOKE ALL ON SCHEMA public FROM {role}")
                    await admin.execute(f"REVOKE fx_rates_writer FROM {role}")
                    await admin.execute(f"DROP ROLE {role}")

            # The operator: SET-only membership — the production shape.
            await admin.execute(f"CREATE ROLE {OPERATOR_ROLE} LOGIN")
            await admin.execute(
                f"GRANT fx_rates_writer TO {OPERATOR_ROLE} WITH INHERIT FALSE, SET TRUE")
            await admin.execute(f"GRANT USAGE ON SCHEMA public TO {OPERATOR_ROLE}")

            # NEGATIVE-CONTROL identity for clause (1): plain inheriting membership.
            await admin.execute(f"CREATE ROLE {INHERITING_ROLE} LOGIN")
            await admin.execute(f"GRANT fx_rates_writer TO {INHERITING_ROLE}")
            await admin.execute(f"GRANT USAGE ON SCHEMA public TO {INHERITING_ROLE}")

            # 🔴 NOTHING HERE MAY ALTER A NORMATIVE ROLE. An earlier version of
            # this fixture ran `ALTER ROLE g3_posting_engine LOGIN` so reads
            # could go through it directly — the suite editing the contract it
            # is asserting about, which would have made
            # test_c01c's NOLOGIN check unfalsifiable. Reads now go through the
            # restricted `fx_t72_probe`, which postmigrate.sh creates and which
            # ASSUMES the reader role. `bg` is used only where a test must act
            # AS the table owner to prove a trigger rather than a privilege
            # error.
            for role in NORMATIVE_NOLOGIN_ROLES:
                canlogin = await bg.fetchval(
                    "SELECT rolcanlogin FROM pg_roles WHERE rolname=$1", role)
                hard_require(canlogin is False,
                             f"{role} is LOGIN before the suite has asserted anything. Something "
                             "in the harness altered a normative role; fix that rather than "
                             "asserting against an edited contract.")
        finally:
            await admin.close()
            await bg.close()

        op = await _connect(_dsn_as(OPERATOR_ROLE))
        try:
            row = await op.fetchrow(
                "SELECT current_user AS cu, "
                "  (SELECT rolsuper     FROM pg_roles WHERE rolname=current_user) AS is_super, "
                "  (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user) AS bypass")
            hard_require(not row["is_super"],
                         f"the operator connection is SUPERUSER ({row['cu']}).")
            hard_require(not row["bypass"], f"the operator ({row['cu']}) has BYPASSRLS.")

            owners = await op.fetchrow(
                "SELECT pg_get_userbyid(relowner) AS fx_owner, "
                "  (SELECT pg_get_userbyid(relowner) FROM pg_class "
                "    WHERE oid='public.credit_lots'::regclass) AS lots_owner "
                "  FROM pg_class WHERE oid='public.fx_rates'::regclass")
            hard_require(owners["fx_owner"] != row["cu"], "the operator OWNS fx_rates.")
            hard_require(owners["lots_owner"] != row["cu"], "the operator OWNS credit_lots.")

            rls = await op.fetchrow(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                " WHERE oid='public.credit_lots'::regclass")
            hard_require(rls["relrowsecurity"] and rls["relforcerowsecurity"],
                         "credit_lots lacks ENABLE+FORCE RLS; clause (13) would prove nothing.")

            fxo = await op.fetchrow(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='fx_rates_owner'")
            hard_require(fxo is not None, "role fx_rates_owner does not exist.")
            hard_require(not fxo["rolsuper"], "fx_rates_owner is SUPERUSER.")
            hard_require(not fxo["rolbypassrls"],
                         "fx_rates_owner has BYPASSRLS; the credit_lots policy would be "
                         "bypassed and clause (13) would pass without it existing.")
        finally:
            await op.close()

    run(_go())
    yield


# ── Helpers ─────────────────────────────────────────────────────────────────
async def _writer(conn):
    await conn.execute("SET ROLE fx_rates_writer")


async def _as_owner(bg):
    await bg.execute("SET ROLE fx_rates_owner")


async def _reader():
    """Read-back identity.

    The normative reader `g3_posting_engine` is NOLOGIN and STAYS NOLOGIN — the
    harness must never alter a role the suite is asserting about. So a
    restricted probe logs in holding nothing of its own and assumes the reader
    role. Everything this connection can see is therefore attributable to
    `g3_posting_engine`'s SELECT grant, which is exactly what makes reading
    through it evidence rather than convenience.
    """
    conn = await _connect(_dsn_as(PROBE_ROLE))
    await conn.execute(f"SET ROLE {READER_ROLE}")
    return conn


class _EngineConnection:
    """The birth path's connection, pinned to the REAL `g3_posting_engine`.

    🔴 WHY THIS TYPE EXISTS. Every birth in this suite used to run on
       `ADMIN_DSN` — the migration role, which is `BYPASSRLS` and holds
       privileges on everything. That connection cannot fail a privilege check
       or an RLS policy, so the whole birth path was being exercised by an
       identity that production never used, and `T72` was PARTIAL for exactly
       that reason. `0091` gives the engine role the minimum it needs; this
       makes the suite actually use it.

       The identity is a SESSION property (`SET ROLE` persists until reset), so
       re-checking it on every call is not ceremony: it is what makes the
       assertion non-vacuous. If the `SET ROLE` had silently not taken, or a
       test reset it, every downstream assertion would quietly go back to being
       measured against a superuser-ish role and still pass.

       The check runs in the SAME transaction as the call it guards. Some tests
       drive `BEGIN`/`COMMIT` by hand, so an unconditional `transaction()` here
       would open a nested one — instead, an already-open transaction is joined
       and only an autocommit call gets a transaction of its own.
    """

    _BIRTH = "g3_birth_lots"

    def __init__(self, conn):
        self._conn = conn

    def __getattr__(self, name):          # everything else passes straight through
        return getattr(self._conn, name)

    async def _assert_identity(self):
        row = await self._conn.fetchrow(
            "SELECT current_user AS cu, session_user AS su, "
            "  (SELECT rolsuper     FROM pg_roles WHERE rolname=current_user) AS is_super, "
            "  (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user) AS bypass")
        assert row["cu"] == ENGINE_ROLE, (
            f"the birth path is about to run as {row['cu']!r}, not {ENGINE_ROLE!r}. "
            "A birth executed by any other identity proves nothing about the engine's "
            "privileges or about the RLS policies that admit it.")
        assert row["su"] == PROBE_ROLE, (
            f"session_user is {row['su']!r}, not the restricted probe {PROBE_ROLE!r}. The engine "
            "identity must be REACHED BY `SET ROLE` from a role that holds nothing of its own — "
            "if something logged in as the engine directly, `g3_posting_engine` is no longer "
            "NOLOGIN and the contract has already been edited.")
        assert row["is_super"] is False, f"{row['cu']} is SUPERUSER; every check below is vacuous."
        assert row["bypass"] is False, (
            f"{row['cu']} has BYPASSRLS; the `credit_lots` policies `0091` adds would never be "
            "consulted and the RLS half of the birth contract would pass untested.")

    async def _guarded(self, method, sql, *args):
        if self._BIRTH not in sql:
            return await method(sql, *args)
        if self._conn.is_in_transaction():
            await self._assert_identity()
            return await method(sql, *args)
        async with self._conn.transaction():
            await self._assert_identity()
            return await method(sql, *args)

    async def fetch(self, sql, *a):    return await self._guarded(self._conn.fetch, sql, *a)
    async def fetchrow(self, sql, *a): return await self._guarded(self._conn.fetchrow, sql, *a)
    async def fetchval(self, sql, *a): return await self._guarded(self._conn.fetchval, sql, *a)
    async def execute(self, sql, *a):  return await self._guarded(self._conn.execute, sql, *a)

    async def close(self):
        await self._conn.close()


async def _as_definer():
    """The BOUNDARY identity, for tests that must reach a helper directly.

    🔴 WHY THESE TESTS CANNOT USE `admin` ANY MORE. `0091` revoked PUBLIC
       EXECUTE on `g3_lock_fx_refs` and `g3_fx_rate_at`, which are owned by
       `fx_rates_owner` — and `neondb_owner` reached them THROUGH that PUBLIC
       grant, not by ownership. So the migration role lost them too, which is
       the tightening working as intended rather than a regression.

       The code-level guards inside `g3_write_lot_resolved` are still worth
       proving — they are the defence in depth behind the privilege wall — but
       they can only be reached by the one identity that is allowed to: the
       boundary owner. Break-glass is the documented way to assume a NOLOGIN
       role, and this is exactly the case the suite reserves it for.
    """
    conn = await _connect(BREAKGLASS_DSN)
    await conn.execute("SET ROLE g3_birth_definer")
    return conn


async def _engine():
    """The birth path, as the identity production actually posts with."""
    conn = await _connect(_dsn_as(PROBE_ROLE))
    await conn.execute(f"SET ROLE {ENGINE_ROLE}")
    engine = _EngineConnection(conn)
    await engine._assert_identity()
    return engine


async def _enter(conn, pair, rate_date, rate, source="jisdor", ref=None):
    return await conn.fetchrow(
        "SELECT * FROM public.fx_rates_enter($1,$2,$3,$4,$5)",
        pair, rate_date, rate, source, ref or f"publication {rate_date}")


async def _correct(conn, pair, rate_date, new_rate, reason,
                   new_source="jisdor", new_ref=None):
    return await conn.fetchrow(
        "SELECT * FROM public.fx_rates_correct($1,$2,$3,$4,$5,$6)",
        pair, rate_date, new_rate, new_source,
        new_ref or f"revised publication {rate_date}", reason)


async def _rate(reader, pair, rate_date):
    return await reader.fetchval(
        "SELECT idr_per_major_unit FROM public.fx_rates "
        " WHERE currency_pair=$1 AND rate_date=$2", pair, rate_date)


async def _make_tenant(admin, label):
    tid = uuid.uuid4()
    await admin.execute(
        "INSERT INTO public.tenants(id,name,slug,email) VALUES ($1,$2,$3,$4)",
        tid, label, f"{label}-{tid.hex}", f"{label}-{tid.hex}@t72.invalid")
    return tid


async def _make_valued_lot(admin, tenant_id, pair, rate_date):
    """A committed, VALUED lot referencing a rate — G3.3-c(6)'s valued shape."""
    await admin.execute(
        "INSERT INTO public.credit_lots"
        "(tenant_id,source,credits_granted,credits_remaining,acquired_at,"
        " is_priced,valuation_status,src_currency,fx_currency_pair,fx_rate_date,"
        " dpp_total_idr,price_per_credit_idr,consideration_minor,consideration_currency) "
        "VALUES ($1,'topup',100,100,now(),true,'valued',$2,$3,$4,1000,10,905,$2)",
        tenant_id, pair.split("/")[0], pair, rate_date)


# ═══════════════════════════════════════════════════════════════════════════
# (1) ROLE GATE — G3.3-c(1)
# ═══════════════════════════════════════════════════════════════════════════
def test_c01_role_gate_and_writer_holds_no_table_privilege():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        try:
            r = await op.fetchrow(
                "SELECT pg_has_role($1,'fx_rates_writer','USAGE') AS w_usage, "
                "       pg_has_role($1,'fx_rates_writer','SET')   AS w_set, "
                "       pg_has_role($1,'fx_rates_owner','MEMBER') AS o_member, "
                "       pg_has_role($1,'fx_rates_owner','USAGE')  AS o_usage", OPERATOR_ROLE)
            assert r["w_usage"] is False, "writer privileges must not be ambient"
            assert r["w_set"] is True, "the operator must be able to SET ROLE to the writer"
            assert r["o_member"] is False and r["o_usage"] is False

            # NEGATIVE CONTROL: plain inheriting membership shows USAGE=true.
            bad = await op.fetchval(
                "SELECT pg_has_role($1,'fx_rates_writer','USAGE')", INHERITING_ROLE)
            assert bad is True, (
                "negative control broken: plain membership must show USAGE=true, else "
                "clause (1) cannot tell the two grant shapes apart")

            # 🔴 G3.3-c(1): fx_rates_writer holds NO TABLE PRIVILEGE AT ALL.
            for table in ("public.fx_rates", "public.fx_rates_corrections",
                          "public.fx_kmk_periods", "public.credit_lots"):
                for verb in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    held = await op.fetchval(
                        "SELECT has_table_privilege('fx_rates_writer',$1,$2)", table, verb)
                    assert held is False, (
                        f"fx_rates_writer must hold NO table privilege; found {verb} on {table}")

            # NEGATIVE CONTROL: it DOES hold EXECUTE, so the sweep above is not
            # reporting false for a role that simply has nothing.
            ex = await op.fetchval(
                "SELECT bool_or(has_function_privilege('fx_rates_writer',p.oid,'EXECUTE')) "
                "  FROM pg_proc p WHERE p.proname='fx_rates_enter' "
                "   AND p.pronamespace='public'::regnamespace")
            assert ex is True, "fx_rates_writer must hold EXECUTE on the API"

            # Before SET ROLE: no EXECUTE.
            state = await sqlstate(
                op, "SELECT public.fx_rates_enter($1,$2,16000,'jisdor','R')", USD, _d(1))
            assert state == INSUFFICIENT_PRIVILEGE, f"expected 42501 before SET ROLE, got {state}"

            await _writer(op)
            assert (await _enter(op, USD, _d(1), 16000))["outcome"] == "entered"

            sch = await op.fetchrow(
                "SELECT has_schema_privilege('fx_rates_owner','public','USAGE')  AS usage, "
                "       has_schema_privilege('fx_rates_owner','public','CREATE') AS create_")
            assert sch["create_"] is False, "fx_rates_owner must hold no standing CREATE"
            assert sch["usage"] is True, (
                "negative control: USAGE must REMAIN, else 'no CREATE' would be satisfied "
                "by the role having no schema access at all")
        finally:
            await op.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (1a) THE OPS MEMBERSHIP IS REAL — G3.3-c(1) and G3.3-c(9)
#
# The operational path is `db-backup` doing `SET ROLE fx_rates_writer`. That
# requires a PERMANENT `GRANT fx_rates_writer TO <ops role> WITH INHERIT FALSE,
# SET TRUE` on the ACTUAL connected role. Asserting it on a role the fixture
# invented would prove only that the fixture can grant things.
# ═══════════════════════════════════════════════════════════════════════════
def test_c01a_ops_role_holds_real_set_only_writer_membership():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        try:
            usage = await op.fetchval(
                "SELECT pg_has_role($1,'fx_rates_writer','USAGE')", OPS_ROLE)
            can_set = await op.fetchval(
                "SELECT pg_has_role($1,'fx_rates_writer','SET')", OPS_ROLE)
            assert can_set is True, (
                f"the REAL ops role {OPS_ROLE!r} cannot SET ROLE fx_rates_writer. The migration "
                "must grant this permanently — otherwise fx_rates_entry.py only ever works "
                "against a synthetic fixture operator and production db-backup is refused.")
            assert usage is False, (
                f"{OPS_ROLE!r} INHERITS fx_rates_writer; the SET ROLE gate is fictional and the "
                "API is callable ambiently by every session that role opens")

            # The grants' own options, not just their effects.
            #
            # There can be MORE THAN ONE membership row: pg_auth_members is keyed
            # by (roleid, member, grantor), so the creator's automatic ADMIN
            # grant sits alongside the explicit one the migration makes. The
            # effective capability is the OR across them, so aggregate — reading
            # a single arbitrary row can pick the admin grant and report set=f
            # while SET ROLE actually works.
            opts = await op.fetchrow(
                "SELECT count(*) AS n, bool_or(m.set_option) AS any_set, "
                "       bool_or(m.inherit_option) AS any_inherit "
                "  FROM pg_auth_members m "
                " WHERE m.roleid='fx_rates_writer'::regrole AND m.member=$1::regrole", OPS_ROLE)
            assert opts["n"] > 0, f"no fx_rates_writer membership row for {OPS_ROLE!r}"
            assert opts["any_set"] is True, (
                f"no grant of fx_rates_writer to {OPS_ROLE!r} carries SET; the operational "
                "path cannot assume the writer role")
            assert opts["any_inherit"] is False, (
                f"a grant of fx_rates_writer to {OPS_ROLE!r} carries INHERIT — the privilege "
                "is ambient and the SET ROLE gate is fictional")

            # NEGATIVE CONTROL: the fixture's INHERITING_ROLE does carry INHERIT,
            # so `any_inherit` is a live discriminator and not false for every
            # member of this role.
            ctl = await op.fetchval(
                "SELECT bool_or(m.inherit_option) FROM pg_auth_members m "
                " WHERE m.roleid='fx_rates_writer'::regrole AND m.member=$1::regrole",
                INHERITING_ROLE)
            assert ctl is True, (
                "negative control broken: the plain-membership role must show INHERIT")

            # END-TO-END: the real ops role can actually drive the API. This is
            # the production path, not a fixture rehearsal.
            await admin.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            await admin.execute("SET LOCAL ROLE fx_rates_writer")
            row = await admin.fetchrow(
                "SELECT * FROM public.fx_rates_enter($1,$2,16000,'jisdor','ops path')",
                USD, _d(2))
            await admin.execute("COMMIT")
            assert row["outcome"] == "entered"

            # NEGATIVE CONTROL: without the SET ROLE the very same connection is
            # refused, so the grant above is doing the work and not some ambient
            # privilege the ops role already had.
            state = await sqlstate(
                admin, "SELECT public.fx_rates_enter($1,$2,16000,'jisdor','no set role')",
                USD, _d(2))
            assert state == INSUFFICIENT_PRIVILEGE, (
                f"the ops role executed the API WITHOUT SET ROLE (got {state}) — the writer "
                "privilege is ambient, which is the failure this gate exists to prevent")
        finally:
            await op.close()
            await admin.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (1c) THE NORMATIVE ROLES ARE STILL NOLOGIN — G3.3-c(1)
#
# `fx_rates_owner` and `fx_rates_writer` are NOLOGIN so they can only ever be
# ASSUMED, never connected to directly; `g3_posting_engine` likewise. An earlier
# harness ran `ALTER ROLE g3_posting_engine LOGIN` so the suite could read
# through it — the harness editing the contract under test. This asserts the
# shape the migration shipped, so that can never pass silently again.
# ═══════════════════════════════════════════════════════════════════════════
def test_c01c_normative_roles_remain_nologin_and_unprivileged():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        try:
            for role in NORMATIVE_NOLOGIN_ROLES:
                r = await op.fetchrow(
                    "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
                    "  FROM pg_roles WHERE rolname=$1", role)
                assert r is not None, f"{role} does not exist"
                assert r["rolcanlogin"] is False, (
                    f"{role} is LOGIN. G3.3-c(1) ships it NOLOGIN so it can only be assumed. "
                    "If a harness altered it to read through it, the harness is editing the "
                    "contract under test.")
                assert r["rolsuper"] is False, f"{role} is SUPERUSER"
                assert r["rolbypassrls"] is False, f"{role} has BYPASSRLS"
                assert r["rolcreaterole"] is False, f"{role} has CREATEROLE"
                assert r["rolcreatedb"] is False, f"{role} has CREATEDB"

            # NEGATIVE CONTROL: the probe IS a login role, so `rolcanlogin` is a
            # live discriminator here and not false for every role in the cluster.
            assert await op.fetchval(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname=$1", PROBE_ROLE) is True, \
                "negative control broken: the probe role must be LOGIN"

            # And the probe brings nothing of its own — everything the reader
            # connection can see is attributable to g3_posting_engine.
            for table in ("public.fx_rates", "public.fx_rates_corrections",
                          "public.fx_kmk_periods", "public.credit_lots"):
                for verb in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert await op.fetchval(
                        "SELECT has_table_privilege($1,$2,$3)", PROBE_ROLE, table, verb) is False, \
                        f"the probe role holds {verb} on {table} in its own right"
            assert await op.fetchval(
                "SELECT pg_has_role($1,$2,'USAGE')", PROBE_ROLE, READER_ROLE) is False, \
                "the probe must not INHERIT the reader role; it assumes it explicitly"
            assert await op.fetchval(
                "SELECT pg_has_role($1,$2,'SET')", PROBE_ROLE, READER_ROLE) is True
        finally:
            await op.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (1b) G3.3-c(8)(v) — the temporary owner membership is REVOKED
# ═══════════════════════════════════════════════════════════════════════════
def test_c01b_temporary_owner_membership_revoked_after_transfer():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        try:
            # The migration role must not retain an ASSUMABLE path into the
            # owner identity: the temporary WITH SET TRUE of G3.3-c(8)(iii) has
            # to be gone by (v), or every migration run would leave a standing
            # way to become fx_rates_owner and the three-role split would be
            # decoration.
            migration_role = ADMIN_DSN.split("://", 1)[1].split("@", 1)[0]
            for priv in ("SET", "USAGE"):
                held = await op.fetchval(
                    "SELECT pg_has_role($1,'fx_rates_owner',$2)", migration_role, priv)
                assert held is False, (
                    f"the migration role {migration_role!r} still holds {priv} on "
                    "fx_rates_owner; G3.3-c(8)(v) requires that temporary grant be revoked")

            # `MEMBER` deliberately is NOT asserted false. On PG16+ a CREATEROLE
            # role that CREATES a role keeps an automatic ADMIN membership, and
            # ADMIN implies MEMBER. That grant is an artifact of role creation,
            # not the temporary grant (iii) made — and a role holding ADMIN can
            # always re-grant itself SET, which is precisely the "acknowledged,
            # documented break-glass" of G3.3-c(1). What matters is that no
            # capability is STANDING, so the grant's own options are asserted.
            opts = await op.fetchrow(
                "SELECT m.admin_option, m.inherit_option, m.set_option "
                "  FROM pg_auth_members m "
                " WHERE m.roleid='fx_rates_owner'::regrole AND m.member=$1::regrole",
                migration_role)
            if opts is not None:
                assert opts["set_option"] is False, (
                    "the residual membership still carries SET — that is the temporary "
                    "capability, and G3.3-c(8)(v) revokes it")
                assert opts["inherit_option"] is False, (
                    "the residual membership INHERITS, so owner rights are ambient")

            # NEGATIVE CONTROL: the migration role is still a real, privileged
            # role elsewhere, so the assertions above are not trivially true of
            # a role that simply has nothing.
            owns_lots = await op.fetchval(
                "SELECT pg_get_userbyid(relowner)=$1 FROM pg_class "
                " WHERE oid='public.credit_lots'::regclass", migration_role)
            assert owns_lots is True, (
                "negative control broken: the migration role should still own credit_lots")

        finally:
            await op.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (2) FUNCTION CONTRACT — all THREE, exact signatures. G3.3-c(2)
# ═══════════════════════════════════════════════════════════════════════════
FN_PREDICATE = """
    SELECT p.proname, pg_get_userbyid(p.proowner) AS owner, p.prosecdef,
           p.provolatile, p.proparallel, p.proconfig,
           pg_get_function_arguments(p.oid) AS args
      FROM pg_proc p
     WHERE p.pronamespace='public'::regnamespace AND p.proname = $1
"""

NORMATIVE_ARGS = {
    "fx_rates_enter":
        "p_pair text, p_rate_date date, p_rate numeric, p_source text, p_source_ref text",
    "fx_rates_correct":
        "p_pair text, p_rate_date date, p_new_rate numeric, p_new_source text, "
        "p_new_source_ref text, p_reason text",
}


def _pins_search_path(proconfig):
    if not proconfig:
        return False
    for entry in proconfig:
        if entry.startswith("search_path="):
            return [p.strip() for p in entry.split("=", 1)[1].split(",")] == \
                   ["pg_catalog", "pg_temp"]
    return False


def test_c02_function_contract_all_three_and_exact_signatures():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        try:
            for name in ("fx_rates_enter", "fx_rates_correct", "fx_rates_reject_mutation"):
                r = await op.fetchrow(FN_PREDICATE, name)
                assert r is not None, f"{name} does not exist"
                assert r["owner"] == "fx_rates_owner", f"{name}: proowner={r['owner']}"
                assert r["prosecdef"] is True, f"{name} is not SECURITY DEFINER"
                assert _char(r["provolatile"]) == "v", f"{name} is not VOLATILE"
                assert _char(r["proparallel"]) == "u", f"{name} is not PARALLEL UNSAFE"
                assert _pins_search_path(r["proconfig"]), (
                    f"{name}: search_path not pinned to pg_catalog,pg_temp "
                    f"(proconfig={r['proconfig']}) — `public` is deliberately excluded")

            # 🔴 EXACT normative signatures. The earlier draft shipped a 9-arg
            # enter() and a 4-arg correct(); both are wrong against G3.3-c(2),
            # and correct() must be able to amend source and source_ref too.
            for name, expected in NORMATIVE_ARGS.items():
                got = (await op.fetchrow(FN_PREDICATE, name))["args"]
                assert got == expected, f"{name} signature\n  expected: {expected}\n  got:      {got}"

            # G3.3-c(7): the rename is a RENAME. `idr_per_usd` must be GONE —
            # a second column carrying the same quantity is the silent-divergence
            # hazard the currency-generic schema removes.
            cols = [r["attname"] for r in await op.fetch(
                "SELECT attname FROM pg_attribute WHERE attrelid='public.fx_rates'::regclass "
                " AND attnum>0 AND NOT attisdropped")]
            assert "idr_per_major_unit" in cols, "the renamed column is absent"
            assert "idr_per_usd" not in cols, (
                "idr_per_usd still exists — G3.3-c(7) requires a RENAME, not an added column")

            # NEGATIVE CONTROLS: three functions, each with exactly ONE defect.
            await admin.execute(f"""
                CREATE FUNCTION public.t72_ctl_invoker_{_RUN}() RETURNS INT
                LANGUAGE sql SET search_path = pg_catalog, pg_temp AS 'SELECT 1';
                CREATE FUNCTION public.t72_ctl_unpinned_{_RUN}() RETURNS INT
                LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';
                CREATE FUNCTION public.t72_ctl_publicpath_{_RUN}() RETURNS INT
                LANGUAGE sql SECURITY DEFINER SET search_path = public, pg_temp AS 'SELECT 1';
            """)
            try:
                assert (await admin.fetchrow(FN_PREDICATE, f"t72_ctl_invoker_{_RUN}"))["prosecdef"] \
                    is False, "negative control broken: SECURITY INVOKER must fail prosecdef"
                assert not _pins_search_path(
                    (await admin.fetchrow(FN_PREDICATE, f"t72_ctl_unpinned_{_RUN}"))["proconfig"]), \
                    "negative control broken: an unpinned search_path must fail"
                assert not _pins_search_path(
                    (await admin.fetchrow(FN_PREDICATE, f"t72_ctl_publicpath_{_RUN}"))["proconfig"]), \
                    "negative control broken: `public` in search_path must fail"
            finally:
                await admin.execute(
                    f"DROP FUNCTION IF EXISTS public.t72_ctl_invoker_{_RUN}();"
                    f"DROP FUNCTION IF EXISTS public.t72_ctl_unpinned_{_RUN}();"
                    f"DROP FUNCTION IF EXISTS public.t72_ctl_publicpath_{_RUN}();")
        finally:
            await op.close()
            await admin.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (3) ISOLATION GUARD — G3.3-c(3)
# ═══════════════════════════════════════════════════════════════════════════
def test_c03_repeatable_read_raises_fx003():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        try:
            await _writer(op)
            d = _d(3)
            await _enter(op, USD, d, 16000)

            await op.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
            state = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','typo')", USD, d)
            await op.execute("ROLLBACK")
            assert state == "FX003", f"expected FX003 under REPEATABLE READ, got {state}"

            # NEGATIVE CONTROL: READ COMMITTED is NOT FX003.
            await op.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            state2 = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','typo')", USD, d)
            await op.execute("ROLLBACK")
            assert state2 != "FX003", "negative control broken: READ COMMITTED must not raise FX003"
        finally:
            await op.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (4) DIRECT-DML DENIAL — per verb, per role, SEPARATELY. G3.3-c(1)/(2)
# ═══════════════════════════════════════════════════════════════════════════
def test_c04_direct_dml_denied_per_verb_and_role():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        app = await _connect(_dsn_as("app_user"))
        try:
            for role in (OPERATOR_ROLE, "app_user", "g3_posting_engine", "fx_rates_writer"):
                for verb in ("INSERT", "UPDATE", "DELETE"):
                    for table in ("public.fx_rates", "public.fx_rates_corrections",
                                  "public.fx_kmk_periods"):
                        granted = await op.fetchval(
                            "SELECT has_table_privilege($1,$2,$3)", role, table, verb)
                        assert granted is False, f"{role} must not hold {verb} on {table}"

            # NEGATIVE CONTROL: SELECT *is* granted to the reader roles.
            for role in ("app_user", "g3_posting_engine"):
                assert await op.fetchval(
                    "SELECT has_table_privilege($1,'public.fx_rates','SELECT')", role) is True, \
                    f"negative control broken: {role} must retain SELECT on fx_rates"

            # The denial is real, not merely catalogued.
            state = await sqlstate(
                app,
                "INSERT INTO public.fx_rates(currency_pair,rate_date,idr_per_major_unit,"
                "source,source_ref,entered_by) VALUES ($1,$2,1,'manual','x','y')", USD, _d(4))
            assert state == INSUFFICIENT_PRIVILEGE, f"expected 42501, got {state}"

            # EXECUTE denial. 0016's ALTER DEFAULT PRIVILEGES writes an EXPLICIT
            # app_user grant, so a REVOKE naming only PUBLIC leaves it standing.
            for fn in ("fx_rates_enter", "fx_rates_correct", "fx_rates_reject_mutation"):
                for role in ("app_user", "g3_posting_engine"):
                    ok = await op.fetchval(
                        "SELECT bool_or(has_function_privilege($1,p.oid,'EXECUTE')) FROM pg_proc p "
                        " WHERE p.proname=$2 AND p.pronamespace='public'::regnamespace", role, fn)
                    assert ok is False, f"{role} must not hold EXECUTE on {fn}"
        finally:
            await op.close()
            await app.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (5) AUDIT COLUMNS — G3.3-c(2)
# ═══════════════════════════════════════════════════════════════════════════
def test_c05_audit_columns_not_parameters_and_record_session_user():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        bg = await _connect(BREAKGLASS_DSN)
        try:
            args = await op.fetchval(
                "SELECT pg_get_function_arguments(p.oid) FROM pg_proc p "
                " WHERE p.proname='fx_rates_enter' AND p.pronamespace='public'::regnamespace")
            assert "entered_by" not in args and "created_at" not in args, \
                f"created_at/entered_by must not be parameters: {args}"

            await _writer(op)
            d = _d(5)
            await _enter(op, USD, d, 16000)

            row = await reader.fetchrow(
                "SELECT entered_by, created_at FROM public.fx_rates "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, d)
            assert row["entered_by"] == OPERATOR_ROLE, (
                f"entered_by must record session_user ({OPERATOR_ROLE}), got {row['entered_by']}")
            assert row["entered_by"] != "fx_rates_writer", (
                "entered_by recorded current_user — under SET ROLE that is uniformly "
                "fx_rates_writer and useless for audit (G3.3-c(2), lesson -033)")
            assert row["entered_by"] != "fx_rates_owner", "entered_by recorded the definer role"
            assert row["created_at"] is not None

            # NEGATIVE CONTROL: current_user really IS the writer role here, so
            # the inequality above is a live discriminator, not a tautology.
            assert await op.fetchval("SELECT current_user") == "fx_rates_writer"

            # Direct UPDATE raises — as the OWNER, so it is the trigger and not
            # a privilege error (G3.3-c(1) break-glass).
            await _as_owner(bg)
            where = "WHERE currency_pair=$1 AND rate_date=$2"
            for col, val in (("entered_by", "'someone_else'"), ("created_at", "now()")):
                s = await sqlstate(bg, f"UPDATE public.fx_rates SET {col}={val} {where}", USD, d)
                assert s == RESTRICT_VIOLATION, f"UPDATE of {col} must raise, got {s}"
        finally:
            await op.close()
            await reader.close()
            await bg.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (6) IDEMPOTENCY — G3.3-c(2)
# ═══════════════════════════════════════════════════════════════════════════
def test_c06_idempotent_on_conflict_with_readback_compare():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        try:
            await _writer(op)
            d = _d(6)
            await _enter(op, USD, d, 16000)
            assert (await _enter(op, USD, d, 16000))["outcome"] == "entered", \
                "an identical re-entry is a no-op, not an error"
            assert await reader.fetchval(
                "SELECT count(*) FROM public.fx_rates WHERE currency_pair=$1 AND rate_date=$2",
                USD, d) == 1, "re-entry must not create a second row"

            state = await sqlstate(
                op, "SELECT public.fx_rates_enter($1,$2,17000,'jisdor','R')", USD, d)
            assert state == "FX006", f"expected FX006 on a divergent re-entry, got {state}"

            # NEGATIVE CONTROL: the stored value is unchanged.
            assert await _rate(reader, USD, d) == 16000, "divergent re-entry mutated the rate"
        finally:
            await op.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (7) NORMALISATION — per ONE major unit. G3.3-c(7)
#     The arithmetic lives in python/ops/fx_rates_entry.py, because the
#     normative fx_rates_enter takes an ALREADY-normalised rate.
# ═══════════════════════════════════════════════════════════════════════════
def test_c07_normalisation_is_per_one_major_unit():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "fx_rates_entry", REPO / "python" / "ops" / "fx_rates_entry.py")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)

    from decimal import Decimal

    # Worked example, per-1 series: ((16050 + 15950)/2) / 1 = 16000.
    assert ops.normalize_bi_mid(16050, 15950, 1, echo=False) == Decimal("16000.000000")
    # Per-100 series (the JPY shape): ((155000 + 154000)/2) / 100 = 1545.
    assert ops.normalize_bi_mid(155000, 154000, 100, echo=False) == Decimal("1545.000000")

    # NEGATIVE CONTROL: the UNNORMALISED mid is what you get by skipping Nilai,
    # and it differs by exactly the quotation unit — this is the error the
    # rename and this helper exist to prevent.
    unnormalised = (Decimal("155000") + Decimal("154000")) / 2
    assert unnormalised == Decimal("154500")
    assert unnormalised == ops.normalize_bi_mid(155000, 154000, 100, echo=False) * 100

    for bad in ((16050, 15950, 0), (16050, 15950, -1), (0, 15950, 1), (16050, -1, 1)):
        with pytest.raises(ops.OperatorError):
            ops.normalize_bi_mid(*bad, echo=False)

    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        try:
            await _writer(op)
            # The normalised figure is what reaches the database.
            await _enter(op, JPY, _d(7), ops.normalize_bi_mid(155000, 154000, 100, echo=False),
                         source="bi_transaction_mid", ref="BI transaction rates")
            assert await _rate(reader, JPY, _d(7)) == 1545
        finally:
            await op.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (8) KMK EFFECTIVE PERIOD
# ═══════════════════════════════════════════════════════════════════════════
def test_c08_kmk_source_ref_interval_contains_rate_date():
    async def _go():
        bg = await _connect(BREAKGLASS_DSN)
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        try:
            kmk_1, kmk_2 = f"KMK-01/KM.10/{_RUN}", f"KMK-02/KM.10/{_RUN}"
            await _as_owner(bg)
            await bg.execute(
                "INSERT INTO public.fx_kmk_periods(kmk_ref,valid_from,valid_to) "
                "VALUES ($1,$2,$3),($4,$5,$6)",
                kmk_1, _d(20), _d(26), kmk_2, _d(27), _d(33))

            await _writer(op)
            await _enter(op, USD, _d(22), 16200, source="kmk_tax", ref=kmk_1)
            assert await reader.fetchval(
                "SELECT source_ref FROM public.fx_rates "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, _d(22)) == kmk_1

            # NEGATIVE CONTROL: the same date citing the KMK that does NOT cover
            # it is refused. Both exist, so this tests CONTAINMENT.
            state = await sqlstate(
                op, "SELECT public.fx_rates_enter($1,$2,16200,'kmk_tax',$3)", USD, _d(23), kmk_2)
            assert state == "FX005", f"a non-covering KMK must raise FX005, got {state}"

            # ...and the covering one for that same date IS accepted.
            await _enter(op, USD, _d(23), 16200, source="kmk_tax", ref=kmk_1)
            assert await _rate(reader, USD, _d(23)) == 16200
        finally:
            await bg.close()
            await op.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (9) rate_date VS PUBLICATION — G3.3-c(5)
#     The two live in SEPARATE COLUMNS: rate_date (the day valued) and
#     source_ref (which NAMES the underlying publication, and may carry a
#     different date). There is no publication_date column — an earlier draft
#     invented one.
# ═══════════════════════════════════════════════════════════════════════════
def test_c09_rate_date_and_source_ref_are_separate_columns():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        try:
            await _writer(op)
            base = _d(40)
            saturday = base + datetime.timedelta(days=(5 - base.weekday()) % 7)
            friday = saturday - datetime.timedelta(days=1)
            assert saturday.weekday() == 5 and friday.weekday() == 4

            # Fallback alpha: a Saturday grant resolves to Friday's publication.
            ref = f"JISDOR publication {friday.isoformat()}"
            await _enter(op, USD, saturday, 16300, source="jisdor", ref=ref)

            row = await reader.fetchrow(
                "SELECT rate_date, source_ref FROM public.fx_rates "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, saturday)
            assert row["rate_date"] == saturday, "rate_date must be the day VALUED"
            assert friday.isoformat() in row["source_ref"], \
                "source_ref must name the Friday publication"
            assert friday.isoformat() not in row["rate_date"].isoformat(), \
                "merging the publication date into rate_date is what this clause forbids"

            # NEGATIVE CONTROL: they are genuinely independent — a DIFFERENT
            # rate_date can cite the SAME publication, which is impossible if
            # one column were doing both jobs.
            await _enter(op, USD, saturday + datetime.timedelta(days=1), 16300,
                         source="jisdor", ref=ref)
            same_ref = await reader.fetchval(
                "SELECT count(DISTINCT rate_date) FROM public.fx_rates WHERE source_ref=$1", ref)
            assert same_ref == 2, (
                "negative control broken: two rate_dates must be able to cite one publication")

            # There is no publication_date column.
            assert await reader.fetchval(
                "SELECT count(*) FROM pg_attribute WHERE attrelid='public.fx_rates'::regclass "
                " AND attname='publication_date'") == 0, \
                "publication_date is not in the normative schema; source_ref names the publication"
        finally:
            await op.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (10) CORRECTION BEFORE / AFTER USE — G3.3-c(3)
#      Correction amends rate, source AND source_ref.
# ═══════════════════════════════════════════════════════════════════════════
def test_c10_correction_before_use_ok_after_use_fx002_and_fk_restrict():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        admin = await _connect(ADMIN_DSN)
        bg = await _connect(BREAKGLASS_DSN)
        try:
            await _writer(op)
            unused, used = _d(50), _d(51)
            await _enter(op, USD, unused, 16000, ref="original publication")
            await _enter(op, USD, used, 16000)

            # BEFORE USE: succeeds, and amends ALL THREE amendable columns.
            res = await _correct(op, USD, unused, 16111, "digit transposed",
                                 new_source="manual", new_ref="corrected publication")
            assert res["old_rate"] == 16000 and res["new_rate"] == 16111

            after = await reader.fetchrow(
                "SELECT idr_per_major_unit, source, source_ref FROM public.fx_rates "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, unused)
            assert after["idr_per_major_unit"] == 16111
            assert after["source"] == "manual", "a correction must be able to amend source"
            assert after["source_ref"] == "corrected publication", \
                "a correction must be able to amend source_ref"

            logged = await reader.fetchrow(
                "SELECT old_rate,new_rate,old_source,new_source,old_source_ref,new_source_ref,"
                "       corrected_by FROM public.fx_rates_corrections "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, unused)
            assert logged is not None, "an accepted correction must leave an audit row"
            assert logged["corrected_by"] == OPERATOR_ROLE, "audit must attribute to session_user"
            assert logged["old_source"] == "jisdor" and logged["new_source"] == "manual"
            assert logged["old_source_ref"] == "original publication"

            # AFTER USE -> FX002.
            tenant_a = await _make_tenant(admin, "t72a")
            await _make_valued_lot(admin, tenant_a, USD, used)
            state = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16222,'jisdor','r','typo')", USD, used)
            assert state == "FX002", f"correcting a USED rate must raise FX002, got {state}"

            # NEGATIVE CONTROL: the unused rate is still correctable.
            assert (await _correct(op, USD, unused, 16333, "second"))["new_rate"] == 16333

            # DELETE of a referenced rate is refused...
            await _as_owner(bg)
            assert await sqlstate(
                bg, "DELETE FROM public.fx_rates WHERE currency_pair=$1 AND rate_date=$2",
                USD, used) is not None, "deleting a referenced rate must be refused"

            # ...and the FK named by the contract is genuinely RESTRICT. The row
            # trigger refuses every DELETE first, so the FK can never be the
            # OBSERVED refusal — its behaviour is asserted in the catalogue.
            for conname, table in (("credit_lots_fx_rate_fk", "public.credit_lots"),
                                   ("fx_rates_corrections_rate_fk", "public.fx_rates_corrections")):
                deltype = await op.fetchval(
                    "SELECT confdeltype FROM pg_constraint "
                    " WHERE conname=$1 AND conrelid=$2::regclass AND contype='f'", conname, table)
                assert deltype is not None, f"FK {conname} is absent"
                assert _char(deltype) == "r", f"{conname} must be ON DELETE RESTRICT"
        finally:
            await op.close()
            await reader.close()
            await admin.close()
            await bg.close()

    run(_go())

# ═══════════════════════════════════════════════════════════════════════════
# (11) CORRECTION-VS-BIRTH RACE — the real two-transaction test.
#
# `0089` shipped the birth path, so this is no longer a tripwire. The engine
# (`g3_birth_lots`) takes EVERY fx_rates `FOR SHARE` lock first, in one ordered
# statement, before any write; the corrector (`fx_rates_correct`) holds at most
# ONE `fx_rates` lock per transaction because a second call fails at the XID
# guard BEFORE locking. Single-holder versus ordered-acquirer cannot cycle.
#
# Every assertion below runs against the REAL engine. Nothing is simulated with
# a hand-rolled `FOR UPDATE` holder.
#
# 🔴 ONE HONEST LIMIT, RECORDED RATHER THAN GLOSSED. These tests invoke the real
#    FUNCTION path but under `ADMIN_DSN` (`neondb_owner`), NOT under
#    `g3_posting_engine`. That is the real code path, not the real production
#    IDENTITY. It cannot be otherwise yet: `g3_posting_engine` holds no
#    privilege on `credit_lots` or `g3_provider_payments`, and `g3_birth_lots`
#    is not SECURITY DEFINER — wiring the engine identity is Gate 3/4 work
#    ("T79 39/39 through real roles"). Until then, no claim here should be read
#    as "the production engine identity was exercised".
# ═══════════════════════════════════════════════════════════════════════════
async def _birth_request(tenant, pair, provider_payment_id, op_id):
    """One g3_birth_lots request that WILL reference an FX rate."""
    return {
        "tenant": str(tenant), "source": "topup", "credits": 100,
        "ledger_op_id": op_id, "provenance_kind": "grant",
        "provider": "dodo", "provider_payment_id": provider_payment_id,
        "attested_at": None,
    }


async def _seed_payment(admin, provider_payment_id, when, currency="USD", fee_minor=1000):
    """A g3_provider_payments aggregate the birth path can value from."""
    # 0086's g3_pp_pin_coherent requires payment_at to travel with its pin
    # evidence, and g3_pp_channel_tax_owner_coherent only admits the two real
    # (channel, tax owner) pairs. The fixture obeys the shipped constraints
    # rather than working around them.
    await admin.execute(
        "INSERT INTO public.g3_provider_payments"
        "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
        " payment_at_pinned_at, supplier_fee_minor, supplier_fee_currency,"
        " sales_channel, tax_owner, provider_tax_minor) "
        "VALUES ('dodo',$1,$2,'t72_fixture',now(),$3,$4,'dodo_mor','provider',0) "
        "ON CONFLICT (provider, provider_payment_id) DO NOTHING",
        provider_payment_id, when, fee_minor, currency)
    # Decision 3A: valuation reads the BALANCE LEDGER, not the settlement-derived
    # `supplier_fee_minor` column. A zero-amount `payment_fees` entry is the
    # explicit zero-fee evidence the admission rules require, so this default
    # fixture yields anchor == fee_minor and the older assertions still hold.
    await _seed_ledger(admin, provider_payment_id,
                       [("payment", fee_minor), ("payment_fees", 0)], currency)


async def _seed_ledger(admin, provider_payment_id, entries, currency="USD"):
    """Balance Ledger entries for one payment. `entries` = [(event_type, minor)]."""
    for i, (event_type, minor) in enumerate(entries):
        await admin.execute(
            "INSERT INTO public.g3_provider_balance_ledger"
            "(provider, provider_payment_id, entry_id, event_type, amount_minor,"
            " currency, raw_entry) VALUES ('dodo',$1,$2,$3,$4,$5,$6::jsonb) "
            "ON CONFLICT (provider, entry_id) DO NOTHING",
            provider_payment_id, f"{provider_payment_id}:{i}:{event_type}",
            event_type, minor, currency,
            json.dumps({"event_type": event_type, "amount": minor, "currency": currency}))


def test_c11_correction_vs_birth_race_against_the_real_engine():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))          # corrector
        engine = await _engine()                   # the real birth path
        # The conflicting FOR UPDATE must come from an identity that HAS one.
        # After 0088 the migration role holds only REFERENCES on fx_rates, so the
        # probe uses the acknowledged break-glass and assumes fx_rates_owner.
        prober = await _connect(BREAKGLASS_DSN)              # negative control
        admin = await _connect(ADMIN_DSN)
        reader = await _reader()
        try:
            rate_a, rate_b = _d(60), _d(61)
            await _writer(op)
            await _enter(op, USD, rate_a, 16000)
            await _enter(op, USD, rate_b, 16000)

            tenant = await _make_tenant(admin, "t72race")
            pay_b = f"pay_b_{_RUN}"
            # payment_at drives fx_rate_date, so pin it to rate_b's day.
            await _seed_payment(
                admin, pay_b,
                datetime.datetime.combine(rate_b, datetime.time(12, 0),
                                          tzinfo=datetime.timezone.utc))

            # ── txn-E: the ENGINE begins a birth that references rate B. Its
            #    ordered FOR SHARE lock is held open, uncommitted. ────────────
            await engine.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            born = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pay_b, f"op_b_{_RUN}")]))
            assert len(born) == 1, "the engine must have produced a lot"
            assert born[0]["fx_rate_date"] == rate_b, (
                f"the birth must reference rate B; got {born[0]['fx_rate_date']} "
                f"(valuation_status={born[0]['valuation_status']})")
            assert born[0]["valuation_status"] == "valued"

            # NEGATIVE CONTROL 1: the engine's FOR SHARE genuinely conflicts
            # with a corrector's FOR UPDATE. Without this, an FX001 below could
            # be explained by there being no lock at all.
            await _as_owner(prober)
            await prober.execute("BEGIN")
            await prober.execute("SET LOCAL lock_timeout = '400ms'")
            blocked = await sqlstate(
                prober,
                "SELECT 1 FROM public.fx_rates WHERE currency_pair=$1 AND rate_date=$2 FOR UPDATE",
                USD, rate_b)
            await prober.execute("ROLLBACK")
            assert blocked == LOCK_NOT_AVAILABLE, (
                f"negative control broken: the engine's FOR SHARE on rate B must block a "
                f"FOR UPDATE (expected 55P03, got {blocked}). If it does not, this whole "
                "test is vacuous.")

            # ── txn-A: the CORRECTOR. Corrects A (succeeds), manipulates the
            #    withdrawn custom GUC, then targets B under a short
            #    lock_timeout. The XID guard must fire BEFORE it ever tries to
            #    lock B, so the answer is FX001 and never 55P03. ─────────────
            await op.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            await op.execute("SET LOCAL lock_timeout = '400ms'")
            await op.execute(
                "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','first')", USD, rate_a)
            # The guard must not rest on any caller-writable state.
            await op.execute("SET LOCAL app.fx_correction_done = ''")
            await op.execute("RESET app.fx_correction_done")

            started = time.monotonic()
            state = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16200,'jisdor','r','second')",
                USD, rate_b)
            waited = time.monotonic() - started
            await op.execute("ROLLBACK")

            assert state != LOCK_NOT_AVAILABLE, (
                "55P03: the corrector BLOCKED on the engine's FOR SHARE, which means it tried "
                "to lock rate B before checking the one-correction-per-transaction guard. "
                "That ordering is what the deadlock-freedom argument depends on.")
            assert state == "FX001", f"expected FX001 from the XID guard, got {state}"
            assert waited < 0.4, (
                f"the guard answered in {waited*1000:.0f} ms against a 400 ms lock_timeout — "
                "it should not have waited on a lock at all")
            print(f"\n[T72.11] FX001 in {waited*1000:.0f} ms against a 400 ms lock_timeout, "
                  f"with the engine holding rate B FOR SHARE")

            # ── The FINALITY proof under real concurrency: once the engine
            #    COMMITS its lot, a FRESH corrector sees it and refuses. ──────
            await engine.execute("COMMIT")
            fresh = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16200,'jisdor','r','after birth')",
                USD, rate_b)
            assert fresh == "FX002", (
                f"after the birth committed, correcting its rate must raise FX002, got {fresh}")

            # NEGATIVE CONTROL 2: rate A, which no lot references, is still
            # correctable — so FX002 tracked the committed reference and not
            # merely "a correction was attempted after an engine ran".
            ok = await _correct(op, USD, rate_a, 16123, "still unused")
            assert ok["new_rate"] == 16123

            # And the lot really does reference rate B in the database. Read via
            # `admin`, not the reader probe: `g3_posting_engine` holds SELECT on
            # the FX tables, never on credit_lots.
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots "
                " WHERE fx_currency_pair=$1 AND fx_rate_date=$2", USD, rate_b) == 1
        finally:
            for c in (engine, prober):
                try:
                    await c.execute("ROLLBACK")
                except Exception:
                    pass
            for c in (op, engine, prober, admin, reader):
                await c.close()

    run(_go())


def test_c11b_engine_takes_every_fx_lock_first_and_in_order():
    """The ordered-acquirer half of `G3.3-c(3)`, asserted structurally.

    Two properties, and neither is provable by watching a single-rate birth:
      * the engine locks EVERY rate it will reference BEFORE it writes anything;
      * it acquires them ordered ascending by `(currency_pair, rate_date)`.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        blocker = await _connect(BREAKGLASS_DSN)   # needs a real FOR UPDATE; see c11
        try:
            await _writer(op)
            # Three rates, deliberately seeded so the natural request order is
            # the REVERSE of the required lock order.
            d1, d2, d3 = _d(130), _d(131), _d(132)
            for d in (d1, d2, d3):
                await _enter(op, USD, d, 16000)

            tenant = await _make_tenant(admin, "t72order")
            reqs = []
            for i, d in enumerate((d3, d2, d1)):          # reverse order on purpose
                pid = f"pay_ord_{i}_{_RUN}"
                await _seed_payment(
                    admin, pid,
                    datetime.datetime.combine(d, datetime.time(12, 0),
                                              tzinfo=datetime.timezone.utc))
                reqs.append(await _birth_request(tenant, USD, pid, f"op_ord_{i}_{_RUN}"))

            # A corrector holds the LOWEST-ordered rate. If the engine locked in
            # request order (d3, d2, d1) it would take d3 and d2 first and only
            # then block on d1 — with rows already written. Because it locks
            # everything up front, it blocks BEFORE writing anything at all.
            await _as_owner(blocker)
            await blocker.execute("BEGIN")
            await blocker.execute(
                "SELECT 1 FROM public.fx_rates WHERE currency_pair=$1 AND rate_date=$2 FOR UPDATE",
                USD, d1)

            await engine.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            await engine.execute("SET LOCAL lock_timeout = '500ms'")
            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(reqs))
            assert state == LOCK_NOT_AVAILABLE, (
                f"the engine should have blocked on the held rate (expected 55P03, got {state})")

            # 🔴 THE POINT: it blocked in the LOCK phase, so NOTHING was written.
            # A writer that locked lazily would have committed lots for d3 and d2
            # before ever reaching d1.
            await engine.execute("ROLLBACK")
            await blocker.execute("ROLLBACK")
            written = await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant)
            assert written == 0, (
                f"{written} lot(s) were written before the engine reached its last lock — the "
                "lock phase does not precede the write phase")

            # NEGATIVE CONTROL: with nothing held, the very same batch succeeds
            # and writes all three, so the failure above was the lock and not a
            # malformed request.
            await engine.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(reqs))
            await engine.execute("COMMIT")
            assert len(rows) == 3, f"expected 3 lots, got {len(rows)}"
            assert {r["fx_rate_date"] for r in rows} == {d1, d2, d3}
            assert all(r["valuation_status"] == "valued" for r in rows)

            # The ordering itself, read off the shipped SQL: ORDER BY must
            # precede FOR SHARE inside the one locking statement.
            src = (REPO / "database" / "migrations" /
                   "0089_g3_birth_path_ordered_fx_locks.sql").read_text(encoding="utf-8")
            body = src[src.index("CREATE OR REPLACE FUNCTION public.g3_lock_fx_refs"):]
            body = body[:body.index("$$;")]
            order_at = body.index("ORDER BY f.currency_pair, f.rate_date")
            share_at = body.index("FOR SHARE")
            assert order_at < share_at, (
                "the lock statement must ORDER BY (currency_pair, rate_date) before FOR SHARE; "
                "unordered acquisition is what lets two engines deadlock against each other")

            # And the writer takes no FX lock of its own.
            wbody = src[src.index("CREATE OR REPLACE FUNCTION public.g3_write_lot_resolved"):]
            wbody = wbody[:wbody.index("$$;")]
            assert "FOR SHARE" not in wbody and "FOR UPDATE" not in wbody, (
                "g3_write_lot_resolved acquires a row lock; the write phase must never take an "
                "fx_rates lock after the lock phase has ended")

            # The engine's phases are in the right order, and the lock phase is
            # actually invoked. Asserted structurally BECAUSE it cannot be
            # observed from outside: a lazily-locking engine still ends up
            # blocked — the FK check takes its own KEY SHARE lock at INSERT time
            # — and the whole transaction rolls back either way, so "no rows
            # written" looks identical. The difference is WHEN the lock is
            # taken, and only the source shows that.
            ebody = src[src.index("CREATE OR REPLACE FUNCTION public.g3_birth_lots"):]
            ebody = ebody[:ebody.index("$$;")]
            lock_call = ebody.find("public.g3_lock_fx_refs")
            verify_at = ebody.find("v_locked <> v_required")
            write_at = ebody.find("g3_write_lot_resolved")
            assert lock_call != -1, (
                "g3_birth_lots never calls g3_lock_fx_refs — there is no lock phase, and the "
                "only locks taken would be whatever the FK check happens to acquire mid-write")
            assert verify_at != -1, (
                "g3_birth_lots never compares the locked count with the required count — a "
                "missing published rate would go undetected and a lot would be born unpriced")
            assert lock_call < verify_at < write_at, (
                "the order must be lock -> verify -> write inside g3_birth_lots; verifying "
                "after the first write is too late to stall")

            # The required count must be derived from the REFERENCES, before the
            # lock, and never re-derived from the lock result afterwards.
            # `v_required := v_locked` would make the comparison tautological and
            # the STALL unreachable — and the defect would still LOOK correct,
            # because the writer's own backstop raises FX007 too. Only the
            # engine's check is "before any write"; the writer's is after some.
            required_at = ebody.find("INTO v_required")
            assert required_at != -1 and required_at < lock_call, (
                "v_required must be computed from the reference set BEFORE the lock phase")
            assert "v_required :=" not in ebody[lock_call:], (
                "v_required is re-assigned after the lock; the completeness comparison is "
                "then tautological and a missing rate would never STALL in the engine")

            # The engine reads the MATERIALISED vector in the write phase, not
            # the requests it was handed.
            assert "jsonb_array_elements(v_resolved)" in ebody, (
                "the write phase must iterate the resolved vector; iterating p_requests again "
                "would re-derive terms that were never locked")

            # The corrector's finality proof is a REAL fresh-snapshot read, not
            # a constant. Also structural: with `v_used := FALSE` the backstop
            # trigger still raises FX002, so the observable behaviour is
            # unchanged and only the source distinguishes defence-in-depth from
            # the finality claim `G3.3-c(3)` actually makes.
            fx88 = (REPO / "database" / "migrations" /
                    "0088_fxpop_controlled_fx_entry.sql").read_text(encoding="utf-8")
            cbody = fx88[fx88.index("CREATE OR REPLACE FUNCTION public.fx_rates_correct"):]
            lock_at = cbody.index("FOR UPDATE")
            exists_at = cbody.index("SELECT EXISTS (")
            fx002_at = cbody.index("USING ERRCODE = 'FX002'")
            assert lock_at < exists_at < fx002_at, (
                "the credit_lots finality test must be its own statement AFTER the FOR UPDATE "
                "and before the FX002 raise")
        finally:
            for c in (engine, blocker):
                try:
                    await c.execute("ROLLBACK")
                except Exception:
                    pass
            for c in (op, engine, admin, blocker):
                await c.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (11c) STALL SEMANTICS AND LOCK-SET STABILITY
#
# Four controls, each for a way the birth path can be quietly wrong while every
# other test still passes.
# ═══════════════════════════════════════════════════════════════════════════
def test_c11c_missing_rate_stalls_and_writes_no_lot():
    """`e4` = α STALL: no rate ⇒ NO LOT. Not an `unknown` lot."""
    async def _go():
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            tenant = await _make_tenant(admin, "t72stall")
            day = _d(200)                       # deliberately NO rate entered
            pid = f"pay_stall_{_RUN}"
            await _seed_payment(
                admin, pid,
                datetime.datetime.combine(day, datetime.time(12, 0),
                                          tzinfo=datetime.timezone.utc))
            req = [await _birth_request(tenant, USD, pid, f"op_stall_{_RUN}")]

            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert state == "FX007", (
                f"a missing published rate must STALL with FX007, got {state}. Writing an "
                "`unknown` lot instead is the direct inverse of T62 — a missing input would "
                "become a silently unpriced balance.")

            # 🔴 AND NOTHING WAS WRITTEN.
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0, \
                "a lot was born despite the STALL"

            # NEGATIVE CONTROL: enter the rate, replay, and the SAME request now
            # succeeds — so FX007 tracked the absent rate and nothing else.
            op = await _connect(_dsn_as(OPERATOR_ROLE))
            try:
                await _writer(op)
                await _enter(op, USD, day, 16000)
            finally:
                await op.close()

            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert len(rows) == 1 and rows[0]["valuation_status"] == "valued"
            assert rows[0]["fx_rate_date"] == day
        finally:
            await engine.close()
            await admin.close()

    run(_go())


def test_c11d_non_usd_stalls_even_when_a_matching_rate_exists():
    """`D22`=B is USD only. The control is that the EUR rate EXISTS."""
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(210)
            await _writer(op)
            # Both rates present. Only the currency rule can refuse now.
            await _enter(op, USD, day, 16000)
            await _enter(op, "EUR/IDR", day, 17500)

            tenant = await _make_tenant(admin, "t72eur")
            pid = f"pay_eur_{_RUN}"
            await _seed_payment(
                admin, pid,
                datetime.datetime.combine(day, datetime.time(12, 0),
                                          tzinfo=datetime.timezone.utc),
                currency="EUR")
            req = [await _birth_request(tenant, "EUR/IDR", pid, f"op_eur_{_RUN}")]

            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert state == "FX008", (
                f"a non-USD paid request must STALL with FX008, got {state}. An EUR/IDR row "
                "exists, so anything that priced this lot did so by treating a currency the "
                "contract does not support as if it were supported.")
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0

            # NEGATIVE CONTROL: the identical shape in USD is accepted, so FX008
            # tracked the CURRENCY and not the fixture.
            pid_usd = f"pay_usd_{_RUN}"
            await _seed_payment(
                admin, pid_usd,
                datetime.datetime.combine(day, datetime.time(12, 0),
                                          tzinfo=datetime.timezone.utc),
                currency="USD")
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pid_usd, f"op_usd_{_RUN}")]))
            assert len(rows) == 1 and rows[0]["valuation_status"] == "valued"
        finally:
            await op.close()
            await engine.close()
            await admin.close()

    run(_go())


def test_c11e_rate_date_is_asia_jakarta_not_utc():
    """A payment at 17:30 UTC belongs to the NEXT Jakarta day (WIB = UTC+7)."""
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            utc_day = _d(220)
            wib_day = utc_day + datetime.timedelta(days=1)
            # 17:30 UTC == 00:30 WIB the following day. Straddling, on purpose.
            instant = datetime.datetime.combine(
                utc_day, datetime.time(17, 30), tzinfo=datetime.timezone.utc)

            await _writer(op)
            await _enter(op, USD, utc_day, 16000)      # the WRONG day, deliberately

            tenant = await _make_tenant(admin, "t72wib")
            pid = f"pay_wib_{_RUN}"
            await _seed_payment(admin, pid, instant)
            req = [await _birth_request(tenant, USD, pid, f"op_wib_{_RUN}")]

            # With only the UTC day's rate present, a WIB-correct engine STALLS.
            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert state == "FX007", (
                f"expected a STALL: only the UTC day's rate exists, and 17:30 UTC belongs to "
                f"{wib_day} in Jakarta. Got {state} — the engine is deriving the rate date in "
                "UTC and would value this lot at a rate that was never in force for it.")

            # Enter the WIB day's rate and it succeeds, on THAT date.
            await _enter(op, USD, wib_day, 16500)
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert rows[0]["fx_rate_date"] == wib_day, (
                f"fx_rate_date must be the Jakarta day {wib_day}, got {rows[0]['fx_rate_date']}")

            # NEGATIVE CONTROL: an instant safely inside the Jakarta day maps to
            # that same day, so the helper is not simply always adding one.
            assert await admin.fetchval(
                "SELECT public.g3_wib_rate_date($1)",
                datetime.datetime.combine(utc_day, datetime.time(3, 0),
                                          tzinfo=datetime.timezone.utc)) == utc_day
            assert await admin.fetchval("SELECT public.g3_wib_rate_date($1)", instant) == wib_day
        finally:
            await op.close()
            await engine.close()
            await admin.close()

    run(_go())


def test_c11f_terms_committed_after_resolution_do_not_reach_the_writer():
    """The anchor must be STALE-PROOF across a real concurrent ledger change.

    The hazard is specifically `NULL -> 'USD'`, committed by another session
    AFTER the engine resolved and BEFORE it writes. Under READ COMMITTED every
    statement takes a fresh snapshot, so a writer that re-read
    `g3_provider_payments` would see terms that were never resolved, derive an
    FX reference that was never in the lock set, and price the lot against a
    rate nothing is holding `FOR SHARE` — while `T72(11)`'s "all locks first"
    claim quietly became false.

    An earlier version of this control ran the hazard BACKWARDS: it handed the
    writer a USD vector while the table said NULL. That direction is satisfied
    by a writer that merely PREFERS the vector, and it would still pass one that
    falls back to the live table whenever the vector is empty — which is exactly
    the failure being guarded against. This drives NULL -> USD, with a real
    second session and a real commit in between.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)     # session A: resolves, then writes
        other = await _connect(ADMIN_DSN)     # session B: changes the terms
        defn = await _as_definer()            # the only identity that may call the helper
        try:
            day = _d(230)
            await _writer(op)
            await _enter(op, USD, day, 16000)          # a rate DOES exist for that day
            tenant = await _make_tenant(admin, "t72terms")
            pid = f"pay_terms_{_RUN}"
            instant = datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.timezone.utc)

            # The aggregate and its ledger: payment 1000 - fees 95 => anchor 905.
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),'dodo_mor','provider',0)",
                pid, instant)
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 95)])

            # ── SESSION A, phase 1: resolve while the terms are still NULL. ──
            resolved = json.loads(await admin.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_terms_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid})))
            assert int(resolved["fee_minor"]) == 905, "precondition: the vector anchors at 905"

            # ── SESSION B: append a tax entry and COMMIT. A FRESH resolution
            #    would now anchor at 1000 - 95 - 55 = 850. ──────────────────────
            await other.execute(
                "INSERT INTO public.g3_provider_balance_ledger"
                "(provider, provider_payment_id, entry_id, event_type, amount_minor,"
                " currency, raw_entry) "
                "VALUES ('dodo',$1,$1||':late-tax','tax',55,'USD','{}'::jsonb)", pid)
            live = json.loads(await other.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_probe_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid})))
            assert int(live["fee_minor"]) == 850, (
                "control setup: after session B commits, a FRESH resolution must differ from the "
                "vector — otherwise the two paths are indistinguishable")

            # ── SESSION A, phase 3: write from the OLD vector. ───────────────
            row_state = await sqlstate(
                defn, "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)",
                json.dumps(resolved))

            # 🔴 NEITHER anchor may be booked. Writing 905 banks a figure the
            # provider has since contradicted — a 55 OVERSTATEMENT — and writing
            # 850 would mean the writer re-read evidence that was never in the
            # lock set. The only correct outcome is to refuse and re-resolve.
            assert row_state == "FX012", (
                f"expected FX012 (ledger moved between resolve and write), got {row_state}. "
                "Booking the resolved 905 here is a 55 overstatement, not snapshot stability.")
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1 AND ledger_op_id=$2",
                tenant, f"op_terms_{_RUN}") == 0, "a lot was born from a stale anchor"

            # NEGATIVE CONTROL: re-resolving under the CURRENT ledger succeeds
            # and books 850 — so FX012 was about the evidence having MOVED, not
            # about the writer being unable to price at all.
            fresh = json.loads(await admin.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_terms_b_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid})))
            assert int(fresh["fee_minor"]) == 850
            await defn.fetchrow(
                "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)", json.dumps(fresh))
            assert await admin.fetchval(
                "SELECT consideration_minor FROM public.credit_lots "
                " WHERE tenant_id=$1 AND ledger_op_id=$2", tenant, f"op_terms_b_{_RUN}") == 850

            # And structurally: the writer names no provider table at all.
            src = (REPO / "database" / "migrations" /
                   "0089_g3_birth_path_ordered_fx_locks.sql").read_text(encoding="utf-8")
            wbody = src[src.index("CREATE OR REPLACE FUNCTION public.g3_write_lot_resolved"):]
            wbody = wbody[:wbody.index("$$;")]
            assert "g3_provider_payments" not in wbody, (
                "g3_write_lot_resolved reads g3_provider_payments; provider terms must reach it "
                "only through the resolved vector")
        finally:
            await op.close()
            await admin.close()
            await other.close()
            await defn.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (12) SECOND-CALL GUARD — G3.3-c(3), the corrector half of the proof
# ═══════════════════════════════════════════════════════════════════════════
def test_c12_one_correction_per_transaction_savepoint_retryable():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        reader = await _reader()
        try:
            await _writer(op)
            x, y = _d(70), _d(71)
            await _enter(op, USD, x, 16000)
            await _enter(op, USD, y, 16000)

            await op.execute("BEGIN")
            await op.execute(
                "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','a')", USD, x)
            state = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','b')", USD, y)
            await op.execute("ROLLBACK")
            assert state == "FX001", f"a second correction in one txn must raise FX001, got {state}"

            # A correction rolled back to a SAVEPOINT leaves no guard row and MAY
            # be retried — pg_current_xact_id() is the top-level xid, stable
            # across savepoints, so the guard is the ROW, not the xid alone.
            await op.execute("BEGIN")
            await op.execute("SAVEPOINT s1")
            await op.execute(
                "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','a')", USD, x)
            await op.execute("ROLLBACK TO SAVEPOINT s1")
            retry = await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16150,'jisdor','r','retry')", USD, x)
            await op.execute("COMMIT")
            assert retry is None, f"a savepoint-rolled-back correction must be retryable, got {retry}"
            assert await _rate(reader, USD, x) == 16150

            # NEGATIVE CONTROL: exactly ONE audit row survives.
            assert await reader.fetchval(
                "SELECT count(*) FROM public.fx_rates_corrections "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, x) == 1

            # correction_xid is XID8, not a lossy BIGINT.
            assert await op.fetchval(
                "SELECT format_type(atttypid,atttypmod) FROM pg_attribute "
                " WHERE attrelid='public.fx_rates_corrections'::regclass "
                "   AND attname='correction_xid'") == "xid8"
            assert await op.fetchval(
                "SELECT count(*) FROM pg_constraint c "
                " WHERE c.conrelid='public.fx_rates_corrections'::regclass AND c.contype='u' "
                "   AND (SELECT array_agg(a.attname::text ORDER BY a.attname) FROM pg_attribute a "
                "         WHERE a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)) "
                "       = ARRAY['correction_xid']") == 1, \
                "UNIQUE (correction_xid) IS the concurrency enforcement (G3.3-c(3))"
        finally:
            await op.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (13) CROSS-TENANT NON-VACUITY — G3.3-c(4)
# ═══════════════════════════════════════════════════════════════════════════
def test_c13_reference_check_tenant_blind_policy_read_only_and_permissive():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        app = await _connect(_dsn_as("app_user"))
        reader = await _reader()
        try:
            await _writer(op)
            referenced, free = _d(80), _d(81)
            await _enter(op, USD, referenced, 16000)
            await _enter(op, USD, free, 16000)

            tenant_a = await _make_tenant(admin, "t72cta")
            tenant_b = await _make_tenant(admin, "t72ctb")
            await _make_valued_lot(admin, tenant_a, USD, referenced)

            await op.execute("RESET app.current_tenant_id")
            assert await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','x')",
                USD, referenced) == "FX002", "with no tenant context the used rate must be refused"

            await op.execute(f"SET app.current_tenant_id = '{tenant_b}'")
            assert await sqlstate(
                op, "SELECT public.fx_rates_correct($1,$2,16100,'jisdor','r','x')",
                USD, referenced) == "FX002", "under tenant B the tenant-A lot must still block it"

            # NEGATIVE CONTROL: an unused rate remains correctable and audited,
            # so FX002 tracked the REFERENCE, not the tenant context.
            assert (await _correct(op, USD, free, 16777, "unused"))["new_rate"] == 16777
            assert await reader.fetchval(
                "SELECT count(*) FROM public.fx_rates_corrections "
                " WHERE currency_pair=$1 AND rate_date=$2", USD, free) == 1

            # RLS still confines app_user.
            await app.execute(f"SET app.current_tenant_id = '{tenant_b}'")
            assert await app.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant_a) == 0
            await app.execute(f"SET app.current_tenant_id = '{tenant_a}'")
            assert await app.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant_a) == 1, \
                "negative control broken: tenant A must see its own lot"

            pol = await op.fetchrow(
                "SELECT polcmd, polpermissive, pg_get_expr(polqual, polrelid) AS qual, "
                "  (SELECT array_agg(pg_get_userbyid(r) ORDER BY r) FROM unnest(polroles) r) AS roles "
                "  FROM pg_policy WHERE polrelid='public.credit_lots'::regclass "
                "   AND polname='fx_rates_reference_check'")
            assert pol is not None, "policy fx_rates_reference_check is absent"
            assert _char(pol["polcmd"]) == "r", "policy must be SELECT-only"
            assert pol["polpermissive"] is True
            assert pol["qual"] == "true"
            assert list(pol["roles"]) == ["fx_rates_owner"]

            # 🔴 G3.3-c(4)'s recorded dependency: the OR-combination holds only
            # while BOTH policies are permissive. A RESTRICTIVE policy would AND
            # and silently break the reference read. T72 locks that down.
            restrictive = await op.fetchval(
                "SELECT count(*) FROM pg_policy "
                " WHERE polrelid='public.credit_lots'::regclass AND NOT polpermissive")
            assert restrictive == 0, (
                "a RESTRICTIVE policy exists on credit_lots; permissive policies combine with "
                "OR but a restrictive one ANDs, which would break the cross-tenant reference "
                "read that G3.3-c(4) depends on")
            ti = await op.fetchrow(
                "SELECT polpermissive, polroles FROM pg_policy "
                " WHERE polrelid='public.credit_lots'::regclass AND polname='tenant_isolation'")
            assert ti["polpermissive"] is True
            assert list(ti["polroles"]) == [0], (
                "tenant_isolation must carry NO `TO` clause so it binds every role (0031:654-658)")

            assert await op.fetchval(
                "SELECT count(*) FROM pg_policy "
                " WHERE polrelid='public.credit_lots'::regclass AND polcmd <> 'r' "
                "   AND 'fx_rates_owner'::regrole = ANY(polroles)") == 0
            for verb in ("INSERT", "UPDATE", "DELETE"):
                assert await op.fetchval(
                    "SELECT has_table_privilege('fx_rates_owner','public.credit_lots',$1)",
                    verb) is False, f"fx_rates_owner must not hold {verb} on credit_lots"
        finally:
            await op.close()
            await admin.close()
            await app.close()
            await reader.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (14) CHECK INVARIANTS — G3.3-c(6), fx_rates half
# ═══════════════════════════════════════════════════════════════════════════
def test_c14_fx_rates_check_invariants():
    async def _go():
        bg = await _connect(BREAKGLASS_DSN)
        try:
            await _as_owner(bg)

            async def ins(pair, rate, source, ref, entered, day):
                return await sqlstate(
                    bg,
                    "INSERT INTO public.fx_rates(currency_pair,rate_date,idr_per_major_unit,"
                    "source,source_ref,entered_by) VALUES ($1,$6,$2,$3,$4,$5)",
                    pair, rate, source, ref, entered, day)

            assert await ins(USD, 0, "manual", "r", "e", _d(90)) == CHECK_VIOLATION
            assert await ins(USD, -1, "manual", "r", "e", _d(90)) == CHECK_VIOLATION
            for bad_pair in ("usd/IDR", "USDIDR", "USD/EUR"):
                assert await ins(bad_pair, 1, "manual", "r", "e", _d(90)) == CHECK_VIOLATION, \
                    f"{bad_pair!r} must be rejected"
            for blank in ("", "   "):
                assert await ins(USD, 1, "manual", blank, "e", _d(90)) == CHECK_VIOLATION
                assert await ins(USD, 1, "manual", "r", blank, _d(90)) == CHECK_VIOLATION
            assert await ins(USD, 1, None, "r", "e", _d(90)) == NOT_NULL_VIOLATION
            assert await ins(USD, 1, "coinflip", "r", "e", _d(90)) == CHECK_VIOLATION

            # NEGATIVE CONTROL: a valid row IS accepted.
            assert await ins(USD, 16000, "manual", "ref", "someone", _d(95)) is None
        finally:
            await bg.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (14b) credit_lots VALUATION / FX INVARIANTS — G3.3-c(6), lot half
#       "an unknown or verified-zero lot carrying an FX reference must fail"
# ═══════════════════════════════════════════════════════════════════════════
def test_c14b_credit_lots_valuation_fx_invariants():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        try:
            await _writer(op)
            d = _d(110)
            await _enter(op, USD, d, 16000)
            tenant = await _make_tenant(admin, "t72val")

            async def lot(**kw):
                cols = {"tenant_id": tenant, "source": "topup", "credits_granted": 10,
                        "credits_remaining": 10, "acquired_at": datetime.datetime.now(
                            datetime.timezone.utc),
                        "is_priced": False, "valuation_status": "unknown",
                        "src_currency": None, "fx_currency_pair": None, "fx_rate_date": None,
                        "unpriced_reason": "t72", "dpp_total_idr": 0,
                        "price_per_credit_idr": 0,
                        "consideration_minor": None, "consideration_currency": None}
                # a valued lot must name the consideration that priced it
                if kw.get("valuation_status") == "valued" and "consideration_minor" not in kw:
                    kw["consideration_minor"] = 905
                    kw["consideration_currency"] = kw.get("src_currency")
                cols.update(kw)
                names = ",".join(cols)
                ph = ",".join(f"${i+1}" for i in range(len(cols)))
                return await sqlstate(
                    admin, f"INSERT INTO public.credit_lots({names}) VALUES ({ph})",
                    *cols.values())

            # 🔴 THE CLAUSE: unknown / verified_zero carrying an FX reference FAILS.
            for status in ("unknown", "verified_zero"):
                got = await lot(valuation_status=status, src_currency="USD",
                                fx_currency_pair=USD, fx_rate_date=d, unpriced_reason="t72")
                assert got == CHECK_VIOLATION, (
                    f"a {status} lot carrying an FX reference must be rejected, got {got}")
                # ...and half a reference is no better than a whole one.
                got_half = await lot(valuation_status=status, fx_currency_pair=USD,
                                     fx_rate_date=None, unpriced_reason="t72")
                assert got_half == CHECK_VIOLATION, \
                    f"a {status} lot with a partial FX reference must be rejected, got {got_half}"

            # valued REQUIRES both FX columns and a pair derived from src_currency.
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="USD", fx_currency_pair=None,
                             fx_rate_date=None) == CHECK_VIOLATION, \
                "a valued lot with no FX reference must be rejected"
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="EUR", fx_currency_pair=USD,
                             fx_rate_date=d) == CHECK_VIOLATION, \
                "fx_currency_pair must equal src_currency || '/IDR'"
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="usd", fx_currency_pair="usd/IDR",
                             fx_rate_date=d) == CHECK_VIOLATION, \
                "src_currency must match ^[A-Z]{3}$"

            # is_priced and valuation_status may never disagree.
            assert await lot(valuation_status="valued", is_priced=False, unpriced_reason="t72",
                             src_currency="USD", fx_currency_pair=USD,
                             fx_rate_date=d) == CHECK_VIOLATION
            assert await lot(valuation_status="unknown", is_priced=True,
                             unpriced_reason=None) == CHECK_VIOLATION

            # A valued lot must NAME the consideration that priced it — the
            # V46(a) assertion. Without this, dpp_total_idr's source is an
            # inference, and the settlement-vs-anchor distinction is invisible
            # on the row itself.
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="USD", fx_currency_pair=USD, fx_rate_date=d,
                             consideration_minor=None, consideration_currency=None,
                             dpp_total_idr=1, price_per_credit_idr=1) == CHECK_VIOLATION, \
                "a valued lot with no recorded consideration must be rejected"
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="USD", fx_currency_pair=USD, fx_rate_date=d,
                             consideration_minor=905, consideration_currency="EUR",
                             dpp_total_idr=1, price_per_credit_idr=1) == CHECK_VIOLATION, \
                "the consideration currency must match src_currency"
            assert await lot(valuation_status="unknown", consideration_minor=905,
                             consideration_currency="USD") == CHECK_VIOLATION, \
                "an unvalued lot must carry no consideration"

            # NEGATIVE CONTROLS: both legitimate shapes ARE accepted.
            assert await lot(valuation_status="verified_zero", unpriced_reason="free grant") is None, \
                "a verified_zero lot with NO FX reference must be accepted"
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="USD", fx_currency_pair=USD, fx_rate_date=d,
                             dpp_total_idr=1000, price_per_credit_idr=100) is None, \
                "a correctly valued lot must be accepted"

            # The FX reference is a real FK into fx_rates.
            assert await lot(valuation_status="valued", is_priced=True, unpriced_reason=None,
                             src_currency="USD", fx_currency_pair=USD,
                             fx_rate_date=_d(999), dpp_total_idr=1,
                             price_per_credit_idr=1) == "23503", \
                "a valued lot must reference a rate that exists"

            # 🔴 THE COLUMNS ARE UNIFIED. `0089` dropped `0088`'s duplicate and
            # renamed `0086`'s `rate_date` to the normative `fx_rate_date`, so
            # there is exactly ONE column and the "pinned equal" CHECK that
            # previously held them together is gone with the duplication.
            cols = [r["attname"] for r in await admin.fetch(
                "SELECT attname FROM pg_attribute "
                " WHERE attrelid='public.credit_lots'::regclass AND attnum>0 AND NOT attisdropped")]
            assert "fx_rate_date" in cols, "the normative column is absent"
            assert "rate_date" not in cols, (
                "credit_lots still carries 0086's rate_date beside fx_rate_date. Two columns for "
                "one business value is what Gate 2 was asked to unify — a writer will eventually "
                "populate the wrong one.")
            assert await admin.fetchval(
                "SELECT count(*) FROM pg_constraint "
                " WHERE conrelid='public.credit_lots'::regclass "
                "   AND conname='credit_lots_fx_rate_date_agrees_with_0086_rate_date'") == 0, \
                "the dual-date reconciliation CHECK outlived the columns it reconciled"

            # NEGATIVE CONTROL: the FK moved onto the surviving column rather
            # than being dropped with the duplicate.
            fk = await admin.fetchrow(
                "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint "
                " WHERE conrelid='public.credit_lots'::regclass AND conname='credit_lots_fx_rate_fk'")
            assert fk is not None, "the FX foreign key did not survive the unification"
            assert "fx_rate_date" in fk["def"] and "ON DELETE RESTRICT" in fk["def"], \
                f"FK lost its column or its RESTRICT: {fk['def']}"
        finally:
            await op.close()
            await admin.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# (15) IMMUTABILITY — G3.3-c(2)
# ═══════════════════════════════════════════════════════════════════════════
def test_c15_immutability_delete_truncate_and_append_only_corrections():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        bg = await _connect(BREAKGLASS_DSN)
        try:
            await _writer(op)
            d = _d(100)
            await _enter(op, USD, d, 16000)

            await _as_owner(bg)
            where = "WHERE currency_pair=$1 AND rate_date=$2"
            for col, val in (("currency_pair", "'EUR/IDR'"), ("rate_date", "'2099-01-01'::date"),
                             ("created_at", "now()"), ("entered_by", "'someone_else'")):
                s = await sqlstate(bg, f"UPDATE public.fx_rates SET {col}={val} {where}", USD, d)
                assert s == RESTRICT_VIOLATION, f"{col} must be immutable, got {s}"

            # NEGATIVE CONTROL: the three AMENDABLE columns do move, else
            # "immutable" would just mean "no UPDATE ever works".
            for col, val in (("idr_per_major_unit", "16001"), ("source", "'manual'"),
                             ("source_ref", "'amended'")):
                s = await sqlstate(bg, f"UPDATE public.fx_rates SET {col}={val} {where}", USD, d)
                assert s is None, f"{col} must remain amendable, got {s}"

            assert await sqlstate(
                bg, f"DELETE FROM public.fx_rates {where}", USD, d) == RESTRICT_VIOLATION

            # TRUNCATE is refused. On fx_rates the FK is checked BEFORE statement
            # triggers fire, so that alone would not prove the trigger works.
            assert await sqlstate(bg, "TRUNCATE public.fx_rates") is not None

            trg = await bg.fetch(
                "SELECT tgname, (tgtype & 1)=0 AS is_statement, (tgtype & 32)<>0 AS on_truncate, "
                "       p.proname AS fn FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid "
                " WHERE t.tgrelid='public.fx_rates'::regclass AND NOT t.tgisinternal")
            by_name = {t["tgname"]: t for t in trg}
            assert "fx_rates_no_mutation" in by_name, "row-level trigger missing"
            tr = by_name.get("fx_rates_no_truncate")
            assert tr is not None, "statement-level TRUNCATE trigger missing"
            assert tr["on_truncate"] is True and tr["is_statement"] is True
            assert tr["fn"] == "fx_rates_reject_mutation"

            # PROOF the statement path refuses: same function, same shape, on a
            # temp table nothing references, so no FK can answer for it.
            await bg.execute(f"CREATE TEMP TABLE t72_trunc_probe_{_RUN}(x INT)")
            await bg.execute(
                f"CREATE TRIGGER t72_probe BEFORE TRUNCATE ON t72_trunc_probe_{_RUN} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION public.fx_rates_reject_mutation()")
            assert await sqlstate(bg, f"TRUNCATE t72_trunc_probe_{_RUN}") == RESTRICT_VIOLATION

            # NEGATIVE CONTROL: an untriggered temp table truncates cleanly.
            await bg.execute(f"CREATE TEMP TABLE t72_trunc_ctl_{_RUN}(x INT)")
            assert await sqlstate(bg, f"TRUNCATE t72_trunc_ctl_{_RUN}") is None

            # fx_rates_corrections is append-only.
            free = _d(120)
            await _enter(op, USD, free, 16000)
            await _correct(op, USD, free, 16100, "audit test")
            assert await sqlstate(bg, "UPDATE public.fx_rates_corrections SET reason='x'") \
                == RESTRICT_VIOLATION
            assert await sqlstate(bg, "DELETE FROM public.fx_rates_corrections") \
                == RESTRICT_VIOLATION
            assert await sqlstate(bg, "TRUNCATE public.fx_rates_corrections") \
                == RESTRICT_VIOLATION
        finally:
            await op.close()
            await bg.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# GATE 3 — DECISION 3A, the supplier-fee anchor
#
#   Supplier Fee = signed payment credit − payment_fees − tax
#
# from the Dodo Balance Ledger, admitted only under four evidence rules.
# `settlement_amount` is reconciliation evidence ONLY.
# ═══════════════════════════════════════════════════════════════════════════
def test_g3a_anchor_is_ledger_derived_not_settlement():
    """The measured fixture: ledger 1000 − 95 − 0 ⇒ **905**, not 1000.

    `MATRIX-046`'s new `V46` assertion: *a lot born at `settlement_amount` while
    `payment_fees ≠ 0` must FAIL.* `0086` stored 1000 because `settlement_amount`
    LOOKED net, which overstates the revenue base by 95 on every fee-bearing
    transaction. Here the birth path never reads that column at all.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(300)
            await _writer(op)
            await _enter(op, USD, day, 17913)          # the pinned fixture rate

            tenant = await _make_tenant(admin, "t72d3a")
            pid = f"pay_d3a_{_RUN}"
            instant = datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            # The aggregate stores the SETTLEMENT figure, 1000 — deliberately wrong.
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, supplier_fee_minor, supplier_fee_currency,"
                " sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),1000,'USD','dodo_mor','provider',0)",
                pid, instant)
            # The LEDGER says: payment 1000, payment_fees 95, no tax ⇒ anchor 905.
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 95)])

            anchor = await admin.fetchrow(
                "SELECT * FROM public.g3_supplier_fee_anchor('dodo',$1,0)", pid)
            assert anchor["anchor_minor"] == 905, (
                f"Decision 3A anchor must be 1000-95-0 = 905, got {anchor['anchor_minor']}")
            assert anchor["currency"] == "USD"

            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pid, f"op_d3a_{_RUN}")]))
            assert len(rows) == 1 and rows[0]["valuation_status"] == "valued"

            lot = await admin.fetchrow(
                "SELECT dpp_total_idr, price_per_credit_idr, src_currency, fx_rate_date "
                "  FROM public.credit_lots WHERE tenant_id=$1", tenant)
            # 905 minor = 9.05 USD; 9.05 x 17913.000000 = 162112.65 IDR.
            assert lot["dpp_total_idr"] == decimal.Decimal("162112.65"), (
                f"expected the 905-derived base 162112.65, got {lot['dpp_total_idr']}. "
                "1000 would have given 179130.00 — the 95-per-transaction overstatement.")
            assert lot["fx_rate_date"] == day and lot["src_currency"] == "USD"

            # 🔴 THE V46 ASSERTION, stated as a comparison rather than a hope: the
            # settlement figure is still sitting in the column, and the lot is
            # NOT priced from it.
            settlement = await admin.fetchval(
                "SELECT supplier_fee_minor FROM public.g3_provider_payments "
                " WHERE provider='dodo' AND provider_payment_id=$1", pid)
            assert settlement == 1000, "fixture: the settlement-derived column still says 1000"
            assert lot["dpp_total_idr"] != decimal.Decimal("179130.00"), (
                "the lot was priced from settlement_amount while payment_fees = 95 — this is "
                "exactly what MATRIX-046's new V46 assertion forbids")

            # The reconciliation view makes the gap visible without pricing from it.
            recon = await admin.fetchrow(
                "SELECT * FROM v_g3_supplier_fee_reconciliation WHERE provider_payment_id=$1", pid)
            assert recon["anchor_minor"] == 905 and recon["overstatement_minor"] == 95
        finally:
            for c in (op, engine, admin):
                await c.close()

    run(_go())


def test_g3a_admission_rules_stall_rather_than_guess():
    """The four evidence rules, each with the accepting control beside it."""
    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            async def anchor(pid, tax=0):
                return await sqlstate(
                    admin, "SELECT * FROM public.g3_supplier_fee_anchor('dodo',$1,$2)",
                    pid, tax)

            # (a) two payment entries ⇒ FX009
            a = f"pay_two_{_RUN}"
            await _seed_ledger(admin, a, [("payment", 500), ("payment", 500), ("payment_fees", 0)])
            assert await anchor(a) == "FX009", "two payment entries must STALL"

            # (b) two settlement currencies ⇒ FX009
            b = f"pay_ccy_{_RUN}"
            await _seed_ledger(admin, b, [("payment", 1000)], currency="USD")
            await _seed_ledger(admin, b, [("payment_fees", 95)], currency="EUR")
            assert await anchor(b) == "FX009", "mixed settlement currencies must STALL"

            # (c) settlement_tax > 0 with no tax entry ⇒ FX009
            c = f"pay_tax_{_RUN}"
            await _seed_ledger(admin, c, [("payment", 1000), ("payment_fees", 95)])
            assert await anchor(c, tax=50) == "FX009", \
                "a positive settlement_tax with no tax entry must STALL"

            # (d) no payment_fees entry and no explicit zero-fee evidence ⇒ FX009
            d = f"pay_nofee_{_RUN}"
            await _seed_ledger(admin, d, [("payment", 1000)])
            assert await anchor(d) == "FX009", (
                "a MISSING fee entry is not a PROVEN zero fee — assuming it is is how 1000 got "
                "stored where 905 belonged")
            # ...and the evidence that lifts it must come from the PROVIDER: a
            # payment_fees entry that exists and says 0. An earlier draft let the
            # CALLER assert `zero_fee_evidenced=true` and this control enshrined
            # that bypass; the parameter no longer exists.
            await _seed_ledger(admin, d, [("payment_fees", 0)])
            assert await anchor(d) is None, \
                "a persisted payment_fees=0 entry IS explicit provider evidence"
            assert "p_zero_fee_evidenced" not in (
                REPO / "database" / "migrations" / "0090_g3_supplier_fee_anchor.sql"
            ).read_text(encoding="utf-8").split("CREATE OR REPLACE FUNCTION public.g3_supplier_fee_anchor")[1].split("$$")[0], \
                "the caller-supplied zero-fee boolean is back; evidence must be provider-side"

            # (e) unknown event type ⇒ FX010, and it is checked FIRST
            e = f"pay_unk_{_RUN}"
            await _seed_ledger(admin, e, [("payment", 1000), ("payment_fees", 95),
                                          ("chargeback_adjustment", -100)])
            assert await anchor(e) == "FX010", "an unmodelled event type must STALL"

            # NEGATIVE CONTROL: the fully-evidenced shape IS admitted, with tax.
            ok = f"pay_ok_{_RUN}"
            await _seed_ledger(admin, ok, [("payment", 1000), ("payment_fees", 95), ("tax", 55)])
            assert await anchor(ok, tax=55) is None
            row = await admin.fetchrow(
                "SELECT * FROM public.g3_supplier_fee_anchor('dodo',$1,55)", ok)
            assert row["anchor_minor"] == 1000 - 95 - 55, "anchor = payment - fees - tax"
        finally:
            await admin.close()

    run(_go())


def test_g3a_birth_stalls_when_the_anchor_is_inadmissible():
    """An inadmissible anchor must STALL the BIRTH, not produce a priced lot."""
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(310)
            await _writer(op)
            await _enter(op, USD, day, 17913)
            tenant = await _make_tenant(admin, "t72d3astall")
            pid = f"pay_stall3a_{_RUN}"
            instant = datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, supplier_fee_minor, supplier_fee_currency,"
                " sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),1000,'USD','dodo_mor','provider',0)",
                pid, instant)
            # Ledger present but inadmissible: no fee entry, no zero-fee evidence.
            await _seed_ledger(admin, pid, [("payment", 1000)])

            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pid, f"op_stall3a_{_RUN}")]))
            assert state == "FX009", f"an inadmissible anchor must STALL the birth, got {state}"
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0, \
                "a lot was born from evidence the admission rules rejected"

            # NEGATIVE CONTROL: add the zero-fee evidence and the same birth
            # succeeds, priced from the ledger.
            await _seed_ledger(admin, pid, [("payment_fees", 0)])
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pid, f"op_stall3a_{_RUN}")]))
            assert rows[0]["valuation_status"] == "valued"
        finally:
            for c in (op, engine, admin):
                await c.close()

    run(_go())


def test_g3a_ledger_is_append_only():
    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            pid = f"pay_ao_{_RUN}"
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 0)])
            for sql in ("UPDATE public.g3_provider_balance_ledger SET amount_minor = 1",
                        "DELETE FROM public.g3_provider_balance_ledger",
                        "TRUNCATE public.g3_provider_balance_ledger"):
                assert await sqlstate(admin, sql) == RESTRICT_VIOLATION, f"{sql} must be refused"

            # NEGATIVE CONTROL: appending is exactly what IS allowed.
            assert await sqlstate(
                admin,
                "INSERT INTO public.g3_provider_balance_ledger"
                "(provider, provider_payment_id, entry_id, event_type, amount_minor,"
                " currency, raw_entry) VALUES ('dodo',$1,$2,'refund',-100,'USD','{}'::jsonb)",
                pid, f"{pid}:refund") is None
        finally:
            await admin.close()

    run(_go())


def test_g3a_no_ledger_stalls_and_writes_no_lot():
    """No Balance Ledger evidence ⇒ **STALL-AWAITING-PROVIDER-LEDGER**, zero lots.

    Absence of evidence is not evidence of simple terms. An earlier draft turned
    "no ledger" into NULL terms, and the writer banked a `valuation_status =
    'unknown'` lot — which is indistinguishable from a legitimate `T64` outcome
    and is the direct inverse of what Decision 3A requires.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(320)
            await _writer(op)
            await _enter(op, USD, day, 17913)
            tenant = await _make_tenant(admin, "t72noledger")
            pid = f"pay_noledger_{_RUN}"
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),'dodo_mor','provider',0)",
                pid, datetime.datetime.combine(day, datetime.time(12, 0),
                                               tzinfo=datetime.timezone.utc))
            req = [await _birth_request(tenant, USD, pid, f"op_noledger_{_RUN}")]

            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert state == "FX011", (
                f"no provider ledger must STALL with FX011, got {state}. An `unknown` lot here "
                "would silently bank an unpriced balance and look like a normal T64 outcome.")
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0, \
                "a lot was born with no provider-ledger evidence at all"

            # The stall happens in the RESOLVE phase, before any lock is taken.
            assert await sqlstate(
                admin, "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_noledger_b_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid})) == "FX011"

            # NEGATIVE CONTROL: give it evidence and the same request is priced,
            # so FX011 tracked the absent ledger and nothing else.
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 95)])
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(req))
            assert rows[0]["valuation_status"] == "valued"
            assert await admin.fetchval(
                "SELECT consideration_minor FROM public.credit_lots WHERE tenant_id=$1",
                tenant) == 905
        finally:
            for c in (op, engine, admin):
                await c.close()

    run(_go())


def test_g3a_replay_with_a_different_anchor_is_a_defect_not_an_idempotent_retry():
    """`G3.2`: the WHOLE birth-immutable tuple is compared on replay.

    An earlier draft compared only `acquired_at`, grandfather, provenance and
    `fx_rate_date`, so a replay of the same `ledger_op_id` carrying a DIFFERENT
    consideration silently returned the original lot and the divergence was
    never surfaced. That is the failure mode idempotence is supposed to prevent,
    not an example of it.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        defn = await _as_definer()   # the only identity that may call the helper
        try:
            day = _d(330)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            tenant = await _make_tenant(admin, "t72replay")
            pid = f"pay_replay_{_RUN}"
            instant = datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.timezone.utc)
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),'dodo_mor','provider',0)", pid, instant)
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 95)])

            op_id = f"op_replay_{_RUN}"
            base = json.loads(await admin.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": op_id, "provider": "dodo",
                            "provider_payment_id": pid})))
            assert int(base["fee_minor"]) == 905
            first = await defn.fetchrow(
                "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)", json.dumps(base))
            assert first["valuation_status"] == "valued"

            # An IDENTICAL replay is idempotent and returns the same lot.
            again = await defn.fetchrow(
                "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)", json.dumps(base))
            assert again["lot_id"] == first["lot_id"], "an identical replay must be idempotent"
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 1

            # 🔴 A replay whose LEGITIMATE re-resolution differs must be
            # REFUSED, not absorbed. A late tax entry moves the anchor to 850,
            # so re-resolving gives a vector that is internally consistent with
            # the ledger and inconsistent with the STORED lot — which is exactly
            # the case a value-blind replay check waves through.
            await admin.execute(
                "INSERT INTO public.g3_provider_balance_ledger"
                "(provider, provider_payment_id, entry_id, event_type, amount_minor,"
                " currency, raw_entry) "
                "VALUES ('dodo',$1,$1||':late-tax','tax',55,'USD','{}'::jsonb)", pid)
            diverged = json.loads(await admin.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": op_id, "provider": "dodo",
                            "provider_payment_id": pid})))
            assert int(diverged["fee_minor"]) == 850, "control: the re-resolution must differ"
            state = await sqlstate(
                defn, "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)",
                json.dumps(diverged))
            assert state == "FX013", (
                f"a replay with consideration 850 against a stored 905 must raise FX013, got "
                f"{state}. Silently returning the 905 lot hides a valuation divergence.")

            # ...and each of the other value fields is compared too, not just
            # the consideration.
            for field, value in (("credits", 200), ("source", "monthly_grant")):
                v = dict(diverged); v[field] = value
                assert await sqlstate(
                    defn, "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)",
                    json.dumps(v)) is not None, f"a replay with a different {field} must be refused"

            # NEGATIVE CONTROL: still exactly one lot, and it is unchanged.
            row = await admin.fetchrow(
                "SELECT consideration_minor, dpp_total_idr, fx_rate_at_grant "
                "  FROM public.credit_lots WHERE tenant_id=$1", tenant)
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 1
            assert row["consideration_minor"] == 905
            assert row["dpp_total_idr"] == decimal.Decimal("144800.00")
            # 0031's fx_rate_at_grant is now actually stored, not read and dropped.
            assert row["fx_rate_at_grant"] == decimal.Decimal("16000.000000")

            # The insert is race-free by construction, not by a prior SELECT.
            src = (REPO / "database" / "migrations" /
                   "0089_g3_birth_path_ordered_fx_locks.sql").read_text(encoding="utf-8")
            wbody = src[src.index("CREATE OR REPLACE FUNCTION public.g3_write_lot_resolved"):]
            wbody = wbody[:wbody.index("$$;")]
            assert "ON CONFLICT (tenant_id, ledger_op_id)" in wbody, (
                "the writer must INSERT ... ON CONFLICT DO NOTHING RETURNING; SELECT-then-INSERT "
                "lets two concurrent callers surface a raw 23505 instead of an idempotent replay")
            assert wbody.index("INSERT INTO public.credit_lots") < wbody.index(
                "SELECT * INTO v_existing"), \
                "the existence check must FOLLOW the conflicting insert, not precede it"
        finally:
            for c in (op, admin, defn):
                await c.close()

    run(_go())


def test_g3a_resolution_holds_the_per_payment_fence():
    """An acquisition step that honours the fence BLOCKS during a birth.

    The digest catches an ingestion that ignores the fence; this proves the
    fence exists at all. Both are needed: the digest alone would let every
    honest ingestion race and then fail late, and the fence alone would be an
    unverifiable convention.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        holder = await _connect(ADMIN_DSN)     # a birth in flight
        ingest = await _connect(ADMIN_DSN)     # an acquisition step
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(340)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            tenant = await _make_tenant(admin, "t72fence")
            pid = f"pay_fence_{_RUN}"
            await admin.execute(
                "INSERT INTO public.g3_provider_payments"
                "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
                "VALUES ('dodo',$1,$2,'t72_fixture',now(),'dodo_mor','provider',0)",
                pid, datetime.datetime.combine(day, datetime.time(12, 0),
                                               tzinfo=datetime.timezone.utc))
            await _seed_ledger(admin, pid, [("payment", 1000), ("payment_fees", 95)])

            # NEGATIVE CONTROL FIRST: with nothing held, the fence is free.
            await ingest.execute("BEGIN")
            await ingest.execute("SET LOCAL lock_timeout = '400ms'")
            free = await sqlstate(
                ingest, "SELECT public.g3_fence_payment('dodo',$1)", pid)
            await ingest.execute("ROLLBACK")
            assert free is None, f"an unheld fence must be takeable, got {free}"

            # A birth resolves — and holds the fence for its whole transaction.
            await holder.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            await holder.execute(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_fence_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid}))

            # An acquisition step that honours the fence now BLOCKS.
            await ingest.execute("BEGIN")
            await ingest.execute("SET LOCAL lock_timeout = '400ms'")
            blocked = await sqlstate(
                ingest, "SELECT public.g3_fence_payment('dodo',$1)", pid)
            await ingest.execute("ROLLBACK")
            assert blocked == LOCK_NOT_AVAILABLE, (
                f"the resolution must HOLD the per-payment fence (expected 55P03, got {blocked}). "
                "Without it an ingestion can insert a ledger entry mid-birth, and only the digest "
                "would notice — after the work was done.")

            await holder.execute("ROLLBACK")

            # ...and it is released with the transaction.
            await ingest.execute("BEGIN")
            await ingest.execute("SET LOCAL lock_timeout = '400ms'")
            assert await sqlstate(
                ingest, "SELECT public.g3_fence_payment('dodo',$1)", pid) is None
            await ingest.execute("ROLLBACK")
        finally:
            for c in (holder, ingest):
                try:
                    await c.execute("ROLLBACK")
                except Exception:
                    pass
            for c in (op, holder, ingest, admin):
                await c.close()

    run(_go())


# ═══════════════════════════════════════════════════════════════════════════
# GATE 3 SCOPE LOCK — channel/tax, helper closure, batch fence, rounding
# ═══════════════════════════════════════════════════════════════════════════
async def _termed_payment(admin, pid, day, *, channel="dodo_mor", owner="provider",
                          currency="USD", entries=((("payment", 1000)), ("payment_fees", 95))):
    await admin.execute(
        "INSERT INTO public.g3_provider_payments"
        "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
        " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
        "VALUES ('dodo',$1,$2,'t72_fixture',now(),$3,$4,0)",
        pid, datetime.datetime.combine(day, datetime.time(12, 0),
                                       tzinfo=datetime.timezone.utc), channel, owner)
    await _seed_ledger(admin, pid, list(entries), currency)


def test_g3_scope_only_dodo_mor_provider_usd_is_admitted():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(350)
            await _writer(op)
            await _enter(op, USD, day, 16000)

            # Only the storable combinations: 0086's own
            # g3_pp_channel_tax_owner_coherent already refuses the mismatched
            # pairs at INSERT, so the cases left for Gate 3 to stall are the
            # direct-sale pair and the un-termed one.
            for i, (ch, ow) in enumerate((("wimba_direct", "wimba"), (None, None))):
                tenant = await _make_tenant(admin, f"t72scope{i}")
                pid = f"pay_scope_{i}_{_RUN}"
                await _termed_payment(admin, pid, day, channel=ch, owner=ow)
                state = await sqlstate(
                    engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                    json.dumps([await _birth_request(tenant, USD, pid, f"op_scope_{i}_{_RUN}")]))
                assert state == "FX008", (
                    f"channel={ch} owner={ow} must STALL (only dodo_mor+provider is in Gate-3 "
                    f"scope), got {state}")
                assert await admin.fetchval(
                    "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0

            # NEGATIVE CONTROL: the one admitted combination IS priced.
            tenant = await _make_tenant(admin, "t72scopeok")
            pid = f"pay_scope_ok_{_RUN}"
            await _termed_payment(admin, pid, day)
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)",
                json.dumps([await _birth_request(tenant, USD, pid, f"op_scope_ok_{_RUN}")]))
            assert rows[0]["valuation_status"] == "valued"
        finally:
            for c in (op, engine, admin):
                await c.close()

    run(_go())


def test_g3_direct_helper_path_is_closed():
    """A hand-made vector cannot price a top-up."""
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        defn = await _as_definer()   # the only identity that may call the helper
        try:
            day = _d(360)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            tenant = await _make_tenant(admin, "t72helper")
            pid = f"pay_helper_{_RUN}"
            await _termed_payment(admin, pid, day)

            good = json.loads(await admin.fetchval(
                "SELECT public.g3_resolve_request($1::jsonb)",
                json.dumps({"tenant": str(tenant), "source": "topup", "credits": 100,
                            "ledger_op_id": f"op_helper_{_RUN}", "provider": "dodo",
                            "provider_payment_id": pid})))

            # no provider identity / no digest
            # Dropping the identity or the digest is refused. Which code fires
            # depends on which check reaches it first — the digest re-check runs
            # before the provenance check, deliberately — so the assertion is
            # that it IS refused, and FX014 is asserted precisely where only the
            # provenance check can answer: the anchor mismatch below.
            for drop in ("provider", "provider_payment_id", "ledger_digest"):
                v = dict(good); v[drop] = None
                got = await sqlstate(
                    defn, "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)", json.dumps(v))
                assert got in ("FX012", "FX014"), \
                    f"a valued top-up without {drop} must be refused, got {got}"

            # an anchor the ledger does not yield
            v = dict(good); v["fee_minor"] = 1000
            assert await sqlstate(
                defn, "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)",
                json.dumps(v)) == "FX014", (
                "a vector claiming 1000 against a ledger anchor of 905 must be refused — that is "
                "precisely the settlement-vs-anchor overstatement")

            # NEGATIVE CONTROL: the genuine vector writes.
            assert (await defn.fetchrow(
                "SELECT * FROM public.g3_write_lot_resolved($1::jsonb)",
                json.dumps(good)))["valuation_status"] == "valued"
        finally:
            for c in (op, admin, defn):
                await c.close()

    run(_go())


def test_g3_batch_fence_is_taken_in_a_fixed_order_before_resolution():
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        blocker = await _connect(ADMIN_DSN)
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(370)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            tenant = await _make_tenant(admin, "t72bfence")
            pids = [f"pay_bf_{i}_{_RUN}" for i in range(3)]
            for pid in pids:
                await _termed_payment(admin, pid, day)
            # requests in REVERSE of lock order
            reqs = [await _birth_request(tenant, USD, pid, f"op_bf_{i}_{_RUN}")
                    for i, pid in enumerate(reversed(pids))]

            # hold the LOWEST-ordered payment; a fixed-order fence blocks before
            # resolving anything, so nothing is written.
            await blocker.execute("BEGIN")
            await blocker.execute("SELECT public.g3_fence_payment('dodo',$1)", pids[0])
            await engine.execute("BEGIN ISOLATION LEVEL READ COMMITTED")
            await engine.execute("SET LOCAL lock_timeout = '500ms'")
            state = await sqlstate(
                engine, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(reqs))
            await engine.execute("ROLLBACK")
            await blocker.execute("ROLLBACK")
            assert state == LOCK_NOT_AVAILABLE, f"expected a block on the fence, got {state}"
            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 0

            # the ordering is in the shipped SQL, in ONE statement
            src = (REPO / "database" / "migrations" /
                   "0090_g3_supplier_fee_anchor.sql").read_text(encoding="utf-8")
            body = src[src.index("CREATE OR REPLACE FUNCTION public.g3_fence_payments"):]
            body = body[:body.index("$$;")]
            assert body.index("ORDER BY g.provider, g.provider_payment_id") < body.index("FOR UPDATE")

            # ...and the BATCH fence runs before any resolution. Asserted
            # structurally: the per-request fence inside g3_resolve_request is a
            # fallback and still blocks, so behaviour alone cannot tell whether
            # the ORDERED batch acquisition — the deadlock-freedom property —
            # is still there.
            src89 = (REPO / "database" / "migrations" /
                     "0089_g3_birth_path_ordered_fx_locks.sql").read_text(encoding="utf-8")
            ebody = src89[src89.index("CREATE OR REPLACE FUNCTION public.g3_birth_lots"):]
            ebody = ebody[:ebody.index("$$;")]
            fence_at = ebody.find("g3_fence_payments")
            resolve_at = ebody.find("g3_resolve_request")
            assert fence_at != -1, (
                "g3_birth_lots never calls g3_fence_payments — fencing would fall back to "
                "per-request order, which is the deadlock hazard the fixed order removes")
            assert fence_at < resolve_at, "the batch fence must precede resolution"

            # NEGATIVE CONTROL: unheld, the same batch writes all three.
            rows = await engine.fetch(
                "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps(reqs))
            assert len(rows) == 3 and all(r["valuation_status"] == "valued" for r in rows)
        finally:
            for c in (engine, blocker):
                try:
                    await c.execute("ROLLBACK")
                except Exception:
                    pass
            for c in (op, engine, blocker, admin):
                await c.close()

    run(_go())


def test_g3_rounding_boundaries_one_terminal_rounding_each():
    """minor → FX → ONE rounding to 2 dp; PPC ONE rounding to 4 dp, half-up."""
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        engine = await _engine()
        admin = await _connect(ADMIN_DSN)
        try:
            await _writer(op)
            # (anchor_minor, rate, credits, expected dpp, expected ppc)
            cases = [
                # exact half at 2 dp: 1.005 x 1000 = 1005.000 -> no rounding needed
                (1, decimal.Decimal("1000.5"), 3, decimal.Decimal("10.01"),
                 decimal.Decimal("3.3367")),
                # 0.01 x 1234.567 = 12.34567 -> 12.35 (half-up at the 3rd dp)
                (1, decimal.Decimal("1234.567"), 7, decimal.Decimal("12.35"),
                 decimal.Decimal("1.7643")),
                # terminates inside 4 dp: 100.00 / 8 = 12.5 exactly. This one
                # discards NO digits, so it constrains the division but proves
                # nothing about the rounding mode — it is here as a control that
                # an exact quotient is not disturbed, not as a boundary.
                (10000, decimal.Decimal("1"), 8, decimal.Decimal("100.00"),
                 decimal.Decimal("12.5000")),
                # 🔴 THE ACTUAL HALF-UP BOUNDARY. 1.00 / 32 = 0.03125 — a digit
                # falls off, and it is exactly 5 with nothing after it, so this
                # is a true tie at the 4th decimal. Half-up gives 0.0313;
                # round-half-even, the other plausible implementation, gives
                # 0.0312. Only a case that DISCARDS a tie can tell those two
                # apart, and until this one existed the suite could not: every
                # PPC case it held was exactly representable.
                (100, decimal.Decimal("1"), 32, decimal.Decimal("1.00"),
                 decimal.Decimal("0.0313")),
            ]
            for i, (anchor, rate, credits, want_dpp, want_ppc) in enumerate(cases):
                day = _d(400 + i)
                await _enter(op, USD, day, rate)
                tenant = await _make_tenant(admin, f"t72round{i}")
                pid = f"pay_round_{i}_{_RUN}"
                await _termed_payment(admin, pid, day,
                                      entries=(("payment", anchor), ("payment_fees", 0)))
                req = await _birth_request(tenant, USD, pid, f"op_round_{i}_{_RUN}")
                req["credits"] = credits
                await engine.fetch("SELECT * FROM public.g3_birth_lots($1::jsonb)",
                                   json.dumps([req]))
                row = await admin.fetchrow(
                    "SELECT dpp_total_idr, price_per_credit_idr, consideration_minor "
                    "  FROM public.credit_lots WHERE tenant_id=$1", tenant)
                assert row["consideration_minor"] == anchor
                assert row["dpp_total_idr"] == want_dpp, (
                    f"case {i}: {anchor} minor x {rate} -> expected {want_dpp}, got "
                    f"{row['dpp_total_idr']} — components must not be rounded separately")
                assert row["price_per_credit_idr"] == want_ppc, (
                    f"case {i}: PPC expected {want_ppc}, got {row['price_per_credit_idr']}")

            # NUMERIC end to end — no float anywhere in the chain.
            types = dict(await admin.fetch(
                "SELECT attname, format_type(atttypid,atttypmod) FROM pg_attribute "
                " WHERE attrelid='public.credit_lots'::regclass "
                "   AND attname IN ('dpp_total_idr','price_per_credit_idr','fx_rate_at_grant')"))
            assert types["dpp_total_idr"] == "numeric(18,2)"
            assert types["price_per_credit_idr"] == "numeric(14,4)"
            assert types["fx_rate_at_grant"] == "numeric(18,6)"
        finally:
            for c in (op, engine, admin):
                await c.close()

    run(_go())


def _acquisition_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "provider_ledger_ingest", REPO / "python" / "ops" / "provider_ledger_ingest.py")
    ops = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ops)
    return ops


async def _fenceable_payment(admin, pid, day):
    """The parent row `g3_fence_payment` locks. Acquisition needs nothing else."""
    await admin.execute(
        "INSERT INTO public.g3_provider_payments"
        "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
        " payment_at_pinned_at, sales_channel, tax_owner, provider_tax_minor) "
        "VALUES ('dodo',$1,$2,'t72_fixture',now(),'dodo_mor','provider',0)",
        pid, datetime.datetime.combine(day, datetime.time(12, 0),
                                       tzinfo=datetime.timezone.utc))


def _entry(eid, event_type, amount, ref="P"):
    return {"id": eid, "event_type": event_type, "amount": amount,
            "currency": "USD", "reference_object_id": ref}


def _row(eid, event_type, amount):
    return {"entry_id": eid, "event_type": event_type, "amount_minor": amount,
            "currency": "USD", "raw_entry": json.dumps({"id": eid})}


def test_c01d_0088_revokes_its_own_temporary_membership_before_committing():
    """0088 must clean up after ITSELF, not rely on a later migration to do it.

    🔴 WHY THIS IS A SOURCE ASSERTION AND NOT A CATALOG ONE. `0091` also revokes
       a temporary `fx_rates_owner` membership as part of its own cleanup, and it
       runs afterwards. So by the time anything queries `pg_auth_members` the
       membership is gone either way, and deleting `0088`'s revoke is invisible —
       measured, not assumed: that mutation went straight through a 74-mutation
       run.

       What that masking would hide is real. A chain that stops between `0088`
       and `0091` — a failure, a partial deploy, an environment that has not
       reached `0091` yet — leaves the migration role holding `fx_rates_owner`,
       which is precisely the privilege `0088` exists to take away. The property
       is "revoked before 0088 commits", and only the ORDER OF THE STATEMENTS can
       express that. `0091`'s cleanup stays where it is, as a backstop for its
       own membership, not as a substitute for this one.
    """
    src = (REPO / "database" / "migrations" / "0088_fxpop_controlled_fx_entry.sql").read_text()
    revoke = src.find("REVOKE fx_rates_owner FROM")
    assert revoke != -1, (
        "0088 never revokes the temporary fx_rates_owner membership it grants itself. Until a "
        "later migration happens to clean up, the migration role keeps the ownership privilege "
        "0088's whole privilege split exists to remove.")
    commit = src.rfind("COMMIT;")
    assert commit != -1, "0088 has no COMMIT"
    assert revoke < commit, (
        f"0088 revokes the temporary membership at offset {revoke}, AFTER its COMMIT at {commit} — "
        "so the membership is durable for anyone reading between the two.")

    # non-vacuity: the grant it is paired with must be there too, or this test
    # would keep passing against a migration that stopped granting at all.
    grant = src.find("GRANT fx_rates_owner TO")
    assert grant != -1 and grant < revoke, (
        "0088 does not grant the temporary membership before revoking it; this assertion would "
        "then be describing a sequence that no longer happens")


def test_g4b_posting_boundary_search_path_and_trigger_hardening():
    """0093's structural contract, asserted where the MUTATION HARNESS can see it.

    🔴 THIS EXISTS BECAUSE A MEASURED MUTATION SURVIVED. The harness runs THIS
       suite and nothing else, so every control living in the node realdb files
       is invisible to it — `M83` reverted the trigger hardening, put `public`
       back on the search_path, and T72 stayed 41/41 without noticing. A control
       that cannot be falsified by the tool that checks controls is decoration.
    """
    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            for fn in ("g3_post_cash_in", "g3_post_consumption", "g3_post_refund",
                       "enforce_period_open", "enforce_journal_header_complete",
                       "enforce_period_open_lines", "enforce_journal_balanced"):
                cfg = await admin.fetchval(
                    "SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    " WHERE n.nspname='public' AND p.proname=$1", fn)
                assert cfg and "search_path=pg_catalog, pg_temp" in cfg, (
                    f"{fn} does not pin search_path to pg_catalog, pg_temp (got {cfg!r}). "
                    "With `public` on the path every unqualified table reference is resolved "
                    "positionally, which is the hijack the pinning exists to remove — and for a "
                    "trigger it is inherited from whoever is writing.")
            for fn in ("g3_post_cash_in", "g3_post_consumption", "g3_post_refund"):
                row = await admin.fetchrow(
                    "SELECT prosecdef, pg_get_userbyid(proowner) AS owner FROM pg_proc p "
                    "  JOIN pg_namespace n ON n.oid=p.pronamespace "
                    " WHERE n.nspname='public' AND p.proname=$1", fn)
                assert row["prosecdef"] is True, f"{fn} is not SECURITY DEFINER"
                assert row["owner"] == "g3_posting_definer", f"{fn} owner is {row['owner']}"
            # the integrity triggers must stay INVOKER — borrowed authority in a
            # trigger means the check no longer runs as whoever is writing.
            for fn in ("enforce_period_open", "enforce_journal_balanced"):
                assert await admin.fetchval(
                    "SELECT prosecdef FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                    " WHERE n.nspname='public' AND p.proname=$1", fn) is False, \
                    f"{fn} became SECURITY DEFINER"
        finally:
            await admin.close()

    run(_go())


def test_g4c_cash_in_takes_its_tenant_authority_from_the_credited_anchor():
    """The anchor guard, exercised — not merely present in the source.

    🔴 ALSO WRITTEN BECAUSE A MUTATION SURVIVED. `M78` leaves the tenant guard
       intact and removes ONLY the credited-anchor lookup: every value the caller
       controls still agrees with itself, the posting succeeds, and nothing looks
       wrong. `p_tenant = current_setting(...)` compares two caller-set values;
       the anchor is the one input the caller did not write.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        engine = await _engine()
        try:
            day = _d(520)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            when = datetime.datetime.combine(day, datetime.time(12, 0),
                                             tzinfo=datetime.timezone.utc)

            async def _payment(pid):
                await admin.execute(
                    "INSERT INTO public.g3_provider_payments"
                    "(provider, provider_payment_id, payment_at, payment_at_pinned_by,"
                    " payment_at_pinned_at, supplier_fee_minor, supplier_fee_currency,"
                    " sales_channel, tax_owner, provider_tax_minor) "
                    "VALUES ('dodo',$1,$2,'t72',now(),100000,'IDR','dodo_mor','provider',0)",
                    pid, when)

            async def _journals(tenant):
                return await admin.fetchval(
                    "SELECT count(*) FROM public.journal_entries WHERE tenant_id=$1", tenant)

            async def _cash_in(tenant, pid, op_id):
                async with engine._conn.transaction():
                    await engine._conn.execute(
                        "SELECT set_config('app.current_tenant_id', $1, true)", str(tenant))
                    assert await engine._conn.fetchval("SELECT current_user") == ENGINE_ROLE
                    return await engine._conn.fetchval(
                        "SELECT public.g3_post_cash_in($1,'dodo',$2,$3)", tenant, pid, op_id)

            # (a) credited anchor present and owned by this tenant -> it posts.
            t_ok = await _make_tenant(admin, "t72anchok")
            pid_ok = f"pay_anch_ok_{_RUN}"
            await _payment(pid_ok)
            await admin.execute(
                "INSERT INTO public.payment_events"
                "(tenant_id, provider, idempotency_key, provider_payment_id, credited) "
                "VALUES ($1,'dodo',$2,$3,true)", t_ok, f"idem_{pid_ok}", pid_ok)
            assert await _cash_in(t_ok, pid_ok, f"ci_ok_{_RUN}") is not None
            assert await _journals(t_ok) == 1, "the admitted case must actually post"

            # (b) NO credited anchor -> refused, and nothing written.
            t_no = await _make_tenant(admin, "t72anchno")
            pid_no = f"pay_anch_no_{_RUN}"
            await _payment(pid_no)
            state = None
            try:
                await _cash_in(t_no, pid_no, f"ci_no_{_RUN}")
            except asyncpg.PostgresError as exc:
                state = str(exc)
            assert state and "no credited payment_events anchor" in state, (
                f"a payment with no credited anchor was posted anyway (got {state!r}). The "
                "caller-supplied tenant is not evidence of ownership.")
            assert await _journals(t_no) == 0, "a refused cash-in must leave zero journals"

            # (c) the anchor belongs to ANOTHER tenant; caller says its own,
            #     consistently, in both the parameter and the GUC.
            t_other = await _make_tenant(admin, "t72anchoth")
            pid_x = f"pay_anch_x_{_RUN}"
            await _payment(pid_x)
            await admin.execute(
                "INSERT INTO public.payment_events"
                "(tenant_id, provider, idempotency_key, provider_payment_id, credited) "
                "VALUES ($1,'dodo',$2,$3,true)", t_ok, f"idem_{pid_x}", pid_x)
            state = None
            try:
                await _cash_in(t_other, pid_x, f"ci_x_{_RUN}")
            except asyncpg.PostgresError as exc:
                state = str(exc)
            assert state and "no credited payment_events anchor" in state, (
                "a consistent but WRONG tenant posted another tenant's payment")
            assert await _journals(t_other) == 0
        finally:
            for c in (op, admin, engine):
                await c.close()

    run(_go())


def test_g4_birth_runs_as_the_real_posting_engine_and_the_probe_holds_nothing():
    """GATE 4. The identity that posts the lot, proven where it actually posts.

    🔴 WHAT THIS REPLACES. Until `0091` the birth path in this suite ran on the
       MIGRATION connection — `neondb_owner`, `BYPASSRLS`, privileged on
       everything. That identity cannot fail a GRANT check or an RLS policy, so
       a green birth said nothing about whether `g3_posting_engine` could do it.
       That gap is exactly why `T72` was recorded PARTIAL rather than PASS.

    The three things that make this non-vacuous, none of which the old shape
    could show:
      * the assertion happens INSIDE the transaction that calls the function —
        not at connect time, where a later `RESET ROLE` would go unnoticed;
      * `session_user` is the restricted probe, so the engine identity was
        REACHED BY `SET ROLE`, not logged into — if it could be logged into,
        `g3_posting_engine` would no longer be NOLOGIN;
      * the same probe WITHOUT `SET ROLE` is refused, which is what proves the
        privileges belong to the engine role rather than to the probe.
    """
    async def _go():
        op = await _connect(_dsn_as(OPERATOR_ROLE))
        admin = await _connect(ADMIN_DSN)
        engine = await _engine()
        bare = await _connect(_dsn_as(PROBE_ROLE))          # deliberately NO SET ROLE
        try:
            day = _d(500)
            await _writer(op)
            await _enter(op, USD, day, 16000)
            tenant = await _make_tenant(admin, "t72g4")
            pid = f"pay_g4_{_RUN}"
            await _seed_payment(admin, pid, datetime.datetime.combine(
                day, datetime.time(12, 0), tzinfo=datetime.timezone.utc))
            req = await _birth_request(tenant, USD, pid, f"op_g4_{_RUN}")

            # ── the birth, and the identity, in ONE transaction ──────────────
            async with engine._conn.transaction():
                who = await engine._conn.fetchrow(
                    "SELECT current_user AS cu, session_user AS su, "
                    "  (SELECT rolsuper     FROM pg_roles WHERE rolname=current_user) AS su_flag, "
                    "  (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user) AS bypass")
                assert who["cu"] == ENGINE_ROLE, who["cu"]
                assert who["su"] == PROBE_ROLE, who["su"]
                assert who["su_flag"] is False and who["bypass"] is False
                rows = await engine._conn.fetch(
                    "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps([req]))
                # Same transaction, AFTER the call: the identity did not move
                # underneath it, and the row really is there to be seen.
                assert await engine._conn.fetchval("SELECT current_user") == ENGINE_ROLE
                assert len(rows) == 1 and rows[0]["valuation_status"] == "valued", rows

            assert await admin.fetchval(
                "SELECT count(*) FROM public.credit_lots WHERE tenant_id=$1", tenant) == 1, (
                "the engine reported a valued lot but nothing is stored — the birth did not "
                "actually write through the RLS policies 0091 adds")

            # ── (c) the probe itself holds NOTHING ───────────────────────────
            assert await bare.fetchval("SELECT current_user") == PROBE_ROLE
            state = await sqlstate(
                bare, "SELECT * FROM public.g3_birth_lots($1::jsonb)", json.dumps([req]))
            assert state == INSUFFICIENT_PRIVILEGE, (
                f"the bare probe ran the birth path and got {state!r}. Then the EXECUTE right "
                "is the probe's own, and 'the engine can post' was never tested — every Gate-4 "
                "assertion above would hold with g3_posting_engine stripped of everything.")
            for verb in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert await bare.fetchval(
                    "SELECT has_table_privilege($1,'public.credit_lots',$2)",
                    PROBE_ROLE, verb) is False, (
                    f"the probe holds {verb} on credit_lots in its own right")

            # ── (d) the engine role is still exactly what 0088 shipped ───────
            attrs = await admin.fetchrow(
                "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=$1",
                ENGINE_ROLE)
            assert attrs["rolcanlogin"] is False, f"{ENGINE_ROLE} became LOGIN"
            assert attrs["rolsuper"] is False, f"{ENGINE_ROLE} became SUPERUSER"
            assert attrs["rolbypassrls"] is False, (
                f"{ENGINE_ROLE} has BYPASSRLS — 0091's policies would never be consulted and "
                "the RLS half of the birth contract would be untested")

            # ...and it got there by GRANT ... SET TRUE, not by inheritance.
            assert await admin.fetchval(
                "SELECT pg_has_role($1,$2,'SET')", PROBE_ROLE, ENGINE_ROLE) is True
            assert await admin.fetchval(
                "SELECT pg_has_role($1,$2,'USAGE')", PROBE_ROLE, ENGINE_ROLE) is False, (
                "the probe INHERITS the engine role; then it holds the engine's privileges "
                "without SET ROLE and the refusal proved above would be an accident of syntax")

            # ── ONE DOOR. The engine holds EXECUTE on the entrypoint and on
            #    NOTHING else in the birth path.
            #
            # 🔴 THIS IS THE ASSERTION THE FIRST CUT OF 0091 WOULD HAVE FAILED.
            #    It granted g3_write_lot_resolved straight to the engine, which
            #    let a hand-made resolver vector reach credit_lots with no
            #    ordered FX-lock phase in front of it — the direct-helper path
            #    Gate 2 closed, re-opened by a privilege instead of by code.
            for fn in ("public.g3_write_lot_resolved(jsonb)",
                       "public.g3_resolve_request(jsonb)",
                       "public.g3_lock_fx_refs(jsonb)",
                       "public.g3_fx_rate_at(text,date)",
                       "public.g3_fence_payments(jsonb)",
                       "public.g3_ledger_digest(text,text)",
                       "public.g3_supplier_fee_anchor(text,text,bigint)"):
                assert await admin.fetchval(
                    "SELECT has_function_privilege($1,$2,'EXECUTE')", ENGINE_ROLE, fn) is False, (
                    f"{ENGINE_ROLE} can execute {fn} directly — that is a second way into the "
                    "birth write that skips g3_birth_lots entirely")
                # 🔴 AND NOT VIA PUBLIC EITHER. g3_lock_fx_refs and
                #    g3_fence_payments are SECURITY DEFINER lock-takers: while
                #    PUBLIC could call them, any caller at all could pin payment
                #    or FX rows. "No grant needed, PUBLIC has it" named this
                #    surface; it did not excuse it.
                assert await admin.fetchval(
                    "SELECT has_function_privilege('public',$1,'EXECUTE')", fn) is False, (
                    f"PUBLIC can execute {fn}")
            assert await admin.fetchval(
                "SELECT has_function_privilege($1,'public.g3_birth_lots(jsonb)','EXECUTE')",
                ENGINE_ROLE) is True

            # ── the boundary itself ──────────────────────────────────────────
            door = await admin.fetchrow(
                "SELECT p.prosecdef, pg_get_userbyid(p.proowner) AS owner "
                "  FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                " WHERE n.nspname='public' AND p.proname='g3_birth_lots'")
            assert door["prosecdef"] is True, "g3_birth_lots is not SECURITY DEFINER"
            assert door["owner"] == "g3_birth_definer", door["owner"]
            boundary = await admin.fetchrow(
                "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
                " WHERE rolname='g3_birth_definer'")
            assert boundary is not None, "the boundary role does not exist"
            assert not (boundary["rolcanlogin"] or boundary["rolsuper"]
                        or boundary["rolbypassrls"]), (
                "g3_birth_definer must be NOLOGIN NOSUPERUSER NOBYPASSRLS — a privileged "
                "boundary would make the credit_lots policies dead code")
            # It owns the door and holds no standing right to create anything.
            assert await admin.fetchval(
                "SELECT has_schema_privilege('g3_birth_definer','public','CREATE')") is False, (
                "the boundary role kept CREATE on schema public after the ownership transfer")
        finally:
            for c in (op, admin, engine, bare):
                await c.close()

    run(_go())


def test_g3_acquisition_pager_speaks_the_documented_offset_contract():
    """`page_number`/`page_size` + `items`, terminating on a SHORT page.

    🔴 THE STUB ASSERTS THE URL, AND THAT IS THE POINT OF THIS TEST. The
       previous version fed pages back through `lambda u, h: next(it)` — the
       request was never looked at — and answered in a cursor dialect
       (`has_more`, `next_cursor`) that this endpoint does not speak. A stub
       free to invent the contract will agree with any pager, including one
       that stops dead after page one, which is exactly what the old code did
       against the real API.
    """
    ops = _acquisition_module()
    size = ops.PAGE_SIZE

    # A FULL page must be followed by another request; a short page ends it.
    # This is the only termination signal the endpoint offers.
    pages = [
        {"items": [_entry(f"a{i}", "payment_fees", 1) for i in range(size)]},
        {"items": [_entry("tail", "payment", 1000)]},
    ]
    urls = []

    def transport(url, headers):
        urls.append(url)
        assert headers["Authorization"] == "Bearer k"
        return pages[len(urls) - 1]

    got = ops.fetch_all_pages("P", api_key="k", base_url="http://x", transport=transport)
    assert len(urls) == 2, (
        f"a FULL page ({size} items) must be followed by another request; the walk stopped "
        f"after {len(urls)}")
    assert len(got) == size + 1

    for n, url in enumerate(urls):
        assert f"page_number={n}" in url, f"page {n} requested as {url!r}"
        assert f"page_size={size}" in url, f"page {n} requested as {url!r}"
        assert "reference_object_id=P" in url
        # The dialect that was invented last time. If any of these come back,
        # the pager has been rewritten against a contract the provider does not
        # implement, and the walk will terminate on page one.
        for absent in ("starting_after", "next_cursor", "has_more", "limit="):
            assert absent not in url, f"page {n} sent {absent!r}: {url!r}"

    # A response in any other shape is refused rather than read as "no entries".
    for bogus in ({"data": [_entry("a", "payment", 1)]}, {"items": None}, {}, []):
        with pytest.raises(ops.OperatorError):
            ops.fetch_all_pages("P", api_key="k", base_url="http://x",
                                transport=lambda u, h, b=bogus: b)

    # An entry belonging to another payment is refused.
    other = {"items": [_entry("z", "payment", 1, ref="OTHER")]}
    with pytest.raises(ops.OperatorError):
        ops.fetch_all_pages("P", api_key="k", base_url="http://x", transport=lambda u, h: other)

    # Offset paging over a moving set re-serves rows. Counting one twice moves
    # the anchor, so a repeat is a refusal, not a de-duplication.
    repeat = [{"items": [_entry(f"a{i}", "payment_fees", 1) for i in range(size)]},
              {"items": [_entry("a0", "payment_fees", 1)]}]
    with pytest.raises(ops.OperatorError):
        ops.fetch_all_pages("P", api_key="k", base_url="http://x",
                            transport=lambda u, h: repeat.pop(0))

    # A pager that never short-pages stops at the hard limit instead of looping.
    served = itertools.count()

    def never_short(url, headers):
        n = next(served)
        return {"items": [_entry(f"p{n}_{i}", "payment_fees", 1) for i in range(size)]}

    with pytest.raises(ops.OperatorError):
        ops.fetch_all_pages("P", api_key="k", base_url="http://x", transport=never_short)
    assert next(served) == ops.MAX_PAGES, (
        f"the hard stop must bound the walk at {ops.MAX_PAGES} pages")


def test_g3_acquisition_persists_under_the_fence_and_reads_back():
    ops = _acquisition_module()
    fetched = [_entry("a", "payment", 1000), _entry("b", "payment_fees", 95)]

    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(380)
            pid = f"pay_tool_{_RUN}"
            await _fenceable_payment(admin, pid, day)
            rows = ops.normalise(fetched)
            first = await ops.ingest(admin, "dodo", pid, rows)
            assert first["inserted"] == 2
            await ops.read_back_and_compare(admin, "dodo", pid, rows)

            # idempotent
            again = await ops.ingest(admin, "dodo", pid, rows)
            assert again["inserted"] == 0 and again["digest"] == first["digest"]

            # 🔴 read-back CATCHES a stored entry that differs from what was fetched
            drifted = [dict(r) for r in rows]
            drifted[0]["amount_minor"] = 999
            with pytest.raises(ops.OperatorError):
                await ops.read_back_and_compare(admin, "dodo", pid, drifted)
        finally:
            await admin.close()

    run(_go())


def test_g3_acquisition_conflict_rolls_the_whole_batch_back():
    """A refused batch must leave the ledger EXACTLY as it found it.

    🔴 THE READ-BACK USED TO RUN AFTER THE COMMIT. It reported the conflict
       correctly and the rows it was objecting to were already durable, so the
       next reader — lot birth — could consume evidence the tool had just
       rejected. This test is the difference between detecting and preventing:
       it does not assert that the refusal happens, it asserts that the NEW
       entry which rode along in the same batch is not in the table afterwards.
    """
    ops = _acquisition_module()

    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(381)
            pid = f"pay_rollback_{_RUN}"
            await _fenceable_payment(admin, pid, day)
            kept = _row(f"rb_keep_{_RUN}", "payment", 1000)
            await ops.ingest(admin, "dodo", pid, [kept])
            before = await admin.fetchval("SELECT public.g3_ledger_digest('dodo',$1)", pid)

            # one entry conflicting with different stored content, one brand new
            newcomer = _row(f"rb_new_{_RUN}", "payment_fees", 95)
            mixed = [{**kept, "amount_minor": 999}, newcomer]
            with pytest.raises(ops.OperatorError):
                await ops.ingest(admin, "dodo", pid, mixed)

            stored = await admin.fetch(
                "SELECT entry_id, amount_minor FROM public.g3_provider_balance_ledger"
                " WHERE provider='dodo' AND provider_payment_id=$1", pid)
            assert [(r["entry_id"], r["amount_minor"]) for r in stored] == [
                (kept["entry_id"], 1000)], (
                "the batch was refused but its NEW entry survived — the insert committed "
                f"before the comparison ran. Stored: {[dict(r) for r in stored]}")
            assert await admin.fetchval(
                "SELECT public.g3_ledger_digest('dodo',$1)", pid) == before, (
                "the digest moved across a refused batch, so the anchor's evidence set moved")
        finally:
            await admin.close()

    run(_go())


def test_g3_acquisition_refuses_stored_entries_the_provider_did_not_return():
    """The read-back is a set EQUALITY, not a containment check.

    An entry sitting under this payment that the fetch did not return still
    reaches `g3_ledger_digest` and `g3_supplier_fee_anchor`, both of which read
    every row for the payment. Proving only that the fetched rows are present
    leaves that entry — and the fee it moves — completely unexamined.
    """
    ops = _acquisition_module()

    async def _go():
        admin = await _connect(ADMIN_DSN)
        try:
            day = _d(382)
            pid = f"pay_exactset_{_RUN}"
            await _fenceable_payment(admin, pid, day)
            a = _row(f"xs_a_{_RUN}", "payment", 1000)
            b = _row(f"xs_b_{_RUN}", "payment_fees", 95)
            await ops.ingest(admin, "dodo", pid, [a, b])

            # a later fetch returns only `a` — `b` is now unexplained evidence
            with pytest.raises(ops.OperatorError) as exc:
                await ops.ingest(admin, "dodo", pid, [a])
            assert b["entry_id"] in str(exc.value), (
                "the refusal must name the entry the provider did not return")

            # and the refusal changed nothing
            assert await admin.fetchval(
                "SELECT count(*) FROM public.g3_provider_balance_ledger"
                " WHERE provider='dodo' AND provider_payment_id=$1", pid) == 2
        finally:
            await admin.close()

    run(_go())
