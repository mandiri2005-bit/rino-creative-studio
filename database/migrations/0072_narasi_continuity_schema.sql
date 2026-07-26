-- =====================================================================
-- 0072_narasi_continuity_schema.sql
--
-- C-03 V2: the five typed, tenant-isolated continuity stores (Section 6.1) that
-- back the offline CC-01/B-07/B-08/BV2-01 lineage once C-02 enables writes.
-- Purely additive/schema-only: no runtime code reads or writes these tables
-- yet, and no existing table/column/role/policy is altered.
--
--   narasi_continuity_contracts          -- versioned Story Contract v3 envelope
--   narasi_continuity_claims             -- per-chapter/per-epoch extracted claims
--   narasi_continuity_violations         -- typed, severity-tagged predicate violations
--   narasi_continuity_coverage           -- per-(job,candidate,predicate-set) coverage
--   narasi_continuity_recovery_artifacts -- encrypted-object metadata only (no body)
--
-- Ownership model: every row binds tenant_id (CASCADE from tenants). The four
-- child stores additionally bind (tenant_id, contract_id, job_id,
-- story_contract_hash) to the owning contract's own composite binding key
-- (CASCADE) -- this is what stops a same-tenant envelope from citing another
-- tenant's actual contract/job, since the FK can only resolve against a row
-- where ALL FOUR columns simultaneously match one real contract.
--
-- Section 3's deliberate friction: a contract's (tenant_id, job_id) binds to
-- jobs(tenant_id, id) as NO ACTION DEFERRABLE INITIALLY DEFERRED. This is
-- what stops cleanup_old_jobs()'s existing 24h DELETE from silently wiping a
-- job that still has continuity evidence -- C-02 must teach that runtime
-- query to skip such jobs before enabling writes; this migration does not
-- touch database.py. Deleting the TENANT still succeeds atomically: the
-- tenant_id CASCADE removes the job AND the continuity rows in the same
-- statement, so the deferred job/contract check never finds anything left to
-- object to by the time it fires at end of transaction.
--
-- Every table enables + forces RLS with one canonical NULLIF-based policy, so
-- an absent/reset app.current_tenant_id denies all rows without a UUID-cast
-- error (NULLIF(..., '') is NULL, NULL::uuid is NULL, and `tenant_id = NULL`
-- is NULL -- never TRUE -- for every row).
--
-- V2 closure (AUDIT-FINDINGS.md): V1's `@>` containment check for
-- access_audit was order-blind, so a reorder or prepend that merely kept the
-- same SET of prior elements passed -- fixed below with an actual index-
-- sliced exact-prefix comparison. V1 let evidence be cleared without setting
-- evidence_purged_at -- fixed with an atomic evidence/expiry/purge-timestamp
-- transition enforced in the same trigger that also makes evidence_expires_at
-- immutable while evidence exists and evidence_purged_at write-once. V1 had no
-- cross-row amendment lineage check at all (self-reference and cross-job
-- supersession both passed) -- fixed with a new BEFORE INSERT trigger that
-- looks up the named parent and requires same job, version = parent + 1,
-- and identical target_language/ordered_chapter_ids. V1 bounded only each
-- evidence item's own text length, never the array itself -- fixed with an
-- explicit 1..100 count and 256 KiB serialization cap (coverage and chapter-
-- identity arrays get the same array-level, not just item-level, treatment).
--
-- V3 closure (AUDIT-FINDINGS.md round 2): the violations UPDATE guard never
-- ran on INSERT, so a row could be created already terminal, already
-- attempted, or already purged -- fixed by making the same guard function
-- branch on TG_OP and firing BEFORE INSERT OR UPDATE, plus a redundant
-- table-level CHECK that evidence and evidence_purged_at can never both be
-- non-null. attempt_count froze only against decrease, never against further
-- increase once a row went terminal -- fixed with an explicit freeze tied to
-- OLD.resolution_state <> 'open' (an increase in the very statement that
-- performs the one open->terminal transition is still allowed, since OLD is
-- still 'open' at that instant). The claim_id FK used a bare composite
-- `ON DELETE SET NULL`, which nulls every referencing column including the
-- non-null tenant_id ownership column -- fixed with PostgreSQL's column-
-- scoped `ON DELETE SET NULL (claim_id)`; the guard now also lets claim_id
-- move from a value to null ONLY when that claim row no longer exists
-- (the real retention-delete consequence), so a caller cannot manually sever
-- a still-live binding. The access-audit timestamp regex accepted any two
-- digits per time component, so PostgreSQL's own "24:00:00 means next-day
-- midnight" normalization slipped a non-canonical instant through -- fixed by
-- range-restricting hour/minute/second in the pattern itself, before any
-- cast. Recovery bucket/version/encryption values used one-argument
-- `btrim(x) = x`, which only strips spaces -- fixed with the two-argument
-- form naming the complete space/tab/newline/CR/FF/VT boundary-whitespace
-- class.
-- =====================================================================

BEGIN;

