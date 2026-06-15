# -*- coding: utf-8 -*-
"""
Project Dalang — orchestrator core primitives (WS-1).

This module provides the foundation for the unified narration orchestration
engine: one env-built LLM client, model routing/registry, a never-raise async
worker wrapper, an async synthesizer (manager/merge role), and a telemetry hook
that records tokens in/out + estimated cost per call.

Design constraints (see Project Dalang build plan / WS-1):
  * Additive & non-breaking. Importing this package must NEVER require the full
    `laozhang_api` import chain to succeed (it pulls redis/asyncpg/fastapi which
    are not present in every environment, e.g. the host 3.14 venv). We therefore
    REUSE the real primitives from `laozhang_api` when they import cleanly, and
    fall back to a self-contained replication of the SAME env pattern otherwise.
  * No hardcoded keys. The client is built from env exactly the way
    `laozhang_api.make_client` does: LAOZHANG_API_KEY + BASE_URL (OpenAI-compatible).
  * Valid for Python 3.11 (Docker) and 3.14 (host).

The upstream `laozhang_api` client is a *synchronous* OpenAI client and its
chat calls are blocking. To stay async-friendly we run those blocking calls in
a thread via `asyncio.to_thread`, then wrap the whole thing in
`asyncio.wait_for` so timeouts and failures degrade gracefully.
"""
from __future__ import annotations

import os
import time
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Reuse-or-replicate the real laozhang_api primitives.
#
# We try the clean import first (so we share the single source of truth for the
# model registry, token ceilings, cost table, and client factory). If that fails
# for ANY reason — missing redis/asyncpg on the host, partial deps, etc. — we
# replicate the exact same env-driven pattern locally so the orchestrator stays
# importable and runnable everywhere. No keys are ever hardcoded.
# ---------------------------------------------------------------------------
_USING_LAOZHANG = False
try:  # pragma: no cover - depends on environment deps
    from laozhang_api import (  # type: ignore
        make_client as _lz_make_client,
        MODELS as _LZ_MODELS,
        MODEL_MAX_TOKENS as _LZ_MODEL_MAX_TOKENS,
        DEFAULT_MAX_TOKENS as _LZ_DEFAULT_MAX_TOKENS,
        _calc_cost as _lz_calc_cost,
    )
    MODELS = _LZ_MODELS
    MODEL_MAX_TOKENS = _LZ_MODEL_MAX_TOKENS
    DEFAULT_MAX_TOKENS = _LZ_DEFAULT_MAX_TOKENS
    _USING_LAOZHANG = True
    log.debug("orchestrator: reusing laozhang_api primitives")
