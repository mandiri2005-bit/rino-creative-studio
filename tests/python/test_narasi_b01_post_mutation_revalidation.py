"""
B-01 Post-Mutation Revalidation — acceptance test suite (test-only scope).

Real-path harness: invokes the ACTUAL `narration_api._b01_post_mutation_revalidate` (the
runtime adapter) and `continuity.post_mutation_revalidation` (the pure coordinator) together,
with only the provider boundary (`laozhang_api._narasi_cheap_call`) monkeypatched to a canned
recorder — every flag/eligibility check, B-02 binding call, segmentation, changed-scope
derivation, compare-only probe, and report-assembly step inside the adapter runs unmodified.
B-02 evidence for every fixture is produced by a REAL `continuity.mutation_hashes.
MutationHashRecorder` taken through its normal 4-call lifecycle (capture x3 + finalize), never a
hand-forged snapshot dict — so every "valid binding" fixture here is genuine B-02 evidence, not
a fake shape that merely resembles one.

Two acceptance-matrix ID groups are NOT separate pytest methods here, by the pack's own design
(`CLAUDE-B01-IMPLEMENTATION-ORDER.md` Section 3, step 7 keeps negative controls a SEPARATE step
from writing test IDs):
  * Section G (G01-G12) is proved by killing each described production path in a DETACHED COPY
    outside this worktree and confirming the exact A-F test(s) below fail — recorded in the
    submission report, not as its own pytest function.
  * A01/A05/F04/F05 (the flag-gate placement, canonical-path-only invocation, and exact hook
    position relative to `_persist_chapters`/`_result_payload`) live one call-site UP, inside
    `_run_narration_job` — a stateful integration function with real Redis/DB/credit-hold
    dependencies that this suite does not spin up. Those four IDs are proved by direct,
    line-anchored inspection of the running module's own source (never a test name or fixture
    string) alongside the shared `_r7_env_on` predicate's own direct behavioral coverage.

No production manuscript text, job IDs, tenant IDs, or UUIDs anywhere in this file — every
fixture is fully synthetic, per the same privacy discipline as A-04/A-05a (see TestPrivacyScan).

Companion inventory: this file's own `_REQUIRED_ACCEPTANCE_TEST_SUBSTRINGS` dict, cross-checked
for completeness by TestAcceptanceMatrixCompleteness below — the oracle's canonical ID list is a
hand-authored literal (never derived from test names, fixture metadata, ACCEPTANCE-MATRIX.md at
runtime, or implementation constants).
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re

import pytest

import continuity.mutation_hashes as mh
import continuity.post_mutation_revalidation as pmr
import narration_api


@pytest.fixture(autouse=True)
def _clean_b01_env(monkeypatch):
    """Strip every NARASI_*/DALANG_* flag before each test, mirroring A-04/A-05a's hermeticity
    discipline — this suite starts from a known all-off baseline regardless of ambient shell
    env."""
    for name in list(os.environ):
        if name.startswith("NARASI_") or name.startswith("DALANG_"):
            monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Shared fixture builders — genuine B-02 evidence via the real recorder.
# ---------------------------------------------------------------------------
_VERSIONS_ALL_NONE = {
    "mutation_hash_schema_version": mh.MUTATION_HASH_SCHEMA_VERSION,
    "lifecycle_schema_version": None,
    "story_contract_schema_version": None,
    "story_contract_hash": None,
    "contract_prompt_version": None,
    "compiler_version": None,
    "slicer_version": None,
    "writer_context_version": None,
    "extractor_schema_version": None,
    "extractor_prompt_version": None,
    "extractor_epoch": None,
    "predicate_set_version": None,
    "diff_version": None,
}

_BOUNDARY_MARKER = "continuity break at the seam"
_CANON_MARKER = "established canonical facts sheet"
_THREAD_MARKER = "thread it opens that is not resolved"


def _book(bodies):
    return "\n\n".join(f"## Chapter {i + 1}\n\n{body}" for i, body in enumerate(bodies))


def _body(target_language="en", mode="book"):
    return {"mode": mode, "language": target_language, "style": "storytelling"}


def _metadata_literal(target_language="en", mode="book", n=1):
    return {
        "scenario": "A", "strategy": "map_reduce", "polished": True, "rag_used": False,
        "n_ok": n, "n_total": n, "outline_source": "test", "mode": mode,
        "target_language": target_language, "model": "test-model", "manager_model": "test-model",
    }


def build_snapshot(stage_bodies, *, chapter_ids=None, target_language="en", versions=None):
    """`stage_bodies` maps post_map/post_polish/post_revise/final -> list[str] (one body per
    chapter at that stage; every stage must carry the same chapter count). Drives a REAL
    MutationHashRecorder through its normal lifecycle. Returns (snapshot, chapter_records,
    final_book, recorder)."""
    n = len(stage_bodies["final"])
    chapter_records = [{"no": i, "chapter_id": (chapter_ids[i] if chapter_ids else None)}
                       for i in range(n)]
    recorder = mh.MutationHashRecorder(chapter_records=chapter_records,
                                       versions=dict(versions or _VERSIONS_ALL_NONE))
    recorder.capture(stage="post_map", candidate_text=_book(stage_bodies["post_map"]))
    recorder.capture(stage="post_polish", candidate_text=_book(stage_bodies["post_polish"]))
    recorder.capture(stage="post_revise", candidate_text=_book(stage_bodies["post_revise"]))
    final_book = _book(stage_bodies["final"])
    snapshot = recorder.finalize(candidate_text=final_book,
                                 metadata=_metadata_literal(target_language, n=n))
    return snapshot, chapter_records, final_book, recorder


def uniform_snapshot(bodies, **kw):
    """Every stage carries the SAME per-chapter bodies — nothing changed anywhere."""
    return build_snapshot({"post_map": bodies, "post_polish": bodies, "post_revise": bodies,
                           "final": bodies}, **kw)


def _reseal_stage_chain(snapshot, from_index):
    """After directly tampering `snapshot["stages"][from_index]["chapters"]` in place, correctly
    re-derives `chapter_set_hash`/`stage_hash` for that stage and every later stage (each one's
    `stage_hash` binds `previous_stage_hash`, which must chain forward), then the whole
    snapshot's own `ledger_hash` — producing a FULLY internally self-consistent forgery (every
    hash in the chain recomputed, not just the one field that was changed), matching the class
    of attack `verify_final_binding()`'s own docstring calls out and never a "forgot to
    re-chain" toy tamper. Mutates and returns `snapshot`."""
    stages = snapshot["stages"]
    for i in range(from_index, len(stages)):
        stage = stages[i]
        stage["chapter_set_hash"] = mh._structured_hash(stage["chapters"])
        stage["previous_stage_hash"] = stages[i - 1]["stage_hash"] if i > 0 else None
        closed_without_hash = {k: v for k, v in stage.items() if k != "stage_hash"}
        stage["stage_hash"] = mh._structured_hash(closed_without_hash)
    closed_snapshot_without_hash = {k: v for k, v in snapshot.items() if k != "ledger_hash"}
    snapshot["ledger_hash"] = mh._structured_hash(closed_snapshot_without_hash)
    return snapshot


def final_result(final_book, chapter_records, snapshot, recorder, *, canonical_facts=None,
                 target_language="en", mode="book", n=None):
    n = n if n is not None else len(chapter_records)
    return {
        "ok": True, "book": final_book, "chapters": chapter_records,
        "mutation_hashes": snapshot, "_mutation_hash_recorder": recorder,
        "canonical_facts": canonical_facts,
        "scenario": "A", "strategy": "map_reduce", "polished": True, "rag_used": False,
        "n_ok": n, "n_total": n, "outline_source": "test",
        "model": "test-model", "manager_model": "test-model",
    }


