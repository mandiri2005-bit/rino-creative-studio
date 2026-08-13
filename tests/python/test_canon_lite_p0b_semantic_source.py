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
        return text, source, None
    return _fake


def _entity(name="Ratna", eid="ent1"):
    return cl.CanonEntityV1(entity_id=eid, canonical_name=name, aliases=(),
                            alias_source="none")


def _anchor(literal="pagi hari", kind="time", aid="anc1"):
    return cl.CanonAnchorV1(anchor_id=aid, kind=kind, literal=literal)


def _event(order=1, eid="evt1", label=None):
    # The label defaults off the id so two `_event()`s with different ids stay
    # distinguishable — `_reject_indistinguishable_events` refuses a shared label.
    return cl.CanonEventV1(event_id=eid, occurs_chapter_order=order,
                           label=label or f"peristiwa {eid}")


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


def _structured(bible_text, semantic="__omit__", **extra):
    """Build a response in the P0-B STRUCTURED contract: one JSON object, prose under
    `bible_text`, envelope under `semantic_source`, anything else as a sidecar key."""
    obj = {css.STRUCTURED_BIBLE_TEXT_KEY: bible_text}
    if semantic != "__omit__":
        obj[css.STRUCTURED_BIBLE_SEMANTIC_KEY] = semantic
    obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


_GOOD_SEMANTIC = {
    "entities": [{"canonical_name": "Ratna", "aliases": []}],
    "anchors": [{"kind": "time", "literal": "pagi hari"}],
    "one_time_events": [{"description": "kehilangan kunci", "occurs_chapter": 1}],
}


def test_new_caller_true_gets_a_tuple_even_when_extraction_fails(monkeypatch):
    """A response that ignores the structured contract FAILS THE ATTEMPT OVER.

    Changed by the `oehhe741` fix, deliberately. Under the old fence contract this kept
    the prose and returned `source=None`, which guaranteed a later assist refusal AFTER
    the whole job had been paid for. A response that is not the object means JSON mode was
    not applied to the call at all, so another rung is exactly what might fix it; with a
    single-model chain there is no other rung and the honest result is the empty shape.
    """
    result, captured, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text="prose, not an object"))
    assert isinstance(result, tuple) and len(result) == 3
    assert result == ("", None, None), "a non-conforming response must not become a bible"
    assert css.STRUCTURED_BIBLE_SEMANTIC_KEY in captured["system"], \
        "the addendum asking for the structured object must be present when requested"


def test_new_caller_true_parses_a_well_formed_structured_object(monkeypatch):
    prose = "1. CHARACTERS\nRatna, seorang guru."
    result, _, outline = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True,
        output_text=_structured(prose, _GOOD_SEMANTIC)))
    text, source, _registry = result
    # 🔴 P0-B #3: ONLY the prose comes back. The envelope is a sibling key, so it can never
    #    reach `ctx.canonical_facts` or a chapter prompt — under the old fence contract the
    #    returned text was the WHOLE response, JSON block included.
    assert text == prose
    assert css.STRUCTURED_BIBLE_SEMANTIC_KEY not in text
    assert "canonical_name" not in text
    assert source is not None
    assert [e.canonical_name for e in source.entities] == ["Ratna"]
    assert source.binds_outline(outline)


def test_the_canon_registry_rides_as_a_sidecar_key_not_a_second_block(monkeypatch):
    """P0-B #2: when `canon_registry` is also requested it shares THIS object. Two
    independent fenced blocks is what made the requests compete in `oehhe741`; a sidecar
    key cannot be dropped in favour of the other one, and must not leak into the prose."""
    prose = "1. CHARACTERS\nRatna."
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True,
        output_text=_structured(prose, _GOOD_SEMANTIC,
                                canon_registry={"events": [], "timeline": []})))
    text, source, _registry = result
    assert text == prose, "a sidecar key must not end up in the fact-sheet"
    assert source is not None, "an unknown sidecar key must not invalidate the envelope"


def test_new_caller_true_survives_a_malformed_envelope_without_losing_the_prose(monkeypatch):
    """Right shape, bad CONTENTS — the opposite of the contract-unmet case above. This one
    does NOT fail over: re-rolling the same model for the same premise is not a fix, and
    candidate selection is where an unusable envelope is meant to be caught."""
    prose = "1. CHARACTERS\nRatna."
    bad = dict(_GOOD_SEMANTIC, anchors=[{"kind": "NOT_A_REAL_KIND", "literal": "x"}])
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text=_structured(prose, bad)))
    text, source, _registry = result
    assert text == prose, "prose must survive a structured-extraction validation failure"
    assert source is None


def test_new_caller_true_survives_the_extraction_step_itself_raising(monkeypatch):
    """Even a defect INSIDE extraction (not just a bad LLM response) must not take the
    prose down with it — patch the parser to blow up and confirm the bible still
    returns."""
    prose = "1. CHARACTERS\nRatna."

    def _boom(*a, **k):
        raise RuntimeError("extraction blew up")
    monkeypatch.setattr(css, "parse_semantic_source_envelope", _boom)
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True,
        output_text=_structured(prose, _GOOD_SEMANTIC)))
    text, source, _registry = result
    assert text == prose
    assert source is None


def test_truncated_response_yields_no_bible_and_no_source(monkeypatch):
    result, _, _ = asyncio.run(_run_bible(
        monkeypatch, structured_semantic=True, output_text="cut off mid",
        finish_reason="max_tokens"))
    text, source, _registry = result
    assert text == ""
    assert source is None


def test_all_attempts_failing_returns_the_empty_tuple_shape(monkeypatch):
    async def always_fails(worker, prompt, timeout=None, task_id=None):
        return {"ok": False, "output": "", "telemetry": {}}
    monkeypatch.setattr(dyn, "run_worker", always_fails)
    result = asyncio.run(dyn.build_story_bible(
        "topik", _outline(2), is_fiction=True, language="id", structured_semantic=True))
    assert result == ("", None, None)


def test_topic_or_outline_missing_returns_the_empty_shape_immediately(monkeypatch):
    called = []
    monkeypatch.setattr(dyn, "run_worker",
                        lambda *a, **k: called.append(1) or asyncio.sleep(0))
    result = asyncio.run(dyn.build_story_bible(
        "", [], is_fiction=True, language="id", structured_semantic=True))
    assert result == ("", None, None)
    assert not called, "no LLM call at all when topic/outline are empty"


def test_nonfiction_also_gets_the_addendum_unlike_canon_registry():
    """`canon_registry`'s addendum is fiction-gated; P0-B's is not — nonfiction jobs have
    real named people/dates/events too. Prompt-level check, no LLM call needed."""
    prompt = dyn._story_bible_prompt(
        "topik", _outline(1), "id", False, structured_semantic=True)
    assert css.STRUCTURED_BIBLE_SEMANTIC_KEY in prompt


def test_the_prompt_switches_to_the_single_object_contract_when_requested():
    """P0-B #2. The old prompt appended "EXCEPTION: ALSO output the fenced X block" — once
    for `canon_registry`, once for the semantic source. Two competing exceptions to the
    same "no prose" rule read as one exception, and live job `oehhe741` dropped the
    semantic one on BOTH best-of candidates. The contract is now a single JSON object with
    named keys, which has no "which exception did it obey?" failure mode.

    OFF must stay byte-identical: not merely free of the new keys, but character-for-
    character the pre-P0-B prompt, which is what every non-assist job gets."""
    on = dyn._story_bible_prompt("t", _outline(1), "id", True, structured_semantic=True)
    off = dyn._story_bible_prompt("t", _outline(1), "id", True, structured_semantic=False)

    assert css.STRUCTURED_BIBLE_TEXT_KEY in on
    assert css.STRUCTURED_BIBLE_SEMANTIC_KEY in on
    assert "ONE JSON object" in on
    assert "```" not in on, "the object contract must not ask for a fenced block at all"

    assert css.STRUCTURED_BIBLE_SEMANTIC_KEY not in off
    assert off.startswith("TOPIC / PREMISE:") and "Output ONLY the numbered sheet" in off
    assert "ONE JSON object" not in off


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
        return text, src, None

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
        return text if not structured_semantic else (text, None, None)
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
        return "bible text", source, None

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


@pytest.mark.parametrize("desc,script", [
    ("리나가 아버지의 책상 서랍에서 비밀 편지를 발견했다", "Korean"),
    ("リナは父の机の引き出しで秘密の手紙を見つけた", "Japanese"),
    ("Рина нашла тайное письмо в ящике стола", "Russian"),
    ("وجدت رينا رسالة سرية في درج المكتب", "Arabic"),
    ("เธอพบจดหมายลับในลิ้นชัก", "Thai"),
    ("丽娜发现了抽屉里的秘密信件", "Chinese"),
    ("रीना को मेज़ की दराज़ में पत्र मिला", "Hindi"),
])
def test_a_description_with_no_latin_letters_still_gets_a_real_identity(desc, script):
    """🔴 THE WHOLE POINT OF `label`. `_event_slug` strips `[^a-z0-9]`, which deletes entire
       writing systems — so for these descriptions the id degrades to `evt_<hash12>`.

       Two earlier designs both failed here. Storing nothing but the id meant QC received a
       bare hash and could not match a passage to the row at all. Then FAIL-CLOSING on an
       empty slug meant every Korean, Japanese, Chinese, Russian, Arabic, Thai and Hindi
       job was REFUSED outright — after its Story Bible had already been paid for —
       because `_cl_wants_semantic` gates on fiction, never on language. That made
       continuity checking structurally impossible for most of the world's scripts.

       The label carries the description in its own script, so identity no longer depends
       on the id being legible. The parse succeeds and QC gets something it can read."""
    outline = _outline(2)
    src = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [],
         "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
        outline_chapters=outline, bible_text=BIBLE)
    event = src.one_time_events[0]
    assert cl._ID_RE.match(event.event_id), event.event_id
    assert event.label == desc, f"{script}: the label must survive verbatim"
    # and it must reach the projection QC actually reads, not just the dataclass
    assert event.to_canonical_obj()["label"] == desc


