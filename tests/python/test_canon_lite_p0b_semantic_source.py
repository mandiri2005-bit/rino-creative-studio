"""P0-B — SEMANTIC SOURCE. entities/anchors/one_time_events, sourced from the existing
Story Bible call, threaded through the shared seam in `narrate_chapters` for BOTH
Scenario A ("Dapur A", explicit accepted outline) and Scenario B ("Dapur B", topic-only —
`orchestrator.dynamic.outline_from_topic`).

Ratified decisions this file enforces:

  * NO new LLM call — the structured envelope rides in the SAME Story Bible response,
    requested via `build_story_bible(..., structured_semantic=True)`, never a second call;
  * NOT `NARASI_CANON_REGISTRY` — a distinct schema, a distinct fence label, no dependency
    on that flag or its parser;
  * entities/anchors/one_time_events come from the Story Bible call ONLY — no post-hoc LLM
    extraction from prose, anywhere;
  * `reveals` stays empty, honestly — P0-B does not touch it (deferred to a CLAIM_REVEAL
    workstream, not built here);
  * a NEW hash (`canon_lite.accepted_outline_content_digest`) binds the FULL accepted
    outline (title + summary/description), because `outline_digest` only binds
    order/id/title and cannot see a summary edit;
  * assist refuses BEFORE the MAP on an absent, unbound, or empty (parsed fine but nothing
    extracted) envelope — never a silent downgrade to an empty canon;
  * off-mode is byte-identical: zero new code path executed, `build_story_bible` called
    with its EXACT pre-P0-B signature.

No live LLM, no live provider, no Railway. `orchestrator.dynamic.build_story_bible` and
`orchestrator.static._write_chapter` are the only two boundaries stubbed; everything else —
routing, outline generation, canon construction, freeze, injection — runs for real.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

import canon_lite as cl                                     # noqa: E402
import canon_lite_l2 as l2                                  # noqa: E402
import canon_lite_semantic_source as css                    # noqa: E402
from orchestrator import dynamic as dyn                     # noqa: E402
from orchestrator import router as rt                        # noqa: E402
from orchestrator import static as st                        # noqa: E402
from orchestrator.context_builder import SharedContext       # noqa: E402

CANARY = "t-canary-p0b"

#: Stand-in Story Bible text. The semantic source binds its OWN hash of this (P0-B round 2),
#: so a row that mutates it — as the surgical-patch rows do — must break `binds_bible()`.
BIBLE = "FAKE BIBLE"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", CANARY)
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.delenv("NARASI_CANON_LITE_MODE", raising=False)
    monkeypatch.delenv("NARASI_BIBLE_BEST_OF", raising=False)
    # P0-B round 2 gates structured extraction to FICTION (nonfiction gets no LLM-derived
    # semantic authority until a grounding provenance path exists). Forced True here rather
    # than depending on which style string the real pakem registry resolves as fiction — a
    # style that fuzzy-fell to a nonfiction entry would silently take every row in this file
    # down the not-eligible refusal path and assert nothing about what it claims to test.
    # The nonfiction REFUSAL itself is covered explicitly, with this override removed.
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: True)
    original = st._write_chapter
    yield
    st._write_chapter = original
    os.environ.pop("NARASI_CANON_LITE_MODE", None)   # same leak guard as sibling files


def _outline(n=3):
    return [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}",
             "word_target": 400} for i in range(n)]


class Recorder:
    """A chapter-worker stub that reports the REAL census fields — the same shape
    `_write_chapter` reports — so assist's post-MAP binding check has something to
    verify. A stub that skipped them would make every assist job here fail as
    unaccountable regardless of what P0-B did, which is the correct behaviour of that
    census and not something this file should route around with a thinner stub."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append(dict(kw))
        report = {}
        if kw.get("canon_text"):
            report = {
                "canon_prompt_sha256": cl.sha256_hex(kw["canon_text"].encode("utf-8")),
                "context_sha256_seen": cl.context_digest(kw["ctx"]),
                "canon_sha256": kw.get("canon_sha256"),
                "context_sha256": kw.get("context_sha256"),
            }
        return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m",
                **report}


def _make_bible_double(*, entities=(), anchors=(), one_time_events=(), text="FAKE BIBLE",
                       raise_exc=None):
    """A `build_story_bible` replacement — instant, no network, no provider. Always
    returns a WELL-FORMED, VALIDATED `CanonLiteSemanticSourceV1` bound to whatever
    outline it is called with (empty by default — `has_any_semantic_content() is
    False` — unless entities/anchors/one_time_events are supplied)."""
    async def _fake(topic, outline, *, is_fiction=True, style=None, language="id",
                    manager_model=None, timeout=None, telemetry_sink=None,
                    extra_negative=None, structured_semantic=False):
        if raise_exc is not None:
            raise raise_exc
        if not structured_semantic:
            return text
        source = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=text, entities=entities,
            anchors=anchors, one_time_events=one_time_events)
        return text, source
    return _fake


def _entity(name="Ratna", eid="ent1"):
    return cl.CanonEntityV1(entity_id=eid, canonical_name=name, aliases=(),
                            alias_source="none")


def _anchor(literal="pagi hari", kind="time", aid="anc1"):
    return cl.CanonAnchorV1(anchor_id=aid, kind=kind, literal=literal)


def _event(order=1, eid="evt1"):
    return cl.CanonEventV1(event_id=eid, occurs_chapter_order=order)


async def _map_chapters(monkeypatch, *, tenant_id=CANARY, n=3, bible_double=None,
                        mode="assist", **kw):
    """Drive the REAL `narrate_chapters` (Scenario A shape) and return `(result, rec)`.

    `mode` sets `NARASI_CANON_LITE_MODE` EXPLICITLY on every call — this file must never
    rely on whatever a sibling test happened to leave in `os.environ` (a raw `os.environ[...]
    = ...` mutation, used by the best-of-N row below, is not auto-reverted by `monkeypatch`
    between tests the way `monkeypatch.setenv` is).
    """
    os.environ["NARASI_CANON_LITE_MODE"] = mode
    if bible_double is not None:
        monkeypatch.setattr(dyn, "build_story_bible", bible_double)
    rec = Recorder()
    st._write_chapter = rec
    chapters = _outline(n)
    kw.setdefault("assist_activation_ready", True)
    res = await st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=n, tenant_id=tenant_id,
        shared_context=SharedContext(topic="topik", chapters=chapters), **kw)
    return res, rec


async def _drive_dapur_b(monkeypatch, *, tenant_id=CANARY, n_chapters=3,
                         bible_double=None, mode="assist", **extra_req):
    """Drive the REAL router entry point with a BARE TOPIC — Scenario B, `outline_from_topic`
    (Dapur B) — with `orch_mode=static` so outline GENERATION needs no LLM of its own (a
    deterministic titles/summaries generator); the ONLY two stubbed boundaries are the
    Story Bible call and the chapter writer, identically to `_map_chapters`. Same explicit
    `mode` handling and the same reason — see `_map_chapters`."""
    os.environ["NARASI_CANON_LITE_MODE"] = mode
    if bible_double is not None:
        monkeypatch.setattr(dyn, "build_story_bible", bible_double)
    rec = Recorder()
    st._write_chapter = rec
    req = {"topic": "Topik dapur B", "n_chapters": n_chapters, "orch_mode": "static",
          "tenant_id": tenant_id, "rag": "off", "polish": "none", **extra_req}
    res = await rt.generate_narration(req, assist_activation_ready=True)
    return res, rec


