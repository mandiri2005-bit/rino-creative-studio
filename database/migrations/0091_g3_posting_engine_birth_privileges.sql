-- =====================================================================
-- 0091_g3_posting_engine_birth_privileges.sql
--
-- GATE 4: let the birth path run as `g3_posting_engine` — through ONE door.
--
-- 🔴 WHY THIS MIGRATION EXISTS. `0088` created `g3_posting_engine` and `0090`
--    gave it SELECT on the fx and Balance-Ledger evidence, but nothing ever let
--    it run `g3_birth_lots` and nothing ever let it write a lot. That was
--    invisible because every test drove the birth path on the MIGRATION
--    connection (`neondb_owner`), which is `BYPASSRLS` and privileged on
--    everything: the engine role existed on paper and was never once executed.
--
-- 🔴 THE FIRST VERSION OF THIS MIGRATION FIXED THAT THE WRONG WAY, AND THE
--    OWNER REVIEW CAUGHT IT. It walked the birth path as the engine, granted
--    whatever each refusal named, and stopped when the path went green. That
--    yields a WORKING set and a WRONG one:
--
--      * it granted EXECUTE on `g3_write_lot_resolved` DIRECTLY to the engine,
--        which hands the runtime a second door — a hand-made resolver vector
--        could be written straight to `credit_lots` with no ordered FX-lock
--        phase in front of it. That is precisely the direct-helper path Gate 2
--        closed, re-opened by a privilege rather than by code.
--      * it left `g3_lock_fx_refs`, `g3_fx_rate_at` and `g3_fence_payments`
--        alone, reasoning that PUBLIC already held EXECUTE so a grant would be
--        redundant. It IS redundant — and that is the point: two of those are
--        `SECURITY DEFINER` lock-takers, so ANY caller could pin payment or FX
--        rows. "No grant needed, PUBLIC has it" names an open surface; it does
--        not excuse one.
--
--    MINIMUM PRIVILEGE IS NOT "the smallest set that makes it work". It is the
--    smallest set that makes it work THROUGH THE INTENDED PATH. Those are
--    different sets, and only the second one is a contract.
--
-- THE SHAPE THIS INSTALLS
-- -----------------------
--   `g3_birth_lots` becomes SECURITY DEFINER, owned by `g3_birth_definer` — a
--   NOLOGIN, NOSUPERUSER, NOBYPASSRLS role that exists only to be that
--   boundary. Every internal privilege (the helpers, `credit_lots`, the RLS
--   admission) belongs to the OWNER, never to the caller. The runtime identity
--   `g3_posting_engine` ends up holding exactly one executable thing:
--   `g3_birth_lots`. It cannot call a helper, cannot touch `credit_lots`, and
--   cannot assemble its own vector.
--
-- 🔴 REVOKE ON A FUNCTION YOU DO NOT OWN IS A WARNING, NOT AN ERROR.
--    `g3_lock_fx_refs` and `g3_fx_rate_at` are owned by `fx_rates_owner`, and
--    `0088` revoked this migration role's membership of it. A plain
--    `REVOKE ... FROM PUBLIC` therefore prints "no privileges could be revoked"
--    and CARRIES ON — the migration would report success having changed
--    nothing. So the revokes run under a TEMPORARY membership that is dropped
--    again immediately (the same shape `0088` uses, and `test_c01b` asserts),
--    and every one of them is VERIFIED afterwards rather than assumed.
--
-- FORWARD-ONLY. `0088`, `0089` and `0090` are accepted and are NOT edited.
-- `G3_LOT_WRITER_ENABLED` stays `'0'`; nothing here turns it on.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regproc('public.g3_birth_lots') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0091: apply 0089 first (g3_birth_lots is absent)';
    END IF;
    IF to_regproc('public.g3_supplier_fee_anchor') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0091: apply 0090 first (g3_supplier_fee_anchor is absent)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'g3_posting_engine') THEN
        RAISE EXCEPTION 'ABORT 0091: role g3_posting_engine is absent (0088 creates it)';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles
                WHERE rolname = 'g3_posting_engine'
                  AND (rolbypassrls OR rolsuper OR rolcanlogin)) THEN
        RAISE EXCEPTION
            'ABORT 0091: g3_posting_engine is LOGIN/SUPERUSER/BYPASSRLS. It must stay '
            'NOLOGIN NOSUPERUSER NOBYPASSRLS — a privileged engine makes this migration '
            'and every Gate-4 assertion vacuous.';
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. The boundary role. It owns one function and logs in nowhere.
-- ---------------------------------------------------------------------
DO $mk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'g3_birth_definer') THEN
        CREATE ROLE g3_birth_definer NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$mk$;