@pytest.mark.parametrize("desc", ["", "   ", "\t\n"])
def test_a_blank_description_is_still_rejected(desc):
    """Relaxing the unsluggable case must NOT relax the empty one: a blank description has
    no semantic content to carry into a label either, so there is nothing to identify."""
    outline = _outline(2)
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [],
             "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
            outline_chapters=outline, bible_text=BIBLE)


def test_a_truncation_colliding_pair_is_distinguishable_by_label():
    """🔴 THE COLLISION CLASS IS REMOVED AT THE SOURCE, NOT DETECTED. `_event_id` caps its
       slug at `_EVENT_SLUG_MAX`, so these two ids differ ONLY by an opaque digest:

           evt_rina_menemukan_surat_rahasia_di_laci_mej_713163bc5b59
           evt_rina_menemukan_surat_rahasia_di_laci_mej_46515164e30b

       When the id was all QC received, that pair was unresolvable. `label` carries the
       description WHOLE and untruncated (`MAX_EVENT_LABEL_LEN` is tied to the same bound
       the description is validated against), so the rows QC reads are now genuinely
       different and both events survive. Refusing them would be the wrong fix: they ARE
       two different events, and a novel is entitled to describe them similarly."""
    outline = _outline(4)
    src = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [], "one_time_events": [
            {"description": "Rina menemukan surat rahasia di laci meja kerja ayahnya",
             "occurs_chapter": 1},
            {"description": "Rina menemukan surat rahasia di laci meja kerja ibunya",
             "occurs_chapter": 4}]},
        outline_chapters=outline, bible_text=BIBLE)
    a, b = src.one_time_events
    # the ids really do collide on their legible half — the defect's precondition holds
    assert a.event_id.rsplit("_", 1)[0] == b.event_id.rsplit("_", 1)[0]
    # ...and the projection QC actually reads still tells them apart
    assert a.label != b.label
    assert a.to_canonical_obj()["label"].endswith("ayahnya")
    assert b.to_canonical_obj()["label"].endswith("ibunya")


def test_two_events_sharing_a_label_are_refused_even_in_different_chapters():
    """🔴 THE CHAPTER IS NOT A TIEBREAK. An earlier version of this check scoped uniqueness
       to `(label, occurs_chapter)` and let a cross-chapter collision through, reasoning
       that `occurs_chapter_order` distinguishes the rows. It does not: no predicate reads
       it (`occurs_chapter_order` appears zero times in `canon_lite_l2`),
       `_accepted_canon_ids_by_claim_type` hands the extractor every event id at once
       unfiltered by chapter, and the prompt names no tiebreak. Using declared placement to
       decide which event a passage evidences would assume the canon matches the text —
       the one thing a continuity check must not assume. The measured cost was a FALSE
       `one_time_event_duplication` on two genuinely different events.

       Note `_validate_simple`'s duplicate-id check CANNOT catch this: the id hash covers
       the chapter, so these two ids differ. Only the label check sees it."""
    outline = _outline(3)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable"):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": "ledakan", "occurs_chapter": 1},
                {"description": "ledakan", "occurs_chapter": 3}]},
            outline_chapters=outline, bible_text=BIBLE)


@pytest.mark.parametrize("variant,what", [
    ("Rina membakar\u00a0surat itu", "NBSP for space"),
    ("Rina membakar \u200bsurat itu", "zero-width space added"),
    ("Rina membakar\u200c surat itu", "zero-width non-joiner"),
    ("\u200eRina membakar surat itu", "bidi mark"),
    ("\ufeffRina membakar surat itu", "byte-order mark"),
    ("Rina mem\u00adbakar surat itu", "soft hyphen"),
    ("rina MEMBAKAR surat itu", "case only"),
    ("Rina  membakar  surat itu", "doubled spaces"),
    ("Rina membakar surat itu ", "trailing space"),
])
def test_labels_that_render_identically_are_refused(variant, what):
    """\U0001f534 NFC IS STORAGE, NOT A COMPARISON KEY. Comparing the stored NFC label
       byte-for-byte let every one of these through: each differs in bytes and renders
       IDENTICALLY, so the two canon rows QC receives are visually the same event carrying
       two different opaque hashes \u2014 the exact defect `label` was introduced to close,
       one encoding layer down.

       `_event_label_fold` asks "would a reader see the same text?" \u2014 NFKC, drop
       zero-width/format characters, collapse whitespace, casefold."""
    base = "Rina membakar surat itu"
    assert variant != base, f"{what}: the two spellings must really differ in bytes"
    outline = _outline(4)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable"):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": base, "occurs_chapter": 1},
                {"description": variant, "occurs_chapter": 3}]},
            outline_chapters=outline, bible_text=BIBLE)


def test_a_label_differing_only_in_unicode_composition_is_refused():
    """The NFD/NFC pair, spelled with explicit escapes so the source cannot silently
    normalize it: `caf\u00e9` composed vs `cafe\u0301` decomposed render identically."""
    composed, decomposed = "caf\u00e9 terbakar", "cafe\u0301 terbakar"
    assert composed != decomposed, "the two spellings really are different bytes"
    outline = _outline(3)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable"):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": composed, "occurs_chapter": 1},
                {"description": decomposed, "occurs_chapter": 2}]},
            outline_chapters=outline, bible_text=BIBLE)


def test_the_fold_does_not_over_merge_genuinely_different_text():
    """POSITIVE CONTROL. The fold is deliberately aggressive, so it needs a floor: a
    zero-width space REPLACING a space genuinely changes the rendering (`membakar surat`
    vs `membakarsurat`), and those are two different descriptions that must both survive.
    Without this row `_event_label_fold` could return a constant and every refusal test
    above would still pass."""
    outline = _outline(4)
    src = css.parse_semantic_source_envelope(
        {"entities": [], "anchors": [], "one_time_events": [
            {"description": "Rina membakar surat itu", "occurs_chapter": 1},
            {"description": "Rina membakar\u200bsurat itu", "occurs_chapter": 3}]},
        outline_chapters=outline, bible_text=BIBLE)
    assert len({cl.event_label_fold(e.label) for e in src.one_time_events}) == 2


@pytest.mark.parametrize("desc,what", [
    ("\u200b\u200b\u200b", "zero-width spaces only"),
    ("\u00ad\u00ad", "soft hyphens only"),
    ("\ufeff\ufeff", "byte-order marks only"),
    ("\u2060\u2060", "word joiners only"),
    ("!!!", "punctuation only"),
    ("\u2192\u2190\u2191\u2193", "arrows only"),
])
def test_a_label_with_no_letter_or_digit_in_any_script_is_refused(desc, what):
    """\U0001f534 `_req_str`'s EMPTINESS GUARD CANNOT SEE THESE. It tests `not text.strip()`,
       and `str.strip()` removes none of U+200B/200C/00AD/2060/FEFF \u2014 so a description
       of three zero-width spaces was non-empty, passed every check, and produced a row
       whose label RENDERS AS NOTHING beside a hash-only id. That is precisely the
       identity-free event `label` exists to make impossible, reached through the very
       field meant to prevent it.

       The rule is the Unicode question the original `[a-z0-9]` test was standing in for:
       at least one letter or digit, in ANY script. It admits every writing system in the
       row above and still refuses text that names nothing."""
    outline = _outline(2)
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [],
             "one_time_events": [{"description": desc, "occurs_chapter": 1}]},
            outline_chapters=outline, bible_text=BIBLE)


def test_the_exported_builder_enforces_the_same_rules_by_the_same_comparison():
    """`build_semantic_source_v1` is exported, so it is a SECOND door into a semantic
    source, and its callers hand over ready-made `CanonEventV1`s nothing upstream has
    checked. A rule enforced at one of two doors is not enforced \u2014 and enforcing a
    WEAKER rule there is worse, because it reads as covered.

    `_validate_simple` discards `_req_str`'s normalized return, so this door never
    normalized its labels; comparing stored bytes here meant a pair door 1 refuses sailed
    through door 2. Both doors now share `_reject_indistinguishable_events`, fold included.
    """
    outline = _outline(3)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable"):
        css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=BIBLE,
            one_time_events=(_event(order=1, eid="evta", label="ledakan"),
                             _event(order=2, eid="evtb", label="ledakan")))
    # ...and labels that merely RENDER identically, the case this door used to miss
    with pytest.raises(cl.CanonSchemaError):
        css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=BIBLE,
            one_time_events=(_event(order=1, eid="evta", label="ledakan besar"),
                             _event(order=2, eid="evtb", label="ledakan\u00a0besar")))
    # ...and an identity-free label
    with pytest.raises(cl.CanonSchemaError, match="no letter or digit"):
        css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=BIBLE,
            one_time_events=(_event(order=1, eid="evta", label="\u200b\u200b"),))


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
                outline_chapters=outline, bible_text=BIBLE), None
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
        return "SURGICALLY PATCHED BIBLE TEXT", src, None

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
        return "REROLLED BIBLE", src, None

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
# 5b. EVERY DOOR ONTO THE LABEL RULE, PINNED INDIVIDUALLY
# ===========================================================================
#
# 🔴 EVERY LABEL TEST ABOVE ENTERS THROUGH `parse_semantic_source_envelope`, AND THAT IS
#    PRECISELY WHY A CALL-SITE REGRESSION IS INVISIBLE HERE. `_validate_events` is reached
#    through FOUR doors, and each is backstopped by another: `build_canon_lite_v1`
#    validates and then constructs a `CanonLiteV1` whose `__post_init__` validates again;
#    `build_semantic_source_v1` stands in the same relation to
#    `_validate_semantic_source_instance`. Defense in depth is correct and stays — but it
#    means reverting ONE door to the pre-fix `_validate_simple` shape leaves a sibling
#    holding, so nothing fails and no test can name the door that broke.
#
#    MEASURED, NOT ARGUED. Reverting each of the four call sites in turn — the literal
#    round-5 defect (canon door open while the semantic-source door was shut) and the
#    literal round-6 defect (instance validator left on the old validator) — left the
#    focused suite at **974 passed, 0 failed, all four times**. The rule-body mutants
#    M10-M14 are blind to it by construction: they neuter the body, which every door
#    shares, so any one door still kills them.
#
#    A door is therefore only pinned by a test that can reach it ALONE. Each test below
#    isolates exactly one: either by calling the validator directly on an instance made
#    invalid after construction, or by no-op'ing the sibling validator so that only the
#    door under test is left able to raise. M15-M18 revert each door in turn and must die
#    here.

