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
        start = raw.find(needle.encode("utf-8"))
        end = start + len(needle.encode("utf-8"))
        return {
            "claims": [{
                "claim_type": l2.CLAIM_ENTITY_MENTION, "canon_ref": "e1",
                "evidence_start": start, "evidence_end": end,
                "evidence_sha256": cl.sha256_hex(raw[start:end]),
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
