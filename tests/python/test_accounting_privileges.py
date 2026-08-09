"""
Guards for audit findings §17.1 (deferred-revenue silent fallback) and §17.2
(app_user privilege / RLS holes on the accounting surface).

DESIGN NOTE — why these are file-based and not database-based.
Audit F-07 found that the financial suite is unreachable from any build, deploy or CI
entrypoint, and that its DB-backed tests skip silently when TEST_DATABASE_URL is unset —
so a regression could land fully green. Every assertion below therefore runs against the
migration SQL itself and needs NO database, NO network and NO driver. The optional live
section at the bottom is strictly additional; it never replaces a static guard.

What these tests are actually binding:
  * the two defects are fixed in the shipped SQL, and
  * the specific shapes that CAUSED them cannot come back.
They do not prove deferred revenue is correct — it is not, and 0071 says so in its header.
"""
import asyncio
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "database" / "migrations"

M0070 = MIGRATIONS / "0070_gl_privilege_hardening.sql"
M0071 = MIGRATIONS / "0071_deferred_revenue_no_silent_fallback.sql"
M0074 = MIGRATIONS / "0074_platform_qc_metering.sql"
M0087 = MIGRATIONS / "0087_cr29_g3_privilege_and_rls_hardening.sql"


def _sql(path: Path) -> str:
    assert path.exists(), f"missing migration: {path.relative_to(REPO)}"
    return path.read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Drop -- line comments so assertions bind executable SQL, not prose.

    Several of these migrations quote the very anti-patterns they remove inside their
    header comments (e.g. the literal `ELSE 248`). Matching raw text would let a comment
    satisfy — or falsely fail — a test that is supposed to be about executable statements.
    """
    return re.sub(r"--[^\n]*", "", sql)


def _view_definition(sql: str, view: str) -> str:
    """Isolate ONE view's statement.

    Necessary because 0071 ships two views, and a whole-file scan would let the diagnostic
    companion's expressions answer for the liability view (an earlier draft of these tests
    did exactly that and reported a fallback that was not in the priced view at all).
    """
    m = re.search(rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+{view}\s+AS(.*?);", sql, re.I | re.S)
    assert m, f"could not isolate the definition of {view}"
    return m.group(1)


def _outer_select_list(definition: str) -> str:
    """The final SELECT list, excluding any CTE that precedes it.

    Aliases inside a WITH block are not output columns; conflating the two is what made the
    column-order guard read `price_idr_per_credit` as the first column of the view.
    """
    m = re.search(r"\)\s*SELECT(.*?)\bFROM\b", definition, re.I | re.S)
    return m.group(1) if m else definition


def _last_migration_defining(view: str) -> Path:
    """The migration that OWNS a view's current definition.

    Migrations are immutable and append-only, so an older file legitimately still contains
    the old broken definition. Only the highest-numbered definer describes live behaviour.
    """
    hits = sorted(
        p for p in MIGRATIONS.glob("*.sql")
        if re.search(rf"CREATE\s+OR\s+REPLACE\s+VIEW\s+{view}\b", p.read_text(encoding="utf-8"), re.I)
        or re.search(rf"CREATE\s+VIEW\s+{view}\b", p.read_text(encoding="utf-8"), re.I)
    )
    assert hits, f"no migration defines view {view}"
    return hits[-1]


# =====================================================================
# §17.1 — v_deferred_revenue must never invent a price
# =====================================================================

def test_deferred_revenue_owned_by_0071():
    assert _last_migration_defining("v_deferred_revenue").name == M0071.name


def test_no_numeric_else_fallback_in_live_view():
    """The exact defect: `ELSE 248` priced every unknown plan at a number that is nobody's.

    Any `ELSE <number>` inside the live definition reintroduces it. `ELSE NULL` is the fix
    and must stay allowed.
    """
    live = _strip_sql_comments(_sql(_last_migration_defining("v_deferred_revenue")))
    for view in ("v_deferred_revenue", "v_deferred_revenue_by_plan"):
        body = _view_definition(live, view)
        offenders = re.findall(r"\bELSE\s+([0-9][0-9_.]*)", body, re.I)
        assert not offenders, (
            f"{view} prices unknown plans from a numeric fallback ({offenders}); "
            "unknown plans must price to NULL so unpriced_credits surfaces them"
        )
        assert re.search(r"\bELSE\s+NULL\b", body, re.I), (
            f"{view} needs an explicit ELSE NULL branch")


def test_view_exposes_incompleteness():
    """A NULL price is only useful if callers can SEE that something went unpriced."""
    body = _strip_sql_comments(_sql(M0071))
    for col in ("unpriced_credits", "priced_credits", "is_complete"):
        assert re.search(rf"\bAS\s+{col}\b", body, re.I), f"view must expose {col}"


def test_backward_compatible_column_prefix():
    """CREATE OR REPLACE VIEW only permits APPENDING columns.

    If the original two ever stop leading, the migration fails at deploy time rather than
    in review, and any external reader (the admin console lives in another repo and was not
    searched) breaks silently. Bind the order here.
    """
    body = _strip_sql_comments(_sql(M0071))
    outer = _outer_select_list(_view_definition(body, "v_deferred_revenue"))
    aliases = [a.lower() for a in re.findall(r"\bAS\s+([a-z_][a-z0-9_]*)\b", outer, re.I)]
    assert aliases[:2] == ["outstanding_credits", "deferred_revenue_idr"], (
        f"original columns must stay first and keep their names; got {aliases[:2]}"
    )
    assert aliases[2:] == ["priced_credits", "unpriced_credits", "is_complete"], (
        f"new columns must be appended after the original two; got {aliases[2:]}"
    )


def test_every_allowed_plan_is_priced_or_explicitly_unpriced():
    """Binds the view's plan vocabulary to the tenants.plan CHECK constraint.

    This is the test that would have caught the original bug: `plus` and `ultra` were added
    to the CHECK in 0042 and sold via Dodo, while the view still only knew
    starter/pro/enterprise — and the fallback hid it. With no fallback, an unknown plan can
    only ever price to NULL, which is safe. The assertion is therefore: no fallback exists,
    and the plans the view DOES price are a subset of the plans the schema permits.
    """
    check_sql = _strip_sql_comments(_sql(MIGRATIONS / "0042_tenant_plan_global_tiers.sql"))
    m = re.search(r"plan\s+IN\s*\(([^)]*)\)", check_sql, re.I)
    assert m, "could not read the tenants.plan CHECK vocabulary from 0042"
    allowed = {p.strip().strip("'") for p in m.group(1).split(",")}
    assert {"plus", "ultra"} <= allowed, "0042 should permit the plus/ultra tiers"

    body = _strip_sql_comments(_sql(M0071))
    priced = {p.lower() for p in re.findall(r"\bWHEN\s+'([a-z_]+)'\s+THEN\s+[0-9]", body, re.I)}
    assert priced, "the view should still price the plans it genuinely knows"
    unknown = priced - allowed
    assert not unknown, f"view prices plans the schema does not permit: {sorted(unknown)}"

    # plus/ultra remain deliberately unpriced — 0071 refuses to invent a figure.
    # If someone later supplies real prices, this assertion is the place to update.
    assert not ({"plus", "ultra"} & priced), (
        "plus/ultra were given prices — confirm they came from the pricing owner and are "
        "consistent with credit_catalog, then update this guard"
    )


# =====================================================================
# §17.2 — privilege and RLS hardening
# =====================================================================

def test_gl_accounts_is_read_only_to_app_user():
    body = _strip_sql_comments(_sql(M0070))
    assert re.search(
        r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE,\s*TRUNCATE\s+ON\s+gl_accounts\s+FROM\s+app_user",
        body, re.I), "0070 must revoke write privileges on the chart of accounts"
    assert re.search(r"GRANT\s+SELECT\s+ON\s+gl_accounts\s+TO\s+app_user", body, re.I)


def test_orphan_reversals_keeps_the_privileges_its_runtime_needs():
    """Guards against over-revoking.

    payments_core.mjs does SELECT (:223), UPDATE (:233) and INSERT (:268) on this queue.
    Revoking any of those would break refund handling in production. Only DELETE/TRUNCATE —
    which no caller uses, and which are the ones that can destroy refund evidence — go.
    """
    body = _strip_sql_comments(_sql(M0070))
    assert re.search(r"REVOKE\s+DELETE,\s*TRUNCATE\s+ON\s+orphan_reversals\s+FROM\s+app_user",
                     body, re.I)
    forbidden = re.findall(
        r"REVOKE\s+([^;]*?)\s+ON\s+orphan_reversals\s+FROM\s+app_user", body, re.I)
    for clause in forbidden:
        for priv in ("INSERT", "UPDATE", "SELECT"):
            assert priv not in clause.upper(), (
                f"0070 revokes {priv} on orphan_reversals — payments_core.mjs needs it"
            )


def test_journal_entries_rls_is_enabled_and_forced():
    body = _strip_sql_comments(_sql(M0070))
    assert re.search(r"ALTER\s+TABLE\s+journal_entries\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
                     body, re.I)
    assert re.search(r"ALTER\s+TABLE\s+journal_entries\s+FORCE\s+ROW\s+LEVEL\s+SECURITY",
                     body, re.I), "ENABLE alone is a no-op for the table owner; FORCE is required"
    assert re.search(r"CREATE\s+POLICY\s+tenant_isolation\s+ON\s+journal_entries", body, re.I)


def test_journal_entries_policy_hides_company_level_rows():
    """tenant_id IS NULL means a company-level entry (0031:157).

    The policy must not accidentally expose those to app_user via an `IS NOT DISTINCT FROM`
    or an OR-NULL escape hatch; plain equality is false for NULL, which is what we want.
    """
    body = _strip_sql_comments(_sql(M0070))
    policy = re.search(
        r"CREATE\s+POLICY\s+tenant_isolation\s+ON\s+journal_entries(.*?);", body, re.I | re.S)
    assert policy, "journal_entries tenant_isolation policy not found"
    text = policy.group(1)
    assert "IS NOT DISTINCT FROM" not in text.upper()
    assert not re.search(r"tenant_id\s+IS\s+NULL", text, re.I), (
        "policy admits NULL tenant_id — company-level entries would become app-readable"
    )


def test_payments_full_dml_is_left_alone():
    """Negative guard: `payments` having full DML is a DELIBERATE decision at 0031:679-680,
    and it is RLS-forced with a tenant_isolation policy. Revoking it here would break the
    intended design. This test fails if a future edit "hardens" it by mistake.
    """
    body = _strip_sql_comments(_sql(M0070))
    assert not re.search(r"REVOKE[^;]*\bON\s+payments\b", body, re.I), (
        "0070 must not touch payments — its full DML is intentional and RLS-confined"
    )


def test_detector_script_ships():
    """The detector runs against production, so it must not be able to mutate anything.

    Checking for the bare substring 'DELETE ' is the wrong mechanism and an earlier version
    of this test failed on its own prose: the script legitimately *describes* DELETE inside
    \\echo output. What matters is whether a mutating keyword ever STARTS a statement, so
    strip comments and \\echo lines first, then look at statement-leading tokens only.
    """
    check = REPO / "database" / "checks" / "app_user_privilege_audit.sql"
    body = _sql(check)
    assert "BEGIN READ ONLY" in body, "the detector must be read-only to be prod-safe"

    executable = _strip_sql_comments(body)
    executable = "\n".join(
        ln for ln in executable.splitlines() if not ln.lstrip().startswith("\\"))

    mutating = ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "GRANT", "REVOKE",
                "TRUNCATE", "CREATE")
    for statement in executable.split(";"):
        head = statement.strip().split(None, 1)
        if head and head[0].upper() in mutating:
            pytest.fail(f"detector contains a mutating statement: {statement.strip()[:80]}")


# =====================================================================
# L2B platform-QC privilege surface (0074)
# =====================================================================

def test_platform_qc_tables_undo_0016_default_dml_grants():
    body = _strip_sql_comments(_sql(M0074))
    for relation in ("platform_qc_usage", "platform_qc_kill"):
        assert re.search(
            rf"REVOKE\s+ALL\s+ON\s+public\.{relation}\s+FROM\s+app_user",
            body, re.I), relation
    assert not re.search(
        r"GRANT\s+(SELECT|INSERT|UPDATE|DELETE|TRUNCATE).*?"
        r"ON\s+public\.platform_qc_(usage|kill)\s+TO\s+app_user",
        body, re.I | re.S)


def test_platform_qc_function_default_grants_are_closed():
    body = _strip_sql_comments(_sql(M0074))
    application = (
        "platform_qc_begin_attempt",
        "platform_qc_finish_attempt",
        "platform_qc_resolve_cost",
        "platform_qc_arm_kill",
        "platform_qc_kill_is_armed",
    )
    reaper = ("platform_qc_reap", "platform_qc_kill_sync")

    for name in application + reaper:
        assert re.search(
            rf"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+public\.{name}\s*\(",
            body, re.I), name
    for name in application:
        assert re.search(
            rf"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+public\.{name}\s*\(.*?"
            rf"\)\s+TO\s+app_user",
            body, re.I | re.S), name
    for name in reaper:
        assert re.search(
            rf"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+public\.{name}\s*\(.*?"
            rf"\)\s+TO\s+platform_qc_reaper",
            body, re.I | re.S), name

    # 0016 also auto-grants new trigger functions and identity sequences.
    assert re.search(
        r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+"
        r"public\.platform_qc_kill_append_only\(\)\s+FROM\s+PUBLIC", body, re.I)
    assert re.search(
        r"REVOKE\s+EXECUTE\s+ON\s+FUNCTION\s+"
        r"public\.platform_qc_kill_append_only\(\)\s+FROM\s+app_user", body, re.I)


def test_platform_qc_reaper_role_is_least_privilege():
    body = _strip_sql_comments(_sql(M0074))
    assert "CREATE ROLE platform_qc_reaper LOGIN NOBYPASSRLS" in body
    assert re.search(
        r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+public\s+TO\s+platform_qc_reaper",
        body, re.I)
    assert re.search(
        r"REVOKE\s+ALL\s+ON\s+ALL\s+TABLES\s+IN\s+SCHEMA\s+public\s+"
        r"FROM\s+platform_qc_reaper", body, re.I)
    assert re.search(
        r"REVOKE\s+ALL\s+ON\s+ALL\s+SEQUENCES\s+IN\s+SCHEMA\s+public\s+"
        r"FROM\s+platform_qc_reaper", body, re.I)


def test_platform_qc_audit_covers_tables_functions_sequences_role_and_rls():
    audit = _strip_sql_comments(
        _sql(REPO / "database" / "checks" / "app_user_privilege_audit.sql"))
    for marker in (
        "platform_qc_usage",
        "platform_qc_kill",
        "v_platform_qc_cost",
        "platform_qc_reaper",
        "rolbypassrls",
        "has_schema_privilege",
        "relforcerowsecurity",
    ):
        assert marker in audit
    assert "app_user must read 5, platform_qc_reaper 2, PUBLIC 0" in _sql(
        REPO / "database" / "checks" / "app_user_privilege_audit.sql")


# =====================================================================
# Optional live confirmation. Additional to, never a substitute for, the above.
# =====================================================================

# Writable + row-unprotected tables that already have a live caller, so revoking would break
# production. Declared KNOWN, not safe — each is an open decision recorded in section A2 of
# database/checks/app_user_privilege_audit.sql. Keeping the gate green today is what makes a
# genuinely new hole visible tomorrow; gl_accounts stayed open from 0029 to 0070 precisely
# because nobody was watching a list that was never clean.
ACCEPTED_UNPROTECTED_WRITABLE = {
    "orphan_reversals",          # cross-tenant by design (0044:87); payments_core.mjs:233,268
    "processed_stripe_events",   # Stripe idempotency marker; billing.mjs:222
    "narasi_known_bad_claims",   # factgate reference; python/database.py:969
    "narasi_known_good_claims",  # factgate reference; python/database.py:999
    "migrations",                # database/migrate.js:115
    # --- CR-29, added by 0087. KNOWN, NOT SAFE. Both are provider-global: keyed by
    # payment identity, which exists before and independently of any tenant, so
    # neither can carry tenant RLS. A permissive USING (true) policy would turn
    # this guard green while protecting nothing, so none was added.
    "g3_provider_payments",      # provider-global payment aggregate; SELECT/INSERT/UPDATE for the
                                 # SECURITY INVOKER functions g3_pin_payment_at, g3_record_payment_terms,
                                 # g3_write_lot, g3_post_cash_in (0086), reached from
                                 # backend/g3_lots.mjs:69,88. DELETE/TRUNCATE revoked by 0087.
    "g3_payment_at_divergence_alerts",  # provider-global append-only audit; INSERT only, written by
                                 # g3_pin_payment_at (0086). No runtime reader exists, so 0087 revoked
                                 # SELECT too. UPDATE/DELETE/TRUNCATE revoked; append-only trigger kept.
}

# g3_topup_quarantine is deliberately NOT here. It carries tenant_id, so the sweep
# above never saw it — which is precisely why its missing RLS was the quietest of
# the four holes 0086 opened. 0087 gives it real FORCED tenant RLS, and
# test_g3_topup_quarantine_rls_is_enabled_and_forced below stops it regressing.


def test_detector_exception_list_matches_this_module():
    """The detector and this test must agree, or one of them silently stops guarding.

    Must compare the ACTUAL section-A exclusion list, not merely check that each name
    appears somewhere in the file. An earlier version did the latter and a mutation that
    renamed an entry in the exclusion list survived, because the name still occurred in the
    section-A2 display query further down. Presence in a file is not membership in a set.
    """
    # Comments must go first: the exclusion list annotates each entry with a source
    # reference like "(0044:87)", and an unstripped [^)]* stops dead at that parenthesis,
    # silently capturing only the first name.
    check = _strip_sql_comments(_sql(REPO / "database" / "checks" / "app_user_privilege_audit.sql"))
    m = re.search(r"relname\s+NOT\s+IN\s*\(([^)]*)\)", check, re.I | re.S)
    assert m, "could not locate section A's exclusion list in the detector"
    excluded = set(re.findall(r"'([a-z0-9_]+)'", m.group(1), re.I))
    assert excluded == ACCEPTED_UNPROTECTED_WRITABLE, (
        "detector's section-A exclusions and ACCEPTED_UNPROTECTED_WRITABLE have diverged.\n"
        f"  only in detector: {sorted(excluded - ACCEPTED_UNPROTECTED_WRITABLE)}\n"
        f"  only in tests:    {sorted(ACCEPTED_UNPROTECTED_WRITABLE - excluded)}"
    )


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                    reason="TEST_DATABASE_URL not set — static guards above still ran")
def test_live_no_writable_unprotected_table():
    # asyncpg is the driver the product already depends on (python/database.py); psycopg is
    # not installed here, and importorskip on it would turn this into a silent no-op — the
    # exact "skips green" failure mode audit F-07 flagged.
    asyncpg = pytest.importorskip("asyncpg")
    sql = """
        SELECT c.relname
          FROM pg_class c
         WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r'
           AND NOT c.relrowsecurity
           AND NOT EXISTS (SELECT 1 FROM information_schema.columns col
                            WHERE col.table_schema='public' AND col.table_name=c.relname
                              AND col.column_name='tenant_id')
           AND EXISTS (SELECT 1 FROM information_schema.role_table_grants g
                        WHERE g.table_schema='public' AND g.table_name=c.relname
                          AND g.grantee='app_user'
                          AND g.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE'))
         ORDER BY 1
    """
    async def _fetch():
        conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        try:
            async with conn.transaction(readonly=True):
                return [r[0] for r in await conn.fetch(sql)]
        finally:
            await conn.close()

    rows = asyncio.run(_fetch())
    unexpected = sorted(set(rows) - ACCEPTED_UNPROTECTED_WRITABLE)
    assert unexpected == [], (
        f"app_user can write these unprotected tables: {unexpected}. Either revoke the write "
        f"privileges, give the table RLS, or add it to ACCEPTED_UNPROTECTED_WRITABLE with the "
        f"caller that needs it."
    )
    # The gl_accounts hole this migration closed must never come back.
    assert "gl_accounts" not in rows


# ─────────────────────────────────────────────────────────────────────────────
# CR-29 / 0087 — the four tables 0086 created
#
# Three were caught by the sweep above and are now on the accepted list with their
# callers. The fourth, g3_topup_quarantine, was NOT caught: it carries tenant_id,
# so the sweep skipped it, and it had no RLS at all — app_user could read and write
# every tenant's quarantine rows. These guards exist so that hole cannot reopen
# quietly, which is exactly how it opened in the first place.
# ─────────────────────────────────────────────────────────────────────────────

def test_g3_topup_quarantine_rls_is_enabled_and_forced():
    body = _strip_sql_comments(_sql(M0087))
    assert re.search(r"ALTER\s+TABLE\s+g3_topup_quarantine\s+ENABLE\s+ROW\s+LEVEL\s+SECURITY",
                     body, re.I)
    assert re.search(r"ALTER\s+TABLE\s+g3_topup_quarantine\s+FORCE\s+ROW\s+LEVEL\s+SECURITY",
                     body, re.I), "ENABLE alone is a no-op for the table owner; FORCE is required"
    policy = re.search(
        r"CREATE\s+POLICY\s+tenant_isolation\s+ON\s+g3_topup_quarantine(.*?);", body, re.I | re.S)
    assert policy, "g3_topup_quarantine tenant_isolation policy not found"
    clause = policy.group(1)
    # Both halves matter: USING alone filters reads while leaving cross-tenant
    # INSERT/UPDATE wide open.
    assert re.search(r"USING\s*\(", clause, re.I), "policy must constrain reads"
    assert re.search(r"WITH\s+CHECK\s*\(", clause, re.I), "policy must constrain writes too"
    assert clause.upper().count("APP.CURRENT_TENANT_ID") >= 2, \
        "both USING and WITH CHECK must key on app.current_tenant_id"


def test_g3_new_tables_lose_the_privileges_they_inherited():
    """0016's ALTER DEFAULT PRIVILEGES grants app_user full DML on every new table.
    0087 must claw back what each of 0086's four tables does not need."""
    body = _strip_sql_comments(_sql(M0087))
    expected = {
        "g3_cr29_workaround_window":        r"REVOKE\s+INSERT,\s*UPDATE,\s*DELETE,\s*TRUNCATE\s+ON\s+g3_cr29_workaround_window\s+FROM\s+app_user",
        "g3_provider_payments":             r"REVOKE\s+DELETE,\s*TRUNCATE\s+ON\s+g3_provider_payments\s+FROM\s+app_user",
        "g3_payment_at_divergence_alerts":  r"REVOKE\s+SELECT,\s*UPDATE,\s*DELETE,\s*TRUNCATE\s+ON\s+g3_payment_at_divergence_alerts\s+FROM\s+app_user",
        "g3_topup_quarantine":              r"REVOKE\s+DELETE,\s*TRUNCATE\s+ON\s+g3_topup_quarantine\s+FROM\s+app_user",
    }
    for table, pattern in expected.items():
        assert re.search(pattern, body, re.I), f"0087 must narrow app_user on {table}"


def test_g3_trigger_only_functions_are_not_app_executable():
    body = _strip_sql_comments(_sql(M0087))
    for fn in ("g3_pp_payment_at_immutable", "g3_cr29_window_guard"):
        assert re.search(rf"REVOKE\s+ALL\s+ON\s+FUNCTION\s+public\.{fn}\(\)\s+FROM\s+PUBLIC,\s*app_user",
                         body, re.I), f"{fn} is a trigger body; app_user must not hold EXECUTE"


# The two exception lists are kept synchronized by the PRE-EXISTING
# test_detector_exception_list_matches_this_module above, which already compares the
# detector's section-A exclusion set against ACCEPTED_UNPROTECTED_WRITABLE and documents
# the comment-stripping trap that makes that comparison reliable. Adding a second copy
# here would be one more thing to drift, so the two CR-29 entries simply join the set and
# that existing guard proves the audit SQL matches.


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"),
                    reason="TEST_DATABASE_URL not set — static guards above still ran")