#: NBSP for space: different bytes, identical rendering, same fold. The pair from Rino's
#: own round-5 reproduction, kept in one place so all four door tests use one witness.
_DOOR_LABEL_A = "Rina membakar surat itu"
_DOOR_LABEL_B = "Rina membakar surat itu"


def _door_events():
    """Two individually-VALID rows that collide only as a pair.

    Distinct `event_id`s on purpose: `_validate_simple`'s duplicate-id check must not be
    what refuses these, or the test would pass with the label rule entirely absent.
    """
    return (cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                            label=_DOOR_LABEL_A),
            cl.CanonEventV1(event_id="evt_bravo02", occurs_chapter_order=2,
                            label=_DOOR_LABEL_B))


def _door_cfg(outline):
    return cl.build_job_config_snapshot(
        outline_chapters=outline, target_language="id",
        narration_style="kdrama_serial")


def test_door_1_the_final_canon_builder_enforces_labels_by_itself(monkeypatch):
    """`_finalize`'s OWN call, with the instance validator no-op'd so it cannot cover.

    This is the round-5 door: `orchestrator/static.py` calls `build_canon_lite_v1`
    directly, so with this call site gone the FINAL CANON — the artifact QC actually
    reads — carries two events whose labels differ only by a non-breaking space.
    """
    outline = _outline(3)
    cfg = _door_cfg(outline)
    monkeypatch.setattr(cl, "_validate_canon_instance", lambda canon: None)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable") as exc:
        cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg,
                               one_time_events=_door_events())
    assert getattr(exc.value, "reason_code", None) == cl.EVENT_LABEL_AMBIGUOUS


def test_door_2_the_canon_instance_validator_enforces_labels_by_itself():
    """`_validate_canon_instance` called directly — the door a caller reaches by building
    a `CanonLiteV1` by hand rather than through the builder.

    The events are swapped in AFTER construction, so the builder's own call never sees
    them. The label check sits ahead of the `canon_sha256` binding check inside the
    validator, so a label refusal — not a hash refusal — is what must come back.
    """
    outline = _outline(3)
    canon = cl.build_canon_lite_v1(
        outline_chapters=outline, job_config=_door_cfg(outline),
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01",
                                         occurs_chapter_order=1,
                                         label="Rina naik bus terakhir"),))
    object.__setattr__(canon, "one_time_events", _door_events())
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable") as exc:
        cl._validate_canon_instance(canon)
    assert getattr(exc.value, "reason_code", None) == cl.EVENT_LABEL_AMBIGUOUS


def test_door_3_the_semantic_source_instance_validator_enforces_labels_by_itself():
    """`_validate_semantic_source_instance` called directly — the round-6 door, left on
    `_validate_simple` when the builder moved to `_validate_events`."""
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE,
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01",
                                         occurs_chapter_order=1,
                                         label="Rina naik bus terakhir"),))
    object.__setattr__(src, "one_time_events", _door_events())
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable") as exc:
        css._validate_semantic_source_instance(src)
    assert getattr(exc.value, "reason_code", None) == cl.EVENT_LABEL_AMBIGUOUS


def test_door_4_the_semantic_source_builder_enforces_labels_by_itself(monkeypatch):
    """`build_semantic_source_v1`'s OWN call, with the instance validator no-op'd.

    It is exported and its callers hand over ready-made `CanonEventV1`s that nothing
    upstream has necessarily checked, so it is a real door in its own right.
    """
    monkeypatch.setattr(css, "_validate_semantic_source_instance", lambda src: None)
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable") as exc:
        css.build_semantic_source_v1(
            outline_chapters=_outline(3), bible_text=BIBLE,
            one_time_events=_door_events())
    assert getattr(exc.value, "reason_code", None) == cl.EVENT_LABEL_AMBIGUOUS


def test_the_legibility_rule_is_pinned_at_the_canon_door_too(monkeypatch):
    """The other half of the rule, at the door round 5 found open. A label of nothing but
    default-ignorable code points passes `_req_str` (`str.strip()` removes none of them)
    and names no event at all."""
    outline = _outline(3)
    cfg = _door_cfg(outline)
    monkeypatch.setattr(cl, "_validate_canon_instance", lambda canon: None)
    with pytest.raises(cl.CanonSchemaError, match="no letter or digit") as exc:
        cl.build_canon_lite_v1(
            outline_chapters=outline, job_config=cfg,
            one_time_events=(cl.CanonEventV1(
                event_id="evt_alpha01", occurs_chapter_order=1,
                label="​ᅟ️͏"),))
    assert getattr(exc.value, "reason_code", None) == cl.EVENT_LABEL_ILLEGIBLE


# ===========================================================================
# 5c. THE ENVELOPE REASON CODES — closed, and never carrying content
# ===========================================================================
#
# 🔴 SHIPPED WITH ZERO TESTS UNTIL NOW. `SEMANTIC_SOURCE_REASON_CODES`,
#    `semantic_source_reason_code` and the `dynamic.py` handler that calls it had no test
#    of any kind — not the vocabulary, not a single classifier branch, not the promise
#    that no label text reaches the log. The codes exist because this diff added a NEW way
#    for a paid job to die, so the thing they must never do is leak the content that
#    killed it.

def test_the_envelope_reason_vocabulary_is_exactly_the_four_agreed_codes():
    """A closed set, pinned by value. Adding a code is a deliberate act: this line is the
    one that makes an accidental fifth code, or a renamed one, fail loudly."""
    assert css.SEMANTIC_SOURCE_REASON_CODES == (
        "event_label_illegible", "event_label_ambiguous",
        "semantic_source_schema_invalid", "other")
    # the two label codes are the CANON's, not re-spelled here — one definition, so a
    # rename cannot leave the classifier agreeing with a stale copy of itself
    assert css.SEMANTIC_SOURCE_REASON_EVENT_LABEL_ILLEGIBLE is cl.EVENT_LABEL_ILLEGIBLE
    assert css.SEMANTIC_SOURCE_REASON_EVENT_LABEL_AMBIGUOUS is cl.EVENT_LABEL_AMBIGUOUS


@pytest.mark.parametrize("label_a,label_b,expected", [
    (_DOOR_LABEL_A, _DOOR_LABEL_B, "event_label_ambiguous"),
    ("ledakan besar", "ledakan besar", "event_label_ambiguous"),
])
def test_a_label_collision_classifies_as_ambiguous_not_as_garbage(
        label_a, label_b, expected):
    """The distinction the code exists to draw: a refusal of WELL-FORMED model output,
    raised after the Story Bible was already bought, must not log the same way as the
    model emitting malformed JSON."""
    with pytest.raises(cl.CanonSchemaError) as exc:
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": label_a, "occurs_chapter": 1},
                {"description": label_b, "occurs_chapter": 3}]},
            outline_chapters=_outline(4), bible_text=BIBLE)
    assert css.semantic_source_reason_code(exc.value) == expected


def test_malformed_output_still_classifies_as_schema_invalid():
    """The other side of the same distinction — genuinely malformed output must NOT
    borrow a label code."""
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)) as exc:
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": "ledakan", "occurs_chapter": "bukan angka"}]},
            outline_chapters=_outline(3), bible_text=BIBLE)
    assert css.semantic_source_reason_code(exc.value) == "semantic_source_schema_invalid"


def test_the_classifier_never_reads_the_exception_message():
    """§10 in one assertion. The classifier reads `.reason_code` and the exception TYPE,
    never the message — so a label echoed into a message (or an attacker-shaped one) can
    neither pick the code nor travel inside it."""
    poisoned = cl.CanonSchemaError(
        f"event_label_ambiguous {_DOOR_LABEL_A} semantic_source_schema_invalid")
    assert css.semantic_source_reason_code(poisoned) == "semantic_source_schema_invalid"
    # ...and the code that comes back carries none of it
    assert _DOOR_LABEL_A not in css.semantic_source_reason_code(poisoned)


@pytest.mark.parametrize("exc", [
    RuntimeError("boom"), ValueError("bad"), KeyboardInterrupt(), MemoryError(),
    cl.CanonSchemaError("plain"), cl.CanonBoundsError("bounds"),
])
def test_every_classifier_branch_returns_a_member_of_the_closed_set(exc):
    """Total over BaseException, including the exception types a `except Exception`
    handler would not even see — the vocabulary must stay closed no matter what future
    code raises."""
    assert css.semantic_source_reason_code(exc) in css.SEMANTIC_SOURCE_REASON_CODES


def test_an_unrecognised_reason_code_on_an_exception_is_not_passed_through():
    """A `.reason_code` is only honoured if it is already in the closed set. Otherwise the
    classifier would become an arbitrary-string channel the moment some future raiser
    attaches one."""
    rogue = cl.CanonSchemaError("x")
    rogue.reason_code = "totally_made_up_code"
    assert css.semantic_source_reason_code(rogue) == "semantic_source_schema_invalid"
    rogue2 = RuntimeError("x")
    rogue2.reason_code = _DOOR_LABEL_A
    assert css.semantic_source_reason_code(rogue2) == "other"


