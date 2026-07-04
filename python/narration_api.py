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
from auth_middleware import get_current_user, get_current_user_optional, CurrentUser

# The orchestration engine front door (WS-6). NEVER raises into us.
from orchestrator.router import generate_narration
# Telemetry record type so the usage sink can read tokens/cost off each call.
from orchestrator.core import CallTelemetry, _extract as _core_extract, estimate_cost as _core_cost

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
                 "cost_usd", "calls", "credits", "_ckpt", "_loop")

    def __init__(self, tenant_id: str, user_id: Optional[str], job_uuid: Optional[str]):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.job_uuid = job_uuid
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.calls = 0
        # A1 crash-safe billing (mirrors classic _meter_actual): running CREDIT total,
        # durably checkpointed to jobs.input_payload._meter.actual after each call so the
        # orphan sweep can COMMIT delivered work after a crash instead of refunding it.
        # The UPDATE also bumps jobs.updated_at (trg_jobs_updated_at) → an actively
        # generating job can never look stale to the sweep. _ckpt: None=unresolved,
        # False=crashsafe off (skip), True=on.
        self.credits = 0
        self._ckpt: Optional[bool] = None
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
                # Actual serving aggregator when the narasi failover client handled the
                # call (kie/laozhang/atlascloud, stamped through CallTelemetry.provider);
                # "laozhang" only as the plain-client default. NOT NULL column.
                job_id=self.job_uuid, provider=(getattr(t, "provider", "") or "laozhang"),
                latency_ms=int(t.latency_ms or 0),
                finish_reason=t.finish_reason or ("error" if not t.ok else "stop"),
                http_status=200 if t.ok else 502, credits=0)
        except Exception as e:  # noqa: BLE001
            log.debug("usage sink log_one failed (non-fatal): %s", e)
        # A1: durable per-call billing checkpoint (classic parity). credit_cost prices the
        # call the same way _log_narasi_usage does; checkpoint_narasi_meter no-ops when the
        # row carries no _meter (crashsafe off / BYOK / no hold) so this stays cheap.
        try:
            if self._ckpt is None:
                try:
                    from laozhang_api import _dalang_crashsafe_enabled as _cse
                    self._ckpt = bool(_cse())
                except Exception:  # noqa: BLE001
                    self._ckpt = False
            if self._ckpt and self.job_uuid and t.ok:
                import credit_catalog as _cat
                self.credits += int(_cat.credit_cost(
                    "narasi", t.model,
                    {"tokens_in": int(t.tokens_in or 0), "tokens_out": int(t.tokens_out or 0)}) or 0)
                await db.checkpoint_narasi_meter(self.tenant_id, self.job_uuid, self.credits)
        except Exception as e:  # noqa: BLE001
            log.debug("usage sink meter checkpoint failed (non-fatal): %s", e)


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

    # CC v3 R-FG4: refresh the known-bad-claims registry (global reference data) into the
    # gate's in-process cache — best-effort; the gate carries a seed fallback regardless.
    try:
        import narasi_gate as _ngate
        _ngate.set_db_claims(await db.get_known_bad_claims(body.get("project_id")))
        # alt_history (§3): contextvar — inherited by every task this job spawns, so
        # concurrent worker jobs can't race each other's canon enforcement.
        _ngate.set_alt_history(bool(body.get("alt_history")))
    except Exception as e:  # noqa: BLE001
        log.warning("known_bad_claims refresh skipped (non-fatal): %s", e)
    try:
        import narasi_factscan as _nfs
        _nfs.set_known_good(await db.get_known_good_claims(body.get("project_id")))
    except Exception as e:  # noqa: BLE001
        log.warning("known_good_claims refresh skipped (non-fatal): %s", e)

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
        # A7 (Rino 2026-07-04): cancel charges the chapters ALREADY generated (partial
        # commit), refunding only the unused remainder — the sink holds the real cost of
        # every completed call at cancel time, and charge.settle() commits actual + refunds
        # the rest. Cancel before ANY chapter completed (sink empty) → full refund as
        # before. Completed chapters stay recoverable via their narasi_chapters
        # checkpoints (S2 resume writes them as each chapter lands).
        if (sink.tokens_out or 0) > 0:
            log.info("narration job %s cancelled after %d calls — settling partial "
                     "(tok_out=%d) instead of full refund", job_id, sink.calls, sink.tokens_out)
            await _settle(meter_op, tenant_id, user_id, model, job_uuid, sink)
        else:
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
    # The gates phase (counters/diet → strip → register → factscan/verify → header) can
    # take minutes on a big book — surface it so the UI doesn't look hung at 10/10 done.
    await _safe_progress(job_id, "Finalisasi: quality gates & verifikasi…")
    # CC v3 gates — terminal bracket/known-bad gate (R-FG4/5/6, ALL scenarios incl. C/D/E
    # whose result carries "output" not "book"), the harari register scorecard (R-H10,
    # report-only), and the "> **Gaya:** ..." metadata header. Never raises.
    await _apply_v3_gates(result, body, tenant_id=tenant_id, user_id=user_id, job_uuid=job_uuid,
                          sink=sink)
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
# CC v3 (Stop the Pendulum) — terminal gates for the ⚡ engine. All best-effort.
# ---------------------------------------------------------------------------
async def _apply_v3_gates(result: dict, body: dict, *, tenant_id=None, user_id=None, job_uuid=None,
                          sink: "Optional[_UsageSink]" = None) -> None:
    """Mutates `result` in place: (1) R-FG4/5/6 deterministic gate on the final book +
    every chapter record (the per-chapter gate in static.py covers scenario A/B workers;
    this terminal pass also covers C/D/E outputs and anything the polish reintroduced);
    (2) R-H10 register scorecard for harari (report-only, one cheap call); (3) the
    "> **Gaya:** ..." metadata header, so ⚡ output matches the classic engine."""
    try:
        import narasi_gate as _ngate
    except Exception:  # noqa: BLE001
        return
    style = str(body.get("style") or "").strip()
    language = str(body.get("language") or "id").strip()

    # ── (0) CC v4 §1: deterministic counters + surgical diet loop (max 2). Budgets come
    # from the style's style_spec (only harari is tuned today; others = OFF/UNMEASURED).
    # Runs BEFORE the terminal gate so a diet rewrite can never ship bracket residue.
    try:
        import narasi_counters as _nc
        entry = None
        try:
            from pakem import resolve_style as _rs
            entry = _rs(style)
        except Exception:  # noqa: BLE001
            entry = None
        has_budgets = bool(((entry or {}).get("style_spec") or {}).get("counters"))
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book and entry is not None:
            wt = 0
            for c in (body.get("chapters") or []):
                if isinstance(c, dict):
                    try:
                        wt += int(c.get("word_target") or c.get("words") or 0)
                    except (TypeError, ValueError):
                        pass
            embed = None
            try:
                import dalang_dedup as _dd
                embed = getattr(_dd, "embed", None)
            except Exception:  # noqa: BLE001
                embed = None
            rep = _nc.scan_manuscript(book, lang=language, style_entry=entry,
                                      word_target=wt or None, embed_fn=embed)
            loops = 0
            while has_budgets and rep.get("over_budget") and loops < 2:
                loops += 1
                try:
                    from laozhang_api import make_narasi_client, _resolve_narasi_lang as _rl
                    instr = _nc.surgical_prompt(rep, language=_rl(language))
                    model = str(body.get("worker_model") or "claude-opus-4-6")
                    cli = make_narasi_client(model)
                    mt = min(32000, int(len(book.split()) * 1.45 * 1.25) + 800)
                    resp = await asyncio.wait_for(asyncio.to_thread(
                        lambda: cli.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": instr + "\n\nMANUSCRIPT:\n" + book}],
                            max_tokens=mt, stream=False)), timeout=600)
                    # A2: this is a real (up to 32k-token Opus) call — it MUST be metered
                    # and logged, or an over-budget book delivers a large rewrite billed to
                    # nobody and invisible in usage_logs. Feed the job's sink: it accumulates
                    # cost_usd for _settle AND fans out a usage_logs row. Also extract the
                    # finish_reason so a truncated rewrite is rejected (A15), not shipped.
                    out, _din, _dout, _dfin = _core_extract(resp)
                    if sink is not None:
                        try:
                            sink(CallTelemetry(
                                model=model, role="editor", ok=True,
                                tokens_in=_din, tokens_out=_dout,
                                cost_usd=_core_cost(model, _din, _dout),
                                finish_reason=_dfin, task_id=f"diet{loops}",
                                provider=str(getattr(resp, "_narasi_served_by", "") or "")))
                        except Exception:  # noqa: BLE001 - never let metering break the gate
                            pass
                    _dtrunc = str(_dfin or "").strip().lower() in ("length", "max_tokens", "max_output_tokens")
                    # a surgical edit can only shrink modestly — reject a gutted rewrite;
                    # A15: reject a truncated rewrite regardless of ratio (it would replace
                    # the full book with a mid-sentence cut).
                    if out and not _dtrunc and len(out.split()) >= int(len(book.split()) * 0.7):
                        book = out
                        result[key] = book
                        rep = _nc.scan_manuscript(book, lang=language, style_entry=entry,
                                                  word_target=wt or None, embed_fn=embed)
                    else:
                        if _dtrunc:
                            log.warning("counter diet loop: rewrite truncated (finish=%s) — kept original", _dfin)
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("counter diet loop failed (non-fatal): %s", e)
                    break
            rep["diet_loops"] = loops
            result["counter_report"] = {
                "over_budget": rep.get("over_budget", []),
                "diet_loops": loops,
                "counters": {k: {kk: vv for kk, vv in v.items() if kk != "sentences"}
                             for k, v in rep.get("counters", {}).items()},
            }
            if rep.get("over_budget"):
                log.warning("counters still over budget after %d diet loop(s): %s",
                            loops, rep["over_budget"])
    except Exception as e:  # noqa: BLE001
        log.warning("counter engine failed (non-fatal): %s", e)

    _mode = "video" if str(body.get("mode") or "").strip() == "video" else "book"

    # ── (0.7) ID-path §5: scholar entity resolution — split-person repair, wrong-domain
    # downgrade, self-debate flag. Deterministic; runs before the terminal gate so its
    # corrections are themselves swept. ──
    try:
        import narasi_entities as _nent
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book:
            fixed, ent_report = _nent.entity_pass(book)
            result[key] = fixed
            for rec in result.get("chapters") or []:
                if rec.get("content"):
                    rec["content"], _er = _nent.entity_pass(rec["content"])
            result["entity_report"] = ent_report
            if ent_report.get("self_debate"):
                log.warning("entity pass: self-debate conflict(s) flagged: %s",
                            [d.get("scholar") for d in ent_report["self_debate"]])
    except Exception as e:  # noqa: BLE001
        log.warning("entity pass failed (non-fatal): %s", e)

    # ── (1) terminal deterministic gate (localized per §2/§3) ──
    gate_report: dict = {}
    try:
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book:
            gated, gate_report = _ngate.gate_text(book, lang=language, mode=_mode)
            result[key] = gated
        for rec in result.get("chapters") or []:
            if rec.get("content"):
                rec["content"], _r = _ngate.gate_text(rec["content"], lang=language, mode=_mode)
        result["gate_report"] = gate_report
    except Exception as e:  # noqa: BLE001
        log.warning("v3 terminal gate failed (non-fatal): %s", e)

    # ── (2) R-H10 register scorecard — entry-driven (any style with a register_spec in
    # the pakem registry), report-only, one cheap call. Deterministic half = banned-tells
    # substring scan; LLM half = counting the style's required moves. ──
    try:
        if str(os.environ.get("NARASI_REGISTER_GATE", "1")).strip().lower() not in ("0", "false", "no", "off"):
            spec = None
            style_key = style
            try:
                from pakem import resolve_style, resolve_style_key
                entry = resolve_style(style)
                spec = entry.get("register_spec")
                style_key = resolve_style_key(style) or style
            except Exception:  # noqa: BLE001
                spec = None
            if spec and (spec.get("required_moves") or spec.get("banned_tells")):
                book = result.get("book") or result.get("output") or ""
                low = book.lower()
                banned = [t for t in (spec.get("banned_tells") or []) if t and t.lower() in low]
                moves = list(spec.get("required_moves") or [])
                counts: dict = {}
                if moves and book:
                    try:
                        from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
                        _sys = ("You are a strict register auditor. For the declared style, count how many times "
                                "each REQUIRED MOVE genuinely occurs in the text (a real, executed instance — not a "
                                "faint echo). Moves: " + ", ".join(moves) + ". "
                                "Return ONLY JSON mapping each move name to an integer count.")
                        raw, _cr = await _narasi_cheap_call(_sys, (book or "")[:12000],
                                                            tenant_id=tenant_id, user_id=user_id,
                                                            job_uuid=job_uuid, json_mode=True)
                        d = _narasi_parse_json(raw) if isinstance(raw, str) else (raw or {})
                        if isinstance(d, dict):
                            counts = {m: int(d.get(m) or 0) for m in moves}
                    except Exception as e:  # noqa: BLE001
                        log.warning("register-gate LLM scan failed (non-fatal): %s", e)
                on_register = (not banned) and all(counts.get(m, 0) >= 1 for m in moves) if counts or not moves else False
                verdict = "on_register" if on_register else "off_register"
                result["register_gate"] = {
                    "style": style_key, "moves": counts,
                    "banned_tells": banned, "verdict": verdict,
                }
                if verdict != "on_register":
                    log.warning("register-gate: manuscript flagged off_register for %s (moves=%s banned=%s)",
                                style_key, counts, banned)
    except Exception as e:  # noqa: BLE001
        log.warning("register gate failed (non-fatal): %s", e)

    # ── (2.7) R-FG9/R-FG10 fact scan — scan-and-report on the FINAL text (post-gates,
    # pre-header). Report-only by spec ("scan first, block second"); regime = style
    # default, job-overridable via body.factual_regime (refactor §4).
    try:
        import narasi_factscan as _nfs
        regime = _effective_regime(body)
        # Biopic rule (precedence §1): a named historical person floats person-claim
        # scanning to hybrid even when the style regime is fictional.
        if body.get("_person_floor") == "hybrid" and regime == "fictional":
            regime = "hybrid"
        _book = result.get("book") or result.get("output") or ""
        if _book:
            result["fact_report"] = _nfs.fact_scan(_book, factual_regime=regime, lang=language)
            _of = result["fact_report"].get("overfiring")
            if _of:
                log.warning("fact-scan detectors over-firing (tune before enforcement): %s", _of)
            # FG-SEARCH verify pass (dormant: FACTGATE_SEARCH_ENABLED=0 / keyless →
            # PROVIDER_DOWN → NEEDS-VERIFY). Report-only per §6; writes the cache stores.
            if regime == "strict":
                try:
                    import narasi_verify as _nv
                    if _nv.verify_enabled():
                        result["fact_report"]["verify"] = await _nv.verify_report(
                            result["fact_report"], project_id=body.get("project_id"),
                            tenant_id=tenant_id)
                except Exception as _ve:  # noqa: BLE001
                    log.warning("verify pass failed (non-fatal): %s", _ve)
            # Regime-mismatch detector (§4, warn-only): a FICTIONAL job dense with real
            # anchors probably meant hybrid/strict. Cheap call; log always.
            if regime == "fictional" and str(os.environ.get("NARRATION_MISMATCH_DETECT", "1")).strip().lower() not in ("0", "false", "no", "off"):
                try:
                    from laozhang_api import _narasi_cheap_call, _narasi_parse_json
                    _sys2 = ("Count REAL-WORLD anchors in this fiction: recognizable real persons, "
                             "places, events, institutions. Return ONLY JSON "
                             "{\"real_persons\": [str], \"real_anchor_count\": int}")
                    raw2, _c2 = await _narasi_cheap_call(_sys2, _book[:12000], tenant_id=tenant_id,
                                                         user_id=user_id, json_mode=True)
                    d2 = _narasi_parse_json(raw2) if isinstance(raw2, str) else {}
                    persons2 = (d2 or {}).get("real_persons") or []
                    anchors = int((d2 or {}).get("real_anchor_count") or 0)
                    per_1k = anchors / max(1, len(_book.split()) / 1000.0)
                    if len(persons2) > 3 or per_1k > 8:
                        result["regime_mismatch_warn"] = {
                            "real_persons": persons2[:6], "anchors_per_1000w": round(per_1k, 1),
                            "message": "Fictional job references substantial real-world material "
                                       "and none of it is verified — did you mean hybrid/strict?"}
                        log.warning("regime-mismatch WARN: %s", result["regime_mismatch_warn"])
                except Exception as _me:  # noqa: BLE001
                    log.warning("mismatch detector failed (non-fatal): %s", _me)
    except Exception as e:  # noqa: BLE001
        log.warning("fact scan failed (non-fatal): %s", e)

    # ── (2.9) ID-path §7: number rendering keyed to Output — video = speakable spelled
    # forms (uniform; ends the '"seribu delapan ratus…" beside "1827"' mix), book = digits.
    try:
        if _mode == "video" and str(language or "").split("-")[0].lower() == "id":
            from narasi_counters import render_numbers_id as _rn
            key = "book" if result.get("book") else "output"
            book = result.get(key) or ""
            if book:
                rendered, n_sp = _rn(book)
                result[key] = rendered
                for rec in result.get("chapters") or []:
                    if rec.get("content"):
                        rec["content"], _n2 = _rn(rec["content"])
                result["number_rendering"] = {"path": "video", "spelled": n_sp}
    except Exception as e:  # noqa: BLE001
        log.warning("number rendering failed (non-fatal): %s", e)

    # §1: the manifest built at job start ships in the editor report.
    if body.get("_gates_manifest"):
        result["gates_manifest"] = body["_gates_manifest"]

    # ── (3) Gaya metadata header (matches the classic stitch header; gate-whitelisted) ──
    try:
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book and not book.lstrip().startswith("> **Gaya:**"):
            try:
                from laozhang_api import _resolve_narasi_lang  # lazy
                lang_label = _resolve_narasi_lang(language)
            except Exception:  # noqa: BLE001
                lang_label = language
            words = len(book.split())
            # Gaya shows the DISPLAY name ("Big History"), never the raw registry key
            # ("harari") the FE submits.
            _style_label = style or "narasi"
            try:
                from pakem import resolve_style as _rs3
                _style_label = (_rs3(style) or {}).get("display_name") or _style_label
            except Exception:  # noqa: BLE001
                pass
            # v4 §5: header gains the Output field so the editor/dual-path filters are auditable.
            _out_path = "video" if str(body.get("mode") or "").strip() == "video" else "book"
            # alt_history (§3): the pipeline writes the disclaimer marker, not the user.
            _alt = " | **Catatan:** sejarah alternatif (alternate history)" if body.get("alt_history") else ""
            result[key] = (f"> **Gaya:** {_style_label} | **Output:** {_out_path} | "
                           f"**Bahasa:** {lang_label} | **{words} kata**{_alt}\n\n---\n\n") + book
    except Exception as e:  # noqa: BLE001
        log.warning("Gaya header failed (non-fatal): %s", e)


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
        # CC v3/v4 reports (bounded dicts; absent when the gates didn't run)
        "gate_report": result.get("gate_report"),
        "register_gate": result.get("register_gate"),
        "counter_report": result.get("counter_report"),
        "fact_report": result.get("fact_report"),
        # ID-path fixes: §1 manifest + §5 entity report + §7 rendering stats
        "gates_manifest": result.get("gates_manifest"),
        "entity_report": result.get("entity_report"),
        "number_rendering": result.get("number_rendering"),
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
        # Pass the dict RAW — the pool's jsonb codec encodes it. json.dumps here would
        # DOUBLE-ENCODE (payload stored as a JSON string → result_payload->>'markdown'
        # NULL, every consumer needs a defensive json.loads). Same rule as
        # rcs-ledger-metadata-double-encode.
        await db.finish_narasi_job(
            tenant_id, job_id, _DB_STATUS.get(status, "error"),
            result=result, error=error)
    except Exception as e:  # noqa: BLE001
        log.warning("finish_narasi_job(%s,%s) failed (non-fatal): %s", job_id, status, e)