# ===========================================================================
# 1. accepted_outline_content_digest — wider than outline_digest, and why
# ===========================================================================
def test_content_digest_changes_when_summary_changes_but_outline_digest_does_not():
    a = _outline(2)
    b = [dict(c) for c in a]
    b[0]["summary"] = "SUMMARY DIUBAH TOTAL"
    assert cl.outline_digest(a) == cl.outline_digest(b), \
        "outline_digest is supposed to be blind to summary text"
    assert cl.accepted_outline_content_digest(a) != cl.accepted_outline_content_digest(b), \
        "accepted_outline_content_digest must SEE a summary edit — that is its whole point"


def test_content_digest_is_stable_for_identical_content():
    a = _outline(3)
    b = _outline(3)
    assert cl.accepted_outline_content_digest(a) == cl.accepted_outline_content_digest(b)


def test_content_digest_reads_description_as_a_fallback_for_summary():
    """Mirrors `_story_bible_prompt`'s own `c.get("summary", c.get("description", ""))` —
    the hash must bind what the LLM was actually shown, under either field name."""
    a = [{"id": 1, "title": "Bab 1", "description": "lewat description bukan summary"}]
    b = [{"id": 1, "title": "Bab 1", "summary": "lewat description bukan summary"}]
    assert cl.accepted_outline_content_digest(a) == cl.accepted_outline_content_digest(b)


def test_content_digest_changes_when_title_or_order_changes_too():
    """Still a SUPERSET of outline_digest's coverage, not a replacement that narrows it."""
    a = _outline(2)
    b = [dict(c) for c in a]
    b[0]["title"] = "Judul Baru"
    assert cl.accepted_outline_content_digest(a) != cl.accepted_outline_content_digest(b)


# ===========================================================================
# 2. canon_lite_semantic_source — build/parse/validate (unit level)
# ===========================================================================
def test_build_semantic_source_round_trips_and_binds():
    outline = _outline(2)
    src = css.build_semantic_source_v1(
        outline_chapters=outline, bible_text=BIBLE, entities=(_entity(),),
        anchors=(_anchor(),), one_time_events=(_event(),))
    assert src.verify_sha256() is True
    assert src.binds_outline(outline) is True
    assert src.has_any_semantic_content() is True


def test_binds_outline_is_false_after_a_summary_edit():
    outline = _outline(2)
    src = css.build_semantic_source_v1(outline_chapters=outline, bible_text=BIBLE,
                                     entities=(_entity(),))
    mutated = [dict(c) for c in outline]
    mutated[0]["summary"] = "DIUBAH SETELAH BINDING"
    assert src.binds_outline(mutated) is False


def test_binds_outline_is_false_and_does_not_raise_on_a_broken_outline():
    outline = _outline(2)
    src = css.build_semantic_source_v1(outline_chapters=outline, bible_text=BIBLE)
    assert src.binds_outline([]) is False           # empty: no accepted outline at all
    assert src.binds_outline("not a list") is False  # nonsense shape


def test_has_any_semantic_content_is_false_when_all_three_tuples_are_empty():
    outline = _outline(1)
    src = css.build_semantic_source_v1(outline_chapters=outline, bible_text=BIBLE)
    assert src.entities == () and src.anchors == () and src.one_time_events == ()
    assert src.has_any_semantic_content() is False


@pytest.mark.parametrize("field", ["entities", "anchors", "one_time_events"])
def test_has_any_semantic_content_is_true_with_only_one_tuple_populated(field):
    """The gate is OR across the three, not AND — any single one is enough for authority.

    🔴 PARAMETRIZED ON A STRING, THE ACTUAL ROW BUILT INSIDE THE TEST — not
    `@pytest.mark.parametrize("kwargs", [{"entities": (_entity(),)}, ...])`. That decorator's
    argument LIST is evaluated once, at COLLECTION time, which is BEFORE
    `test_canon_lite_module_has_no_import_time_side_effects` (a different file, sorted
    earlier) reloads `canon_lite` during its own EXECUTION — so an entity pre-built at
    collection time would carry the stale, pre-reload `CanonEntityV1` into a test that runs
    afterward, and fail the exact same way the production module's `from canon_lite import
    CanonEntityV1` did (see that module's docstring). Building the row here, inside the test
    body, means `_entity()`/`_anchor()`/`_event()` resolve `cl.CanonAnchorV1` etc fresh, at
    EXECUTION time, exactly like every other row-builder call in this file already does.
    """
    row_by_field = {"entities": _entity(), "anchors": _anchor(), "one_time_events": _event()}
    kwargs = {field: (row_by_field[field],)}
    outline = _outline(1)
    src = css.build_semantic_source_v1(outline_chapters=outline, bible_text=BIBLE, **kwargs)
    assert src.has_any_semantic_content() is True


def test_parser_round_trips_a_well_formed_envelope():
    outline = _outline(2)
    raw = {"entities": [{"canonical_name": "Bima", "aliases": ["Si Bima"]}],
          "anchors": [{"kind": "quantity", "literal": "3 hari"}],
          "one_time_events": [{"description": "ledakan", "occurs_chapter": 2}]}
    src = css.parse_semantic_source_envelope(raw, outline_chapters=outline, bible_text=BIBLE)
    assert [e.canonical_name for e in src.entities] == ["Bima"]
    assert src.entities[0].alias_source == "canon_source"
    assert [a.kind for a in src.anchors] == ["quantity"]
    assert [e.occurs_chapter_order for e in src.one_time_events] == [2]
    assert src.verify_sha256() and src.binds_outline(outline)


@pytest.mark.parametrize("raw,label", [
    ("not a dict", "non-mapping envelope"),
    ({"entities": []}, "missing anchors/one_time_events"),
    ({"entities": [], "anchors": [], "one_time_events": [], "reveals": []}, "unknown field"),
    ({"entities": [{"canonical_name": "X"}], "anchors": [], "one_time_events": []},
     "entity missing aliases"),
    ({"entities": [], "anchors": [{"kind": "date", "literal": "x"}],
      "one_time_events": []}, "bad anchor kind"),
    ({"entities": [], "anchors": [],
      "one_time_events": [{"description": "x", "occurs_chapter": 99}]},
     "occurs_chapter out of range"),
    ({"entities": [], "anchors": [],
      "one_time_events": [{"description": "x", "occurs_chapter": True}]},
     "occurs_chapter as bool"),
])
def test_parser_rejects_every_malformed_shape(raw, label):
    outline = _outline(1)
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        css.parse_semantic_source_envelope(raw, outline_chapters=outline, bible_text=BIBLE)


def test_ids_are_positional_and_never_read_from_the_llm():
    """Even if a hostile/confused response supplied id-shaped keys, the schema does not
    accept them at all (`_ENTITY_FIELDS`/`_ANCHOR_FIELDS`/`_EVENT_FIELDS` have no id key) —
    this row proves the POSITIVE side: real ids always come out positional."""
    outline = _outline(1)
    raw = {"entities": [{"canonical_name": "A", "aliases": []},
                        {"canonical_name": "B", "aliases": []}],
          "anchors": [], "one_time_events": []}
    src = css.parse_semantic_source_envelope(raw, outline_chapters=outline, bible_text=BIBLE)
    assert [e.entity_id for e in src.entities] == ["ent1", "ent2"]