except Exception as _imp_err:  # noqa: BLE001 - intentional broad fallback
    log.info(
        "orchestrator: laozhang_api not importable (%s); using self-contained "
        "env client/registry fallback", _imp_err.__class__.__name__,
    )
    _lz_make_client = None  # type: ignore
    _lz_calc_cost = None  # type: ignore

    # ---- Replicated env pattern (mirrors laozhang_api lines 76-244) --------
    API_KEY = os.environ.get("LAOZHANG_API_KEY", "")
    DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
    DEEPSEEK_DIRECT_MODELS = {"deepseek-v4-pro", "deepseek-r1"}
    BASE_URL = "https://api.laozhang.ai/v1"

    # Alias -> upstream model name. Kept in sync with laozhang_api.MODELS; only
    # used when the real module is unavailable.
    MODELS = {
        "gemini-2.5-flash": "gemini-2.5-flash",
        "deepseek-v3": "deepseek-chat",
        "gpt-4o-mini": "gpt-4o-mini",
        "qwen-max": "qwen-max",
        "gemini-2.5-flash-lite": "gemini-2.5-flash-lite",
        "gemini-2.5-pro": "gemini-2.5-pro",
        "claude-sonnet": "claude-sonnet-4-6-thinking",
        "claude-sonnet-4-6": "claude-sonnet-4-6",
        "gpt-4o": "gpt-4o",
        "grok-4": "grok-4-latest",
        "claude-opus-4-6": "claude-opus-4-6",
        "claude-opus-4-7": "claude-opus-4-7",
        "claude-opus-4-7-thinking": "claude-opus-4-7-thinking",
        "glm": "glm-4.5-flash",
        "gpt-5-nano": "gpt-5-nano",
        "deepseek-v3-0324": "deepseek-v3-250324",
        "deepseek-v4-pro": "deepseek-v4-pro",
        "deepseek-r1": "deepseek-r1",
        "grok-4-fast": "grok-4-fast",
        "gemini-3-flash": "gemini-3-flash-preview",
    }

    MODEL_MAX_TOKENS: dict[str, int] = {
        "gemini-2.5-flash": 16384,
        "deepseek-chat": 8192,
        "gpt-4o-mini": 16384,
        "qwen-max": 8192,
        "gemini-2.5-flash-lite": 8192,
        "gemini-2.5-pro": 65536,
        "claude-sonnet-4-6": 8192,
        "claude-sonnet-4-6-thinking": 64000,
        "gpt-4o": 16384,
        "grok-4-latest": 32000,
        "claude-opus-4-6": 32000,
        "claude-opus-4-7": 32000,
        "claude-opus-4-7-thinking": 32000,
        "glm-4.5-flash": 4096,
        "gpt-5-nano": 16384,
        "deepseek-v3-250324": 8192,
        "deepseek-v4-pro": 65536,
        "deepseek-r1": 65536,
        "grok-4-fast": 8192,
        "gemini-3-flash-preview": 8192,
    }
    DEFAULT_MAX_TOKENS = 16384

    # Best-effort $/1M tokens (input, output). Mirrors laozhang_api._MODEL_COSTS_PER_M.
    _MODEL_COSTS_PER_M: dict[str, tuple[float, float]] = {
        "gpt-4o-mini": (0.15, 0.60),
        "gpt-4o": (5.00, 15.00),
        "gpt-5-nano": (0.05, 0.40),
        "claude-haiku": (0.80, 4.00),
        "claude-sonnet": (3.00, 15.00),
        "claude-opus": (15.00, 75.00),
        "deepseek-chat": (0.27, 1.10),
        "deepseek-v3": (0.27, 1.10),
        "deepseek-v4-pro": (0.55, 2.19),
        "deepseek-r1": (0.55, 2.19),
        "gemini-2.5-flash-lite": (0.075, 0.30),
        "gemini-2.5-flash": (0.15, 0.60),
        "gemini-2.5-pro": (1.25, 10.00),
        "gemini-3-flash": (0.15, 0.60),
        "qwen-max": (1.60, 6.40),
        "grok-4-fast": (0.20, 0.50),
        "grok-4": (3.00, 15.00),
        "glm": (0.10, 0.10),
    }

    def _lz_calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:  # type: ignore
        """Replicated cost estimate (longest-matching-prefix wins)."""
        names = [str(model).lower(), str(MODELS.get(model, "")).lower()]
        for name in names:
            if not name:
                continue
            key = max((k for k in _MODEL_COSTS_PER_M if name.startswith(k)),
                      key=len, default=None)
            if key:
                in_p, out_p = _MODEL_COSTS_PER_M[key]
                return round((tokens_in * in_p + tokens_out * out_p) / 1_000_000, 8)
        return 0.0

    def _lz_make_client(model: str = ""):  # type: ignore
        """Replicated env client factory (mirrors laozhang_api.make_client).

        Builds an OpenAI-compatible client from env. DeepSeek-direct models use
        DEEPSEEK_API_KEY; everything else uses LAOZHANG_API_KEY. No keys are
        hardcoded — both come from the environment.
        """
        from openai import OpenAI  # local import: keep module import light
        resolved = MODELS.get(model, model)
        if resolved in DEEPSEEK_DIRECT_MODELS or model in DEEPSEEK_DIRECT_MODELS:
            key = DEEPSEEK_API_KEY or API_KEY
            if not key:
                raise ValueError("DEEPSEEK_API_KEY / LAOZHANG_API_KEY not set.")
            return OpenAI(api_key=key, base_url=BASE_URL)
        return OpenAI(api_key=API_KEY, base_url=BASE_URL)


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Public cost estimator — delegates to the shared/replicated calculator."""
    try:
        return float(_lz_calc_cost(model, int(tokens_in or 0), int(tokens_out or 0)))
    except Exception:  # noqa: BLE001 - cost must never break a call
        return 0.0


# ---------------------------------------------------------------------------
# Model registry / routing
# ---------------------------------------------------------------------------
# Defaults are overridable by env so deployments can re-route without code edits.
WORKER_MODEL = os.environ.get("WORKER_MODEL", "gemini-2.5-flash")
MANAGER_MODEL = os.environ.get("MANAGER_MODEL", "claude-sonnet-4-6")
try:
    MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS", "6")))
except (TypeError, ValueError):
    MAX_WORKERS = 6

# Per-style worker-model hints. A style can prefer a stronger/cheaper model.
# Overridable via env: ORCH_STYLE_MODEL_<STYLE_UPPER>=<model-alias>.
# Keys are lowercased style names; lookup is substring-tolerant (see route_model).
_DEFAULT_STYLE_MODEL_HINTS: dict[str, str] = {
    "harari": MANAGER_MODEL,            # dense Big-History reasoning -> manager-grade
    "academic popular": MANAGER_MODEL,
    "literary essay": MANAGER_MODEL,
    "journalistic": WORKER_MODEL,
    "creative non-fiction": WORKER_MODEL,
    "storytelling": WORKER_MODEL,
    "bedtime story": WORKER_MODEL,
    "national geographic": WORKER_MODEL,
    "youtube": WORKER_MODEL,
    "pov": WORKER_MODEL,
    "podcast narrative": WORKER_MODEL,
    "cinematic voiceover": WORKER_MODEL,
}


def _load_style_hints() -> dict[str, str]:
    """Merge default style hints with any ORCH_STYLE_MODEL_* env overrides."""
    hints = dict(_DEFAULT_STYLE_MODEL_HINTS)
    prefix = "ORCH_STYLE_MODEL_"
    for k, v in os.environ.items():
        if k.startswith(prefix) and v.strip():
            style = k[len(prefix):].replace("_", " ").strip().lower()
            if style:
                hints[style] = v.strip()
    return hints


STYLE_MODEL_HINTS = _load_style_hints()


def route_model(*, role: str = "worker", style: Optional[str] = None,
                override: Optional[str] = None) -> str:
    """Resolve the model alias to use for a given role/style.

    Precedence: explicit override > per-style hint (worker role only) > role default.
    Substring match on style so "harari (sapiens)" still hits the "harari" hint.
    """
    if override:
        return override
    if role == "manager":
        return MANAGER_MODEL
    if style:
        s = style.strip().lower()
        if s in STYLE_MODEL_HINTS:
            return STYLE_MODEL_HINTS[s]
        for key, model in STYLE_MODEL_HINTS.items():
            if key and key in s:
                return model
    return WORKER_MODEL


def max_tokens_for(model: str) -> int:
    """Output-token ceiling for a model alias/upstream name (reused table)."""
    resolved = MODELS.get(model, model)
    return MODEL_MAX_TOKENS.get(resolved, MODEL_MAX_TOKENS.get(model, DEFAULT_MAX_TOKENS))


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------
@dataclass
class CallTelemetry:
    """One LLM call's accounting record. Later wired to db.log_usage / usage_logs."""
    model: str
    role: str = "worker"
    ok: bool = True
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 1
    finish_reason: str = ""
    error: str = ""
    task_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "role": self.role, "ok": self.ok,
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd, "latency_ms": self.latency_ms,
            "attempts": self.attempts, "finish_reason": self.finish_reason,
            "error": self.error, "task_id": self.task_id,
        }


