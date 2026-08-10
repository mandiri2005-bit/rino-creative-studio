#!/usr/bin/env python3
"""
T72 mutation harness.

For each mutation: break exactly ONE mechanism of `PLAN-046 §G3.3-c`,
re-provision a disposable database, apply the full migration chain, run the T72
suite, and record whether the suite FAILED. A mutation the suite does not catch
is a hole in the suite, not a curiosity.

    tests/t72/harness.sh mutate            # all
    tests/t72/harness.sh mutate M07 M12    # a subset

Verdicts:
  CAUGHT(test)      migration applied and the SUITE failed   <- what we want
  CAUGHT(migration) the migration itself refused it          <- weaker but valid
  NOT CAUGHT        green with the defect in                 <- a suite hole
  PATTERN-MISS      the mutation did not apply; harness bug, counted as failure
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
MIG = REPO / "database" / "migrations" / "0088_fxpop_controlled_fx_entry.sql"
MIG89 = REPO / "database" / "migrations" / "0089_g3_birth_path_ordered_fx_locks.sql"
MIG90 = REPO / "database" / "migrations" / "0090_g3_supplier_fee_anchor.sql"
TEST = REPO / "tests" / "python" / "test_t72_fx_rates_population_contract.py"
# 🔴 ACQUISITION WAS NOT A MUTATION TARGET UNTIL NOW, so every "N/N caught"
#    figure this harness has ever printed said nothing whatsoever about the step
#    that puts the ledger evidence in the table. Both defects the owner review
#    found — a pager speaking an invented cursor contract, and a read-back that
#    ran after the commit — sat in a file no mutation could reach.
OPS = REPO / "python" / "ops" / "provider_ledger_ingest.py"
MIG91 = REPO / "database" / "migrations" / "0091_g3_posting_engine_birth_privileges.sql"
# 🔴 0092 RESTATES TWO FUNCTIONS, SO IT IS THE SURVIVOR DEFINITION FOR BOTH.
#    `CREATE OR REPLACE` has no in-place patch, so 0092 copies
#    `g3_resolve_request` (from 0090) and `g3_write_lot_resolved` (from 0089)
#    forward in full and applies AFTER them. Mutating the ORIGINAL text therefore
#    changes nothing installed: 0092 overwrites it moments later with a clean
#    copy. Measured, not theorised — a 74-mutation run scored 60/74 and ten of
#    the twelve survivors had their pattern sitting verbatim inside 0092.
#
#    So every mutation aimed at those two functions points HERE. The rule to
#    carry forward: mutate the LAST migration that defines a function, not the
#    first. A copy-forward silently disarms every control aimed upstream of it.
MIG92 = REPO / "database" / "migrations" / "0092_g3_opening_census_scope_exemption.sql"
MIG93 = REPO / "database" / "migrations" / "0093_g3_item5_posting_boundary.sql"
HARNESS = os.environ.get("T72_HARNESS", str(HERE / "harness.sh"))

# (id, target, description, [(pattern, replacement), ...])
MUTATIONS = [
    # ── G3.3-c(2): the function contract ────────────────────────────────────
    ("M01", MIG, "fx_rates_enter loses SECURITY DEFINER",
     [(r"(FUNCTION public\.fx_rates_enter.*?PARALLEL UNSAFE\n)SECURITY DEFINER\n", r"\1")]),
    ("M02", MIG, "fx_rates_enter search_path includes public",
     [(r"(FUNCTION public\.fx_rates_enter.*?)SET search_path = pg_catalog, pg_temp",
       r"\1SET search_path = public, pg_temp")]),
    # NOTE: simply DELETING `PARALLEL UNSAFE` is not a mutation — plpgsql
    # defaults to UNSAFE, so proparallel stays 'u' and nothing has changed.
    # The real defect is declaring the definer SAFE, which would let it run in
    # a parallel worker where it cannot write.
    ("M03", MIG, "fx_rates_enter declared PARALLEL SAFE",
     [(r"(FUNCTION public\.fx_rates_enter.*?VOLATILE\n)PARALLEL UNSAFE\n", r"\1PARALLEL SAFE\n")]),
    ("M04", MIG, "enter() gains a 6th parameter (signature drift)",
     [(r"(CREATE OR REPLACE FUNCTION public\.fx_rates_enter\([^)]*?)\n\) RETURNS",
       r"\1,\n    p_note       TEXT DEFAULT NULL\n) RETURNS")]),
    ("M05", MIG, "correct() loses p_new_source / p_new_source_ref",
     [(r"    p_new_source     TEXT,\n    p_new_source_ref TEXT,\n", ""),
      (r"IF p_new_source IS NULL OR p_new_source_ref IS NULL\n"
       r"       OR length\(btrim\(p_new_source_ref\)\) = 0 THEN\n"
       r"        RAISE EXCEPTION 'fx_rates_correct: new source and source_ref are required'\n"
       r"            USING ERRCODE = 'FX004';\n    END IF;\n", ""),
      (r"source             = p_new_source,\n           source_ref         = p_new_source_ref\n",
       "source             = source,\n           source_ref         = source_ref\n"),
      (r"v_old_source, p_new_source,\n         v_old_source_ref, p_new_source_ref,",
       "v_old_source, v_old_source,\n         v_old_source_ref, v_old_source_ref,")]),

    # ── G3.3-c(2): audit columns ────────────────────────────────────────────
    ("M06", MIG, "entered_by records current_user instead of session_user",
     [(r"(p_source, p_source_ref, )session_user(, clock_timestamp\(\))", r"\1current_user\2")]),

    # ── G3.3-c(3): the correction protocol ──────────────────────────────────
    ("M07", MIG, "isolation guard removed",
     [(r"    IF current_setting\('transaction_isolation'\) <> 'read committed' THEN.*?"
       r"USING ERRCODE = 'FX003';\n    END IF;\n", "")]),
    ("M08", MIG, "XID guard moved AFTER the row lock (wrong check order)",
     [(r"    v_xid := pg_current_xact_id\(\);\n"
       r"    IF EXISTS \(SELECT 1 FROM public\.fx_rates_corrections c WHERE c\.correction_xid = v_xid\) THEN.*?"
       r"USING ERRCODE = 'FX001';\n    END IF;\n",
       "    v_xid := pg_current_xact_id();\n")]),
    ("M09", MIG, "UNIQUE (correction_xid) dropped",
     [(r"    CONSTRAINT fx_rates_corrections_xid_unique UNIQUE \(correction_xid\),\n", "")]),
    ("M10", MIG, "correction_xid stored as BIGINT instead of XID8",
     [(r"    correction_xid XID8          NOT NULL,",
       "    correction_xid BIGINT        NOT NULL,"),
      (r"    v_xid            XID8;", "    v_xid            BIGINT;"),
      (r"v_xid := pg_current_xact_id\(\);", "v_xid := pg_current_xact_id()::TEXT::BIGINT;")]),
    ("M11", MIG, "finality EXISTS folded into the locking statement",
     [(r"    SELECT EXISTS \(\n        SELECT 1 FROM public\.credit_lots l\n"
       r"         WHERE l\.fx_currency_pair = p_pair AND l\.fx_rate_date = p_rate_date\n"
       r"    \) INTO v_used;\n", "    v_used := FALSE;\n")]),

    # ── G3.3-c(4): the cross-tenant read ────────────────────────────────────
    ("M12", MIG, "credit_lots reference policy removed (reference check goes blind)",
     [(r"CREATE POLICY fx_rates_reference_check\nON public\.credit_lots FOR SELECT TO "
       r"fx_rates_owner USING \(true\);", "-- policy removed by mutation")]),
    ("M13", MIG, "reference policy widened from SELECT to ALL",
     [(r"ON public\.credit_lots FOR SELECT TO fx_rates_owner USING \(true\);",
       "ON public.credit_lots FOR ALL TO fx_rates_owner USING (true);")]),
    ("M14", MIG, "a RESTRICTIVE policy is added to credit_lots (breaks the OR-combination)",
     [(r"(GRANT SELECT ON public\.credit_lots TO fx_rates_owner;)",
       r"\1\nCREATE POLICY t72_mut_restrictive ON public.credit_lots AS RESTRICTIVE "
       r"FOR SELECT TO fx_rates_owner USING (true);")]),

    # ── G3.3-c(1): the privilege split ──────────────────────────────────────
    ("M15", MIG, "fx_rates_writer granted SELECT (must hold NO table privilege)",
     [(r"(GRANT EXECUTE ON FUNCTION public\.fx_rates_enter\(TEXT,DATE,NUMERIC,TEXT,TEXT\)   TO fx_rates_writer;)",
       r"\1\nGRANT SELECT ON fx_rates TO fx_rates_writer;")]),
    ("M16", MIG, "function REVOKE names only PUBLIC (the 0016 trap restored)",
     [(r"REVOKE ALL ON FUNCTION public\.fx_rates_enter\(TEXT,DATE,NUMERIC,TEXT,TEXT\)\n"
       r"    FROM PUBLIC, app_user, g3_posting_engine;",
       "REVOKE ALL ON FUNCTION public.fx_rates_enter(TEXT,DATE,NUMERIC,TEXT,TEXT) FROM PUBLIC;")]),

    # ── G3.3-c(8): the ownership sequence ───────────────────────────────────
    ("M17", MIG, "temporary fx_rates_owner membership NOT revoked",
     [(r"    IF v_granted THEN\n        EXECUTE format\('REVOKE fx_rates_owner FROM %I', "
       r"v_migration_role\);\n    END IF;\n", "")]),

    # ── G3.3-c(7): the currency-generic rename ──────────────────────────────
    ("M18", MIG, "rename replaced by an added column (idr_per_usd survives)",
     [(r"ALTER TABLE fx_rates RENAME COLUMN idr_per_usd TO idr_per_major_unit;",
       "ALTER TABLE fx_rates ADD COLUMN idr_per_major_unit NUMERIC(18,6);\n"
       "UPDATE fx_rates SET idr_per_major_unit = idr_per_usd;\n"
       "ALTER TABLE fx_rates ALTER COLUMN idr_per_major_unit SET NOT NULL;")]),

    # ── G3.3-c(6): the invariants ───────────────────────────────────────────
    ("M19", MIG89, "credit_lots unvalued-has-no-FX-reference CHECK dropped",
     [(r"    ADD CONSTRAINT credit_lots_unvalued_has_no_fx_reference\n"
       r"        CHECK \(valuation_status = 'valued'\n"
       r"               OR \(fx_currency_pair IS NULL AND fx_rate_date IS NULL\)\);",
       "    ADD CONSTRAINT credit_lots_unvalued_placeholder CHECK (true);")]),
    ("M20", MIG89, "credit_lots valued-has-FX-reference CHECK dropped",
     [(r"    ADD CONSTRAINT credit_lots_valued_has_fx_reference\n"
       r"        CHECK \(valuation_status <> 'valued'\n"
       r"               OR \(fx_currency_pair IS NOT NULL AND fx_rate_date IS NOT NULL\n"
       r"                   AND src_currency IS NOT NULL\n"
       r"                   AND fx_currency_pair = src_currency \|\| '/IDR'\)\),\n", "")]),
    ("M21", MIG, "fx_rates currency_pair format CHECK dropped",
     [(r"    ADD CONSTRAINT fx_rates_currency_pair_format\n"
       r"        CHECK \(currency_pair ~ '\^\[A-Z\]\{3\}/IDR\$'\),\n", "")]),
    ("M22", MIG, "KMK interval containment check removed",
     [(r"        IF NOT EXISTS \(\n            SELECT 1 FROM public\.fx_kmk_periods k\n"
       r"             WHERE k\.kmk_ref = p_source_ref\n"
       r"               AND p_rate_date BETWEEN k\.valid_from AND k\.valid_to\n        \) THEN",
       "        IF FALSE THEN")]),
    ("M23", MIG89, "credit_lots FX foreign key weakened to ON DELETE CASCADE",
     [(r"(FOREIGN KEY \(fx_currency_pair, fx_rate_date\)\n"
       r"        REFERENCES fx_rates \(currency_pair, rate_date\) ON DELETE )RESTRICT",
       r"\1CASCADE")]),
    ("M24", MIG, "immutability row trigger dropped",
     [(r"CREATE TRIGGER fx_rates_no_mutation\n    BEFORE UPDATE OR DELETE ON fx_rates\n"
       r"    FOR EACH ROW EXECUTE FUNCTION public\.fx_rates_reject_mutation\(\);",
       "-- row trigger removed by mutation")]),
    ("M25", MIG, "statement-level TRUNCATE trigger dropped",
     [(r"CREATE TRIGGER fx_rates_no_truncate\n    BEFORE TRUNCATE ON fx_rates\n"
       r"    FOR EACH STATEMENT EXECUTE FUNCTION public\.fx_rates_reject_mutation\(\);",
       "-- truncate trigger removed by mutation")]),

    # ── G3.3-c(1)/(9): the OPERATIONAL path ─────────────────────────────────
    ("M29", MIG, "ops role never granted fx_rates_writer (ops path does not exist)",
     [(r"    EXECUTE format\('GRANT fx_rates_writer TO %I WITH INHERIT FALSE, SET TRUE', v_ops_role\);\n",
       ""),
      # the migration's own assertions would catch it first; drop them too, so
      # the mutation reaches the SUITE and tests what the suite can see.
      (r"    IF pg_has_role\(v_ops_role, 'fx_rates_writer', 'USAGE'\) THEN.*?END IF;\n", ""),
      (r"    IF NOT pg_has_role\(v_ops_role, 'fx_rates_writer', 'SET'\) THEN.*?END IF;\n", "")]),
    ("M30", MIG, "ops membership granted plain (INHERIT TRUE — the gate goes fictional)",
     [(r"GRANT fx_rates_writer TO %I WITH INHERIT FALSE, SET TRUE",
       "GRANT fx_rates_writer TO %I"),
      (r"    IF pg_has_role\(v_ops_role, 'fx_rates_writer', 'USAGE'\) THEN.*?END IF;\n", "")]),

    # ── G3.3-c(1): NOLOGIN normative roles ──────────────────────────────────
    ("M31", MIG, "g3_posting_engine shipped LOGIN",
     [(r"CREATE ROLE g3_posting_engine NOLOGIN NOBYPASSRLS;",
       "CREATE ROLE g3_posting_engine LOGIN NOBYPASSRLS;")]),
    ("M32", MIG, "fx_rates_writer shipped LOGIN",
     [(r"CREATE ROLE fx_rates_writer NOLOGIN NOBYPASSRLS;",
       "CREATE ROLE fx_rates_writer LOGIN NOBYPASSRLS;")]),
    ("M32b", MIG, "fx_rates_owner shipped LOGIN (the most privileged of the three)",
     [(r"CREATE ROLE fx_rates_owner NOLOGIN NOBYPASSRLS;",
       "CREATE ROLE fx_rates_owner LOGIN NOBYPASSRLS;")]),
    # NOTE: the existing-role NOLOGIN preflight cannot be mutation-tested from
    # here. `provision.sh` drops the normative roles before every run, so on a
    # fresh cluster the preflight has nothing to reject and removing it is a
    # no-op — the same trap M03 fell into. It is covered instead by
    # `tests/t72/harness.sh preflight`, which pre-creates fx_rates_owner as
    # LOGIN in a scratch database and asserts the migration ABORTS.

    # NOTE: the dual-date CHECK is created by 0088 and DROPPED by 0089 when the
    # columns are unified, so mutating it is invisible once the unit applies in
    # full. The property that replaced it — there is exactly ONE column — is
    # covered by M38 below.


    # ── GATE 2 — the birth path (0089) ──────────────────────────────────────
    ("M34", MIG89, "engine skips the lock phase entirely (locks lazily, or not at all)",
     [(r"    v_locked := public\.g3_lock_fx_refs\(v_refs\);",
       "    v_locked := v_required;  -- lock phase removed by mutation")]),
    ("M35", MIG89, "lock statement loses its ORDER BY (unordered acquisition)",
     [(r"         ORDER BY f\.currency_pair, f\.rate_date      -- <- the ordering, in ONE statement\n",
       "")]),
    ("M36", MIG89, "lock phase drops FOR SHARE (a plain read locks nothing)",
     [(r"           FOR SHARE OF f\n", "\n")]),
    ("M37", MIG89, "write phase acquires its own FX lock after writing has begun",
     [(r"(    v_rate := public\.g3_fx_rate_at\(v_pair, v_fxdate\);)",
       r"\1\n        PERFORM 1 FROM public.fx_rates f WHERE f.currency_pair = v_pair "
       r"AND f.rate_date = v_fxdate FOR SHARE;")]),
    ("M39", MIG89, "lock-count verification removed (missing rate no longer STALLs)",
     [(r"    IF v_locked <> v_required THEN.*?USING ERRCODE = 'FX007';\n    END IF;\n", "")]),
    ("M40", MIG89, "missing rate writes an `unknown` lot instead of stalling",
     [(r"    v_locked := public\.g3_lock_fx_refs\(v_refs\);",
       "    v_locked := public.g3_lock_fx_refs(v_refs);\n    v_required := v_locked;")]),
    ("M41", MIG89, "rate date derived in UTC instead of Asia/Jakarta",
     [(r"    SELECT \(p_instant AT TIME ZONE 'Asia/Jakarta'\)::date;",
       "    SELECT (p_instant AT TIME ZONE 'UTC')::date;")]),
    ("M42", MIG92, "non-USD currency accepted instead of stalling",
     [(r"            v_stall := format\('unsupported source currency %s \(D22=B is USD only\)', v_ccy\);",
       "            v_pair := v_ccy || '/IDR';\n            v_date := public.g3_wib_rate_date(v_acq);")]),
    ("M43", MIG89, "write phase re-reads provider terms (lock set becomes unstable)",
     [(r"(    IF v_tenant IS NULL OR v_source IS NULL OR v_op IS NULL THEN)",
       "    SELECT g.supplier_fee_currency INTO v_ccy\n"
       "      FROM public.g3_provider_payments g\n"
       "     WHERE g.provider_payment_id = p_resolved->>'ledger_op_id';\n\\1")]),
    ("M44", MIG89, "write phase iterates the RAW requests, not the resolved vector",
     [(r"SELECT w\.\* FROM jsonb_array_elements\(v_resolved\) res,",
       "SELECT w.* FROM jsonb_array_elements(p_requests) res,")]),
    ("M38", MIG89, "date columns left un-unified (0086's rate_date survives)",
     [(r"ALTER TABLE credit_lots RENAME COLUMN rate_date TO fx_rate_date;",
       "ALTER TABLE credit_lots ADD COLUMN fx_rate_date DATE;")]),

    # ── GATE 3 — Decision 3A, the supplier-fee anchor (0090) ────────────────
    ("M45", MIG90, "anchor stops subtracting payment_fees (the 1000-vs-905 defect)",
     [(r"    RETURN QUERY SELECT \(v_payment - v_fees - v_tax\)::BIGINT, v_ccy;",
       "    RETURN QUERY SELECT v_payment::BIGINT, v_ccy;")]),
    ("M46", MIG92, "resolver reads settlement-derived supplier_fee_minor again",
     [(r"            SELECT a\.anchor_minor, a\.currency INTO v_anchor\n"
       r"              FROM public\.g3_supplier_fee_anchor\(v_provider, v_ppid, v_settle_tax\) a;\n"
       r"            v_fee := v_anchor\.anchor_minor;\n"
       r"            v_ccy := v_anchor\.currency;",
       "            SELECT g.supplier_fee_minor, g.supplier_fee_currency\n"
       "              INTO v_fee, v_ccy FROM public.g3_provider_payments g\n"
       "             WHERE g.provider = v_provider AND g.provider_payment_id = v_ppid;")]),
    ("M47", MIG90, "unknown ledger event type no longer STALLs",
     [(r"    IF v_unknown IS NOT NULL THEN.*?USING ERRCODE = 'FX010';\n    END IF;\n", "")]),
    # M48 withdrawn: after the zero-fee boolean was removed it became identical to M52.
    ("M49", MIG90, "balance-ledger append-only triggers dropped",
     [(r"CREATE TRIGGER g3_pbl_no_mutation\n    BEFORE UPDATE OR DELETE ON g3_provider_balance_ledger\n"
       r"    FOR EACH ROW EXECUTE FUNCTION public\.l2c_reject_mutation\(\);", "")]),
    ("M50", MIG90, "single-payment-entry cardinality rule removed",
     [(r"    IF v_n_payment <> 1 THEN.*?USING ERRCODE = 'FX009';\n    END IF;\n", "")]),

    ("M51", MIG90, "no ledger yields NULL terms instead of STALL-AWAITING-PROVIDER-LEDGER",
     [(r"    IF NOT EXISTS \(SELECT 1 FROM public\.g3_provider_balance_ledger l\n"
       r"                    WHERE l\.provider = p_provider\n"
       r"                      AND l\.provider_payment_id = p_provider_payment_id\) THEN.*?"
       r"USING ERRCODE = 'FX011';\n    END IF;\n", "")]),
    ("M52", MIG90, "a MISSING payment_fees entry accepted as a proven zero fee",
     [(r"    IF v_n_fees = 0 THEN.*?USING ERRCODE = 'FX009';\n    END IF;\n", "")]),
    ("M53", MIG90, "caller-supplied zero-fee boolean reinstated as authority",
     [(r"(    p_settlement_tax_minor BIGINT DEFAULT NULL\n\) RETURNS TABLE)",
       "    p_settlement_tax_minor BIGINT DEFAULT NULL,\n"
       "    p_zero_fee_evidenced   BOOLEAN DEFAULT FALSE\n) RETURNS TABLE"),
      (r"    IF v_n_fees = 0 THEN", "    IF v_n_fees = 0 AND NOT p_zero_fee_evidenced THEN"),
      (r"REVOKE ALL ON FUNCTION public\.g3_supplier_fee_anchor\(TEXT,TEXT,BIGINT\)",
       "REVOKE ALL ON FUNCTION public.g3_supplier_fee_anchor(TEXT,TEXT,BIGINT,BOOLEAN)")]),
    ("M54", MIG89, "valued lot no longer records the consideration that priced it",
     [(r"    ADD CONSTRAINT credit_lots_valued_has_consideration\n"
       r"        CHECK \(valuation_status <> 'valued'\n"
       r"               OR \(consideration_minor IS NOT NULL\n"
       r"                   AND consideration_currency = src_currency\)\),\n", "")]),

    ("M55", MIG92, "digest re-verification removed (a stale anchor gets booked)",
     [(r"    IF p_resolved->>'ledger_digest' IS NOT NULL THEN.*?USING ERRCODE = 'FX012';\n        END IF;\n    END IF;\n",
       "")]),
    ("M56", MIG92, "replay compares only timestamps again (value fields dropped)",
     [(r"    OR v_existing\.source                 IS DISTINCT FROM v_source.*?"
       r"OR v_existing\.fx_rate_at_grant       IS DISTINCT FROM v_rate THEN",
       "    OR v_existing.fx_rate_date           IS DISTINCT FROM v_fxdate THEN")]),
    ("M57", MIG89, "writer reverts to SELECT-then-INSERT (race-prone replay)",
     [(r"    ON CONFLICT \(tenant_id, ledger_op_id\) WHERE ledger_op_id IS NOT NULL DO NOTHING\n",
       "")]),
    ("M58", MIG92, "the per-payment fence is not taken before deriving the anchor",
     [(r"            PERFORM public\.g3_fence_payment\(v_provider, v_ppid\);\n", "")]),
    ("M59", MIG92, "fx_rate_at_grant read and discarded again",
     [(r"        CASE WHEN v_status = 'valued' THEN v_ccy ELSE NULL END,\n        v_rate\n    \)",
       "        CASE WHEN v_status = 'valued' THEN v_ccy ELSE NULL END,\n        NULL\n    )")]),

    ("M60", MIG92, "channel/tax scope lock removed (non-dodo_mor accepted)",
     [(r"    IF v_source = 'topup' AND v_stall IS NULL\n"
       r"       AND NOT \(\(p_request->>'provenance_kind'\) = 'opening_census'\n"
       r"                AND v_provider IS NULL AND v_ppid IS NULL\)\n"
       r"       AND \(v_channel IS DISTINCT FROM 'dodo_mor' OR v_owner IS DISTINCT FROM 'provider'\) THEN.*?"
       r"    END IF;\n\n", "")]),
    ("M61", MIG92, "direct-helper path reopened (hand-made vector can price a top-up)",
     [(r"    IF v_source = 'topup' AND v_status = 'valued' THEN\n"
       r"        IF p_resolved->>'provider' IS NULL.*?\n        END;\n    END IF;\n", "")]),
    ("M62", MIG89, "batch fence not taken before resolution",
     [(r"    PERFORM public\.g3_fence_payments\(p_requests\);\n", "")]),
    ("M63", MIG90, "batch fence acquires in unordered sequence",
     [(r"         ORDER BY g\.provider, g\.provider_payment_id\n           FOR UPDATE OF g",
       "           FOR UPDATE OF g")]),
    ("M64", MIG92, "dpp rounds the FX product to 0 dp instead of 2",
     [(r"        v_dpp := round\(\(v_fee::NUMERIC / 100\) \* v_rate, 2\);",
       "        v_dpp := round((v_fee::NUMERIC / 100) * v_rate, 0);")]),
    ("M65", MIG92, "ppc rounds to 2 dp instead of 4",
     [(r"        v_ppc := round\(v_dpp / v_credits, 4\);", "        v_ppc := round(v_dpp / v_credits, 2);")]),
    # ── Acquisition: the pager contract ─────────────────────────────────────
    ("M66", OPS, "pager stops after the first page (the shipped defect)",
     [(r"        if len\(items\) < PAGE_SIZE:\n            break\n",
       "        if True:\n            break\n")]),
    ("M67", OPS, "pager reverts to the invented cursor dialect (limit/starting_after)",
     [(r'f"&page_size=\{PAGE_SIZE\}&page_number=\{page\}"\)',
       'f"&limit={PAGE_SIZE}&starting_after={page}")')]),
    ("M68", OPS, "response shape guessed again (`data` accepted, absent reads as empty)",
     [(r"        if not isinstance\(body, dict\).*?        items = body\[\"items\"\]\n",
       "        items = body.get(\"data\") or body.get(\"items\") or []\n")]),
    ("M69", OPS, "cross-page duplicate silently accepted (offset paging re-serves a row)",
     [(r'            if eid is not None and eid in seen_ids:\n'
       r'                raise OperatorError\(\n.*?snapshot of the ledger\."\)\n', "")]),
    # ── Acquisition: the atomic read-back ───────────────────────────────────
    # 🔴 M70 IS THE ONE THAT MATTERS. It does not delete the read-back — the
    #    comparison still runs and still raises, so any test that only asserts
    #    "a conflict is refused" stays green. It moves the call back OUTSIDE the
    #    transaction, which is exactly where it used to live. Only a test that
    #    checks the table AFTER the refusal can tell the two apart.
    ("M70", OPS, "read-back runs after COMMIT again (detects, does not prevent)",
     [(r"        await read_back_and_compare\(conn, provider, payment_id, rows\)\n"
       r"        digest = await conn\.fetchval\(\n"
       r"            \"SELECT public\.g3_ledger_digest\(\$1,\$2\)\", provider, payment_id\)\n"
       r"    return \{",
       "        digest = await conn.fetchval(\n"
       "            \"SELECT public.g3_ledger_digest($1,$2)\", provider, payment_id)\n"
       "    await read_back_and_compare(conn, provider, payment_id, rows)\n"
       "    return {")]),
    ("M71", OPS, "read-back drops the extra-row direction (containment, not equality)",
     [(r'    extra = sorted\(set\(stored\).*?pricing off it\."\)\n', "")]),
    # ── The rounding MODE, not the rounding precision ───────────────────────
    # 🔴 M65 already breaks the precision (4 dp -> 2 dp) and half the suite
    #    notices. M72 keeps the precision exactly right and changes only the
    #    TIE-BREAK, half-up to half-even — a substitution that is invisible on
    #    every quotient that does not land exactly on a half. Before the
    #    `1.00 / 32 = 0.03125` case existed, no PPC case in this suite discarded
    #    a tie, so this mutation would have gone straight through: two different
    #    rounding modes agreeing on every input anyone had thought to test.
    ("M72", MIG92, "ppc rounds half-EVEN instead of half-up (mode, not precision)",
     [(r"        v_ppc := round\(v_dpp / v_credits, 4\);",
       "        v_ppc := CASE\n"
       "            WHEN (v_dpp / v_credits) * 10000\n"
       "                 - floor((v_dpp / v_credits) * 10000) = 0.5\n"
       "             AND floor((v_dpp / v_credits) * 10000)::BIGINT % 2 = 0\n"
       "            THEN floor((v_dpp / v_credits) * 10000) / 10000\n"
       "            ELSE round(v_dpp / v_credits, 4)\n"
       "        END;")]),
    # ── GATE 4: the Item-5 posting boundary (0093) ──────────────────────────
    # 🔴 M78 IS THE ONE THAT MATTERS. The tenant GUARD stays — `p_tenant` must
    #    still equal the transaction context — and only the credited-anchor
    #    lookup goes. Everything a caller controls still agrees with itself, so
    #    the posting succeeds and looks entirely normal; what is lost is the only
    #    check that consults evidence the caller did NOT write.
    ("M78", MIG93, "credited-anchor tenant authority removed (caller's word is enough again)",
     [(r"    PERFORM 1 FROM public\.payment_events pe\n.*?nothing is posted\.',\n"
       r"            p_provider, p_provider_payment_id, p_tenant;\n    END IF;\n\n", "")]),
    ("M79", MIG93, "posting functions lose SECURITY DEFINER",
     [(r"LANGUAGE plpgsql\nSECURITY DEFINER\nSET search_path = pg_catalog, pg_temp\nAS \$\$",
       "LANGUAGE plpgsql\nSET search_path = pg_catalog, pg_temp\nAS $$")]),
    ("M80", MIG93, "posting boundary search_path admits public again",
     [(r"LANGUAGE plpgsql\nSECURITY DEFINER\nSET search_path = pg_catalog, pg_temp\nAS \$\$",
       "LANGUAGE plpgsql\nSECURITY DEFINER\nSET search_path = pg_catalog, public, pg_temp\nAS $$")]),
    ("M81", MIG93, "posting EXECUTE handed straight to app_user",
     [(r"(GRANT EXECUTE ON FUNCTION public\.g3_post_cash_in\(uuid,text,text,text\) TO g3_posting_engine)",
       r"\1; GRANT EXECUTE ON FUNCTION public.g3_post_cash_in(uuid,text,text,text) TO app_user")]),
    ("M82", MIG93, "lot UPDATE widened from two columns to the whole table",
     [(r"GRANT UPDATE \(recognized_idr, credits_remaining\)  ON public\.credit_lots TO g3_posting_definer;",
       "GRANT UPDATE ON public.credit_lots TO g3_posting_definer;")]),
    ("M83", MIG93, "trigger hardening reverted (public back on the search_path)",
     [(r"LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS \$trg\$",
       "LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $trg$")]),
    # ── GATE 4: the census identity guard ───────────────────────────────────
    # 🔴 REMOVING THE GUARD DOES NOT MAKE THE MISLABELLED REQUEST SUCCEED — it
    #    makes it fail for the WRONG REASON, or not at all when the payment is
    #    otherwise valid. That is the whole point of siting H4b on a payment with
    #    good terms, complete ledger evidence and a seeded rate: with the guard
    #    gone there is nothing else left to stall on.
    ("M77", MIG92, "census identity guard removed (a labelled census may carry a provider)",
     [(r"    IF v_source = 'topup'\n"
       r"       AND \(p_request->>'provenance_kind'\) = 'opening_census'\n"
       r"       AND \(v_provider IS NOT NULL OR v_ppid IS NOT NULL\) THEN.*?    END IF;\n\n", "")]),
    # ── GATE 4: the posting-engine identity ─────────────────────────────────
    # 🔴 M73 IS THE ONE THAT MATTERS HERE, and it is the mirror of M70. It does
    #    not remove the engine wiring — the birth still runs, on a real
    #    non-superuser LOGIN role, and every functional assertion about lots and
    #    valuation still holds. What it removes is the `SET ROLE`, so the caller
    #    is the PROBE rather than `g3_posting_engine`. If the suite stays green
    #    under this, then "the birth path runs as the posting engine" was never
    #    being measured and Gate 4 is decoration.
    ("M73", TEST, "engine connects but never SET ROLEs (runs as the bare probe)",
     [(r'    await conn\.execute\(f"SET ROLE \{ENGINE_ROLE\}"\)\n    engine = _EngineConnection',
       "    engine = _EngineConnection")]),
    ("M74", TEST, "engine reverts to the migration role (the pre-Gate-4 PARTIAL shape)",
     [(r"    conn = await _connect\(_dsn_as\(PROBE_ROLE\)\)\n"
       r'    await conn\.execute\(f"SET ROLE \{ENGINE_ROLE\}"\)\n',
       "    conn = await _connect(ADMIN_DSN)\n")]),
    # The COMMENTs go with the policies: leaving them behind makes 0091 fail to
    # apply, and a migration-level abort would prove nothing about whether the
    # SUITE notices a boundary that cannot write through RLS.
    ("M75", MIG91, "privileges granted but the RLS policies are dropped",
     [(r"CREATE POLICY g3_birth_definer_reads_lots.*?append-only here\.';\n", "")]),
    # 🔴 BYPASSRLS GOES TO THE BOUNDARY OWNER, NOT THE ENGINE. The engine never
    #    touches credit_lots any more — the definer does — so handing BYPASSRLS
    #    to the engine would break nothing and prove nothing. This gives it to
    #    the role that actually performs the INSERT, which makes 0091's two
    #    policies dead code while the birth still succeeds.
    ("M76", MIG91, "boundary owner handed BYPASSRLS instead of being admitted by policy",
     [(r"(CREATE POLICY g3_birth_definer_reads_lots)",
       "ALTER ROLE g3_birth_definer BYPASSRLS;\n\\1")]),
    # ── Guards of the SUITE itself ──────────────────────────────────────────
    ("M26", TEST, "GUARD: operator granted plain INHERIT membership",
     [(r"GRANT fx_rates_writer TO \{OPERATOR_ROLE\} WITH INHERIT FALSE, SET TRUE",
       "GRANT fx_rates_writer TO {OPERATOR_ROLE}")]),
    ("M27", TEST, "GUARD: suite runs as superuser (the T79 defect verbatim)",
     [(r'^OPERATOR_ROLE = "fx_t72_operator"', 'OPERATOR_ROLE = "postgres"')]),
]


def sh(cmd):
    return subprocess.run(cmd, shell=True, cwd=str(REPO), capture_output=True, text=True)


def main():
    only = [a for a in sys.argv[1:] if a.startswith("M")] or None
    TARGETS = (MIG, MIG89, MIG90, TEST, OPS, MIG91, MIG92, MIG93)
    for t in TARGETS:
        shutil.copy(t, str(t) + ".orig")
    results = []
    try:
        for mid, target, desc, subs in MUTATIONS:
            if only and mid not in only:
                continue
            src = open(str(target) + ".orig").read()
            mutated, missed = src, None
            for pat, rep in subs:
                mutated, n = re.subn(pat, rep, mutated, flags=re.S | re.M)
                if n == 0:
                    missed = pat[:70]
                    break
            if missed:
                results.append((mid, desc, "PATTERN-MISS", missed))
                shutil.copy(str(target) + ".orig", target)
                continue
            open(target, "w").write(mutated)

            mig = sh(f"'{HARNESS}' db")
            if mig.returncode != 0:
                tail = (mig.stdout + mig.stderr).strip().splitlines()
                results.append((mid, desc, "CAUGHT(migration)", tail[-1][:90] if tail else ""))
            else:
                res = sh(f"'{HARNESS}' test -q --tb=no")
                lines = [l for l in res.stdout.splitlines() if "passed" in l or "failed" in l]
                results.append((mid, desc,
                                "CAUGHT(test)" if res.returncode != 0 else "NOT CAUGHT",
                                lines[-1][:90] if lines else ""))
            shutil.copy(str(target) + ".orig", target)
    finally:
        for t in TARGETS:
            shutil.copy(str(t) + ".orig", t)
            os.remove(str(t) + ".orig")

    print("\n" + "=" * 100)
    caught = 0
    for mid, desc, verdict, detail in results:
        ok = verdict.startswith("CAUGHT")
        caught += ok
        print(f"{'  ' if ok else '!!'} {mid}  {verdict:<18} {desc}")
        if detail:
            print(f"       {detail}")
    print("=" * 100)
    print(f"{caught}/{len(results)} mutations caught")
    return 0 if caught == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
