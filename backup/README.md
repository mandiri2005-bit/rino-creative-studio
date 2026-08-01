# Off-site database backup

A Railway **cron** service. It dumps the production database, verifies the dump actually
carries rows, uploads it to object storage, verifies the uploaded size, then prunes old
copies. It runs independently of any laptop.

## Why it verifies instead of just dumping

When this database was lost on 2026-07-25, every dump on hand was schema-only — `CREATE
TABLE` statements and zero `COPY` blocks. They looked like backups. This job refuses to
upload a dump with no `TABLE DATA`, with fewer rows than `BACKUP_MIN_ROWS`, or with fewer
tables than the live database, and it never prunes before a verified upload.

## Required variables

| variable | meaning |
|---|---|
| `BACKUP_DATABASE_URL` | connection string used for `pg_dump`. Use the **owner** role — `app_user` is `NOBYPASSRLS`, so dumping as `app_user` would silently produce a row-filtered backup. |
| `BACKUP_REMOTE` | rclone destination, e.g. `r2:wimba-backups` |
| `RCLONE_CONFIG_R2_TYPE` | `s3` |
| `RCLONE_CONFIG_R2_PROVIDER` | `Cloudflare` |
| `RCLONE_CONFIG_R2_ACCESS_KEY_ID` | R2 access key |
| `RCLONE_CONFIG_R2_SECRET_ACCESS_KEY` | R2 secret |
| `RCLONE_CONFIG_R2_ENDPOINT` | `https://<account-id>.r2.cloudflarestorage.com` |
| `RCLONE_CONFIG_R2_REGION` | `auto` — R2 has no regions, but the S3 backend still signs with one |
| `RCLONE_CONFIG_R2_NO_CHECK_BUCKET` | `true` — a bucket-scoped R2 token cannot HeadBucket/ListBuckets, and without this every operation fails on that check rather than on anything real |
| `RCLONE_CONFIG_R2_ACL` | `private` |

Optional: `BACKUP_RETAIN_DAYS` (default `30`), `BACKUP_MIN_ROWS` (default `1`).

The R2 API token must have **Object Read & Write** on this bucket — read alone cannot
upload, and write without delete cannot prune, which would fail the run at the prune step
*after* a good upload.

rclone is configured entirely through `RCLONE_CONFIG_<REMOTE>_*` environment variables, so
no config file and no secret ever lands in the image or the repository.

## Setting the schedule

Railway service settings → **Cron Schedule**. `0 3 * * *` is 03:00 UTC daily. The service
must exit after each run, which is why `restartPolicyType` is `NEVER`.

## Restoring

```
pg_restore -d "$TARGET_DATABASE_URL" wimba-<stamp>.dump
```

Expect one benign error on `ALTER DEFAULT PRIVILEGES FOR ROLE cloud_admin` — that is Neon
platform internals and is not part of the application schema. Do not pass
`--exit-on-error`, or the restore stops there; without it the restore completes and that
statement is simply skipped.

`--no-sync` is a `pg_dump` option and is **not** accepted by `pg_restore`.

## A caution about verifying a restore

Check row counts per table, not just the exit code, and not just the table count. Capture
the exit code of `pg_restore` itself — reading `$?` after piping it through `tail` reports
the exit code of `tail`, which will happily be `0` while the restore failed.