async def _settle(meter_op: Optional[str], tenant_id: str, user_id: Optional[str],
                  model: str, job_uuid: Optional[str], sink: _UsageSink) -> None:
    """Settle the credit hold at the ACTUAL blended cost. The sink accumulates the
    real per-call cost across every model used (workers AND a possibly different,
    pricier manager), so we settle from that true USD — NOT by re-pricing the whole
    token total at the single worker `model` (which undercharges when the manager
    model is more expensive)."""
    if not meter_op:
        return
    # A4: never settle DELIVERED work at usd=0 — a 0.0 cost makes charge.settle() treat the
    # job as free and FULL-REFUND the hold. sink.cost_usd is 0 only when every model name
    # missed the pricing table (an operator routing to a genuinely new model family). Floor
    # from the token totals at a conservative blended rate so the platform recovers
    # something and the hold isn't wiped; log loudly so the misconfig is visible.
    _usd = float(sink.cost_usd or 0.0)
    if _usd <= 0.0 and (sink.tokens_out or 0) > 0:
        _usd = (int(sink.tokens_in) * 1.0 + int(sink.tokens_out) * 5.0) / 1_000_000.0
        log.warning("settle(%s): sink priced 0 for %d out tokens (pricing-table miss for model=%s?) "
                    "— flooring usd=%.5f to avoid a free-book full refund",
                    meter_op, sink.tokens_out, model, _usd)
    try:
        charge = metering.Charge(
            tenant_id=tenant_id, user_id=user_id, op_id=meter_op,
            operation="narasi", model=model, held=0)
        await charge.settle(
            {"tokens_in": sink.tokens_in, "tokens_out": sink.tokens_out},
            job_id=job_uuid, tok_in=sink.tokens_in, tok_out=sink.tokens_out,
            usd=_usd)
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
# ── CONTENT-SAFETY SCOPE MARKER (regime-precedence spec §5) ──────────────────
# The fact-gate + living-person guard reduce ACCURACY and DEFAMATION exposure. They do
# NOT cover content safety: harmful-instruction-in-fiction, medical misinformation framed
# as story ("ramuan X menyembuhkan Y" in a dongeng), or platform-policy violations wrapped
# in narrative. The strict regime was never a safety net for these; the fictional regime
# just makes the absence visible. This is a SEPARATE, currently-UNBUILT layer — owner
# decision pending (Rino). Do not mistake the guards below for covering it.
# ─────────────────────────────────────────────────────────────────────────────


