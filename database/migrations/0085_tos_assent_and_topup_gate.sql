-- =====================================================================
-- 0085_tos_assent_and_topup_gate.sql
--
-- L2C CR-29 ITEM 4: the latest-assent pin, its shared serialization boundary,
-- and the shadow-capable top-up checkout gate.
--
-- Contract: PLAN-045 §S10.G3.1-i plus the HANDOFF legal-evidence corrections.
-- Acceptance: MATRIX-045 T81 (recorded) plus the additions labelled in the test
-- suites until MATRIX is updated.
--
-- SHIPS INACTIVE. This migration creates a gate that CANNOT refuse anything: the
-- only activation row it writes is `shadow_started`, and the gate function's
-- shadow branch records what it observed and returns success unconditionally.
-- Activation is a separate, later, attested act (see g3_gate_release_attestations).
--
-- WHAT THIS IS NOT
--   * NOT checkout idempotency. There is no client_operation_id here and no
--     replay/same-session-return behaviour. That is CR-29 item 6's final contract.
--   * NOT proof a provider checkout exists. topup_checkout_intents records ASSENT
--     EVIDENCE at checkout-creation time. It carries no session id, no provider
--     binding and no lifecycle state. An intent whose subsequent provider call
--     fails leaves an orphan row: that is EXPECTED and correct. Never read this
--     table as an inventory of live checkouts.
--   * NOT a legal determination. No policy question (jurisdiction determination,
--     age treatment, paid-user rejection, re-prompt rule, blocking granularity) is
--     answered here. Where behaviour depends on one, it is activation-blocking.
--
-- THE DEFECT THIS FIXES
--   A gate that asks "does an `accepted` row exist?" passes a stale acceptance that
--   sits behind a later rejection. The predicate must be LATEST-EVENT. But ordering
--   alone cannot deliver it: BIGSERIAL values are allocated at INSERT and become
--   visible at COMMIT, so a lower event_seq can commit after a higher one — the same
--   late-commit hazard MATRIX T63 exists to catch. Ordering needs MUTUAL EXCLUSION
--   behind it, which is why every writer AND the gate take the same advisory lock as
--   their first statement.
--
-- FORWARD-ONLY ONCE APPLIED. Corrections after deployment are new migrations.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. LEGAL / POLICY RELEASE REGISTRY
--    Release identity only. The bytes a human actually read live in
--    tos_version_artifacts — one hash cannot mean both "this release" and
--    "the exact localised file this person saw".
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tos_versions (
    tos_version          TEXT        PRIMARY KEY,
    published_at         TIMESTAMPTZ NOT NULL,
    effective_at         TIMESTAMPTZ NOT NULL,
    superseded_by        TEXT        REFERENCES tos_versions(tos_version),
    is_lifecycle_cutover BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT tos_versions_version_nonblank CHECK (length(btrim(tos_version)) > 0)
);
COMMENT ON TABLE  tos_versions IS
    'Legal/policy release identity. NOT the bytes shown to a user - see tos_version_artifacts.';

-- At most one lifecycle-cutover row, ever (G3.1-e).
CREATE UNIQUE INDEX IF NOT EXISTS tos_versions_single_cutover
    ON tos_versions ((TRUE)) WHERE is_lifecycle_cutover;

