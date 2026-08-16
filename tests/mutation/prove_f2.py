#!/usr/bin/env python3
"""Mutation proof for the F2 legacy no-op / accounting controls.

Companion to prove_stage1.py and prove_f1_round2.py; the hardening in `_classify`
is copied from prove_f1_round2.py deliberately — see the note there.

    python3 tests/mutation/prove_f2.py              # prove the controls
    python3 tests/mutation/prove_f2.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Each entry breaks ONE control and names the test that must go red. A control whose
test still passes when the control is removed is not a control.

⚠ ON MUTANT 8 — a correction to the brief, not a deviation from it.
   The brief asks for a mutant that "compares `_new` with `_body`". That rewrite is
   NOT a mutation: `_p == _body + _trail` holds by construction (`_body = _p.rstrip()`,
   `_trail = _p[len(_p.rstrip()):]`), so `(_new + _trail) == _p` and `_new == _body`
   are the SAME predicate — verified over 200k random whitespace-heavy pairs, 0
   divergences. Scoring an unkillable no-difference rewrite as a mutant is precisely
   the failure prove_f1_round2.py was written to stop. The defect the brief is
   protecting against is real and IS killable in its other form: comparing the
   stripped candidate straight against the raw part (`_new == _p`), which drops the
   server-owned trailing frame and makes every echo of a trailing-framed chapter read
   as a change. That is what mutant 8 does.
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
LZ = WT / "python/laozhang_api.py"
NAPI = WT / "python/narration_api.py"
T = "tests/python/test_narasi_legacy_noop_accounting.py"

# Serial and parallel carry byte-identical guard lines at different nesting depths, so
# every pattern is ^-anchored at its EXACT indentation — otherwise count=1 would always
# hit the parallel lane (it comes first in the file) and the serial mutants would be
# silently proving the wrong lane.
BREAKS = [
    ("1. serial no-op predicate removed",
     [(LZ, r"(?m)^            _is_noop = \(_new \+ _trail\) == _p$",
       "            _is_noop = False")],
     T, "test_stats_row3_two_noops_are_unresolved_not_revised[0-serial]"),

    ("2. parallel no-op predicate removed",
     [(LZ, r"(?m)^                _is_noop = \(_new \+ _trail\) == _p$",
       "                _is_noop = False")],
     T, "test_stats_row3_two_noops_are_unresolved_not_revised[2-parallel]"),

    ("3. serial accept gate ships a no-op as `revised`",
     [(LZ, r"(?m)^                    and _fid >= _min_fid and not _is_noop\):$",
       "                    and _fid >= _min_fid):")],
     T, "test_stats_row3_two_noops_are_unresolved_not_revised[0-serial]"),

    ("3b. parallel accept gate ships a no-op as `revised`",
     [(LZ, r"(?m)^                        and _fid >= _min_fid and not _is_noop\):$",
       "                        and _fid >= _min_fid):")],
     T, "test_stats_row3_two_noops_are_unresolved_not_revised[2-parallel]"),

    ("4. serial corrective retry disabled",
     [(LZ, r"(?m)^            if \(_attempt_no == 1 and _fid >= _min_fid$",
       "            if (False and _attempt_no == 1 and _fid >= _min_fid")],
     T, "test_stats_row2_noop_then_valid_change[0-serial]"),

    ("4b. parallel corrective retry disabled",
     [(LZ, r"(?m)^                if \(_attempt_no == 1 and _fid >= _min_fid$",
       "                if (False and _attempt_no == 1 and _fid >= _min_fid")],
     T, "test_stats_row2_noop_then_valid_change[2-parallel]"),

    ("5. serial retry not counted as a physical provider call",
     [(LZ, r"(?m)^            _n_attempts \+= 1 ",
       "            _n_attempts += 1 if _attempt_no == 1 else 0 ")],
     T, "test_stats_row2_noop_then_valid_change[0-serial]"),

    ("6. serial counts a FAILED candidate as a changed chapter",
     [(LZ, r"(?m)^        if _accepted_txt is not None and _accepted_txt != _p:$",
       "        if _accepted_txt is not None or _chapter_called:")],
     T, "test_stats_row3_two_noops_are_unresolved_not_revised[0-serial]"),

    ("7. parallel skips the shared 2*max_ch budget again (the 2x cost defect)",
     [(LZ, r"(?m)^                if _parallel_attempts \+ _attempts_already_spent >= 2 \* _max_ch:$",
       "                if (_attempts_already_spent > 0\n"
       "                        and _parallel_attempts + _attempts_already_spent"
       " >= 2 * _max_ch):")],
     T, "test_budget_all_first_attempts_noop_never_exceeds_two_times_max[2-parallel]"),

    ("8. serial compares the STRIPPED candidate against the raw part "
     "(trailing frame becomes a fake change)",
     [(LZ, r"(?m)^            _is_noop = \(_new \+ _trail\) == _p$",
       "            _is_noop = _new == _p")],
     T, "test_stats_row6_trailing_frame_only_difference_is_ineffective[0-serial]"),

    ("8b. parallel compares the STRIPPED candidate against the raw part",
     [(LZ, r"(?m)^                _is_noop = \(_new \+ _trail\) == _p$",
       "                _is_noop = _new == _p")],
     T, "test_stats_row6_trailing_frame_only_difference_is_ineffective[2-parallel]"),

    ("9. violations_verified_resolved aliased to chapters_changed",
     [(LZ, r'(?m)^        "violations_verified_resolved": 0,$',
       '        "violations_verified_resolved": chapters_changed,')],
     T, "test_a_changed_chapter_is_never_reported_as_a_verified_resolution"),

    ("10. critique[\"legacy_revise\"] publication removed",
     [(LZ, r'(?m)^        critique\["legacy_revise"\] = stats$',
       "        pass")],
     T, "test_dispatcher_publishes_the_block_and_it_reaches_the_result_payload"),

    # ── F2 round 2: the two closure defects Rino's review found ──
    ("11. shared-headroom routing removed (parallel starves the corrective retry)",
     [(LZ, r"(?m)^    if _rev_par >= 2 and _attempts_already_spent == 0:$",
       "    if _rev_par >= 2:")],
     T, "test_shared_headroom_never_starves_the_corrective_retry[4]"),

    ("11b. routing OVER-applied (parallel lane serialised unconditionally) — proves "
     "the guard is minimal, not a blanket kill of the lane",
     [(LZ, r"(?m)^    if _rev_par >= 2 and _attempts_already_spent == 0:$",
       "    if False:")],
     T, "test_parallel_lane_still_retries_when_first_attempts_are_genuinely_concurrent"),

    ("12. narration/L3 gate route stops copying the block onto `result`",
     [(NAPI,
       r'(?m)^                result\["legacy_revise"\] = _v3g_revise_request\["legacy_revise"\]$',
       "                pass")],
     T, "test_v3_gates_copy_the_legacy_block_onto_the_result"),

    ("13. _result_payload stops forwarding the block to the durable row",
     [(NAPI, r'(?m)^        payload\["legacy_revise"\] = _legacy_revise$',
       "        pass")],
     T, "test_result_payload_persists_the_legacy_block"),
]


def _pytest(test_file, test_name):
    with isolated_run() as env:
        return subprocess.run(
            [sys.executable, "-m", "pytest", f"{test_file}::{test_name}",
             "-q", "-p", "no:cacheprovider", "--no-header"],
            cwd=WT, capture_output=True, text=True, env=env)


def _classify(proc):
    """What pytest ACTUALLY said. Returns ("pass"|"fail", None) or (None, reason).

    🔴 THE ONE RULE THIS FILE EXISTS TO ENFORCE ON ITSELF (inherited verbatim from
       prove_f1_round2.py). Scoring a mutant KILLED on `returncode != 0` is also true
       for a missing test file, a mistyped node id, a collection error and a usage
       error — an earlier harness printed "9/9 mutants killed" against test files that
       did not exist. Only exit 0 and exit 1 are verdicts; everything else is this
       instrument being broken and is reported as such.
    """
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
    # 1. The node must EXIST and PASS before we mutate anything. Without this, a
    #    typo'd node id is indistinguishable from a control that works.
    verdict, why = _classify(_pytest(test_file, test_name))
    if verdict != "pass":
        print(f"  !! BASELINE NOT GREEN ({why or verdict}) — {label}")
        return False

    targets = list(dict.fromkeys(path for path, _p, _r in edits))
    for path in targets:
        if not path.exists():
            print(f"  !! SOURCE MISSING {path} — {label}")
            return False

    # 3. And the tree must be byte-identical afterwards — bytes, mode AND mtime_ns.
    #    A harness that leaves a mutation behind poisons every later run; one that
    #    restores the bytes but not the timestamp poisons the BYTECODE instead, and
    #    that is worse because `git diff` then reports clean. `restore_guard` restores
    #    and re-verifies all three, and raises if it cannot.
    with restore_guard(*targets):
        for path, pattern, repl in edits:
            src = path.read_text(encoding="utf-8")
            # count=0 (all) then assert EXACTLY one site: a pattern that silently
            # matches two lanes would mutate only the first under count=1 and the
            # entry would then be proving a lane it does not name.
            mutated, n = re.subn(pattern, repl, src)
            if n != 1:
                print(f"  !! PATTERN MATCHED {n}x (need exactly 1) — {label}")
                return False
            path.write_text(mutated, encoding="utf-8")

        # 2. And with the control removed it must FAIL — as a test failure, not as
        #    any of the ways pytest can decline to run at all.
        verdict, why = _classify(_pytest(test_file, test_name))
        if verdict is None:
            print(f"  !! INCONCLUSIVE ({why}) — {label}")
            return False
        killed = (verdict == "fail")
        print(f"  {'KILLED ' if killed else 'SURVIVED'} — {label}")
        return killed


def selftest():
    """Break the instrument on purpose. Each case MUST be refused, not scored.

    A proof harness is only evidence if it can come back negative, so this is run
    before trusting any result below it."""
    print("Self-test: the harness must REFUSE these three broken configurations\n")
    cases = [
        ("missing test file",
         ("1. serial no-op predicate removed",
          [(LZ, r"(?m)^            _is_noop = \(_new \+ _trail\) == _p$",
            "            _is_noop = False")],
          "tests/python/test_this_file_does_not_exist.py",
          "test_stats_row3_two_noops_are_unresolved_not_revised[0-serial]")),
        ("wrong node id",
         ("1. serial no-op predicate removed",
          [(LZ, r"(?m)^            _is_noop = \(_new \+ _trail\) == _p$",
            "            _is_noop = False")],
          T, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(LZ, r"(?m)^            _is_noop = _THIS_SYMBOL_WAS_RENAMED$", "            pass")],
          T, "test_stats_row3_two_noops_are_unresolved_not_revised[0-serial]")),
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
        print("Mutation proof: F2 legacy no-op / accounting controls\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
