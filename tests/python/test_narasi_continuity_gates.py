"""
Permanent-continuity execution plan — Package A regression tests (hermetic).

Covers the two flag-gated source changes shipped this pass (A-03, A-06) plus
minimized, synthetic fixtures for four previously-confirmed defect shapes named
in the execution plan's A-04/A-05a scope. No production manuscript text or job
IDs are used anywhere in this file, per A-05a's privacy requirement.

Flags:
  NARASI_SOURCE_FACT_SPLIT       (A-03, default OFF — legacy canonical_facts
                                   slot byte-identical when off)
  NARASI_OUTLINE_CHAPCOUNT_ENFORCE (A-06, default OFF — no chapter-count check
                                   when off, matching today's live behavior)
"""
import asyncio
import inspect
import os
import re

import pytest

import orchestrator.context_builder as context_builder
from orchestrator.context_builder import SharedContext


# ---------------------------------------------------------------------------
# A-03: source_facts / story_contract separation
# ---------------------------------------------------------------------------
class TestA03SourceFactSplitFlagOff:
    """Flag off (default) must be byte-identical to pre-A-03 behavior."""

    def test_new_fields_exist_but_unused_by_default(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext()
        assert ctx.source_facts == ""
        assert ctx.story_contract == ""

    def test_legacy_bible_rendering_unchanged(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext(canonical_facts="Ha-neul is 24.", facts_are_bible=True)
        out = ctx.brief_block()
        assert "STORY BIBLE" in out
        assert "Ha-neul is 24." in out
        assert "REFERENCE SOURCE FACTS" not in out  # new block never appears when flag is off

    def test_legacy_rag_facts_rendering_unchanged(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext(canonical_facts="Founded 1890.", facts_are_bible=False)
        out = ctx.brief_block()
        assert "CANONICAL FACTS (the ONLY names" in out
        assert "Founded 1890." in out

    def test_legacy_empty_facts_rendering_unchanged(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext()
        out = ctx.brief_block()
        assert "none retrieved for this job" in out


class TestA03SourceFactSplitFlagOn:
    """Flag on: source_facts and story_contract render as two independent blocks.

    Every test below sets `facts_are_bible` explicitly (fiction=True vs.
    nonfiction=False) per Codex's 8.3.2 review: build_story_bible() runs for BOTH
    regimes, and the split renderer must preserve the same fiction/nonfiction
    framing distinction the legacy (flag-off) branch already made — a nonfiction
    continuity sheet must never receive fiction's "invent new detail" permission.
    """

    def test_both_present_fiction_bible_neither_suppresses_the_other(self, monkeypatch):
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(
            source_facts="Seoul has ~9.7M residents.",
            story_contract="Ha-neul is 24 and lost her voice in Chapter 3.",
            facts_are_bible=True,
        )
        out = ctx.brief_block()
        assert "STORY BIBLE" in out and "Ha-neul is 24" in out
        assert "REFERENCE SOURCE FACTS" in out and "Seoul has ~9.7M residents." in out
        assert "CANONICAL FACTS (the ONLY names" not in out  # fiction framing only, no nonfiction leak
        # Old single-slot framing must not also appear (no double-rendering of the
        # actual STORY BIBLE *block header* — a passing cross-reference to it inside
        # the REFERENCE SOURCE FACTS block, when both are present, is intentional).
        assert out.count("STORY BIBLE (canonical facts FIXED") == 1
        assert out.count("REFERENCE SOURCE FACTS (background material") == 1

    def test_story_contract_only_fiction_bible(self, monkeypatch):
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(story_contract="Fixed: the drive holds 40,000 files.", facts_are_bible=True)
        out = ctx.brief_block()
        assert "STORY BIBLE" in out
        assert "may invent NEW concrete detail" in out
        assert "REFERENCE SOURCE FACTS" not in out
        assert "CANONICAL FACTS (the ONLY names" not in out

    def test_story_contract_only_nonfiction_sheet(self, monkeypatch):
        # Codex 8.3.2: the confirmed bug — story_contract was previously always
        # rendered as STORY BIBLE regardless of facts_are_bible. A nonfiction
        # continuity sheet (facts_are_bible=False) must get [VERIFY]/no-fabrication
        # framing, matching the legacy branch's intent (not its exact label — see
        # 8.4.2 below for why the label itself changed).
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(story_contract="Founded 1890; population 40,000.", facts_are_bible=False)
        out = ctx.brief_block()
        assert "NONFICTION CONTINUITY SHEET" in out
        assert "[VERIFY: ...]" in out
        assert "STORY BIBLE" not in out
        assert "may invent NEW concrete detail" not in out
        assert "do NOT invent or add new factual specifics" in out

    def test_nonfiction_sheet_alone_is_not_labeled_a_factual_authority(self, monkeypatch):
        # Codex 8.4.2: with no source_facts to defer to, the sheet must still deny
        # itself factual authority, not merely omit an override clause.
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(story_contract="Founded 1890; population 40,000.", facts_are_bible=False)
        out = ctx.brief_block()
        assert "NOT a factual authority" in out or "NOT verified against" in out

    def test_nonfiction_sheet_plus_source_facts_correct_override_wording(self, monkeypatch):
        # Codex 8.4.2: build_story_bible(is_fiction=False) has no source_facts/RAG
        # parameter (confirmed via signature inspection below) — it is generated
        # purely from topic/outline, so it must NOT outrank retrieved source facts.
        # Both present, facts_are_bible=False: REFERENCE SOURCE FACTS must be named
        # as the HIGHER authority, and the sheet must explicitly defer to it.
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(
            source_facts="The city sits on a major river.",
            story_contract="Founded 1890; population 40,000.",
            facts_are_bible=False,
        )
        out = ctx.brief_block()
        assert "NONFICTION CONTINUITY SHEET" in out
        assert "STORY BIBLE" not in out
        # source facts must be labeled the HIGHER authority, not the other way around
        assert "is the HIGHER factual authority" in out
        assert "NONFICTION CONTINUITY SHEET above disagrees with this material, THIS material is correct" in out
        # the old (backwards) precedence wording must not appear in any form
        assert "it never overrides" not in out
        # the sheet's own text must explicitly defer to source facts on conflict
        assert "REFERENCE SOURCE FACTS below, REFERENCE SOURCE FACTS is correct" in out

    def test_build_story_bible_has_no_source_facts_parameter(self):
        # Grounds the 8.4.2 precedence fix in the actual function signature, not
        # just an assertion: confirms the nonfiction sheet genuinely cannot have
        # consumed source_facts, so it cannot be a grounded factual authority.
        import inspect as _inspect
        from orchestrator.dynamic import build_story_bible
        params = set(_inspect.signature(build_story_bible).parameters)
        assert "source_facts" not in params
        assert not any("rag" in p.lower() or "source" in p.lower() for p in params)

    def test_fiction_bible_precedence_over_source_facts_unchanged(self, monkeypatch):
        # Codex 8.4.2 explicitly preserves fiction precedence: invented, fixed
        # canon still outranks incidental real-world reference material.
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(
            source_facts="Seoul has ~9.7M residents.",
            story_contract="Ha-neul is 24 and lost her voice in Chapter 3.",
            facts_are_bible=True,
        )
        out = ctx.brief_block()
        assert "it never overrides the STORY BIBLE above" in out
        assert "is the HIGHER factual authority" not in out

    def test_source_facts_only(self, monkeypatch):
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(source_facts="The Han river runs through Seoul.")
        out = ctx.brief_block()
        assert "REFERENCE SOURCE FACTS" in out
        assert "STORY BIBLE" not in out
        assert "CANONICAL FACTS (the ONLY names" not in out  # no dangling override-note either way
        assert "NONFICTION CONTINUITY SHEET" not in out  # no dangling reference to an absent sheet

    def test_neither_present_falls_back_to_verify_framing(self, monkeypatch):
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext()
        out = ctx.brief_block()
        assert "none retrieved for this job" in out

    def test_legacy_canonical_facts_ignored_when_flag_on(self, monkeypatch):
        # A job that (incorrectly) still has canonical_facts populated must not
        # leak the legacy framing once the flag routes rendering through the
        # new fields — prevents a double/contradictory rendering.
        monkeypatch.setenv("NARASI_SOURCE_FACT_SPLIT", "1")
        ctx = SharedContext(canonical_facts="stale legacy value", facts_are_bible=True)
        out = ctx.brief_block()
        assert "stale legacy value" not in out


class TestA03FlagOffCompatibility:
    """Flag-off compatibility, explicitly re-verified after the 8.3.2 fix (not just
    covered incidentally by TestA03SourceFactSplitFlagOff above)."""

    def test_flag_off_fiction_bible_unaffected_by_fix(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext(canonical_facts="Ha-neul is 24.", facts_are_bible=True)
        out = ctx.brief_block()
        assert "STORY BIBLE" in out and "may invent NEW concrete detail" in out

    def test_flag_off_nonfiction_unaffected_by_fix(self, monkeypatch):
        monkeypatch.delenv("NARASI_SOURCE_FACT_SPLIT", raising=False)
        ctx = SharedContext(canonical_facts="Founded 1890.", facts_are_bible=False)
        out = ctx.brief_block()
        assert "CANONICAL FACTS (the ONLY names" in out
        assert "STORY BIBLE" not in out


class TestA03RagRoutingSource:
    """build_shared_context's RAG-extraction call site routes to the correct slot."""

    def test_source_routes_to_source_facts_when_flag_on(self):
        src = inspect.getsource(context_builder.build_shared_context)
        assert "NARASI_SOURCE_FACT_SPLIT" in src
        assert "ctx.source_facts = _facts" in src
        assert "ctx.canonical_facts = _facts" in src  # the flag-off branch still present


class TestA03BibleGatingSource:
    """static.py's bible-generation gate and every write-back site respect the flag."""

    def test_gating_and_writebacks_present(self):
        import orchestrator.static as static
        src = inspect.getsource(static.narrate_chapters)
        assert "_sfs = str(os.environ.get(\"NARASI_SOURCE_FACT_SPLIT\"" in src
        assert "_bible_slot = (ctx.story_contract if _sfs else ctx.canonical_facts)" in src
        # three write-back sites: initial pin + two ledger-enforce re-roll sites.
        # word-boundary regex so "_bible" doesn't also match as a prefix of "_bible2".
        assert len(re.findall(r"ctx\.story_contract = _bible\b(?!2)", src)) == 1
        assert len(re.findall(r"ctx\.story_contract = _bible2\b", src)) == 2


# ---------------------------------------------------------------------------
# A-06: outline returned chapter-count and structure enforcement
# ---------------------------------------------------------------------------
class TestA06ChapterCountGateBehavioral:
    """Real behavioral tests against `_narasi_enforce_chapter_count`, extracted
    from `_narasi_outline_impl` into a standalone, directly-callable, pure
    function specifically so this can be exercised without mocking an LLM call
    or invoking the full outline pipeline (Codex 8.3.3: "source-text
    introspection alone is not acceptance evidence").

    Codex 8.4 item 3 correction: a length-only check accepted any container
    whose len() happened to equal chap_count — a dict, a bare string, and a list
    of plain strings were all independently reproduced as wrongly "accepted".
    The tests below cover the corrected structural validation directly.
    """

    def _fn(self):
        import laozhang_api
        return laozhang_api._narasi_enforce_chapter_count

    def _chapters(self, n):
        return [{"id": str(i + 1), "title": f"Ch {i+1}", "description": f"desc {i+1}", "words": 1000}
                for i in range(n)]

    def _good_chapter(self, **overrides):
        base = {"id": "1", "title": "A", "description": "d1", "words": 500}
        base.update(overrides)
        return base

    def test_flag_on_mismatch_rejects(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()(self._chapters(4), 5)
        assert exc_info.value.status_code == 422
        assert "4 chapter" in str(exc_info.value.detail)
        assert "expected 5" in str(exc_info.value.detail)

    def test_flag_on_match_accepts(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        result = self._fn()(self._chapters(5), 5)
        assert result is None  # no-op / accept, no exception raised

    def test_flag_off_preserves_legacy_behavior_regardless_of_mismatch(self, monkeypatch):
        monkeypatch.delenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", raising=False)
        # A gross mismatch (4 vs. 5) must still pass through untouched when the
        # flag is off — this is today's live, unenforced behavior. Also true for
        # every malformed-structure case below (not re-tested individually here
        # since the function returns immediately on flag-off, before any type check).
        result = self._fn()(self._chapters(4), 5)
        assert result is None

    def test_flag_on_empty_chapters_rejects_same_as_any_mismatch(self, monkeypatch):
        # Documented policy: an empty/missing chapters list is not a special case —
        # len([]) == 0 != chap_count for any chap_count > 0, rejected identically.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([], 5)
        assert exc_info.value.status_code == 422
        assert "0 chapter" in str(exc_info.value.detail)

    def test_flag_on_none_chapters_rejected_as_wrong_type(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()(None, 3)
        assert exc_info.value.status_code == 422
        assert "NoneType" in str(exc_info.value.detail)

    def test_flag_on_zero_requested_and_zero_returned_accepts(self, monkeypatch):
        # Degenerate but consistent case: 0 requested, 0 returned — not a mismatch.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        result = self._fn()([], 0)
        assert result is None

    def test_flag_on_dict_rejected_not_silently_length_matched(self, monkeypatch):
        # Codex 8.4 item 3 repro #1: a 2-key dict has len()==2, previously accepted
        # for chap_count=2.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()({"a": 1, "b": 2}, 2)
        assert exc_info.value.status_code == 422
        assert "dict" in str(exc_info.value.detail)

    def test_flag_on_string_rejected_not_silently_length_matched(self, monkeypatch):
        # Codex 8.4 item 3 repro #2: a 2-character string has len()==2, previously
        # accepted for chap_count=2.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()("ab", 2)
        assert exc_info.value.status_code == 422
        assert "str" in str(exc_info.value.detail)

    def test_flag_on_list_of_bare_strings_rejected(self, monkeypatch):
        # Codex 8.4 item 3 repro #3: a list of 2 plain strings IS a list of the
        # right length, but no entry is a chapter object — previously accepted.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()(["x", "y"], 2)
        assert exc_info.value.status_code == 422
        assert "chapter 1" in str(exc_info.value.detail).lower()

    def test_flag_on_missing_id_rejected(self, monkeypatch):
        # Codex 8.5 item 2: 'id' was not checked at all before this pass.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        bad = {k: v for k, v in self._good_chapter().items() if k != "id"}
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([bad, self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "id" in str(exc_info.value.detail)

    def test_flag_on_missing_title_rejected(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        bad = {k: v for k, v in self._good_chapter().items() if k != "title"}
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([bad, self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "title" in str(exc_info.value.detail)

    def test_flag_on_missing_description_rejected(self, monkeypatch):
        # Codex 8.5 item 2: 'description' was not checked at all before this pass.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        bad = {k: v for k, v in self._good_chapter().items() if k != "description"}
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([bad, self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "description" in str(exc_info.value.detail)

    def test_flag_on_missing_words_rejected(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        bad = {k: v for k, v in self._good_chapter().items() if k != "words"}
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([bad, self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "words" in str(exc_info.value.detail)

    def test_flag_on_numeric_string_words_accepted(self, monkeypatch):
        # "500" (a clean positive-integer string) is normalized and accepted —
        # the documented numeral-string policy (Codex 8.5 item 2): normalize a
        # clean positive-integer string, reject anything else string-shaped.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        chapters = [self._good_chapter(words="500"), self._good_chapter(id="2")]
        result = self._fn()(chapters, 2)
        assert result is None
        # Codex 8.6 item 2: "normalized" must mean the stored value is a real int,
        # not merely that validation passed while leaving the original string in place.
        assert chapters[0]["words"] == 500
        assert isinstance(chapters[0]["words"], int)

    def test_flag_on_whole_float_words_accepted(self, monkeypatch):
        # 500.0 has no fractional part -- int(500.0) is not destructive, so it's
        # accepted (distinct from a genuinely fractional float like 500.5).
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        chapters = [self._good_chapter(words=500.0), self._good_chapter(id="2")]
        result = self._fn()(chapters, 2)
        assert result is None
        # Codex 8.6 item 2: the float must be written back as an int, not left as 500.0.
        assert chapters[0]["words"] == 500
        assert isinstance(chapters[0]["words"], int)

    def test_flag_on_plain_int_words_left_as_int(self, monkeypatch):
        # An already-valid plain int must remain an int (not silently become e.g. a
        # bool or a different numeric type) after passing through normalization.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        chapters = [self._good_chapter(words=750), self._good_chapter(id="2")]
        result = self._fn()(chapters, 2)
        assert result is None
        assert chapters[0]["words"] == 750
        assert isinstance(chapters[0]["words"], int)
        assert not isinstance(chapters[0]["words"], bool)

    @pytest.mark.parametrize("bad_words", [-5, 0, 3.7, float("nan"), float("inf"), float("-inf"), True])
    def test_flag_on_invalid_words_values_rejected(self, monkeypatch, bad_words):
        # Codex 8.5 item 2, independently reproduced before this fix: negative,
        # zero, fractional, NaN, infinite, and boolean 'words' values were all
        # wrongly accepted by the 8.4 fix (which only checked isinstance(int,float)
        # and excluded plain bool, missing every one of these).
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([self._good_chapter(words=bad_words), self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "words" in str(exc_info.value.detail)

    @pytest.mark.parametrize("bad_words_str", ["-5", "3.5", "five", "0", "", "  ", "5.0"])
    def test_flag_on_invalid_words_strings_rejected(self, monkeypatch, bad_words_str):
        # Documented numeral-string policy: only a clean positive-integer string
        # ([1-9]\d*) is accepted; negative/fractional/non-digit/zero/blank/
        # decimal-suffixed strings are all rejected, never silently coerced.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            self._fn()([self._good_chapter(words=bad_words_str), self._good_chapter(id="2")], 2)
        assert exc_info.value.status_code == 422
        assert "words" in str(exc_info.value.detail)

    def test_gate_call_site_reuses_admitted_chap_count_variable(self):
        # Confirm the call site in _narasi_outline_impl passes the SAME chap_count
        # that was admitted earlier in the function, not a re-derived value.
        import laozhang_api
        src = inspect.getsource(laozhang_api._narasi_outline_impl)
        assert re.search(r'chap_count\s*=\s*int\(body\.get\("chap_count"\)', src)
        assert "_narasi_enforce_chapter_count(result[\"chapters\"], chap_count)" in src
        # ordering (Codex 8.4 item 3): the call now runs BEFORE word-count
        # enforcement, not after — specifically so a malformed structure never
        # reaches the word-count block's unguarded c.get()/c["words"]=... access.
        i_call = src.index("_narasi_enforce_chapter_count(result")
        i_words = src.index("ENFORCE word count")
        i_dedup_apply = src.index("fix cross-chapter duplicate reveals")
        assert i_call < i_words < i_dedup_apply


class TestA06RealOutlinePathIntegration:
    """Real integration tests through `_narasi_outline_impl` itself, with only
    `_narasi_complete` (the LLM call) and `_log_narasi_usage` mocked — proving
    flag-on match/mismatch/malformed-field behavior on the REAL parse and call
    path, not just the isolated helper (Codex 8.4 item 3: "Direct helper tests
    are useful but do not exercise mocked LLM parse/integration behavior").

    CORRECTION (Codex 8.5 item 2): flag-off coverage here is precise, not
    overstated — flag-off is only shown to preserve legacy behavior for a
    structurally-valid outline whose chapter COUNT mismatches. For a genuinely
    malformed structure, flag-off does NOT protect the historical word-count
    block from its own unguarded field access; it still raises an uncaught
    `AttributeError`, exactly as it always has — a documented pre-existing gap,
    not something flag-off "handles" (see
    `test_real_path_flag_off_malformed_structure_still_crashes_historically`).

    `db.save_outline`'s failure (no test DB configured) is intentionally left
    unmocked: it is wrapped in its own try/except in `_narasi_outline_impl` and
    is best-effort by design — this is itself part of what these tests confirm
    (a DB-less test environment still reaches a real accept/reject verdict).
    """

    class _FakeMsg:
        def __init__(self, content):
            self.content = content

    class _FakeChoice:
        def __init__(self, content):
            self.message = TestA06RealOutlinePathIntegration._FakeMsg(content)

    class _FakeResp:
        def __init__(self, content):
            self.choices = [TestA06RealOutlinePathIntegration._FakeChoice(content)]

    def _mock_complete(self, monkeypatch, raw_value):
        import json as _json
        import laozhang_api

        def _fake(model, messages, max_tok, role="", phase=""):
            payload = raw_value if isinstance(raw_value, str) else _json.dumps(raw_value)
            return self._FakeResp(payload), model

        async def _log_usage_stub(*a, **kw):
            return None

        monkeypatch.setattr(laozhang_api, "_narasi_complete", _fake)
        monkeypatch.setattr(laozhang_api, "_log_narasi_usage", _log_usage_stub)

    def _body(self, **overrides):
        base = {"action": "outline", "topic": "a test topic", "style": "storytelling",
                "language": "en", "word_min": 400, "word_max": 600, "chap_count": 2}
        base.update(overrides)
        return base

    def test_real_path_match_accepts(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": 500},
            {"id": "2", "title": "B", "description": "d2", "words": 500},
        ]})
        import laozhang_api
        result = asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert len(result["chapters"]) == 2

    def test_real_path_numeric_string_words_normalized_to_int(self, monkeypatch):
        # Codex 8.7 item 2: a persistent test through the REAL _narasi_outline_impl
        # path (not just the direct helper, already covered in
        # TestA06ChapterCountGateBehavioral.test_flag_on_numeric_string_words_accepted)
        # proving the accepted numeral string survives the full real parse, gate, and
        # downstream word-count-normalization path as an actual int, not merely that
        # a manual/transient check happened to return int once.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": "500"},
            {"id": "2", "title": "B", "description": "d2", "words": "500"},
        ]})
        import laozhang_api
        # word_min/word_max comfortably contain the 1000-word total so the historical
        # word-count block (which runs AFTER the gate) has no ratio to rescale and
        # leaves the gate's normalized ints untouched -- isolating what THIS test claims.
        result = asyncio.run(laozhang_api._narasi_outline_impl(self._body(word_min=900, word_max=1100)))
        for ch in result["chapters"]:
            assert ch["words"] == 500
            assert isinstance(ch["words"], int)

    def test_real_path_whole_float_words_normalized_to_int(self, monkeypatch):
        # Codex 8.7 item 2: same real-path proof for a whole finite float.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": 500.0},
            {"id": "2", "title": "B", "description": "d2", "words": 500.0},
        ]})
        import laozhang_api
        result = asyncio.run(laozhang_api._narasi_outline_impl(self._body(word_min=900, word_max=1100)))
        for ch in result["chapters"]:
            assert ch["words"] == 500
            assert isinstance(ch["words"], int)

    def test_real_path_mismatch_rejects(self, monkeypatch):
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": 500},
        ]})
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert exc_info.value.status_code == 422
        assert "1 chapter" in str(exc_info.value.detail)

    def test_real_path_malformed_rejects_with_422_not_500(self, monkeypatch):
        # The specific failure mode this rework closes: before the fix, this shape
        # (a list of bare strings, survives JSON parsing and _extract_chapters)
        # reached the word-count block's unguarded c.get("words", 0) and raised an
        # uncaught AttributeError there instead of a clean 422.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": ["just a string", "another string"]})
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert exc_info.value.status_code == 422
        assert "chapter 1" in str(exc_info.value.detail).lower()

    def test_real_path_missing_id_rejects_with_422(self, monkeypatch):
        # Codex 8.5 item 2: real-path coverage for the newly-enforced 'id' field.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"title": "A", "description": "d1", "words": 500},
            {"id": "2", "title": "B", "description": "d2", "words": 500},
        ]})
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert exc_info.value.status_code == 422
        assert "id" in str(exc_info.value.detail)

    def test_real_path_missing_description_rejects_with_422(self, monkeypatch):
        # Codex 8.5 item 2: real-path coverage for the newly-enforced 'description' field.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "words": 500},
            {"id": "2", "title": "B", "description": "d2", "words": 500},
        ]})
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert exc_info.value.status_code == 422
        assert "description" in str(exc_info.value.detail)

    @pytest.mark.parametrize("bad_words", [-5, 0, 3.7, float("nan"), float("inf"), float("-inf")])
    def test_real_path_invalid_words_rejects_with_422_not_500(self, monkeypatch, bad_words):
        # Codex 8.5 item 2, real-path coverage: before this fix, these values would
        # reach the word-count block's int(c.get("words", 0)) and either silently
        # truncate (a fractional/negative value) or raise (int(nan)/int(inf) both
        # raise ValueError/OverflowError) -- a 500, not a clean 422.
        #
        # CORRECTION (Codex 8.6 item 2): a prior version of this test skipped the
        # NaN/inf cases, reasoning "NaN/inf cannot round-trip through json.dumps".
        # That premise is WRONG -- Python's json.dumps has allow_nan=True by
        # DEFAULT, so it happily emits the non-standard-JSON tokens `NaN`/
        # `Infinity`/`-Infinity`, and json.loads accepts them back by default too
        # (independently verified: json.loads(json.dumps(float("nan"))) is nan).
        # `_mock_complete` below round-trips `bad_words` through exactly this
        # json.dumps/json.loads pair (via the fake `_narasi_complete` response
        # body), so NaN/inf now exercise the REAL mocked parse path, not a direct
        # call to the helper -- zero skips, per Codex's explicit instruction.
        monkeypatch.setenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", "1")
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": bad_words},
            {"id": "2", "title": "B", "description": "d2", "words": 500},
        ]})
        import laozhang_api
        with pytest.raises(laozhang_api.HTTPException) as exc_info:
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert exc_info.value.status_code == 422
        assert "words" in str(exc_info.value.detail)

    def test_real_path_flag_off_accepts_valid_structure_mismatch(self, monkeypatch):
        # Flag off preserves legacy behavior ONLY for a structurally-valid outline
        # whose chapter COUNT merely mismatches -- this is the one case the ledger
        # may accurately claim flag-off compatibility for.
        monkeypatch.delenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", raising=False)
        self._mock_complete(monkeypatch, {"chapters": [
            {"id": "1", "title": "A", "description": "d1", "words": 500},
        ]})
        import laozhang_api
        result = asyncio.run(laozhang_api._narasi_outline_impl(self._body()))
        assert len(result["chapters"]) == 1  # legacy: mismatch silently accepted

    def test_real_path_flag_off_malformed_structure_still_crashes_historically(self, monkeypatch):
        # CORRECTION (Codex 8.5 item 2): the ledger previously overstated this as
        # "flag-off preserves legacy behavior" for malformed input generally. That
        # is only true for a valid-structure COUNT mismatch (test above). For a
        # genuinely malformed structure (e.g. chapters that aren't dicts at all),
        # the flag being off does NOT protect the historical word-count block from
        # its own unguarded c.get(...) access -- it still raises an uncaught
        # AttributeError here, exactly as it always has. This is documented,
        # pre-existing legacy behavior this rework does not change (and is
        # explicitly out of scope to fix without the flag on) -- not a new
        # regression, and not something the ledger may claim is "handled".
        monkeypatch.delenv("NARASI_OUTLINE_CHAPCOUNT_ENFORCE", raising=False)
        self._mock_complete(monkeypatch, {"chapters": ["just a string", "another string"]})
        import laozhang_api
        with pytest.raises(AttributeError):
            asyncio.run(laozhang_api._narasi_outline_impl(self._body()))