# A telemetry sink is any callable taking a CallTelemetry. The default sink just
# logs at DEBUG; the caller (e.g. /narration runtime) can pass a sink that writes
# to usage_logs via db.log_usage. Sinks must never raise back into the call path.
TelemetrySink = Callable[[CallTelemetry], None]


def _default_sink(t: CallTelemetry) -> None:
    log.debug(
        "orchestrator.call model=%s role=%s ok=%s tin=%d tout=%d cost=$%.6f %dms attempts=%d %s",
        t.model, t.role, t.ok, t.tokens_in, t.tokens_out, t.cost_usd,
        t.latency_ms, t.attempts, t.error,
    )


def _emit(sink: Optional[TelemetrySink], t: CallTelemetry) -> None:
    try:
        (sink or _default_sink)(t)
    except Exception:  # noqa: BLE001 - telemetry must never break generation
        log.debug("orchestrator: telemetry sink raised (ignored)", exc_info=True)


# ---------------------------------------------------------------------------
# Worker spec
# ---------------------------------------------------------------------------
@dataclass
class Worker:
    """A configured generation worker. `model`/`style` drive routing; the rest
    tune the call. Pass either an explicit model or a style to be routed."""
    name: str = "worker"
    role: str = "worker"
    model: Optional[str] = None
    style: Optional[str] = None
    system: str = ""
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    telemetry_sink: Optional[TelemetrySink] = field(default=None, repr=False)

    def resolved_model(self) -> str:
        return route_model(role=self.role, style=self.style, override=self.model)