-- ---------------------------------------------------------------------
-- 2. IMMUTABLE LOCALISED ARTIFACT IDENTITY
--    One row per (version, locale). artifact_sha256 is of the EXACT rendered/
--    downloadable bytes. The 3-tuple is the FK target for assent evidence, so
--    an acceptance can only ever name bytes that actually exist.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tos_version_artifacts (
    tos_version     TEXT        NOT NULL REFERENCES tos_versions(tos_version),
    locale          TEXT        NOT NULL,
    artifact_sha256 TEXT        NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    archive_uri     TEXT        NOT NULL,
    byte_size       BIGINT      NOT NULL CHECK (byte_size > 0),
    content_type    TEXT        NOT NULL,
    archived_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (tos_version, locale),
    -- composite target: assent pins reference the tuple AS A WHOLE
    CONSTRAINT tos_version_artifacts_tuple UNIQUE (tos_version, locale, artifact_sha256),
    CONSTRAINT tos_version_artifacts_locale_nonblank CHECK (length(btrim(locale)) > 0),
    CONSTRAINT tos_version_artifacts_uri_nonblank    CHECK (length(btrim(archive_uri)) > 0)
);
COMMENT ON COLUMN tos_version_artifacts.artifact_sha256 IS
    'SHA-256 of the exact rendered/downloadable bytes. NOT a release-bundle hash.';

-- ---------------------------------------------------------------------
-- 3. APPEND-ONLY PUBLICATION / WITHDRAWAL HISTORY
--    Memo 8.4 requires a served-from/served-until range for every document ever
--    displayed. served_until CANNOT be an UPDATE on an append-only artifact row,
--    so the interval is DERIVED from published/withdrawn events.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tos_artifact_publication_events (
    event_seq       BIGSERIAL   PRIMARY KEY,
    tos_version     TEXT        NOT NULL,
    locale          TEXT        NOT NULL,
    artifact_sha256 TEXT        NOT NULL,
    event           TEXT        NOT NULL CHECK (event IN ('published','withdrawn')),
    public_route    TEXT        NOT NULL CHECK (length(btrim(public_route)) > 0),
    occurred_at     TIMESTAMPTZ NOT NULL,
    FOREIGN KEY (tos_version, locale, artifact_sha256)
        REFERENCES tos_version_artifacts (tos_version, locale, artifact_sha256)
);
CREATE INDEX IF NOT EXISTS tos_artifact_publication_events_lookup
    ON tos_artifact_publication_events (tos_version, locale, event_seq DESC);

-- Derived served interval. A view, never a stored mutable column.
CREATE OR REPLACE VIEW tos_artifact_served_intervals AS
SELECT  p.tos_version,
        p.locale,
        p.artifact_sha256,
        p.public_route,
        p.occurred_at AS served_from,
        (SELECT MIN(w.occurred_at)
           FROM tos_artifact_publication_events w
          WHERE w.tos_version     = p.tos_version
            AND w.locale          = p.locale
            AND w.artifact_sha256 = p.artifact_sha256
            AND w.public_route    = p.public_route
            AND w.event           = 'withdrawn'
            AND w.event_seq       > p.event_seq) AS served_until
  FROM tos_artifact_publication_events p
 WHERE p.event = 'published';

-- ---------------------------------------------------------------------
-- 4. APPEND-ONLY ASSENT EVENTS
--    accepted and rejected carry IDENTICAL evidence pins. A rejection that
--    cannot name what was rejected is not evidence either.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tos_acceptances (
    acceptance_event_id   UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    event_seq             BIGSERIAL   NOT NULL,
    tenant_id             UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    actor_id              UUID        NOT NULL REFERENCES users(id)   ON DELETE RESTRICT,
    tos_version           TEXT        NOT NULL,
    locale                TEXT        NOT NULL,
    artifact_sha256       TEXT        NOT NULL,
    event                 TEXT        NOT NULL CHECK (event IN ('accepted','rejected')),
    event_at              TIMESTAMPTZ NOT NULL,
    surface               TEXT        NOT NULL CHECK (length(btrim(surface)) > 0),
    -- WHY this locale/version was presented. Recorded, never inferred later.
    determination_rule    TEXT        NOT NULL CHECK (length(btrim(determination_rule)) > 0),
    -- Memo 8.4 "identifier rilis": the DEPLOY that served the document, not the
    -- legal release id (that is tos_version).
    serving_deployment_id TEXT        NOT NULL
        CHECK (length(btrim(serving_deployment_id)) > 0
               AND serving_deployment_id <> 'unknown-deployment'),
    -- tuple-addressable key: the FK target for an intent's assent pin
    CONSTRAINT tos_acceptances_pin_tuple
        UNIQUE (tenant_id, actor_id, tos_version, acceptance_event_id, event_seq),
    FOREIGN KEY (tos_version, locale, artifact_sha256)
        REFERENCES tos_version_artifacts (tos_version, locale, artifact_sha256)
);
COMMENT ON TABLE tos_acceptances IS
    'Append-only ToS assent evidence. Data-minimised per memo 8.4: NO ip address, NO user-agent, NO location.';