# ===========================================================================
# 5d. ROUND 7 — whitespace-as-control, NFC at the door, and the version literals
# ===========================================================================
#
# All three were found by Rino against a suite that was 997/997 green, and all three are
# the same family as everything before them: a rule that holds at one door, in one form,
# for one alphabet of inputs, and is simply absent one layer over.

@pytest.mark.parametrize("ws,what", [
    ("\t", "TAB — category Cc"),
    ("\n", "LINE FEED — Cc"),
    ("\r", "CARRIAGE RETURN — Cc"),
    ("\x0b", "VERTICAL TAB — Cc"),
    ("\x0c", "FORM FEED — Cc"),
    (" ", "NBSP — Zs"),
    (" ", "OGHAM SPACE MARK — Zs"),
    (" ", "LINE SEPARATOR — Zl"),
    (" ", "PARAGRAPH SEPARATOR — Zp"),
    ("　", "IDEOGRAPHIC SPACE — Zs"),
])
def test_whitespace_collapses_to_a_space_it_does_not_vanish(ws, what):
    """🔴 THE `Cc` WHITESPACE WAS BEING DELETED, NOT COLLAPSED — SO IT JOINED THE WORDS.

    The fold dropped `Cf`/`Cc` BEFORE collapsing runs of whitespace, and TAB/LF/CR/VT/FF
    are all `Cc`. `"Rina\\tpergi"` therefore folded to `"rinapergi"` while `"Rina pergi"`
    folded to `"rina pergi"`: two DIFFERENT folds for two labels a reader sees as the same
    event, which is a false MISS — the direction this fold exists to prevent, and the one
    that ships two rows QC has to guess between.

    The `Zs`/`Zl`/`Zp` cases never had the bug (`str.split()` handles them), and are here
    so a future rewrite cannot fix the controls by breaking the separators.
    """
    base = "Rina membakar surat itu"
    variant = f"Rina membakar{ws}surat itu"
    assert variant != base, f"{what}: the two spellings must really differ in bytes"
    assert cl.event_label_fold(variant) == cl.event_label_fold(base), what
    with pytest.raises(cl.CanonSchemaError, match="indistinguishable"):
        css.parse_semantic_source_envelope(
            {"entities": [], "anchors": [], "one_time_events": [
                {"description": base, "occurs_chapter": 1},
                {"description": variant, "occurs_chapter": 3}]},
            outline_chapters=_outline(4), bible_text=BIBLE)


@pytest.mark.parametrize("inv,what", [
    ("​", "ZERO WIDTH SPACE"), ("‌", "ZWNJ"), ("﻿", "BOM"),
    ("­", "SOFT HYPHEN"), ("͏", "COMBINING GRAPHEME JOINER"),
    ("️", "VARIATION SELECTOR-16"),
])
def test_invisible_characters_are_deleted_and_do_not_become_a_word_break(inv, what):
    """The OTHER direction of the same fix, and the reason it is a two-branch loop rather
    than "map everything control-ish to a space".

    An invisible character renders as NOTHING, so `"Rina<ZWSP>pergi"` reads as `"Rinapergi"`
    — one word. If the whitespace fix had turned these into spaces too, it would fold to
    `"rina pergi"` and collide with a genuinely different label. Deleting them is correct;
    collapsing them would be a false MATCH manufactured by the fix for the false MISS.
    """
    joined = f"Rina{inv}pergi"
    assert cl.event_label_fold(joined) == cl.event_label_fold("Rinapergi"), what
    assert cl.event_label_fold(joined) != cl.event_label_fold("Rina pergi"), what


def test_the_fold_stays_idempotent_across_every_whitespace_and_invisible_case():
    """Idempotence is the property the trailing re-normalize exists for, and the whitespace
    branch is new code in front of it. An equivalence class that is not idempotent is not
    one."""
    for s in ["Rina\tpergi", "Rina\npergi", "Rina pergi", "Rina pergi",
              "Rina  pergi", "Rina​pergi", "Rina️pergi", "café terbakar",
              " Rina　pergi\t", "리나가 시장에 간다", "ΣΣ ς σ"]:
        once = cl.event_label_fold(s)
        assert cl.event_label_fold(once) == once, ascii(s)


# --- NFC at the door, not only at the constructor ------------------------------

#: Spelled with explicit escapes on purpose: typing these literally lets the editor, the
#: filesystem or a shell silently normalize them, and the test would then assert nothing.
_NFC_LABEL = "café terbakar"
_NFD_LABEL = "café terbakar"


def _nfd_mutated_event():
    """A row whose label was made NFD AFTER construction — the only way to get one, since
    `__post_init__` binds NFC. Exactly what a hand-built row or a mutated artifact looks
    like."""
    row = cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1, label=_NFC_LABEL)
    object.__setattr__(row, "label", _NFD_LABEL)
    return row


def test_the_repro_precondition_actually_holds():
    """Guard the guard: if these two ever stop differing in bytes, or stop folding equal,
    every NFD test below passes for the wrong reason."""
    assert _NFD_LABEL != _NFC_LABEL
    assert cl.event_label_fold(_NFD_LABEL) == cl.event_label_fold(_NFC_LABEL)
    assert _nfd_mutated_event().label == _NFD_LABEL, "post-construction mutation must stick"


@pytest.mark.parametrize("door", ["validate_events", "final_canon", "sem_src_builder",
                                  "sem_src_instance"])
def test_a_non_nfc_label_is_refused_at_every_door(door):
    """🔴 `_req_str` NORMALIZES TO MEASURE AND THROWS THE RESULT AWAY. `_bind_norm`'s own
       docstring states the rule this broke — "a validator that normalizes and returns a
       value nobody assigns leaves the original bytes in the object and in its hash". NFC
       was therefore guaranteed only by `CanonEventV1.__post_init__`, and a row that never
       went through it kept its NFD label. That label FOLDS equal to its NFC twin, so the
       uniqueness rule passes it happily — and then it is HASHED as it lies, producing two
       different `canon_sha256`/`source_sha256` for one event. The semantic-source doors
       have no hash check behind them to notice.

       REFUSED, not repaired: normalizing inside `_validate_canon_instance` would rewrite a
       tampered artifact into passing its own `verify_sha256()`.
    """
    row = _nfd_mutated_event()
    outline = _outline(3)
    runners = {
        "validate_events": lambda: cl._validate_events((row,), count=3),
        "final_canon": lambda: cl.build_canon_lite_v1(
            outline_chapters=outline, job_config=_door_cfg(outline), one_time_events=(row,)),
        "sem_src_builder": lambda: css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=BIBLE, one_time_events=(row,)),
        "sem_src_instance": lambda: css._validate_semantic_source_instance(
            _sem_src_with_mutated_label()),
    }
    with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
        runners[door]()


def _sem_src_with_mutated_label():
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE,
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                                         label="Rina naik bus terakhir"),))
    object.__setattr__(src.one_time_events[0], "label", _NFD_LABEL)
    return src


def test_a_non_nfc_refusal_classifies_inside_the_agreed_four_codes():
    """It raises a plain `CanonSchemaError` with NO `reason_code` on purpose, so the
    classifier files it under `semantic_source_schema_invalid`. That keeps the vocabulary
    Rino agreed to at exactly four — this defect did not need a fifth code."""
    with pytest.raises(cl.CanonSchemaError) as exc:
        cl._validate_events((_nfd_mutated_event(),), count=3)
    assert getattr(exc.value, "reason_code", None) is None
    assert css.semantic_source_reason_code(exc.value) == "semantic_source_schema_invalid"
    assert css.semantic_source_reason_code(exc.value) in css.SEMANTIC_SOURCE_REASON_CODES


def test_an_nfd_label_supplied_normally_still_WORKS():
    """The control that keeps the fix from becoming a refusal of legitimate input. A caller
    handing NFD text to the CONSTRUCTOR is fine — `__post_init__` binds it to NFC and the
    event is built. Only a row that bypassed the constructor is refused."""
    canon = cl.build_canon_lite_v1(
        outline_chapters=_outline(3), job_config=_door_cfg(_outline(3)),
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                                         label=_NFD_LABEL),))
    assert canon.one_time_events[0].label == _NFC_LABEL
    assert canon.verify_sha256()


def test_one_event_spelled_two_ways_hashes_identically():
    """The actual harm the NFD bypass caused, stated as an equality. Two envelopes for the
    SAME event must not carry two different `source_sha256`."""
    def envelope(label):
        return css.build_semantic_source_v1(
            outline_chapters=_outline(3), bible_text=BIBLE,
            one_time_events=(cl.CanonEventV1(event_id="evt_alpha01",
                                             occurs_chapter_order=1, label=label),))
    assert envelope(_NFC_LABEL).source_sha256 == envelope(_NFD_LABEL).source_sha256


# --- versions, digest domains, and the projection rule -------------------------

def test_the_artifact_versions_moved_because_the_rows_changed():
    """`CanonEventV1` gained a field, so the artifacts that carry it are a different shape.
    Pinned by value: a silent revert shows up here."""
    assert cl.SCHEMA_VERSION == "canon_lite_v2"
    assert css.SEMANTIC_SOURCE_SCHEMA_VERSION == "canon_lite_semantic_source_v2"


def test_the_versions_that_deliberately_did_NOT_move_are_pinned_too():
    """The boundary of the decision, made explicit so a future round does not "tidy" these
    into v2 as well. None of these objects' own fields changed, and bumping them would
    force re-acceptance of shapes that never moved — the same reasoning that keeps
    `CLAIMS_SCHEMA_VERSION` where it is."""
    assert cl.JOB_CONFIG_SCHEMA_VERSION == "job_config_snapshot_v1"
    assert cl.PARITY_SCHEMA_VERSION == "canon_lite_parity_v1"
    assert l2.CLAIMS_SCHEMA_VERSION == "chapter_claims_v2"
    assert l2.REPORT_SCHEMA_VERSION == "continuity_report_v1"