# Transient error fingerprints worth a retry (rate limits, timeouts, 5xx, resets).
_TRANSIENT_MARKERS = (
    "rate limit", "429", "timeout", "timed out", "temporarily",
    "overloaded", "502", "503", "504", "connection reset",
    "connection error", "service unavailable", "internal server error",
)


def _is_transient(exc: BaseException) -> bool:
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _build_messages(system: str, task: str) -> list[dict[str, str]]:
    msgs: list[dict[str, str]] = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": task})
    return msgs


def _sync_chat(model: str, messages: list[dict[str, str]], *,
               temperature: float, max_tokens: int):
    """Blocking single chat-completion call against the env-built client.
    Mirrors laozhang_api's call shape: client.chat.completions.create(...)."""
    client = _lz_make_client(model)
    resolved = MODELS.get(model, model)
    return client.chat.completions.create(
        model=resolved,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=False,
    )


def _extract(resp: Any) -> tuple[str, int, int, str]:
    """Pull text + token usage + finish_reason from an OpenAI-style response."""
    text = ""
    finish = ""
    try:
        choice = resp.choices[0]
        text = (getattr(choice.message, "content", None) or "").strip()
        finish = getattr(choice, "finish_reason", "") or ""
    except Exception:  # noqa: BLE001
        pass
    usage = getattr(resp, "usage", None)
    tok_in = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    tok_out = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return text, tok_in, tok_out, finish


async def run_worker(
    worker: Worker,
    task: str,
    timeout: float = 120.0,
    *,
    task_id: str = "",
    max_retries: int = 2,
    base_backoff: float = 0.75,
) -> dict[str, Any]:
    """Run a single worker call. NEVER raises.

    On success returns:
        {"ok": True, "output": <text>, "model": ..., "telemetry": {...}}
    On any failure/timeout returns a marker:
        {"ok": False, "output": None, "error": <str>, "model": ..., "telemetry": {...}}

    Transient errors (rate limit / timeout / 5xx) are retried with exponential
    backoff up to `max_retries`. The whole call (per attempt) is bounded by
    `timeout` seconds via asyncio.wait_for.
    """
    model = worker.resolved_model()
    ceiling = max_tokens_for(model)
    max_tokens = min(worker.max_tokens or ceiling, ceiling)
    messages = _build_messages(worker.system, task)

    started = time.monotonic()
    last_err = ""
    attempts = 0

    for attempt in range(1, max_retries + 2):  # 1 initial + max_retries
        attempts = attempt
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    _sync_chat, model, messages,
                    temperature=worker.temperature, max_tokens=max_tokens,
                ),
                timeout=timeout,
            )
            text, tok_in, tok_out, finish = _extract(resp)
            latency_ms = int((time.monotonic() - started) * 1000)
            tele = CallTelemetry(
                model=model, role=worker.role, ok=True,
                tokens_in=tok_in, tokens_out=tok_out,
                cost_usd=estimate_cost(model, tok_in, tok_out),
                latency_ms=latency_ms, attempts=attempts,
                finish_reason=finish, task_id=task_id or worker.name,
            )
            _emit(worker.telemetry_sink, tele)
            if not text:
                # Empty output is a soft failure — surface as a never-raise marker.
                return {
                    "ok": False, "output": None,
                    "error": "empty_output", "model": model,
                    "telemetry": tele.as_dict(),
                }
            return {
                "ok": True, "output": text, "model": model,
                "telemetry": tele.as_dict(),
            }

        except asyncio.TimeoutError:
            last_err = f"timeout after {timeout}s"
            log.warning("run_worker[%s] attempt %d timed out (%ss)",
                        worker.name, attempt, timeout)
            # Timeouts are transient — fall through to backoff/retry.
        except asyncio.CancelledError:
            raise  # never swallow cancellation
        except Exception as exc:  # noqa: BLE001 - never-raise contract
            last_err = f"{type(exc).__name__}: {exc}"
            if not _is_transient(exc):
                log.warning("run_worker[%s] non-transient error: %s",
                            worker.name, last_err)
                break  # don't retry hard errors (bad request, auth, etc.)
            log.warning("run_worker[%s] attempt %d transient error: %s",
                        worker.name, attempt, last_err)

        # Backoff before the next attempt (skip after the final attempt).
        if attempt <= max_retries:
            await asyncio.sleep(base_backoff * (2 ** (attempt - 1)))

    latency_ms = int((time.monotonic() - started) * 1000)
    tele = CallTelemetry(
        model=model, role=worker.role, ok=False,
        latency_ms=latency_ms, attempts=attempts,
        error=last_err, task_id=task_id or worker.name,
    )
    _emit(worker.telemetry_sink, tele)
    return {
        "ok": False, "output": None, "error": last_err,
        "model": model, "telemetry": tele.as_dict(),
    }


