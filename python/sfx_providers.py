"""SFX (sound-effects) generation for the Wimba Music app (App 4 of the Creator Suite).

AI generation is SFX-ONLY — NO AI MUSIC (hard copyright constraint; music is CC0-library-only).
Mirrors tts_providers.py: a registry (audio_registry.json) drives a cheapest-first failover `chain`;
every step rides the FAL_API_KEY we already hold. The laozhang_api /sfx/generate endpoint owns the
billing (hold the ceiling → synth → persist Vault → commit the real cost / refund on fail); this
module just synthesizes ONE clip and reports the winning step's normalized USD so the debit is real.

Pricing normalization: the GATE holds estimate_usd() = the CEILING across all chain steps for the
requested (max-capped) duration, so a failover to a pricier step can never exceed the reservation.
synth_sfx() returns the WINNER's actual cost_usd, and commit settles to that (refunding the unused
hold). cost = usd_flat OR usd_per_s * effective_seconds (effective = min(duration, step.max_duration)).
"""
from __future__ import annotations

import os
import json
import base64
import asyncio
from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)
_POLL_EVERY = float(os.getenv("SFX_POLL_EVERY", "2.0"))
_POLL_MAX = float(os.getenv("SFX_POLL_MAX", "180.0"))
_MAX_AUDIO_BYTES = int(os.getenv("SFX_MAX_AUDIO_BYTES", str(25 * 1024 * 1024)))
_MAX_DURATION = int(os.getenv("SFX_MAX_DURATION", "30"))


class ProviderError(RuntimeError):
    """Raised by an adapter when a provider call fails (→ failover to next step)."""


def _key(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise ProviderError(f"missing env {name}")
    return v


# ── registry ──────────────────────────────────────────────────────────────────
def _registry_path() -> str:
    return os.getenv("AUDIO_REGISTRY_PATH") or os.path.join(os.path.dirname(__file__), "audio_registry.json")


def _load_registry() -> dict:
    try:
        with open(_registry_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # never crash the whole API on a malformed/absent registry
        print(f"[sfx_providers] registry load failed: {e}")
        return {"sfx": {"chain": []}}


_REGISTRY = _load_registry()


def reload_registry() -> dict:
    """Re-read the registry from disk (used by tests / hot config swaps)."""
    global _REGISTRY
    _REGISTRY = _load_registry()
    return _REGISTRY


def _sfx() -> dict:
    return _REGISTRY.get("sfx", {}) or {}


def _chain() -> list:
    return _sfx().get("chain", []) or []


def clamp_duration(duration) -> int:
    """Clamp the requested duration to [1, registry/global max]. The UI caps at 30s; defense-in-depth."""
    cap = int(_sfx().get("max_duration") or _MAX_DURATION)
    try:
        d = int(round(float(duration)))
    except Exception:
        d = 0
    return max(1, min(d, cap))


def _step_cost(step: dict, duration: int) -> float:
    """Normalized USD for one chain step at the effective (step-max-capped) duration."""
    eff = min(int(duration), int(step.get("max_duration") or duration))
    if step.get("usd_flat") is not None:
        return round(float(step["usd_flat"]), 6)
    return round(float(step.get("usd_per_s") or 0.0) * max(0, eff), 6)


def estimate_usd(duration) -> float:
    """CEILING USD across all chain steps for the (clamped) duration — what the gate HOLDS, so any
    failover step is always covered. Returns 0.0 if the chain is empty (→ caller treats as unconfigured)."""
    d = clamp_duration(duration)
    steps = _chain()
    if not steps:
        return 0.0
    return max(_step_cost(s, d) for s in steps)


# ── template substitution ($PROMPT str, $DURATION numeric) ─────────────────────
def _render(tmpl, ctx: dict):
    if isinstance(tmpl, str):
        if tmpl == "$DURATION":
            return ctx["duration"]                       # keep numeric type
        return tmpl.replace("$PROMPT", ctx["prompt"]).replace("$DURATION", str(ctx["duration"]))
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
    return None


# ── adapter (one clip → bytes, mime) ───────────────────────────────────────────
async def _fal_sfx(client, step: dict, ctx: dict) -> tuple[bytes, str]:
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


_ADAPTERS = {"fal": _fal_sfx}


async def synth_sfx(prompt: str, duration) -> dict:
    """Synthesize ONE sound-effect via the cheapest-first failover chain.
    Returns {audio: bytes, mime, cost_usd, model, served_by}. Raises ProviderError if the chain is
    empty (not configured) or every step fails. cost_usd = the WINNING step's normalized USD."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ProviderError("empty prompt")
    d = clamp_duration(duration)
    steps = _chain()
    if not steps:
        raise ProviderError("sfx generation not configured")
    ctx = {"prompt": prompt, "duration": d}

    errors = []
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for step in steps:
            prov = step.get("provider")
            fn = _ADAPTERS.get(prov)
            if not fn:
                errors.append(f"{prov}:unknown-step-provider")
                continue
            try:
                data, mime = await fn(client, step, ctx)
                if not data:
                    raise ProviderError("empty audio")
                return {"audio": data, "mime": mime or "audio/mpeg",
                        "cost_usd": _step_cost(step, d), "model": step.get("slug"),
                        "served_by": step.get("slug")}
            except Exception as e:  # noqa: BLE001 — failover on ANY step failure
                errors.append(f"{step.get('slug')}:{str(e)[:120]}")
                continue
    raise ProviderError("all sfx providers failed → " + " | ".join(errors))
