#!/usr/bin/env python3
"""Mutation proof for the F7 chapter-balance telemetry.

    python3 tests/mutation/prove_f7.py              # prove the controls
    python3 tests/mutation/prove_f7.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Isolation is delegated to `_harness`: every subprocess gets its own empty bytecode cache
prefix, and sources are restored through `restore_guard` (bytes + mode + mtime_ns, verified).
This file owns NO second cache-invalidation mechanism.

Verdict rules inherited from the F1-F6 harnesses: only pytest exit 0 and exit 1 are verdicts;
exit 2/3/4/5, a collection error, or an exit 1 with no "N failed" summary is INCONCLUSIVE
rather than a kill; the baseline node must be GREEN before mutating; replacements go through
a CALLABLE so `re.sub` cannot process escapes in them; every pattern must match EXACTLY ONCE.

🔴 THE FIVE RATIFIED F6 SOURCES ARE NEVER TOUCHED BY THIS HARNESS — not even temporarily under
   `restore_guard`. A restore that verifies clean is still a write.

🔴 THE LANGUAGE MUTANTS DO NOT NEED A NON-LATIN LITERAL, AND DELIBERATELY DO NOT CARRY ONE.
   Mutant 1 narrows F7's vocabulary to the Latin subset the frozen F6 gate happens to know;
   the witnesses are parametrised from production's own label map, so the seven languages that
   used to be invisible judge it without this file ever transcribing a label. A hand-copy of
   that list has already lost an entry to transcription once.

🔴 "STALE" AND "CALLER-SUPPLIED" ARE ONE CODE FACT, SO THEY GET ONE MUTANT (14). Writing two
   against one line is the "two mechanisms for one rule" failure this workstream documented
   repeatedly: each hides the other's removal, and neither is then proven.
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
F7 = WT / "python/narasi_f7.py"
DB = WT / "python/database.py"
TB = "tests/python/test_narasi_f7_chapter_balance.py"

BREAKS = [
    # ── the 19-language grammar: P1 #1 ────────────────────────────────────
    # 🔴 THE ORIGINAL DEFECT. Measuring through the frozen gate's six Latin keywords left
    #    it/ko/ar/hi/th/vi/tl permanently UNMEASURED, and a two-label test could not see it.
    ("1. the vocabulary narrows to the Latin subset the frozen F6 gate knows",
     [(F7, r"(?m)^LABEL_ALTERNATION = _label_alternation\(\)$",
       'LABEL_ALTERNATION = r"Chapter[ \\t]*[0-9]|Bab[ \\t]*[0-9]"')],
     TB, "test_every_production_language_is_measured[th]"),

    ("2. the upstream shape stops being validated, so a rebuild degrades in silence",
     [(F7, r"(?m)^    if not \(pattern\.startswith\(prefix\) and pattern\.endswith\(suffix\)\):$",
       "    if False:")],
     TB, "test_a_broken_upstream_vocabulary_raises_rather_than_narrowing_silently"),

    # ── two-phase regime selection: marked and bare are MUTUALLY EXCLUSIVE ──
    # 🔴 `chapter_heading_patterns` states the rule outright: the bare matcher must not run on
    #    a document that already carries `## `. Merging the regimes into one optional-hash
    #    pattern reads as harmless and turns ordinary prose into chapter breaks.
    ("3. the marked regime stops requiring its marker — bare prose is a heading again",
     [(F7, r'(?m)^    rf"\(\?im\)\^\[\^\\S\\n\]\*#\{\{1,3\}\}\[\^\\S\\n\]\*\(\?:\{LABEL_ALTERNATION\}\)\[\^\\n\]\*\$"\)$',
       '    rf"(?im)^[^\\S\\n]*#{{0,3}}[^\\S\\n]*(?:{LABEL_ALTERNATION})[^\\n]*$")')],
     TB, "test_a_marked_document_never_splits_on_bare_prose_indonesian"),

    ("3b. the marked regime becomes a generic markdown H2 splitter",
     [(F7, r'(?m)^    rf"\(\?im\)\^\[\^\\S\\n\]\*#\{\{1,3\}\}\[\^\\S\\n\]\*\(\?:\{LABEL_ALTERNATION\}\)\[\^\\n\]\*\$"\)$',
       '    r"(?im)^[^\\S\\n]*#{2,3}[^\\S\\n]*\\S[^\\n]*$")')],
     TB, "test_a_generic_markdown_h2_is_not_a_chapter"),

    ("3c. the regime is pinned to MARKED — a document without `##` loses every heading",
     [(F7, r'(?m)^    return _MARKED_HEADING_RX if _chp\.MARKER_RX\.search\(text or ""\) else _BARE_HEADING_RX$',
       "    return _MARKED_HEADING_RX")],
     TB, "test_a_document_with_no_marker_still_finds_its_headings"),

    ("3d. the regime is pinned to BARE — prose in a marked book splits it",
     [(F7, r'(?m)^    return _MARKED_HEADING_RX if _chp\.MARKER_RX\.search\(text or ""\) else _BARE_HEADING_RX$',
       "    return _BARE_HEADING_RX")],
     TB, "test_a_marked_document_never_splits_on_bare_prose_english"),

    ("3e. the bare regime becomes case-insensitive — any sentence opening with a label splits",
     [(F7, r'(?m)^_BARE_HEADING_RX = re\.compile\(rf"\(\?m\)\^\[\^\\S\\n\]\*\(\?:\{LABEL_ALTERNATION\}\)\[\^\\n\]\*\$"\)$',
       '_BARE_HEADING_RX = re.compile(rf"(?im)^[^\\S\\n]*(?:{LABEL_ALTERNATION})[^\\n]*$")')],
     TB, "test_the_bare_regime_stays_case_sensitive"),

    # ── ordinal validation: a digit is not an ordinal until it ends like one ──
    ("4. a trailing ASCII letter no longer disqualifies an ordinal (`Chapter 1a`)",
     [(F7, r"(?m)^    if tail\[:1\]\.isascii\(\) and tail\[:1\]\.isalpha\(\):\n        return None$",
       "    if False:\n        return None")],
     TB, "test_a_malformed_ordinal_is_not_a_chapter_number[Chapter 1a: Appendix]"),

    ("4b. a decimal tail no longer disqualifies an ordinal (`Chapter 1.5`)",
     [(F7, r'(?m)^    if tail\[:1\] in \(".", ","\) and tail\[1:2\]\.isdigit\(\):\n        return None$',
       "    if False:\n        return None")],
     TB, "test_a_malformed_ordinal_is_not_a_chapter_number[Chapter 1.5: Half]"),

    ("4c. the ordinal digit run stops being maximal — `Chapter 12` reads as chapter one",
     [(F7, r'(?m)^_ORDINAL_RX = re\.compile\(r"\(\?<!\[0-9\]\)\(\[0-9\]\{1,4\}\)\(\?!\[0-9\]\)"\)$',
       '_ORDINAL_RX = re.compile(r"([0-9])")')],
     TB, "test_a_two_digit_chapter_number_is_read_whole"),

    # ── byte-preserving split ─────────────────────────────────────────────
    ("5. the preamble before the first heading is discarded",
     [(F7, r"(?m)^    if starts\[0\] > 0:\n        blocks\.append\(text\[:starts\[0\]\]\)          # preamble, kept rather than discarded$",
       "    if False:\n        blocks.append(text[:starts[0]])")],
     TB, "test_chapter_blocks_rejoin_to_the_original_byte_for_byte"),

    ("6. the last chapter is dropped from the block list",
     [(F7, r"(?m)^    bounds = starts \+ \[len\(text\)\]$", "    bounds = starts + [starts[-1]]")],
     TB, "test_the_v9_observation_measures_to_the_recorded_ratio"),

    # ── what may be counted ───────────────────────────────────────────────
    ("7. the heading is counted as body text",
     [(F7, r"(?m)^        counts\.append\(len\(block\[len\(heading\):\]\.split\(\)\)\)$",
       "        counts.append(len(block.split()))")],
     TB, "test_long_and_localised_headings_do_not_change_the_body_counts"),

    ("8. the preamble and the server's `> **Gaya:**` header are counted as a chapter",
     [(F7, r"(?m)^        if not heading:\n            continue\n        counts\.append",
       "        if heading is None:\n            continue\n        counts.append")],
     TB, "test_the_preamble_and_the_gaya_header_are_excluded"),

    # ── the arithmetic ────────────────────────────────────────────────────
    ("9. the ratio is inverted — min over max",
     [(F7, r"(?m)^        ratio = round\(most / fewest, 2\)$",
       "        ratio = round(fewest / most, 2)")],
     TB, "test_the_ratio_is_max_over_min_rounded_to_two_decimals"),

    ("10. the two-decimal rounding is dropped",
     [(F7, r"(?m)^        ratio = round\(most / fewest, 2\)$", "        ratio = most / fewest")],
     TB, "test_the_ratio_is_max_over_min_rounded_to_two_decimals"),

    ("11. min and max are swapped",
     [(F7, r"(?m)^        fewest = min\(counts\)\n        most = max\(counts\)$",
       "        fewest = max(counts)\n        most = min(counts)")],
     TB, "test_the_v9_observation_measures_to_the_recorded_ratio"),

    ("12. tied SHORTEST chapters collapse to the first one",
     [(F7, r'(?m)^        "shortest_chapters": \[n for n, words in enumerate\(counts, 1\) if words == fewest\],$',
       '        "shortest_chapters": [counts.index(fewest) + 1],')],
     TB, "test_ties_keep_every_chapter_index_one_based"),

    ("13. tied LONGEST chapters collapse to the first one",
     [(F7, r'(?m)^        "longest_chapters": \[n for n, words in enumerate\(counts, 1\) if words == most\],$',
       '        "longest_chapters": [counts.index(most) + 1],')],
     TB, "test_ties_keep_every_chapter_index_one_based"),

    # ── the report is server-owned ────────────────────────────────────────
    ("14. a caller-supplied report is trusted, so a stale measurement is published",
     [(F7, r'(?m)^    return \{\*\*payload, PAYLOAD_KEY: chapter_balance\(\n'
           r'        payload\.get\("markdown"\), expected_chapters=expected_chapter_counts\(payload\)\)\}$',
       '    return {**payload, PAYLOAD_KEY: payload.get(PAYLOAD_KEY) or chapter_balance(\n'
       '        payload.get("markdown"), expected_chapters=expected_chapter_counts(payload))}')],
     TB, "test_the_report_measures_the_final_markdown_not_an_earlier_snapshot"),

    ("15. the caller's payload is used as scratch space and mutated in place",
     [(F7, r'(?m)^    return \{\*\*payload, PAYLOAD_KEY: chapter_balance\(\n'
           r'        payload\.get\("markdown"\), expected_chapters=expected_chapter_counts\(payload\)\)\}$',
       '    payload[PAYLOAD_KEY] = chapter_balance(\n'
       '        payload.get("markdown"), expected_chapters=expected_chapter_counts(payload))\n'
       '    return payload')],
     TB, "test_the_callers_payload_is_not_mutated"),

    # ── the structural refusals ───────────────────────────────────────────
    ("16. the heading sequence stops being consulted",
     [(F7, r"(?m)^        if not _headings_well_formed\(markdown\):$", "        if False:")],
     TB, "test_a_broken_heading_sequence_is_unmeasured[ordinals0-duplicate]"),

    ("17. the ordinals no longer have to run 1..N in order",
     [(F7, r"(?m)^    return bool\(ordinals\) and ordinals == list\(range\(1, len\(ordinals\) \+ 1\)\)$",
       "    return bool(ordinals)")],
     TB, "test_a_broken_heading_sequence_is_unmeasured[ordinals1-reordered]"),

    ("18. a zero-word chapter becomes a denominator",
     [(F7, r"(?m)^        if fewest <= 0:$", "        if False:")],
     TB, "test_a_zero_word_chapter_cannot_produce_infinity_nan_or_a_ratio"),

    ("19. the 200-chapter bound is removed — an unbounded list reaches the jobs row",
     [(F7, r"(?m)^        if len\(counts\) > MAX_CHAPTERS:$", "        if False:")],
     TB, "test_more_than_two_hundred_chapters_is_bounded_and_unmeasured"),

    ("20. absent / non-text markdown is no longer refused as such",
     [(F7, r"(?m)^        if not isinstance\(markdown, str\) or not markdown\.strip\(\):$",
       "        if False:")],
     TB, "test_absent_or_non_text_markdown_is_unmeasured[None]"),

    ("21. the internal-error refusal carries the exception text into telemetry",
     [(F7, r'(?m)^    except Exception:.*\n        return _unmeasured\("internal_error"\)$',
       '    except Exception as exc:\n'
       '        return {**_unmeasured("internal_error"), "detail": str(exc)}')],
     TB, "test_an_internal_failure_is_reported_as_a_bounded_reason_not_an_exception"),

    # ── expected chapter count: P1 #2 ─────────────────────────────────────
    # 🔴 A TRUNCATED BOOK IS INTERNALLY CONSISTENT — two chapters of a three-chapter job have
    #    headings 1..2 and a real ratio. Only the server's own count reveals the loss.
    ("22. the expected-count comparison is removed — a truncated book measures cleanly",
     [(F7, r"(?m)^        if expected_chapters and set\(expected_chapters\) != \{len\(counts\)\}:$",
       "        if False:")],
     TB, "test_a_book_missing_a_chapter_is_refused_against_the_servers_own_count"),

    ("23. only `n_total` is honoured, so the classic path's `chapters` proves nothing",
     [(F7, r'(?m)^EXPECTED_COUNT_KEYS = \("n_total", "chapters"\)$',
       'EXPECTED_COUNT_KEYS = ("n_total",)')],
     TB, "test_either_server_owned_count_field_alone_is_enough_to_refuse[chapters]"),

    ("24. `True` is accepted as a chapter count (bool is an int in Python)",
     [(F7, r"(?m)^        if isinstance\(value, bool\) or not isinstance\(value, int\) or value <= 0:$",
       "        if not isinstance(value, int) or value <= 0:")],
     TB, "test_an_unusable_count_field_is_ignored_rather_than_obeyed[True]"),

    ("25. two server-owned counts that CONTRADICT each other are accepted",
     [(F7, r"(?m)^        if expected_chapters and set\(expected_chapters\) != \{len\(counts\)\}:$",
       "        if expected_chapters and len(counts) not in expected_chapters:")],
     TB, "test_two_server_owned_counts_that_disagree_are_themselves_a_refusal"),

    ("26. the expectation never reaches the measurement, so the seam cannot refuse",
     [(F7, r'(?m)^        payload\.get\("markdown"\), expected_chapters=expected_chapter_counts\(payload\)\)\}$',
       '        payload.get("markdown"), expected_chapters=frozenset())}')],
     TB, "test_the_seam_refuses_a_truncated_book_end_to_end"),

    # ── the persistence seam: P2 ──────────────────────────────────────────
    ("27. the attachment is dropped, so nothing reaches the durable payload",
     [(DB, r"(?m)^            result = _f7\.attach_chapter_balance\(result\)$", "            pass")],
     TB, "test_a_done_job_persists_the_chapter_balance"),

    # 🔴 `finish_narasi_job` IS SHARED with the async outline path, which finishes `done` with
    #    a dict that holds no manuscript. Keying on status alone stamps telemetry onto it.
    ("28. the guard drops the manuscript requirement — outline payloads get stamped",
     [(DB, r"(?m)^    if isinstance\(_f7_markdown, str\) and _f7_markdown\.strip\(\):$",
       '    if status == "done" and isinstance(result, dict):')],
     TB, "test_an_outline_payload_is_persisted_byte_identical"),

    ("29. a job that FAILED is reported on as if it had been delivered",
     [(DB, r'(?m)^        status == "done" and isinstance\(result, dict\)\) else None$',
       "        isinstance(result, dict)) else None")],
     TB, "test_a_failed_job_carrying_a_payload_is_still_not_measured[cancelled]"),
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
    good = [(F7, r"(?m)^        ratio = round\(most / fewest, 2\)$",
             "        ratio = round(fewest / most, 2)")]
    cases = [
        ("missing test file",
         ("9. inverted ratio", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_the_ratio_is_max_over_min_rounded_to_two_decimals")),
        ("wrong node id",
         ("9. inverted ratio", good, TB, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(F7, r"(?m)^_THIS_SYMBOL_WAS_RENAMED = 1$", "    pass")],
          TB, "test_the_ratio_is_max_over_min_rounded_to_two_decimals")),
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
        print("Mutation proof: F7 chapter-balance telemetry\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
