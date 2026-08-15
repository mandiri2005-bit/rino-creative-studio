"""F3 — a `.strip()`-ped L3 repair candidate must LAND, not merely reach the validator.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F3 + the 2026-08-15 closing brief.

PROVEN MECHANISM (reproduced below before any fix code was written, per the brief's
instruction not to patch a guess): `_rebuild()` (canon_lite_l3_repair.py) joins blocks
with `b"".join(blocks)` — NO separator of its own. Each block's OWN trailing bytes (the
blank line before the next chapter's heading) are what keep the whole-book re-split able
to see the next `## ` as a heading at all. A provider that returns a repaired chapter
`.strip()`-ped of its own trailing whitespace — an ordinary, unremarkable thing for an
LLM completion to do — glues the next chapter's heading onto the candidate's last prose
line. Re-materializing then finds ONE FEWER block: `post.block_count` fires, and the
stage it names (block COUNT changed) is a downstream symptom, not the cause.

Fix (canon_lite_l3_adapter.py `repair_provider`): split the original block into a CORE
and its exact trailing frame BEFORE sending it to the model; send only the core;
reattach the ORIGINAL's own exact trailing bytes to whatever the model returns. No
generic `"\\n\\n"` is ever introduced — only bytes the original block already had.

🔴 WHAT THIS FILE HAD TO GROW. Its first six tests proved the adapter's framing in
   ISOLATION, which is not the DoD. "The candidate reached the validator" and "the
   candidate was ACCEPTED, changed the manuscript, and shipped" are different claims,
   and only the second one closes F3. §3 below therefore drives the REAL production
   seam — real session, real `repair_manuscript`, real staging/rebuild/materialization
   — faking only the two outbound boundaries (`run_worker` and the metered QC
   provider), and §4 runs every negative case through the ENGINE rather than asserting
   that the adapter left the damage visible.
"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "python"))

import canon_lite as cl                          # noqa: E402
import canon_lite_l2 as l2                       # noqa: E402
import canon_lite_l3_adapter as ad               # noqa: E402
import canon_lite_l3_repair as rp                # noqa: E402
import canon_lite_qc_runner as qcr               # noqa: E402
import narration_api as na                       # noqa: E402
from orchestrator import static as st            # noqa: E402


THREE_CHAPTER_BOOK = (
    "## Chapter 1\nMaya walked along the shoreline.\n\n"
    "## Chapter 2\nThe lighthouse keeper counted lanterns.\n\n"
    "## Chapter 3\nThe tide finally turned.\n"
)

GOOD = "Suranto"
BAD = "Hartono"
#: Three chapters, REAL separators, one repairable violation (chapter 2 names BAD where
#: the canon says GOOD). Three rather than two because the defect is about the boundary
#: BETWEEN blocks: with two chapters a lost separator can only ever damage the tail, and
#: the "untargeted blocks stay byte-identical" claim has no block after the target to
#: make it non-trivial.
BOOK3 = (f"## Bab 1\nNamanya {GOOD} di desa itu.\n\n"
         f"## Bab 2\nNamanya {BAD} di kota itu.\n\n"
         f"## Bab 3\nAkhirnya {GOOD} pulang.\n")

CANARY = "t-canary"


def _canon3():
    chapters = tuple(
        cl.CanonChapterV1(chapter_id=f"ch{i + 1}", order=i + 1,
                          expected_title=cl.UNKNOWN)
        for i in range(3))
    return cl._finalize({
        "schema_version": cl.SCHEMA_VERSION,
        "outline_sha256": "a" * 64, "generation_config_sha256": "b" * 64,
        "target_language": "id", "chapters": chapters,
        "entities": (cl.CanonEntityV1(entity_id="e1", canonical_name=GOOD,
                                      aliases=(), alias_source="none"),),
        "anchors": (), "one_time_events": (),
        "reveals": (), "flashback_exceptions": (),
        "fact_source_policy": "fiction_generated",
        "advisory_bible_sha256": cl.UNKNOWN,
    })


def _coverage(entity_state):
    states = {
        l2.PREDICATE_ENTITY_NAME: entity_state,
        l2.PREDICATE_FIXED_LITERAL: l2.COVERAGE_NO_CLAIMS_FOUND,
        l2.PREDICATE_ONE_TIME_EVENT: l2.COVERAGE_NO_CLAIMS_FOUND,
    }
    return tuple(l2.PredicateCoverageV1(predicate=p, state=states[p])
                 for p in l2.SEMANTIC_PREDICATES)


def _claims_for(block_bytes, *, index, chapter_id, canon, needle):
    start = block_bytes.find(needle.encode("utf-8"))
    assert start >= 0, (needle, block_bytes)
    end = start + len(needle.encode("utf-8"))
    return l2.ChapterClaimsV1(
        schema_version=l2.CLAIMS_SCHEMA_VERSION, chapter_index=index,
        chapter_id=chapter_id, content_sha256=cl.sha256_hex(block_bytes),
        canon_sha256=canon.canon_sha256,
        atom_table_sha256=l2.build_chapter_atom_table(block_bytes)[1],
        extractor_version=cl.L2_EXTRACTOR_VERSION, model_version="m1",
        prompt_sha256="c" * 64, predicate_set_version=l2.PREDICATE_SET_VERSION,
        coverage=_coverage(l2.COVERAGE_CHECKED),
        claims=(l2.ObservedClaimV1(
            claim_type=l2.CLAIM_ENTITY_MENTION, canon_ref="e1",
            evidence_start=start, evidence_end=end,
            evidence_sha256=cl.sha256_hex(block_bytes[start:end])),))


# ===========================================================================
# 1. The mechanism itself, isolated — proves the CAUSE, not just a symptom,
#    and stands as a permanent regression pin regardless of where the fix lives.
# ===========================================================================

def test_stripped_candidate_reproduces_post_block_count_via_rebuild_join():
    snap = l2.materialize_final_snapshot({"book": THREE_CHAPTER_BOOK})
    assert len(snap.blocks) == 3
    original_ch1 = snap.block_bytes(0)

    # A full-chapter, valid, heading-intact candidate — the ONLY thing wrong with it is
    # that its own trailing whitespace (the blank line before "## Chapter 2") was
    # stripped, exactly what an ordinary LLM completion does.
    stripped_candidate = original_ch1.decode("utf-8").strip().encode("utf-8")
    assert stripped_candidate != original_ch1
    assert stripped_candidate.rstrip() == stripped_candidate  # confirms it IS stripped

    staged_blocks = [stripped_candidate, snap.block_bytes(1), snap.block_bytes(2)]
    staged_bytes = snap.manuscript_bytes[:snap.prefix_byte_end] + b"".join(staged_blocks)
    staged_snapshot = l2.materialize_final_snapshot({"book": staged_bytes.decode("utf-8")})

    assert staged_snapshot is None or len(staged_snapshot.blocks) != len(snap.blocks), (
        "if this assertion ever fails, the mechanism has changed — re-diagnose "
        "before trusting any fix built on this test file")
    if staged_snapshot is not None:
        assert len(staged_snapshot.blocks) == 2, "chapter 2's heading was swallowed"


# ===========================================================================
# 2. The fix at the adapter — `repair_provider` must reattach the original's
#    exact trailing frame regardless of what the (stubbed) model returns.
# ===========================================================================

class _StrippingWorker:
    """Stands in for `orchestrator.static.run_worker` — returns `original_body` run
    through `output_transform`, default `.strip()`, exactly what an ordinary LLM
    completion routinely does to its own trailing whitespace. Records every prompt so a
    test can assert on what was actually SENT, independent of what it returns.

    Does NOT try to parse the chapter body back out of the prompt: the instruction
    preamble itself contains blank-line-separated paragraphs, so a naive
    `rsplit("\\n\\n", 1)` silently returns an EMPTY string whenever the body appended
    last also ends in "\\n\\n" — a stub bug that looks exactly like a production bug from
    the outside. Told the exact original body once, at construction.
    """

    def __init__(self, original_body: str, output_transform=lambda body: body.strip()):
        self.prompts = []
        self._original_body = original_body
        self._transform = output_transform

    async def __call__(self, worker, prompt, *, timeout, task_id):
        self.prompts.append(prompt)
        assert self._original_body in prompt, (
            "the adapter did not send the expected chapter body in its prompt")
        return {"output": self._transform(self._original_body)}


def _session():
    async def _metered(_request):
        raise AssertionError("extract_chapter not exercised by this test")
    return ad.L3AssistSession(metered_provider=_metered, canon_text="CANON v1\n",
                              worker_model="m1")


def test_repair_provider_reattaches_the_exact_original_trailing_frame(monkeypatch):
    snap = l2.materialize_final_snapshot({"book": THREE_CHAPTER_BOOK})
    original_ch1 = snap.block_bytes(0)
    expected_trailing_frame = original_ch1[len(original_ch1.rstrip()):]
    assert expected_trailing_frame == b"\n\n"

    worker = _StrippingWorker(original_ch1.decode("utf-8").rstrip())
    monkeypatch.setattr(st, "run_worker", worker)

    session = _session()
    candidate = asyncio.run(session.repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=original_ch1,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    assert candidate is not None
    assert candidate.endswith(expected_trailing_frame), (
        "the candidate must carry the ORIGINAL block's own exact trailing bytes, "
        f"got tail={candidate[-10:]!r}")
    assert worker.prompts, "run_worker was never called"


def test_the_prompt_carries_the_core_only_and_never_the_trailing_frame(monkeypatch):
    """§2 of the brief, explicitly. Sending the frame to the model would put the very
    bytes the fix exists to own back under the model's control — it could return them
    altered, and the reattachment would then be papering over a second copy rather than
    restoring the only one."""
    snap = l2.materialize_final_snapshot({"book": THREE_CHAPTER_BOOK})
    original_ch1 = snap.block_bytes(0)
    core = original_ch1.rstrip()
    frame = original_ch1[len(core):]
    assert frame == b"\n\n"

    worker = _StrippingWorker(core.decode("utf-8"))
    monkeypatch.setattr(st, "run_worker", worker)
    asyncio.run(_session().repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=original_ch1,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    assert len(worker.prompts) == 1
    prompt = worker.prompts[0]
    assert prompt.endswith(core.decode("utf-8")), (
        "the prompt must END with the core; anything after it is the frame leaking "
        f"into the model's input, tail={prompt[-12:]!r}")
    assert not prompt.endswith(original_ch1.decode("utf-8")), (
        "the ORIGINAL block (core + its trailing frame) was sent verbatim — the split "
        "did not happen")


def test_repair_provider_reattachment_makes_the_staged_manuscript_keep_its_block_count(
        monkeypatch):
    snap = l2.materialize_final_snapshot({"book": THREE_CHAPTER_BOOK})
    original_ch1 = snap.block_bytes(0)
    monkeypatch.setattr(st, "run_worker",
                        _StrippingWorker(original_ch1.decode("utf-8").rstrip()))
    session = _session()

    candidate = asyncio.run(session.repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=original_ch1,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    staged_bytes = (snap.manuscript_bytes[:snap.prefix_byte_end] + candidate
                    + snap.block_bytes(1) + snap.block_bytes(2))
    staged_snapshot = l2.materialize_final_snapshot({"book": staged_bytes.decode("utf-8")})
    assert staged_snapshot is not None
    assert len(staged_snapshot.blocks) == 3


def test_repair_provider_reattaches_a_trailing_frame_that_differs_from_any_generic_fallback(
        monkeypatch):
    """The b"\\n\\n" fixture above cannot distinguish "reattaches the block's own real
    bytes" from "always emits a blank line" — a generic fallback produces the same two
    newlines. This fixture's frame is THREE newlines, which no generic fallback can
    produce, so only a byte-for-byte reattachment passes."""
    book = ("## Chapter 1\nMaya walked along the shoreline.\n\n\n"
            "## Chapter 2\nThe lighthouse keeper counted lanterns.\n")
    snap = l2.materialize_final_snapshot({"book": book})
    assert len(snap.blocks) == 2
    original_ch1 = snap.block_bytes(0)
    expected_trailing_frame = original_ch1[len(original_ch1.rstrip()):]
    assert expected_trailing_frame == b"\n\n\n"

    monkeypatch.setattr(st, "run_worker",
                        _StrippingWorker(original_ch1.decode("utf-8").rstrip()))
    candidate = asyncio.run(_session().repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=original_ch1,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    assert candidate is not None
    assert candidate.endswith(expected_trailing_frame), (
        "must reattach the block's own real trailing bytes, not a generic "
        f"fallback; got tail={candidate[-10:]!r}")


def test_repair_provider_preserves_a_single_trailing_newline_frame(monkeypatch):
    """The LAST chapter usually carries a single `\\n` and no blank line. The fix must
    not widen it into a generic blank-line separator.

    (Named for what the fixture actually IS. It was called "...no trailing whitespace at
    all" while asserting a one-byte `b"\\n"` frame — the genuinely empty frame is the
    test below, and the two are different claims.)"""
    book = "## Chapter 1\nOnly one chapter, no trailing blank line.\n"
    snap = l2.materialize_final_snapshot({"book": book})
    last = snap.block_bytes(0)
    expected_trailing_frame = last[len(last.rstrip()):]
    assert expected_trailing_frame == b"\n"

    worker = _StrippingWorker(last.decode("utf-8").rstrip())
    monkeypatch.setattr(st, "run_worker", worker)
    candidate = asyncio.run(_session().repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=last,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    assert candidate is not None
    assert candidate.endswith(expected_trailing_frame)
    assert not candidate.endswith(b"\n\n"), (
        "a hardcoded generic blank-line fallback must not be manufactured where "
        f"the original had none; got tail={candidate[-10:]!r}")
    assert worker.prompts, "run_worker was never called"


def test_repair_provider_manufactures_nothing_for_a_zero_byte_frame(monkeypatch):
    """The genuinely EMPTY frame — a manuscript whose last chapter ends without any
    trailing newline at all. `b""` is the one frame a "reattach something sensible"
    implementation is most tempted to fill in, and the only fixture that can tell
    "reattach the original's own bytes" apart from "reattach unless empty"."""
    book = "## Chapter 1\nNo trailing newline at all."
    snap = l2.materialize_final_snapshot({"book": book})
    last = snap.block_bytes(0)
    assert last[len(last.rstrip()):] == b"", "the fixture must carry a ZERO-byte frame"

    worker = _StrippingWorker(last.decode("utf-8").rstrip())
    monkeypatch.setattr(st, "run_worker", worker)
    candidate = asyncio.run(_session().repair_provider(
        chapter_index=0, chapter_id="ch1", block_bytes=last,
        violation_codes=(l2.VIOLATION_ENTITY_NAME,), canon=None, attempt=1))

    assert candidate is not None
    assert candidate == candidate.rstrip(), (
        "nothing may be manufactured where the original block had no trailing bytes; "
        f"got tail={candidate[-10:]!r}")
    assert candidate == last, "a zero-frame block must come back byte-identical here"


# ===========================================================================
# 3. THE DoD — a stripped semantic repair LANDS, end to end, on the production
#    seam. Real session, real repair_manuscript, real staging/materialization;
#    only `run_worker` and the metered QC provider are faked.
# ===========================================================================

@pytest.fixture
def production_shape(monkeypatch):
    """Allowlist the tenant AND pin both hooks to None — the production shape (the
    hooks are test injection points and are None in a real job)."""
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", CANARY)
    before = (na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR)
    na._L3_REPAIR_PROVIDER = None
    na._L3_CHAPTER_EXTRACTOR = None
    yield
    na._L3_REPAIR_PROVIDER, na._L3_CHAPTER_EXTRACTOR = before


class _ReadingExtractor:
    """The metered QC boundary. Reads the ACTUAL candidate bytes it is handed and
    reports what it really finds — a stub that always returns a clean verdict would
    make every candidate look repaired and the closed-loop assertion vacuous."""

    def __init__(self):
        self.calls = 0
        self.seen = []

    async def __call__(self, request):
        self.calls += 1
        body = request.chapter_bytes
        self.seen.append(body)
        start = body.find(GOOD.encode("utf-8"))
        if start < 0:
            # Honest "found nothing to bless": the schema refuses CHECKED with no claims.
            return {"coverage": {p: l2.COVERAGE_NO_CLAIMS_FOUND
                                 for p in l2.SEMANTIC_PREDICATES},
                    "claims": []}
        end = start + len(GOOD.encode("utf-8"))
        atom_start = next(a.index for a in request.chapter_atoms if a.byte_start == start)
        atom_end = next(a.index for a in request.chapter_atoms if a.byte_end == end)
        return {
            "coverage": {p: (l2.COVERAGE_CHECKED if p == "entity_name_contradiction"
                             else l2.COVERAGE_NO_CLAIMS_FOUND)
                         for p in l2.SEMANTIC_PREDICATES},
            "claims": [{"claim_type": l2.CLAIM_ENTITY_MENTION, "canon_ref": "e1",
                        "atom_start": atom_start, "atom_end": atom_end}],
        }


def _result3(book=BOOK3):
    bodies = [seg.split("\n", 1)[1].rstrip("\n") for seg in book.split("## ")[1:]]
    return {"book": book,
            "chapters": [{"no": i, "ok": True, "content": b} for i, b in enumerate(bodies)],
            "n_ok": len(bodies), "n_total": len(bodies)}


def _drive_seam(monkeypatch, *, output_transform):
    """The production seam, driven exactly as a real job drives it."""
    canon = _canon3()
    snap = l2.materialize_final_snapshot({"book": BOOK3}, canon=canon)
    assert len(snap.blocks) == 3
    extractor = _ReadingExtractor()
    counters = {"waves": 0, "repairs": 0}

    claims = {
        0: _claims_for(snap.block_bytes(0), index=0,
                       chapter_id=snap.blocks[0].chapter_id, canon=canon, needle=GOOD),
        1: _claims_for(snap.block_bytes(1), index=1,
                       chapter_id=snap.blocks[1].chapter_id, canon=canon, needle=BAD),
        2: _claims_for(snap.block_bytes(2), index=2,
                       chapter_id=snap.blocks[2].chapter_id, canon=canon, needle=GOOD),
    }

    async def _fake_wave(*_a, on_session=None, **_k):
        counters["waves"] += 1
        assert on_session is not None, "the seam stopped passing on_session"
        on_session(extractor)
        return claims

    monkeypatch.setattr(qcr, "maybe_run_metered_wave", _fake_wave)

    repaired_full_block = snap.block_bytes(1).replace(
        BAD.encode("utf-8"), GOOD.encode("utf-8")).decode("utf-8")

    async def _fake_run_worker(worker, prompt, timeout=None, task_id=None):
        counters["repairs"] += 1
        return {"ok": True, "output": output_transform(repaired_full_block)}

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)

    result = _result3()
    telemetry = asyncio.run(na._canon_lite_l3_assist_repair(
        result, mode="assist", canon=canon, wave_token=None, run_id="j1",
        tenant_id=CANARY))
    return telemetry, result, counters, extractor, snap


def test_a_stripped_semantic_repair_lands_end_to_end(production_shape, monkeypatch):
    """🔴 THE F3 DoD. The provider returns a valid full chapter with its trailing
    whitespace stripped — the exact shape canary v9 died on. It must be ACCEPTED, change
    the manuscript, and ship. "Reached the validator" is not the bar."""
    telemetry, result, counters, extractor, snap = _drive_seam(
        monkeypatch, output_transform=lambda block: block.strip())

    # the stub really did strip — otherwise this test silently tests nothing
    assert snap.block_bytes(1).decode("utf-8").rstrip() != snap.block_bytes(1).decode("utf-8")

    assert counters["waves"] == 1
    assert counters["repairs"] == 1, "one repairable violation, one repair call"
    assert extractor.calls == 1, "re-extraction must ride the job's own metered session"

    recorded = result["canon_lite_l3"]
    assert telemetry.get("stage") == "complete"
    assert recorded["chapters_repaired"] == 1
    assert recorded["manuscript_changed"] is True
    assert recorded["delivery_binding"] == l2.BINDING_MATCH

    delivered = l2.materialize_final_snapshot(result, canon=None)
    assert delivered is not None
    assert len(delivered.blocks) == 3, "chapter count must survive the repair"
    assert recorded["manuscript_sha256"] == delivered.manuscript_sha256, (
        "the recorded hash does not describe the manuscript on the delivery path")


def test_the_landed_repair_keeps_headings_order_and_every_untargeted_byte(
        production_shape, monkeypatch):
    """The other half of the DoD, and the test the old substring version could not make:
    compare untargeted blocks by BYTE EQUALITY after an ACCEPTED repair, not by
    containment on a corrupted manuscript."""
    _telemetry, result, _counters, _extractor, snap = _drive_seam(
        monkeypatch, output_transform=lambda block: block.strip())

    delivered = l2.materialize_final_snapshot(result, canon=None)
    original_headings = [snap.block_bytes(i).split(b"\n", 1)[0] for i in range(3)]
    delivered_headings = [delivered.block_bytes(i).split(b"\n", 1)[0] for i in range(3)]
    assert delivered_headings == original_headings, "heading text and order must be identical"

    # untargeted blocks: byte equality, not containment
    for i in (0, 2):
        assert delivered.block_bytes(i) == snap.block_bytes(i), (
            f"block {i} was not targeted and must be byte-identical")

    # the targeted block: violation gone, and the ORIGINAL trailing frame preserved
    target_before, target_after = snap.block_bytes(1), delivered.block_bytes(1)
    assert BAD.encode("utf-8") not in target_after, "the violation survived the repair"
    assert GOOD.encode("utf-8") in target_after
    frame_before = target_before[len(target_before.rstrip()):]
    frame_after = target_after[len(target_after.rstrip()):]
    assert frame_before == b"\n\n"
    assert frame_after == frame_before, (
        f"the target lost its exact original trailing frame: {frame_after!r}")
    assert BAD not in result["book"]


# ===========================================================================
# 4. NEGATIVE goldens — executed through the ENGINE, not asserted on the
#    adapter's output. "The guard is still there because I did not edit it" is
#    not evidence; the rejection path has to run.
#
# 🔴 SCOPE NOTE — `post.chapter_identity` is DELIBERATELY not proved here, and is
#    not among prove_f3.py's mutants. Ownership of that gate's evidence stays with
#    the synthetic case in test_canon_lite_l3_repair_stage2.py, because no candidate
#    F3 can produce reaches it: one that renames a heading is refused earlier at
#    `pre.heading`, and one that ADDS a heading is refused by the adapter's one-block
#    preflight (and, absent the adapter, by `post.block_count`). Manufacturing a
#    "realistic" fixture for an unreachable branch would be inventing coverage, which
#    is the failure mode this suite has been burned by more than once.
# ===========================================================================

def _repaired_snapshot(run, original_snapshot):
    """Materialize the manuscript the ENGINE actually produced.

    🔴 NOT FROM `result`. `repair_manuscript` does NOT write the repaired book back into
       the caller's `result` dict — that substitution belongs to the production seam. An
       engine-level assertion that reads `result` is therefore reading the ORIGINAL
       bytes and passes no matter what the repair did: two tests here were written that
       way and were VACUOUS until mutant 2 of prove_f3.py survived and exposed them. The
       authoritative bytes are `run.repaired_manuscript`, which the schema binds to
       `manuscript_sha256_after`. Guarded below so this can never go quiet again."""
    assert run.repaired_manuscript != original_snapshot.manuscript_bytes, (
        "the engine reported a repair but produced byte-identical output — this "
        "assertion exists so a vacuous fixture fails loudly instead of passing")
    snap = l2.materialize_final_snapshot(
        {"book": run.repaired_manuscript.decode("utf-8")}, canon=None)
    assert snap is not None
    return snap


def _stage_recorder(monkeypatch):
    """Record which stage each rejection was attributed to, without silencing the real
    logger — the stage NAME is the contract here, not merely that something failed."""
    seen = []
    real = rp._log_invalid_candidate

    def _rec(stage, **kw):
        seen.append(stage)
        return real(stage, **kw)

    monkeypatch.setattr(rp, "_log_invalid_candidate", _rec)
    return seen


#: Same three chapters, long enough that a SMUGGLED heading line stays under the 15%
#: edit-ratio guard. Measured, not guessed: against the short BOOK3 the extra-heading
#: candidate is ~70% of the chapter, so `exceeds_edit_guard` rejects it BEFORE staging
#: and `post.block_count` never runs — the test would have passed on "something refused
#: it" while proving nothing about the gate it names.
_FILLER = ("Hari itu hujan turun perlahan di atas atap seng, dan suara langkahnya "
           "terdengar sampai ke ujung lorong yang sempit. ")
LONG_BOOK3 = (f"## Bab 1\nNamanya {GOOD} di desa itu. " + _FILLER * 3 + "\n\n"
              f"## Bab 2\nNamanya {BAD} di kota itu. " + _FILLER * 3 + "\n\n"
              f"## Bab 3\nAkhirnya {GOOD} pulang. " + _FILLER * 3 + "\n")


def _engine_run(monkeypatch, *, output_transform, book=BOOK3):
    """Real `L3AssistSession.repair_provider` + real `repair_manuscript`."""
    canon = _canon3()
    snap = l2.materialize_final_snapshot({"book": book}, canon=canon)
    assert len(snap.blocks) == 3
    claims = {
        i: _claims_for(snap.block_bytes(i), index=i,
                       chapter_id=snap.blocks[i].chapter_id, canon=canon,
                       needle=(BAD if i == 1 else GOOD))
        for i in range(3)
    }
    repaired_full_block = snap.block_bytes(1).replace(
        BAD.encode("utf-8"), GOOD.encode("utf-8")).decode("utf-8")

    async def _fake_run_worker(worker, prompt, timeout=None, task_id=None):
        return {"ok": True, "output": output_transform(repaired_full_block)}

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)
    extractor = _ReadingExtractor()
    session = ad.L3AssistSession(metered_provider=extractor, canon_text="CANON v1\n",
                                 worker_model="m1")
    result = {"book": book}
    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=session.repair_provider,
        extract_chapter=session.extract_chapter))
    return run, snap, result


def test_engine_rejects_a_renamed_heading_at_pre_heading(monkeypatch):
    stages = _stage_recorder(monkeypatch)
    run, snap, _result = _engine_run(
        monkeypatch,
        output_transform=lambda b: b.strip().replace("## Bab 2", "## Bab Dua"))

    telemetry = rp.run_telemetry(run)
    assert telemetry["chapters_repaired"] == 0
    assert telemetry["manuscript_changed"] is False
    assert rp.REASON_INVALID_CANDIDATE in telemetry["unresolved_reasons"]
    assert "pre.heading" in stages, f"stages seen: {stages}"
    assert run.manuscript_sha256_after == snap.manuscript_sha256, (
        "a rejected candidate must leave the manuscript byte-identical")


def test_engine_rejects_a_dropped_heading_at_pre_heading(monkeypatch):
    stages = _stage_recorder(monkeypatch)
    run, snap, _result = _engine_run(
        monkeypatch,
        output_transform=lambda b: b.strip().split("\n", 1)[1])

    assert rp.run_telemetry(run)["chapters_repaired"] == 0
    assert "pre.heading" in stages, f"stages seen: {stages}"
    assert run.manuscript_sha256_after == snap.manuscript_sha256


def test_engine_rejects_an_extra_inserted_heading_before_it_can_be_staged(monkeypatch):
    """Correct FIRST heading — so `pre.heading` is satisfied — but a second chapter
    heading smuggled into the body.

    🔴 MEASURED, AND IT CONTRADICTS THE BRIEF'S PREDICTION. The brief expects this at
       `post.block_count`. It is actually refused EARLIER, by the adapter's own
       one-block preflight (`canon_lite_l3_adapter.py:286`, "candidate does not form
       one chapter block"), which materializes the candidate alone and requires exactly
       one block before spending a re-extraction on it. The engine therefore records
       `reextraction_failed` and `post.block_count` never runs. Asserting the predicted
       stage here would have been a green test for a thing that never happened.
       `post.block_count` is pinned separately, below, by the one candidate shape that
       can actually reach it.

       ⚠ PRECISION ON THE COST CLAIM: no provider CALL is made, but the session's
         attempt budget has ALREADY been debited — `extract_chapter`'s first statement
         is `self._spend("extract")`, ahead of the preflight. "Refused before the
         metered call" is true; "refused for free" would not be."""
    stages = _stage_recorder(monkeypatch)
    run, snap, _result = _engine_run(
        monkeypatch, book=LONG_BOOK3,
        output_transform=lambda b: b.strip() + "\n\n## Bab 2b\nBab selundupan.")

    telemetry = rp.run_telemetry(run)
    assert telemetry["chapters_repaired"] == 0
    assert telemetry["manuscript_changed"] is False
    assert telemetry["unresolved_reasons"] == {"reextraction_failed": 1}
    assert stages == [], (
        f"no invalid-candidate stage should run — it never got that far: {stages}")
    assert run.manuscript_sha256_after == snap.manuscript_sha256


def test_post_block_count_still_fires_for_the_one_shape_that_reaches_it(monkeypatch):
    """`post.block_count` is precisely the gate F3's reattachment stops from firing, so
    the only candidate that can still reach it is one that lost its separator WITHOUT
    the adapter — i.e. the pre-F3 world. Driven with a raw provider (no adapter) to
    reproduce it through the real engine, proving the gate is live and that F3 removed
    the CAUSE rather than the check."""
    stages = _stage_recorder(monkeypatch)
    canon = _canon3()
    # LONG chapters deliberately: on the short fixture, dropping two newlines is itself
    # >15% of the chapter, so `exceeds_edit_guard` rejects the candidate before staging
    # and the block-count gate is never reached (measured — `guard_rejected`).
    snap = l2.materialize_final_snapshot({"book": LONG_BOOK3}, canon=canon)
    claims = {
        i: _claims_for(snap.block_bytes(i), index=i,
                       chapter_id=snap.blocks[i].chapter_id, canon=canon,
                       needle=(BAD if i == 1 else GOOD))
        for i in range(3)
    }
    # A valid, heading-intact, repaired chapter — stripped, and NOT reframed.
    stripped = snap.block_bytes(1).replace(
        BAD.encode("utf-8"), GOOD.encode("utf-8")).strip()

    async def _raw_provider(**_kw):
        return stripped

    session = ad.L3AssistSession(metered_provider=_ReadingExtractor(),
                                 canon_text="CANON v1\n", worker_model="m1")
    result = {"book": LONG_BOOK3}
    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result=result, claims_by_index=claims,
        repair_provider=_raw_provider, extract_chapter=session.extract_chapter))

    telemetry = rp.run_telemetry(run)
    assert telemetry["chapters_repaired"] == 0
    assert telemetry["manuscript_changed"] is False
    assert "post.block_count" in stages, f"stages seen: {stages}"
    assert "pre.heading" not in stages, (
        "the heading was intact — a pre.heading rejection would mean the block-count "
        "gate was never the thing under test")
    assert run.manuscript_sha256_after == snap.manuscript_sha256