def test_the_qc_projection_rule_names_the_version_the_artifact_actually_is():
    """🔴 THIS CONSTANT IS A CLAIM ABOUT A SHAPE, AND THE SHAPE MOVED UNDER IT. It read
       `canon_lite_v1.to_canonical_obj(include_hash=True)` while the projection had gained
       an event `label` — describing something that no longer existed. It stayed harmless
       only because the template changed in the same diff and moved `PROMPT_SHA256` anyway.

       This assertion is the mechanism replacing that accident: the rule must NAME the
       artifact's own `SCHEMA_VERSION`, so a future projection change that forgets to move
       the rule contradicts the artifact and fails here.
    """
    import canon_lite_qc_provider as qp
    assert cl.SCHEMA_VERSION in qp.QC_CANON_PROJECTION_RULE
    assert f"rev={qp.QC_CANON_PROJECTION_REVISION}" in qp.QC_CANON_PROJECTION_RULE
    assert "canon_lite_v1." not in qp.QC_CANON_PROJECTION_RULE


def test_the_canon_digest_domain_is_pinned_by_value():
    """The hash DOMAIN separates "these bytes, meaning a canon" from the same bytes meaning
    anything else. `to_canonical_obj()` changed shape, so the domain moved with it. Pinned
    behaviourally rather than by scraping the source: recomputing under the expected domain
    must reproduce the artifact's own hash."""
    outline = _outline(3)
    canon = cl.build_canon_lite_v1(
        outline_chapters=outline, job_config=_door_cfg(outline),
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                                         label="Rina naik bus terakhir"),))
    assert canon.canon_sha256 == cl._digest(
        "canon_lite.canon.v2", canon.to_canonical_obj(include_hash=False))
    assert canon.canon_sha256 != cl._digest(
        "canon_lite.canon.v1", canon.to_canonical_obj(include_hash=False))


def test_the_semantic_source_digest_domain_is_pinned_by_value():
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE,
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                                         label="Rina naik bus terakhir"),))
    hashed = {
        "schema_version": css.SEMANTIC_SOURCE_SCHEMA_VERSION,
        "accepted_outline_content_sha256": src.accepted_outline_content_sha256,
        "bible_sha256": src.bible_sha256,
        "entities": [e.to_canonical_obj() for e in src.entities],
        "anchors": [a.to_canonical_obj() for a in src.anchors],
        "one_time_events": [e.to_canonical_obj() for e in src.one_time_events],
    }
    assert src.source_sha256 == cl._digest("canon_lite.semantic_source.v2", hashed)
    assert src.source_sha256 != cl._digest("canon_lite.semantic_source.v1", hashed)


# --- the telemetry handler, driven for real ------------------------------------

def test_the_telemetry_handler_names_the_reason_and_carries_no_label(monkeypatch, caplog):
    """The one log line a paid job's refusal leaves behind, asserted END TO END through the
    real `build_story_bible` rather than through the classifier alone.

    §7 listed this as the remaining belt-and-braces gap: the classifier was pinned, but
    nothing drove the `except` branch and READ what it emitted. Two live jobs in this
    workstream (`yp8f04rr`, `98o7l3o8`) already died on this exact path.
    """
    colliding = {"entities": [], "anchors": [], "one_time_events": [
        {"description": _DOOR_LABEL_A, "occurs_chapter": 1},
        {"description": _DOOR_LABEL_B, "occurs_chapter": 2}]}
    with caplog.at_level("WARNING"):
        result, _, _ = asyncio.run(_run_bible(
            monkeypatch, structured_semantic=True,
            output_text=_structured("prosa bible yang sah", semantic=colliding)))

    # the prose survives; only the envelope is refused (the handler's whole point)
    assert result[1] is None, "a refused envelope must not become a semantic source"
    assert result[0] == "prosa bible yang sah", "a parse failure must not touch the prose"

    lines = [r.getMessage() for r in caplog.records
             if "semantic_source parse failed" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one bounded line, got {lines}"
    line = lines[0]
    assert "reason=event_label_ambiguous" in line, line
    assert "class=CanonSchemaError" in line, line
    # ...and NOTHING of the content that caused it
    assert _DOOR_LABEL_A not in line and "membakar" not in line, line
    assert "indistinguishable" not in line, "the exception body must not travel"


def test_the_telemetry_handler_cannot_raise_even_if_the_classifier_does(monkeypatch, caplog):
    """`build_story_bible`'s contract is "never raises", and this handler runs INSIDE an
    exception handler — a raise here escapes as a different exception from a function whose
    callers do not expect one. The classifier is wrapped in its own try/except → `other`
    for exactly that reason; this proves the wrapper, not the comment."""
    def exploding(exc):
        raise RuntimeError("classifier itself is broken")
    monkeypatch.setattr(css, "semantic_source_reason_code", exploding)

    colliding = {"entities": [], "anchors": [], "one_time_events": [
        {"description": _DOOR_LABEL_A, "occurs_chapter": 1},
        {"description": _DOOR_LABEL_B, "occurs_chapter": 2}]}
    with caplog.at_level("WARNING"):
        result, _, _ = asyncio.run(_run_bible(
            monkeypatch, structured_semantic=True,
            output_text=_structured("prosa bible yang sah", semantic=colliding)))

    assert result[1] is None
    assert result[0] == "prosa bible yang sah"
    lines = [r.getMessage() for r in caplog.records
             if "semantic_source parse failed" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "reason=other" in lines[0], lines[0]
    assert "other" in css.SEMANTIC_SOURCE_REASON_CODES


# ===========================================================================
# 5e. ROUND 8 — NFC on EVERY text field, and a genuinely self-binding envelope
# ===========================================================================
#
# 🔴 ROUND 7 FIXED NFC FOR `label` AND LEFT THE IDENTICAL HOLE ON EVERY OTHER TEXT FIELD.
#    `canonical_name`, every alias and anchor `literal` are each `_bind_norm`'d at
#    construction and each validated by a `_req_str` call whose normalized return value is
#    discarded. Measured by Rino: all three ACCEPTED an NFD value mutated in before the
#    canon was built, the NFD text reached `to_canonical_obj()` — the projection QC reads —
#    and `canon_sha256` was computed over those bytes, so **`verify_sha256()` returned
#    True**. A perfectly self-consistent artifact carrying text in a form the system
#    promises it never stores, with two reader-identical canons hashing differently.
#
#    The rule now lives once, in `_req_nfc`, and is applied at every validator door to
#    every `_bind_norm`'d field. Fixing `label` alone is what made round 8 necessary; these
#    tests exist so a per-field fix cannot pass for the whole class again.

#: ASCII escapes, NOT literal characters — a literal NFD string is silently
#: NFC-normalized in transit and the pair collapses into one.
_R8_NFC = "caf\u00e9 Rina"
_R8_NFD = "cafe\u0301 Rina"


def _r8_entity(name=_R8_NFC, aliases=(), source="none", mutate=None):
    e = cl.CanonEntityV1(entity_id="ent_alpha01", canonical_name=name,
                         aliases=tuple(aliases), alias_source=source)
    if mutate:
        mutate(e)
    return e


def _r8_anchor(literal=_R8_NFC, mutate=None):
    a = cl.CanonAnchorV1(anchor_id="anc_alpha01", kind="time", literal=literal)
    if mutate:
        mutate(a)
    return a


def test_the_round8_repro_precondition_holds():
    """Guard the guard. A literal NFD string typed into a source file, a shell or an editor
    is silently NFC-normalized in transit — these are built from escapes for that reason,
    and if they ever stop differing every test below passes for nothing."""
    assert _R8_NFD != _R8_NFC
    assert cl._norm_text(_R8_NFD) == _R8_NFC
    e = _r8_entity(mutate=lambda x: object.__setattr__(x, "canonical_name", _R8_NFD))
    assert e.canonical_name == _R8_NFD, "post-construction mutation must actually stick"


@pytest.mark.parametrize("what,rows", [
    ("canonical_name", lambda: {"entities": (_r8_entity(
        mutate=lambda e: object.__setattr__(e, "canonical_name", _R8_NFD)),)}),
    ("alias", lambda: {"entities": (_r8_entity(
        name="Rina", aliases=(_R8_NFC,), source="job_input",
        mutate=lambda e: object.__setattr__(e, "aliases", (_R8_NFD,))),)}),
    ("second alias only", lambda: {"entities": (_r8_entity(
        name="Rina", aliases=("Neng", _R8_NFC), source="job_input",
        mutate=lambda e: object.__setattr__(e, "aliases", ("Neng", _R8_NFD))),)}),
    ("anchor literal", lambda: {"anchors": (_r8_anchor(
        mutate=lambda a: object.__setattr__(a, "literal", _R8_NFD)),)}),
    ("event label", lambda: {"one_time_events": (cl.CanonEventV1(
        event_id="evt_alpha01", occurs_chapter_order=1, label=_R8_NFC),)}),
])
def test_nfd_is_refused_in_every_text_field_by_the_canon_builder(what, rows):
    """The door `orchestrator/static.py` actually calls. "second alias only" is deliberate:
    an implementation that checked `aliases[0]` and stopped would pass every other case."""
    kw = rows()
    if what == "event label":                      # mutate after construction
        object.__setattr__(kw["one_time_events"][0], "label", _R8_NFD)
    outline = _outline(3)
    with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
        cl.build_canon_lite_v1(outline_chapters=outline, job_config=_door_cfg(outline), **kw)


def test_nfd_is_REFUSED_by_the_instance_validator_and_not_silently_repaired():
    """🔴 REFUSE, NEVER REPAIR — the distinction Rino ratified, and the reason the rule is a
       check rather than a re-binding. A validator that normalized in place would take a
       TAMPERED artifact, quietly restore the field to the form its stored hash was computed
       over, and hand back a canon that passes its own `verify_sha256()`. The tampering
       would be undone and never reported: an integrity check turned into a repair shop.
    """
    outline = _outline(3)
    canon = cl.build_canon_lite_v1(
        outline_chapters=outline, job_config=_door_cfg(outline),
        anchors=(_r8_anchor(),))
    assert canon.verify_sha256(), "fixture precondition: the artifact starts self-consistent"
    object.__setattr__(canon.anchors[0], "literal", _R8_NFD)

    with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
        cl._validate_canon_instance(canon)
    # ...and the artifact was NOT quietly rewritten on the way out
    assert canon.anchors[0].literal == _R8_NFD, (
        "the validator repaired the field instead of refusing it — a tampered artifact "
        "would now pass verify_sha256() with no trace")


def test_nfd_in_target_language_is_refused_at_the_instance_door():
    """`target_language` is `_bind_norm`'d on the canon itself, so it is the same class of
    field as the row text — included so the rule is provably applied to ALL of them."""
    outline = _outline(3)
    canon = cl.build_canon_lite_v1(outline_chapters=outline, job_config=_door_cfg(outline))
    object.__setattr__(canon, "target_language", _R8_NFD)
    with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
        cl._validate_canon_instance(canon)


@pytest.mark.parametrize("door", ["builder", "instance"])
def test_nfd_entities_and_anchors_are_refused_by_the_semantic_source_doors(door):
    """The envelope carries the same rows and has no canon hash behind it."""
    ent = _r8_entity(mutate=lambda e: object.__setattr__(e, "canonical_name", _R8_NFD))
    if door == "builder":
        with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
            css.build_semantic_source_v1(outline_chapters=_outline(3), bible_text=BIBLE,
                                         entities=(ent,))
        return
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE, entities=(_r8_entity(),))
    object.__setattr__(src.entities[0], "canonical_name", _R8_NFD)
    with pytest.raises(cl.CanonSchemaError, match="not NFC-normalized"):
        css._validate_semantic_source_instance(src)


def test_one_name_spelled_two_ways_produces_one_hash():
    """The harm, stated as an equality. NFD handed to the CONSTRUCTOR is still legitimate
    input — `__post_init__` binds it — so the two spellings must converge, not diverge."""
    outline = _outline(3)

    def canon_for(name):
        return cl.build_canon_lite_v1(
            outline_chapters=outline, job_config=_door_cfg(outline),
            entities=(cl.CanonEntityV1(entity_id="ent_alpha01", canonical_name=name,
                                       aliases=(), alias_source="none"),))
    a, b = canon_for(_R8_NFC), canon_for(_R8_NFD)
    assert a.canon_sha256 == b.canon_sha256
    assert b.entities[0].canonical_name == _R8_NFC
    assert a.verify_sha256() and b.verify_sha256()


# --- the envelope's own hash: format AND binding --------------------------------

def _envelope_with_sha(bad):
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE, entities=(_r8_entity(),))
    object.__setattr__(src, "source_sha256", bad)
    return src


