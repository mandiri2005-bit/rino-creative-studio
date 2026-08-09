# Reserved migration numbers

**Read this before allocating a migration number.** Some numbers are **claimed by work that is
not in this directory and not on any branch this repository's deploy branch can see**, so neither
`ls` here, nor a `git` scan, nor the production `migrations` table will show them. That is not
hypothetical: it has already caused `0072` to be reported free twice.

| Number | Claimed by | State | Where it actually lives |
|---|---|---|---|
| `0072` | Narasi **C-03** — schema/RLS for the five continuity stores | accepted 2026-07-22 after a V1→V4 rejection chain; **frozen, offline, never applied** | local refs `codex/c03-schema-frozen` (orphan, byte archive) and `codex/b04b-phase0-recovered` (`dd4a823c…`, full tree) — **neither is on the remote** |
| `0075`–`0083` | **L2C `PLAN-044`** — the `S1`…`S9` slice sequence (`0075` = `S1` identity columns incl. `llm_call_action`; `0076` = `S6` settlement; `0078` = `S9` `credit_ledger`; the rest allocated within the same plan) | **allocated on paper only — `PLAN-044` + `MATRIX-044` are still `DRAFT FOR RATIFICATION`, so none of it is authorised to be implemented**; no file written, nothing applied | `WIMBA_CONT_PROJECT/L2C-BILLING-INTEGRITY-PLAN-044.md` — a **document outside this repository**, so no `git` scan here will ever show it |
| `0084` | **L2C tranche 2** — `UNIQUE` constraint enforcing one credited payment anchor per `(provider, provider_payment_id)` | reserved 2026-08-07 by owner decision; **WRITTEN** as `0084_credited_anchor_uniqueness.sql` and 🔴 **APPLIED TO PRODUCTION** — production high-water is **68 / `0084`**. ⛔ *Corrected 2026-08-09: this row previously read "NOT applied to production (production high-water is still 67 / `0074`)", which was true when written and false once tranche 2 deployed. Verified by a read-only census of the production `migrations` table: 68 applied, high-water `0084_credited_anchor_uniqueness.sql`.* Deliberately skips the `PLAN-044` block above rather than taking the apparent next number | branch `fix/l2c-tranche2-anchor-uniqueness` **in this repository** (anchored per step 4 below) |
| `0085` | **L2C `CR-29` item 4** — ToS assent evidence, the shared serialization boundary, and the shadow-capable top-up checkout gate | reserved 2026-08-09 by owner directive `OWNER-DIRECTIVE-L2C-IMPLEMENT-NOW-20260809`; **WRITTEN** as `0085_tos_assent_and_topup_gate.sql`, applied + reversal-verified against a disposable cluster, **NOT applied to production**. Allocated only after all three checks agreed it was unused: deploy-lineage scan at `04f1c1c7`, this file, and a read-only production census ending in `ROLLBACK` (`0085` → 0 rows). **Ships the gate INACTIVE** — the only activation row it writes is `shadow_started`, and shadow mode never refuses a checkout | branch `l2c/cr29-item4-assent-gate` **in this repository** |

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

**Renumbering is not the hard part.** The V4 pack is a byte archive, not a runnable suite, and it
has exactly **one** hard blocker: `tracked_diff_sha256` (`24720f09…`), the exact bytes of the
pre-existing dirty diff. Those bytes are in no anchored artefact, because 3 of the 8 modified
files feeding them were not reconstructed — stated in `dd4a823c`'s own commit message. The other
preflight gates are either satisfiable (`required_files` is **11 of 11** in
`codex/b04b-phase0-recovered`) or merely environment-fragile (the status gate binds porcelain
lines, not file bytes).

A C-03 revival is therefore a **new** acceptance round, not a replay. Treat the ledger's "153
assertions" as **unverifiable** — not as something a successful re-run would restore, since it is
a claim about a candidate the pack cannot identify. Full detail in `ANCHOR.md` on
`codex/c03-schema-frozen` and in `L2B-METER-PREREQUISITES-RECORD-002` / `-003`.

## Why L2C tranche 2 is `0084` and not `0075`

Every mechanical check says `0075` is free. All of them are correct, and all of them are
misleading in the same way `0072` was — the block `0075`–`0083` is claimed by `PLAN-044`, a
document that lives in `WIMBA_CONT_PROJECT/`, **outside this repository**, so it is invisible to
`ls`, to `git`, and to the production `migrations` table alike. Taking "the next number after the
highest one on the deploy branch" lands squarely on `S1`'s `0075`.

`PLAN-044`'s block is held rather than reclaimed even though **none of it is ratified**, precisely
because a draft that is later ratified must not find its numbers taken. Tranche 2 therefore skips
the whole block and starts after it.

**Where this was looked for, on 2026-08-07** — stating the scope, per the rule at the end of this
file:

- deploy branch `feat/subscription-global` at `104d238c`: 67 migrations, highest
  `0074_platform_qc_metering.sql`;
- production `migrations` table, read-only transaction: 67 rows, highest
  `0074_platform_qc_metering.sql`;
- **every** remote head under `refs/remotes/origin/` (17 branches, enumerated — not sampled): no
  file matching `00(7[5-9]|8[0-4])` on any of them; the highest anywhere is `0074`, except
  `c03-schema-frozen` / `b04b-phase0-recovered` which top out at their own `0072`;
- this file: the only *number* reservation before today was `0072` (the `0070`/`0071` table below
  records a past collision, not a reservation);
- `PLAN-044`: allocates `0075`–`0083`.

No migration and no reservation was found for `0084` in any of those places. That is a claim about
those scopes, and they are listed so the next person can widen them rather than repeat them.

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
