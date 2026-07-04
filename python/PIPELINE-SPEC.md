# NARASI ⚡ PIPELINE-SPEC — canonical, versioned

**Version: 2.0.0 (2026-07-04)** · Ratifies user's `PIPELINE-SPEC-v1.md` as v2 canonical:
adds Aceh review rule deltas (R-E3 within-doc + `same_scholar_max` + `debate_pairing_variance`
+ `subtotal_scope` + `sequence-claim` classes + synonym-swap dedup) and the 16-CC-addenda
consolidation. Supersedes 16 addenda now archived to `/changelog`.

## 0. Rule zero — no silent deployment gaps

`narasi_manifest.build_manifest()` resolves EVERY rule below to
`active | UNMEASURED(reason) | n/a(reason)` at job start, **fails the job (422)** on a
rule with no state or an unknown style label (no silent fallback), and ships the
manifest in the result payload (`gates_manifest`). Adding a gate to the pipeline without
registering it in `PIPELINE_RULES` (or vice versa) is the failure class this kills.
The matrix below is **generated from `build_manifest` itself** (strict regime, harari,
prod flag state) — regenerate with the snippet in §7 whenever rules change.

## 1. Pipeline order (⚡ path, per job)

```
narration_start (API):
  _narration_admit (caps) → _living_person_guard (BLOCKING, pre-hold)
  → build_manifest (fail-closed) → credit HOLD (routed model, tunable est)
  → jobs row (+_meter op_id if crashsafe) → BullMQ enqueue (BYOK stays in-process)

narration-worker (_process → _run_narration_job):
  tenant-ctx seed → known_bad/known_good/alt_history load (per-job ContextVars)
  → generate_narration:
      router (scenario A–E) → shared context (KNOWN CORRECTIONS injected)
      → chapters parallel [run_worker → failover chain KIE→LaoZhang→Atlas
                           → per-chapter gate (vo_strip=False) → word-gate
                           (floor 0.9× + finish_reason continuation) → S2 checkpoint
                           (GATED text)]
      → assemble → polish (skip if > polish-model ceiling; discard on truncation)
  → _apply_v3_gates:
      (0)   counters + diet loop (scaled budgets, tolerance ×1.3, metered via sink)
      (0.7) entity_pass (variant unify, R-E3 collapse, wrong-domain, self-debate)
      (1)   terminal gate: known_bad → resolve_flags (localized, idempotent,
            class-gated) → dehedge_known_good → placeholder-prose cut → marker strip
            → foreign-token scan → directive strip → meta-leak/broken-sub report
      (2)   register scorecard (R-H10, entry-driven)
      (2.7) factscan (numeric + ID spelled-quantity) → verify pass (Tavily/Serper +
            Opus verdicts, cache writes, metered)
      (2.9) number rendering (video+ID → spellout)
      (3)   Gaya header (display name + Output field + alt_history marker)
  → persist chapters → finalize (raw dict payload) → settle (credits_actual =
    catalog-parity sink total; F4 clamp; usd floor) → checkpoint/sweep protects crash
```

## 2. Deployment coverage matrix (generated)

Prod flag state assumed: `FACTGATE_SEARCH_ENABLED=1` + search keys set,
`NARASI_TERMINAL_GATE/REGISTER_GATE/WORDGATE=on`, `NARRATION_LIVING_GUARD=on`.

