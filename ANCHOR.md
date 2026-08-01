# C-03 schema/RLS pack — durability anchor

> ### ⚠ AMENDED 2026-08-01 — this ref is a BYTE ARCHIVE, not a runnable acceptance pack
>
> Amended under `L2B-METER-PREREQUISITES-RECORD-002`, then corrected again under
> `-RECORD-003` (Amendment 2), which narrowed three overstatements in the first amendment —
> see *What is NOT archived*. Two claims written below were already false when this ref was
> created, and are corrected in place:
>
> 1. the migrations **are** committed to a ref, and were when this was written — see
>    **Provenance**;
> 2. the V4 pack **cannot be re-run**, and no amount of additional anchoring could have made
>    it runnable — see **What is NOT archived**.
>
> What this ref does guarantee: the bytes it holds are the bytes that were found. It does not
> certify them as the bytes that were accepted, and it cannot reproduce the acceptance run.
>
> Pre-amendment state preserved at `ccb26ad95537793157e68679e282bd28eb6ffc26`, which is this
> commit's parent and remains the ref `codex/c03-schema-frozen`.

**This branch is an ORPHAN and must never be merged.** It exists so that an accepted-but-
undeployed artefact stops living only in an untracked directory. It carries no history from
`main` or `feat/subscription-global`, and its `database/migrations/` files are **not** eligible
for `database/migrate.js` on any deploy branch — that is the point of keeping them here.

Created under `GO L2B-METER PREREQUISITES D-METER-20/21`, 2026-08-01. Nothing here is authorised
to be applied, pushed, deployed, or renumbered.

## What was anchored, and why more than `0072`

The token names `0072`. Anchoring it alone would have preserved even less: the acceptance runner
verifies `required_files`, which includes C-03's own `0070` and `0071`. All three are therefore
anchored together.

**This did not make the pack runnable, and could not have.** See *What is NOT archived*.

| File | sha256 |
|---|---|
| `database/migrations/0070_narasi_lifecycle.sql` | `cb495e187bc4178f42b84614e7de1abcd0b71dca96dacc1adc42e0ed1751c9fa` |
| `database/migrations/0071_narasi_derived_input.sql` | `93df3050a57e51c67b9ea3e352ebb7291b6be1a4cd4dc8db0494deb1b5ab47b2` |
| `database/migrations/0072_narasi_continuity_schema.sql` | `2d4ef11934b04e263a43970dc5d38aa4fe73e0312293bb8357c61145c435329a` |

Plus the V4 acceptance pack under `acceptance-v4/`, self-verified by its own `SHA256SUMS`.

## Provenance, stated exactly — including what is NOT proven

Origin: worktree `/tmp/wt-narasi-perf` (now gone), backend HEAD
`76b287c18b58b84c792432ff5fb326e5aa6f2008`, branch `perf/narasi-revise-fast`. Recovered from
`Documents/wt-narasi-perf-recovered/database/migrations/`.

### 🔴 Corrected 2026-08-01 — "never committed to any ref" was false

This section previously read: *"**The migrations were never committed to any ref**; a repo scan
legitimately reported them absent, which is why `0072` was twice reported free."* True at C-03's
acceptance on 2026-07-22. **Already false when written on 2026-08-01.**

All three files are tracked in commit `dd4a823c40147605507d502ed6c67ef924a7bceb` — *"Recovery
baseline: B-04b Phase 0 backend state on 76b287c"* — whose parent is `76b287c18b58b84c…`, the
C-03 baseline HEAD itself. Byte-identical to this ref's copies, verified:

| File | This ref | `dd4a823c` |
|---|---|---|
| `0070_narasi_lifecycle.sql` | `cb495e18…` | `cb495e18…` ✔ |
| `0071_narasi_derived_input.sql` | `93df3050…` | `93df3050…` ✔ |
| `0072_narasi_continuity_schema.sql` | `2d4ef119…` | `2d4ef119…` ✔ |

