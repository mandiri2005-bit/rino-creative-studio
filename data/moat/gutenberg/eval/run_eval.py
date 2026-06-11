#!/usr/bin/env python3
"""
run_eval.py — STEP 0.3 (skeleton only; logic added in Step 0.3)
==============================================================================
Runs the frozen golden set through the LIVE retrieval + generation paths and
dumps the raw results to a timestamped JSON. NO scoring here — this only
*captures* what the system does today, so the scorers (0.5, 0.6) and the
baseline freeze (0.4) have something to read.

WHAT IT WILL DO (per entry in golden_set.yaml)
------------------------------------------------------------------------------
1. RETRIEVAL CAPTURE — call the live retrieval path and record the passages it
   returns (id + score + source), which is what recall@k needs.

   Use rag_narration.get_narration_context(...) — it returns the full passage
   dicts under result["passages"], each with passage_id / score / source /
   style_label / quality_score:

       from rag_narration import get_narration_context
       from style_rag_config import get_style_config
       cfg = get_style_config(entry["style"])
       ctx = await get_narration_context(
           topic=entry["topic"],
           style=cfg["style_filter"],
           structure=cfg["structure_filter"],
           min_quality=cfg["min_quality"],
           top_k=TOP_K,                       # widen vs prod (e.g. 10) for recall@k
           query_instruction=cfg["query_instruction"],
           prefer_source="rino" if entry["prefer_source"] else None,
           min_score=MIN_SCORE,               # 0.55 default (RAG_MIN_SCORE)
       )
       retrieved = ctx["passages"]            # list of dicts w/ passage_id, score

   NOTE: compare_rag_vs_standard() / generate_rag_narration() return only
   `sources` (title strings) + `passages_retrieved` (count) — NOT passage ids.
   So retrieval IDs/scores MUST be captured via get_narration_context (above)
   or search_passages directly, not from the generation return value.

2. OUTPUT CAPTURE — generate narration for the SAME entry on both paths so 0.6
   can score them. Reuse the existing A/B helper:

       from rag_narration import compare_rag_vs_standard
       out = await compare_rag_vs_standard(
           topic=entry["topic"], style=entry["style"],
           language=entry["lang"], model=MODEL, api_key=API_KEY,
       )
       # out["rag"]["narration"], out["standard"]["narration"]

   (Path A = RAG-on, Path B = standard. If/when a separate Google-native path
   is wired, add it here as a third output — keep each path's text separate.)

3. WRITE — dump everything to results/run_YYYYMMDD_HHMMSS.json:
       { "meta": {...}, "entries": [ {id, topic, style, lang, prefer_source,
         "retrieved": [{passage_id, score, source}, ...],
         "output": {"rag": "...", "standard": "..."} }, ... ] }

Reuse the live functions — do NOT reimplement retrieval or generation.

Usage:
    python run_eval.py
    python run_eval.py --golden golden_set.yaml --top-k 10 --out-dir results
    python run_eval.py --limit 3            # smoke test on first 3 entries
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --- live pipeline imports (wired in Step 0.3) -------------------------------
# Make the project importable, then import the real entrypoints.
# sys.path.insert(0, str(HERE.parent))
# from rag_narration import get_narration_context, compare_rag_vs_standard
# from style_rag_config import get_style_config


def load_golden(path: Path) -> list[dict]:
    """Load + validate golden_set.yaml -> list of entry dicts. (Step 0.3)"""
    raise NotImplementedError("wired in Step 0.3")


async def run_entry(entry: dict, top_k: int, min_score: float,
                    model: str, api_key: str) -> dict:
    """Retrieval capture + dual-path output for one entry. (Step 0.3)"""
    raise NotImplementedError("wired in Step 0.3")


async def run_all(args: argparse.Namespace) -> Path:
    """Iterate the golden set, capture each entry, write run_*.json. (Step 0.3)"""
    raise NotImplementedError("wired in Step 0.3")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run golden set through live RAG paths (capture only).")
    p.add_argument("--golden", type=Path, default=HERE / "golden_set.yaml",
                   help="path to golden_set.yaml (default: ./golden_set.yaml)")
    p.add_argument("--out-dir", type=Path, default=HERE / "results",
                   help="where to write run_*.json (default: ./results)")
    p.add_argument("--top-k", type=int, default=10,
                   help="passages to retrieve per entry; widen vs prod for recall@k (default 10)")
    p.add_argument("--min-score", type=float,
                   default=float(os.environ.get("RAG_MIN_SCORE", "0.55")),
                   help="retrieval score gate (default 0.55 / RAG_MIN_SCORE)")
    p.add_argument("--model", default=os.environ.get("RAG_DEFAULT_MODEL", "gemini-2.5-flash"),
                   help="generation model (default gemini-2.5-flash / RAG_DEFAULT_MODEL)")
    p.add_argument("--api-key", default=os.environ.get("LAOZHANG_API_KEY", ""),
                   help="LaoZhang API key (or set LAOZHANG_API_KEY)")
    p.add_argument("--limit", type=int, default=None,
                   help="only run the first N entries (smoke test)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(f"[run_eval] skeleton — wiring lands in Step 0.3")
    print(f"  golden   : {args.golden}")
    print(f"  out-dir  : {args.out_dir}")
    print(f"  top_k    : {args.top_k}   min_score: {args.min_score}")
    print(f"  model    : {args.model}")
    # out_path = asyncio.run(run_all(args))
    # print(f"  wrote    : {out_path}")


if __name__ == "__main__":
    main()
