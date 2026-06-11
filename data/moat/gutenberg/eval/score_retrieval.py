#!/usr/bin/env python3
"""
score_retrieval.py — STEP 0.5 (skeleton only; logic added in Step 0.5)
==============================================================================
Scores RETRIEVAL quality of a run against the FROZEN baseline. This is one half
of the eval gate; output quality is scored separately in score_output.py. Keep
them separate so a regression has a clear address ("recall dropped" vs "the
model got worse").

INPUTS
------------------------------------------------------------------------------
  - a run file        : results/run_*.json   (default: latest)
  - the baseline      : baseline/baseline_v1.json   (frozen in Step 0.4)

FIRST: verify the golden_set.yaml SHA256 stored in the baseline matches the
current golden_set.yaml. If they differ, the fixture changed and the comparison
is INVALID — print a loud warning and refuse to certify the numbers.

PER ENTRY (compare run vs baseline passage-id lists)
------------------------------------------------------------------------------
  - recall@5   : fraction of baseline top-5 ids still present in run top-5
  - recall@10  : fraction of baseline top-10 ids still present in run top-10
  - overlap    : Jaccard / set-overlap of retrieved ids (run vs baseline)
  - drift      : mean abs delta of `score` for ids present in both

AGGREGATE
------------------------------------------------------------------------------
  - mean recall@5 / recall@10 / overlap / drift across all entries
  - a SEPARATE roll-up restricted to entries where prefer_source == true
    (so the rino path is tracked on its own — proprietary content must keep
    ranking first)

OUTPUT
------------------------------------------------------------------------------
  - results/retrieval_score_YYYYMMDD_HHMMSS.json (machine-readable)
  - a compact table to stdout

SANITY: scoring the exact run used to freeze the baseline must give recall≈1.0.

Usage:
    python score_retrieval.py
    python score_retrieval.py --run results/run_20260611_120000.json
    python score_retrieval.py --baseline baseline/baseline_v1.json --k 10
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def latest_run(results_dir: Path) -> Path:
    """Return the newest results/run_*.json. (Step 0.5)"""
    raise NotImplementedError("wired in Step 0.5")


def verify_fixture(run: dict, baseline: dict) -> bool:
    """Check golden_set SHA256 in baseline matches current fixture. (Step 0.5)"""
    raise NotImplementedError("wired in Step 0.5")


def score_entry(run_entry: dict, base_entry: dict, k: int) -> dict:
    """recall@5, recall@10, overlap, drift for one entry. (Step 0.5)"""
    raise NotImplementedError("wired in Step 0.5")


def score_run(run_path: Path, baseline_path: Path, k: int) -> dict:
    """Score a whole run; write retrieval_score_*.json; return summary. (Step 0.5)"""
    raise NotImplementedError("wired in Step 0.5")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score retrieval (recall@k) vs frozen baseline.")
    p.add_argument("--run", type=Path, default=None,
                   help="run file to score (default: latest results/run_*.json)")
    p.add_argument("--baseline", type=Path, default=HERE / "baseline" / "baseline_v1.json",
                   help="frozen baseline (default: ./baseline/baseline_v1.json)")
    p.add_argument("--results-dir", type=Path, default=HERE / "results",
                   help="results dir to search for the latest run (default: ./results)")
    p.add_argument("--k", type=int, default=10, help="top-k for recall (default 10)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[score_retrieval] skeleton — wiring lands in Step 0.5")
    print(f"  run      : {args.run or '(latest in ' + str(args.results_dir) + ')'}")
    print(f"  baseline : {args.baseline}")
    print(f"  k        : {args.k}")
    # summary = score_run(args.run or latest_run(args.results_dir), args.baseline, args.k)


if __name__ == "__main__":
    main()
