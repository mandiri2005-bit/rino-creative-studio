# -*- coding: utf-8 -*-
"""
Project Dalang — narration_api (WS-8, runtime convergence).

ONE production job contract for narration across the Python and Node paths.

This module is the consolidation of the (non-existent-in-repo) `narration_api.py`
prototype: a single background-job runtime that drives the unified orchestration
engine (`orchestrator.router.generate_narration`) and exposes it as a clean,
pollable job — the SAME contract whether the request arrives from the Python UI
(Gradio / direct) or from the Node Google path (backend/server.js).

It does NOT re-implement client/routing/RAG/assembly/anti-drift — those all live
in the `orchestrator` + `pakem` packages (WS-1..WS-7). WS-8 only adds the
*production envelope* around a generation run, reusing the EXACT primitives the
existing video / TTS / narasi jobs already use:

  * Auth        — auth_middleware.get_current_user (Clerk JWT → tenant/user).
  * Job row     — database.create_narasi_job / finish_narasi_job (asyncpg, RLS
                  via the tenant-scoped query helpers). The jobs table is the
                  durable source of truth for status/result/error.
  * Progress    — redis_client: a per-chapter HASH `narration:{id}:chapters`
                  (chapter:N = pending→running→done/failed) that the UI polls so
                  individual checkboxes light up as `asyncio.as_completed` lands
                  each chapter; plus rc.set_progress for the human string.
  * Cancel      — redis_client cancel flag `narration_{id}` (rc.set_cancel /
                  rc.is_cancelled), checked by the runtime between chapters.
  * Status m/c  — running → polishing → done | failed | cancelled, mirrored into
                  both Redis (`narration:{id}:status`) and the jobs row.
  * Metering    — a credit HOLD across the whole (long) job via
                  metering.begin_charge (HTTP 402 up front if short), kept warm
                  with credits.touch_hold so its TTL never lapses mid-flight, and
                  settled at ACTUAL token cost (refunded on cancel / zero output).
  * usage_logs  — cost rows written from the orchestrator's per-call telemetry
                  sink (tokens in/out + estimated USD) → database.log_usage.

Endpoints (registered on the SHARED `laozhang_api.app`):
  * POST /narration            → 202; init the Redis checkbox hash (expire 1h),
                                 create the jobs row, HOLD credits, kick off
                                 generate_narration in a background task; returns
                                 {job_id, status:"running", total}.
  * GET  /narration/{id}       → {status, done, total, chapters:[...], error,
                                 progress, output?}. Reads Redis (fast) first,
                                 falls back to the durable jobs row.
  * POST /narration/{id}/cancel→ set the cancel flag; the runtime stops after the
                                 current chapter and refunds the unused hold.

Importing this module registers the routes as a side effect (it shares the one
FastAPI app). To activate, `import narration_api` after `laozhang_api` is loaded
(e.g. add `import narration_api  # noqa: F401` near the bottom of laozhang_api,
or import it in app.py). It is additive and never shadows existing routes.

Smoke-safe: every heavy dependency (db, redis, metering, orchestrator) is reused
by import, and every call into them is wrapped so a missing live backend degrades
to a best-effort no-op rather than crashing the module import or a request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import Depends, HTTPException

# Reuse the ONE app + the real production primitives. These imports are the whole
# point of WS-8 convergence — import, never reinvent.
from laozhang_api import app, _resolve_user_uuid  # shared FastAPI app + Clerk→UUID
import redis_client as rc
import database as db
import metering
import credits as credits_lib
from auth_middleware import get_current_user, CurrentUser

# The orchestration engine front door (WS-6). NEVER raises into us.
from orchestrator.router import generate_narration
# Telemetry record type so the usage sink can read tokens/cost off each call.
from orchestrator.core import CallTelemetry

log = logging.getLogger("narration_api")

# ---------------------------------------------------------------------------
# Redis key layout for a narration job. The jobs table stays the durable source
# of truth; Redis only fronts the fast-changing per-chapter checkbox state and
# the status/progress strings so GET /narration/{id} is cheap and the DB isn't
# written on every chapter tick.
# ---------------------------------------------------------------------------
_CHAPTERS_TTL = 3600          # 1h — the checkbox hash + status expire together
_STATUS_PENDING = "pending"
_STATUS_RUNNING = "running"
_STATUS_POLISHING = "polishing"
_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"

# Map the runtime status → the jobs.status_enum the DB accepts (running/polishing
# both persist as 'processing'; terminal states map 1:1 except 'failed'→'error').
_DB_STATUS = {
    _STATUS_RUNNING: "processing",
    _STATUS_POLISHING: "processing",
    _STATUS_DONE: "done",
    _STATUS_FAILED: "error",
    _STATUS_CANCELLED: "cancelled",
}


def _chapters_key(job_id: str) -> str:
    return f"narration:{job_id}:chapters"


def _status_key(job_id: str) -> str:
    return f"narration:{job_id}:status"


def _cancel_token(job_id: str) -> str:
    # rc.set_cancel/is_cancelled prefix this with 'cancel:' internally.
    return f"narration_{job_id}"


# ---------------------------------------------------------------------------
# Redis helpers — all best-effort (never raise; a Redis outage must not wedge a
# job, exactly like redis_client's own contract).
# ---------------------------------------------------------------------------
async def _redis():
    """The shared async Redis client, or None if unavailable."""
    try:
        return rc.client()
    except Exception:  # noqa: BLE001
        return None


async def _init_checkboxes(job_id: str, total: int) -> None:
    """Seed the per-chapter checkbox hash (all 'pending') + the status, expire 1h.
    The UI renders one checkbox per `chapter:N` field and flips it as the field
    moves pending → running → done/failed."""
    r = await _redis()
    if r is None:
        return
    try:
        mapping = {f"chapter:{i}": _STATUS_PENDING for i in range(total)}
        mapping["total"] = str(total)
        mapping["done"] = "0"
        key = _chapters_key(job_id)
        await r.delete(key)
        if mapping:
            await r.hset(key, mapping=mapping)
            await r.expire(key, _CHAPTERS_TTL)
        await r.set(_status_key(job_id), _STATUS_RUNNING, ex=_CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("init_checkboxes(%s) failed: %s", job_id, e)


async def _set_chapter_state(job_id: str, no: int, state: str) -> None:
    """Flip one chapter's checkbox field; bump the 'done' counter on terminal states."""
    r = await _redis()
    if r is None:
        return
    try:
        key = _chapters_key(job_id)
        await r.hset(key, f"chapter:{no}", state)
        if state in (_STATUS_DONE, _STATUS_FAILED):
            await r.hincrby(key, "done", 1)
        await r.expire(key, _CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("set_chapter_state(%s,%d,%s) failed: %s", job_id, no, state, e)


async def _set_status(job_id: str, status: str) -> None:
    r = await _redis()
    if r is None:
        return
    try:
        await r.set(_status_key(job_id), status, ex=_CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("set_status(%s,%s) failed: %s", job_id, status, e)


async def _read_checkboxes(job_id: str) -> tuple[Optional[str], int, int, list[dict]]:
    """Read (status, done, total, chapters[]) from Redis. Returns (None,0,0,[]) if
    the hash is gone (expired/never-existed) so GET can fall back to the DB."""
    r = await _redis()
    if r is None:
        return None, 0, 0, []
    try:
        status = await r.get(_status_key(job_id))
        h = await r.hgetall(_chapters_key(job_id))
        if not h:
            return status, 0, 0, []
        total = int(h.get("total", 0) or 0)
        done = int(h.get("done", 0) or 0)
        chapters = []
        for i in range(total):
            chapters.append({"no": i, "state": h.get(f"chapter:{i}", _STATUS_PENDING)})
        return status, done, total, chapters
    except Exception as e:  # noqa: BLE001
        log.warning("read_checkboxes(%s) failed: %s", job_id, e)
        return None, 0, 0, []


# ---------------------------------------------------------------------------
# Telemetry → usage_logs. The orchestrator emits one CallTelemetry per LLM call
# (worker chapters AND the manager polish). We (a) accumulate the run total so the
# credit hold settles at ACTUAL cost, and (b) write a usage_logs row per call so
# nothing is invisible — mirroring how _log_narasi_usage records each chapter.
# ---------------------------------------------------------------------------
class _UsageSink:
    """A telemetry sink (callable taking a CallTelemetry) that totals tokens/cost
    for hold-settlement and fans each call out to a usage_logs row. Must never
    raise back into the generation path (the orchestrator guards this too)."""

    __slots__ = ("tenant_id", "user_id", "job_uuid", "tokens_in", "tokens_out",
                 "cost_usd", "calls", "_loop")

    def __init__(self, tenant_id: str, user_id: Optional[str], job_uuid: Optional[str]):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.job_uuid = job_uuid
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.calls = 0
        try:
            self._loop = asyncio.get_event_loop()
        except Exception:  # noqa: BLE001
            self._loop = None

    def __call__(self, t: CallTelemetry) -> None:
        # Accumulate synchronously (the sink is called from worker threads/coros).
        try:
            self.tokens_in += int(t.tokens_in or 0)
            self.tokens_out += int(t.tokens_out or 0)
            self.cost_usd += float(t.cost_usd or 0.0)
            self.calls += 1
        except Exception:  # noqa: BLE001
            pass
        # Fan one durable usage row out, best-effort. Schedule it on the loop so we
        # don't block generation on a DB round-trip; swallow everything.
        try:
            loop = self._loop or asyncio.get_event_loop()
            loop.create_task(self._log_one(t))
        except Exception:  # noqa: BLE001
            pass

    async def _log_one(self, t: CallTelemetry) -> None:
        try:
            await db.log_usage(
                self.tenant_id, self.user_id, t.model, "narasi",
                int(t.tokens_in or 0), int(t.tokens_out or 0), float(t.cost_usd or 0.0),
                job_id=self.job_uuid, provider=None,
                latency_ms=int(t.latency_ms or 0),
                finish_reason=t.finish_reason or ("error" if not t.ok else "stop"),
                http_status=200 if t.ok else 502, credits=0)
        except Exception as e:  # noqa: BLE001
            log.debug("usage sink log_one failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Cooperative cancel — a telemetry sink can't cancel, but the orchestrator runs
# chapters via asyncio.as_completed inside narrate_chapters. We can't reach into
# that loop, so cancellation is enforced at the JOB boundary: we race the whole
# generate_narration coroutine against a cancel-watcher; if the flag flips we
# cancel the task, mark the job cancelled, and refund the hold. Chapters already
# completed are still persisted by the runtime's own progress writes.
# ---------------------------------------------------------------------------
async def _cancel_watcher(job_id: str, poll: float = 1.5) -> None:
    """Resolve as soon as the cancel flag is observed in Redis."""
    while True:
        try:
            if await rc.is_cancelled(_cancel_token(job_id)):
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(poll)


# ---------------------------------------------------------------------------
# The background runtime — drives generate_narration with the production envelope.
# ---------------------------------------------------------------------------
async def _run_narration_job(
    *, body: dict, job_id: str, job_uuid: Optional[str],
    tenant_id: str, user_id: Optional[str], total: int,
    meter_op: Optional[str], model: str,
) -> None:
    """Background task: hold → generate → settle/refund, with per-chapter Redis
    checkboxes, a status machine, durable persistence, and cancel handling.
    NEVER raises (it's a fire-and-forget create_task; an escaping exception would
    be an unhandled-task warning and a stranded hold)."""
    sink = _UsageSink(tenant_id, user_id, job_uuid)
    started = time.monotonic()
    charge_settled = False

    # Per-chapter checkbox driver. generate_narration doesn't stream chapter
    # completions back to us, so we approximate live checkbox lighting by polling
    # the durable narasi_chapters writes the runtime makes — but the orchestrator
    # writes chapters all at once at the end. To still light checkboxes AS work
    # lands, we pass a telemetry sink that flips the chapter field when its worker
    # call returns. CallTelemetry.task_id is "chN" (1-based) for chapter workers.
    def _checkbox_from_telemetry(t: CallTelemetry) -> None:
        sink(t)  # keep accounting + usage logging
        tid = (t.task_id or "")
        if tid.startswith("ch") and tid[2:].isdigit():
            no = int(tid[2:]) - 1
            state = _STATUS_DONE if t.ok else _STATUS_FAILED
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_set_chapter_state(job_id, no, state))
            except Exception:  # noqa: BLE001
                pass

    req = dict(body or {})
    req.update({
        "job_id": job_id,
        "tenant_id": tenant_id,
        "telemetry_sink": _checkbox_from_telemetry,
    })

    # Keep the credit hold's TTL warm across a long job so it never lapses and
    # strands the reservation. Runs alongside the cancel watcher.
    async def _keep_hold_warm() -> None:
        if not meter_op:
            return
        while True:
            try:
                await credits_lib.touch_hold(tenant_id, meter_op)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    gen_task = asyncio.ensure_future(generate_narration(req))
    cancel_task = asyncio.ensure_future(_cancel_watcher(job_id))
    warm_task = asyncio.ensure_future(_keep_hold_warm())

    result: Optional[dict] = None
    cancelled = False
    try:
        await _set_status(job_id, _STATUS_RUNNING)
        await _safe_progress(job_id, "Menyusun narasi...")

        done, pending = await asyncio.wait(
            {gen_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and not gen_task.done():
            # Cancel requested mid-flight → stop generation after the in-flight call.
            cancelled = True
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        else:
            # Generation finished first (or together) → take its result.
            try:
                result = await gen_task
            except Exception as exc:  # noqa: BLE001 - router never raises, belt+braces
                log.exception("narration job %s: generate_narration raised", job_id)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        for t in (cancel_task, warm_task):
            if not t.done():
                t.cancel()
        # Drain cancellations quietly.
        for t in (cancel_task, warm_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --------------------- terminal handling ------------------------------
    if cancelled:
        await _finalize(
            job_id, job_uuid, tenant_id, status=_STATUS_CANCELLED,
            result=None, error="Job cancelled by user")
        await _refund(meter_op, tenant_id, job_id)
        return

    result = dict(result or {})
    ok = bool(result.get("ok")) and bool(result.get("book") or result.get("output"))

    # Light any chapter checkboxes the telemetry path didn't catch (e.g. a chapter
    # whose worker was short-circuited) from the final chapter records.
    await _reconcile_checkboxes(job_id, result, total)

    if not ok:
        await _finalize(
            job_id, job_uuid, tenant_id, status=_STATUS_FAILED,
            result=_result_payload(result), error=str(result.get("error") or "generation_failed"))
        await _refund(meter_op, tenant_id, job_id)
        return

    # Success: persist chapters + the assembled script, settle the hold at ACTUAL.
    await _set_status(job_id, _STATUS_POLISHING if result.get("polished") else _STATUS_DONE)
    await _persist_chapters(tenant_id, job_uuid, result)
    await _finalize(
        job_id, job_uuid, tenant_id, status=_STATUS_DONE,
        result=_result_payload(result), error=None)

    # Settle the credit hold at the real token total the sink accumulated.
    await _settle(meter_op, tenant_id, user_id, model, job_uuid, sink)
    charge_settled = True
    log.info("narration job %s done in %.1fs (%d calls, tok_in=%d tok_out=%d)",
             job_id, time.monotonic() - started, sink.calls, sink.tokens_in, sink.tokens_out)
    # Defensive: if we somehow reached here without settling, refund.
    if not charge_settled:
        await _refund(meter_op, tenant_id, job_id)


# ---------------------------------------------------------------------------
# Terminal / persistence helpers — all best-effort.
# ---------------------------------------------------------------------------
def _result_payload(result: dict) -> dict:
    """The durable result_payload stored on the jobs row. Keep it bounded so we
    don't bloat the row with megabytes — the full chapters live in
    narasi_chapters; here we keep the assembled markdown + run metadata."""
    book = result.get("book") or result.get("output") or ""
    return {
        "markdown": book,
        "scenario": result.get("scenario"),
        "strategy": result.get("strategy"),
        "polished": bool(result.get("polished")),
        "rag_used": bool(result.get("rag_used")),
        "n_ok": result.get("n_ok"),
        "n_total": result.get("n_total"),
        "settings": result.get("settings"),
        "outline_source": result.get("outline_source"),
    }


async def _safe_progress(job_id: str, msg: str) -> None:
    try:
        await rc.set_progress(job_id, msg, ttl=_CHAPTERS_TTL)
    except Exception:  # noqa: BLE001
        pass


async def _reconcile_checkboxes(job_id: str, result: dict, total: int) -> None:
    """Make the checkbox hash agree with the final chapter records (in case a
    worker telemetry event was missed)."""
    chapters = result.get("chapters")
    if not isinstance(chapters, list):
        return
    r = await _redis()
    if r is None:
        return
    try:
        key = _chapters_key(job_id)
        ndone = 0
        for rec in chapters:
            no = int(rec.get("no", 0))
            state = _STATUS_DONE if rec.get("ok") else _STATUS_FAILED
            await r.hset(key, f"chapter:{no}", state)
            ndone += 1
        await r.hset(key, "done", str(ndone))
        await r.expire(key, _CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.debug("reconcile_checkboxes(%s) failed: %s", job_id, e)


async def _persist_chapters(tenant_id: str, job_uuid: Optional[str], result: dict) -> None:
    """Write each chapter to narasi_chapters (durable read-back). Idempotent on
    (job_id, chapter_index). Skips if we have no internal job UUID (RLS needs it)."""
    if not job_uuid:
        return
    chapters = result.get("chapters")
    if not isinstance(chapters, list):
        return
    for rec in chapters:
        try:
            content = rec.get("content") or ""
            wc = len((content or "").split())
            await db.save_narasi_chapter(
                tenant_id, job_uuid, int(rec.get("no", 0)), content,
                word_count=wc, source_prompt="", retrieved_ids=[],
                version=1, approved=False)
        except Exception as e:  # noqa: BLE001
            log.warning("persist chapter %s failed (non-fatal): %s", rec.get("no"), e)


async def _finalize(job_id: str, job_uuid: Optional[str], tenant_id: str, *,
                    status: str, result: Optional[dict], error: Optional[str]) -> None:
    """Write the terminal status to BOTH Redis (fast) and the jobs row (durable)."""
    await _set_status(job_id, status)
    try:
        await rc.set_progress(
            job_id,
            {"done": "Selesai", "failed": f"Gagal: {error}",
             "cancelled": "Dibatalkan"}.get(status, status),
            ttl=_CHAPTERS_TTL)
    except Exception:  # noqa: BLE001
        pass
    try:
        await db.finish_narasi_job(
            tenant_id, job_id, _DB_STATUS.get(status, "error"),
            result=json.dumps(result) if result is not None else None,
            error=error)
    except Exception as e:  # noqa: BLE001
        log.warning("finish_narasi_job(%s,%s) failed (non-fatal): %s", job_id, status, e)


async def _settle(meter_op: Optional[str], tenant_id: str, user_id: Optional[str],
                  model: str, job_uuid: Optional[str], sink: _UsageSink) -> None:
    """Settle the credit hold at the ACTUAL accumulated token total."""
    if not meter_op:
        return
    try:
        charge = metering.Charge(
            tenant_id=tenant_id, user_id=user_id, op_id=meter_op,
            operation="narasi", model=model, held=0)
        await charge.settle(
            {"tokens_in": sink.tokens_in, "tokens_out": sink.tokens_out},
            job_id=job_uuid, tok_in=sink.tokens_in, tok_out=sink.tokens_out)
    except Exception as e:  # noqa: BLE001
        log.warning("settle hold(%s) failed (non-fatal): %s", meter_op, e)


async def _refund(meter_op: Optional[str], tenant_id: str, job_id: str) -> None:
    """Refund the unused hold (cancel / failure / zero output)."""
    if not meter_op:
        return
    try:
        await credits_lib.refund(tenant_id, meter_op)
    except Exception as e:  # noqa: BLE001
        log.warning("refund hold(%s) for %s failed (non-fatal): %s", meter_op, job_id, e)


# ===========================================================================
# Endpoints — the ONE job contract. Registered on the shared laozhang_api.app.
# ===========================================================================
def _count_chapters(body: dict) -> int:
    """Best-effort estimate of how many chapters the run will produce, so we can
    seed the right number of checkboxes up front. Mirrors the router's shape
    inspection: an explicit list wins; else n_chapters; else 1."""
    for key in ("chapters", "outline", "titles"):
        v = body.get(key)
        if isinstance(v, list) and v:
            return len(v)
    for key in ("n_chapters", "num_chapters"):
        v = body.get(key)
        try:
            if v and int(v) > 0:
                return int(v)
        except (TypeError, ValueError):
            pass
    return 1


@app.post("/narration", status_code=202)
async def narration_start(body: dict, user: CurrentUser = Depends(get_current_user)):
    """Start a unified narration job. Returns 202 immediately with the job id.

    Body is the orchestrator request (topic / chapters / brief / goal / style /
    language / mode / n_chapters / ...). The SHAPE drives routing — see
    orchestrator.router. We add the production envelope: Redis checkboxes, a
    credit hold, a durable jobs row, and a background generation task.
    """
    tenant_id = user.tenant_id
    try:
        user_uuid = await _resolve_user_uuid(user.tenant_id, user.user_id)
    except Exception:  # noqa: BLE001
        user_uuid = None

    body = dict(body or {})
    job_id = (str(body.get("pre_job_id") or uuid.uuid4().hex[:8]))[:16]
    topic = str(body.get("topic") or body.get("goal") or body.get("brief") or "").strip()
    model = str(body.get("worker_model") or os.environ.get("WORKER_MODEL")
                or "gemini-2.5-flash").strip()
    total = _count_chapters(body)

    # ── Credit HOLD up front (HTTP 402 if short). BYOK pays upstream → no hold. ──
    meter_op = None
    try:
        byok = _byok()
        if not byok:
            est_units = {
                "tokens_in": 1500 * max(1, total),
                "tokens_out": sum(
                    int((c.get("word_target") or c.get("words") or 800))
                    for c in (body.get("chapters") or [{}] * total)
                ) * 2 or (800 * total * 2),
            }
            meter_op = f"narration:{job_id}:{uuid.uuid4().hex[:8]}"
            await metering.begin_charge(
                tenant_id=tenant_id, user_id=user_uuid, operation="narasi",
                model=model, estimate_units=est_units, op_id=meter_op)
    except HTTPException:
        raise  # 402 surfaces to the client untouched
    except Exception as e:  # noqa: BLE001 - never let a metering hiccup block a job
        log.warning("narration hold skipped (non-fatal): %s", e)
        meter_op = None

    # ── Durable jobs row (poll can see it immediately) ──
    job_uuid = None
    try:
        await db.create_narasi_job(tenant_id, user_uuid, job_id, topic, total)
        _row = await db.get_job_by_external(tenant_id, job_id)
        job_uuid = _row.get("id") if _row else None
    except Exception as e:  # noqa: BLE001
        log.warning("create narration job row failed (non-fatal): %s", e)

    # ── Seed the per-chapter checkbox hash (expire 1h) + clear any stale cancel ──
    try:
        await rc.clear_cancel(_cancel_token(job_id))
    except Exception:  # noqa: BLE001
        pass
    await _init_checkboxes(job_id, total)
    await _safe_progress(job_id, "Memulai narasi...")

    # ── Kick off generation; return the id immediately ──
    asyncio.create_task(_run_narration_job(
        body=body, job_id=job_id, job_uuid=job_uuid,
        tenant_id=tenant_id, user_id=user_uuid, total=total,
        meter_op=meter_op, model=model,
    ))
    return {"ok": True, "job_id": job_id, "status": _STATUS_RUNNING, "total": total}


@app.get("/narration/{job_id}")
async def narration_status(job_id: str, user: CurrentUser = Depends(get_current_user)):
    """Poll a narration job. Redis (fast, per-chapter checkboxes) first; falls back
    to the durable jobs row when the hash has expired. Tenant-scoped via RLS."""
    status, done, total, chapters = await _read_checkboxes(job_id)

    # Durable row (source of truth for terminal state + the assembled output).
    row = None
    try:
        row = await db.get_job_by_external(user.tenant_id, job_id)
    except Exception as e:  # noqa: BLE001
        log.warning("narration_status get_job(%s) failed: %s", job_id, e)
    if not row and not chapters:
        raise HTTPException(404, "job not found")

    # Prefer the durable terminal status when the job has finished; otherwise the
    # live Redis status (running/polishing).
    db_status = (row or {}).get("status")
    eff_status = status or _STATUS_RUNNING
    if db_status in ("done", "error", "cancelled"):
        eff_status = {"done": _STATUS_DONE, "error": _STATUS_FAILED,
                      "cancelled": _STATUS_CANCELLED}.get(db_status, eff_status)

    if row:
        total = total or int(row.get("progress_total") or 0)
        done = done or int(row.get("progress_current") or 0)

    result = (row or {}).get("result_payload")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:  # noqa: BLE001
            result = {"markdown": result}

    out: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "status": eff_status,
        "done": done,
        "total": total,
        "chapters": chapters,
        "progress": None,
        "error": (row or {}).get("error_message"),
        "found": True,
    }
    try:
        out["progress"] = await rc.get_progress(job_id)
    except Exception:  # noqa: BLE001
        pass
    if eff_status == _STATUS_DONE and isinstance(result, dict):
        out["output"] = result.get("markdown")
        out["result"] = result
    return out


@app.post("/narration/{job_id}/cancel")
async def narration_cancel(job_id: str, user: CurrentUser = Depends(get_current_user)):
    """Request cancellation. The runtime stops after the in-flight chapter, marks
    the job cancelled, and refunds the unused credit hold. Tenant-scoped."""
    row = None
    try:
        row = await db.get_job_by_external(user.tenant_id, job_id)
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        # Still allow setting the flag if the live checkbox hash exists (the row
        # may not be readable, but a running job should still be cancellable).
        _, _, total, chapters = await _read_checkboxes(job_id)
        if not chapters:
            raise HTTPException(404, "job not found")
    try:
        await rc.set_cancel(_cancel_token(job_id))
    except Exception as e:  # noqa: BLE001
        log.warning("set_cancel(%s) failed: %s", job_id, e)
    await _set_status(job_id, _STATUS_CANCELLED)
    return {"ok": True, "status": "cancel_requested", "job_id": job_id}


# ---------------------------------------------------------------------------
# BYOK detection — reuse the laozhang_api helper if importable; else env fallback.
# Kept tiny + local so this module imports even when laozhang_api's BYOK plumbing
# isn't fully wired in a given environment.
# ---------------------------------------------------------------------------
def _byok() -> bool:
    try:
        from laozhang_api import _byok_active  # type: ignore
        return bool(_byok_active())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["narration_start", "narration_status", "narration_cancel"]
