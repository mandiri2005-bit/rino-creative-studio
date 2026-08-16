"""F5 — authority protection and semantic dedup.

BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md §F5 (`fecd3dcb…20b5`), plus Rino's
2026-08-15 correction that supersedes parts of the first instruction.

🔴 TWO FAILURE MODES, AND WHY EACH IS EXPENSIVE.

1. THE LANE LEDGER TELLING REPAIR TO RENAME THIS STORY'S OWN CANON. The ledger bans
   terms overused ACROSS OTHER stories in the lane. A name, number or literal that the
   accepted outline, the pinned Bible or the canon registry OWNS is not a lane tic —
   and once repair actually lands (F4b made it land), obeying that hit corrupts the
   story's own canon. Canary v9 recorded exactly this: `['name:Tae-jun', 'number:11',
   'number:19', 'number:22']` poisoning every chapter.

2. FOUR DETECTORS BILLING FOUR SLOTS FOR ONE DEFECT. critic, register, canon_diff and
   thread_tracker each describe the same claim in their own words. Keying dedup on the
   EVIDENCE TEXT dedups presentations, not claims — so the same defect still burns the
   repair budget several times over, and the generic finding can outlive the specific
   one that would have routed to the structural lane.

🔴 THE SPECIFICITY RULE. `outline_missing_beat` is bound to accepted narrative authority
   and routes to the addressed-patch lane; `unresolved_thread` is a generic fallback.
   When both describe the SAME claim, the authority-specific one must survive —
   whichever order they arrive in.

🔴 SEVERITY MERGE, NOT FIRST-OCCURRENCE. Rino's correction: the survivor keeps the
   HIGHEST severity of the merged set. Taking the first occurrence's severity would let
   a `low` sighting arriving first silently downgrade a `critical` one.
"""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python"))

import narasi_counters as nc  # noqa: E402
import narration_api as na  # noqa: E402


# ── fixtures: an accepted authority that does NOT repeat itself verbatim ───

TOPIC = "Kontrak cinta 30 hari di rooftop Seoul."
OUTLINE = (
    "1. Atap — Mira dan Tae-jun menandatangani kontrak di lantai 11.\n"
    "2. Sidang — deposisi dibuka pada hari ke-19.\n"
    "3. Pilihan — keduanya memilih bersama, setara."
)
BIBLE = "FACT-SHEET: gedung Hanul, atap lantai sebelas, sidang tertutup."


class _Entity:
    def __init__(self, name, aliases=()):
        self.canonical_name = name
        self.aliases = tuple(aliases)


class _Anchor:
    def __init__(self, kind, literal):
        self.kind = kind
        self.literal = literal


class _Canon:
    """Stands in for the Canon Lite registry: entities + anchors only."""

    def __init__(self, entities=(), anchors=()):
        self.entities = tuple(entities)
        self.anchors = tuple(anchors)


CANON = _Canon(
    entities=[_Entity("Eun-soo", aliases=("Soo",)), _Entity("Tae-jun")],
    anchors=[_Anchor("duration", "30 days"), _Anchor("address", "Hanul 11F")],
)


def _ownership(**kwargs):
    base = dict(topic=TOPIC, outline=OUTLINE, bible=BIBLE, canon=CANON)
    base.update(kwargs)
    return nc.build_authority_ownership(**base)


# ══════════════════════════════════════════════════════════════════════════
# OWNERSHIP SET
# ══════════════════════════════════════════════════════════════════════════

def test_accepted_outline_owns_a_name_the_topic_never_writes():
    """🔴 THE CANARY v9 DEFECT. `Tae-jun` is established by the accepted outline and
    appears nowhere in the user's topic. Before F5 only the topic was consulted, so the
    ledger was free to order repair to rename the story's own protagonist."""
    assert "Tae-jun" not in TOPIC
    assert nc.authority_owns_term("Tae-jun", _ownership()) is True