CREATE INDEX IF NOT EXISTS tos_acceptances_latest
    ON tos_acceptances (tenant_id, actor_id, tos_version, event_seq DESC);

-- ---------------------------------------------------------------------
-- 5. GATE ACTIVATION STATE
--    Append-only. is_gate_active is STORED so an intent can bind to it by
--    composite FK; the CHECK forces it to agree with `state`.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS g3_topup_gate_activation (
    activation_seq BIGSERIAL   PRIMARY KEY,
    state          TEXT        NOT NULL
                       CHECK (state IN ('shadow_started','activated','deactivated')),
    is_gate_active BOOLEAN     NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL,
    note           TEXT,
    CONSTRAINT g3_topup_gate_state_matches_flag
        CHECK (is_gate_active = (state = 'activated')),
    CONSTRAINT g3_topup_gate_seq_state UNIQUE (activation_seq, is_gate_active)
);

-- ---------------------------------------------------------------------
-- 6. RELEASE ATTESTATION
--    Activation prerequisites that PostgreSQL cannot itself observe - counsel
--    review, UI liveness, a public route serving the hashed bytes. This records
--    that a named party ASSERTED them. It proves what was attested, NOT that the
--    assertion is true, and NOT counsel's substantive legal judgement.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS g3_gate_release_attestations (
    attestation_id        BIGSERIAL   PRIMARY KEY,
    target_sha            TEXT        NOT NULL CHECK (length(btrim(target_sha)) > 0),
    deployment_identity   TEXT        NOT NULL CHECK (length(btrim(deployment_identity)) > 0),
    artifact_hashes       JSONB       NOT NULL,
    route_verified        BOOLEAN     NOT NULL,
    ui_verified           BOOLEAN     NOT NULL,
    privacy_flow_verified BOOLEAN     NOT NULL,
    patches_reconciled    BOOLEAN     NOT NULL,
    legal_artifacts_verified BOOLEAN  NOT NULL,
    tos_version           TEXT        NOT NULL REFERENCES tos_versions(tos_version),
    policy_decisions      JSONB       NOT NULL,
    acceptance_run_ref    TEXT        NOT NULL CHECK (length(btrim(acceptance_run_ref)) > 0),
    attested_by           TEXT        NOT NULL CHECK (length(btrim(attested_by)) > 0),
    attested_at           TIMESTAMPTZ NOT NULL,
    CONSTRAINT g3_gate_attestation_artifacts_nonempty
        CHECK (jsonb_typeof(artifact_hashes) = 'object' AND artifact_hashes <> '{}'::jsonb),
    CONSTRAINT g3_gate_attestation_policy_decisions_object
        CHECK (jsonb_typeof(policy_decisions) = 'object')
);
COMMENT ON TABLE g3_gate_release_attestations IS
    'Proves WHAT WAS ATTESTED, not that the assertion is true. No cross-system fence exists.';

-- An activation names the exact attestation it relied on. A stale, unrelated
-- attestation must never unlock a later deployment merely because it exists.
ALTER TABLE g3_topup_gate_activation
    ADD COLUMN release_attestation_id BIGINT
        REFERENCES g3_gate_release_attestations(attestation_id),
    ADD CONSTRAINT g3_topup_gate_active_requires_attestation
        CHECK (state <> 'activated' OR release_attestation_id IS NOT NULL);

