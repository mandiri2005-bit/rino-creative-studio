"""
Dalang v2 — Slice 2 crash-safe billing (hermetic).

Covers §5 (H4/H5): op_id + delivered-cost checkpoint on the jobs row, persist-then-bill
reorder, and the narasi orphan-hold sweep that COMMITS delivered cost (not refund-whole,
so a crash no longer delivers chapters for free) or refunds when nothing was delivered.

The money-critical piece — _sweep_orphaned_narasi_jobs — is exercised behaviorally with
mocked db + credits. The SQL/reorder/lifespan wiring is locked with source inspection
(matching the repo's migration-test convention), since it needs a real DB to run live.

Flag: DALANG_CRASHSAFE_ENABLED (default OFF ⟹ billing order byte-identical, sweep unregistered).
"""
import asyncio
import inspect
import os
import pytest

import laozhang_api
import database


MIG = os.path.join(os.path.dirname(__file__),
                   "../../database/migrations/0054_narasi_jobs_sweep_stale.sql")


class TestCrashsafeFlag:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("DALANG_CRASHSAFE_ENABLED", raising=False)
        assert laozhang_api._dalang_crashsafe_enabled() is False

    def test_on(self, monkeypatch):
        monkeypatch.setenv("DALANG_CRASHSAFE_ENABLED", "1")
        assert laozhang_api._dalang_crashsafe_enabled() is True


class TestSweepReconcile:
    """The sweep must COMMIT delivered cost (fixes H4), refund only when none, and skip
    rows without a checkpoint op_id. commit/refund idempotency lives in credits.py."""

    def _run(self, monkeypatch, stale_rows):
        calls = {"commit": [], "refund": []}

        async def fake_sweep(secs):
            return stale_rows

        async def fake_commit(tid, op, act, **kw):
            calls["commit"].append((tid, op, act, kw.get("user_id"), kw.get("metadata")))
            return 0

        async def fake_refund(tid, op, **kw):
            calls["refund"].append((tid, op))
            return 0

        monkeypatch.setattr(laozhang_api.db, "sweep_stale_narasi_jobs", fake_sweep)
        monkeypatch.setattr(laozhang_api.credits_lib, "commit", fake_commit)
        monkeypatch.setattr(laozhang_api.credits_lib, "refund", fake_refund)
        n = asyncio.run(laozhang_api._sweep_orphaned_narasi_jobs())
        return n, calls

    def test_commits_delivered_with_user_attribution(self, monkeypatch):
        n, calls = self._run(monkeypatch, [
            {"tenant_id": "t1", "op_id": "narasi:abc:1", "meter_actual": 50, "user_id": "u1"}])
        assert n == 1
        assert len(calls["commit"]) == 1
        tid, op, act, usr, meta = calls["commit"][0]
        assert (tid, op, act, usr) == ("t1", "narasi:abc:1", 50, "u1")
        assert meta and meta.get("swept") is True   # ledger row tagged swept + attributed
        assert calls["refund"] == []

    def test_refunds_when_nothing_delivered(self, monkeypatch):
        n, calls = self._run(monkeypatch, [
            {"tenant_id": "t1", "op_id": "narasi:abc:1", "meter_actual": 0}])
        assert calls["refund"] == [("t1", "narasi:abc:1")]
        assert calls["commit"] == []

    def test_skips_row_without_op_id(self, monkeypatch):
        n, calls = self._run(monkeypatch, [
            {"tenant_id": "t1", "op_id": None, "meter_actual": 50}])
        assert calls["commit"] == [] and calls["refund"] == []

    def test_mixed_batch(self, monkeypatch):
        n, calls = self._run(monkeypatch, [
            {"tenant_id": "t1", "op_id": "op1", "meter_actual": 30},
            {"tenant_id": "t2", "op_id": "op2", "meter_actual": 0},
            {"tenant_id": "t3", "op_id": None, "meter_actual": 99},
        ])
        assert n == 3
        assert [(c[0], c[1], c[2]) for c in calls["commit"]] == [("t1", "op1", 30)]
        assert calls["refund"] == [("t2", "op2")]

    def test_settle_error_does_not_abort_batch(self, monkeypatch):
        """A failed settle on one orphan must not stop the rest (best-effort loop)."""
        seen = []

        async def fake_sweep(secs):
            return [{"tenant_id": "t1", "op_id": "op1", "meter_actual": 10},
                    {"tenant_id": "t2", "op_id": "op2", "meter_actual": 20}]

        async def boom_commit(tid, op, act, **kw):
            seen.append(op)
            if op == "op1":
                raise RuntimeError("redis down")
            return 0

        async def fake_refund(tid, op, **kw):
            return 0

        monkeypatch.setattr(laozhang_api.db, "sweep_stale_narasi_jobs", fake_sweep)
        monkeypatch.setattr(laozhang_api.credits_lib, "commit", boom_commit)
        monkeypatch.setattr(laozhang_api.credits_lib, "refund", fake_refund)
        n = asyncio.run(laozhang_api._sweep_orphaned_narasi_jobs())
        assert n == 2
        assert seen == ["op1", "op2"]   # op2 still attempted after op1 raised


