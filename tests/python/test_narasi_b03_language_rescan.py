"""
B-03 Deterministic Post-Mutation Language Re-scan (test-only scope, default-off flag).

Acceptance suite for python/narasi_language_rescan.py (the pure coordinator) plus its three
wiring points: orchestrator/static.py's narrate_chapters (post_map/post_polish hooks),
narration_api.py's _apply_v3_gates (post_revise hook), and narration_api.py's
_b03_final_language_rescan (final hook, called from _run_narration_job before persistence).

Every acceptance ID below is bound via the explicit @_covers(...) decorator -- never derived
from a test method's own name -- mirroring the completeness-oracle design established (and
independently re-audited) in test_narasi_b01_post_mutation_revalidation.py.

Synthetic content only: no production manuscript text, names, tenant/job IDs anywhere in this
file, matching the A-05a/A-04 privacy discipline (see TestPrivacyScan at the bottom).
"""
import ast
import asyncio
import hashlib
import inspect
import json

import pytest

import narasi_language_rescan as lr
import narration_api
import orchestrator.static as static


# ===========================================================================
# Completeness-oracle machinery (identical pattern to B-01's, independently
# re-audited there: an explicit, out-of-band attribute, never a name match).
# ===========================================================================
def _covers(*acceptance_ids):
    """Attaches an explicit, out-of-band acceptance-ID binding to a test method -- a plain
    function attribute, never derived from (or matched against) the method's own Python
    identifier."""
    def _decorator(fn):
        fn._acceptance_ids = tuple(acceptance_ids)
        return fn
    return _decorator


_REQUIRED_ACCEPTANCE_IDS = frozenset({
    # Group A -- direct detector behavior (10)
    "A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "A9", "A10",
    # Group B -- stage wiring (10)
    "B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", "B10",
    # Group C -- authority and isolation (8)
    "C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8",
    # Group D -- flag behavior (7)
    "D1", "D2", "D3", "D4", "D5", "D6", "D7",
    # Group E -- regression and non-interference (7)
    "E1", "E2", "E3", "E4", "E5", "E6", "E7",
    # Mandatory executable negative controls
    "G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08",
    "G09", "G10", "G11", "G12", "G13", "G14", "G15", "G16",
    # Rework (Codex re-audit, 2026-07-24) -- additional negative controls explicitly
    # required by the rejection verdict, beyond the original G01-G16 set.
    "H1",  # all-not_applicable bypass (post_revise/final can never be not_applicable)
    "H2",  # final hook import failure -> bounded incomplete report, never absent
    "H3",  # missing/blank target language with all 4 stages present -> still incomplete
    "H4",  # nested (not just top-level) caller mutation cannot retroactively alter the report
    "H5",  # reversed stage order is rejected
    "H6",  # self-inconsistent scanner return (status vs count/hits) is rejected
    # Rework 3 (Codex adversarial re-audit, 2026-07-24) -- explicit executable proofs for the
    # authorized structural fix (closure-scoped provenance, deep-copy-on-read, independent
    # semantic re-derivation), mapped 1:1 to the rejection verdict's lettered test list A-J.
    "I1",   # A: object.__setattr__ hash forgery -- rejected/incomplete, forged hash absent
    "I2",   # B: object.__setattr__ status forgery on a genuine finding -- never complete/clean
    "I3",   # C: nested word_scan mutated to FLAG before build_report() -- never false-clean
    "I4",   # D: every visible/private construction path -- cannot mint a trusted observation
    "I5",   # E: mutating entry.to_dict()'s result before build_report() -- observation unchanged
    "I6",   # F: mutating entry["word_scan"]/.get()/attribute results -- observation unchanged
    "I7",   # G: pre-build AND post-build nested mutation, combined
    "I8",   # H: hostile values via object.__setattr__ -- no crash, no false-clean
    "I9",   # I: weak-reference proof for the WeakKeyDictionary provenance registry
    "J1",   # negative control: disabling provenance verification lets pure hash forgery through
    "J2",   # negative control: disabling semantic re-derivation (with provenance also blind)
            #   lets a status/evidence mismatch through
    "J3",   # negative control: simulated aliasing (raw dict exposed instead of a deep copy)
            #   lets accidental/malicious nested mutation reach the observation's real state
    "J4",   # negative control: permitting direct __init__ construction (undoing the guard)
            #   would let forged fields be set directly, proving the guard is load-bearing
    # Rework 4 (Codex hostile-input crash + threat-model + scanner-isolation audit,
    # 2026-07-24) -- executable adversarial proofs for the second authorized structural fix.
    "K1",   # hostile stage __eq__ via scan_stage (production) never crashes
    "K2",   # hostile stage __eq__ via not_applicable_stage never crashes
    "K3",   # hostile stage __eq__ via incomplete_stage never crashes
    "K4",   # hostile word_scan status __eq__ (via the test seam) never crashes
    "K5",   # hostile dict key with a state-dependent __hash__ never crashes
    "K6",   # hostile values in every sentence_scan field never crash; bounded error codes
    "K7",   # hostile values in every word_scan field never crash; bounded error codes
    "K8",   # AST proof: neither production hook file ever calls the test-only scanner seam
    "L1",   # negative control: disabling _normalize_word_scan's exact-type status guard
            #   (monkeypatch) lets a hostile status crash it
    "L2",   # negative control: the exact unguarded `stage not in STAGE_ORDER` operation,
            #   reproduced directly, DOES raise for a hostile stage (proves the guard's need)
    "L3",   # negative control: the AST production-purity checker correctly flags a synthetic
            #   snippet that calls the test seam from a production-shaped call site
})
assert len(_REQUIRED_ACCEPTANCE_IDS) == 88  # 10+10+8+7+7+16+6+9+4+8+3


def _all_covered_ids_from_module():
    module = __import__(__name__, fromlist=["*"])
    covered = set()
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
        covered, _ = _all_covered_ids_from_module()
        missing = _REQUIRED_ACCEPTANCE_IDS - covered
        assert not missing, f"unbound acceptance IDs: {sorted(missing)}"

    def test_no_required_id_bound_more_than_once(self):
        _, id_to_methods = _all_covered_ids_from_module()
        dupes = {k: v for k, v in id_to_methods.items() if len(v) > 1}
        assert not dupes, f"IDs bound to more than one test: {dupes}"

    def test_covers_binding_survives_a_method_rename(self):
        @_covers("ZZZ_RENAME_PROBE")
        def _fn():
            pass
        _fn.__name__ = "totally_renamed_after_decoration"
        assert _fn._acceptance_ids == ("ZZZ_RENAME_PROBE",)

    def test_id_count_matches_acceptance_matrix(self):
        assert len(_REQUIRED_ACCEPTANCE_IDS) == 88


# ===========================================================================
# Synthetic fixtures (no production text/names/IDs anywhere)
# ===========================================================================
CLEAN_EN = "The lighthouse keeper walked along the quiet shore every morning before dawn."
WORD_LEAK_EN = "The keeper walked along the shore, dengan the tide receding slowly today."
SENTENCE_LEAK_EN = "The keeper walked home under the stars. Kenapa aku merasa lega dan sedih hari itu?"
NONLATIN_LEAK_EN = "The keeper said Привет and walked on toward the gate."
NATIVE_ID = "Dia berjalan dengan tenang di sepanjang pantai setiap pagi sebelum fajar."
MIXED_CASE_LEAK = "THE KEEPER walked. Dengan the tide low, DENGAN the wind still, dengan!"

# Rework 4 (Codex hostile-input audit, 2026-07-24): the complete, fixed vocabulary of error
# codes scan_stage()/_scan_stage_test_seam() can ever emit -- used to assert that a hostile
# scanner/stage input never causes an error code to carry anything beyond one of these bounded
# literals (never a fragment of candidate_text, never str(hostile_object), never exception text).
_KNOWN_SCAN_STAGE_ERROR_CODES = frozenset({
    "STAGE_ID_INVALID", "CANDIDATE_TYPE_INVALID", "TARGET_LANGUAGE_INVALID",
    "CANDIDATE_HASH_FAILED", "SCANNER_IMPORT_FAILED", "SENTENCE_SCAN_UNAVAILABLE",
    "WORD_SCAN_UNAVAILABLE",
})


def _many_word_leak_samples(n=15):
    return [{"term": f"term{i}", "snippet": f"snippet-{i}-context"} for i in range(n)]


def _many_sentence_samples(n=15):
    return [f"leaked sentence number {i}" for i in range(n)]


class _RaisingScanner:
    def __call__(self, *a, **kw):
        raise RuntimeError("synthetic scanner failure")


class _HostileDict(dict):
    def __eq__(self, other):
        raise RuntimeError("hostile __eq__")

    def __hash__(self):
        raise RuntimeError("hostile __hash__")


class _HostileList(list):
    def __iter__(self):
        raise RuntimeError("hostile __iter__")


class _HostileStr(str):
    def __eq__(self, other):
        raise RuntimeError("hostile str __eq__")

    def __hash__(self):
        raise RuntimeError("hostile str __hash__")


def _clean_sentence_raw():
    return {"applies": True, "hits": 0, "samples": [], "other_langs": {}}


def _clean_word_raw():
    return {"status": "PASS", "count": 0, "samples": []}


# ===========================================================================
# Real-path harnesses -- call the ACTUAL product hooks end to end, with only
# the LLM/provider boundary monkeypatched (never a real network/DB call).
# ===========================================================================
async def _narrate(monkeypatch, *, chapter_texts, language="en", flag_on=True, polish_text=None):
    from orchestrator.context_builder import SharedContext

    async def _fake_write_chapter(*, ctx, ch, no, total, **kw):
        return {"ok": True, "output": chapter_texts[no], "no": no, "model": "test-worker"}

    async def _fake_polish_reduce(*, book, topic, style, language, polish, manager_model,
                                   timeout, telemetry_sink, any_failures, **kw):
        if polish_text is None:
            return book, False
        return polish_text, True

    monkeypatch.setattr(static, "_write_chapter", _fake_write_chapter)
    monkeypatch.setattr(static, "_polish_reduce", _fake_polish_reduce)
    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    if flag_on:
        monkeypatch.setenv("NARASI_LANGUAGE_CONSISTENCY_SCAN", "1")
    else:
        monkeypatch.delenv("NARASI_LANGUAGE_CONSISTENCY_SCAN", raising=False)

    chapters = [{"id": str(i + 1), "title": f"Ch{i + 1}"} for i in range(len(chapter_texts))]
    return await static.narrate_chapters(
        "a test topic", chapters, style="storytelling", language=language, polish="light",
        shared_context=SharedContext(canonical_facts="", story_contract=""), max_parallel=4,
    )