-- ---------------------------------------------------------------------
-- 7. TOP-UP CHECKOUT INTENT (assent evidence at checkout-creation time)
--    NO client_operation_id. NO lifecycle state. NO provider binding.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS topup_checkout_intents (
    checkout_intent_id    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id             UUID        NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    actor_id              UUID        NOT NULL REFERENCES users(id)   ON DELETE RESTRICT,
    created_at            TIMESTAMPTZ NOT NULL,
    gate_activation_seq   BIGINT      NOT NULL,
    gate_active_at_create BOOLEAN     NOT NULL,
    pack_key              TEXT        NOT NULL CHECK (length(btrim(pack_key)) > 0),
    tos_version           TEXT,
    acceptance_event_id   UUID,
    acceptance_event_seq  BIGINT,
    -- the gate pin binds to the state RECORDED at that sequence
    FOREIGN KEY (gate_activation_seq, gate_active_at_create)
        REFERENCES g3_topup_gate_activation (activation_seq, is_gate_active),
    -- the assent pin binds to the acceptance tuple AS A WHOLE (MATCH SIMPLE: an
    -- all-NULL pin is permitted, which is exactly the shadow-mode row)
    FOREIGN KEY (tenant_id, actor_id, tos_version, acceptance_event_id, acceptance_event_seq)
        REFERENCES tos_acceptances (tenant_id, actor_id, tos_version, acceptance_event_id, event_seq),
    -- gate active => all three assent pins present (implication, so shadow rows stay representable)
    CONSTRAINT topup_intent_active_requires_pins CHECK (
        NOT gate_active_at_create
        OR (tos_version IS NOT NULL AND acceptance_event_id IS NOT NULL
            AND acceptance_event_seq IS NOT NULL)),
    -- pins are all-present or all-absent; a partial pin is never valid
    CONSTRAINT topup_intent_pins_all_or_none CHECK (
        (tos_version IS NULL AND acceptance_event_id IS NULL AND acceptance_event_seq IS NULL)
     OR (tos_version IS NOT NULL AND acceptance_event_id IS NOT NULL
         AND acceptance_event_seq IS NOT NULL))
);
COMMENT ON TABLE topup_checkout_intents IS
    'Assent evidence at checkout creation. NOT proof a provider checkout exists; orphan rows are expected.';