**Why every scan missed it:** `dd4a823c` lived in a *separate clone* at
`~/Documents/wt-narasi-perf-recovered`, with its own object store and an `origin` pointing at a
local filesystem path. `git ls-tree` over every branch of the main repository was correct and
complete — and blind, because the commit was never in that repository. It is now anchored there
as `codex/b04b-phase0-recovered` (local ref, not pushed).

The old sentence was not careless: three independent checks agreed. All three were scoped to one
repository, and none said so. **"Absent from every ref" is only ever a claim about the refs you
thought to ask.** That is the same failure that reported `0072` free twice, arriving a third time
by a different route.

**Binding chain, verified before anchoring:**

```
acceptance-v4/SHA256SUMS  →  SOURCE-BASELINE.json                     OK
SOURCE-BASELINE.json      →  0070_narasi_lifecycle.sql                MATCH
SOURCE-BASELINE.json      →  0071_narasi_derived_input.sql            MATCH
                             0072_narasi_continuity_schema.sql        ← BOUND BY NOTHING
```

🔴 **The accepted candidate has no positive binding.** `SOURCE-BASELINE.json` records
`rejected_v1/v2/v3_candidate_sha256` and **no accepted-V4 hash**. The pack records what was
refused and never what was accepted. All that can be said for `2d4ef119…` is:

- it matches **none** of the three recorded rejected hashes (`6df1bee7…`, `591246a3…`,
  `068291d5…`), which is consistent with it being V4;
- that is **elimination, not identification**. This anchor preserves the bytes that were found;
  it does not certify them as the bytes that were accepted.

Anyone reviving C-03 must therefore establish acceptance **afresh** rather than trusting the
ledger's "ACHIEVED" — its claim of 153 assertions is about a file whose identity this pack cannot
confirm. That cannot be done by re-running `acceptance-v4/run_c03_acceptance.py`; see the next
section for why.

## 🔴 What is NOT archived — the pack cannot be re-run

`acceptance-v4/` is on this ref in full and self-verifies against its own `SHA256SUMS`. That
makes it **readable**, not **runnable**. `run_c03_acceptance.py` gates on worktree state *before*
executing a single assertion, and one of those gates cannot be satisfied from anything anchored:

| Preflight gate (`run_c03_acceptance.py`, ll. 135–153) | Demanded | Available |
|---|---|---|
| `backend_head` | `76b287c18b58b84c…` | commit reachable; the **worktree state** it names is not |
| `backend_branch` | `perf/narasi-revise-fast` | — |
| `required_files` (11 entries) | 11 hashes | **11 of 11**, all verified in `codex/b04b-phase0-recovered` (2 of 11 on this ref alone) |
| `status_without_candidate_sha256` | `180c5865502615f7…` | binds porcelain status **lines** — codes and paths, not file bytes — and `preexisting_status_paths` records that path set: **fragile, not lost** |
| `tracked_diff_sha256` | `24720f091ce58dd0…` | 🔴 **the one hard blocker** — exact bytes of `git diff --binary`, held nowhere |

**Amended 2026-08-01 (Amendment 2).** This table previously read "2 of 11" and claimed the status
gate needed untracked *bytes*. Both were wrong. The `required_files` figure was scoped to this
orphan and was stale the moment `dd4a823c` was fetched — in the very record that documented the
fetch. The status gate hashes `git status --porcelain=v1 -z` output (ll. 121–130), so it binds
paths and status codes only. Neither is the blocker. **`tracked_diff_sha256` is.**

Two further dependencies are outside the ref entirely:

- **`C01_RUNNER`** is hard-coded to the absolute path
  `/Users/rino/docs/IMPORTANT-wimba-narasi-c01-story-contract-closure-acceptance-v2/run_c01_closure_acceptance.py`.
  Verified 2026-08-01: **that file still exists** (6,379 bytes) — and is on no ref in this
  repository and in no pack. It survives by accident of one directory on one laptop, and nothing
  before this amendment recorded that the pack depends on it.
- **`preexisting_status_paths` includes `python/.pytest_cache/`.** Because the status gate binds
  paths and status codes rather than contents, this does **not** make the baseline impossible to
  reproduce. It does couple acceptance to a cache directory that must exist, untracked, with a
  matching porcelain entry — environment-fragile, and nothing anyone would think to preserve on
  purpose.