def test_accepted_outline_owns_a_number_across_languages():
    """The outline writes «lantai 11» and «hari ke-19»; a ledger hit on the English
    number word must still read as owned. The matcher already does cross-language
    numerics — F5's job is to point it at the outline as well as the topic."""
    own = _ownership()
    assert nc.authority_owns_term("11", own) is True
    assert nc.authority_owns_term("eleven", own) is True
    assert nc.authority_owns_term("19", own) is True


def test_the_pinned_bible_owns_its_own_established_terms():
    own = _ownership()
    assert nc.authority_owns_term("Hanul", own) is True
    assert nc.authority_owns_term("sebelas", own) is True


def test_canon_registry_owns_canonical_names_and_registered_aliases():
    """Entities contribute their canonical name AND every accepted alias."""
    own = _ownership(topic="", outline="", bible="")
    assert nc.authority_owns_term("Eun-soo", own) is True
    assert nc.authority_owns_term("Soo", own) is True, "a registered alias is owned"


def test_canon_anchors_own_bound_literals_and_their_numbers():
    """A bound literal owns itself and the number inside it, across languages."""
    own = _ownership(topic="", outline="", bible="")
    assert nc.authority_owns_term("30 days", own) is True
    assert nc.authority_owns_term("30", own) is True
    assert nc.authority_owns_term("thirty", own) is True


def test_the_number_matchers_language_coverage_is_pinned_not_assumed():
    """🔴 A DOCUMENTED LIMIT, NOT A SILENT ONE. Cross-language equivalence holds only as
    far as the shared matcher's own word map reaches: Indonesian TEENS are covered
    (`sebelas` ↔ 11, and the `N belas` special case), Indonesian TENS are not
    (`tiga puluh` has no entry). F5 deliberately does NOT widen that map — it is shared
    with other lanes and widening it is its own change with its own blast radius. Pinned
    here so the gap is visible rather than mistaken for coverage."""
    own = _ownership(topic="", outline="", bible="")
    assert nc.authority_owns_term("thirty", own) is True
    assert nc.authority_owns_term("tiga puluh", own) is False, (
        "not a bug in F5: the shared number map has no Indonesian tens")

    teens = nc.build_authority_ownership(topic="kontrak sebelas hari", outline="",
                                         bible="", canon=None)
    assert nc.authority_owns_term("11", teens) is True
    assert nc.authority_owns_term("eleven", teens) is True


def test_an_unregistered_near_alias_is_not_owned():
    """🔴 THE FALSE EXEMPTION THAT WOULD DISARM THE LEDGER. Ownership is matched by
    category and value, never by free substring: a name that merely looks like a
    registered one, an unrelated number, or a token that happens to sit inside another
    word must all stay enforceable."""
    own = _ownership()
    # `Eun` and `day` are genuine substrings of registered values (`eun-soo`,
    # `30 days`) — exactly what a substring match would wrongly exempt.
    for term in ("Eun", "day", "Eun-soo-ya", "Taejun", "Soo-ah", "Hanulmart",
                 "42", "forty-two"):
        assert nc.authority_owns_term(term, own) is False, term


def test_ownership_refuses_empty_and_junk_terms():
    own = _ownership()
    for term in ("", "   ", None):
        assert nc.authority_owns_term(term, own) is False


def test_the_candidate_bible_is_never_its_own_authority_at_pin_time():
    """🔴 THE SELF-EXEMPTION TRAP. At Bible-pin the candidate has not been accepted yet.
    Feeding it in as authority would make every term the Bible just invented
    self-exempt and switch ledger enforcement off entirely."""
    own = nc.build_authority_ownership(topic=TOPIC, outline=OUTLINE, bible="", canon=None)
    assert nc.authority_owns_term("Hanul", own) is False, (
        "only the ACCEPTED authorities may exempt; the candidate Bible may not")
    assert nc.authority_owns_term("Tae-jun", own) is True, (
        "the accepted outline still protects its own terms")


# ══════════════════════════════════════════════════════════════════════════
# LEDGER EXEMPTION AT THE TWO CALL SITES
# ══════════════════════════════════════════════════════════════════════════

