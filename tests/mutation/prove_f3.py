#!/usr/bin/env python3
"""Mutation proof for the F3 trailing-frame / landed-repair controls.

    python3 tests/mutation/prove_f3.py              # prove the controls
    python3 tests/mutation/prove_f3.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Each entry breaks ONE control and names the test that must go red. A control whose test
still passes when the control is removed is not a control.

🔴 WHY THIS FILE EXISTS SEPARATELY FROM prove_stage2.py. The older harness scores a
   mutant as killed on ANY nonzero pytest exit — which is equally true of a missing test
   file, a mistyped node id, a collection error and a usage error. `_classify` below is
   the hardened version from prove_f1_round2.py / prove_f2.py: only exit 0 and exit 1
   are verdicts, exit 1 additionally has to carry an "N failed" summary, and the source
   is restored byte-identically after every mutant.

Serial/parallel-style twins do not exist here, but the adapter and the engine both carry
`.rstrip()`/heading logic, so every pattern is ^-anchored at its exact indentation and
must match EXACTLY ONE site.
"""
import os
import pathlib
import re
import subprocess
import sys

WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
AD = WT / "python/canon_lite_l3_adapter.py"
RP = WT / "python/canon_lite_l3_repair.py"
T = "tests/python/test_canon_lite_l3_trailing_frame.py"

BREAKS = [
    ("1. trailing-frame reattachment removed (the whole F3 fix)",
     [(AD, r"(?m)^        return candidate_core \+ trailing_frame$",
       "        return candidate_core")],
     T, "test_a_stripped_semantic_repair_lands_end_to_end"),

    ("2. exact frame replaced by a hardcoded generic b\"\\n\\n\"",
     [(AD, r"(?m)^        core, trailing_frame = _split_trailing_frame\(block_bytes\)$",
       '        core, trailing_frame = _split_trailing_frame(block_bytes)[0], b"\\n\\n"')],
     T, "test_a_three_newline_frame_is_restored_exactly_through_the_engine"),

    ("3. the WHOLE block is sent to the provider instead of the core",
     [(AD, r'(?m)^                \+ "\\n\\n" \+ core\.decode\("utf-8", errors="strict"\),$',
       '                + "\\n\\n" + block_bytes.decode("utf-8", errors="strict"),')],
     T, "test_the_prompt_carries_the_core_only_and_never_the_trailing_frame"),

    ("4. pre.heading guard removed",
     [(RP,
       r"(?m)^    if before_heading is not None and _heading_line\(data\) != before_heading:$",
       "    if False:")],
     T, "test_engine_rejects_a_renamed_heading_at_pre_heading"),

    ("5. post.block_count guard removed",
     [(RP,
       r"(?m)^            if staged_snapshot is None or "
       r"len\(staged_snapshot\.blocks\) != len\(current_snapshot\.blocks\):$",
       "            if staged_snapshot is None:")],
     T, "test_post_block_count_still_fires_for_the_one_shape_that_reaches_it"),

    ("6. .rstrip() widened to .strip() — the leading frame gets masked",
     [(AD, r'(?m)^        candidate_core = text\.rstrip\(\)\.encode\("utf-8"\)$',
       '        candidate_core = text.strip().encode("utf-8")')],
     T, "test_engine_rejects_a_leading_blank_frame_rather_than_masking_it"),
]


def _pytest(test_file, test_name):
    return subprocess.run(
        [sys.executable, "-m", "pytest", f"{test_file}::{test_name}",
         "-q", "-p", "no:cacheprovider", "--no-header"],
        cwd=WT, capture_output=True, text=True)


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
    # 1. The node must EXIST and PASS before we mutate anything.
    verdict, why = _classify(_pytest(test_file, test_name))
    if verdict != "pass":
        print(f"  !! BASELINE NOT GREEN ({why or verdict}) — {label}")
        return False

    backups = {}
    try:
        for path, pattern, repl in edits:
            if path not in backups:
                if not path.exists():
                    print(f"  !! SOURCE MISSING {path} — {label}")
                    return False
                backups[path] = path.read_text(encoding="utf-8")
            src = path.read_text(encoding="utf-8")
            # 🔴 LITERAL REPLACEMENT, VIA A CALLABLE. `re.sub` processes escapes in a
            #    STRING template: a replacement carrying `\n` (as any mutant that
            #    rewrites a bytes/str literal must) would emit a real newline INTO the
            #    source, producing a SyntaxError rather than the intended mutation.
            #    That is a mutant which cannot be killed by the behaviour it names —
            #    and it is only visible at all because `_classify` refuses to score a
            #    collection failure as a kill (it reported these as INCONCLUSIVE).
            mutated, n = re.subn(pattern, lambda _m, _r=repl: _r, src)
            if n != 1:
                print(f"  !! PATTERN MATCHED {n}x (need exactly 1) — {label}")
                return False
            path.write_text(mutated, encoding="utf-8")

        # 2. With the control removed it must FAIL — as a test failure, not as any of
        #    the ways pytest can decline to run at all.
        verdict, why = _classify(_pytest(test_file, test_name))
        if verdict is None:
            print(f"  !! INCONCLUSIVE ({why}) — {label}")
            return False
        killed = (verdict == "fail")
        print(f"  {'KILLED ' if killed else 'SURVIVED'} — {label}")
        return killed
    finally:
        # 3. The tree must be byte-identical afterwards.
        for path, original in backups.items():
            path.write_text(original, encoding="utf-8")
            if path.read_text(encoding="utf-8") != original:
                print(f"  !! RESTORE FAILED for {path} — tree is now dirty")
                raise SystemExit(2)


def selftest():
    """Break the instrument on purpose. Each case MUST be refused, not scored."""
    print("Self-test: the harness must REFUSE these three broken configurations\n")
    good_edit = [(AD, r"(?m)^        return candidate_core \+ trailing_frame$",
                  "        return candidate_core")]
    cases = [
        ("missing test file",
         ("1. reattachment removed", good_edit,
          "tests/python/test_this_file_does_not_exist.py",
          "test_a_stripped_semantic_repair_lands_end_to_end")),
        ("wrong node id",
         ("1. reattachment removed", good_edit, T, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(AD, r"(?m)^        return _THIS_SYMBOL_WAS_RENAMED$", "        pass")],
          T, "test_a_stripped_semantic_repair_lands_end_to_end")),
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
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 2)
    print("Mutation proof: F3 trailing-frame / landed-repair controls\n")
    results = [run(*b) for b in BREAKS]
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
