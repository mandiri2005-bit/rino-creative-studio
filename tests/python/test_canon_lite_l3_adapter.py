"""L3-ASSIST — the concrete provider/extractor session.

Four properties, and each one is a way the metering discipline can be lost while
everything still appears to work:

  * ONE session per job. A second metered provider would still meter, still
    succeed, and still double the in-flight population the ceiling was derived
    from — invisible to any test that does not count sessions.
  * A call budget that is the session's own, independent of the engine's
    per-chapter bound, because they protect against different runaways.
  * Index and id are STAMPED and say so; content hash and canon hash are not, and
    are what a wrong answer would fail on.
  * Non-assist imports nothing and spends nothing.
"""
import asyncio
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                       # noqa: E402
import canon_lite_l2 as l2                    # noqa: E402
import canon_lite_l3_adapter as ad            # noqa: E402

from test_canon_lite_l3_repair_stage2 import (  # noqa: E402
    BAD_NAME, BOOK, GOOD_NAME, _claims_for, _entity_canon)


class _Metered:
    """Stands in for the job's MeteredProvider. Records every request it sees."""

    def __init__(self, claims_needle=GOOD_NAME):
        self.requests = []
        self._needle = claims_needle

    async def __call__(self, request):
        self.requests.append(request)
        raw = request.chapter_bytes
        needle = (self._needle if self._needle.encode() in raw else BAD_NAME)
        return {
            # The wire carries the evidence verbatim; the server locates it and derives
            # the span and digest.
            "claims": [{
                "claim_type": l2.CLAIM_ENTITY_MENTION, "canon_ref": "e1",
                "quote": needle,
            }],
            "coverage": {
                p: (l2.COVERAGE_CHECKED if p == l2.PREDICATE_ENTITY_NAME
                    else l2.COVERAGE_NO_CLAIMS_FOUND)
                for p in l2.SEMANTIC_PREDICATES},
        }


def _session(metered=None, **kw):
    return ad.L3AssistSession(
        metered_provider=metered or _Metered(),
        canon_text="CANON v1\n", worker_model="m1", **kw)


def _block(book=BOOK, index=1):
    snap = l2.materialize_final_snapshot({"book": book})
    return snap.block_bytes(index)


# ── 1. one session, and it must be the job's own ────────────────────────────
def test_a_session_refuses_to_mint_its_own_provider():
    """🔴 THE SECOND WAVE, CAUGHT AT CONSTRUCTION. A session that quietly built its
    own adapter would meter correctly and still break the `max_inflight`
    arithmetic, because that ceiling assumes one open wave per job."""
    for bad in (None, "not callable", 42):
        with pytest.raises(ValueError, match="metered_provider"):
            ad.L3AssistSession(metered_provider=bad, canon_text="x",
                               worker_model="m1")


def test_the_extractor_rides_the_provider_it_was_given():
    metered = _Metered()
    session = _session(metered)
    canon = _entity_canon()
    asyncio.run(session.extract_chapter(
        chapter_index=1, chapter_id="ch2", block_bytes=_block(), canon=canon))
    assert len(metered.requests) == 1, \
        "the re-extraction did not go through the job's metered provider"


def test_the_runner_hands_its_metered_session_out_exactly_once():
    """The reuse has to be literal: the seam takes the provider the wave built."""
    import canon_lite_qc_runner as qcr
    import inspect
    sig = inspect.signature(qcr.maybe_run_metered_wave)
    assert "on_session" in sig.parameters
    src = inspect.getsource(qcr.maybe_run_metered_wave)
    assert src.count("on_session(metered)") == 1
    # ...and it is handed out AFTER construction and BEFORE the wave runs, so a
    # caller cannot receive a provider that was never the one used.
    assert src.index("metered = MeteredProvider") < src.index("on_session(metered)")
    assert src.index("on_session(metered)") < src.index("await _ext.extract_all")


def test_the_adapter_never_reaches_for_a_second_wave():
    """By construction, not by convention."""
    import inspect
    src = inspect.getsource(ad)
    assert "maybe_run_metered_wave" not in src.replace(
        "`maybe_run_metered_wave`", "")
    assert "extract_all" not in src.replace("`extract_all`", "")