def test_extract_semantic_source_json_finds_the_labelled_fence_only():
    text = (
        "1. CHARACTERS\nRatna\n\n"
        "```json canon_registry\n{\"events\": []}\n```\n\n"
        f"```json {css.SEMANTIC_SOURCE_FENCE_LABEL}\n"
        "{\"entities\": [], \"anchors\": [], \"one_time_events\": []}\n```"
    )
    found = css.extract_semantic_source_json(text)
    assert found == {"entities": [], "anchors": [], "one_time_events": []}, \
        "must find ITS OWN fence, not canon_registry's, even though both are present"


@pytest.mark.parametrize("text", [
    "no fence at all here",
    "```json canon_registry\n{\"events\": []}\n```",         # only the WRONG label
    f"```json {css.SEMANTIC_SOURCE_FENCE_LABEL}\nnot valid json at all\n```",
    "", None, 12345,
])
def test_extract_semantic_source_json_returns_none_never_raises(text):
    assert css.extract_semantic_source_json(text) is None


# ===========================================================================
# 3. build_story_bible(structured_semantic=...) — the same-call contract
# ===========================================================================
async def _run_bible(monkeypatch, *, structured_semantic, output_text, ok=True,
                     finish_reason="stop"):
    """Drive the REAL `build_story_bible`, mocking ONLY `run_worker` — the network
    boundary — so the prompt-building, fence-parsing and return-shape logic all run for
    real."""
    captured = {}

    async def fake_run_worker(worker, prompt, timeout=None, task_id=None):
        captured["system"] = worker.system
        captured["prompt"] = prompt
        return {"ok": ok, "output": output_text, "telemetry": {"finish_reason": finish_reason}}

    monkeypatch.setattr(dyn, "run_worker", fake_run_worker)
    outline = _outline(2)
    result = await dyn.build_story_bible(
        "topik uji", outline, is_fiction=True, language="id",
        structured_semantic=structured_semantic)
    return result, captured, outline


def test_old_callers_get_a_plain_string_and_an_unmodified_prompt(monkeypatch):
    """`structured_semantic` OMITTED entirely — the exact pre-P0-B call shape."""
    async def fake_run_worker(worker, prompt, timeout=None, task_id=None):
        assert css.SEMANTIC_SOURCE_FENCE_LABEL not in worker.system, \
            "an old caller's prompt must not grow the P0-B addendum at all"
        assert css.SEMANTIC_SOURCE_FENCE_LABEL not in prompt
        return {"ok": True, "output": "bible prose only",
                "telemetry": {"finish_reason": "stop"}}
    monkeypatch.setattr(dyn, "run_worker", fake_run_worker)
    result = asyncio.run(dyn.build_story_bible(
        "topik uji", _outline(2), is_fiction=True, language="id"))
    assert isinstance(result, str)
    assert result == "bible prose only"


def test_new_caller_true_gets_a_tuple_even_when_extraction_fails(monkeypatch):
    result, captured, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text="prose, no fence at all"))
    assert isinstance(result, tuple) and len(result) == 2
    text, source = result
    assert text == "prose, no fence at all"
    assert source is None
    assert css.SEMANTIC_SOURCE_FENCE_LABEL in captured["system"], \
        "the addendum asking for the fence must be present when requested"


def test_new_caller_true_parses_a_well_formed_fence(monkeypatch):
    bible = (
        "1. CHARACTERS\nRatna, seorang guru.\n\n"
        f"```json {css.SEMANTIC_SOURCE_FENCE_LABEL}\n"
        '{"entities":[{"canonical_name":"Ratna","aliases":[]}],'
        '"anchors":[{"kind":"time","literal":"pagi hari"}],'
        '"one_time_events":[{"description":"kehilangan kunci","occurs_chapter":1}]}\n```'
    )
    result, _, outline = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text=bible))
    text, source = result
    assert text == bible, "the prose is returned UNCHANGED, fence left in place (matches canon_registry precedent)"
    assert source is not None
    assert [e.canonical_name for e in source.entities] == ["Ratna"]
    assert source.binds_outline(outline)


def test_new_caller_true_survives_a_malformed_fence_without_losing_the_prose(monkeypatch):
    bible = (
        "1. CHARACTERS\nRatna.\n\n"
        f"```json {css.SEMANTIC_SOURCE_FENCE_LABEL}\n"
        '{"entities":[{"canonical_name":"Ratna","aliases":[]}],'
        '"anchors":[{"kind":"NOT_A_REAL_KIND","literal":"x"}],'  # invalid enum
        '"one_time_events":[]}\n```'
    )
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text=bible))
    text, source = result
    assert text == bible, "prose must survive a structured-extraction validation failure"
    assert source is None


def test_new_caller_true_survives_the_extraction_step_itself_raising(monkeypatch):
    """Even a defect INSIDE extraction (not just a bad LLM response) must not take the
    prose down with it — patch the extractor to blow up and confirm the bible still
    returns."""
    bible = f"1. CHARACTERS\nRatna.\n\n```json {css.SEMANTIC_SOURCE_FENCE_LABEL}\n{{}}\n```"

    def _boom(text):
        raise RuntimeError("extraction blew up")
    monkeypatch.setattr(css, "extract_semantic_source_json", _boom)
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text=bible))
    text, source = result
    assert text == bible
    assert source is None


def test_truncated_response_yields_no_bible_and_no_source(monkeypatch):
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text="cut off mid",
        finish_reason="max_tokens"))
    text, source = result
    assert text == ""
    assert source is None


def test_all_attempts_failing_returns_the_empty_tuple_shape(monkeypatch):
    async def always_fails(worker, prompt, timeout=None, task_id=None):
        return {"ok": False, "output": "", "telemetry": {}}
    monkeypatch.setattr(dyn, "run_worker", always_fails)
    result = asyncio.run(dyn.build_story_bible(
        "topik", _outline(2), is_fiction=True, language="id", structured_semantic=True))
    assert result == ("", None)


def test_topic_or_outline_missing_returns_the_empty_shape_immediately(monkeypatch):
    called = []
    monkeypatch.setattr(dyn, "run_worker",
                        lambda *a, **k: called.append(1) or asyncio.sleep(0))
    result = asyncio.run(dyn.build_story_bible(
        "", [], is_fiction=True, language="id", structured_semantic=True))
    assert result == ("", None)
    assert not called, "no LLM call at all when topic/outline are empty"


def test_nonfiction_also_gets_the_addendum_unlike_canon_registry():
    """`canon_registry`'s addendum is fiction-gated; P0-B's is not — nonfiction jobs have
    real named people/dates/events too. Prompt-level check, no LLM call needed."""
    prompt = dyn._story_bible_prompt(
        "topik", _outline(1), "id", False, structured_semantic=True)
    assert css.SEMANTIC_SOURCE_FENCE_LABEL in prompt


def test_the_prompt_carries_the_suppression_exception_when_requested():
    """Same bug class as the `canon_registry` suppression fix this file's own docstring
    documents — the base prompt's "Output ONLY... no preamble, no prose" would otherwise
    swallow the fenced block."""
    on = dyn._story_bible_prompt("t", _outline(1), "id", True, structured_semantic=True)
    off = dyn._story_bible_prompt("t", _outline(1), "id", True, structured_semantic=False)
    assert "EXCEPTION" in on and css.SEMANTIC_SOURCE_FENCE_LABEL in on
    assert css.SEMANTIC_SOURCE_FENCE_LABEL not in off


