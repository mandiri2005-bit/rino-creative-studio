#!/usr/bin/env python3
"""Prove the Stage-1 controls are load-bearing: break each property, expect a FAIL.

A control that passes is worth nothing until the thing it guards can be removed
and observed to break it. Everything is restored in `finally`.

THREE source files are mutated, not one. The freeze lives in `canon_lite.py`, the
wiring in `static.py`, and the persistence in `narration_api.py` — and a harness
that only ever mutated the wiring would leave the other two proven by assertion
alone. That is trap #7 (two mechanisms, one visible output) at the file level.

Run it from anywhere:  python3 tests/mutation/prove_stage1.py
Override the checkout with PROVE_WT=/path/to/worktree.
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

# The repo this harness belongs to, derived from its own location. It used to be a
# hard-coded scratchpad path, which meant the proof lived outside the branch it was
# proving and died with the session. PROVE_WT still overrides, for running the
# harness against a different checkout.
WT = pathlib.Path(os.environ.get(
    "PROVE_WT", str(pathlib.Path(__file__).resolve().parents[2])))
STATIC = WT / "python/orchestrator/static.py"
CANON = WT / "python/canon_lite.py"
NAPI = WT / "python/narration_api.py"
TEST = "tests/python/test_canon_lite_l3_assist_stage1.py"

# (label, file, pattern, replacement, test that must FAIL)
BREAKS = [
    # ── injection identity ───────────────────────────────────────────────
    ("no injection — canon never reaches the worker",
     STATIC,
     r"                canon_text=_cl_canon_text, canon_prompt_sha=_cl_prompt_sha,\n",
     "",
     "test_every_worker_receives_identical_canon_bytes_and_hash"),
    ("hash decoupled from the bytes actually sent",
     STATIC,
     r'_cl_prompt_sha = _cl\.sha256_hex\(_cl_canon_text\.encode\("utf-8"\)\)',
     '_cl_prompt_sha = _cl.sha256_hex(b"not-the-canon")',
     "test_every_worker_receives_identical_canon_bytes_and_hash"),
    ("worker echoes the dispatched prompt hash instead of measuring",
     STATIC,
     r'        res\["canon_prompt_sha256"\] = _cl_h\.sha256_hex\(\n'
     r'            _system\[:len\(canon_text\)\]\.encode\("utf-8"\)\)',
     '        res["canon_prompt_sha256"] = canon_prompt_sha',
     "test_real_write_chapter_prepends_canon_and_hashes_delivered_bytes"),
    ("worker echoes the dispatched context digest instead of measuring",
     STATIC,
     r'        res\["context_sha256_seen"\] = _cl_h\.context_digest\(ctx\)',
     '        res["context_sha256_seen"] = context_sha256',
     "test_real_write_chapter_prepends_canon_and_hashes_delivered_bytes"),
    ("MAP serialised — one chapter at a time",
     STATIC,
     r"    sem = asyncio\.Semaphore\(max\(1, int\(max_parallel or 1\)\)\)",
     "    sem = asyncio.Semaphore(1)",
     "test_map_stays_genuinely_parallel_under_assist"),

    # ── GATE 2: the freeze, all three layers ─────────────────────────────
    ("soft freeze under assist — context stays writable",
     STATIC,
     r"_cl\.SharedContextFreeze\(ctx, hard=_cl_assist\)",
     "_cl.SharedContextFreeze(ctx, hard=False)",
     "test_shared_context_is_hard_frozen_before_any_worker_starts"),
    ("deep freeze removed — nested containers writable again",
     CANON,
     r"                    object\.__setattr__\(ctx, attr, _deep_frozen\(current\)\)",
     "                    object.__setattr__(ctx, attr, current)",
     "test_nested_container_mutation_is_refused_not_merely_detected"),
    ("deep freeze removed — one worker can poison the next",
     CANON,
     r"                    object\.__setattr__\(ctx, attr, _deep_frozen\(current\)\)",
     "                    object.__setattr__(ctx, attr, current)",
     "test_one_workers_mutation_is_invisible_to_the_next_worker"),
    ("MAP reads the original alias again, not the frozen snapshot",
     STATIC,
     r"        chapters = _frozen_chapters",
     "        pass",
     "test_the_original_chapter_alias_cannot_reach_the_workers"),
    ("outline divergence tolerated — canon describes another book",
     STATIC,
     r'            return _assist_refuse\("canon_lite_assist_outline_divergence"\)',
     "            pass",
     "test_assist_refuses_when_the_outline_diverges_from_the_canon"),

    # ── GATE 1: fail closed before the first worker ──────────────────────
    ("composer fallback runs the whole book uncanonical",
     STATIC,
     r'        return _assist_refuse\("canon_lite_assist_composer_unavailable"\)',
     "        pass",
     "test_assist_refuses_before_any_worker_when_the_composer_is_unavailable"),
    ("assist fails OPEN when the canon cannot be built",
     STATIC,
     r'            return _assist_refuse\("canon_lite_assist_canon_unavailable"\)',
     "            pass",
     "test_assist_refuses_before_any_worker_when_arming_fails"),
    ("assist fails OPEN when arming raises",
     STATIC,
     r'                return _assist_refuse\("canon_lite_assist_arming_failed"\)',
     "                pass",
     "test_assist_refuses_when_the_rendering_is_empty"),

    # ── GATE 3: the census fails the job ─────────────────────────────────
    ("census downgraded to a log line — mismatch still ships",
     STATIC,
     r'                _cl_fail_code = "canon_lite_assist_binding_mismatch"',
     "                pass",
     "test_one_chapter_with_a_foreign_canon_hash_fails_the_job"),
    ("census tolerates a chapter with no binding at all",
     STATIC,
     r'                _cl_fail_code = "canon_lite_assist_binding_missing"',
     "                pass",
     "test_missing_worker_hash_fails_the_job"),
    ("census stops checking the context digest",
     STATIC,
     r'                _cl_fail_code = "canon_lite_assist_context_mismatch"',
     "                pass",
     "test_a_worker_seeing_a_foreign_context_fails_the_job"),
    ("freeze violation demoted to advisory",
     STATIC,
     r'                _cl_fail_code = "canon_lite_assist_freeze_violation"',
     "                pass",
     "test_freeze_violation_fails_the_job"),
    ("verdict computed but never acted on",
     STATIC,
     r"    if _cl_fail_code:\n",
     "    if False:\n",
     "test_one_chapter_with_a_foreign_canon_hash_fails_the_job"),

    # ── GATE 4: resume + persistence ─────────────────────────────────────
    ("assist reuses checkpoints it cannot bind to this run",
     STATIC,
     r"                    _pre = \{\}",
     "                    pass",
     "test_assist_never_reuses_a_checkpoint"),
    ("binding persisted unconditionally — null on every non-assist job",
     NAPI,
     r"    if _cl_binding:\n        payload\[\"canon_lite_binding\"\] = _cl_binding",
     '    payload["canon_lite_binding"] = _cl_binding',
     "test_the_persisted_payload_gains_the_key_ONLY_when_assist_ran"),
    ("binding never reaches the persisted payload at all",
     NAPI,
     r"    _cl_binding = result\.get\(\"canon_lite_binding\"\)",
     "    _cl_binding = None",
     "test_the_persisted_payload_gains_the_key_ONLY_when_assist_ran"),

    # ── the behavioural split itself ─────────────────────────────────────
    ("shadow dragged into assist's fail-closed behaviour",
     STATIC,
     r"    _cl_assist = \(_cl_mode == \"assist\"\)",
     '    _cl_assist = (_cl_mode in ("assist", "shadow"))',
     "test_shadow_still_degrades_where_assist_refuses"),
]

BACKUPS = {p: p.read_bytes() for p in (STATIC, CANON, NAPI)}
ok = True
try:
    for label, target, pat, rep, test in BREAKS:
        for p, data in BACKUPS.items():        # always mutate a pristine tree
            p.write_bytes(data)
        broken, n = re.subn(pat, rep, target.read_text())
        if n != 1:
            print(f"!! PATTERN-MISS ({n}) for: {label}")
            ok = False
            continue
        target.write_text(broken)
        # 🔴 A SAME-SIZE MUTATION IS INVISIBLE TO PYTHON'S BYTECODE CACHE.
        #    Invalidation keys on (mtime_seconds, size); `= 2` -> `= 3` changes
        #    neither when the rewrite lands inside the same second, so the stale
        #    .pyc is reused and the mutation "survives" without ever running.
        #    Every same-size mutation in this table would report a false pass.
        for cache in WT.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"{TEST}::{test}", "-q", "--tb=no",
             "-p", "no:cacheprovider", "-p", "no:warnings"],
            cwd=str(WT), capture_output=True, text=True)
        caught = r.returncode != 0
        print(f"{'   caught  ' if caught else '!! SURVIVED'}  {label}  -> {test}")
        ok = ok and caught
finally:
    for p, data in BACKUPS.items():
        p.write_bytes(data)
    print("restored")
sys.exit(0 if ok else 1)