# ── 2. the budget is the session's own ──────────────────────────────────────
def test_reextraction_failure_is_classified_with_the_extractors_own_vocabulary(caplog):
    """🔴 THIS SITE WAS THE UNNAMED HALF OF `provider_failed` — see canon_lite_l3_repair's
    `REASON_REEXTRACTION_FAILED` docstring for the live canary this traces back to.
    `extract_chapter` must classify a re-extraction fault with the SAME bounded
    vocabulary `canon_lite_extractor` already uses for a first-pass extraction failure
    (never inventing a second, narrower one), log it as ONE bounded line, then re-raise
    so the engine still records the failure as an outcome."""
    class _KnownFault(RuntimeError):
        """Shaped like the real QcProviderError: a `.code` from the closed vocabulary,
        never provider text. Built by hand rather than importing the real class, which
        would violate this module's own lazy-import boundary from a non-metering host."""
        def __init__(self, code):
            self.code = code
            super().__init__(code)

    class _BoomMetered(_Metered):
        async def __call__(self, request):
            self.requests.append(request)
            raise _KnownFault("qc_provider_timeout")

    session = _session(metered=_BoomMetered())
    canon = _entity_canon()
    with caplog.at_level("WARNING"):
        with pytest.raises(_KnownFault):
            asyncio.run(session.extract_chapter(
                chapter_index=1, chapter_id="ch2", block_bytes=_block(), canon=canon))

    lines = [r.getMessage() for r in caplog.records
             if "repair re-extraction failed" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one bounded line, got {lines}"
    assert "error_code=l3_repair_reextraction_error" in lines[0]
    # the EXACT code the extractor's own vocabulary would give a first-pass failure —
    # not just "some code, not obviously wrong"
    assert "reason=qc_provider_timeout" in lines[0]
    assert "boom" not in lines[0], "the raw exception message must never reach the log"


def test_reextraction_failure_falls_back_to_other_when_unclassifiable(caplog):
    """The other half of the classifier: an exception carrying no bounded code at all
    (not a QcProviderError, not a CanonSchemaError) must still produce a CLOSED-vocabulary
    reason — 'other' — never leak its own message into the reason itself."""
    class _BoomMetered(_Metered):
        async def __call__(self, request):
            self.requests.append(request)
            raise TimeoutError("boom")

    session = _session(metered=_BoomMetered())
    canon = _entity_canon()
    with caplog.at_level("WARNING"):
        with pytest.raises(TimeoutError):
            asyncio.run(session.extract_chapter(
                chapter_index=1, chapter_id="ch2", block_bytes=_block(), canon=canon))

    lines = [r.getMessage() for r in caplog.records
             if "repair re-extraction failed" in r.getMessage()]
    assert len(lines) == 1
    assert "reason=other" in lines[0]
    assert "boom" not in lines[0]


def test_the_session_budget_is_independent_of_the_engine_bound():
    session = _session(max_calls=2)
    canon = _entity_canon()
    assert session.calls_remaining == 2
    for _ in range(2):
        asyncio.run(session.extract_chapter(
            chapter_index=1, chapter_id="ch2", block_bytes=_block(), canon=canon))
    assert session.calls_remaining == 0
    with pytest.raises(ad.SessionBudgetExhausted):
        asyncio.run(session.extract_chapter(
            chapter_index=1, chapter_id="ch2", block_bytes=_block(), canon=canon))


def test_an_exhausted_budget_declines_a_repair_rather_than_faulting():
    """🔴 A DECLINE, NOT A RAISE. The engine treats a raise as a provider FAULT and
    ends the chapter; running out of budget is neither a fault nor a reason to
    hide that the chapter went unrepaired."""
    session = _session(max_calls=1)
    session._calls = 1                       # spent
    got = asyncio.run(session.repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=_block(),
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))
    assert got is None


@pytest.mark.parametrize("bad", [0, -1, ad.MAX_SESSION_CALLS + 1, True, "8"])
def test_the_budget_ceiling_is_bounded_at_construction(bad):
    with pytest.raises(ValueError, match="max_calls"):
        _session(max_calls=bad)


# ── 3. what is stamped, and what is not ─────────────────────────────────────
def test_index_and_id_are_stamped_but_the_hashes_are_measured():
    """🔴 THE HONEST HALF OF THE BINDING. A one-block request is index 0 by
    construction, so those two fields carry no information — the session supplies
    them because it is the caller. The hashes are NOT supplied: they come out of
    the extractor bound to the candidate's own bytes and canon, and they are what
    an answer about the wrong text would fail on."""
    canon = _entity_canon()
    block = _block()
    session = _session()
    art = asyncio.run(session.extract_chapter(
        chapter_index=1, chapter_id="ch2", block_bytes=block, canon=canon))

    assert art.chapter_index == 1 and art.chapter_id == "ch2"      # stamped
    assert art.content_sha256 == cl.sha256_hex(block)              # measured
    assert art.canon_sha256 == canon.canon_sha256                  # measured
    # And the engine's own binding check accepts it for THIS chapter only.
    import canon_lite_l3_repair as rp
    assert rp._binding_ok(art, chapter_index=1, chapter_id="ch2",
                          candidate=block, canon=canon)
    assert not rp._binding_ok(art, chapter_index=1, chapter_id="ch2",
                              candidate=block + b" ", canon=canon)


