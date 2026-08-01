# Migrations

Applied in filename order by `database/migrate.js` (tracked in the `migrations`
table). Each file is wrapped `BEGIN; … COMMIT;` (the runner strips the outer pair).

## Numbering rule (READ BEFORE ADDING A MIGRATION)

**Allocate against the DEPLOY branch for the product you are changing, then cross-check
the production `migrations` table, then check `RESERVED.md`.** All three, in that order.

For Wimba the deploy branch is **`feat/subscription-global`** — the branch Railway
(`positive-radiance`) actually deploys. For cerita it is split; see
`~/docs/IMPORTANT-cerita-ai-backend-deploy-runbook.md`.

> ### ⚠ The old rule said `origin/main`, and following it now causes an immediate collision
>
> That rule predates the branch drift it was written about. Measured 2026-08-01:
>
> | Source | Highest migration |
> |---|---|
> | `origin/main` — what the old rule pointed at | `0035` |
> | `feat/subscription-global` — the actual deploy branch | `0071` |
> | Wimba production `migrations` table | **66 applied, through `0071`** |
>
> The old rule yields `0036`, a slot forty migrations in the past that sorts ahead of
> everything already applied. The same trap is live for cerita, whose production database
> is ahead of its repo baseline.

Why the cross-checks are not redundant:

- **The deploy branch** is the only tree whose contents actually reach production.
- **The production `migrations` table** catches numbers applied from a branch you are not
  looking at. It records by *filename*, so two different files sharing a number are two
  different rows — the collision surfaces as disorder, not as an error.
- **`RESERVED.md`** catches numbers claimed by accepted work that is not on the deploy
  branch and not on the remote — work committed to a local-only ref, to a *separate clone*,
  or to no ref at all. Neither of the first two checks can see those, and this has already
  caused `0072` to be reported free **three** times, by three different mechanisms.

> **Amended 2026-08-01.** This list previously said `RESERVED.md` catches work "never
> committed to any ref". That framing hid the actual failure: the C-03 migrations *were*
> committed, to a branch in a separate clone whose `origin` was a local filesystem path. A
> `git ls-tree` over every branch **here** was complete, correct, and blind. A scan's scope
> is part of its result — state it, or the next reader inherits your assumption as a fact.

Historical precedent this rule exists for: `0031` was claimed independently by **two**
branches (`feat/accounting-foundation` → `0031_accounting_foundation.sql` and an early
Dodo draft → `0031_dodo_payments.sql`). The Dodo work was renumbered to
`0032_payment_events.sql` to resolve it.

If two unmerged branches still end up with the same number, the one merging
**second** must renumber to the next free slot before merging.

> Future option (lower friction): switch to timestamped names
> (`YYYYMMDDHHMMSS_name.sql`) so parallel branches never collide. Not adopted yet
> to keep the existing zero-padded sequence readable.
