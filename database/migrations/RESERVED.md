# Reserved migration numbers

**Read this before allocating a migration number.** Some numbers are **claimed by work that is
not in this directory and not in any branch of this repository**, so neither `ls` here, nor a
`git` scan, nor the production `migrations` table will show them. That is not hypothetical: it
has already caused `0072` to be reported free twice.

| Number | Claimed by | State | Where it actually lives |
|---|---|---|---|
| `0072` | Narasi **C-03** — schema/RLS for the five continuity stores | accepted 2026-07-22 after a V1→V4 rejection chain; **frozen, offline, never applied** | local orphan ref `codex/c03-schema-frozen` |

## Why `0072` is not simply free

C-03 was developed in a worktree that was never committed to any ref. The worktree was later
deleted, the files survived only in an untracked recovery directory, and every mechanical check
therefore returned "absent":

- `ls database/migrations/` — nothing;
- `git ls-tree` over every branch — nothing;
- the production `migrations` table — stops at `0071`.

All three were correct and all three were misleading. It is now anchored on the orphan ref
`codex/c03-schema-frozen` (see its `ANCHOR.md`), which is why this file exists: **the anchor makes
it durable, this file makes it visible.**

## ⚠ C-03 also carries `0070` and `0071`, and those numbers are already used here

C-03's branch allocated its own `0070`–`0072` while this branch independently allocated
`0070`–`0071` for unrelated work. Both of ours are **applied in production**.

| Number | C-03's file | This branch's file |
|---|---|---|
| `0070` | `0070_narasi_lifecycle.sql` | `0070_gl_privilege_hardening.sql` — applied |
| `0071` | `0071_narasi_derived_input.sql` | `0071_deferred_revenue_no_silent_fallback.sql` — applied |
| `0072` | `0072_narasi_continuity_schema.sql` | *(reserved — do not take)* |

So `0072` is the only number C-03 can keep. Its `0070` and `0071` **must be renumbered on
revival**, and the acceptance pack's `SOURCE-BASELINE.json` names them by path, so it has to be
regenerated at the same time or the runner will verify files that no longer exist.

## How to allocate a new number

1. Take the next free number **after the highest in this directory on the deploy branch**
   (`feat/subscription-global`) — see `README.md`, and note that the rule there was corrected in
   the same change that added this file.
2. Cross-check against the production `migrations` table.
3. **Check this file**, because steps 1 and 2 cannot see anything listed here.
4. If your work is accepted but not yet deployed, add it here **and** anchor it on a ref. An
   artefact that lives only in a working directory is invisible to everyone and one `rm -rf` from
   gone.
