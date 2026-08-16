#!/usr/bin/env python3
"""Mutation proof for the F4a physical-provider-call controls.

    python3 tests/mutation/prove_f4a.py              # prove the controls
    python3 tests/mutation/prove_f4a.py --selftest   # prove THIS FILE can fail

Override the checkout with PROVE_WT=/path/to/worktree.

Hardening inherited from prove_f1_round2 / prove_f2 / prove_f3: only pytest exit 0 and
exit 1 are verdicts, exit 1 must carry an "N failed" summary, anything else is
INCONCLUSIVE rather than a kill, and the source is restored byte-identically.

⚠ TWO PATTERN HAZARDS THIS FILE HAD TO HANDLE, both real:
  · `"provider_calls": provider_calls,` now appears THREE times in laozhang_api.py — in
    F2's legacy stats builder, in F4a's structural summary, and in the structural stats
    return. The first two share an indentation, so the summary mutant is anchored on the
    FOLLOWING line (`"chapters_accepted"`) to stay unique. A pattern that matched two
    sites would mutate only the first under count=1 and the entry would silently prove
    the wrong builder.
  · replacements are applied through a CALLABLE, not a string template: `re.sub`
    processes escapes in a string replacement, so any mutant writing a literal
    containing `\\n` would inject a real newline and produce a SyntaxError instead of
    the intended mutation (found the hard way in prove_f3.py).
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
T = "tests/python/test_narasi_f4a_provider_calls.py"
PT = "tests/python/test_narasi_f4a_probe_artifact.py"
PROBE = WT / "python/tools/narasi_f4a_probe.py"

BREAKS = [
    # Anchored on the preceding `if`: since the timeout handshake was added there are
    # TWO `provider_calls += 1` sites (the normal path and the bounded join), and a
    # bare pattern would match both — the harness refused it, correctly.
    ("1. the plain-client upstream increment is dropped",
     [(LZ, r"(?m)^                if not _narasi_reserve_upstream\(\):$", "                if False:")],
     T, "test_unknown_operation_is_one_attempt_and_two_physical_calls"),

    ("2. provider_calls hardcoded to the attempt count in the returned stats",
     [(LZ, r'(?m)^            "provider_calls": provider_calls,$',
       '            "provider_calls": attempted,')],
     T, "test_a_client_that_fails_before_create_is_not_billed"),

    ("3. the field is dropped from the structural summary",
     [(LZ, r'(?m)^        "provider_calls": provider_calls,\n'
           r'        "chapters_accepted": accepted,$',
       '        "chapters_accepted": accepted,')],
     T, "test_the_summary_is_exactly_the_v3_shape"),

    ("4. the legacy budget is debited with attempts again (the cost defect)",
     [(LZ, r'(?m)^                    attempts_already_spent=int\(patch_stats\["provider_calls"\]\),$',
       '                    attempts_already_spent=int(patch_stats["attempted"]),')],
     T, "test_the_legacy_budget_is_debited_with_physical_calls_not_attempts[0]"),

    ("5. the RAW operation token is logged instead of its hash",
     [(LZ, r"(?m)^            chapter_number, subtype, len\(token\), token_hash,$",
       "            chapter_number, subtype, len(token), token,")],
     T, "test_the_raw_operation_token_never_reaches_logs_or_the_summary"),

    ("6. the schema version is left at v1 after the field was added",
     [(LZ, r'(?m)^_STRUCTURAL_PATCH_SUMMARY_VERSION = "structural_patch_summary_v3"$',
       '_STRUCTURAL_PATCH_SUMMARY_VERSION = "structural_patch_summary_v2"')],
     T, "test_the_summary_is_exactly_the_v3_shape"),

    # ── the four defects the first six mutants did NOT cover (audit reject) ──
    ("7. the abandon guard is removed — a written-off attempt dispatches anyway",
     [(LZ, r"(?m)^            if generation in self\._abandoned:$", "            if False:")],
     T, "test_an_attempt_abandoned_before_dispatch_is_forbidden_to_dispatch"),

    ("7b. the adapter invocation is reinstated as a `floor` under the sink",
     [(LZ, r'(?m)^    provider_calls = 0 if _upstream_ledger is None else int\(_upstream_ledger\.count\)$',
       "    provider_calls = max(0 if _upstream_ledger is None else int(_upstream_ledger.count), 1 if attempted else 0)")],
     T, "test_a_failover_that_dies_while_being_built_costs_nothing"),

    ("7d. the rung loop stops honouring the caller's abandonment",
     [(LZ, r"(?m)^                    _upstream_ledger\.abandon\(_generation\)$",
       "                    pass")],
     T, "test_a_failover_retry_is_forbidden_once_the_caller_has_given_up"),

    ("7e. the vertex reservation moves OUT of the helper, back above its own preflight",
     [(LZ, r'(?m)^            _narasi_check_reserve\(_reserve, "vertex_genai"\)$', "            pass"),
],
     T, "test_the_vertex_helper_refuses_after_its_own_client_preflight"),

    ("7j. the FAL submit line loses its reservation",
     [(LZ, r'(?m)^            _narasi_check_reserve\(_reserve, "fal_queue"\)$', "            pass")],
     T, "test_the_fal_helper_refuses_at_submit_not_at_polling"),

    ("7h. the plain structural client stops forcing max_retries=0",
     [(LZ, r"(?m)^                _client = _client\.with_options\(max_retries=0\)$",
       "                pass")],
     T, "test_the_plain_structural_client_disables_sdk_retries"),

    ("7i. the probe re-invents its independent observation from adapter entries",
     [(PROBE, r'(?m)^    upstream = int\(observed\["upstream_requests"\]\)$',
       '    upstream = int(observed["upstream_requests"]) or entered')],
     PT, "test_a_probe_with_no_transport_observer_is_not_live"),

    ("7f. the artifact schema version is left at v1 after the shape changed",
     [(PROBE, r'(?m)^ARTIFACT_SCHEMA_VERSION = "narasi_f4a_probe_artifact_v2"$',
       'ARTIFACT_SCHEMA_VERSION = "narasi_f4a_probe_artifact_v1"')],
     PT, "test_the_schema_version_moved_with_the_shape"),

    ("7g. the ledger stops honouring the cap",
     [(LZ, r"(?m)^            if self\._cap is not None and self\._count >= self\._cap:$",
       "            if False:")],
     PT, "test_the_upstream_cap_is_enforced_not_promised"),

    ("7c. the upstream boundary stops recording rung requests",
     [(LZ, r'(?m)^            _narasi_check_reserve\(_reserve, "anthropic"\)$', "            pass")],
     T, "test_the_anthropic_helper_refuses_at_its_own_transport_line"),

    ("8. a post-call fault propagates again and the zero-fallback erases the ledger",
     [(LZ, r'(?m)^            _rejected\("internal_error", chapter_number, original_words,\n'
           r'                      final_source="internal_error"\)$',
       "            raise")],
     T, "test_a_fault_after_the_call_keeps_the_accounting"),

    ("9. artifact `live` gated on the FLAG again instead of on evidence",
     [(PROBE,
       r'(?m)^    if not allow_network:\n'
       r'        recording_mode = "offline_dry_run"\n'
       r'    elif upstream >= 1 and observed\["response_received"\] and reported == upstream:\n'
       r'        recording_mode = "live"\n'
       r'    else:\n'
       r'        recording_mode = "failed_probe"$',
       '    recording_mode = "live" if allow_network else "offline_dry_run"')],
     PT, "test_a_paid_run_that_never_reached_the_provider_is_not_live_evidence"),

    ("10. usage is no longer read off the response, so cost silently reports zero",
     [(PROBE, r'(?m)^            captured\["usage"\] = getattr\(response, "usage", None\)$',
       "            captured[\"usage\"] = None")],
     PT, "test_cost_is_recorded_from_the_response_without_a_ledger_write"),
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
    good = [(LZ, r"(?m)^            provider_calls \+= 1$", "            pass")]
    cases = [
        ("missing test file",
         ("1. increment removed", good,
          "tests/python/test_this_file_does_not_exist.py",
          "test_unknown_operation_is_one_attempt_and_two_physical_calls")),
        ("wrong node id",
         ("1. increment removed", good, T, "test_no_such_test_name_at_all")),
        ("pattern miss (e.g. after an innocent rename)",
         ("pattern that no longer exists",
          [(LZ, r"(?m)^            _THIS_SYMBOL_WAS_RENAMED \+= 1$", "            pass")],
          T, "test_unknown_operation_is_one_attempt_and_two_physical_calls")),
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
        print("Mutation proof: F4a physical provider-call controls\n")
        results = [run(*b) for b in BREAKS]
    except IsolationError as exc:
        print(f"\n!! {exc}")
        sys.exit(2)
    print(f"\n{sum(results)}/{len(results)} mutants killed")
    sys.exit(0 if all(results) else 1)