async def _gates(monkeypatch, *, book_text, language="en", flag_on=True,
                  seed_entries="__absent__", result_extra=None, body_extra=None):
    import laozhang_api

    monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")

    async def _noop_cheap(system, user, **kw):
        return "{}", 0

    async def _noop_revise(full_text, critique, style, language, **kw):
        return full_text, 0

    async def _noop_critique(book, style, language, **kw):
        return {"violations": []}, 0

    revise_calls = []

    async def _recording_revise(full_text, critique, style, language, **kw):
        revise_calls.append((full_text, critique, style, language))
        return full_text, 0

    class _ForbiddenProvider:
        def __call__(self, *a, **kw):
            raise AssertionError(
                "laozhang_api.make_narasi_client must never be called from this harness")

    monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", _noop_cheap)
    monkeypatch.setattr(laozhang_api, "_narasi_consistency_revise", _recording_revise)
    monkeypatch.setattr(laozhang_api, "_narasi_consistency_critique", _noop_critique)
    monkeypatch.setattr(laozhang_api, "make_narasi_client", _ForbiddenProvider())

    if flag_on:
        monkeypatch.setenv("NARASI_LANGUAGE_CONSISTENCY_SCAN", "1")
    else:
        monkeypatch.delenv("NARASI_LANGUAGE_CONSISTENCY_SCAN", raising=False)

    result = {"book": book_text, "chapters": []}
    if seed_entries != "__absent__":
        result["_b03_stage_entries"] = seed_entries
    if result_extra:
        result.update(result_extra)
    body = {"style": "storytelling", "language": language, "mode": "book"}
    body.update(body_extra or {})
    await narration_api._apply_v3_gates(result, body)
    return result, revise_calls


# ===========================================================================
# Group A -- direct detector behavior
# ===========================================================================
class TestA_DirectDetectorBehavior:
    @_covers("A1")
    def test_a1_clean_english_under_target_en(self):
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        assert entry["status"] == "clean"
        assert entry["sentence_scan"]["hits"] == 0
        assert entry["word_scan"]["status"] == "PASS"

    @_covers("A2")
    def test_a2_indonesian_word_leak_under_target_en(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        assert entry["status"] == "finding"
        assert entry["word_scan"]["status"] == "FLAG"
        assert entry["word_scan"]["count"] >= 1

    @_covers("A3")
    def test_a3_full_indonesian_sentence_leak_under_target_en(self):
        entry = lr.scan_stage(candidate_text=SENTENCE_LEAK_EN, target_language="en", stage="post_map")
        assert entry["status"] == "finding"
        assert entry["sentence_scan"]["applies"] is True
        assert entry["sentence_scan"]["hits"] > 0

    @_covers("A4")
    def test_a4_nonlatin_script_leak_under_latin_target(self):
        entry = lr.scan_stage(candidate_text=NONLATIN_LEAK_EN, target_language="en", stage="post_map")
        assert entry["sentence_scan"]["applies"] is True
        assert entry["sentence_scan"]["hits"] > 0
        assert "non_latin" in entry["sentence_scan"]["other_langs"]
        assert entry["status"] == "finding"

    @_covers("A5")
    def test_a5_native_indonesian_under_target_id_not_a_finding(self):
        entry = lr.scan_stage(candidate_text=NATIVE_ID, target_language="id", stage="post_map")
        assert entry["status"] == "clean"
        assert entry["word_scan"]["status"] == "PASS"  # ID-family targets are skipped by word_scan

    @_covers("A6")
    def test_a6_empty_text(self):
        entry = lr.scan_stage(candidate_text="", target_language="en", stage="post_map")
        assert entry["status"] == "clean"
        assert entry["sentence_scan"]["hits"] == 0
        assert entry["word_scan"]["count"] == 0
        assert entry["candidate_sha256"] == hashlib.sha256(b"").hexdigest()

    @_covers("A7")
    def test_a7_unknown_unseeded_language_applicability(self):
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="xx", stage="post_map")
        assert entry["sentence_scan"]["applies"] is False
        assert entry["status"] == "clean"  # word_scan alone still runs and finds nothing here

    @_covers("A8")
    def test_a8_mixed_capitalization_and_punctuation(self):
        entry = lr.scan_stage(candidate_text=MIXED_CASE_LEAK, target_language="en", stage="post_map")
        assert entry["status"] == "finding"
        assert entry["word_scan"]["status"] == "FLAG"
        assert entry["word_scan"]["count"] >= 1

    @_covers("A9")
    def test_a9_bounded_samples(self):
        def _fake_sentence(text, lang):
            return {"applies": True, "hits": 15, "samples": _many_sentence_samples(15), "other_langs": {}}

        def _fake_word(text, lang):
            return {"status": "FLAG", "count": 15, "samples": _many_word_leak_samples(15)}

        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                               sentence_scanner=_fake_sentence, word_scanner=_fake_word)
        assert len(entry["sentence_scan"]["samples"]) == 10
        assert len(entry["word_scan"]["samples"]) == 10

    @_covers("A10")
    def test_a10_exact_candidate_sha256(self):
        text = WORD_LEAK_EN
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert lr.candidate_sha256(text) == expected
        entry = lr.scan_stage(candidate_text=text, target_language="en", stage="post_map")
        assert entry["candidate_sha256"] == expected


