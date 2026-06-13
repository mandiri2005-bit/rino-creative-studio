#!/usr/bin/env python3
"""
freeze_baseline.py — STEP 0.4
==============================================================================
Takes ONE clean run from results/ and freezes its RETRIEVAL half into a
read-only baseline that later runs are scored against (recall@k in 0.5). This
is the one-shot window: after this, retrieval may change freely — the baseline
stays as the permanent point of comparison.

WHAT IT CAPTURES (retrieval only — NOT narration/output)
------------------------------------------------------------------------------
Per golden-set entry:  id, prefer_source, and the ordered passage list as
[{passage_id, score}, ...]. (source is dropped here — the live capture records
it as null; 0.5 splits the rino roll-up by the entry-level prefer_source flag
instead, so nothing downstream depends on per-passage source.)

METADATA (so a stale comparison is detectable)
------------------------------------------------------------------------------
created_at, source run file, qdrant collection, embed model, top_k, n_entries,
and the SHA256 of golden_set.yaml copied from the run's meta. 0.5 re-checks that
hash against the live golden_set.yaml; if they differ, the fixture moved and the
comparison is invalid.

AFTER WRITING: chmod 444 (read-only). Re-freezing requires chmod +w first — a
deliberate friction so the baseline isn't clobbered by accident.

Usage:
    python freeze_baseline.py                       # newest run in results/
    python freeze_baseline.py --run results/run_20260613_131448.json
    python freeze_baseline.py --out baseline/baseline_v1.json --force
"""

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def latest_run(results_dir: Path) -> Path:
    runs = sorted(results_dir.glob("run_*.json"))
    if not runs:
        raise SystemExit(f"[freeze] no run_*.json in {results_dir}")
    return runs[-1]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_baseline(run_path: Path) -> dict:
    """Extract the retrieval snapshot + metadata from a run file."""
    run = json.loads(run_path.read_text(encoding="utf-8"))
    meta = run.get("meta", {})
    entries = run.get("entries", [])
    if not entries:
        raise SystemExit(f"[freeze] run has no entries: {run_path}")

    n_err = sum(1 for e in entries if e.get("error"))
    if n_err:
        raise SystemExit(
            f"[freeze] run has {n_err} errored entrie(s) — refusing to freeze a "
            f"baseline with holes. Re-run cleanly first: {run_path}"
        )

    frozen = []
    for e in entries:
        passages = [
            {"passage_id": p.get("passage_id"), "score": p.get("score")}
            for p in e.get("retrieved", [])
        ]
        frozen.append({
            "id":            e["id"],
            "prefer_source": e.get("prefer_source", False),
            "passages":      passages,
        })

    return {
        "meta": {
            "created_at":    datetime.now().isoformat(timespec="seconds"),
            "frozen_from":   run_path.name,
            "run_created_at": meta.get("created_at"),
            "collection":    meta.get("collection"),
            "embed_model":   meta.get("embed_model"),
            "top_k":         meta.get("top_k"),
            "n_entries":     len(frozen),
            "golden_set":    meta.get("golden_set"),
            "golden_sha256": meta.get("golden_sha256"),
        },
        "entries": frozen,
    }


def write_readonly(baseline: dict, out_path: Path, force: bool) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        if not force:
            raise SystemExit(
                f"[freeze] {out_path} already exists. This is the frozen baseline; "
                f"pass --force to overwrite (and `chmod +w` it first if read-only)."
            )
        # make writable so we can overwrite a previously-frozen (444) file
        out_path.chmod(stat.S_IWUSR | stat.S_IRUSR)

    out_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    # read-only: r--r--r--
    out_path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Freeze a retrieval baseline from a run (read-only).")
    p.add_argument("--run", type=Path, default=None,
                   help="run file to freeze (default: newest results/run_*.json)")
    p.add_argument("--results-dir", type=Path, default=HERE / "results",
                   help="dir to search for the latest run (default: ./results)")
    p.add_argument("--out", type=Path, default=HERE / "baseline" / "baseline_v1.json",
                   help="baseline output path (default: ./baseline/baseline_v1.json)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing baseline (use deliberately)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    run_path = args.run or latest_run(args.results_dir)
    print(f"[freeze] reading run : {run_path}")

    baseline = build_baseline(run_path)
    write_readonly(baseline, args.out, args.force)

    m = baseline["meta"]
    print(f"[freeze] wrote baseline : {args.out}  (read-only)")
    print(f"  entries        : {m['n_entries']}")
    print(f"  collection     : {m['collection']}")
    print(f"  embed_model    : {m['embed_model']}  top_k={m['top_k']}")
    print(f"  golden_set     : {m['golden_set']}")
    print(f"  golden_sha256  : {m['golden_sha256']}")
    n_rino = sum(1 for e in baseline["entries"] if e["prefer_source"])
    print(f"  prefer_source  : {n_rino} entrie(s) flagged rino")
    print(f"\n  next: ls -l {args.out}   # confirm -r--r--r--")


if __name__ == "__main__":
    main()
