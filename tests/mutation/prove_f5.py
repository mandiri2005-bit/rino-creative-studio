#!/usr/bin/env python3
"""Mutation proof for the F5 authority-protection and semantic-dedup controls.

    python3 tests/mutation/prove_f5.py              # prove the controls
    python3 tests/mutation/prove_f5.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Hardening inherited from prove_f1_round2 / prove_f2 / prove_f3 / prove_f4a / prove_f4b:
only pytest exit 0 and exit 1 are verdicts; exit 2/3/4/5, a collection error, or an
exit 1 with no "N failed" summary is INCONCLUSIVE rather than a kill; the baseline node
must be GREEN before mutating; the source is restored byte-identically in a `finally`;
replacements go through a CALLABLE so `re.sub` cannot process escapes in them; every
pattern must match EXACTLY ONCE.
"""
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _harness import IsolationError, isolated_run, restore_guard  # noqa: E402

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
NA = WT / "python/narration_api.py"
ST = WT / "python/orchestrator/static.py"
NC = WT / "python/narasi_counters.py"
T = "tests/python/test_narasi_f5_authority_dedup.py"
#: Round 3 — the controls that only exist on the shape production actually emits, and
#: the pin-time branch reached the way production reaches it.
TP = "tests/python/test_narasi_f5_production_claim_identity.py"
TB = "tests/python/test_narasi_f5_bible_pin_enforcement.py"