def test_best_of_n_pairs_each_candidates_own_envelope_with_its_own_prose(monkeypatch):
    """🔴 THE INTERACTION THAT IS EASY TO GET WRONG. Two candidates, two DIFFERENT
    envelopes — the winner picked by the prose judge must carry ITS OWN envelope, never
    the other candidate's. Exercised through the REAL best-of-N branch in
    `narrate_chapters`, not a standalone unit of `build_story_bible`."""
    calls = {"n": 0}

    async def two_candidates(topic, outline, *, is_fiction=True, style=None,
                             language="id", manager_model=None, timeout=None,
                             telemetry_sink=None, extra_negative=None,
                             structured_semantic=False):
        calls["n"] += 1
        i = calls["n"]
        text = f"CANDIDATE {i} PROSE"
        if not structured_semantic:
            return text
        src = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=text,
            entities=(_entity(name=f"Entity-From-Candidate-{i}", eid="ent1"),))
        return text, src

    async def judge_picks_two(sys, user, *, tenant_id, user_id, job_uuid, json_mode):
        return json.dumps({"winner": 2, "reason": "test"}), None

    monkeypatch.setenv("NARASI_BIBLE_BEST_OF", "2")
    # best-of-N is fiction-only (`if _fic: _bo_n = ...`); `_is_fiction_style(None)` returns
    # False on no style at all, which would silently take the single-call branch instead
    # and defeat this row without ever asserting anything wrong. Force it directly rather
    # than depending on which style string the real pakem registry resolves as fiction.
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: True)
    monkeypatch.setattr(dyn, "build_story_bible", two_candidates)
    import laozhang_api as lz
    monkeypatch.setattr(lz, "_narasi_cheap_call", judge_picks_two)

    # Not `_map_chapters()`: that helper would re-patch `build_story_bible` with the
    # single-envelope double, overwriting `two_candidates` above. Drive
    # `narrate_chapters` directly instead, keeping `two_candidates` installed.
    st._write_chapter = Recorder()
    os.environ["NARASI_CANON_LITE_MODE"] = "assist"
    chapters = _outline(2)
    result = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=2, tenant_id=CANARY,
        shared_context=SharedContext(topic="topik", chapters=chapters),
        assist_activation_ready=True))
    assert result.get("ok"), result
    canon = result.get("_canon_lite_canon")
    assert canon is not None
    assert [e.canonical_name for e in canon.entities] == ["Entity-From-Candidate-2"], (
        "the WINNING prose (candidate 2) must carry candidate 2's OWN envelope, not "
        "candidate 1's — pairing them independently would silently mix an unused "
        "candidate's semantics into the delivered book")


# ===========================================================================
# 4. THE SEAM — Dapur A and Dapur B, through narrate_chapters, for real
# ===========================================================================
#
# 🔴 "SHARED SEAM" IS A STRUCTURAL PROPERTY HERE, NOT A CODE PATH THAT CAN DRIFT APART.
#    P0-B's entire validate-and-inject block lives INSIDE `narrate_chapters` — the ONE
#    function BOTH Scenario A (`_run_chaptered`, explicit accepted chapters) and Scenario B
#    (`_run_topic_to_book` -> `outline_from_topic` -> the SAME `_run_chaptered`) call. There
#    is no Scenario-differentiated branch for it to exist on one side of and not the other:
#    a regression here breaks both scenarios together, by construction. The two rows below
#    are the POSITIVE proof of that claim for each entry point independently — if a future
#    change ever did introduce a scenario-specific gate around P0-B, exactly one of these
#    two rows would start failing while the other kept passing, which is the discriminating
#    power a "shared seam" claim needs to be falsifiable at all.

def test_dapur_a_reaches_the_seam_and_produces_semantic_authority(monkeypatch):
    """Scenario A: an explicit, already-accepted outline — never touches
    `outline_from_topic` at all."""
    double = _make_bible_double(entities=(_entity("Dapur A Entity"),))
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok"), res
    canon = res.get("_canon_lite_canon")
    assert canon is not None
    assert [e.canonical_name for e in canon.entities] == ["Dapur A Entity"]
    assert l2.has_semantic_authority(canon) is True
    # And it actually reached the workers — the seam's whole POINT.
    assert rec.calls and all(c.get("canon_text") for c in rec.calls)


def test_dapur_b_reaches_the_seam_and_produces_semantic_authority(monkeypatch):
    """Scenario B: bare topic, real routing through `generate_narration` ->
    `_run_topic_to_book` -> `orchestrator.dynamic.outline_from_topic` (forced
    deterministic via orch_mode=static, so outline GENERATION needs no LLM of its own) ->
    the SAME `_run_chaptered` -> `narrate_chapters` Scenario A reaches."""
    double = _make_bible_double(entities=(_entity("Dapur B Entity"),))
    res, rec = asyncio.run(_drive_dapur_b(monkeypatch, bible_double=double, n_chapters=2))
    assert res.get("ok"), res
    assert res.get("strategy") == "topic_to_book"
    canon = res.get("_canon_lite_canon")
    assert canon is not None
    assert [e.canonical_name for e in canon.entities] == ["Dapur B Entity"]
    assert l2.has_semantic_authority(canon) is True
    assert rec.calls and all(c.get("canon_text") for c in rec.calls)


def test_dapur_a_and_dapur_b_bind_the_same_kind_of_canon(monkeypatch):
    """Not just 'both succeed' — both produce a canon whose entities/anchors/events came
    from the SAME kind of source, through the SAME validation, with the SAME authority
    predicate available on it. Run back to back so a shared-state leak between them would
    also show up here."""
    double = _make_bible_double(anchors=(_anchor("3 hari lagi"),))
    res_a, _ = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    res_b, _ = asyncio.run(_drive_dapur_b(monkeypatch, bible_double=double, n_chapters=2))
    for label, res in (("A", res_a), ("B", res_b)):
        assert res.get("ok"), (label, res)
        canon = res.get("_canon_lite_canon")
        assert canon is not None, label
        assert [a.literal for a in canon.anchors] == ["3 hari lagi"], label
        assert l2.has_semantic_authority(canon) is True, label


# ── 4b. negative controls — refusal BEFORE the MAP, never a silent empty canon ──────────
def _double_returning_none_source(text="prose, no fence at all"):
    """Simulates exactly what `build_story_bible` itself returns when the fence is
    absent, unparseable, or fails validation — ALL THREE collapse to `(text, None)` by
    the time `narrate_chapters` sees them (proven separately, at the `build_story_bible`
    level, by the `test_new_caller_true_*` rows in section 3). This is the ONE shape the
    seam actually has to handle for "extraction produced nothing usable"."""
    async def _fake(topic, outline, *, is_fiction=True, style=None, language="id",
                    manager_model=None, timeout=None, telemetry_sink=None,
                    extra_negative=None, structured_semantic=False):
        return text if not structured_semantic else (text, None)
    return _fake


def test_assist_refuses_before_the_map_when_the_bible_never_returns_a_fence(monkeypatch):
    """No fence, unparseable, or failed validation — indistinguishable from `narrate_chapters`'s
    point of view, and all correctly bucketed as `unavailable`."""
    double = _double_returning_none_source("prose with no fence whatsoever")
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_unavailable"
    assert rec.calls == [], "no worker call happened — the refusal is BEFORE the MAP"


def test_assist_refuses_before_the_map_when_extraction_finds_nothing(monkeypatch):
    """Fence present, parses fine, but all three tuples are empty — parses cleanly,
    carries no authority. Must refuse just as surely as an absent fence."""
    double = _make_bible_double()   # entities=(), anchors=(), one_time_events=() — default
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_empty"
    assert rec.calls == []


