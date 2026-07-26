"""
B-07/B-08 (Rework Contract I): product-owned behavioral repository tests.

These tests call the REAL functions (database.create_narasi_job, narration_api.
narration_start/_run_narration_job/_persist_chapters/narration_status, narration_worker.
_process, laozhang_api.narasi_generate/narasi_stitch/narasi_review/oneshot_fix_submit/
narasi_save_edit/narasi_status/_narasi_resolve_manuscript_lineage/_narasi_build_lineage_binding)
with the db/redis/metering/provider boundary mocked via monkeypatch -- never source-string
inspection, never mocking away the function under test itself, never live infrastructure.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import database
import laozhang_api
import narration_api
import narration_worker
from auth_middleware import CurrentUser, get_current_user
from continuity.lifecycle import admit_outline_lifecycle, build_job_lifecycle


_TENANT = "99999999-9999-4999-8999-999999999999"
_UID = "88888888-8888-4888-8888-888888888888"

# B-07/B-08 Rework 5: same real-node-execution technique the acceptance packs use for
# narasiLifecycle.mjs -- genuine JS execution, never source-text inspection, for the pure
# frontend validation module. Standalone-runnable (falls back to the known repo path) so
# this file doesn't require B0708_FRONTEND when run outside the full acceptance runner.
_FRONTEND_DIR = Path(os.environ.get("B0708_FRONTEND", "/Users/rino/Documents/cerita-ai-studio")).resolve()


def node_eval(expression: str):
    module = _FRONTEND_DIR / "src/narasiLifecycle.mjs"
    script = (
        f'import * as m from {json.dumps(module.as_uri())};'
        f'const result=await ({expression});process.stdout.write(JSON.stringify(result));'
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=_FRONTEND_DIR,
        text=True, capture_output=True, timeout=10, check=False,
        env={**os.environ, "NO_PROXY": "*", "no_proxy": "*"},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# B-07/B-08 Rework 6 (V7 rows 7-9): reads the frontend's OWN literal source text -- these
# 3 rows are genuinely about wiring (does main.jsx call the real hook, is a key/value pair
# UUID-shaped) rather than runtime behavior a mocked call could exercise, mirroring the V7
# pack's own `frontend()` helper exactly.
def _frontend_text(relpath):
    return (_FRONTEND_DIR / relpath).read_text(encoding="utf-8")


# B-07/B-08 Rework 6 (V7 row 17): the pack's own real-Chrome mounted-transport runner --
# this is pack-provided tooling (a headless-Chrome + real Vite harness), not app source, so
# it is referenced from the pack directory exactly like node_eval references the frontend
# repo. Standalone-runnable via the same env-fallback convention as _FRONTEND_DIR above.
_PACK_DIR = Path(os.environ.get(
    "B0708_V7_PACK",
    "/Users/rino/docs/IMPORTANT-wimba-narasi-b07-b08-lifecycle-acceptance-v7")).resolve()


def _user(tenant=_TENANT, uid=_UID):
    return CurrentUser(tenant_id=tenant, user_id=uid, plan="pro", tier="pro")


def _outline(chapters=None):
    return admit_outline_lifecycle(
        "8d95ed8a-1853-4ea4-9f47-5ae00fb61e21", "en",
        chapters or [{"id": "1", "title": "One", "words": 500},
                     {"id": "2", "title": "Two", "words": 600}],
    )


def _job_lifecycle(outline):
    return build_job_lifecycle(outline, outline["chapters"])


def run(coro):
    return asyncio.run(coro)


# ── 1. v1 job insert has _meter and lifecycle atomically ────────────────────
class TestJobInsertAtomicity:
    def test_insert_carries_both_meter_and_lifecycle_in_one_call(self, monkeypatch):
        captured = {}

        async def fake_fetchval(sql, *args, tenant=None):
            captured["sql"] = sql
            captured["input_payload"] = args[-1]
            return "11111111-1111-4111-8111-111111111111"

        monkeypatch.setattr(database, "_q_fetchval", fake_fetchval)
        outline = _outline()
        snap = _job_lifecycle(outline)
        jid = run(database.create_narasi_job(
            _TENANT, _UID, "ext123", "topic", 2, op_id="op:abc", lifecycle=snap))
        assert jid == "11111111-1111-4111-8111-111111111111"
        payload = captured["input_payload"]
        assert payload is not None
        assert payload["_meter"] == {"op_id": "op:abc", "actual": 0}
        assert payload["narasi_lifecycle"]["lifecycle_hash"] == snap["lifecycle_hash"]
        # single INSERT -- no separate UPDATE/jsonb_set call was made
        assert "INSERT INTO jobs" in captured["sql"]
        assert "jsonb_set" not in captured["sql"]

    def test_flag_off_legacy_shape_is_byte_identical(self, monkeypatch):
        captured = {}

        async def fake_fetchval(sql, *args, tenant=None):
            captured["input_payload"] = args[-1]
            return "22222222-2222-4222-8222-222222222222"

        monkeypatch.setattr(database, "_q_fetchval", fake_fetchval)
        run(database.create_narasi_job(_TENANT, _UID, "ext456", "topic", 1))
        assert captured["input_payload"] is None


# ── 2. internal UUID wins over duplicate external IDs ────────────────────────
class TestDuplicateExternalIdUuidAuthority:
    def test_run_narration_job_reads_by_uuid_not_external(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        # Two rows share the same external_job_id (a "duplicate_external" scenario); only
        # the one keyed by job_uuid carries the real lifecycle -- get_job_by_external would
        # return the WRONG (stale/newest) row if it were used instead.
        right_row = {"id": "uuid-right", "input_payload": {"narasi_lifecycle": snap}}
        wrong_row = {"id": "uuid-wrong", "input_payload": {}}
        calls = {"get_job": [], "get_job_by_external": []}

        async def fake_get_job(tenant_id, job_id):
            calls["get_job"].append(job_id)
            return right_row if job_id == "uuid-right" else None

        async def fake_get_job_by_external(tenant_id, external_id):
            calls["get_job_by_external"].append(external_id)
            return wrong_row

        finalized = []

        async def fake_finalize(job_id, job_uuid, tenant_id, *, status, result, error):
            finalized.append((status, error))

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(narration_api, "db", database)
        monkeypatch.setattr(narration_api, "_finalize", fake_finalize)
        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")

        request_body = {
            "target_language": "id",  # WRONG on purpose: proves the durable snapshot,
            "chapters": [],           # not this mismatched body, is read
        }
        run(narration_api._run_narration_job(
            body=request_body, job_id="ext-dup", job_uuid="uuid-right",
            tenant_id="t1", user_id="u1", total=2, meter_op=None, model="gemini-2.5-flash",
        ))
        assert calls["get_job"] == ["uuid-right"]
        assert calls["get_job_by_external"] == []   # never consulted when job_uuid exists
        assert finalized and finalized[0][0] == narration_api._STATUS_FAILED
        assert "LIFECYCLE_QUEUE_MISMATCH" in finalized[0][1]


# ── 3. worker durable-read/missing/malformed/mismatch failures never invoke
#       _run_narration_job ──────────────────────────────────────────────────
class TestWorkerFailClosed:
    def _patch_common(self, monkeypatch, *, get_job=None, get_job_by_external=None):
        calls = {"run_job": 0, "finish": []}

        async def fake_run_job(**kw):
            calls["run_job"] += 1

        async def fake_finish(tenant_id, job_id, status, result=None, error=None):
            calls["finish"].append((status, error))

        async def fake_finish_by_id(tenant_id, job_uuid, status, result=None, error=None):
            calls["finish"].append((status, error))

        async def default_get_job(tenant_id, job_id):
            return None

        async def default_get_job_by_external(tenant_id, external_id):
            return None

        monkeypatch.setattr(database, "get_job", get_job or default_get_job)
        monkeypatch.setattr(database, "get_job_by_external", get_job_by_external or default_get_job_by_external)
        monkeypatch.setattr(database, "finish_narasi_job", fake_finish)
        # B-07/B-08 Rework 2 (Contract B): the worker prefers the UUID-scoped finalizer
        # whenever job_uuid is present -- mock it too so a real, unmocked database call
        # (which casts job_uuid through uuid.UUID()) is never reached in these tests.
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_by_id)
        monkeypatch.setattr(narration_api, "_run_narration_job", fake_run_job)
        return calls

    def _job(self, **overrides):
        job = {"data": {"job_id": "j1", "job_uuid": "22222222-2222-4222-8222-222222222222", "tenant_id": "t1",
                        "user_id": "u1", "total": 1, "body": {}}, "id": "bull-1"}
        job["data"].update(overrides)

        class FakeJob:
            def __init__(self, data):
                self.data = data
                self.id = "bull-1"
                self.attemptsMade = 0
        return FakeJob(job["data"])

    def test_durable_read_exception_for_a_declared_v1_queue_item_never_invokes_run_job(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def raising_get_job(tenant_id, job_id):
            raise ConnectionError("db unreachable")
        calls = self._patch_common(monkeypatch, get_job=raising_get_job)
        result = run(narration_worker._process(self._job(narasi_lifecycle=snap)))
        assert calls["run_job"] == 0
        assert result == {"skipped": "lifecycle_persistence_failed"}
        assert calls["finish"] and "LIFECYCLE_PERSISTENCE_FAILED" in calls["finish"][0][1]

    def test_missing_durable_row_with_declared_queue_lifecycle_never_invokes_run_job(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        calls = self._patch_common(monkeypatch, get_job=lambda t, j: _none())
        result = run(narration_worker._process(self._job(narasi_lifecycle=snap)))
        assert calls["run_job"] == 0
        assert result == {"skipped": "lifecycle_persistence_failed"}
        assert calls["finish"] and calls["finish"][0][0] == "error"
        assert "LIFECYCLE_PERSISTENCE_FAILED" in calls["finish"][0][1]

    def test_malformed_durable_snapshot_never_invokes_run_job(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        bad_snap = copy.deepcopy(snap)
        bad_snap["schema_version"] = "999"

        async def fake_get_job(tenant_id, job_id):
            return {"id": job_id, "status": "processing", "input_payload": {"narasi_lifecycle": bad_snap}}
        calls = self._patch_common(monkeypatch, get_job=fake_get_job)
        result = run(narration_worker._process(self._job(narasi_lifecycle=snap)))
        assert calls["run_job"] == 0
        assert result == {"skipped": "lifecycle_mismatch"}

    def test_queue_body_mismatch_never_invokes_run_job(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job(tenant_id, job_id):
            return {"id": job_id, "status": "processing", "input_payload": {"narasi_lifecycle": snap}}
        calls = self._patch_common(monkeypatch, get_job=fake_get_job)
        mismatched_body = {"target_language": "fr", "chapters": []}
        result = run(narration_worker._process(self._job(narasi_lifecycle=snap, body=mismatched_body)))
        assert calls["run_job"] == 0
        assert result == {"skipped": "lifecycle_mismatch"}
        assert calls["finish"] and "LIFECYCLE_QUEUE_MISMATCH" in calls["finish"][0][1]

    def test_legacy_job_with_no_lifecycle_anywhere_still_invokes_run_job(self, monkeypatch):
        async def fake_get_job(tenant_id, job_id):
            return {"id": job_id, "status": "processing", "input_payload": {}}
        calls = self._patch_common(monkeypatch, get_job=fake_get_job)
        run(narration_worker._process(self._job()))
        assert calls["run_job"] == 1   # legacy path is unaffected


async def _none():
    return None


# ── 4. v1 final and checkpoint chapter writes include IDs and fail terminally ─
class TestChapterWritesFatalForV1:
    def test_persist_chapters_raises_for_v1_bound_write_failure(self, monkeypatch):
        async def raising_save(*a, **kw):
            raise ConnectionError("write failed")
        monkeypatch.setattr(database, "save_narasi_chapter", raising_save)
        monkeypatch.setattr(narration_api, "db", database)
        result = {"chapters": [{"no": 0, "content": "text", "chapter_id": "ch_" + "a" * 32}]}
        with pytest.raises(narration_api._NarasiLifecyclePersistError):
            run(narration_api._persist_chapters("t1", "job-uuid", result))

    def test_persist_chapters_best_effort_for_legacy_chapter(self, monkeypatch):
        async def raising_save(*a, **kw):
            raise ConnectionError("write failed")
        monkeypatch.setattr(database, "save_narasi_chapter", raising_save)
        monkeypatch.setattr(narration_api, "db", database)
        result = {"chapters": [{"no": 0, "content": "text"}]}   # no chapter_id -- legacy
        run(narration_api._persist_chapters("t1", "job-uuid", result))  # must not raise

    def test_persist_chapters_calls_save_with_chapter_id_kwarg(self, monkeypatch):
        captured = {}

        async def fake_save(tenant_id, job_uuid, idx, content, *, word_count, source_prompt,
                            retrieved_ids, version, approved, chapter_id=None):
            captured["chapter_id"] = chapter_id
        monkeypatch.setattr(database, "save_narasi_chapter", fake_save)
        monkeypatch.setattr(narration_api, "db", database)
        cid = "ch_" + "b" * 32
        result = {"chapters": [{"no": 0, "content": "text", "chapter_id": cid}]}
        run(narration_api._persist_chapters("t1", "job-uuid", result))
        assert captured["chapter_id"] == cid


# ── 5. status/readback returns IDs and language ──────────────────────────────
class TestStatusReadback:
    def test_narration_status_surfaces_language_and_chapter_ids(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        row = {
            "status": "done",
            "result_payload": {"markdown": "book", "chapters": [
                {"chapter_id": "ch_" + "c" * 32}, {"chapter_id": "ch_" + "d" * 32}]},
            "input_payload": {"narasi_lifecycle": snap},
            "progress_total": 2, "progress_current": 2,
        }

        # B-07/B-08 Rework 6 (Contract A): status now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied, db.get_job mocked.
        async def fake_get_job(tenant_id, job_uuid):
            return row

        async def fake_read_checkboxes(job_id):
            return "done", 2, 2, []

        async def fake_get_progress(job_id):
            return ""

        monkeypatch.setattr(narration_api, "db", database)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(narration_api, "_read_checkboxes", fake_read_checkboxes)
        monkeypatch.setattr(narration_api, "rc", type("_R", (), {"get_progress": staticmethod(fake_get_progress)}))
        out = run(narration_api.narration_status("ext1", _user(), job_uuid="row-uuid-status1"))
        assert out["target_language"] == "en"
        assert out["chapter_id"] == ["ch_" + "c" * 32, "ch_" + "d" * 32]

    def test_narasi_status_surfaces_language_and_chapter_ids(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        row = {
            "status": "done", "progress_current": 2, "progress_total": 2,
            "result_payload": {"chapters": [{"chapter_id": "ch_" + "e" * 32}]},
            "input_payload": {"narasi_lifecycle": snap},
        }

        # B-07/B-08 Rework 6 (Contract A): status now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied, db.get_job mocked.
        async def fake_get_job(tenant_id, job_uuid):
            return row

        async def fake_get_progress(job_id):
            return ""

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"get_progress": staticmethod(fake_get_progress)}))
        out = run(laozhang_api.narasi_status("ext1", _user(), job_uuid="row-uuid-status2"))
        assert out["target_language"] == "en"
        assert out["chapter_id"] == ["ch_" + "e" * 32]


# ── 6. Google/classic/Dalang pre-cost rejection ──────────────────────────────
class TestPreCostRejection:
    def test_narasi_generate_rejects_before_hold_when_flag_on_and_no_lifecycle(self, monkeypatch):
        charge_calls = []

        async def fake_begin_charge(**kw):
            charge_calls.append(kw)

        async def fake_resolve_user_uuid(t, u):
            return "u1"

        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")
        monkeypatch.setattr(laozhang_api.metering, "begin_charge", fake_begin_charge)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "_narasi_admit", lambda body, kind=None: None)
        monkeypatch.setattr(laozhang_api, "_dalang_admission_enabled", lambda: False)
        body = {"topic": "t", "chapters": [{"id": "1", "title": "One", "words": 400}]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_generate(body, _user()))
        assert caught.value.status_code == 422
        assert charge_calls == []   # no hold was ever placed

    def test_narration_start_rejects_before_hold_when_flag_on_and_no_lifecycle(self, monkeypatch):
        charge_calls = []

        async def fake_begin_charge(**kw):
            charge_calls.append(kw)

        async def fake_resolve_user_uuid(t, u):
            return "u1"

        async def fake_living_person_guard(body, tenant_id):
            return None

        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")
        monkeypatch.setattr(narration_api.metering, "begin_charge", fake_begin_charge)
        monkeypatch.setattr(narration_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(narration_api, "_living_person_guard", fake_living_person_guard)
        monkeypatch.setattr(narration_api, "_narration_admit", lambda body: None)
        body = {"topic": "t", "chapters": []}
        with pytest.raises(HTTPException) as caught:
            run(narration_api.narration_start(body, _user()))
        assert caught.value.status_code == 422
        assert charge_calls == []

    def test_google_validate_job_endpoint_rejects_before_success(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        forged = copy.deepcopy(snap)
        forged["lifecycle_hash"] = "f" * 64
        body = {"job_lifecycle": forged, "target_language": "en", "outline_lifecycle": outline,
                "chapters": [{"chapter_id": c["chapter_id"], "chapter_index": c["chapter_index"],
                              "legacy_id": c["legacy_id"]} for c in snap["chapters"]]}
        out = run(laozhang_api.narasi_lifecycle_validate_job(body, _user()))
        assert out["ok"] is False
        assert out["code"]

    def test_google_validate_job_endpoint_accepts_consistent_snapshot(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        body = {"job_lifecycle": snap, "target_language": "en",
                "chapters": [{"chapter_id": c["chapter_id"], "chapter_index": c["chapter_index"],
                              "legacy_id": c["legacy_id"]} for c in snap["chapters"]]}
        out = run(laozhang_api.narasi_lifecycle_validate_job(body, _user()))
        assert out["ok"] is True


# ── 7. stitch with flag toggled off remains lifecycle-bound ──────────────────
class TestStitchFlagIndependence:
    def test_stitch_rejects_conflicting_language_even_with_flag_off(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        row = {"id": "job-uuid", "input_payload": {"narasi_lifecycle": snap},
               "result_payload": {"markdown": "## Bab 1\n\nisi"}}

        async def fake_get_job_by_external(tenant_id, job_id):
            return row

        monkeypatch.delenv("NARASI_LIFECYCLE_V1", raising=False)   # explicitly OFF
        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(laozhang_api, "db", database)
        body = {"style": "storytelling", "language": "id"}   # conflicts with the durable "en"
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("ext1", body, _user()))
        assert caught.value.status_code == 409

    def test_stitch_read_failure_fails_closed_even_with_flag_off(self, monkeypatch):
        async def raising_get_job(tenant_id, job_id):
            raise ConnectionError("db down")

        monkeypatch.delenv("NARASI_LIFECYCLE_V1", raising=False)
        monkeypatch.setattr(database, "get_job_by_external", raising_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("ext1", {}, _user()))
        assert caught.value.status_code == 502


# ── 8. Review/One-Shot/save-edit lineage and conflict behavior ───────────────
class TestLineageAndConflict:
    def test_resolve_lineage_derives_language_from_source_job(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        _src_uuid = "10101010-1010-4101-8101-101010101010"

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        # B-07/B-08 Rework 5 (Contract F): a v1-bound row is now resolved by UUID -- a
        # caller with only the external id gets V1_REQUIRES_UUID instead.
        async def fake_get_job(tenant_id, job_uuid):
            assert job_uuid == _src_uuid
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")
        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)
        result = run(laozhang_api._narasi_resolve_manuscript_lineage(
            "t1", source_job_id="src1", source_job_uuid=_src_uuid, manuscript_language=None))
        assert result == "en"

    def test_resolve_lineage_rejects_conflicting_explicit_language(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api._narasi_resolve_manuscript_lineage(
                "t1", source_job_id="src1", manuscript_language="fr"))
        assert caught.value.status_code == 409

    def test_resolve_lineage_standalone_requires_language_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("NARASI_LIFECYCLE_V1", "1")
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api._narasi_resolve_manuscript_lineage(
                "t1", source_job_id=None, manuscript_language=None))
        assert caught.value.status_code == 422

    def test_build_lineage_binding_produces_real_artifact_binding(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        _src_uuid = "77777777-7777-4777-8777-777777777777"

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        # B-07/B-08 Rework 5 (Contract F): a v1-bound row is now resolved by UUID -- a
        # caller with only the external id gets V1_REQUIRES_UUID instead.
        async def fake_get_job(tenant_id, job_uuid):
            assert job_uuid == _src_uuid
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)
        binding, source_job_uuid = run(laozhang_api._narasi_build_lineage_binding(
            "t1", "src1", "review_input", "manuscript text", source_job_uuid=_src_uuid))
        assert binding is not None
        assert binding["artifact_kind"] == "review_input"
        assert binding["lifecycle_hash"] == snap["lifecycle_hash"]
        assert source_job_uuid == _src_uuid

    def test_build_lineage_binding_none_for_standalone(self, monkeypatch):
        binding, source_job_uuid = run(laozhang_api._narasi_build_lineage_binding(
            "t1", None, "oneshot_input", "text"))
        assert binding is None
        assert source_job_uuid is None


# ── 9. flag-off legacy byte-compatible paths ──────────────────────────────────
class TestFlagOffLegacyByteCompat:
    def test_create_narasi_job_legacy_shape_unaffected_by_flag(self, monkeypatch):
        monkeypatch.delenv("NARASI_LIFECYCLE_V1", raising=False)
        captured = {}

        async def fake_fetchval(sql, *args, tenant=None):
            captured["input_payload"] = args[-1]
            return "33333333-3333-4333-8333-333333333333"

        monkeypatch.setattr(database, "_q_fetchval", fake_fetchval)
        run(database.create_narasi_job(_TENANT, _UID, "ext789", "topic", 1))
        assert captured["input_payload"] is None

    def test_worker_legacy_job_runs_exactly_as_before(self, monkeypatch):
        async def fake_get_job(tenant_id, job_id):
            return {"id": job_id, "status": "processing", "input_payload": None}

        calls = {"run_job": 0}

        async def fake_run_job(**kw):
            calls["run_job"] += 1

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(narration_api, "_run_narration_job", fake_run_job)

        class FakeJob:
            data = {"job_id": "j1", "job_uuid": "uuid-1", "tenant_id": "t1",
                    "user_id": "u1", "total": 1, "body": {}}
            id = "bull-1"
            attemptsMade = 0
        run(narration_worker._process(FakeJob()))
        assert calls["run_job"] == 1

    def test_persist_chapters_no_job_uuid_is_a_pure_noop(self):
        # legacy guard: RLS needs an internal UUID; absent one, nothing is attempted
        run(narration_api._persist_chapters("t1", None, {"chapters": [{"no": 0, "content": "x"}]}))


# ── 10. retry-chapter endpoint is lifecycle-bound (Rework 2, Finding #5) ─────
class _FakeRetryMsg:
    def __init__(self, content):
        self.content = content


class _FakeRetryChoice:
    def __init__(self, content):
        self.message = _FakeRetryMsg(content)


class _FakeRetryResp:
    def __init__(self, content):
        self.choices = [_FakeRetryChoice(content)]


class TestRetryChapterLifecycleBound:
    """Real integration tests through narasi_lifecycle_retry_chapter -- the endpoint
    replacing the old unbound standalone action="chapter" call (Finding #5). Only the LLM
    call (_narasi_complete) and the db/usage boundary are mocked; pakem.assembler.compose,
    continuity.lifecycle validation, and the endpoint's own binding checks all run for real."""

    def _patch_provider(self, monkeypatch, text="regenerated chapter text word " * 10):
        async def fake_log_usage(*a, **kw):
            return 0

        async def fake_resolve_user_uuid(t, u):
            return _UID

        def fake_complete(model, messages, max_tok, role="", phase=""):
            return _FakeRetryResp(text), model

        monkeypatch.setattr(laozhang_api, "_narasi_complete", fake_complete)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)

    def _patch_source_job(self, monkeypatch, row):
        # B-07/B-08 Rework 3 (Contract H): the endpoint now reads the source job by its
        # internal UUID (db.get_job) -- never by external id -- since two rows can share
        # the same external_job_id and an external-id lookup risks resolving the wrong one.
        async def fake_get_job(tenant_id, job_uuid):
            return row
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)

    def test_forged_outline_lifecycle_mismatch_rejects(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        self._patch_source_job(monkeypatch, {"id": "job-uuid-1", "input_payload": {"narasi_lifecycle": snap}})
        self._patch_provider(monkeypatch)
        # A DIFFERENT, internally-VALID admitted outline (own genuine hash) -- not the
        # source job's own -- proves the endpoint cross-checks against the source job's
        # stored lifecycle, not merely that the supplied payload is self-consistent.
        different_outline = admit_outline_lifecycle(
            "11111111-2222-4333-8444-555555555555", "en",
            [{"id": "1", "title": "Other", "description": "Different", "words": 300}])
        body = {"source_job_uuid": "job-uuid-1", "outline_lifecycle": different_outline, "model": "gemini-2.5-flash",
                "chapter_id": different_outline["chapters"][0]["chapter_id"]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 409
        assert caught.value.detail["error"] == "LIFECYCLE_MISMATCH"

    def test_chapter_id_not_in_admitted_outline_rejects(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        self._patch_source_job(monkeypatch, {"id": "job-uuid-2", "input_payload": {"narasi_lifecycle": snap}})
        self._patch_provider(monkeypatch)
        body = {"source_job_uuid": "job-uuid-2", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": "ch_" + "9" * 32}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 404
        assert caught.value.detail["error"] == "CHAPTER_NOT_FOUND"

    def test_source_job_without_lifecycle_rejects(self, monkeypatch):
        outline = _outline()
        self._patch_source_job(monkeypatch, {"id": "job-uuid-3", "input_payload": {}})   # legacy job
        self._patch_provider(monkeypatch)
        body = {"source_job_uuid": "job-uuid-3", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": outline["chapters"][0]["chapter_id"]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 409
        assert caught.value.detail["error"] == "SOURCE_JOB_NOT_LIFECYCLE_BOUND"

    def test_source_job_not_found_rejects(self, monkeypatch):
        outline = _outline()
        self._patch_source_job(monkeypatch, None)
        self._patch_provider(monkeypatch)
        body = {"source_job_uuid": "missing-uuid", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": outline["chapters"][0]["chapter_id"]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 404
        assert caught.value.detail["error"] == "SOURCE_JOB_NOT_FOUND"

    @pytest.mark.parametrize("missing_field", ["source_job_uuid", "chapter_id", "outline_lifecycle"])
    def test_missing_required_field_rejects(self, monkeypatch, missing_field):
        outline = _outline()
        body = {"source_job_uuid": "job-uuid-x", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": outline["chapters"][0]["chapter_id"]}
        body.pop(missing_field)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 422

    def test_brief_without_binding_rejects(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        self._patch_source_job(monkeypatch, {"id": "job-uuid-4", "input_payload": {"narasi_lifecycle": snap}})
        self._patch_provider(monkeypatch)
        body = {"source_job_uuid": "job-uuid-4", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": outline["chapters"][0]["chapter_id"], "brief": "some brief text"}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 422
        assert caught.value.detail["error"] == "ARTIFACT_BINDING_INVALID"

    def test_valid_retry_regenerates_and_persists_by_chapter_id(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        target_chapter = outline["chapters"][1]
        self._patch_source_job(monkeypatch, {"id": "job-uuid-5", "input_payload": {"narasi_lifecycle": snap}})
        self._patch_provider(monkeypatch, text="brand new chapter content word " * 10)

        captured = {}

        async def fake_save_narasi_chapter(tenant_id, job_uuid, chapter_index, content,
                                           word_count, source_prompt, retrieved_ids, *, chapter_id=None):
            captured["tenant_id"] = tenant_id
            captured["job_uuid"] = job_uuid
            captured["chapter_index"] = chapter_index
            captured["content"] = content
            captured["chapter_id"] = chapter_id
            return "chapter-row-uuid"
        monkeypatch.setattr(database, "save_narasi_chapter", fake_save_narasi_chapter)

        body = {"source_job_uuid": "job-uuid-5", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": target_chapter["chapter_id"]}
        out = run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert out["ok"] is True
        assert out["chapter_id"] == target_chapter["chapter_id"]
        assert "brand new chapter content" in out["text"]
        # Persisted against the SOURCE JOB's own internal UUID, at the EXACT admitted
        # chapter_index, keyed by chapter_id -- never by position in some unrelated list.
        assert captured["job_uuid"] == "job-uuid-5"
        assert captured["chapter_index"] == target_chapter["chapter_index"]
        assert captured["chapter_id"] == target_chapter["chapter_id"]

    def test_persist_failure_is_fatal_not_silently_swallowed(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        self._patch_source_job(monkeypatch, {"id": "job-uuid-6", "input_payload": {"narasi_lifecycle": snap}})
        self._patch_provider(monkeypatch)

        async def raising_save(*a, **kw):
            raise ConnectionError("write failed")
        monkeypatch.setattr(database, "save_narasi_chapter", raising_save)

        body = {"source_job_uuid": "job-uuid-6", "outline_lifecycle": outline, "model": "gemini-2.5-flash",
                "chapter_id": outline["chapters"][0]["chapter_id"]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body, _user()))
        assert caught.value.status_code == 502
        assert caught.value.detail["error"] == "LIFECYCLE_PERSISTENCE_FAILED"


# ── 11. Rework 3 (Contract B/D): Classic hold placed only after the durable insert ──
class TestClassicHoldOrderRuntime:
    def test_narasi_generate_creates_durable_row_before_placing_hold(self, monkeypatch):
        calls = []

        async def fake_create_narasi_job(tenant_id, user_id, external_id, topic, total_chapters=0,
                                          op_id=None, lifecycle=None):
            calls.append("create_narasi_job")
            return "11111111-1111-4111-8111-111111111111"

        async def fake_begin_charge(**kw):
            calls.append("begin_charge")

        async def fake_resolve_user_uuid(t, u):
            return "u1"

        async def fake_impl_guarded(*a, **kw):
            return None

        monkeypatch.setattr(database, "create_narasi_job", fake_create_narasi_job)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api.metering, "begin_charge", fake_begin_charge)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "_narasi_admit", lambda body, kind=None: None)
        monkeypatch.setattr(laozhang_api, "_dalang_admission_enabled", lambda: False)
        monkeypatch.setattr(laozhang_api, "_narasi_generate_impl_guarded", fake_impl_guarded)

        body = {"topic": "t", "chapters": [{"id": "1", "title": "One", "words": 400}]}
        out = run(laozhang_api.narasi_generate(body, _user()))
        # The runtime CALL ORDER (not just the source line order an AST check would see) --
        # a real hold-before-insert regression would append "begin_charge" first.
        assert calls == ["create_narasi_job", "begin_charge"]
        assert out["ok"] is True
        assert out["job_uuid"] == "11111111-1111-4111-8111-111111111111"


# ── 12. Rework 3 (Contract E): Google persist rejects a declared lifecycle against a
#         lifecycle-less durable row (row exists, but was never admitted as v1) ──
class TestGooglePersistDeclaredAgainstLifecycleLessRow:
    def test_persist_rejects_declared_lifecycle_when_row_has_none(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job(tenant_id, job_uuid):
            return {"id": "row-uuid-7", "input_payload": {}}   # row exists, never v1-admitted

        async def fake_resolve_user_uuid(t, u):
            return _UID

        calls = []

        async def fake_save_chapter(*a, **kw):
            calls.append((a, kw))

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "save_narasi_chapter", fake_save_chapter)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)

        body = {
            "job_id": "caller-run", "job_uuid": "row-uuid-7",
            "language": "en", "job_lifecycle": snap,
            "chapters": [{"index": 0, "content": "text", "chapter_id": snap["chapters"][0]["chapter_id"]}],
        }
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_persist(body, _user()))
        assert caught.value.status_code == 409
        assert calls == []   # never reaches a chapter write


# ── 13. Rework 3 (Contract F): stitch rejects a duplicate structured row even when
#         every admitted chapter_id is otherwise covered ──────────────────────────
class TestStitchDuplicateRowRejects:
    def test_stitch_rejects_duplicate_chapter_row_with_complete_id_coverage(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        admitted = snap["chapters"]

        # B-07/B-08 Rework 6 (Contract A): stitch now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied in the body.
        async def fake_get_job(tenant_id, job_uuid):
            return {"id": "row-uuid-8", "input_payload": {"narasi_lifecycle": snap},
                    "result_payload": {"markdown": "STALE MARKDOWN"}}

        async def fake_get_narasi_chapters(tenant_id, job_uuid):
            # Every admitted chapter_id IS present (no missing/foreign id), but chapter 0 has
            # TWO rows -- 3 total rows for 2 admitted chapters. A set-only comparison would
            # miss this; the length check is what catches it.
            return [
                {"chapter_id": admitted[0]["chapter_id"], "chapter_index": 0, "content": "first"},
                {"chapter_id": admitted[0]["chapter_id"], "chapter_index": 0, "content": "duplicate"},
                {"chapter_id": admitted[1]["chapter_id"], "chapter_index": 1, "content": "second"},
            ]

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_narasi_chapters", fake_get_narasi_chapters)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("ext1", {"language": "en", "job_uuid": "row-uuid-8"}, _user()))
        assert caught.value.status_code == 502


# ── 14. Rework 3 (Contract D): status derives per-chapter state from durable identity,
#          never from progress_current vs. loop position ──────────────────────────
class TestClassicStatusMembershipNotPositional:
    def test_status_marks_the_actually_finished_middle_chapter_done(self, monkeypatch):
        outline = _outline([{"id": "1", "title": "One", "words": 300},
                            {"id": "2", "title": "Two", "words": 300},
                            {"id": "3", "title": "Three", "words": 300}])
        snap = _job_lifecycle(outline)
        admitted = snap["chapters"]
        # Only the MIDDLE (index 1) chapter has actually finished. progress_current=1 would,
        # under the old positional derivation, wrongly mark chapter 0 (not chapter 1) done.
        row = {
            "status": "processing", "progress_current": 1, "progress_total": 3,
            "input_payload": {"narasi_lifecycle": snap},
            "result_payload": {"chapters": [{"chapter_id": admitted[1]["chapter_id"]}]},
        }

        # B-07/B-08 Rework 6 (Contract A): status now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied, db.get_job mocked.
        async def fake_get_job(tenant_id, job_uuid):
            return row

        async def fake_get_progress(job_id):
            return ""

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"get_progress": staticmethod(fake_get_progress)}))
        out = run(laozhang_api.narasi_status("ext1", _user(), job_uuid="row-uuid-membership"))
        states = {c["chapter_id"]: c["state"] for c in out["chapters"]}
        assert states[admitted[0]["chapter_id"]] == "pending"
        assert states[admitted[1]["chapter_id"]] == "done"
        assert states[admitted[2]["chapter_id"]] == "pending"
        assert out["current"] == 1   # the aggregate count is untouched -- only identity changed


# ── 15. Rework 3 (Contract H): Review/save-edit source-linked persistence is genuinely
#          fatal at runtime -- not just absent from the source text ────────────────
class _FakeChatMsg:
    def __init__(self, content):
        self.content = content


class _FakeChatChoice:
    def __init__(self, content):
        self.message = _FakeChatMsg(content)
        self.finish_reason = "stop"


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20


class _FakeChatResp:
    def __init__(self, content):
        self.choices = [_FakeChatChoice(content)]
        self.usage = _FakeUsage()


class _FakeReviewClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                return _FakeChatResp("a fine editorial review")


class TestReviewAndSaveEditFatalPersistenceRuntime:
    def _patch_review_common(self, monkeypatch):
        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_log_usage(*a, **kw):
            return 0

        async def fake_persist_asset(*a, **kw):
            return None

        # B-07/B-08 Rework 4 (Contract G): narasi_review now writes a pre-provider derived-
        # input record before the chat.completions.create call -- mock it so this real-DB
        # primitive is never reached in a unit test with no live pool.
        async def fake_create_derived_input(*a, **kw):
            return "derived-input-uuid"

        # B-07/B-08 Rework 5 (Contract G): the derived-input row is now terminalized
        # done/error after the provider call -- mock it too, same reason.
        async def fake_finish_narasi_job_by_id(*a, **kw):
            return None

        monkeypatch.setattr(laozhang_api, "make_client", lambda model: _FakeReviewClient())
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
        monkeypatch.setattr(laozhang_api, "_persist_asset", fake_persist_asset)
        monkeypatch.setattr(database, "create_derived_input", fake_create_derived_input)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_narasi_job_by_id)

    def test_source_linked_review_raises_when_moat_persistence_fails(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        _src_uuid = "row-uuid-9"

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        # B-07/B-08 Rework 5 (Contract F): a v1-bound row is now resolved by UUID -- the
        # request must carry source_job_uuid, not just the external id.
        async def fake_get_job(tenant_id, job_uuid):
            assert job_uuid == _src_uuid
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        async def raising_moat(*a, **kw):
            raise ConnectionError("moat db down")

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "save_moat_session", raising_moat)
        monkeypatch.setattr(laozhang_api, "db", database)
        self._patch_review_common(monkeypatch)

        body = {"source_job_id": "ext1", "source_job_uuid": _src_uuid,
                "message": "please review this manuscript"}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(body, _user()))
        assert caught.value.status_code == 500

    def test_standalone_review_survives_moat_persistence_failure(self, monkeypatch):
        async def raising_moat(*a, **kw):
            raise ConnectionError("moat db down")

        monkeypatch.setattr(database, "save_moat_session", raising_moat)
        monkeypatch.setattr(laozhang_api, "db", database)
        self._patch_review_common(monkeypatch)

        body = {"message": "please review this manuscript"}   # no source_job_id -- standalone
        out = run(laozhang_api.narasi_review(body, _user()))
        assert out["ok"] is True   # non-fatal for a genuinely standalone call

    def test_source_linked_save_edit_raises_when_pair_persistence_fails(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        _src_uuid = "row-uuid-10"

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        # B-07/B-08 Rework 5 (Contract F): a v1-bound row is now resolved by UUID -- the
        # caller must send the run's job_uuid, not just the path's external job_id.
        async def fake_get_job(tenant_id, job_uuid):
            assert job_uuid == _src_uuid
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def raising_pair(*a, **kw):
            raise ConnectionError("pair db down")

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "save_correction_pair", raising_pair)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)

        body = {"chap_id": "1", "original_text": "the original text here",
                "corrected_text": "the corrected text here", "job_uuid": _src_uuid}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_save_edit("ext1", body, _user()))
        assert caught.value.status_code == 502
        assert caught.value.detail["error"] == "LIFECYCLE_PERSISTENCE_FAILED"

    def test_source_linked_save_edit_persists_real_binding_on_success(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        _src_uuid = "row-uuid-11"

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        # B-07/B-08 Rework 5 (Contract F): a v1-bound row is now resolved by UUID -- the
        # caller must send the run's job_uuid, not just the path's external job_id.
        async def fake_get_job(tenant_id, job_uuid):
            assert job_uuid == _src_uuid
            return {"id": _src_uuid, "input_payload": {"narasi_lifecycle": snap}}

        async def fake_resolve_user_uuid(t, u):
            return _UID

        captured = {}

        async def fake_save_pair(moat_sid, tenant_id, user_id, original_text, corrected_text,
                                  style_label, topic, duration_min, language, *,
                                  source_job_uuid=None, lineage_binding=None):
            captured["source_job_uuid"] = source_job_uuid
            captured["lineage_binding"] = lineage_binding
            return {"quality_tier": "high", "edit_ratio": 0.2}

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "save_correction_pair", fake_save_pair)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)

        body = {"chap_id": "1", "original_text": "the original text here",
                "corrected_text": "the corrected text here", "job_uuid": _src_uuid}
        out = run(laozhang_api.narasi_save_edit("ext1", body, _user()))
        assert out["ok"] is True
        assert captured["source_job_uuid"] == _src_uuid
        assert captured["lineage_binding"]["lifecycle_hash"] == snap["lifecycle_hash"]


