# ── narration_worker — BullMQ S1 (spec: ~/docs/narasi-bullmq-port-spec.md).
# Standalone entrypoint (separate Railway service, same image):
#     python narration_worker.py
# Consumes the `narration` queue (official `bullmq` PyPI package — queue format is
# fully compatible with the Node BullMQ used by video/recipe) and runs the same
# narration-job entrypoint the in-process path uses (see _process below), so
# status/persist/settle/refund behavior is identical. The API enqueues instead of
# ensure_future when NARRATION_BULLMQ_ENABLED=1 (default OFF — in-process path unchanged).
#
# Durability contract:
#   * idempotent: a retried/stalled job whose jobs-row is already terminal is ACKed
#     without work (no double-settle — meter op_ids are idempotent anyway).
#   * S2 resume: with NARRATION_RESUME_ENABLED=1 the ⚡ map skips chapters already
#     checkpointed in narasi_chapters (see orchestrator/static.py), so a worker that
#     died mid-run continues instead of rewriting from zero.
#   * graceful drain on SIGTERM (Railway redeploy): stop taking new jobs, finish the
#     active ones (mirrors the video-worker pattern).
from __future__ import annotations

import asyncio
import logging
import os
import signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("narration-worker")

QUEUE_NAME = os.environ.get("NARRATION_QUEUE", "narration")
CONCURRENCY = int(os.environ.get("NARRATION_WORKER_CONCURRENCY", "4"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

_TERMINAL = {"done", "failed", "cancelled", "error"}


async def _process(job, token=None):  # noqa: ANN001 - bullmq job
    """One narration job. Payload = what narration_start would have run in-process."""
    import database as db

    data = dict(job.data or {})
    job_id = str(data.get("job_id") or "")
    job_uuid = data.get("job_uuid")
    tenant_id = data.get("tenant_id")
    log.info("job %s picked up (bull id %s, attempt %s)", job_id, job.id, getattr(job, "attemptsMade", "?"))

    # RLS tenant context: database._current_tenant() reads the auth middleware's
    # per-REQUEST contextvar — which never exists in this worker process. Without this,
    # every db call that doesn't pass tenant= explicitly runs set_config('', ...) and the
    # RLS policy cast ''::uuid blows up ("invalid input syntax for type uuid") → the
    # terminal-status write fails and the job strands in 'processing' forever (first hit:
    # job 758k9iqa). Seed the ctx from the job payload so the whole run is tenant-scoped.
    try:
        from auth_middleware import _tenant_ctx, TenantContext
        _tenant_ctx.set(TenantContext(tenant_id=str(tenant_id or ""),
                                      user_id=str(data.get("user_id") or "")))
    except Exception as e:  # noqa: BLE001
        log.warning("tenant ctx seed failed (db calls fall back to explicit tenant=): %s", e)

    # Idempotency: a stalled-retry of an already-finished job must not re-run/settle. Uses the
    # internal jobs.id UUID when the queue payload carries one (Contract C: UUID is durable
    # authority) -- get_job_by_external only remains a fallback for an older enqueued item
    # that predates job_uuid being threaded onto the payload.
    try:
        if job_uuid:
            row = await db.get_job(tenant_id, job_uuid)
        else:
            row = await db.get_job_by_external(tenant_id, job_id)
        if row and str(row.get("status") or "") in _TERMINAL:
            log.info("job %s already terminal (%s) — ack without work", job_id, row.get("status"))
            return {"skipped": "terminal"}
    except Exception as e:  # noqa: BLE001
        log.warning("terminal-check failed (continuing): %s", e)
        row = None

    # B-07/B-08: compare the BullMQ QUEUE payload against the durable job's own lifecycle
    # snapshot before any work -- a stalled retry, a reused pre_job_id, or a cross-job body
    # mismatch is terminal and bounded, never a guessed-language or ordinal-resume
    # continuation. A missing durable row for a queue item that DECLARES a narasi_lifecycle,
    # or a durable row that doesn't itself carry one, is a fatal persistence failure -- never
    # silently swallowed and continued. A legacy job with no lifecycle anywhere skips this
    # check entirely and runs exactly as before.
    # B-07/B-08 (Rework 2, Contract B): UUID-scoped finalization whenever job_uuid is known --
    # two rows can share the same external_job_id, and finishing by external id risks
    # updating the wrong (e.g. newest) row.
    async def _finish_terminal(status, *, error):
        if job_uuid:
            await db.finish_narasi_job_by_id(tenant_id, job_uuid, status, error=error)
        else:
            await db.finish_narasi_job(tenant_id, job_id, status, error=error)

    _queue_declared_lifecycle = data.get("narasi_lifecycle")
    _durable_lifecycle = ((row or {}).get("input_payload") or {}).get("narasi_lifecycle")
    if _queue_declared_lifecycle or _durable_lifecycle:
        from continuity.lifecycle import (
            LifecycleValidationError, validate_job_lifecycle_request,
            validate_durable_job_snapshot, _exact_equal,
        )
        if not row or not _durable_lifecycle:
            log.error("job %s: LIFECYCLE_PERSISTENCE_FAILED -- queue declared a v1 lifecycle "
                      "but the durable job row carries none", job_id)
            await _finish_terminal("error", error="LIFECYCLE_PERSISTENCE_FAILED")
            return {"skipped": "lifecycle_persistence_failed"}
        if _queue_declared_lifecycle:
            # B-07/B-08 Rework 4 (Contract H): a copied lifecycle_hash alone is insufficient --
            # a job snapshot's lifecycle_hash is inherited verbatim from its OUTLINE (never
            # recomputed over the job object's own target_language/chapters), so a queue
            # snapshot can drift on a field OTHER than lifecycle_hash while the hash still
            # "matches". Both snapshots are fully validated, then recursively exact-compared
            # in full -- not just the hash field -- before any provider work.
            try:
                _validated_queue_snapshot = validate_durable_job_snapshot(_queue_declared_lifecycle)
                _validated_durable_snapshot = validate_durable_job_snapshot(_durable_lifecycle)
            except LifecycleValidationError as _queue_snap_lve:
                log.error("job %s LIFECYCLE_QUEUE_MISMATCH: malformed queue/durable snapshot: %s",
                         job_id, _queue_snap_lve.code)
                await _finish_terminal("error", error=f"LIFECYCLE_QUEUE_MISMATCH: {_queue_snap_lve.code}")
                return {"skipped": "lifecycle_mismatch"}
            if not _exact_equal(_validated_queue_snapshot, _validated_durable_snapshot):
                log.error("job %s LIFECYCLE_QUEUE_MISMATCH: queue snapshot does not exactly "
                         "match the durable row", job_id)
                await _finish_terminal("error", error="LIFECYCLE_QUEUE_MISMATCH: QUEUE_DURABLE_SNAPSHOT_MISMATCH")
                return {"skipped": "lifecycle_mismatch"}
        try:
            validate_job_lifecycle_request(_durable_lifecycle, data.get("body") or {})
        except LifecycleValidationError as _lve:
            log.error("job %s LIFECYCLE_QUEUE_MISMATCH: %s", job_id, _lve.code)
            await _finish_terminal("error", error=f"LIFECYCLE_QUEUE_MISMATCH: {_lve.code}")
            return {"skipped": "lifecycle_mismatch"}

    # ROUND-9 EXEC LOCK (roll-12 postmortem): the terminal-check above cannot stop a
    # stalled-REDELIVERY of a job whose first run is STILL ALIVE — job 50ritcxi ran
    # TWICE, overlapping (pickups 16:14:28 and 16:27:46, both attempt 0; the "26-minute
    # job" was really 15 minutes run twice, double-billed). A short-TTL redis lock with
    # a heartbeat: live run ⟹ lock held ⟹ duplicate suppressed; crashed run ⟹ heartbeat
    # stops ⟹ TTL expires ⟹ the stalled retry proceeds (crash recovery unchanged).
    # attempt>0 (real bull retry after a FAILURE) steals the lock. Fail-open on redis
    # errors — never blocks a legit run.
    # B-07/B-08 Rework 4 (Contract A/H): the exec lock is UUID-scoped -- job_id-only
    # scoping would let two rows sharing one external id share (and falsely dedupe
    # against) the same lock. Falls back to job_id only for a legacy pre-v1 queue
    # item that never had a job_uuid threaded onto its payload.
    job_uuid = job_uuid or job_id
    _hb_task = None
    _lock_key = f"narasi:exec:{job_uuid}"
    try:
        import redis_client as _rc
        _r = _rc.client()
        if _r is not None:
            _attempt = int(getattr(job, "attemptsMade", 0) or 0)
            _got = await _r.set(_lock_key, "1", nx=True, ex=120)
            if not _got and _attempt == 0:
                log.error("job %s DUPLICATE stalled-delivery suppressed — first run still holds the "
                          "exec lock (no work, no billing)", job_id)
                return {"skipped": "duplicate-delivery"}
            if not _got:
                await _r.set(_lock_key, "1", ex=120)   # attempt>0: real retry steals the lock
            async def _hb():
                while True:
                    await asyncio.sleep(45)
                    try:
                        await _r.set(_lock_key, "1", ex=120)
                    except Exception:  # noqa: BLE001
                        return
            _hb_task = asyncio.create_task(_hb())
    except Exception as e:  # noqa: BLE001
        log.warning("exec-lock unavailable (continuing unguarded): %s", e)

    try:
        from narration_api import _run_narration_job  # laozhang_api already imported via main()
        await _run_narration_job(
            body=data.get("body") or {}, job_id=job_id, job_uuid=data.get("job_uuid"),
            tenant_id=tenant_id, user_id=data.get("user_id"),
            total=int(data.get("total") or 1), meter_op=data.get("meter_op"),
            model=str(data.get("model") or "claude-opus-4-6"),
        )
        return {"ok": True}
    finally:
        if _hb_task is not None:
            _hb_task.cancel()
        try:
            import redis_client as _rc2
            _r2 = _rc2.client()
            if _r2 is not None:
                await _r2.delete(_lock_key)
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    # laozhang_api must import FIRST (prod import order) — it wires narration_api, db,
    # redis, pakem, and the failover client. NOTE: the NARASI_EXECUTOR_THREADS widening
    # lives in laozhang_api's FastAPI *lifespan*, which this worker never runs — so we MUST
    # widen the default ThreadPoolExecutor here ourselves (A20). Without it the worker runs
    # every blocking to_thread LLM call on the stock min(32,cpu+4) pool, throttling
    # concurrency and (with the failover chain) stranding blocked rung threads.
    os.environ.setdefault("NARASI_EXECUTOR_THREADS", os.environ.get("NARASI_EXECUTOR_THREADS", "64"))
    import laozhang_api  # noqa: F401
    import database as db
    import redis_client as rc

    _exec_threads = int(os.environ.get("NARASI_EXECUTOR_THREADS", "0") or 0)
    if _exec_threads > 0:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        asyncio.get_running_loop().set_default_executor(
            _TPE(max_workers=_exec_threads, thread_name_prefix="llm"))
        log.info("default ThreadPoolExecutor widened to %d threads", _exec_threads)

    await db.init_db()
    await rc.init_redis()

    # A1 crash-safe billing: run the narasi orphan sweep HERE too — the lifespan-registered
    # loop only lives in the API process, but with BullMQ on the jobs run in THIS process,
    # so this worker's own crashes must also be reaped (settle the delivered checkpoint /
    # refund, mark 'error', free the per-tenant cap). Same idempotent loop as the API's
    # (commit/refund are op_id-idempotent; the sweep UPDATE...RETURNING hands each row to
    # exactly one sweeper), gated on the same flag.
    _sweep_task = None
    try:
        from laozhang_api import _dalang_crashsafe_enabled, _narasi_jobs_sweep_loop
        if _dalang_crashsafe_enabled():
            _sweep_task = asyncio.create_task(_narasi_jobs_sweep_loop())
            log.info("narasi orphan-sweep loop started (crash-safe billing ON)")
    except Exception as e:  # noqa: BLE001
        log.warning("narasi sweep loop failed to start (non-fatal): %s", e)

    from bullmq import Worker  # official package; add `bullmq` to requirements

    stop_event = asyncio.Event()

    worker = Worker(
        QUEUE_NAME, _process,
        {"connection": REDIS_URL, "concurrency": CONCURRENCY,
         # stalled jobs (worker died mid-run) are re-enqueued once; with S2 resume ON
         # the retry SKIPS checkpointed chapters instead of rewriting them.
         # ROUND-9: lockDuration was the library default (30s) — one starved renewal
         # marks a LIVE 15-minute job stalled and re-delivers it (roll-12 double run).
         # 180s of lock + the exec-lock guard in _process = belt and braces.
         "maxStalledCount": 1, "stalledInterval": 60_000,
         "lockDuration": int(os.environ.get("NARASI_BULL_LOCK_MS", "180000"))},
    )

    def _drain(signame: str):
        log.info("%s received — draining (no new jobs; finishing active)", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _drain, sig.name)

    log.info("narration-worker up: queue=%s concurrency=%d", QUEUE_NAME, CONCURRENCY)
    await stop_event.wait()
    await worker.close()   # waits for active jobs, stops taking new ones
    if _sweep_task:
        _sweep_task.cancel()
        try:
            await _sweep_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    log.info("narration-worker drained — bye")


if __name__ == "__main__":
    asyncio.run(main())
