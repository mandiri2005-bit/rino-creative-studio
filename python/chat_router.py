# -*- coding: utf-8 -*-
"""
chat_router.py — single source of truth for CHAT models + providers + per-model
failover dispatch.

WHY
---
Model defs used to be scattered (frontend main.jsx tiers/labels + backend MODELS/
capabilities/max_tokens). This centralises everything into ONE registry the
frontend fetches (`GET /models`), and adds per-model PROVIDER FAILOVER:

    user picks a MODEL  ->  dispatcher walks the model's provider CHAIN in order
    -> on provider credit/quota/5xx, REFUND + try next provider
    -> on success, COMMIT the credit hold at the price of the provider that served
    -> all providers exhausted -> graceful "server busy" notice

Adding a provider ("route") = one entry in PROVIDERS (+ an adapter only if the
"kind" is new). Adding a model = one entry in MODEL_REGISTRY. No frontend rebuild
to change models (the dropdown is fetched).

DECISIONS (Rino, 2026-06-17)
  1. Loop the whole chain; all exhausted -> localized "server busy, try later".
  2. Mid-stream failure -> REFUND the hold + emit a [RESTART] marker (client clears
     the partial) + retry next provider with a fresh hold (no charge for the
     discarded partial).
  3. Failover triggers: insufficient_balance / quota / 429 / 401 / 5xx. A real
     client error (400 invalid request) does NOT fail over.
  4. A provider whose API key env is unset is auto-skipped from the chain.
  5. Metering is accurate per provider: settle() is called with the provider that
     actually served + that provider's real USD (registry rates).
  6. Phase 1: user picks MODEL only (no route toggle); Rino orders each model's
     chain by cost. Phase 2 may expose provider choice.

NOT YET WIRED. This module is standalone; wiring into /chat/stream + Node proxy +
frontend is Step 2-4. Tool-calling (MCP) port from chat_stream() is a follow-up.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

log = logging.getLogger("chat_router")

# ──────────────────────────────────────────────────────────────────────────────
# PROVIDERS — "add a route" = add an entry here. 4 adapter "kinds" cover all.
#   openai_compat : OpenAI-compatible /chat/completions (laozhang, openai, deepseek)
#   anthropic     : Anthropic Messages API
#   google_vertex : Google GenAI on Vertex via OAuth (reuses _genai_client)
#   google_key    : Google GenAI via plain GEMINI_API_KEY
# A provider is "usable" only if its credential is present (else auto-skipped).
# ──────────────────────────────────────────────────────────────────────────────
PROVIDERS: dict[str, dict] = {
    "laozhang":   {"kind": "openai_compat", "base_url": "https://api.laozhang.ai/v1", "key_env": "LAOZHANG_API_KEY", "gateway": True},
    "openai":     {"kind": "openai_compat", "base_url": "https://api.openai.com/v1",  "key_env": "OPENAI_API_KEY"},
    "deepseek":   {"kind": "openai_compat", "base_url": "https://api.deepseek.com",   "key_env": "DEEPSEEK_API_KEY"},
    "anthropic":  {"kind": "anthropic",     "base_url": "https://api.anthropic.com",  "key_env": "ANTHROPIC_API_KEY"},
    "vertex":     {"kind": "google_vertex"},                       # creds via _ensure_vertex() (GCP_* OAuth)
    "google_key": {"kind": "google_key",    "key_env": "GEMINI_API_KEY"},
}


def provider_usable(name: str) -> bool:
    """A provider is usable if its credential is configured (Decision #4)."""
    p = PROVIDERS.get(name)
    if not p:
        return False
    if p.get("gateway"):
        return True              # laozhang gateway: always listable (key is per-request, not env)
    if p["kind"] == "google_vertex":
        # OAuth refresh-token creds; lazy-checked via laozhang_api._ensure_vertex().
        try:
            from laozhang_api import _ensure_vertex
            return bool(_ensure_vertex())
        except Exception:
            return False
    return bool(os.getenv(p.get("key_env", ""), ""))


# ──────────────────────────────────────────────────────────────────────────────
# MODEL REGISTRY — the single source of truth.
#   chain: ordered list of Step (provider, upstream model id, per-1M token USD).
#   The user picks `id`; the dispatcher walks `chain` (skipping unusable providers).
#   cost_in/out are USD per 1M tokens FOR THAT PROVIDER -> accurate metering (#5).
#   This is a REPRESENTATIVE seed; the full spec list is filled in Step 4.
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class Step:
    provider: str
    model: str                       # upstream model id for this provider
    cost_in: float = 0.0             # USD / 1M input tokens
    cost_out: float = 0.0            # USD / 1M output tokens


@dataclass
class Model:
    id: str
    display: str
    tier: str = "medium"             # legacy; superseded by `group`
    group: str = "other"             # provider family — drives dropdown grouping
    desc: str = ""                   # short description — dropdown hover tooltip
    badge: str = ""                  # "", "⭐", "⭐⭐"
    vision: bool = False
    tools: bool = False
    max_tokens: int = 8192
    chain: list[Step] = field(default_factory=list)

    def public(self) -> dict:
        """Shape returned to the frontend dropdown (no cost/provider internals)."""
        return {"id": self.id, "display": self.display, "group": self.group,
                "desc": self.desc, "badge": self.badge,
                "vision": self.vision, "tools": self.tools}


# Chain policy (Rino re-orders by cost later; costs are USD/1M tok, tune vs live pricing):
#   route "google" -> Vertex OAuth first (survives GEMINI_API_KEY leaks), then API key,
#                     then laozhang gateway.
#   route "laozhang" -> laozhang gateway first, then the model's native direct provider
#                       (if its key is configured).
def _chain(model_id: str, ci: float, co: float, route: str, direct: Optional[str]) -> list[Step]:
    if route == "google":
        return [Step("vertex",     model_id, ci, co),
                Step("google_key", model_id, ci, co),
                Step("laozhang",   model_id, ci, co)]
    chain = [Step("laozhang", model_id, ci, co)]
    if direct:
        chain.append(Step(direct, model_id, ci, co))
    return chain


# Dropdown groups, in display order.
GROUP_ORDER = ["gemini", "gpt", "reasoning", "claude", "grok", "deepseek", "qwen", "other"]
GROUP_LABELS = {
    "gemini": "Gemini", "gpt": "GPT", "reasoning": "Reasoning (o-series)",
    "claude": "Claude", "grok": "Grok", "deepseek": "DeepSeek",
    "qwen": "Qwen", "other": "Other",
}


def _est_cost(mid: str) -> tuple[float, float]:
    """Rough USD/1M (in, out) — placeholder for v2 metering only; Rino tunes later.
    The LIVE laozhang route meters via laozhang_api._MODEL_COSTS_PER_M, not this."""
    m = mid.lower()
    if any(k in m for k in ("nano", "mini", "lite", "haiku", "turbo", "gemma", "spark", "3-5-haiku")):
        return (0.10, 0.40)
    if any(k in m for k in ("opus", "o3-pro", "gpt-5-pro", "5.5", "qwq-plus", "480b", "grok-4")):
        return (5.00, 25.00)
    if any(k in m for k in ("flash", "sonnet", "deepseek", "qwen", "grok-3", "kimi",
                            "llama", "doubao", "glm", "minimax", "ernie", "gpt-4o", "4.1", "v3", "v4")):
        return (0.50, 2.50)
    return (1.50, 8.00)


def _vision(mid: str) -> bool:
    m = mid.lower()
    return any(k in m for k in ("gemini", "gpt-4o", "gpt-4.1", "gpt-5", "claude", "grok-4", "doubao", "llama-4"))


# (id, display, group, badge, route, direct_provider, desc) — Rino's curated catalog (2026-06-18).
# 6 Gemini lead the Google (Vertex-OAuth) route; everything else routes via laozhang.
_SPEC: list[tuple] = [
    # ── Gemini (Google/Vertex-OAuth route) ──
    ("gemini-3.5-flash",       "Gemini 3.5 Flash",       "gemini", "",   "google",   None, "Most intelligent model for sustained frontier performance in agentic and coding tasks"),
    ("gemini-3.1-flash-lite",  "Gemini 3.1 Flash-Lite",  "gemini", "",   "google",   None, "Lighter fast model"),
    ("gemini-3-flash-preview", "Gemini 3 Flash Preview", "gemini", "⭐⭐", "google",   None, "Fast multimodal model"),
    ("gemini-3.1-pro-preview", "Gemini 3.1 Pro Preview", "gemini", "⭐⭐", "google",   None, "Latest Pro preview with strong tool and agent capabilities"),
    ("gemini-2.5-flash",       "Gemini 2.5 Flash",       "gemini", "⭐",  "google",   None, "Fast speed, low cost"),
    ("gemini-2.5-flash-lite",  "Gemini 2.5 Flash Lite",  "gemini", "",   "google",   None, "Fast speed, very low cost"),
    ("gemini-2.5-pro",         "Gemini 2.5 Pro",         "gemini", "⭐",  "laozhang", None, "Coding advantage, multimodal"),
    ("gemini-2.0-flash-001",   "Gemini 2.0 Flash",       "gemini", "",   "laozhang", None, "Experimental"),
    # ── GPT ──
    ("gpt-5.5",                "GPT-5.5",                "gpt", "⭐⭐", "laozhang", "openai", "OpenAI's latest flagship model"),
    ("gpt-5.1",                "GPT-5.1",                "gpt", "⭐⭐", "laozhang", "openai", "Strong performance, balanced"),
    ("gpt-5.1-codex",          "GPT-5.1-Codex",          "gpt", "⭐⭐", "laozhang", "openai", "Coding specialized"),
    ("gpt-5.1-codex-high",     "GPT-5.1-Codex High",     "gpt", "⭐",  "laozhang", "openai", "High performance coding"),
    ("gpt-5.1-codex-mini",     "GPT-5.1-Codex Mini",     "gpt", "",   "laozhang", "openai", "Lightweight coding"),
    ("gpt-5",                  "GPT-5",                  "gpt", "",   "laozhang", "openai", "General tasks"),
    ("gpt-5-pro",              "GPT-5 Pro",              "gpt", "",   "laozhang", "openai", "Professional version"),
    ("gpt-5-mini",             "GPT-5 Mini",             "gpt", "",   "laozhang", "openai", "Lightweight efficient"),
    ("gpt-5-nano",             "GPT-5 Nano",             "gpt", "",   "laozhang", "openai", "Ultra-lightweight"),
    ("gpt-4.1",                "GPT-4.1",                "gpt", "⭐",  "laozhang", "openai", "Fast speed"),
    ("gpt-4.1-mini",           "GPT-4.1 Mini",           "gpt", "",   "laozhang", "openai", "Affordable lightweight"),
    ("gpt-4.1-nano",           "GPT-4.1 Nano",           "gpt", "",   "laozhang", "openai", "Ultra-low-cost"),
    ("gpt-4o",                 "GPT-4o",                 "gpt", "",   "laozhang", "openai", "Balanced multimodal"),
    ("gpt-4o-mini",            "GPT-4o Mini",            "gpt", "",   "laozhang", "openai", "Lightweight, fast, compatible"),
    # ── Reasoning (OpenAI o-series) ──
    ("o3-pro",                 "o3-pro",                 "reasoning", "⭐⭐", "laozhang", "openai", "Strongest reasoning"),
    ("o3",                     "o3",                     "reasoning", "⭐",  "laozhang", "openai", "Reasoning model"),
    ("o4-mini",                "o4-mini",                "reasoning", "⭐⭐", "laozhang", "openai", "Lightweight reasoning"),
    # ── Claude ──
    ("claude-opus-4-7",           "Claude Opus 4.7",           "claude", "⭐⭐", "laozhang", "anthropic", "Anthropic's most capable current model"),
    ("claude-opus-4-7-thinking",  "Claude Opus 4.7 Thinking",  "claude", "⭐⭐", "laozhang", "anthropic", "Deep reasoning mode"),
    ("claude-opus-4-6",           "Claude Opus 4.6",           "claude", "⭐⭐", "laozhang", "anthropic", "High-capability Opus model"),
    ("claude-sonnet-4-6",         "Claude Sonnet 4.6",         "claude", "⭐⭐", "laozhang", "anthropic", "Balanced speed, cost, and intelligence"),
    ("claude-sonnet-4-6-thinking","Claude Sonnet 4.6 Thinking","claude", "⭐",  "laozhang", "anthropic", "Reasoning mode"),
    ("claude-haiku-4-5",          "Claude Haiku 4.5",          "claude", "",   "laozhang", "anthropic", "Lightweight fast"),
    ("claude-opus-4-5",           "Claude Opus 4.5",           "claude", "",   "laozhang", "anthropic", "Classic high-performance version"),
    ("claude-opus-4-5-thinking",  "Claude Opus 4.5 Thinking",  "claude", "",   "laozhang", "anthropic", "Chain-of-thought mode"),
    ("claude-sonnet-4-5",         "Claude Sonnet 4.5",         "claude", "",   "laozhang", "anthropic", "Stable coding version"),
    ("claude-sonnet-4",           "Claude 4 Sonnet",           "claude", "⭐",  "laozhang", "anthropic", "Stable version"),
    ("claude-opus-4-1",           "Claude 4.1 Opus",           "claude", "",   "laozhang", "anthropic", "Enhanced version"),
    ("claude-3-7-sonnet-latest",  "Claude 3.7 Sonnet",         "claude", "",   "laozhang", "anthropic", "Legacy compatibility"),
    ("claude-3-5-sonnet-latest",  "Claude 3.5 Sonnet",         "claude", "",   "laozhang", "anthropic", "Balanced performance"),
    ("claude-3-5-haiku-latest",   "Claude 3.5 Haiku",          "claude", "",   "laozhang", "anthropic", "Lightweight fast"),
    # ── Grok ──
    ("grok-4",                 "Grok 4",                 "grok", "⭐", "laozhang", None, "Latest official version"),
    ("grok-4-fast-reasoning",  "Grok 4 Fast Reasoning",  "grok", "",  "laozhang", None, "Fast reasoning"),
    ("grok-4-fast",            "Grok 4 Fast",            "grok", "",  "laozhang", None, "Speed optimized"),
    ("grok-3-latest",          "Grok 3",                 "grok", "",  "laozhang", None, "Stable version"),
    ("grok-3-deepsearch",      "Grok 3 DeepSearch",      "grok", "",  "laozhang", None, "Deep search, per-call"),
    ("grok-3-mini-latest",     "Grok 3 Mini",            "grok", "",  "laozhang", None, "Small with reasoning"),
    # ── DeepSeek ──
    ("deepseek-v4-pro",        "DeepSeek V4 Pro",        "deepseek", "⭐⭐", "laozhang", "deepseek", "Strong capability"),
    ("deepseek-v4-flash",      "DeepSeek V4 Flash",      "deepseek", "⭐",  "laozhang", "deepseek", "Reliable"),
    ("deepseek-v3.2-exp",      "DeepSeek V3.2 Exp",      "deepseek", "⭐",  "laozhang", "deepseek", "Experimental latest"),
    ("deepseek-v3-1-250821",   "DeepSeek V3.1",          "deepseek", "⭐",  "laozhang", "deepseek", "Think/Non-Think dual mode"),
    ("deepseek-v3",            "DeepSeek V3",            "deepseek", "",   "laozhang", "deepseek", "Strong capability"),
    ("deepseek-r1",            "DeepSeek R1",            "deepseek", "",   "laozhang", "deepseek", "Reasoning model"),
    ("deepseek-chat",          "DeepSeek Chat",          "deepseek", "",   "laozhang", "deepseek", "Chat-optimized version"),
    ("deepseek-coder",         "DeepSeek Coder",         "deepseek", "",   "laozhang", "deepseek", "Code-specialized model"),
    # ── Qwen ──
    ("qwq-plus",               "QwQ Plus",               "qwen", "⭐⭐", "laozhang", None, "Latest reasoning model"),
    ("qwq-72b-preview",        "QwQ 72B Preview",        "qwen", "",   "laozhang", None, "Preview version"),
    ("qwen3-coder-480b-a35b-instruct", "Qwen3 Coder 480B", "qwen", "⭐⭐", "laozhang", None, "Large coding model"),
    ("qwen3-coder-plus",       "Qwen3 Coder Plus",       "qwen", "⭐",  "laozhang", None, "Enhanced coding"),
    ("qwen-max",               "Qwen Max",               "qwen", "",   "laozhang", None, "Strongest version"),
    ("qwen-plus",              "Qwen Plus",              "qwen", "",   "laozhang", None, "Enhanced version"),
    ("qwen-turbo",             "Qwen Turbo",             "qwen", "",   "laozhang", None, "Fast version"),
    ("qwen-2.5-72b",           "Qwen 2.5",               "qwen", "",   "laozhang", None, "Open-source large model"),
    # ── Other ──
    ("kimi-k2-250711",         "Kimi K2 Official",       "other", "⭐", "laozhang", None, "Official partnership, strong stability"),
    ("llama-4-maverick",       "Llama 4 Maverick",       "other", "⭐", "laozhang", None, "Latest open source"),
    ("Doubao-1.5-vision-pro-32k", "Doubao 1.5 Vision Pro", "other", "⭐", "laozhang", None, "Multimodal"),
    ("gemma-3-12b",            "Gemma 3 12B",            "other", "",  "laozhang", None, "Google open source"),
    ("ernie-4.0",              "ERNIE 4.0",              "other", "",  "laozhang", None, "Baidu's latest model"),
    ("glm-4",                  "GLM-4",                  "other", "",  "laozhang", None, "Tsinghua-based model"),
    ("spark-3.5",              "Spark 3.5",              "other", "",  "laozhang", None, "iFlytek's latest version"),
    ("minimax-abab6.5",        "MiniMax",                "other", "",  "laozhang", None, "Strong overall capabilities"),
]


def _mk(spec: tuple) -> Model:
    mid, disp, group, badge, route, direct, desc = spec
    ci, co = _est_cost(mid)
    mx = 16384 if route == "google" else 8192
    return Model(mid, disp, tier=group, group=group, desc=desc, badge=badge,
                 vision=_vision(mid), tools=True, max_tokens=mx,
                 chain=_chain(mid, ci, co, route, direct))


MODEL_REGISTRY: list[Model] = [_mk(s) for s in _SPEC]

_BY_ID: dict[str, Model] = {m.id: m for m in MODEL_REGISTRY}


def get_model(model_id: str) -> Optional[Model]:
    return _BY_ID.get(model_id)


def usable_chain(m: Model) -> list[Step]:
    """The model's chain filtered to providers whose creds are configured (#4)."""
    return [s for s in m.chain if provider_usable(s.provider)]


# No available channel on the current laozhang account, or times out in chat
# (live probe 2026-06-18). Kept in the catalog but flagged `down` → rendered red.
# (The 6 Gemini Google-route models were NOT probed — Vertex needs prod OAuth creds.)
DOWN_MODELS: set[str] = {
    "gemini-2.0-flash-001", "gpt-5.1-codex-high", "gpt-5-pro", "o3-pro",
    "claude-haiku-4-5", "claude-opus-4-5", "claude-opus-4-5-thinking", "claude-sonnet-4-5",
    "claude-sonnet-4", "claude-opus-4-1", "claude-3-7-sonnet-latest",
    "claude-3-5-sonnet-latest", "claude-3-5-haiku-latest",
    "grok-4-fast", "grok-3-deepsearch", "grok-3-mini-latest",
    "deepseek-coder", "qwq-72b-preview", "qwen-2.5-72b",
    "kimi-k2-250711", "llama-4-maverick", "Doubao-1.5-vision-pro-32k",
    "gemma-3-12b", "ernie-4.0", "glm-4", "spark-3.5", "minimax-abab6.5",
}


def list_models() -> list[dict]:
    """Payload for GET /chat/models — models with >=1 usable provider, each tagged
    with `route` (google = Vertex-OAuth endpoint; laozhang = gateway) and `down`
    (known-degraded → frontend renders red)."""
    out: list[dict] = []
    for m in MODEL_REGISTRY:
        uc = usable_chain(m)
        if not uc:
            continue
        d = m.public()
        kind = PROVIDERS.get(uc[0].provider, {}).get("kind", "")
        d["route"] = "google" if kind in ("google_vertex", "google_key") else "laozhang"
        d["down"] = m.id in DOWN_MODELS
        out.append(d)
    return out


def step_usd(step: Step, tok_in: int, tok_out: int) -> float:
    """Real upstream USD for THIS provider+model (accurate metering #5)."""
    return round((tok_in * step.cost_in + tok_out * step.cost_out) / 1_000_000, 8)


# ──────────────────────────────────────────────────────────────────────────────
# Error classification (Decision #3): which failures fail over vs surface.
# ──────────────────────────────────────────────────────────────────────────────
class FailoverError(Exception):
    """Provider unavailable for THIS call (credit/quota/rate/auth/5xx) -> try next."""


class RealError(Exception):
    """A genuine request error (e.g. 400 invalid) -> do NOT fail over; surface."""


_FAILOVER_HINTS = ("insufficient", "quota", "balance", "exceeded", "rate limit",
                   "overloaded", "unavailable", "capacity")


def classify(exc: Exception) -> Exception:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None) or \
             getattr(getattr(exc, "response", None), "status_code", None)
    msg = str(exc).lower()
    if status in (401, 402, 403, 408, 409, 429) or (isinstance(status, int) and status >= 500):
        return FailoverError(str(exc))
    if status == 400:
        return RealError(str(exc))
    if any(h in msg for h in _FAILOVER_HINTS):
        return FailoverError(str(exc))
    # Unknown/transport error -> treat as failover (give the next provider a shot).
    return FailoverError(str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Provider adapters — each is an async generator yielding normalized events:
#   {"type":"text","text": str}            a token / text delta
#   {"type":"usage","tok_in":int,"tok_out":int}   final usage (once, at the end)
# Adapters raise on upstream error; the dispatcher classifies + fails over.
# ──────────────────────────────────────────────────────────────────────────────
def _build_messages(system: str, history: list[dict], prompt: str,
                    images: Optional[list[dict]]) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": system}] if system else []
    msgs.extend(history or [])
    if images:
        parts: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            b64, mime = img.get("b64", ""), img.get("mime", "image/png")
            if b64:
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{mime};base64,{b64}"}})
        msgs.append({"role": "user", "content": parts})
    else:
        msgs.append({"role": "user", "content": prompt})
    return msgs


async def _stream_openai_compat(provider: dict, step: Step, system: str,
                                history: list[dict], prompt: str, temperature: float,
                                max_tokens: int, images) -> AsyncIterator[dict]:
    """laozhang / openai / deepseek — OpenAI-compatible streaming."""
    from openai import AsyncOpenAI
    key = os.getenv(provider["key_env"], "")
    client = AsyncOpenAI(api_key=key, base_url=provider["base_url"])
    messages = _build_messages(system, history, prompt, images)
    # GPT-5 / o-series need max_completion_tokens (>=1) + default temperature, not max_tokens.
    _reason = (step.model or "").lower().startswith(("gpt-5", "o1", "o3", "o4"))
    _mt = max(1, int(max_tokens or 0))
    _env_t = os.getenv("CHAT_TEMPERATURE", "").strip()  # one tunable knob, overrides request value
    try:
        _temp = float(_env_t) if _env_t else float(temperature)
    except (TypeError, ValueError):
        _temp = float(temperature)
    _gen = {"extra_body": {"max_completion_tokens": _mt}} if _reason else {"temperature": _temp, "max_tokens": _mt}
    try:
        stream = await client.chat.completions.create(
            model=step.model, messages=messages, stream=True,
            stream_options={"include_usage": True}, **_gen)
        usage = {"tok_in": 0, "tok_out": 0}
        async for chunk in stream:
            if chunk.usage:
                usage = {"tok_in": chunk.usage.prompt_tokens or 0,
                         "tok_out": chunk.usage.completion_tokens or 0}
            for ch in (chunk.choices or []):
                delta = getattr(ch.delta, "content", None)
                if delta:
                    yield {"type": "text", "text": delta}
        yield {"type": "usage", **usage}
    except Exception as e:
        raise classify(e)


async def _stream_anthropic(provider: dict, step: Step, system: str,
                            history: list[dict], prompt: str, temperature: float,
                            max_tokens: int, images) -> AsyncIterator[dict]:
    """Anthropic Messages API streaming (requires `anthropic` package + key)."""
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic(api_key=os.getenv(provider["key_env"], ""))
    # Anthropic takes system as a top-level arg; history/user as messages.
    a_msgs = [m for m in (history or []) if m.get("role") in ("user", "assistant")]
    a_msgs.append({"role": "user", "content": prompt})
    try:
        usage = {"tok_in": 0, "tok_out": 0}
        async with client.messages.stream(
            model=step.model, system=system or "", messages=a_msgs,
            temperature=temperature, max_tokens=max_tokens) as stream:
            async for text in stream.text_stream:
                if text:
                    yield {"type": "text", "text": text}
            final = await stream.get_final_message()
            if final and final.usage:
                usage = {"tok_in": final.usage.input_tokens or 0,
                         "tok_out": final.usage.output_tokens or 0}
        yield {"type": "usage", **usage}
    except Exception as e:
        raise classify(e)


async def _stream_google(provider_name: str, step: Step, system: str,
                         history: list[dict], prompt: str, temperature: float,
                         max_tokens: int, images) -> AsyncIterator[dict]:
    """Google GenAI streaming — Vertex OAuth (`vertex`) or API key (`google_key`)."""
    from google import genai as _genai
    from google.genai import types as _gt
    if provider_name == "vertex":
        from laozhang_api import _genai_client          # cached Vertex-OAuth client
        client = _genai_client()
        if client is None:
            raise FailoverError("vertex not configured")
    else:
        client = _genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    # Flatten history+prompt into contents; images appended as inline parts.
    parts = [_gt.Part.from_text(prompt)]
    for img in (images or []):
        if img.get("b64"):
            import base64 as _b64
            parts.append(_gt.Part.from_bytes(
                data=_b64.b64decode(img["b64"]), mime_type=img.get("mime", "image/png")))
    cfg = _gt.GenerateContentConfig(
        system_instruction=system or None, temperature=temperature,
        max_output_tokens=max_tokens)
    try:
        usage = {"tok_in": 0, "tok_out": 0}
        stream = await client.aio.models.generate_content_stream(
            model=step.model, contents=parts, config=cfg)
        async for ev in stream:
            if getattr(ev, "text", None):
                yield {"type": "text", "text": ev.text}
            um = getattr(ev, "usage_metadata", None)
            if um:
                usage = {"tok_in": getattr(um, "prompt_token_count", 0) or 0,
                         "tok_out": getattr(um, "candidates_token_count", 0) or 0}
        yield {"type": "usage", **usage}
    except Exception as e:
        raise classify(e)


async def _run_adapter(step: Step, **kw) -> AsyncIterator[dict]:
    p = PROVIDERS[step.provider]
    kind = p["kind"]
    if kind == "openai_compat":
        async for ev in _stream_openai_compat(p, step, **kw):
            yield ev
    elif kind == "anthropic":
        async for ev in _stream_anthropic(p, step, **kw):
            yield ev
    elif kind in ("google_vertex", "google_key"):
        async for ev in _stream_google(step.provider, step, **kw):
            yield ev
    else:
        raise RealError(f"unknown provider kind {kind}")


# ──────────────────────────────────────────────────────────────────────────────
# DISPATCHER — the heart. Walks the model's provider chain with credit
# hold/settle/refund per attempt and failover. Yields SSE-ready chunk strings
# (same markers the frontend already understands), so wiring is a drop-in.
# ──────────────────────────────────────────────────────────────────────────────
async def dispatch_chat(*, tenant_id: Optional[str], user_id: Optional[str],
                        model_id: str, system: str, history: list[dict], prompt: str,
                        temperature: float = 0.9, max_tokens: int = 0,
                        images: Optional[list[dict]] = None,
                        op_base: str = "", lang: str = "id",
                        byok: bool = False, cancel_check=None) -> AsyncIterator[str]:
    """Walk model_id's provider chain with per-attempt credit hold/settle/refund and
    failover. Yields SSE-ready chunk strings (raw text + [USAGE]/[DONE]/[ERROR]/
    [CANCELLED]/[RESTART] markers). `cancel_check` is an optional async callable
    returning True to abort; `byok` skips the credit hold (user pays upstream)."""
    import metering as _metering
    import credit_catalog as _cat

    m = get_model(model_id)
    if m is None:
        yield f"[ERROR: unknown model {model_id}]"
        yield "[DONE]"
        return
    chain = usable_chain(m)
    if not chain:
        yield "[ERROR: no provider configured for this model]"
        yield "[DONE]"
        return

    mt = max_tokens or m.max_tokens
    prompt_chars = len(prompt or "") + sum(len(str(h.get("content", ""))) for h in (history or []))
    est_units = _cat.estimate_chat_credits(model_id, prompt_chars=prompt_chars, max_tokens=mt)
    op_base = op_base or f"chat:{model_id}"

    for idx, step in enumerate(chain):
        op_id = f"{op_base}:{step.provider}:{idx}"
        # HOLD the user's credit for this attempt (402 if the USER is out of credit;
        # that's the user's balance, not a provider failover — surface it).
        try:
            charge = await _metering.begin_charge(
                tenant_id=tenant_id, user_id=user_id, operation="chat",
                model=model_id, estimate_units={"tokens_in": prompt_chars // 4, "tokens_out": mt},
                op_id=op_id, byok=byok)
        except Exception as e:
            # HTTPException(402) from metering -> user out of credit; stop, surface.
            yield f"[ERROR: {getattr(e, 'detail', e)}]"
            yield "[DONE]"
            return

        emitted = False
        usage = {"tok_in": 0, "tok_out": 0}
        try:
            async for ev in _run_adapter(
                    step, system=system, history=history, prompt=prompt,
                    temperature=temperature, max_tokens=mt, images=images):
                if cancel_check is not None and await cancel_check():
                    await charge.refund()      # user-initiated cancel → no charge
                    yield "[CANCELLED]"
                    return
                if ev["type"] == "text":
                    emitted = True
                    yield ev["text"]
                elif ev["type"] == "usage":
                    usage = {"tok_in": ev["tok_in"], "tok_out": ev["tok_out"]}
            # SUCCESS: commit the hold at THIS provider's real cost (#5).
            usd = step_usd(step, usage["tok_in"], usage["tok_out"])
            await charge.settle({"tokens_in": usage["tok_in"], "tokens_out": usage["tok_out"]},
                                tok_in=usage["tok_in"], tok_out=usage["tok_out"],
                                provider=step.provider, usd=usd)
            yield f"[USAGE:{json.dumps({'input': usage['tok_in'], 'output': usage['tok_out'], 'provider': step.provider})}]"
            yield "[DONE]"
            return
        except FailoverError as e:
            await charge.refund()                         # discard this attempt's cost
            log.warning("failover %s/%s: %s", model_id, step.provider, e)
            if emitted:
                yield "[RESTART]"                          # client clears partial (#2)
            continue                                       # try next provider
        except RealError as e:
            await charge.refund()
            yield f"[ERROR: {e}]"
            yield "[DONE]"
            return
        except Exception as e:                             # unexpected -> treat as failover
            await charge.refund()
            log.warning("failover(unexpected) %s/%s: %s", model_id, step.provider, e)
            if emitted:
                yield "[RESTART]"
            continue

    # All providers exhausted (#1).
    busy = ("Maaf, server lagi sibuk. Coba lagi sebentar ya 🙏"
            if lang == "id" else "Sorry, the server is busy right now. Please try again shortly.")
    yield f"[ERROR: {busy}]"
    yield "[DONE]"
