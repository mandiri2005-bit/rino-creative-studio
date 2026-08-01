# Deploying a migration to production

Migrations do **not** run automatically. There is no release phase in `Procfile`,
`package.json` or `railway.json`, so pushing to `feat/subscription-global` deploys the
**code** but leaves the schema untouched. Applying a migration is a deliberate manual step.

## The recipe

Run from the repo root (so `railway` resolves the linked project):

```bash
NODE_PATH="$PWD/backend/node_modules" \
NODE_ENV=production \
MIGRATE_EXPECT_HOST=ep-late-boat \
DATABASE_URL="$(railway variables --service db-backup --kv | grep -m1 '^BACKUP_DATABASE_URL=' | cut -d= -f2-)" \
node database/migrate.js
```

`migrate.js` prints the host, database and role it actually reached before applying
anything. Read those three lines. If they are not what you meant, stop.

## Why each part is there

**`DATABASE_URL=` supplied explicitly.** The repo `.env` used to carry a `DATABASE_URL`
pointing at `ep-proud-truth-aqg5hqu4` — alive, but empty and stuck at migration `0028`,
with none of the accounting tables. `NODE_ENV=production node database/migrate.js` would
therefore connect happily to the wrong database and report success. Those keys have since
been renamed in `.env` so the script fails loudly instead, but always pass the URL
explicitly rather than trusting ambient config. (`dotenv` never overrides a variable that
is already set, so an explicit value always wins.)

**`MIGRATE_EXPECT_HOST=`** aborts if the endpoint you reached does not contain that
substring. Opt-in and substring-based so no infrastructure hostname has to be committed.
Without it the script only warns.

**`BACKUP_DATABASE_URL`, not `DATABASE_POOL_URL`.** Migrations run DDL, `GRANT` and
`REVOKE`, which the `app_user` role on the pooled endpoint cannot do. `BACKUP_DATABASE_URL`
is the owner role on the direct (non-pooled) endpoint.

**`NODE_PATH=`** because `pg` lives in `backend/node_modules`, not at the repo root.
`dotenv` is not installed at all; `migrate.js` treats it as optional.

## Safety properties

`migrate.js` applies only files absent from the `migrations` table, each inside its own
transaction, and aborts on the first failure. Re-running it is safe: it will report
"All migrations are up to date" and do nothing.

## Afterwards

Verify against the database rather than trusting the success message:

```bash
psql "<owner url>" -X -f database/checks/app_user_privilege_audit.sql
```

Section A must be empty. Anything listed there is a table the application role can write
with no row-level protection — see the accounting audit §17.2 / §18.4.

## Related

- Deploy branch and clash rules: every Wimba push and production deploy goes to
  `feat/subscription-global`.
- `~/docs/database.txt` and `~/docs/database_owner.txt` point at a **deleted** Neon
  endpoint and are marked dead. Do not use them.