# ── 14. Rework 4 (Contract A): an external id paired with a valid job_uuid must name the
#         SAME row -- once job_uuid is supplied, resolution is by UUID and never by
#         newest-by-external, and a mismatched pair rejects before any read/write ──────
class TestUuidExternalPairMismatch:
    def test_narasi_status_rejects_mismatched_external_id_for_valid_uuid(self, monkeypatch):
        async def fake_get_job(tenant_id, job_uuid):
            return {"id": job_uuid, "external_job_id": "right-id", "status": "processing",
                    "input_payload": {}, "result_payload": {}}

        async def forbidden_external(*a, **kw):
            raise AssertionError("must not resolve by external id once job_uuid is supplied")

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_job_by_external", forbidden_external)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_status("wrong-id", _user(),
                                           job_uuid="11111111-1111-4111-8111-111111111111"))
        assert caught.value.status_code == 409

    def test_narasi_cancel_rejects_mismatched_external_id_for_valid_uuid(self, monkeypatch):
        async def fake_get_job(tenant_id, job_uuid):
            return {"id": job_uuid, "external_job_id": "right-id"}

        async def forbidden_external(*a, **kw):
            raise AssertionError("must not resolve by external id once job_uuid is supplied")

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_job_by_external", forbidden_external)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_cancel("wrong-id", _user(),
                                           job_uuid="11111111-1111-4111-8111-111111111111"))
        assert caught.value.status_code == 409


