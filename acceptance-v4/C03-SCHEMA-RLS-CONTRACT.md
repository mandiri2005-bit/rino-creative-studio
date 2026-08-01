# C-03 Schema/RLS Acceptance Contract V4

V4 supersedes V1, V2, and V3. Read `AUDIT-FINDINGS.md` first. Every earlier requirement remains in
force; V4 makes the retained timestamp-ownership rule executable on INSERT.

## 0. V4 final closure rule

Both allowed BEFORE INSERT guards, `narasi_c03_guard_contract_insert()` and the INSERT branch of
`narasi_c03_guard_violation_update()`, must assign `NEW.updated_at := now()` before returning. A
caller-supplied past or future `updated_at` is overwritten. This is the INSERT equivalent of the
already-required UPDATE behavior and does not authorize caller control of lifecycle timestamps.

## 0A. V3 closure rules retained by V4

- Every violation INSERT starts with `resolution_state='open'`, `resolved_at IS NULL`,
  `attempt_count=0`, and `evidence_purged_at IS NULL`. A row with evidence may never coexist with a
  purge timestamp. Purge remains an UPDATE-only atomic transition.
- Attempts may increase only while the old and new resolution states are both `open`. Once a row
  reaches `repaired|dismissed|unresolved`, `attempt_count`, resolution state, and `resolved_at` are
  all terminal and immutable. Retention-only evidence purge remains allowed after resolution.
- Expiry deletion of a referenced claim must succeed, preserve the same-tenant violation row, keep
  its `tenant_id` unchanged, and set only `claim_id` to null. The composite FK action must never try
  to null the ownership column.
- Canonical audit timestamps use `YYYY-MM-DDTHH:MM:SS[.fraction]Z` with hour `00..23`, minute
  `00..59`, and second `00..59`, and must also parse as a valid finite timestamp. PostgreSQL parser
  normalization (including `24:00:00`) is not canonical input.
- "Trimmed nonblank" recovery metadata means at least one non-whitespace character and no leading
  or trailing PostgreSQL `[[:space:]]` character. This applies to `object_bucket`, non-null
  `object_version`, `encryption_algorithm`, and `encryption_key_id`; internal non-boundary
  whitespace is not prohibited by this schema contract.

## 0B. V2 closure rules retained by V3

- Ordered chapter IDs contain `1..512` unique B-08 IDs and serialize to at most 64 KiB.
- Slice-hash maps have the exact same `1..512` keys and serialize to at most 64 KiB.
- Violation evidence contains `1..100` exact records and serializes to at most 256 KiB; null remains
  valid after a recorded purge.
- Coverage contains `1..10000` exact records and serializes to at most 1 MiB.
- An inserted contract must be `validated` with `generation_started_at IS NULL`.
- An amendment must reference a distinct, already-existing contract for the same tenant and job;
  its version is exactly parent version plus one; `target_language` and ordered chapter identities
  equal the parent. Branching/skipping, self-reference, cross-job supersession, and language/chapter
  identity drift fail.
- Violation state starts `open` with zero attempts. It may remain open while attempts rise or transition once to
  `repaired|dismissed|unresolved`; every terminal state has a non-null `resolved_at`, and terminal
  state/timestamp cannot be changed. Evidence expiry is immutable while evidence exists. Purge is
  one atomic transition to `evidence=NULL`, `evidence_expires_at=NULL`, and non-null
  `evidence_purged_at`; it cannot be omitted, changed, or reversed.
- Recovery audit is **exact-prefix append-only**: for old length N, every new element `0..N-1` is
  byte/equality-identical and new items may occur only at indices N onward. Reorder, prepend,
  replacement, shrink, and duplicate `event_id` fail. Audit contains at most 100 records/32 KiB;
  `at` is finite canonical UTC RFC3339 ending in `Z`.
- Recovery bucket, optional version, and encryption strings must be trimmed nonblank values within their bounds.
  `object_key` additionally rejects a trailing slash so every path segment is nonempty.