@pytest.mark.parametrize("bad,why", [
    ("z" * 64, "64 chars, not hex at all"),
    ("A" * 64, "hex digits but UPPERCASE — a different string claiming the same bytes"),
    ("abc123", "too short"),
    ("a" * 65, "too long"),
    (None, "not a string"),
    (True, "a bool, which is an int, which is not a digest"),
])
def test_a_MALFORMED_source_sha256_is_refused_AS_MALFORMED(bad, why):
    """🔴 "VALIDATED, SELF-BINDING" WAS A CLAIM THE CONSTRUCTOR NEVER CHECKED. It tested the
       TYPE and the LENGTH and nothing else — not even that the characters were hex. So
       `"z"*64` and `"0"*64` both built an envelope whose own `verify_sha256()` was False.
       `CanonLiteV1` has always ended its instance validator with `_req_sha256` + a binding
       check; the envelope that carries semantic AUTHORITY into assist had neither, leaving
       it strictly weaker than the artifact it feeds.

       ⚠️ THE MESSAGE IS THE ASSERTION, and a first version of this test missed that. The
       binding check alone refuses all of these too — a garbage digest does not bind
       anything — so asserting "it raises" cannot tell the format check from the binding
       check, and the mutation that deleted the format check SURVIVED. It matters which
       one fires: an operator reading `does not bind this content` for `"z"*64` goes
       looking for a stale envelope instead of a corrupt field.
    """
    with pytest.raises(cl.CanonSchemaError, match="expected a lowercase 64-hex sha256"):
        css._validate_semantic_source_instance(_envelope_with_sha(bad))


def test_a_WELL_FORMED_but_unbound_source_sha256_is_refused_AS_UNBOUND():
    """The other half of the pair: a real lowercase-hex digest that simply is not this
    content's. Only the binding check can see this one, and it must say so."""
    with pytest.raises(cl.CanonSchemaError, match="does not bind this content"):
        css._validate_semantic_source_instance(_envelope_with_sha("0" * 64))


def test_a_content_swap_under_a_valid_hash_is_refused():
    """The binding check earning its place: the digest is well-formed and was genuinely
    correct — for different content."""
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE,
        entities=(cl.CanonEntityV1(entity_id="ent_alpha01", canonical_name="Rina",
                                   aliases=(), alias_source="none"),))
    assert src.verify_sha256(), "fixture precondition"
    object.__setattr__(src, "entities", (cl.CanonEntityV1(
        entity_id="ent_alpha01", canonical_name="Bukan Rina", aliases=(),
        alias_source="none"),))
    with pytest.raises(cl.CanonSchemaError, match="does not bind this content"):
        css._validate_semantic_source_instance(src)