BREAKS = [
    # ── authority ownership ────────────────────────────────────────────────
    ("1. the accepted OUTLINE stops protecting its terms at Bible-pin",
     [(ST, r'(?m)^    ownership = _build\(topic=str\(topic or ""\), outline=str\(outline or ""\), bible=""\)$',
       '    ownership = _build(topic=str(topic or ""), outline="", bible="")')],
     T, "test_the_bible_pin_filter_keeps_only_unowned_terms"),

    ("2. the post-generation exemption stops reading the private authority packet",
     [(NA, r"(?m)^        outline=_narrative_authority_text\(result if isinstance\(result, dict\) else \{\}\),$",
       '        outline="",')],
     T, "test_the_post_generation_exemption_reads_the_full_pinned_authority"),

    ("3. EVERY ledger hit becomes exempt — the ledger is switched off",
     [(NC, r"(?m)^    text = str\(term or \"\"\)\.strip\(\)\n"
           r"    if not text or not isinstance\(ownership, dict\):\n"
           r"        return False$",
       '    text = str(term or "").strip()\n'
       "    if True:\n"
       "        return True")],
     T, "test_an_unowned_term_still_reaches_repair"),

    ("3b. ownership matches by free substring again — near-aliases become exempt",
     [(NC, r"(?m)^    if text\.lower\(\) in registered:$",
       "    if any(text.lower() in one for one in registered):")],
     T, "test_an_unregistered_near_alias_is_not_owned"),

    ("3c. the pin-time filter is handed the candidate Bible as authority",
     [(ST, r"(?m)^                                    _terms = _f5_unowned_ledger_terms\(\n"
           r"                                        _terms_all, topic=topic or \"\",\n"
           r"                                        outline=_f5outline\)\[:12\]$",
       "                                    _terms = _f5_unowned_ledger_terms(\n"
       "                                        _terms_all, topic=topic or \"\",\n"
       "                                        outline=_f5outline + _bible)[:12]")],
     T, "test_the_bible_pin_filter_cannot_be_handed_the_candidate_bible"),

    # ── semantic dedup ─────────────────────────────────────────────────────
    ("4. dedup reverts to normalized EVIDENCE only",
     [(NA, r"(?m)^        if claim is not None and claim in claim_slot:$", "        if False:")],
     T, "test_the_same_claim_worded_differently_collapses_to_one_target"),

    ("5. the chapter leaves the semantic key — claims collapse across chapters",
     [(NA, r"(?m)^                return \(family, chapter, key if family != .authority_beat. else .claim.,$",
       '                return (family, 0, key if family != "authority_beat" else "claim",')],
     T, "test_the_same_claim_in_different_chapters_stays_two_findings"),

    ("6. an unknown chapter stops being conservative and starts collapsing",
     [(NA, r"(?m)^    chapter = _v3g_violation_chapter\(violation\)\n"
           r"    if chapter is None:\n"
           r"        return None$",
       "    chapter = _v3g_violation_chapter(violation)\n"
       "    if chapter is None:\n"
       "        chapter = -1")],
     T, "test_an_unknown_chapter_never_collapses_two_findings"),

    ("7. the merge point stops deduping at all",
     [(NA, r"(?m)^    _v3g_merged = _v3g_dedup_violations\($", "    _v3g_merged = list(")],
     T, "test_dedup_runs_at_the_merge_point_before_the_revise_is_called"),

    # ── the specificity rule ───────────────────────────────────────────────
    ("8. the winner is picked blindly by arrival order",
     [(NA, r"(?m)^    if a_specific != b_specific:\n"
           r"        return \(a, b\) if a_specific else \(b, a\)$",
       "    if False:\n"
       "        return (a, b)")],
     T, "test_the_specificity_rule_holds_in_both_input_orders"),

    ("9. the specificity priority is INVERTED — the generic type wins",
     [(NA, r"(?m)^        return \(a, b\) if a_specific else \(b, a\)$",
       "        return (b, a) if a_specific else (a, b)")],
     T, "test_the_specificity_rule_holds_in_both_input_orders"),

    # ── severity merge (Rino's correction) ─────────────────────────────────
    # 🔴 THE FIRST FORM OF THIS MUTANT COULD NOT DIE, AND THAT WAS THE FINDING.
    #    It was pointed at a SAME-TYPE pair, where `_v3g_prefer` already orders by
    #    severity — so the merge's lift was redundant there and its removal changed
    #    nothing. The two mechanisms only diverge when the TYPES differ: the specific
    #    type must lead even at `low`, and only the merge can then carry the `critical`
    #    across. Both halves now have their own mutant.
    ("10. the merge stops lifting severity — a specific-but-low survivor keeps `low`",
     [(NA, r"(?m)^    if rank\.get\(other_sev, 0\) > rank\.get\(win_sev, 0\):$", "    if False:")],
     T, "test_the_merge_point_contract_holds_on_a_realistic_detector_mix"),

    # Mutant 10b DELETED with the duplicate it targeted: `_v3g_prefer` no longer
    # orders by severity, because `_v3g_merge_claim` already decides it. Mutant 10
    # proves that single door.

    # ── leakage ────────────────────────────────────────────────────────────
    ("11. the internal claim key rides out on the survivor",
     [(NA, r"(?m)^        merged = dict\(winner\)$",
       "        merged = dict(winner)\n"
       "        merged[\"claim_key\"] = str(_v3g_claim_identity(winner))")],
     T, "test_no_internal_claim_metadata_reaches_the_survivor"),

    ("12. dedup mutates the caller's violation dicts in place",
     [(NA, r"(?m)^        merged = dict\(other\)$", "        merged = other")],
     T, "test_dedup_never_mutates_its_inputs"),

    # ── round 2: the contracts that failed on PRODUCTION shape ────────────
    ("13. ownership reads the advisory sidecar again instead of the accepted canon",
     [(NA, r'(?m)^        canon=\(result or \{\}\)\.get\("_canon_lite_canon"\) if isinstance\(result, dict\) else None,$',
       '        canon=(result or {}).get("canon_registry") if isinstance(result, dict) else None,')],
     T, "test_ownership_reads_the_accepted_canon_not_the_advisory_sidecar"),

    ("14. the ledger category is dropped before the ownership decision",
     [(NA, r"(?m)^                            if _ledger_hit_is_exempt_for_result\(_mterm, _mtopic, result,\n"
           r"                                                                category=_mcat\):$",
       "                            if _ledger_hit_is_exempt_for_result(_mterm, _mtopic, result,\n"
       "                                                                category=\"\"):")],
     T, "test_the_manuscript_ledger_call_site_forwards_the_category"),

    ("14b. ownership ignores the category it was given",
     [(NC, r"(?m)^    if category and by_category:$", "    if False:")],
     T, "test_the_ledger_category_survives_to_the_ownership_decision"),

    ("15. the exact-evidence fallback drops the duplicate instead of merging it",
     [(NA, r"(?m)^            index = seen\[key\]\n"
           r"            winner, loser = _v3g_prefer\(deduped\[index\], violation\)\n"
           r"            deduped\[index\] = _v3g_merge_claim\(winner, loser\)\n"
           r"            continue$",
       "            continue")],
     T, "test_an_exact_duplicate_also_keeps_the_highest_severity"),

    ("16. a cross-type merge lifts only the severity NUMBER, not the text it describes",
     [(NA, r"(?m)^        merged = dict\(other\)\n"
           r"        merged\[.type.\] = winner\.get\(.type.\)$",
       "        merged = dict(winner)\n"
       "        merged[\"severity\"] = other.get(\"severity\")")],
     T, "test_a_cross_type_merge_carries_evidence_and_fix_from_the_severe_occurrence"),

    ("17. the Bible-pin path splits the category off before ownership again",
     [(ST, r'(?m)^                                    _terms_all = sorted\(\{str\(h\.get\("term", ""\)\)$',
       '                                    _terms_all = sorted({str(h.get("term", "")).split(":", 1)[-1]')],
     T, "test_the_bible_pin_filter_cannot_be_handed_the_candidate_bible"),

    ("17b. the pin-time filter stops passing the category to ownership",
     [(ST, r"(?m)^        if not _owns\(value, ownership, category=category\):$",
       '        if not _owns(value, ownership, category=""):')],
     T, "test_the_bible_pin_filter_is_category_aware_and_returns_bare_values"),

    # 🔴 RETARGETED WITH THE RULE IT GUARDS. This used to point at a blanket
    #    `_F5_NUMERIC_CATEGORIES` shape check. That check could not separate
    #    `date:Tuesday` (owned) from `duration:Tuesday` (not) and would have been a
    #    SECOND mechanism beside the typed validator table, so it was deleted rather
    #    than kept. The mutant now points at the one gate that remains.
    ("18. occurrence in prose is proof of category again — any category is exempt",
     [(NC, r"(?m)^        if not _f5_free_text_may_prove\(text, category, authority_text\):\n"
           r"            continue$",
       "        if False:\n            continue")],
     T, "test_the_ledger_category_survives_to_the_ownership_decision"),

    ("18b. an unprovable category is treated as proven — `food:Soo` is exempt again",
     [(NC, r"(?m)^    if validator is None:\n        return False$",
       "    if validator is None:\n        return True")],
     T, "test_prose_cannot_prove_a_category_that_has_no_validator"),

    # ── round 3: the sidecar that makes the semantic path exist in PRODUCTION ──
    # Every mutant below is scored against a test that runs the REAL detectors or the
    # REAL pin-time branch. A control provable only against a hand-built dict is what
    # let the semantic path sit dead in production while its own suite stayed green.
    ("19. the register gate stops publishing its server-owned claim",
     [(NA, r'(?m)^                                        "_f5_claim": _f5_claim_token\("register_move", m\),$',
       '                                        "_f5_claim": "",')],
     TP, "test_the_register_gate_publishes_a_server_owned_claim_for_each_finding"),

    ("20. the thread tracker stops publishing any server-owned claim",
     [(NA, r"(?m)^                            \"_f5_claim\": \(_f5_outline_beat_claim\(_u, _outline_packets\)\n"
           r"                                          or _f5_claim_token\(\"thread\", _t\.get\(\"id\"\)\)\),$",
       '                            "_f5_claim": "",')],
     TP, "test_the_thread_tracker_publishes_the_server_rewritten_thread_id"),

    ("21. identity ignores the server token and demands a chapter again",
     [(NA, r"(?m)^    if isinstance\(server_claim, str\) and server_claim\.strip\(\):$",
       "    if False:")],
     TP, "test_every_real_finding_now_resolves_to_a_semantic_identity"),

    ("22. the beat is vouched for by SOME chapter's outline, not its own",
     [(NA, r'(?m)^    total = _f5_outline_beat_count\(outline_packets\.get\(str\(chapter\)\) or ""\)$',
       '    total = _f5_outline_beat_count(next(iter(outline_packets.values()), ""))')],
     TP, "test_the_beat_reference_needs_the_packet_for_that_very_chapter"),

    ("23. the beat ordinal stops being bounded by the outline's length",
     [(NA, r"(?m)^    if not total or beat > total:$", "    if not total:")],
     TP, "test_the_beat_reference_is_admitted_only_within_the_accepted_outline[3-]"),

    # ── round 4: the SHARED outline reference that makes the pair one target ──
    ("23b. the critic stops publishing its outlined-beat reference",
     [(NA, r"(?m)^                        _cv_claim = _f5_outline_beat_claim\(_cv_one, _outline_packets\)$",
       '                        _cv_claim = ""')],
     TP, "test_both_detectors_reach_the_merge_point_with_the_same_outline_beat_token"),

    # 🔴 THE ORDER IS THE CONTRACT. Both branches produce a valid token, so a harness
    #    that only checked "a token is present" would score this SURVIVED. The thread id
    #    is an identity no other detector can ever produce, so preferring it silently
    #    un-merges the pair while every single-detector test stays green.
    ("23c. the tracker prefers its own thread id over the shared outlined beat",
     [(NA, r"(?m)^                            \"_f5_claim\": \(_f5_outline_beat_claim\(_u, _outline_packets\)\n"
           r"                                          or _f5_claim_token\(\"thread\", _t\.get\(\"id\"\)\)\),$",
       '                            "_f5_claim": (_f5_claim_token("thread", _t.get("id"))\n'
       '                                          or _f5_outline_beat_claim(_u, _outline_packets)),')],
     TP, "test_the_final_choice_pair_collapses_to_one_repair_target"),

    ("23d. the tracker is no longer shown the outline it must reference",
     [(NA, r"(?m)^                            \+ _tt_outline_brief$",
       '                            + ""')],
     TP, "test_the_tracker_is_shown_the_accepted_outline_beats_to_reference"),

    # 🔴 TWO EXITS, TWO MUTANTS. Both strips call ONE helper, so a single mutant on the
    #    helper would kill both tests at once and prove neither exit is independently
    #    wired. Each mutant below bypasses the helper at exactly one call site.
    ("24. the merge point stops stripping — the sidecar rides out to the revise",
     [(NA, r"(?m)^    return _f5_strip_server_keys\(deduped\)$",
       "    return list(deduped)")],
     TP, "test_the_sidecar_never_reaches_the_revise_payload"),

    ("24b. the thread-tracker REPORT stops stripping — the sidecar rides out in `result`",
     [(NA, r'(?m)^                                                "violations": _f5_strip_server_keys\(_tt_violations\)\}$',
       '                                                "violations": _tt_violations}')],
     TP, "test_the_sidecar_never_reaches_the_published_thread_tracker_report"),

    # ── round 3: category+value ownership over FREE authority text ──
    # 🔴 REWITNESSED. This pointed at `test_the_registry_category_overrules_prose…`,
    #    whose categories (`food`/`place`/`surname`/`organisation`) the typed validator
    #    table already refuses on its own — so deleting the conflict rule changed
    #    nothing there and the mutant SURVIVED. The rule's own case is a value whose
    #    validator PASSES while the registry has filed it elsewhere.
    ("25. the registry's category no longer overrules a passing validator",
     [(NC, r"(?m)^    if _f5_category_conflict\(lowered, category, by_category\):$",
       "    if False:")],
     T, "test_the_registry_overrules_a_validator_that_would_otherwise_have_passed"),

    # 🔴 MUTANT 26 WAS DELETED WITH THE DUPLICATE IT TARGETED. It removed the "if the
    #    value IS this category, no conflict" guard inside `_f5_category_conflict` — and
    #    SURVIVED, because `authority_owns_term` returns True on the exact-category match
    #    long before that guard can run. The guard was a second mechanism for a rule the
    #    early return already owned; deleting it leaves one door, and the mutant below
    #    points at that door instead.
    ("26. the exact-category match stops being the door — an owned value falls through",
     [(NC, r"(?m)^    if text\.lower\(\) in registered:\n        return True$",
       "    if False:\n        return True")],
     T, "test_a_registered_value_still_matches_its_own_category_exactly"),

    # ── round 3: the pin-time branch, in-run ──
    # 🔴 ONLY THE IN-RUN TEST CATCHES THIS ONE. The filter still runs and still returns
    # the right answer; the call site simply ignores it and quotes the unfiltered list
    # back to the re-roll. Every direct unit test of `_f5_unowned_ledger_terms` stays
    # green, because the defect is in what production DOES with the result.
    ("27. the re-roll is told to avoid every flagged term, owned ones included",
     [(ST, r'(?m)^                                                extra_negative=", "\.join\(_terms\)\)$',
       '                                                extra_negative=", ".join(_terms_all))')],
     TB, "test_the_reroll_quotes_back_only_the_unowned_term"),
]