# ── 15. Rework 4 (Contract B): Dalang telemetry's OWN chapter_id is authority for the
#         checkbox sink -- never derived from task_id/position ────────────────────────
class TestDalangTelemetryDirectIdentity:
    def test_checkbox_sink_prefers_telemetry_chapter_id_over_task_position(self, monkeypatch):
        from orchestrator import core as orchestrator_core
        outline = _outline()
        admitted = outline["chapters"]
        captured = []

        async def fake_set_chapter_state(job_id, no, state, chapter_id=None):
            captured.append((no, state, chapter_id))

        class _DummySink:
            credits = 0

            def __call__(self, t):
                return None

        monkeypatch.setattr(narration_api, "_set_chapter_state", fake_set_chapter_state)
        real_second_id = admitted[1]["chapter_id"]

        async def scenario():
            sink = narration_api._ChapterCheckboxSink(
                _DummySink(), job_id="job-uuid", total=2, admitted_chapters=admitted)
            # task_id="ch1" would positionally resolve to admitted[0]; the telemetry's OWN
            # chapter_id explicitly names admitted[1] -- a deliberate mismatch proving the
            # sink reads identity from the event, never from task_id/position.
            tele = orchestrator_core.CallTelemetry(model="m", task_id="ch1", ok=True,
                                                   chapter_id=real_second_id)
            sink(tele)
            await asyncio.sleep(0)

        run(scenario())
        assert captured and captured[0][2] == real_second_id