def test_engine_rejects_a_wrapper_preamble_rather_than_masking_it(monkeypatch):
    """A leading "Sure, here's the revised chapter:" frame must NOT be trimmed into a
    valid-looking candidate — `.rstrip()` only, never `.strip()`, on the adapter side,
    so the engine's own heading gate still has something real to catch."""
    stages = _stage_recorder(monkeypatch)
    run, snap, _result = _engine_run(
        monkeypatch,
        output_transform=lambda b: "Tentu, ini versi perbaikannya:\n\n" + b.strip())

    assert rp.run_telemetry(run)["chapters_repaired"] == 0
    assert "pre.heading" in stages, f"stages seen: {stages}"
    assert run.manuscript_sha256_after == snap.manuscript_sha256


def test_engine_rejects_a_leading_blank_frame_rather_than_masking_it(monkeypatch):
    """The whitespace form of the wrapper case, and the reason the adapter uses
    `.rstrip()` and never `.strip()`. A candidate that arrives with blank lines BEFORE
    its heading is malformed: `_heading_line` reads the first line, finds it empty, and
    `pre.heading` refuses. Trimming the leading edge in the adapter would silently turn
    that malformed candidate into a well-formed one — masking the very failure the gate
    exists to expose, and doing it on the side of the seam that has no gate of its own."""
    stages = _stage_recorder(monkeypatch)
    run, snap, _result = _engine_run(
        monkeypatch, book=LONG_BOOK3,
        output_transform=lambda b: "\n\n" + b.strip())

    assert rp.run_telemetry(run)["chapters_repaired"] == 0
    assert "pre.heading" in stages, f"stages seen: {stages}"
    assert run.manuscript_sha256_after == snap.manuscript_sha256