def test_the_post_generation_exemption_reads_the_full_pinned_authority():
    """`canonical_facts` may be empty or may simply not repeat the literal. The accepted
    outline, held privately on the result, must still protect it."""
    result = {"canonical_facts": "", "_narrative_authority": {"text": OUTLINE}}
    assert na._ledger_hit_is_exempt_for_result("Tae-jun", "unrelated topic", result) is True
    assert na._ledger_hit_is_exempt_for_result("11", "unrelated topic", result) is True


def test_an_unowned_term_still_reaches_repair():
    result = {"canonical_facts": BIBLE, "_narrative_authority": {"text": OUTLINE}}
    assert na._ledger_hit_is_exempt_for_result("Hanjin", TOPIC, result) is False


def test_the_authority_text_never_leaves_the_exemption():
    """Authority is read INTERNALLY. It must not be returned, logged or attached — the
    exemption answers a boolean and nothing else."""
    result = {"canonical_facts": BIBLE, "_narrative_authority": {"text": OUTLINE}}
    before = dict(result)
    verdict = na._ledger_hit_is_exempt_for_result("Tae-jun", TOPIC, result)
    assert verdict is True
    assert result == before, "the exemption mutated the result payload"


# ══════════════════════════════════════════════════════════════════════════
# SEMANTIC CLAIM DEDUP
# ══════════════════════════════════════════════════════════════════════════

def _v(vtype, chapter, evidence, *, severity="high", **extra):
    item = {"type": vtype, "chapter": chapter, "evidence": evidence,
            "severity": severity, "fix": "…"}
    item.update(extra)
    return item


def test_the_same_claim_worded_differently_collapses_to_one_target():
    """🔴 THE GOLDEN. Two detectors, two paraphrases, ONE authority beat in ONE chapter.
    Evidence-keyed dedup keeps both and bills two slots; claim-keyed dedup keeps one."""
    a = _v("outline_missing_beat", 3,
           '"Stay?" remains unanswered; Eun-soo never makes the final mutual choice.',
           beat="final_choice")
    b = _v("unresolved_thread", 3,
           "The outline's completed equal-partner decision never occurs on-page.",
           beat="final_choice")

    out = na._v3g_dedup_violations([a, b])
    assert len(out) == 1
    assert out[0]["type"] == "outline_missing_beat"


def test_the_specificity_rule_holds_in_both_input_orders():
    """A generic fallback must never displace the authority-bound classification, and
    the result must not depend on which detector happened to run first."""
    specific = _v("outline_missing_beat", 3, "beat never lands", beat="final_choice")
    generic = _v("unresolved_thread", 3, "thread left open", beat="final_choice")

    for order in ([specific, generic], [generic, specific]):
        out = na._v3g_dedup_violations(list(order))
        assert len(out) == 1, order
        assert out[0]["type"] == "outline_missing_beat", order


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [("low", "critical", "critical"), ("critical", "low", "critical"),
     ("medium", "high", "high"), ("high", "medium", "high")],
)
def test_the_survivor_keeps_the_highest_severity_not_the_first(first, second, expected):
    """🔴 RINO'S CORRECTION. First-occurrence-wins lets a `low` sighting that happens to
    arrive first silently downgrade a `critical` one describing the same claim."""
    a = _v("outline_missing_beat", 3, "a", severity=first, beat="final_choice")
    b = _v("outline_missing_beat", 3, "b", severity=second, beat="final_choice")

    out = na._v3g_dedup_violations([a, b])
    assert len(out) == 1
    assert out[0]["severity"] == expected


def test_equal_types_let_the_higher_severity_occurrence_keep_its_evidence():
    """Rino's tie-break #2. Severity decides not only the survivor's severity but WHICH
    occurrence leads — so the evidence and fix a reader sees belong to the finding that
    judged the claim most seriously, not to whichever detector happened to run first."""
    low = _v("outline_missing_beat", 3, "the mild wording", severity="low",
             beat="final_choice")
    high = _v("outline_missing_beat", 3, "the severe wording", severity="critical",
              beat="final_choice")

    for order in ([low, high], [high, low]):
        out = na._v3g_dedup_violations(list(order))
        assert len(out) == 1
        assert out[0]["severity"] == "critical"
        assert out[0]["evidence"] == "the severe wording", order


