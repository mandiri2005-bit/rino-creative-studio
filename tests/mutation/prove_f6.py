#!/usr/bin/env python3
"""Mutation proof for the F6 controls.

    python3 tests/mutation/prove_f6.py              # prove the controls
    python3 tests/mutation/prove_f6.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Isolation is delegated to `_harness`: every subprocess gets its own empty bytecode cache
prefix, and sources are restored through `restore_guard` (bytes + mode + mtime_ns,
verified). This file owns NO second cache-invalidation mechanism.

Hardening inherited from the F1-F5 harnesses: only pytest exit 0 and exit 1 are verdicts;
exit 2/3/4/5, a collection error, or an exit 1 with no "N failed" summary is INCONCLUSIVE
rather than a kill; the baseline node must be GREEN before mutating; replacements go
through a CALLABLE so `re.sub` cannot process escapes in them; every pattern must match
EXACTLY ONCE.
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
NG = WT / "python/narasi_gate.py"
F6 = WT / "python/narasi_f6.py"
#: 🔴 The audit's finding: the first version of this harness never touched `laozhang_api.py`,
#: so the critic prompt and the census carry-through had NO witness at all.
LZ = WT / "python/laozhang_api.py"
TF = "tests/python/test_narasi_f6_chapter_framing.py"
TC = "tests/python/test_narasi_f6_tense_census.py"
TA = "tests/python/test_narasi_f6_resolution_accounting.py"
TCC = "tests/python/test_narasi_f6_critic_census_contract.py"
NA = WT / "python/narration_api.py"
TCL = "tests/python/test_narasi_f6_closed_loop.py"
TDB = "tests/python/test_narasi_f6_delivery_block.py"
#: The four classes added after `tense_drift`. `TG` drives the REAL job body with one book
#: carrying all five defects, so a mutant pointed at it is judged on what the job DELIVERED —
#: which is the standard this workstream kept failing to meet with per-line witnesses.
TG = "tests/python/test_narasi_f6_golden_five.py"
TCLS = "tests/python/test_narasi_f6_classes.py"
TORD = "tests/python/test_narasi_f6_ordering_behavioral.py"
#: The FINAL SCAN suite: every row drives the real job and asks what it DELIVERED.
TFS = "tests/python/test_narasi_f6_final_scan.py"

BREAKS = [
    # ── framing: the root cause ────────────────────────────────────────────
    # 🔴 THE ORIGINAL DEFECT, RE-INTRODUCED. Without the hash allowance the pattern
    #    matches nothing on a real assembled book and every scanner sees ONE chapter.
    ("1. the splitter stops recognising the `##` heading production actually emits",
     [(NG, r"(?m)^    rf\"\(\?im\)\^\\s\*#\{\{0,3\}\}\\s\*\{_CHAPTER_HEADING_CORE\}\\b\[\^\\n\]\*\$\"$",
       '    rf"(?im)^\\s*{_CHAPTER_HEADING_CORE}\\b[^\\n]*$"')],
     TF, "test_the_gate_splitter_finds_every_chapter_in_the_production_shape"),

    ("2. the block splitter becomes a generic markdown separator",
     [(NG, r"(?m)^    rf\"\(\?im\)\^\[\^\\S\\n\]\*#\{\{0,3\}\}\[\^\\S\\n\]\*\{_CHAPTER_HEADING_CORE\}\\b\[\^\\n\]\*\$\"$",
       '    r"(?im)^[^\\S\\n]*#{2,3}[^\\S\\n]*\\S[^\\n]*$"')],
     TF, "test_block_splitting_uses_the_same_chapter_grammar_as_the_scanner"),

    # 🔴 ONE VOCABULARY. If the two patterns drift apart, the chapter the scanner counts
    #    is not the chapter the repair edits.
    #    The first form of this mutant merely INSERTED an unused constant — dead code that
    #    changed no behaviour — and SURVIVED, as it should have. A mutant has to make the
    #    two splitters actually disagree, and the witness has to look somewhere they can:
    #    a block splitter narrowed to `Chapter` agrees on an English book and diverges on
    #    every other language, so the test compares them across the whole keyword set.
    ("3. the block splitter narrows to English while the scanner stays multilingual",
     [(NG, r"(?m)^    rf\"\(\?im\)\^\[\^\\S\\n\]\*#\{\{0,3\}\}\[\^\\S\\n\]\*\{_CHAPTER_HEADING_CORE\}\\b\[\^\\n\]\*\$\"$",
       '    r"(?im)^[^\\S\\n]*#{0,3}[^\\S\\n]*(?:Chapter)[^\\S\\n]+[0-9]{1,4}\\b[^\\n]*$"')],
     TF, "test_block_splitting_uses_the_same_chapter_grammar_as_the_scanner"),

    # ── framing: byte preservation ─────────────────────────────────────────
    ("4. the preamble before the first heading is discarded",
     [(NG, r"(?m)^    if starts\[0\] > 0:\n        blocks\.append\(text\[:starts\[0\]\]\)          # preamble, kept rather than discarded$",
       "    if False:\n        blocks.append(text[:starts[0]])")],
     TF, "test_a_preamble_before_the_first_heading_is_kept_not_discarded"),

    # 🔴 THE SEPARATOR MUST NOT MIGRATE. With `\s*` the match can begin on the blank
    #    line BEFORE the heading, so the server-owned separator leaves the earlier block
    #    and a byte comparison of an untouched chapter fails for the wrong reason.
    ("5. a block may begin on the blank line before its heading",
     [(NG, r"(?m)^    rf\"\(\?im\)\^\[\^\\S\\n\]\*#\{\{0,3\}\}\[\^\\S\\n\]\*\{_CHAPTER_HEADING_CORE\}\\b\[\^\\n\]\*\$\"$",
       '    rf"(?im)^\\s*#{{0,3}}\\s*{_CHAPTER_HEADING_CORE}\\b[^\\n]*$"')],
     TF, "test_the_separator_between_chapters_stays_with_the_earlier_block"),

    ("6. the last chapter is dropped from the block list",
     [(NG, r"(?m)^    bounds = starts \+ \[len\(text\)\]$", "    bounds = starts + [starts[-1]]")],
     TF, "test_chapter_blocks_rejoin_to_the_original_byte_for_byte"),

    ("7. the heading reader stops returning the heading verbatim",
     [(NG, r"(?m)^    return match\.group\(0\) if match else \"\"$",
       '    return match.group(0).strip().lstrip("#").strip() if match else ""')],
     TF, "test_each_block_begins_at_its_own_heading_and_keeps_it_verbatim"),

    # ── tense census: the server's own arithmetic ──────────────────────────
    ("8. an outlier is reported without a STRICT majority",
     [(NG, r"(?m)^    majority = next\(\(v for v, n in counts\.items\(\) if n \* 2 > len\(per_chapter\)\), None\)$",
       "    majority = next((v for v, n in counts.items() if n * 2 >= len(per_chapter)), None)")],
     TC, "test_an_exact_half_is_not_a_majority"),

    ("9. an unrecognised tense label is read as `not the majority` instead of refused",
     [(NG, r"(?m)^        if normalized not in _TENSE_VALUES:\n            return \{\*\*empty, \"reason\": \"invalid_value\"\}$",
       "        if False:\n            return {**empty, \"reason\": \"invalid_value\"}")],
     TC, "test_a_value_outside_the_closed_vocabulary_invalidates_the_census"),

    ("10. the census no longer has to cover every chapter — indices stop meaning chapters",
     [(NG, r"(?m)^    if chapter_count is not None and len\(per_chapter\) != int\(chapter_count\):$",
       "    if False:")],
     TC, "test_a_census_that_does_not_cover_every_chapter_is_refused"),

    ("11. the census is unbounded — a runaway model answer is accepted",
     [(NG, r"(?m)^    if len\(tense_by_chapter\) > _TENSE_CENSUS_MAX_CHAPTERS:$", "    if False:")],
     TC, "test_the_census_is_bounded_so_a_runaway_list_cannot_be_used"),

    # ── accounting: the partition ──────────────────────────────────────────
    # 🔴 IDENTITY MUST SURVIVE THE REWRITE IT IS MEASURING. Fold evidence into the id and
    #    the same defect reads as a NEW violation after repair, so the original leaves the
    #    books instead of being marked unresolved.
    ("12. violation identity folds in evidence and stops surviving the repair",
     [(F6, r"(?m)^        return f\"\{vclass\}:\{chapter\}\"$",
       '        return f"{vclass}:{chapter}:{violation.get(\'evidence\')}"')],
     TA, "test_identity_is_stable_across_the_repair_that_rewrote_the_chapter"),

    # 🔴 AUDIT FINDING: `class + chapter` merged two DIFFERENT teleports in chapter 2 into
    #    one `teleport:2`; `detected` fell to 1, the second defect left the books, and
    #    resolving the first allowed delivery.
    # 🔴 THE FIRST FORM OF 12b POINTED AT A TEST WHOSE TELEPORTS BOTH CARRY CLAIMS, so the
    #    mutation could not reach it and it SURVIVED. The claim requirement is only
    #    observable where a claim is ABSENT.
    # 🔴 THE REPLACEMENT MUST NAME A SYMBOL THAT EXISTS. This first read `{chapter_part}`, a
    #    name left over from an earlier revision of the function — the mutant died with a
    #    NameError, so it was scored KILLED without the witness ever proving anything about
    #    semantic identity. A mutant that cannot run is the same empty evidence as a mutant
    #    that cannot be reached.
    ("12b. a multi-instance class stops needing its server-owned claim",
     [(F6, r"(?m)^    if not claim:\n        return None$",
       "    if not claim:\n        return f\"{vclass}:{chapter}\"")],
     TA, "test_a_multi_instance_violation_without_a_server_claim_has_no_identity"),

    # 🔴 ROUND-2 AUDIT: the same violation changed identity when a claim appeared
    #    (`tense_drift:2` → `tense_drift:2:same`), so detection and verification could be
    #    about two "different" violations.
    ("12d. a single-instance identity starts reading a claim it must ignore",
     [(F6, r"(?m)^    if vclass in _F6_SINGLE_INSTANCE_CLASSES:\n        return f\"\{vclass\}:\{chapter\}\"$",
       "    if vclass in _F6_SINGLE_INSTANCE_CLASSES:\n"
       "        _c = str(violation.get(\"f6_claim\") or \"\").strip().lower()\n"
       "        return f\"{vclass}:{chapter}\" if not _c else f\"{vclass}:{chapter}:{_c}\"")],
     TA, "test_a_single_instance_identity_ignores_any_claim_that_appears"),

    # 🔴 ROUND-2 AUDIT: `tense_drift:0` existed, was "repaired" via a change set containing 0,
    #    and opened delivery.
    ("12e. a chapter that is not a 1-based integer is accepted as identifiable",
     [(F6, r"(?m)^    chapter = violation\.get\(\"chapter\"\)\n"
           r"    if isinstance\(chapter, bool\) or not isinstance\(chapter, int\) or chapter < 1:\n"
           r"        return None$",
       "    chapter = violation.get(\"chapter\")\n"
       "    if False:\n        return None")],
     TA, "test_a_chapter_that_is_not_a_1_based_integer_is_unidentifiable[0]"),

    # 🔴 THE FIRST FORM POINTED AT `_unidentifiable()`, WHICH HAD BECOME DEAD CODE — the
    #    orphan count moved into `resolution_accounting` and nothing called the helper any
    #    more, so mutating it changed nothing and it SURVIVED. The helper is deleted; this
    #    points at the line that actually counts them.
    ("12c. an unidentifiable violation vanishes instead of counting as unresolved",
     [(F6, r"(?m)^    orphans = len\(detected_list\) - len\(usable\)$", "    orphans = 0")],
     TA, "test_two_unidentifiable_violations_are_still_two"),

    # 🔴 THE FIRST FORM OF 13 WAS INERT: the `isinstance` guard short-circuits on a missing
    #    verdict, so the line it mutated was never reached. A mutant that cannot execute
    #    proves nothing, which is exactly why it SURVIVED.
    ("13. a missing verdict is assumed resolved",
     [(F6, r"(?m)^        if isinstance\(verdicts\.get\(i\), str\)\n"
           r"        and verdicts\.get\(i\)\.strip\(\)\.lower\(\) == _RESOLVED$",
       "        if str(verdicts.get(i) or _RESOLVED).strip().lower() == _RESOLVED")],
     TA, "test_a_missing_verdict_is_unresolved_never_assumed_resolved"),

    # 🔴 AUDIT FINDING: a verdict string alone resolved a violation with zero repair
    #    attempts, zero provider calls and zero chapters changed.
    ("13b. a resolution stops being corroborated by the chapter actually changing",
     [(F6, r"(?m)^        and chapter_by_id\.get\(i\) in changed$", "        and True")],
     # 🔴 REPOINTED. The old witness reports ZERO repair attempts, and the resolution gate now
     #    refuses those on its own — so it masked this mutant entirely. This row runs a real
     #    attempt that changed nothing, where the change-set check is the only thing left.
     TA, "test_a_repair_that_ran_and_changed_nothing_resolves_nothing"),

    ("13c. a resolution accepts ANY changed chapter, not its own",
     [(F6, r"(?m)^        and chapter_by_id\.get\(i\) in changed$", "        and bool(changed)")],
     TA, "test_a_resolution_needs_ITS_OWN_chapter_to_have_changed"),

    # 🔴 ROUND-2 AUDIT: membership in a caller-supplied set was the WHOLE corroboration, so
    #    zero attempts + zero calls + a claimed changed chapter still resolved and shipped.
    ("13d. a chapter may be vouched for with no repair attempt behind it",
     [(F6, r"(?m)^    resolved_ids = \[\] if \(collateral or int\(repair_attempts\) < 1\) else \[$",
       "    resolved_ids = [] if collateral else [")],
     TA, "test_a_chapter_that_changed_without_a_repair_resolves_nothing"),

    ("13e. a repair attempt may happen with no provider call behind it",
     [(F6, r"(?m)^    if int\(repair_attempts\) >= 1 and int\(provider_calls\) < 1:$", "    if False:")],
     TA, "test_a_repair_attempt_cannot_happen_without_a_provider_call"),

    # 🔴 ROUND-2 AUDIT: the change set has to be OBSERVED from bytes, not asserted.
    ("13f. the change set stops comparing bytes and calls every chapter changed",
     [(F6, r"(?m)^        if before != after:\n            changed\.add\(index\)$",
       "        if True:\n            changed.add(index)")],
     TA, "test_changed_chapters_comes_from_a_server_owned_byte_comparison"),

    ("13g. a structural break is reported as an ordinary chapter edit",
     [(F6, r"(?m)^    if len\(before_blocks\) != len\(after_blocks\):$", "    if False:")],
     TA, "test_a_block_count_change_is_not_silently_reported_as_a_chapter_edit"),

    # 🔴 ROUND-3 AUDIT: a targeted repair that rewrote chapters nobody asked for still
    #    resolved and shipped, breaking untouched-chapter byte identity.
    ("13h. a repair may rewrite chapters nobody targeted and still resolve",
     [(F6, r"(?m)^    resolved_ids = \[\] if \(collateral or int\(repair_attempts\) < 1\) else \[$",
       "    resolved_ids = [] if int(repair_attempts) < 1 else [")],
     TA, "test_a_repair_that_rewrote_chapters_nobody_targeted_resolves_nothing"),

    ("13i. counters are published without any domain check",
     [(F6, r"(?m)^        if isinstance\(value, bool\) or not isinstance\(value, int\) or value < 0:$",
       "        if False:")],
     TA, "test_counters_must_be_non_negative_integers[-7]"),

    # 🔴 ROUND-3 AUDIT: the preamble was numbered as a chapter, so editing Chapter 2 of a book
    #    with a preamble reported `{3}` — every repair and verdict off by one.
    ("13j. the preamble is numbered as a chapter again",
     [(F6, r"(?m)^            return blocks\[0\], blocks\[1:\]$", '            return "", blocks')],
     TA, "test_a_preamble_does_not_shift_the_chapter_numbering"),

    # 🔴 ROUND-4 AUDIT: `## Chapter 2` → `## Bab 99` compared as an ordinary block edit, so a
    #    destroyed heading read as a repair of chapter 2 and opened delivery.
    ("13l. a rewritten heading is treated as an ordinary chapter edit",
     [(F6, r"(?m)^        if _ngate\.chapter_heading_line\(before\) != _ngate\.chapter_heading_line\(after\):\n"
           r"            return None$",
       "        if False:\n            return None")],
     TA, "test_a_rewritten_heading_is_a_structural_failure_not_a_chapter_edit"),

    # 🔴 ROUND-4 AUDIT: `tense_drift:999` with `changed={999}` resolved a three-chapter book.
    ("13m. a finding beyond the end of the book is accepted as verifiable",
     [(F6, r"(?m)^        return \(violation_identity\(violation\) is not None\n"
           r"                and chapter is not None and chapter <= chapter_count\)$",
       "        return violation_identity(violation) is not None")],
     TA, "test_a_chapter_beyond_the_end_of_the_book_cannot_be_resolved"),

    ("13n. the change set may name chapters the book does not have",
     [(F6, r"(?m)^    beyond = sorted\(c for c in changed if c > chapter_count\)$",
       "    beyond = []")],
     TA, "test_a_change_set_naming_a_chapter_the_book_does_not_have_is_a_broken_instrument"),

    ("13o. the chapter count itself is accepted unvalidated",
     [(F6, r"(?m)^    if isinstance\(chapter_count, bool\) or not isinstance\(chapter_count, int\) or chapter_count < 1:$",
       "    if False:")],
     TA, "test_the_chapter_count_itself_must_be_a_positive_integer[0]"),

    # 🔴 THE TWO SIDES MUST BE TRIMMED INDEPENDENTLY. The gates append the `> **Gaya:** …`
    #    header AFTER the pre-repair snapshot, so a preamble legitimately exists on one side
    #    only; trimming jointly made every real job compare 3 blocks against 4 and refuse.
    ("13k. the two sides stop being trimmed independently",
     [(F6, r"(?m)^    preamble_before, before_blocks = _split_preamble\(before_blocks\)\n"
           r"    preamble_after, after_blocks = _split_preamble\(after_blocks\)$",
       "    preamble_after, after_blocks = _split_preamble(after_blocks)\n"
       "    preamble_before, before_blocks = \"\", list(before_blocks)")],
     TA, "test_a_preamble_appearing_on_only_one_side_does_not_break_the_comparison"),

    # 🔴 "WE DIDN'T GET TO IT" MUST NOT BE A PASS.
    ("14. a detected but untargeted violation quietly leaves the books",
     [(F6, r"(?m)^    unresolved_ids = \[i for i in detected_ids if i not in resolved_ids\]$",
       "    unresolved_ids = [i for i in targeted_ids if i not in resolved_ids]")],
     TA, "test_a_detected_but_never_targeted_violation_is_unresolved_not_forgotten"),

    ("15. the partition invariant stops being checked",
     [(F6, r"(?m)^    if n_resolved \+ n_unresolved != n_detected:$", "    if False:")],
     TA, "test_the_accounting_refuses_to_publish_a_broken_partition"),

    ("16. two sightings of one violation are counted as two",
     [(F6, r"(?m)^        if identity is not None and identity not in seen:$",
       "        if identity is not None:")],
     TA, "test_a_duplicate_detection_is_counted_once"),

    # ── the deterministic verifier ─────────────────────────────────────────
    # 🔴 THE v9 CLAIM ITSELF: "the chapter changed" asserted as "the defect is gone".
    ("17. the tense verifier stops checking the outlier list",
     [(F6, r"(?m)^    if chapter in outliers_after:\n        return False$",
       "    if False:\n        return False")],
     TA, "test_tense_is_resolved_only_when_the_whole_census_is_consistent"),

    # 🔴 AUDIT FINDING: the drift MOVED to another chapter and the books called it fixed.
    ("17b. a NEW outlier created by the repair is ignored",
     [(F6, r"(?m)^    return not \(outliers_after - outliers_before\)$", "    return True")],
     TA, "test_moving_the_drift_to_another_chapter_is_not_a_resolution"),

    # 🔴 AUDIT FINDING: a chapter was DELETED and the shorter census read as cleaner.
    ("17c. the post-repair census no longer has to describe the same book",
     [(F6, r"(?m)^    census = _ngate\.tense_census\(tense_by_chapter_after, chapter_count=chapter_count\)$",
       "    census = _ngate.tense_census(tense_by_chapter_after)")],
     TA, "test_losing_a_chapter_is_not_a_resolution"),

    # 🔴 ROUND-2 AUDIT: `valid=True` from the parser was read as "the book is consistent", so a
    #    two-and-two split with NO dominant tense resolved.
    ("17d. a book with no dominant tense counts as consistent",
     [(F6, r"(?m)^    if not census\.get\(\"majority\"\):\n        return False$",
       "    if False:\n        return False")],
     TA, "test_a_book_with_no_dominant_tense_is_not_a_resolution"),

    ("18. an unreadable post-repair census counts as a resolution",
     [(F6, r"(?m)^    census = _ngate\.tense_census\(tense_by_chapter_after, chapter_count=chapter_count\)\n"
           r"    if not census\.get\(\"valid\"\):\n        return False$",
       "    census = _ngate.tense_census(tense_by_chapter_after, chapter_count=chapter_count)\n"
       "    if not census.get(\"valid\"):\n        return True")],
     TA, "test_a_census_that_cannot_be_read_after_repair_is_not_a_resolution"),

    ("18b. an unreadable PRE-repair census counts as a resolution",
     [(F6, r"(?m)^    before = _ngate\.tense_census\(tense_by_chapter_before, chapter_count=chapter_count\)\n"
           r"    if not before\.get\(\"valid\"\):\n        return False$",
       "    before = _ngate.tense_census(tense_by_chapter_before, chapter_count=chapter_count)\n"
       "    if not before.get(\"valid\"):\n        return True")],
     TA, "test_a_before_census_that_cannot_be_read_vouches_for_nothing"),

    # 🔴 ROUND-3 AUDIT: the before-set was a caller's CLAIM, so faking it made a moved drift
    #    read as pre-existing. It is derived from the before-census now.
    ("18c. a chapter that was never an outlier before can be `resolved`",
     [(F6, r"(?m)^    if chapter not in outliers_before:\n        return False$",
       "    if False:\n        return False")],
     TA, "test_a_target_that_was_not_an_outlier_before_is_not_this_violation"),

    # 🔴 ROUND-3 AUDIT: chapters supposed to be byte-identical had their labels move, and the
    #    verifier vouched anyway.
    ("18d. a chapter nobody changed may silently change its tense label",
     [(F6, r"(?m)^        if index not in changed and labels_before\[index - 1\] != labels_after\[index - 1\]:$",
       "        if False:")],
     TA, "test_relabelling_a_chapter_nobody_touched_is_not_a_resolution"),

    # 🔴 MUTANT 19 WAS DELETED WITH THE DUPLICATE IT TARGETED. It removed a
    #    `chapter < 1 or chapter > chapter_count` guard from the verifier and SURVIVED,
    #    because the guard was unreachable: outliers come from a census validated to hold
    #    exactly `chapter_count` entries, so `chapter not in outliers_before` already refuses
    #    every out-of-range chapter. The mutant below points at that single door instead.
    ("19. the `before` outlier set is read from the AFTER census",
     [(F6, r"(?m)^    outliers_before = set\(before\.get\(\"outliers\"\) or \[\]\)$",
       "    outliers_before = set(census.get(\"outliers\") or [])")],
     TA, "test_tense_is_resolved_only_when_the_whole_census_is_consistent"),

    # ── the critic contract, on the REAL call path ─────────────────────────
    # 🔴 THE WITNESS GAP THE AUDIT FOUND. `laozhang_api.py` could be reverted byte-for-byte
    #    to 9d1e8a1 with all 41 F6 tests green and prove_f6 at 19/19, because the harness
    #    never mutated it. These five do.
    ("20. the critic is no longer asked for a per-chapter tense census",
     [(LZ, r'(?m)^        "6b\. TENSE CENSUS — separately from any finding, report `tense_by_chapter`: ONE entry per "$',
       '        "6b. (removed) "')],
     TCC, "test_the_critic_is_asked_for_a_per_chapter_tense_census"),

    ("21. the JSON contract stops advertising the census field",
     [(LZ, r"(?m)^        '\}\], \"tense_by_chapter\": \[\"past\"\|\"present\"\|\"mixed\", … one per chapter in order, '$",
       "        '}], '")],
     TCC, "test_the_json_contract_advertises_the_field"),

    ("22. the census the model returned is dropped before the verdict",
     [(LZ, r'(?m)^    if "tense_by_chapter" in \(v or \{\}\):$', "    if False:")],
     TCC, "test_a_census_the_model_returns_survives_to_the_verdict"),

    ("23. the census payload is carried through unbounded",
     [(LZ, r'(?m)^        out\["tense_by_chapter"\] = _bound_tense_census_payload\(v\.get\("tense_by_chapter"\)\)$',
       '        out["tense_by_chapter"] = v.get("tense_by_chapter")')],
     TCC, "test_a_runaway_census_is_bounded_before_it_reaches_the_verdict"),

    # 🔴 ROUND-2 AUDIT: the bound handled only lists, so a 20MB string crossed whole into a
    #    persisted payload through the very field the bound exists to contain.
    # 🔴 THE ANCHOR REACHES PAST THE GUARD ON PURPOSE. `if not isinstance(raw, list): return []`
    #    now opens all THREE bounding helpers, so the guard alone matches three times and the
    #    harness refuses it. The two lines after it are unique to the tense bound.
    ("23b. a non-list census is handed back raw instead of replaced",
     [(LZ, r"(?m)^    if not isinstance\(raw, list\):\n        return \[\]\n"
           r"    bounded = \[\]\n    for value in raw\[:_TENSE_CARRY_MAX_ENTRIES\]:\n"
           r'        bounded\.append\(value\[:_TENSE_CARRY_MAX_VALUE\] if isinstance\(value, str\) else ""\)$',
       "    if not isinstance(raw, list):\n        return raw\n"
       "    bounded = []\n    for value in raw[:_TENSE_CARRY_MAX_ENTRIES]:\n"
       '        bounded.append(value[:_TENSE_CARRY_MAX_VALUE] if isinstance(value, str) else "")')],
     TCC, "test_a_non_list_census_is_replaced_not_passed_through[huge_string]"),

    # ── the production closed loop ─────────────────────────────────────────
    ("30. the detected hard violations are never routed to the repair",
     [(NA, r"(?m)^                _v3g_merged = _v3g_merged \+ _f6_routed$",
       "                pass")],
     TCL, "test_the_tense_outlier_is_detected_and_routed_to_the_repair"),

    ("31. the repair attempt and provider call go uncounted",
     [(NA, r"(?m)^                _f6_pending\[\"repair_attempts\"\] \+= 1\n"
           r"                _f6_pending\[\"provider_calls\"\] \+= 1$",
       "                pass")],
     TCL, "test_a_real_repair_resolves_and_delivery_is_allowed"),

    ("32. the bounded verification pass is skipped and nothing is verified",
     [(NA, r"(?m)^        if mutated:$", "        if False:")],
     TCL, "test_a_real_repair_resolves_and_delivery_is_allowed"),

    # 🔴 THE ORDERING THE AUDIT NAMED: the change set must come from the FINAL bytes, not
    #    from the book the gates handed back — the dedup guard still runs between them.
    ("33. the change set is computed against the gates' book, not the delivered one",
     [(NA, r"(?m)^        final_blocks = _ngate\.split_chapter_blocks\(final_text\)$",
       "        final_blocks = list(pending.get(\"blocks_before\") or [])")],
    # The revert case blocks either way — a change set from the gates' book and one from the
    # delivered book both come out empty — so only the collateral case can tell them apart.
     TCL, "test_a_post_gates_edit_to_another_chapter_shows_up_as_collateral"),

    # 🔴 ROUND-5 AUDIT: the finaliser ran two mutators too early — the L3-assist repair and
    #    the F1 scrub both still rewrite the manuscript after the dedup guard. This mutant
    #    moves the call back to where it was.
    ("36. the finaliser is called before the L3-assist repair and the F1 scrub",
     [(NA, r"(?m)^    # F6 verification and accounting do NOT happen here: the L3-assist repair and the F1\n"
           r"    # scrub below both still mutate `result`\. See the finalise call further down, which is\n"
           r"    # the LAST thing before the delivery decision\.$",
       "    _f6_out = await _f6_finalize(result, body, tenant_id=tenant_id, user_id=user_id,\n"
       "                                 job_uuid=job_uuid, sink=sink, job_id=job_id)")],
     TCL, "test_the_finaliser_runs_after_every_manuscript_mutator"),

    # 🔴 ROUND-5 AUDIT: F6 read the legacy critic flag, which defaults to "0" — so on a
    #    default configuration nothing detected and nothing blocked.
    ("37. F6 defaults to OFF again",
     [(NA, r'(?m)^    return str\(os\.environ\.get\("NARASI_F6_ENABLED", "1"\)\)\.strip\(\)\.lower\(\) not in \($',
       '    return str(os.environ.get("NARASI_F6_ENABLED", "0")).strip().lower() not in (')],
     TCL, "test_f6_is_on_unless_it_is_explicitly_switched_off"),

    ("38. F6 stops asking for its own census when the legacy critic did not run",
     [(NA, r"(?m)^            if _f6_cq\.get\(\"tense_by_chapter\"\) is None and _f6_n_ch >= 1:$",
       "            if False:")],
     TCL, "test_f6_obtains_its_own_census_when_the_legacy_critic_never_ran"),

    # 🔴 ROUND-5 AUDIT: an absent or malformed census fell through to `outliers=[]`, which the
    #    accounting read as "nothing detected" and delivered.
    ("39. an unreadable tense census reads as a clean book again",
     [(NA, r"(?m)^    if not tense\.get\(\"valid\"\) or not tense\.get\(\"majority\"\):$",
       "    if False:")],
     TCL, "test_a_missing_census_blocks_instead_of_reading_as_a_clean_book"),

    ("39b. an unreadable teleport census reads as a clean book",
     [(NA, r"(?m)^    if not teleports\.get\(\"valid\"\):$", "    if False:")],
     TG, "test_a_missing_census_blocks_instead_of_reading_as_a_clean_book"),

    ("39c. an unreadable beat census reads as a clean book",
     [(NA, r"(?m)^    if outline_sizes and not beats\.get\(\"valid\"\):$", "    if False:")],
     TG, "test_a_missing_beat_census_blocks"),

    ("40. UNPROVED stops blocking and falls through to the ordinary accounting",
     [(NA, r"(?m)^    unproven = pending\.get\(\"unproven\"\)$", "    unproven = None")],
     TCL, "test_a_malformed_census_blocks_too"),

    ("41. a detection failure is swallowed as non-fatal again",
     [(NA, r'(?m)^                           "provider_calls": 0, "unproven": f"detection_error:\{str\(_f6e\)\[:80\]\}"\}$',
       '                           "provider_calls": 0, "unproven": None}')],
     TCL, "test_a_detection_failure_blocks_rather_than_being_swallowed"),

    # ── the delivery decision itself, observed on the real job path ────────
    # 🔴 UNTIL THESE EXISTED, DELETING THE WHOLE BLOCK TURNED NOTHING RED. The only
    #    witness was a source-order assertion, which is not evidence that the refusal
    #    does anything.
    ("42. the hard block is removed — an unresolved book is delivered",
     [(NA, r"(?m)^    if isinstance\(_f6_out, dict\) and _f6_out\.get\(\"delivery_blocked\"\):$",
       "    if False:")],
     TDB, "test_an_unresolved_violation_finalises_the_job_as_FAILED"),

    ("43. the block no longer returns, so persistence happens anyway",
     [(NA, r"(?m)^        await _p0a_flush\(\"failed\"\)\n        return\n"
           r"    # Private prompt material must not cross the persistence boundary\.[^\n]*$",
       "        await _p0a_flush(\"failed\")\n"
       "    # Private prompt material must not cross the persistence boundary. The bounded")],
     TDB, "test_an_unresolved_violation_never_persists_the_chapters"),

    ("44. the blocked job is not refunded",
     [(NA, r"(?m)^            result=_result_payload\(result\), error=\"f6_unresolved_hard_violation\"\)\n"
           r"        await _refund\(meter_op, tenant_id, job_id\)$",
       "            result=_result_payload(result), error=\"f6_unresolved_hard_violation\")")],
     TDB, "test_an_unresolved_violation_refunds_the_hold"),

    ("45. the blocked job is finalised DONE instead of FAILED",
     [(NA, r"(?m)^            job_id, job_uuid, tenant_id, status=_STATUS_FAILED,\n"
           r"            result=_result_payload\(result\), error=\"f6_unresolved_hard_violation\"\)$",
       "            job_id, job_uuid, tenant_id, status=_STATUS_DONE,\n"
       "            result=_result_payload(result), error=\"f6_unresolved_hard_violation\")")],
     TDB, "test_an_unresolved_violation_finalises_the_job_as_FAILED"),

    ("34. an accounting that could not be computed permits delivery",
     [(NA, r"(?m)^                      \"violations_unresolved\": -1, \"delivery_blocked\": True,$",
       '                      "violations_unresolved": -1, "delivery_blocked": False,')],
     TCL, "test_an_accounting_that_cannot_be_computed_blocks_rather_than_permits"),

    ("35. the private pre-repair state survives into the persisted result",
     [(NA, r"(?m)^    pending = result\.pop\(\"_f6_pending\", None\)$",
       "    pending = result.get(\"_f6_pending\", None)")],
     TCL, "test_the_private_pending_state_never_survives_the_finaliser"),

    ("24. bounding DROPS unusable entries so entry N stops being chapter N",
     [(LZ, r'(?m)^        bounded\.append\(value\[:_TENSE_CARRY_MAX_VALUE\] if isinstance\(value, str\) else ""\)$',
       "        if isinstance(value, str):\n            bounded.append(value[:_TENSE_CARRY_MAX_VALUE])")],
     TCC, "test_bounding_preserves_list_length_so_entry_n_is_still_chapter_n"),

    # ══════════════════════════════════════════════════════════════════════
    # THE FOUR CLASSES ADDED AFTER `tense_drift`
    #
    # 🔴 EVERY ONE OF THESE IS JUDGED ON WHAT THE JOB DELIVERED. `TG` drives the real
    #    `_run_narration_job_after_parity` with one book carrying all five defects, so a
    #    surviving mutant here means a defect reached persistence — not that a line went
    #    unexercised. That is the standard the earlier rounds of this workstream missed.
    # ══════════════════════════════════════════════════════════════════════

    # ── the detectors ──────────────────────────────────────────────────────
    ("50. the teleport detector is removed — the census is read and then ignored",
     [(NA, r"(?m)^    for _ch in \(teleports\.get\(\"offenders\"\) or \[\]\):$",
       "    for _ch in []:")],
     TG, "test_all_five_classes_are_detected_on_one_book"),

    ("51. the beat detector is removed — an unexecuted outline beat is nobody's violation",
     [(NA, r"(?m)^    for \(_bch, _bno\), _bstate in sorted\(\(beats\.get\(\"states\"\) or \{\}\)\.items\(\)\):$",
       "    for (_bch, _bno), _bstate in []:")],
     TG, "test_all_five_classes_are_detected_on_one_book"),

    ("52. the ceiling detector is removed — an over-long chapter is never flagged",
     [(NA, r"(?m)^        if _ch <= len\(counts\) and counts\[_ch - 1\] > _whi:$",
       "        if False:")],
     TG, "test_all_five_classes_are_detected_on_one_book"),

    ("53. the tense detector is removed on the five-defect path too",
     [(NA, r"(?m)^    \} for _ch in \(tense\.get\(\"outliers\"\) or \[\]\)\]$",
       "    } for _ch in []]")],
     TG, "test_all_five_classes_are_detected_on_one_book"),

    # ── the verifiers, each forced to vouch ────────────────────────────────
    # 🔴 THE COUNT MUST REACH ZERO. Two moves becoming one leaves a teleport in the book.
    ("54. the teleport verifier accepts a count that merely fell",
     [(F6, r"(?m)^    if counts_after\[chapter - 1\] != 0:\n        return False$",
       "    if False:\n        return False")],
     TG, "test_one_failed_class_out_of_five_blocks_the_whole_delivery[teleport-kwargs1]"),

    ("54b. the teleport verifier ignores a move pushed into another chapter",
     [(F6, r"(?m)^    return not any\(counts_after\[i\] > counts_before\[i\] for i in range\(chapter_count\)\)$",
       "    return True")],
     # 🔴 THE FIRST WITNESS PUT THE NEW MOVE IN AN UNTOUCHED CHAPTER, where the
     #    byte-identity rule refuses it first — so the mutated line never ran and the
     #    mutant SURVIVED without proving anything. Cause 2 in the handoff's list: a
     #    witness that cannot discriminate, not redundant code. This one puts the extra
     #    move inside the change set, where only this line can catch it.
     TCLS, "test_a_new_teleport_inside_the_change_set_is_not_a_resolution"),

    # 🔴 "I WILL GIVE A DEPOSITION" IS NOT A DEPOSITION.
    ("55. the beat verifier accepts a promise as an execution",
     [(F6, r"(?m)^    if states_a\.get\(key\) != \"executed\":\n        return False$",
       "    if states_a.get(key) not in (\"executed\", \"promised\"):\n        return False")],
     TG, "test_a_promise_is_not_an_execution"),

    ("55b. the beat verifier stops enforcing the outline's ordering chain",
     [(F6, r"(?m)^    if require_order:$", "    if False:")],
     # 🔴 THE GOLDEN ROW REGRESSES BEAT (1,1) TO `absent`, so the no-regression rule blocks
     #    it whether or not ordering is enforced — same verdict, two different reasons, and
     #    the mutant SURVIVED. This witness has nothing executed before and nothing to
     #    regress, so ordering is the only rule left standing.
     TCLS, "test_the_ordering_chain_is_enforced_only_for_beat_execution"),

    ("55c. the beat verifier lets an earlier beat regress to pay for the target",
     [(F6, r"(?m)^        if state == \"executed\" and states_a\.get\(other\) != \"executed\":\n"
           r"            return False$",
       "        if False:\n            return False")],
     TCLS, "test_a_repair_that_dropped_another_beat_resolves_nothing"),

    # 🔴 AN EMPTIED CHAPTER IS COMFORTABLY UNDER ITS CEILING — the floor is the other half of
    #    the same `word_target` contract, and dropping it makes deletion a repair.
    ("56. the ceiling verifier drops the floor, so an emptied chapter passes",
     [(F6, r"(?m)^    if not floor <= after\[chapter - 1\] <= ceiling:\n        return False$",
       "    if not after[chapter - 1] <= ceiling:\n        return False")],
     # 🔴 AN ALL-WHITESPACE CANDIDATE IS STOPPED BY THE EMPTY-CANDIDATE GUARD and never
     #    reaches the floor at all, so the old witness left this line unexecuted and the
     #    mutant SURVIVED. A two-word candidate is a real answer: it gets spliced, and the
     #    floor is the only thing that refuses it.
     TG, "test_a_gutted_reducer_candidate_blocks"),

    ("57. the ceiling verifier accepts an oversized candidate",
     [(F6, r"(?m)^    if not floor <= after\[chapter - 1\] <= ceiling:\n        return False$",
       "    if not floor <= after[chapter - 1]:\n        return False")],
     TG, "test_an_oversized_reducer_candidate_blocks"),

    ("57b. the ceiling verifier lets a reduction push another chapter over its own ceiling",
     [(F6, r"(?m)^        if before\[index - 1\] <= limits\[1\] < after\[index - 1\]:\n"
           r"            return False$",
       "        if False:\n            return False")],
     TCLS, "test_a_reduction_that_pushed_another_chapter_over_resolves_nothing"),

    # 🔴 THE DISPATCH ITSELF. Every verifier above can be correct and still be ignored.
    ("58. the finaliser marks every verdict resolved without asking a verifier",
     [(NA, r"(?m)^            verdicts\[identity\] = \"resolved\" if proved else \"unresolved\"$",
       "            verdicts[identity] = \"resolved\"")],
     TG, "test_one_failed_class_out_of_five_blocks_the_whole_delivery[tense_drift-kwargs0]"),

    # ── the server-owned claims ────────────────────────────────────────────
    # 🔴 AUDIT FINDING #2, IN BOTH ITS FORMS. An identity built from prose does not survive the
    #    rewrite it is measuring; an identity without the server's own discriminator collapses
    #    two violations into one and lets the second leave the books.
    ("59. the beat claim is built from evidence prose instead of the bounded reference",
     [(F6, r"(?m)^    return f\"outline_beat:\{chapter\}\|\{beat\}\"$",
       "    return f\"outline_beat:{source.get('evidence')}\"")],
     # 🔴 THE FIVE-DEFECT BOOK PAIRS `final_beat` WITH `beat_execution`, and the class is
     #    part of the identity — so both claims collapsing to the same token still left two
     #    distinct ids and the mutant SURVIVED. Two violations of the SAME class is the only
     #    shape where a gutted claim actually merges them.
     TG, "test_two_unexecuted_beats_in_one_chapter_are_two_violations"),

    ("60. the teleport claim drops the server's own ordinal and collapses the instances",
     [(NA, r'(?m)^                "f6_claim": f"teleport_instance:\{_ch\}\|\{_tk\}",$',
       '                "f6_claim": "teleport_instance",')],
     TG, "test_two_teleports_in_one_chapter_are_two_violations"),

    ("60b. an unbounded outline reference is admitted as a claim",
     [(F6, r"(?m)^    if isinstance\(total, bool\) or not isinstance\(total, int\) or beat > total:\n"
           r"        return \"\"$",
       "    if False:\n        return \"\"")],
     TCLS, "test_an_unbounded_reference_yields_no_claim[source0]"),

    # ── the bounded reduction ──────────────────────────────────────────────
    ("61. the bounded reduction attempt becomes a retry ladder",
     [(NA, r"(?m)^_F6_CEILING_REDUCTION_ATTEMPTS = 1$",
       "_F6_CEILING_REDUCTION_ATTEMPTS = 2")],
     TG, "test_the_reduction_gets_exactly_one_bounded_attempt"),

    # 🔴 THE HEADING IS THE SERVER'S, NOT THE MODEL'S. Splicing the candidate in without
    #    re-attaching it destroys the block boundary the whole comparison rests on.
    ("62. the ceiling splice stops re-attaching the server-owned heading",
     [(NA, r"(?m)^                    _f6_blocks\[_f6_at\] = _f6_head \+ _f6_pre \+ _f6_cand \+ _f6_post$",
       "                    _f6_blocks[_f6_at] = _f6_pre + _f6_cand + _f6_post")],
     TG, "test_all_five_repaired_persists_and_finalises_DONE"),

    ("62b. the reducer is handed the whole book instead of one chapter",
     [(NA, r"(?m)^                        _f6_body\.strip\(\), target_words=int\(_f6_lim\[1\]\), style=style,$",
       "                        result.get(_f6_rk) or \"\", target_words=int(_f6_lim[1]), style=style,")],
     TG, "test_the_reducer_is_handed_one_chapter_and_the_server_owned_ceiling"),

    ("62c. an empty reducer candidate is spliced in anyway",
     [(NA, r"(?m)^                    if not _f6_cand:\n                        continue$",
       "                    if False:\n                        continue")],
     # 🔴 SPLICING THE EMPTY CANDIDATE IN ALSO BLOCKS — it guts the chapter, which fails the
     #    floor — so "it refused" cannot tell the two apart and the mutant SURVIVED. The
     #    witness now asserts the CHANGE SET: with the guard, chapter 1 is never touched.
     TG, "test_an_empty_reducer_candidate_is_never_spliced_into_the_book"),

    # ── the five-defect accounting ─────────────────────────────────────────
    ("63. the accounting silently drops one of the five detected violations",
     [(NA, r"(?m)^        detected = list\(pending\.get\(\"detected\"\) or \(\)\)$",
       "        detected = list(pending.get(\"detected\") or ())[:-1]")],
     TG, "test_all_five_classes_are_detected_on_one_book"),

    ("63b. only the targeted violations are verified, so an untargeted one is forgotten",
     [(NA, r"(?m)^        for violation in targeted if mutated else \(\):$",
       "        for violation in () if mutated else ():")],
     TG, "test_all_five_repaired_persists_and_finalises_DONE"),

    # ── ordering, BEHAVIOURALLY ────────────────────────────────────────────
    # 🔴 THE SAME EDIT AS MUTANT 36, A DIFFERENT KIND OF EVIDENCE. 36 dies against a test that
    #    reads the source order; this one dies against a job that RAN the L3-assist repair,
    #    had the drift put back into the delivered bytes, and had to refuse. Until this row
    #    existed, the ordering claim rested entirely on line numbers.
    ("64. the finaliser is called before the L3-assist repair (behavioural witness)",
     [(NA, r"(?m)^    # F6 verification and accounting do NOT happen here: the L3-assist repair and the F1\n"
           r"    # scrub below both still mutate `result`\. See the finalise call further down, which is\n"
           r"    # the LAST thing before the delivery decision\.$",
       "    _f6_out = await _f6_finalize(result, body, tenant_id=tenant_id, user_id=user_id,\n"
       "                                 job_uuid=job_uuid, sink=sink, job_id=job_id)")],
     TORD, "test_a_drift_reintroduced_by_the_l3_assist_repair_blocks_delivery"),

    ("65. the hard block is removed — the five-defect book ships (behavioural witness)",
     [(NA, r"(?m)^    if isinstance\(_f6_out, dict\) and _f6_out\.get\(\"delivery_blocked\"\):$",
       "    if False:")],
     TG, "test_one_failed_class_out_of_five_blocks_the_whole_delivery[tense_drift-kwargs0]"),

    # ── the new censuses ───────────────────────────────────────────────────
    ("66. a bool passes as a teleport count",
     [(NG, r"(?m)^        if isinstance\(value, bool\) or not isinstance\(value, int\) or value < 0:\n"
           r"            return \{\*\*empty, \"reason\": \"invalid_value\"\}$",
       "        if not isinstance(value, int) or value < 0:\n"
       "            return {**empty, \"reason\": \"invalid_value\"}")],
     TCLS, "test_a_bool_is_not_a_count"),

    ("67. the teleport census stops having to cover every chapter",
     [(NG, r"(?m)^    if chapter_count is not None and len\(counts\) != int\(chapter_count\):$",
       "    if False:")],
     TCLS, "test_a_teleport_census_must_cover_every_chapter"),

    ("68. a partial beat census is accepted, so a missing beat goes unreported",
     [(NG, r"(?m)^    if set\(states\) != expected:$", "    if False:")],
     TCLS, "test_an_incomplete_beat_census_is_refused"),

    # 🔴 MUTANT 69 IS INVERTED FROM ITS FIRST FORM, AND THE REASON IS A PRODUCTION INCIDENT.
    # It used to assert that an out-of-keyspace row INVALIDATES the census. Live job `lyjzgd69`
    # (2026-08-17) proved that rule wrong at the customer's expense: complete, correct coverage
    # plus one invented pair was read as UNPROVED, and UNPROVED blocks. The rule now is "drop
    # it and count it", so the mutant restores the old fatal return — and its witness is the
    # BEHAVIOURAL row on the real job path, not the census helper, because what has to stay
    # dead is the REFUSAL, not a return value.
    ("69. an out-of-keyspace beat row is fatal again — a sound book is refused",
     [(NG, r"(?m)^        if key not in expected:\n            ignored_unknown \+= 1\n            continue$",
       "        if key not in expected:\n            return {**empty, \"reason\": \"unknown_beat\"}")],
     TG, "test_an_invented_beat_beside_complete_coverage_never_blocks_a_sound_book"),

    ("69c. the dropped rows stop being counted, so the filter is unauditable",
     [(NG, r"(?m)^            ignored_unknown \+= 1$", "            ignored_unknown += 0")],
     TCLS, "test_a_beat_the_outline_does_not_have_is_ignored_and_counted"),

    ("69d. an unknown row is allowed to stand in for a beat the outline owns",
     [(NG, r"(?m)^    if set\(states\) != expected:\n        return \{\*\*empty, \"reason\": \"incomplete_coverage\"\}$",
       "    if False:\n        return {**empty, \"reason\": \"incomplete_coverage\"}")],
     TG, "test_an_invented_beat_cannot_hide_a_beat_the_outline_owns"),

    ("69e. a row with no usable identity is swallowed by the filter instead of refused",
     [(NG, r"(?m)^        if \(isinstance\(chapter, bool\) or not isinstance\(chapter, int\)\n"
           r"                or isinstance\(beat, bool\) or not isinstance\(beat, int\)\):\n"
           r"            return \{\*\*empty, \"reason\": \"invalid_entry\"\}$",
       "        if False:\n            return {**empty, \"reason\": \"invalid_entry\"}")],
     TCLS, "test_a_row_with_no_usable_identity_is_still_fatal"),

    ("69b. two contradictory answers for one beat are silently reconciled",
     [(NG, r"(?m)^        if key in states and states\[key\] != state:\n"
           r"            return \{\*\*empty, \"reason\": \"contradictory_entry\"\}$",
       "        if False:\n            return {**empty, \"reason\": \"contradictory_entry\"}")],
     TCLS, "test_two_contradictory_answers_for_one_beat_are_no_answer"),

    ("70. the heading is counted as prose, so localisation moves a chapter's word count",
     [(NG, r"(?m)^        counts\.append\(len\(block\[len\(heading\):\]\.split\(\)\)\)$",
       "        counts.append(len(block.split()))")],
     TCLS, "test_the_heading_is_not_counted_as_prose"),

    ("70b. the preamble is counted as chapter one",
     [(NG, r"(?m)^        heading = chapter_heading_line\(block\)\n        if not heading:\n"
           r"            continue\n        counts\.append\(len\(block\[len\(heading\):\]\.split\(\)\)\)$",
       "        heading = chapter_heading_line(block)\n        if False:\n            continue\n"
       "        counts.append(len(block[len(heading):].split()))")],
     TCLS, "test_a_preamble_is_not_counted_as_a_chapter"),

    # ── the word contract ──────────────────────────────────────────────────
    ("71. the ceiling stops being the generator's own `word_target × 1.1` contract",
     [(F6, r"(?m)^        bounds\[index\] = \(int\(raw \* 0\.9\), int\(raw \* 1\.1\)\)$",
       "        bounds[index] = (0, int(raw * 3))")],
     TCLS, "test_the_ceiling_is_the_generators_own_contract"),

    # 🔴 THE OLD TRIGGER WAS "a violation we already knew about was targeted", which is exactly
    #    why a repair could introduce a defect of an UNFLAGGED class and ship it. The trigger is
    #    now the mutation itself.
    ("72. only a targeted violation triggers the re-read, so a new defect is never seen",
     [(NA, r"(?m)^        mutated = bool\(changed\) or attempts >= 1$",
       "        mutated = bool(changed) and bool(detected)")],
     # 🔴 THE WITNESS HAS TO HAVE NOTHING DETECTED. Any row with a pre-existing violation
     #    satisfies both the old trigger and the new one, so it cannot tell them apart.
     TFS, "test_a_late_mutation_on_a_book_with_NOTHING_detected_still_blocks"),

    # ══════════════════════════════════════════════════════════════════════
    # THE FINAL SCAN, THE KILL SWITCH, AND THE FAIL-CLOSED EDGES
    #
    # 🔴 EACH OF THESE WAS PROVEN RED BEFORE ITS FIX. The witness is the job's DELIVERY
    #    decision, never a helper's return value or a boolean flag.
    # ══════════════════════════════════════════════════════════════════════

    # ── the final scan itself ──────────────────────────────────────────────
    ("80. the final scan never runs, so a defect the repair INTRODUCED ships",
     [(NA, r"(?m)^            final_scan = _f6_scan\($", "            final_scan = None or _f6_skip(")],
     TFS, "test_a_repair_that_introduces_tense_drift_elsewhere_blocks"),

    ("81. what the final scan found never reaches the books",
     [(NA, r"(?m)^            detected = detected \+ final_scan\[\"violations\"\]$",
       "            detected = detected")],
     TFS, "test_a_repair_that_introduces_a_teleport_blocks"),

    # 🔴 THE FIRST FORM OF 81b ADDED THE SCAN'S FINDINGS TO `targeted` AS WELL, and SURVIVED —
    #    correctly, because it is inert: the verdict loop has already run by then, so a
    #    late-targeted violation still has no verdict and stays unresolved. This form records
    #    them on the WRONG SIDE of the books, which is a real defect shape: `targeted_ids` keeps
    #    only ids that are also in `detected_ids`, so a finding filed as targeted-but-not-
    #    detected vanishes from the partition entirely.
    ("81b. the scan's findings are filed as targeted instead of detected, and vanish",
     [(NA, r"(?m)^            detected = detected \+ final_scan\[\"violations\"\]$",
       "            targeted = targeted + final_scan[\"violations\"]")],
     TFS, "test_a_repair_that_pushes_another_chapter_over_its_ceiling_blocks"),

    ("82. an unreadable census ABOUT THE DELIVERED BOOK stops blocking",
     [(NA, r"(?m)^        if final_scan is not None and final_scan\[\"unproven\"\]:$",
       "        if False:")],
     TFS, "test_a_repair_that_leaves_the_book_with_no_dominant_tense_blocks"),

    # 🔴 MUTANT 83 IS GONE, AND THE ROUTE THERE IS WORTH KEEPING. It swapped the final scan's
    #    chapter count from the delivered book's to the snapshot's. I first deleted it calling
    #    the two readings inseparable; the audit was right that the reasoning was wrong, because
    #    at that time `changed is None` did not refuse at all. Once it DID refuse, the mutant
    #    survived again — and for a third reason: the structural rule guarantees the two counts
    #    are equal by the time the scan runs, so the recount was dead code. It has been removed
    #    rather than mutated. A line no delivery outcome can distinguish is not a control.

    # ══════════════════════════════════════════════════════════════════════
    # THE FIVE STRUCTURAL BLOCKERS THE AUDIT FOUND
    # ══════════════════════════════════════════════════════════════════════
    ("90. a change set that cannot be determined stops refusing on its own account",
     [(NA, r"(?m)^        if changed is None:$", "        if False:")],
     TFS, "test_a_structural_break_with_GENUINELY_nothing_detected_blocks"),

    ("91. the manuscript no longer has to have the chapters the request asked for",
     [(NA, r"(?m)^    if expected_chapters and chapter_count != int\(expected_chapters\):$",
       "    if False:")],
     TFS, "test_a_manuscript_short_of_the_requested_chapter_count_blocks"),

    ("91b. the expected count is read from the manuscript instead of the request",
     [(NA, r"(?m)^            _f6_expected = len\(body\.get\(\"chapters\"\) or \(\)\) if isinstance\($",
       "            _f6_expected = _f6_n_ch if isinstance(")],
     TFS, "test_a_manuscript_short_of_the_requested_chapter_count_blocks"),

    ("92. an outline beat is remapped onto a chapter the outline never named",
     [(NA, r'(?m)^            "chapter": _bch,$',
       '            "chapter": min(_bch, chapter_count) if chapter_count else _bch,')],
     TFS, "test_an_outline_chapter_beyond_the_manuscript_is_never_remapped"),

    ("93. the heading sequence stops being checked",
     [(NA, r"(?m)^    if not _ngate\.chapter_headings_well_formed\(text\):$", "    if False:")],
     TFS, "test_headings_out_of_sequence_block"),

    ("93b. a keyword with no ordinal is a heading again",
     [(NG, r"(?m)^    rf\"\(\?:\(\?:\{_CHAPTER_HEADING_KEYWORDS\}\)\[\^\\S\\n\]\+\[0-9\]\{\{1,4\}\}\"$",
       '    rf"(?:(?:{_CHAPTER_HEADING_KEYWORDS})\\b"')],
     TCLS, "test_a_keyword_without_a_valid_ordinal_is_not_a_heading[Bab ini dimulai dengan tenang dan tidak ada yang berubah.]"),

    ("93c. the ordinals no longer have to run 1..N in order",
     [(NG, r"(?m)^    return bool\(sequence\) and sequence == list\(range\(1, len\(sequence\) \+ 1\)\)$",
       "    return bool(sequence)")],
     TCLS, "test_headings_must_run_1_to_N_in_order[numbers2-False]"),

    ("94. any preamble is normalised away again, not just the server's own framing",
     [(F6, r"(?m)^    if not _is_server_framing\(preamble_before\) or not _is_server_framing\(preamble_after\):\n"
           r"        return None$",
       "    if False:\n        return None")],
     TFS, "test_an_injected_preamble_is_a_structural_failure"),

    ("94b. the server header may be rewritten between the snapshot and delivery",
     [(F6, r"(?m)^    if preamble_before\.strip\(\) and preamble_after\.strip\(\) and preamble_before != preamble_after:\n"
           r"        return None$",
       "    if False:\n        return None")],
     TA, "test_a_changed_preamble_is_not_attributed_to_any_chapter"),

    ("94c. framing is judged by its first bytes, so text hidden under the header passes",
     [(F6, r"(?m)^    return all\(line\.startswith\(\">\"\) or set\(line\) == \{\"-\"\} for line in lines\)$",
       "    return True")],
     TFS, "test_an_injected_preamble_is_a_structural_failure"),

    ("95. a chapter another gate targeted counts as collateral again",
     [(F6, r"(?m)^    allowed \|= \{c for c in \(co_targeted_chapters or \(\)\)\n"
           r"                if isinstance\(c, int\) and not isinstance\(c, bool\) and c >= 1\}$",
       "    allowed |= set()")],
     TFS, "test_a_chapter_another_gate_targeted_is_not_collateral"),

    # 🔴 THE FIRST FORM WAS `[] or sorted({…})`, WHICH EVALUATES TO THE SORTED SET. An inert
    #    mutation cannot die, and it SURVIVED for that reason alone — cause 4 in the handoff's
    #    list, a mutant that cannot run. This one empties the value where it is handed on.
    ("95b. the other gates' targets never reach the finaliser, so the widening is inert",
     [(NA, r'(?m)^                "co_targeted": list\(_f6_co_targeted\),$',
       '                "co_targeted": [],')],
     TFS, "test_a_chapter_another_gate_targeted_is_not_collateral"),

    # ── `no_majority` is the drift, not the absence of it ──────────────────
    ("84. a book with no dominant tense reads as clean again",
     [(NA, r"(?m)^    if not tense\.get\(\"valid\"\) or not tense\.get\(\"majority\"\):$",
       "    if not tense.get(\"valid\"):")],
     TFS, "test_a_book_with_no_dominant_tense_is_unproved_not_clean"),

    # ── the kill switch ────────────────────────────────────────────────────
    ("85. the switch stops wrapping detection, so an off F6 still detects and routes",
     [(NA, r"(?m)^    if _f6_enabled\(\):\n        try:\n            import narasi_f6 as _nf6$",
       "    if True:\n        try:\n            import narasi_f6 as _nf6")],
     TFS, "test_the_kill_switch_disables_detection_routing_and_the_block"),

    ("85b. the switch stops wrapping the finaliser, so an off F6 still refuses deliveries",
     [(NA, r"(?m)^    if not _f6_enabled\(\):\n        return \{\}$",
       "    if False:\n        return {}")],
     TFS, "test_a_job_with_no_pre_repair_state_is_fine_when_f6_is_off"),

    # ── one chapter is a book ──────────────────────────────────────────────
    ("86. the own-census call goes back to needing two chapters",
     [(NA, r"(?m)^            if _f6_cq\.get\(\"tense_by_chapter\"\) is None and _f6_n_ch >= 1:$",
       "            if _f6_cq.get(\"tense_by_chapter\") is None and _f6_n_ch >= 2:")],
     TFS, "test_a_single_chapter_book_is_censused_and_delivered"),

    # ── no pre-repair state ────────────────────────────────────────────────
    ("87. a job with no pre-repair state is waved through again",
     [(NA, r"(?m)^    if not isinstance\(pending, dict\):\n"
           r"        log\.error\(\"narration job %s: F6 has no pre-repair state — delivery BLOCKED\", job_id\)$",
       "    if not isinstance(pending, dict):\n        return {}")],
     TFS, "test_a_job_that_never_recorded_a_pre_repair_state_is_refused"),

    # ── the durable payload ────────────────────────────────────────────────
    ("88. the accounting is dropped on its way to the jobs row",
     [(NA, r"(?m)^        payload\[\"f6\"\] = _f6_out$", "        pass")],
     TFS, "test_the_accounting_reaches_the_durable_result_payload"),

    ("88b. the persisted id lists stop being bounded",
     [(NA, r"(?m)^                _f6_out\[_k\] = \[str\(_i\)\[:_F6_PAYLOAD_MAX_ID_CHARS\]\n"
           r"                               for _i in _v\[:_F6_PAYLOAD_MAX_IDS\]\]$",
       "                _f6_out[_k] = list(_v)")],
     TFS, "test_the_persisted_accounting_is_bounded"),

    ("88c. the refusal reason is persisted unbounded",
     [(NA, r"(?m)^                _f6_out\[_k\] = str\(_f6\.get\(_k\)\)\[:_F6_PAYLOAD_MAX_REASON_CHARS\]$",
       "                _f6_out[_k] = str(_f6.get(_k))")],
     TFS, "test_the_persisted_accounting_is_bounded"),

    ("73. the verification pass is asked without the authority, so no beat census comes back",
     [(NA, r"(?m)^                authority_text=_narrative_authority_text\(result\)\)$",
       "                authority_text=\"\")")],
     TG, "test_all_five_repaired_persists_and_finalises_DONE"),
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
    good = [(NG, r"(?m)^    bounds = starts \+ \[len\(text\)\]$",
             "    bounds = starts + [starts[-1]]")]
    cases = [
        ("missing test file",
         ("6. last chapter dropped", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_chapter_blocks_rejoin_to_the_original_byte_for_byte")),
        ("wrong node id",
         ("6. last chapter dropped", good, TF, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(NG, r"(?m)^_THIS_SYMBOL_WAS_RENAMED = 1$", "    pass")],
          TF, "test_chapter_blocks_rejoin_to_the_original_byte_for_byte")),
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
        print("Mutation proof: F6 chapter framing\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
