"""TTS multi-provider failover synth for the Wimba voiceover expansion (App 3).

Mirrors image_providers.py: a registry (tts_registry.json) drives a per-(provider, model)
failover chain; each adapter synthesizes ONE chunk and returns (audio_bytes, mime). The Node
/api/tts/start runner owns the job loop (paragraph split, WAV/MP3 write to TTS_DIR, Vault
persist, Redis live-job, cancel flag, credit debit); this module just synthesizes a chunk and
reports the winning model's normalized upstream cost so Node can debit the REAL COGS.

Why Python: the FAL / AIMLAPI keys live ONLY on this service (Node holds none), so all new-provider
synthesis must run here. google + openai(tts-1/hd) stay on the legacy Node runners untouched.

Billing-unit normalization: every model carries ONE rate (usd_per_1k_chars); cost_usd is computed
uniformly as rate * chars/1000 regardless of which chain step served the request (predictable debit).
"""
from __future__ import annotations

import os
import json
import base64
import asyncio
from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_POLL_EVERY = float(os.getenv("TTS_POLL_EVERY", "2.0"))
_POLL_MAX = float(os.getenv("TTS_POLL_MAX", "180.0"))
_MAX_AUDIO_BYTES = int(os.getenv("TTS_MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))


class ProviderError(RuntimeError):
    """Raised by an adapter when a provider call fails (→ failover to next step)."""


def _key(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ProviderError(f"missing env {name}")
    return v


# ── registry ──────────────────────────────────────────────────────────────────
def _registry_path() -> str:
    return os.getenv("TTS_REGISTRY_PATH") or os.path.join(os.path.dirname(__file__), "tts_registry.json")


def _load_registry() -> dict:
    try:
        with open(_registry_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # never crash the whole API on a malformed/absent registry
        print(f"[tts_providers] registry load failed: {e}")
        return {"providers": {}}


_REGISTRY = _load_registry()


def reload_registry() -> dict:
    """Re-read the registry from disk (used by tests / hot config swaps)."""
    global _REGISTRY
    _REGISTRY = _load_registry()
    return _REGISTRY


def _providers() -> dict:
    return _REGISTRY.get("providers", {}) or {}


def has_provider(provider: str) -> bool:
    return (provider or "").strip().lower() in _providers()


def model_entry(provider: str, model: str) -> Optional[dict]:
    p = _providers().get((provider or "").strip().lower())
    if not p:
        return None
    return (p.get("models") or {}).get(model)


def is_indonesian(language: str) -> bool:
    l = (language or "").strip().lower()
    return l in ("indonesian", "id", "id-id", "bahasa")


def estimate_usd(provider: str, model: str, chars: int) -> Optional[float]:
    """Normalized upstream USD for `chars` characters. Returns None if `provider` is not a
    registry provider (caller then falls back to the catalog 'tts' flat rate for google/laozhang).
    Raises ProviderError if the provider IS in the registry but the model isn't (so the gate 400s)."""
    p = _providers().get((provider or "").strip().lower())
    if not p:
        return None
    m = (p.get("models") or {}).get(model)
    if not m:
        raise ProviderError(f"unknown tts model {model!r} for provider {provider!r}")
    rate = float(m.get("usd_per_1k_chars") or 0.0)
    return rate * max(0, int(chars or 0)) / 1000.0


# ── template substitution (data-driven request shapes) ─────────────────────────
def _render(tmpl, ctx: dict):
    if isinstance(tmpl, str):
        return (tmpl.replace("$TEXT", ctx["text"])
                    .replace("$VOICE", ctx["voice"])
                    .replace("$LANG", ctx["language"]))
    if isinstance(tmpl, dict):
        return {k: _render(v, ctx) for k, v in tmpl.items()}
    if isinstance(tmpl, list):
        return [_render(v, ctx) for v in tmpl]
    return tmpl


# ── shared HTTP helpers (poll + size-capped fetch) ─────────────────────────────
async def _poll(client, url: str, headers: dict, *, done, err=None,
                interval=_POLL_EVERY, max_s=_POLL_MAX):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_s
    while loop.time() < deadline:
        r = await client.get(url, headers=headers, timeout=15.0)
        r.raise_for_status()
        j = r.json()
        if err and err(j):
            raise ProviderError(f"provider job failed: {str(j)[:200]}")
        if done(j):
            return True
        await asyncio.sleep(interval)
    raise ProviderError("poll timeout")


async def _capped_get(client, url: str, headers: Optional[dict] = None) -> tuple[bytes, str]:
    async with client.stream("GET", url, headers=headers or {}) as r:
        r.raise_for_status()
        mime = (r.headers.get("content-type", "application/octet-stream").split(";")[0]).strip()
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) > _MAX_AUDIO_BYTES:
                raise ProviderError("audio exceeds size cap")
        return bytes(buf), (mime or "audio/mpeg")


def _extract_audio_url(out: dict) -> Optional[str]:
    if not isinstance(out, dict):
        return None
    a = out.get("audio")
    if isinstance(a, dict) and a.get("url"):
        return a["url"]
    if isinstance(a, str) and a.startswith("http"):
        return a
    for k in ("audio_url", "url"):
        v = out.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    af = out.get("audio_file")
    if isinstance(af, dict) and af.get("url"):
        return af["url"]
    return None


def _extract_audio_b64(out: dict) -> Optional[str]:
    if not isinstance(out, dict):
        return None
    for k in ("audio_base64", "audio_b64", "b64_json"):
        v = out.get(k)
        if isinstance(v, str) and v:
            return v
    d = out.get("data")
    if isinstance(d, list) and d and isinstance(d[0], dict) and d[0].get("b64_json"):
        return d[0]["b64_json"]
    return None


# ── adapters (one chunk → bytes, mime) ─────────────────────────────────────────
async def _fal_tts(client, step: dict, ctx: dict) -> tuple[bytes, str]:
    h = {"Authorization": f"Key {_key('FAL_API_KEY')}", "Content-Type": "application/json"}
    body = _render(step.get("body", {}), ctx)
    r = await client.post(f"https://queue.fal.run/{step['slug']}", headers=h, json=body)
    r.raise_for_status()
    j = r.json()
    status_url, resp_url = j.get("status_url"), j.get("response_url")
    if not status_url or not resp_url:
        raise ProviderError(f"fal: no queue urls ({str(j)[:160]})")
    await _poll(client, status_url, h,
                done=lambda s: s.get("status") == "COMPLETED",
                err=lambda s: s.get("status") in ("FAILED", "ERROR"))
    rr = await client.get(resp_url, headers=h)
    rr.raise_for_status()
    out = rr.json()
    url = _extract_audio_url(out)
    if url:
        return await _capped_get(client, url)
    b64 = _extract_audio_b64(out)
    if b64:
        return base64.b64decode(b64), "audio/mpeg"
    raise ProviderError(f"fal: no audio in response ({str(out)[:160]})")


async def _aiml_tts(client, step: dict, ctx: dict) -> tuple[bytes, str]:
    h = {"Authorization": f"Bearer {_key('AIMLAPI_API_KEY')}", "Content-Type": "application/json"}
    endpoint = step["endpoint"]
    body = _render(step.get("body", {}), ctx)
    mode = step.get("response", "direct_bytes")
    r = await client.post(endpoint, headers=h, json=body)
    r.raise_for_status()
    ct = (r.headers.get("content-type", "").split(";")[0]).strip()
    if mode == "direct_bytes" and (ct.startswith("audio/") or ct == "application/octet-stream"):
        return r.content, (ct or "audio/mpeg")
    # JSON-wrapped audio (url or base64) — also the fallback when direct_bytes returned JSON
    try:
        out = r.json()
    except Exception:
        if r.content:
            return r.content, (ct or "audio/mpeg")
        raise ProviderError("aiml: empty non-json response")
    url = _extract_audio_url(out)
    if url:
        return await _capped_get(client, url)
    b64 = _extract_audio_b64(out)
    if b64:
        return base64.b64decode(b64), "audio/mpeg"
    raise ProviderError(f"aiml: no audio in response ({str(out)[:160]})")


_ADAPTERS = {"fal": _fal_tts, "aiml": _aiml_tts}


async def synth(provider: str, model: str, voice: str, language: str, text: str) -> dict:
    """Synthesize ONE chunk via the model's cheapest-first failover chain.
    Returns {audio: bytes, mime, cost_usd, provider, model, served_by}. Raises ProviderError if
    every chain step fails. cost_usd = model.usd_per_1k_chars * chars/1000 (normalized, x1 markup)."""
    provider = (provider or "").strip().lower()
    p = _providers().get(provider)
    if not p:
        raise ProviderError(f"unknown tts provider: {provider!r}")
    m = (p.get("models") or {}).get(model)
    if not m:
        raise ProviderError(f"unknown tts model {model!r} for provider {provider!r}")
    if not m.get("enabled", True):
        raise ProviderError(f"tts model {model!r} is disabled")
    if m.get("no_indonesian") and is_indonesian(language):
        raise ProviderError(f"tts model {model!r} has no Indonesian voice")
    if not (text or "").strip():
        raise ProviderError("empty text")

    voice = (voice or p.get("default_voice") or "").strip()
    ctx = {"text": text, "voice": voice, "language": (language or "")}
    rate = float(m.get("usd_per_1k_chars") or 0.0)
    cost = round(rate * max(0, len(text)) / 1000.0, 6)

    errors = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for step in (m.get("chain") or []):
            prov = step.get("provider")
            fn = _ADAPTERS.get(prov)
            if not fn:
                errors.append(f"{prov}:unknown-step-provider")
                continue
            try:
                data, mime = await fn(client, step, ctx)
                if not data:
                    raise ProviderError("empty audio")
                return {"audio": data, "mime": mime or "audio/mpeg", "cost_usd": cost,
                        "provider": provider, "model": model, "served_by": prov}
            except Exception as e:  # noqa: BLE001 — failover on ANY step failure
                errors.append(f"{prov}:{str(e)[:120]}")
                continue
    raise ProviderError("all tts providers failed → " + " | ".join(errors))