**Measured 2026-08-01** against the surviving recovery directory, the nearest thing to the
baseline that still exists:

| | Baseline demands | `~/Documents/wt-narasi-perf-recovered` |
|---|---|---|
| HEAD | `76b287c1…` | `dd4a823c…` |
| branch | `perf/narasi-revise-fast` | `recovery/b04b-phase0-76b287c` |
| tracked diff | `24720f09…` | `9b7bcbbe…` |
| status | `180c5865…` | `b258511e…` |

Four gates, four mismatches. And `dd4a823c` is not a damaged copy of the baseline — it is a
**later, larger** state: it commits 50 files and +26,987 lines of A-04 / B-01 / B-03 / B-04a
suites that postdate C-03 entirely.

The decisive evidence is in that commit's own message, which says what was never rebuilt:

> Missing from this baseline: `backend/server.js`, `orchestrator/router.py`,
> `orchestrator/static.py` (3 of 8 modified files, not reconstructed …)

Those three are C-03's own modified tracked files. **3 of the 8 inputs to `24720f09…` are not
present in any artefact anchored here.**

Stated precisely, because the distinction matters: that commit message records the three files as
**not reconstructed in that round**. It does not prove their bytes exist nowhere — session
captures, other unarchived directories, or a later reconstruction attempt could still produce
them. What can be said today is that nothing anchored contains them, so the diff cannot be
recomputed → the preflight cannot pass → the suite cannot run. And a suite that cannot run could
never have been made runnable by anchoring more migrations. **Anchoring was not the missing
piece.**

**What a C-03 revival must therefore do:** build a *new* baseline and run a *new* acceptance
round. From what is anchored today the historical run cannot be reproduced. And even if the three
files were later recovered, the ledger's "153 assertions" would still not be trustworthy — it is
a claim about a candidate this pack cannot identify (see *Provenance*). Treat that number as
unverifiable, not as something a successful re-run would restore.

## 🔴 The numbering collision is triple, not single

C-03 was developed on a branch that allocated its own `0070`–`0072` while
`feat/subscription-global` independently allocated `0070`–`0071` for unrelated work. Both of the
latter are **already applied in production**.

| Number | C-03 (this branch) | Deploy branch + production |
|---|---|---|
| `0070` | `0070_narasi_lifecycle.sql` | `0070_gl_privilege_hardening.sql` — **applied** |
| `0071` | `0071_narasi_derived_input.sql` | `0071_deferred_revenue_no_silent_fallback.sql` — **applied** |
| `0072` | `0072_narasi_continuity_schema.sql` | free, reserved for C-03 |

So **`0072` is the only number C-03 can still keep.** Its `0070` and `0071` must be renumbered on
revival, and the pack's `SOURCE-BASELINE.json` `required_files` — which names them by path — must
be regenerated to match, or the acceptance runner will verify files that no longer exist under
those names. This is not a numbering tidiness issue: it is why the pack cannot simply be replayed.

`database/migrations/RESERVED.md` on the deploy branch records the reservation so a future
allocator sees it without needing to know this branch exists.

## Rehydrating

```
git worktree add --detach <path> codex/c03-schema-frozen
```

Then verify before doing anything else:

```
cd <path>/acceptance-v4 && shasum -c SHA256SUMS
shasum -a 256 ../database/migrations/007*.sql      # compare against the table above
```

**Both checks are byte-integrity only.** A green `shasum -c` means the archive is intact. It does
not mean the pack is runnable — it is not — and it does not mean `2d4ef119…` is the accepted V4.

**Sibling ref.** `codex/b04b-phase0-recovered` (`dd4a823c…`) holds the same three migrations,
byte-identical, inside a full backend tree. It is the *only* copy of the B-04b Phase 0 recovery
baseline and is likewise local-only. Do not merge it either.

**Durability bound:** this is a **local** ref, and so is its sibling. Git objects survive
directory deletion, which an untracked recovery folder does not — but a single laptop is not an
off-site anchor. Pushing is the real fix and remains **unauthorised** by the tokens that created
these refs.
