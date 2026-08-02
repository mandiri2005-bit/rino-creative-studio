"""Static and disposable-PostgreSQL acceptance for migration 0074."""
import asyncio
import os
import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import canon_lite_qc_meter as meter
import credit_catalog


REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "database" / "migrations"
M0074 = MIGRATIONS / "0074_platform_qc_metering.sql"


def _sql() -> str:
    return M0074.read_text(encoding="utf-8")


def _without_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _function_body(sql: str, name: str) -> str:
    match = re.search(
        rf"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.{name}\s*\("
        rf".*?\)\s*(?:RETURNS|RETURNS\s+TABLE).*?\$\$(.*?)\$\$;",
        sql, re.I | re.S)
    assert match, f"function not found: {name}"
    return match.group(0)


def test_numbering_and_quarantine_are_exact():
    assert not list(MIGRATIONS.glob("0072*.sql"))
    assert not list(MIGRATIONS.glob("0073*.sql"))
    assert M0074.exists()
    assert (MIGRATIONS / "0070_gl_privilege_hardening.sql").exists()
    assert (MIGRATIONS / "0071_deferred_revenue_no_silent_fallback.sql").exists()
    # 0073 is an intentionally untracked recovery file in the primary worktree. Copying it
    # here would make this package touch an FAQ path and violate I2, while requiring it in a
    # shipped test would fail every clean clone. The acceptance evidence separately checks
    # the primary worktree path and hash; this test binds the runner-relevant fact: absent.


def test_usage_logs_and_customer_views_are_not_touched():
    executable = _without_comments(_sql()).lower()
    assert not re.search(r"\b(alter|insert\s+into|update|delete\s+from)\s+(public\.)?usage_logs\b",
                         executable)
    for view in ("v_pl_monthly", "v_op_margin", "v_cogs_by_provider"):
        assert not re.search(rf"create\s+(or\s+replace\s+)?view\s+{view}\b", executable)


def test_sentinel_and_opex_code_match_python_sources():
    sql = _sql()
    sentinel = str(meter.PLATFORM_QC_TENANT_ID)
    assert sentinel == "670711f2-ecc9-5577-9bdf-cc77611d1b4b"
    assert sql.count(sentinel) >= 3
    assert credit_catalog.PLATFORM_QC_GL_OPEX_CODE == "6740"
    assert "'6740'" in sql
    with pytest.raises(ValueError, match="no revenue/COGS pair"):
        credit_catalog.gl_codes("platform_qc")


def test_table_contract_is_dedicated_and_unknown_never_means_zero():
    sql = _without_comments(_sql())
    assert re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+public\.platform_qc_usage", sql, re.I)
    for forbidden in ("revenue", "is_paid", "credits"):
        columns = re.search(
            r"CREATE\s+TABLE.*?platform_qc_usage\s*\((.*?)\n\);", sql, re.I | re.S).group(1)
        assert not re.search(rf"^\s*{forbidden}\s+", columns, re.I | re.M)
    assert re.search(
        r"CHECK\s*\(\(cost_state\s*=\s*'known'\)\s*=\s*"
        r"\(cost_usd\s+IS\s+NOT\s+NULL\)\)", sql, re.I)
    assert re.search(
        r"CHECK\s*\(\(cost_state\s*=\s*'known'\)\s*=\s*"
        r"\(cost_resolved_at\s+IS\s+NOT\s+NULL\)\)", sql, re.I)


def test_identity_bounds_and_catalog_shape():
    sql = _without_comments(_sql())
    assert re.search(
        r"UNIQUE\s*\(\s*run_id\s*,\s*job_uuid\s*,\s*phase\s*,\s*unit_index\s*,"
        r"\s*attempt_ordinal\s*\)", sql, re.I)
    assert "attempt_key" not in sql.lower()
    assert "attempt_timeout_s" in sql
    assert "E' \\t\\n\\r\\f\\x0B'" in _sql()
    constraints = "\n".join(re.findall(r"CONSTRAINT.*?CHECK\s*\(.*?\)", sql, re.I | re.S))
    assert not re.search(r"\bphase\s+IN\s*\(", constraints, re.I)
    assert meter.PHASE_CATALOG == ("canon_lite_l2_extract",)
    assert meter.PHASE_CATALOG_VERSION


