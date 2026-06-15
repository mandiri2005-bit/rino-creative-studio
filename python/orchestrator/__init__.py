# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator package.

Unified narration orchestration primitives. WS-1 ships the foundation:
  * an env-built OpenAI-compatible LLM client (reuses laozhang_api.make_client
    when importable, else replicates the same LAOZHANG_API_KEY + BASE_URL pattern),
  * a model registry/router driven by env (WORKER_MODEL / MANAGER_MODEL / per-style
    hints / MAX_WORKERS),
  * `run_worker` — a never-raise async worker wrapper (wait_for + retry/backoff),
  * `synthesize` — async merge/polish via the manager model,
  * a per-call telemetry record (tokens in/out + estimated cost) for usage_logs.
"""
from .core import (
    Worker,
    CallTelemetry,
    TelemetrySink,
    run_worker,
    synthesize,
    route_model,
    max_tokens_for,
    estimate_cost,
    WORKER_MODEL,
    MANAGER_MODEL,
    MAX_WORKERS,
    STYLE_MODEL_HINTS,
)

__all__ = [
    "Worker",
    "CallTelemetry",
    "TelemetrySink",
    "run_worker",
    "synthesize",
    "route_model",
    "max_tokens_for",
    "estimate_cost",
    "WORKER_MODEL",
    "MANAGER_MODEL",
    "MAX_WORKERS",
    "STYLE_MODEL_HINTS",
]