def _effective_regime(body: dict) -> str:
    """Precedence chain floor input (regime-precedence spec §1): job override else style
    default. The living-guard and person-floor sit ABOVE this."""
    r = str(body.get("factual_regime") or "").strip().lower()
    if r in ("strict", "hybrid", "fictional"):
        return r
    try:
        from pakem import resolve_style
        return (resolve_style(str(body.get("style") or "")) or {}).get("factual_regime", "strict")
    except Exception:  # noqa: BLE001
        return "strict"


async def _living_person_guard(body: dict, tenant_id) -> None:
    """Regime-precedence spec §2 — BLOCKING, pre-generation, zero tokens spent on a
    blocked job. Fires only when regime ∈ {fictional, hybrid} and a living/recently-
    deceased real person is the subject/central character. Deaths are monotonic, so the
    model's knowledge suffices for 'historical'; uncertain → treated as LIVING
    (fail-closed on status). Internal detector errors fail-OPEN (log, never block users
    on our bug). Kill switch NARRATION_LIVING_GUARD=0; admin override via
    LIVING_GUARD_ADMIN_TENANTS + body.override_living_guard (always logged)."""
    if str(os.environ.get("NARRATION_LIVING_GUARD", "1")).strip().lower() in ("0", "false", "no", "off"):
        return
    regime = _effective_regime(body)
    if regime == "strict":
        return
    if body.get("override_living_guard"):
        admins = {t.strip() for t in os.environ.get("LIVING_GUARD_ADMIN_TENANTS", "").split(",") if t.strip()}
        if str(tenant_id) in admins:
            log.warning("living-guard OVERRIDDEN by admin tenant %s (job topic: %.60s)",
                        tenant_id, str(body.get("topic") or ""))
            return
    try:
        text = " | ".join(filter(None, [
            str(body.get("topic") or ""), str(body.get("brief") or "")[:800],
            " ; ".join(f"{c.get('title','')}: {c.get('summary', c.get('description',''))}"
                       for c in (body.get("chapters") or [])[:20] if isinstance(c, dict))[:1200],
        ]))[:2500]
        if not text.strip():
            return
        from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
        _sys = ("You screen story briefs for REAL, identifiable people. List every real person "
                "named in the text. For each: role = subject|central|minor, and status = "
                "living|recently_deceased (died in the last ~20 years)|historical (died longer ago)"
                "|unsure. Fictional/invented characters and generic unnamed roles are NOT listed. "
                "Return ONLY JSON: {\"persons\": [{\"name\": str, \"role\": str, \"status\": str}]}")
        raw, _cr = await _narasi_cheap_call(_sys, text, tenant_id=tenant_id, user_id=None,
                                            json_mode=True)
        d = _narasi_parse_json(raw) if isinstance(raw, str) else (raw or {})
        persons = (d or {}).get("persons") or []
        hits, historical = [], []
        for p in persons:
            if not isinstance(p, dict):
                continue
            status = str(p.get("status") or "unsure").lower()
            role = str(p.get("role") or "minor").lower()
            if status in ("living", "recently_deceased", "unsure") and role in ("subject", "central"):
                hits.append(p.get("name") or "?")
            elif status == "historical":
                historical.append(p.get("name") or "?")
        if hits:
            log.warning("living-person guard BLOCKED job (regime=%s): %s", regime, hits)
            raise HTTPException(422, {
                "error": "living_person_guard",
                "persons": hits[:5],
                "message": ("Cerita fiksi/hybrid tentang tokoh nyata yang masih hidup (atau baru "
                            "wafat) diblokir. Dua jalur: (1) jadikan komposit — ganti nama & "
                            "samarkan detail identitas, atau (2) tulis sebagai nonfiksi strict "
                            "dengan fact-gate penuh (set factual_regime: strict)."),
            })
        # Biopic rule (§1): named HISTORICAL person + fictional regime → person-claims
        # float to hybrid; the world stays unverified. alt_history lowers it for the dead.
        if historical and regime == "fictional" and not body.get("alt_history"):
            body["_person_floor"] = "hybrid"
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — detector bug must never block a user
        log.warning("living-person guard failed OPEN (non-fatal): %s", e)


