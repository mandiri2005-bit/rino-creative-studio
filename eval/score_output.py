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
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# LaoZhang OpenAI-compatible endpoint — same client pattern rag_narration.py uses.
LAOZHANG_BASE_URL = "https://api.laozhang.ai/v1"

# Judge defaults to Claude Sonnet 4.6 (different family from the gemini generator,
# so no self-preference; non-thinking — scoring 1-10 doesn't need reasoning tokens).
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"

# Dimensions used if no project rubric file is found (see docstring).
RUBRIC_DIMENSIONS = [
    "factual_accuracy",   # only scored when passages were actually retrieved
    "style_fit",
    "coherence",
    "language_fluency",
    "overall",
]

# factual_accuracy is meaningless without source passages (17/25 entries run
# RAG-off → no passages). For those, we DROP this dimension rather than punish
# prose for not citing sources it was never given.
FACTUAL_DIM = "factual_accuracy"


def latest_run(results_dir: Path) -> Path:
    """Return the newest results/run_*.json."""
    runs = sorted(results_dir.glob("run_*.json"))
    if not runs:
        raise SystemExit(f"[score_output] no run_*.json in {results_dir}")
    return runs[-1]


# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------
def _build_judge_prompt(entry: dict, path_obj: dict, has_passages: bool) -> str:
    """Construct the judge instruction. Returns a single user-message string."""
    narration = (path_obj or {}).get("narration", "") or ""
    passages = (path_obj or {}).get("sources", []) or []

    dims = [d for d in RUBRIC_DIMENSIONS if d != FACTUAL_DIM or has_passages]
    dim_lines = {
        "factual_accuracy": "factual_accuracy: is every claim supported by the SOURCE PASSAGES below (not invented)?",
        "style_fit":        "style_fit: does it match the requested documentary style/register?",
        "coherence":        "coherence: structure, logical flow, no contradictions or repetition.",
        "language_fluency": "language_fluency: idiomatic and natural in the target language.",
        "overall":          "overall: holistic quality as documentary narration.",
    }
    rubric = "\n".join(f"  - {dim_lines[d]}" for d in dims)

    src_block = ""
    if has_passages:
        joined = "\n".join(f"  [{i+1}] {s}" for i, s in enumerate(passages)) or "  (none)"
        src_block = f"\nSOURCE PASSAGES (ground truth for factual_accuracy):\n{joined}\n"

    return (
        "You are a strict evaluator of documentary narration quality. Score the "
        "NARRATION on each dimension from 1 (poor) to 10 (excellent).\n\n"
        f"TOPIC: {entry.get('topic','')}\n"
        f"REQUESTED STYLE: {entry.get('style','')}\n"
        f"TARGET LANGUAGE: {entry.get('lang','')}\n"
        f"{src_block}\n"
        f"NARRATION TO SCORE:\n\"\"\"\n{narration}\n\"\"\"\n\n"
        "DIMENSIONS:\n"
        f"{rubric}\n\n"
        "Return ONLY a JSON object, no prose, no markdown fences, with exactly these "
        f"keys: {dims}. Each value an integer 1-10. Example: "
        + json.dumps({d: 8 for d in dims})
    )