# ---------------------------------------------------------------------------
# Synthesis (manager / merge / polish)
# ---------------------------------------------------------------------------
def _format_results(results: list[dict[str, Any]]) -> str:
    """Render worker results into a numbered block for the manager prompt.
    Failed/empty workers are labelled so the manager can route around them."""
    blocks: list[str] = []
    for i, r in enumerate(results, 1):
        if isinstance(r, dict):
            ok = r.get("ok", False)
            out = r.get("output")
            if ok and out:
                blocks.append(f"--- PART {i} ---\n{out}")
            else:
                err = r.get("error", "no output")
                blocks.append(f"--- PART {i} (FAILED: {err}) ---")
        else:
            blocks.append(f"--- PART {i} ---\n{str(r)}")
    return "\n\n".join(blocks)


_SYNTH_INSTRUCTIONS = {
    "merge": (
        "You are the editor-in-chief. Merge the parts below into ONE coherent, "
        "seamless piece. Remove duplication and contradictions, smooth the "
        "transitions, and preserve every concrete fact, name, date and number. "
        "Do NOT add a preamble or meta-commentary — return only the merged text."
    ),
    "polish": (
        "You are the editor-in-chief. Polish the draft(s) below for voice, "
        "rhythm and clarity WITHOUT changing the facts or the meaning. Return "
        "only the polished text — no notes, no preamble."
    ),
    "synthesize": (
        "You are the lead author. Synthesize the parts below into a single "
        "unified result that honours the task. Keep what is strong, reconcile "
        "conflicts, and return only the final text."
    ),
}


async def synthesize(
    task: str,
    results: list[dict[str, Any]],
    role: str = "merge",
    *,
    model: Optional[str] = None,
    system: str = "",
    timeout: float = 180.0,
    temperature: float = 0.4,
    telemetry_sink: Optional[TelemetrySink] = None,
    task_id: str = "synthesize",
) -> dict[str, Any]:
    """Merge/polish many worker results into one, via the manager model.

    `role` ∈ {"merge", "polish", "synthesize"} selects the editor instruction.
    NEVER raises — delegates to run_worker, so failures return an {"ok": False}
    marker. If exactly one usable result exists and role is "merge", it is
    returned as-is (no spend) for efficiency.
    """
    usable = [r for r in results if isinstance(r, dict) and r.get("ok") and r.get("output")]
    if role == "merge" and len(usable) == 1:
        only = usable[0]
        return {
            "ok": True, "output": only["output"],
            "model": only.get("model", ""), "telemetry": {"skipped": "single_result"},
        }

    instruction = _SYNTH_INSTRUCTIONS.get(role, _SYNTH_INSTRUCTIONS["merge"])
    body = _format_results(results)
    prompt = (
        f"{instruction}\n\n"
        f"ORIGINAL TASK:\n{task}\n\n"
        f"PARTS TO {role.upper()}:\n{body}\n\n"
        f"Now produce the single final result."
    )
    manager = Worker(
        name=f"manager:{role}",
        role="manager",
        model=model or MANAGER_MODEL,
        system=system or "You are a meticulous senior editor.",
        temperature=temperature,
        telemetry_sink=telemetry_sink,
    )
    return await run_worker(manager, prompt, timeout=timeout, task_id=task_id)


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
    "MODELS",
    "MODEL_MAX_TOKENS",
    "DEFAULT_MAX_TOKENS",
]