def test_a_candidate_that_is_not_one_block_is_refused():
    session = _session()
    canon = _entity_canon()
    for bad in (b"tanpa judul sama sekali\n",
                b"## Bab 2\nsatu\n\n## Bab 3\ndua\n"):
        with pytest.raises(ValueError, match="one chapter block"):
            asyncio.run(session.extract_chapter(
                chapter_index=1, chapter_id="ch2", block_bytes=bad, canon=canon))


# ── 4. mode isolation, proven in a clean interpreter ────────────────────────
@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_a_non_assist_job_imports_no_l3_module_and_spends_nothing(mode):
    """🔴 MEASURED IN A FRESH INTERPRETER. Asserting it in-process proves nothing:
    this test file has already imported every module the check is about."""
    script = (
        "import asyncio, sys; sys.path.insert(0, 'python');\n"
        "import narration_api as na\n"
        f"r = {{'book': 'x'}}\n"
        f"out = asyncio.run(na._canon_lite_l3_assist_repair("
        f"r, mode={mode!r}, canon=None, wave_token=None))\n"
        "assert out is None, out\n"
        "assert r == {'book': 'x'}, r\n"
        "leaked = [m for m in ('canon_lite_l3_adapter', 'canon_lite_l3_repair',\n"
        "                      'canon_lite_qc_runner', 'canon_lite_qc_provider')\n"
        "          if m in sys.modules]\n"
        "print('LEAKED:' + ','.join(leaked))\n"
    )
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    out = subprocess.run([sys.executable, "-c", script], cwd=root,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-2000:]
    leaked = out.stdout.strip().rsplit("LEAKED:", 1)[-1].strip()
    assert leaked == "", f"mode={mode} imported a paid-path module: {leaked}"


# ── 5. the tenant has to reach the REAL QC gate ─────────────────────────────
#
# 🔴 EVERY OTHER CONTROL IN THIS FILE STUBS THE WAVE, so none of them can see the
#    gate inside it. That is exactly where the tenant went missing: the seam
#    resolved `assist` for a legitimate canary, entered `maybe_run_metered_wave`,
#    and the gate there re-resolved the mode WITHOUT the tenant — getting `off`,
#    refusing the wave, never calling `on_session`. Assist then reported
#    `unchecked/no_session` having repaired nothing, and every stubbed test stayed
#    green because none of them ran that gate.

def _qc_env(mode="assist", allow="t-canary"):
    import canon_lite_qc_provider as qc
    env = {"NARASI_CANON_LITE_MODE": mode,
           "CANON_LITE_EXTRACTOR_CONCURRENCY": "2", "L2B_MAX_INFLIGHT": "8",
           qc.QC_API_KEY_ENV: "dummy-not-a-real-credential"}
    if allow is not None:
        env["NARASI_CANON_LITE_ASSIST_TENANTS"] = allow
    return env


@pytest.mark.parametrize("allow,tenant,permitted", [
    ("t-canary", "t-canary", True),        # the canary itself
    ("t-canary", "t-other", False),        # a different tenant
    ("t-canary", None, False),             # tenant not threaded at all
    (None, "t-canary", False),             # allowlist unset
    ("", "t-canary", False),               # allowlist empty
])
def test_the_qc_gate_decides_on_the_JOBS_mode_not_the_deployments(
        allow, tenant, permitted):
    """The gate that arms the metered wave, driven directly."""
    import canon_lite_qc_meter as meter
    import canon_lite_qc_runner as runner
    meter.reset_host_role_for_tests()
    try:
        meter.declare_host_role("narration_worker")
        assert runner.metered_wave_permitted(
            _qc_env(allow=allow), tenant_id=tenant) is permitted
    finally:
        meter.reset_host_role_for_tests()


