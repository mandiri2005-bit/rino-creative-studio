#!/usr/bin/env python3
"""Mutation proof for the F8 chapter-seam contract.

    python3 tests/mutation/prove_f8.py              # prove the controls
    python3 tests/mutation/prove_f8.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Isolation is delegated to `_harness`: every subprocess gets its own empty bytecode cache
prefix, and sources are restored through `restore_guard` (bytes + mode + mtime_ns, verified).
This file owns NO second cache-invalidation mechanism.

Verdict rules inherited from the F1-F7 harnesses: only pytest exit 0 and exit 1 are verdicts;
exit 2/3/4/5, a collection error, or an exit 1 with no "N failed" summary is INCONCLUSIVE
rather than a kill; the baseline node must be GREEN before mutating; replacements go through a
CALLABLE so `re.sub` cannot process escapes in them; every pattern must match EXACTLY ONCE.

🔴 EVERY MUTANT HERE RE-OPENS A FAIL-OPEN PATH THAT WAS REPRODUCED FOR REAL. The first version
   of `narasi_f8.py` shipped six of them and an audit found every one. They share a shape:
   something the CALLER supplied was treated as authority. Each mutant below hands that
   authority back and is judged by a witness that can only fail because of THAT rule.

🔴 ONE RULE, ONE MUTANT, ONE WITNESS. Two mutants against one line, or one mutant pointed at
   several test names, is the "two mechanisms for one rule" failure this workstream documented
   repeatedly — each hides the other's removal and neither is then proven.
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
F8 = WT / "python/narasi_f8.py"
NA = WT / "python/narration_api.py"
LZ = WT / "python/laozhang_api.py"
TB = "tests/python/test_narasi_f8_seam_contract.py"
TD = "tests/python/test_narasi_f8_delivery_path.py"
TC = "tests/python/test_narasi_f8_combined_closed_loop.py"
TS = "tests/python/test_narasi_f8_structural_lane.py"
TR = "tests/python/test_narasi_f8_real_lanes.py"

BREAKS = [
    # ── census framing: the server's own chapter count ────────────────────
    ("1. an invalid chapter count reads as `not applicable` instead of UNPROVED",
     [(F8, r'(?m)^    if chapter_count < 1:\n        return _refused\("invalid_chapter_count"\)$',
       '    if chapter_count < 1:\n        return {"valid": True, "applicable": False,\n'
       '                "reason": NOT_APPLICABLE, "rows": [], "missing": {},\n'
       '                "missing_dims": frozenset(), "seams": [], "chapter_count": 0}')],
     TB, "test_an_invalid_chapter_count_is_unproved_not_not_applicable[0]"),

    ("2. the chapter bound is removed — a runaway book is measured",
     [(F8, r'(?m)^    if chapter_count > MAX_CHAPTERS:\n        return _refused\("too_many_seams"\)$',
       "    if False:\n        return _refused(\"too_many_seams\")")],
     TB, "test_a_book_over_the_chapter_bound_is_unproved_and_reachable"),

    # ── census validation ─────────────────────────────────────────────────
    ("3. the census length check is removed — a partial census indexes the wrong seams",
     [(F8, r'(?m)^    if len\(rows\) != expected:\n        return _refused\("wrong_length"\)$',
       "    if False:\n        return _refused(\"wrong_length\")")],
     TB, "test_a_census_that_cannot_be_indexed_is_unproved[rows4-wrong_length]"),

    ("4. adjacency is no longer checked — a tidy census may describe another book",
     [(F8, r'(?m)^        if first != index or second != index \+ 1:\n            return _refused\("not_adjacent"\)$',
       "        if False:\n            return _refused(\"not_adjacent\")")],
     TB, "test_a_census_that_cannot_be_indexed_is_unproved[rows7-not_adjacent]"),

    ("5. `True` is accepted as a chapter number (bool is an int in Python)",
     [(F8, r"(?m)^    return \(isinstance\(value, int\) and not isinstance\(value, bool\)\n"
           r"            and 1 <= value <= MAX_CHAPTERS\)$",
       "    return (isinstance(value, int)\n            and 1 <= value <= MAX_CHAPTERS)")],
     TB, "test_a_census_that_cannot_be_indexed_is_unproved[rows10-invalid_chapter]"),

    ("6. an unknown dimension status is accepted into the closed vocabulary",
     [(F8, r'(?m)^            if not isinstance\(status, str\) or status not in STATUSES:\n'
           r'                return _refused\("invalid_status"\)$',
       "            if False:\n                return _refused(\"invalid_status\")")],
     TB, "test_a_census_that_cannot_be_indexed_is_unproved[rows13-invalid_status]"),

    ("7. a row missing a dimension is accepted as complete",
     [(F8, r'(?m)^            if dimension not in row:\n                return _refused\("missing_dimension"\)$',
       "            if False:\n                return _refused(\"missing_dimension\")")],
     TB, "test_a_census_that_cannot_be_indexed_is_unproved[rows15-missing_dimension]"),

    ("8. `missing` stops being recorded — every seam reads sound",
     [(F8, r'(?m)^            if status == "missing":$', "            if False:")],
     TB, "test_detection_targets_the_later_chapter_and_names_the_seam"),

    # ── detection ─────────────────────────────────────────────────────────
    ("9. the violation targets the EARLIER chapter — the finished chapter is rewritten",
     [(F8, r'(?m)^            "chapter": second,$', '            "chapter": first,')],
     TB, "test_detection_targets_the_later_chapter_and_names_the_seam"),

    ("10. one violation per DIMENSION instead of per seam — one repair budgeted three times",
     [(F8, r"(?m)^        found\.append\(\{$",
       "        found.extend({} for _ in gone[1:])\n        found.append({")],
     TB, "test_one_violation_per_seam_not_one_per_dimension"),

    # ── the unresolved universe ───────────────────────────────────────────
    # 🔴 THE ROUND-1 P1. Deriving unresolved from the caller's target list let a run that
    #    routed nothing report a clean book while the defect sat in the before-census.
    ("11. the unresolved universe comes from the target list, not the detected set",
     [(F8, r"(?m)^    detected = set\(before_missing\)$",
       "    detected = {e['seam'] for e in canonical}")],
     TB, "test_a_detected_seam_with_no_target_still_blocks_delivery"),

    ("12. an invalid before-census no longer refuses",
     [(F8, r"(?m)^    if not before\.get\(\"valid\"\):$", "    if False:")],
     TB, "test_an_unproved_census_detects_nothing_but_must_not_read_as_clean"),

    ("13. an omitted after-census reads as a clean book",
     [(F8, r"(?m)^    if not after\.get\(\"valid\"\):$", "    if False:")],
     TB, "test_an_omitted_after_census_cannot_resolve_anything"),

    # ── resolution requires all six conditions ────────────────────────────
    ("14. a dimension still missing after repair no longer blocks resolution",
     [(F8, r"(?m)^        if after_missing\.get\(identity\):$", "        if False:")],
     TB, "test_a_repair_that_swaps_one_missing_dimension_for_another_is_not_resolved"),

    ("15. the addressed-patch attribution requirement is dropped",
     [(F8, r"(?m)^        if identity not in attributed:$", "        if False:")],
     TB, "test_a_changed_chapter_without_structural_attribution_is_not_a_seam_repair"),

    ("16. the changed-byte requirement is dropped — the observation moves, the manuscript does not",
     [(F8, r'(?m)^        if entry\["chapter_b"\] not in changed:$', "        if False:")],
     TB, "test_a_seam_whose_chapter_never_changed_is_not_resolved_even_when_attributed"),

    ("17. collateral no longer invalidates the run's resolutions",
     [(F8, r"(?m)^        if collateral:\n            continue                                  # the whole run is untrustworthy$",
       "        if False:\n            continue")],
     TB, "test_a_chapter_that_changed_without_authorisation_is_collateral"),

    # ── dimension sensitivity ─────────────────────────────────────────────
    # 🔴 THE ROUND-1 P1 SUBSTITUTION. Seam-level set arithmetic cannot see `causal` closing
    #    while `location` opens: the seam is in the broken set both times.
    ("18. new defects are computed seam-level, so a dimension substitution is invisible",
     [(F8, r"(?m)^    new_defects = sorted\(frozenset\(after\.get\(\"missing_dims\"\) or \(\)\)\n"
           r"                         - frozenset\(before\.get\(\"missing_dims\"\) or \(\)\)\)$",
       "    new_defects = sorted(set(after_missing) - set(before_missing))")],
     TB, "test_a_newly_missing_dimension_on_an_already_broken_seam_is_a_new_defect"),

    ("19. a newly broken seam is not treated as a defect at all",
     [(F8, r"(?m)^    collateral = sorted\(changed - allowed\)$",
       "    new_defects = []\n    collateral = sorted(changed - allowed)")],
     TB, "test_a_clean_seam_that_breaks_during_repair_is_a_new_defect"),

    # ── target and chapter-set authority ──────────────────────────────────
    ("20. a caller-supplied seam identity is trusted over the recomputed one",
     [(F8, r'(?m)^        if entry\.get\("seam"\) is not None and entry\.get\("seam"\) != identity:\n            return None$',
       "        if False:\n            return None")],
     TB, "test_a_caller_supplied_identity_that_contradicts_its_chapter_pair_is_unproved"),

    ("21. duplicate routing targets are accepted",
     [(F8, r"(?m)^        if identity in seen:\n            return None$",
       "        if False:\n            return None")],
     TB, "test_duplicate_targets_are_unproved"),

    ("22. a non-adjacent or out-of-book target is accepted",
     [(F8, r"(?m)^        if second != first \+ 1 or second > chapter_count:\n            return None$",
       "        if False:\n            return None")],
     TB, "test_a_malformed_target_row_is_unproved_not_ignored[bad1]"),

    # 🔴 SILENT FILTERING IS HOW AN UNAUTHORISED EDIT BECOMES INVISIBLE.
    ("23. an invalid member of a chapter set is silently filtered instead of refused",
     [(F8, r"(?m)^        if not _is_chapter\(value\) or value > chapter_count:\n            return None$",
       "        if not _is_chapter(value) or value > chapter_count:\n            continue")],
     TB, "test_an_invalid_member_of_either_chapter_set_is_unproved_not_filtered[2]"),

    # ── accounting ────────────────────────────────────────────────────────
    ("24. the accounting reason echoes whatever the caller handed it",
     [(F8, r'(?m)^    reason = reason if reason in VERDICT_REASONS else "unknown"$',
       '    reason = str(reason)[:64]')],
     TB, "test_the_reason_comes_from_a_closed_vocabulary_never_caller_prose"),

    ("25. the accounting invariants stop being enforced",
     [(F8, r"(?m)^    contradicted = any\(contradictions\)$", "    contradicted = False")],
     TB, "test_an_accounting_contradiction_blocks_delivery[kw0-resolved <= byte_changing]"),

    ("26. an unproved census no longer forces a block",
     [(F8, r"(?m)^    blocked = bool\(verdict\.get\(\"blocked\"\)\) or contradicted or not census_valid \\$",
       "    blocked = bool(verdict.get(\"blocked\")) or contradicted \\")],
     TB, "test_an_unproved_census_blocks_even_when_the_verdict_claims_otherwise"),

    ("27. collateral no longer forces zero resolutions in the accounting",
     [(F8, r"(?m)^        bool\(collateral\) and bool\(resolved\),$", "        False,")],
     TB, "test_a_verdict_claiming_both_collateral_and_resolutions_is_a_contradiction"),

    ("28. the detected = resolved + unresolved invariant is dropped",
     [(F8, r"(?m)^        applicable and census_valid and n_detected != len\(resolved\) \+ len\(unresolved\),$",
       "        False,")],
     TB, "test_counts_that_do_not_add_up_are_a_contradiction"),

    ("29. persisted id lists stop being bounded",
     [(F8, r"(?m)^        return sorted\(values\)\[:MAX_PERSISTED_IDS\]$",
       "        return sorted(values)")],
     TB, "test_persisted_id_lists_are_capped_by_a_book_that_exceeds_the_cap"),

    # ══ PRODUCTION CALL SITES ═════════════════════════════════════════════
    # 🔴 A PURE-MODULE PROOF DOES NOT PROVE WIRING. `29/29` over `narasi_f8.py` alone left
    #    the prompt, the dedup, the early return, the attribution, the counters, the hard
    #    block and the payload entirely unwitnessed — every one a real round-2 finding.

    ('30. the actuator directive is emptied — the provider gets `-> FIX:` and nothing else',
     [(F8, '(?m)^            "fix": _directive\\(first, second, gone\\),$',
       '            "fix": "",')],
     TB, 'test_a_violation_carries_a_directive_the_actuator_can_act_on'),

    ('31. the locator is emptied, so evidence can never match the manuscript',
     [(F8, '(?m)^            "evidence": _evidence\\(openings\\.get\\(second\\), second\\),$',
       '            "evidence": "",')],
     TB, 'test_a_violation_carries_a_directive_the_actuator_can_act_on'),

    ('32. the directive stops naming which dimensions are missing',
     [(F8, '(?m)^    wanted = \\[d for d in DIMENSIONS if d in set\\(gone or \\(\\)\\)\\]$',
       '    wanted = []')],
     TB, 'test_the_directive_names_only_the_dimensions_that_are_missing'),

    ('33. production stops handing the openings to detection',
     [(NA, '(?m)^            _f8_detected = _nf8\\.detect\\(_f8_before, openings=_f8_openings\\)$',
       '            _f8_detected = _nf8.detect(_f8_before)')],
     TD, 'test_the_actuator_receives_a_directive_naming_the_missing_dimensions'),

    ('34. dedup goes back to deleting every free-text finding, disagreement included',
     [(NA, '(?m)^                    _f8_conflicts\\.append\\(f"free_text_ch\\{_vc\\}"\\)$',
       '                    pass')],
     TD, 'test_a_free_text_finding_that_contradicts_the_census_blocks'),

    ('35. a canonical duplicate is routed twice, budgeting one repair as two',
     [(NA, '(?m)^                        continue                      # canonical duplicate — one budget item$',
       '                        _f8_kept.append(_v)\n                        continue')],
     TD, 'test_a_free_text_boundary_finding_and_the_census_are_one_target'),

    ('36. the post-scan is skipped whenever detection found nothing',
     [(NA, '(?m)^        if not \\(before\\.get\\("missing"\\) or \\{\\}\\) and not changed and not structural_break \\\\$',
       '        if not (before.get("missing") or {}) and not structural_break \\')],
     TD, 'test_a_seam_broken_by_another_gates_repair_is_caught_even_when_none_was_detected'),

    ('37. attribution falls back to chapter level, so any accepted op resolves the seam',
     [(NA, '(?m)^        attribution = \\{str\\(_i\\) for _i in \\(accepted_ids or \\(\\)\\) if isinstance\\(_i, str\\)\\}$',
       "        attribution = {'seam:%d|%d' % (_t['chapter_a'], _t['chapter_b'])\n                       for _t in targeted if _t.get('chapter_b') in\n                       {int(_c) for _c in (accepted or ()) if isinstance(_c, int)}}")],
     TD, 'test_attribution_is_per_seam_not_per_chapter'),

    ('38. the counters go back to being assumed from the target list',
     [(NA, '(?m)^        n_attempts = int\\(counts\\.get\\("attempted"\\) or 0\\)\\n        n_calls = int\\(counts\\.get\\("provider_calls"\\) or 0\\)$',
       '        n_attempts = len(targeted)\n        n_calls = 1 if targeted else 0')],
     TD, 'test_the_counters_come_from_the_structural_lane_not_from_the_target_list'),

    ('39. the structural lane stops recording WHICH seam it accepted',
     [(LZ, '(?m)^                    accepted_violation_ids\\.add\\(_aid\\[:64\\]\\)$',
       '                    pass')],
     TS, 'test_the_lane_records_the_seam_it_accepted_not_merely_the_chapter'),

    ('40. the F8 hard block no longer refuses delivery',
     [(NA, '(?m)^    if isinstance\\(_f8_out, dict\\) and _f8_out\\.get\\("delivery_blocked"\\):$',
       '    if False:')],
     TD, 'test_a_seam_that_is_still_missing_after_repair_blocks_delivery[still0-seam:1|2]'),

    ('41. the accounting never reaches the durable payload',
     [(NA, '(?m)^        payload\\["f8"\\] = dict\\(_f8\\)$',
       '        pass')],
     TD, 'test_both_v9_seams_are_detected_targeted_and_resolved'),

    ('42. the two gates stop sharing one merged revise',
     [(NA, '(?m)^                _v3g_merged = _v3g_merged \\+ _f8_detected$',
       '                _v3g_merged = list(_f8_detected)')],
     TC, 'test_the_two_gates_share_one_bounded_post_repair_read'),


    ('43. attribution ignores WHICH address changed — any accepted op in the chapter counts',
     [(LZ, '(?m)^        if _changed_positions & _F8_SEAM_OPENING_UNITS:$',
       '        if True:')],
     TS, 'test_attribution_follows_the_edited_address_not_the_chapter[u003-False]'),

    ('44. the authorised opening range widens to the whole chapter',
     [(LZ, '(?m)^_F8_SEAM_OPENING_UNITS = frozenset\\(\\{1\\}\\)$',
       '_F8_SEAM_OPENING_UNITS = frozenset(range(1, 200))')],
     TS, 'test_attribution_follows_the_edited_address_not_the_chapter[u003-False]'),

    ("45. F8's counters fall back to the lane-wide totals",
     [(LZ, '(?m)^                "targeted": len\\(patch_stats\\.get\\("f8_targeted_ids"\\) or \\(\\)\\),$',
       '                "targeted": int(patch_stats.get("targeted") or 0),')],
     TR, 'test_published_f8_counters_exclude_an_unrelated_structural_chapter'),

    ('46. F8 physical exchanges are taken from the lane-wide ledger',
     [(LZ, '(?m)^                "provider_calls": int\\(patch_stats\\.get\\("f8_provider_calls"\\) or 0\\),$',
       '                "provider_calls": int(patch_stats.get("provider_calls") or 0),')],
     TR, 'test_published_f8_counters_exclude_an_unrelated_structural_chapter'),

    ('47. a census/free-text disagreement is reported as a malformed row again',
     [(NA, '(?m)^            verdict = \\{\\*\\*clean, \\"blocked\\": True, \\"reason\\": _nf8\\.BOUNDARY_CONFLICT,$',
       '            verdict = {**clean, "blocked": True, "reason": "before_not_a_row",')],
     TD, 'test_the_conflict_reason_is_truthful_not_a_malformed_row'),

    # ── the two wiring rules the mandatory combined acceptance exposed ────
    # Both were found by BUILDING the nine-defect fixture, not by reading the code: the
    # legacy lane logged "UNMAPPED evidence heads" and repaired nothing, and the L3 seam
    # logged "repaired chapters could not be written back" and delivered nothing.
    ("48. F6's tense finding loses the locator the legacy lane routes on",
     [(NA, '(?m)^                     f"rest of the book is \\{tense\\[\'majority\'\\]\\} @ch\\{_ch\\}"\\),$',
       '                     f"rest of the book is {tense[\'majority\']}"),')],
     TC, 'test_a_routed_tense_finding_carries_the_locator_the_legacy_lane_reads'),

    ("49. F6's teleport finding loses the locator the legacy lane routes on",
     [(NA, '(?m)^                             f"transition on the page \\(occurrence \\{_tk\\} of \\{_tn\\}\\) @ch\\{_ch\\}"\\),$',
       '                             f"transition on the page (occurrence {_tk} of {_tn})"),')],
     TC, 'test_a_routed_teleport_finding_carries_the_locator_the_legacy_lane_reads'),

    ('50. the chapter records are never brought back onto the repaired book',
     [(NA, '(?m)^    _rs_n = _resync_chapter_records\\(result\\)$', '    _rs_n = 0')],
     TC, 'test_the_chapter_records_are_re_derived_from_the_repaired_book'),

    ('51. the re-sync stops checking that the records are a census over the blocks',
     [(NA, '(?m)^        if set\\(by_index\\) != set\\(range\\(len\\(blocks\\)\\)\\):\\n            return 0$',
       '        if False:\n            return 0')],
     TC, 'test_the_re_sync_refuses_records_that_are_not_a_census'),

    ('52. the re-sync writes an empty body, and stops being all-or-nothing',
     [(NA, '(?m)^            if not _content:\\n'
           '                return 0                      # an empty body is not a re-derivation$',
       '            if False:\n                return 0')],
     TC, 'test_the_re_sync_never_writes_an_empty_body'),

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
    good = [(F8, r"(?m)^    detected = set\(before_missing\)$",
             "    detected = {e['seam'] for e in canonical}")]
    cases = [
        ("missing test file",
         ("11. unresolved universe", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_a_detected_seam_with_no_target_still_blocks_delivery")),
        ("wrong node id",
         ("11. unresolved universe", good, TB, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(F8, r"(?m)^_THIS_SYMBOL_WAS_RENAMED = 1$", "    pass")],
          TB, "test_a_detected_seam_with_no_target_still_blocks_delivery")),
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
        print("Mutation proof: F8 chapter-seam contract\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
