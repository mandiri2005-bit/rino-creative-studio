"""P0-B under the ACTUAL production flag combination, not one feature at a time.

Live job `oehhe741` failed on an interaction, not on a unit: `NARASI_CANON_REGISTRY=1`
asked for a second fenced block that competed with the semantic one, `NARASI_BIBLE_BEST_OF=2`
let a judge pick between two candidates that both lacked an envelope, and
`NARASI_LEDGER_ENFORCE_SURGICAL=1` would have rewritten the pinned bible out from under any
envelope that had survived. Every one of those flags is ON in production and every P0-B test
that existed before this file ran with them OFF.

So the fixture here is the production combination, and every row runs under it.

Covered:
  * the judge may never be handed a candidate that cannot arm (#4);
  * zero usable candidates refuses BEFORE the MAP, it does not degrade (#4, #6);
  * the REAL surgical branch is contained BEFORE its provider call, and the pinned bible
    and its envelope stay the pair they started as (#5);
  * the `canon_registry` sidecar survives best-of and reaches the result dict (#2).

`build_story_bible` and `_write_chapter` are the only stubbed boundaries — routing, canon
construction, the ledger branch, freeze and arming all run for real. No network.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

import canon_lite as cl  # noqa: E402
import canon_lite_semantic_source as css  # noqa: E402
import laozhang_api as lz  # noqa: E402
import narasi_counters as nc  # noqa: E402
from orchestrator import dynamic as dyn  # noqa: E402
from orchestrator import static as st  # noqa: E402
from orchestrator.context_builder import SharedContext  # noqa: E402

CANARY = "52a5bb48-1ea9-54ac-b8a5-b25ff885205a"

#: First words of the surgical line-patch system prompt (`static.py`). The judge uses the
#: SAME cheap-call helper, so "no provider call at all" would be the wrong assertion.
_SURGICAL_MARKER = "You are patching single lines of a story fact-sheet"
_JUDGE_MARKER = "You are judging candidate STORY BIBLES"

#: A banned lane term that the topic does NOT supply, so the premise exemption cannot skip
#: enforcement. It must appear IN the candidate prose: the surgical branch collects the
#: offending LINES first and never calls a provider when it finds none — a bible that
#: merely gets reported as hit-carrying, without the term in its text, walks past the
#: provider call on its own and would make the containment assertion vacuous.
BANNED_TERM = "Deok-su"


class Recorder:
    """Stands in for `_write_chapter` and counts MAP entries — `calls == []` is how a
    refusal proves it happened BEFORE any chapter work, not after.

    Reports the REAL census fields (identical to the sibling P0-B suite's recorder): with
    assist armed, `narrate_chapters` verifies after the MAP that every chapter saw the
    canon it was given, and a thinner stub would fail that census for reasons that have
    nothing to do with what this file tests."""

    def __init__(self):
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append(dict(kw))
        report = {}
        if kw.get("canon_text"):
            report = {
                "canon_generation_prompt_sha256_seen":
                    cl.sha256_hex(kw["canon_text"].encode("utf-8")),
                "context_sha256_seen": cl.context_digest(kw["ctx"]),
                "canon_sha256": kw.get("canon_sha256"),
                "context_sha256": kw.get("context_sha256"),
            }
        return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m",
                **report}


@pytest.fixture(autouse=True)
def _production_flags(monkeypatch):
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", CANARY)
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.setenv("NARASI_CANON_REGISTRY", "1")
    monkeypatch.setenv("NARASI_BIBLE_BEST_OF", "2")
    monkeypatch.setenv("NARASI_LEDGER_VALIDATOR", "1")
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE", "1")
    monkeypatch.setenv("NARASI_LEDGER_ENFORCE_SURGICAL", "1")
    # best-of and the semantic path are both fiction-gated; pin it rather than depend on
    # which style string the pakem registry happens to resolve as fiction.
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: True)
    original = st._write_chapter
    yield
    st._write_chapter = original
    os.environ.pop("NARASI_CANON_LITE_MODE", None)


def _outline(n=2):
    return [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}",
             "word_target": 400} for i in range(n)]


def _entity(name="Ratna", eid="ent1"):
    return cl.CanonEntityV1(entity_id=eid, canonical_name=name, aliases=(),
                            alias_source="none")


def _spy_cheap_calls(monkeypatch, *, judge_winner=2) -> list:
    seen: list = []

    async def _fake(system, user, *, tenant_id=None, user_id=None, job_uuid=None,
                    json_mode=False, **kw):
        seen.append(str(system))
        return json.dumps({"winner": judge_winner, "reason": "spy"}), None

    monkeypatch.setattr(lz, "_narasi_cheap_call", _fake)
    return seen


def _run(monkeypatch, bible_double, *, n=2):
    monkeypatch.setattr(dyn, "build_story_bible", bible_double)
    rec = Recorder()
    st._write_chapter = rec
    os.environ["NARASI_CANON_LITE_MODE"] = "assist"
    chapters = _outline(n)
    res = asyncio.run(st.narrate_chapters(
        "topik uji", chapters, polish="none", max_parallel=n, tenant_id=CANARY,
        shared_context=SharedContext(topic="topik uji", chapters=chapters),
        assist_activation_ready=True))
    return res, rec


#: The three ways an envelope can be present, well-formed, non-empty — and still unusable.
#: One per check the filter is required to make, so each has its own witness instead of
#: all three resting on a single "bound to the wrong bible" case.
INVALID_KINDS = ("bible", "outline", "sha")


def _broken(kind, *, outline, text, entity_name):
    """Build an envelope that fails EXACTLY ONE of the filter's checks."""
    if kind == "bible":
        # binds_bible() fails: hashed against prose this candidate does not ship.
        return css.build_semantic_source_v1(
            outline_chapters=outline, bible_text="A COMPLETELY DIFFERENT BIBLE",
            entities=(_entity(name=entity_name),))
    if kind == "outline":
        # binds_outline() fails: bound to an outline the job is not writing.
        other = [dict(c, summary="OUTLINE THE JOB IS NOT WRITING") for c in outline]
        src = css.build_semantic_source_v1(
            outline_chapters=other, bible_text=text,
            entities=(_entity(name=entity_name),))
        # Re-point the bible hash at THIS prose, then re-seal the object, so `binds_bible`
        # AND `verify_sha256` both still pass and the outline binding is the only thing
        # wrong. Without the re-seal this case would also trip the hash check and could
        # not witness `binds_outline` on its own.
        object.__setattr__(src, "bible_sha256", cl.advisory_bible_digest(text))
        object.__setattr__(src, "source_sha256", src.compute_sha256())
        return src
    if kind == "sha":
        # verify_sha256() fails: contents no longer match the self-hash on the object.
        src = css.build_semantic_source_v1(
            outline_chapters=outline, bible_text=text,
            entities=(_entity(name=entity_name),))
        object.__setattr__(src, "source_sha256", "0" * 64)
        return src
    raise AssertionError(f"unknown invalid kind {kind!r}")


def _candidates(valid_flags, *, registry=None, invalid_kind="bible"):
    """A `build_story_bible` double yielding one candidate per call.

    `valid_flags[i]` False ⟹ that candidate's envelope is broken in exactly the way named
    by `invalid_kind` — never a `None` stand-in, so the filter is forced to actually run
    the binding and hash checks rather than a truthiness test that would pass them all.
    """
    calls = {"n": 0}

    async def _fake(topic, outline, *, is_fiction=True, style=None, language="id",
                    manager_model=None, timeout=None, telemetry_sink=None,
                    extra_negative=None, structured_semantic=False):
        calls["n"] += 1
        i = calls["n"]
        text = f"CANDIDATE {i} PROSE"
        if not structured_semantic:
            return text
        text = f"CANDIDATE {i} PROSE\n3. TOKOH\nSeorang tetangga bernama {BANNED_TERM}.\n"
        name = f"Entity-From-Candidate-{i}"
        if valid_flags[(i - 1) % len(valid_flags)]:
            src = css.build_semantic_source_v1(
                outline_chapters=outline, bible_text=text,
                entities=(_entity(name=name),))
        else:
            src = _broken(invalid_kind, outline=outline, text=text, entity_name=name)
        return text, src, registry

    return _fake


# ===========================================================================
# #4 — the judge may only choose among candidates that can actually arm
# ===========================================================================
@pytest.mark.parametrize("invalid_kind", INVALID_KINDS)
def test_the_judge_is_never_offered_a_candidate_that_cannot_arm(monkeypatch, invalid_kind):
    """Candidate 2 is unusable; the judge is rigged to prefer it. It must never get the
    chance — with one eligible candidate left there is nothing to judge, so the judge is
    not called at all and candidate 1's own envelope is what arms.

    This is the `oehhe741` shape exactly: a judge happily picking a candidate that the
    arming gate was always going to refuse, after the call had been paid for.

    Parametrised over the THREE ways to be unusable so each of the filter's checks —
    `binds_bible`, `binds_outline`, `verify_sha256` — has its own witness. A single case
    would let two of the three be deleted without any test noticing."""
    seen = _spy_cheap_calls(monkeypatch, judge_winner=2)
    res, rec = _run(monkeypatch, _candidates([True, False], invalid_kind=invalid_kind))

    assert res.get("ok") is True, res
    canon = res.get("_canon_lite_canon")
    assert canon is not None
    assert [e.canonical_name for e in canon.entities] == ["Entity-From-Candidate-1"], (
        "the unusable candidate won, or its envelope leaked into the winner")
    assert [s for s in seen if _JUDGE_MARKER in s] == [], (
        "the judge was called with a single eligible candidate — it was offered the "
        "unusable one too")
    assert rec.calls, "the job should have run: one candidate was perfectly usable"


def test_zero_usable_candidates_refuses_before_the_map(monkeypatch):
    """Both unusable ⟹ refuse, never degrade to an empty canon (#6). `rec.calls == []`
    is the load-bearing half: refusing after the MAP would already have spent the money
    this gate exists to protect."""
    _spy_cheap_calls(monkeypatch)
    res, rec = _run(monkeypatch, _candidates([False, False]))

    assert res.get("ok") is False, res
    assert res.get("error") == "canon_lite_assist_semantic_source_unavailable"
    assert res.get("chapters") == []
    assert rec.calls == [], "refusal must happen BEFORE the MAP"


# ===========================================================================
# #5 — the REAL surgical branch, contained ahead of its provider call
# ===========================================================================
def _force_one_ledger_hit(monkeypatch):
    """Report a bible-level hit on a term the topic does NOT supply, so enforcement is not
    skipped by the premise exemption. Patching the SCANNER keeps the branch under test
    real — the gating, term filtering, containment and pinning all still execute."""
    def _fake_scan(text, *, bible="", style_key=None, **kw):
        if bible:
            return {"bible_hits": 1,
                    "hits": [{"term": f"name:{BANNED_TERM}", "where": "bible"}]}
        return {"bible_hits": 0, "hits": []}

    monkeypatch.setattr(nc, "ledger_hits_scan", _fake_scan)


def test_the_real_surgical_branch_is_contained_before_it_spends(monkeypatch):
    """Assist holds a bound envelope, so a patch could only invalidate it. The containment
    must therefore happen AHEAD of the provider call — an implementation that checked at
    the apply site would still bill for a patch it was always going to discard."""
    _force_one_ledger_hit(monkeypatch)
    seen = _spy_cheap_calls(monkeypatch, judge_winner=1)
    res, rec = _run(monkeypatch, _candidates([True, True]))

    assert res.get("ok") is True, res
    assert [s for s in seen if _SURGICAL_MARKER in s] == [], (
        "a surgical patch was requested from a provider even though its output could "
        "only ever have been discarded")
    assert rec.calls, "containment must not cost the job"


def test_containment_leaves_the_bible_and_its_envelope_a_matching_pair(monkeypatch):
    """The point of containing the patch: what arms is still the prose the envelope was
    hashed against. A mutated bible with a stale envelope is the `..._bible_changed`
    refusal, and getting here with `ok=True` is what proves the pair survived."""
    _force_one_ledger_hit(monkeypatch)
    _spy_cheap_calls(monkeypatch, judge_winner=1)
    res, _ = _run(monkeypatch, _candidates([True, True]))

    assert res.get("ok") is True, res
    assert res.get("error") != "canon_lite_assist_semantic_source_bible_changed"
    facts = res.get("canonical_facts") or ""
    assert facts.startswith("CANDIDATE "), (
        "the pinned bible is no longer the candidate prose the envelope binds to")
    canon = res.get("_canon_lite_canon")
    assert canon is not None and canon.entities, "assist armed with no semantic authority"


# ===========================================================================
# #2 — the registry sidecar survives best-of and reaches the consumer
# ===========================================================================
def test_the_registry_sidecar_reaches_the_result_dict_through_best_of(monkeypatch):
    _spy_cheap_calls(monkeypatch, judge_winner=1)
    registry = {"events": [{"id": "e1", "summary": "s"}]}
    res, _ = _run(monkeypatch, _candidates([True, True], registry=registry))

    assert res.get("ok") is True, res
    assert res.get("canon_registry") == registry, (
        "the sidecar was dropped between the bible call and the result dict — the "
        "consumer would fall through to its paid re-extraction")
