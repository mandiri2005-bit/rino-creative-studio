# C-03 V1/V2 Independent Audit Findings

## V1 decision

V1 candidate `6df1bee7ee851ba59493e7c330515632fc07195dc0fedf857a7ad7afdb7ccd9f`
is rejected despite the V1 runner printing PASS. Codex reran that exact runner successfully, then
executed source-derived adversarial probes on real local PostgreSQL 18.4. The database accepted all
six forbidden operations below.

## Reproduced blockers

1. `recovery_audit_reorder_accepted`: `[A,B] -> [B,A]` passed because JSONB containment is not
   prefix equality.
2. `recovery_audit_prepend_accepted`: `[A,B] -> [C,A,B]` passed; an event can be backfilled ahead of
   prior history.
3. `evidence_purge_without_audit_timestamp_accepted`: evidence and expiry could be cleared while
   `evidence_purged_at` remained null.
4. `self_superseding_contract_accepted`: a new contract could use its own ID as
   `supersedes_contract_id`, so it did not reference an older row.
5. `cross_job_supersession_accepted`: a contract for one job could supersede a contract belonging to
   another job in the same tenant.
6. `unbounded_violation_evidence_accepted`: 1,000 evidence objects of 500 characters each were
   accepted because only per-item text length was constrained.

## Source-audit gaps targeted by V2

V2 also makes the implied word `bounded` executable everywhere and closes adjacent lifecycle gaps:

- coverage, ordered chapter IDs, slice hashes, and violation evidence receive exact count/byte caps;
- amendment lineage is same-job, non-self, consecutive, target-language-stable, and chapter-set-
  stable;
- every inserted contract begins `validated` with no generation timestamp;
- evidence expiry/purge metadata cannot be independently rewritten or extended;
- violation resolution has a one-way terminal state machine and terminal timestamp;
- private object bucket, encryption fields, and key path must be meaningful; trailing-slash empty
  segments fail; and
- access-audit timestamps are finite canonical UTC, event IDs are unique, and history is exact-prefix
  append-only.

## V2 decision

V2 candidate `591246a386fc6788750229452e2124701ac961cc30e378c2d0f960d94a59604e`
is also rejected despite the immutable V2 runner printing `134` assertions and PASS. Codex
independently reran that exact runner successfully, then used the same full migration chain and a
fresh Unix-socket-only PostgreSQL 18.4 cluster for source-derived probes. Six generalized defects
remained:

1. `terminal_violation_insert_accepted`: `resolution_state='repaired'` plus `resolved_at=now()` was
   accepted on INSERT, although every violation must start open.
2. `terminal_attempt_increment_accepted`: after an open violation became terminal, its
   `attempt_count` could still increase; terminal resolution was not actually terminal.
3. `evidence_and_purge_timestamp_coexist_accepted`: a new row could contain both live evidence and
   a non-null `evidence_purged_at`, a state impossible under the atomic purge lifecycle.
4. `referenced_claim_retention_delete_failed`: the composite claim FK used unqualified
   `ON DELETE SET NULL`, so PostgreSQL attempted to null both `claim_id` and non-null `tenant_id`;
   direct expiry deletion of a referenced claim failed instead of preserving violation metadata
   with only `claim_id=NULL`.
5. `noncanonical_24_hour_audit_timestamp_accepted`: `2026-07-22T24:00:00Z` matched the broad regex
   and PostgreSQL normalized it, even though canonical RFC3339 hours are `00..23`.
6. `non_space_whitespace_metadata_accepted`: tab/newline-only bucket, algorithm, and key-ID values
   passed because one-argument `btrim()` trims spaces, not the complete boundary-whitespace class.

These are distinct root causes, not alternate examples of the V1 defects: INSERT lifecycle was not
guarded, terminal immutability omitted attempts, the purge agreement was only update-time, a
composite SET NULL action targeted the ownership column, timestamp syntax admitted parser
normalization, and text trimming used a narrower character class than the contract's word
"trimmed". V3 tests the generalized invariant and includes valid controls; literal special-casing
does not pass.

V3 supersedes V1 and V2 entirely. This file authorizes no product or live action by itself.

## V3 decision

V3 candidate `068291d5f9e5f60a64f1ba9a45fb26b52748d402fc07b0a7ec0f678683de056b`
is rejected despite the immutable V3 runner printing `151` assertions and PASS. Codex reproduced
that PASS and independently confirmed all six V3 root-cause behaviors. Full source review then found
one retained V2 rule that the V3 oracle had not asserted: every allowed mutation guard must own
`updated_at=now()` rather than trust a caller-supplied regression.

Two direct PostgreSQL probes supplied `updated_at='2000-01-01T00:00:00Z'` on INSERT. Both the
contract INSERT guard and the newly-expanded violation INSERT guard accepted the row and persisted
the forged year 2000 unchanged. Their UPDATE branches already overwrite caller values correctly;
only the INSERT branches return before assigning `NEW.updated_at`.

V4 adds exactly two generalized closure assertions, one for each allowed INSERT guard. The required
implementation is source-level guard ownership (`NEW.updated_at := now()` before return), not a
literal-date blacklist or test-specific CHECK. V4 retains all 151 V3 assertions unchanged and
supersedes every earlier C-03 pack.