def test_live_g3_topup_quarantine_rejects_cross_tenant_access():
    """Non-vacuous proof, as app_user, that the policy actually isolates.

    Runs under SET ROLE app_user: the owner role in CI carries BYPASSRLS, so a test
    that stayed as the owner would pass against a table with no policy at all —
    exactly the vacuity this guard exists to avoid. The assertions below are
    written so that DROPPING the policy makes them fail.
    """
    asyncpg = pytest.importorskip("asyncpg")
    import uuid
    a, b = uuid.uuid4(), uuid.uuid4()
    pay_a, pay_b = f"pay_iso_{a.hex[:8]}", f"pay_iso_{b.hex[:8]}"

    async def _run():
        conn = await asyncpg.connect(os.environ["TEST_DATABASE_URL"])
        try:
            for t in (a, b):
                await conn.execute(
                    "INSERT INTO tenants (id,name,slug,email) VALUES ($1,$2,$3,$4)",
                    t, f"iso_{t.hex[:6]}", f"iso-{t.hex[:6]}", f"iso-{t.hex[:6]}@example.test")
            for p in (pay_a, pay_b):
                await conn.execute(
                    "INSERT INTO g3_provider_payments (provider, provider_payment_id) VALUES ('dodo',$1)", p)
            for t, p in ((a, pay_a), (b, pay_b)):
                await conn.execute(
                    "INSERT INTO g3_topup_quarantine (provider, provider_payment_id, tenant_id, reason)"
                    " VALUES ('dodo',$1,$2,'iso_fixture')", p, t)

            # Sanity: as the BYPASSRLS owner, both rows are visible. Without this the
            # "tenant A sees 1" assertion below could pass on an empty table.
            both = await conn.fetchval(
                "SELECT count(*) FROM g3_topup_quarantine WHERE reason='iso_fixture'")
            assert both == 2, f"fixture precondition failed: owner sees {both} rows, expected 2"

            await conn.execute("SET ROLE app_user")
            await conn.execute("SELECT set_config('app.current_tenant_id', $1, false)", str(a))

            seen = await conn.fetch(
                "SELECT tenant_id FROM g3_topup_quarantine WHERE reason='iso_fixture'")
            assert [r["tenant_id"] for r in seen] == [a], \
                f"cross-tenant SELECT leaked: tenant A saw {[str(r['tenant_id']) for r in seen]}"

            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.execute(
                    "INSERT INTO g3_topup_quarantine (provider, provider_payment_id, tenant_id, reason)"
                    " VALUES ('dodo',$1,$2,'iso_crosswrite')", pay_b, b)

            # UPDATE cannot raise — a row the policy hides is simply not matched — so
            # the proof is that B's row is untouched, checked back as the owner.
            await conn.execute(
                "UPDATE g3_topup_quarantine SET reason='iso_tampered' WHERE reason='iso_fixture'")
            await conn.execute("RESET ROLE")
            tampered = await conn.fetchval(
                "SELECT count(*) FROM g3_topup_quarantine WHERE tenant_id=$1 AND reason='iso_tampered'", b)
            assert tampered == 0, "cross-tenant UPDATE modified another tenant's quarantine row"
        finally:
            try:
                await conn.execute("RESET ROLE")
                await conn.execute("DELETE FROM g3_topup_quarantine WHERE provider='dodo' AND provider_payment_id = ANY($1::text[])", [pay_a, pay_b])
                await conn.execute("DELETE FROM g3_provider_payments WHERE provider='dodo' AND provider_payment_id = ANY($1::text[])", [pay_a, pay_b])
                await conn.execute("DELETE FROM tenants WHERE id = ANY($1::uuid[])", [a, b])
            finally:
                await conn.close()

    asyncio.run(_run())
