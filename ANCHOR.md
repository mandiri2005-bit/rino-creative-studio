# C-03 schema/RLS pack — durability anchor

**This branch is an ORPHAN and must never be merged.** It exists so that an accepted-but-
undeployed artefact stops living only in an untracked directory. It carries no history from
`main` or `feat/subscription-global`, and its `database/migrations/` files are **not** eligible
for `database/migrate.js` on any deploy branch — that is the point of keeping them here.

Created under `GO L2B-METER PREREQUISITES D-METER-20/21`, 2026-08-01. Nothing here is authorised
to be applied, pushed, deployed, or renumbered.

## What was anchored, and why more than `0072`

The token names `0072`. Anchoring it alone would have preserved an **unrunnable** pack: the
acceptance runner verifies `required_files`, which includes C-03's own `0070` and `0071`. All
three are therefore anchored together.

| File | sha256 |
|---|---|
| `database/migrations/0070_narasi_lifecycle.sql` | `cb495e187bc4178f42b84614e7de1abcd0b71dca96dacc1adc42e0ed1751c9fa` |
| `database/migrations/0071_narasi_derived_input.sql` | `93df3050a57e51c67b9ea3e352ebb7291b6be1a4cd4dc8db0494deb1b5ab47b2` |
| `database/migrations/0072_narasi_continuity_schema.sql` | `2d4ef11934b04e263a43970dc5d38aa4fe73e0312293bb8357c61145c435329a` |

Plus the V4 acceptance pack under `acceptance-v4/`, self-verified by its own `SHA256SUMS`.

## Provenance, stated exactly — including what is NOT proven

Origin: worktree `/tmp/wt-narasi-perf` (now gone), backend HEAD
`76b287c18b58b84c792432ff5fb326e5aa6f2008`, branch `perf/narasi-revise-fast`. **The migrations
were never committed to any ref**; a repo scan legitimately reported them absent, which is why
`0072` was twice reported free. Recovered from
`Documents/wt-narasi-perf-recovered/database/migrations/`.

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

Anyone reviving C-03 must re-run `acceptance-v4/run_c03_acceptance.py` against these files rather
than trusting the ledger's "ACHIEVED". The ledger's claim of 153 assertions is about a file whose
identity the pack cannot confirm.

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

**Durability bound:** this is a **local** ref. Git objects survive directory deletion, which an
untracked recovery folder does not — but a single laptop is not an off-site anchor. Pushing this
branch is the real fix and was **not** authorised by the token that created it.