| rule | id/video | id/book | en/video | en/book |
|---|---|---|---|---|
| `known_bad` | ✅ | ✅ | ✅ | ✅ |
| `verify_resolve` | ✅ | ✅ | ✅ | ✅ |
| `terminal_strip` | ✅ | ✅ | ✅ | ✅ |
| `foreign_placeholder` | ✅ | ✅ | — n/a<br><sub>no blocklist for 'en' (native language)</sub> | — n/a<br><sub>no blocklist for 'en' (native language)</sub> |
| `counters.citations` | ✅ | ✅ | ✅ | ✅ |
| `counters.aporia` | ✅ | ✅ | ✅ | ✅ |
| `counters.epithet` | ✅ | ✅ | ✅ | ✅ |
| `counters.word_budget` | ✅ | ✅ | ✅ | ✅ |
| `counters.anchors` | ✅ | ✅ | ✅ | ✅ |
| `counters.scene_dup` | ✅ | ✅ | ✅ | ✅ |
| `counters.thesis` | ✅ | ✅ | ✅ | ✅ |
| `register_gate` | ✅ | ✅ | ✅ | ✅ |
| `factscan` | 🟡 UNMEASURED<br><sub>numeric+spelled-quantity sweep active; FG10 prose detectors EN-only</sub> | 🟡 UNMEASURED<br><sub>numeric+spelled-quantity sweep active; FG10 prose detectors EN-only</sub> | ✅ | ✅ |
| `verify_search` | ✅ | ✅ | ✅ | ✅ |
| `entity_pass` | ✅ | ✅ | ✅ | ✅ |
| `attribution_verify` | 🟡 UNMEASURED<br><sub>FG3-b wrong-subject check needs FG-SEARCH; table-seeded names only</sub> | 🟡 UNMEASURED<br><sub>FG3-b wrong-subject check needs FG-SEARCH; table-seeded names only</sub> | 🟡 UNMEASURED<br><sub>FG3-b wrong-subject check needs FG-SEARCH; table-seeded names only</sub> | 🟡 UNMEASURED<br><sub>FG3-b wrong-subject check needs FG-SEARCH; table-seeded names only</sub> |
| `style_spec` | ✅ | ✅ | ✅ | ✅ |
| `number_rendering` | ✅ | — n/a<br><sub>book path keeps digits</sub> | — n/a<br><sub>no spellout rules for 'en'</sub> | — n/a<br><sub>book path keeps digits</sub> |
| `header` | ✅ | ✅ | ✅ | ✅ |
| `living_guard` | ✅ | ✅ | ✅ | ✅ |

## 3. Language packs (`narasi_counters.LANGUAGE_PACKS`)

Every served language has AT MINIMUM `hedge_value`/`hedge_prose` + a `foreign_tokens`
blocklist (FG6 localized everywhere; EN tokens can never leak). Missing cells = that
counter reports UNMEASURED in the manifest — grown from real manuscripts, never faked.

| lang | hedge_value | hedge_prose | foreign_tokens | aporia | attribution | epithet | spelled_quantity | spell_number |
|---|---|---|---|---|---|---|---|---|
| id | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| en | ✅ | ✅ | — | ✅ | ✅ | ✅ | — | — |
| jv | ✅ | ✅ | ✅ | ✅ | — | — | — | — |
| su | ✅ | ✅ | ✅ | — | — | — | — | — |
| ms | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| ban | ✅ | ✅ | ✅ | — | — | — | — | — |
| min | ✅ | ✅ | ✅ | — | — | — | — | — |
| es | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| fr | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| de | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| pt | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| nl | ✅ | ✅ | ✅ | ✅ | — | — | — | — |
| vi | ✅ | ✅ | ✅ | — | — | — | — | — |
| tl | ✅ | ✅ | ✅ | — | — | — | — | — |
| ar | ✅ | ✅ | ✅ | — | — | — | — | — |
| zh | ✅ | ✅ | ✅ | — | — | — | — | — |
| ja | ✅ | ✅ | ✅ | — | — | — | — | — |
| ko | ✅ | ✅ | ✅ | — | — | — | — | — |
| hi | ✅ | ✅ | ✅ | — | — | — | — | — |
| th | ✅ | ✅ | ✅ | — | — | — | — | — |

## 4. Hedge discipline (R-FG6 + R-FG8, the consolidated rules)