def test_assist_refuses_cleanly_even_if_build_story_bible_violates_its_own_contract(
        monkeypatch):
    """🔴 DEFENSE IN DEPTH, NOT A REAL SCENARIO. `build_story_bible()`'s contract is
    `None` or a validated `CanonLiteSemanticSourceV1` — never a bare dict — but this row
    proves `narrate_chapters` does not simply TRUST that contract. Before the `isinstance`
    guard, a value that merely LOOKED like a source (any truthy object without
    `.verify_sha256()`) raised `AttributeError` inside the try block, which the outer
    `except` caught and reported as the unrelated, less precise
    `canon_lite_assist_canon_unavailable` — still a refusal, still before the MAP, but the
    wrong diagnosis. The guard makes the diagnosis honest without changing the outcome."""
    async def _lying_double(topic, outline, *, is_fiction=True, style=None,
                            language="id", manager_model=None, timeout=None,
                            telemetry_sink=None, extra_negative=None,
                            structured_semantic=False):
        text = "prose"
        if not structured_semantic:
            return text
        return text, {"entities": "not a CanonLiteSemanticSourceV1 at all"}
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=_lying_double, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_unavailable"
    assert rec.calls == []


def test_assist_refuses_when_the_story_bible_call_itself_raises(monkeypatch):
    double = _make_bible_double(raise_exc=RuntimeError("bible call blew up"))
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok") is False, res
    assert rec.calls == []


def test_shadow_tolerates_an_empty_envelope_and_still_delivers(monkeypatch):
    """Shadow never refuses — it proceeds on empty tuples, same 'tolerate and log'
    discipline as an unavailable canon."""
    double = _make_bible_double()   # empty envelope
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2,
                                         mode="shadow"))
    assert res.get("ok"), res
    assert rec.calls, "shadow still ran the MAP"
    canon = res.get("_canon_lite_canon")
    if canon is not None:
        assert l2.has_semantic_authority(canon) is False


# ── 4c. mutation-after-binding — the outline moves between bind-time and use-time ───────
def test_a_summary_edit_after_binding_is_detected_and_assist_refuses(monkeypatch):
    """🔴 THE CHECK `outline_sha256` CANNOT DO. The outline's TITLE/ID/ORDER stay
    identical — only the SUMMARY changes, between the Story Bible call and the canon
    build. `outline_sha256` (title/id/order only) would not notice; the new
    `accepted_outline_content_sha256` binding must."""
    calls = {"n": 0}

    async def mutating_bible(topic, outline, *, is_fiction=True, style=None,
                             language="id", manager_model=None, timeout=None,
                             telemetry_sink=None, extra_negative=None,
                             structured_semantic=False):
        calls["n"] += 1
        source = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text="bible text", entities=(_entity(),))
        # Mutate the SAME chapter dicts the caller handed in — a realistic in-place edit,
        # not a copy substitution the caller could not have produced.
        if isinstance(outline, list) and outline:
            outline[0]["summary"] = "SUMMARY MUTATED AFTER THE BIBLE CALL RETURNED"
        return "bible text", source

    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=mutating_bible, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_unbound"
    assert rec.calls == []
    assert calls["n"] == 1, "the bible call itself still only happens once"


# ── 4d. reveals stay honestly empty ──────────────────────────────────────────────────────
def test_reveals_are_never_populated_by_p0b(monkeypatch):
    double = _make_bible_double(
        entities=(_entity(),), anchors=(_anchor(),), one_time_events=(_event(),))
    res, _ = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok"), res
    canon = res.get("_canon_lite_canon")
    assert canon is not None
    assert canon.reveals == (), \
        "P0-B must never populate reveals — that is CLAIM_REVEAL's job, not this one's"
    # And the authority predicate agrees reveals don't count either way (canon_lite_l2.py).
    assert l2.has_semantic_authority(canon) is True   # true because of entities, not reveals


# ── 4e. off-mode: zero new code executed, exact pre-P0-B call shape ─────────────────────
def test_off_mode_never_calls_build_story_bible_with_structured_semantic(monkeypatch):
    captured = {}

    async def spy(topic, outline, *, is_fiction=True, style=None, language="id",
                  manager_model=None, timeout=None, telemetry_sink=None,
                  extra_negative=None, structured_semantic=False):
        captured["structured_semantic"] = structured_semantic
        captured["called"] = captured.get("called", 0) + 1
        return "bible text"   # the EXACT pre-P0-B return shape

    monkeypatch.setattr(dyn, "build_story_bible", spy)
    st._write_chapter = Recorder()
    os.environ.pop("NARASI_CANON_LITE_MODE", None)   # off
    chapters = _outline(2)
    res = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=2,
        shared_context=SharedContext(topic="topik", chapters=chapters)))
    assert res.get("ok"), res
    assert captured.get("called") == 1, "story bible still runs off-mode — unrelated to P0-B"
    assert captured["structured_semantic"] is False, \
        "off-mode must never request the structured envelope"
    assert "_canon_lite_canon" not in res


def test_off_mode_result_carries_no_canon_lite_keys_at_all(monkeypatch):
    double = _make_bible_double(entities=(_entity(),))
    monkeypatch.setattr(dyn, "build_story_bible", double)
    st._write_chapter = Recorder()
    os.environ.pop("NARASI_CANON_LITE_MODE", None)
    chapters = _outline(2)
    res = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=2,
        shared_context=SharedContext(topic="topik", chapters=chapters)))
    assert res.get("ok"), res
    assert "_canon_lite_canon" not in res
    assert "canon_lite_binding" not in res


# ===========================================================================
# 5. No provider call, anywhere in this file
# ===========================================================================
def test_this_files_own_fakes_never_touch_the_network():
    """Sanity check on the test doubles themselves: every one of them is a plain
    coroutine with no import of an HTTP/provider client. The real proof is structural —
    every row above ran under the same outbound-network guard as the rest of the suite
    (see `outbound_guard_plugin`) and none of them failed on a blocked call."""
    import inspect
    src = inspect.getsource(_make_bible_double)
    for banned in ("requests", "httpx", "OpenAI", "genai", "urlopen"):
        assert banned not in src


# ===========================================================================
# 5b. ROUND-2 BLOCKERS — event identity, bible provenance, shadow, nonfiction
# ===========================================================================

# ── B1. event semantic identity survives into the canon and the QC projection ──────────
def test_two_different_events_in_one_chapter_stay_distinguishable():
    """🔴 THE ROUND-1 DEFECT, REPRODUCED AND CLOSED. `CanonEventV1` stores no description
       field — only `event_id` and `occurs_chapter_order`. With a POSITIONAL id, "kehilangan
       kunci" and "ledakan gedung" in chapter 1 both became
       `{"event_id":"evt1","occurs_chapter_order":1}`: identical rows, identical
       `source_sha256`, identical canon hash. The extraction carried a real distinction that
       died at the schema boundary, and an event-only canon still counted as semantic
       authority — a canary that verifies nothing.
    """
    outline = _outline(2)
    def _src(desc):
        return css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [],
             "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
            outline_chapters=outline, bible_text=BIBLE)
    a, b = _src("kehilangan kunci"), _src("ledakan gedung")
    assert a.one_time_events[0].event_id != b.one_time_events[0].event_id
    assert a.one_time_events[0].to_canonical_obj() != b.one_time_events[0].to_canonical_obj()
    assert a.source_sha256 != b.source_sha256