CREATE INDEX IF NOT EXISTS topup_checkout_intents_tenant
    ON topup_checkout_intents (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------
-- 8. APPEND-ONLY ENFORCEMENT
--    Row-level for UPDATE/DELETE, statement-level for TRUNCATE. The one narrow
--    exception is tos_versions.superseded_by, NULL -> value, on an older row.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.l2c_reject_mutation() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'append-only: % on %.% is not permitted',
        TG_OP, TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END $$;

CREATE OR REPLACE FUNCTION public.l2c_tos_versions_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'append-only: DELETE on tos_versions is not permitted'
            USING ERRCODE = 'restrict_violation';
    END IF;
    -- only superseded_by may move, only NULL -> value, only forward
    IF OLD.superseded_by IS NOT NULL OR NEW.superseded_by IS NULL THEN
        RAISE EXCEPTION 'append-only: tos_versions is immutable except superseded_by NULL->value'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF (NEW.tos_version, NEW.published_at, NEW.effective_at,
        NEW.is_lifecycle_cutover, NEW.created_at)
       IS DISTINCT FROM
       (OLD.tos_version, OLD.published_at, OLD.effective_at,
        OLD.is_lifecycle_cutover, OLD.created_at) THEN
        RAISE EXCEPTION 'append-only: only superseded_by may change on tos_versions'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END $$;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['tos_version_artifacts','tos_artifact_publication_events',
                             'tos_acceptances','g3_topup_gate_activation',
                             'g3_gate_release_attestations','topup_checkout_intents'] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I_no_mutation BEFORE UPDATE OR DELETE ON public.%I
               FOR EACH ROW EXECUTE FUNCTION public.l2c_reject_mutation()', t, t);
        EXECUTE format(
            'CREATE TRIGGER %I_no_truncate BEFORE TRUNCATE ON public.%I
               FOR EACH STATEMENT EXECUTE FUNCTION public.l2c_reject_mutation()', t, t);
    END LOOP;
    EXECUTE 'CREATE TRIGGER tos_versions_guard BEFORE UPDATE OR DELETE ON public.tos_versions
               FOR EACH ROW EXECUTE FUNCTION public.l2c_tos_versions_guard()';
    EXECUTE 'CREATE TRIGGER tos_versions_no_truncate BEFORE TRUNCATE ON public.tos_versions
               FOR EACH STATEMENT EXECUTE FUNCTION public.l2c_reject_mutation()';
END $$;

-- ---------------------------------------------------------------------
-- 9. THE SHARED SERIALIZATION BOUNDARY
--    A 64-bit collision makes two unrelated (tenant, actor) pairs serialize
--    against each other: it degrades to OVER-serialization, never under. The
--    failure mode is latency, never a missed exclusion.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.assent_lock_key(p_tenant UUID, p_actor UUID)
RETURNS BIGINT LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, pg_temp AS $$
    SELECT pg_catalog.hashtextextended(p_tenant::text || ':' || p_actor::text, 0)
$$;

-- ---------------------------------------------------------------------
-- 10. WRITER: record an acceptance or rejection
--     Takes the lock FIRST. Tenant is bound to the session GUC; the actor must
--     be a real, active user OF THAT TENANT. Caller-supplied identity is not
--     trusted: p_tenant must equal the GUC or the call fails closed.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.tos_record_assent(
    p_tenant                UUID,
    p_actor                 UUID,
    p_tos_version           TEXT,
    p_locale                TEXT,
    p_artifact_sha256       TEXT,
    p_event                 TEXT,
    p_surface               TEXT,
    p_determination_rule    TEXT,
    p_serving_deployment_id TEXT
) RETURNS public.tos_acceptances
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_guc UUID;
    v_actor_guc UUID;
    v_current_version TEXT;
    v_row public.tos_acceptances;
BEGIN
    -- FIRST STATEMENT: the shared boundary.
    PERFORM pg_catalog.pg_advisory_xact_lock(public.assent_lock_key(p_tenant, p_actor));

    -- Tenant authority: the session GUC, not the argument. A missing or malformed
    -- GUC raises (22P02 for a non-uuid) and must NOT be swallowed.
    v_guc := pg_catalog.current_setting('app.current_tenant_id', TRUE)::uuid;
    IF v_guc IS NULL THEN
        RAISE EXCEPTION 'tos_record_assent: app.current_tenant_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_guc <> p_tenant THEN
        RAISE EXCEPTION 'tos_record_assent: tenant mismatch with session context'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    v_actor_guc := pg_catalog.current_setting('app.current_actor_id', TRUE)::uuid;
    IF v_actor_guc IS NULL THEN
        RAISE EXCEPTION 'tos_record_assent: app.current_actor_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_actor_guc <> p_actor THEN
        RAISE EXCEPTION 'tos_record_assent: actor mismatch with authenticated session context'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    -- Actor authority: must be an active user OF THIS TENANT.
    IF NOT EXISTS (SELECT 1 FROM public.users u
                    WHERE u.id = p_actor AND u.tenant_id = p_tenant AND u.is_active) THEN
        RAISE EXCEPTION 'tos_record_assent: actor is not an active user of this tenant'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT v.tos_version INTO v_current_version
      FROM public.tos_versions v
     WHERE v.published_at <= pg_catalog.now()
       AND v.effective_at <= pg_catalog.now()
       AND v.superseded_by IS NULL
     ORDER BY v.effective_at DESC, v.tos_version DESC
     LIMIT 1;
    IF v_current_version IS NULL OR v_current_version <> p_tos_version THEN
        RAISE EXCEPTION 'tos_record_assent: artifact is not for the current published/effective version'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM public.tos_artifact_served_intervals s
         WHERE s.tos_version = p_tos_version
           AND s.locale = p_locale
           AND s.artifact_sha256 = p_artifact_sha256
           AND s.served_until IS NULL
    ) THEN
        RAISE EXCEPTION 'tos_record_assent: artifact is not currently served'
            USING ERRCODE = 'restrict_violation';
    END IF;

    INSERT INTO public.tos_acceptances (
        tenant_id, actor_id, tos_version, locale, artifact_sha256,
        event, event_at, surface, determination_rule, serving_deployment_id)
    VALUES (
        p_tenant, p_actor, p_tos_version, p_locale, p_artifact_sha256,
        p_event, pg_catalog.clock_timestamp(),   -- server-stamped, never a parameter
        p_surface, p_determination_rule, p_serving_deployment_id)
    RETURNING * INTO v_row;

    RETURN v_row;
END $$;

-- ---------------------------------------------------------------------
-- 11. GATE: evaluate assent and record the checkout intent
--     SHADOW MODE RECORDS AND OBSERVES BUT NEVER REFUSES.
--     ACTIVE MODE FAILS CLOSED.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.topup_gate_create_intent(
    p_tenant   UUID,
    p_actor    UUID,
    p_pack_key TEXT
) RETURNS public.topup_checkout_intents
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_guc        UUID;
    v_actor_guc  UUID;
    v_gate       public.g3_topup_gate_activation;
    v_ver        TEXT;
    v_evt        public.tos_acceptances;
    v_row        public.topup_checkout_intents;
BEGIN
    -- FIRST STATEMENT: the SAME key the writer takes.
    PERFORM pg_catalog.pg_advisory_xact_lock(public.assent_lock_key(p_tenant, p_actor));

    v_guc := pg_catalog.current_setting('app.current_tenant_id', TRUE)::uuid;
    IF v_guc IS NULL THEN
        RAISE EXCEPTION 'topup_gate_create_intent: app.current_tenant_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_guc <> p_tenant THEN
        RAISE EXCEPTION 'topup_gate_create_intent: tenant mismatch with session context'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    v_actor_guc := pg_catalog.current_setting('app.current_actor_id', TRUE)::uuid;
    IF v_actor_guc IS NULL THEN
        RAISE EXCEPTION 'topup_gate_create_intent: app.current_actor_id is not set'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF v_actor_guc <> p_actor THEN
        RAISE EXCEPTION 'topup_gate_create_intent: actor mismatch with authenticated session context'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.users u
                    WHERE u.id = p_actor AND u.tenant_id = p_tenant AND u.is_active) THEN
        RAISE EXCEPTION 'topup_gate_create_intent: actor is not an active user of this tenant'
            USING ERRCODE = 'insufficient_privilege';
    END IF;

    SELECT * INTO v_gate FROM public.g3_topup_gate_activation
     ORDER BY activation_seq DESC LIMIT 1;
    IF v_gate IS NULL THEN
        RAISE EXCEPTION 'topup_gate_create_intent: no gate activation state recorded'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NOT v_gate.is_gate_active THEN
        -- SHADOW: record the false gate pin, no assent pins, NEVER refuse.
        INSERT INTO public.topup_checkout_intents (
            tenant_id, actor_id, created_at, gate_activation_seq,
            gate_active_at_create, pack_key)
        VALUES (p_tenant, p_actor, pg_catalog.clock_timestamp(),
                v_gate.activation_seq, FALSE, p_pack_key)
        RETURNING * INTO v_row;
        RETURN v_row;
    END IF;

    -- ACTIVE: fail closed.
    -- Currently published AND effective. A future effective_at never satisfies it.
    SELECT tos_version INTO v_ver
      FROM public.tos_versions
     WHERE published_at <= pg_catalog.now()
       AND effective_at <= pg_catalog.now() AND superseded_by IS NULL
     ORDER BY effective_at DESC, tos_version DESC
     LIMIT 1;
    IF v_ver IS NULL THEN
        RAISE EXCEPTION 'topup_gate_create_intent: no published, effective ToS version'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- LATEST event for the tuple, under the lock. Not "an accepted row exists".
    SELECT * INTO v_evt
      FROM public.tos_acceptances
     WHERE tenant_id = p_tenant AND actor_id = p_actor AND tos_version = v_ver
     ORDER BY event_seq DESC
     LIMIT 1;
    IF v_evt IS NULL OR v_evt.event <> 'accepted' THEN
        RAISE EXCEPTION 'topup_gate_create_intent: latest assent for the effective version is not accepted'
            USING ERRCODE = 'restrict_violation';
    END IF;

    INSERT INTO public.topup_checkout_intents (
        tenant_id, actor_id, created_at, gate_activation_seq, gate_active_at_create,
        pack_key, tos_version, acceptance_event_id, acceptance_event_seq)
    VALUES (p_tenant, p_actor, pg_catalog.clock_timestamp(),
            v_gate.activation_seq, TRUE, p_pack_key,
            v_ver, v_evt.acceptance_event_id, v_evt.event_seq)
    RETURNING * INTO v_row;

    RETURN v_row;