def _parse_judge_json(text: str, expected_dims: list[str]) -> dict | None:
    """Defensive parse: strip fences, grab the first {...}, keep expected int keys."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    out = {}
    for d in expected_dims:
        v = obj.get(d)
        if isinstance(v, (int, float)):
            out[d] = float(v)
    return out or None


def judge_one(entry: dict, path_key: str, judge_model: str, api_key: str,
              client=None, max_retries: int = 2) -> dict:
    """LLM-judge one (entry, path) -> {dimension: score}. Errors captured, not raised."""
    path_obj = (entry.get("output") or {}).get(path_key)
    result = {"path": path_key, "scores": None, "error": None, "skipped_factual": False}

    if not path_obj or not (path_obj.get("narration") or "").strip():
        result["error"] = "no narration"
        return result

    # passages present only if the path actually used RAG with sources
    has_passages = bool(path_obj.get("sources")) and (path_obj.get("passages_retrieved", 0) or 0) > 0
    result["skipped_factual"] = not has_passages
    expected = [d for d in RUBRIC_DIMENSIONS if d != FACTUAL_DIM or has_passages]

    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=LAOZHANG_BASE_URL, timeout=120.0)

    prompt = _build_judge_prompt(entry, path_obj, has_passages)
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=judge_model,
                max_tokens=300,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.choices[0].message.content
            scores = _parse_judge_json(text, expected)
            if scores and "overall" in scores:
                result["scores"] = scores
                return result
            last_err = f"unparseable judge reply: {text[:120]!r}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        time.sleep(1.5 * (attempt + 1))
    result["error"] = last_err
    return result


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def _mean(vals: list) -> float | None:
    clean = [v for v in vals if v is not None]
    return round(statistics.mean(clean), 3) if clean else None


def _rollup(judged: list[dict]) -> dict:
    """Per-dimension + overall means for one path across entries with scores."""
    scored = [j["scores"] for j in judged if j.get("scores")]
    out = {"n_scored": len(scored), "n_error": sum(1 for j in judged if j.get("error"))}
    for d in RUBRIC_DIMENSIONS:
        out[d] = _mean([s.get(d) for s in scored])
    out["n_factual_skipped"] = sum(1 for j in judged if j.get("skipped_factual"))
    return out


def score_run(run_path: Path, judge_model: str, api_key: str,
              limit: int | None = None) -> dict:
    """Judge every entry on both paths; write output_score_*.json; return summary."""
    run = json.loads(run_path.read_text(encoding="utf-8"))
    entries = [e for e in run.get("entries", []) if not e.get("error")]
    if limit:
        entries = entries[:limit]

    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=LAOZHANG_BASE_URL, timeout=120.0)

    print(f"[score_output] judging {len(entries)} entries x2 paths "
          f"({len(entries)*2} calls) | judge={judge_model}")

    per_entry, rag_judged, std_judged = [], [], []
    for i, e in enumerate(entries, 1):
        print(f"  [{i:>2}/{len(entries)}] {e['id']:<34} ", end="", flush=True)
        rag = judge_one(e, "rag", judge_model, api_key, client=client)
        std = judge_one(e, "standard", judge_model, api_key, client=client)
        rag_judged.append(rag)
        std_judged.append(std)
        per_entry.append({"id": e["id"], "style": e.get("style"),
                          "lang": e.get("lang"), "prefer_source": e.get("prefer_source"),
                          "rag": rag, "standard": std})

        def _o(j):
            return f"{j['scores']['overall']:.0f}" if j.get("scores") else ("ERR" if j.get("error") else "—")
        flags = "(no-passage rag)" if rag.get("skipped_factual") else ""
        print(f"rag.overall={_o(rag):>3}  std.overall={_o(std):>3}  {flags}")

    summary = {
        "meta": {
            "scored_at":   datetime.now().isoformat(timespec="seconds"),
            "run":         run_path.name,
            "judge_model": judge_model,
            "n_entries":   len(entries),
            "dimensions":  RUBRIC_DIMENSIONS,
        },
        "rag":      _rollup(rag_judged),
        "standard": _rollup(std_judged),
        "per_entry": per_entry,
    }

    out_dir = run_path.parent
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"output_score_{stamp}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["meta"]["out_path"] = str(out_path)
    return summary


def _fmt(v) -> str:
    return "  — " if v is None else f"{v:.2f}"


def print_table(summary: dict) -> None:
    m = summary["meta"]
    rag, std = summary["rag"], summary["standard"]
    print(f"\n[score_output] run={m['run']}  judge={m['judge_model']}")
    print(f"  entries judged : {m['n_entries']}  (each scored on rag + standard)")
    print(f"\n  {'dimension':<20} {'RAG':>7} {'STANDARD':>9}")
    for d in RUBRIC_DIMENSIONS:
        print(f"  {d:<20} {_fmt(rag.get(d)):>7} {_fmt(std.get(d)):>9}")
    print(f"  {'(n scored)':<20} {rag['n_scored']:>7} {std['n_scored']:>9}")
    print(f"  {'(errors)':<20} {rag['n_error']:>7} {std['n_error']:>9}")
    if rag.get("n_factual_skipped") or std.get("n_factual_skipped"):
        print(f"  {'(factual skipped)':<20} {rag['n_factual_skipped']:>7} {std['n_factual_skipped']:>9}"
              f"   # no-passage entries: factual_accuracy not scored")
    print(f"\n  wrote {m['out_path']}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score output quality (LLM-judge), per path.")
    p.add_argument("--run", type=Path, default=None,
                   help="run file to score (default: latest results/run_*.json)")
    p.add_argument("--results-dir", type=Path, default=HERE / "results",
                   help="results dir to search for the latest run (default: ./results)")
    p.add_argument("--judge-model", default=os.environ.get("RAG_JUDGE_MODEL", DEFAULT_JUDGE_MODEL),
                   help=f"judge model (default {DEFAULT_JUDGE_MODEL} / RAG_JUDGE_MODEL)")
    p.add_argument("--api-key", default=os.environ.get("LAOZHANG_API_KEY", ""),
                   help="LaoZhang API key (or set LAOZHANG_API_KEY)")
    p.add_argument("--limit", type=int, default=None,
                   help="only judge the first N entries (smoke test before the full 50 calls)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("[score_output] no API key (set LAOZHANG_API_KEY or pass --api-key).")
    run_path = args.run or latest_run(args.results_dir)
    summary = score_run(run_path, args.judge_model, args.api_key, limit=args.limit)
    print_table(summary)


if __name__ == "__main__":
    main()