def test_the_same_description_in_different_chapters_is_two_events():
    """Two genuinely different OCCURRENCES — the id must distinguish them too."""
    outline = _outline(3)
    def _src(chapter):
        return css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [],
             "one_time_events": [{"description": "ledakan", "occurs_chapter": chapter}]},
            outline_chapters=outline, bible_text=BIBLE)
    assert _src(1).one_time_events[0].event_id != _src(3).one_time_events[0].event_id


def test_the_same_description_twice_in_one_chapter_is_rejected_as_a_duplicate():
    """One event listed twice is a malformed envelope, not two events — and the
    content-derived id makes that detectable where a positional id hid it."""
    outline = _outline(2)
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": "ledakan", "occurs_chapter": 1},
                {"description": "ledakan", "occurs_chapter": 1}]},
            outline_chapters=outline, bible_text=BIBLE)


def test_event_ids_are_legible_and_schema_valid():
    """MEANINGFUL, not just unique: a bare hash would be stable and collision-free while
    still telling an extractor nothing about WHICH event to look for."""
    outline = _outline(2)
    src = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [],
         "one_time_events": [{"description": "Ratna kehilangan kunci", "occurs_chapter": 1}]},
        outline_chapters=outline, bible_text=BIBLE)
    eid = src.one_time_events[0].event_id
    assert "kehilangan" in eid and "kunci" in eid, eid
    assert cl._ID_RE.match(eid), f"event_id must satisfy canon_lite's own id rule: {eid}"
    assert len(eid) <= cl.MAX_ID_LEN


@pytest.mark.parametrize("desc", ["", "   ", "\t\n"])
def test_an_empty_description_is_rejected_outright(desc):
    """A blank description is a malformed envelope, not an unsluggable one — the event has
    no semantic content to preserve in the first place, so it never reaches id generation.
    `canon_lite._req_str` already enforces this; this row pins that it stays enforced."""
    outline = _outline(2)
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [],
             "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
            outline_chapters=outline, bible_text=BIBLE)


@pytest.mark.parametrize("desc", ["日本語だけの説明", "!!!", "...---...", "→←↑↓"])
def test_an_unsluggable_description_still_produces_a_unique_valid_id(desc):
    """Legibility is best-effort; UNIQUENESS is not. A NON-EMPTY description that carries no
    `[a-z0-9]` at all slugifies to nothing and falls back to a hash-only id — still valid,
    still distinct. (An empty description is a different case entirely — rejected above.)"""
    outline = _outline(2)
    src = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [],
         "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
        outline_chapters=outline, bible_text=BIBLE)
    eid = src.one_time_events[0].event_id
    assert cl._ID_RE.match(eid), eid
    other = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [],
         "one_time_events": [{"description": desc + " berbeda", "occurs_chapter": 1}]},
        outline_chapters=outline, bible_text=BIBLE)
    assert eid != other.one_time_events[0].event_id


def test_event_identity_survives_into_the_canon_and_the_qc_projection(monkeypatch):
    """🔴 END TO END, THROUGH THE REAL SEAM. Not just the envelope — the CANON hash, the
       rendered canon QC actually reads, and `_accepted_canon_ids_by_claim_type`'s
       CLAIM_ONE_TIME_EVENT set must all distinguish the two events."""
    def _double(desc):
        async def _fake(topic, outline, *, is_fiction=True, style=None, language="id",
                        manager_model=None, timeout=None, telemetry_sink=None,
                        extra_negative=None, structured_semantic=False):
            if not structured_semantic:
                return BIBLE
            return BIBLE, css.parse_semantic_source_envelope(
                {"entities": [], "anchors": [],
                 "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
                outline_chapters=outline, bible_text=BIBLE)
        return _fake

    res_a, _ = asyncio.run(_map_chapters(
        monkeypatch, bible_double=_double("kehilangan kunci"), n=2))
    res_b, _ = asyncio.run(_map_chapters(
        monkeypatch, bible_double=_double("ledakan gedung"), n=2))
    canon_a, canon_b = res_a.get("_canon_lite_canon"), res_b.get("_canon_lite_canon")
    assert canon_a is not None and canon_b is not None
    assert canon_a.canon_sha256 != canon_b.canon_sha256, "canon hash lost the distinction"
    assert cl.render_canon(canon_a) != cl.render_canon(canon_b), \
        "the rendering QC actually reads lost the distinction"
    ids_a = l2._accepted_canon_ids_by_claim_type(canon_a)[l2.CLAIM_ONE_TIME_EVENT]
    ids_b = l2._accepted_canon_ids_by_claim_type(canon_b)[l2.CLAIM_ONE_TIME_EVENT]
    assert ids_a and ids_b and ids_a != ids_b, \
        "the QC projection cannot tell the two events apart"


# ── B2. the source is bound to the WINNING bible, not just the outline ─────────────────
def test_a_surgical_bible_mutation_after_extraction_makes_assist_refuse(monkeypatch):
    """🔴 THE PROVENANCE GAP. `narrate_chapters` can rewrite `ctx.canonical_facts` IN PLACE
       after the Story Bible call — the lane-ledger surgical patch does exactly that
       (`_bible2.replace(old, new, 1)`), leaving the OUTLINE untouched. Round 1 bound the
       source to the outline only, so the canon would have received tuples extracted from
       the OLD bible alongside `advisory_bible_text` from the NEW one, both passing every
       validation, with nothing recording that they disagree.
    """
    async def _surgical(topic, outline, *, is_fiction=True, style=None, language="id",
                        manager_model=None, timeout=None, telemetry_sink=None,
                        extra_negative=None, structured_semantic=False):
        original = "ORIGINAL BIBLE TEXT"
        if not structured_semantic:
            return original
        src = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=original, entities=(_entity(),))
        # The caller pins a DIFFERENT bible than the one this envelope came from — exactly
        # what the surgical ledger patch does a few statements later in the real code.
        return "SURGICALLY PATCHED BIBLE TEXT", src

    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=_surgical, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_bible_changed"
    assert rec.calls == [], "refusal must happen BEFORE the MAP"


def test_a_reroll_without_its_own_envelope_makes_assist_refuse(monkeypatch):
    """A reroll that replaced the bible but produced no usable envelope of its own must
    NOT fall back to the superseded extraction."""
    async def _reroll_lost_envelope(topic, outline, *, is_fiction=True, style=None,
                                    language="id", manager_model=None, timeout=None,
                                    telemetry_sink=None, extra_negative=None,
                                    structured_semantic=False):
        if not structured_semantic:
            return "REROLLED BIBLE"
        src = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text="FIRST BIBLE", entities=(_entity(),))
        return "REROLLED BIBLE", src

    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=_reroll_lost_envelope,
                                          n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_bible_changed"
    assert rec.calls == []


def test_the_matching_bible_still_passes(monkeypatch):
    """Positive control: the binding must not refuse the ordinary case where the pinned
    bible IS the one the envelope came from."""
    double = _make_bible_double(entities=(_entity(),))
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok"), res
    assert res["_canon_lite_canon"] is not None
    assert rec.calls