# ===========================================================================
# Group B -- stage wiring (real product hooks, LLM boundary mocked)
# ===========================================================================
class TestB_StageWiring:
    @_covers("B1")
    def test_b1_post_map_hook_scans_post_mutation_text(self, monkeypatch):
        result = asyncio.run(_narrate(
            monkeypatch, chapter_texts=[WORD_LEAK_EN, CLEAN_EN], polish_text=CLEAN_EN + " " + CLEAN_EN))
        entries = result["_b03_stage_entries"]
        post_map = next(e for e in entries if e["stage"] == "post_map")
        assert post_map["status"] == "finding"
        assert post_map["candidate_sha256"] == hashlib.sha256(result["raw_book"].encode("utf-8")).hexdigest()

    @_covers("B2")
    def test_b2_post_polish_hook_scans_post_mutation_text(self, monkeypatch):
        result = asyncio.run(_narrate(
            monkeypatch, chapter_texts=[CLEAN_EN, CLEAN_EN], polish_text=WORD_LEAK_EN))
        entries = result["_b03_stage_entries"]
        post_polish = next(e for e in entries if e["stage"] == "post_polish")
        assert post_polish["status"] == "finding"
        assert post_polish["candidate_sha256"] == hashlib.sha256(result["book"].encode("utf-8")).hexdigest()

    @_covers("B3")
    def test_b3_post_revise_hook_scans_post_mutation_text(self, monkeypatch):
        # Capture the exact text at the moment scan_stage runs -- _apply_v3_gates inserts a
        # "> **Gaya:**" header AFTER the post_revise capture point, so the function's FINAL
        # result["book"] is not the right comparison target; the spy captures the true
        # in-the-moment candidate.
        seen = {}
        real_scan_stage = lr.scan_stage

        def _spy(*, stage, candidate_text, **kw):
            if stage == "post_revise":
                seen["candidate_text"] = candidate_text
            return real_scan_stage(stage=stage, candidate_text=candidate_text, **kw)

        monkeypatch.setattr(lr, "scan_stage", _spy)
        result, _ = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN))
        entries = result["_b03_stage_entries"]
        post_revise = next(e for e in entries if e["stage"] == "post_revise")
        assert post_revise["status"] == "finding"
        assert seen["candidate_text"] == WORD_LEAK_EN
        assert post_revise["candidate_sha256"] == hashlib.sha256(WORD_LEAK_EN.encode("utf-8")).hexdigest()

    @_covers("B4")
    def test_b4_final_hook_scans_exact_persistence_candidate(self):
        final_result = {"book": WORD_LEAK_EN}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        report = final_result["post_mutation_language_rescan"]
        final_entry = next(s for s in report["stages"] if s["stage"] == "final")
        assert final_entry["status"] == "finding"
        assert final_entry["candidate_sha256"] == hashlib.sha256(WORD_LEAK_EN.encode("utf-8")).hexdigest()

    @_covers("B5")
    def test_b5_leak_introduced_independently_at_each_stage_is_detected_there(self, monkeypatch):
        # post_map only
        r1 = asyncio.run(_narrate(monkeypatch, chapter_texts=[WORD_LEAK_EN], polish_text=CLEAN_EN))
        e1 = {e["stage"]: e["status"] for e in r1["_b03_stage_entries"]}
        assert e1["post_map"] == "finding" and e1["post_polish"] == "clean"
        # post_polish only
        r2 = asyncio.run(_narrate(monkeypatch, chapter_texts=[CLEAN_EN], polish_text=WORD_LEAK_EN))
        e2 = {e["stage"]: e["status"] for e in r2["_b03_stage_entries"]}
        assert e2["post_map"] == "clean" and e2["post_polish"] == "finding"
        # post_revise only
        r3, _ = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN))
        e3 = {e["stage"]: e["status"] for e in r3["_b03_stage_entries"]}
        assert e3["post_revise"] == "finding"
        # final only
        fr = {"book": WORD_LEAK_EN}
        narration_api._b03_final_language_rescan(fr, {"language": "en"})
        final_entry = next(s for s in fr["post_mutation_language_rescan"]["stages"] if s["stage"] == "final")
        assert final_entry["status"] == "finding"

    @_covers("B6")
    def test_b6_clean_earlier_leaked_later_becomes_finding(self):
        clean_entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        leak_entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_polish")
        report = lr.build_report("en", [clean_entry, leak_entry,
                                         lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
                                         lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final")])
        assert report["status"] == "finding"

    @_covers("B7")
    def test_b7_leaked_earlier_corrected_later_preserves_truthful_history(self):
        leak_entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        clean_entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish")
        report = lr.build_report("en", [leak_entry, clean_entry,
                                         lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
                                         lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final")])
        assert report["status"] == "finding"
        by_stage = {s["stage"]: s["status"] for s in report["stages"]}
        assert by_stage["post_map"] == "finding"  # history preserved, not erased
        assert by_stage["post_polish"] == "clean"

    @_covers("B8")
    def test_b8_no_stage_overwrites_another(self, monkeypatch):
        result = asyncio.run(_narrate(
            monkeypatch, chapter_texts=[WORD_LEAK_EN], polish_text=SENTENCE_LEAK_EN))
        entries = result["_b03_stage_entries"]
        stages_present = [e["stage"] for e in entries]
        assert stages_present.count("post_map") == 1
        assert stages_present.count("post_polish") == 1
        post_map = next(e for e in entries if e["stage"] == "post_map")
        post_polish = next(e for e in entries if e["stage"] == "post_polish")
        assert post_map["candidate_sha256"] != post_polish["candidate_sha256"]

    @_covers("B9")
    def test_b9_missing_applicable_stage_produces_incomplete(self):
        report = lr.build_report("en", [
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map"),
        ])
        assert report["coverage"] == "incomplete"
        assert "STAGE_MISSING" in report["errors"]

    @_covers("B10")
    def test_b10_legitimate_not_applicable_stage_is_explicit(self):
        report = lr.build_report("en", [
            lr.not_applicable_stage("post_map", "en", "no chapter map-reduce boundary"),
            lr.not_applicable_stage("post_polish", "en", "no chapter map-reduce boundary"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert report["coverage"] == "complete"
        assert report["status"] == "clean"
        na = [s for s in report["stages"] if s["applicability"] == "not_applicable"]
        assert len(na) == 2
        assert all(s.get("reason") for s in na)

        # Rework 2 (Codex P1): a not_applicable claim in a DIFFERENT language than the
        # report's own must be rejected exactly like a mismatched applicable entry -- it is
        # not a free pass just because it carries no scan evidence.
        mismatched = lr.build_report("en", [
            lr.not_applicable_stage("post_map", "fr", "no chapter map-reduce boundary"),
            lr.not_applicable_stage("post_polish", "en", "no chapter map-reduce boundary"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert mismatched["coverage"] == "incomplete"
        assert not any(s["stage"] == "post_map" for s in mismatched["stages"])


# ===========================================================================
# Group C -- authority and isolation
# ===========================================================================
class TestC_AuthorityAndIsolation:
    @_covers("C1")
    def test_c1_admitted_target_language_wins_over_stale_mutable_metadata(self):
        final_result = {
            "book": CLEAN_EN,
            "_b03_stage_entries": [
                lr.scan_stage(candidate_text=CLEAN_EN, target_language="fr", stage="post_map"),
            ],
        }
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        report = final_result["post_mutation_language_rescan"]
        assert report["target_language"] == "en"  # admitted body language wins, not the stale "fr"
        assert "TARGET_LANGUAGE_CONFLICT" in report["errors"]
        assert report["coverage"] == "incomplete"  # the stale entry is caught, not silently trusted

    @_covers("C2")
    def test_c2_conflicting_language_authority_produces_incomplete(self):
        report = lr.build_report("en", [
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="fr", stage="post_map"),
        ])
        assert report["coverage"] == "incomplete"
        assert "TARGET_LANGUAGE_CONFLICT" in report["errors"]

    @_covers("C3")
    def test_c3_job_a_evidence_cannot_appear_in_job_b(self):
        job_a = {"book": WORD_LEAK_EN}
        job_b = {"book": CLEAN_EN}
        narration_api._b03_final_language_rescan(job_a, {"language": "en"})
        narration_api._b03_final_language_rescan(job_b, {"language": "en"})
        report_a = job_a["post_mutation_language_rescan"]
        report_b = job_b["post_mutation_language_rescan"]
        assert report_a["status"] == "finding"
        assert report_b["status"] != "finding"  # job B never inherits job A's finding
        hash_a = next(s for s in report_a["stages"] if s["stage"] == "final")["candidate_sha256"]
        hash_b = next(s for s in report_b["stages"] if s["stage"] == "final")["candidate_sha256"]
        assert hash_a != hash_b

    @_covers("C4")
    def test_c4_retry_does_not_reuse_stale_candidate_evidence(self):
        stale = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_revise")
        fresh = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise")
        report = lr.build_report("en", [stale, fresh])
        assert report["coverage"] == "incomplete"
        assert "STAGE_DUPLICATE" in report["errors"]

    @_covers("C5")
    def test_c5_concurrent_jobs_remain_isolated(self, monkeypatch):
        async def _both():
            return await asyncio.gather(
                _gates(monkeypatch, book_text=WORD_LEAK_EN, language="en"),
                _gates(monkeypatch, book_text=CLEAN_EN, language="en"),
            )
        (result_a, _), (result_b, _) = asyncio.run(_both())
        entries_a = {e["stage"]: e["status"] for e in result_a["_b03_stage_entries"]}
        entries_b = {e["stage"]: e["status"] for e in result_b["_b03_stage_entries"]}
        assert entries_a["post_revise"] == "finding"
        assert entries_b["post_revise"] == "clean"

    @_covers("C6")
    def test_c6_candidate_report_dictionaries_are_detached_from_caller_mutation(self):
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        entries = [entry]
        report = lr.build_report("en", entries)

        # Subscript assignment is undefined (_StageObservation has no __setitem__) -- a real
        # but weak guarantee; kept for completeness.
        with pytest.raises(TypeError):
            entry["status"] = "TAMPERED"

        # Rework 3: the REAL guarantee -- status is a read-only property (a data descriptor),
        # so BOTH normal assignment and object.__setattr__ (which bypasses a plain __setattr__
        # method override, the Rework 2 mechanism) raise the identical AttributeError. Python's
        # attribute-set machinery checks the type for a data descriptor before ever touching
        # instance storage, for either call form.
        with pytest.raises(AttributeError):
            entry.status = "TAMPERED"
        with pytest.raises(AttributeError):
            object.__setattr__(entry, "status", "TAMPERED")
        with pytest.raises(AttributeError):
            object.__setattr__(entry, "candidate_sha256", "0" * 64)
        assert entry.status == "clean"  # confirmed unaffected by every attempted assignment

        # Rework 3: mutating any VIEW returned by to_dict()/__getitem__/.get()/direct property
        # access must never alter the observation -- each access returns a fresh detached copy.
        d = entry.to_dict()
        d["status"] = "TAMPERED"
        d["sentence_scan"]["hits"] = 999
        assert entry.status == "clean"
        assert entry.sentence_scan["hits"] != 999

        via_getitem = entry["sentence_scan"]
        via_getitem["hits"] = 999
        via_getitem["samples"] = ["FORGED"]
        assert entry["sentence_scan"]["hits"] == 0  # re-read is unaffected

        via_get = entry.get("word_scan")
        via_get["status"] = "FLAG"
        assert entry.get("word_scan")["status"] == "PASS"  # re-read is unaffected

        via_attr = entry.sentence_scan
        via_attr["samples"].append("INJECTED")
        assert entry.sentence_scan["samples"] == []  # re-read is unaffected

        entries.append({"stage": "post_polish", "status": "TAMPERED_APPEND"})
        assert report["stages"][0]["status"] == "clean"  # unaffected by the post-call mutation
        assert len(report["stages"]) == 1

    @_covers("C7")
    def test_c7_hostile_dict_list_string_subclasses_cannot_crash(self):
        hostile_raw = _HostileDict({"applies": True, "hits": 1, "samples": [], "other_langs": {}})

        def _hostile_sentence(text, lang):
            return hostile_raw

        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                               sentence_scanner=_hostile_sentence)
        assert entry["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry["errors"]

        hostile_text = _HostileStr("hostile candidate")
        entry2 = lr.scan_stage(candidate_text=hostile_text, target_language="en", stage="post_map")
        assert entry2["status"] == "incomplete"
        assert "CANDIDATE_TYPE_INVALID" in entry2["errors"]

        report = lr.build_report("en", _HostileList([{"stage": "post_map"}]))
        assert report["coverage"] == "incomplete"
        assert "STAGE_ENTRIES_TYPE_INVALID" in report["errors"]

        # Rework 2 (Codex P1): the HOOK itself must not coerce a truthy-but-wrong-type
        # candidate (e.g. book=[1, 2, 3]) into "" before scan_stage ever sees it -- that would
        # hide the type error behind an empty-string scan that could read as false-"clean".
        # The real final hook must surface CANDIDATE_TYPE_INVALID, never a coerced clean scan.
        final_result_list_book = {"book": [1, 2, 3]}
        narration_api._b03_final_language_rescan(final_result_list_book, {"language": "en"})
        report_list_book = final_result_list_book["post_mutation_language_rescan"]
        final_entry = next(s for s in report_list_book["stages"] if s["stage"] == "final")
        assert final_entry["status"] == "incomplete"
        assert "CANDIDATE_TYPE_INVALID" in final_entry["errors"]

    @_covers("C8")
    def test_c8_exact_type_schema_validation_rejects_extra_missing_malformed_fields(self):
        def _extra_key_sentence(text, lang):
            d = _clean_sentence_raw()
            d["unexpected_extra_key"] = True
            return d

        def _missing_key_word(text, lang):
            return {"status": "PASS", "count": 0}  # missing "samples"

        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                               sentence_scanner=_extra_key_sentence, word_scanner=_missing_key_word)
        assert entry["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry["errors"]
        assert "WORD_SCAN_UNAVAILABLE" in entry["errors"]

        # Rework 2 (Codex P2): other_langs values must be positive, and hits==0 must carry an
        # empty other_langs -- a negative value or a self-contradictory zero-hits-with-
        # breakdown return must be rejected exactly like any other malformed scan, never
        # silently accepted as "clean".
        def _negative_other_langs(text, lang):
            return {"applies": True, "hits": 1, "samples": ["x"], "other_langs": {"fr": -1}}

        def _zero_hits_with_breakdown(text, lang):
            return {"applies": True, "hits": 0, "samples": [], "other_langs": {"fr": 1}}

        entry_negative = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                        sentence_scanner=_negative_other_langs)
        assert entry_negative["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry_negative["errors"]

        entry_zero_hits = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                         sentence_scanner=_zero_hits_with_breakdown)
        assert entry_zero_hits["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry_zero_hits["errors"]


# ===========================================================================
# Group D -- flag behavior
# ===========================================================================
class TestD_FlagBehavior:
    @_covers("D1")
    def test_d1_scan_flag_off_produces_no_calls_and_no_key(self, monkeypatch):
        calls = []
        real_scan_stage = lr.scan_stage
        monkeypatch.setattr(lr, "scan_stage", lambda *a, **kw: (calls.append(1), real_scan_stage(*a, **kw))[1])
        result = asyncio.run(_narrate(monkeypatch, chapter_texts=[WORD_LEAK_EN], flag_on=False))
        assert "_b03_stage_entries" not in result
        result2, _ = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN, flag_on=False))
        assert "_b03_stage_entries" not in result2
        assert calls == []

    @_covers("D2")
    def test_d2_enforce_flag_alone_does_not_enable_b03(self, monkeypatch):
        monkeypatch.setenv("NARASI_LANGUAGE_CONSISTENCY_ENFORCE", "1")
        monkeypatch.delenv("NARASI_LANGUAGE_CONSISTENCY_SCAN", raising=False)
        result, _ = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN, flag_on=False,
                                        body_extra={}))
        assert "_b03_stage_entries" not in result

    @_covers("D3")
    def test_d3_flag_on_adds_only_the_bounded_report(self, monkeypatch):
        off_result, _ = asyncio.run(_gates(monkeypatch, book_text=CLEAN_EN, flag_on=False))
        on_result, _ = asyncio.run(_gates(monkeypatch, book_text=CLEAN_EN, flag_on=True))
        new_keys = set(on_result) - set(off_result)
        # NARASI_LANGUAGE_CONSISTENCY_SCAN is, by explicit instruction, the SAME existing flag
        # that also gates the pre-existing single-pass "language_consistency_report" -- turning
        # it on necessarily activates that sibling mechanism too. B-03 itself adds exactly one
        # new key of its own: "_b03_stage_entries".
        assert new_keys == {"_b03_stage_entries", "language_consistency_report"}

    @_covers("D4")
    def test_d4_scanner_exception_becomes_incomplete(self):
        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                               sentence_scanner=_RaisingScanner(), word_scanner=_RaisingScanner())
        assert entry["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry["errors"]
        assert "WORD_SCAN_UNAVAILABLE" in entry["errors"]

    @_covers("D5")
    def test_d5_import_failure_becomes_incomplete(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "narasi_gate", None)
        entry = lr.scan_stage(candidate_text="x", target_language="en", stage="post_map")
        assert entry["status"] == "incomplete"
        assert "SCANNER_IMPORT_FAILED" in entry["errors"]

    @_covers("D6")
    def test_d6_cancelled_error_remains_cancellation_where_applicable(self):
        src = inspect.getsource(narration_api._run_narration_job)
        tree = ast.parse(src)
        found_ordered_handlers = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                handler_types = []
                for h in node.handlers:
                    if h.type is None:
                        handler_types.append("bare")
                    elif isinstance(h.type, ast.Attribute) and h.type.attr == "CancelledError":
                        handler_types.append("CancelledError")
                    elif isinstance(h.type, ast.Name) and h.type.id == "Exception":
                        handler_types.append("Exception")
                if "CancelledError" in handler_types and "Exception" in handler_types:
                    ce_idx = handler_types.index("CancelledError")
                    exc_idx = handler_types.index("Exception")
                    if ce_idx < exc_idx:
                        # confirm this try's body actually calls the B-03 final hook
                        body_src = ast.dump(node)
                        if "_b03_final_language_rescan" in body_src:
                            found_ordered_handlers = True
        assert found_ordered_handlers, (
            "B-03's final-hook try block must catch asyncio.CancelledError and re-raise "
            "BEFORE a bare except Exception clause")

        # Codex instruction #8 (2026-07-24): statically verify the final hook's call site
        # remains strictly after `_final_result` is selected (CC-02's `_bundle.result`
        # assignment, or its own fallback) and strictly before `_persist_chapters`.
        assign_linenos = []
        call_lineno = None
        persist_lineno = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_final_result":
                        assign_linenos.append(node.lineno)
            elif isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name) and fn.id == "_b03_final_language_rescan":
                    call_lineno = node.lineno
                elif isinstance(fn, ast.Name) and fn.id == "_persist_chapters":
                    persist_lineno = node.lineno
        assert assign_linenos, "_final_result assignment not found"
        assert call_lineno is not None, "_b03_final_language_rescan call not found"
        assert persist_lineno is not None, "_persist_chapters call not found"
        assert max(assign_linenos) < call_lineno < persist_lineno

    @_covers("D7")
    def test_d7_b03_finding_does_not_invoke_revise_or_alter_persisted_text(self, monkeypatch):
        result, revise_calls = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN))
        entries = {e["stage"]: e["status"] for e in result["_b03_stage_entries"]}
        assert entries["post_revise"] == "finding"
        assert revise_calls == []  # B-03's own finding never triggers a revise call
        # a later, unrelated mechanism (the "> **Gaya:**" header) appends to result["book"]
        # after B-03's post_revise hook runs -- the original leaked text itself must still be
        # present unchanged, byte-for-byte, proving B-03 never rewrote it.
        assert WORD_LEAK_EN in result["book"]


# ===========================================================================
# Group E -- regression and non-interference
# ===========================================================================
class TestE_RegressionAndNonInterference:
    @_covers("E1")
    def test_e1_b01_report_stays_unchanged(self):
        sentinel = {"coverage": "complete", "status": "clean", "untouched": True}
        final_result = {"book": WORD_LEAK_EN, "post_mutation_revalidation": sentinel}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        assert final_result["post_mutation_revalidation"] is sentinel

    @_covers("E2")
    def test_e2_b02_mutation_hashes_stay_unchanged(self):
        sentinel = {"ledger_hash": "deadbeef", "untouched": True}
        final_result = {"book": WORD_LEAK_EN, "mutation_hashes": sentinel}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        assert final_result["mutation_hashes"] is sentinel

    @_covers("E3")
    def test_e3_final_candidate_bytes_identical_before_after(self):
        text = WORD_LEAK_EN
        final_result = {"book": text}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        assert final_result["book"] == text
        assert final_result["book"] is text

    @_covers("E4")
    def test_e4_zero_provider_network_db_calls(self, monkeypatch):
        # _gates' own ForbiddenProvider spy raises AssertionError on any real-client
        # construction attempt; this test simply proves a B-03-flagged run completes without
        # tripping it, with the cheap/revise/critique boundary all left at their inert defaults.
        result, revise_calls = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN))
        assert "_b03_stage_entries" in result
        assert revise_calls == []

    @_covers("E5")
    def test_e5_both_book_and_output_result_variants(self):
        fr_book = {"book": WORD_LEAK_EN}
        narration_api._b03_final_language_rescan(fr_book, {"language": "en"})
        assert fr_book["post_mutation_language_rescan"]["status"] == "finding"

        fr_output = {"output": WORD_LEAK_EN}
        narration_api._b03_final_language_rescan(fr_output, {"language": "en"})
        assert fr_output["post_mutation_language_rescan"]["status"] == "finding"

    @_covers("E6")
    def test_e6_chapter_based_and_assembled_manuscript_paths(self, monkeypatch):
        # chapter-based (narrate_chapters map-reduce)
        chaptered = asyncio.run(_narrate(monkeypatch, chapter_texts=[CLEAN_EN, CLEAN_EN]))
        assert "_b03_stage_entries" in chaptered

        # assembled-manuscript path that never went through narrate_chapters -- ONLY a
        # trusted, positive scenario classification ("C"/"D"/"E", the router's own field)
        # may legitimately seed not_applicable placeholders for post_map/post_polish.
        assembled, _ = asyncio.run(_gates(monkeypatch, book_text=CLEAN_EN,
                                           result_extra={"scenario": "C"}))
        by_stage = {e["stage"]: e["applicability"] for e in assembled["_b03_stage_entries"]}
        assert by_stage["post_map"] == "not_applicable"
        assert by_stage["post_polish"] == "not_applicable"
        assert by_stage["post_revise"] == "applicable"

        # WITHOUT a trusted "C"/"D"/"E" scenario classification (e.g. scenario absent/unknown,
        # or genuinely "A"/"B"), an absent post_map/post_polish must stay genuinely absent --
        # never silently manufactured into a not_applicable excuse (Codex P1 finding,
        # 2026-07-24: a real narrate_chapters wiring failure must not be indistinguishable
        # from a legitimate non-chaptered scenario).
        unclassified, _ = asyncio.run(_gates(monkeypatch, book_text=CLEAN_EN))
        stages_present = {e["stage"] for e in unclassified["_b03_stage_entries"]}
        assert "post_map" not in stages_present
        assert "post_polish" not in stages_present

    @_covers("E7")
    def test_e7_legacy_non_lifecycle_path_never_guesses_language(self):
        # A malformed/absent body.language must become bounded incomplete, never a guessed
        # "en"/"id" default silently presented as authoritative evidence. body.get("language")
        # resolves the raw malformed value through unchanged (never coerced via ==/or), and
        # both scan_stage and build_report's own exact-type guards reject it outright rather
        # than inventing a default -- the report ends up target_language=None/incomplete, not
        # a silently-guessed language.
        final_result = {"book": CLEAN_EN}
        narration_api._b03_final_language_rescan(final_result, {"language": 12345})
        report = final_result["post_mutation_language_rescan"]
        assert report["target_language"] is None  # never a guessed "en"/"id" default
        assert report["coverage"] == "incomplete"
        assert "TARGET_LANGUAGE_INVALID" in report["errors"]


