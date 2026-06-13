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
import hashlib
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# golden_set.yaml lives next to this file (eval/). The baseline stored its
# SHA256 at freeze time; verify_fixture re-hashes the live file and compares.
GOLDEN_PATH = HERE / "golden_set.yaml"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def latest_run(results_dir: Path) -> Path:
    """Return the newest results/run_*.json."""
    runs = sorted(results_dir.glob("run_*.json"))
    if not runs:
        raise SystemExit(f"[score_retrieval] no run_*.json in {results_dir}")
    return runs[-1]


def _ids(passages: list[dict], k: int | None = None) -> list[str]:
    """Ordered passage_id list, optionally truncated to top-k."""
    out = [p.get("passage_id") for p in passages]
    return out[:k] if k is not None else out


def _scores(passages: list[dict]) -> dict[str, float]:
    """passage_id -> score map (skip ids/scores that are None)."""
    return {
        p["passage_id"]: p["score"]
        for p in passages
        if p.get("passage_id") is not None and p.get("score") is not None
    }


def verify_fixture(baseline: dict) -> bool:
    """True if the live golden_set.yaml hash matches the one frozen in baseline.
    A mismatch means the fixture moved and recall numbers are not comparable."""
    frozen = baseline.get("meta", {}).get("golden_sha256")
    if not frozen:
        print("[score_retrieval] WARNING: baseline has no golden_sha256 — cannot verify fixture.")
        return False
    if not GOLDEN_PATH.exists():
        print(f"[score_retrieval] WARNING: {GOLDEN_PATH} not found — cannot verify fixture.")
        return False
    live = hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest()
    return live == frozen


def score_entry(run_entry: dict, base_entry: dict, k: int) -> dict:
    """recall@5, recall@k, set-overlap (Jaccard), drift for one entry.

    Recall is SET-based: a baseline passage counts as 'recalled' if it appears
    anywhere in the run's top-k, regardless of position. Rank shifts are tracked
    separately by `drift` (mean abs score delta over shared ids)."""
    run_pass = run_entry.get("retrieved", [])     # run uses key 'retrieved'
    base_pass = base_entry.get("passages", [])     # baseline uses key 'passages'

    def recall_at(n: int) -> float | None:
        base_top = set(_ids(base_pass, n))
        if not base_top:
            return None
        run_top = set(_ids(run_pass, n))
        return len(base_top & run_top) / len(base_top)

    # full set-overlap (Jaccard) over everything retrieved, not just top-k
    base_all = set(_ids(base_pass))
    run_all = set(_ids(run_pass))
    union = base_all | run_all
    overlap = (len(base_all & run_all) / len(union)) if union else None

    # drift: mean |Δscore| for passages present in BOTH (shared ids)
    bs, rs = _scores(base_pass), _scores(run_pass)
    shared = set(bs) & set(rs)
    drift = (statistics.mean(abs(bs[i] - rs[i]) for i in shared)) if shared else None

    return {
        "id":         run_entry.get("id"),
        "recall@5":   recall_at(5),
        f"recall@{k}": recall_at(k),
        "overlap":    overlap,
        "drift":      drift,
        "n_base":     len(base_pass),
        "n_run":      len(run_pass),
        "n_shared":   len(shared),
    }


def _mean(vals: list) -> float | None:
    clean = [v for v in vals if v is not None]
    return statistics.mean(clean) if clean else None


