# Singapore Neon cutover — reusable baseline

Tooling + runbook to consolidate ceritaAI infra into Singapore (`ap-southeast-1`), co-locating
Railway + Neon + Upstash. The risky part is the **Neon Postgres cutover** (paying-customer billing
data). This folder is the **single baseline** used both to rehearse it (Phase 0, local Docker lab)
and to execute it for real (Phase 2, against Neon) — the only difference is `env.sh` (`MODE=lab|real`).

> Decision status: the migration is **deferred** (nice-to-have, not urgent — see memory
> `ceritaai-region-consolidation`). This baseline exists so that when a trigger fires we execute,
> not re-derive. Co-location is the real win (~590 ms → ~10 ms DB wait per chatty request); the move
> is **all-or-nothing** (app in SG + DB in US-East = ~222 ms/query, strictly worse).

## Files
| File | Role |
|---|---|
| `run.sh` | Parameterized driver. Same subcommands for lab and real. |
| `env.example.sh` | Copy to `env.sh`, set `MODE` + connection strings. `env.sh` is gitignored. |
| `lib.sh` | Connection abstraction (`srcpsql`/`tgtpsql` + subscription conn strings) for both modes. |
| `sql/guard.sql` | Hard-stop check: any sequence behind `max(col)`? Must be empty before resuming writes. |
| `sql/advance.sql` | Advance every sequence to `max(col)` (self-executing via `\gexec`). |
| `sql/reconcile.sql` | Per-table row counts + money aggregates. Run on both, diff. |
| `sql/lag.sql` | Replication lag / initial-copy state / slot-retained WAL. |

## Quick start — Phase 0 rehearsal (local, zero risk, no cloud creds)
```bash
cp env.example.sh env.sh        # MODE=lab is the default
./run.sh lab-up                 # two PG16 containers: pg_source:5433, pg_target:5434
./run.sh build-source-lab       # reset source + apply migrations 0001..0028
./run.sh build-target           # dump source schema -> restore target (pre-creates app_user)
./run.sh replicate              # forward publication + subscription
./run.sh lag                    # wait until srsubstate=r and bytes_behind ~0
./run.sh reconcile              # source vs target must match
./run.sh cutover                # guard -> advance -> reconcile -> drop subscription
./run.sh failback               # reverse replication (target -> source)
./run.sh lab-down               # tear down
```

## Phase 2 — real Neon cutover
Fill the `MODE=real` block in `env.sh` (Neon source/target URLs + subscription conn strings; the
admin role MUST be `neondb_owner`, which is BYPASSRLS). Then, in a low-traffic WIB window:
```
./run.sh preflight              # wal_level, BYPASSRLS owner, slot/wal_sender headroom
./run.sh build-target           # restore schema into the new SG Neon project
./run.sh replicate              # start logical replication US-East -> SG
./run.sh lag                    # let it stream to bytes_behind ~0 (hours/days, no downtime)
#  --- maintenance window: put app read-only ---
./run.sh cutover                # guard -> advance -> reconcile -> drop subscription
./run.sh failback               # arm reverse replication SG -> US-East (the safety net)
#  repoint DATABASE_URL + DATABASE_POOL_URL to SG, redeploy Railway(SG), resume writes
```

## Findings (this schema, verified in the lab)
1. **Migrations build clean.** `0001→0028` apply with no errors on a fresh PG16 → 21 tables. ✅ verified
2. **Sequence-collision risk is near-zero here.** Every app table uses a **UUID PK**
   (`gen_random_uuid()`). The *only* sequence in the whole DB is `migrations_id_seq` (not written by
   normal traffic). So the classic "forgot to advance sequences → duplicate-key on billing" landmine
   barely applies — money tables can't collide. `guard.sql`/`advance.sql` stay in as a safety net. ✅ verified
3. **The dominant risk is the subscription owner, not sequences.** 19 tables are `FORCE ROW LEVEL
   SECURITY` and are owned by a privileged role (Neon: `neondb_owner`). In PG15+ the apply/tablesync
   worker **`SET ROLE`s to the table owner** to apply rows; a subscription owned by a role that can't
   `SET ROLE` to that owner dies immediately. ✅ **verified in lab**: an `app_user`-owned subscription
   produced `ERROR: role "app_user" cannot SET ROLE to "postgres"`, looping tablesync failures
   (`sync_error_count` climbing past 32), and **zero rows replicated** — while the slot keeps retaining
   WAL on the source (= prod disk risk). The same subscription owned by the superuser/owner replicated
   all data with a perfect reconcile. ⇒ the target subscription MUST be owned by **`neondb_owner`**,
   never `app_user`. (`app_user` has INSERT on credit_ledger, so this is the owner/SET-ROLE barrier,
   which sits in front of the RLS WITH CHECK barrier — either way: don't own the sub with app_user.)
4. **`pg_dump --schema-only` carries `GRANT … TO app_user`** → restore aborts if the role is absent.
   ✅ mitigated by `build-target`, which pre-creates `app_user` before restore (clean 21-table restore).
5. **Connection trap.** The subscription runs inside the subscriber and reaches the publisher by
   network host + internal port — lab: `host=pg_source port=5432` (not localhost/5433). ✅ verified
6. **DDL & sequences are not replicated.** Restore schema first; advance sequences at cutover. ✅ verified
7. **Failback = reverse replication** target→source (`copy_data=false`), armed right after cutover.
   ✅ **verified in lab**: a post-cutover `-30` write on the target streamed back to the old source
   (both reached `rows=5, SUM(delta)=1400`). The reverse path carries no DDL and doesn't advance source
   sequences — reverse-`reconcile` before trusting it.

**Full lab pass (this session):** migrations build → seed → schema dump/restore → forward replication
→ `lag` (bytes_behind 0) → `reconcile` (source=target, credit SUM matched) → `cutover` (guard 0 rows
→ advance → drop subscription, slot cleanly removed) → `failback` (reverse stream verified). Every
step green against the real FORCE-RLS schema.

## Safety gates (non-negotiable for the real run)
- Subscription owner = `neondb_owner` (BYPASSRLS). Confirm with `preflight`.
- `guard.sql` returns zero rows before resuming writes.
- `reconcile` row counts + money aggregates match source↔target before AND (reverse) after cutover.
- Old US-East Neon project stays hot ≥ 1 week; failback subscription armed before resuming writes.
- Out of scope: Cloudflare (global edge, nothing to migrate). Separate item: Qdrant (vector DB) is a
  4th stateful service with its own region — handle alongside Neon when this runs.

## Phase 0b staging — provisioned (2026-06-17)
Real SG data stores stood up (throwaway, free/low-cost — DELETE on teardown):
- Neon SG target: `rcs-sg-staging` (project `curly-hill-79252285`, `ap-southeast-1`)
- Neon US-East source-probe: `rcs-use-probe` (`wild-field-78503220`, `us-east-1`)
- Upstash SG: `rcs-sg-staging` (`present-giraffe-150090`, Global primary `ap-southeast-1`) — conn in `upstash_sg.local`

`env.sh` (gitignored) is pre-wired `MODE=real` to the two Neon staging projects, so the harness runs
end-to-end against real Neon WITHOUT touching prod. `./run.sh preflight` verified: both reachable,
target `neondb_owner` BYPASSRLS=t, slots/wal_senders 10/10. Caveat surfaced: Neon source
`wal_level=replica` — **logical replication must be ENABLED on the real source** (Neon console) before a
Phase 2 cutover. App tier (Railway SG) not deployed. For real Phase 2: swap `SOURCE_URL`→prod `rino-saas`,
`TARGET_URL`→a fresh SG project.
