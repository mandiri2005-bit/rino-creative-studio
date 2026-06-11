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
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- gate thresholds — tune after the first baseline run (Step 0.7) ----------
MIN_RECALL_AT_10        = 0.85   # aggregate recall@10 vs baseline
MIN_RINO_RECALL_AT_10   = 0.85   # recall@10 on prefer_source=true entries only
MIN_OUTPUT_SCORE        = 8.0    # overall output score, EACH path, must stay >= this


def run_pipeline(use_latest: bool) -> tuple[dict, dict]:
    """Run run_eval (unless --use-latest), then both scorers; return summaries. (Step 0.7)"""
    raise NotImplementedError("wired in Step 0.7")


def evaluate(retrieval_summary: dict, output_summary: dict) -> bool:
    """Compare summaries to thresholds; print per-metric PASS/FAIL; return all_pass. (Step 0.7)"""
    raise NotImplementedError("wired in Step 0.7")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the eval harness end to end; PASS/FAIL gate.")
    p.add_argument("--use-latest", action="store_true",
                   help="skip run_eval; score the latest results/run_*.json instead")
    p.add_argument("--k", type=int, default=10, help="top-k for recall (default 10)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[eval_gate] skeleton — wiring lands in Step 0.7")
    print(f"  thresholds: recall@10>={MIN_RECALL_AT_10}  "
          f"rino_recall@10>={MIN_RINO_RECALL_AT_10}  output>={MIN_OUTPUT_SCORE}")
    print(f"  use_latest: {args.use_latest}")
    # retr, outp = run_pipeline(args.use_latest)
    # all_pass = evaluate(retr, outp)
    # sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