- All mutation guards set `updated_at=now()` themselves and reject a caller-supplied time regression.

## 1. Objective and authority

C-03 is the persistence safety gate for the five continuity stores named in the CLEAN execution
plan. This contract authorizes exactly one additive migration:

`database/migrations/0072_narasi_continuity_schema.sql`

It does not authorize runtime writes, repository adapters, provider calls, Railway or Neon access,
deployment, a commit, or a push. C-02, C-06, and C-07 remain blocked after this migration is written;
they need separate acceptance contracts.

The migration must create these five tables and no sixth continuity table:

1. `narasi_continuity_contracts`
2. `narasi_continuity_claims`
3. `narasi_continuity_violations`
4. `narasi_continuity_coverage`
5. `narasi_continuity_recovery_artifacts`

The migration is accepted only when the immutable pack runner passes against a real, disposable
local PostgreSQL cluster. Static SQL inspection or mocked policy tests are not substitutes.

## 2. Global invariants

Every table must:

- have a UUID primary key and non-null `tenant_id`;
- bind `tenant_id` to `tenants(id) ON DELETE CASCADE`;
- enable and force row-level security;
- have one canonical policy with both `USING` and `WITH CHECK` based on
  `NULLIF(current_setting('app.current_tenant_id', true), '')::uuid`;
- deny all rows, without a UUID-cast error, when the tenant setting is absent or reset;
- revoke table privileges from `PUBLIC` and grant only the explicitly required operations to the
  non-`BYPASSRLS`, non-superuser `app_user` role;
- use composite ownership foreign keys so a row cannot cite another tenant's job, project,
  contract, claim, or recovery metadata while merely copying the caller's `tenant_id`;
- use bounded text and JSON values; and
- contain no manuscript body, prompt, provider response, raw model output, generic logs, or public
  recovery URL.

No C-03-owned function may be `SECURITY DEFINER`. No trigger may disable RLS or rewrite the tenant
setting. All names and behavior below are normative; aliases do not satisfy the contract.

## 3. Parent ownership and operational-job deletion

The migration must add C-03-owned, non-partial unique indexes:

- `uq_c03_jobs_tenant_id_id` on `jobs(tenant_id, id)`;
- `uq_c03_projects_tenant_id_id` on `projects(tenant_id, id)`.

Contract rows must bind `(tenant_id, job_id)` to `jobs(tenant_id, id)` using
`ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`. This deliberately prevents the existing
24-hour operational job cleanup from deleting a job that has continuity evidence. Tenant deletion
must still succeed atomically because tenant cascades remove both parent and continuity rows before
the deferred check. C-02 must update job cleanup before enabling writes; C-03 does not change
runtime cleanup code.

If `project_id` is present, `(tenant_id, project_id)` must reference `projects(tenant_id, id)` with
`ON DELETE CASCADE`. Deleting a linked project or its tenant must remove all five stores through the
contract. A null project is allowed for legacy or projectless jobs.

## 4. Contracts store

`narasi_continuity_contracts` must include:

- ownership: `id`, `tenant_id`, nullable `project_id`, non-null `job_id`;
- versioning: positive `contract_version`, nullable `supersedes_contract_id`, `status`;
- amendment metadata: nullable `amendment_reason`, non-null JSON arrays/objects
  `affected_chapter_ids` and `revalidation_plan`;
- immutable contract data: `story_contract`, lowercase SHA-256 `story_contract_hash`,
  `target_language`, `contract_schema_version`, `contract_prompt_version`, `compiler_version`,
  `slicer_version`, `ordered_chapter_ids`, and `chapter_slice_hashes`;
- lifecycle timestamps: `generation_started_at`, `created_at`, and `updated_at`.

Required constraints:

- unique `(tenant_id, id)` and `(tenant_id, job_id, contract_version)`;
- a unique binding key covering `(tenant_id, id, job_id, story_contract_hash)` for child FKs;
- chapter IDs use `^ch_[0-9a-f]{32}$`, are ordered, nonempty, and duplicate-free;
- `chapter_slice_hashes` has exactly the same chapter IDs as `ordered_chapter_ids` and every value is
  lowercase SHA-256;
