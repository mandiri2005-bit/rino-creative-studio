# -*- coding: utf-8 -*-
"""
Project Dalang — eval package (WS-9).

A small, dependency-light evaluation + A/B harness for the narration
orchestrator. It scores generated narration on three dimensions — coherence,
factuality, and style adherence — using heuristics (no LLM judge), compares
configurations side-by-side (PAKEM_VERSION / ORCH_MODE / POLISH / RAG), saves a
baseline JSON, and gates regressions. A companion cost dashboard reads
usage_logs for cache-hit ratio and cost/job.

Import-safe by design:
  * No hard dependency on PyYAML (eval.run ships a tiny fallback parser).
  * No hard dependency on a live DB or LLM (run --dry scores fixtures;
    cost_dashboard degrades to an empty table without a DB).

Modules:
  * eval.run            — case loader, scorers, A/B comparison, baseline, gate.
  * eval.cost_dashboard — usage_logs cache-hit ratio + cost/job table.
"""

__all__ = ["run", "cost_dashboard"]