def score_run(run_path: Path, baseline_path: Path, k: int) -> dict:
    """Score a whole run vs baseline; write retrieval_score_*.json; return summary."""
    run = json.loads(run_path.read_text(encoding="utf-8"))
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    fixture_ok = verify_fixture(baseline)
    if not fixture_ok:
        print("\n" + "!" * 72)
        print("!  FIXTURE MISMATCH: live golden_set.yaml != the one frozen in baseline.")
        print("!  Recall numbers below are NOT a valid comparison. Investigate before")
        print("!  trusting anything. (Did golden_set.yaml change after the freeze?)")
        print("!" * 72 + "\n")

    base_by_id = {e["id"]: e for e in baseline.get("entries", [])}

    rows, missing = [], []
    for e in run.get("entries", []):
        if e.get("error"):
            continue  # errored entries have no retrieval to score
        base_e = base_by_id.get(e["id"])
        if base_e is None:
            missing.append(e["id"])
            continue
        rows.append(score_entry(e, base_e, k))

    def rollup(subset: list[dict]) -> dict:
        return {
            "n":          len(subset),
            "recall@5":   _mean([r["recall@5"] for r in subset]),
            f"recall@{k}": _mean([r[f"recall@{k}"] for r in subset]),
            "overlap":    _mean([r["overlap"] for r in subset]),
            "drift":      _mean([r["drift"] for r in subset]),
        }

    # rino roll-up uses the entry-level prefer_source flag from the baseline,
    # NOT per-passage source (corpus passages can be source=rino too).
    rino_ids = {e["id"] for e in baseline.get("entries", []) if e.get("prefer_source")}
    rino_rows = [r for r in rows if r["id"] in rino_ids]

    summary = {
        "meta": {
            "scored_at":      datetime.now().isoformat(timespec="seconds"),
            "run":            run_path.name,
            "baseline":       baseline_path.name,
            "k":              k,
            "fixture_ok":     fixture_ok,
            "n_scored":       len(rows),
            "n_missing_in_baseline": len(missing),
            "missing_ids":    missing,
        },
        "aggregate":      rollup(rows),
        "rino_aggregate": rollup(rino_rows),
        "per_entry":      rows,
    }

    out_dir = run_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"retrieval_score_{stamp}.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary["meta"]["out_path"] = str(out_path)
    return summary


def _fmt(v) -> str:
    return "  —  " if v is None else f"{v:.3f}"


def print_table(summary: dict, k: int) -> None:
    m = summary["meta"]
    agg, rino = summary["aggregate"], summary["rino_aggregate"]
    print(f"\n[score_retrieval] run={m['run']}  vs  baseline={m['baseline']}  (k={k})")
    print(f"  fixture verified : {'yes' if m['fixture_ok'] else 'NO — numbers invalid'}")
    print(f"  entries scored   : {m['n_scored']}"
          + (f"  (missing in baseline: {m['n_missing_in_baseline']})" if m['n_missing_in_baseline'] else ""))

    print(f"\n  {'':<34} {'rec@5':>7} {'rec@'+str(k):>7} {'overlap':>8} {'drift':>7}")
    print(f"  {'ALL ('+str(agg['n'])+')':<34} {_fmt(agg['recall@5']):>7} "
          f"{_fmt(agg[f'recall@{k}']):>7} {_fmt(agg['overlap']):>8} {_fmt(agg['drift']):>7}")
    print(f"  {'RINO ('+str(rino['n'])+', prefer_source)':<34} {_fmt(rino['recall@5']):>7} "
          f"{_fmt(rino[f'recall@{k}']):>7} {_fmt(rino['overlap']):>8} {_fmt(rino['drift']):>7}")

    # per-entry, lowest recall@k first so regressions surface at the top
    rows = sorted(summary["per_entry"],
                  key=lambda r: (r[f"recall@{k}"] if r[f"recall@{k}"] is not None else 1.0))
    print(f"\n  per-entry (worst recall@{k} first):")
    for r in rows:
        print(f"    {r['id']:<34} {_fmt(r['recall@5']):>7} {_fmt(r[f'recall@{k}']):>7} "
              f"{_fmt(r['overlap']):>8} {_fmt(r['drift']):>7}  "
              f"(base={r['n_base']} run={r['n_run']} shared={r['n_shared']})")
    print(f"\n  wrote {m['out_path']}")


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
    run_path = args.run or latest_run(args.results_dir)
    summary = score_run(run_path, args.baseline, args.k)
    print_table(summary, args.k)


if __name__ == "__main__":
    main()