def test_the_same_claim_in_different_chapters_stays_two_findings():
    a = _v("outline_missing_beat", 3, "x", beat="final_choice")
    b = _v("outline_missing_beat", 4, "x", beat="final_choice")
    assert len(na._v3g_dedup_violations([a, b])) == 2


def test_different_claims_in_the_same_chapter_stay_two_findings():
    a = _v("outline_missing_beat", 3, "x", beat="final_choice")
    b = _v("outline_missing_beat", 3, "y", beat="first_meeting")
    assert len(na._v3g_dedup_violations([a, b])) == 2


def test_two_distinct_threads_in_one_chapter_stay_two_findings():
    a = _v("unresolved_thread", 3, "the contract @ch3", thread="contract")
    b = _v("unresolved_thread", 3, "the recording @ch3", thread="recording")
    assert len(na._v3g_dedup_violations([a, b])) == 2


def test_an_unknown_chapter_never_collapses_two_findings():
    """Conservative by construction: without a resolvable chapter the two findings may
    be targeting different ones, and dropping a real violation is the worse error."""
    a = _v("outline_missing_beat", None, "no locator", beat="final_choice")
    b = _v("outline_missing_beat", None, "no locator either", beat="final_choice")
    assert len(na._v3g_dedup_violations([a, b])) == 2


def test_findings_with_no_safe_identity_are_both_kept():
    """No structured claim identity, different evidence — merging would be a guess."""
    a = _v("timeline", 3, "the clock jumps")
    b = _v("timeline", 3, "the season changes")
    assert len(na._v3g_dedup_violations([a, b])) == 2


def test_a_literal_repeat_still_collapses():
    """The behaviour F5 already had must survive the rewrite."""
    a = _v("ledger_hit", 3, "«Tae-jun» reused")
    b = _v("ledger_hit", 3, "«Tae-jun» reused")
    assert len(na._v3g_dedup_violations([a, b])) == 1


def test_dedup_never_mutates_its_inputs():
    """The same violation dicts feed report/telemetry elsewhere."""
    a = _v("outline_missing_beat", 3, "a", severity="low", beat="final_choice")
    b = _v("unresolved_thread", 3, "b", severity="critical", beat="final_choice")
    snapshot = (dict(a), dict(b))

    na._v3g_dedup_violations([a, b])
    assert (a, b) == snapshot, "dedup mutated a caller's violation dict"


def test_no_internal_claim_metadata_reaches_the_survivor():
    """🔴 THE KEY IS INTERNAL. It must never ride out to a provider prompt or a public
    payload — the survivor carries only fields the pipeline already published."""
    a = _v("outline_missing_beat", 3, "a", beat="final_choice")
    b = _v("unresolved_thread", 3, "b", beat="final_choice")

    out = na._v3g_dedup_violations([a, b])
    for key in out[0]:
        assert not str(key).startswith("_"), f"internal key {key!r} leaked"
    assert "claim_key" not in out[0]
    assert "_claim" not in out[0]


# ══════════════════════════════════════════════════════════════════════════
# BIBLE-PIN END-TO-END — zero provider call for an authority-owned hit
# ══════════════════════════════════════════════════════════════════════════

def test_the_bible_pin_filter_keeps_only_unowned_terms():
    """🔴 THE FILTER ITSELF, EXECUTED. An earlier version of this test drove
    `narrate_chapters` end to end and asserted "no enforcement provider call" — but the
    enforcement branch is only reached when the bible is BUILT in-run, and the harness
    supplied one instead. `ledger_hits_scan` was never called, so the assertion held for
    a reason that had nothing to do with F5, and the mutation run caught it surviving.
    The decision now lives in a named helper and is called directly."""
    import orchestrator.static as st

    outline = "1. Atap — Mira dan Tae-jun bertemu di lantai 11\n2. Sidang\n3. Pilihan"
    kept = st._f5_unowned_ledger_terms(
        ["Tae-jun", "11", "Hanjin", "Mira"], topic="Kontrak cinta di Seoul.",
        outline=outline)

    assert kept == ["Hanjin"], (
        "only the term no accepted authority owns may reach enforcement")