-- ── Parent ownership indexes (Section 3) ────────────────────────────────────
CREATE UNIQUE INDEX IF NOT EXISTS uq_c03_jobs_tenant_id_id     ON jobs     (tenant_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_c03_projects_tenant_id_id ON projects (tenant_id, id);

-- ── Validator functions (pure, invoker-security, no table access) ──────────

-- V2: adds an exact 1..512 count bound and a 64 KiB serialization bound on top
-- of V1's shape/format/duplicate checks (used for both ordered_chapter_ids,
-- which must be nonempty, and affected_chapter_ids, which may be empty).
CREATE OR REPLACE FUNCTION narasi_c03_valid_chapter_ids(ids jsonb, require_nonempty boolean)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT
        jsonb_typeof(ids) = 'array'
        AND jsonb_array_length(ids) <= 512
        AND (NOT require_nonempty OR jsonb_array_length(ids) > 0)
        AND octet_length(ids::text) <= 65536
        AND (SELECT bool_and(jsonb_typeof(elem) = 'string' AND (elem #>> '{}') ~ '^ch_[0-9a-f]{32}$')
             FROM jsonb_array_elements(ids) AS elem) IS NOT FALSE
        AND (SELECT count(*) FROM jsonb_array_elements_text(ids)) =
            (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(ids) AS value)
$$;

-- V2: adds the matching 1..512 key-count bound and 64 KiB serialization bound.
CREATE OR REPLACE FUNCTION narasi_c03_valid_slice_hashes(hashes jsonb, ordered_ids jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT
        jsonb_typeof(hashes) = 'object'
        AND (SELECT count(*) FROM jsonb_object_keys(hashes)) BETWEEN 1 AND 512
        AND octet_length(hashes::text) <= 65536
        AND (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(hashes) AS key)
            = (SELECT array_agg(DISTINCT value ORDER BY value) FROM jsonb_array_elements_text(ordered_ids) AS value)
        AND (SELECT bool_and(jsonb_typeof(value) = 'string' AND (value #>> '{}') ~ '^[0-9a-f]{64}$')
             FROM jsonb_each(hashes) AS kv(key, value)) IS NOT FALSE
$$;

-- Per-row amendment METADATA shape only (root has none; amendment has a
-- nonblank reason, an affected subset of this row's own chapter set, and a
-- nonempty plan). Cross-row lineage (same job, consecutive version, parent
-- language/chapter-set match, non-self) cannot be expressed here -- a CHECK
-- constraint cannot query other rows -- so that lives in the new BEFORE
-- INSERT trigger narasi_c03_guard_contract_insert below.
CREATE OR REPLACE FUNCTION narasi_c03_valid_amendment(
    version integer, supersedes uuid, reason text, affected jsonb, plan jsonb, ordered_ids jsonb
) RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN version = 1 THEN
            supersedes IS NULL AND reason IS NULL
            AND affected = '[]'::jsonb AND plan = '{}'::jsonb
        ELSE
            supersedes IS NOT NULL
            AND reason IS NOT NULL AND length(btrim(reason)) > 0
            AND jsonb_typeof(affected) = 'array' AND jsonb_array_length(affected) > 0
            AND (SELECT bool_and((elem #>> '{}') IN (SELECT jsonb_array_elements_text(ordered_ids)))
                 FROM jsonb_array_elements(affected) AS elem) IS NOT FALSE
            AND jsonb_typeof(plan) = 'object' AND plan <> '{}'::jsonb
    END
$$;

-- V2: adds an exact 1..100 item-count bound and 256 KiB serialization bound
-- on top of V1's per-item shape/500-char/offset/hash checks -- V1 constrained
-- only each item's own text length, never the array as a whole, so 1,000
-- valid-shaped items passed (AUDIT-FINDINGS.md finding 6). NULL still means
-- "no evidence, including after a recorded purge" -- unchanged from V1.
CREATE OR REPLACE FUNCTION narasi_c03_valid_evidence(evidence jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT evidence IS NULL OR (
        jsonb_typeof(evidence) = 'array'
        AND jsonb_array_length(evidence) BETWEEN 1 AND 100
        AND octet_length(evidence::text) <= 262144
        AND (SELECT bool_and(
                (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(elem) AS k)
                    = ARRAY['end','source_hash','start','text']
                AND jsonb_typeof(elem->'text') = 'string'
                AND octet_length(elem->>'text') <= 500
                AND jsonb_typeof(elem->'start') = 'number'
                AND (elem->>'start')::numeric = floor((elem->>'start')::numeric)
                AND jsonb_typeof(elem->'end') = 'number'
                AND (elem->>'end')::numeric = floor((elem->>'end')::numeric)
                AND (elem->>'start')::bigint >= 0
                AND (elem->>'start')::bigint <= (elem->>'end')::bigint
                AND jsonb_typeof(elem->'source_hash') = 'string'
                AND (elem->>'source_hash') ~ '^[0-9a-f]{64}$'
             ) FROM jsonb_array_elements(evidence) AS elem) IS NOT FALSE
    )
$$;

-- V2: adds an exact 1..10000 item-count bound and 1 MiB serialization bound.
CREATE OR REPLACE FUNCTION narasi_c03_valid_coverage(coverage jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
    SELECT
        jsonb_typeof(coverage) = 'array'
        AND jsonb_array_length(coverage) BETWEEN 1 AND 10000
        AND octet_length(coverage::text) <= 1048576
        AND (SELECT bool_and(
                (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(elem) AS k)
                    = ARRAY['chapter_id','predicate_id','state']
                AND jsonb_typeof(elem->'chapter_id') = 'string'
                AND (elem->>'chapter_id') ~ '^ch_[0-9a-f]{32}$'
                AND jsonb_typeof(elem->'predicate_id') = 'string'
                AND octet_length(elem->>'predicate_id') BETWEEN 1 AND 128
                AND (elem->>'state') IN ('complete','partial','failed','not_applicable')
             ) FROM jsonb_array_elements(coverage) AS elem) IS NOT FALSE
        AND (SELECT count(*) FROM jsonb_array_elements(coverage)) =
            (SELECT count(DISTINCT ARRAY[elem->>'chapter_id', elem->>'predicate_id'])
             FROM jsonb_array_elements(coverage) AS elem)
$$;

-- V2: `at` must now be an exact finite canonical UTC RFC3339 string ending in
-- a literal 'Z' (V1 only required that the string cast to timestamptz at
-- all, so "2026-07-22 12:00:00+00" -- a valid but noncanonical timestamp --
-- passed). Also adds a whole-array unique-event_id requirement: this
-- function is re-evaluated by the table's own CHECK constraint on every
-- UPDATE (not just INSERT), so appending a duplicate of an existing event_id
-- is caught here without any extra trigger logic (AUDIT-FINDINGS.md's
-- reorder/prepend defects are closed separately, in the trigger below, since
-- exact-prefix-vs-history is an OLD-vs-NEW comparison a pure function of the
-- new value alone cannot express).
-- V3: the hour/minute/second components are now individually range-bound
-- (00..23 / 00..59 / 00..59) inside the pattern itself. V2's `[0-9]{2}` per
-- component matched "24:00:00", which PostgreSQL's timestamptz parser
-- silently normalizes to the next day's midnight -- a real, finite, but
-- non-canonical instant that made isfinite() below true for input the
-- contract does not accept. Rejecting out-of-range components in the text
-- itself, before any cast, closes that gap regardless of what the parser
-- would have done with it.
CREATE OR REPLACE FUNCTION narasi_c03_valid_access_audit(audit jsonb)
RETURNS boolean LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    elem jsonb;
    seen_ids text[] := ARRAY[]::text[];
    this_id text;
BEGIN
    IF jsonb_typeof(audit) IS DISTINCT FROM 'array' THEN RETURN false; END IF;
    IF jsonb_array_length(audit) > 100 THEN RETURN false; END IF;
    IF octet_length(audit::text) > 32768 THEN RETURN false; END IF;
    FOR elem IN SELECT value FROM jsonb_array_elements(audit) LOOP
        IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(elem) AS k)
            IS DISTINCT FROM ARRAY['action','actor_hash','at','event_id','outcome']
        THEN
            RETURN false;
        END IF;
        IF jsonb_typeof(elem->'event_id') IS DISTINCT FROM 'string'
            OR (elem->>'event_id') !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN
            RETURN false;
        END IF;
        this_id := lower(elem->>'event_id');
        IF this_id = ANY(seen_ids) THEN
            RETURN false;
        END IF;
        seen_ids := seen_ids || this_id;
        IF jsonb_typeof(elem->'at') IS DISTINCT FROM 'string'
            OR (elem->>'at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](\.[0-9]+)?Z$'
        THEN
            RETURN false;
        END IF;
        BEGIN
            IF NOT isfinite((elem->>'at')::timestamptz) THEN
                RETURN false;
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RETURN false;
        END;
        IF jsonb_typeof(elem->'actor_hash') IS DISTINCT FROM 'string'
            OR (elem->>'actor_hash') !~ '^[0-9a-f]{64}$'
        THEN
            RETURN false;
        END IF;
        IF (elem->>'action') NOT IN ('read', 'delete', 'expire') THEN RETURN false; END IF;
        IF (elem->>'outcome') NOT IN ('allowed', 'denied', 'succeeded', 'failed') THEN RETURN false; END IF;
    END LOOP;
    RETURN true;
END;
$$;

-- ── 1. narasi_continuity_contracts ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS narasi_continuity_contracts (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id               UUID,
    job_id                   UUID        NOT NULL,
    contract_version         INTEGER     NOT NULL CHECK (contract_version > 0),
    supersedes_contract_id   UUID,
    status                   TEXT        NOT NULL DEFAULT 'validated'
                                 CHECK (status IN ('validated', 'active', 'rejected', 'superseded')),
    amendment_reason         TEXT        CHECK (amendment_reason IS NULL OR octet_length(amendment_reason) <= 2000),
    affected_chapter_ids     JSONB       NOT NULL DEFAULT '[]'::jsonb
                                 CHECK (narasi_c03_valid_chapter_ids(affected_chapter_ids, false)
                                        AND octet_length(affected_chapter_ids::text) <= 65536),
    revalidation_plan        JSONB       NOT NULL DEFAULT '{}'::jsonb
                                 CHECK (jsonb_typeof(revalidation_plan) = 'object'
                                        AND octet_length(revalidation_plan::text) <= 16384),
    story_contract           JSONB       NOT NULL
                                 CHECK (jsonb_typeof(story_contract) = 'object'
                                        AND octet_length(story_contract::text) <= 2097152),
    story_contract_hash      TEXT        NOT NULL CHECK (story_contract_hash ~ '^[0-9a-f]{64}$'),
    target_language          TEXT        NOT NULL CHECK (octet_length(target_language) BETWEEN 1 AND 64),
    contract_schema_version  TEXT        NOT NULL CHECK (octet_length(contract_schema_version) BETWEEN 1 AND 32),
    contract_prompt_version  TEXT        NOT NULL CHECK (octet_length(contract_prompt_version) BETWEEN 1 AND 32),
    compiler_version         TEXT        NOT NULL CHECK (octet_length(compiler_version) BETWEEN 1 AND 32),
    slicer_version           TEXT        NOT NULL CHECK (octet_length(slicer_version) BETWEEN 1 AND 32),
    ordered_chapter_ids      JSONB       NOT NULL CHECK (narasi_c03_valid_chapter_ids(ordered_chapter_ids, true)),
    chapter_slice_hashes     JSONB       NOT NULL
                                 CHECK (narasi_c03_valid_slice_hashes(chapter_slice_hashes, ordered_chapter_ids)),
    generation_started_at    TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_c03_contracts_tenant_id       UNIQUE (tenant_id, id),
    CONSTRAINT uq_c03_contracts_tenant_job_ver  UNIQUE (tenant_id, job_id, contract_version),
    CONSTRAINT uq_c03_contracts_binding         UNIQUE (tenant_id, id, job_id, story_contract_hash),
    CONSTRAINT narasi_c03_contracts_amendment_valid
        CHECK (narasi_c03_valid_amendment(
            contract_version, supersedes_contract_id, amendment_reason,
            affected_chapter_ids, revalidation_plan, ordered_chapter_ids
        )),

    FOREIGN KEY (tenant_id, job_id)
        REFERENCES jobs (tenant_id, id) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (tenant_id, project_id)
        REFERENCES projects (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, supersedes_contract_id)
        REFERENCES narasi_continuity_contracts (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS idx_c03_contracts_tenant_job ON narasi_continuity_contracts (tenant_id, job_id);
CREATE INDEX IF NOT EXISTS idx_c03_contracts_project     ON narasi_continuity_contracts (tenant_id, project_id)
    WHERE project_id IS NOT NULL;

-- V2: prevents lineage branching -- at most one row may name a given contract
-- as its supersedes_contract_id, so the version chain can only ever be linear.
CREATE UNIQUE INDEX IF NOT EXISTS uq_c03_contracts_no_branching
    ON narasi_continuity_contracts (tenant_id, supersedes_contract_id)
    WHERE supersedes_contract_id IS NOT NULL;

-- ── 2. narasi_continuity_claims ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS narasi_continuity_claims (
    id                        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contract_id               UUID        NOT NULL,
    job_id                    UUID        NOT NULL,
    chapter_id                TEXT        NOT NULL CHECK (chapter_id ~ '^ch_[0-9a-f]{32}$'),
    chapter_content_hash      TEXT        NOT NULL CHECK (chapter_content_hash ~ '^[0-9a-f]{64}$'),
    story_contract_hash       TEXT        NOT NULL CHECK (story_contract_hash ~ '^[0-9a-f]{64}$'),
    extractor_schema_version  TEXT        NOT NULL CHECK (octet_length(extractor_schema_version) BETWEEN 1 AND 32),
    extractor_prompt_version  TEXT        NOT NULL CHECK (octet_length(extractor_prompt_version) BETWEEN 1 AND 32),
    extractor_epoch           INTEGER     NOT NULL CHECK (extractor_epoch >= 0),
    model_route               TEXT        NOT NULL CHECK (octet_length(model_route) BETWEEN 1 AND 128),
    claims                    JSONB       NOT NULL
                                  CHECK (jsonb_typeof(claims) = 'array' AND octet_length(claims::text) <= 262144),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at                TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),

    CONSTRAINT uq_c03_claims_tenant_id     UNIQUE (tenant_id, id),
    CONSTRAINT uq_c03_claims_idempotency   UNIQUE (job_id, chapter_id, chapter_content_hash, story_contract_hash,
                                                    extractor_epoch, model_route),
    CONSTRAINT narasi_c03_claims_expiry_window
        CHECK (expires_at > created_at AND expires_at <= created_at + interval '30 days'),

    FOREIGN KEY (tenant_id, contract_id, job_id, story_contract_hash)
        REFERENCES narasi_continuity_contracts (tenant_id, id, job_id, story_contract_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_c03_claims_tenant_contract ON narasi_continuity_claims (tenant_id, contract_id);

-- ── 3. narasi_continuity_violations ─────────────────────────────────────────
-- V3: claim_id's FK now nulls only itself on claim deletion (PostgreSQL's
-- column-scoped `ON DELETE SET NULL (claim_id)`, not the bare form, which
-- would null every referencing column including the non-null tenant_id
-- ownership column and fail the delete outright). The new
-- evidence_purge_agreement CHECK is a redundant, table-level guarantee that
-- evidence and evidence_purged_at can never both be non-null; the guard
-- trigger below is what actually prevents that state from arising, on both
-- INSERT and UPDATE.
CREATE TABLE IF NOT EXISTS narasi_continuity_violations (
    id                       UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contract_id              UUID        NOT NULL,
    claim_id                 UUID,
    job_id                   UUID        NOT NULL,
    story_contract_hash      TEXT        NOT NULL CHECK (story_contract_hash ~ '^[0-9a-f]{64}$'),
    predicate_id             TEXT        NOT NULL CHECK (octet_length(predicate_id) BETWEEN 1 AND 128),
    predicate_set_version    TEXT        NOT NULL CHECK (octet_length(predicate_set_version) BETWEEN 1 AND 64),
    violation_type           TEXT        NOT NULL CHECK (octet_length(violation_type) BETWEEN 1 AND 128),
    severity                 TEXT        NOT NULL CHECK (severity IN ('info', 'low', 'medium', 'high', 'blocker')),
    evidence                 JSONB       CHECK (narasi_c03_valid_evidence(evidence)),
    attempt_count            INTEGER     NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    resolution_state         TEXT        NOT NULL DEFAULT 'open'
                                 CHECK (resolution_state IN ('open', 'repaired', 'dismissed', 'unresolved')),
    resolved_at              TIMESTAMPTZ,
    evidence_expires_at      TIMESTAMPTZ,
    evidence_purged_at       TIMESTAMPTZ,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT narasi_c03_violations_resolution_agreement
        CHECK (
            (resolution_state IN ('repaired', 'dismissed', 'unresolved') AND resolved_at IS NOT NULL)
            OR (resolution_state = 'open' AND resolved_at IS NULL)
        ),
    CONSTRAINT narasi_c03_violations_evidence_expiry
        CHECK (
            (evidence IS NULL AND evidence_expires_at IS NULL)
            OR (evidence IS NOT NULL AND evidence_expires_at IS NOT NULL
                AND evidence_expires_at > created_at
                AND evidence_expires_at <= created_at + interval '30 days')
        ),
    CONSTRAINT narasi_c03_violations_evidence_purge_agreement
        CHECK (evidence IS NULL OR evidence_purged_at IS NULL),

    FOREIGN KEY (tenant_id, contract_id, job_id, story_contract_hash)
        REFERENCES narasi_continuity_contracts (tenant_id, id, job_id, story_contract_hash) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, claim_id)
        REFERENCES narasi_continuity_claims (tenant_id, id) ON DELETE SET NULL (claim_id)
);

CREATE INDEX IF NOT EXISTS idx_c03_violations_tenant_contract ON narasi_continuity_violations (tenant_id, contract_id);
CREATE INDEX IF NOT EXISTS idx_c03_violations_claim ON narasi_continuity_violations (tenant_id, claim_id)
    WHERE claim_id IS NOT NULL;

-- ── 4. narasi_continuity_coverage ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS narasi_continuity_coverage (
    id                        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                 UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contract_id               UUID        NOT NULL,
    job_id                    UUID        NOT NULL,
    story_contract_hash       TEXT        NOT NULL CHECK (story_contract_hash ~ '^[0-9a-f]{64}$'),
    final_candidate_hash      TEXT        NOT NULL CHECK (final_candidate_hash ~ '^[0-9a-f]{64}$'),
    predicate_set_version     TEXT        NOT NULL CHECK (octet_length(predicate_set_version) BETWEEN 1 AND 64),
    extractor_schema_version  TEXT        NOT NULL CHECK (octet_length(extractor_schema_version) BETWEEN 1 AND 32),
    extractor_prompt_version  TEXT        NOT NULL CHECK (octet_length(extractor_prompt_version) BETWEEN 1 AND 32),
    extractor_epoch           INTEGER     NOT NULL CHECK (extractor_epoch >= 0),
    diff_version              TEXT        NOT NULL CHECK (octet_length(diff_version) BETWEEN 1 AND 32),
    coverage                  JSONB       NOT NULL CHECK (narasi_c03_valid_coverage(coverage)),
    coverage_hash             TEXT        NOT NULL CHECK (coverage_hash ~ '^[0-9a-f]{64}$'),
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_c03_coverage_idempotency UNIQUE (job_id, final_candidate_hash, predicate_set_version),

    FOREIGN KEY (tenant_id, contract_id, job_id, story_contract_hash)
        REFERENCES narasi_continuity_contracts (tenant_id, id, job_id, story_contract_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_c03_coverage_tenant_contract ON narasi_continuity_coverage (tenant_id, contract_id);

-- ── 5. narasi_continuity_recovery_artifacts ─────────────────────────────────
-- V2: object_bucket/encryption_algorithm/encryption_key_id must be trimmed
-- nonblank (btrim(x) = x rejects both all-whitespace and leading/trailing-
-- padded values, given the existing >=1-byte length bound); object_key
-- additionally rejects a trailing slash so every path segment is nonempty.
-- V3: the trim now names the complete PostgreSQL `[[:space:]]` boundary class
-- (space, tab, newline, CR, FF, VT) via two-argument btrim, since one-argument
-- btrim(text) only strips the space character -- a tab- or newline-only or
-- -padded value satisfied V2's check without containing a single visible
-- character.
CREATE TABLE IF NOT EXISTS narasi_continuity_recovery_artifacts (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    contract_id             UUID        NOT NULL,
    job_id                  UUID        NOT NULL,
    story_contract_hash     TEXT        NOT NULL CHECK (story_contract_hash ~ '^[0-9a-f]{64}$'),
    final_candidate_hash    TEXT        NOT NULL CHECK (final_candidate_hash ~ '^[0-9a-f]{64}$'),
    object_bucket           TEXT        NOT NULL
                                CHECK (btrim(object_bucket, E' \t\n\r\f\v') = object_bucket
                                       AND octet_length(object_bucket) BETWEEN 1 AND 128),
    object_key              TEXT        NOT NULL
                                CHECK (
                                    octet_length(object_key) BETWEEN 1 AND 1024
                                    AND object_key !~ '://'
                                    AND left(object_key, 1) <> '/'
                                    AND right(object_key, 1) <> '/'
                                    AND object_key !~ '[\x00-\x1f\x7f]'
                                    AND object_key !~ '\\'
                                    AND object_key !~ '//'
                                    AND NOT (string_to_array(object_key, '/') && ARRAY['.', '..'])
                                ),
    object_version          TEXT        CHECK (object_version IS NULL OR (
                                    btrim(object_version, E' \t\n\r\f\v') = object_version
                                    AND octet_length(object_version) BETWEEN 1 AND 128
                                )),
    encryption_algorithm    TEXT        NOT NULL
                                CHECK (btrim(encryption_algorithm, E' \t\n\r\f\v') = encryption_algorithm
                                       AND octet_length(encryption_algorithm) BETWEEN 1 AND 64),
    encryption_key_id       TEXT        NOT NULL
                                CHECK (btrim(encryption_key_id, E' \t\n\r\f\v') = encryption_key_id
                                       AND octet_length(encryption_key_id) BETWEEN 1 AND 256),
    ciphertext_sha256       TEXT        NOT NULL CHECK (ciphertext_sha256 ~ '^[0-9a-f]{64}$'),
    ciphertext_size_bytes   BIGINT      NOT NULL CHECK (ciphertext_size_bytes > 0),
    terminal_at             TIMESTAMPTZ NOT NULL,
    expires_at              TIMESTAMPTZ NOT NULL
                                CHECK (expires_at > terminal_at AND expires_at <= terminal_at + interval '30 days'),
    deleted_at               TIMESTAMPTZ,
    access_audit             JSONB       NOT NULL DEFAULT '[]'::jsonb CHECK (narasi_c03_valid_access_audit(access_audit)),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    FOREIGN KEY (tenant_id, contract_id, job_id, story_contract_hash)
        REFERENCES narasi_continuity_contracts (tenant_id, id, job_id, story_contract_hash) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_c03_recovery_tenant_contract ON narasi_continuity_recovery_artifacts (tenant_id, contract_id);

-- ── Mutation-guard trigger functions (invoker-security; no RLS bypass) ──────

-- V2 (new): a new contract must always start `validated` with no generation
-- timestamp (root or amendment alike). When it is an amendment, this looks up
-- the named parent -- within the SAME tenant/job -- and requires the new row
-- to be exactly one version ahead with identical target_language and
-- ordered_chapter_ids; self-reference and any parent lookup miss (including
-- a same-tenant, different-job "parent") both fail closed. This is cross-row
-- validation a CHECK constraint cannot express; narasi_c03_valid_amendment
-- above still separately validates this row's own amendment-metadata shape.
CREATE OR REPLACE FUNCTION narasi_c03_guard_contract_insert()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    parent narasi_continuity_contracts%ROWTYPE;
BEGIN
    IF NEW.status <> 'validated' OR NEW.generation_started_at IS NOT NULL THEN
        RAISE EXCEPTION 'narasi_continuity_contracts: a new contract must be validated with no generation timestamp';
    END IF;

    IF NEW.supersedes_contract_id IS NOT NULL THEN
        IF NEW.supersedes_contract_id = NEW.id THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: a contract cannot supersede itself';
        END IF;
        SELECT * INTO parent FROM narasi_continuity_contracts
            WHERE tenant_id = NEW.tenant_id AND id = NEW.supersedes_contract_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: supersedes_contract_id must reference an existing same-tenant contract';
        END IF;
        IF parent.job_id <> NEW.job_id THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: an amendment must supersede a contract for the same job';
        END IF;
        IF NEW.contract_version <> parent.contract_version + 1 THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: amendment version must be exactly the parent version plus one';
        END IF;
        IF NEW.target_language <> parent.target_language THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: amendment target_language must match the parent';
        END IF;
        IF NEW.ordered_chapter_ids <> parent.ordered_chapter_ids THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: amendment chapter identities must match the parent';
        END IF;
    END IF;

    -- V4: this guard owns updated_at on INSERT too -- a caller-supplied value
    -- (including a deliberate regression) is replaced with the real current
    -- time, matching the UPDATE guard below.
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION narasi_c03_guard_contract_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.contract_version IS DISTINCT FROM OLD.contract_version
        OR NEW.supersedes_contract_id IS DISTINCT FROM OLD.supersedes_contract_id
        OR NEW.amendment_reason IS DISTINCT FROM OLD.amendment_reason
        OR NEW.affected_chapter_ids IS DISTINCT FROM OLD.affected_chapter_ids
        OR NEW.revalidation_plan IS DISTINCT FROM OLD.revalidation_plan
        OR NEW.story_contract IS DISTINCT FROM OLD.story_contract
        OR NEW.story_contract_hash IS DISTINCT FROM OLD.story_contract_hash
        OR NEW.target_language IS DISTINCT FROM OLD.target_language
        OR NEW.contract_schema_version IS DISTINCT FROM OLD.contract_schema_version
        OR NEW.contract_prompt_version IS DISTINCT FROM OLD.contract_prompt_version
        OR NEW.compiler_version IS DISTINCT FROM OLD.compiler_version
        OR NEW.slicer_version IS DISTINCT FROM OLD.slicer_version
        OR NEW.ordered_chapter_ids IS DISTINCT FROM OLD.ordered_chapter_ids
        OR NEW.chapter_slice_hashes IS DISTINCT FROM OLD.chapter_slice_hashes
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'narasi_continuity_contracts: only status/generation_started_at/updated_at may change';
    END IF;

    IF OLD.status = 'validated' AND NEW.status = 'active' THEN
        IF OLD.generation_started_at IS NOT NULL OR NEW.generation_started_at IS NULL THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: validated->active must set generation_started_at exactly once';
        END IF;
    ELSIF OLD.status = 'validated' AND NEW.status = 'rejected' THEN
        IF NEW.generation_started_at IS DISTINCT FROM OLD.generation_started_at THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: rejected transition must not touch generation_started_at';
        END IF;
    ELSIF OLD.status = 'active' AND NEW.status = 'superseded' THEN
        IF NEW.generation_started_at IS DISTINCT FROM OLD.generation_started_at THEN
            RAISE EXCEPTION 'narasi_continuity_contracts: superseded transition must not touch generation_started_at';
        END IF;
    ELSE
        RAISE EXCEPTION 'narasi_continuity_contracts: status transition % -> % is not permitted', OLD.status, NEW.status;
    END IF;

    -- V2: this guard owns updated_at outright -- any caller-supplied value
    -- (including an attempted regression) is replaced with the real current
    -- time, never trusted.
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION narasi_c03_reject_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is immutable after insertion', TG_TABLE_NAME;
END;
$$;

-- V2: evidence purge is now one atomic transition (evidence and
-- evidence_expires_at both clear to NULL while evidence_purged_at is set,
-- exactly once, in the same statement); evidence_expires_at is immutable
-- while evidence is still present (V1 allowed it to be rewritten
-- independently); evidence_purged_at is write-once once evidence is absent.
-- The resolution-state machine is now one-way: `open` may repeat/advance
-- attempts, but the single transition into repaired/dismissed/unresolved is
-- terminal -- neither resolution_state nor resolved_at may change again.
-- V3: this same function now also fires BEFORE INSERT (see the trigger
-- below), since V2's guard covered only UPDATE and a row could be created
-- already terminal, already attempted, or already purged. On INSERT it
-- enforces the open/unresolved/zero-attempt/unpurged starting state; the
-- redundant table-level CHECK above covers the evidence/purge-timestamp
-- coexistence half of that same invariant. On UPDATE, attempt_count now
-- freezes together with resolution_state/resolved_at once
-- OLD.resolution_state is no longer 'open' -- an increase in the very
-- statement that performs the one open->terminal transition is still fine,
-- since OLD is still 'open' at that instant; only a LATER attempt to move it
-- fails. claim_id is no longer in the blanket identity-immutability list
-- below: it may move from a value to NULL, but only when that claim row no
-- longer exists -- i.e. only as the real consequence of the claim's own
-- retention delete (PostgreSQL's `ON DELETE SET NULL (claim_id)` cascades
-- into this same trigger), never as a direct caller edit severing a still-
-- live binding.
CREATE OR REPLACE FUNCTION narasi_c03_guard_violation_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.resolution_state <> 'open' OR NEW.resolved_at IS NOT NULL THEN
            RAISE EXCEPTION 'narasi_continuity_violations: a new violation must start open with no resolution timestamp';
        END IF;
        IF NEW.attempt_count <> 0 THEN
            RAISE EXCEPTION 'narasi_continuity_violations: a new violation must start at zero attempts';
        END IF;
        IF NEW.evidence_purged_at IS NOT NULL THEN
            RAISE EXCEPTION 'narasi_continuity_violations: a new violation cannot start already purged';
        END IF;
        -- V4: this guard owns updated_at on INSERT too -- see the matching
        -- fix in narasi_c03_guard_contract_insert() above.
        NEW.updated_at := now();
        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.contract_id IS DISTINCT FROM OLD.contract_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.story_contract_hash IS DISTINCT FROM OLD.story_contract_hash
        OR NEW.predicate_id IS DISTINCT FROM OLD.predicate_id
        OR NEW.predicate_set_version IS DISTINCT FROM OLD.predicate_set_version
        OR NEW.violation_type IS DISTINCT FROM OLD.violation_type
        OR NEW.severity IS DISTINCT FROM OLD.severity
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'narasi_continuity_violations: identity/predicate fields are immutable';
    END IF;

    IF NEW.claim_id IS DISTINCT FROM OLD.claim_id THEN
        IF OLD.claim_id IS NULL OR NEW.claim_id IS NOT NULL THEN
            RAISE EXCEPTION 'narasi_continuity_violations: claim_id may only transition from an existing value to null';
        END IF;
        IF EXISTS (
            SELECT 1 FROM narasi_continuity_claims
            WHERE tenant_id = OLD.tenant_id AND id = OLD.claim_id
        ) THEN
            RAISE EXCEPTION 'narasi_continuity_violations: claim_id cannot be cleared while the referenced claim still exists';
        END IF;
    END IF;

    IF NEW.attempt_count < OLD.attempt_count THEN
        RAISE EXCEPTION 'narasi_continuity_violations: attempt_count cannot decrease';
    END IF;
    IF OLD.resolution_state <> 'open' AND NEW.attempt_count <> OLD.attempt_count THEN
        RAISE EXCEPTION 'narasi_continuity_violations: attempt_count is frozen after a terminal resolution';
    END IF;

    IF OLD.evidence IS NOT NULL THEN
        IF NEW.evidence IS NULL THEN
            IF NEW.evidence_expires_at IS NOT NULL THEN
                RAISE EXCEPTION 'narasi_continuity_violations: purge must clear evidence_expires_at';
            END IF;
            IF OLD.evidence_purged_at IS NOT NULL OR NEW.evidence_purged_at IS NULL THEN
                RAISE EXCEPTION 'narasi_continuity_violations: purge must set evidence_purged_at exactly once, atomically';
            END IF;
        ELSE
            IF NEW.evidence IS DISTINCT FROM OLD.evidence THEN
                RAISE EXCEPTION 'narasi_continuity_violations: evidence may only be cleared, never replaced';
            END IF;
            IF NEW.evidence_expires_at IS DISTINCT FROM OLD.evidence_expires_at THEN
                RAISE EXCEPTION 'narasi_continuity_violations: evidence_expires_at is immutable while evidence exists';
            END IF;
            IF NEW.evidence_purged_at IS DISTINCT FROM OLD.evidence_purged_at THEN
                RAISE EXCEPTION 'narasi_continuity_violations: evidence_purged_at cannot be set while evidence exists';
            END IF;
        END IF;
    ELSE
        IF NEW.evidence IS NOT NULL THEN
            RAISE EXCEPTION 'narasi_continuity_violations: evidence cannot be reintroduced once absent';
        END IF;
        IF NEW.evidence_purged_at IS DISTINCT FROM OLD.evidence_purged_at THEN
            RAISE EXCEPTION 'narasi_continuity_violations: evidence_purged_at is write-once';
        END IF;
        IF NEW.evidence_expires_at IS DISTINCT FROM OLD.evidence_expires_at THEN
            RAISE EXCEPTION 'narasi_continuity_violations: evidence_expires_at cannot change once evidence is absent';
        END IF;
    END IF;

    IF OLD.resolution_state <> 'open' THEN
        IF NEW.resolution_state IS DISTINCT FROM OLD.resolution_state
            OR NEW.resolved_at IS DISTINCT FROM OLD.resolved_at
        THEN
            RAISE EXCEPTION 'narasi_continuity_violations: a terminal resolution_state/resolved_at cannot change';
        END IF;
    ELSIF NEW.resolution_state <> 'open' THEN
        IF NEW.resolution_state NOT IN ('repaired', 'dismissed', 'unresolved') THEN
            RAISE EXCEPTION 'narasi_continuity_violations: invalid resolution_state transition';
        END IF;
        IF NEW.resolved_at IS NULL THEN
            RAISE EXCEPTION 'narasi_continuity_violations: a terminal resolution_state requires resolved_at';
        END IF;
    ELSE
        IF NEW.resolved_at IS DISTINCT FROM OLD.resolved_at THEN
            RAISE EXCEPTION 'narasi_continuity_violations: resolved_at cannot be set while resolution_state remains open';
        END IF;
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION narasi_c03_guard_coverage_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'narasi_continuity_coverage rows are immutable after insertion';
END;
$$;

-- V2: replaces V1's `@>` containment check (order-blind: a reorder or
-- prepend that kept the same SET of prior elements wrongly passed) with a
-- true exact-prefix comparison via array slicing -- the first
-- length(OLD.access_audit) elements of NEW must be identical, in the same
-- order, to OLD in full; NEW may only be longer, never shorter, and only the
-- tail beyond that point may differ. Global event_id uniqueness and the
-- canonical-timestamp format are enforced by narasi_c03_valid_access_audit's
-- own CHECK constraint, which re-fires on this same UPDATE.
CREATE OR REPLACE FUNCTION narasi_c03_guard_recovery_update()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    old_arr jsonb[];
    new_arr jsonb[];
    old_len integer;
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
        OR NEW.contract_id IS DISTINCT FROM OLD.contract_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.story_contract_hash IS DISTINCT FROM OLD.story_contract_hash
        OR NEW.final_candidate_hash IS DISTINCT FROM OLD.final_candidate_hash
        OR NEW.object_bucket IS DISTINCT FROM OLD.object_bucket
        OR NEW.object_key IS DISTINCT FROM OLD.object_key
        OR NEW.object_version IS DISTINCT FROM OLD.object_version
        OR NEW.encryption_algorithm IS DISTINCT FROM OLD.encryption_algorithm
        OR NEW.encryption_key_id IS DISTINCT FROM OLD.encryption_key_id
        OR NEW.ciphertext_sha256 IS DISTINCT FROM OLD.ciphertext_sha256
        OR NEW.ciphertext_size_bytes IS DISTINCT FROM OLD.ciphertext_size_bytes
        OR NEW.terminal_at IS DISTINCT FROM OLD.terminal_at
        OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'narasi_continuity_recovery_artifacts: only deleted_at/access_audit/updated_at may change';
    END IF;

    IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
        RAISE EXCEPTION 'narasi_continuity_recovery_artifacts: deleted_at cannot change once set';
    END IF;

    SELECT array_agg(elem ORDER BY idx) INTO old_arr
        FROM jsonb_array_elements(OLD.access_audit) WITH ORDINALITY AS t(elem, idx);
    SELECT array_agg(elem ORDER BY idx) INTO new_arr
        FROM jsonb_array_elements(NEW.access_audit) WITH ORDINALITY AS t(elem, idx);
    old_len := coalesce(array_length(old_arr, 1), 0);
    IF coalesce(array_length(new_arr, 1), 0) < old_len THEN
        RAISE EXCEPTION 'narasi_continuity_recovery_artifacts: access_audit cannot shrink';
    END IF;
    IF old_len > 0 AND new_arr[1:old_len] IS DISTINCT FROM old_arr THEN
        RAISE EXCEPTION 'narasi_continuity_recovery_artifacts: access_audit must be exact-prefix append-only';
    END IF;

    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_c03_contracts_guard_insert ON narasi_continuity_contracts;
CREATE TRIGGER trg_c03_contracts_guard_insert
    BEFORE INSERT ON narasi_continuity_contracts
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_guard_contract_insert();

DROP TRIGGER IF EXISTS trg_c03_contracts_guard ON narasi_continuity_contracts;
CREATE TRIGGER trg_c03_contracts_guard
    BEFORE UPDATE ON narasi_continuity_contracts
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_guard_contract_update();

DROP TRIGGER IF EXISTS trg_c03_claims_guard ON narasi_continuity_claims;
CREATE TRIGGER trg_c03_claims_guard
    BEFORE UPDATE ON narasi_continuity_claims
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_reject_update();

DROP TRIGGER IF EXISTS trg_c03_violations_guard ON narasi_continuity_violations;
CREATE TRIGGER trg_c03_violations_guard
    BEFORE INSERT OR UPDATE ON narasi_continuity_violations
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_guard_violation_update();

DROP TRIGGER IF EXISTS trg_c03_coverage_guard ON narasi_continuity_coverage;
CREATE TRIGGER trg_c03_coverage_guard
    BEFORE UPDATE ON narasi_continuity_coverage
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_guard_coverage_update();

DROP TRIGGER IF EXISTS trg_c03_recovery_guard ON narasi_continuity_recovery_artifacts;
CREATE TRIGGER trg_c03_recovery_guard
    BEFORE UPDATE ON narasi_continuity_recovery_artifacts
    FOR EACH ROW EXECUTE FUNCTION narasi_c03_guard_recovery_update();

-- ── Row-Level Security: ENABLE + FORCE + one NULLIF-based policy ────────────
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'narasi_continuity_contracts', 'narasi_continuity_claims', 'narasi_continuity_violations',
        'narasi_continuity_coverage', 'narasi_continuity_recovery_artifacts'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY;', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY;', t);
        EXECUTE format('DROP POLICY IF EXISTS narasi_c03_tenant_isolation ON %I;', t);
        EXECUTE format($f$
            CREATE POLICY narasi_c03_tenant_isolation ON %I
                USING      (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)
                WITH CHECK (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
        $f$, t);
    END LOOP;
END $$;

-- ── Least-privilege grants (Section 2) ──────────────────────────────────────
REVOKE ALL ON narasi_continuity_contracts, narasi_continuity_claims, narasi_continuity_violations,
    narasi_continuity_coverage, narasi_continuity_recovery_artifacts FROM PUBLIC;
REVOKE ALL ON narasi_continuity_contracts, narasi_continuity_claims, narasi_continuity_violations,
    narasi_continuity_coverage, narasi_continuity_recovery_artifacts FROM app_user;

GRANT SELECT, INSERT, UPDATE ON narasi_continuity_contracts TO app_user;
GRANT SELECT, INSERT, DELETE ON narasi_continuity_claims TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON narasi_continuity_violations TO app_user;
GRANT SELECT, INSERT ON narasi_continuity_coverage TO app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON narasi_continuity_recovery_artifacts TO app_user;

-- ── Comments ─────────────────────────────────────────────────────────────────
COMMENT ON TABLE narasi_continuity_contracts IS 'C-03: versioned Story Contract v3 envelope. Immutable after insertion except the validated->active->superseded / validated->rejected status transitions; amendments are new, same-job, consecutive-version, language/chapter-stable rows, never in-place edits.';
COMMENT ON TABLE narasi_continuity_claims IS 'C-03: per-chapter/per-extractor-epoch extracted claims. Fully immutable; deleted only for retention.';
COMMENT ON TABLE narasi_continuity_violations IS 'C-03: typed, severity-tagged predicate violations. Evidence purge is one atomic evidence/expiry/purge-timestamp transition; resolution_state has a one-way terminal transition out of open.';
COMMENT ON TABLE narasi_continuity_coverage IS 'C-03: one row per (job, final_candidate_hash, predicate_set_version). Fully immutable after insertion.';
COMMENT ON TABLE narasi_continuity_recovery_artifacts IS 'C-03: encrypted quality_failed candidate metadata only -- the manuscript body itself is never stored here. access_audit is exact-prefix append-only; deleted_at may be set exactly once.';
COMMENT ON COLUMN narasi_continuity_contracts.job_id IS 'NO ACTION DEFERRABLE INITIALLY DEFERRED against jobs(tenant_id, id) -- stops the existing 24h cleanup_old_jobs() from deleting a job with continuity evidence, while still allowing an atomic tenant-cascade delete.';
COMMENT ON COLUMN narasi_continuity_recovery_artifacts.object_key IS 'Relative private-object key only: no scheme, leading/trailing slash, backslash, control character, empty segment, or ./.. segment. Never a public URL.';

COMMIT;