def test_engine_accepts_and_the_untargeted_blocks_are_byte_identical(monkeypatch):
    """The positive control for §4: with the SAME engine wiring the stripped candidate
    is accepted — so every rejection above is attributable to its own defect and not to
    a harness that could never accept anything."""
    run, snap, _result = _engine_run(monkeypatch, output_transform=lambda b: b.strip())

    telemetry = rp.run_telemetry(run)
    assert telemetry["chapters_repaired"] == 1
    assert telemetry["manuscript_changed"] is True

    after = _repaired_snapshot(run, snap)
    assert len(after.blocks) == 3
    for i in (0, 2):
        assert after.block_bytes(i) == snap.block_bytes(i)
    assert BAD.encode("utf-8") not in after.block_bytes(1)


def test_a_three_newline_frame_is_restored_exactly_through_the_engine(monkeypatch):
    """A frame no generic fallback could invent, carried all the way through an
    ACCEPTED engine repair rather than only through the adapter."""
    canon = _canon3()
    book = (f"## Bab 1\nNamanya {GOOD} di desa itu.\n\n\n"
            f"## Bab 2\nNamanya {BAD} di kota itu.\n\n\n"
            f"## Bab 3\nAkhirnya {GOOD} pulang.\n")
    snap = l2.materialize_final_snapshot({"book": book}, canon=canon)
    assert snap.block_bytes(1)[len(snap.block_bytes(1).rstrip()):] == b"\n\n\n"
    claims = {
        i: _claims_for(snap.block_bytes(i), index=i,
                       chapter_id=snap.blocks[i].chapter_id, canon=canon,
                       needle=(BAD if i == 1 else GOOD))
        for i in range(3)
    }
    repaired = snap.block_bytes(1).replace(BAD.encode(), GOOD.encode()).decode("utf-8")

    async def _fake_run_worker(worker, prompt, timeout=None, task_id=None):
        return {"ok": True, "output": repaired.strip()}

    monkeypatch.setattr(st, "run_worker", _fake_run_worker)
    session = ad.L3AssistSession(metered_provider=_ReadingExtractor(),
                                 canon_text="CANON v1\n", worker_model="m1")
    run = asyncio.run(rp.repair_manuscript(
        snap, canon, mode="assist", result={"book": book}, claims_by_index=claims,
        repair_provider=session.repair_provider,
        extract_chapter=session.extract_chapter))

    assert rp.run_telemetry(run)["chapters_repaired"] == 1
    after = _repaired_snapshot(run, snap)
    target = after.block_bytes(1)
    assert target[len(target.rstrip()):] == b"\n\n\n", (
        f"the three-newline frame was not restored exactly: {target[-6:]!r}")
    assert BAD.encode("utf-8") not in target