def test_the_bible_pin_filter_cannot_be_handed_the_candidate_bible():
    """🔴 THE SELF-EXEMPTION TRAP, CLOSED BY THE SIGNATURE. There is no `bible`
    parameter, so the candidate cannot be passed in by accident or by a later edit that
    "just adds one more authority"."""
    import inspect

    import orchestrator.static as st

    params = set(inspect.signature(st._f5_unowned_ledger_terms).parameters)
    assert params == {"terms", "topic", "outline"}, params
    assert "bible" not in params
    assert "canonical_facts" not in params

    # And the call site passes exactly those two authorities, nothing more.
    source = inspect.getsource(st.narrate_chapters)
    call = source[source.index("_f5_unowned_ledger_terms"):][:220]
    assert "topic=" in call and "outline=" in call
    assert "_bible" not in call, "the candidate bible reached the pin-time filter"

    # ...and the term set it is handed keeps `category:value` INTACT. Splitting it at
    # the collection site would strip the category before the filter ever sees it —
    # invisible to a test that calls the filter directly with well-formed input.
    collect = source[source.index("_terms_all = sorted("):][:320]
    assert '.split(":", 1)[-1]' not in collect, (
        "the ledger category is stripped before the pin-time ownership decision")


def test_an_unowned_bible_hit_is_still_enforceable():
    """The mirror control. `Hanjin` appears in neither topic nor outline, so it stays a
    lane tic and must remain eligible for enforcement — F5 narrows the exemption, it
    does not disarm the ledger."""
    own = nc.build_authority_ownership(
        topic="Kontrak cinta di Seoul.",
        outline="1. Atap — Mira dan Tae-jun bertemu\n2. Sidang\n3. Pilihan",
        bible="")
    assert nc.authority_owns_term("Tae-jun", own) is True
    assert nc.authority_owns_term("Hanjin", own) is False, (
        "an unowned lane term must stay enforceable")


# ══════════════════════════════════════════════════════════════════════════
# THE REVISE SEAM — dedup happens BEFORE budgeting
# ══════════════════════════════════════════════════════════════════════════

def test_dedup_runs_at_the_merge_point_before_the_revise_is_called():
    """🔴 DEDUP AFTER BUDGETING SAVES NOTHING. Proved structurally, not by re-running the
    gate: the merged list handed to `_narasi_consistency_revise` must BE the output of
    `_v3g_dedup_violations`, with nothing budgeting-related between them. An AST witness
    rather than a string match, so a comment or a rename cannot fake it."""
    import ast
    import inspect

    source = inspect.getsource(na._apply_v3_gates)
    tree = ast.parse(textwrap.dedent(source))

    dedup_targets = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "_v3g_dedup_violations"):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    dedup_targets.add(target.id)
    assert dedup_targets, "the merge point no longer calls _v3g_dedup_violations"

    # The merged list reaches the revise through one wrapper
    # (`{"violations": _v3g_merged}`), so follow assignment dataflow transitively
    # rather than demanding the literal name at the call site.
    reachable = set(dedup_targets)
    for _ in range(6):                       # fixed point; the chain is short
        grew = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            if names & reachable:
                for target in node.targets:
                    for inner in ast.walk(target):
                        if isinstance(inner, ast.Name) and inner.id not in reachable:
                            reachable.add(inner.id)
                            grew = True
        if not grew:
            break

    revise_args = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_narasi_consistency_revise":
                for part in list(node.args) + [k.value for k in node.keywords]:
                    for inner in ast.walk(part):
                        if isinstance(inner, ast.Name):
                            revise_args.add(inner.id)
    assert revise_args, "the revise call site moved"
    assert reachable & revise_args, (
        "the revise no longer consumes the deduped list — dedup would then be running "
        "beside the budget rather than before it")