# ── 16. Rework 4 (Contract C): EVERY hold exception (not just HTTPException) fail-closed,
#         both engines -- terminalize before progress/queue/spawn/provider ────────────
class TestGenericHoldFailureFailClosed:
    def test_narasi_generate_generic_hold_failure_terminalizes_before_spawn(self, monkeypatch):
        calls = {"finish": 0, "spawn": 0}

        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_create_narasi_job(*a, **kw):
            return "row-uuid-20"

        async def raising_charge(**kw):
            raise ConnectionError("meter unreachable")

        async def fake_finish_by_id(*a, **kw):
            calls["finish"] += 1

        def fake_create_task(coro):
            calls["spawn"] += 1
            coro.close()

        monkeypatch.setattr(database, "create_narasi_job", fake_create_narasi_job)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_by_id)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api.metering, "begin_charge", raising_charge)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "_narasi_admit", lambda *a, **kw: None)
        monkeypatch.setattr(laozhang_api, "_dalang_admission_enabled", lambda: False)
        monkeypatch.setattr(laozhang_api, "_byok_active", lambda: False)
        monkeypatch.setattr(laozhang_api.asyncio, "create_task", fake_create_task)

        body = {"topic": "t", "chapters": [{"id": "1", "title": "One", "words": 400}]}
        with pytest.raises(ConnectionError):
            run(laozhang_api.narasi_generate(body, _user()))
        assert calls == {"finish": 1, "spawn": 0}

    def test_narration_start_generic_hold_failure_terminalizes_before_spawn(self, monkeypatch):
        calls = {"finish": 0, "spawn": 0}

        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_living_person_guard(*a, **kw):
            return None

        async def fake_create_narasi_job(*a, **kw):
            return "row-uuid-21"

        async def raising_charge(**kw):
            raise ConnectionError("meter unreachable")

        async def fake_finish_by_id(*a, **kw):
            calls["finish"] += 1

        def fake_create_task(coro):
            calls["spawn"] += 1
            coro.close()

        monkeypatch.setattr(database, "create_narasi_job", fake_create_narasi_job)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_by_id)
        monkeypatch.setattr(narration_api, "db", database)
        monkeypatch.setattr(narration_api.metering, "begin_charge", raising_charge)
        monkeypatch.setattr(narration_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(narration_api, "_living_person_guard", fake_living_person_guard)
        monkeypatch.setattr(narration_api, "_narration_admit", lambda body: None)
        monkeypatch.setattr(narration_api, "_byok", lambda: False)
        monkeypatch.setattr(narration_api.asyncio, "create_task", fake_create_task)

        body = {"topic": "t", "chapters": [{"id": "1", "title": "One", "words": 400}]}
        with pytest.raises(ConnectionError):
            run(narration_api.narration_start(body, _user()))
        assert calls == {"finish": 1, "spawn": 0}


# ── 17. Rework 4 (Contract D): Google persist rejects an external id that does not name
#         the same row as the supplied job_uuid -- before any chapter write ────────────
class TestGooglePersistExternalUuidPairMismatch:
    def test_persist_rejects_wrong_external_id_paired_with_valid_uuid(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job(tenant_id, job_uuid):
            return {"id": job_uuid, "external_job_id": "right-id",
                    "input_payload": {"narasi_lifecycle": snap}}

        calls = []

        async def fake_save_chapter(*a, **kw):
            calls.append((a, kw))

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "save_narasi_chapter", fake_save_chapter)
        monkeypatch.setattr(laozhang_api, "db", database)

        body = {
            "job_id": "wrong-id", "job_uuid": "row-uuid-22", "language": "en",
            "job_lifecycle": snap,
            "chapters": [{"index": 0, "chapter_index": 0, "content": "text",
                          "chapter_id": snap["chapters"][0]["chapter_id"]}],
        }
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_persist(body, _user()))
        assert caught.value.status_code == 409
        assert calls == []


# ── 18. Rework 4 (Contract E): stitch fully validates the durable snapshot's structure
#         and each row's (chapter_id, chapter_index) PAIR, not just the id set ─────────
class TestStitchFullValidationAndIndexPairs:
    def test_stitch_rejects_malformed_durable_bindings_shape(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        malformed = copy.deepcopy(snap)
        malformed["bindings"] = {"brief": {"forged": True}}

        # B-07/B-08 Rework 6 (Contract A): stitch now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied in the body.
        async def fake_row(tenant_id, job_uuid):
            return {"id": "row-uuid-24", "external_job_id": "ext1",
                    "input_payload": {"narasi_lifecycle": malformed}, "result_payload": {}}

        async def fake_chapters(tenant_id, job_uuid):
            return [{"chapter_id": c["chapter_id"], "chapter_index": c["chapter_index"], "content": "text"}
                    for c in malformed["chapters"]]

        monkeypatch.setattr(database, "get_job", fake_row)
        monkeypatch.setattr(database, "get_narasi_chapters", fake_chapters)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("ext1", {"language": "en", "job_uuid": "row-uuid-24"}, _user()))
        assert caught.value.status_code == 502

    def test_stitch_rejects_chapter_id_index_swap(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        # B-07/B-08 Rework 6 (Contract A): stitch now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied in the body.
        async def fake_row(tenant_id, job_uuid):
            return {"id": "row-uuid-25", "external_job_id": "ext1",
                    "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}}

        async def fake_chapters(tenant_id, job_uuid):
            # SAME id set as admitted, but chapter_index values SWAPPED between the two rows.
            return [
                {"chapter_id": snap["chapters"][0]["chapter_id"], "chapter_index": 1, "content": "a"},
                {"chapter_id": snap["chapters"][1]["chapter_id"], "chapter_index": 0, "content": "b"},
            ]

        monkeypatch.setattr(database, "get_job", fake_row)
        monkeypatch.setattr(database, "get_narasi_chapters", fake_chapters)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("ext1", {"language": "en", "job_uuid": "row-uuid-25"}, _user()))
        assert caught.value.status_code == 502


# ── 19. Rework 4 (Contract G): a pre-provider derived-input write failure stops the
#         provider call entirely -- a genuine RUNTIME proof, not just source ordering ──
class TestPreProviderDerivedInputRuntimeProof:
    def test_review_never_calls_provider_when_derived_input_write_fails(self, monkeypatch):
        calls = {"provider": 0}

        class _RaisingProviderClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        calls["provider"] += 1
                        return _FakeChatResp("should never run")

        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def raising_create_derived_input(*a, **kw):
            raise ConnectionError("derived-input db down")

        monkeypatch.setattr(laozhang_api, "make_client", lambda model: _RaisingProviderClient())
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(database, "create_derived_input", raising_create_derived_input)
        monkeypatch.setattr(laozhang_api, "db", database)

        body = {"message": "please review this manuscript"}
        with pytest.raises(ConnectionError):
            run(laozhang_api.narasi_review(body, _user()))
        assert calls["provider"] == 0

    def test_build_lineage_binding_raises_when_named_source_disappears(self, monkeypatch):
        async def missing(*a, **kw):
            return None

        monkeypatch.setattr(database, "get_job_by_external", missing)
        monkeypatch.setattr(laozhang_api, "db", database)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api._narasi_build_lineage_binding(
                _TENANT, "named-source", "review_input", "text"))
        assert caught.value.status_code == 404


# ── 20. Rework 4 (Contract H): BullMQ rejects a same-hash but field-drifted queue
#         snapshot -- full validate + recursive exact-compare, not a hash-only check ──
class TestWorkerQueueSnapshotExactCompare:
    def test_process_rejects_queue_snapshot_with_same_hash_different_language(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        queue_snap = copy.deepcopy(snap)
        queue_snap["target_language"] = "fr"   # lifecycle_hash stays identical (copied from outline)

        calls = {"run_job": 0, "finish": []}

        async def fake_run_job(**kw):
            calls["run_job"] += 1

        async def fake_get_job(tenant_id, job_id):
            return {"id": job_id, "status": "processing", "input_payload": {"narasi_lifecycle": snap}}

        async def fake_finish_by_id(tenant_id, job_uuid, status, result=None, error=None):
            calls["finish"].append((status, error))

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_by_id)
        monkeypatch.setattr(narration_api, "_run_narration_job", fake_run_job)

        class FakeJob:
            id = "bull-99"
            attemptsMade = 0
            data = {"job_id": "same", "job_uuid": "row-uuid-23", "tenant_id": "t1",
                    "user_id": "u1", "total": 2, "body": {"target_language": "en", "chapters": []},
                    "narasi_lifecycle": queue_snap}

        result = run(narration_worker._process(FakeJob()))
        assert calls["run_job"] == 0
        assert result == {"skipped": "lifecycle_mismatch"}
        assert calls["finish"] and "QUEUE_DURABLE_SNAPSHOT_MISMATCH" in calls["finish"][0][1]


