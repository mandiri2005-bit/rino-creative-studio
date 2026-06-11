#!/usr/bin/env python3
"""
score_output.py — STEP 0.6 (skeleton only; logic added in Step 0.6)
==============================================================================
Scores OUTPUT (narration) quality with the 8.5 rubric run as an LLM-judge. This
is the other half of the gate, kept DELIBERATELY SEPARATE from retrieval
scoring: good retrieval can still yield bad prose, and vice versa — one number
would hide which half regressed.

INPUT
------------------------------------------------------------------------------
  - a run file : results/run_*.json (default: latest) — has both output paths
    per entry (rag / standard).

PER ENTRY, PER PATH (rag, standard)
------------------------------------------------------------------------------
Send (topic, style, lang, retrieved passages, narration) to a judge model and
get per-dimension scores back as JSON. Reuse the existing 8.5 rubric if it lives
in a project file; otherwise use these dimensions, each 1–10:
  - factual_accuracy   (consistent with the retrieved passages / not invented)
  - style_fit          (matches the requested registry style)
  - coherence          (structure, flow, no contradictions)
  - language_fluency   (idiomatic in the target language, esp. Indonesian)
  - overall            (holistic 1–10)

The judge call reuses the OpenAI-compatible client already in the project
(LaoZhang base_url). Prompt the judge to return ONLY JSON; parse defensively.

AGGREGATE
------------------------------------------------------------------------------
  - mean per-dimension + mean overall, computed PER PATH (rag vs standard) so
    the two are directly comparable
  - keep rag and standard side by side; do not average them together

OUTPUT
------------------------------------------------------------------------------
  - results/output_score_YYYYMMDD_HHMMSS.json (machine-readable)
  - a compact per-path table to stdout
  - written to a SEPARATE file from retrieval scores (different thresholds)

Usage:
    python score_output.py
    python score_output.py --run results/run_20260611_120000.json
    python score_output.py --judge-model gemini-2.5-flash
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Dimensions used if no project rubric file is found (see docstring).
RUBRIC_DIMENSIONS = [
    "factual_accuracy",
    "style_fit",
    "coherence",
    "language_fluency",
    "overall",
]


def latest_run(results_dir: Path) -> Path:
    """Return the newest results/run_*.json. (Step 0.6)"""
    raise NotImplementedError("wired in Step 0.6")


def judge_one(entry: dict, path_key: str, judge_model: str, api_key: str) -> dict:
    """LLM-judge one (entry, path) -> per-dimension scores. (Step 0.6)"""
    raise NotImplementedError("wired in Step 0.6")


def score_run(run_path: Path, judge_model: str, api_key: str) -> dict:
    """Score every entry/path; write output_score_*.json; return summary. (Step 0.6)"""
    raise NotImplementedError("wired in Step 0.6")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score output quality (8.5 rubric LLM-judge), per path.")
    p.add_argument("--run", type=Path, default=None,
                   help="run file to score (default: latest results/run_*.json)")
    p.add_argument("--results-dir", type=Path, default=HERE / "results",
                   help="results dir to search for the latest run (default: ./results)")
    p.add_argument("--judge-model", default=os.environ.get("RAG_JUDGE_MODEL", "gemini-2.5-flash"),
                   help="judge model (default gemini-2.5-flash / RAG_JUDGE_MODEL)")
    p.add_argument("--api-key", default=os.environ.get("LAOZHANG_API_KEY", ""),
                   help="LaoZhang API key (or set LAOZHANG_API_KEY)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[score_output] skeleton — wiring lands in Step 0.6")
    print(f"  run        : {args.run or '(latest in ' + str(args.results_dir) + ')'}")
    print(f"  judge      : {args.judge_model}")
    print(f"  dimensions : {', '.join(RUBRIC_DIMENSIONS)}")
    # summary = score_run(args.run or latest_run(args.results_dir), args.judge_model, args.api_key)


if __name__ == "__main__":
    main()
