# T72 — `fx_rates_population_contract`

Acceptance for the `fxpop` contract. **Normative source: `PLAN-046 §A/S10.G3.3-c`.**
`MATRIX-046 T72` is the acceptance for that contract, not its specification — an earlier
implementation was built from the T72 row alone and diverged from the normative signatures,
the rename, the privilege split and the correction protocol. Read `G3.3-c` first.

## Status: **PASS** — Gate 4 CLOSED 2026-08-10

All fifteen clauses execute and pass. **Clause (11), the correction-vs-birth race, is a REAL
two-transaction test** — the tripwire it replaced is gone. `0089` shipped the birth path, so
`test_c11_*` runs against the actual engine: with `g3_birth_lots` holding a rate `FOR SHARE`
mid-birth, a corrector that has already corrected another rate gets **`FX001` in ~1 ms**, never
`55P03`; a negative control first proves that same lock genuinely blocks a plain `FOR UPDATE`.
After the engine commits, a fresh corrector gets **`FX002`**.

**PARTIAL WAS ABOUT IDENTITY, NOT COVERAGE — and the identity is now wired.** These tests used
to drive the real function path under `neondb_owner`: `BYPASSRLS`, privileged on everything,
incapable of failing a privilege check or an RLS policy even in principle. `0091` made
`g3_birth_lots` the single executable entrypoint, owned by the NOLOGIN/NOSUPERUSER/NOBYPASSRLS
boundary role `g3_birth_definer`, with EXECUTE granted to `g3_posting_engine` alone — no helper
EXECUTE, no table privilege on `credit_lots`, and PUBLIC revoked on the three `SECURITY DEFINER`
lock-takers. Births run through a restricted LOGIN probe that assumes the engine by `SET ROLE`;
the bare probe is refused `42501`, and the identity is asserted inside the transaction that
calls the function.

Measured at the close: **T72 43/43**, **81/81 mutations caught**, **52/52** across the four
`l2c_realdb_*` suites through a non-superuser principal, preflight OK, hashes identical before
and after the mutation run.

**What a PASS here still does not claim.** The writer stays OFF (`G3_LOT_WRITER_ENABLED='0'`),
nothing is deployed, and `--fetch` has never run against the live Dodo API.

Simulating the engine with a hand-rolled `FOR UPDATE` holder — which the tripwire existed to
prevent — would have exercised only `0088`'s own code path. Reporting that green as a PASS on
the race is the failure mode this project already paid for at `T79`, where a suite connected as
`postgres` reported 35/35 against a writer that had shipped `SECURITY INVOKER`.

## Run it

```bash
tests/t72/harness.sh test          # provision + migrate + run the suite
tests/t72/harness.sh mutate        # all 81 mutations
tests/t72/harness.sh mutate M07    # one
tests/t72/harness.sh preflight     # 0088 must refuse a pre-existing LOGIN fx_rates_owner
tests/t72/harness.sh down          # stop the cluster
tests/t72/harness.sh destroy       # stop AND delete the data directory
```

Bootstraps from nothing: `up` creates the data directory, runs `initdb`, and only then writes
its `.t72-disposable` marker — writing the marker first made the directory non-empty and
`initdb` refused it, so the cluster could only ever be started by hand.

## What is under test, and where it lives

| migration | contents |
|---|---|
| `0088` | the `fxpop` contract: three roles, the `idr_per_major_unit` rename, `fx_rates_corrections` (`correction_xid XID8 UNIQUE`), the three `SECURITY DEFINER` functions, the three-statement correction protocol, the `credit_lots` reference policy, the ownership sequence |
| `0089` | the birth path: the date-column unification, and `resolve → materialise → lock (ONE ordered `FOR SHARE` statement) → VERIFY the count → write` |

🔴 **`0088` and `0089` ship as ONE unit.** `0088` alone leaves `credit_lots` carrying two columns
for one business value; `0089` is what unifies them. Neither may be committed or deployed alone.

Overridable: `T72_PGBIN`, `T72_PGPORT` (default 55488), `T72_PGDATA`, `T72_DB`, `T72_NODE_PATH`.

`migrate.js` needs node's `pg`. The harness resolves it from `backend/node_modules` or
`node_modules` in **this** checkout and **fails with instructions** if it is absent — it is
deliberately not defaulted to another checkout on the machine, which made earlier runs
unreproducible. Either `npm install` in `backend/`, or point `T72_NODE_PATH` at a
`node_modules` that contains `pg` (it is validated, not trusted).

## Safety — what this harness will and will not delete

`T72_PGDATA` is an env override that a `rm -rf` once consumed unvalidated. Now nothing is
removed unless it passes every one of these, and a directory the harness did not create is
never touched:

| guard | refuses |
|---|---|
| absolute path | `relative/path` |
| no `..` segments | `/tmp/a/../../etc` |
| at least two path segments | `/tmp` |
| not a system or home root | `/`, `/Users`, `/etc`, `$HOME`, … |
| under a temp root | `/Users/rino/Documents/anything` |
| carries the `.t72-disposable` marker, or is empty/absent | any non-empty directory the harness did not create |