def test_binds_bible_uses_the_same_projection_as_the_canon():
    """`canon_lite.advisory_bible_digest` is ONE function with two callers — the canon's own
    `advisory_bible_sha256` and the source's `bible_sha256`. Two separately-written hashes
    of 'the bible' could disagree even when nothing changed, and the binding check would be
    comparing numbers never guaranteed to match."""
    outline = _outline(2)
    src = css.build_semantic_source_v1(outline_chapters=outline, bible_text=BIBLE,
                                       entities=(_entity(),))
    cfg = cl.build_job_config_snapshot(outline_chapters=outline, target_language="id",
                                       narration_style=None)
    canon = cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg,
                                   advisory_bible_text=BIBLE,
                                   entities=src.entities)
    assert src.bible_sha256 == canon.advisory_bible_sha256


def test_a_source_cannot_be_built_without_naming_its_bible():
    """These tuples were extracted FROM a bible; an envelope claiming none has unstated
    provenance and must not exist at all."""
    outline = _outline(2)
    for empty in (None, "", "   "):
        with pytest.raises(cl.CanonSchemaError):
            css.build_semantic_source_v1(outline_chapters=outline, bible_text=empty,
                                         entities=(_entity(),))


# ── B3. shadow must not change what the user gets ─────────────────────────────────────
def test_shadow_never_requests_the_structured_envelope(monkeypatch):
    """🔴 SHADOW'S GOVERNING INVARIANT. Requesting the envelope changes the Story Bible
       SYSTEM prompt, the user prompt, the output tokens, AND — because the fence is
       deliberately left in the returned text — `ctx.canonical_facts`, which every chapter
       worker receives as its pinned prefix. Round 1 had shadow requesting it, silently
       redefining a mode whose whole contract is "may observe, never prevent; no
       user-visible change".
    """
    captured = {}

    async def spy(topic, outline, *, is_fiction=True, style=None, language="id",
                  manager_model=None, timeout=None, telemetry_sink=None,
                  extra_negative=None, structured_semantic=False):
        captured["structured_semantic"] = structured_semantic
        return "bible prose only"

    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=spy, n=2,
                                          mode="shadow"))
    assert res.get("ok"), res
    assert captured.get("structured_semantic") is False, \
        "shadow requested the structured envelope — that is a user-visible change"
    assert rec.calls, "shadow still ran the MAP"


def test_shadow_chapter_input_is_identical_with_and_without_p0b_available(monkeypatch):
    """The property that actually matters to a user: what the chapter workers were HANDED.
    A shadow job's `canonical_facts` must not carry a JSON fence, and its workers must see
    the same prefix they would have seen before P0-B existed."""
    double = _make_bible_double(entities=(_entity(),), text="PLAIN BIBLE PROSE")
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2,
                                          mode="shadow"))
    assert res.get("ok"), res
    assert rec.calls
    for call in rec.calls:
        facts = getattr(call["ctx"], "canonical_facts", "") or ""
        assert css.SEMANTIC_SOURCE_FENCE_LABEL not in facts, \
            "a shadow job's chapter prefix carries the P0-B fence"
        assert facts == "PLAIN BIBLE PROSE"


# ── B4. nonfiction gets no ungrounded authority ───────────────────────────────────────
def test_nonfiction_assist_refuses_rather_than_taking_ungrounded_authority(monkeypatch):
    """🔴 SCOPE GATE. The envelope's tuples come from an LLM restating or inventing the
       fact-sheet's content. For FICTION that is legitimate — the bible DECIDES the
       invented specifics, so it IS the authority. NONFICTION runs the opposite regime:
       pin only what the premise gives, mark unknowns [VERIFY], never fabricate. Promoting
       LLM-produced tuples to canon authority there would let QC enforce ungrounded claims
       about real people and dates. Until a provenance path proves job_input/source_grounded
       grounding, nonfiction refuses — bounded, before the MAP — rather than degrading.
    """
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: False)
    double = _make_bible_double(entities=(_entity(),))
    res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2))
    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_not_eligible"
    assert rec.calls == [], "refusal before the MAP"


def test_nonfiction_never_requests_the_structured_envelope(monkeypatch):
    """Not merely refused after the fact — the request is never made, so a nonfiction job's
    Story Bible prompt and token bill are untouched."""
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: False)
    captured = {}

    async def spy(topic, outline, *, is_fiction=True, style=None, language="id",
                  manager_model=None, timeout=None, telemetry_sink=None,
                  extra_negative=None, structured_semantic=False):
        captured["structured_semantic"] = structured_semantic
        return "continuity sheet prose"

    asyncio.run(_map_chapters(monkeypatch, bible_double=spy, n=2))
    assert captured.get("structured_semantic") is False


def test_nonfiction_shadow_and_off_are_unaffected(monkeypatch):
    """The nonfiction refusal is an ASSIST gate. Shadow and off must still deliver."""
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: False)
    double = _make_bible_double(entities=(_entity(),))
    for mode in ("shadow", "off"):
        res, rec = asyncio.run(_map_chapters(monkeypatch, bible_double=double, n=2,
                                              mode=mode))
        assert res.get("ok"), (mode, res)
        assert rec.calls, mode


# ===========================================================================
# 6. MUTATION HARNESS — bypass, empty-authority, wrong-binding, shared-seam
# ===========================================================================
#
# Same discipline as `test_canon_lite_l3_assist_activation.py`'s own harness: each mutant
# is applied to a COPY of the tree (this repo's `python/` — `canon_lite_semantic_source.py`
# included, it is just another file under `python/` — plus this test file) and run there in
# a subprocess. PATTERN-MISS (the old text no longer exists) and SURVIVED (the mutation
# applied but the row still passed) are both reported, not just a bare non-zero exit.
#
# 🔴 "DAPUR-B-ONLY WIRING" IS TESTED BY REVERTING THE WHOLE SHARED BLOCK, ON PURPOSE. P0-B's
#    validate-and-inject logic lives entirely inside `narrate_chapters`, the ONE function
#    both `_run_chaptered` (Scenario A) and `_run_topic_to_book` -> `_run_chaptered`
#    (Scenario B) call — there is no per-scenario branch for a mutation to disable on one
#    side only. Reverting the shared block therefore breaks BOTH `test_dapur_a_reaches_...`
#    and `test_dapur_b_reaches_...` TOGETHER, and that joint failure is the actual proof: a
#    regression that broke only one scenario would require a scenario-specific branch to
#    exist in the first place, and none does. Two independent kill-witnesses, not one,
#    is what gives that claim its falsifiability — a future change that DID introduce a
#    scenario-specific gate would fail exactly one of the two, not both.

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