def test_a_legitimately_built_envelope_still_passes_both_checks():
    """The control. A rule that refuses everything is not a rule."""
    src = css.build_semantic_source_v1(
        outline_chapters=_outline(3), bible_text=BIBLE,
        entities=(_r8_entity(name=_R8_NFD),),          # NFD INPUT is fine — post_init binds
        anchors=(_r8_anchor(literal=_R8_NFD),),
        one_time_events=(cl.CanonEventV1(event_id="evt_alpha01", occurs_chapter_order=1,
                                         label=_R8_NFD),))
    assert src.verify_sha256()
    assert src.entities[0].canonical_name == _R8_NFC
    assert src.anchors[0].literal == _R8_NFC
    assert src.one_time_events[0].label == _R8_NFC
    css._validate_semantic_source_instance(src)          # must not raise


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
            "            event_id=_event_id(description, occurs),\n"
            "            occurs_chapter_order=occurs, label=label))",
            "            event_id=_positional_id(\"evt\", i),\n"
            "            occurs_chapter_order=occurs, label=label))",
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
    {
        "id": "M10-label-collision-accepted",
        "why": ("the label-collision check never fires, so two events a bible describes "
                "identically ship as two canon rows differing only by an opaque hash — "
                "unique to the schema, indistinguishable to the QC extractor, which is the "
                "exact defect `label` exists to close. NOTE this mutates the check's BODY, "
                "not its call site: the rule is enforced at TWO doors "
                "(parse_semantic_source_envelope and the exported build_semantic_source_v1), "
                "so neutering one call leaves the other holding and the mutant SURVIVES — "
                "observed, not hypothesised, on the first run of this entry"),
        "edits": [(
            "python/canon_lite.py",
            "        first = seen.get(fold)",
            "        first = None",
        )],
        "nodes": ["test_two_events_sharing_a_label_are_refused_even_in_different_chapters"],
    },
    {
        "id": "M11-fold-drops-only-cf-cc-not-default-ignorables",
        "why": ("the fold goes back to guessing by CATEGORY instead of using the "
                "Default_Ignorable_Code_Point property, so U+FE0F and U+034F (both `Mn`) "
                "and the Hangul fillers (`Lo`) survive it — two labels differing only by "
                "an invisible character ship as two indistinguishable canon rows, and a "
                "label made only of Hangul fillers passes the letter-or-digit rule while "
                "rendering as nothing"),
        # Re-pointed when the fold grew its whitespace branch (see M21): the old anchor
        # described a comprehension that no longer exists. The MUTATION is unchanged in
        # meaning — drop the property test, keep only the category guess.
        "edits": [(
            "python/canon_lite.py",
            "        elif _is_default_ignorable(ch) or unicodedata.category(ch) in (\"Cf\", \"Cc\"):",
            "        elif unicodedata.category(ch) in (\"Cf\", \"Cc\"):",
        )],
        "nodes": ["test_the_fold_drops_every_default_ignorable_code_point"],
        "test_file": "tests/python/test_canon_lite_l1.py",
    },
    {
        "id": "M12-label-dropped-from-the-qc-projection",
        "why": ("`label` stops travelling in `to_canonical_obj`, so the canon still HOLDS "
                "the event's identity but QC never receives it — the rows the extractor "
                "reads go back to {event_id, occurs_chapter_order} and every non-Latin "
                "event becomes an opaque hash again, silently"),
        "edits": [(
            "python/canon_lite.py",
            "        return {\"event_id\": self.event_id,\n"
            "                \"occurs_chapter_order\": self.occurs_chapter_order,\n"
            "                \"label\": self.label}",
            "        return {\"event_id\": self.event_id,\n"
            "                \"occurs_chapter_order\": self.occurs_chapter_order}",
        )],
        "nodes": ["test_a_description_with_no_latin_letters_still_gets_a_real_identity"],
    },
    {
        "id": "M13-label-compared-as-raw-bytes",
        "why": ("the collision check compares the stored NFC label instead of its fold, so "
                "two labels that RENDER identically — NBSP for space, an added zero-width "
                "character, different case — ship as two indistinguishable canon rows; the "
                "R3/R4 defect one encoding layer down"),
        "edits": [(
            "python/canon_lite.py",
            "        fold = event_label_fold(label)",
            "        fold = label",
        )],
        "nodes": ["test_labels_that_render_identically_are_refused"],
    },
    {
        "id": "M14-illegible-label-check-dropped",
        "why": ("the letter-or-digit rule stops firing, so a description of nothing but "
                "zero-width characters becomes a canon row whose label renders as EMPTY "
                "beside a hash-only id — the identity-free event `label` exists to prevent, "
                "and one `_req_str`'s `.strip()`-based emptiness guard cannot catch"),
        "edits": [(
            "python/canon_lite.py",
            "        if not any(unicodedata.category(ch)[0] in (\"L\", \"N\") for ch in fold):",
            "        if False:",
        )],
        "nodes": ["test_a_label_with_no_letter_or_digit_in_any_script_is_refused"],
    },

    # ── CALL SITES, not the rule body ────────────────────────────────────────
    # 🔴 M10-M14 ALL MUTATE THE BODY OF `_validate_events`, AND A BODY MUTANT CANNOT SEE
    #    THE DEFECT THIS WORKSTREAM ACTUALLY SHIPPED TWICE. Round 5 was an intact body
    #    that the canon door did not CALL; round 6 was the same shape at the semantic-source
    #    instance validator. Reverting each door in turn to its pre-fix `_validate_simple`
    #    shape left the focused suite at 974 passed / 0 failed every time — four live
    #    regressions, fully green. These six restore the missing half of the coverage: the
    #    rule is only enforced where somebody calls it.
    {
        "id": "M15-final-canon-door-reverted",
        "why": ("the EXACT round-5 defect: `_finalize` goes back to `_validate_simple`, so "
                "the FINAL CANON — the artifact QC reads, built by the path "
                "`orchestrator/static.py` calls directly — stops enforcing labels while "
                "the semantic-source door still holds and hides it"),
        "edits": [(
            "python/canon_lite.py",
            '    canon_kwargs["one_time_events"] = _validate_events(\n'
            '        canon_kwargs["one_time_events"], count=count)',
            '    canon_kwargs["one_time_events"] = _validate_simple(\n'
            '        canon_kwargs["one_time_events"], kind="one_time_events",\n'
            '        max_n=MAX_EVENTS, id_attr="event_id",\n'
            '        order_attrs=("occurs_chapter_order",), count=count,\n'
            '        row_type=CanonEventV1, text_attrs={"label": MAX_EVENT_LABEL_LEN})',
        )],
        "nodes": ["test_door_1_the_final_canon_builder_enforces_labels_by_itself",
                  "test_the_legibility_rule_is_pinned_at_the_canon_door_too"],
    },
    {
        "id": "M16-canon-instance-door-reverted",
        "why": ("a directly-constructed `CanonLiteV1` stops enforcing labels — the door "
                "`__post_init__` exists to be, and the one a caller reaches without the "
                "builder"),
        "edits": [(
            "python/canon_lite.py",
            "    _validate_events(canon.one_time_events, count=count)",
            '    _validate_simple(\n'
            '        canon.one_time_events, kind="one_time_events", max_n=MAX_EVENTS,\n'
            '        id_attr="event_id", order_attrs=("occurs_chapter_order",),\n'
            '        count=count, row_type=CanonEventV1,\n'
            '        text_attrs={"label": MAX_EVENT_LABEL_LEN})',
        )],
        "nodes": ["test_door_2_the_canon_instance_validator_enforces_labels_by_itself"],
    },
    {
        "id": "M17-semantic-source-instance-door-reverted",
        "why": ("the EXACT round-6 defect: the instance validator left on the old "
                "validator when the builder moved, so constructing "
                "`CanonLiteSemanticSourceV1(...)` directly mints a source with duplicate "
                "labels"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "    canon_lite._validate_events(src.one_time_events, count=0, order_attrs=())",
            '    canon_lite._validate_simple(\n'
            '        src.one_time_events, kind="one_time_events",\n'
            '        max_n=canon_lite.MAX_EVENTS, id_attr="event_id", order_attrs=(),\n'
            '        count=0, row_type=canon_lite.CanonEventV1,\n'
            '        text_attrs={"label": canon_lite.MAX_EVENT_LABEL_LEN})',
        )],
        "nodes": ["test_door_3_the_semantic_source_instance_validator_enforces_labels_by_itself"],
    },
    {
        "id": "M18-semantic-source-builder-door-reverted",
        "why": ("the exported builder stops enforcing labels; its callers hand over "
                "ready-made CanonEventV1s that nothing upstream has checked"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "    validated_events = canon_lite._validate_events(\n"
            "        tuple(one_time_events), count=0, order_attrs=())",
            '    validated_events = canon_lite._validate_simple(\n'
            '        tuple(one_time_events), kind="one_time_events",\n'
            '        max_n=canon_lite.MAX_EVENTS, id_attr="event_id", order_attrs=(),\n'
            '        count=0, row_type=canon_lite.CanonEventV1,\n'
            '        text_attrs={"label": canon_lite.MAX_EVENT_LABEL_LEN})',
        )],
        "nodes": ["test_door_4_the_semantic_source_builder_enforces_labels_by_itself"],
    },

    # ── the envelope reason codes ────────────────────────────────────────────
    {
        "id": "M19-reason-code-collapses-to-one-bucket",
        "why": ("the specific `.reason_code` is ignored, so a label collision — a refusal "
                "of well-formed output raised AFTER the Story Bible was paid for — logs "
                "identically to the model emitting garbage, which is the one diagnosis "
                "that sends an operator looking in the wrong place"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            '    code = getattr(exc, "reason_code", None)\n'
            "    if code in SEMANTIC_SOURCE_REASON_CODES:\n"
            "        return code",
            '    code = getattr(exc, "reason_code", None)\n'
            "    if False:\n"
            "        return code",
        )],
        "nodes": ["test_a_label_collision_classifies_as_ambiguous_not_as_garbage"],
    },
    {
        "id": "M20-reason-code-becomes-an-open-string-channel",
        "why": ("the closed-set membership test is dropped, so ANY string a future raiser "
                "attaches to an exception travels into the log line — the vocabulary stops "
                "being closed and §10's bound on what leaves for a log is gone"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "    if code in SEMANTIC_SOURCE_REASON_CODES:\n        return code",
            "    if code is not None:\n        return code",
        )],
        "nodes": ["test_an_unrecognised_reason_code_on_an_exception_is_not_passed_through"],
    },

    # ── ROUND 7: whitespace-as-control, NFC at the door, version literals ────
    {
        "id": "M21-whitespace-control-deleted-instead-of-collapsed",
        "why": ("the fold goes back to dropping Cf/Cc BEFORE collapsing, so TAB/LF/CR/VT/FF "
                "— all category Cc — are DELETED and the words either side are joined: "
                "'Rina\\tpergi' folds to 'rinapergi' while 'Rina pergi' folds to "
                "'rina pergi', and one event ships as two rows QC must guess between"),
        "edits": [(
            "python/canon_lite.py",
            "        if ch.isspace():\n            kept.append(\" \")",
            "        if False:\n            kept.append(\" \")",
        )],
        "nodes": ["test_whitespace_collapses_to_a_space_it_does_not_vanish"],
    },
    {
        "id": "M22-invisibles-become-spaces-instead-of-vanishing",
        "why": ("the OVER-correction for M21: invisible characters treated as separators "
                "rather than deleted, so 'Rina<ZWSP>pergi' — which a reader sees as one "
                "word — folds to 'rina pergi' and collides with a genuinely different "
                "label. A false MATCH manufactured by the fix for the false MISS"),
        "edits": [(
            "python/canon_lite.py",
            "        elif _is_default_ignorable(ch) or unicodedata.category(ch) in (\"Cf\", \"Cc\"):\n"
            "            continue",
            "        elif _is_default_ignorable(ch) or unicodedata.category(ch) in (\"Cf\", \"Cc\"):\n"
            "            kept.append(\" \")",
        )],
        "nodes": ["test_invisible_characters_are_deleted_and_do_not_become_a_word_break"],
    },
    {
        "id": "M23-nfc-rule-body-neutered",
        "why": ("`_req_nfc` stops refusing, so EVERY text field goes back to keeping "
                "whatever bytes it arrived with: the NFD value reaches to_canonical_obj() "
                "and canon_sha256 is computed over it, so the artifact still verifies "
                "against itself while two reader-identical canons hash differently"),
        "edits": [(
            "python/canon_lite.py",
            "    if type(value) is str and value != UNKNOWN and value != _norm_text(value):",
            "    if False:",
        )],
        "nodes": ["test_a_non_nfc_label_is_refused_at_every_door",
                  "test_nfd_is_refused_in_every_text_field_by_the_canon_builder",
                  "test_one_event_spelled_two_ways_hashes_identically"],
    },
    {
        "id": "M24-nfc-repaired-instead-of-refused",
        "why": ("the door normalizes in place rather than refusing — which makes "
                "`_validate_canon_instance` silently rewrite a TAMPERED artifact into "
                "passing its own verify_sha256(), turning an integrity check into a repair "
                "shop and leaving the tampering unreported"),
        "edits": [(
            "python/canon_lite.py",
            "        for attr, max_len in dict(text_attrs).items():\n"
            "            _req_str(getattr(row, attr), f\"{kind}[{i}].{attr}\", max_len=max_len)\n"
            "            _req_nfc(getattr(row, attr), f\"{kind}[{i}].{attr}\")",
            "        for attr, max_len in dict(text_attrs).items():\n"
            "            object.__setattr__(row, attr, _req_str(\n"
            "                getattr(row, attr), f\"{kind}[{i}].{attr}\", max_len=max_len))",
        )],
        "nodes": ["test_nfd_is_REFUSED_by_the_instance_validator_and_not_silently_repaired"],
    },

    # ── ROUND 8: the rule is only enforced where somebody CALLS it ───────────
    # M23 neuters the shared body; these five remove it from ONE door each. Round 5 is the
    # reason both kinds exist — a body mutant is killed by any surviving call site, so it
    # cannot see the defect this workstream keeps shipping.
    {
        "id": "M30-nfc-dropped-for-canonical_name",
        "why": "entities[i].canonical_name goes back to keeping NFD bytes into the hash",
        "edits": [(
            "python/canon_lite.py",
            "        _req_nfc(e.canonical_name, f\"entities[{i}].canonical_name\")\n",
            "",
        )],
        "nodes": ["test_nfd_is_refused_in_every_text_field_by_the_canon_builder",
                  "test_one_name_spelled_two_ways_produces_one_hash"],
    },
    {
        "id": "M31-nfc-dropped-for-aliases",
        "why": "every alias goes back to keeping NFD bytes into the hash",
        "edits": [(
            "python/canon_lite.py",
            "            _req_nfc(alias, f\"entities[{i}].aliases[{j}]\")\n",
            "",
        )],
        "nodes": ["test_nfd_is_refused_in_every_text_field_by_the_canon_builder"],
    },
    {
        "id": "M32-nfc-checked-on-the-first-alias-only",
        "why": ("the subtler shape: the rule is applied to aliases[0] and stops, so an "
                "entity whose SECOND alias is NFD passes — an implementation that looks "
                "correct in every single-alias test"),
        "edits": [(
            "python/canon_lite.py",
            "            _req_nfc(alias, f\"entities[{i}].aliases[{j}]\")",
            "            if j == 0:\n"
            "                _req_nfc(alias, f\"entities[{i}].aliases[{j}]\")",
        )],
        "nodes": ["test_nfd_is_refused_in_every_text_field_by_the_canon_builder"],
    },
    {
        "id": "M33-nfc-dropped-for-anchor-literal-and-event-label",
        "why": ("the `text_attrs` call site — anchors' literal and events' label together, "
                "since both reach the rule through _validate_simple"),
        "edits": [(
            "python/canon_lite.py",
            "            _req_nfc(getattr(row, attr), f\"{kind}[{i}].{attr}\")\n",
            "",
        )],
        "nodes": ["test_nfd_is_refused_in_every_text_field_by_the_canon_builder",
                  "test_a_non_nfc_label_is_refused_at_every_door"],
    },
    {
        "id": "M34-nfc-dropped-for-target_language-at-the-instance-door",
        "why": "the canon's own bound text field stops being checked when re-validated",
        "edits": [(
            "python/canon_lite.py",
            "    _req_nfc(canon.target_language, \"target_language\")\n",
            "",
        )],
        "nodes": ["test_nfd_in_target_language_is_refused_at_the_instance_door"],
    },

    # ── ROUND 8: the envelope's "self-binding" claim ─────────────────────────
    {
        "id": "M35-source-sha256-back-to-type-and-length",
        "why": ("the digest is checked for TYPE and LENGTH only — not even that the "
                "characters are hex — so 'z'*64 and 'A'*64 build an envelope whose own "
                "verify_sha256() is False, from a constructor that advertises itself as "
                "validated and self-binding"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "    canon_lite._req_sha256(src.source_sha256, \"source_sha256\")",
            "    if not isinstance(src.source_sha256, str) or len(src.source_sha256) != 64:\n"
            "        raise canon_lite.CanonSchemaError(\n"
            "            \"source_sha256: expected a 64-char hex sha256\")",
        )],
        "nodes": ["test_a_MALFORMED_source_sha256_is_refused_AS_MALFORMED"],
    },
    {
        "id": "M36-envelope-binding-check-removed",
        "why": ("the hash is well-formed but never checked to BIND the content, so an "
                "envelope whose entities were swapped after construction still validates — "
                "the exact 'self-binding' claim that was false before round 8"),
        "edits": [(
            "python/canon_lite_semantic_source.py",
            "    if not src.verify_sha256():\n"
            "        raise canon_lite.CanonSchemaError(\n"
            "            \"source_sha256: does not bind this content\")",
            "    if False:\n"
            "        raise canon_lite.CanonSchemaError(\n"
            "            \"source_sha256: does not bind this content\")",
        )],
        "nodes": ["test_a_content_swap_under_a_valid_hash_is_refused",
                  "test_a_WELL_FORMED_but_unbound_source_sha256_is_refused_AS_UNBOUND"],
    },
    {
        "id": "M25-projection-rule-reverted-to-the-v1-literal",
        "why": ("the constant goes back to describing a projection shape that no longer "
                "exists — `canon_lite_v1.to_canonical_obj(...)` after the projection gained "
                "an event label. It is the QC contract's only statement about that shape, "
                "and it would again be safe purely by accident"),
        "edits": [(
            "python/canon_lite_qc_provider.py",
            "QC_CANON_PROJECTION_RULE = (\n"
            "    f\"canon_lite_v2.to_canonical_obj(include_hash=True) rev={QC_CANON_PROJECTION_REVISION}\")",
            "QC_CANON_PROJECTION_RULE = \"canon_lite_v1.to_canonical_obj(include_hash=True)\"",
        )],
        "nodes": ["test_the_qc_projection_rule_names_the_version_the_artifact_actually_is"],
    },
    {
        "id": "M26-canon-digest-domain-reverted",
        "why": ("the hash DOMAIN goes back to v1 while the canonical object it covers has "
                "gained a field — both sites together, so the artifact still verifies "
                "against itself and ONLY a test pinning the domain by value can see it"),
        "edits": [
            ("python/canon_lite.py",
             "        return _digest(\"canon_lite.canon.v2\", self.to_canonical_obj(include_hash=False))",
             "        return _digest(\"canon_lite.canon.v1\", self.to_canonical_obj(include_hash=False))"),
            ("python/canon_lite.py",
             "        canon_sha256=_digest(\"canon_lite.canon.v2\", hash_input), **canon_kwargs)",
             "        canon_sha256=_digest(\"canon_lite.canon.v1\", hash_input), **canon_kwargs)"),
        ],
        "nodes": ["test_the_canon_digest_domain_is_pinned_by_value"],
    },
    {
        "id": "M27-schema-version-reverted",
        "why": ("`SCHEMA_VERSION` goes back to canon_lite_v1 while CanonEventV1 carries a "
                "field it did not have — two genuinely different artifact shapes claiming "
                "one version, and the projection rule now contradicts the artifact"),
        "edits": [(
            "python/canon_lite.py",
            "SCHEMA_VERSION = \"canon_lite_v2\"",
            "SCHEMA_VERSION = \"canon_lite_v1\"",
        )],
        "nodes": ["test_the_artifact_versions_moved_because_the_rows_changed",
                  "test_the_qc_projection_rule_names_the_version_the_artifact_actually_is"],
    },

    # ── ROUND 7: the telemetry handler, which two live jobs already died on ──
    {
        "id": "M28-telemetry-logs-the-raw-exception-body",
        "why": ("the bounded code is replaced by the exception's own message, so "
                "model-produced text and the event LABEL that caused the refusal travel "
                "into the log line — the §10 bound on what leaves for a log, gone"),
        "edits": [(
            "python/orchestrator/dynamic.py",
            "                        type(_sse).__name__, _reason)",
            "                        type(_sse).__name__, str(_sse))",
        )],
        "nodes": ["test_the_telemetry_handler_names_the_reason_and_carries_no_label"],
    },
    {
        "id": "M29-telemetry-classifier-left-unguarded",
        "why": ("the try/except around the classifier is removed, so a raise inside it "
                "escapes from a function whose contract is 'never raises' — and it escapes "
                "DURING exception handling, which is how jobs yp8f04rr and 98o7l3o8 died"),
        "edits": [(
            "python/orchestrator/dynamic.py",
            "                    try:\n"
            "                        _reason = semantic_source_reason_code(_sse)\n"
            "                    except Exception:      # noqa: BLE001 — telemetry may never break a job\n"
            "                        _reason = \"other\"",
            "                    _reason = semantic_source_reason_code(_sse)",
        )],
        "nodes": ["test_the_telemetry_handler_cannot_raise_even_if_the_classifier_does"],
    },
]