`provision.sh` **does not swallow role-cleanup failures**. Every listed role is dropped across
every database, and the post-condition is then verified; a role that survives aborts the run
rather than letting a contaminated cluster report itself "fresh".

`postmigrate.sh` **never alters a normative role** and asserts as a post-condition that
`fx_rates_owner`, `fx_rates_writer` and `g3_posting_engine` are still `NOLOGIN`, non-superuser
and non-BYPASSRLS.

## The suite hard-fails; it never skips

`tests/python/test_accounting_privileges.py` documents the opposite convention and audit F-07
flagged it: DB-backed tests that skip silently when the DSN is unset let a regression land
fully green. Here, each of these **fails the run**:

| condition | result |
|---|---|
| `T72_DSN` unset | exit 2, collection error |
| `T72_BREAKGLASS_DSN` unset | exit 2, collection error |
| `asyncpg` not importable | exit 2, collection error |
| operator connection is superuser | exit 1, naming the `T79` defect |
| operator owns `fx_rates`/`credit_lots` | exit 1 |
| `credit_lots` lacks ENABLE+FORCE RLS | exit 1 |
| `fx_rates_owner` is superuser or BYPASSRLS | exit 1 |

## Why five connections

After `0088` the migration role holds **no privilege on `fx_rates` at all** — ownership moved to
`fx_rates_owner` and the temporary membership was revoked per `G3.3-c(8)(v)`. That is the
contract working, and it forces the identities apart:

| name | role | why |
|---|---|---|
| `admin` | `neondb_owner` | owns `credit_lots`, BYPASSRLS; builds fixtures |
| `operator` | `fx_t72_operator` | restricted; SET-only on `fx_rates_writer`; makes every API call |
| `reader` | `fx_t72_probe` → `SET ROLE g3_posting_engine` | the normative reader is **NOLOGIN and stays NOLOGIN**. A restricted probe that owns nothing and inherits nothing logs in and ASSUMES it, so everything the connection can see is attributable to `g3_posting_engine`'s SELECT grant |
| `app` | `app_user` | denial + RLS assertions |
| `breakglass` | `postgres` | the "acknowledged, documented break-glass" of `G3.3-c(1)`, used **only** where a test must act AS the table owner to prove a TRIGGER rather than a privilege error — never for a privilege assertion |

## Environment notes that cost time

- `initdb` needs `--locale=C`; a macOS shell's default locale is rejected.
- `unix_socket_directories = ''`. A scratchpad or `TMPDIR` path easily exceeds the 103-byte
  socket-path limit, so this cluster is TCP-only by construction.
- `migrate.js` needs `PGSSLMODE=disable` against a local cluster.
- `postmigrate.sh` grants `neondb_owner` SET on `app_user`. Without it the **pre-existing**
  `test_live_g3_topup_quarantine_rejects_cross_tenant_access` fails for an environment reason —
  which is itself evidence that CI runs that suite as a superuser.

## Mutations

43, each breaking exactly one mechanism of `G3.3-c` or of the birth path. All 43 are caught —
most by the suite, four before it: two signature drifts break the later `ALTER … OWNER TO` so the
migration will not apply, and two `LOGIN` normative roles trip `postmigrate.sh`'s post-condition.
Those four cannot even reach a test run, which is the stronger outcome; `test_c01c` asserts the
same `NOLOGIN` property from inside the suite so the attribution stays unambiguous.

Mutations must target the SURVIVING definition. `M19`, `M20` and `M23` originally pointed at
constraints `0089` drops and recreates, so mutating `0088`'s copy was invisible in the final
state — retargeted to `0089`.

Two mutations were themselves defective on the first pass and were fixed rather than excused:

- deleting `PARALLEL UNSAFE` is a **no-op** — plpgsql already defaults to unsafe, so `proparallel`
  never moved. The real defect is declaring it `PARALLEL SAFE`.
- mutating an assertion to `assert x == x` defeats any suite by construction and measures nothing.
  The meaningful defect is a **broken tripwire scanner**, which now has its own positive control.

## Three properties are asserted STRUCTURALLY, and why

Some defects are invisible from outside, so the assertion reads the shipped SQL. Each is labelled
in the test:

* **lock → verify → write ordering** inside `g3_birth_lots`. A lazily-locking engine still ends up
  blocked — the FK check takes its own `KEY SHARE` lock at `INSERT` time — and the whole
  transaction rolls back either way, so "no rows written" looks identical.
* **`v_required` is derived before the lock and never re-assigned after it.** `v_required :=
  v_locked` makes the completeness comparison tautological, but the writer's own `FX007` backstop
  then raises the same error, so behaviour is unchanged. **A defence-in-depth backstop can hide a
  missing primary check**: only the engine's comparison happens *before any write*.
* **the corrector's finality test is a real fresh-snapshot read.** With `v_used := FALSE` the
  backstop trigger still raises `FX002`.

## A note on the money-path guard

`tests/python/test_accounting_privileges.py` reports **21/21 with a live `TEST_DATABASE_URL`** and
**19 passed + 2 skipped without one** — its live section skips silently, which is the F-07
convention this suite deliberately breaks. Always run it with the DSN, or the two assertions that
actually touch the database contribute nothing.