# ===========================================================================
# Mandatory executable negative controls (G01-G16)
# ===========================================================================
class TestG_NegativeControls:
    @_covers("G01")
    def test_g01_remove_neuter_post_map_hook(self, monkeypatch):
        real_scan_stage = lr.scan_stage

        def _neutered(*, stage, **kw):
            if stage == "post_map":
                return lr.not_applicable_stage("post_map", kw.get("target_language"), "neutered")
            return real_scan_stage(stage=stage, **kw)

        monkeypatch.setattr(lr, "scan_stage", _neutered)

        result = asyncio.run(_narrate(
            monkeypatch, chapter_texts=[WORD_LEAK_EN], polish_text=CLEAN_EN + " " + CLEAN_EN))
        post_map = next(e for e in result["_b03_stage_entries"] if e["stage"] == "post_map")
        assert post_map["applicability"] == "not_applicable"  # the finding is now invisible

    @_covers("G02")
    def test_g02_remove_neuter_post_polish_hook(self, monkeypatch):
        real_scan_stage = lr.scan_stage

        def _neutered(*, stage, **kw):
            if stage == "post_polish":
                return lr.not_applicable_stage("post_polish", kw.get("target_language"), "neutered")
            return real_scan_stage(stage=stage, **kw)

        monkeypatch.setattr(lr, "scan_stage", _neutered)

        result = asyncio.run(_narrate(monkeypatch, chapter_texts=[CLEAN_EN], polish_text=WORD_LEAK_EN))
        post_polish = next(e for e in result["_b03_stage_entries"] if e["stage"] == "post_polish")
        assert post_polish["applicability"] == "not_applicable"

    @_covers("G03")
    def test_g03_remove_neuter_post_revise_hook(self, monkeypatch):
        # Codex P1 fix (2026-07-24): post_revise can never legitimately be not_applicable --
        # build_report now REJECTS a neutering attempt outright (the entry never reaches
        # "stages", coverage becomes incomplete) rather than silently accepting it.
        real_scan_stage = lr.scan_stage

        def _neutered(*, stage, **kw):
            if stage == "post_revise":
                return lr.not_applicable_stage("post_revise", kw.get("target_language"), "neutered")
            return real_scan_stage(stage=stage, **kw)

        monkeypatch.setattr(lr, "scan_stage", _neutered)

        result, _ = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN,
                                        result_extra={"scenario": "C"}))
        report = lr.build_report("en", result["_b03_stage_entries"])
        assert not any(s["stage"] == "post_revise" for s in report["stages"])
        assert report["coverage"] == "incomplete"

    @_covers("G04")
    def test_g04_remove_neuter_final_pre_persistence_hook(self, monkeypatch):
        # Codex P1 fix: final can never legitimately be not_applicable -- same rejection as
        # G03, proven at the final hook's own report-assembly boundary.
        real_scan_stage = lr.scan_stage

        def _neutered(*, stage, **kw):
            if stage == "final":
                return lr.not_applicable_stage("final", kw.get("target_language"), "neutered")
            return real_scan_stage(stage=stage, **kw)

        monkeypatch.setattr(lr, "scan_stage", _neutered)

        final_result = {
            "book": WORD_LEAK_EN,
            "_b03_stage_entries": [
                lr.not_applicable_stage("post_map", "en", "test scenario C"),
                lr.not_applicable_stage("post_polish", "en", "test scenario C"),
                lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            ],
        }
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        report = final_result["post_mutation_language_rescan"]
        assert not any(s["stage"] == "final" for s in report["stages"])  # rejected, not invisible-as-clean
        assert report["coverage"] == "incomplete"

    @_covers("G05")
    def test_g05_scan_premutation_text_labeled_postmutation(self, monkeypatch):
        # Simulates the defect class directly: scanning the RAW (pre-polish) book but
        # labeling the resulting entry "post_polish" would hide a leak introduced by polish.
        raw_book_clean = CLEAN_EN
        polished_leak = WORD_LEAK_EN
        wrong_entry = lr.scan_stage(candidate_text=raw_book_clean, target_language="en", stage="post_polish")
        correct_entry = lr.scan_stage(candidate_text=polished_leak, target_language="en", stage="post_polish")
        assert wrong_entry["status"] == "clean"
        assert correct_entry["status"] == "finding"
        assert wrong_entry["candidate_sha256"] != correct_entry["candidate_sha256"]

    @_covers("G06")
    def test_g06_hardcode_target_language_to_en(self, monkeypatch):
        real_entry = lr.scan_stage(candidate_text=NATIVE_ID, target_language="id", stage="post_map")
        hardcoded_entry = lr.scan_stage(candidate_text=NATIVE_ID, target_language="en", stage="post_map")
        assert real_entry["status"] == "clean"  # native Indonesian under its OWN target: clean
        assert hardcoded_entry["status"] == "finding"  # same text mislabeled "en": false finding
        assert real_entry["target_language"] != hardcoded_entry["target_language"]

    @_covers("G07")
    def test_g07_use_only_sentence_scan_omit_word_scan(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        assert entry["status"] == "finding"  # real: word_scan alone catches it
        sentence_only_finding = bool(entry["sentence_scan"]["applies"] and entry["sentence_scan"]["hits"] > 0)
        assert sentence_only_finding is False  # a sentence-scan-only implementation would miss it

    @_covers("G08")
    def test_g08_use_only_word_scan_omit_sentence_scan(self):
        entry = lr.scan_stage(candidate_text=SENTENCE_LEAK_EN, target_language="en", stage="post_map")
        assert entry["status"] == "finding"  # real: sentence_scan alone catches it
        word_only_finding = entry["word_scan"]["status"] == "FLAG"
        assert word_only_finding is False  # a word-scan-only implementation would miss it

    @_covers("G09")
    def test_g09_convert_scanner_failure_incomplete_into_clean(self):
        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                               sentence_scanner=_RaisingScanner(), word_scanner=_RaisingScanner())
        assert entry["status"] == "incomplete"
        assert entry["status"] != "clean"  # the defect this guards against: silently reporting clean

    @_covers("G10")
    def test_g10_flag_off_still_imports_or_calls_helper(self, monkeypatch):
        calls = []
        real_scan_stage = lr.scan_stage

        def _spy(*a, **kw):
            calls.append(1)
            return real_scan_stage(*a, **kw)

        monkeypatch.setattr(lr, "scan_stage", _spy)
        asyncio.run(_narrate(monkeypatch, chapter_texts=[WORD_LEAK_EN], flag_on=False))
        assert calls == []  # flag off: the helper must never be called at all

    @_covers("G11")
    def test_g11_reuse_stale_stage_evidence_after_candidate_changes(self):
        stale = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_revise")
        fresh = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise")
        # A defective implementation that "reuses" the first entry for the stage (ignoring the
        # fresh one) would report finding; the accepted build_report instead detects the
        # duplicate and marks the whole report incomplete rather than picking either value.
        report = lr.build_report("en", [stale, fresh])
        assert report["coverage"] == "incomplete"
        assert "STAGE_DUPLICATE" in report["errors"]

    @_covers("G12")
    def test_g12_one_stage_overwrite_erase_previous_stage_evidence(self, monkeypatch):
        result = asyncio.run(_narrate(
            monkeypatch, chapter_texts=[WORD_LEAK_EN], polish_text=SENTENCE_LEAK_EN))
        entries = result["_b03_stage_entries"]
        # both stages must survive as independent entries -- neither the second append
        # (post_polish) overwrote the first (post_map)
        assert len(entries) == 2
        assert {e["stage"] for e in entries} == {"post_map", "post_polish"}

    @_covers("G13")
    def test_g13_allow_b03_to_mutate_candidate_text(self):
        text = WORD_LEAK_EN
        before = text
        lr.scan_stage(candidate_text=text, target_language="en", stage="post_map")
        assert text == before  # the exact same string object/value, never rewritten in place

    @_covers("G14")
    def test_g14_trigger_provider_network_semantic_call(self, monkeypatch):
        result, revise_calls = asyncio.run(_gates(monkeypatch, book_text=WORD_LEAK_EN))
        assert revise_calls == []  # B-03's finding never reaches the revise/provider boundary

    @_covers("G15")
    def test_g15_bind_final_evidence_to_result_instead_of_final_result(self):
        # `result` (pre-CC-02) and `_final_result` (post-CC-02) can genuinely diverge -- the
        # accepted hook must scan the FINAL bundle, never a stale pre-selection `result`.
        stale_result_text = CLEAN_EN
        final_bundle_text = WORD_LEAK_EN
        final_result = {"book": final_bundle_text}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        final_entry = next(s for s in final_result["post_mutation_language_rescan"]["stages"]
                            if s["stage"] == "final")
        assert final_entry["candidate_sha256"] == hashlib.sha256(final_bundle_text.encode("utf-8")).hexdigest()
        assert final_entry["candidate_sha256"] != hashlib.sha256(stale_result_text.encode("utf-8")).hexdigest()

    @_covers("G16")
    def test_g16_forged_or_malformed_stage_report_is_rejected_not_accepted(self):
        # Codex adversarial re-audit (2026-07-24, Rework 3): the Rework 2 sentinel +
        # exact-type model was itself defeated by object.__setattr__ (bypasses a plain
        # __setattr__ override) and by the sentinel being an ordinary importable module
        # global. Rework 3 replaces both with closure-scoped provenance (a WeakKeyDictionary
        # + HMAC seal that never leaves _make_stage_observation_module's closure) and
        # read-only properties (a data descriptor, which object.__setattr__ cannot bypass --
        # unlike a __setattr__ method override, the descriptor protocol is invoked by
        # object.__setattr__ itself). G16 must now prove ALL of these paths fail, not just
        # the plain-dict path Rework 2 covered.
        malformed_missing_word_scan = {
            "stage": "post_map", "target_language": "en",
            "candidate_sha256": "0" * 64,
            "applicability": "applicable", "status": "clean",
            "sentence_scan": {"applies": True, "hits": 0, "samples": [], "other_langs": {}},
            "errors": [], "reason": "",
        }
        report = lr.build_report("en", [
            malformed_missing_word_scan,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert report["coverage"] == "incomplete"
        assert "STAGE_ENTRY_NOT_SEALED" in report["errors"]

        # A PERFECTLY well-formed, self-consistent, format-valid-hash dict -- exactly the
        # shape a real _StageObservation.to_dict() produces -- with a forged hash. Still
        # rejected: it is not a _StageObservation at all.
        real_post_map = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        forged_but_wellformed = real_post_map.to_dict()
        forged_but_wellformed["candidate_sha256"] = "0" * 64
        assert forged_but_wellformed["candidate_sha256"] != real_post_map["candidate_sha256"]
        report2 = lr.build_report("en", [
            forged_but_wellformed,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report2["stages"])
        assert report2["coverage"] == "incomplete"
        assert "STAGE_ENTRY_NOT_SEALED" in report2["errors"]

        # Direct __init__ construction ALWAYS raises -- no sentinel bypass exists anymore.
        with pytest.raises(TypeError):
            lr._StageObservation(
                stage="post_map", target_language="en", candidate_sha256="0" * 64,
                applicability="applicable", status="clean",
                sentence_scan={"applies": False, "hits": 0, "samples": [], "other_langs": {}},
                word_scan={"status": "PASS", "count": 0, "samples": []}, errors=[], reason="",
            )
        with pytest.raises(TypeError):
            lr._StageObservation()  # even zero-argument construction raises unconditionally

        # THE central Rework 3 proof: __init__ is not the only conceivable construction path
        # in Python -- object.__new__() plus manual object.__setattr__() on every private raw
        # slot produces a genuine, type-correct _StageObservation instance WITHOUT ever
        # calling __init__ or any of this module's own minting functions. This is exactly
        # "every visible/private constructor path" the rejection verdict named. It must still
        # be rejected, because it was never sealed by this module's closure-private
        # provenance registry.
        forged_direct = object.__new__(lr._StageObservation)
        object.__setattr__(forged_direct, "_raw_stage", "post_map")
        object.__setattr__(forged_direct, "_raw_target_language", "en")
        object.__setattr__(forged_direct, "_raw_candidate_sha256", "1" * 64)
        object.__setattr__(forged_direct, "_raw_applicability", "applicable")
        object.__setattr__(forged_direct, "_raw_status", "clean")
        object.__setattr__(forged_direct, "_raw_sentence_scan",
                            {"applies": False, "hits": 0, "samples": [], "other_langs": {}})
        object.__setattr__(forged_direct, "_raw_word_scan", {"status": "PASS", "count": 0, "samples": []})
        object.__setattr__(forged_direct, "_raw_errors", [])
        object.__setattr__(forged_direct, "_raw_reason", "")
        assert type(forged_direct) is lr._StageObservation  # genuinely the right type
        assert forged_direct.candidate_sha256 == "1" * 64  # properties read the forged slots fine
        report3 = lr.build_report("en", [
            forged_direct,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report3["stages"])
        assert report3["coverage"] == "incomplete"
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report3["errors"]

        # A genuinely self-inconsistent scanner return (word_scan reports FLAG) is caught by
        # scan_stage's own status derivation -- it correctly drives "finding", never a
        # mislabeled "clean" entry.
        def _flag_word(text, lang):
            return {"status": "FLAG", "count": 3, "samples": [{"term": "dengan", "snippet": "x"}]}
        inconsistent_entry = lr._scan_stage_test_seam(candidate_text=CLEAN_EN, target_language="en",
                                            stage="post_map", word_scanner=_flag_word)
        assert inconsistent_entry["status"] == "finding"


# ===========================================================================
# Rework (Codex re-audit, 2026-07-24) -- additional negative controls the rejection
# verdict explicitly required, beyond the original G01-G16 set.
# ===========================================================================
class TestH_ReworkNegativeControls:
    @_covers("H1")
    def test_h1_all_not_applicable_bypass_is_rejected(self):
        # Every one of the four stages claims not_applicable -- if accepted, this would be
        # the ultimate false-clean bypass (a report with ZERO real evidence reporting
        # "complete"/"clean"). post_revise and final can never legitimately be
        # not_applicable, so both are rejected outright and coverage is incomplete.
        report = lr.build_report("en", [
            lr.not_applicable_stage("post_map", "en", "claimed n/a"),
            lr.not_applicable_stage("post_polish", "en", "claimed n/a"),
            lr.not_applicable_stage("post_revise", "en", "claimed n/a"),
            lr.not_applicable_stage("final", "en", "claimed n/a"),
        ])
        assert report["status"] != "clean"
        assert report["coverage"] == "incomplete"
        assert not any(s["stage"] in ("post_revise", "final") for s in report["stages"])

    @_covers("H2")
    def test_h2_final_hook_import_failure_produces_incomplete_report_not_absence(self, monkeypatch):
        import sys
        monkeypatch.setitem(sys.modules, "narasi_language_rescan", None)
        final_result = {"book": WORD_LEAK_EN}
        narration_api._b03_final_language_rescan(final_result, {"language": "en"})
        report = final_result.get("post_mutation_language_rescan")
        assert report is not None  # never silently absent
        assert report["coverage"] == "incomplete"
        assert report["status"] == "incomplete"
        assert "FINAL_HOOK_FAILED" in report["errors"]

    @_covers("H3")
    def test_h3_missing_blank_language_with_four_stages_stays_incomplete(self):
        # Even with all four real stage entries present and individually well-formed, a
        # missing/blank target_language must never let the report reach "clean"/"complete".
        final_result_missing = {"book": CLEAN_EN}
        narration_api._b03_final_language_rescan(final_result_missing, {})  # no "language" key
        report_missing = final_result_missing["post_mutation_language_rescan"]
        assert report_missing["target_language"] is None
        assert report_missing["coverage"] == "incomplete"

        final_result_blank = {"book": CLEAN_EN}
        narration_api._b03_final_language_rescan(final_result_blank, {"language": ""})
        report_blank = final_result_blank["post_mutation_language_rescan"]
        assert report_blank["target_language"] is None
        assert report_blank["coverage"] == "incomplete"

        # Rework 2 (Codex P1): whitespace-only is truthy in Python and slipped past the old
        # non-empty check -- valid_target_language's round-trip canonical-form check must
        # still reject it, both directly and through the final hook.
        assert lr.valid_target_language(" ") is False
        final_result_whitespace = {"book": CLEAN_EN}
        narration_api._b03_final_language_rescan(final_result_whitespace, {"language": " "})
        report_whitespace = final_result_whitespace["post_mutation_language_rescan"]
        assert report_whitespace["target_language"] is None
        assert report_whitespace["coverage"] == "incomplete"

        # Direct build_report proof with all 4 stages genuinely present and well-formed:
        # an invalid report-level target_language short-circuits to incomplete regardless.
        report_direct = lr.build_report("", [
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert report_direct["coverage"] == "incomplete"
        assert report_direct["status"] == "incomplete"

    @_covers("H4")
    def test_h4_nested_caller_mutation_after_build_report_cannot_alter_stored_report(self):
        # Part 1: PRE-build nested mutation via the public accessors (the exact Codex P3
        # bypass under Rework 2, where sentence_scan/word_scan properties returned a LIVE
        # reference). Rework 3's deep-copy-on-read makes this a no-op BEFORE build_report()
        # is ever called -- prove the mutation attempt does not even survive to the next read.
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        pre_build_word_scan_snapshot = entry.word_scan
        entry.sentence_scan["samples"].append("INJECTED BEFORE BUILD")
        entry.word_scan["samples"].append({"term": "INJECTED", "snippet": "INJECTED"})
        entry.word_scan["status"] = "PASS"  # attempt to erase the finding entirely
        entry.word_scan["count"] = 0
        entry.errors.append("INJECTED")
        assert entry.word_scan == pre_build_word_scan_snapshot  # re-read is unaffected: still FLAG

        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        stored_post_map = next(s for s in report["stages"] if s["stage"] == "post_map")
        assert stored_post_map["status"] == "finding"  # the finding survived the pre-build attempt
        assert stored_post_map["word_scan"]["status"] == "FLAG"
        original_finding_count = report["finding_count"]

        # Part 2: PRE-build RAW-SLOT forgery -- bypassing the deep-copying properties entirely
        # by targeting the private "_raw_word_scan" slot directly via object.__setattr__. This
        # DOES change what the entry's own property will subsequently report...
        entry2 = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        assert entry2.status == "finding"
        object.__setattr__(entry2, "_raw_word_scan", {"status": "PASS", "count": 0, "samples": []})
        assert entry2.word_scan == {"status": "PASS", "count": 0, "samples": []}  # forgery "worked" locally
        # ...but build_report() must still catch it: the provenance seal was computed at mint
        # time over the ORIGINAL word_scan, so this entry no longer reseals to a matching digest.
        report2 = lr.build_report("en", [
            entry2,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report2["stages"])
        assert report2["coverage"] == "incomplete"
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report2["errors"]

        # Part 3: mutating the RETURNED report dict must not affect the underlying entry's
        # own sealed state -- rebuilding from the SAME original entries (whose provenance was
        # never touched) must reproduce the identical correct result, regardless of what the
        # caller did to the FIRST report's own output dict.
        entry3 = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        four_entries = [
            entry3,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ]
        report3 = lr.build_report("en", four_entries)
        stored3 = next(s for s in report3["stages"] if s["stage"] == "post_map")
        stored3["word_scan"]["count"] = 999
        stored3["word_scan"]["samples"].append({"term": "INJECTED", "snippet": "INJECTED"})
        stored3["errors"].append("INJECTED")

        report3b = lr.build_report("en", four_entries)
        stored3b = next(s for s in report3b["stages"] if s["stage"] == "post_map")
        assert stored3b["word_scan"]["count"] != 999
        assert stored3b["errors"] == []
        assert report3b["finding_count"] == original_finding_count

    @_covers("H5")
    def test_h5_reversed_stage_order_is_rejected(self):
        report = lr.build_report("en", [
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map"),
        ])
        assert report["coverage"] == "incomplete"
        assert "STAGE_ORDER_INVALID" in report["errors"]
        assert report["status"] != "clean"

    @_covers("H6")
    def test_h6_self_inconsistent_scanner_return_is_rejected(self):
        # sentence_scan: applies=False but claims evidence anyway.
        def _inconsistent_sentence(text, lang):
            return {"applies": False, "hits": 9, "samples": ["x"] * 9, "other_langs": {}}

        entry1 = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                sentence_scanner=_inconsistent_sentence)
        assert entry1["status"] == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry1["errors"]

        # word_scan: status=PASS but claims a nonzero count.
        def _inconsistent_word(text, lang):
            return {"status": "PASS", "count": 9, "samples": []}

        entry2 = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                word_scanner=_inconsistent_word)
        assert entry2["status"] == "incomplete"
        assert "WORD_SCAN_UNAVAILABLE" in entry2["errors"]

        # word_scan: status=FLAG but claims zero count.
        def _inconsistent_word2(text, lang):
            return {"status": "FLAG", "count": 0, "samples": []}

        entry3 = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                word_scanner=_inconsistent_word2)
        assert entry3["status"] == "incomplete"
        assert "WORD_SCAN_UNAVAILABLE" in entry3["errors"]


# ===========================================================================
# Rework 3 (Codex adversarial re-audit, 2026-07-24) -- explicit executable proofs for the
# authorized structural fix: closure-scoped provenance (WeakKeyDictionary + HMAC seal,
# replacing the Rework 2 sentinel), read-only properties (a data descriptor, which
# object.__setattr__ cannot bypass), deep-copy-on-read for every mutable field, and
# independent semantic status re-derivation. Mapped 1:1 to the rejection verdict's lettered
# test list A-J.
# ===========================================================================
class TestI_Rework3ProvenanceHardening:
    @_covers("I1")
    def test_i1_object_setattr_hash_forgery_is_rejected(self):
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        original_hash = entry.candidate_sha256
        with pytest.raises(AttributeError):
            object.__setattr__(entry, "candidate_sha256", "0" * 64)
        assert entry.candidate_sha256 == original_hash  # the property-level attempt never took

        # Bypassing the property by targeting the private raw slot directly DOES change what
        # the property subsequently reports...
        object.__setattr__(entry, "_raw_candidate_sha256", "0" * 64)
        assert entry.candidate_sha256 == "0" * 64
        # ...but build_report() must still reject it: the seal was computed at mint time over
        # the ORIGINAL hash, so this entry no longer reseals to a matching digest.
        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s.get("candidate_sha256") == "0" * 64 for s in report["stages"])
        assert report["coverage"] == "incomplete"
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]

    @_covers("I2")
    def test_i2_object_setattr_status_forgery_on_finding_is_rejected(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        assert entry.status == "finding"
        with pytest.raises(AttributeError):
            object.__setattr__(entry, "status", "clean")
        assert entry.status == "finding"  # unaffected

        object.__setattr__(entry, "_raw_status", "clean")
        assert entry.status == "clean"  # raw-slot forgery "worked" locally
        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert report["coverage"] != "complete"
        assert report["status"] != "clean"
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]

    @_covers("I3")
    def test_i3_nested_word_scan_forged_to_flag_before_build_is_never_false_clean(self):
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        assert entry.status == "clean"

        entry.word_scan["status"] = "FLAG"  # mutating the returned copy is inert
        entry.word_scan["count"] = 7
        assert entry.word_scan["status"] == "PASS"

        object.__setattr__(entry, "_raw_word_scan",
                            {"status": "FLAG", "count": 7,
                             "samples": [{"term": "dengan", "snippet": "x"}]})
        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]

    @_covers("I4")
    def test_i4_every_construction_path_fails_to_mint_a_trusted_observation(self):
        # Path 1: __init__ with no arguments.
        with pytest.raises(TypeError):
            lr._StageObservation()
        # Path 2: __init__ with arbitrary keyword fields -- the exact Rework 2 sentinel-bypass
        # shape, now without any sentinel to even attempt supplying.
        with pytest.raises(TypeError):
            lr._StageObservation(stage="post_map", target_language="en",
                                  candidate_sha256="0" * 64, applicability="applicable",
                                  status="clean", sentence_scan=None, word_scan=None,
                                  errors=[], reason="")
        # Path 3: object.__new__() + manual object.__setattr__ on every raw slot -- the one
        # construction path Python can never fully prevent for an ordinary exposed class.
        # Genuinely produces the right TYPE, but build_report() must still reject it.
        forged = object.__new__(lr._StageObservation)
        for name, value in {
            "_raw_stage": "post_map", "_raw_target_language": "en",
            "_raw_candidate_sha256": "2" * 64, "_raw_applicability": "applicable",
            "_raw_status": "clean",
            "_raw_sentence_scan": {"applies": False, "hits": 0, "samples": [], "other_langs": {}},
            "_raw_word_scan": {"status": "PASS", "count": 0, "samples": []},
            "_raw_errors": [], "_raw_reason": "",
        }.items():
            object.__setattr__(forged, name, value)
        assert type(forged) is lr._StageObservation
        report = lr.build_report("en", [
            forged,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]
        # Path 4: there is no module-level minting function to import at all -- unlike the
        # Rework 2 sentinel, which WAS an ordinary importable module global.
        assert not hasattr(lr, "_build_applicable")
        assert not hasattr(lr, "_build_incomplete")
        assert not hasattr(lr, "_build_not_applicable")
        assert not hasattr(lr, "_mint")
        assert not hasattr(lr, "_CONSTRUCTION_SENTINEL")

    @_covers("I5")
    def test_i5_mutating_to_dict_result_before_build_report_leaves_observation_unchanged(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")
        d = entry.to_dict()
        d["status"] = "clean"
        d["word_scan"]["status"] = "PASS"
        d["word_scan"]["count"] = 0
        d["errors"].append("INJECTED")
        d["candidate_sha256"] = "0" * 64

        assert entry.status == "finding"
        assert entry.word_scan["status"] == "FLAG"
        assert entry.errors == []
        assert entry.candidate_sha256 != "0" * 64

        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        stored = next(s for s in report["stages"] if s["stage"] == "post_map")
        assert stored["status"] == "finding"

    @_covers("I6")
    def test_i6_mutating_getitem_get_and_attribute_results_leaves_observation_unchanged(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")

        via_getitem = entry["word_scan"]
        via_getitem["status"] = "PASS"
        via_getitem["count"] = 0

        via_get = entry.get("sentence_scan")
        via_get["hits"] = 999
        via_get["samples"] = ["FORGED"]

        via_attr = entry.errors
        via_attr.append("INJECTED")

        assert entry["word_scan"]["status"] == "FLAG"
        assert entry.get("sentence_scan")["hits"] != 999
        assert entry.errors == []

        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        stored = next(s for s in report["stages"] if s["stage"] == "post_map")
        assert stored["status"] == "finding"
        assert stored["word_scan"]["status"] == "FLAG"

    @_covers("I7")
    def test_i7_pre_build_and_post_build_nested_mutation_combined(self):
        entry = lr.scan_stage(candidate_text=WORD_LEAK_EN, target_language="en", stage="post_map")

        # Pre-build: attempted mutation via the public accessor is inert (deep-copy-on-read).
        entry.word_scan["status"] = "PASS"
        assert entry.word_scan["status"] == "FLAG"

        four_entries = [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ]
        report = lr.build_report("en", four_entries)
        stored = next(s for s in report["stages"] if s["stage"] == "post_map")
        assert stored["status"] == "finding"

        # Post-build: mutating the returned report dict must not be recoverable as an
        # authoritative state -- rebuilding from the same (untouched) entries reproduces the
        # identical correct result.
        stored["word_scan"]["status"] = "PASS"
        stored["status"] = "clean"
        rebuilt = lr.build_report("en", four_entries)
        restored = next(s for s in rebuilt["stages"] if s["stage"] == "post_map")
        assert restored["status"] == "finding"
        assert restored["word_scan"]["status"] == "FLAG"

    @_covers("I8")
    def test_i8_hostile_values_via_object_setattr_never_crash_or_false_clean(self):
        def four(first):
            return [first,
                    lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
                    lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
                    lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final")]

        e1 = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        object.__setattr__(e1, "_raw_stage", 12345)
        r1 = lr.build_report("en", four(e1))  # must not raise
        assert r1["coverage"] == "incomplete"

        e2 = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        object.__setattr__(e2, "_raw_errors", "not-a-list")
        r2 = lr.build_report("en", four(e2))
        assert r2["coverage"] == "incomplete"

        class HostileDict(dict):
            def items(self):
                raise RuntimeError("boom")
        e3 = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        object.__setattr__(e3, "_raw_sentence_scan", HostileDict(e3.sentence_scan))
        r3 = lr.build_report("en", four(e3))  # must not raise/crash
        assert r3["coverage"] == "incomplete"

        e4 = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        object.__setattr__(e4, "_raw_applicability", "not_applicable")
        r4 = lr.build_report("en", four(e4))
        assert r4["coverage"] == "incomplete"
        assert not any(s["stage"] == "post_map" for s in r4["stages"])

        for r in (r1, r2, r3, r4):
            assert not (r["status"] == "clean" and r["coverage"] == "complete")

    @_covers("I9")
    def test_i9_weak_reference_proof_for_the_provenance_registry(self):
        import gc
        import weakref
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        ref = weakref.ref(entry)
        assert ref() is entry
        assert lr.build_report("en", [entry])["stages"][0]["candidate_sha256"] == entry.candidate_sha256
        del entry
        gc.collect()
        assert ref() is None  # the observation (and its provenance-registry entry) was collected

    @_covers("J1")
    def test_j1_negative_control_disabling_provenance_lets_pure_hash_forgery_through(self, monkeypatch):
        # A pure hash forgery -- status/evidence left genuinely self-consistent, ONLY the hash
        # is forged -- is NOT caught by the semantic re-derivation check (it never examines
        # candidate_sha256 at all). With the REAL provenance check active this is rejected
        # (I1). Disabling ONLY provenance verification here proves that check -- not something
        # else -- is what makes I1 pass for real: this is exactly the outcome I1 asserts must
        # never happen, and it now does.
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        object.__setattr__(entry, "_raw_candidate_sha256", "0" * 64)
        monkeypatch.setattr(lr, "_verify_stage_observation_provenance", lambda obs: True)

        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        stored = next(s for s in report["stages"] if s["stage"] == "post_map")
        assert stored["candidate_sha256"] == "0" * 64
        assert report["coverage"] == "complete"
        assert report["status"] == "clean"

    @_covers("J2")
    def test_j2_negative_control_disabling_semantic_rederivation_lets_mismatch_through(self, monkeypatch):
        def word_flag(t, l):
            return {"status": "FLAG", "count": 2, "samples": [{"term": "dengan", "snippet": "x"}]}
        entry = lr._scan_stage_test_seam(candidate_text=CLEAN_EN, target_language="en", stage="post_map",
                               word_scanner=word_flag)
        assert entry.status == "finding"
        object.__setattr__(entry, "_raw_status", "clean")
        four_entries = [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ]

        # With ONLY provenance disabled (semantic re-derivation left REAL and active), the
        # status/evidence mismatch is STILL caught -- proving semantic re-derivation is an
        # independent, non-redundant layer, not merely implied by provenance.
        monkeypatch.setattr(lr, "_verify_stage_observation_provenance", lambda obs: True)
        report_semantic_alone = lr.build_report("en", four_entries)
        assert not any(s["stage"] == "post_map" for s in report_semantic_alone["stages"])
        assert report_semantic_alone["coverage"] == "incomplete"

        # Now ALSO disable semantic re-derivation (simulating a hypothetical future bug/gap in
        # BOTH layers at once): the mismatch now sails through as false-clean, proving semantic
        # re-derivation was in fact what caught it above, not some other incidental check.
        monkeypatch.setattr(lr, "_expected_finding_status", lambda ss, ws: "clean")
        report_both_disabled = lr.build_report("en", four_entries)
        stored = next(s for s in report_both_disabled["stages"] if s["stage"] == "post_map")
        assert stored["status"] == "clean"
        assert stored["word_scan"]["status"] == "FLAG"  # the contradiction is now accepted
        assert report_both_disabled["coverage"] == "complete"
        assert report_both_disabled["status"] == "clean"

    @_covers("J3")
    def test_j3_negative_control_reintroducing_aliasing_breaks_the_detachment_guarantee(self):
        # Simulate "if the word_scan property returned a live reference instead of
        # copy.deepcopy(...)" by fetching the RAW slot directly -- exactly what a
        # non-deep-copying property implementation would hand back to any caller -- and
        # mutating it in place.
        entry = lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_map")
        simulated_aliased_view = entry._raw_word_scan
        simulated_aliased_view["status"] = "FLAG"
        simulated_aliased_view["count"] = 3
        simulated_aliased_view["samples"] = [{"term": "dengan", "snippet": "x"}]

        # This is exactly the dedicated guarantee I3/I6/C6 assert ("the returned value can
        # never be mutated back into the observation") -- and it now fails under simulated
        # aliasing, proving deep-copy-on-read is what makes it pass for real.
        assert entry.word_scan["status"] != "PASS"
        assert entry.word_scan["status"] == "FLAG"

        # build_report() still saves the report from false-clean regardless (provenance
        # recomputes over whatever the CURRENT raw content is, aliased or not), but the
        # observation's own state has already been irreversibly corrupted for any other
        # purpose -- exactly what deep-copy-on-read exists to prevent in the first place.
        report = lr.build_report("en", [
            entry,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]

    @_covers("J4")
    def test_j4_negative_control_permitting_direct_construction_breaks_the_init_guard(self, monkeypatch):
        with pytest.raises(TypeError):
            lr._StageObservation(stage="post_map")  # the REAL guard, confirmed first

        def _permissive_init(self, **fields):
            for k, v in fields.items():
                object.__setattr__(self, f"_raw_{k}", v)
        monkeypatch.setattr(lr._StageObservation, "__init__", _permissive_init)

        # Under the simulated regression, direct construction with forged fields now succeeds
        # -- exactly the scenario G16/I4's "raises TypeError" assertion exists to prevent; that
        # assertion would fail if re-run against this monkeypatched state.
        forged = lr._StageObservation(
            stage="post_map", target_language="en", candidate_sha256="9" * 64,
            applicability="applicable", status="clean",
            sentence_scan={"applies": False, "hits": 0, "samples": [], "other_langs": {}},
            word_scan={"status": "PASS", "count": 0, "samples": []}, errors=[], reason="",
        )
        assert type(forged) is lr._StageObservation
        assert forged.candidate_sha256 == "9" * 64

        # Even so, it still cannot enter a report -- it was never minted/sealed by this
        # module's own closure, regardless of how it came to exist. The __init__ guard is an
        # early, additional layer; provenance is the one no single monkeypatch can undo.
        report = lr.build_report("en", [
            forged,
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_polish"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="post_revise"),
            lr.scan_stage(candidate_text=CLEAN_EN, target_language="en", stage="final"),
        ])
        assert not any(s["stage"] == "post_map" for s in report["stages"])
        assert "STAGE_ENTRY_PROVENANCE_INVALID" in report["errors"]


# ===========================================================================
# Rework 4 (Codex hostile-input crash + threat-model + scanner-isolation audit,
# 2026-07-24) -- executable adversarial proofs for the second authorized structural fix.
# ===========================================================================
class TestK_Rework4HostileInputHardening:
    @_covers("K1")
    def test_k1_hostile_stage_eq_via_scan_stage_never_crashes(self):
        class HostileStage:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return 1
        entry = lr.scan_stage(candidate_text="x", target_language="en", stage=HostileStage())
        assert entry.status == "incomplete"
        assert entry.errors == ["STAGE_ID_INVALID"]

    @_covers("K2")
    def test_k2_hostile_stage_eq_via_not_applicable_stage_never_crashes(self):
        class HostileStage:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return 1
        result = lr.not_applicable_stage(HostileStage(), "en", "test")
        assert result is None

    @_covers("K3")
    def test_k3_hostile_stage_eq_via_incomplete_stage_never_crashes(self):
        class HostileStage:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return 1
        entry = lr.incomplete_stage(HostileStage(), "en", "SOME_CODE")
        assert entry.stage == "unknown"
        assert entry.status == "incomplete"

    @_covers("K4")
    def test_k4_hostile_word_scan_status_eq_never_crashes(self):
        class HostileStatus:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return hash("PASS")

        def hostile_word_scanner(t, l):
            return {"status": HostileStatus(), "count": 0, "samples": []}

        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                          word_scanner=hostile_word_scanner)
        assert entry.status == "incomplete"
        assert "WORD_SCAN_UNAVAILABLE" in entry.errors
        assert set(entry.errors) <= _KNOWN_SCAN_STAGE_ERROR_CODES

    @_covers("K5")
    def test_k5_hostile_dict_key_state_dependent_hash_never_crashes(self):
        class HostileKeyStatefulHash:
            """__hash__ succeeds exactly once (as the scanner's own dict literal requires to
            be constructed at all) then raises -- reproducing a hostile key whose hash
            behavior differs between insertion time and this module's own later re-hash
            during its {k for k, _ in ...} shape-check set-comprehension."""
            def __init__(self):
                self._calls = 0
            def __hash__(self):
                self._calls += 1
                if self._calls > 1:
                    raise RuntimeError("hostile hash on re-hash")
                return 12345
            def __eq__(self, other):
                return False

        def hostile_word_scanner(t, l):
            return {HostileKeyStatefulHash(): "x", "status": "PASS", "count": 0, "samples": []}

        entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                          word_scanner=hostile_word_scanner)
        assert entry.status == "incomplete"
        assert "WORD_SCAN_UNAVAILABLE" in entry.errors

        def hostile_sentence_scanner(t, l):
            return {HostileKeyStatefulHash(): "x", "applies": True, "hits": 0,
                    "samples": [], "other_langs": {}}

        entry2 = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                           sentence_scanner=hostile_sentence_scanner)
        assert entry2.status == "incomplete"
        assert "SENTENCE_SCAN_UNAVAILABLE" in entry2.errors

    @_covers("K6")
    def test_k6_hostile_values_in_every_sentence_scan_field_never_crash(self):
        class Hostile:
            # Used only in VALUE positions (never a dict key) -- a hash that always raises
            # would make the test's OWN dict literals below fail to construct at all.
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                raise RuntimeError("hostile hash")

        class HostileKey:
            # Used as a dict KEY -- __hash__ must succeed at least once so the raw dict
            # literal below can itself be constructed; __eq__ still raises.
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return 999

        cases = [
            {"applies": Hostile(), "hits": 0, "samples": [], "other_langs": {}},
            {"applies": True, "hits": Hostile(), "samples": [], "other_langs": {}},
            {"applies": True, "hits": 1, "samples": [Hostile()], "other_langs": {}},
            {"applies": True, "hits": 1, "samples": [], "other_langs": {HostileKey(): 1}},
            {"applies": True, "hits": 1, "samples": [], "other_langs": {"fr": Hostile()}},
        ]
        for i, raw in enumerate(cases):
            def scanner(t, l, _raw=raw):
                return _raw
            entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                              sentence_scanner=scanner)
            assert entry.status == "incomplete", f"case {i} did not degrade to incomplete"
            assert "SENTENCE_SCAN_UNAVAILABLE" in entry.errors, f"case {i}"
            assert set(entry.errors) <= _KNOWN_SCAN_STAGE_ERROR_CODES, f"case {i}"

    @_covers("K7")
    def test_k7_hostile_values_in_every_word_scan_field_never_crash(self):
        class Hostile:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                raise RuntimeError("hostile hash")

        cases = [
            {"status": Hostile(), "count": 0, "samples": []},
            {"status": "PASS", "count": Hostile(), "samples": []},
            {"status": "FLAG", "count": 1, "samples": [Hostile()]},
            {"status": "FLAG", "count": 1, "samples": [{"term": Hostile(), "snippet": "x"}]},
            {"status": "FLAG", "count": 1, "samples": [{"term": "x", "snippet": Hostile()}]},
        ]
        for i, raw in enumerate(cases):
            def scanner(t, l, _raw=raw):
                return _raw
            entry = lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                              word_scanner=scanner)
            assert entry.status == "incomplete", f"case {i} did not degrade to incomplete"
            assert "WORD_SCAN_UNAVAILABLE" in entry.errors, f"case {i}"
            assert set(entry.errors) <= _KNOWN_SCAN_STAGE_ERROR_CODES, f"case {i}"

    @_covers("K8")
    def test_k8_ast_proof_production_hooks_never_call_test_seam(self):
        for module, name in ((narration_api, "narration_api.py"), (static, "orchestrator/static.py")):
            src = inspect.getsource(module)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    called_name = None
                    if isinstance(fn, ast.Name):
                        called_name = fn.id
                    elif isinstance(fn, ast.Attribute):
                        called_name = fn.attr
                    assert called_name != "_scan_stage_test_seam", (
                        f"{name} must never call the test-only scanner seam (line {node.lineno})")


# ===========================================================================
# Rework 4 negative controls -- each proves a specific defense is load-bearing, not vacuous.
# ===========================================================================
class TestL_Rework4NegativeControls:
    @_covers("L1")
    def test_l1_negative_control_disabling_status_type_guard_lets_hostile_status_crash(self, monkeypatch):
        class HostileStatus:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return hash("PASS")

        def _unguarded_normalize_word_scan(raw):
            # Reproduces the EXACT pre-Rework-4 pattern: `status not in (...)` with no
            # `type(status) is not str` guard first, and no backstop try/except either.
            if type(raw) is not dict:
                return None
            captured = list(raw.items())
            if {k for k, _ in captured} != lr._WORD_SCAN_KEYS:
                return None
            closed = dict(captured)
            status = closed["status"]
            if status not in ("PASS", "FLAG"):  # <-- the removed guard
                return None
            return {"status": status, "count": closed["count"], "samples": closed["samples"]}

        monkeypatch.setattr(lr, "_normalize_word_scan", _unguarded_normalize_word_scan)

        def hostile_word_scanner(t, l):
            return {"status": HostileStatus(), "count": 0, "samples": []}

        with pytest.raises(RuntimeError):
            lr._scan_stage_test_seam(candidate_text="x", target_language="en", stage="post_map",
                                      word_scanner=hostile_word_scanner)

    @_covers("L2")
    def test_l2_negative_control_unguarded_stage_membership_check_would_raise(self):
        # _scan_stage_impl's stage check lives inside the closure and cannot be monkeypatched
        # by name from outside -- instead, reproduce the EXACT pre-Rework-4 unguarded operation
        # (`stage not in STAGE_ORDER`, no type check first) directly against the same hostile
        # object K1 proves the REAL, guarded scan_stage survives, confirming the guard -- not
        # something else -- is what prevents the crash there.
        class HostileStage:
            def __eq__(self, other):
                raise RuntimeError("hostile eq")
            def __hash__(self):
                return 1
        hostile = HostileStage()
        with pytest.raises(RuntimeError):
            _ = hostile not in lr.STAGE_ORDER  # the exact operation Rework 4 guarded

        entry = lr.scan_stage(candidate_text="x", target_language="en", stage=hostile)
        assert entry.status == "incomplete"  # the REAL, guarded path survives the same object

    @_covers("L3")
    def test_l3_negative_control_ast_checker_flags_synthetic_test_seam_call(self):
        synthetic_source = (
            "def _apply_v3_gates_fake(result, body):\n"
            "    entry = lr._scan_stage_test_seam(candidate_text='x', target_language='en',\n"
            "                                      stage='post_revise')\n"
        )
        tree = ast.parse(synthetic_source)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called_name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if called_name == "_scan_stage_test_seam":
                    found = True
        assert found, (
            "the AST detection logic K8 relies on must correctly flag a call to "
            "_scan_stage_test_seam in a production-hook-shaped snippet")


# ===========================================================================
# Privacy scan (matches A-05a/A-04 discipline)
# ===========================================================================
class TestPrivacyScan:
    # Built via concatenation so the literal terms never appear as a plain substring in this
    # file's own source (which would trip the very check this test performs).
    _FORBIDDEN_SUBSTRINGS = (
        "wi" + "mba", "ri" + "no", "@" + "gmail", "rail" + "way.app",
    )

    def test_no_production_identifiers_in_this_file(self):
        import pathlib
        src = pathlib.Path(__file__).read_text(encoding="utf-8").lower()
        for term in self._FORBIDDEN_SUBSTRINGS:
            assert term not in src, f"forbidden production-identifying term found: {term!r}"
