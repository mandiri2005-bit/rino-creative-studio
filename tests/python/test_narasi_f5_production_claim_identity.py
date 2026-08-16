"""F5 — the semantic claim identity, on the shape production ACTUALLY emits.

🔴 WHY THIS FILE EXISTS SEPARATELY FROM `test_narasi_f5_authority_dedup.py`. That suite
   proves the dedup RULES, and it proves them on dicts it writes itself — every one of
   which carries `beat="final_choice"`. No detector in this codebase publishes `beat`,
   or `thread`, or `event`, or any other key `_V3G_CLAIM_KEYS` reads. The critic returns
   type/severity/evidence/fix/chapter; the register gate and canon-diff return no
   locator at all. So the semantic path was green in its own tests and DEAD in
   production: `_v3g_claim_identity` returned None for every violation a real run
   produced, and each sighting kept burning its own repair-budget slot.

   Everything here therefore runs the REAL `_apply_v3_gates` with the real detectors and
   the provider seam replaced — never a hand-built violation dict. `make_narasi_client`
   raises, so any attempt at a network call fails the test rather than escaping.

🔴 THE SIDECAR IS ONLY OBSERVABLE ON THE WAY IN. `_v3g_dedup_violations` strips every
   `_f5*` key from what it returns, so a test that inspects the revise payload can never
   see it — and a test that concluded "no sidecar" from that would be reading the strip,
   not the wiring. The spy below records the merge point's INPUT.
"""
import asyncio
import json
import sys

import pytest

import laozhang_api as lz
import narration_api as na


def _na():
    """The `narration_api` currently registered for imports.

    Same reload hazard as the fixture's `live_lz`: asserting against this file's
    collection-time reference can interrogate a module object production is no longer
    using."""
    return sys.modules.get("narration_api", na)


#: An accepted outline packet in the exact shape `narasi_outline_packet` renders, so the
#: server's own parser is what bounds the beat ordinals the detectors may cite.
PACKET_CH3 = (
    "OUTLINE PACKET CONTRACT: v1\n"
    "\n"
    "CURRENT ORDERED OUTLINE BEATS:\n"
    "Chapter 3: \"The Choice\"\n"
    "  1. she refuses the settlement\n"
    "  2. she and Jin make the final choice together\n"
    "\n"
    "NEXT RESERVED OUTLINE STATE:\n"
    "NONE — this chapter closes the book.\n")


def _book():
    return "\n".join("## Chapter %d\n%s tak terbantahkan" % (i, "kata " * 80)
                     for i in range(1, 5))


@pytest.fixture
def gate_run(monkeypatch):
    """Run the real gate phase; return a recorder of what the merge point received."""
    # 🔴 PATCH THE MODULE THAT IS REGISTERED NOW, NOT THIS FILE'S IMPORT. Several legacy
    # endpoint tests reload `laozhang_api` during a full-tree run, which leaves the
    # collection-time reference above pointing at a DEAD module object: the stubs land
    # on the corpse, `_apply_v3_gates` calls the live one, and the gate reaches for a
    # real network client. Alone this file passed; in the full tree it errored with
    # "cheap call failed (non-fatal): Connection error" — a failure mode that only
    # exists when the whole suite runs, which is exactly why the tree is the check.
    live_lz = sys.modules.get("laozhang_api", lz)
    live_na = sys.modules.get("narration_api", na)
    seen = {"dedup_input": [], "revise_violations": None, "clients": 0}

    def blocked(*_a, **_k):
        seen["clients"] += 1
        raise RuntimeError("network blocked in test")

    async def critique(*_a, **_k):
        return ({"violations": []}, 0)

    async def revise(_book, request, *_a, **_k):
        seen["revise_violations"] = request.get("violations")
        return ("REVISED", 0)

    async def cheap(sys_prompt, *_a, **_k):
        text = str(sys_prompt)
        if "register auditor" in text:
            return (json.dumps({"counts": {"withhold_judgment": 0}}), 0)
        if "THREADS: " in text:
            # Pass 2. The gate REWRITES every thread id (`s0_<slug>`) before building
            # this prompt, so echo back the ids it actually sent. A stub that replies
            # with pass 1's slugs is silently dropped by the id lookup and the whole
            # detector reports a clean run — a vacuum that looks like a pass.
            threads, _end = json.JSONDecoder().raw_decode(
                text.split("THREADS: ", 1)[1])
            return (json.dumps({"unresolved": [
                {"id": t.get("id"), "why": "the ending never returns to it"}
                for t in threads]}), 0)
        if "looking for every THREAD" in text:
            return (json.dumps({"threads": [
                {"id": "final-choice", "thread_type": "promised_consequence",
                 "description": "the choice is never made on-page",
                 "chapter_introduced": 3, "quote": "kata kata kata"}]}), 0)
        return ("{}", 0)

    real_dedup = live_na._v3g_dedup_violations

    def spy(violations):
        seen["dedup_input"] = [dict(v) for v in violations if isinstance(v, dict)]
        return real_dedup(violations)

    monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
    monkeypatch.setenv("NARASI_REGISTER_GATE", "1")
    monkeypatch.setenv("NARASI_REGISTER_GATE_ENFORCE", "1")
    monkeypatch.setenv("NARASI_THREAD_TRACKER", "1")
    monkeypatch.setenv("NARASI_THREAD_TRACKER_ENFORCE", "1")
    monkeypatch.setattr(live_na, "_v3g_dedup_violations", spy)
    monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
    monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
    monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
    monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(live_lz, "_narasi_consistency_revise", revise)

    import pakem
    monkeypatch.setattr(pakem, "resolve_style", lambda _s: {"register_spec": {
        "required_moves": ["withhold_judgment"],
        "banned_tells": ["tak terbantahkan"]}})
    monkeypatch.setattr(pakem, "resolve_style_key", lambda _s: "harari")

    def run():
        result = {"ok": True, "book": _book(),
                  "chapters": [{"no": i} for i in range(1, 5)]}
        body = {"chapters": [{"word_target": 800}] * 4,
                "style": "harari", "language": "id"}
        asyncio.run(live_na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                            job_uuid=None, sink=None, job_id="j"))
        assert seen["clients"] == 0, "a provider client was built — this test is not offline"
        assert seen["dedup_input"], "the merge point was never reached; the run proves nothing"
        seen["result"] = result
        return seen

    return run


