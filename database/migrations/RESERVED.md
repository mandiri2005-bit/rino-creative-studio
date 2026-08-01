# Reserved migration numbers

**Read this before allocating a migration number.** Some numbers are **claimed by work that is
not in this directory and not on any branch this repository's deploy branch can see**, so neither
`ls` here, nor a `git` scan, nor the production `migrations` table will show them. That is not
hypothetical: it has already caused `0072` to be reported free twice.

| Number | Claimed by | State | Where it actually lives |
|---|---|---|---|
| `0072` | Narasi **C-03** — schema/RLS for the five continuity stores | accepted 2026-07-22 after a V1→V4 rejection chain; **frozen, offline, never applied** | local refs `codex/c03-schema-frozen` (orphan, byte archive) and `codex/b04b-phase0-recovered` (`dd4a823c…`, full tree) — **neither is on the remote** |

## Why `0072` is not simply free

C-03 was developed in a worktree that was not committed to any ref *of this repository*. The
worktree was later deleted, the files survived only in an untracked recovery directory, and as of
the 2026-07-22 acceptance every mechanical check returned "absent":

- `ls database/migrations/` — nothing;
- `git ls-tree` over every branch — nothing;
- the production `migrations` table — stops at `0071`.

All three were correct and all three were misleading. It is now anchored on the orphan ref
`codex/c03-schema-frozen` (see its `ANCHOR.md`), which is why this file exists: **the anchor makes
it durable, this file makes it visible.**

> ### ⚠ Amended 2026-08-01 — the scan was scoped, and the scope was never stated
>
> This file originally said the work was *"not in any branch of this repository"* and that
> `git ls-tree` over every branch found nothing. **Both were already false when written.** The
> three C-03 migrations were tracked all along in commit
> `dd4a823c40147605507d502ed6c67ef924a7bceb`, whose parent is C-03's own baseline HEAD — but that
> commit lived in a **separate clone** (`~/Documents/wt-narasi-perf-recovered`, its `origin` a
> local filesystem path), so it was invisible to every scan of *this* repository. It has since
> been anchored here as `codex/b04b-phase0-recovered`.
>
> The scans were complete. Their **scope** was the unstated assumption. When you conclude a
> number is free, you have shown it is free *in the places you looked* — which is exactly the
> reasoning that produced this file, arriving a third time by a new route.

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

**Renumbering is not the hard part.** The V4 pack is a byte archive, not a runnable suite: its
preflight demands a worktree HEAD, branch, dirty-diff hash and status hash that no surviving
artefact reproduces, and 3 of the 8 modified files feeding that diff were never reconstructed —
stated as much in `dd4a823c`'s own commit message. A C-03 revival is therefore a **new**
acceptance round, not a replay. The ledger's "153 assertions" is not recoverable; do not plan
around restoring it. Full detail in `ANCHOR.md` on `codex/c03-schema-frozen` and in
`L2B-METER-PREREQUISITES-RECORD-002`.

## How to allocate a new number

1. Take the next free number **after the highest in this directory on the deploy branch**
   (`feat/subscription-global`) — see `README.md`, and note that the rule there was corrected in
   the same change that added this file.
2. Cross-check against the production `migrations` table.
3. **Check this file**, because steps 1 and 2 cannot see anything listed here.
4. If your work is accepted but not yet deployed, add it here **and** anchor it on a ref **in
   this repository**. An artefact that lives only in a working directory is invisible to everyone
   and one `rm -rf` from gone — and one that lives only in a *separate clone* is invisible to
   every scan run here, which is how `0072` was missed a third time.

When you report a number free, say where you looked. "Free" is a claim about a scope, and the
scope is the part that has been wrong every time.