# ── 21. Rework 5 (Contract A): the Dalang GET route is HTTP-level auth-enforced and
#         dispatches end-to-end to narration_status by real FastAPI routing -- not just
#         inspect.signature-correct in isolation ──────────────────────────────────────
class TestDalangRouteHttpLevel:
    def test_unauthenticated_request_is_rejected_before_any_row_read(self, monkeypatch):
        calls = {"get_job_by_external": 0}

        async def spy_get_job_by_external(tenant_id, job_id):
            calls["get_job_by_external"] += 1
            return None

        monkeypatch.setattr(database, "get_job_by_external", spy_get_job_by_external)
        monkeypatch.setattr(narration_api, "db", database)
        client = TestClient(narration_api.app)
        r = client.get("/narration/ext1")
        assert r.status_code == 401
        assert calls["get_job_by_external"] == 0   # auth rejected before any row read

    def test_authenticated_request_dispatches_to_the_correct_row_by_uuid(self, monkeypatch):
        row_a = {"id": "http-uuid-a", "external_job_id": "ext-shared", "status": "processing",
                 "progress_current": 0, "progress_total": 2, "input_payload": {}, "result_payload": {}}
        row_b = {"id": "http-uuid-b", "external_job_id": "ext-shared", "status": "done",
                 "progress_current": 2, "progress_total": 2, "input_payload": {},
                 "result_payload": {"markdown": "x"}}
        rows_by_uuid = {"http-uuid-a": row_a, "http-uuid-b": row_b}

        async def fake_get_job(tenant_id, job_uuid):
            return rows_by_uuid.get(job_uuid)

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(narration_api, "db", database)
        narration_api.app.dependency_overrides[get_current_user] = lambda: _user()
        try:
            client = TestClient(narration_api.app)
            ra = client.get("/narration/ext-shared", params={"job_uuid": "http-uuid-a"})
            rb = client.get("/narration/ext-shared", params={"job_uuid": "http-uuid-b"})
        finally:
            narration_api.app.dependency_overrides.pop(get_current_user, None)
        assert ra.status_code == 200 and ra.json()["job_uuid"] == "http-uuid-a"
        assert rb.status_code == 200 and rb.json()["job_uuid"] == "http-uuid-b"
        # genuinely two DIFFERENT rows resolved (not the same row echoed twice)
        assert ra.json()["status"] == "running" and rb.json()["status"] == "done"


# ── 22. Rework 5 (Contract C): TRUE two-row DB+Redis isolation -- cancel/chapters-list
#         act on the EXACT selected UUID's row, never the other row sharing the same
#         external_job_id (no one-row mock stands in for "duplicate row" proof) ────────
class TestTwoRowCancelIsolation:
    def test_narasi_cancel_sets_redis_flag_scoped_to_the_selected_row_only(self, monkeypatch):
        rows = {"cancel-uuid-a": {"id": "cancel-uuid-a", "external_job_id": "ext-dup"},
                "cancel-uuid-b": {"id": "cancel-uuid-b", "external_job_id": "ext-dup"}}

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        cancelled_keys = []

        async def fake_set_cancel(key):
            cancelled_keys.append(key)

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"set_cancel": staticmethod(fake_set_cancel)}))

        run(laozhang_api.narasi_cancel("ext-dup", _user(), job_uuid="cancel-uuid-a"))
        run(laozhang_api.narasi_cancel("ext-dup", _user(), job_uuid="cancel-uuid-b"))
        assert cancelled_keys == ["narasi_cancel-uuid-a", "narasi_cancel-uuid-b"]

    def test_narration_cancel_sets_redis_flag_scoped_to_the_selected_row_only(self, monkeypatch):
        rows = {"cancel-uuid-a": {"id": "cancel-uuid-a", "external_job_id": "ext-dup"},
                "cancel-uuid-b": {"id": "cancel-uuid-b", "external_job_id": "ext-dup"}}

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        cancelled_keys = []

        async def fake_set_cancel(key):
            cancelled_keys.append(key)

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(narration_api, "db", database)
        monkeypatch.setattr(narration_api, "rc", type("_R", (), {"set_cancel": staticmethod(fake_set_cancel)}))

        run(narration_api.narration_cancel("ext-dup", _user(), job_uuid="cancel-uuid-a"))
        run(narration_api.narration_cancel("ext-dup", _user(), job_uuid="cancel-uuid-b"))
        assert cancelled_keys == ["narration_cancel-uuid-a", "narration_cancel-uuid-b"]


class TestTwoRowChaptersListIsolation:
    def test_chapters_list_reads_ratings_for_the_selected_row_only(self, monkeypatch):
        rows = {"rating-uuid-a": {"id": "rating-uuid-a", "external_job_id": "ext-rate"},
                "rating-uuid-b": {"id": "rating-uuid-b", "external_job_id": "ext-rate"}}

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        rating_calls = []

        async def fake_get_chapters_for_rating(tenant_id, job_id):
            rating_calls.append(job_id)
            return [{"id": f"chapter-of-{job_id}", "chapter_index": 0, "rating": 0}]

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_chapters_for_rating", fake_get_chapters_for_rating)
        monkeypatch.setattr(laozhang_api, "db", database)

        out_a = run(laozhang_api.narasi_chapters_list("ext-rate", _user(), job_uuid="rating-uuid-a"))
        out_b = run(laozhang_api.narasi_chapters_list("ext-rate", _user(), job_uuid="rating-uuid-b"))
        assert rating_calls == ["rating-uuid-a", "rating-uuid-b"]
        assert out_a["chapters"][0]["id"] == "chapter-of-rating-uuid-a"
        assert out_b["chapters"][0]["id"] == "chapter-of-rating-uuid-b"
        assert out_a["job_uuid"] == "rating-uuid-a" and out_b["job_uuid"] == "rating-uuid-b"

    def test_chapters_list_rejects_v1_row_selected_by_external_id_alone(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": "rating-uuid-v1", "external_job_id": job_id,
                    "input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(laozhang_api, "db", database)

        # B-07/B-08 Rework 6 (Contract A): the resolver's V1_REQUIRES_UUID rejection now
        # PROPAGATES as a real bounded 409 -- a caller can no longer mistake lifecycle
        # ambiguity for an ordinary empty/legacy read via a soft {"ok": False}.
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_chapters_list("ext-v1", _user(), job_uuid=None))
        assert caught.value.status_code == 409
        assert "V1_REQUIRES_UUID" in str(caught.value.detail)


# ── 23. Rework 5 (Contract D): the checkbox sink's legacy (no admitted chapter set) path
#         still carries a REAL direct chapter_id through byte-identically after the
#         fail-closed change -- the new check must never affect a genuinely legacy run ──
class TestSinkLegacyPassthroughStillWorks:
    def test_sink_still_carries_a_real_direct_chapter_id_for_a_legacy_run(self, monkeypatch):
        from orchestrator import core as orchestrator_core
        captured = []

        async def fake_set_chapter_state(job_id, no, state, chapter_id=None):
            captured.append((no, state, chapter_id))

        class _DummySink:
            credits = 0

            def __call__(self, t):
                return None

        monkeypatch.setattr(narration_api, "_set_chapter_state", fake_set_chapter_state)

        async def scenario():
            sink = narration_api._ChapterCheckboxSink(
                _DummySink(), job_id="legacy-job", total=1, admitted_chapters=[])
            tele = orchestrator_core.CallTelemetry(model="m", task_id="ch1", ok=True, chapter_id="ch_real123")
            sink(tele)
            await asyncio.sleep(0)

        run(scenario())
        assert captured == [(0, narration_api._STATUS_DONE, "ch_real123")]

    def test_sink_still_flips_the_checkbox_for_a_genuinely_legacy_run_with_no_chapter_id_at_all(self, monkeypatch):
        # The TRUE legacy case: admitted_chapters=[] (no v1 lifecycle at all) AND the event
        # itself carries no chapter_id either (a pre-v1 run has no identity concept). The
        # NEW Rework 5 fail-closed check is scoped to `self._admitted_chapters` truthy --
        # it must never accidentally also swallow this genuinely legacy, no-identity case.
        from orchestrator import core as orchestrator_core
        captured = []

        async def fake_set_chapter_state(job_id, no, state, chapter_id=None):
            captured.append((no, state, chapter_id))

        class _DummySink:
            credits = 0

            def __call__(self, t):
                return None

        monkeypatch.setattr(narration_api, "_set_chapter_state", fake_set_chapter_state)

        async def scenario():
            sink = narration_api._ChapterCheckboxSink(
                _DummySink(), job_id="legacy-job-2", total=1, admitted_chapters=[])
            tele = orchestrator_core.CallTelemetry(model="m", task_id="ch1", ok=True, chapter_id=None)
            sink(tele)
            await asyncio.sleep(0)

        run(scenario())
        assert captured == [(0, narration_api._STATUS_DONE, None)]


class TestDalangStatusAcceptsCompleteDirectIdentity:
    def test_status_succeeds_when_every_v1_checkbox_carries_its_own_chapter_id(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        admitted = snap["chapters"]

        # B-07/B-08 Rework 6 (Contract A): status now resolves a lifecycle-bound row by
        # UUID (the external-only bootstrap is gone) -- job_uuid supplied, db.get_job mocked.
        async def fake_get_job(tenant_id, job_uuid):
            return {"id": "status-uuid-1", "status": "processing",
                    "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}}

        async def fake_read_checkboxes(job_id):
            return None, 0, len(admitted), [
                {"no": i, "state": "done", "chapter_id": admitted[i]["chapter_id"]}
                for i in range(len(admitted))
            ]

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(narration_api, "db", database)
        monkeypatch.setattr(narration_api, "_read_checkboxes", fake_read_checkboxes)
        out = run(narration_api.narration_status("ext1", _user(), job_uuid="status-uuid-1"))
        assert out["found"] is True
        assert {c["chapter_id"] for c in out["chapters"]} == {c["chapter_id"] for c in admitted}


# ── 24. Rework 5 (Contract F): TRUE two-row lineage isolation -- language and binding for
#         a NAMED source_job_uuid always come from the SAME selected row, never a second
#         (potentially different) newest-by-external read; a v1 row named only by external
#         id rejects and requires the caller's UUID ────────────────────────────────────
class TestLineageTrueTwoRowIsolation:
    def test_language_and_binding_always_derive_from_the_same_selected_uuid(self, monkeypatch):
        outline_en = _outline()
        snap_en = _job_lifecycle(outline_en)
        outline_fr = admit_outline_lifecycle(
            "11111111-2222-4333-8444-555555555555", "fr",
            [{"id": "1", "title": "Un", "words": 300}])
        snap_fr = build_job_lifecycle(outline_fr, outline_fr["chapters"])

        rows = {
            "lineage-uuid-en": {"id": "lineage-uuid-en", "external_job_id": "ext-lineage",
                                "input_payload": {"narasi_lifecycle": snap_en}},
            "lineage-uuid-fr": {"id": "lineage-uuid-fr", "external_job_id": "ext-lineage",
                                "input_payload": {"narasi_lifecycle": snap_fr}},
        }

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        async def forbidden_get_job_by_external(tenant_id, job_id):
            raise AssertionError("get_job_by_external must never be consulted when job_uuid is supplied")

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_job_by_external", forbidden_get_job_by_external)
        monkeypatch.setattr(laozhang_api, "db", database)

        lang_en = run(laozhang_api._narasi_resolve_manuscript_lineage(
            "t1", source_job_id="ext-lineage", source_job_uuid="lineage-uuid-en", manuscript_language=None))
        lang_fr = run(laozhang_api._narasi_resolve_manuscript_lineage(
            "t1", source_job_id="ext-lineage", source_job_uuid="lineage-uuid-fr", manuscript_language=None))
        binding_en, uuid_en = run(laozhang_api._narasi_build_lineage_binding(
            "t1", "ext-lineage", "review_input", "text", source_job_uuid="lineage-uuid-en"))
        binding_fr, uuid_fr = run(laozhang_api._narasi_build_lineage_binding(
            "t1", "ext-lineage", "review_input", "text", source_job_uuid="lineage-uuid-fr"))

        assert lang_en == "en" and lang_fr == "fr"
        assert uuid_en == "lineage-uuid-en" and uuid_fr == "lineage-uuid-fr"
        assert binding_en["lifecycle_hash"] == snap_en["lifecycle_hash"]
        assert binding_fr["lifecycle_hash"] == snap_fr["lifecycle_hash"]
        assert binding_en["lifecycle_hash"] != binding_fr["lifecycle_hash"]

    def test_external_only_v1_source_rejects_and_requires_uuid(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)

        async def fake_get_job_by_external(tenant_id, job_id):
            return {"id": "lineage-uuid-x", "external_job_id": job_id,
                    "input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setattr(database, "get_job_by_external", fake_get_job_by_external)
        monkeypatch.setattr(laozhang_api, "db", database)

        with pytest.raises(HTTPException) as caught:
            run(laozhang_api._narasi_resolve_manuscript_lineage(
                "t1", source_job_id="ext-only", source_job_uuid=None, manuscript_language=None))
        assert caught.value.status_code == 409
        assert "V1_REQUIRES_UUID" in str(caught.value.detail)


# ── 25. Rework 5 (Contract G): the retained derived-input UUID is terminalized done/error
#         on every Review branch -- provider failure and full success, both observed via a
#         REAL finish_narasi_job_by_id call, never merely inferred from source text ──────
class TestReviewDerivedInputTerminalLifecycle:
    def _patch_common(self, monkeypatch, *, provider_should_fail):
        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_log_usage(*a, **kw):
            return 0

        async def fake_persist_asset(*a, **kw):
            return None

        async def fake_create_derived_input(*a, **kw):
            return "review-derived-uuid"

        terminalized = []

        async def fake_finish_narasi_job_by_id(tenant_id, job_uuid, status, result=None, error=None):
            terminalized.append((job_uuid, status, error))

        class _FailingReviewClient:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        raise ConnectionError("provider down")

        client_factory = (lambda model: _FailingReviewClient()) if provider_should_fail \
            else (lambda model: _FakeReviewClient())

        monkeypatch.setattr(laozhang_api, "make_client", client_factory)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
        monkeypatch.setattr(laozhang_api, "_persist_asset", fake_persist_asset)
        monkeypatch.setattr(database, "create_derived_input", fake_create_derived_input)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_narasi_job_by_id)
        monkeypatch.setattr(laozhang_api, "db", database)
        return terminalized

    def test_provider_failure_terminalizes_the_derived_row_as_error(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch, provider_should_fail=True)
        body = {"message": "please review this manuscript"}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(body, _user()))
        assert caught.value.status_code == 500
        assert len(terminalized) == 1
        assert terminalized[0][0] == "review-derived-uuid"
        assert terminalized[0][1] == "error"
        assert "provider down" in terminalized[0][2]

    def test_full_success_terminalizes_the_derived_row_as_done(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch, provider_should_fail=False)
        body = {"message": "please review this manuscript"}
        out = run(laozhang_api.narasi_review(body, _user()))
        assert out["ok"] is True
        assert terminalized == [("review-derived-uuid", "done", None)]

    def test_terminalize_write_failure_never_masks_the_original_provider_error(self, monkeypatch):
        # B-07/B-08 Rework 5 (Contract G): the error-path terminalize call is wrapped in its
        # own try/except specifically so a SECOND failure (the terminal write itself) can
        # never replace or swallow the FIRST, real failure the caller actually needs to see.
        self._patch_common(monkeypatch, provider_should_fail=True)

        async def raising_finish(tenant_id, job_uuid, status, result=None, error=None):
            raise ConnectionError("jobs table unreachable during terminalize")

        monkeypatch.setattr(database, "finish_narasi_job_by_id", raising_finish)
        body = {"message": "please review this manuscript"}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_review(body, _user()))
        assert caught.value.status_code == 500
        assert "provider down" in str(caught.value.detail)