def test_the_merge_point_contract_holds_on_a_realistic_detector_mix():
    """What the seam actually hands on, exercised through the real function: a duplicate
    claim from two detectors collapses to ONE slot carrying the specific type and the
    highest severity, a genuinely different finding keeps its own slot, and no internal
    key rides along."""
    merged = na._v3g_dedup_violations([
        _v("outline_missing_beat", 3, "beat never lands", severity="low",
           beat="final_choice"),
        _v("unresolved_thread", 3, "thread left open", severity="critical",
           beat="final_choice"),
        _v("unresolved_thread", 3, "a different thread", thread="recording"),
    ])

    assert len(merged) == 2, "the duplicate claim must consume ONE slot"
    assert merged[0]["type"] == "outline_missing_beat"
    assert merged[0]["severity"] == "critical", "the merge keeps the highest severity"
    assert merged[1]["type"] == "unresolved_thread"
    for violation in merged:
        for key in violation:
            assert not str(key).startswith("_"), f"internal key {key!r} reached revise"
        assert "claim_key" not in violation


# ══════════════════════════════════════════════════════════════════════════
# ROUND 2 — the four contracts that failed on PRODUCTION shape
# ══════════════════════════════════════════════════════════════════════════

def _result_with_canon():
    """The shape `_apply_v3_gates` actually sees: the ACCEPTED Canon Lite under
    `_canon_lite_canon` (popped only later in the job), never the advisory sidecar."""
    return {"canonical_facts": "", "_narrative_authority": {"text": ""},
            "canon_registry": {"events": []},          # advisory sidecar, no name table
            "_canon_lite_canon": CANON}


def test_ownership_reads_the_accepted_canon_not_the_advisory_sidecar():
    """🔴 THE WRONG OBJECT. `canon_registry` is an advisory sidecar with no
    canonical_name/aliases table at all, so pointing ownership at it made the whole
    "canon names, aliases and bound literals are owned" claim untrue at the production
    call site — while the synthetic unit tests, which passed the registry directly,
    stayed green."""
    result = _result_with_canon()
    assert na._ledger_hit_is_exempt_for_result("Soo", "topik", result,
                                               category="name") is True
    assert na._ledger_hit_is_exempt_for_result("30 days", "topik", result,
                                               category="duration") is True
    assert na._ledger_hit_is_exempt_for_result("Eun-soo", "topik", result,
                                               category="name") is True


def test_the_ledger_category_survives_to_the_ownership_decision():
    """🔴 "MATCH CATEGORY AND VALUE" WAS ONLY "MATCH VALUE". Both call sites split
    `name:Tae-jun` down to `Tae-jun` before deciding, so a value owned in one category
    could exempt the same value reported under another."""
    result = _result_with_canon()
    assert na._ledger_hit_is_exempt_for_result("Soo", "topik", result,
                                               category="name") is True
    assert na._ledger_hit_is_exempt_for_result("Soo", "topik", result,
                                               category="number") is False, (
        "a name must not exempt a number that happens to read the same")
    assert nc.authority_owns_term("30 days", _ownership(topic="", outline="", bible=""),
                                  category="name") is False

    # 🔴 WITH THE OUTLINE PRESENT — the earlier version of this test emptied it, so it
    # only ever proved the REGISTRY path. Free text carries no categories at all, so a
    # numeric category answered by a non-numeric value must be refused BEFORE the text
    # fallback runs, or an outline sentence owns `number:Soo` just by saying "Soo".
    prose = nc.build_authority_ownership(
        topic="", outline="Soo accepts the offer di lantai 11", bible="")
    assert nc.authority_owns_term("Soo", prose, category="name") is True
    assert nc.authority_owns_term("Soo", prose, category="number") is False
    assert nc.authority_owns_term("11", prose, category="number") is True