COMMENT ON ROLE g3_birth_definer IS
  'Owns public.g3_birth_lots and nothing else. The SECURITY DEFINER boundary for lot birth: every privilege the birth path needs internally is held HERE, so the calling runtime identity (g3_posting_engine) needs only EXECUTE on the entrypoint. NOLOGIN by construction; NOBYPASSRLS so the credit_lots policies below are actually consulted.';

-- Ownership can only be assigned to a role the current role belongs to, and a
-- PG18 CREATEROLE grantor gets ADMIN but not SET. Take the membership, use it,
-- drop it — it must not outlive this migration.
GRANT g3_birth_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

-- ---------------------------------------------------------------------
-- 2. The single door.
-- ---------------------------------------------------------------------
-- 🔴 PostgreSQL checks CREATE-on-schema of the INCOMING owner at `ALTER ...
--    OWNER TO` time, and never again. So the boundary role borrows it for the
--    length of the transfer and hands it straight back: it must end up owning
--    exactly one function and holding NO standing right to create anything.
--    (`fx_rates_owner` is the existing proof that this is a stable state — it
--    owns functions in `public` while holding neither USAGE nor CREATE on it.)
DO $own$
DECLARE
    v_had_create BOOLEAN := has_schema_privilege('g3_birth_definer', 'public', 'CREATE');
    v_had_usage  BOOLEAN := has_schema_privilege('g3_birth_definer', 'public', 'USAGE');
BEGIN
    IF NOT v_had_create THEN EXECUTE 'GRANT CREATE ON SCHEMA public TO g3_birth_definer'; END IF;
    IF NOT v_had_usage  THEN EXECUTE 'GRANT USAGE  ON SCHEMA public TO g3_birth_definer'; END IF;

    -- ORDER MATTERS. The membership above is `INHERIT FALSE`, so the moment
    -- ownership moves this role stops counting as the owner and any further
    -- ALTER on the function is refused. Set the attribute first, hand over
    -- second.
    EXECUTE 'ALTER FUNCTION public.g3_birth_lots(JSONB) SECURITY DEFINER';
    EXECUTE 'ALTER FUNCTION public.g3_birth_lots(JSONB) OWNER TO g3_birth_definer';

    IF NOT v_had_create THEN EXECUTE 'REVOKE CREATE ON SCHEMA public FROM g3_birth_definer'; END IF;
    IF NOT v_had_usage  THEN EXECUTE 'REVOKE USAGE  ON SCHEMA public FROM g3_birth_definer'; END IF;
END
$own$;

-- ---------------------------------------------------------------------
-- 3. Everything the path needs INTERNALLY belongs to the boundary owner.
-- ---------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION public.g3_resolve_request(JSONB)                TO g3_birth_definer;
GRANT EXECUTE ON FUNCTION public.g3_write_lot_resolved(JSONB)             TO g3_birth_definer;
GRANT EXECUTE ON FUNCTION public.g3_ledger_digest(TEXT,TEXT)              TO g3_birth_definer;
GRANT EXECUTE ON FUNCTION public.g3_supplier_fee_anchor(TEXT,TEXT,BIGINT) TO g3_birth_definer;
GRANT EXECUTE ON FUNCTION public.g3_fence_payments(JSONB)                 TO g3_birth_definer;

GRANT SELECT ON public.fx_rates                      TO g3_birth_definer;
GRANT SELECT ON public.g3_provider_payments          TO g3_birth_definer;
GRANT SELECT ON public.g3_provider_balance_ledger    TO g3_birth_definer;
GRANT INSERT, SELECT ON public.credit_lots           TO g3_birth_definer;

-- The RLS admission. `credit_lots` is ENABLE + FORCE RLS and `tenant_isolation`
-- carries no `TO` clause, so it binds the boundary owner too. Without these the
-- GRANTs above are inert and the INSERT fails on policy, not on privilege.
CREATE POLICY g3_birth_definer_reads_lots ON public.credit_lots
    FOR SELECT TO g3_birth_definer USING (true);
CREATE POLICY g3_birth_definer_writes_lots ON public.credit_lots
    FOR INSERT TO g3_birth_definer WITH CHECK (true);