END $$;

-- ---------------------------------------------------------------------
-- 12. ACTIVATION GUARD: fail closed unless the evidence exists
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_topup_gate_activation_guard() RETURNS TRIGGER
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_prev  public.g3_topup_gate_activation;
    v_att   public.g3_gate_release_attestations;
    v_ver   TEXT;
BEGIN
    SELECT * INTO v_prev FROM public.g3_topup_gate_activation
     ORDER BY activation_seq DESC LIMIT 1;

    IF NEW.state = 'shadow_started' THEN
        IF v_prev IS NOT NULL THEN
            RAISE EXCEPTION 'gate: shadow_started may only be the genesis event'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF v_prev IS NULL THEN
        RAISE EXCEPTION 'gate: genesis shadow_started must exist first'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.state = 'activated' THEN
        IF v_prev.is_gate_active THEN
            RAISE EXCEPTION 'gate: already active' USING ERRCODE = 'restrict_violation';
        END IF;
        -- a published, effective version must exist
        SELECT tos_version INTO v_ver FROM public.tos_versions
         WHERE published_at <= pg_catalog.now()
           AND effective_at <= pg_catalog.now() AND superseded_by IS NULL
         ORDER BY effective_at DESC, tos_version DESC LIMIT 1;
        IF v_ver IS NULL THEN
            RAISE EXCEPTION 'gate: cannot activate without a published, effective ToS version'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- that version must have at least one published artifact
        IF NOT EXISTS (SELECT 1 FROM public.tos_artifact_served_intervals s
                        WHERE s.tos_version = v_ver AND s.served_until IS NULL) THEN
            RAISE EXCEPTION 'gate: cannot activate without a currently-served artifact'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- a complete release attestation must exist
        SELECT * INTO v_att FROM public.g3_gate_release_attestations
         WHERE attestation_id = NEW.release_attestation_id;
        IF v_att IS NULL THEN
            RAISE EXCEPTION 'gate: cannot activate without the named release attestation'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NOT (v_att.route_verified AND v_att.ui_verified
                AND v_att.privacy_flow_verified AND v_att.patches_reconciled
                AND v_att.legal_artifacts_verified) THEN
            RAISE EXCEPTION 'gate: named release attestation is incomplete'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF v_att.tos_version <> v_ver THEN
            RAISE EXCEPTION 'gate: release attestation names a different ToS version'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM pg_catalog.unnest(ARRAY['IT4-D1','IT4-D2','IT4-D3','IT4-D4','IT4-D5']) AS d(decision_key)
             WHERE pg_catalog.jsonb_typeof(v_att.policy_decisions -> d.decision_key) IS DISTINCT FROM 'string'
                OR pg_catalog.length(pg_catalog.btrim(
                       COALESCE(v_att.policy_decisions ->> d.decision_key, ''))) = 0
        ) THEN
            RAISE EXCEPTION 'gate: release attestation lacks a nonblank required policy decision'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF EXISTS (
            SELECT 1 FROM public.tos_artifact_served_intervals s
             WHERE s.tos_version = v_ver AND s.served_until IS NULL
               AND COALESCE(v_att.artifact_hashes ->> s.locale, '') <> s.artifact_sha256
        ) THEN
            RAISE EXCEPTION 'gate: release attestation artifact hashes do not match served artifacts'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.state = 'deactivated' THEN
        IF NOT v_prev.is_gate_active THEN
            RAISE EXCEPTION 'gate: not active' USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    RETURN NEW;