def test_final_surface_is_seven_security_definer_functions():
    sql = _without_comments(_sql())
    expected = {
        "platform_qc_begin_attempt",
        "platform_qc_finish_attempt",
        "platform_qc_resolve_cost",
        "platform_qc_arm_kill",
        "platform_qc_kill_is_armed",
        "platform_qc_reap",
        "platform_qc_kill_sync",
    }
    permanent = set(re.findall(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+public\.([a-z0-9_]+)", sql, re.I))
    # The append-only trigger helper is not a granted application/reaper function.
    assert permanent == expected | {"platform_qc_kill_append_only"}
    for name in expected:
        body = _function_body(sql, name)
        assert re.search(r"SECURITY\s+DEFINER", body, re.I)
        assert re.search(r"SET\s+search_path\s*=\s*''", body, re.I)
        assert re.search(
            rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.{name}\s*\(",
            sql, re.I), name
    assert "platform_qc_reap()" in sql
    assert not re.search(r"platform_qc_reap\s*\(\s*p_", sql, re.I)


def test_role_is_created_before_grants_and_function_grants_are_closed():
    sql = _without_comments(_sql())
    create_role = sql.index("CREATE ROLE platform_qc_reaper")
    first_reaper_grant = sql.index("GRANT USAGE ON SCHEMA public TO platform_qc_reaper")
    assert create_role < first_reaper_grant
    assert "CREATE ROLE platform_qc_reaper LOGIN NOBYPASSRLS" in sql
    assert re.search(r"REVOKE\s+ALL\s+ON\s+public\.platform_qc_usage\s+FROM\s+app_user", sql, re.I)
    assert re.search(r"REVOKE\s+ALL\s+ON\s+public\.platform_qc_kill\s+FROM\s+app_user", sql, re.I)
    assert sql.count(" TO app_user;") >= 5
    assert sql.count(" TO platform_qc_reaper;") >= 3  # schema usage + two functions


def test_cost_is_computed_from_frozen_decimal_rates():
    sql = _without_comments(_sql())
    body = _function_body(sql, "platform_qc_resolve_cost")
    signature = re.search(
        r"platform_qc_resolve_cost\s*\((.*?)\)\s*RETURNS", body, re.I | re.S).group(1)
    assert "p_cost_usd" not in signature
    assert "p_provider_reported_cost_usd numeric" in re.sub(r"\s+", " ", signature)
    assert "DEFAULT" not in signature.upper()
    assert re.search(r"round\s*\(.*?/ ?1000000\s*,\s*6\s*\)", body, re.I | re.S)
    assert "::numeric(14,6)" in body
    for field in ("tokens_in", "tokens_out", "cost_usd", "provider_reported_cost_usd"):
        assert re.search(rf"v_row\.{field}\s+IS\s+NOT\s+DISTINCT\s+FROM", body, re.I)


def test_rls_keeps_house_predicate_without_touching_customer_guc_in_functions():
    sql = _without_comments(_sql())
    assert re.search(
        r"ALTER\s+TABLE\s+public\.platform_qc_usage\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
        sql, re.I)
    assert re.search(
        r"ALTER\s+TABLE\s+public\.platform_qc_usage\s+FORCE\s+ROW\s+LEVEL\s+SECURITY",
        sql, re.I)
    policy = re.search(
        r"CREATE\s+POLICY\s+tenant_isolation\s+ON\s+public\.platform_qc_usage(.*?);",
        sql, re.I | re.S).group(1)
    assert "app.current_tenant_id" in policy
    assert "current_user <> session_user" in policy
    assert "app.platform_qc_definer" in policy
    functions = sql[
        sql.index("CREATE OR REPLACE FUNCTION public.platform_qc_arm_kill"):
        sql.index("ALTER TABLE public.platform_qc_usage ENABLE ROW LEVEL SECURITY")
    ]
    assert "set_config('app.current_tenant_id'" not in functions
    kill_policy = re.search(
        r"CREATE\s+POLICY\s+platform_qc_kill_owner_path"
        r"\s+ON\s+public\.platform_qc_kill(.*?);",
        sql, re.I | re.S).group(1)
    assert "USING (true)" not in kill_policy
    assert "current_user <> session_user" in kill_policy
    assert "pg_catalog.pg_class" in kill_policy


def test_kill_table_blocks_every_destructive_statement_by_trigger():
    sql = _without_comments(_sql())
    assert re.search(
        r"CREATE\s+TRIGGER\s+platform_qc_kill_no_mutate\s+"
        r"BEFORE\s+UPDATE\s+OR\s+DELETE", sql, re.I)
    assert re.search(
        r"CREATE\s+TRIGGER\s+platform_qc_kill_no_truncate\s+"
        r"BEFORE\s+TRUNCATE", sql, re.I)


def test_deployment_header_names_both_deploy_gates():
    header = _sql().split("BEGIN;", 1)[0]
    assert "DG-1" in header and "DG-2" in header
    assert "SCHEMA FIRST, CODE SECOND" in header


@pytest.mark.skipif(
    not all(os.getenv(name) for name in (
        "TEST_DATABASE_URL", "TEST_APP_DATABASE_URL", "TEST_REAPER_DATABASE_URL")),
    reason="disposable PostgreSQL URLs not supplied; static contract still runs",
)
def test_live_function_rls_and_grant_contract():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        owner = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        isolated_owner = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        app = await asyncpg.connect(os.environ["TEST_APP_DATABASE_URL"])
        reaper = await asyncpg.connect(os.environ["TEST_REAPER_DATABASE_URL"])
        try:
            job_uuid = UUID("22222222-2222-4222-8222-222222222222")
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                tenant = await owner.fetchrow(
                    """SELECT plan,is_active,slug,email FROM public.tenants
                       WHERE id=$1""", meter.PLATFORM_QC_TENANT_ID)
                assert tuple(tenant) == (
                    "platform", False, "__platform_qc__", "platform-qc@wimba.invalid")
                for relation in (
                    "credit_balances", "subscriptions", "users", "usage_logs",
                ):
                    assert await owner.fetchval(
                        f"SELECT count(*) FROM public.{relation} WHERE tenant_id=$1",
                        meter.PLATFORM_QC_TENANT_ID) == 0
                await owner.execute(
                    """INSERT INTO public.jobs(id,tenant_id,job_type,status)
                       VALUES($1,$2,'narasi','running')""",
                    job_uuid, meter.PLATFORM_QC_TENANT_ID)

            assert await app.fetchval(
                "SELECT current_setting('app.current_tenant_id', true) IS NULL")
            begin_sql = """SELECT * FROM public.platform_qc_begin_attempt(
                 $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)"""
            base_args = [
                "live-guc-restore", job_uuid, "live-test",
                "canon_lite_l2_extract", 0, 1,
                "fake", "fake-model", "test-v1",
                Decimal("0.075"), Decimal("0.150"), Decimal("30"),
            ]

            function_bound_cases = (
                (0, None, "run_id"),
                (0, "r" * 65, "run_id"),
                (1, None, "job_uuid"),
                (2, "", "job_external_id"),
                (2, "j" * 65, "job_external_id"),
                (3, "", "phase"),
                (3, "p" * 65, "phase"),
                (4, -1, "unit_index"),
                (4, 10001, "unit_index"),
                (5, 0, "attempt_ordinal"),
                (5, 1001, "attempt_ordinal"),
                (6, "", "provider"),
                (6, "p" * 65, "provider"),
                (7, "", "model_upstream"),
                (7, "m" * 129, "model_upstream"),
                (8, "", "pricing_version"),
                (8, "v" * 65, "pricing_version"),
                (9, Decimal("-1"), "rate_in"),
                (9, Decimal("10001"), "rate_in"),
                (10, Decimal("-1"), "rate_out"),
                (10, Decimal("10001"), "rate_out"),
                (11, Decimal("0"), "attempt_timeout_s"),
                (11, Decimal("901"), "attempt_timeout_s"),
            )
            for case_number, (index, replacement, code) in enumerate(
                function_bound_cases
            ):
                invalid = list(base_args)
                invalid[0] = f"function-bound-{case_number}"
                invalid[index] = replacement
                with pytest.raises(asyncpg.CheckViolationError) as error:
                    await app.fetchrow(begin_sql, *invalid)
                assert error.value.message == f"platform_qc_bounds:{code}"

            row = await app.fetchrow(
                begin_sql, *base_args)
            assert row["outcome"] in ("inserted", "replay")
            replay = await app.fetchrow(begin_sql, *base_args)
            assert (replay["outcome"], replay["attempt_id"]) == (
                "replay", row["attempt_id"])
            subscale = list(base_args)
            subscale[9] = Decimal("0.0750004")
            assert (await app.fetchrow(begin_sql, *subscale))["outcome"] == "replay"

            provenance_variants = {
                2: "other-job-external",
                6: "other-provider",
                7: "other-model",
                8: "other-pricing",
                9: Decimal("0.0751"),
                10: Decimal("0.1501"),
                11: Decimal("31"),
            }
            for index, replacement in provenance_variants.items():
                changed = list(base_args)
                changed[index] = replacement
                assert (await app.fetchrow(begin_sql, *changed))["outcome"] == "conflict"
            assert await app.fetchval(
                "SELECT public.platform_qc_kill_is_armed()") is True

            queue_retry = list(base_args)
            queue_retry[0] = "live-guc-restore-queue-retry"
            queue_row = await app.fetchrow(begin_sql, *queue_retry)
            assert queue_row["outcome"] == "inserted"
            assert queue_row["attempt_id"] != row["attempt_id"]

            whitespace_cases = {
                0: "\n",
                2: " ",
                3: "\t",
                6: "\r",
                7: "\v",
                8: "\f",
            }
            for index, replacement in whitespace_cases.items():
                invalid = list(base_args)
                invalid[0] = f"invalid-whitespace-{index}"
                invalid[index] = replacement
                with pytest.raises(asyncpg.CheckViolationError):
                    await app.fetchrow(begin_sql, *invalid)
            visible_v = list(base_args)
            visible_v[0] = "visible-v-control"
            visible_v[3] = "v"
            assert (await app.fetchrow(begin_sql, *visible_v))["outcome"] == "inserted"

            grace_ids = []
            for label, timeout, expected_seconds in (
                ("floor", Decimal("1"), Decimal("31")),
                ("cap", Decimal("900"), Decimal("1200")),
            ):
                args = list(base_args)
                args[0] = f"grace-{label}"
                args[11] = timeout
                created = await app.fetchrow(begin_sql, *args)
                grace_ids.append(created["attempt_id"])
                async with owner.transaction():
                    await owner.execute(
                        "SELECT set_config('app.current_tenant_id',$1,true)",
                        str(meter.PLATFORM_QC_TENANT_ID))
                    delta = await owner.fetchval(
                        """SELECT extract(epoch FROM deadline_at-attempted_at)
                           FROM public.platform_qc_usage WHERE id=$1""",
                        created["attempt_id"])
                assert delta == expected_seconds
                await app.fetchrow(
                    "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                    created["attempt_id"])

            assert await app.fetchval(
                "SELECT current_setting('app.current_tenant_id', true) IS NULL")

            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.fetchval("SELECT count(*) FROM public.platform_qc_usage")
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(
                    "INSERT INTO public.platform_qc_kill(event,reason_code,actor) "
                    "VALUES ('cleared','forbidden','operator')")

            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                await owner.execute("DELETE FROM public.jobs WHERE id=$1", job_uuid)
                stored = await owner.fetchrow(
                    "SELECT tenant_id, gl_opex_code FROM public.platform_qc_usage WHERE id=$1",
                    row["attempt_id"])
            assert stored["tenant_id"] == meter.PLATFORM_QC_TENANT_ID
            assert stored["gl_opex_code"] == credit_catalog.PLATFORM_QC_GL_OPEX_CODE
            for attempt_id in (
                row["attempt_id"], queue_row["attempt_id"],
                (await app.fetchrow(begin_sql, *visible_v))["attempt_id"],
            ):
                await app.fetchrow(
                    "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                    attempt_id)

            direct_insert = """INSERT INTO public.platform_qc_usage(
                   tenant_id,run_id,job_uuid,job_external_id,phase,unit_index,
                   attempt_ordinal,attempt_timeout_s,deadline_at,provider,
                   model_upstream,pricing_version,rate_in_usd_per_m,
                   rate_out_usd_per_m,gl_opex_code,cost_state,cost_usd,
                   cost_resolved_at,provider_reported_cost_usd,tokens_in,tokens_out)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,now()+interval '60s',$9,$10,$11,
                      $12,$13,'6740',$14,$15,
                      CASE WHEN $14='known' THEN now() ELSE NULL END,$16,$17,$18)"""
            direct_base = [
                meter.PLATFORM_QC_TENANT_ID,
                "direct-bounds",
                UUID("22222222-2222-4222-8222-222222222299"),
                "direct-job",
                "direct-phase",
                0,
                1,
                Decimal("30"),
                "fake",
                "fake-model",
                "test-v1",
                Decimal("0.075"),
                Decimal("0.150"),
                "unknown",
                None,
                None,
                None,
                None,
            ]
            direct_bound_cases = (
                (1, None), (1, "r" * 65),
                (2, None),
                (3, ""), (3, "j" * 65),
                (4, ""), (4, "p" * 65),
                (5, -1), (5, 10001),
                (6, 0), (6, 1001),
                (7, Decimal("0")), (7, Decimal("901")),
                (8, ""), (8, "p" * 65),
                (9, ""), (9, "m" * 129),
                (10, ""), (10, "v" * 65),
                (11, Decimal("-1")), (11, Decimal("10001")),
                (12, Decimal("-1")), (12, Decimal("10001")),
                (15, Decimal("-1")), (15, Decimal("101")),
                (16, -1), (16, 10000001),
                (17, -1), (17, 10000001),
            )
            for case_number, (index, replacement) in enumerate(direct_bound_cases):
                invalid = list(direct_base)
                invalid[1] = f"direct-bound-{case_number}"
                invalid[index] = replacement
                with pytest.raises((
                    asyncpg.CheckViolationError,
                    asyncpg.NotNullViolationError,
                )):
                    async with owner.transaction():
                        await owner.execute(
                            "SELECT set_config('app.current_tenant_id',$1,true)",
                            str(meter.PLATFORM_QC_TENANT_ID))
                        await owner.execute(direct_insert, *invalid)

            for case_number, cost in enumerate((Decimal("-1"), Decimal("101"))):
                invalid = list(direct_base)
                invalid[1] = f"direct-cost-bound-{case_number}"
                invalid[13] = "known"
                invalid[14] = cost
                with pytest.raises(asyncpg.CheckViolationError):
                    async with owner.transaction():
                        await owner.execute(
                            "SELECT set_config('app.current_tenant_id',$1,true)",
                            str(meter.PLATFORM_QC_TENANT_ID))
                        await owner.execute(direct_insert, *invalid)

            resolve_bound_args = list(base_args)
            resolve_bound_args[0] = "resolve-bounds"
            resolve_row = await app.fetchrow(begin_sql, *resolve_bound_args)
            for tokens_in, tokens_out, provider_cost, code in (
                (-1, 0, None, "tokens_in"),
                (10000001, 0, None, "tokens_in"),
                (0, -1, None, "tokens_out"),
                (0, 10000001, None, "tokens_out"),
                (0, 0, Decimal("-1"), "provider_reported_cost"),
                (0, 0, Decimal("101"), "provider_reported_cost"),
            ):
                with pytest.raises(asyncpg.CheckViolationError) as error:
                    await app.fetchrow(
                        "SELECT * FROM public.platform_qc_resolve_cost($1,$2,$3,$4)",
                        resolve_row["attempt_id"], tokens_in, tokens_out, provider_cost)
                assert error.value.message == f"platform_qc_bounds:{code}"

            overflow_args = list(base_args)
            overflow_args[0] = "resolve-cost-overflow"
            overflow_args[9] = Decimal("10000")
            overflow_args[10] = Decimal("10000")
            overflow_row = await app.fetchrow(begin_sql, *overflow_args)
            with pytest.raises(asyncpg.CheckViolationError) as error:
                await app.fetchrow(
                    "SELECT * FROM public.platform_qc_resolve_cost($1,10000000,10000000,NULL)",
                    overflow_row["attempt_id"])
            assert error.value.message == "platform_qc_bounds:cost_usd"
            for bounded_attempt in (resolve_row, overflow_row):
                await app.fetchrow(
                    "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                    bounded_attempt["attempt_id"])

            # FORCE RLS is measured as a non-superuser owner, never as qcadmin.
            assert await isolated_owner.fetchval(
                "SELECT count(*) FROM public.platform_qc_usage") == 0
            assert await isolated_owner.fetchval(
                "SELECT count(*) FROM public.platform_qc_kill") == 0
            async with isolated_owner.transaction():
                await isolated_owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                assert await isolated_owner.fetchval(
                    "SELECT count(*) FROM public.platform_qc_usage") >= 1
                assert await isolated_owner.fetchval(
                    "SELECT count(*) FROM public.platform_qc_kill") >= 1

            # Match a different tenant GUC so RLS admits the statement; the sentinel CHECK,
            # not row security, must then reject the non-platform attribution.
            other_tenant = UUID("33333333-3333-4333-8333-333333333333")
            with pytest.raises(asyncpg.CheckViolationError):
                async with isolated_owner.transaction():
                    await isolated_owner.execute(
                        "SELECT set_config('app.current_tenant_id',$1,true)",
                        str(other_tenant))
                    await isolated_owner.execute(
                        """INSERT INTO public.platform_qc_usage(
                               tenant_id,run_id,job_uuid,phase,unit_index,attempt_ordinal,
                               attempt_timeout_s,deadline_at,provider,model_upstream,
                               pricing_version,rate_in_usd_per_m,rate_out_usd_per_m,
                               gl_opex_code)
                           VALUES($1,'non-sentinel',$2,'live-test',0,1,30,now()+interval '60s',
                                  'fake','fake-model','test-v1',0.075,0.150,'6740')""",
                        other_tenant, UUID("44444444-4444-4444-8444-444444444444"))

            app_grants = await owner.fetch(
                """SELECT routine_name
                     FROM information_schema.role_routine_grants
                    WHERE specific_schema='public' AND grantee='app_user'
                      AND routine_name LIKE 'platform_qc_%'
                    ORDER BY 1""")
            names = [record["routine_name"] for record in app_grants]
            assert names == [
                "platform_qc_arm_kill",
                "platform_qc_begin_attempt",
                "platform_qc_finish_attempt",
                "platform_qc_kill_is_armed",
                "platform_qc_resolve_cost",
            ]
            assert await reaper.fetchval("SELECT current_user") == "platform_qc_reaper"
            assert await owner.fetchval(
                "SELECT rolbypassrls FROM pg_roles WHERE rolname='platform_qc_reaper'") is False
            assert await app.fetchval(
                "SELECT has_function_privilege(current_user,"
                "'public.platform_qc_reap()','EXECUTE')") is False
            assert await reaper.fetchval(
                "SELECT has_function_privilege(current_user,"
                "'public.platform_qc_arm_kill(text)','EXECUTE')") is False
        finally:
            await reaper.close()
            await app.close()
            await isolated_owner.close()
            await owner.close()

    asyncio.run(run())


@pytest.mark.skipif(
    not all(os.getenv(name) for name in (
        "TEST_DATABASE_URL", "TEST_APP_DATABASE_URL", "TEST_REAPER_DATABASE_URL")),
    reason="disposable PostgreSQL URLs not supplied; static contract still runs",
)
def test_live_cost_lifecycle_reaper_and_kill_contract():
    asyncpg = pytest.importorskip("asyncpg")

    async def run():
        owner = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        app1 = await asyncpg.connect(os.environ["TEST_APP_DATABASE_URL"])
        app2 = await asyncpg.connect(os.environ["TEST_APP_DATABASE_URL"])
        reaper = await asyncpg.connect(os.environ["TEST_REAPER_DATABASE_URL"])

        async def platform_event(event, reason):
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                await owner.execute(
                    """INSERT INTO public.platform_qc_kill(event,reason_code,actor)
                       VALUES($1,$2,'operator')""", event, reason)

        async def begin(
            connection, run_id, job_uuid, *, unit=0, ordinal=1,
            timeout=Decimal("30"), rate_in=Decimal("0.075"),
            rate_out=Decimal("0.150"),
        ):
            return await connection.fetchrow(
                """SELECT * FROM public.platform_qc_begin_attempt(
                     $1,$2,'live-job','canon_lite_l2_extract',$3,$4,
                     'fake','fake-model','test-v1',$5,$6,$7)""",
                run_id, job_uuid, unit, ordinal, rate_in, rate_out, timeout)

        customer_tenant = UUID("60000000-0000-4000-8000-000000000001")

        async def customer_surfaces():
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(customer_tenant))
                return (
                    await owner.fetchval(
                        "SELECT count(*) FROM public.credit_ledger WHERE tenant_id=$1",
                        customer_tenant),
                    await owner.fetchval(
                        "SELECT count(*) FROM public.usage_logs WHERE tenant_id=$1",
                        customer_tenant),
                    await owner.fetchval(
                        "SELECT count(*) FROM public.journal_entries WHERE tenant_id=$1",
                        customer_tenant),
                    len(await owner.fetch(
                        """SELECT id FROM public.usage_logs
                           WHERE tenant_id=$1 ORDER BY created_at DESC""",
                        customer_tenant)),
                    await owner.fetchval(
                        "SELECT count(*) FROM public.platform_qc_usage"),
                )

        try:
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(customer_tenant))
                await owner.execute(
                    """INSERT INTO public.tenants(id,name,slug,email,plan,is_active)
                       VALUES($1,'H1 active customer','h1-active-customer',
                              'h1-active@wimba.invalid','starter',true)""",
                    customer_tenant)
                await owner.execute(
                    """INSERT INTO public.credit_ledger(
                           tenant_id,delta,reason,op_id,balance_after)
                       VALUES($1,10,'topup','h1-active-ledger',10)""",
                    customer_tenant)
                await owner.execute(
                    """INSERT INTO public.usage_logs(
                           tenant_id,endpoint,model_alias,model_upstream,provider,
                           tokens_in,tokens_out,cost_usd,credits,is_paid)
                       VALUES($1,'image','fake-customer','fake-customer','other',
                              1,1,0.01,1,true)""",
                    customer_tenant)
            before_customer = await customer_surfaces()
            assert before_customer == (1, 1, 0, 1, 0)
            rounding = await owner.fetchrow(
                """SELECT round(2.5::numeric,0), round(3.5::numeric,0),
                          round((-2.5)::numeric,0), round(2.5::double precision)""")
            assert tuple(rounding) == (
                Decimal("3"), Decimal("4"), Decimal("-3"), 2.0)

            await platform_event("cleared", "live_contract_start")
            # Establish a clean lifecycle baseline. Other live tests deliberately leave an
            # attempted row behind, and wall-clock delay must not change this test's count.
            await reaper.fetchval("SELECT public.platform_qc_reap()")

            # Cost payload replay is scale-normalised, but a surviving difference conflicts.
            cost_attempt = await begin(
                app1, "live-cost",
                UUID("50000000-0000-4000-8000-000000000001"))
            first = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,1000,500,$2)",
                cost_attempt["attempt_id"], Decimal("0.010000"))
            same = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,1000,500,$2)",
                cost_attempt["attempt_id"], Decimal("0.0100004"))
            conflict = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,1000,500,$2)",
                cost_attempt["attempt_id"], Decimal("0.0101"))
            assert (first["outcome"], same["outcome"], conflict["outcome"]) == (
                "applied", "already_same", "conflict")
            await app1.fetchrow(
                "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                cost_attempt["attempt_id"])
            finish_loser = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_finish_attempt($1,'failed')",
                cost_attempt["attempt_id"])
            assert tuple(finish_loser) == (False, "succeeded")

            missing = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,0,0,NULL)",
                UUID("50000000-0000-4000-8000-000000000099"))
            assert missing["outcome"] == "missing"
            assert await app1.fetchval(
                "SELECT public.platform_qc_kill_is_armed()") is True

            # Force two resolvers to take the same pre-CAS snapshot. Equal computed cost with
            # different token payloads must produce one applied and one conflict.
            await platform_event("cleared", "before_concurrent_cost")
            raced = await begin(
                app1, "live-cost-race",
                UUID("50000000-0000-4000-8000-000000000002"),
                rate_out=Decimal("0.300"))
            with pytest.raises(asyncpg.CheckViolationError):
                async with owner.transaction():
                    await owner.execute(
                        "SELECT set_config('app.current_tenant_id',$1,true)",
                        str(meter.PLATFORM_QC_TENANT_ID))
                    await owner.execute(
                        """UPDATE public.platform_qc_usage
                              SET cost_usd=0
                            WHERE id=$1 AND cost_state='unknown'""",
                        raced["attempt_id"])
            tx = owner.transaction()
            await tx.start()
            await owner.execute(
                "SELECT set_config('app.current_tenant_id',$1,true)",
                str(meter.PLATFORM_QC_TENANT_ID))
            await owner.fetchval(
                "SELECT id FROM public.platform_qc_usage WHERE id=$1 FOR UPDATE",
                raced["attempt_id"])
            resolve1 = asyncio.create_task(app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,1000,500,NULL)",
                raced["attempt_id"]))
            resolve2 = asyncio.create_task(app2.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,2000,250,NULL)",
                raced["attempt_id"]))
            await asyncio.sleep(0.02)
            await tx.commit()
            outcomes = {row["outcome"] for row in await asyncio.gather(resolve1, resolve2)}
            assert outcomes == {"applied", "conflict"}
            await app1.fetchrow(
                "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                raced["attempt_id"])

            # Three stale rows are swept before any cache classification. A late finish loses
            # lifecycle CAS, while cost still resolves independently on the timed-out row.
            stale_ids = []
            for index in range(3):
                row = await begin(
                    app1, f"live-stale-{index}",
                    UUID(f"50000000-0000-4000-8000-{index + 10:012d}"),
                    timeout=Decimal("1"))
                stale_ids.append(row["attempt_id"])
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                await owner.execute(
                    "UPDATE public.platform_qc_usage SET deadline_at=now()-interval '1s' "
                    "WHERE id=ANY($1::uuid[])", stale_ids)
            assert await reaper.fetchval("SELECT public.platform_qc_reap()") == 3
            late = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_finish_attempt($1,'succeeded')",
                stale_ids[0])
            late_cost = await app1.fetchrow(
                "SELECT * FROM public.platform_qc_resolve_cost($1,1,1,NULL)",
                stale_ids[0])
            assert (late["lifecycle_applied"], late["current_state"]) == (False, "timeout")
            assert late_cost["outcome"] == "applied"

            for invalid in (None, "", "ARMED", "armed ", "banana", "0"):
                row = await reaper.fetchrow(
                    "SELECT * FROM public.platform_qc_kill_sync($1::text)", invalid)
                assert tuple(row) == (False, "invalid_cache_state", True)
            unreadable = await reaper.fetchrow(
                "SELECT * FROM public.platform_qc_kill_sync('unreadable')")
            assert tuple(unreadable) == (False, "cache_unreadable", True)

            # Rehearse reset ordering with a local negative-only cache. Delete-first cannot
            # clear an armed authority. The ratified A→B→C→D sequence does.
            await app1.fetchrow(
                "SELECT * FROM public.platform_qc_arm_kill('reset_rehearsal')")
            cache = {meter.KILL_KEY: "1", meter.INFLIGHT_KEY: "0"}
            cache.pop(meter.KILL_KEY)
            delete_first = await reaper.fetchrow(
                "SELECT * FROM public.platform_qc_kill_sync('absent')")
            assert tuple(delete_first) == (False, "cache_absent", True)
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                attempted_rows = await owner.fetchval(
                    """SELECT count(*) FROM public.platform_qc_usage
                       WHERE attempt_state='attempted'""")
            assert attempted_rows == 0
            assert meter.phase_a_ready(
                l2b_enabled=False,
                inflight_raw=cache[meter.INFLIGHT_KEY],
                inflight_unreadable=False,
                attempted_rows=attempted_rows,
                max_inflight=8,
            ).passes
            note = {
                "recorded_at_utc": "2026-08-02T00:00:00Z",
                "operator": "Rino Yufahri",
                **{field: True for field in meter.RESET_ATTESTATION_FIELDS},
            }
            assert meter.validate_reset_attestation(note) == (True, ())
            cache[meter.KILL_KEY] = "1"
            await platform_event("clearing", "reset_phase_c_started")
            cache.pop(meter.KILL_KEY)
            assert cache.get(meter.KILL_KEY) is None
            await platform_event("cleared", "reset_phase_c_complete")
            assert await app1.fetchval(
                "SELECT public.platform_qc_kill_is_armed()") is False

            # A cleared authority plus armed cache is repaired. An abandoned clearing state
            # is reported on every run and never repaired.
            await platform_event("cleared", "before_repair")
            repaired = await reaper.fetchrow(
                "SELECT * FROM public.platform_qc_kill_sync('armed')")
            assert tuple(repaired) == (True, None, True)
            await platform_event("clearing", "operator_reset_started")
            for _ in range(2):
                clearing = await reaper.fetchrow(
                    "SELECT * FROM public.platform_qc_kill_sync('armed')")
                assert tuple(clearing) == (False, "clearing", True)

            # Concurrent application arms serialize and append one row. Newest id, not an
            # out-of-order timestamp, defines state.
            await platform_event("cleared", "before_concurrent_arm")
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                before = await owner.fetchval(
                    "SELECT count(*) FROM public.platform_qc_kill")
            armed1, armed2 = await asyncio.gather(
                app1.fetchrow("SELECT * FROM public.platform_qc_arm_kill('race_one')"),
                app2.fetchrow("SELECT * FROM public.platform_qc_arm_kill('race_two')"))
            assert {armed1["was_already_armed"], armed2["was_already_armed"]} == {False, True}
            async with owner.transaction():
                await owner.execute(
                    "SELECT set_config('app.current_tenant_id',$1,true)",
                    str(meter.PLATFORM_QC_TENANT_ID))
                after = await owner.fetchval(
                    "SELECT count(*) FROM public.platform_qc_kill")
                await owner.execute(
                    """INSERT INTO public.platform_qc_kill(
                           event,reason_code,actor,created_at)
                       VALUES('cleared','older_timestamp_newer_id','operator',
                              now()-interval '1 day')""")
            assert after == before + 1
            assert await app1.fetchval(
                "SELECT public.platform_qc_kill_is_armed()") is False

            # All destructive statements are trigger-blocked. Empty-table fail-closed is
            # exercised only after deliberately disabling those triggers in a rolled-back tx.
            for statement in (
                "UPDATE public.platform_qc_kill SET reason_code='x' WHERE id=(SELECT max(id) FROM public.platform_qc_kill)",
                "DELETE FROM public.platform_qc_kill WHERE id=(SELECT max(id) FROM public.platform_qc_kill)",
                "TRUNCATE public.platform_qc_kill",
            ):
                with pytest.raises(asyncpg.RaiseError):
                    async with owner.transaction():
                        await owner.execute(
                            "SELECT set_config('app.current_tenant_id',$1,true)",
                            str(meter.PLATFORM_QC_TENANT_ID))
                        await owner.execute(statement)

            tx = owner.transaction()
            await tx.start()
            await owner.execute(
                "SELECT set_config('app.current_tenant_id',$1,true)",
                str(meter.PLATFORM_QC_TENANT_ID))
            await owner.execute(
                "ALTER TABLE public.platform_qc_kill DISABLE TRIGGER USER")
            await owner.execute("DELETE FROM public.platform_qc_kill")
            assert await owner.fetchval(
                "SELECT public.platform_qc_kill_is_armed()") is True
            await tx.rollback()
            assert await customer_surfaces() == before_customer
        finally:
            await reaper.close()
            await app2.close()
            await app1.close()
            await owner.close()

    asyncio.run(run())