class TestDbLayerSource:
    def test_create_stamps_op_id_into_meter(self):
        src = inspect.getsource(database.create_narasi_job)
        assert "input_payload" in src
        assert "_meter" in src and "op_id" in src
        # byte-identical guarantee: only stamps when op_id given
        assert 'if op_id else None' in src

    def test_checkpoint_is_atomic_and_per_run(self):
        src = inspect.getsource(database.save_narasi_chapter)
        assert "meter_checkpoint" in src
        assert "jsonb_set" in src and "_meter,actual" in src
        assert "conn.transaction()" in src        # same txn as the chapter upsert (atomic)
        assert "WHERE id=$1" in src               # scoped to THIS run's jobs.id, not external id
        assert "input_payload ? '_meter'" in src  # never fabricates a checkpoint when off

    def test_checkpoint_helper_is_per_run_scoped(self):
        # Per-chapter checkpoint stays folded/atomic in save_narasi_chapter; the standalone
        # helper exists ONLY for post-loop cost (Slice-4 book critic) and is scoped by jobs.id,
        # never the shared external_id (per-run correctness).
        import inspect as _i
        assert hasattr(database, "checkpoint_narasi_meter")
        src = _i.getsource(database.checkpoint_narasi_meter)
        assert "WHERE id=$1" in src and "input_payload ? '_meter'" in src
        assert "external_job_id" not in src

    def test_sweep_wraps_security_definer_fn_cross_tenant(self):
        src = inspect.getsource(database.sweep_stale_narasi_jobs)
        assert "narasi_jobs_sweep_stale" in src
        assert "user_id" in src     # returns user_id for ledger attribution
        assert 'tenant=""' in src   # cross-tenant read (no per-tenant scoping)


class TestMigration0054:
    def _sql(self):
        return open(MIG).read()

    def test_present_security_definer(self):
        s = self._sql()
        assert "CREATE OR REPLACE FUNCTION narasi_jobs_sweep_stale" in s
        assert "SECURITY DEFINER" in s
        assert "SET search_path = public" in s

    def test_grants_locked_to_app_user(self):
        s = self._sql()
        assert "REVOKE ALL ON FUNCTION narasi_jobs_sweep_stale(interval) FROM PUBLIC" in s
        assert "GRANT EXECUTE ON FUNCTION narasi_jobs_sweep_stale(interval) TO app_user" in s

    def test_only_checkpointed_narasi_rows(self):
        s = self._sql()
        # text compare (not the enum literal): prod's enum has 'narasi' but the migration
        # chain never ADDs it, so the enum cast would fail CREATE FUNCTION on a fresh DB.
        assert "job_type::text = 'narasi'" in s
        assert "->> 'op_id') IS NOT NULL" in s   # never touches non-checkpointed jobs
        assert "RETURNING" in s and "meter_actual" in s
        assert "jobs.user_id" in s               # attribution for the swept commit

    def test_next_free_number_not_0053(self):
        # 0053 was coordinated to the concurrent atlascloud hotfix; Slice 2 owns 0054.
        d = os.path.dirname(MIG)
        assert os.path.exists(os.path.join(d, "0054_narasi_jobs_sweep_stale.sql"))


class TestReorderAndLifespanSource:
    def test_persist_then_bill_gated(self):
        src = inspect.getsource(laozhang_api._narasi_generate_impl)
        assert "_chap_cr" in src                              # staged, not billed inline
        assert "if not _dalang_crashsafe_enabled():" in src   # OFF ⟹ legacy order
        assert "meter_checkpoint=" in src                     # ON ⟹ atomic bill+checkpoint after persist

    def test_lifespan_registers_sweep_under_flag(self):
        src = inspect.getsource(laozhang_api)
        assert "asyncio.create_task(_narasi_jobs_sweep_loop())" in src
        assert "_narasi_sweep_task" in src   # present + added to the shutdown-cancel tuple