COMMENT ON POLICY g3_birth_definer_reads_lots ON public.credit_lots IS
  'Tenant-blind SELECT for the birth boundary ONLY. It must see an existing lot for any tenant to detect a replay before inserting; a tenant-scoped read with no app.current_tenant_id set would return nothing and turn a replay into a silent ON CONFLICT DO NOTHING. Permissive by design — a RESTRICTIVE policy here would AND with tenant_isolation and break the fx_rates_reference_check read (test_c13).';
COMMENT ON POLICY g3_birth_definer_writes_lots ON public.credit_lots IS
  'Tenant-blind INSERT for the birth boundary ONLY. g3_birth_lots is a BATCH function spanning several tenants in one call, so a per-tenant GUC cannot gate it. No UPDATE or DELETE policy exists and no such privilege is granted: lots are append-only here.';

-- ---------------------------------------------------------------------
-- 4. Close the helper surface. Two of these are DEFINER lock-takers, and
--    PUBLIC could call them.
-- ---------------------------------------------------------------------
REVOKE EXECUTE ON FUNCTION public.g3_fence_payments(JSONB)     FROM PUBLIC, app_user, g3_posting_engine;
REVOKE EXECUTE ON FUNCTION public.g3_resolve_request(JSONB)    FROM g3_posting_engine;
REVOKE EXECUTE ON FUNCTION public.g3_write_lot_resolved(JSONB) FROM PUBLIC, app_user, g3_posting_engine;
REVOKE EXECUTE ON FUNCTION public.g3_ledger_digest(TEXT,TEXT)  FROM g3_posting_engine;
REVOKE EXECUTE ON FUNCTION public.g3_supplier_fee_anchor(TEXT,TEXT,BIGINT) FROM g3_posting_engine;
REVOKE INSERT, SELECT ON public.credit_lots FROM g3_posting_engine;

-- `g3_lock_fx_refs` and `g3_fx_rate_at` belong to fx_rates_owner. Revoking them
-- from here without its identity is the silent no-op described in the header.
--
-- 🔴 fx_rates_owner holds no USAGE on schema `public` — it owns functions there
--    but cannot resolve a name in it — so the whole section runs inside one
--    block that borrows USAGE and gives it back EXACTLY as it found it. A
--    migration that silently leaves a normative role holding schema USAGE it
--    did not have before has widened the very surface it came to narrow.
DO $fxo$
DECLARE
    v_had_usage BOOLEAN := has_schema_privilege('fx_rates_owner', 'public', 'USAGE');
BEGIN
    IF NOT v_had_usage THEN
        EXECUTE 'GRANT USAGE ON SCHEMA public TO fx_rates_owner';
    END IF;

    EXECUTE 'GRANT fx_rates_owner TO ' || quote_ident(CURRENT_USER) || ' WITH INHERIT FALSE, SET TRUE';
    EXECUTE 'SET LOCAL ROLE fx_rates_owner';
    EXECUTE 'REVOKE EXECUTE ON FUNCTION public.g3_lock_fx_refs(JSONB)   FROM PUBLIC, app_user, g3_posting_engine';
    EXECUTE 'REVOKE EXECUTE ON FUNCTION public.g3_fx_rate_at(TEXT,DATE) FROM PUBLIC, app_user, g3_posting_engine';
    EXECUTE 'GRANT  EXECUTE ON FUNCTION public.g3_lock_fx_refs(JSONB)   TO g3_birth_definer';
    EXECUTE 'GRANT  EXECUTE ON FUNCTION public.g3_fx_rate_at(TEXT,DATE) TO g3_birth_definer';
    EXECUTE 'RESET ROLE';

    IF NOT v_had_usage THEN
        EXECUTE 'REVOKE USAGE ON SCHEMA public FROM fx_rates_owner';
    END IF;
END
$fxo$;

-- ---------------------------------------------------------------------
-- 5. The runtime identity ends up holding exactly ONE executable thing.
-- ---------------------------------------------------------------------
-- Granted AS the new owner: after the transfer this migration role is no longer
-- the owner and cannot grant on it.
DO $door$
BEGIN
    EXECUTE 'SET LOCAL ROLE g3_birth_definer';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.g3_birth_lots(JSONB) TO g3_posting_engine';
    EXECUTE 'RESET ROLE';
END
$door$;