def _build_mutant_tree(root, edits, test_files=()):
    shutil.copytree(REPO / "python", root / "python", ignore=_IGNORE)
    (root / "tests" / "python").mkdir(parents=True)
    # `test_files` lets a mutant be witnessed by a test in ANOTHER suite. The label rules
    # moved onto the canon itself, so the test that proves the fold covers every
    # default-ignorable code point lives with `canon_lite`'s own suite — copying only this
    # file would silently collect zero nodes and report the mutant as killed.
    names = {"conftest.py", Path(__file__).name} | {Path(f).name for f in test_files}
    for name in sorted(names):
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
    test_file = mutant.get("test_file", f"tests/python/{Path(__file__).name}")
    _build_mutant_tree(root, mutant["edits"], test_files=(test_file,))

    nodes = [f"{test_file}::{n}" for n in mutant["nodes"]]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=root, capture_output=True, text=True, timeout=600)
    # 🔴 A MUTANT WHOSE WITNESS VANISHED IS THE ONE FAILURE THIS HARNESS CANNOT AFFORD, and
    #    the first version of this guard could not detect it: pytest writes `ERROR: not
    #    found: <node>` to STDERR, not stdout, and the check was `"ERROR" not in
    #    <list of lines>` — a membership test needing a line exactly equal to "ERROR".
    #    It was inert in both directions. Read stderr, match as a SUBSTRING, and cover
    #    both exit codes: 4 (node not found) and 5 (nothing collected).
    if proc.returncode in (4, 5) or "not found:" in proc.stderr or "no tests ran" in proc.stdout:
        raise AssertionError(
            f"{mutant['id']}: its witness collected no tests (rc={proc.returncode}) — the "
            f"node name or test_file is stale, so this mutant proves nothing.\n"
            f"stdout: {proc.stdout[-1500:]}\nstderr: {proc.stderr[-1500:]}")
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
