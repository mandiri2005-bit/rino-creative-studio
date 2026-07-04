# ── narration_worker — BullMQ S1 (spec: ~/docs/narasi-bullmq-port-spec.md).
# Standalone entrypoint (separate Railway service, same image):
#     python narration_worker.py
# Consumes the `narration` queue (official `bullmq` PyPI package — queue format is
# fully compatible with the Node BullMQ used by video/recipe) and runs the SAME
# `_run_narration_job` the in-process path uses, so status/persist/settle/refund
# behavior is identical. The API enqueues instead of ensure_future when
# NARRATION_BULLMQ_ENABLED=1 (default OFF — in-process path unchanged).
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
    from narration_api import _run_narration_job  # laozhang_api already imported via main()

    data = dict(job.data or {})
    job_id = str(data.get("job_id") or "")
    tenant_id = data.get("tenant_id")
    log.info("job %s picked up (bull id %s, attempt %s)", job_id, job.id, getattr(job, "attemptsMade", "?"))

    # Idempotency: a stalled-retry of an already-finished job must not re-run/settle.
    try:
        row = await db.get_job_by_external(tenant_id, job_id)
        if row and str(row.get("status") or "") in _TERMINAL:
            log.info("job %s already terminal (%s) — ack without work", job_id, row.get("status"))
            return {"skipped": "terminal"}
    except Exception as e:  # noqa: BLE001
        log.warning("terminal-check failed (continuing): %s", e)

    await _run_narration_job(
        body=data.get("body") or {}, job_id=job_id, job_uuid=data.get("job_uuid"),
        tenant_id=tenant_id, user_id=data.get("user_id"),
        total=int(data.get("total") or 1), meter_op=data.get("meter_op"),
        model=str(data.get("model") or "claude-opus-4-6"),
    )
    return {"ok": True}


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
         "maxStalledCount": 1, "stalledInterval": 60_000},
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