END $$;

CREATE TRIGGER g3_topup_gate_activation_guard_trg
    BEFORE INSERT ON g3_topup_gate_activation
    FOR EACH ROW EXECUTE FUNCTION public.g3_topup_gate_activation_guard();

-- ---------------------------------------------------------------------
-- 13. PRIVILEGES
--     0016_app_role.sql sets ALTER DEFAULT PRIVILEGES granting app_user full DML
--     on every NEW table, so these REVOKEs are mandatory, not decorative. The
--     -032 lesson: revoking PUBLIC alone leaves the app_user grant standing.
-- ---------------------------------------------------------------------
REVOKE ALL ON tos_versions, tos_version_artifacts, tos_artifact_publication_events,
              tos_acceptances, g3_topup_gate_activation, g3_gate_release_attestations,
              topup_checkout_intents
    FROM PUBLIC, app_user;

-- read-only visibility for the application; writes go through the functions only
GRANT SELECT ON tos_versions, tos_version_artifacts, tos_artifact_publication_events,
                tos_artifact_served_intervals
    TO app_user;

-- sequences: no direct access (the functions run as owner)
REVOKE ALL ON SEQUENCE tos_acceptances_event_seq_seq,
                       tos_artifact_publication_events_event_seq_seq,
                       g3_topup_gate_activation_activation_seq_seq,
                       g3_gate_release_attestations_attestation_id_seq
    FROM PUBLIC, app_user;