def _by_type(seen, vtype):
    return [v for v in seen["dedup_input"] if v.get("type") == vtype]


@pytest.fixture
def pair_run(monkeypatch):
    """🔴 THE GOLDEN, ON THE REAL GATE. Two detectors, two readings of ONE silence: the
    critic calls the final choice an outlined beat that never lands, the thread tracker
    calls it a thread the ending never returns to. Both are asked for a BOUNDED
    reference into the accepted outline, and this fixture drives the real
    `_apply_v3_gates` end to end to see whether they arrive as one target or two."""
    live_lz = sys.modules.get("laozhang_api", lz)
    live_na = sys.modules.get("narration_api", na)
    seen = {"dedup_input": [], "dedup_output": None, "clients": 0, "pass2_prompt": ""}

    def blocked(*_a, **_k):
        seen["clients"] += 1
        raise RuntimeError("network blocked in test")

    async def critique(*_a, **_k):
        return ({"violations": [
            # The final-choice reading, deliberately the MILDER of the two.
            {"type": "outline_missing_beat", "severity": "low", "chapter": 3,
             "outline_chapter": 3, "outline_beat": 2,
             "evidence": "the mild wording", "fix": "land the outlined choice on-page"},
            # Unrelated, and `critical|high` so the critic's revise lane arms at all.
            {"type": "timeline", "severity": "high", "chapter": 1,
             "evidence": "a date drifts", "fix": "align the date"},
        ]}, 0)

    async def revise(_book, request, *_a, **_k):
        seen["revise_violations"] = request.get("violations")
        return ("REVISED", 0)

    async def cheap(sys_prompt, *_a, **_k):
        text = str(sys_prompt)
        if "THREADS: " in text:
            seen["pass2_prompt"] = text
            threads, _end = json.JSONDecoder().raw_decode(
                text.split("THREADS: ", 1)[1])
            return (json.dumps({"unresolved": [
                {"id": t.get("id"), "why": "the ending never returns to it",
                 "outline_chapter": 3, "outline_beat": 2} for t in threads]}), 0)
        if "looking for every THREAD" in text:
            return (json.dumps({"threads": [
                {"id": "final-choice", "thread_type": "promised_consequence",
                 "description": "the final choice is never made on-page",
                 "chapter_introduced": 1, "quote": "kata kata kata"}]}), 0)
        return ("{}", 0)

    real_dedup = live_na._v3g_dedup_violations

    def spy(violations):
        seen["dedup_input"] = [dict(v) for v in violations if isinstance(v, dict)]
        out = real_dedup(violations)
        seen["dedup_output"] = out
        return out

    monkeypatch.setenv("NARASI_DIET_MAX_LOOPS", "0")
    monkeypatch.setenv("NARASI_REGISTER_GATE", "0")
    monkeypatch.setenv("NARASI_THREAD_TRACKER", "1")
    monkeypatch.setenv("NARASI_THREAD_TRACKER_ENFORCE", "1")
    monkeypatch.setattr(live_na, "_v3g_dedup_violations", spy)
    monkeypatch.setattr(live_lz, "make_narasi_client", blocked)
    monkeypatch.setattr(live_lz, "_narasi_cheap_call", cheap)
    monkeypatch.setattr(live_lz, "_narasi_critique_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "_narasi_critique_revise_enabled", lambda: True)
    monkeypatch.setattr(live_lz, "NARASI_CRITIQUE_MIN_CHAPTERS", 1)
    monkeypatch.setattr(live_lz, "_narasi_consistency_critique", critique)
    monkeypatch.setattr(live_lz, "_narasi_consistency_revise", revise)

    def run():
        result = {"ok": True, "book": _book(),
                  "chapters": [{"no": i} for i in range(1, 5)],
                  # The private MAP authority packet, exactly where the gate reads it.
                  "_narrative_authority": {
                      "text": "AUTHORITY",
                      "outline_packets_by_chapter": {"3": PACKET_CH3}}}
        body = {"chapters": [{"word_target": 800}] * 4,
                "style": "storytelling", "language": "id"}
        asyncio.run(live_na._apply_v3_gates(result, body, tenant_id="t", user_id="u",
                                            job_uuid=None, sink=None, job_id="j"))
        assert seen["clients"] == 0, "a provider client was built — this is not offline"
        assert seen["dedup_input"], "the merge point was never reached"
        seen["result"] = result
        return seen

    return run


def _authority_beat_targets(seen):
    """The surviving targets in the `authority_beat` family — the pair's repair slots."""
    return [v for v in (seen["dedup_output"] or [])
            if str(v.get("type") or "") in ("outline_missing_beat", "unresolved_thread")]


# ---------------------------------------------------------------------------
# The sidecar exists on the real emission path
# ---------------------------------------------------------------------------
def test_the_register_gate_publishes_a_server_owned_claim_for_each_finding(gate_run):
    """Both register lanes — a required move never executed and a banned tell present —
    carry the token this process built from its OWN vocabulary."""
    seen = gate_run()
    claims = sorted(v.get("_f5_claim") for v in _by_type(seen, "register"))
    assert claims == ["register_move:withhold_judgment",
                      "register_tell:tak terbantahkan"]


def test_the_thread_tracker_publishes_the_server_rewritten_thread_id(gate_run):
    """The id in the token is the gate's OWN rewritten id, not the model's raw slug —
    the rewrite is what guarantees two distinct threads cannot collide on one token."""
    seen = gate_run()
    threads = _by_type(seen, "unresolved_thread")
    assert len(threads) == 1
    claim = threads[0].get("_f5_claim")
    assert claim.startswith("thread:")
    assert claim != "thread:final-choice", "the raw model slug must not be the identity"
    assert claim == "thread:s0_final-choice"


# ---------------------------------------------------------------------------
# ...and it is what makes the semantic path resolvable at all
# ---------------------------------------------------------------------------
def test_every_real_finding_now_resolves_to_a_semantic_identity(gate_run):
    """🔴 THE REGRESSION THIS FILE EXISTS FOR. Before the sidecar, EVERY one of these
    resolved to None — the register gate and canon-diff publish no chapter, and the
    chapter guard refused an identity without one. The semantic branch could not fire
    on a single production finding."""
    seen = gate_run()
    for violation in seen["dedup_input"]:
        assert _na()._v3g_claim_identity(violation) is not None, violation


def test_stripping_the_sidecar_returns_every_finding_to_no_identity(gate_run):
    """The mirror of the test above, on the SAME real dicts: remove `_f5_claim` and the
    production shape is unidentifiable again. This is what pins the sidecar as the
    load-bearing part rather than something the shape happened to satisfy anyway."""
    seen = gate_run()
    for violation in seen["dedup_input"]:
        bare = {k: v for k, v in violation.items() if k != "_f5_claim"}
        assert _na()._v3g_claim_identity(bare) is None, bare


# ---------------------------------------------------------------------------
# ...and two sightings of ONE real claim collapse to one repair slot
# ---------------------------------------------------------------------------
def test_two_sightings_of_one_real_claim_collapse_at_the_merge_point(gate_run):
    """The duplicate is built by re-sighting a finding the REAL detector emitted —
    different wording, different severity, same claim — not by inventing a shape."""
    seen = gate_run()
    original = _by_type(seen, "register")[0]
    second = dict(original)
    second["evidence"] = "a different auditor, the same missing move"
    second["severity"] = "critical"

    merged = _na()._v3g_dedup_violations([original, second])
    registers = [v for v in merged if v.get("type") == "register"]
    assert len(registers) == 1, "two sightings of one move still burn two repair slots"
    assert registers[0]["severity"] == "critical", "the merge must keep the worse judgement"
    assert registers[0]["evidence"] == "a different auditor, the same missing move"


def test_two_different_real_claims_are_never_collapsed(gate_run):
    """The other direction, and the one that matters more: the move and the banned tell
    are different claims from the SAME detector and must both survive."""
    seen = gate_run()
    registers = _by_type(seen, "register")
    merged = _na()._v3g_dedup_violations(list(registers))
    assert len(merged) == 2, "two distinct register claims were merged into one"


def test_the_sidecar_never_reaches_the_revise_payload(gate_run):
    """Server-owned means server-only: nothing starting with `_f5` may reach a provider
    prompt or a public payload."""
    seen = gate_run()
    assert seen["revise_violations"], "the revise never ran; this proves nothing"
    for violation in seen["revise_violations"]:
        assert not [k for k in violation if str(k).startswith("_f5")], violation


def test_the_sidecar_never_reaches_the_published_thread_tracker_report(gate_run):
    """🔴 THE SECOND EXIT, WHICH THE FIRST VERSION OF THIS WORK LEAKED THROUGH. The
    merge point strips the sidecar on its way out, so the revise payload was clean —
    but the thread tracker also publishes its violations into `result`, and that path
    had no strip at all. The job result carried `_f5_claim` straight out. Both exits go
    through one helper now; this test is the second exit's own witness."""
    seen = gate_run()
    report = seen["result"].get("thread_tracker") or {}
    assert report.get("violations"), "the tracker published nothing; this proves nothing"
    for violation in report["violations"]:
        assert not [k for k in violation if str(k).startswith("_f5")], violation


# ---------------------------------------------------------------------------
# THE GOLDEN: two paraphrases of one final-choice silence become ONE target
# ---------------------------------------------------------------------------
def test_both_detectors_reach_the_merge_point_with_the_same_outline_beat_token(pair_run):
    """The precondition for everything below: the critic and the thread tracker each
    produced the SAME server-validated token from their own bounded reference."""
    seen = pair_run()
    critic = _by_type(seen, "outline_missing_beat")
    tracker = _by_type(seen, "unresolved_thread")
    assert len(critic) == 1 and len(tracker) == 1, seen["dedup_input"]
    assert critic[0]["_f5_claim"] == "outline_beat:3|2"
    assert tracker[0]["_f5_claim"] == "outline_beat:3|2", (
        "the tracker fell back to its thread id — no shared vocabulary, no merge")


def test_the_final_choice_pair_collapses_to_one_repair_target(pair_run):
    """🔴 THE CONTRACT THIS WHOLE MECHANISM EXISTS FOR. Before the shared reference the
    pair burned TWO repair-budget slots for one defect, and the specific type's routing
    never ran because the generic sighting survived beside it."""
    seen = pair_run()
    targets = _authority_beat_targets(seen)
    assert len(targets) == 1, (
        f"the final-choice pair still burns {len(targets)} targets: {targets}")


def test_the_authority_specific_type_wins_the_routing(pair_run):
    """`outline_missing_beat` routes to the structural addressed-patch lane;
    `unresolved_thread` does not. The survivor must be the one that routes, even though
    it arrived as the MILDER sighting."""
    seen = pair_run()
    assert _authority_beat_targets(seen)[0]["type"] == "outline_missing_beat"


def test_the_survivor_carries_the_severest_judgement_and_its_own_words(pair_run):
    """Severity, evidence and fix travel together: a `high` label attached to the mild
    sighting's wording describes nothing a repair prompt can act on."""
    seen = pair_run()
    survivor = _authority_beat_targets(seen)[0]
    assert survivor["severity"] == "high", "the milder sighting's severity survived"
    assert "the ending never returns to it" in survivor["fix"], (
        "the severest occurrence's own wording did not travel with its severity")
    assert survivor["evidence"] != "the mild wording"


def test_an_unrelated_finding_is_not_swept_into_the_pair(pair_run):
    """The merge must be narrow: the timeline finding shares neither claim nor family."""
    seen = pair_run()
    others = [v for v in seen["dedup_output"] if v.get("type") == "timeline"]
    assert len(others) == 1
    assert len(seen["dedup_output"]) == 2, seen["dedup_output"]


def test_the_tracker_is_shown_the_accepted_outline_beats_to_reference(pair_run):
    """The tracker's pass 2 sees threads and an ending; without the beats handed over
    it has nothing to point at and can only fall back to its own slug."""
    seen = pair_run()
    assert "OUTLINE BEATS:" in seen["pass2_prompt"]
    assert "ch3 beat2: she and Jin make the final choice together" in seen["pass2_prompt"]


def test_the_pair_stays_two_targets_when_the_reference_is_out_of_bounds(monkeypatch,
                                                                        pair_run):
    """🔴 THE REFUSAL THAT KEEPS THIS HONEST. A beat ordinal the accepted outline does
    not have is not a claim anybody verified, so it must NOT merge — two targets is the
    safe answer, because dropping a real distinct violation is the worse error."""
    live_na = sys.modules.get("narration_api", na)
    real = live_na._f5_outline_beats
    # One beat in that chapter, so the cited ordinal 2 is now out of bounds.
    monkeypatch.setattr(live_na, "_f5_outline_beats",
                        lambda packet: real(packet)[:1])
    seen = pair_run()
    assert len(_authority_beat_targets(seen)) == 2, (
        "an unverifiable beat reference was allowed to merge two findings")


# ---------------------------------------------------------------------------
# The bounded reference: an identity only when the accepted outline vouches for it
# ---------------------------------------------------------------------------
_PACKET = ("OUTLINE PACKET CONTRACT: v1\n"
           "\n"
           "CURRENT ORDERED OUTLINE BEATS:\n"
           "Chapter 3: \"The Choice\"\n"
           "  1. she refuses the settlement\n"
           "  2. she signs the deposition\n"
           "\n"
           "NEXT RESERVED OUTLINE STATE:\n"
           "NONE — this chapter closes the book.\n")


def test_the_outline_packet_beat_count_is_read_from_the_accepted_packet():
    assert _na()._f5_outline_beat_count(_PACKET) == 2
    assert _na()._f5_outline_beat_count("") == 0
    assert _na()._f5_outline_beat_count("no beats section here") == 0


@pytest.mark.parametrize("beat, expected", [
    (1, "outline_beat:3|1"),
    (2, "outline_beat:3|2"),
    (3, ""),                 # past the end of THIS chapter's accepted outline
    (0, ""),
    (-1, ""),
    ("final_choice", ""),    # a name is not an ordinal the packet can vouch for
    (True, ""),              # bool is an int in Python; it is not a beat
    (None, ""),
])
def test_the_beat_reference_is_admitted_only_within_the_accepted_outline(beat, expected):
    """🔴 BOUNDED MEANS CHECKED, NOT TRUSTED. The reference comes from a model, so it is
    admitted only when the accepted outline actually has that beat. Everything else
    yields "", which leaves the finding unmergeable: dropping a real distinct violation
    is the worse error of the two."""
    source = {"outline_chapter": 3, "outline_beat": beat}
    assert _na()._f5_outline_beat_claim(source, {"3": _PACKET}) == expected


def test_the_beat_reference_needs_the_packet_for_that_very_chapter():
    """A beat ordinal vouched for by ANOTHER chapter's outline is not vouched for."""
    source = {"outline_chapter": 4, "outline_beat": 1}
    assert _na()._f5_outline_beat_claim(source, {"3": _PACKET}) == ""
    assert _na()._f5_outline_beat_claim(source, {}) == ""


def test_the_same_reference_yields_one_token_whichever_detector_sends_it():
    """🔴 ONE FUNCTION, ONE TOKEN. The critic sends the reference on a violation dict and
    the tracker on a pass-2 entry; if each built its own token the pair could never
    match, which is precisely the state this replaced."""
    from_critic = {"type": "outline_missing_beat", "chapter": 7,
                   "outline_chapter": 3, "outline_beat": 2, "evidence": "x"}
    from_tracker = {"id": "s0_final-choice", "why": "y",
                    "outline_chapter": 3, "outline_beat": 2}
    claim = _na()._f5_outline_beat_claim(from_critic, {"3": _PACKET})
    assert claim == "outline_beat:3|2"
    assert _na()._f5_outline_beat_claim(from_tracker, {"3": _PACKET}) == claim


def test_a_finding_without_a_beat_reference_is_passed_through_untouched():
    """When a detector cannot point at an outlined beat it must omit the reference, and
    the dict then arrives at the merge point exactly as it was written — no empty
    `_f5_claim` key attached, no identity invented."""
    violation = {"type": "outline_missing_beat", "chapter": 3,
                 "evidence": "the choice never lands", "fix": "land it"}
    assert _na()._f5_outline_beat_claim(violation, {"3": _PACKET}) == ""
    assert _na()._v3g_claim_identity(violation) is None