def test_the_registry_category_overrules_prose_that_merely_says_the_word():
    """🔴 THE FREE-TEXT CATEGORY LEAK. `ownership["texts"]` is topic/outline/bible —
    prose with no categories at all — so the text fallback can only prove a VALUE
    appears, never the SENSE it appears in. An outline saying "Soo accepts the offer"
    therefore owned `food:Soo`, `surname:Soo` and `place:Soo` exactly as readily as
    `name:Soo`, and each of those is a real lane-repetition tic that stopped reaching
    repair. The registry IS categorised, so it is what answers: it records Soo as a
    `name`, and prose cannot overrule that."""
    own = _ownership(topic="", bible="", outline="Soo accepts the offer di lantai 11")
    assert nc.authority_owns_term("Soo", own, category="name") is True
    for wrong in ("food", "surname", "place", "organisation"):
        assert nc.authority_owns_term("Soo", own, category=wrong) is False, wrong


def test_the_registry_overrules_a_validator_that_would_otherwise_have_passed():
    """🔴 WHERE THE CONFLICT RULE IS THE ONLY THING LEFT STANDING. The typed validators
    answer "could this value be this category at all"; they cannot answer "is it this
    category HERE". `30 days` passes the numeric test perfectly well, and an outline
    that mentions it would then own `number:30 days` — while the registry has already
    recorded it as a `duration`. The registry is categorised, so it decides.

    The earlier witness for this rule used `food`/`place`/`surname`, which the validator
    table refuses on its own — so the mutation that deleted this rule SURVIVED against
    it. A rule needs a test only it can fail."""
    own = _ownership(topic="", bible="", outline="Sidang berjalan 30 days penuh.")
    assert nc._f5_value_is_numeric("30 days") is True, "the validator would pass it"
    assert nc.authority_owns_term("30 days", own, category="duration") is True
    assert nc.authority_owns_term("30 days", own, category="number") is False, (
        "the registry records this value as a duration; prose cannot re-file it")


def test_a_typed_validator_separates_the_two_cases_a_blanket_rule_could_not():
    """🔴 THE FALSE NEGATIVE THIS MUST NOT REINTRODUCE, AND THE LEAK IT MUST STILL CLOSE.
    `date`/`duration` once went into a blanket numeric rule, and an outline that
    legitimately says "Tuesday" stopped owning `date:Tuesday` — a properly established
    term sent to repair. The blanket rule could not tell the two apart. A typed
    validator can: "Tuesday" IS a date and is not a span."""
    own = _ownership(topic="", bible="", outline="Sidang digelar hari Tuesday.")
    assert nc.authority_owns_term("Tuesday", own, category="date") is True
    assert nc.authority_owns_term("Tuesday", own, category="duration") is False, (
        "a weekday is not a duration; only the blanket rule had to choose one answer "
        "for both categories")

    span = _ownership(topic="", bible="", outline="Sidang berjalan 30 hari penuh.")
    assert nc.authority_owns_term("30 hari", span, category="duration") is True


def test_prose_cannot_prove_a_category_that_has_no_validator():
    """🔴 THE RESIDUAL, NOW CLOSED. With no registry there is nothing categorised to
    contradict prose — and an earlier version of this suite concluded from that that
    prose should win, pinning `food:Soo is True` as acceptable. It is not: a bare
    occurrence proves the VALUE appears and says nothing about the sense, so a category
    with no validator is UNPROVEN and unproven never exempts. Silence is refusal."""
    prose = nc.build_authority_ownership(
        topic="", outline="Soo accepts the offer", bible="")
    assert nc.authority_owns_term("Soo", prose, category="name") is True, (
        "a capitalised standalone token is checkable as a proper name")
    for unprovable in ("food", "place", "organisation", "object"):
        assert nc.authority_owns_term("Soo", prose, category=unprovable) is False, unprovable


def test_the_premise_supplied_surname_survives_because_it_is_checkable():
    """🔴 THE ROUND-4 LESSON, KEPT AS A TEST. `Han` out of the user's own premise
    "Han Seo-jin" is not a lane tic and must never be re-rolled away. What makes it a
    surname is that it sits inside a multi-token proper name — checkable, so checked.
    `Soo` followed by a lowercase verb is not, which is the `surname:Soo` leak."""
    premise = nc.build_authority_ownership(
        topic="Han Seo-jin kembali ke Seoul", outline="", bible="")
    assert nc.authority_owns_term("Han", premise, category="surname") is True

    prose = nc.build_authority_ownership(
        topic="", outline="Soo accepts the offer", bible="")
    assert nc.authority_owns_term("Soo", prose, category="surname") is False