- bounded contract JSON and version/language/reason strings;
- root contracts have no amendment metadata; amendment rows require a nonblank reason, nonempty
  affected chapter IDs drawn from the ordered chapter set, and a nonempty revalidation plan; and
- a superseding row must reference a contract owned by the same tenant.

Contract payload, ownership, hashes, versions, language, chapter identities, slice hashes, and
amendment metadata are immutable from insertion. The only permitted updates are:

- `validated -> active` while setting `generation_started_at` once;
- `validated -> rejected`;
- `active -> superseded`; and
- `updated_at` changing with an allowed status transition.

All other status transitions, clearing/changing `generation_started_at`, and payload mutation must
fail. Amendments are new rows, never updates to an accepted contract. There must be no
`raw_model_output` column.

## 5. Claims store

`narasi_continuity_claims` must bind `(tenant_id, contract_id, job_id, story_contract_hash)` to the
matching contract and include `chapter_id`, `chapter_content_hash`, `extractor_schema_version`,
`extractor_prompt_version`, nonnegative `extractor_epoch`, `model_route`, bounded `claims`,
`created_at`, and `expires_at`.

The exact idempotency key is:

`(job_id, chapter_id, chapter_content_hash, story_contract_hash, extractor_epoch, model_route)`

Chapter/content/contract hashes must be validated. `claims` must be a bounded JSON array. Expiry must
be after creation and no later than 30 days after creation. Rows are immutable except deletion for
retention. If a violation references an expiring claim, deletion must set only the violation's
nullable `claim_id` to null; it must preserve the violation's non-null tenant ownership and all
other metadata. The application role gets `SELECT`, `INSERT`, and `DELETE`, not `UPDATE`.

## 6. Violations store

`narasi_continuity_violations` must bind to the same tenant/contract/job/hash and optionally to a
same-tenant claim. It includes `predicate_id`, `predicate_set_version`, `violation_type`, severity
`info|low|medium|high|blocker`, bounded evidence, nonnegative `attempt_count`, resolution state
`open|repaired|dismissed|unresolved`, optional `resolved_at`, evidence expiry/purge timestamps, and
created/updated timestamps.

Evidence is null or a JSON array. Every evidence item has exactly `text`, `start`, `end`, and
`source_hash`; text is at most 500 characters, offsets are integers with `0 <= start <= end`, and
the source hash is lowercase SHA-256. Evidence expiry is required while evidence exists, must be
after creation, and may not exceed 30 days. Evidence may only be cleared, never replaced or
expanded. Core identity and predicate fields are immutable. Resolution transitions and monotonic
attempt-count increases are allowed; `resolved_at` must agree with the state.

INSERT is a lifecycle boundary, not a shortcut around the update guard: every new violation is open,
unresolved, at attempt zero, and has no purge timestamp. Attempts may rise only before or in the
same statement as the one terminal transition. After that transition, attempts, resolution state,
and resolution timestamp are fixed; evidence may still be purged by the independent retention
transition.

## 7. Coverage store

`narasi_continuity_coverage` must bind to the same tenant/contract/job/hash and include
`final_candidate_hash`, `predicate_set_version`, `extractor_schema_version`, `extractor_prompt_version`,
`extractor_epoch`, `diff_version`, bounded `coverage`, `coverage_hash`, `created_at`, and `updated_at`.

It is unique on `(job_id, final_candidate_hash, predicate_set_version)`. Coverage is a nonempty JSON
array of exact records `{chapter_id, predicate_id, state}`; chapter IDs must be stable B-08 IDs and
state must be `complete|partial|failed|not_applicable`. Duplicate chapter/predicate pairs fail.
Coverage rows are immutable after insertion. The application role gets only `SELECT` and `INSERT`.

## 8. Recovery metadata store