def _pytest(test_file, test_name):
    with isolated_run() as env:
        return subprocess.run(
            [sys.executable, "-m", "pytest", f"{test_file}::{test_name}",
             "-q", "-p", "no:cacheprovider", "--no-header"],
            cwd=WT, capture_output=True, text=True, env=env)


def _classify(proc):
    """What pytest ACTUALLY said. Returns ("pass"|"fail", None) or (None, reason)."""
    out = proc.stdout + proc.stderr
    if proc.returncode == 5 or "no tests ran" in out:
        return None, "no tests collected (missing file or wrong node id?)"
    if proc.returncode == 4:
        return None, "pytest usage error"
    if proc.returncode in (2, 3):
        return None, f"pytest aborted (exit {proc.returncode})"
    if "error" in out.lower() and re.search(r"\b\d+ errors?\b", out):
        return None, "collection/setup error, not a test failure"
    if proc.returncode == 1:
        if not re.search(r"\b\d+ failed\b", out):
            return None, "exit 1 with no 'N failed' summary"
        return "fail", None
    if proc.returncode == 0:
        if not re.search(r"\b\d+ passed\b", out):
            return None, "exit 0 with no 'N passed' summary"
        return "pass", None
    return None, f"unrecognized pytest exit {proc.returncode}"


def run(label, edits, test_file, test_name):
    verdict, why = _classify(_pytest(test_file, test_name))
    if verdict != "pass":
        print(f"  !! BASELINE NOT GREEN ({why or verdict}) — {label}")
        return False

    targets = list(dict.fromkeys(path for path, _p, _r in edits))
    for path in targets:
        if not path.exists():
            print(f"  !! SOURCE MISSING {path} — {label}")
            return False

    # 🔴 A SAME-LENGTH REPLACEMENT IS INVISIBLE TO CPYTHON'S CACHE KEY — `(a, b) if x
    #    else (b, a)` → `(b, a) if x else (a, b)` changes neither size nor, inside one
    #    mtime tick, the timestamp. Mutant 9 was observed SURVIVING here while never
    #    executing. The defence is no longer a local `_drop_bytecode` belt-and-braces:
    #    `_pytest` gives every subprocess its OWN empty cache prefix, so no run can
    #    read what another compiled, and `restore_guard` puts bytes, mode and mtime_ns
    #    back and verifies them. ONE mechanism, in one place, testable on its own.
    with restore_guard(*targets):
        for path, pattern, repl in edits:
            src = path.read_text(encoding="utf-8")
            mutated, n = re.subn(pattern, lambda _m, _r=repl: _r, src)
            if n != 1:
                print(f"  !! PATTERN MATCHED {n}x (need exactly 1) — {label}")
                return False
            path.write_text(mutated, encoding="utf-8")

        verdict, why = _classify(_pytest(test_file, test_name))
        if verdict is None:
            print(f"  !! INCONCLUSIVE ({why}) — {label}")
            return False
        killed = (verdict == "fail")
        print(f"  {'KILLED ' if killed else 'SURVIVED'} — {label}")
        return killed


def selftest():
    print("Self-test: the harness must REFUSE these three broken configurations\n")
    good = [(NA, r"(?m)^    if rank\.get\(other_sev, 0\) > rank\.get\(win_sev, 0\):$",
             "    if False:")]
    cases = [
        ("missing test file",
         ("10. first severity wins", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_the_survivor_keeps_the_highest_severity_not_the_first[low-critical-critical]")),
        ("wrong node id",
         ("10. first severity wins", good, T, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(NA, r"(?m)^    _THIS_SYMBOL_WAS_RENAMED = 1$", "    pass")],
          T, "test_the_survivor_keeps_the_highest_severity_not_the_first[low-critical-critical]")),
    ]
    ok = True
    for name, args in cases:
        refused = not run(*args)
        print(f"  {'OK      ' if refused else 'BROKEN  '} — {name} "
              f"{'refused' if refused else 'was SCORED AS A KILL'}")
        ok = ok and refused
    print(f"\nself-test: {'PASS — the harness can fail' if ok else 'FAIL — this harness proves nothing'}")
    return ok


if __name__ == "__main__":
    try:
        if "--selftest" in sys.argv:
            sys.exit(0 if selftest() else 2)
        print("Mutation proof: F5 authority protection + semantic dedup\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