MUTANTS = [
    {
        "id": "M1-assist-bypasses-the-refusal",
        "why": ("the semantic-authority refusal check is removed, so assist arms on a "
                "canon that structurally cannot pass has_semantic_authority()"),
        "edits": [(
            "python/orchestrator/static.py",
            "        # P0-B: a non-None canon is not enough for assist — it must carry REAL semantic\n"
            "        # authority. `_cl_semantic_reason` is always a bounded literal from the fixed set\n"
            "        # above; never provider/LLM text (§C12-style discipline, same as the L3 reason\n"
            "        # codes this mirrors).\n"
            "        if _cl_assist and _cl_semantic_valid is None:\n"
            "            return _assist_refuse(_cl_semantic_reason)\n",
            "",
        )],
        "nodes": ["test_assist_refuses_before_the_map_when_the_bible_never_returns_a_fence",
                  "test_assist_refuses_before_the_map_when_extraction_finds_nothing"],
    },
    {
        "id": "M2-empty-authority-treated-as-ok",
        "why": ("the has_any_semantic_content() check is dropped, so a well-formed but "
                "EMPTY envelope is accepted as valid — the exact false-canary shape"),
        "edits": [(
            "python/orchestrator/static.py",
            "                elif not _cl_semantic_source.has_any_semantic_content():\n"
            "                    _cl_semantic_reason = \"canon_lite_assist_semantic_source_empty\"\n",
            "",
        )],
        "nodes": ["test_assist_refuses_before_the_map_when_extraction_finds_nothing"],
    },
    {
        "id": "M3-wrong-binding-outline-mutation-undetected",
        "why": ("the binds_outline() re-check is dropped, so a summary edited after the "
                "Story Bible call returns is never noticed"),
        "edits": [(
            "python/orchestrator/static.py",
            "                elif not _cl_semantic_source.binds_outline(_cl_outline):\n"
            "                    _cl_semantic_reason = \"canon_lite_assist_semantic_source_unbound\"\n",
            "",
        )],
        "nodes": ["test_a_summary_edit_after_binding_is_detected_and_assist_refuses"],
    },
    {
        "id": "M4-shared-seam-removed",
        "why": ("the entire P0-B validate-and-inject block is removed from narrate_chapters "
                "— the ONE function both Dapur A and Dapur B call — so BOTH scenarios lose "
                "semantic authority together; see the section docstring above for why this "
                "is the correct way to test a 'shared seam' claim"),
        "edits": [(
            "python/orchestrator/static.py",
            "                entities=(_cl_semantic_valid.entities if _cl_semantic_valid else ()),\n"
            "                anchors=(_cl_semantic_valid.anchors if _cl_semantic_valid else ()),\n"
            "                one_time_events=(\n"
            "                    _cl_semantic_valid.one_time_events if _cl_semantic_valid else ()),\n",
            "                entities=(), anchors=(), one_time_events=(),\n",
        )],
        "nodes": ["test_dapur_a_reaches_the_seam_and_produces_semantic_authority",
                  "test_dapur_b_reaches_the_seam_and_produces_semantic_authority"],
    },
    {
        "id": "M5-off-mode-requests-structured-semantic",
        "why": ("off-mode starts requesting the structured envelope too — growing every "
                "off-mode job's prompt/output-token bill for a feature it never asked for, "
                "violating 'off-path byte-identical, no new call'"),
        "edits": [(
            "python/orchestrator/static.py",
            "    _cl_wants_semantic = (_cl_mode == \"assist\") and _is_fiction_style(style)",
            "    _cl_wants_semantic = True",
        )],
        "nodes": ["test_off_mode_never_calls_build_story_bible_with_structured_semantic"],
    },
    {
        "id": "M6-event-description-collision",
        "why": ("event ids go back to positional (`evt1`), so two DIFFERENT events in the "
                "same chapter collapse to byte-identical canon rows and QC cannot tell "
                "which event it is supposed to find"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "            event_id=_event_id(row[\"description\"], occurs), occurs_chapter_order=occurs))",
            "            event_id=_positional_id(\"evt\", i), occurs_chapter_order=occurs))",
        )],
        "nodes": ["test_two_different_events_in_one_chapter_stay_distinguishable",
                  "test_event_identity_survives_into_the_canon_and_the_qc_projection"],
    },
    {
        "id": "M7-bible-swap-after-extraction-undetected",
        "why": ("the binds_bible() recheck is dropped, so a bible replaced after extraction "
                "(surgical ledger patch, or a reroll that lost its envelope) pairs new prose "
                "with the superseded semantic tuples and both still pass validation"),
        "edits": [(
            "python/orchestrator/static.py",
            "                elif not _cl_semantic_source.binds_bible(_cl_bible_now):\n",
            "                elif False:\n",
        )],
        "nodes": ["test_a_surgical_bible_mutation_after_extraction_makes_assist_refuse",
                  "test_a_reroll_without_its_own_envelope_makes_assist_refuse"],
    },
    {
        "id": "M8-shadow-requests-structured-semantic",
        "why": ("shadow starts requesting the structured envelope again — changing the "
                "Story Bible prompt, its output tokens, and the ctx.canonical_facts every "
                "chapter worker receives, which is a user-visible change shadow may not make"),
        "edits": [(
            "python/orchestrator/static.py",
            "    _cl_wants_semantic = (_cl_mode == \"assist\") and _is_fiction_style(style)",
            "    _cl_wants_semantic = (_cl_mode in (\"shadow\", \"assist\")) and _is_fiction_style(style)",
        )],
        "nodes": ["test_shadow_never_requests_the_structured_envelope"],
    },
    {
        "id": "M9-nonfiction-granted-authority-without-grounding",
        "why": ("the fiction gate is removed, so a nonfiction job — whose regime forbids "
                "fabrication and marks unknowns [VERIFY] — has LLM-invented entities/"
                "anchors/events promoted to canon authority QC then enforces against"),
        "edits": [(
            "python/orchestrator/static.py",
            "    _cl_wants_semantic = (_cl_mode == \"assist\") and _is_fiction_style(style)",
            "    _cl_wants_semantic = (_cl_mode == \"assist\")",
        )],
        "nodes": ["test_nonfiction_assist_refuses_rather_than_taking_ungrounded_authority"],
    },
]


def _build_mutant_tree(root, edits):
    shutil.copytree(REPO / "python", root / "python", ignore=_IGNORE)
    (root / "tests" / "python").mkdir(parents=True)
    for name in ("conftest.py", Path(__file__).name):
        shutil.copy2(REPO / "tests" / "python" / name, root / "tests" / "python" / name)
    for rel, old, new in edits:
        target = root / rel
        text = target.read_text("utf-8")
        found = text.count(old)
        assert found == 1, (
            f"PATTERN-MISS in {rel}: expected exactly 1 occurrence of the pre-mutation "
            f"text, found {found}. The code moved and this mutation now describes "
            f"nothing — the harness is stale, not the tree.")
        target.write_text(text.replace(old, new, 1), "utf-8")


@pytest.mark.parametrize("mutant", MUTANTS, ids=[m["id"] for m in MUTANTS])
def test_the_mutation_is_killed(tmp_path, mutant):
    root = tmp_path / "tree"
    root.mkdir()
    _build_mutant_tree(root, mutant["edits"])

    this_file = Path(__file__).name
    nodes = [f"tests/python/{this_file}::{n}" for n in mutant["nodes"]]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=root, capture_output=True, text=True, timeout=600)
    assert proc.returncode != 0, (
        f"SURVIVED {mutant['id']} — {mutant['why']}. The mutation applied cleanly and "
        f"{', '.join(mutant['nodes'])} still passed, so the fix is unguarded.\n"
        f"{proc.stdout[-3000:]}")
    assert " failed" in proc.stdout, (
        f"{mutant['id']} exited non-zero WITHOUT a test failure — the mutant broke "
        f"collection rather than being caught:\n{proc.stdout[-3000:]}\n"
        f"{proc.stderr[-2000:]}")


def test_the_mutation_harness_itself_can_fail(tmp_path):
    """The control for the control: a harness that reports every mutant as killed because
    the subprocess errors on import, or because pytest never collected anything, would look
    identical to a working one. Drive it with a NO-OP mutation and require a PASS."""
    root = tmp_path / "tree"
    root.mkdir()
    _build_mutant_tree(root, [])
    this_file = Path(__file__).name
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"tests/python/{this_file}::test_content_digest_is_stable_for_identical_content"],
        cwd=root, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        "the unmutated copy of the tree does not pass, so every SURVIVED/killed verdict "
        f"from this harness is meaningless:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