# ── 26. Rework 5 (Contract G): One-Shot's derived-input row is terminalized across its
#         FULL branch set -- job creation failure, missing BYOK key, background provider
#         failure, and background full success -- never left "processing" on any of them ──
class TestOneshotDerivedInputTerminalLifecycle:
    def _patch_common(self, monkeypatch):
        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_create_derived_input(*a, **kw):
            return "oneshot-derived-uuid"

        terminalized = []

        async def fake_finish_narasi_job_by_id(tenant_id, job_uuid, status, result=None, error=None):
            terminalized.append((job_uuid, status, error))

        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(database, "create_derived_input", fake_create_derived_input)
        monkeypatch.setattr(database, "finish_narasi_job_by_id", fake_finish_narasi_job_by_id)
        monkeypatch.setattr(laozhang_api, "db", database)
        return terminalized

    def test_job_creation_failure_terminalizes_derived_row_before_any_background_work(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch)

        async def raising_create_job(*a, **kw):
            raise ConnectionError("jobs table unreachable")

        monkeypatch.setattr(database, "create_job", raising_create_job)

        body = {"content": "manuscript body", "system": "editor rules"}
        with pytest.raises(ConnectionError):
            run(laozhang_api.oneshot_fix_submit(body, _user()))
        assert terminalized == [("oneshot-derived-uuid", "error", terminalized[0][2])]
        assert "jobs table unreachable" in terminalized[0][2]

    def test_missing_byok_key_terminalizes_derived_row_as_error(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch)

        async def fake_create_job(*a, **kw):
            return "oneshot-job-1"

        async def fake_set_progress(*a, **kw):
            return None

        monkeypatch.setattr(database, "create_job", fake_create_job)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"set_progress": staticmethod(fake_set_progress)}))
        monkeypatch.setattr(laozhang_api, "DEEPSEEK_API_KEY", "")

        body = {"content": "manuscript body", "system": "editor rules", "model": "deepseek-v4-pro"}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.oneshot_fix_submit(body, _user()))
        assert caught.value.status_code == 400
        assert terminalized == [("oneshot-derived-uuid", "error", terminalized[0][2])]
        assert "DEEPSEEK_API_KEY" in terminalized[0][2]

    def test_background_provider_failure_terminalizes_derived_row_as_error(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch)

        async def fake_create_job(*a, **kw):
            return "oneshot-job-2"

        async def fake_set_progress(*a, **kw):
            return None

        class _RaisingOpenAI:
            def __init__(self, **kw):
                pass

            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        raise ConnectionError("oneshot provider down")

        fail_job_calls = []

        async def fake_fail_job(job_id, error):
            fail_job_calls.append((job_id, error))

        monkeypatch.setattr(database, "create_job", fake_create_job)
        monkeypatch.setattr(database, "fail_job", fake_fail_job)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"set_progress": staticmethod(fake_set_progress)}))
        monkeypatch.setattr(laozhang_api, "OpenAI", _RaisingOpenAI)

        real_create_task = asyncio.create_task
        captured = {}

        def spy_create_task(coro):
            task = real_create_task(coro)
            captured["task"] = task
            return task

        monkeypatch.setattr(laozhang_api.asyncio, "create_task", spy_create_task)
        body = {"content": "manuscript body", "system": "editor rules"}

        async def scenario():
            out = await laozhang_api.oneshot_fix_submit(body, _user())
            await captured["task"]
            return out

        out = run(scenario())
        assert out["ok"] is True   # submit itself succeeds; the failure is in the background
        assert fail_job_calls and fail_job_calls[0][0] == "oneshot-job-2"
        assert terminalized == [("oneshot-derived-uuid", "error", terminalized[0][2])]
        assert "oneshot provider down" in terminalized[0][2]

    def test_task_scheduling_failure_terminalizes_derived_row_as_error(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch)

        async def fake_create_job(*a, **kw):
            return "oneshot-job-4"

        async def fake_set_progress(*a, **kw):
            return None

        def raising_create_task(coro):
            coro.close()
            raise RuntimeError("event loop is shutting down")

        monkeypatch.setattr(database, "create_job", fake_create_job)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {"set_progress": staticmethod(fake_set_progress)}))
        monkeypatch.setattr(laozhang_api.asyncio, "create_task", raising_create_task)

        body = {"content": "manuscript body", "system": "editor rules"}
        with pytest.raises(RuntimeError):
            run(laozhang_api.oneshot_fix_submit(body, _user()))
        assert terminalized == [("oneshot-derived-uuid", "error", terminalized[0][2])]
        assert "event loop is shutting down" in terminalized[0][2]

    def test_background_full_success_terminalizes_derived_row_as_done(self, monkeypatch):
        terminalized = self._patch_common(monkeypatch)

        async def fake_create_job(*a, **kw):
            return "oneshot-job-3"

        async def fake_set_progress(*a, **kw):
            return None

        async def fake_delete_progress(*a, **kw):
            return None

        async def fake_log_usage(*a, **kw):
            return 0

        async def fake_save_correction_pair(*a, **kw):
            return {"quality_tier": "high", "edit_ratio": 0.1}

        async def fake_persist_asset(*a, **kw):
            return None

        complete_job_calls = []

        async def fake_complete_job(job_id, result):
            complete_job_calls.append((job_id, result))

        class _SucceedingOpenAI:
            def __init__(self, **kw):
                pass

            class chat:
                class completions:
                    @staticmethod
                    def create(**kw):
                        return _FakeChatResp(
                            "---FIXED_BOOK_START---\nfixed manuscript text\n---FIXED_BOOK_END---")

        monkeypatch.setattr(database, "create_job", fake_create_job)
        monkeypatch.setattr(database, "save_correction_pair", fake_save_correction_pair)
        monkeypatch.setattr(database, "complete_job", fake_complete_job)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
        monkeypatch.setattr(laozhang_api, "_persist_asset", fake_persist_asset)
        monkeypatch.setattr(laozhang_api, "rc", type("_R", (), {
            "set_progress": staticmethod(fake_set_progress),
            "delete_progress": staticmethod(fake_delete_progress),
        }))
        monkeypatch.setattr(laozhang_api, "OpenAI", _SucceedingOpenAI)

        real_create_task = asyncio.create_task
        captured = {}

        def spy_create_task(coro):
            task = real_create_task(coro)
            captured["task"] = task
            return task

        monkeypatch.setattr(laozhang_api.asyncio, "create_task", spy_create_task)
        body = {"content": "manuscript body", "system": "editor rules"}

        async def scenario():
            out = await laozhang_api.oneshot_fix_submit(body, _user())
            await captured["task"]
            return out

        out = run(scenario())
        assert out["ok"] is True
        assert complete_job_calls and complete_job_calls[0][0] == "oneshot-job-3"
        assert terminalized == [("oneshot-derived-uuid", "done", None)]