Resolution order for `[VERIFY: inner]` (per `resolve_flags`):
1. **Unmeasured language** → unwrap verbatim (never enforce with the wrong language).
2. **known_good context** → EXACT (verified world-claim; inner's own hedge dropped).
3. **Date/year token** (1000–2099, month-name forms) → EXACT — a date is not an estimate.
4. **Preceding qualifier** ("lebih dari/hanya/rata-rata/…") → BARE value (idempotency:
   one value carries at most ONE hedge).
5. **Numeric inner** → hedge ONCE, localized (`sekitar {v}`).
6. **Substantive non-numeric inner** → hedge-wrap once (never delete a value); inner
   already hedged → keep as-is.
7. **Generic/meta inner** ("number", "jumlah surat yang disita…") → prose word; if the
   bracket IS the sentence → CUT.
Post-passes: `dehedge_known_good` (prose hedges glued to cache-verified facts removed),
double-hedge normalizer, placeholder-prose cut, foreign-token replacement.

## 5. Fact-gate stores & seed protocol

- `narasi_known_bad_claims`: python regex with `(?P<bad>)`, action replace|flag, global
  scope w/ context guards. ID seeds MUST cover spelled-number variants and cross-
  sentence `[\s\S]{0,N}` windows (0062 lesson).
- `narasi_known_good_claims`: sweep-exempt + EXACT-render + verify cache. Both word
  orders for a fact (0064 lesson). Verify pass writes back at conf ≥ 0.75.
- **Seed-verify discipline (0065 lesson, blocking rule)**: EVERY fact in a
  review-driven seed migration MUST be search-verified before landing — reviewer facts
  are claims-to-check, not verdicts. 0063 shipped `Kyai Mojo menyerah Februari 1829` as
  `known_good` from reviewer memory; correct date is 12 November 1828 (Mlangi/Sleman).
  The wrong `known_good` **caused** the round-3 hallucination (gap flagged missing →
  filled from memory → error). A wrong exemption is worse than an empty cache. Rule:
  seeds land only after (a) an external source is cited in the SQL comment, OR (b) an
  FG-SEARCH verify at ≥0.75 confidence writes them back. NEVER from reviewer recall
  alone.
- Review protocol: every human review's verdicts become the next migration
  (0060 Cortés → 0061/0062/0063/0064/0065 Diponegoro). Round-3 correction (0065) is
  the mechanism working correctly — the store SELF-CORRECTS when reality contradicts a
  prior seed.

## 6. Entity layer (`narasi_entities`)

SCHOLAR_TABLE (scholars + military/political figures): canonical, surname, variants
(incl. Dutch-particle confabulations), epithet + epithet_fixes. Passes: variant unify →
R-E3 first-mention-keeps-epithet/later-bare-surname → wrong-domain downgrade-to-anonymous
(Budiardjo/Buiskool class) → self-debate (needs ≥2 distinct name-forms) → surname-cluster
report (per-sentence, stoplisted). FG3-b live search verify = NOT WIRED
(`attribution_verify: UNMEASURED` — named citations are human-review items).

## 7. Regenerating the matrix

```bash
cd python && python3 - <<'EOF'
import narasi_manifest as NM
from pakem import resolve_style
for lang in ("id","en"):
    for mode in ("video","book"):
        m = NM.build_manifest(style_entry=resolve_style("harari"), lang=lang,
                              regime="strict", mode=mode, style="harari")
        print(lang, mode, {k: v["status"] for k, v in m.items()})
EOF
```

## 8. Env/flag registry (narasi-relevant)

`NARASI_TERMINAL_GATE` `NARASI_REGISTER_GATE` `NARASI_WORDGATE` (default on) ·
`NARASI_FAILOVER_ENABLED` + `KIE_API_KEY`/`ATLASCLOUD_API_KEY` · `NARASI_OUTLINE_MODEL`
`NARASI_POLISH_MODEL` `NARASI_POLISH_MAX_WORDS` · `NARASI_DIET_TOLERANCE` (1.3)
`NARASI_BUDGET_BASELINE_WORDS` (1500) · `NARASI_HOLD_TOKENS_IN_PER_CH` (13000)
`NARASI_HOLD_OUT_MULT` (3.0) · `FACTGATE_SEARCH_ENABLED` + `TAVILY_API_KEY`/`SERPER_API_KEY`
+ `FACTGATE_VERDICT_MODEL`/`FACTGATE_CACHE_MIN_CONFIDENCE` · `NARRATION_BULLMQ_ENABLED`
`NARRATION_RESUME_ENABLED` `NARRATION_MAX_ACTIVE_PER_TENANT` `NARRATION_LIVING_GUARD`
`DALANG_CRASHSAFE_ENABLED` · `NARASI_EXECUTOR_THREADS`.

## 9. Changelog (superseded addenda)

v3 Stop-the-Pendulum → gates; dual-path medium layer; registry expansion (75 styles);
v4 deterministic counters; refactor pipeline_rules×style_spec; fact-gate FG8/9/10;
FG-SEARCH infra; regime-precedence/living-guard; id-path fixes; id-path round-2 patch.
Full documents live in ~/Downloads/cc-instruksi-*.md (historical record only).
