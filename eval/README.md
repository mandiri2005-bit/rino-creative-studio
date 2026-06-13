# eval/ — RAG evaluation gate

A measurement harness for the Rino Creative Studio documentary-narration RAG
pipeline. It freezes a fixed test set + a retrieval baseline, then scores every
later change so regressions surface as numbers instead of vibes.

**The whole point:** before any change to retrieval, the registry, or generation
quality, run the gate and record the numbers. Make the change. Run the gate
again. If it FAILs, don't merge. This is what keeps Steps 1–9 from quietly
breaking things.

---

## Quick start

```bash
# fastest: re-check the gate on the most recent score files (0 API calls)
python eval/eval_gate.py --use-scores

# default: score the latest existing RUN, re-judging output (~50 judge calls)
LAOZHANG_API_KEY=sk-... python eval/eval_gate.py

# full: re-run the 25-entry golden set through the live pipeline, then score
#       (needs Qdrant up + API key + ~8 min). Only when retrieval/generation changed.
LAOZHANG_API_KEY=sk-... python eval/eval_gate.py --full
```

Exit code `0` = PASS, `1` = FAIL. `echo $?` to check in scripts.

---

## The pieces

| file | what it does | API calls |
|------|--------------|-----------|
| `golden_set.yaml` | 25 frozen documentary topics (15 `id`, 10 `en`, 6 `prefer_source` rino). **Do not edit** — its SHA256 is baked into the baseline. | — |
| `check_golden_set.py` | validates golden set invariants (counts, labels, language split, prefer_source rules). | — |
| `run_eval.py` | runs the golden set through live retrieval + both generation paths (RAG-on / standard), dumps `results/run_*.json`. **Capture only, no scoring.** | 50 (gen) |
| `freeze_baseline.py` | snapshots a run's retrieval into read-only `baseline/baseline_v1.json` + golden hash. One-shot. | — |
| `score_retrieval.py` | recall@k + drift vs the frozen baseline. Deterministic. | — |
| `score_output.py` | LLM-judge (Claude Sonnet 4.6) scores narration quality per path. | 50 (judge) |
| `eval_gate.py` | runs the above, compares to thresholds, prints PASS/FAIL, exits 0/1. | 0–100 |

The live pipeline (`qdrant_index_v2.py`, `rag_narration.py`, `style_rag_config.py`)
lives in `data/moat/gutenberg/`; `run_eval.py` adds it to `sys.path`
(override with env `RINO_PIPELINE_DIR` if the layout changes).

---

## Thresholds (in `eval_gate.py`, edit as you calibrate)

```
MIN_RECALL_AT_10      = 0.85   # recall@10 vs baseline, aggregate
MIN_RINO_RECALL_AT_10 = 0.85   # recall@10 on the 6 prefer_source entries
MIN_STANDARD_OVERALL  = 8.0    # standard-path quality — HARD floor, must not regress
RAG_BASELINE_OVERALL  = 6.52   # frozen RAG quality; RAG must not slide below this...
RAG_TOLERANCE         = 0.30   # ...minus this noise allowance -> floor 6.22
```

**Why standard is a hard gate but RAG is a ratchet.** At baseline (run 133537)
the RAG path scored *worse* than the no-RAG path (overall 6.52 vs 8.52) —
retrieval grounding is currently broken (see findings). So:

- **standard** is the product's floor quality. It must never drop below 8.0.
- **RAG** is a feature under repair. It's allowed to be low, but it must not get
  *worse* than its baseline. As Steps 5/6/7 improve RAG, **raise
  `RAG_BASELINE_OVERALL`** so each gain locks in as the new floor.

A green gate does **not** mean RAG is good — it means RAG hasn't regressed. The
goal of later steps is to make RAG *beat* standard, then ratchet the floor up.

---

## Reading the scores

- **`results/retrieval_score_*.json`** — per-entry recall@5 / recall@10 / overlap
  / drift, plus an aggregate and a separate `rino_aggregate` (the 6 prefer_source
  entries, tracked on their own). recall is SET-based (passage present in top-k,
  order-independent); drift tracks score movement separately.
- **`results/output_score_*.json`** — per-entry, per-path (rag / standard)
  dimension scores from the judge: factual_accuracy, style_fit, coherence,
  language_fluency, overall. `factual_accuracy` is **skipped** for entries that
  ran without passages (RAG-off) — you can't grade fidelity to sources that
  weren't supplied.