def _narration_admit(body: dict) -> None:
    """CC v3 admission for the ⚡ engine — mirrors the classic _narasi_admit caps (same
    env-tunable constants): ≤20 chapters, ≤8,000 words/chapter, ≤120,000 words total.
    Previously /narration/start had NO cap (the classic caps live on /narasi/*), so a
    direct API caller could exceed them. Raises HTTPException(400)."""
    try:
        from laozhang_api import (DALANG_MAX_CHAPTERS, DALANG_MAX_TOTAL_WORDS,
                                  DALANG_MAX_WORDS_PER_CHAPTER)  # lazy: import-order safe
    except Exception:  # noqa: BLE001 - never block a job on an import hiccup
        return
    chapters = None
    for key in ("chapters", "outline", "titles"):
        v = body.get(key)
        if isinstance(v, list) and v:
            chapters = v
            break
    n = len(chapters) if chapters else 0
    if not n:
        try:
            n = int(body.get("n_chapters") or body.get("num_chapters") or 1)
        except (TypeError, ValueError):
            n = 1
    if n > DALANG_MAX_CHAPTERS:
        raise HTTPException(400, f"too many chapters: {n} > {DALANG_MAX_CHAPTERS}")
    total = 0
    for c in chapters or []:
        if not isinstance(c, dict):
            continue
        try:
            w = int(c.get("word_target") or c.get("words") or 800)
        except (TypeError, ValueError):
            w = 800
        if w > DALANG_MAX_WORDS_PER_CHAPTER:
            raise HTTPException(400, f"chapter words {w} > {DALANG_MAX_WORDS_PER_CHAPTER}")
        total += max(0, w)
    if total > DALANG_MAX_TOTAL_WORDS:
        raise HTTPException(400, f"total words {total} > {DALANG_MAX_TOTAL_WORDS}")


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
    _narration_admit(body)   # CC v3: ⚡ caps (chapters/words) — 400 BEFORE any hold
    await _living_person_guard(body, user.tenant_id)   # §2: blocking, pre-hold, pre-tokens
    # ID-path §1: gates-active manifest — BEFORE the hold, zero tokens on a job whose
    # enforcement state can't be fully resolved. UNMEASURED/n-a are honest states and
    # pass; a rule with NO state fails the start (silent absence is the killed class).
    try:
        import narasi_manifest as _nm
        _entry = None
        try:
            from pakem import resolve_style as _rs_m
            _entry = _rs_m(str(body.get("style") or ""))
        except Exception:  # noqa: BLE001
            _entry = None
        body["_gates_manifest"] = _nm.build_manifest(
            style_entry=_entry, lang=str(body.get("language") or "id"),
            regime=_effective_regime(body),
            mode="video" if str(body.get("mode") or "").strip() == "video" else "book")
    except Exception as _me:
        # ManifestError = deliberate fail-closed; anything else must not block a job.
        import narasi_manifest as _nm2
        if isinstance(_me, _nm2.ManifestError):
            raise HTTPException(422, {"error": "gates_manifest", "message": str(_me)})
        log.warning("gates manifest build failed (non-fatal): %s", _me)
    # BullMQ S3 fairness: cap concurrent narasi jobs per tenant (0 = off, default).
    _cap = int(os.environ.get("NARRATION_MAX_ACTIVE_PER_TENANT", "0") or 0)
    if _cap > 0:
        try:
            if await db.count_active_narasi_jobs(tenant_id) >= _cap:
                raise HTTPException(429, f"too many narrations in flight (max {_cap}) — "
                                         "wait for one to finish")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - never block on a count hiccup
            pass
    job_id = (str(body.get("pre_job_id") or uuid.uuid4().hex[:8]))[:16]
    topic = str(body.get("topic") or body.get("goal") or body.get("brief") or "").strip()
    model = str(body.get("worker_model") or os.environ.get("WORKER_MODEL")
                or "gemini-2.5-flash").strip()
    total = _count_chapters(body)

    # ── Credit HOLD up front (HTTP 402 if short). BYOK pays upstream → no hold. ──
    # A6: price the hold at the model the workers will ACTUALLY run on. Manager-routed
    # styles (harari/academic-popular/literary-essay) route their worker to
    # MANAGER_MODEL=claude-sonnet-4-6 (~14× the gemini `model` estimate); pricing the hold
    # at the cheap `model` under-reserves, then the F4 clamp caps the debit at the too-small
    # hold and the platform eats the delta. Resolve the routed model here so the hold covers
    # the real blended cost. (settle still bills the sink's true per-call USD.)
    _style_for_hold = str(body.get("style") or "").strip()
    hold_model = model
    try:
        from orchestrator.core import route_model as _route_model
        hold_model = _route_model(role="worker", style=_style_for_hold,
                                  override=body.get("worker_model")) or model
    except Exception:  # noqa: BLE001
        hold_model = model
    try:
        is_byok = bool(_byok())
    except Exception:  # noqa: BLE001
        is_byok = False
    meter_op = None
    try:
        if not is_byok:
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
                model=hold_model, estimate_units=est_units, op_id=meter_op)
    except HTTPException:
        raise  # 402 surfaces to the client untouched
    except Exception as e:  # noqa: BLE001 - never let a metering hiccup block a job
        log.warning("narration hold skipped (non-fatal): %s", e)
        meter_op = None

    # ── Durable jobs row (poll can see it immediately) ──
    # A1 (crash-safe billing, mirrors classic laozhang_api narasi_start): stamp the hold's
    # op_id into input_payload._meter so the orphan sweep (narasi_jobs_sweep_stale / 0054)
    # can settle/refund the hold after a crash — without it a SIGKILL/OOM/redeploy mid-run
    # strands the hold ~6h AND leaks the per-tenant active cap via the stuck-'processing'
    # row. Gated on DALANG_CRASHSAFE_ENABLED exactly like the classic callsite.
    job_uuid = None
    try:
        _ckpt_op = None
        try:
            from laozhang_api import _dalang_crashsafe_enabled as _cse  # lazy — no top-level cycle
            _ckpt_op = meter_op if _cse() else None
        except Exception:  # noqa: BLE001
            _ckpt_op = None
        await db.create_narasi_job(tenant_id, user_uuid, job_id, topic, total, op_id=_ckpt_op)
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
    # ── BullMQ S1 (NARRATION_BULLMQ_ENABLED, default OFF): enqueue to the durable
    # `narration` queue instead of running in-process — the narration-worker service
    # picks it up (survives API restarts; S2 resume continues checkpointed chapters).
    # Any enqueue failure falls back to the in-process path (never lose a job).
    # A8: a BYOK job must NEVER be enqueued. The worker runs in a separate process that
    # cannot reconstruct the per-request BYOK key (a contextvar), so it would generate on
    # the PLATFORM key while meter_op=None means _settle never runs — platform pays the full
    # upstream cost and recovers nothing. BYOK always runs in-process.
    _bullmq_on = str(os.environ.get("NARRATION_BULLMQ_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    if _bullmq_on and not is_byok:
        _enq_ok = False
        try:
            from bullmq import Queue as _BullQueue
            _q = _BullQueue(os.environ.get("NARRATION_QUEUE", "narration"),
                            {"connection": os.environ.get("REDIS_URL", "redis://localhost:6379")})
            try:
                await _q.add("narration", {
                    "job_id": job_id, "job_uuid": job_uuid, "tenant_id": tenant_id,
                    "user_id": user_uuid, "total": total, "meter_op": meter_op,
                    "model": model, "body": body,
                }, {"jobId": job_id, "removeOnComplete": True, "attempts": 2})
                _enq_ok = True   # the job is durably enqueued the instant add() returns
            finally:
                try:
                    await _q.close()   # a close() error must NOT trigger the in-process fallback
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            log.warning("bullmq enqueue failed — falling back in-process: %s", e)
        # A9: only fall through to the in-process path if the ADD itself failed. Enqueued +
        # in-process = the same job_id runs twice (doubled COGS, duplicate usage_logs,
        # racing Redis/gate state) even though customer credits stay op_id-idempotent.
        if _enq_ok:
            return {"ok": True, "job_id": job_id, "status": _STATUS_RUNNING,
                    "total": total, "queued": True}

    asyncio.create_task(_run_narration_job(
        body=body, job_id=job_id, job_uuid=job_uuid,
        tenant_id=tenant_id, user_id=user_uuid, total=total,
        meter_op=meter_op, model=model,
    ))
    return {"ok": True, "job_id": job_id, "status": _STATUS_RUNNING, "total": total}


@app.get("/narration/queue/health")
async def narration_queue_health(user: Optional[CurrentUser] = Depends(get_current_user_optional)):
    """BullMQ S4: queue depth/health for ops. `enabled` mirrors the S1 flag; counts are
    best-effort (absent when bullmq isn't installed or the flag is off)."""
    enabled = str(os.environ.get("NARRATION_BULLMQ_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    out: dict = {"enabled": enabled, "queue": os.environ.get("NARRATION_QUEUE", "narration")}
    if enabled:
        try:
            from bullmq import Queue as _BullQueue
            _q = _BullQueue(out["queue"], {"connection": os.environ.get("REDIS_URL", "redis://localhost:6379")})
            out["counts"] = await _q.getJobCounts("waiting", "active", "failed", "delayed")
            await _q.close()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
    return out


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