def test_an_uncategorised_hit_still_decides_on_the_deterministic_matcher_alone():
    """The typed gate must not change the bare-term path: there is no category to
    prove, so there is nothing for it to refuse."""
    prose = nc.build_authority_ownership(
        topic="", outline="Soo accepts the offer di lantai 11", bible="")
    assert nc.authority_owns_term("Soo", prose) is True
    assert nc.authority_owns_term("11", prose) is True
    assert nc.authority_owns_term("Hanjin", prose) is False


def test_a_registered_value_still_matches_its_own_category_exactly():
    """The conflict rule must not shadow the registry's own positive match."""
    own = _ownership(topic="", outline="", bible="")
    assert nc.authority_owns_term("30 days", own, category="duration") is True
    assert nc.authority_owns_term("30 days", own, category="address") is False
    assert nc.authority_owns_term("Hanul 11F", own, category="address") is True


def test_the_bible_pin_filter_is_category_aware_and_returns_bare_values():
    """The pin-time path used to split `category:value` before deciding, so the
    category never reached ownership at all. It now travels intact and only the bare
    value is handed on to enforcement."""
    import orchestrator.static as st

    kept = st._f5_unowned_ledger_terms(
        ["name:Soo", "number:Soo", "number:11", "name:Hanjin"],
        topic="", outline="Soo accepts the offer di lantai 11")

    assert kept == ["Soo", "Hanjin"], (
        "`name:Soo` and `number:11` are owned; `number:Soo` and `name:Hanjin` are not")
    assert all(":" not in k for k in kept), "enforcement quotes bare values"


def test_the_manuscript_ledger_call_site_forwards_the_category():
    """The ownership side is proved behaviourally above; this pins the WIRING, which no
    direct call can reach — the manuscript ledger loop sits deep inside the gate."""
    import inspect

    source = inspect.getsource(na._apply_v3_gates)
    call = source[source.index("_ledger_hit_is_exempt_for_result("):][:220]
    assert "category=_mcat" in call, (
        "the ledger category is dropped on the way to the ownership decision")
    assert 'category=""' not in call


def test_an_exact_duplicate_also_keeps_the_highest_severity():
    """🔴 THE FALLBACK PATH SKIPPED THE MERGE. Without a structured claim identity the
    dedup falls back to exact evidence — and that branch used to `continue`, keeping the
    FIRST occurrence's severity. A `low` arriving before a `critical` silently
    downgraded the claim, which is exactly what the severity correction forbids."""
    for first, second in (("low", "critical"), ("critical", "low")):
        out = na._v3g_dedup_violations([
            _v("timeline", 2, "jam melompat", severity=first),
            _v("timeline", 2, "jam melompat", severity=second),
        ])
        assert len(out) == 1
        assert out[0]["severity"] == "critical", (first, second)


def test_a_cross_type_merge_carries_evidence_and_fix_from_the_severe_occurrence():
    """🔴 A SEVERITY THAT NO LONGER DESCRIBES ITS OWN TEXT. Lifting only the number left
    `critical` attached to the `low` finding's wording — so the repair prompt and any
    reader saw the mild description under the severe label. Type and routing stay with
    the authority-specific finding; severity, evidence and fix travel together."""
    out = na._v3g_dedup_violations([
        _v("outline_missing_beat", 3, "wording ringan", severity="low",
           fix="fix-ringan", beat="final_choice"),
        _v("unresolved_thread", 3, "wording berat", severity="critical",
           fix="fix-berat", beat="final_choice"),
    ])

    assert len(out) == 1
    assert out[0]["type"] == "outline_missing_beat", "routing stays authority-specific"
    assert out[0]["severity"] == "critical"
    assert out[0]["evidence"] == "wording berat"
    assert out[0]["fix"] == "fix-berat"
    assert out[0]["chapter"] == 3