-- ---------------------------------------------------------------------
-- 5b. The runtime can ASSUME the engine, and can do nothing else with it.
--
--     🔴 THE RUNTIME PRINCIPAL IS `app_user`, NOT `neondb_owner`. An earlier
--     cut of this migration granted the membership to `neondb_owner` because
--     `env.example` documented the pool that way. Production disagrees with the
--     example file: `DATABASE_POOL_URL` on BOTH the `backend` and `python`
--     services connects as `app_user`. Granting the migration role instead
--     would have shipped a birth path that works in every test and fails the
--     moment the writer is switched on — `SET LOCAL ROLE g3_posting_engine`
--     raised by a role holding no membership. The same shape as a suite that
--     passes because it runs as a superuser: green everywhere, wrong where it
--     counts.
--
--     `neondb_owner` stays the MIGRATION and admin principal and is deliberately
--     NOT given a runtime binding. It gets SET-only membership: `app_user` may
--     `SET LOCAL ROLE g3_posting_engine` for the length of a birth transaction
--     and nothing more.
--
-- 🔴 `INHERIT FALSE` IS LOAD-BEARING. With inheritance the principal would hold
--    the engine's EXECUTE implicitly, on every statement, with no role change
--    and nothing marking where a birth begins and ends. The identity assertion
--    would then be describing an ambient privilege rather than a deliberate,
--    transaction-scoped assumption — and `SET LOCAL` is what stops the identity
--    leaking onto the next borrower of a pooled connection.
-- ---------------------------------------------------------------------
DO $runtime$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        RAISE EXCEPTION
            'ABORT 0091: role app_user is absent (0016 creates it). It is the RUNTIME pool '
            'principal, and without the membership below the birth path cannot assume the '
            'posting engine at all.';
    END IF;
    EXECUTE 'GRANT g3_posting_engine TO app_user WITH INHERIT FALSE, SET TRUE';
END
$runtime$;

-- ---------------------------------------------------------------------
-- 6. Neither membership outlives this migration.
-- ---------------------------------------------------------------------
REVOKE fx_rates_owner     FROM CURRENT_USER;
REVOKE g3_birth_definer   FROM CURRENT_USER;

-- ---------------------------------------------------------------------
-- 7. POST-CONDITIONS. Every revoke above is verified, because REVOKE on a
--    function you do not own only WARNS.
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    v_fn   TEXT;
    v_open TEXT := '';