`narasi_continuity_recovery_artifacts` contains metadata only and binds to the same
tenant/contract/job/hash. It includes `final_candidate_hash`, private-object coordinates
`object_bucket`, `object_key`, optional `object_version`, `encryption_algorithm`, `encryption_key_id`,
`ciphertext_sha256`, positive `ciphertext_size_bytes`, `terminal_at`, `expires_at`, `deleted_at`, and
a bounded `access_audit` JSON array.

It must not contain columns whose names or comments imply manuscript/content/body/text/prompt/output/
response/log storage, nor a URL. Bucket/key/version/key-id strings are bounded. `object_key` is a
relative private key: no scheme, leading slash, control character, backslash, empty path segment, or
`.`/`..` segment. Encryption metadata and ciphertext hash are mandatory. Expiry is after terminal
time and no later than 30 days after terminal time.

Every access-audit item has exactly `event_id`, `at`, `actor_hash`, `action`, and `outcome`; UUID,
timestamp, actor SHA-256, action `read|delete|expire`, and outcome `allowed|denied|succeeded|failed`
are validated. Audit entries are append-only with a strict 100-entry/32-KiB bound. Other recovery
metadata is immutable except `deleted_at` may be set once. The application role gets `SELECT`,
`INSERT`, `UPDATE`, and `DELETE`; RLS and triggers still enforce tenant and mutation rules.

The timestamp validator must reject syntactically out-of-range components before PostgreSQL can
normalize them. Recovery bucket/version/encryption values use boundary-whitespace checks that cover
space, tab, newline, carriage return, form feed, and vertical tab; one-argument `btrim(text)` alone
does not satisfy this rule.

C-03 proves schema capability only. C-07 still owns private-object encryption, authorized read APIs,
audit-event production, expiry deletion of the object and metadata, and orphan sweeping.

## 9. Retention, deletion, and rollback

Contracts and coverage follow project/tenant retention. Claims expire within 30 days. Violation raw
evidence expires within 30 days, while non-evidence metadata may persist. Recovery metadata/object
expiry is within 30 days. A shorter future tenant policy is allowed; this migration must never permit
a longer interval.

The pack-owned `rollback_c03.sql` is test evidence, not a product migration and must never be copied
into `database/migrations`. It must:

1. fail closed if any of the five stores contains a row;
2. leave all rows and objects intact after that refusal;
3. when all five stores are empty, drop only C-03 tables, functions, triggers, and the two C-03 parent
   indexes in dependency-safe order; and
4. leave pre-C-03 tables, data, roles, policies, types, and functions unchanged.

## 10. Migration and acceptance gates

The product migration must be additive, transactional, and idempotent. It must apply twice cleanly
against the full repository migration chain 0001-0071. Only the named 0072 file may change.

The runner must prove all of the following on real PostgreSQL, including every retained V2 rule and
every V3 closure rule:

- pack integrity, source baseline, exact product edit scope, and no symlinks;
- full pre-C-03 migration chain plus two applications of 0072;
- exact five-table inventory, required columns/types/defaults, constraints, indexes, FKs, trigger
  functions, grants, `ENABLE` + `FORCE` RLS, and non-definer security;
- no-tenant zero visibility; tenant A CRUD; tenant B invisibility; cross-tenant insert/update/link
  denial; same-count fake ownership denial; and `PUBLIC` DML denial;
- contract immutability/status transitions/amendment rules;
- claims idempotency and all typed/bounded/retention constraints;
- violation evidence shape/500-character limit/purge-only mutation/resolution consistency;
- coverage shape/state/uniqueness/immutability;
- recovery metadata-only shape/private key/encryption/expiry/append-only audit constraints;
- direct job deletion refusal, linked-project cascade, tenant cascade, no orphan rows;
- rollback refusal with data, then clean rollback with zero C-03 residue and intact parent schema;
- inherited C-01 V2 acceptance; and
- byte-identical before/after Git status and clean `git diff --check`.

There is no quick, partial, catalog-only, mock, skip, xfail, or manual-equivalent mode. Any mismatch is
a failure. Claude submits one final report without self-approval; Codex independently reruns the exact
pack before updating the ledger.