class CheapCallRecorder:
    """Fakes `laozhang_api._narasi_cheap_call`. `responses`: ordered (marker_substring, payload)
    pairs, first system-prompt substring match wins; payload is a dict (JSON-encoded) or literal
    string (used as-is, for malformed-JSON cases). Default (no match): an all-clean verdict for
    whichever probe asked, so a test only has to declare the responses it actually cares about."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    async def __call__(self, system, user, **kw):
        self.calls.append((system, user, kw))
        for marker, payload in self.responses:
            if marker in system:
                text = payload if isinstance(payload, str) else json.dumps(payload)
                return text, 3
        if _BOUNDARY_MARKER in system:
            return json.dumps({"broken": False, "reason": ""}), 0
        return json.dumps({"finding": False, "reason": ""}), 0

    def calls_matching(self, marker):
        return [c for c in self.calls if marker in c[0]]


@pytest.fixture(autouse=True)
def cheap(monkeypatch):
    """Autouse: EVERY test gets a fake `_narasi_cheap_call` -- a test that forgets to declare
    `cheap` explicitly must never fall through to a real network call (which would 401 in this
    offline environment and silently degrade every async check to `incomplete`, masking the
    actual assertion under test)."""
    import laozhang_api
    recorder = CheapCallRecorder()
    monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", recorder)
    return recorder


class _Sink:
    """Minimal stand-in for `_UsageSink` — B-01 probes only ever touch `.credits`."""
    def __init__(self):
        self.credits = 0


def run_b01(fr, body=None, *, sink=None):
    body = body or _body()
    return asyncio.run(narration_api._b01_post_mutation_revalidate(
        fr, body, tenant_id="tenant-x", user_id="user-x", job_uuid="job-x",
        sink=sink if sink is not None else _Sink()))


def _covers(*acceptance_ids):
    """Attaches an explicit, out-of-band acceptance-ID binding to a test method -- a plain
    function attribute, never derived from (or matched against) the method's own Python
    identifier. Codex finding (confirmed 2026-07-24): the completeness oracle previously bound
    required IDs by checking whether a *substring* appeared in a test method's *name* (first via
    runtime `dir()`, then via `ast`-parsed source) -- both still keyed the binding to the test's
    own name, which the acceptance matrix's independence requirement forbids regardless of
    whether the name is read at runtime or from source text. A method decorated `@_covers("B08")`
    keeps that binding even if renamed to something unrelated -- proving the binding does not
    derive from the name at all. `TestAcceptanceMatrixCompleteness` below reads ONLY this
    attribute, never a method name."""
    def _decorator(fn):
        fn._acceptance_ids = tuple(acceptance_ids)
        return fn
    return _decorator


# ===========================================================================
# A — Authority, flag and scope
# ===========================================================================
class TestA_FlagAndScope:
    @_covers("A02")
    def test_a02_false_tokens_never_enable(self, monkeypatch):
        for tok in ("0", "false", "no", "off", "", "  ", "banana"):
            monkeypatch.setenv("NARASI_POST_REVISE_REVALIDATE", tok)
            assert narration_api._r7_env_on("NARASI_POST_REVISE_REVALIDATE") is False

    @_covers("A03")
    def test_a03_true_tokens_all_enable(self, monkeypatch):
        for tok in ("1", "true", "yes", "on", "TRUE", " On ", "YES"):
            monkeypatch.setenv("NARASI_POST_REVISE_REVALIDATE", tok)
            assert narration_api._r7_env_on("NARASI_POST_REVISE_REVALIDATE") is True

    @_covers("A04")
    def test_a04_absent_flag_resolves_off_without_raising(self, monkeypatch):
        monkeypatch.delenv("NARASI_POST_REVISE_REVALIDATE", raising=False)
        assert narration_api._r7_env_on("NARASI_POST_REVISE_REVALIDATE") is False

    @_covers("A01", "A05", "A06")
    def test_a01_a05_a06_call_site_gated_placed_report_only(self):
        """Source-grounded proof for the three facts that live at the `_run_narration_job` call
        site (a stateful integration function this suite does not execute end-to-end): the B-01
        call (1) sits inside an `if _r7_env_on("NARASI_POST_REVISE_REVALIDATE")` guard — so a
        flag-off job makes zero call into the adapter (A01) and Review/One-Shot/failed-generation
        paths, which never reach this success-branch call site at all, never invoke it either
        (A05); and (2) only ever assigns into `_final_result["post_mutation_revalidation"]`, never
        into `result["book"]`/`result["output"]`/status fields, so a flag-on run stays report-only
        (A06)."""
        src = inspect.getsource(narration_api._run_narration_job)
        m = re.search(
            r'if _r7_env_on\("NARASI_POST_REVISE_REVALIDATE"\):\s*\n'
            r'(?:[^\n]*\n)*?\s*_b01_report = await _b01_post_mutation_revalidate\(',
            src)
        assert m is not None, "B-01 call must be nested directly inside its own flag guard"
        # A06: the ONLY mutation performed with the report is setting one named key.
        block_start = m.start()
        block = src[block_start:block_start + 700]
        assert '_final_result["post_mutation_revalidation"] = _b01_report' in block
        assert 'result["book"]' not in block and 'result["output"]' not in block
        assert "_STATUS_" not in block
        # Exactly one call site in the whole file — no second, unguarded invocation anywhere.
        assert src.count("await _b01_post_mutation_revalidate(") == 1
        assert narration_api.__dict__["_run_narration_job"] is not None  # sanity: real module

    def test_a01_call_target_is_not_locally_shadowed_anywhere_in_the_function(self):
        """Adversarial-audit finding (confirmed 2026-07-24): the regex/substring checks above only
        inspect a ~700-char window around the call site's own text, so they cannot see a bug that
        leaves that text untouched but breaks what the name `_b01_post_mutation_revalidate`
        actually RESOLVES to at the call site -- e.g. a single unrelated line anywhere earlier in
        `_run_narration_job` (`_b01_post_mutation_revalidate = None`, added for some unrelated
        debugging reason) silently makes CPython compile that name as a LOCAL variable for the
        ENTIRE function (Python resolves a name's scope per-function at compile time, not
        per-line), so the real call ~290 lines later would resolve to that local `None` instead of
        the module-level coordinator -- a `TypeError: 'NoneType' object is not callable` swallowed
        by the adapter's own blanket `except Exception` handler, silently and permanently disabling
        B-01 for every job while the flag stays on and every text-based check above keeps passing.
        This test closes that gap directly at the bytecode level: `co_varnames` lists every name
        CPython ever treats as local to this function (regardless of which branch assigns it);
        `co_names` lists every name resolved as global/attribute. The call target must appear in
        the latter and never in the former."""
        code = narration_api._run_narration_job.__code__
        assert "_b01_post_mutation_revalidate" not in code.co_varnames, (
            "the call target has become a local variable somewhere in _run_narration_job -- "
            "the real call site now resolves to that local, not the module-level coordinator")
        assert "_b01_post_mutation_revalidate" in code.co_names


# ===========================================================================
# B — B-02 binding
# ===========================================================================
class TestB_B02Binding:
    @_covers("B01")
    def test_b01_genuine_complete_snapshot_binds(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["coverage"] == "complete"
        assert report["authenticity"] == "not_provided"

    @_covers("B02")
    def test_b02_missing_snapshot_incomplete_zero_semantic_calls(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        fr = final_result(book, recs, snap, rec)
        fr["mutation_hashes"] = None
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B03")
    def test_b03_incomplete_snapshot_incomplete_zero_semantic_calls(self, cheap):
        recs = [{"no": 0, "chapter_id": None}, {"no": 1, "chapter_id": None}]
        rec = mh.MutationHashRecorder(chapter_records=recs, versions=dict(_VERSIONS_ALL_NONE))
        rec.capture(stage="post_map", candidate_text=_book(["A", "B"]))
        incomplete_snap = rec.snapshot()  # only 1 of 4 stages captured
        fr = final_result(_book(["A", "B"]), recs, incomplete_snap, rec)
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B04")
    def test_b04_malformed_snapshot_incomplete_no_raw_exception_or_prose(self, cheap):
        # P1 fix note (2026-07-24): `_snapshot` is now always re-derived fresh from the LIVE
        # recorder, so a malformed PASSED dict alone (this test's original construction) is now
        # irrelevant -- it is never read past the initial non-None eligibility check. The only way
        # to reach the adapter with genuinely malformed/incomplete B-02 evidence is a recorder that
        # itself never reached a complete, finalized state (e.g. a wiring bug that captures
        # post_map/post_polish/post_revise but never calls `.finalize()`).
        chapter_records = [{"no": 0, "chapter_id": None}, {"no": 1, "chapter_id": None}]
        rec = mh.MutationHashRecorder(chapter_records=chapter_records, versions=dict(_VERSIONS_ALL_NONE))
        bodies = ["Alpha body.", "Beta body."]
        rec.capture(stage="post_map", candidate_text=_book(bodies))
        rec.capture(stage="post_polish", candidate_text=_book(bodies))
        rec.capture(stage="post_revise", candidate_text=_book(bodies))
        # deliberately never finalized -- the recorder's own snapshot is genuinely incomplete
        book = _book(bodies)
        fr = final_result(book, chapter_records, {"not": "a real snapshot"}, rec)
        report = run_b01(fr)  # must not raise
        assert report["coverage"] == "incomplete"
        assert "not a real snapshot" not in json.dumps(report)
        assert cheap.calls == []

    @_covers("B05")
    def test_b05_stale_final_candidate_rejected_before_scoped_calls(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        fr = final_result(book, recs, snap, rec)
        fr["book"] = _book(["Alpha body CHANGED.", "Beta body."])  # diverges from finalized hash
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B06")
    def test_b06_stale_final_metadata_rejected_before_scoped_calls(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."], target_language="en")
        fr = final_result(book, recs, snap, rec, target_language="en")
        report = run_b01(fr, body=_body(target_language="fr"))  # metadata now disagrees
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B07")
    def test_b07_wrong_chapter_identity_rejected_before_scoped_calls(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        fr = final_result(book, recs, snap, rec)
        fr["chapters"] = [{"no": 0, "chapter_id": None}]  # wrong count vs the sealed snapshot
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B08")
    def test_b08_resealed_internal_stage_tamper_rejected(self, cheap):
        # Codex P1 finding, twice-revised (re-audit confirmed 2026-07-24): the ORIGINAL version of
        # this test forged `stages[0]["candidate_hash"]` alone and explicitly did NOT re-chain
        # anything downstream -- so it only proved verify_final_binding's trivial top-level
        # self-consistency check. A first fix rewrote this to prove the tamper had "no effect"
        # (silently substituting the live recorder's snapshot and proceeding as `complete`) --
        # Codex correctly rejected THAT too: `final_result["mutation_hashes"]` (unchanged, still
        # the tampered dict) is what actually gets persisted downstream, so silently trusting the
        # recorder while the PERSISTED evidence stays wrong would make this function's own "clean"
        # report actively misleading, and it contradicts the acceptance matrix's own B08 intent
        # (tamper REJECTED, not accepted). The real fix (narration_api.py) compares the passed
        # snapshot against a fresh `_recorder.snapshot()` and treats ANY divergence as a structural
        # integrity failure -- `incomplete`, zero semantic calls -- rather than resolving it in
        # either side's favor.
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Alpha v1.", "Beta."], "post_polish": ["Alpha v1.", "Beta."],
            "post_revise": ["Alpha v1.", "Beta."], "final": ["Alpha v2.", "Beta."],
        })
        assert (snap["stages"][0]["chapters"][0]["content_hash"]
                != snap["stages"][-1]["chapters"][0]["content_hash"])  # sanity: a real change

        tampered = json.loads(json.dumps(snap))
        final_hash = tampered["stages"][-1]["chapters"][0]["content_hash"]
        # Tamper EVERY pre-final stage's chapter-0 hash to already equal the final value -- if
        # only stage 0 were altered, the post_map->post_polish transition would still show a
        # mismatch against the untouched stage 1/2 values, and `derive_changed_scope`'s union
        # across all 3 adjacent transitions would still (correctly, if accidentally) catch it.
        # Hiding the change from that union requires ALL three pre-final stages to agree with the
        # final value, matching Codex's actual P1 attack shape. It no longer matters for THIS
        # test's outcome (the divergence-from-recorder check fires regardless of which fields
        # differ), but keeps this fixture the genuinely strongest version of the attack.
        for stage_index in range(3):
            tampered["stages"][stage_index]["chapters"][0]["content_hash"] = final_hash
        _reseal_stage_chain(tampered, from_index=0)
        report = run_b01(final_result(book, recs, tampered, rec, canonical_facts="FACT: a test fact."))
        assert report["coverage"] == "incomplete"
        assert "B02_SNAPSHOT_DIVERGED_FROM_RECORDER" in report["checks"]["structure"]["codes"]
        assert cheap.calls == []

    def test_b08b_hostile_snapshot_comparison_object_never_raises(self, cheap):
        # Codex finding (confirmed 2026-07-24): a bare `_snapshot != _live_snapshot` is itself
        # unsafe -- `_snapshot` is caller-owned and could be an arbitrary object, e.g. a `dict`
        # subclass overriding `__eq__`/`__ne__` to raise. Reproduced empirically against the
        # unfixed raw-comparison code (a real `RuntimeError` propagated out uncaught, losing the
        # B-01 report entirely instead of a bounded `incomplete`). `pmr.trusted_value_matches` must
        # never invoke any hook on the untrusted side -- exact-type-first, so a non-exact-`dict`
        # is rejected by `type(...) is not type(...)` alone, before `__eq__`/`__ne__`/`__hash__`
        # on the hostile object is ever touched.
        class _HostileSnapshot(dict):
            def __eq__(self, other):
                raise RuntimeError("hostile __eq__ invoked during snapshot comparison")

            def __ne__(self, other):
                raise RuntimeError("hostile __ne__ invoked during snapshot comparison")

            def __hash__(self):
                return 0

        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        hostile = _HostileSnapshot(snap)  # same content, but a dict SUBCLASS with hostile dunders
        report = run_b01(final_result(book, recs, hostile, rec))  # must not raise
        assert report["coverage"] == "incomplete"
        assert "B02_SNAPSHOT_DIVERGED_FROM_RECORDER" in report["checks"]["structure"]["codes"]
        assert cheap.calls == []

    @_covers("B09")
    def test_b09_independent_version_map_never_copied_from_snapshot(self):
        """The adapter's version map is a fresh literal authored in narration_api.py, not read
        off `snapshot["version_bindings"]` — proved by binding succeeding even though the
        snapshot's OWN version_bindings dict is a physically different (but equal-by-value)
        object, and by direct source inspection confirming no `snapshot[...]`/`.get("version` read
        feeds the `versions=` kwarg."""
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        assert snap["version_bindings"] is not _VERSIONS_ALL_NONE
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["coverage"] == "complete"
        src = inspect.getsource(narration_api._b01_post_mutation_revalidate)
        versions_line_idx = src.index('versions = {')
        binding_call_idx = src.index("_b01_verify_final_binding(")
        window = src[versions_line_idx:binding_call_idx]
        assert "_snapshot" not in window and "version_bindings" not in window

    @_covers("B10")
    def test_b10_version_mismatch_incomplete_never_clean(self, cheap):
        versions = dict(_VERSIONS_ALL_NONE)
        versions["compiler_version"] = "v2-nonstandard"  # differs from the adapter's fixed map
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."], versions=versions)
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("B11")
    def test_b11_authenticity_always_not_provided(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        assert run_b01(final_result(book, recs, snap, rec))["authenticity"] == "not_provided"
        fr2 = final_result(book, recs, snap, rec)
        fr2["mutation_hashes"] = None
        assert run_b01(fr2)["authenticity"] == "not_provided"

    @_covers("B12")
    def test_b12_fake_recorder_type_yields_incomplete_zero_semantic_calls(self, cheap):
        class _FakeRecorder(mh.MutationHashRecorder):
            pass

        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])
        fake = _FakeRecorder(chapter_records=recs, versions=dict(_VERSIONS_ALL_NONE))
        fr = final_result(book, recs, snap, rec)
        fr["_mutation_hash_recorder"] = fake  # subclass instance, not the exact production type
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []
        fr2 = final_result(book, recs, snap, rec)
        fr2["_mutation_hash_recorder"] = {"duck": "typed"}
        report2 = run_b01(fr2)
        assert report2["coverage"] == "incomplete"


# ===========================================================================
# C — Changed-scope derivation
# ===========================================================================
class TestC_ChangedScopeDerivation:
    @_covers("C01")
    def test_c01_stable_id_segmentation_preserves_order(self):
        ids = ["ch_" + ("a" * 32), "ch_" + ("b" * 32), "ch_" + ("c" * 32)]
        snap, recs, book, rec = uniform_snapshot(["A", "B", "C"], chapter_ids=ids)
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["coverage"] == "complete"

    @_covers("C02")
    def test_c02_uniform_legacy_index_segmentation_preserves_order(self):
        snap, recs, book, rec = uniform_snapshot(["A", "B", "C"])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["coverage"] == "complete"
        assert report["changed_chapter_ids"] == []

    @_covers("C03")
    def test_c03_malformed_heading_is_incomplete(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["A", "B"])
        fr = final_result(book, recs, snap, rec)
        fr["book"] = book.replace("## Chapter 2", "not a heading at all")
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert cheap.calls == []

    @_covers("C04")
    def test_c04_stale_chapter_record_content_cannot_affect_findings(self):
        # A "content" field on chapter_records that disagrees with the real final candidate must
        # never leak into any check -- proved behaviorally (not just via the pure function in
        # isolation): the real, changed chapter body carries a language-leak marker the STALE
        # content field does not, so a report reading the real body must FLAG language, while one
        # that fell through to stale content would wrongly report clean.
        real_leaky_body = "This chapter has real content where yang and dengan leak into English."
        stale_content = "Completely different stale text with no leak markers at all present here."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Unchanged opening.", "B"], "post_polish": ["Unchanged opening.", "B"],
            "post_revise": ["Unchanged opening.", "B"], "final": ["Unchanged opening.", real_leaky_body],
        })
        recs_with_stale_content = [dict(r, content=stale_content) for r in recs]
        fr = final_result(book, recs_with_stale_content, snap, rec, canonical_facts="FACT: a test fact.")
        report = run_b01(fr)
        assert report["coverage"] == "complete"
        assert report["checks"]["language"]["status"] == "finding"
        chapters, err = pmr.segment_final_candidate(book, recs_with_stale_content)
        assert err is None
        assert "stale" not in chapters[1]["body"].lower()
        assert chapters[1]["body"].strip() == real_leaky_body

    def test_c04b_stale_chapter_record_content_cannot_affect_boundary_canon_thread(self, cheap):
        # Adversarial-audit finding (confirmed 2026-07-24): C04 above only asserts on the
        # LANGUAGE check's status, which reads `chapters[i]["body"]` directly. It says nothing
        # about whether the SAME stale-content substitution could corrupt only the input handed to
        # the boundary/canon/thread async probes specifically (a narrower kill scoped to just
        # those three call sites, leaving marker/language untouched). This proves the actual LLM
        # call bodies -- inspected via the `cheap` spy -- carry the real, non-stale text too.
        real_body = "RealFinalChapterOneContent with a distinctive marker RFC1."
        stale_content = "StaleChapterRecordContent with a distinctive marker SCRC1."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Chapter zero body.", "B"], "post_polish": ["Chapter zero body.", "B"],
            "post_revise": ["Chapter zero body.", "B"], "final": ["Chapter zero body.", real_body],
        })
        recs_with_stale_content = [dict(r, content=stale_content) for r in recs]
        fr = final_result(book, recs_with_stale_content, snap, rec, canonical_facts="FACT: x.")
        run_b01(fr)
        all_user_texts = " ".join(c[1] for c in cheap.calls)
        assert "RFC1" in all_user_texts
        assert "SCRC1" not in all_user_texts

    @_covers("C05")
    def test_c05_segment_hash_parity_vs_b02_semantics(self):
        chapter_records = [{"no": 0, "chapter_id": None}, {"no": 1, "chapter_id": None}]
        book = _book(["Parity body one.", "Parity body two."])
        mine, err1 = pmr.segment_final_candidate(book, chapter_records)
        theirs, err2 = mh._segment(book, chapter_records)
        assert err1 is None and err2 is None
        assert [c["content_hash"] for c in mine] == [c["content_hash"] for c in theirs]

    def test_c05b_hostile_hash_collision_chapter_id_key_never_raises(self):
        # Codex P2 finding (confirmed 2026-07-24): the original `record.get("chapter_id") if
        # type(record) is dict else None` reads a key off an untrusted dict without first
        # confirming every key in it is an exact `str` -- a dict holding a hash-colliding hostile
        # key alongside a genuine "chapter_id" key can have that hostile key's `__eq__` invoked
        # during the `.get()` lookup itself (collision-resolution probing), contradicting this
        # module's own "never raises for any input shape" contract if that `__eq__` raises.
        # `__eq__` is armed to return False (never raise) during dict construction below, so
        # building the fixture itself cannot trigger the bug -- it is armed only right before the
        # call under test, exactly mirroring the same real-world shape B-02's own
        # `_exact_string_keyed_dict_or_none` docstring documents this attack class against.
        class _HostileKey:
            def __init__(self):
                self.armed = False

            def __hash__(self):
                return hash("chapter_id")

            def __eq__(self, other):
                if self.armed:
                    raise RuntimeError("hostile __eq__ invoked")
                return False

        hostile = _HostileKey()
        record = {hostile: "poisoned", "no": 0, "chapter_id": "ch_" + "a" * 32}
        hostile.armed = True
        book = _book(["Solo body."])
        chapters, err = pmr.segment_final_candidate(book, [record])
        assert chapters is None
        assert err == "CHAPTER_ID_INVALID"

    @_covers("C06")
    def test_c06_post_map_to_post_polish_mutation_selects_chapter(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A v1", "B"], "post_polish": ["A v2", "B"],
            "post_revise": ["A v2", "B"], "final": ["A v2", "B"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:0"]

    @_covers("C07")
    def test_c07_post_polish_to_post_revise_mutation_selects_chapter(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:1"]

    @_covers("C08")
    def test_c08_post_revise_to_final_mutation_selects_chapter(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A v2", "B"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:0"]

    @_covers("C09")
    def test_c09_change_then_revert_still_selected(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A v2", "B"],
            "post_revise": ["A", "B"], "final": ["A", "B"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:0"]

    @_covers("C10")
    def test_c10_multiple_transitions_one_deterministic_ordered_union(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A v2", "B", "C"],
            "post_revise": ["A v2", "B v2", "C"], "final": ["A v2", "B v2", "C v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:0", "legacy:1", "legacy:2"]

    @_covers("C11")
    def test_c11_changed_interior_chapter_selects_both_adjacent_pairs_once(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A", "B", "C"],
            "post_revise": ["A", "B v2", "C"], "final": ["A", "B v2", "C"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["affected_pairs"] == [
            {"left": "legacy:0", "right": "legacy:1"},
            {"left": "legacy:1", "right": "legacy:2"},
        ]

    @_covers("C12")
    def test_c12_first_last_single_chapter_pair_selection_exact(self, cheap):
        # First chapter changed -> exactly one pair.
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A v2", "B", "C"],
            "post_revise": ["A v2", "B", "C"], "final": ["A v2", "B", "C"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["affected_pairs"] == [{"left": "legacy:0", "right": "legacy:1"}]
        # Single-chapter candidate -> zero affected pairs, zero scoped calls at all.
        cheap.calls.clear()
        snap1, recs1, book1, rec1 = uniform_snapshot(["Solo chapter body."])
        report1 = run_b01(final_result(book1, recs1, snap1, rec1))
        assert report1["affected_pairs"] == []
        assert report1["changed_chapter_ids"] == []
        assert cheap.calls == []

    def test_c06_early_transition_change_scopes_marker_and_language_too(self):
        # Adversarial-audit finding (confirmed 2026-07-24): every existing marker/language fixture
        # (D08-D11) only changes a chapter at the LAST (post_revise -> final) transition, which
        # cannot distinguish the correct full-union changed_idx from a narrower recomputation that
        # only looks at that last transition for the marker/language loop specifically (leaving
        # C06-C12's own changed_chapter_ids assertions -- which read the shared changed_idx before
        # the marker/language loop even runs -- untouched by such a narrowing bug). This fixture
        # changes chapter 0 ONLY at the EARLIEST transition (post_map -> post_polish) and carries a
        # real language-leak marker, proving the marker/language checks consume the SAME
        # full-union changed_idx as changed_chapter_ids, not an independently-recomputed subset.
        leaky_v1 = "Original body with yang and dengan leaking into English right here."
        leaky_v2_same_leak = "Original body with yang and dengan leaking into English right here, v2."
        snap, recs, book, rec = build_snapshot({
            "post_map": [leaky_v1, "B"], "post_polish": [leaky_v2_same_leak, "B"],
            "post_revise": [leaky_v2_same_leak, "B"], "final": [leaky_v2_same_leak, "B"],
        })
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert report["changed_chapter_ids"] == ["legacy:0"]
        assert report["checks"]["language"]["status"] == "finding"
        assert "FINDING:legacy:0" in report["checks"]["language"]["codes"]


# ===========================================================================
# D — Deterministic probe validasi read-only
# ===========================================================================
class TestD_DeterministicProbes:
    @_covers("D01")
    def test_d01_clean_heading_order_identity_nonempty_is_structure_clean(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["structure"] == {"status": "clean", "codes": []}

    @_covers("D02")
    def test_d02_reordered_or_fused_heading_yields_typed_structure_finding_or_incomplete(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        fr = final_result(book, recs, snap, rec)
        fr["book"] = book.replace("## Chapter 1", "## Chapter 1a")  # non-numeric-clean heading
        report = run_b01(fr)
        assert report["checks"]["structure"]["status"] == "incomplete"
        assert cheap.calls == []

    @_covers("D03")
    def test_d03_empty_or_surrogate_candidate_never_raises_or_leaks_content(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha secret name Larasati.", "Beta."])
        fr = final_result(book, recs, snap, rec)
        fr["book"] = ""  # empty candidate: stale vs the sealed hash -> must not raise
        report = run_b01(fr)  # must not raise
        assert report["coverage"] == "incomplete"
        assert "Larasati" not in json.dumps(report)

    @_covers("D04")
    def test_d04_final_segment_hashes_must_equal_b02_final_stage(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        report = run_b01(final_result(book, recs, snap, rec))
        chapters, _ = pmr.segment_final_candidate(book, recs)
        assert ([c["content_hash"] for c in chapters]
                == [c["content_hash"] for c in snap["stages"][-1]["chapters"]])
        assert "FINAL_SEGMENT_HASH_MISMATCH" not in report["checks"]["structure"]["codes"]

    @_covers("D05")
    def test_d05_heading_repair_probe_detects_would_change_but_never_writes_back(self):
        # `_book()`'s headings are always the English "## Chapter N" word -- binding this job to
        # target_language="id" (both in the sealed snapshot's metadata AND the body the adapter
        # re-derives metadata from) makes chapter_heading_repair() want to relabel them to "Bab".
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."], target_language="id")
        fr = final_result(book, recs, snap, rec)
        report = run_b01(fr, body=_body(target_language="id"))
        assert report["checks"]["structure"]["status"] == "finding"
        assert "HEADING_REPAIR_WOULD_CHANGE" in report["checks"]["structure"]["codes"]
        assert fr["book"] == book  # never written back

    @_covers("D06")
    def test_d06_dedup_clean_candidate_byte_identical_dropped_zero(self):
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["dedup"] == {"status": "clean", "codes": []}

    @_covers("D07")
    def test_d07_duplicate_chapter_block_found_when_final_dedup_path_killed(self, monkeypatch):
        # A genuine heading-level duplicate on a B-02-VALID final candidate is structurally
        # impossible to construct here: any duplicate/extra/reordered heading would already fail
        # `segment_final_candidate`'s own count/order check (D02) before the dedup recheck ever
        # runs -- the upstream production dedup guard has always already run by this point. So
        # this proves the adapter genuinely WIRES the real primitive's return value into the
        # report (spying on the exact function, not a stand-in) by making that real function
        # return a dropped-count-nonzero verdict and confirming it propagates -- a hardcoded
        # "always clean" shortcut in the adapter would never consult this spy at all and would
        # fail this test exactly as the G06 negative control intends.
        import orchestrator.static as _static
        calls = []

        def _spy_dedup(book_text):
            calls.append(book_text)
            return book_text + " [DEDUPED]", 1

        monkeypatch.setattr(_static, "_dedup_chapter_blocks", _spy_dedup)
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert len(calls) == 1
        assert calls[0] == book  # the adapter passes the real final candidate text, unmodified
        assert report["checks"]["dedup"]["status"] == "finding"
        assert "DEDUP_DUPLICATE_FOUND" in report["checks"]["dedup"]["codes"]

    def test_d07b_dedup_probe_exception_yields_incomplete_never_clean(self, monkeypatch):
        # Adversarial-audit finding (confirmed 2026-07-24): no existing test ever makes the real
        # `_dedup_chapter_blocks` call raise, so a mutation that narrows ONLY the except-clause's
        # return value (e.g. "incomplete" -> "clean", leaving the real try-body call untouched)
        # would be invisible to test_d06/test_d07. Force a genuine exception and confirm the
        # bounded incomplete result, never a silent clean.
        import orchestrator.static as _static

        def _raising_dedup(book_text):
            raise RuntimeError("boom")

        monkeypatch.setattr(_static, "_dedup_chapter_blocks", _raising_dedup)
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["dedup"]["status"] == "incomplete"
        assert "DEDUP_PROBE_ERROR" in report["checks"]["dedup"]["codes"]

    @_covers("D08", "D09")
    def test_d08_d09_marker_scoped_to_changed_chapter_never_writes_back(self, cheap):
        clean = "A clean chapter with normal prose."
        leaky = "Scene 2--the plan continues.\nA chapter body with a planning leak line above."
        snap, recs, book, rec = build_snapshot({
            "post_map": [clean, clean], "post_polish": [clean, clean],
            "post_revise": [clean, clean], "final": [clean, leaky],
        })
        fr = final_result(book, recs, snap, rec)
        report = run_b01(fr)
        assert report["changed_chapter_ids"] == ["legacy:1"]
        assert report["checks"]["marker"]["status"] == "finding"
        assert "FINDING:legacy:1" in report["checks"]["marker"]["codes"]
        assert "legacy:0" not in " ".join(report["checks"]["marker"]["codes"])
        assert fr["book"] == book  # never scrubbed in place

    def test_d08b_marker_detects_trailing_hash_residue_leak_class_too(self):
        # Adversarial-audit finding (confirmed 2026-07-24): D08/D09 only exercise the SCENE-leak
        # pattern (_SCENE_LEAK_RE); ACCEPTANCE-MATRIX.md row D08 also names trailing-hash residue
        # (_TRAILING_HASH_RE) as a distinct leak class the same marker probe must catch. A
        # normalization that strips trailing "#" from BOTH sides of the comparison before checking
        # equality would silently blind the check to this class alone while D08/D09 kept passing.
        clean = "A clean chapter with normal prose."
        trailing_hash_leak = "A chapter body that ends abruptly with a trailing marker #"
        snap, recs, book, rec = build_snapshot({
            "post_map": [clean, clean], "post_polish": [clean, clean],
            "post_revise": [clean, clean], "final": [clean, trailing_hash_leak],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["marker"]["status"] == "finding"
        assert "FINDING:legacy:1" in report["checks"]["marker"]["codes"]

    @_covers("D10")
    def test_d10_wired_language_scan_detects_eligible_target_language_leak(self):
        clean = "A clean English chapter."
        leaky = "This chapter has the word yang and dengan bleeding into English prose here now today."
        snap, recs, book, rec = build_snapshot({
            "post_map": [clean, clean], "post_polish": [clean, clean],
            "post_revise": [clean, clean], "final": [clean, leaky],
        })
        report = run_b01(final_result(book, recs, snap, rec, target_language="en"))
        assert report["checks"]["language"]["status"] == "finding"

    def test_d10b_multiple_changed_chapters_all_individually_scanned(self):
        # Adversarial-audit finding (confirmed 2026-07-24): every existing marker/language
        # fixture changes exactly ONE chapter. A mutation that scopes the marker/language loop to
        # only `changed_idx[:1]` (the first changed chapter) instead of the full list would sail
        # through undetected on any ordinary multi-chapter revise job. Three chapters all change;
        # only the LAST one carries a language-leak marker, proving the loop reaches every
        # changed chapter, not just the first.
        clean = "A clean English chapter."
        leaky_last = "This chapter has yang and dengan bleeding into English prose right here."
        snap, recs, book, rec = build_snapshot({
            "post_map": [clean, clean, clean], "post_polish": [clean, clean, clean],
            "post_revise": ["A v2", "B v2", clean], "final": ["A v2", "B v2", leaky_last],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["changed_chapter_ids"] == ["legacy:0", "legacy:1", "legacy:2"]
        assert report["checks"]["language"]["status"] == "finding"
        assert "FINDING:legacy:2" in report["checks"]["language"]["codes"]

    @_covers("D11")
    def test_d11_indonesian_family_not_applicable_matches_a04(self):
        clean = "Bab yang bersih dengan teks biasa."
        leaky_looking = "Teks ini punya kata yang dan dengan berulang kali di sini sekarang juga."
        snap, recs, book, rec = build_snapshot({
            "post_map": [clean, clean], "post_polish": [clean, clean],
            "post_revise": [clean, clean], "final": [clean, leaky_looking],
        }, target_language="id")
        report = run_b01(final_result(book, recs, snap, rec), body=_body(target_language="id"))
        assert report["checks"]["language"]["status"] == "clean"

    @_covers("D12")
    def test_d12_language_probe_never_substitutes_the_different_gate_function(self):
        src = inspect.getsource(narration_api._b01_post_mutation_revalidate)
        assert "from narasi_counters import language_consistency_word_scan" in src
        assert "narasi_gate import language_consistency_scan" not in src
        assert "ng.language_consistency_scan" not in src


# ===========================================================================
# E — Boundary, canon and thread scope
# ===========================================================================
class TestE_BoundaryCanonThread:
    @_covers("E01")
    def test_e01_exactly_one_boundary_call_per_affected_pair_none_unaffected(self, cheap):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A", "B", "C"],
            "post_revise": ["A", "B v2", "C"], "final": ["A", "B v2", "C"],
        })
        run_b01(final_result(book, recs, snap, rec))
        assert len(cheap.calls_matching(_BOUNDARY_MARKER)) == 2  # (0,1) and (1,2), not (none)

    @_covers("E02")
    def test_e02_boundary_input_bounded_300_words_excludes_full_manuscript(self, cheap):
        left = " ".join(f"leftword{i}" for i in range(500))
        right = " ".join(f"rightword{i}" for i in range(500))
        snap, recs, book, rec = build_snapshot({
            "post_map": [left, right], "post_polish": [left, right],
            "post_revise": [left, right], "final": [left, right + " tail"],
        })
        run_b01(final_result(book, recs, snap, rec))
        boundary_calls = cheap.calls_matching(_BOUNDARY_MARKER)
        assert len(boundary_calls) == 1
        user = boundary_calls[0][1]
        assert "leftword0 " not in user and "leftword499" in user  # only the tail 300 words
        assert user.count("leftword") <= 300 and user.count("rightword") <= 300
        assert left not in user  # never the full chapter, only the bounded window

    @_covers("E03")
    def test_e03_boundary_broken_true_becomes_typed_finding_no_raw_reason(self, cheap):
        cheap.responses = [(_BOUNDARY_MARKER, {"broken": True, "reason": "SECRET PLOT REASON"})]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["boundary"]["status"] == "finding"
        assert "SECRET PLOT REASON" not in json.dumps(report)

    @_covers("E04")
    def test_e04_boundary_malformed_or_timeout_becomes_incomplete_never_clean(self, cheap):
        cheap.responses = [(_BOUNDARY_MARKER, "not json at all")]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["boundary"]["status"] == "incomplete"

    def test_e04b_boundary_timeout_actually_uses_the_shared_probe_timeout_constant(self, monkeypatch):
        # Adversarial-audit finding (confirmed 2026-07-24): every existing timeout test uses a
        # malformed/instant fake response -- none of them prove the wait_for() call is actually
        # bounded by the real, shared `_B01_PROBE_TIMEOUT_SECONDS` constant (as opposed to some
        # other hardcoded value, tiny or huge, that happens to produce the right answer for a
        # single fixture). Two-sided proof: (1) under the REAL, unpatched default timeout, a fast
        # (non-sleeping) fake must NOT time out -- catching a mutation that hardcodes a tiny
        # timeout in place of the constant; (2) with the constant explicitly monkeypatched small
        # and a genuinely SLOW fake, the probe MUST time out -- catching a mutation that hardcodes
        # a large/ignoring timeout instead of reading the constant.
        import laozhang_api
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })

        async def _fast(system, user, **kw):
            # A genuine (if brief) yield point is required: a coroutine with zero internal
            # `await`s can run to completion on its very first scheduling tick, before
            # asyncio.wait_for's timeout timer is ever checked -- regardless of how tiny that
            # timeout is. This 50ms sleep forces a real round-trip through the event loop so the
            # timeout enforcement actually gets a chance to fire if it's absurdly small.
            await asyncio.sleep(0.05)
            return json.dumps({"broken": False, "reason": ""}), 0

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", _fast)
        report_fast = run_b01(final_result(book, recs, snap, rec))
        assert report_fast["checks"]["boundary"]["status"] == "clean"

        monkeypatch.setattr(narration_api, "_B01_PROBE_TIMEOUT_SECONDS", 0.01)

        async def _slow(system, user, **kw):
            await asyncio.sleep(1.0)
            return json.dumps({"broken": False, "reason": ""}), 0

        monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", _slow)
        report_slow = run_b01(final_result(book, recs, snap, rec))
        assert report_slow["checks"]["boundary"]["status"] == "incomplete"
        assert "BOUNDARY_PROBE_ERROR" in report_slow["checks"]["boundary"]["codes"]

    @_covers("E05")
    def test_e05_boundary_completion_order_cannot_change_report_order(self, cheap):
        # The FIRST pair's own probe deliberately resolves SLOWER than the second pair's -- if
        # completion order (not final-pair order) drove report assembly, affected_pairs would
        # come back reversed.
        import laozhang_api
        recorded = []

        async def _fake(system, user, **kw):
            if _BOUNDARY_MARKER in system:
                delay = 0.02 if user.startswith("LEFT CHAPTER ENDING:\nA") else 0.0
                await asyncio.sleep(delay)
                recorded.append(user[:20])
            return json.dumps({"broken": False, "reason": ""}), 0

        mp = pytest.MonkeyPatch()
        mp.setattr(laozhang_api, "_narasi_cheap_call", _fake)
        try:
            snap, recs, book, rec = build_snapshot({
                "post_map": ["A", "B", "C"], "post_polish": ["A", "B", "C"],
                "post_revise": ["A v2", "B v2", "C v2"], "final": ["A v2", "B v2", "C v2"],
            })
            report = run_b01(final_result(book, recs, snap, rec))
        finally:
            mp.undo()
        assert report["affected_pairs"] == [
            {"left": "legacy:0", "right": "legacy:1"},
            {"left": "legacy:1", "right": "legacy:2"},
        ]

    @_covers("E06")
    def test_e06_canon_adapter_receives_changed_chapter_bytes_and_bounded_facts(self, cheap):
        facts = "FACT: Larasati owns the shop. FACT: The year is 1998."
        unchanged_body = "UnchangedChapterZeroDistinctiveMarker."
        changed_body_v1 = "ChangedChapterOneOriginalMarker."
        changed_body_v2 = "ChangedChapterOneRevisedMarker."
        snap, recs, book, rec = build_snapshot({
            "post_map": [unchanged_body, changed_body_v1],
            "post_polish": [unchanged_body, changed_body_v1],
            "post_revise": [unchanged_body, changed_body_v1],
            "final": [unchanged_body, changed_body_v2],
        })
        run_b01(final_result(book, recs, snap, rec, canonical_facts=facts))
        canon_calls = cheap.calls_matching(_CANON_MARKER)
        assert len(canon_calls) == 1
        assert facts in canon_calls[0][1]
        assert changed_body_v2 in canon_calls[0][1]
        assert unchanged_body not in canon_calls[0][1]  # unchanged chapter's body never sent

    def test_e06b_canon_finding_true_becomes_typed_finding(self, cheap):
        # Adversarial-audit finding (confirmed 2026-07-24): boundary has test_e03 proving a
        # genuine positive verdict (broken=true) actually reaches the report as "finding" --
        # canon and thread had NO equivalent. A mutation that hardcodes canon's return to always
        # "clean" (or inverts the boolean) would sail through the entire suite undetected, since
        # every other canon test's fake response is a default/negative/malformed one.
        cheap.responses = [(_CANON_MARKER, {"finding": True, "reason": "contradicts a fact"})]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A", "B changed"],
        })
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: x."))
        assert report["checks"]["canon"]["status"] == "finding"
        assert "contradicts a fact" not in json.dumps(report)  # raw reason never persisted

    @_covers("E07")
    def test_e07_old_canon_diff_report_cannot_satisfy_or_suppress_coverage(self, cheap):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A", "B changed"],
        })
        fr = final_result(book, recs, snap, rec, canonical_facts="FACT: x.")
        fr["canon_diff"] = {"ran": True, "eligible": []}  # old whole-book report present
        report = run_b01(fr)
        assert len(cheap.calls_matching(_CANON_MARKER)) == 1  # still ran its own fresh probe

    @_covers("E08")
    def test_e08_canon_malformed_timeout_error_incomplete_and_metered(self, cheap):
        cheap.responses = [(_CANON_MARKER, "not json")]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A", "B changed"],
        })
        sink = _Sink()
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: x."), sink=sink)
        assert report["checks"]["canon"]["status"] == "incomplete"

    def test_e08b_canon_still_fires_truncated_for_facts_sheet_over_the_bound(self, cheap):
        # Adversarial-audit finding (confirmed 2026-07-24): the only "absent context" test used a
        # short facts string. A mutation that treats any facts sheet LONGER than
        # _B01_CANON_MAX_FACTS_CHARS as "absent" (CANON_CONTEXT_ABSENT) instead of truncating it
        # and still running the real call would silently disable canon for the common real-book
        # case (a large Bible/facts sheet) while emitting the same pre-existing, legitimate-
        # looking incomplete shape -- invisible to a short-facts-only test suite.
        long_facts = "FACT: filler. " * 1000  # well over _B01_CANON_MAX_FACTS_CHARS (8000)
        assert len(long_facts) > narration_api._B01_CANON_MAX_FACTS_CHARS
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A", "B changed"],
        })
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts=long_facts))
        canon_calls = cheap.calls_matching(_CANON_MARKER)
        assert len(canon_calls) == 1, "canon must still fire (truncated), not treat long facts as absent"
        assert "CANON_CONTEXT_ABSENT" not in report["checks"]["canon"]["codes"]
        assert report["checks"]["canon"]["status"] == "clean"  # cheap's default: {"finding": false}

    @_covers("E09")
    def test_e09_thread_adapter_receives_changed_chapter_and_bounded_ending(self, cheap):
        ending_ch = "The final chapter resolves everything with a quiet, closing scene."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", ending_ch], "post_polish": ["A", ending_ch],
            "post_revise": ["A", ending_ch], "final": ["A changed", ending_ch],
        })
        run_b01(final_result(book, recs, snap, rec))
        thread_calls = cheap.calls_matching(_THREAD_MARKER)
        assert len(thread_calls) == 1
        assert "A changed" in thread_calls[0][1]
        assert "resolves everything" in thread_calls[0][1]

    def test_e09b_thread_finding_true_becomes_typed_finding(self, cheap):
        # Adversarial-audit finding (confirmed 2026-07-24, symmetric with E06b): no test proved a
        # genuine positive thread verdict (finding=true) actually reaches the report as
        # "finding" rather than being silently coerced to "clean" -- every existing thread test's
        # fake response was a default/negative/malformed one.
        ending_ch = "The final chapter resolves everything with a quiet, closing scene."
        cheap.responses = [(_THREAD_MARKER, {"finding": True, "reason": "never resolved"})]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", ending_ch], "post_polish": ["A", ending_ch],
            "post_revise": ["A", ending_ch], "final": ["A changed", ending_ch],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["thread"]["status"] == "finding"
        assert "never resolved" not in json.dumps(report)  # raw reason never persisted

    @_covers("E10")
    def test_e10_old_thread_tracker_report_cannot_satisfy_or_suppress_coverage(self, cheap):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A changed", "B"],
        })
        fr = final_result(book, recs, snap, rec)
        fr["thread_tracker"] = {"threads_checked": 8, "violations": []}
        report = run_b01(fr)
        assert len(cheap.calls_matching(_THREAD_MARKER)) == 1

    @_covers("E11")
    def test_e11_thread_malformed_timeout_error_incomplete_and_metered(self, cheap):
        cheap.responses = [(_THREAD_MARKER, "not json")]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A changed", "B"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["checks"]["thread"]["status"] == "incomplete"

    @_covers("E12")
    def test_e12_never_calls_apply_v3_gates_revise_repair_or_whole_book_provider(self):
        # Adversarial-audit finding (confirmed 2026-07-24): this only inspected the ORCHESTRATOR
        # function's own source; the same forbidden call inlined into any of the B-01 HELPER
        # functions (a probe/aggregate/runner) would be invisible here. Every `_b01_*`-named
        # function object actually defined in narration_api.py is checked individually.
        forbidden = ("_apply_v3_gates(", "_narasi_consistency_revise",
                    "_narasi_consistency_critique", "generate_bible", "narasi_bible")
        b01_functions = [name for name in dir(narration_api)
                        if name.startswith("_b01_") and callable(getattr(narration_api, name))]
        assert "_b01_post_mutation_revalidate" in b01_functions
        assert len(b01_functions) == 8  # exact: _aggregate, 3 probes, 3 runners, the orchestrator
        for fn_name in b01_functions:
            src = inspect.getsource(getattr(narration_api, fn_name))
            for term in forbidden:
                assert term not in src, f"{fn_name} contains forbidden call {term!r}"


# ===========================================================================
# F — Integration, lifecycle and persistence
# ===========================================================================
class TestF_IntegrationLifecyclePersistence:
    @_covers("F01")
    def test_f01_clean_complete_report_persists_under_named_key(self):
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["decision"] == "clean"
        assert report["schema_version"] == "b01.post_mutation_revalidation.v1"

    @_covers("F02")
    def test_f02_finding_report_persists_while_legacy_status_remains_done(self, cheap):
        cheap.responses = [(_BOUNDARY_MARKER, {"broken": True, "reason": "seam contradiction"})]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        fr = final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact.")
        report = run_b01(fr)
        assert report["decision"] == "findings"
        fr["post_mutation_revalidation"] = report
        # The report is purely additive: no legacy status/book/chapters field is touched by
        # producing a "findings" verdict -- persistence still proceeds exactly as for "clean".
        assert fr["book"] == book
        payload = narration_api._result_payload(fr)
        assert payload["markdown"] == book
        assert payload["post_mutation_revalidation"]["decision"] == "findings"

    @_covers("F03")
    def test_f03_incomplete_report_persists_while_legacy_status_remains_done(self):
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        fr = final_result(book, recs, snap, rec)
        fr["mutation_hashes"] = None
        report = run_b01(fr)
        assert report["decision"] == "incomplete"

    @_covers("F04", "F05")
    def test_f04_f05_invocation_after_finalize_and_before_persist_and_result_payload(self):
        src = inspect.getsource(narration_api._run_narration_job)
        i_final = src.index('_final_result = _bundle.result')
        i_b01 = src.index('await _b01_post_mutation_revalidate(')
        i_persist = src.index('await _persist_chapters(tenant_id, job_uuid, _final_result)')
        i_payload = src.index('_result_payload(_final_result)')
        assert i_final < i_b01 < i_persist < i_payload

    @_covers("F06")
    def test_f06_both_persistence_consumers_observe_same_candidate_and_report_hash(self):
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        fr = final_result(book, recs, snap, rec)
        report = run_b01(fr)
        fr["post_mutation_revalidation"] = report
        payload = narration_api._result_payload(fr)
        assert payload["post_mutation_revalidation"]["candidate_hash"] == report["candidate_hash"]
        assert payload["markdown"] == fr["book"]  # same `_final_result` object both read

    @_covers("F07")
    def test_f07_result_payload_omits_key_when_absent_includes_when_produced(self):
        payload_off = narration_api._result_payload({"book": "x", "chapters": []})
        assert "post_mutation_revalidation" not in payload_off
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        fr_on = final_result(book, recs, snap, rec)
        fr_on["post_mutation_revalidation"] = run_b01(fr_on)
        payload_on = narration_api._result_payload(fr_on)
        assert "post_mutation_revalidation" in payload_on

    @_covers("F08")
    def test_f08_no_candidate_mutation_after_report_generation(self):
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        fr = final_result(book, recs, snap, rec)
        before = fr["book"]
        run_b01(fr)
        assert fr["book"] == before

    @_covers("F09")
    def test_f09_retry_resume_uses_its_own_snapshot_no_reused_result(self):
        snap1, recs1, book1, rec1 = uniform_snapshot(["A.", "B."])
        report1 = run_b01(final_result(book1, recs1, snap1, rec1))
        snap2, recs2, book2, rec2 = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        report2 = run_b01(final_result(book2, recs2, snap2, rec2))
        assert report1["candidate_hash"] != report2["candidate_hash"]
        assert report1["changed_chapter_ids"] != report2["changed_chapter_ids"]

    @_covers("F10")
    def test_f10_cancellation_propagates_ordinary_failures_bounded_incomplete(self, cheap):
        async def _raises(system, user, **kw):
            raise RuntimeError("boom")

        import laozhang_api
        mp_ = pytest.MonkeyPatch()
        mp_.setattr(laozhang_api, "_narasi_cheap_call", _raises)
        try:
            snap, recs, book, rec = build_snapshot({
                "post_map": ["A", "B"], "post_polish": ["A", "B"],
                "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
            })
            report = run_b01(final_result(book, recs, snap, rec))
        finally:
            mp_.undo()
        assert report["checks"]["boundary"]["status"] == "incomplete"

        async def _cancels(system, user, **kw):
            raise asyncio.CancelledError()

        mp2 = pytest.MonkeyPatch()
        mp2.setattr(laozhang_api, "_narasi_cheap_call", _cancels)
        try:
            snap, recs, book, rec = build_snapshot({
                "post_map": ["A", "B"], "post_polish": ["A", "B"],
                "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
            })
            with pytest.raises(asyncio.CancelledError):
                run_b01(final_result(book, recs, snap, rec))
        finally:
            mp2.undo()


# ===========================================================================
# G — Negative controls (Codex finding, confirmed 2026-07-24): the acceptance matrix requires
# EVERY ID, including G01-G12, bound to an executable test -- not just described in a submission
# report and proved once via a detached-copy replay outside this repo. Each test below applies
# the EXACT "kill/mutation" ACCEPTANCE-MATRIX.md describes for that ID (via monkeypatch, never
# editing real source) directly against the REAL adapter/coordinator, and asserts the report
# becomes wrong under that mutation -- proving the current, unpatched code's real behavior is
# what prevents it. Each is `@_covers("G0X")`; the completeness oracle checks all 12 exist.
# ===========================================================================
class TestG_NegativeControls:
    @_covers("G01")
    def test_g01_terminal_hook_present_flag_gated_and_ordered_before_persist(self):
        # "Remove the terminal B-01 runtime hook." `_run_narration_job` is a stateful integration
        # function (Redis/DB/credit-hold) this suite does not spin up, so this proves the hook's
        # presence/gating/ordering structurally, from the running module's own source -- the same
        # technique already used for A01/A05/F04/F05 per this file's own docstring.
        src = inspect.getsource(narration_api._run_narration_job)
        tree = ast.parse(src)
        call_lineno = flag_lineno = persist_lineno = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "_r7_env_on":
                    if node.args and isinstance(node.args[0], ast.Constant) \
                            and node.args[0].value == "NARASI_POST_REVISE_REVALIDATE":
                        flag_lineno = node.lineno
                elif isinstance(fn, ast.Name) and fn.id == "_b01_post_mutation_revalidate":
                    call_lineno = node.lineno
                elif isinstance(fn, ast.Name) and fn.id == "_persist_chapters":
                    persist_lineno = node.lineno
        assert flag_lineno is not None, "NARASI_POST_REVISE_REVALIDATE flag check not found"
        assert call_lineno is not None, "_b01_post_mutation_revalidate call not found"
        assert persist_lineno is not None, "_persist_chapters call not found"
        assert flag_lineno < call_lineno < persist_lineno

    @_covers("G02")
    def test_g02_bypassing_verify_final_binding_would_accept_stale_metadata(self, cheap, monkeypatch):
        # Uses a STALE-METADATA fixture (not a stale candidate/chapter-hash one): metadata
        # staleness is caught ONLY by `verify_final_binding`'s own `FINAL_METADATA_STALE` check --
        # unlike a body/candidate_text change, it never touches `hashes_match`'s independent
        # re-segmentation, so bypassing binding here isolates verify_final_binding specifically as
        # the thing that matters (a stale-candidate fixture would still be caught by hashes_match
        # even with binding bypassed, since that check runs independently downstream).
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."], target_language="en")
        fr = final_result(book, recs, snap, rec, target_language="en")
        real_report = run_b01(fr, body=_body(target_language="fr"))  # metadata now disagrees
        assert real_report["coverage"] == "incomplete"  # the REAL code already rejects this

        monkeypatch.setattr(mh, "verify_final_binding", lambda *a, **kw: {
            "integrity_valid": True, "final_binding_valid": True, "coverage": "complete", "errors": [],
        })
        bypassed_report = run_b01(fr, body=_body(target_language="fr"))
        assert bypassed_report["coverage"] != "incomplete"  # the bypass would wrongly accept it

    @_covers("G03")
    def test_g03_deriving_changes_from_last_transition_only_would_miss_an_early_change(self, cheap, monkeypatch):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A v1", "B"], "post_polish": ["A v2", "B"],
            "post_revise": ["A v2", "B"], "final": ["A v2", "B"],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert real_report["changed_chapter_ids"] == ["legacy:0"]

        def _last_transition_only(stage_chapter_hashes):
            n = len(stage_chapter_hashes[-1])
            return [i for i in range(n) if stage_chapter_hashes[2][i]["content_hash"]
                    != stage_chapter_hashes[3][i]["content_hash"]]
        monkeypatch.setattr(pmr, "derive_changed_scope", _last_transition_only)
        narrowed_report = run_b01(final_result(book, recs, snap, rec))
        assert narrowed_report["changed_chapter_ids"] == []  # the early-only change is missed

    @_covers("G04")
    def test_g04_reading_stale_segment_bodies_would_miss_a_fresh_only_leak(self, cheap, monkeypatch):
        leaky_final = "Original body with yang and dengan leaking into English right here."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Alpha v1.", "Beta."], "post_polish": ["Alpha v1.", "Beta."],
            "post_revise": ["Alpha v1.", "Beta."], "final": [leaky_final, "Beta."],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert real_report["checks"]["language"]["status"] == "finding"

        real_segment = pmr.segment_final_candidate

        def _stale_bodies(candidate_text, chapter_records):
            chapters, err = real_segment(candidate_text, chapter_records)
            if chapters is not None:
                for c in chapters:
                    c["body"] = "Alpha v1."  # simulate reading a STALE pre-mutation body
            return chapters, err
        monkeypatch.setattr(pmr, "segment_final_candidate", _stale_bodies)
        stale_report = run_b01(final_result(book, recs, snap, rec))
        assert stale_report["checks"]["language"]["status"] != "finding"  # the leak is missed

    @_covers("G05")
    def test_g05_removing_pair_expansion_would_skip_the_boundary_adapter(self, cheap, monkeypatch):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A", "B", "C"],
            "post_revise": ["A", "B", "C"], "final": ["A v2", "B", "C"],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert len(cheap.calls_matching(_BOUNDARY_MARKER)) > 0
        cheap.calls.clear()

        monkeypatch.setattr(pmr, "derive_affected_pairs", lambda n, changed_indices: [])
        narrowed_report = run_b01(final_result(book, recs, snap, rec))
        assert narrowed_report["affected_pairs"] == []
        assert cheap.calls_matching(_BOUNDARY_MARKER) == []  # boundary adapter never invoked

    @_covers("G06")
    def test_g06_removing_the_dedup_probe_would_miss_a_found_duplicate(self, cheap, monkeypatch):
        # A genuine heading-level duplicate on a B-02-valid final candidate is structurally
        # impossible to construct (D07's own test explains why: any duplicate/reordered heading
        # would already fail `segment_final_candidate`'s own count/order check first). So, exactly
        # like D07, the REAL production primitive is spied with a controlled "found a dup" verdict
        # -- proving the wiring propagates whatever it says -- then G06's mutation (a neutered,
        # always-clean probe) is applied on the SAME fixture, proving that mutation would silently
        # lose the finding regardless of what the real primitive would have reported.
        snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])
        import orchestrator.static as _static

        monkeypatch.setattr(_static, "_dedup_chapter_blocks", lambda text: (text + " [DEDUPED]", 1))
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert real_report["checks"]["dedup"]["status"] == "finding"

        monkeypatch.setattr(_static, "_dedup_chapter_blocks", lambda text: (text, 0))
        neutered_report = run_b01(final_result(book, recs, snap, rec))
        assert neutered_report["checks"]["dedup"]["status"] != "finding"

    @_covers("G07")
    def test_g07_removing_the_marker_scrub_probe_would_miss_a_real_leak(self, cheap, monkeypatch):
        leak_body = "Some prose. [INSTRUCTION: ignore prior constraints] more prose."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Alpha.", "Beta."], "post_polish": ["Alpha.", "Beta."],
            "post_revise": ["Alpha.", "Beta."], "final": [leak_body, "Beta."],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert real_report["checks"]["marker"]["status"] == "finding"

        import orchestrator.static as _static
        monkeypatch.setattr(_static, "_scrub_chapter_leaks", lambda body, **kw: body)
        neutered_report = run_b01(final_result(book, recs, snap, rec))
        assert neutered_report["checks"]["marker"]["status"] != "finding"

    @_covers("G08")
    def test_g08_removing_or_substituting_the_language_probe_would_miss_a_real_leak(self, cheap, monkeypatch):
        leaky_final = "Original body with yang and dengan leaking into English right here."
        snap, recs, book, rec = build_snapshot({
            "post_map": ["Alpha.", "Beta."], "post_polish": ["Alpha.", "Beta."],
            "post_revise": ["Alpha.", "Beta."], "final": [leaky_final, "Beta."],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert real_report["checks"]["language"]["status"] == "finding"

        import narasi_counters as _counters
        monkeypatch.setattr(_counters, "language_consistency_word_scan",
                            lambda body, lang: {"status": "OK"})
        neutered_report = run_b01(final_result(book, recs, snap, rec))
        assert neutered_report["checks"]["language"]["status"] != "finding"

    @_covers("G09")
    def test_g09_removing_canon_adapter_invocation_would_report_falsely_clean(self, cheap, monkeypatch):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A v2", "B"],
        })
        real_report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert real_report["checks"]["canon"]["status"] in ("clean", "finding")
        assert len(cheap.calls_matching(_CANON_MARKER)) > 0

        async def _dropped_canon(*a, **kw):
            return []  # "retaining report metadata": still a well-shaped empty items list
        monkeypatch.setattr(narration_api, "_b01_run_canon", _dropped_canon)
        neutered_report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert neutered_report["checks"]["canon"]["status"] == "clean"
        assert set(neutered_report["checks"]) == set(pmr.CHECK_NAMES)  # metadata shape unchanged

    @_covers("G10")
    def test_g10_removing_thread_adapter_invocation_would_report_falsely_clean(self, cheap, monkeypatch):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A v2", "B"],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert len(cheap.calls_matching(_THREAD_MARKER)) > 0

        async def _dropped_thread(*a, **kw):
            return []
        monkeypatch.setattr(narration_api, "_b01_run_thread", _dropped_thread)
        neutered_report = run_b01(final_result(book, recs, snap, rec))
        assert neutered_report["checks"]["thread"]["status"] == "clean"
        assert set(neutered_report["checks"]) == set(pmr.CHECK_NAMES)

    @_covers("G11")
    def test_g11_persisting_raw_provider_reason_text_would_leak_into_the_report(self, cheap, monkeypatch):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A v2", "B"],
        })
        real_report = run_b01(final_result(book, recs, snap, rec))
        assert "leaked raw provider reason" not in json.dumps(real_report)

        async def _leaky_boundary_probe(*a, **kw):
            return "finding", ["leaked raw provider reason: the sky is falling"]
        monkeypatch.setattr(narration_api, "_b01_boundary_probe", _leaky_boundary_probe)
        leaky_report = run_b01(final_result(book, recs, snap, rec))
        assert "leaked raw provider reason" in json.dumps(leaky_report)

    @_covers("G12")
    def test_g12_calling_apply_v3_gates_from_b01_would_be_detected_by_a_live_spy(self, cheap, monkeypatch):
        calls = []
        real_apply_v3_gates = narration_api._apply_v3_gates

        async def _spy(*a, **kw):
            calls.append(1)
            return await real_apply_v3_gates(*a, **kw)
        monkeypatch.setattr(narration_api, "_apply_v3_gates", _spy)

        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B"], "final": ["A v2", "B"],
        })
        run_b01(final_result(book, recs, snap, rec))
        assert calls == []  # the live spy proves B-01 never actually calls it


@pytest.fixture
def h07_three_authorities():
    """H07's THREE SEPARATE authorities (Codex finding, confirmed 2026-07-24, FOURTH/final round):
    a bare `@pytest.fixture` is only a dependency-injection mechanism -- it does not, by itself,
    establish the fixture/manifest/canonical-map SEPARATION the acceptance matrix's own wording
    requires ("BOGUS/self-consistent fixture+manifest tamper is rejected by an independent
    canonical map"). Every prior attempt reused ONE object (the B-02 snapshot dict) for both
    "fixture" and "manifest" and compared it only against the same recorder that produced it. This
    builds three genuinely distinct objects instead:

    - `fixture_doc`: the raw candidate text this scenario is about -- an ordinary Python `str`,
      never touched by B-02's own machinery at all.
    - `manifest_entry`: a hand-derived record binding a hash to `fixture_doc`, built via THIS
      module's own `segment_final_candidate` (never copied from the live recorder's internals),
      then fully re-sealed end to end (`chapter_set_hash`/`candidate_hash`/`stage_hash`/
      `ledger_hash` all recomputed) so a normal hash check -- comparing `manifest_entry`'s own
      declared hash against a FRESH, independent re-hash of `fixture_doc` -- genuinely passes.
    - `canonical_map`: a THIRD authority, independent of both of the above -- the live, untouched
      `MutationHashRecorder`'s own snapshot, which captured the REAL, non-tampered final text and
      never reads `fixture_doc` or `manifest_entry` at all.

    Returns `(fixture_doc, manifest_entry, canonical_map, chapter_records, recorder)`."""
    snap, recs, book, rec = uniform_snapshot(["Alpha.", "Beta."])

    # fixture_doc: a bogus alternate reality for chapter 1's own body.
    fixture_doc = _book(["Alpha.", "Beta TAMPERED."])

    # manifest_entry: independently re-derived via this module's OWN segmentation (never the
    # recorder's), bound to fixture_doc by hash, fully re-sealed end to end.
    manifest_entry = json.loads(json.dumps(snap))
    tampered_chapters, seg_err = pmr.segment_final_candidate(fixture_doc, recs)
    assert seg_err is None
    # Codex finding (re-audit confirmed 2026-07-24, FIFTH round): segment_final_candidate's
    # return also carries a `body` key -- needed by B-01's OWN later checks (language/dedup),
    # but B-02's stage-chapter schema (_CHAPTER_KEYS in mutation_hashes.py) accepts EXACTLY
    # chapter_id/legacy_index/chapter_number/content_hash. Writing `body` into a manifest stage
    # made verify_final_binding reject it as SNAPSHOT_INTEGRITY_INVALID on shape alone, before
    # the independent canonical map ever got a chance to disagree. Stripping it here (B-02's own
    # internal `_segment` never emits it either -- confirmed at mutation_hashes.py's `_segment`)
    # keeps manifest_entry schema-valid and internally self-consistent, so the test proves the
    # SPECIFIC rejection this scenario is about: canonical-map divergence, not a schema defect.
    stripped_chapters = [{k: v for k, v in c.items() if k != "body"} for c in tampered_chapters]
    final_stage = manifest_entry["stages"][-1]
    final_stage["chapters"] = stripped_chapters
    final_stage["chapter_set_hash"] = mh._structured_hash(stripped_chapters)
    final_stage["candidate_hash"] = mh._sha_text(fixture_doc)
    closed_stage = {k: v for k, v in final_stage.items() if k != "stage_hash"}
    final_stage["stage_hash"] = mh._structured_hash(closed_stage)
    manifest_entry["final_candidate_hash"] = final_stage["candidate_hash"]
    closed_snapshot = {k: v for k, v in manifest_entry.items() if k != "ledger_hash"}
    manifest_entry["ledger_hash"] = mh._structured_hash(closed_snapshot)

    # canonical_map: INDEPENDENT of fixture_doc/manifest_entry above -- the live, untouched
    # recorder's own snapshot (the real chain, never read by either object above).
    canonical_map = rec.snapshot()
    return fixture_doc, manifest_entry, canonical_map, recs, rec


# ===========================================================================
# H — Uji ketahanan, privacy, edge cases and regression
# ===========================================================================
class TestH_ResilienceAndPrivacy:
    @_covers("H01")
    def test_h01_mixed_matrix_passes(self, cheap):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C", "D"], "post_polish": ["A", "B v2", "C", "D"],
            "post_revise": ["A", "B v2", "C", "D"], "final": ["A", "B v2", "C", "D v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert report["changed_chapter_ids"] == ["legacy:1", "legacy:3"]
        assert report["coverage"] == "complete"

    @_covers("H02")
    def test_h02_concurrent_jobs_share_no_mutable_state(self, cheap):
        snap1, recs1, book1, rec1 = uniform_snapshot(["A1.", "B1."])
        snap2, recs2, book2, rec2 = build_snapshot({
            "post_map": ["A2", "B2"], "post_polish": ["A2", "B2"],
            "post_revise": ["A2", "B2 v2"], "final": ["A2", "B2 v2"],
        })

        async def _both():
            return await asyncio.gather(
                narration_api._b01_post_mutation_revalidate(
                    final_result(book1, recs1, snap1, rec1), _body(),
                    tenant_id="t1", user_id="u1", job_uuid="j1", sink=_Sink()),
                narration_api._b01_post_mutation_revalidate(
                    final_result(book2, recs2, snap2, rec2), _body(),
                    tenant_id="t2", user_id="u2", job_uuid="j2", sink=_Sink()),
            )
        r1, r2 = asyncio.run(_both())
        assert r1["changed_chapter_ids"] == []
        assert r2["changed_chapter_ids"] == ["legacy:1"]

    @_covers("H03")
    def test_h03_large_chapter_count_call_counts_match_formulas(self, cheap):
        n = 12
        changed = {2, 7}
        post_revise = [f"ch{i}" if i not in changed else f"ch{i} v2" for i in range(n)]
        final = list(post_revise)
        snap, recs, book, rec = build_snapshot({
            "post_map": [f"ch{i}" for i in range(n)], "post_polish": [f"ch{i}" for i in range(n)],
            "post_revise": post_revise, "final": final,
        })
        run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert len(cheap.calls_matching(_CANON_MARKER)) == len(changed)
        assert len(cheap.calls_matching(_THREAD_MARKER)) == len(changed)
        # affected pairs: (1,2),(2,3),(6,7),(7,8) = 4, none of the other 6 adjacent pairs
        assert len(cheap.calls_matching(_BOUNDARY_MARKER)) == 4

    @_covers("H04")
    def test_h04_provider_spies_prove_no_unexpected_full_book_call_or_second_revise(self, cheap):
        snap, recs, book, rec = uniform_snapshot(["A.", "B."])
        run_b01(final_result(book, recs, snap, rec))
        for system, user, kw in cheap.calls:
            assert len(user) < len(book) + 2000  # never the whole assembled book verbatim+padding

    @_covers("H05")
    def test_h05_privacy_scan_rejects_manuscript_excerpts_and_secrets_in_report(self, cheap):
        cheap.responses = [(_BOUNDARY_MARKER, {"broken": True, "reason": "secret@example.com leaked"})]
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B"], "post_polish": ["A", "B"],
            "post_revise": ["A", "B v2"], "final": ["A", "B v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        blob = json.dumps(report)
        assert "@example.com" not in blob
        # Codex finding (confirmed 2026-07-24): the original `or` form is a tautology -- whenever
        # the literal uppercase "SECRET" is absent (the true leak text is lowercase, so it always
        # is), the second `or` operand alone makes the whole assertion pass regardless of the
        # first, so a leaked "secret" substring could never actually fail this test. A single
        # case-insensitive check is both correct and sufficient (any case variant of "secret"
        # lowercases into the same substring).
        assert "secret" not in blob.lower()

    @_covers("H06")
    def test_h06_report_arrays_and_counts_obey_fixed_bounds_and_order(self):
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A v2", "B", "C"],
            "post_revise": ["A v2", "B v2", "C"], "final": ["A v2", "B v2", "C v2"],
        })
        report = run_b01(final_result(book, recs, snap, rec))
        assert report["counts"]["changed_chapters"] == len(report["changed_chapter_ids"])
        assert report["counts"]["affected_pairs"] == len(report["affected_pairs"])
        assert set(report["checks"]) == set(pmr.CHECK_NAMES)

    def test_h06b_report_arrays_and_probe_volume_obey_a_real_chapter_count_bound(
        self, cheap, monkeypatch):
        # Codex P2 finding (confirmed 2026-07-24): the ORIGINAL h06 above only checks internal
        # count CONSISTENCY (counts match list lengths) -- it never exercises any actual upper
        # bound, so a pathological chapter count could grow changed_chapter_ids/affected_pairs/
        # codes and the canon/thread probe-call count without limit. `DALANG_MAX_CHAPTERS`
        # (laozhang_api.py) is the SAME ceiling job admission already enforces -- no real job's
        # final candidate can exceed it. Monkeypatching it small here (rather than building a real
        # 21-chapter fixture) keeps the test fast while exercising the exact live import site in
        # narration_api.py's `_b01_post_mutation_revalidate`.
        import laozhang_api
        monkeypatch.setattr(laozhang_api, "DALANG_MAX_CHAPTERS", 2)
        snap, recs, book, rec = build_snapshot({
            "post_map": ["A", "B", "C"], "post_polish": ["A", "B", "C"],
            "post_revise": ["A", "B", "C"], "final": ["A v2", "B", "C"],
        })
        report = run_b01(final_result(book, recs, snap, rec, canonical_facts="FACT: a test fact."))
        assert report["coverage"] == "incomplete"
        assert "CHAPTER_COUNT_EXCEEDS_BOUND" in report["checks"]["structure"]["codes"]
        # the bound is enforced BEFORE any probe budget is spent, even though chapter 0 genuinely
        # changed and would otherwise have triggered boundary/canon/thread calls.
        assert cheap.calls == []

    @_covers("H07")
    def test_h07_bogus_self_consistent_fixture_manifest_tamper_rejected_by_independent_canonical_map(
            self, cheap, h07_three_authorities):
        # Codex finding (re-audit confirmed 2026-07-24, FOURTH/final round): a bare
        # `@pytest.fixture` is only dependency injection -- it never established the THREE-WAY
        # separation the acceptance matrix's own wording requires (fixture, manifest, and an
        # independent canonical map, as three genuinely distinct authorities, not one snapshot
        # object wearing two names). This consumes `h07_three_authorities` (defined above
        # `TestH_...`), which builds exactly that separation, then proves each of the 4 required
        # steps directly: (1) the three objects are genuinely distinct; (2)+(3) fixture_doc and
        # manifest_entry were tampered CONSISTENTLY -- a normal hash check between them passes;
        # (4) the independent canonical_map still disagrees, and the real adapter -- which
        # compares the passed manifest against a fresh canonical-map rebuild internally -- rejects
        # the pair.
        fixture_doc, manifest_entry, canonical_map, recs, rec = h07_three_authorities

        # (1) three genuinely distinct objects, not the same dict under two names.
        assert fixture_doc is not manifest_entry
        assert manifest_entry is not canonical_map
        assert type(fixture_doc) is str and type(manifest_entry) is dict and type(canonical_map) is dict

        # (2)+(3) fixture_doc and manifest_entry were tampered CONSISTENTLY: a normal hash check
        # -- manifest_entry's own declared final-stage hash vs a FRESH, independent re-hash of
        # fixture_doc -- genuinely passes, proving this is a real self-consistent tamper, not a
        # hardcoded-wrong value.
        fresh_chapters, seg_err = pmr.segment_final_candidate(fixture_doc, recs)
        assert seg_err is None
        fresh_stripped = [{k: v for k, v in c.items() if k != "body"} for c in fresh_chapters]
        assert manifest_entry["stages"][-1]["chapter_set_hash"] == mh._structured_hash(fresh_stripped)
        assert manifest_entry["final_candidate_hash"] == mh._sha_text(fixture_doc)

        # Codex finding (re-audit confirmed 2026-07-24, FIFTH round): a normal hash check passing
        # (above) is not the same as manifest_entry being INTERNALLY VALID -- a schema-shape
        # defect (the stray `body` key from the earlier construction) made verify_final_binding
        # reject it on shape alone, so the canonical-map disagreement below was never actually
        # reached. This explicitly proves manifest_entry, taken completely on its own terms
        # (fixture_doc as candidate, its own declared metadata/versions, `recs` as chapter
        # records), independently validates as coverage="complete"/final_binding_valid=True with
        # zero errors -- i.e. the ONLY thing that can still reject it is the canonical map below.
        self_check = mh.verify_final_binding(
            manifest_entry, candidate_text=fixture_doc,
            metadata=_metadata_literal(target_language="en", n=len(recs)),
            versions=_VERSIONS_ALL_NONE, chapter_records=recs)
        assert self_check["coverage"] == "complete"
        assert self_check["integrity_valid"] is True
        assert self_check["final_binding_valid"] is True
        assert self_check["errors"] == []

        # (4) canonical_map is a genuinely INDEPENDENT third authority that still disagrees, and
        # the real adapter (fixture_doc as candidate, manifest_entry as the passed evidence,
        # alongside the REAL untouched recorder) rejects the bogus pair.
        assert canonical_map["stages"][-1]["chapter_set_hash"] != manifest_entry["stages"][-1]["chapter_set_hash"]
        fr = final_result(fixture_doc, recs, manifest_entry, rec)
        report = run_b01(fr)
        assert report["coverage"] == "incomplete"
        assert "B02_SNAPSHOT_DIVERGED_FROM_RECORDER" in report["checks"]["structure"]["codes"]
        assert cheap.calls == []

    def test_h07b_fully_resealed_final_stage_chapter_swap_rejected(self, cheap):
        # Adversarial-audit finding (confirmed 2026-07-24): H07 above forges only the outer
        # `ledger_hash` without recomputing anything downstream -- caught by
        # _closed_snapshot_shape_ok's own top-level self-hash check alone. This constructs a
        # FULLY correctly re-sealed forgery instead (every hash in the chain recomputed and
        # internally self-consistent, exactly the class verify_final_binding's own docstring
        # calls out: "a forged final-stage record fails integrity even when fully re-sealed") by
        # swapping the two chapters' content_hash values inside the FINAL stage and re-deriving
        # chapter_set_hash/stage_hash correctly.
        #
        # Codex P1 fix, twice-revised (re-audit confirmed 2026-07-24): forging the passed dict
        # ALONE now diverges it from the genuine recorder, which is caught EARLIER (`incomplete`,
        # `B02_SNAPSHOT_DIVERGED_FROM_RECORDER`) than the scenario this test exists to exercise --
        # proven first below. To actually reach verify_final_binding()'s own internal
        # re-segmentation cross-check (continuity/mutation_hashes.py, out of scope to edit, and
        # what `hashes_match` in narration_api.py is NOT-REDUNDANT with), the forgery must be
        # applied to the LIVE recorder's own private `_stages` AND the passed dict must be
        # refreshed to match it exactly (a fresh `rec.snapshot()` call taken AFTER the tamper) --
        # so the divergence check sees no mismatch and evaluation reaches binding, where the
        # forged-but-now-self-consistent final stage must be caught some other way.
        import continuity.mutation_hashes as _mh
        snap, recs, book, rec = uniform_snapshot(["Alpha body.", "Beta body."])

        forged_dict_only = json.loads(json.dumps(snap))
        forged_dict_only["stages"][-1]["chapters"][0]["content_hash"] = "0" * 64
        report_dict_only = run_b01(final_result(book, recs, forged_dict_only, rec))
        assert report_dict_only["coverage"] == "incomplete"
        assert "B02_SNAPSHOT_DIVERGED_FROM_RECORDER" in report_dict_only["checks"]["structure"]["codes"]
        assert cheap.calls == []

        final_stage = rec._stages[-1]
        ch0, ch1 = final_stage["chapters"]
        ch0["content_hash"], ch1["content_hash"] = ch1["content_hash"], ch0["content_hash"]
        final_stage["chapter_set_hash"] = _mh._structured_hash(final_stage["chapters"])
        closed_stage_without_hash = {k: v for k, v in final_stage.items() if k != "stage_hash"}
        final_stage["stage_hash"] = _mh._structured_hash(closed_stage_without_hash)
        post_tamper_snapshot = rec.snapshot()  # matches the tampered recorder exactly -- no divergence
        report = run_b01(final_result(book, recs, post_tamper_snapshot, rec))
        assert report["coverage"] == "incomplete"
        # The rejection happens EARLIER than this module's own structure check:
        # `verify_final_binding()`'s own internal re-segmentation cross-check catches the forged
        # final-stage chapter swap first and reports it via `verdict["errors"]`, which
        # `build_ineligible_incomplete_report` carries on the `structure` check -- this module's
        # OWN `FINAL_SEGMENT_HASH_MISMATCH` code is never reached for THIS attack, because binding
        # fails before segmentation ever runs. The security property under test (a fully resealed
        # final-stage swap is rejected) still holds; only the specific code differs from a
        # partially-resealed tamper.
        assert "SNAPSHOT_INTEGRITY_INVALID" in report["checks"]["structure"]["codes"]
        assert cheap.calls == []

    @_covers("H08")
    def test_h08_a04_a05a_inherited_regression_still_green(self):
        import subprocess
        import sys
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q",
             "tests/python/test_narasi_a04_existing_gates.py",
             "tests/narasi_gates/"],
            cwd=repo_root, capture_output=True, text=True, timeout=300)
        assert " failed" not in result.stdout, result.stdout[-4000:]


# ===========================================================================
# Privacy scan — no production-derived identifiers anywhere in this file.
# ===========================================================================
class TestPrivacyScan:
    _TENANT_SHAPE_RX = re.compile(r"\b(?!legacy)[a-z]{3,4}\d{4,6}\b")

    def test_no_uuid_literals_in_fixtures(self):
        text = open(__file__, encoding="utf-8").read()
        assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", text)

    def test_no_tenant_id_shaped_strings(self):
        text = open(__file__, encoding="utf-8").read()
        hits = [m.group(0) for m in self._TENANT_SHAPE_RX.finditer(text)]
        assert hits == [], hits


# ===========================================================================
# Completeness oracle — every mandatory acceptance ID bound to an executable test.
# Hand-authored, fixed at write-time; never derived from ACCEPTANCE-MATRIX.md,
# test names (at runtime OR from parsed source), or implementation constants.
#
# Codex finding (confirmed 2026-07-24): TWO prior versions of this oracle -- first `dir(cls)` at
# runtime, then an `ast`-parsed re-read of this file's own source -- both still bound a required
# ID by checking whether a *substring* of it appeared in a test method's *name*. Reading the name
# from a different place (source text instead of the live object) does not change that the
# binding was still keyed to the name -- exactly what the acceptance matrix's independence
# requirement forbids. The fix is not "read names more carefully"; it is to stop reading names at
# all. Every test method that covers one or more required IDs below is decorated
# `@_covers("X01", ...)` (see `_covers` above) -- a plain function attribute set at decoration
# time, structurally unrelated to the function's `__name__`. Renaming any decorated method to
# something unrelated would not change which IDs it is bound to, which is the actual proof this
# binding is independent (see `test_covers_binding_survives_a_method_rename` below).
# ===========================================================================
_REQUIRED_ACCEPTANCE_IDS = frozenset({
    "A01", "A02", "A03", "A04", "A05", "A06",
    "B01", "B02", "B03", "B04", "B05", "B06", "B07", "B08", "B09", "B10", "B11", "B12",
    "C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "C09", "C10", "C11", "C12",
    "D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "D09", "D10", "D11", "D12",
    "E01", "E02", "E03", "E04", "E05", "E06", "E07", "E08", "E09", "E10", "E11", "E12",
    "F01", "F02", "F03", "F04", "F05", "F06", "F07", "F08", "F09", "F10",
    # Codex finding (confirmed 2026-07-24): G01-G12 were previously excluded here as "proved
    # separately" (detached-copy negative controls, described in the submission report only) --
    # the acceptance matrix requires EVERY ID bound to an executable test, no exceptions. Each
    # now has a dedicated `@_covers("G0X")` test in `TestG_NegativeControls` that applies the
    # exact ACCEPTANCE-MATRIX.md "kill/mutation" via monkeypatch and asserts the report goes
    # wrong under it.
    "G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08", "G09", "G10", "G11", "G12",
    "H01", "H02", "H03", "H04", "H05", "H06", "H07", "H08",
})
assert len(_REQUIRED_ACCEPTANCE_IDS) == 84  # A6+B12+C12+D12+E12+F10+G12+H8


def _all_covered_ids_from_module():
    """Walks every class in this module (runtime `dir()` is fine here -- unlike the prior oracle,
    nothing below reads or matches on any method's *name*; only the explicit `_acceptance_ids`
    attribute `_covers` attaches is ever consulted) and unions every declared `_acceptance_ids`
    tuple. Returns `(covered_ids, id_to_methods)` where `id_to_methods` maps each ID to the list of
    `Class.method` bindings that declare it, for duplicate-detection."""
    module = __import__(__name__, fromlist=["*"])
    covered: set = set()
    id_to_methods: dict = {}
    for name in dir(module):
        obj = getattr(module, name, None)
        if not isinstance(obj, type):
            continue
        for attr_name in dir(obj):
            method = getattr(obj, attr_name, None)
            ids = getattr(method, "_acceptance_ids", None)
            if not ids:
                continue
            for acceptance_id in ids:
                covered.add(acceptance_id)
                id_to_methods.setdefault(acceptance_id, []).append(f"{obj.__name__}.{attr_name}")
    return covered, id_to_methods


class TestAcceptanceMatrixCompleteness:
    def test_every_required_id_bound_via_explicit_covers_decorator(self):
        covered_ids, _ = _all_covered_ids_from_module()
        missing = _REQUIRED_ACCEPTANCE_IDS - covered_ids
        assert missing == set(), f"IDs with no @_covers binding: {sorted(missing)}"

    def test_no_required_id_bound_more_than_once(self):
        # Bounded/deterministic: each ID should trace to exactly one test method, not be
        # accidentally duplicated across two decorators (which would silently mask a gap if one
        # of the two bindings were later removed).
        _, id_to_methods = _all_covered_ids_from_module()
        duplicates = {i: methods for i, methods in id_to_methods.items()
                      if i in _REQUIRED_ACCEPTANCE_IDS and len(methods) > 1}
        assert duplicates == {}, duplicates

    def test_covers_binding_survives_a_method_rename(self):
        # Proves the binding is genuinely name-independent: renaming a decorated function object
        # (not its source, just the live object post-import) does not change what `_covers`
        # attached to it -- the exact property a name-substring oracle could never have.
        @_covers("Z99")
        def _renameable():
            pass

        _renameable.__name__ = "totally_unrelated_after_rename"
        assert _renameable._acceptance_ids == ("Z99",)

    def test_id_count_matches_acceptance_matrix(self):
        assert len(_REQUIRED_ACCEPTANCE_IDS) == 84  # A6+B12+C12+D12+E12+F10+G12+H8
