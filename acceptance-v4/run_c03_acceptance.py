#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
from textwrap import dedent


ROOT = Path(__file__).resolve().parent
CANDIDATE = Path("database/migrations/0072_narasi_continuity_schema.sql")
C01_RUNNER = Path(
    "/Users/rino/docs/IMPORTANT-wimba-narasi-c01-story-contract-closure-acceptance-v2/"
    "run_c01_closure_acceptance.py"
)
C01_FINAL = "C-01 CLOSURE ACCEPTANCE: PASS"
TABLES = (
    "narasi_continuity_contracts",
    "narasi_continuity_claims",
    "narasi_continuity_violations",
    "narasi_continuity_coverage",
    "narasi_continuity_recovery_artifacts",
)
TENANT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TENANT_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT_A = "11111111-1111-4111-8111-111111111111"
PROJECT_B = "22222222-2222-4222-8222-222222222222"
JOB_A = "33333333-3333-4333-8333-333333333333"
JOB_B = "44444444-4444-4444-8444-444444444444"
CONTRACT_A = "55555555-5555-4555-8555-555555555555"
CONTRACT_A2 = "66666666-6666-4666-8666-666666666666"
CLAIM_A = "77777777-7777-4777-8777-777777777777"
VIOLATION_A = "88888888-8888-4888-8888-888888888888"
COVERAGE_A = "99999999-9999-4999-8999-999999999999"
RECOVERY_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
CH1 = "ch_11111111111111111111111111111111"
CH2 = "ch_22222222222222222222222222222222"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=text,
        check=False,
    )
    if check and result.returncode != 0:
        stdout = result.stdout if text else result.stdout.decode(errors="replace")
        stderr = result.stderr if text else result.stderr.decode(errors="replace")
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(args)}\n{stdout}\n{stderr}"
        )
    return result


def git(worktree: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(worktree), *args], text=text)


def verify_pack() -> None:
    manifest = ROOT / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise AssertionError("SHA256SUMS missing or symlinked")
    listed: set[str] = set()
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        digest, relative = line.split("  ", 1)
        listed.add(relative)
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            raise AssertionError(f"pack member missing or symlinked: {relative}")
        actual = sha_file(path)
        if actual != digest:
            raise AssertionError(f"pack hash mismatch for {relative}: {actual} != {digest}")
    expected = {
        "AUDIT-FINDINGS.md",
        "C03-SCHEMA-RLS-CONTRACT.md",
        "CLAUDE-C03-ORDER.md",
        "README.md",
        "SOURCE-BASELINE.json",
        "rollback_c03.sql",
        "run_c03_acceptance.py",
    }
    if listed != expected:
        raise AssertionError(f"pack manifest inventory mismatch: {sorted(listed ^ expected)}")


def status_without_candidate(worktree: Path) -> bytes:
    raw = git(worktree, "status", "--porcelain=v1", "-z", text=False).stdout
    kept: list[bytes] = []
    for row in raw.split(b"\0"):
        if not row:
            continue
        if row[3:].decode(errors="strict") == str(CANDIDATE):
            continue
        kept.append(row)
    return b"\0".join(kept) + (b"\0" if kept else b"")


def verify_baseline(worktree: Path, *, candidate_required: bool) -> dict[str, object]:
    baseline = json.loads((ROOT / "SOURCE-BASELINE.json").read_text(encoding="utf-8"))
    head = git(worktree, "rev-parse", "HEAD").stdout.strip()
    branch = git(worktree, "branch", "--show-current").stdout.strip()
    if head != baseline["backend_head"]:
        raise AssertionError(f"backend HEAD drift: {head}")
    if branch != baseline["backend_branch"]:
        raise AssertionError(f"backend branch drift: {branch}")
    for relative, expected in baseline["required_files"].items():
        path = worktree / relative
        actual = sha_file(path) if path.is_file() and not path.is_symlink() else "MISSING"
        if actual != expected:
            raise AssertionError(f"source baseline drift: {relative}: {actual} != {expected}")
    diff = git(worktree, "diff", "--binary", text=False).stdout
    if sha_bytes(diff) != baseline["tracked_diff_sha256"]:
        raise AssertionError("pre-existing tracked diff drifted")
    status_hash = sha_bytes(status_without_candidate(worktree))
    if status_hash != baseline["status_without_candidate_sha256"]:
        raise AssertionError(
            "worktree status outside the authorized 0072 candidate drifted: " + status_hash
        )
    candidate = worktree / CANDIDATE
    if candidate_required and (not candidate.is_file() or candidate.is_symlink()):
        raise AssertionError("0072 candidate is missing or symlinked")
    if not candidate_required and candidate.exists():
        raise AssertionError("0072 already exists before authorized implementation")
    higher = [
        p.name
        for p in (worktree / "database" / "migrations").glob("*.sql")
        if p.name[:4].isdigit() and int(p.name[:4]) >= 72 and p != candidate
    ]
    if higher:
        raise AssertionError(f"unexpected migration at or after 0072: {higher}")
    deleted = [
        row.decode(errors="replace")
        for row in git(worktree, "status", "--porcelain=v1", "-z", text=False).stdout.split(b"\0")
        if row and b"D" in row[:2]
    ]
    if deleted:
        raise AssertionError(f"tracked deletion present: {deleted}")
    if git(worktree, "diff", "--check").stdout.strip():
        raise AssertionError("git diff --check failed")
    return baseline


