"""Canon Lite QC runner — the ONLY sanctioned caller of the metered extraction path.

Implements the DG-4 caller wiring for the ratified provider/model amendment
(e5412fd9f0e32127f01568d3bd5665aac691cb6ea1635498a2f4c0fb6695ca08) under Topology
Amendment 001 §1.1.

WHY THIS MODULE EXISTS SEPARATELY. The adapter and its constants live in
canon_lite_qc_provider, which must not be imported on any host that must never meter
(§6.0a). This module can therefore be imported freely — it holds the GATE SEQUENCE and
performs the provider import only after both gates pass, function-locally. Importing this
module imports no adapter, runs no constant gate, and reads no configuration.

FROZEN EVALUATION ORDER — mode, then host sentinel, then config. Never reordered:

    1. resolve_mode() != off          — a typo in the flag leaves production on legacy
    2. metered_host_ok()              — server-set at worker boot, never payload-derived
    3. load config + import adapter   — ONLY here

Order matters for a specific reason: the module ships to the `python` service too, where
CANON_LITE_EXTRACTOR_CONCURRENCY is intentionally absent. Loading config before the host
check would raise a configuration error on a host that was never supposed to meter, taking
down a service that had no intention of calling a provider.

D-METER-23: nothing here authorises a provider call. With the flag off — the current
production state — this module returns None before importing anything.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional
from uuid import UUID

log = logging.getLogger("canon-lite-qc-runner")

#: The only phase this runner may bill against (PHASE_CATALOG is a one-element tuple).
QC_PHASE = "canon_lite_l2_extract"


def metered_wave_permitted(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Gates 1 and 2 only. Performs NO configuration read and NO adapter import.

    Split out so the decision is testable on every host without the side effects that
    follow it: a test can assert `python`-service paths are refused without ever risking
    the import that must not happen there.
    """
    import canon_lite as _cl
    from canon_lite_qc_meter import metered_host_ok

    if _cl.resolve_mode(environ) == _cl.MODE_OFF:
        return False
    return metered_host_ok()


async def maybe_run_metered_wave(
    snapshot: Any,
    canon: Any,
    *,
    run_id: str,
    job_uuid: Any,
    job_external_id: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
    wave_token: Any = None,
    sink: Any = None,
    adapter_factory: Any = None,
    redis_getter: Any = None,
) -> Optional[Mapping[int, Any]]:
    """Run EXACTLY ONE metered extraction wave, or return None having done nothing.

    Returns `claims_by_index` for the report, or None when the gates refuse. Returning
    None is the overwhelmingly common production outcome and is not an error.

    The caller treats any exception as "no claims": a metering fault must never change
    legacy narration delivery. That containment lives at the call site, not here — this
    function raises honestly so the failure is visible to tests and to the meter.
    """
    if not metered_wave_permitted(environ):
        return None

    # ---- gate 3: config, then the adapter import. Not one line earlier. ----
    from canon_lite_qc_meter import (AttemptContext, MeteredProvider, QcUsageSink,
                                     load_extractor_concurrency, load_max_inflight)
    import canon_lite_extractor as _ext
    import canon_lite_qc_provider as _qc          # LAZY — §6.0a

    env = os.environ if environ is None else environ
    extractor_concurrency = load_extractor_concurrency(environ)
    max_inflight = load_max_inflight(environ)

    api_key = env.get(_qc.QC_API_KEY_ENV)
    if not api_key:
        # The credential is Railway-supplied on narration-worker only. Its absence is a
        # configuration fault, not a reason to silently skip metering — skipping would
        # produce a report that looks measured but never called anything.
        raise RuntimeError("qc_provider_api_key_missing")

    if isinstance(job_uuid, str):
        job_uuid = UUID(job_uuid)

    fields = _qc.attempt_context_fields()
    context = AttemptContext(
        run_id=run_id,
        job_uuid=job_uuid,
        job_external_id=job_external_id,
        phase=QC_PHASE,
        **fields,
    )

    if adapter_factory is None:
        def adapter_factory():                                  # noqa: ANN202
            return _qc.QcProviderAdapter(api_key=api_key, environ=environ)
    adapter = adapter_factory()

    metered_kwargs = dict(
        sink=QcUsageSink() if sink is None else sink,
        context=context,
        usage_reader=_qc.usage_reader,
        max_inflight=max_inflight,
    )
    if redis_getter is not None:
        metered_kwargs["redis_getter"] = redis_getter
    metered = MeteredProvider(adapter, **metered_kwargs)

    # ONE wave per job. The max_inflight bound is
    # replicas × jobs-per-worker × extractor_concurrency, which holds only while a job
    # keeps at most one wave open; a second concurrent wave would double the real
    # in-flight count while the derived ceiling stayed put.
    #
    # ⚠ The token must be JOB-SCOPED, which is why it is a parameter. Minting a fresh
    # token here would guard nothing at all: every call would get its own, so two waves
    # for one job would both "claim" successfully and the invariant would be decorative.
    # A caller that runs one wave per job may omit it; a caller that could run more must
    # thread the same token through, and the second wave then raises.
    if wave_token is None:
        wave_token = _ext.ExtractionWaveToken()
    run = await _ext.extract_all(
        snapshot,
        canon,
        provider=metered,
        model_version=_qc.QC_MODEL_UPSTREAM,
        prompt_sha256=_qc.PROMPT_SHA256,
        max_concurrency=extractor_concurrency,
        wave_token=wave_token,
    )

    # ⚠ `assert_reconciled` is deliberately NOT called with run.logical_attempts. Those
    # are two different numbers: logical_attempts counts extractor attempts, while
    # emitted_attempts counts metered rows, and an attempt that is refused BEFORE
    # sink.begin() — metering blocked, kill armed, in-flight ceiling hit — is a logical
    # attempt with no row. Equating them would fire a false ReconciliationMismatch on
    # exactly the paths the meter is designed to refuse.
    #
    # What IS invariant here: a row can never exist without an attempt that produced it.
    if metered.emitted_attempts > run.logical_attempts:
        raise RuntimeError("qc_emitted_attempts_exceed_logical")
    # True reconciliation compares emitted rows against DURABLE rows, which this process
    # cannot read; that is the reaper's job (DG-5), not the caller's.
    log.info("canon lite qc wave: units=%d logical_attempts=%d emitted_rows=%d",
             run.logical_extractions, run.logical_attempts, metered.emitted_attempts)
    return run.claims_by_index


__all__ = ["QC_PHASE", "metered_wave_permitted", "maybe_run_metered_wave"]