Split output scores by language to see whether a regression is `id`- or
`en`-specific (the per_entry rows carry `lang`).

---

## SOP — run this after every retrieval / registry / quality change

1. **Before** the change: `python eval/eval_gate.py --use-scores` — note the numbers.
2. Make the change (one thing per step).
3. **After**: re-score. If you changed *generation/quality only*, default mode
   (re-judge) is enough. If you changed *retrieval/registry*, use `--full` so a
   fresh run is captured first.
4. Gate PASS → safe to merge. Gate FAIL → a metric dropped below threshold.
   Fix it or justify it before merging. **Do not merge a regression.**

### When a FAIL is expected (deliberate retrieval change)

A deliberate reindex / hybrid retrieval change (Step 7) **will** drop recall
below 1.0 — the baseline was frozen on the old retrieval. That's the signal to
**re-freeze a new baseline**, not to revert:

```bash
# only after you've confirmed the new retrieval is genuinely better
LAOZHANG_API_KEY=sk-... python eval/run_eval.py            # fresh run on new retrieval
python eval/freeze_baseline.py --out baseline/baseline_v2.json   # new frozen point
git add eval/freeze_baseline.py eval/baseline/baseline_v2.json
git commit -m "eval: re-freeze retrieval baseline v2 (post-<change>)"
git tag -a eval-baseline-v2 -m "frozen retrieval baseline after <change>"
```

The old baseline stays at tag `eval-baseline-v1`, so you can always check out
and compare. Point the gate at the new one with `--baseline`.

---

## Baseline

The frozen retrieval baseline is `baseline/baseline_v1.json` (read-only,
`chmod 444`), tagged in git as **`eval-baseline-v1`** (commit of run 133537).
To restore or inspect it:

```bash
git show eval-baseline-v1:eval/baseline/baseline_v1.json   # view the frozen baseline
git checkout eval-baseline-v1                              # check out the whole tagged state
```

---

## Findings at freeze (baseline run 133537) — read before trusting RAG

These are real product findings the harness surfaced, recorded so they aren't lost:

1. **RAG currently degrades quality.** overall: RAG 6.52 vs standard 8.52. The
   gap holds in **both** languages (id 5.88 vs 8.88 when RAG fires; en 4.75 vs
   8.62). Not a language problem — a grounding problem.
2. **factual_accuracy of RAG is ~1.9.** When retrieved passages actually enter
   the narration, factual fidelity collapses. The retrieved material — rino
   style-fragments (`[musik]`, voiceover snippets) and anachronistic 19th-c.
   Gutenberg text for modern topics — is **not valid factual grounding** as-is.
3. **When passages are dropped (gate 0.55), quality is normal.** RAG-off entries
   score on par with standard. So the passages are the problem, not the idea of RAG.
4. **`style_filter=None` for all entries.** Retrieval isn't filtering by
   style_label — it leans on source + min_quality. This is the "two style
   vocabularies" mismatch (corpus `style_label` vs registry style names) that
   Step 5 is meant to fix.
5. **rino content bleeds into non-rino topics.** e.g. the "kopi" entry
   (prefer_source=false) retrieved a rino passage at rank 1. Watch this — could
   be useful locality or could be noise.

**Implication:** the "moat" (proprietary rino content + corpus RAG) is, in its
current form, *lowering* output quality. Steps 5/6/7 exist to fix retrieval +
grounding. This harness is how you'll prove when that actually works — not by
intuition, by the gate going green at a higher RAG floor.

---

## Known tech debt (out of scope for the gate, fix separately)

- The live pipeline under `data/moat/gutenberg/` is **not version-controlled**
  (`.gitignore` ignores `data/`). The `source`-field fix to `qdrant_index_v2.py`
  made during Step 0.3 lives on disk but isn't in git. Decide how to split
  code-vs-corpus and bring the pipeline under version control.
- `political` style has only 1 golden-set entry (n=1) — a single flaky sample
  decides the whole style. Bump to ≥2 in golden_set v2.
- golden_set v2 should add a field marking "well-grounded vs thin-grounding"
  topics, and use era-appropriate English topics if testing the Gutenberg corpus
  fairly (a 19th-c. corpus can't ground a topic about MH370).