def verify_candidate_static(candidate: Path) -> None:
    raw = candidate.read_text(encoding="utf-8")
    upper = raw.upper()
    if len(raw.encode()) > 200_000:
        raise AssertionError("0072 exceeds 200 KiB")
    if not re.search(r"\bBEGIN\s*;", upper) or not re.search(r"\bCOMMIT\s*;\s*$", upper):
        raise AssertionError("0072 must be explicitly transactional")
    forbidden = {
        "SECURITY DEFINER": "C-03 functions must be invoker-security",
        "CREATE EXTENSION": "0072 may not install extensions",
        "DROP TABLE": "0072 is additive and may not drop tables",
        "TRUNCATE ": "0072 may not truncate data",
        "DATABASE_URL": "0072 may not reference configured databases",
        "CREATE ROLE": "0072 may not create roles",
    }
    for needle, message in forbidden.items():
        if needle in upper:
            raise AssertionError(message)
    create_table_count = len(re.findall(r"\bCREATE\s+TABLE\b", raw, re.I))
    found = set(re.findall(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)", raw, re.I))
    if create_table_count != 5 or found != set(TABLES):
        raise AssertionError(f"0072 must create exactly the five continuity tables, got {found}")
    function_names = re.findall(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+([a-z0-9_]+)", raw, re.I
    )
    if not function_names or any(not name.lower().startswith("narasi_c03_") for name in function_names):
        raise AssertionError("every 0072 function must use the narasi_c03_ prefix")


class Evidence:
    def __init__(self) -> None:
        self.groups: Counter[str] = Counter()

    def check(self, group: str, name: str, condition: bool, detail: str = "") -> None:
        if not condition:
            raise AssertionError(f"[{group}] {name} failed{': ' + detail if detail else ''}")
        self.groups[group] += 1

    def expect_failure(
        self,
        group: str,
        name: str,
        result: subprocess.CompletedProcess,
        contains: str | None = None,
    ) -> None:
        output = (result.stdout or "") + (result.stderr or "")
        ok = result.returncode != 0 and (contains is None or contains.lower() in output.lower())
        self.check(group, name, ok, output[-1000:])


class LocalPostgres:
    def __init__(self, evidence: Evidence) -> None:
        self.evidence = evidence
        self.root = Path(tempfile.mkdtemp(prefix="wimba-c03-postgres-"))
        self.data = self.root / "data"
        self.socket = self.root / "socket"
        self.log = self.root / "postgres.log"
        self.port = random.randint(55440, 58999)
        self.started = False
        names = ("initdb", "pg_ctl", "psql", "createdb")
        paths = {name: shutil.which(name) for name in names}
        missing = [name for name, path in paths.items() if not path]
        if missing:
            raise AssertionError(f"missing local PostgreSQL binaries: {missing}")
        self.bin = {name: str(path) for name, path in paths.items()}
        self.env = {
            key: value
            for key, value in os.environ.items()
            if "DATABASE_URL" not in key.upper()
            and key not in {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGSERVICE"}
        }
        self.env["PGCONNECT_TIMEOUT"] = "5"

    def start(self) -> None:
        run(
            [self.bin["initdb"], "-D", str(self.data), "--auth=trust", "--no-locale", "--encoding=UTF8"],
            env=self.env,
        )
        self.socket.mkdir(mode=0o700)
        options = f"-k {self.socket} -p {self.port} -c listen_addresses=''"
        run(
            [self.bin["pg_ctl"], "-D", str(self.data), "-l", str(self.log), "-o", options, "start"],
            env=self.env,
        )
        self.started = True
        run(
            [
                self.bin["createdb"], "-h", str(self.socket), "-p", str(self.port),
                "c03_acceptance",
            ],
            env=self.env,
        )
        version = self.query("SHOW server_version")
        self.evidence.check("postgres", "real local PostgreSQL server", bool(version.strip()), version)
        listening = self.query("SHOW listen_addresses")
        self.evidence.check("postgres", "TCP listening disabled", listening.strip() == "", listening)

    def stop(self) -> None:
        if self.started:
            run(
                [self.bin["pg_ctl"], "-D", str(self.data), "stop", "-m", "fast"],
                env=self.env,
                check=False,
            )
            self.started = False
        shutil.rmtree(self.root, ignore_errors=True)

    def base(self) -> list[str]:
        return [
            self.bin["psql"], "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-q",
            "-h", str(self.socket), "-p", str(self.port), "-d", "c03_acceptance",
        ]

    def file(self, path: Path, *, check: bool = True) -> subprocess.CompletedProcess:
        return run(self.base() + ["-f", str(path)], env=self.env, check=check)

    def execute(
        self,
        sql: str,
        *,
        role: str | None = None,
        tenant: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        prefix = ""
        if role:
            prefix += f"SET ROLE {role};"
        if tenant is not None:
            prefix += f"SET app.current_tenant_id = '{tenant}';"
        return run(self.base() + ["-c", prefix + sql], env=self.env, check=check)

    def query(self, sql: str, *, role: str | None = None, tenant: str | None = None) -> str:
        return self.execute(sql, role=role, tenant=tenant).stdout.strip()


@contextmanager
def local_postgres(evidence: Evidence):
    pg = LocalPostgres(evidence)
    try:
        pg.start()
        yield pg
    finally:
        pg.stop()


def apply_pre_c03_chain(pg: LocalPostgres, worktree: Path, ev: Evidence) -> int:
    migrations = []
    for path in sorted((worktree / "database" / "migrations").glob("*.sql")):
        match = re.match(r"^(\d{4})_", path.name)
        if match and int(match.group(1)) <= 71:
            migrations.append(path)
    if not migrations or migrations[-1].name != "0071_narasi_derived_input.sql":
        raise AssertionError("pre-C-03 migration inventory does not end at 0071")
    for path in migrations:
        pg.file(path)
    ev.check("migration", "full pre-C-03 chain applied", len(migrations) >= 60, str(len(migrations)))
    return len(migrations)


def parent_schema_fingerprint(pg: LocalPostgres) -> str:
    queries = [
        "SELECT table_name,column_name,data_type,udt_name,is_nullable,coalesce(column_default,'') "
        "FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name IN ('tenants','jobs','projects') ORDER BY 1,ordinal_position",
        "SELECT conrelid::regclass::text,conname,contype,condeferrable,condeferred,convalidated,"
        "pg_get_constraintdef(oid,true) FROM pg_constraint "
        "WHERE conrelid IN ('tenants'::regclass,'jobs'::regclass,'projects'::regclass) ORDER BY 1,2",
        "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
        "AND tablename IN ('tenants','jobs','projects') ORDER BY 1,2",
        "SELECT tablename,policyname,roles::text,cmd,qual,with_check FROM pg_policies "
        "WHERE schemaname='public' AND tablename IN ('tenants','jobs','projects') ORDER BY 1,2",
        "SELECT table_name,grantee,privilege_type FROM information_schema.role_table_grants "
        "WHERE table_schema='public' AND table_name IN ('tenants','jobs','projects') "
        "ORDER BY 1,2,3",
        "SELECT event_object_table,trigger_name,action_timing,event_manipulation,action_statement "
        "FROM information_schema.triggers WHERE trigger_schema='public' "
        "AND event_object_table IN ('tenants','jobs','projects') ORDER BY 1,2,3",
        "SELECT rolname,rolsuper,rolinherit,rolcreaterole,rolcreatedb,rolcanlogin,rolreplication,"
        "rolbypassrls FROM pg_roles WHERE rolname='app_user'",
        "SELECT enumlabel,enumsortorder FROM pg_enum WHERE enumtypid='job_type_enum'::regtype "
        "ORDER BY enumsortorder",
    ]
    payload = [pg.query(query) for query in queries]
    return sha_bytes(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode())


def catalog_tests(pg: LocalPostgres, ev: Evidence) -> None:
    inventory = pg.query(
        "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables "
        "WHERE schemaname='public' AND tablename LIKE 'narasi_continuity_%'"
    ).split(",")
    ev.check("catalog", "exact five-table inventory", inventory == sorted(TABLES), str(inventory))

    rls = pg.query(
        "SELECT count(*) FROM pg_class WHERE relname = ANY(ARRAY["
        + ",".join(f"'{name}'" for name in TABLES)
        + "]) AND relrowsecurity AND relforcerowsecurity"
    )
    ev.check("catalog", "ENABLE and FORCE RLS on all tables", rls == "5", rls)

    policy_rows = pg.query(
        "SELECT tablename || '|' || policyname || '|' || qual || '|' || with_check "
        "FROM pg_policies WHERE schemaname='public' AND tablename LIKE 'narasi_continuity_%' "
        "ORDER BY tablename"
    ).splitlines()
    ev.check("catalog", "one policy per table", len(policy_rows) == 5, str(policy_rows))
    ev.check(
        "catalog",
        "policies use NULLIF tenant expression in USING and WITH CHECK",
        all(row.lower().count("nullif") >= 2 and row.lower().count("current_setting") >= 2 for row in policy_rows),
        str(policy_rows),
    )

    def columns(table: str) -> set[str]:
        raw = pg.query(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='public' AND table_name='{table}' ORDER BY column_name"
        )
        return set(raw.splitlines()) if raw else set()

    required = {
        "narasi_continuity_contracts": {
            "id", "tenant_id", "project_id", "job_id", "contract_version",
            "supersedes_contract_id", "status", "amendment_reason", "affected_chapter_ids",
            "revalidation_plan", "story_contract", "story_contract_hash", "target_language",
            "contract_schema_version", "contract_prompt_version", "compiler_version",
            "slicer_version", "ordered_chapter_ids", "chapter_slice_hashes",
            "generation_started_at", "created_at", "updated_at",
        },
        "narasi_continuity_claims": {
            "id", "tenant_id", "contract_id", "job_id", "chapter_id",
            "chapter_content_hash", "story_contract_hash", "extractor_schema_version",
            "extractor_prompt_version", "extractor_epoch", "model_route", "claims",
            "created_at", "expires_at",
        },
        "narasi_continuity_violations": {
            "id", "tenant_id", "contract_id", "claim_id", "job_id", "story_contract_hash",
            "predicate_id", "predicate_set_version", "violation_type", "severity", "evidence",
            "attempt_count", "resolution_state", "resolved_at", "evidence_expires_at",
            "evidence_purged_at", "created_at", "updated_at",
        },
        "narasi_continuity_coverage": {
            "id", "tenant_id", "contract_id", "job_id", "story_contract_hash",
            "final_candidate_hash", "predicate_set_version", "extractor_schema_version",
            "extractor_prompt_version", "extractor_epoch", "diff_version", "coverage",
            "coverage_hash", "created_at", "updated_at",
        },
        "narasi_continuity_recovery_artifacts": {
            "id", "tenant_id", "contract_id", "job_id", "story_contract_hash",
            "final_candidate_hash", "object_bucket", "object_key", "object_version",
            "encryption_algorithm", "encryption_key_id", "ciphertext_sha256",
            "ciphertext_size_bytes", "terminal_at", "expires_at", "deleted_at",
            "access_audit", "created_at", "updated_at",
        },
    }
    for table, expected in required.items():
        actual = columns(table)
        ev.check("catalog", f"{table} required columns", expected <= actual, str(sorted(expected - actual)))

    forbidden = pg.query(
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='narasi_continuity_recovery_artifacts' "
        "AND column_name ~* '(^|_)(manuscript|content|body|text|prompt|output|response|log|logs|url)($|_)'"
    )
    ev.check("catalog", "recovery has no body/log/URL column", forbidden == "", forbidden)

    secdef = pg.query(
        "SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace "
        "AND proname LIKE 'narasi_c03_%' AND prosecdef"
    )
    ev.check("catalog", "no SECURITY DEFINER C-03 function", secdef == "0", secdef)

    parent_indexes = pg.query(
        "SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND "
        "(indexname='uq_c03_jobs_tenant_id_id' OR indexname='uq_c03_projects_tenant_id_id') "
        "AND indexdef LIKE 'CREATE UNIQUE INDEX%' AND indexdef NOT LIKE '% WHERE %'"
    )
    ev.check("catalog", "two non-partial unique parent indexes", parent_indexes == "2", parent_indexes)

    job_fk = pg.query(
        "SELECT condeferrable::text || '|' || condeferred::text || '|' || confdeltype::text "
        "FROM pg_constraint WHERE conrelid='narasi_continuity_contracts'::regclass "
        "AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (tenant_id, job_id)%'"
    )
    ev.check("catalog", "job ownership FK is deferred NO ACTION", job_fk == "true|true|a", job_fk)
    project_fk = pg.query(
        "SELECT confdeltype FROM pg_constraint "
        "WHERE conrelid='narasi_continuity_contracts'::regclass "
        "AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (tenant_id, project_id)%'"
    )
    ev.check("catalog", "project ownership FK cascades", project_fk == "c", project_fk)
    tenant_fks = pg.query(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = ANY(ARRAY["
        + ",".join(f"'{name}'::regclass" for name in TABLES)
        + "]) AND contype='f' AND pg_get_constraintdef(oid) LIKE "
        "'FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE%'"
    )
    ev.check("catalog", "all five tenant FKs cascade", tenant_fks == "5", tenant_fks)
    for table in TABLES[1:]:
        binding_fk = pg.query(
            "SELECT confdeltype FROM pg_constraint "
            f"WHERE conrelid='{table}'::regclass AND pg_get_constraintdef(oid) LIKE "
            "'FOREIGN KEY (tenant_id, contract_id, job_id, story_contract_hash)%'"
        )
        ev.check(
            "catalog", f"{table} has composite contract ownership FK",
            binding_fk == "c", binding_fk,
        )
    invalid_constraints = pg.query(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = ANY(ARRAY["
        + ",".join(f"'{name}'::regclass" for name in TABLES)
        + "]) AND NOT convalidated"
    )
    ev.check("catalog", "all continuity constraints are validated", invalid_constraints == "0", invalid_constraints)
    guard_triggers = pg.query(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid = ANY(ARRAY["
        + ",".join(f"'{name}'::regclass" for name in TABLES)
        + "]) AND NOT tgisinternal AND tgenabled <> 'D'"
    )
    ev.check("catalog", "every table has an enabled mutation guard", int(guard_triggers) >= 5, guard_triggers)
    role_safety = pg.query(
        "SELECT (NOT rolbypassrls AND NOT rolsuper)::text FROM pg_roles WHERE rolname='app_user'"
    )
    ev.check("catalog", "app_user cannot bypass RLS", role_safety == "true", role_safety)

    expected_grants = {
        "narasi_continuity_contracts": {"SELECT", "INSERT", "UPDATE"},
        "narasi_continuity_claims": {"SELECT", "INSERT", "DELETE"},
        "narasi_continuity_violations": {"SELECT", "INSERT", "UPDATE", "DELETE"},
        "narasi_continuity_coverage": {"SELECT", "INSERT"},
        "narasi_continuity_recovery_artifacts": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    }
    for table, expected in expected_grants.items():
        raw = pg.query(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            f"WHERE table_schema='public' AND table_name='{table}' AND grantee='app_user' "
            "ORDER BY privilege_type"
        )
        actual = set(raw.splitlines()) if raw else set()
        ev.check("catalog", f"least-privilege app_user grants on {table}", actual == expected, str(actual))
    public_grants = pg.query(
        "SELECT count(*) FROM information_schema.role_table_grants "
        "WHERE table_schema='public' AND table_name LIKE 'narasi_continuity_%' AND grantee='PUBLIC'"
    )
    ev.check("catalog", "PUBLIC has no continuity-table grant", public_grants == "0", public_grants)


def root_contract_sql(
    *,
    contract_id: str = CONTRACT_A,
    tenant: str = TENANT_A,
    project: str = PROJECT_A,
    job: str = JOB_A,
    version: int = 1,
    status: str = "validated",
    supersedes: str | None = None,
) -> str:
    supersedes_sql = "NULL" if supersedes is None else f"'{supersedes}'::uuid"
    reason = "NULL" if supersedes is None else "'Repair chapter 2 continuity'"
    affected = "'[]'::jsonb" if supersedes is None else f"'[\"{CH2}\"]'::jsonb"
    plan = "'{}'::jsonb" if supersedes is None else "'{\"mode\":\"revalidate_affected\"}'::jsonb"
    return dedent(
        f"""
        INSERT INTO narasi_continuity_contracts (
          id, tenant_id, project_id, job_id, contract_version, supersedes_contract_id,
          status, amendment_reason, affected_chapter_ids, revalidation_plan,
          story_contract, story_contract_hash, target_language, contract_schema_version,
          contract_prompt_version, compiler_version, slicer_version, ordered_chapter_ids,
          chapter_slice_hashes
        ) VALUES (
          '{contract_id}', '{tenant}', '{project}', '{job}', {version}, {supersedes_sql},
          '{status}', {reason}, {affected}, {plan},
          '{{\"schema_version\":\"story-contract-v3\"}}'::jsonb, '{HASH_A}', 'en', '3',
          'prompt-1', 'compiler-1', 'slicer-1', '[\"{CH1}\",\"{CH2}\"]'::jsonb,
          '{{\"{CH1}\":\"{HASH_B}\",\"{CH2}\":\"{HASH_C}\"}}'::jsonb
        );
        """
    )


def seed_parents(pg: LocalPostgres) -> None:
    pg.execute(
        dedent(
            f"""
            INSERT INTO tenants (id, name, slug, email) VALUES
              ('{TENANT_A}', 'Tenant A', 'c03-tenant-a', 'c03-a@example.invalid'),
              ('{TENANT_B}', 'Tenant B', 'c03-tenant-b', 'c03-b@example.invalid');
            INSERT INTO projects (id, tenant_id, name) VALUES
              ('{PROJECT_A}', '{TENANT_A}', 'Project A'),
              ('{PROJECT_B}', '{TENANT_B}', 'Project B');
            INSERT INTO jobs (id, tenant_id, job_type, status) VALUES
              ('{JOB_A}', '{TENANT_A}', 'narasi_derived_input', 'queued'),
              ('{JOB_B}', '{TENANT_B}', 'narasi_derived_input', 'queued');
            """
        )
    )


def rls_and_contract_tests(pg: LocalPostgres, ev: Evidence) -> None:
    seed_parents(pg)
    for table in TABLES:
        count = pg.query(
            f"RESET app.current_tenant_id; SELECT count(*) FROM {table};",
            role="app_user",
        ).splitlines()[-1]
        ev.check("rls", f"unset tenant sees zero rows in {table}", count == "0", count)

    pg.execute(root_contract_sql(), role="app_user", tenant=TENANT_A)
    ev.check(
        "contract", "tenant A inserts root contract",
        pg.query("SELECT count(*) FROM narasi_continuity_contracts", role="app_user", tenant=TENANT_A) == "1",
    )
    ev.check(
        "rls", "tenant B cannot see tenant A contract",
        pg.query("SELECT count(*) FROM narasi_continuity_contracts", role="app_user", tenant=TENANT_B) == "0",
    )
    changed = pg.query(
        "UPDATE narasi_continuity_contracts SET status='rejected' RETURNING id",
        role="app_user", tenant=TENANT_B,
    )
    ev.check("rls", "cross-tenant update affects zero", changed == "", changed)
    ev.expect_failure(
        "grants",
        "contract DELETE privilege absent",
        pg.execute(
            "DELETE FROM narasi_continuity_contracts RETURNING id",
            role="app_user", tenant=TENANT_B, check=False,
        ),
    )

    bad = pg.execute(
        root_contract_sql(contract_id="55555555-5555-4555-8555-555555555556", tenant=TENANT_B,
                          project=PROJECT_B, job=JOB_B),
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("rls", "cross-tenant insert blocked by WITH CHECK", bad)
    bad = pg.execute(
        root_contract_sql(contract_id="55555555-5555-4555-8555-555555555557", tenant=TENANT_A,
                          project=PROJECT_A, job=JOB_B),
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("ownership", "same-tenant envelope cannot cite tenant B job", bad)
    bad = pg.execute(
        root_contract_sql(contract_id="55555555-5555-4555-8555-555555555558", tenant=TENANT_A,
                          project=PROJECT_B, job=JOB_A),
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("ownership", "same-tenant envelope cannot cite tenant B project", bad)

    bad = pg.execute(
        f"UPDATE narasi_continuity_contracts SET story_contract='{{\"changed\":true}}'::jsonb "
        f"WHERE id='{CONTRACT_A}'",
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("contract", "contract payload mutation rejected", bad)
    ev.expect_failure(
        "contract", "slice hash keyset mismatch rejected",
        pg.execute(
            root_contract_sql(contract_id="55555555-5555-4555-8555-555555555560")
            .replace(f'"{CH2}":"{HASH_C}"', f'"{CH1}":"{HASH_C}"'),
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )
    ev.expect_failure(
        "contract", "duplicate ordered chapter IDs rejected",
        pg.execute(
            root_contract_sql(contract_id="55555555-5555-4555-8555-555555555561")
            .replace(f'["{CH1}","{CH2}"]', f'["{CH1}","{CH1}"]'),
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )
    pg.execute(
        f"UPDATE narasi_continuity_contracts SET status='active', generation_started_at=now(), "
        f"updated_at=now() WHERE id='{CONTRACT_A}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check(
        "contract", "validated-to-active transition accepted",
        pg.query(f"SELECT status FROM narasi_continuity_contracts WHERE id='{CONTRACT_A}'",
                 role="app_user", tenant=TENANT_A) == "active",
    )
    bad = pg.execute(
        f"UPDATE narasi_continuity_contracts SET status='validated' WHERE id='{CONTRACT_A}'",
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("contract", "reverse status transition rejected", bad)
    bad = pg.execute(
        root_contract_sql(contract_id="55555555-5555-4555-8555-555555555559", version=2),
        role="app_user", tenant=TENANT_A, check=False,
    )
    ev.expect_failure("contract", "non-root contract requires amendment metadata", bad)
    pg.execute(
        root_contract_sql(contract_id=CONTRACT_A2, version=2, supersedes=CONTRACT_A),
        role="app_user", tenant=TENANT_A,
    )
    ev.check(
        "contract", "valid amendment inserts as new row",
        pg.query("SELECT count(*) FROM narasi_continuity_contracts", role="app_user", tenant=TENANT_A) == "2",
    )
    ev.expect_failure(
        "contract", "amendment cannot name chapter outside contract",
        pg.execute(
            root_contract_sql(
                contract_id="66666666-6666-4666-8666-666666666667",
                version=3,
                supersedes=CONTRACT_A2,
            ).replace(CH2, "ch_ffffffffffffffffffffffffffffffff", 1),
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )

    bad = pg.execute(
        f"BEGIN; DELETE FROM jobs WHERE id='{JOB_A}'; SET CONSTRAINTS ALL IMMEDIATE; COMMIT;",
        check=False,
    )
    ev.expect_failure("deletion", "direct operational-job cleanup is refused", bad)
    ev.check("deletion", "refused job delete preserved job", pg.query(f"SELECT count(*) FROM jobs WHERE id='{JOB_A}'") == "1")


def child_store_tests(pg: LocalPostgres, ev: Evidence) -> None:
    contract_b = "bbbbbbbb-5555-4555-8555-bbbbbbbbbbbb"
    pg.execute(root_contract_sql(contract_id=contract_b, tenant=TENANT_B, project=PROJECT_B, job=JOB_B))
    claim_insert = dedent(
        f"""
        INSERT INTO narasi_continuity_claims (
          id, tenant_id, contract_id, job_id, chapter_id, chapter_content_hash,
          story_contract_hash, extractor_schema_version, extractor_prompt_version,
          extractor_epoch, model_route, claims
        ) VALUES (
          '{CLAIM_A}', '{TENANT_A}', '{CONTRACT_A}', '{JOB_A}', '{CH1}', '{HASH_D}',
          '{HASH_A}', 'extractor-1', 'prompt-1', 1, 'platform:test',
          '[{{\"claim_id\":\"synthetic-1\"}}]'::jsonb
        );
        """
    )
    pg.execute(claim_insert, role="app_user", tenant=TENANT_A)
    ev.check("claims", "valid claim inserted", pg.query("SELECT count(*) FROM narasi_continuity_claims", role="app_user", tenant=TENANT_A) == "1")
    ev.expect_failure("claims", "exact idempotency duplicate rejected", pg.execute(claim_insert.replace(f"'{CLAIM_A}'", "gen_random_uuid()", 1), role="app_user", tenant=TENANT_A, check=False))
    bad = claim_insert.replace(f"'{CLAIM_A}'", "gen_random_uuid()", 1).replace(f"'{CH1}'", "'chapter-1'", 1)
    ev.expect_failure("claims", "unstable chapter ID rejected", pg.execute(bad, role="app_user", tenant=TENANT_A, check=False))
    ev.expect_failure(
        "ownership", "tenant A claim cannot bind tenant B contract/job",
        pg.execute(
            f"INSERT INTO narasi_continuity_claims (tenant_id,contract_id,job_id,chapter_id,"
            f"chapter_content_hash,story_contract_hash,extractor_schema_version,extractor_prompt_version,"
            f"extractor_epoch,model_route,claims) VALUES ('{TENANT_A}','{contract_b}','{JOB_B}','{CH2}',"
            f"'{HASH_D}','{HASH_A}','e','p',2,'route','[]'::jsonb)",
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )
    # The next two checks use direct INSERT...SELECT to avoid relying on text substitutions.
    ev.expect_failure(
        "claims", "claims object rejected",
        pg.execute(
            f"INSERT INTO narasi_continuity_claims (tenant_id,contract_id,job_id,chapter_id,"
            f"chapter_content_hash,story_contract_hash,extractor_schema_version,extractor_prompt_version,"
            f"extractor_epoch,model_route,claims) VALUES ('{TENANT_A}','{CONTRACT_A}','{JOB_A}','{CH2}',"
            f"'{HASH_D}','{HASH_A}','e','p',2,'route','{{}}'::jsonb)",
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )
    ev.expect_failure(
        "claims", "claim retention over 30 days rejected",
        pg.execute(
            f"INSERT INTO narasi_continuity_claims (tenant_id,contract_id,job_id,chapter_id,"
            f"chapter_content_hash,story_contract_hash,extractor_schema_version,extractor_prompt_version,"
            f"extractor_epoch,model_route,claims,created_at,expires_at) VALUES "
            f"('{TENANT_A}','{CONTRACT_A}','{JOB_A}','{CH2}','{HASH_D}','{HASH_A}','e','p',2,'route',"
            f"'[]'::jsonb,now(),now()+interval '31 days')",
            role="app_user", tenant=TENANT_A, check=False,
        ),
    )
    ev.expect_failure(
        "claims", "claims UPDATE privilege absent",
        pg.execute("UPDATE narasi_continuity_claims SET extractor_epoch=3", role="app_user", tenant=TENANT_A, check=False),
    )

    evidence = json.dumps([{"text": "small synthetic span", "start": 0, "end": 20, "source_hash": HASH_D}], separators=(",", ":"))
    violation_insert = dedent(
        f"""
        INSERT INTO narasi_continuity_violations (
          id, tenant_id, contract_id, claim_id, job_id, story_contract_hash, predicate_id,
          predicate_set_version, violation_type, severity, evidence, evidence_expires_at
        ) VALUES (
          '{VIOLATION_A}', '{TENANT_A}', '{CONTRACT_A}', '{CLAIM_A}', '{JOB_A}', '{HASH_A}',
          'occurrence.once', 'predicate-set-1', 'duplicate_occurrence', 'high',
          '{evidence}'::jsonb, now()+interval '30 days'
        );
        """
    )
    pg.execute(violation_insert, role="app_user", tenant=TENANT_A)
    ev.check("violations", "valid bounded violation inserted", pg.query("SELECT count(*) FROM narasi_continuity_violations", role="app_user", tenant=TENANT_A) == "1")
    too_long = json.dumps([{"text": "x" * 501, "start": 0, "end": 501, "source_hash": HASH_D}], separators=(",", ":"))
    ev.expect_failure(
        "violations", "501-character evidence rejected",
        pg.execute(violation_insert.replace(f"'{VIOLATION_A}'", "gen_random_uuid()", 1).replace(f"'{evidence}'::jsonb", f"'{too_long}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    extra = json.dumps([{"text": "x", "start": 0, "end": 1, "source_hash": HASH_D, "prompt": "forbidden"}], separators=(",", ":"))
    ev.expect_failure(
        "violations", "extra evidence key rejected",
        pg.execute(violation_insert.replace(f"'{VIOLATION_A}'", "gen_random_uuid()", 1).replace(f"'{evidence}'::jsonb", f"'{extra}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    bad_offsets = json.dumps([{"text": "x", "start": 2, "end": 1, "source_hash": HASH_D}], separators=(",", ":"))
    ev.expect_failure(
        "violations", "reversed evidence offsets rejected",
        pg.execute(violation_insert.replace(f"'{VIOLATION_A}'", "gen_random_uuid()", 1).replace(f"'{evidence}'::jsonb", f"'{bad_offsets}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    ev.expect_failure(
        "violations", "evidence retention over 30 days rejected",
        pg.execute(violation_insert.replace(f"'{VIOLATION_A}'", "gen_random_uuid()", 1).replace("now()+interval '30 days'", "now()+interval '31 days'"), role="app_user", tenant=TENANT_A, check=False),
    )
    pg.execute(
        f"UPDATE narasi_continuity_violations SET attempt_count=1,resolution_state='repaired',"
        f"resolved_at=now(),updated_at=now() WHERE id='{VIOLATION_A}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check("violations", "valid resolution transition accepted", pg.query(f"SELECT resolution_state FROM narasi_continuity_violations WHERE id='{VIOLATION_A}'", role="app_user", tenant=TENANT_A) == "repaired")
    ev.expect_failure(
        "violations", "attempt count cannot decrease",
        pg.execute(f"UPDATE narasi_continuity_violations SET attempt_count=0 WHERE id='{VIOLATION_A}'", role="app_user", tenant=TENANT_A, check=False),
    )
    ev.expect_failure(
        "violations", "evidence cannot be replaced",
        pg.execute(f"UPDATE narasi_continuity_violations SET evidence='[]'::jsonb WHERE id='{VIOLATION_A}'", role="app_user", tenant=TENANT_A, check=False),
    )
    pg.execute(
        f"UPDATE narasi_continuity_violations SET evidence=NULL,evidence_expires_at=NULL,"
        f"evidence_purged_at=now(),updated_at=now() WHERE id='{VIOLATION_A}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check("violations", "evidence purge-only update accepted", pg.query(f"SELECT evidence IS NULL FROM narasi_continuity_violations WHERE id='{VIOLATION_A}'", role="app_user", tenant=TENANT_A) == "t")

    coverage = json.dumps([
        {"chapter_id": CH1, "predicate_id": "occurrence.once", "state": "complete"},
        {"chapter_id": CH2, "predicate_id": "occurrence.once", "state": "not_applicable"},
    ], separators=(",", ":"))
    coverage_insert = dedent(
        f"""
        INSERT INTO narasi_continuity_coverage (
          id,tenant_id,contract_id,job_id,story_contract_hash,final_candidate_hash,
          predicate_set_version,extractor_schema_version,extractor_prompt_version,
          extractor_epoch,diff_version,coverage,coverage_hash
        ) VALUES (
          '{COVERAGE_A}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}','{HASH_B}',
          'predicate-set-1','extractor-1','prompt-1',1,'diff-1','{coverage}'::jsonb,'{HASH_C}'
        );
        """
    )
    pg.execute(coverage_insert, role="app_user", tenant=TENANT_A)
    ev.check("coverage", "valid coverage inserted", pg.query("SELECT count(*) FROM narasi_continuity_coverage", role="app_user", tenant=TENANT_A) == "1")
    ev.expect_failure("coverage", "coverage idempotency duplicate rejected", pg.execute(coverage_insert.replace(f"'{COVERAGE_A}'", "gen_random_uuid()", 1), role="app_user", tenant=TENANT_A, check=False))
    invalid_coverage = coverage.replace("not_applicable", "clean")
    ev.expect_failure(
        "coverage", "unknown coverage state rejected",
        pg.execute(coverage_insert.replace(f"'{COVERAGE_A}'", "gen_random_uuid()", 1).replace(f"'{HASH_B}'", f"'{HASH_D}'", 1).replace(f"'{coverage}'::jsonb", f"'{invalid_coverage}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    duplicate_coverage = json.dumps([
        {"chapter_id": CH1, "predicate_id": "occurrence.once", "state": "complete"},
        {"chapter_id": CH1, "predicate_id": "occurrence.once", "state": "partial"},
    ], separators=(",", ":"))
    ev.expect_failure(
        "coverage", "duplicate chapter/predicate coverage pair rejected",
        pg.execute(coverage_insert.replace(f"'{COVERAGE_A}'", "gen_random_uuid()", 1).replace(f"'{HASH_B}'", f"'{HASH_D}'", 1).replace(f"'{coverage}'::jsonb", f"'{duplicate_coverage}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    ev.expect_failure(
        "coverage", "coverage UPDATE privilege absent",
        pg.execute("UPDATE narasi_continuity_coverage SET diff_version='changed'", role="app_user", tenant=TENANT_A, check=False),
    )

    audit = json.dumps([{
        "event_id": "12345678-1234-4234-8234-123456789abc",
        "at": "2026-07-22T12:00:00Z", "actor_hash": HASH_D,
        "action": "read", "outcome": "allowed",
    }], separators=(",", ":"))
    recovery_insert = dedent(
        f"""
        INSERT INTO narasi_continuity_recovery_artifacts (
          id,tenant_id,contract_id,job_id,story_contract_hash,final_candidate_hash,
          object_bucket,object_key,object_version,encryption_algorithm,encryption_key_id,
          ciphertext_sha256,ciphertext_size_bytes,terminal_at,expires_at,access_audit
        ) VALUES (
          '{RECOVERY_A}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}','{HASH_B}',
          'narasi-private','tenant-a/job-a/candidate.enc','v1','AES-256-GCM','kms-key-1',
          '{HASH_C}',4096,now(),now()+interval '30 days','{audit}'::jsonb
        );
        """
    )
    pg.execute(recovery_insert, role="app_user", tenant=TENANT_A)
    ev.check("recovery", "valid encrypted-object metadata inserted", pg.query("SELECT count(*) FROM narasi_continuity_recovery_artifacts", role="app_user", tenant=TENANT_A) == "1")
    ev.expect_failure(
        "recovery", "path traversal object key rejected",
        pg.execute(recovery_insert.replace(f"'{RECOVERY_A}'", "gen_random_uuid()", 1).replace("tenant-a/job-a/candidate.enc", "tenant-a/../candidate.enc"), role="app_user", tenant=TENANT_A, check=False),
    )
    ev.expect_failure(
        "recovery", "public URL object key rejected",
        pg.execute(recovery_insert.replace(f"'{RECOVERY_A}'", "gen_random_uuid()", 1).replace("tenant-a/job-a/candidate.enc", "https://example.invalid/candidate.enc"), role="app_user", tenant=TENANT_A, check=False),
    )
    ev.expect_failure(
        "recovery", "recovery retention over 30 days rejected",
        pg.execute(recovery_insert.replace(f"'{RECOVERY_A}'", "gen_random_uuid()", 1).replace("now()+interval '30 days'", "now()+interval '31 days'"), role="app_user", tenant=TENANT_A, check=False),
    )
    invalid_audit = json.dumps([{
        "event_id": "not-a-uuid", "at": "yesterday", "actor_hash": HASH_D,
        "action": "read", "outcome": "allowed",
    }], separators=(",", ":"))
    ev.expect_failure(
        "recovery", "malformed audit record rejected",
        pg.execute(recovery_insert.replace(f"'{RECOVERY_A}'", "gen_random_uuid()", 1).replace(f"'{audit}'::jsonb", f"'{invalid_audit}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    too_many_audits = json.dumps([
        {
            "event_id": f"12345678-1234-4234-8234-{index:012x}",
            "at": "2026-07-22T12:00:00Z",
            "actor_hash": HASH_D,
            "action": "read",
            "outcome": "allowed",
        }
        for index in range(101)
    ], separators=(",", ":"))
    ev.expect_failure(
        "recovery", "101-entry audit rejected",
        pg.execute(recovery_insert.replace(f"'{RECOVERY_A}'", "gen_random_uuid()", 1).replace(f"'{audit}'::jsonb", f"'{too_many_audits}'::jsonb"), role="app_user", tenant=TENANT_A, check=False),
    )
    audit2 = json.dumps({
        "event_id": "22345678-1234-4234-8234-123456789abc",
        "at": "2026-07-22T12:01:00Z", "actor_hash": HASH_C,
        "action": "read", "outcome": "denied",
    }, separators=(",", ":"))
    pg.execute(
        f"UPDATE narasi_continuity_recovery_artifacts SET access_audit=access_audit||'{audit2}'::jsonb,"
        f"updated_at=now() WHERE id='{RECOVERY_A}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check("recovery", "append-only audit accepted", pg.query(f"SELECT jsonb_array_length(access_audit) FROM narasi_continuity_recovery_artifacts WHERE id='{RECOVERY_A}'", role="app_user", tenant=TENANT_A) == "2")
    ev.expect_failure(
        "recovery", "audit replacement rejected",
        pg.execute(f"UPDATE narasi_continuity_recovery_artifacts SET access_audit='[]'::jsonb WHERE id='{RECOVERY_A}'", role="app_user", tenant=TENANT_A, check=False),
    )
    pg.execute(f"UPDATE narasi_continuity_recovery_artifacts SET deleted_at=now(),updated_at=now() WHERE id='{RECOVERY_A}'", role="app_user", tenant=TENANT_A)
    ev.expect_failure(
        "recovery", "deleted_at cannot change after set",
        pg.execute(f"UPDATE narasi_continuity_recovery_artifacts SET deleted_at=now()+interval '1 second' WHERE id='{RECOVERY_A}'", role="app_user", tenant=TENANT_A, check=False),
    )

    for table in TABLES:
        count = pg.query(
            f"SELECT count(*) FROM {table} WHERE tenant_id='{TENANT_A}'",
            role="app_user", tenant=TENANT_B,
        )
        ev.check("rls", f"tenant B cannot read tenant A {table}", count == "0", count)

    pg.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='c03_public_user') THEN CREATE ROLE c03_public_user; END IF; END $$; GRANT USAGE ON SCHEMA public TO c03_public_user;")
    ev.expect_failure(
        "grants", "PUBLIC-derived role cannot select continuity data",
        pg.execute("SELECT count(*) FROM narasi_continuity_contracts", role="c03_public_user", check=False),
    )
    pg.execute(f"DELETE FROM narasi_continuity_contracts WHERE id='{contract_b}'")


def v2_closure_tests(pg: LocalPostgres, ev: Evidence) -> None:
    def reject(name: str, sql: str) -> None:
        result = pg.execute(
            "BEGIN;" + sql + ";ROLLBACK;",
            role="app_user", tenant=TENANT_A, check=False,
        )
        ev.expect_failure("v2_closure", name, result)

    job_a2 = "33333333-3333-4333-8333-333333333334"
    job_a3 = "33333333-3333-4333-8333-333333333335"
    job_a4 = "33333333-3333-4333-8333-333333333336"
    pg.execute(
        f"INSERT INTO jobs(id,tenant_id,job_type,status) VALUES "
        f"('{job_a2}','{TENANT_A}','narasi_derived_input','queued'),"
        f"('{job_a3}','{TENANT_A}','narasi_derived_input','queued'),"
        f"('{job_a4}','{TENANT_A}','narasi_derived_input','queued')"
    )

    self_id = "66666666-6666-4666-8666-666666666668"
    reject(
        "self-superseding contract rejected",
        root_contract_sql(
            contract_id=self_id, job=job_a2, version=2, supersedes=self_id,
        ),
    )
    reject(
        "cross-job supersession rejected",
        root_contract_sql(
            contract_id="66666666-6666-4666-8666-666666666669",
            job=job_a2, version=2, supersedes=CONTRACT_A,
        ),
    )
    reject(
        "skipped contract version rejected",
        root_contract_sql(
            contract_id="66666666-6666-4666-8666-666666666670",
            version=4, supersedes=CONTRACT_A2,
        ),
    )
    reject(
        "amendment target-language drift rejected",
        root_contract_sql(
            contract_id="66666666-6666-4666-8666-666666666671",
            version=3, supersedes=CONTRACT_A2,
        ).replace("'en', '3'", "'id', '3'", 1),
    )
    changed_chapter = "ch_ffffffffffffffffffffffffffffffff"
    changed_identity_sql = root_contract_sql(
        contract_id="66666666-6666-4666-8666-666666666672",
        version=3, supersedes=CONTRACT_A2,
    ).replace(CH2, changed_chapter)
    reject("amendment chapter-identity drift rejected", changed_identity_sql)
    reject(
        "contract cannot be inserted active",
        root_contract_sql(
            contract_id="66666666-6666-4666-8666-666666666673",
            job=job_a2, status="active",
        ),
    )

    timestamp_contract = "66666666-6666-4666-8666-666666666674"
    pg.execute(
        root_contract_sql(contract_id=timestamp_contract, job=job_a3),
        role="app_user", tenant=TENANT_A,
    )
    pg.execute(
        f"UPDATE narasi_continuity_contracts SET status='rejected',"
        f"updated_at='2000-01-01T00:00:00Z' WHERE id='{timestamp_contract}'",
        role="app_user", tenant=TENANT_A,
    )
    refreshed = pg.query(
        f"SELECT (updated_at >= created_at)::text FROM narasi_continuity_contracts "
        f"WHERE id='{timestamp_contract}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check("v2_closure", "mutation guard owns non-regressing updated_at", refreshed == "true", refreshed)

    chapter_ids = [f"ch_{index:032x}" for index in range(1, 514)]
    slice_hashes = {chapter_id: HASH_B for chapter_id in chapter_ids}
    oversized_contract = root_contract_sql(
        contract_id="66666666-6666-4666-8666-666666666675", job=job_a4,
    ).replace(
        json.dumps([CH1, CH2], separators=(",", ":")),
        json.dumps(chapter_ids, separators=(",", ":")),
    ).replace(
        json.dumps({CH1: HASH_B, CH2: HASH_C}, separators=(",", ":")),
        json.dumps(slice_hashes, separators=(",", ":")),
    )
    reject("513-chapter contract rejected", oversized_contract)

    evidence_a = {
        "text": "synthetic", "start": 0, "end": 9, "source_hash": HASH_D,
    }
    evidence_json = json.dumps([evidence_a], separators=(",", ":"))
    violation_id = "88888888-8888-4888-8888-888888888890"
    pg.execute(
        f"INSERT INTO narasi_continuity_violations ("
        f"id,tenant_id,contract_id,job_id,story_contract_hash,predicate_id,predicate_set_version,"
        f"violation_type,severity,evidence,evidence_expires_at) VALUES ("
        f"'{violation_id}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        f"'v2.purge','set-1','type','high','{evidence_json}'::jsonb,now()+interval '30 days')",
        role="app_user", tenant=TENANT_A,
    )
    reject(
        "evidence purge requires purge timestamp",
        f"UPDATE narasi_continuity_violations SET evidence=NULL,evidence_expires_at=NULL "
        f"WHERE id='{violation_id}'",
    )
    reject(
        "evidence expiry cannot be independently rewritten",
        f"UPDATE narasi_continuity_violations SET evidence_expires_at=created_at+interval '29 days' "
        f"WHERE id='{violation_id}'",
    )
    pg.execute(
        f"UPDATE narasi_continuity_violations SET evidence=NULL,evidence_expires_at=NULL,"
        f"evidence_purged_at=now() WHERE id='{violation_id}'",
        role="app_user", tenant=TENANT_A,
    )
    reject(
        "evidence purge timestamp is write-once",
        f"UPDATE narasi_continuity_violations SET evidence_purged_at=now()+interval '1 second' "
        f"WHERE id='{violation_id}'",
    )

    terminal_id = "88888888-8888-4888-8888-888888888891"
    pg.execute(
        f"INSERT INTO narasi_continuity_violations ("
        f"id,tenant_id,contract_id,job_id,story_contract_hash,predicate_id,predicate_set_version,"
        f"violation_type,severity) VALUES ('{terminal_id}','{TENANT_A}','{CONTRACT_A}',"
        f"'{JOB_A}','{HASH_A}','v2.state','set-1','type','high')",
        role="app_user", tenant=TENANT_A,
    )
    pg.execute(
        f"UPDATE narasi_continuity_violations SET attempt_count=1,resolution_state='unresolved',"
        f"resolved_at=now() WHERE id='{terminal_id}'",
        role="app_user", tenant=TENANT_A,
    )
    reject(
        "terminal violation state cannot change",
        f"UPDATE narasi_continuity_violations SET resolution_state='dismissed' "
        f"WHERE id='{terminal_id}'",
    )

    oversized_evidence = json.dumps(
        [
            {"text": "x", "start": index, "end": index + 1, "source_hash": HASH_D}
            for index in range(101)
        ],
        separators=(",", ":"),
    )
    reject(
        "101-item violation evidence rejected",
        f"INSERT INTO narasi_continuity_violations (tenant_id,contract_id,job_id,"
        f"story_contract_hash,predicate_id,predicate_set_version,violation_type,severity,evidence,"
        f"evidence_expires_at) VALUES ('{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        f"'v2.bound','set-1','type','high','{oversized_evidence}'::jsonb,"
        f"now()+interval '30 days')",
    )

    oversized_coverage = json.dumps(
        [
            {"chapter_id": CH1, "predicate_id": f"predicate-{index}", "state": "complete"}
            for index in range(10001)
        ],
        separators=(",", ":"),
    )
    reject(
        "10001-item coverage rejected",
        f"INSERT INTO narasi_continuity_coverage (tenant_id,contract_id,job_id,story_contract_hash,"
        f"final_candidate_hash,predicate_set_version,extractor_schema_version,"
        f"extractor_prompt_version,extractor_epoch,diff_version,coverage,coverage_hash) VALUES ("
        f"'{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}','{'e' * 64}','v2-large','e','p',1,"
        f"'d','{oversized_coverage}'::jsonb,'{'f' * 64}')",
    )

    audit_a = {
        "event_id": "12345678-1234-4234-8234-123456789abc",
        "at": "2026-07-22T12:00:00Z", "actor_hash": HASH_D,
        "action": "read", "outcome": "allowed",
    }
    audit_b = {
        "event_id": "22345678-1234-4234-8234-123456789abc",
        "at": "2026-07-22T12:01:00Z", "actor_hash": HASH_C,
        "action": "read", "outcome": "denied",
    }
    audit_c = {
        "event_id": "32345678-1234-4234-8234-123456789abc",
        "at": "2026-07-22T12:02:00Z", "actor_hash": HASH_B,
        "action": "read", "outcome": "allowed",
    }
    audit_ab = json.dumps([audit_a, audit_b], separators=(",", ":"))
    recovery_id = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaab"
    recovery_insert = (
        f"INSERT INTO narasi_continuity_recovery_artifacts (id,tenant_id,contract_id,job_id,"
        f"story_contract_hash,final_candidate_hash,object_bucket,object_key,object_version,"
        f"encryption_algorithm,encryption_key_id,ciphertext_sha256,ciphertext_size_bytes,"
        f"terminal_at,expires_at,access_audit) VALUES ('{recovery_id}','{TENANT_A}',"
        f"'{CONTRACT_A}','{JOB_A}','{HASH_A}','{HASH_B}','narasi-private',"
        f"'tenant/job/v2.enc','v1','AES-256-GCM','kms-key','{HASH_C}',4096,now(),"
        f"now()+interval '30 days','{audit_ab}'::jsonb)"
    )
    pg.execute(recovery_insert, role="app_user", tenant=TENANT_A)
    audit_ba = json.dumps([audit_b, audit_a], separators=(",", ":"))
    audit_cab = json.dumps([audit_c, audit_a, audit_b], separators=(",", ":"))
    audit_duplicate = json.dumps([audit_a, audit_b, audit_a], separators=(",", ":"))
    reject(
        "access audit reorder rejected",
        f"UPDATE narasi_continuity_recovery_artifacts SET access_audit='{audit_ba}'::jsonb "
        f"WHERE id='{recovery_id}'",
    )
    reject(
        "access audit prepend rejected",
        f"UPDATE narasi_continuity_recovery_artifacts SET access_audit='{audit_cab}'::jsonb "
        f"WHERE id='{recovery_id}'",
    )
    reject(
        "duplicate access-audit event ID rejected",
        f"UPDATE narasi_continuity_recovery_artifacts SET access_audit='{audit_duplicate}'::jsonb "
        f"WHERE id='{recovery_id}'",
    )
    audit_valid_append = json.dumps(audit_c, separators=(",", ":"))
    pg.execute(
        f"UPDATE narasi_continuity_recovery_artifacts SET access_audit=access_audit||"
        f"'{audit_valid_append}'::jsonb WHERE id='{recovery_id}'",
        role="app_user", tenant=TENANT_A,
    )
    ev.check(
        "v2_closure", "valid audit append remains supported",
        pg.query(
            f"SELECT jsonb_array_length(access_audit) FROM "
            f"narasi_continuity_recovery_artifacts WHERE id='{recovery_id}'",
            role="app_user", tenant=TENANT_A,
        ) == "3",
    )

    noncanonical = dict(audit_a)
    noncanonical["event_id"] = "42345678-1234-4234-8234-123456789abc"
    noncanonical["at"] = "2026-07-22 12:00:00+00"
    noncanonical_json = json.dumps([noncanonical], separators=(",", ":"))
    reject(
        "noncanonical audit timestamp rejected",
        recovery_insert.replace(recovery_id, "aaaaaaaa-1111-4111-8111-aaaaaaaaaaac", 1)
        .replace(audit_ab, noncanonical_json, 1),
    )
    reject(
        "blank recovery bucket rejected",
        recovery_insert.replace(recovery_id, "aaaaaaaa-1111-4111-8111-aaaaaaaaaaad", 1)
        .replace("'narasi-private'", "'   '", 1),
    )
    reject(
        "blank encryption metadata rejected",
        recovery_insert.replace(recovery_id, "aaaaaaaa-1111-4111-8111-aaaaaaaaaaae", 1)
        .replace("'AES-256-GCM','kms-key'", "'   ','   '", 1),
    )
    reject(
        "trailing-slash object key rejected",
        recovery_insert.replace(recovery_id, "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaf", 1)
        .replace("tenant/job/v2.enc", "tenant/job/", 1),
    )


def v3_closure_tests(pg: LocalPostgres, ev: Evidence) -> None:
    def reject(name: str, sql: str) -> None:
        ev.expect_failure(
            "v3_closure",
            name,
            pg.execute(sql, role="app_user", tenant=TENANT_A, check=False),
        )

    terminal_insert = (
        "INSERT INTO narasi_continuity_violations ("
        "tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity,resolution_state,resolved_at"
        ") VALUES ("
        f"'{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        "'v3.initial-terminal','set-v3','type','high','repaired',now())"
    )
    reject("terminal violation INSERT rejected", terminal_insert)
    reject(
        "nonzero initial attempt count rejected",
        "INSERT INTO narasi_continuity_violations ("
        "tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity,attempt_count"
        ") VALUES ("
        f"'{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        "'v3.initial-attempt','set-v3','type','high',1)",
    )

    evidence = json.dumps(
        [{"text": "x", "start": 0, "end": 1, "source_hash": HASH_D}],
        separators=(",", ":"),
    )
    reject(
        "live evidence cannot coexist with purge timestamp on INSERT",
        "INSERT INTO narasi_continuity_violations ("
        "tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity,evidence,evidence_expires_at,"
        "evidence_purged_at"
        ") VALUES ("
        f"'{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        f"'v3.initial-purge-live','set-v3','type','high','{evidence}'::jsonb,"
        "now()+interval '1 day',now())",
    )
    reject(
        "purge timestamp cannot be pre-seeded without evidence",
        "INSERT INTO narasi_continuity_violations ("
        "tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity,evidence_purged_at"
        ") VALUES ("
        f"'{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        "'v3.initial-purge-null','set-v3','type','high',now())",
    )

    terminal_id = "88888888-8888-4888-8888-888888888893"
    pg.execute(
        "INSERT INTO narasi_continuity_violations ("
        "id,tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity"
        ") VALUES ("
        f"'{terminal_id}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        "'v3.terminal-attempt','set-v3','type','high')",
        role="app_user",
        tenant=TENANT_A,
    )
    pg.execute(
        f"UPDATE narasi_continuity_violations SET attempt_count=1,"
        f"resolution_state='unresolved',resolved_at=now() WHERE id='{terminal_id}'",
        role="app_user",
        tenant=TENANT_A,
    )
    ev.check(
        "v3_closure",
        "open violation can transition once with final attempt count",
        pg.query(
            f"SELECT attempt_count=1 AND resolution_state='unresolved' "
            f"FROM narasi_continuity_violations WHERE id='{terminal_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "t",
    )
    reject(
        "terminal violation attempt count is immutable",
        f"UPDATE narasi_continuity_violations SET attempt_count=attempt_count+1 "
        f"WHERE id='{terminal_id}'",
    )

    claim_id = "77777777-7777-4777-8777-777777777779"
    linked_violation_id = "88888888-8888-4888-8888-888888888894"
    pg.execute(
        "INSERT INTO narasi_continuity_claims ("
        "id,tenant_id,contract_id,job_id,chapter_id,chapter_content_hash,"
        "story_contract_hash,extractor_schema_version,extractor_prompt_version,"
        "extractor_epoch,model_route,claims"
        ") VALUES ("
        f"'{claim_id}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{CH1}',"
        f"'{HASH_D}','{HASH_A}','e-v3','p-v3',313,'route-v3','[]'::jsonb)",
        role="app_user",
        tenant=TENANT_A,
    )
    pg.execute(
        "INSERT INTO narasi_continuity_violations ("
        "id,tenant_id,contract_id,claim_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity"
        ") VALUES ("
        f"'{linked_violation_id}','{TENANT_A}','{CONTRACT_A}','{claim_id}',"
        f"'{JOB_A}','{HASH_A}','v3.claim-retention','set-v3','type','medium')",
        role="app_user",
        tenant=TENANT_A,
    )
    reject(
        "caller cannot manually clear a live claim binding",
        f"UPDATE narasi_continuity_violations SET claim_id=NULL "
        f"WHERE id='{linked_violation_id}'",
    )
    pg.execute(
        f"DELETE FROM narasi_continuity_claims WHERE id='{claim_id}'",
        role="app_user",
        tenant=TENANT_A,
    )
    ev.check(
        "v3_closure",
        "referenced claim retention delete removes the claim",
        pg.query(
            f"SELECT count(*) FROM narasi_continuity_claims WHERE id='{claim_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "0",
    )
    ev.check(
        "v3_closure",
        "claim retention preserves violation tenant and nulls only claim_id",
        pg.query(
            f"SELECT tenant_id='{TENANT_A}'::uuid AND claim_id IS NULL "
            f"FROM narasi_continuity_violations WHERE id='{linked_violation_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "t",
    )

    def recovery_sql(
        row_id: str,
        *,
        bucket: str = "'private-v3'",
        version: str = "'v3'",
        algorithm: str = "'AES-256-GCM'",
        key_id: str = "'kms-v3'",
        audit: str = "'[]'::jsonb",
    ) -> str:
        return (
            "INSERT INTO narasi_continuity_recovery_artifacts ("
            "id,tenant_id,contract_id,job_id,story_contract_hash,final_candidate_hash,"
            "object_bucket,object_key,object_version,encryption_algorithm,encryption_key_id,"
            "ciphertext_sha256,ciphertext_size_bytes,terminal_at,expires_at,access_audit"
            ") VALUES ("
            f"'{row_id}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}','{HASH_B}',"
            f"{bucket},'job/{row_id}.enc',{version},{algorithm},{key_id},'{HASH_C}',1,"
            f"now(),now()+interval '1 day',{audit})"
        )

    def audit_at(event_id: str, at: str) -> str:
        value = [{
            "event_id": event_id,
            "at": at,
            "actor_hash": HASH_D,
            "action": "read",
            "outcome": "allowed",
        }]
        return "'" + json.dumps(value, separators=(",", ":")) + "'::jsonb"

    reject(
        "24-hour parser normalization is not canonical RFC3339",
        recovery_sql(
            "aaaaaaaa-2222-4222-8222-aaaaaaaaaaa1",
            audit=audit_at(
                "52345678-1234-4234-8234-123456789abc",
                "2026-07-22T24:00:00Z",
            ),
        ),
    )
    reject(
        "overflowing audit minute is rejected",
        recovery_sql(
            "aaaaaaaa-2222-4222-8222-aaaaaaaaaaa2",
            audit=audit_at(
                "62345678-1234-4234-8234-123456789abc",
                "2026-07-22T12:60:00Z",
            ),
        ),
    )
    reject(
        "tab-only recovery bucket rejected",
        recovery_sql("aaaaaaaa-2222-4222-8222-aaaaaaaaaaa3", bucket="E'\\t'"),
    )
    reject(
        "leading-tab recovery bucket rejected",
        recovery_sql("aaaaaaaa-2222-4222-8222-aaaaaaaaaaa4", bucket="E'\\tprivate'"),
    )
    reject(
        "trailing-newline object version rejected",
        recovery_sql("aaaaaaaa-2222-4222-8222-aaaaaaaaaaa5", version="E'v3\\n'"),
    )
    reject(
        "newline-only encryption algorithm rejected",
        recovery_sql("aaaaaaaa-2222-4222-8222-aaaaaaaaaaa6", algorithm="E'\\n'"),
    )
    reject(
        "tab-only encryption key ID rejected",
        recovery_sql("aaaaaaaa-2222-4222-8222-aaaaaaaaaaa7", key_id="E'\\t'"),
    )
    canonical_id = "aaaaaaaa-2222-4222-8222-aaaaaaaaaaa8"
    pg.execute(
        recovery_sql(
            canonical_id,
            audit=audit_at(
                "72345678-1234-4234-8234-123456789abc",
                "2026-07-22T23:59:59.123Z",
            ),
        ),
        role="app_user",
        tenant=TENANT_A,
    )
    ev.check(
        "v3_closure",
        "canonical upper-bound audit time with fraction remains valid",
        pg.query(
            f"SELECT count(*) FROM narasi_continuity_recovery_artifacts "
            f"WHERE id='{canonical_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "1",
    )


def v4_closure_tests(pg: LocalPostgres, ev: Evidence) -> None:
    job_id = "33333333-3333-4333-8333-333333333339"
    contract_id = "55555555-5555-4555-8555-555555555559"
    pg.execute(
        f"INSERT INTO jobs (id,tenant_id,job_type,status) VALUES ("
        f"'{job_id}','{TENANT_A}','narasi_derived_input','queued')"
    )
    pg.execute(
        "INSERT INTO narasi_continuity_contracts ("
        "id,tenant_id,project_id,job_id,contract_version,status,affected_chapter_ids,"
        "revalidation_plan,story_contract,story_contract_hash,target_language,"
        "contract_schema_version,contract_prompt_version,compiler_version,slicer_version,"
        "ordered_chapter_ids,chapter_slice_hashes,updated_at"
        ") VALUES ("
        f"'{contract_id}','{TENANT_A}','{PROJECT_A}','{job_id}',1,'validated',"
        f"'[]'::jsonb,'{{}}'::jsonb,'{{}}'::jsonb,'{HASH_A}','en','3','p','c','s',"
        f"'[\"{CH1}\"]'::jsonb,'{{\"{CH1}\":\"{HASH_B}\"}}'::jsonb,"
        "'2000-01-01T00:00:00Z')",
        role="app_user",
        tenant=TENANT_A,
    )
    ev.check(
        "v4_closure",
        "contract INSERT guard owns updated_at",
        pg.query(
            f"SELECT updated_at >= created_at FROM narasi_continuity_contracts "
            f"WHERE id='{contract_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "t",
    )

    violation_id = "88888888-8888-4888-8888-888888888895"
    pg.execute(
        "INSERT INTO narasi_continuity_violations ("
        "id,tenant_id,contract_id,job_id,story_contract_hash,predicate_id,"
        "predicate_set_version,violation_type,severity,updated_at"
        ") VALUES ("
        f"'{violation_id}','{TENANT_A}','{CONTRACT_A}','{JOB_A}','{HASH_A}',"
        "'v4.updated-at','set-v4','type','high','2000-01-01T00:00:00Z')",
        role="app_user",
        tenant=TENANT_A,
    )
    ev.check(
        "v4_closure",
        "violation INSERT guard owns updated_at",
        pg.query(
            f"SELECT updated_at >= created_at FROM narasi_continuity_violations "
            f"WHERE id='{violation_id}'",
            role="app_user",
            tenant=TENANT_A,
        ) == "t",
    )


def cascade_and_rollback_tests(
    pg: LocalPostgres, ev: Evidence, parent_fingerprint_before: str
) -> None:
    # Rollback must refuse while tenant A rows exist and leave them untouched.
    retained_contract_count = pg.query("SELECT count(*) FROM narasi_continuity_contracts")
    refusal = pg.file(ROOT / "rollback_c03.sql", check=False)
    ev.expect_failure("rollback", "rollback refuses retained rows", refusal, "rollback refused")
    ev.check("rollback", "rollback refusal preserved five tables", pg.query("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'narasi_continuity_%'") == "5")
    ev.check(
        "rollback", "rollback refusal preserved contract rows",
        pg.query("SELECT count(*) FROM narasi_continuity_contracts") == retained_contract_count,
        retained_contract_count,
    )

    # A linked project cascade must clear every child but preserve the operational job.
    tenant_c = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    project_c = "cccccccc-1111-4111-8111-cccccccccccc"
    job_c = "cccccccc-2222-4222-8222-cccccccccccc"
    contract_c = "cccccccc-3333-4333-8333-cccccccccccc"
    pg.execute(
        f"INSERT INTO tenants(id,name,slug,email) VALUES"
        f"('{tenant_c}','Tenant C','c03-tenant-c','c03-c@example.invalid');"
        f"INSERT INTO projects(id,tenant_id,name) VALUES('{project_c}','{tenant_c}','Project C');"
        f"INSERT INTO jobs(id,tenant_id,job_type,status) VALUES('{job_c}','{tenant_c}','narasi_derived_input','queued');"
    )
    pg.execute(root_contract_sql(contract_id=contract_c, tenant=tenant_c, project=project_c, job=job_c))
    pg.execute(
        f"INSERT INTO narasi_continuity_claims(tenant_id,contract_id,job_id,chapter_id,"
        f"chapter_content_hash,story_contract_hash,extractor_schema_version,extractor_prompt_version,"
        f"extractor_epoch,model_route,claims) VALUES('{tenant_c}','{contract_c}','{job_c}','{CH1}',"
        f"'{HASH_D}','{HASH_A}','e','p',1,'route','[]')"
    )
    pg.execute(f"DELETE FROM projects WHERE id='{project_c}'")
    ev.check("deletion", "project cascade removes contract", pg.query(f"SELECT count(*) FROM narasi_continuity_contracts WHERE tenant_id='{tenant_c}'") == "0")
    ev.check("deletion", "project cascade removes child rows", pg.query(f"SELECT count(*) FROM narasi_continuity_claims WHERE tenant_id='{tenant_c}'") == "0")
    ev.check("deletion", "project cascade preserves operational job", pg.query(f"SELECT count(*) FROM jobs WHERE id='{job_c}'") == "1")

    # Tenant deletion must succeed despite the deferred direct-job protection.
    pg.execute(f"DELETE FROM tenants WHERE id='{TENANT_A}'")
    ev.check("deletion", "tenant cascade removes job", pg.query(f"SELECT count(*) FROM jobs WHERE id='{JOB_A}'") == "0")
    for table in TABLES:
        ev.check("deletion", f"tenant cascade leaves no tenant A orphan in {table}", pg.query(f"SELECT count(*) FROM {table} WHERE tenant_id='{TENANT_A}'") == "0")
    pg.execute(f"DELETE FROM tenants WHERE id='{tenant_c}'")

    # All stores are now empty; rollback may remove only C-03 objects.
    exact_total = pg.query(
        "SELECT (SELECT count(*) FROM narasi_continuity_contracts) + "
        "(SELECT count(*) FROM narasi_continuity_claims) + "
        "(SELECT count(*) FROM narasi_continuity_violations) + "
        "(SELECT count(*) FROM narasi_continuity_coverage) + "
        "(SELECT count(*) FROM narasi_continuity_recovery_artifacts)"
    )
    ev.check("rollback", "all stores empty before clean rollback", exact_total == "0", exact_total)
    pg.file(ROOT / "rollback_c03.sql")
    ev.check("rollback", "clean rollback removes all continuity tables", pg.query("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'narasi_continuity_%'") == "0")
    ev.check("rollback", "clean rollback removes C-03 functions", pg.query("SELECT count(*) FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname LIKE 'narasi_c03_%'") == "0")
    ev.check("rollback", "clean rollback removes parent indexes", pg.query("SELECT count(*) FROM pg_indexes WHERE indexname IN ('uq_c03_jobs_tenant_id_id','uq_c03_projects_tenant_id_id')") == "0")
    ev.check("rollback", "parent tables remain", pg.query("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename IN ('tenants','jobs','projects')") == "3")
    ev.check("rollback", "app_user remains NOBYPASSRLS", pg.query("SELECT (NOT rolbypassrls AND NOT rolsuper)::text FROM pg_roles WHERE rolname='app_user'") == "true")
    ev.check("rollback", "unrelated tenant B remains", pg.query(f"SELECT count(*) FROM tenants WHERE id='{TENANT_B}'") == "1")
    ev.check(
        "rollback", "pre-C-03 parent schema restored byte-equivalently",
        parent_schema_fingerprint(pg) == parent_fingerprint_before,
    )


def run_c01(worktree: Path, ev: Evidence) -> None:
    if not C01_RUNNER.is_file() or C01_RUNNER.is_symlink():
        raise AssertionError(f"missing immutable C-01 V2 runner: {C01_RUNNER}")
    result = run(
        [
            sys.executable, str(C01_RUNNER),
            "--backend-worktree", str(worktree),
            "--frontend-worktree", "/Users/rino/Documents/cerita-ai-studio",
        ],
        cwd=worktree,
    )
    output = (result.stdout or "") + (result.stderr or "")
    ev.check("regression", "inherited C-01 V2 runner passes", C01_FINAL in output, output[-2000:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-worktree", required=True)
    args = parser.parse_args()
    worktree = Path(args.backend_worktree).resolve()
    ev = Evidence()

    verify_pack()
    verify_baseline(worktree, candidate_required=True)
    candidate = worktree / CANDIDATE
    verify_candidate_static(candidate)
    status_before = git(worktree, "status", "--porcelain=v1", "-z", text=False).stdout
    candidate_hash = sha_file(candidate)

    with local_postgres(ev) as pg:
        migration_count = apply_pre_c03_chain(pg, worktree, ev)
        parent_fingerprint_before = parent_schema_fingerprint(pg)
        pg.file(candidate)
        ev.check("migration", "0072 first application", True)
        pg.file(candidate)
        ev.check("migration", "0072 second idempotent application", True)
        catalog_tests(pg, ev)
        rls_and_contract_tests(pg, ev)
        child_store_tests(pg, ev)
        v2_closure_tests(pg, ev)
        v3_closure_tests(pg, ev)
        v4_closure_tests(pg, ev)
        cascade_and_rollback_tests(pg, ev, parent_fingerprint_before)

    run_c01(worktree, ev)
    verify_baseline(worktree, candidate_required=True)
    if sha_file(candidate) != candidate_hash:
        raise AssertionError("0072 changed during acceptance run")
    status_after = git(worktree, "status", "--porcelain=v1", "-z", text=False).stdout
    if status_after != status_before:
        raise AssertionError("worktree status changed during C-03 acceptance run")
    if git(worktree, "diff", "--check").stdout.strip():
        raise AssertionError("git diff --check failed after acceptance")

    totals = ", ".join(f"{group}={count}" for group, count in sorted(ev.groups.items()))
    print(f"C-03 evidence: migrations={migration_count}+2; {totals}")
    print(f"C-03 candidate sha256: {candidate_hash}")
    print("C-03 V4 SCHEMA/RLS ACCEPTANCE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