# ---------------------------------------------------------------------------
# A-04 / A-05a: minimized regression fixtures for previously-confirmed defect
# shapes (chapter-heading fusion + entity-attribute drift claim shape). Full
# coverage of all nine live gates (chapter-boundary, canon-diff, world-state,
# duration, numeric ledger, thread-tracker, language, location-continuity,
# brand-enforce) is intentionally out of scope for this pass — flagged as
# remaining A-04 work, not silently claimed complete.
# ---------------------------------------------------------------------------
class TestA04ChapterHeadingFusionFixture:
    """Minimized synthetic repro of the confirmed 5637e72 fused-heading defect,
    exercised through the real public function (chapter_heading_repair), not a
    regex-presence check."""

    def test_clean_two_chapter_text_is_byte_identical(self):
        import narasi_gate
        clean = "## Chapter 1: Arrival\n\nShe walked in.\n\n## Chapter 2: Departure\n\nHe left."
        out, n_repairs = narasi_gate.chapter_heading_repair(clean, lang="en")
        assert n_repairs == 0
        assert out == clean

    def test_single_hash_fused_heading_is_split(self):
        import narasi_gate
        fused = "She walked away.# Chapter 2: Departure\n\nHe left."
        out, n_repairs = narasi_gate.chapter_heading_repair(fused, lang="en")
        assert n_repairs == 1
        assert "away.#" not in out
        assert "\n\n# Chapter 2: Departure" in out

    def test_double_hash_fused_heading_is_split(self):
        import narasi_gate
        fused = "She walked away.## Chapter 2: Departure\n\nHe left."
        out, n_repairs = narasi_gate.chapter_heading_repair(fused, lang="en")
        assert n_repairs == 1
        assert "\n\n## Chapter 2: Departure" in out

    def test_headings_own_leading_hash_does_not_false_positive(self):
        # The pre-5637e72 bug: `(?<=\S)` let a heading's own "#" satisfy the
        # lookbehind, corrupting clean "## Chapter N" into "#\n\n# Chapter N".
        import narasi_gate
        clean = "## Chapter 1: Arrival\n\nShe walked in.\n\n## Chapter 2: Departure\n\nHe left."
        out, n_repairs = narasi_gate.chapter_heading_repair(clean, lang="en")
        assert n_repairs == 0