def test_a_non_canary_stops_before_the_provider_is_ever_built():
    """🔴 REFUSED BEFORE THE ADAPTER EXISTS, not after it declines. The gate is the
    only layer that can promise zero provider construction and zero meter rows."""
    import canon_lite_qc_meter as meter
    import canon_lite_qc_runner as runner
    built = []
    meter.reset_host_role_for_tests()
    try:
        meter.declare_host_role("narration_worker")
        got = asyncio.run(runner.maybe_run_metered_wave(
            l2.materialize_final_snapshot({"book": BOOK}), _entity_canon(),
            run_id="j1", job_uuid="00000000-0000-4000-8000-000000000001",
            job_external_id="j1", environ=_qc_env(), wave_token=object(),
            tenant_id="t-other",
            adapter_factory=lambda: built.append(1) or _Metered(),
            on_session=lambda m: built.append("session")))
        assert got is None
        assert built == [], "a non-canary job built a provider or a session"
    finally:
        meter.reset_host_role_for_tests()


def test_the_gate_reads_the_effective_mode_not_the_global_one():
    """By construction: the wrong resolver here is invisible in behaviour until a
    canary runs in production."""
    import inspect
    import canon_lite_qc_runner as runner
    src = inspect.getsource(runner.metered_wave_permitted)
    assert "resolve_effective_mode" in src
    assert "tenant_id=tenant_id" in src
    # and the seam threads it
    import narration_api as na
    seam = inspect.getsource(na._canon_lite_l3_assist_repair)
    assert "tenant_id=tenant_id" in seam


# ── 6. Phase A still reads the DEPLOYMENT's configuration ───────────────────
def test_operator_phase_a_reads_the_global_mode_not_a_tenants():
    """🔴 THE OTHER HALF OF THE SPLIT. Phase A asks "is this deployment configured
    for a mode", which has no tenant and must not acquire one — narrowing it would
    make an operator readiness verdict say OFF on a deployment that is very much
    ON for its canary."""
    import canon_lite as cl
    import canon_lite_qc_meter as meter
    env = _qc_env(allow="t-canary")
    assert cl.resolve_mode(env) == "assist"
    assert cl.resolve_effective_mode(env, tenant_id="t-other") == "off"

    # Phase A is a readiness-to-reset gate: it REQUIRES the flag to be off, so a
    # deployment configured `assist` must fail it with `l2b_flag_not_off`.
    # `inflight_raw` is the RAW Redis value, so it is a string here — an int reads
    # as malformed and would make both verdicts fail for an unrelated reason,
    # which would look like this control passing.
    armed = meter.phase_a_from_product_resolver(
        inflight_raw="0", inflight_unreadable=False, attempted_rows=0,
        max_inflight=8, environ=env)
    assert armed.passes is False and armed.reason_code == "l2b_flag_not_off", \
        ("Phase A read a per-tenant answer and called an armed deployment OFF — "
         f"got {armed}")

    # ...and it does pass on a deployment that really is off, so the assertion
    # above is discriminating rather than a verdict that always fails.
    off_env = dict(env)
    off_env["NARASI_CANON_LITE_MODE"] = "off"
    assert meter.phase_a_from_product_resolver(
        inflight_raw="0", inflight_unreadable=False, attempted_rows=0,
        max_inflight=8, environ=off_env).passes is True


def test_a_canary_passes_the_real_qc_gate_and_reaches_the_provider():
    """🔴 THE POSITIVE TWIN. A gate that refuses EVERYBODY satisfies every negative
    control in this file — including the one directly above — while leaving assist
    permanently inert. That is precisely the shape of the defect this round is
    closing: the gate re-resolved the mode without the tenant, said `off`, and no
    canary ever got a session.

    The adapter factory is the tripwire. It is only reached once the gate has
    permitted the wave, so reaching it IS the proof, and raising from it stops the
    run before any real provider work happens.
    """
    import canon_lite_extractor as _ext
    import canon_lite_qc_meter as meter
    import canon_lite_qc_runner as runner

    class _Reached(RuntimeError):
        pass

    def _factory():
        raise _Reached()

    meter.reset_host_role_for_tests()
    try:
        meter.declare_host_role("narration_worker")
        with pytest.raises(_Reached):
            asyncio.run(runner.maybe_run_metered_wave(
                l2.materialize_final_snapshot({"book": BOOK}), _entity_canon(),
                run_id="j1", job_uuid="00000000-0000-4000-8000-000000000001",
                job_external_id="j1", environ=_qc_env(),
                wave_token=_ext.ExtractionWaveToken(),
                tenant_id="t-canary", adapter_factory=_factory))
    finally:
        meter.reset_host_role_for_tests()