# ── 27. Rework 5 (Contract E): narasiLifecycle.mjs restore deep-validation -- REAL node
#         execution (never source-text) proving exact chapter identity/order, exact
#         artifact-binding schema, and cross-outline drift are all genuinely enforced ────
class TestNarasiLifecycleRestoreDeepValidation:
    def _envelope(self, outline_id="8d95ed8a-1853-4ea4-9f47-5ae00fb61e21", language="en"):
        return {
            "schema_version": "1", "outline_id": outline_id, "target_language": language,
            "status": "active",
            "chapters": [
                {"chapter_id": "ch_" + "a" * 32, "chapter_index": 0, "legacy_id": "1"},
                {"chapter_id": "ch_" + "b" * 32, "chapter_index": 1, "legacy_id": "2"},
            ],
            "lifecycle_hash": "c" * 64,
        }

    def _complete_snapshot(self, env):
        brief_text = "brief text"
        return {
            "target_language": env["target_language"], "outline": [], "outline_text": "text",
            "outline_lifecycle": env, "brief": brief_text,
            # B-07/B-08 Rework 6 (Contract D): content_hash is the REAL SHA-256 of brief_text --
            # a stub/placeholder hash alongside real Brief bytes is exactly the drift this
            # round's exact-byte verification exists to reject.
            "brief_binding": {
                "schema_version": "1", "outline_id": env["outline_id"],
                "lifecycle_hash": env["lifecycle_hash"], "target_language": env["target_language"],
                "artifact_kind": "brief",
                "content_hash": hashlib.sha256(brief_text.encode("utf-8")).hexdigest(),
            },
            "story_contract": None, "chapter_plan": None, "chapters": env["chapters"],
        }

    def test_valid_complete_draft_with_full_snapshot_roundtrips_as_v1_bound(self):
        env = self._envelope()
        snap = self._complete_snapshot(env)
        # B-07/B-08 Rework 6 (Contract D): EVERY field the snapshot mirrors (outline,
        # outline_text, brief, brief_binding, story_contract, chapter_plan) must equal the
        # TOP-LEVEL draft's own value exactly -- a real draft always carries all of them
        # (buildGenerationSnapshot is always called with the SAME values the caller just
        # set at top level), so a genuinely valid fixture must match every one, not just brief.
        draft = {"outline_lifecycle": env, "outline": snap["outline"], "outline_text": snap["outline_text"],
                 "brief": snap["brief"], "brief_binding": snap["brief_binding"],
                 "story_contract": snap["story_contract"], "chapter_plan": snap["chapter_plan"],
                 "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "v1_bound" and out["can_generate"] is True
        assert out["generation_snapshot"]["target_language"] == "en"

    def test_snapshot_with_reordered_chapters_rejects(self):
        env = self._envelope()
        snap = self._complete_snapshot(env)
        snap["chapters"] = list(reversed(env["chapters"]))
        draft = {"outline_lifecycle": env, "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1" and out["can_generate"] is False

    def test_snapshot_with_wrong_chapter_index_rejects(self):
        env = self._envelope()
        snap = self._complete_snapshot(env)
        snap["chapters"] = copy.deepcopy(env["chapters"])
        snap["chapters"][0]["chapter_index"] = 5
        draft = {"outline_lifecycle": env, "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"

    def test_snapshot_with_wrong_legacy_id_rejects(self):
        env = self._envelope()
        snap = self._complete_snapshot(env)
        snap["chapters"] = copy.deepcopy(env["chapters"])
        snap["chapters"][0]["legacy_id"] = "forged-legacy-id"
        draft = {"outline_lifecycle": env, "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"

    def test_snapshot_bound_to_a_different_outline_rejects(self):
        env = self._envelope()
        other_env = self._envelope(outline_id="99999999-9999-4999-8999-999999999999")
        snap = self._complete_snapshot(other_env)
        draft = {"outline_lifecycle": env, "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"

    def test_top_level_brief_binding_with_wrong_artifact_kind_rejects(self):
        env = self._envelope()
        binding = {"schema_version": "1", "outline_id": env["outline_id"],
                   "lifecycle_hash": env["lifecycle_hash"], "target_language": env["target_language"],
                   "artifact_kind": "review_input", "content_hash": "d" * 64}
        draft = {"outline_lifecycle": env, "brief_binding": binding}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"

    def test_top_level_brief_binding_bound_to_a_different_outline_rejects(self):
        env = self._envelope()
        binding = {"schema_version": "1", "outline_id": "22222222-2222-4222-8222-222222222222",
                   "lifecycle_hash": env["lifecycle_hash"], "target_language": env["target_language"],
                   "artifact_kind": "brief", "content_hash": "d" * 64}
        draft = {"outline_lifecycle": env, "brief_binding": binding}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"

    def test_valid_top_level_brief_binding_roundtrips(self):
        env = self._envelope()
        # B-07/B-08 Rework 6 (Contract D): content_hash must be the REAL SHA-256 of the
        # paired top-level brief bytes -- a stub hash is exactly what this round's exact-
        # byte verification now rejects, so this "valid" fixture must use a real one.
        brief_text = "a real top-level brief"
        binding = {"schema_version": "1", "outline_id": env["outline_id"],
                   "lifecycle_hash": env["lifecycle_hash"], "target_language": env["target_language"],
                   "artifact_kind": "brief",
                   "content_hash": hashlib.sha256(brief_text.encode("utf-8")).hexdigest()}
        draft = {"outline_lifecycle": env, "brief": brief_text, "brief_binding": binding}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "v1_bound" and out["brief_binding"] == binding and out["brief"] == brief_text

    def test_invalid_v1_echoes_raw_dependent_fields_verbatim(self):
        env = self._envelope()
        draft = {"outline_lifecycle": env, "brief_binding": {"forged": True},
                 "generation_snapshot": {"x": 9}, "brief": "raw brief bytes",
                 "source_job_uuid": "11111111-1111-4111-8111-111111111111"}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        # B-07/B-08 Rework 5: state/can_generate are the ONLY trust signal -- these raw
        # dependent fields are echoed unvalidated (main.jsx never reads them for a
        # non-v1_bound state), which is what lets this coexist with the pre-existing
        # immutable roundtrip test without weakening either assertion.
        assert out["state"] == "invalid_v1" and out["can_generate"] is False
        assert out["brief"] == "raw brief bytes"
        assert out["brief_binding"] == {"forged": True}
        assert out["generation_snapshot"] == {"x": 9}

    def test_snapshot_with_wrong_target_language_rejects(self):
        # ONLY the snapshot's own top-level target_language field is drifted -- its nested
        # outline_lifecycle still matches the restored envelope exactly -- isolating
        # isValidGenerationSnapshot's OWN target_language cross-check from the separate
        # nested-outline-language check that a fully-drifted fixture would also trip.
        env = self._envelope(language="en")
        snap = self._complete_snapshot(env)
        snap["target_language"] = "fr"
        draft = {"outline_lifecycle": env, "generation_snapshot": snap}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1" and out["can_generate"] is False

    def test_top_level_brief_binding_with_wrong_language_rejects(self):
        env = self._envelope(language="en")
        binding = {"schema_version": "1", "outline_id": env["outline_id"],
                   "lifecycle_hash": env["lifecycle_hash"], "target_language": "fr",
                   "artifact_kind": "brief", "content_hash": "d" * 64}
        draft = {"outline_lifecycle": env, "brief_binding": binding}
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1"


# ── 28. Rework 5 (Contract B.1/A): TRUE two-row isolation for stitch -- two rows
#         sharing one external_job_id, each with its OWN complete admitted chapter set;
#         stitching by each row's own UUID must never surface the other row's content ──
class TestStitchTrueTwoRowIsolation:
    def test_stitch_by_uuid_never_surfaces_the_other_shared_external_id_rows_content(self, monkeypatch):
        outline = _outline()
        snap = _job_lifecycle(outline)
        admitted = snap["chapters"]
        rows = {
            "stitch-uuid-a": {"id": "stitch-uuid-a", "external_job_id": "ext-stitch",
                              "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}},
            "stitch-uuid-b": {"id": "stitch-uuid-b", "external_job_id": "ext-stitch",
                              "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}},
        }
        chapters_by_uuid = {
            "stitch-uuid-a": [
                {"chapter_id": admitted[0]["chapter_id"], "chapter_index": 0, "content": "ROWA_CH0"},
                {"chapter_id": admitted[1]["chapter_id"], "chapter_index": 1, "content": "ROWA_CH1"},
            ],
            "stitch-uuid-b": [
                {"chapter_id": admitted[0]["chapter_id"], "chapter_index": 0, "content": "ROWB_CH0"},
                {"chapter_id": admitted[1]["chapter_id"], "chapter_index": 1, "content": "ROWB_CH1"},
            ],
        }

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        async def fake_get_narasi_chapters(tenant_id, job_uuid):
            return chapters_by_uuid.get(job_uuid, [])

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(database, "get_narasi_chapters", fake_get_narasi_chapters)
        monkeypatch.setattr(laozhang_api, "db", database)

        out_a = run(laozhang_api.narasi_stitch(
            "ext-stitch", {"job_uuid": "stitch-uuid-a", "language": "en"}, _user()))
        out_b = run(laozhang_api.narasi_stitch(
            "ext-stitch", {"job_uuid": "stitch-uuid-b", "language": "en"}, _user()))

        assert out_a["job_uuid"] == "stitch-uuid-a" and out_b["job_uuid"] == "stitch-uuid-b"
        a_contents = {c["content"] for c in out_a["chapters"]}
        b_contents = {c["content"] for c in out_b["chapters"]}
        assert a_contents == {"ROWA_CH0", "ROWA_CH1"}
        assert b_contents == {"ROWB_CH0", "ROWB_CH1"}
        assert a_contents.isdisjoint(b_contents)


# ── 29. Rework 5 (Contract H): TRUE two-row isolation for retry -- two rows sharing one
#         external_job_id, each admitted from a DIFFERENT outline; retrying against each
#         row's own source_job_uuid validates against THAT row's own admitted lifecycle,
#         never the other row's ─────────────────────────────────────────────────────
class TestRetryTrueTwoRowIsolation:
    def test_retry_validates_against_the_selected_rows_own_lifecycle_never_the_others(self, monkeypatch):
        outline_a = _outline()
        snap_a = _job_lifecycle(outline_a)
        outline_b = admit_outline_lifecycle(
            "11111111-2222-4333-8444-555555555555", "en",
            [{"id": "1", "title": "Other", "words": 300}])
        snap_b = build_job_lifecycle(outline_b, outline_b["chapters"])
        rows = {
            "retry-uuid-a": {"id": "retry-uuid-a", "external_job_id": "ext-retry",
                             "input_payload": {"narasi_lifecycle": snap_a}},
            "retry-uuid-b": {"id": "retry-uuid-b", "external_job_id": "ext-retry",
                             "input_payload": {"narasi_lifecycle": snap_b}},
        }

        async def fake_get_job(tenant_id, job_uuid):
            return rows.get(job_uuid)

        monkeypatch.setattr(database, "get_job", fake_get_job)
        monkeypatch.setattr(laozhang_api, "db", database)

        # Row A's own admitted chapter, retried against row A's own UUID -- must resolve.
        body_a = {"source_job_uuid": "retry-uuid-a", "outline_lifecycle": outline_a,
                  "model": "gemini-2.5-flash", "chapter_id": outline_a["chapters"][0]["chapter_id"]}

        def fake_complete(model, messages, max_tok, role="", phase=""):
            return _FakeRetryResp("regenerated chapter text word " * 10), model

        async def fake_log_usage(*a, **kw):
            return 0

        async def fake_resolve_user_uuid(t, u):
            return _UID

        async def fake_save_narasi_chapter(tenant_id, job_uuid, chapter_index, content,
                                           word_count, source_prompt, retrieved_ids, *, chapter_id=None):
            return "chapter-row-uuid"

        monkeypatch.setattr(laozhang_api, "_narasi_complete", fake_complete)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(database, "save_narasi_chapter", fake_save_narasi_chapter)
        out_a = run(laozhang_api.narasi_lifecycle_retry_chapter(body_a, _user()))
        assert out_a["ok"] is True

        # The SAME chapter_id (valid for row A's outline) retried against row B's UUID must
        # be rejected as CHAPTER_NOT_FOUND -- row B's own admitted outline never contains it.
        # This proves retry resolves row B's OWN lifecycle, never falling through to row A's.
        body_cross = {"source_job_uuid": "retry-uuid-b", "outline_lifecycle": outline_b,
                      "model": "gemini-2.5-flash", "chapter_id": outline_a["chapters"][0]["chapter_id"]}
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_lifecycle_retry_chapter(body_cross, _user()))
        assert caught.value.status_code == 404
        assert caught.value.detail["error"] == "CHAPTER_NOT_FOUND"


# ── 30. Rework 5 (Contract G): One-Shot's derived-input row creation failing -- like
#         Review's own pre-provider proof -- causes ZERO job creation, provider call, or
#         background task, since create_derived_input happens before ANY of them ────────
class TestOneshotPreProviderDerivedInputRuntimeProof:
    def test_oneshot_never_creates_a_job_or_calls_provider_when_derived_input_write_fails(self, monkeypatch):
        job_creation_calls = {"n": 0}

        async def raising_create_derived_input(*a, **kw):
            raise ConnectionError("derived-input db down")

        async def spy_create_job(*a, **kw):
            job_creation_calls["n"] += 1
            return "should-never-be-created"

        async def fake_resolve_user_uuid(t, u):
            return _UID

        monkeypatch.setattr(database, "create_derived_input", raising_create_derived_input)
        monkeypatch.setattr(database, "create_job", spy_create_job)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
        monkeypatch.setattr(laozhang_api, "db", database)

        real_create_task = asyncio.create_task
        task_spawned = {"n": 0}

        def spy_create_task(coro):
            task_spawned["n"] += 1
            return real_create_task(coro)

        monkeypatch.setattr(laozhang_api.asyncio, "create_task", spy_create_task)

        body = {"content": "manuscript body", "system": "editor rules"}
        with pytest.raises(ConnectionError):
            run(laozhang_api.oneshot_fix_submit(body, _user()))
        assert job_creation_calls["n"] == 0
        assert task_spawned["n"] == 0


_RUN_A = "33333333-3333-4333-8333-333333333333"
_RUN_B = "44444444-4444-4444-8444-444444444444"


# ── 31. Rework 6 (Contract A, V7 rows 1-3): no V1 external-only bootstrap remains in
#         status/stitch/chapter-readback -- a lifecycle-bound row selected by external id
#         alone is rejected with 409 BEFORE any Redis progress read or chapter fetch ────────
class TestNoV1ExternalBootstrapRework6:
    @pytest.mark.parametrize("surface", ["classic", "dalang"])
    def test_status_rejects_external_only_v1_before_redis(self, surface, monkeypatch):
        module = laozhang_api if surface == "classic" else narration_api
        snap = _job_lifecycle(_outline())
        touched = []

        async def external(*args):
            return {"id": _RUN_A, "external_job_id": "duplicate", "status": "processing",
                    "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}}

        async def progress(*args):
            touched.append(args)
            return "wrong-run"

        monkeypatch.setattr(module.db, "get_job_by_external", external)
        if surface == "classic":
            monkeypatch.setattr(module.rc, "get_progress", progress)
            call = module.narasi_status("duplicate", _user())
        else:
            monkeypatch.setattr(module, "_read_checkboxes", progress)
            call = module.narration_status("duplicate", _user())
        with pytest.raises(HTTPException) as caught:
            run(call)
        assert caught.value.status_code == 409
        assert touched == []

    def test_stitch_rejects_external_only_v1_before_chapter_read(self, monkeypatch):
        snap = _job_lifecycle(_outline())
        touched = []

        async def external(*args):
            return {"id": _RUN_A, "external_job_id": "duplicate",
                    "input_payload": {"narasi_lifecycle": snap}, "result_payload": {}}

        async def chapters(*args):
            touched.append(args)
            return []

        monkeypatch.setattr(laozhang_api.db, "get_job_by_external", external)
        monkeypatch.setattr(laozhang_api.db, "get_narasi_chapters", chapters)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_stitch("duplicate", {"language": "en"}, _user()))
        assert caught.value.status_code == 409 and touched == []

    def test_chapter_readback_propagates_external_only_v1_rejection(self, monkeypatch):
        snap = _job_lifecycle(_outline())

        async def external(*args):
            return {"id": _RUN_A, "external_job_id": "duplicate",
                    "input_payload": {"narasi_lifecycle": snap}}

        monkeypatch.setattr(laozhang_api.db, "get_job_by_external", external)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_chapters_list("duplicate", _user()))
        assert caught.value.status_code == 409


# ── 32. Rework 6 (Contract B, V7 rows 4-6, 9): rate-all is UUID-first end-to-end, two rows
#         sharing one external id are independently addressable by their own UUID, a
#         lifecycle-bound row can't be rated by external id alone, and the frontend's past-
#         jobs picker + rating readback + Cancel/retry/recover all key off job_uuid, never a
#         parallel activeRunUuidRef or `X||sourceJobUuid` stale-state fallback ─────────────
class TestUuidBoundRatingAndSelectionRework6:
    def test_rate_all_uses_exact_uuid_for_two_duplicate_rows(self, monkeypatch):
        rows = {
            _RUN_A: {"id": _RUN_A, "external_job_id": "duplicate"},
            _RUN_B: {"id": _RUN_B, "external_job_id": "duplicate"},
        }
        rated = []

        async def exact(tenant_id, job_uuid):
            return rows.get(job_uuid)

        async def forbidden(*args):
            raise AssertionError("rate-all used newest-by-external")

        async def save(tenant_id, user_id, job_uuid, rating):
            rated.append((job_uuid, rating))
            return 2

        async def resolve(*args):
            return _UID

        monkeypatch.setattr(laozhang_api.db, "get_job", exact)
        monkeypatch.setattr(laozhang_api.db, "get_job_by_external", forbidden)
        monkeypatch.setattr(laozhang_api.db, "save_approval_all", save)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", resolve)
        out_a = run(laozhang_api.narasi_rate_all(
            {"job_id": "duplicate", "job_uuid": _RUN_A, "rating": 5}, _user()))
        out_b = run(laozhang_api.narasi_rate_all(
            {"job_id": "duplicate", "job_uuid": _RUN_B, "rating": 4}, _user()))
        assert rated == [(_RUN_A, 5), (_RUN_B, 4)]
        assert out_a["job_uuid"] == _RUN_A and out_b["job_uuid"] == _RUN_B

    def test_rate_all_rejects_external_only_v1(self, monkeypatch):
        snap = _job_lifecycle(_outline())

        async def external(*args):
            return {"id": _RUN_A, "external_job_id": "duplicate",
                    "input_payload": {"narasi_lifecycle": snap}}

        async def resolve(*args):
            return _UID

        monkeypatch.setattr(laozhang_api.db, "get_job_by_external", external)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", resolve)
        with pytest.raises(HTTPException) as caught:
            run(laozhang_api.narasi_rate_all({"job_id": "duplicate", "rating": 5}, _user()))
        assert caught.value.status_code == 409

    def test_frontend_rate_all_transports_rating_uuid(self):
        text = _frontend_text("src/main.jsx")
        wrapper = next(line for line in text.splitlines() if "narasiRateAll:" in line)
        assert "jobUuid" in wrapper or "job_uuid" in wrapper
        state_region = text[text.index("const[chapRatings"):text.index("const requireRating")]
        assert "ratingJobUuid" in state_region
        assert re.search(r"narasiRateAll\([^\n]*(?:ratingJobUuid|job_uuid)", state_region)

    def test_duplicate_past_jobs_are_selected_by_uuid_not_external_id(self):
        text = _frontend_text("src/main.jsx")
        region = text[text.index("pastJobs.length>0"):text.index("{/* Config */")]
        assert re.search(r"<option\s+key=\{j\.job_uuid\}\s+value=\{j\.job_uuid\}", region)
        assert "pastJobs.find(pj=>pj.job_uuid===v)" in region
        assert re.search(r"recoverJob\([^,]+\.external_job_id\s*,\s*[^)]+\.job_uuid\)", region)

    def test_main_uses_mounted_run_authority_without_state_fallback(self):
        text = _frontend_text("src/main.jsx")
        assert "useNarasiRunAuthority" in text
        assert "activeRunUuidRef" not in text
        assert "||sourceJobUuid" not in text.replace(" ", "")


# ── 33. Rework 6 (Contract C, V7 row 10): Review and One-Shot each resolve their source-job
#         lineage with EXACTLY ONE db.get_job read per request -- never two independent
#         lookups (language, then binding) that a concurrent source-job mutation could split
#         across two different snapshot epochs ─────────────────────────────────────────────
class TestOneReadDerivedLineageRework6:
    @pytest.mark.parametrize("endpoint", ["review", "oneshot"])
    def test_endpoint_reads_source_snapshot_exactly_once(self, endpoint, monkeypatch):
        outline_a = _outline()
        snap_a = _job_lifecycle(outline_a)
        outline_b = admit_outline_lifecycle(
            "99999999-9999-4999-8999-999999999999", "fr",
            [{"id": "1", "title": "Autre", "words": 300}],
        )
        snap_b = build_job_lifecycle(outline_b, outline_b["chapters"])
        reads = []
        captured = []

        async def exact(*args):
            reads.append(args)
            snap = snap_a if len(reads) == 1 else snap_b
            return {"id": _RUN_A, "external_job_id": "duplicate",
                    "input_payload": {"narasi_lifecycle": snap}}

        async def resolve(*args):
            return _UID

        class StopAfterCreate(Exception):
            pass

        async def create(*args, **kwargs):
            captured.append(kwargs)
            raise StopAfterCreate()

        monkeypatch.setattr(laozhang_api.db, "get_job", exact)
        monkeypatch.setattr(laozhang_api.db, "create_derived_input", create)
        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", resolve)
        common = {"source_job_id": "duplicate", "source_job_uuid": _RUN_A,
                  "manuscript_language": "en"}
        if endpoint == "review":
            body = {**common, "message": "review text"}
            call = laozhang_api.narasi_review(body, _user())
        else:
            body = {**common, "content": "book text", "system": "rules"}
            call = laozhang_api.oneshot_fix_submit(body, _user())
        with pytest.raises(StopAfterCreate):
            run(call)
        assert len(reads) == 1
        assert captured[0]["language"] == "en"
        assert captured[0]["lineage_binding"]["lifecycle_hash"] == snap_a["lifecycle_hash"]


# ── 34. Rework 6 (Contract D, V7 rows 11-13): restore verifies the REAL SHA-256 of the
#         exact Brief bytes (top-level and mirrored-in-snapshot) against brief_binding's
#         content_hash, and cross-checks the snapshot's mirrored outline_text against the
#         top-level draft's own value -- a byte-for-byte or mirrored-field drift is rejected
#         even when every schema/shape check alone would have passed ──────────────────────
class TestExactRestoreBytesRework6:
    def _envelope(self):
        return {
            "schema_version": "1",
            "outline_id": "8d95ed8a-1853-4ea4-9f47-5ae00fb61e21",
            "target_language": "en", "status": "active",
            "chapters": [{"chapter_id": "ch_" + "a" * 32,
                          "chapter_index": 0, "legacy_id": "1"}],
            "lifecycle_hash": "b" * 64,
        }

    def _binding(self, env, text):
        return {"schema_version": "1", "outline_id": env["outline_id"],
                "lifecycle_hash": env["lifecycle_hash"], "target_language": "en",
                "artifact_kind": "brief",
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}

    @pytest.mark.parametrize("mutation", ["top_brief", "snapshot_brief", "snapshot_outline"])
    def test_restore_rejects_bytes_or_mirrored_snapshot_drift(self, mutation):
        env = self._envelope()
        binding = self._binding(env, "expected brief")
        draft = {"outline_lifecycle": env, "outline": copy.deepcopy(env["chapters"]),
                 "outline_text": "canonical outline", "brief": "expected brief",
                 "brief_binding": binding, "story_contract": None, "chapter_plan": None}
        if mutation == "top_brief":
            draft["brief"] = "mutated brief"
        else:
            snap = {
                "target_language": "en", "outline": copy.deepcopy(draft["outline"]),
                "outline_text": draft["outline_text"], "outline_lifecycle": env,
                "brief": draft["brief"], "brief_binding": binding,
                "story_contract": None, "chapter_plan": None,
                "chapters": copy.deepcopy(env["chapters"]),
            }
            if mutation == "snapshot_brief":
                snap["brief"] = "mutated snapshot brief"
            else:
                snap["outline_text"] = "mutated outline"
            draft["generation_snapshot"] = snap
        out = node_eval(f"m.restoreLifecycleDraft({json.dumps(draft)})")
        assert out["state"] == "invalid_v1" and out["can_generate"] is False


# ── 35. Rework 6 (Contract F, V7 rows 14-15): terminal derived-input writes go through one
#         shared bounded/idempotent helper -- a transient first write failure is retried and
#         reaches "error" durably, for both Review and One-Shot ───────────────────────────
class TestTerminalWriteRetryRework6:
    @pytest.mark.parametrize("endpoint", ["review", "oneshot"])
    def test_transient_terminal_write_is_retried_to_error(self, endpoint, monkeypatch):
        attempts = []

        async def resolve(*args):
            return _UID

        async def create(*args, **kwargs):
            return "derived-row"

        async def finish(tenant_id, job_uuid, status, result=None, error=None):
            attempts.append((job_uuid, status))
            if len(attempts) == 1:
                raise ConnectionError("transient terminal write")

        monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", resolve)
        monkeypatch.setattr(laozhang_api.db, "create_derived_input", create)
        monkeypatch.setattr(laozhang_api.db, "finish_narasi_job_by_id", finish)

        if endpoint == "review":
            class FailingClient:
                class chat:
                    class completions:
                        @staticmethod
                        def create(**kwargs):
                            raise ConnectionError("provider down")

            monkeypatch.setattr(laozhang_api, "make_client", lambda model: FailingClient())
            with pytest.raises(HTTPException):
                run(laozhang_api.narasi_review(
                    {"message": "review", "manuscript_language": "en"}, _user()))
        else:
            async def create_job(*args, **kwargs):
                return "oneshot-job"

            async def set_progress(*args, **kwargs):
                return None

            async def fail_job(*args, **kwargs):
                return None

            class FailingOpenAI:
                def __init__(self, **kwargs):
                    pass

                class chat:
                    class completions:
                        @staticmethod
                        def create(**kwargs):
                            raise ConnectionError("provider down")

            monkeypatch.setattr(laozhang_api.db, "create_job", create_job)
            monkeypatch.setattr(laozhang_api.db, "fail_job", fail_job)
            monkeypatch.setattr(laozhang_api, "rc", SimpleNamespace(set_progress=set_progress))
            monkeypatch.setattr(laozhang_api, "OpenAI", FailingOpenAI)
            captured = {}
            real_create_task = asyncio.create_task

            def spawn(coro):
                task = real_create_task(coro)
                captured["task"] = task
                return task

            monkeypatch.setattr(laozhang_api.asyncio, "create_task", spawn)

            async def scenario():
                await laozhang_api.oneshot_fix_submit(
                    {"content": "book", "system": "rules", "manuscript_language": "en"}, _user())
                await captured["task"]

            run(scenario())
        assert attempts == [("derived-row", "error"), ("derived-row", "error")]


# ── 36. Rework 6 (Contract E, V7 row 17): the REAL run-authority hook, mounted in a genuine
#         headless-Chrome + Vite harness (not source-text inspection) -- proves the
#         bind/cancel/select/recover/retry transport calls fire in the exact expected
#         sequence, including the immediate start/cancel race ─────────────────────────────
class TestMountedRunAuthorityTransportRework6:
    def test_real_react_mounted_run_authority_transport(self):
        proc = subprocess.run(
            ["node", str(_PACK_DIR / "run-mounted-frontend.mjs"), str(_FRONTEND_DIR)],
            cwd=_PACK_DIR, text=True, capture_output=True, timeout=45, check=False,
            env={**os.environ, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"},
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "MOUNTED_FRONTEND_PASS" in proc.stdout


# ── 37. V8 (Codex reconciliation): restoreLifecycleDraft rejects a Brief binding whose
#         content_hash correctly equals SHA-256 of the empty string when Brief bytes are
#         empty/absent -- both at the top level and inside a nested generation_snapshot.
#         Hashing alone can never substitute for the presence check: sha256("") is a fixed,
#         publicly computable constant, so hash-only validation would accept a forged
#         binding paired with no real Brief at all. The invariant is symmetric: Brief bytes
#         and Brief binding must either both be present or both be absent ──────────────────
class TestRestoreRejectsBindingWithoutBriefBytes:
    def test_restore_rejects_binding_when_brief_bytes_are_absent(self):
        empty_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        envelope = {
            "schema_version": "1",
            "outline_id": "8d95ed8a-1853-4ea4-9f47-5ae00fb61e21",
            "target_language": "en", "status": "active",
            "chapters": [{"chapter_id": "ch_" + "a" * 32,
                          "chapter_index": 0, "legacy_id": "1"}],
            "lifecycle_hash": "b" * 64,
        }
        binding = {
            "schema_version": "1", "outline_id": envelope["outline_id"],
            "lifecycle_hash": envelope["lifecycle_hash"], "target_language": "en",
            "artifact_kind": "brief", "content_hash": empty_hash,
        }
        base = {
            "outline_lifecycle": envelope, "outline": copy.deepcopy(envelope["chapters"]),
            "outline_text": "canonical outline", "brief": "",
            "story_contract": None, "chapter_plan": None,
        }
        top_level = {**base, "brief_binding": binding}
        nested = {
            **base, "brief_binding": None,
            "generation_snapshot": {
                "target_language": "en", "outline": copy.deepcopy(envelope["chapters"]),
                "outline_text": "canonical outline", "outline_lifecycle": envelope,
                "brief": "", "brief_binding": binding, "story_contract": None,
                "chapter_plan": None, "chapters": copy.deepcopy(envelope["chapters"]),
            },
        }
        outputs = node_eval(
            f'[{json.dumps(top_level)},{json.dumps(nested)}].map(x=>m.restoreLifecycleDraft(x))')
        assert [item["state"] for item in outputs] == ["invalid_v1", "invalid_v1"]
        assert all(item["can_generate"] is False for item in outputs)
