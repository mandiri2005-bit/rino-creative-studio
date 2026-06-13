#!/usr/bin/env python3
"""
eval_gate.py — STEP 0.7 (skeleton only; logic added in Step 0.7)
==============================================================================
One command that runs the harness end to end and returns a PASS/FAIL with an
exit code. THIS is what you run after every later change (Steps 1–9): if it
exits non-zero, something regressed below threshold — don't merge.

FLOW
------------------------------------------------------------------------------
  1. run_eval      -> produce a fresh results/run_*.json
                      (or reuse the latest with --use-latest)
  2. score_retrieval -> recall@k vs frozen baseline
  3. score_output    -> 8.5 rubric per path
  4. compare each metric to its threshold (constants below), print actual vs
     threshold with PASS/FAIL per metric
  5. exit 0 if ALL pass, exit 1 if ANY fail

THRESHOLDS (edit these as you calibrate on the first baseline run)
------------------------------------------------------------------------------
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# import the scorers we already built (Steps 0.5 / 0.6) — don't reimplement
import importlib.util


def _load(modname: str):
    spec = importlib.util.spec_from_file_location(modname, HERE / f"{modname}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- gate thresholds — strategy (a): hard-gate STANDARD, ratchet RAG ---------
# Rationale (calibrated on baseline run 133537):
#   * STANDARD is the product's floor quality — it must NOT regress. Hard gate.
#   * RAG is currently WORSE than standard (6.52 vs 8.52) — grounding is broken
#     and Steps 5/6/7 are meant to fix it. So RAG is gated as a RATCHET: it may
#     be low, but it must not slide BELOW its frozen baseline (minus noise
#     tolerance). When you improve RAG, raise RAG_BASELINE_OVERALL to lock it in.
#   * recall is vs the frozen baseline (==1.0 at freeze). A deliberate reindex
#     (Step 7) will drop it; that's the signal to re-freeze (baseline_v2), not a
#     reason to revert. See eval/README.

MIN_RECALL_AT_10        = 0.85   # aggregate recall@10 vs baseline
MIN_RINO_RECALL_AT_10   = 0.85   # recall@10 on prefer_source=true entries only
MIN_STANDARD_OVERALL    = 8.0    # standard-path overall must stay >= this (hard floor)

# RAG ratchet: must not drop below (baseline - tolerance). Raise the baseline as
# RAG improves so each gain becomes the new floor.
RAG_BASELINE_OVERALL    = 6.52   # frozen RAG overall from baseline run 133537
RAG_TOLERANCE           = 0.30   # noise allowance for the non-deterministic judge
MIN_RAG_OVERALL         = RAG_BASELINE_OVERALL - RAG_TOLERANCE   # = 6.22


def _latest(results_dir: Path, prefix: str) -> Path | None:
    files = sorted(results_dir.glob(f"{prefix}_*.json"))
    return files[-1] if files else None


def load_existing_scores(results_dir: Path) -> tuple[dict, dict]:
    """Read the most recent retrieval_score_* and output_score_* WITHOUT re-running
    anything (0 API calls). For re-checking the gate on already-computed numbers."""
    rs = _latest(results_dir, "retrieval_score")
    os_ = _latest(results_dir, "output_score")
    if rs is None or os_ is None:
        missing = "retrieval_score_*" if rs is None else "output_score_*"
        raise SystemExit(f"[eval_gate] --use-scores needs existing score files; "
                         f"none found matching {missing} in {results_dir}. "
                         f"Run score_retrieval.py / score_output.py first, or drop --use-scores.")
    print(f"[eval_gate] using existing scores (0 calls):")
    print(f"  retrieval: {rs.name}")
    print(f"  output   : {os_.name}")
    return (json.loads(rs.read_text(encoding="utf-8")),
            json.loads(os_.read_text(encoding="utf-8")))


def run_pipeline(use_latest: bool, run_path: Path | None, baseline: Path,
                 k: int, judge_model: str | None, api_key: str) -> tuple[dict, dict]:
    """Score the latest (or a fresh) run with both scorers; return summaries."""
    sr = _load("score_retrieval")
    so = _load("score_output")
    results_dir = HERE / "results"

    if not use_latest:
        # full re-run requires the live pipeline; we import run_eval and execute it
        re = _load("run_eval")
        import asyncio, types
        ns = types.SimpleNamespace(
            golden=HERE / "golden_set.yaml", out_dir=results_dir,
            top_k=k, duration=2,
            model=judge_model or "gemini-2.5-flash", api_key=api_key, limit=None,
        )
        run_path = asyncio.run(re.run_all(ns))

    rp = run_path or sr.latest_run(results_dir)
    print(f"[eval_gate] scoring run: {rp.name}")

    retrieval = sr.score_run(rp, baseline, k)
    output = so.score_run(rp, judge_model or so.DEFAULT_JUDGE_MODEL, api_key)
    return retrieval, output


def _check(name: str, actual, threshold, comparator=">=") -> tuple[str, bool]:
    """Return (printable line, passed). Handles None actuals as FAIL."""
    if actual is None:
        return (f"  [FAIL] {name:<28} actual=  —    threshold {comparator} {threshold}", False)
    passed = actual >= threshold if comparator == ">=" else actual <= threshold
    tag = "PASS" if passed else "FAIL"
    return (f"  [{tag}] {name:<28} actual={actual:>5.3f}  threshold {comparator} {threshold}", passed)


def evaluate(retrieval_summary: dict, output_summary: dict) -> bool:
    """Compare summaries to thresholds; print per-metric PASS/FAIL; return all_pass."""
    agg = retrieval_summary.get("aggregate", {})
    rino = retrieval_summary.get("rino_aggregate", {})
    # recall key is dynamic (recall@{k}); pull whatever recall@N exists
    def _recall(d):
        for key in d:
            if key.startswith("recall@") and key != "recall@5":
                return d[key]
        return d.get("recall@5")

    rag = output_summary.get("rag", {})
    std = output_summary.get("standard", {})

    print("\n" + "=" * 72)
    print("  EVAL GATE — PASS/FAIL")
    print("=" * 72)

    fixture_ok = retrieval_summary.get("meta", {}).get("fixture_ok", True)
    if not fixture_ok:
        print("  [FAIL] fixture                 golden_set.yaml != baseline hash (INVALID)")

    lines, results = [], []
    for line, ok in [
        _check("recall@10 (all)",        _recall(agg),  MIN_RECALL_AT_10),
        _check("recall@10 (rino)",       _recall(rino), MIN_RINO_RECALL_AT_10),
        _check("output overall (std)",   std.get("overall"),  MIN_STANDARD_OVERALL),
        _check("output overall (rag)",   rag.get("overall"),  round(MIN_RAG_OVERALL, 3)),
    ]:
        lines.append(line); results.append(ok)

    for l in lines:
        print(l)

    all_pass = fixture_ok and all(results)
    print("-" * 72)
    print(f"  RAG ratchet: baseline={RAG_BASELINE_OVERALL}  tolerance=-{RAG_TOLERANCE}  "
          f"floor={MIN_RAG_OVERALL:.2f}  (raise baseline as RAG improves)")
    print(f"\n  RESULT: {'PASS — safe to merge' if all_pass else 'FAIL — do not merge; investigate'}")
    print("=" * 72)
    return all_pass


def parse_args() -> argparse.Namespace:
    import os
    p = argparse.ArgumentParser(description="Run the eval harness end to end; PASS/FAIL gate.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="re-run run_eval from scratch (needs API key + Qdrant + ~8min), "
                           "then re-score both halves.")
    mode.add_argument("--use-scores", action="store_true",
                      help="0 calls: re-check the gate on the most recent existing "
                           "retrieval_score_* / output_score_* files. Fastest.")
    # default (neither flag): score the latest existing RUN, re-judging output (~50 calls)
    p.add_argument("--run", type=Path, default=None,
                   help="specific run file to score (default: latest results/run_*.json)")
    p.add_argument("--baseline", type=Path, default=HERE / "baseline" / "baseline_v1.json",
                   help="frozen baseline (default: ./baseline/baseline_v1.json)")
    p.add_argument("--k", type=int, default=10, help="top-k for recall (default 10)")
    p.add_argument("--judge-model", default=os.environ.get("RAG_JUDGE_MODEL"),
                   help="judge model (default: score_output's DEFAULT_JUDGE_MODEL)")
    p.add_argument("--api-key", default=os.environ.get("LAOZHANG_API_KEY", ""),
                   help="LaoZhang API key (or set LAOZHANG_API_KEY)")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if args.use_scores:
        # no scoring, no run — just read what's already on disk
        retr, outp = load_existing_scores(HERE / "results")
    else:
        if not args.api_key:
            raise SystemExit("[eval_gate] no API key (set LAOZHANG_API_KEY or pass --api-key) — "
                             "the output judge needs it. (Use --use-scores to skip judging.)")
        retr, outp = run_pipeline(
            use_latest=not args.full, run_path=args.run, baseline=args.baseline,
            k=args.k, judge_model=args.judge_model, api_key=args.api_key,
        )

    all_pass = evaluate(retr, outp)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
