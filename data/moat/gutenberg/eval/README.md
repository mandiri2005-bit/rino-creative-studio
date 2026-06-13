# eval/ — Frozen RAG eval gate

Promotes the `compare_rag_vs_standard` path (in `rag_narration.py`) into a
**scored, frozen harness**. Every later change to retrieval, the style registry,
or output quality is judged here against a baseline frozen *before* any of those
changes. Cheap insurance against silent regression.

## Files

| file | step | role |
|------|------|------|
| `golden_set.yaml`     | 0.2 | the frozen fixture: 25 topics × style × lang × prefer_source |
| `run_eval.py`         | 0.3 | run the fixture through live retrieval + generation; capture raw results |
| `freeze_baseline.py`  | 0.4 | snapshot today's retrieved ids+scores → `baseline/baseline_v1.json` (read-only) |
| `score_retrieval.py`  | 0.5 | recall@k of a run vs the frozen baseline |
| `score_output.py`     | 0.6 | 8.5-rubric LLM-judge of narration quality, per path |
| `eval_gate.py`        | 0.7 | run everything → PASS/FAIL with exit code |
| `baseline/`           | 0.4 | the frozen baseline JSON (committed + git-tagged) |
| `results/`            | —   | per-run output JSON (NOT committed) |

> Files are skeletons until their step wires the logic in. Order: 0.1 → 0.8.

## The two scores are separate — on purpose

- **Retrieval** (`score_retrieval.py`): recall@5 / recall@10 / overlap / drift
  vs the frozen baseline. Answers "is retrieval still returning the same good
  passages?"
- **Output** (`score_output.py`): 8.5-rubric LLM-judge over the narration,
  scored per path (rag vs standard). Answers "is the prose still good?"

A single combined number hides which half regressed. Two files, two thresholds.

## `style` in the golden set = REGISTRY KEY, not corpus label

`golden_set.yaml`'s `style` field flows into
`compare_rag_vs_standard(style=...)` → `get_style_config(style)`. Use the
canonical registry keys (or their aliases) from `style_rag_config.py`:

```
storytelling, bedtime_story, creative_nonfiction, big_history,
pov_first_person, natgeo, youtube_popular_science, journalistic,
literary_essay, podcast_narrative, academic_popular, cinematic_voiceover,
narrative_nonfiction_mystery, fiction
```

Corpus `style_label` values (`conversational`, `political`, `lyrical`,
`financial`, `dramatic`, …) are a **different axis** used by retrieval filtering
and must not be used here — they fall through to the default config silently.

## How to run (after Step 0.7)

```bash
# full gate: run fixture, score both halves, PASS/FAIL
python eval/eval_gate.py
echo $?            # 0 = PASS, 1 = FAIL

# score the latest run without re-running generation
python eval/eval_gate.py --use-latest
```

### Thresholds
Edit the constants at the top of `eval_gate.py` after the first baseline run:
`MIN_RECALL_AT_10`, `MIN_RINO_RECALL_AT_10`, `MIN_OUTPUT_SCORE`.

## SOP for Steps 1–9

1. Run `python eval/eval_gate.py` **before** the change; note the numbers.
2. Make the change (retrieval / registry / quality).
3. Run the gate again. If **FAIL**, fix before merging — don't merge a regression.

## Baseline recovery

The frozen baseline is committed and tagged `eval-baseline-v1`. To inspect or
restore it:

```bash
git show eval-baseline-v1:eval/baseline/baseline_v1.json
git checkout eval-baseline-v1 -- eval/baseline/baseline_v1.json
```

If a later step *intentionally* changes retrieval for the better (e.g. Step 7
hybrid + re-index), freeze a new `baseline_v2` with a new tag — a deliberate
decision with evidence, not a default. The old tag stays, so you can always
compare back.
```