BEGIN
    FOREACH v_fn IN ARRAY ARRAY[
        'public.g3_lock_fx_refs(jsonb)',
        'public.g3_fx_rate_at(text,date)',
        'public.g3_fence_payments(jsonb)',
        'public.g3_write_lot_resolved(jsonb)',
        'public.g3_resolve_request(jsonb)'
    ] LOOP
        IF has_function_privilege('public', v_fn, 'EXECUTE') THEN
            v_open := v_open || ' PUBLIC:' || v_fn;
        END IF;
        IF has_function_privilege('g3_posting_engine', v_fn, 'EXECUTE') THEN
            v_open := v_open || ' g3_posting_engine:' || v_fn;
        END IF;
    END LOOP;

    IF v_open <> '' THEN
        RAISE EXCEPTION
            'ABORT 0091: helper EXECUTE survived the revoke —%. A REVOKE on a function this '
            'role does not own emits a WARNING and changes nothing, so this migration would '
            'otherwise report success while leaving a second path into the birth write open.',
            v_open;
    END IF;

    IF NOT has_function_privilege('g3_posting_engine', 'public.g3_birth_lots(jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'ABORT 0091: g3_posting_engine cannot execute the one entrypoint it needs';
    END IF;
    IF has_table_privilege('g3_posting_engine', 'public.credit_lots', 'INSERT')
       OR has_table_privilege('g3_posting_engine', 'public.credit_lots', 'SELECT') THEN
        RAISE EXCEPTION 'ABORT 0091: g3_posting_engine still holds table privilege on credit_lots';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                    WHERE n.nspname='public' AND p.proname='g3_birth_lots'
                      AND p.prosecdef
                      AND pg_get_userbyid(p.proowner) = 'g3_birth_definer') THEN
        RAISE EXCEPTION 'ABORT 0091: g3_birth_lots is not a SECURITY DEFINER owned by g3_birth_definer';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='g3_birth_definer'
                AND (rolcanlogin OR rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'ABORT 0091: g3_birth_definer must be NOLOGIN NOSUPERUSER NOBYPASSRLS';
    END IF;
    IF pg_has_role(CURRENT_USER, 'fx_rates_owner', 'SET')
       OR pg_has_role(CURRENT_USER, 'g3_birth_definer', 'SET') THEN
        RAISE EXCEPTION 'ABORT 0091: a temporary membership outlived the migration';
    END IF;
    -- ── THE RUNTIME BINDING, asserted on the role production actually uses ──
    IF NOT pg_has_role('app_user', 'g3_posting_engine', 'SET') THEN
        RAISE EXCEPTION
            'ABORT 0091: app_user cannot SET ROLE to g3_posting_engine. It is the runtime pool '
            'principal; without this the birth path is dead the moment the writer is enabled.';
    END IF;
    IF pg_has_role('app_user', 'g3_posting_engine', 'USAGE') THEN
        RAISE EXCEPTION
            'ABORT 0091: app_user INHERITS g3_posting_engine. The membership must be INHERIT '
            'FALSE, SET TRUE — otherwise the birth privilege is ambient on every statement, '
            'nothing distinguishes a birth transaction from any other, and the identity '
            'assertions downstream describe an accident rather than a decision.';
    END IF;
    -- Ambient inheritance would also hand app_user the engine's reach. Prove it
    -- has none of it EXCEPT by explicitly assuming the role.
    IF has_function_privilege('app_user', 'public.g3_birth_lots(jsonb)', 'EXECUTE') THEN
        RAISE EXCEPTION 'ABORT 0091: app_user holds DIRECT EXECUTE on g3_birth_lots';
    END IF;
    FOR v_fn IN SELECT unnest(ARRAY[
        'public.g3_write_lot_resolved(jsonb)','public.g3_resolve_request(jsonb)',
        'public.g3_lock_fx_refs(jsonb)','public.g3_fx_rate_at(text,date)',
        'public.g3_fence_payments(jsonb)'])
    LOOP
        IF has_function_privilege('app_user', v_fn, 'EXECUTE') THEN
            RAISE EXCEPTION 'ABORT 0091: app_user holds direct EXECUTE on %', v_fn;
        END IF;
    END LOOP;
    IF has_table_privilege('app_user', 'public.credit_lots', 'INSERT')
       OR has_table_privilege('app_user', 'public.credit_lots', 'UPDATE')
       OR has_table_privilege('app_user', 'public.credit_lots', 'DELETE') THEN
        RAISE EXCEPTION
            'ABORT 0091: app_user can write credit_lots without assuming the engine — the '
            'membership has leaked table privilege into the runtime principal';
    END IF;
    -- `neondb_owner` is the migration/admin principal and must NOT hold a RUNTIME
    -- binding: a second role able to birth is a second door.
    --
    -- 🔴 THIS CANNOT BE ASKED WITH `pg_has_role`. A CREATEROLE role that creates
    --    another role is given an automatic membership row carrying
    --    `admin_option`, and admin implies the ABILITY to grant itself SET — so
    --    `pg_has_role(...,'SET')` is true for `neondb_owner` no matter what, and
    --    an assertion built on it fails against a perfectly correct database.
    --    (It did, on the first run of this very check.) The question that
    --    actually distinguishes a runtime binding from role administration is
    --    whether the GRANT itself carries `SET`/`INHERIT`, which lives in
    --    `pg_auth_members`.
    IF EXISTS (
        SELECT 1 FROM pg_auth_members m
          JOIN pg_roles r ON r.oid = m.roleid
          JOIN pg_roles g ON g.oid = m.member
         WHERE r.rolname = 'g3_posting_engine' AND g.rolname = 'neondb_owner'
           AND (m.set_option OR m.inherit_option)) THEN
        RAISE EXCEPTION
            'ABORT 0091: neondb_owner holds a SET or INHERIT membership of g3_posting_engine. It '
            'is the migration principal, not the runtime one — production connects as app_user, '
            'and a second role able to assume the engine is a second door into lot birth.';
    END IF;
    -- ...and app_user's membership is the runtime one: SET, never INHERIT.
    IF NOT EXISTS (
        SELECT 1 FROM pg_auth_members m
          JOIN pg_roles r ON r.oid = m.roleid
          JOIN pg_roles g ON g.oid = m.member
         WHERE r.rolname = 'g3_posting_engine' AND g.rolname = 'app_user'
           AND m.set_option AND NOT m.inherit_option) THEN
        RAISE EXCEPTION
            'ABORT 0091: app_user does not hold exactly a SET-TRUE / INHERIT-FALSE membership of '
            'g3_posting_engine';
    END IF;
END
$verify$;

COMMIT;