-- functions: PUBLIC never; app_user only the two call sites it needs
REVOKE ALL ON FUNCTION tos_record_assent(UUID,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT)  FROM PUBLIC;
REVOKE ALL ON FUNCTION topup_gate_create_intent(UUID,UUID,TEXT)                          FROM PUBLIC;
-- app_user never needs this: the lock is taken INSIDE the definer functions.
REVOKE ALL ON FUNCTION assent_lock_key(UUID,UUID)                                        FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION l2c_reject_mutation()                                             FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION l2c_tos_versions_guard()                                          FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION g3_topup_gate_activation_guard()                                  FROM PUBLIC, app_user;

GRANT EXECUTE ON FUNCTION tos_record_assent(UUID,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT,TEXT) TO app_user;
GRANT EXECUTE ON FUNCTION topup_gate_create_intent(UUID,UUID,TEXT)                        TO app_user;

-- ---------------------------------------------------------------------
-- 14. GENESIS: shadow mode. THIS IS THE ONLY ACTIVATION ROW THIS MIGRATION WRITES.
--     The gate ships INACTIVE and cannot refuse anything.
-- ---------------------------------------------------------------------
INSERT INTO g3_topup_gate_activation (state, is_gate_active, occurred_at, note)
SELECT 'shadow_started', FALSE, now(),
       'CR-29 item 4 genesis. Gate ships inactive; activation is a separate attested act.'
 WHERE NOT EXISTS (SELECT 1 FROM g3_topup_gate_activation);

COMMIT;