class TestA05aEntityAttributeClaimShape:
    """Minimized synthetic repro of the confirmed entity-attribute gender-flip
    defect shape (a character's tracked attribute must carry a verbatim quote
    + chapter number, not a free-text unlocatable description)."""

    def test_extraction_schema_requires_quote_and_chapter(self):
        import narration_api
        src = inspect.getsource(narration_api)
        i = src.index("entity-attribute continuity checker")
        schema_region = src[i:i + 2500]
        # Python source escapes inner double-quotes inside a double-quoted string
        # literal, so the raw source text contains `\"quote\"`, not bare `"quote"`.
        assert '\\"quote\\"' in schema_region
        assert '\\"chapter\\"' in schema_region

    def test_truncation_cap_is_shared_budget_not_flat_slice(self):
        import narration_api
        src = inspect.getsource(narration_api)
        i = src.index("entity-attribute continuity checker")
        region = src[max(0, i - 500):i + 2500]
        assert "_ea_max_chars = int(os.environ.get(\"NARASI_CRITIQUE_MAX_CHARS\"" in region
        # the confirmed truncation-bug slice must not recur in THIS gate specifically
        # (other unrelated functions in this ~4600-line file may legitimately use a
        # flat cap of their own for a different, already-reviewed purpose).
        assert "[:60000]" not in region
