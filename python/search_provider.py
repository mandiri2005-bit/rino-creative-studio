# ── search_provider — FG-SEARCH §1: thin provider-agnostic web search for the fact-gate.
# NOT part of llm routing (search is a tool, not a model). Tavily primary (include_answer
# gives an LLM-ready cited summary) + Serper fallback; circuit breaker per provider; both
# down → PROVIDER_DOWN and the claim stays NEEDS-VERIFY (same guard class as UNMEASURED —
# never PASS on infrastructure failure). stdlib-only (urllib), 10s timeout, no retries on
# quota errors (trip the breaker instead).
# Env: TAVILY_API_KEY, SERPER_API_KEY — absent keys simply skip that provider.
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["Snippet", "search", "provider_status", "PROVIDER_DOWN"]

PROVIDER_DOWN = "PROVIDER_DOWN"
_TIMEOUT = float(os.environ.get("FACTGATE_SEARCH_TIMEOUT", "10"))
_BREAKER_FAILS = 3          # consecutive failures to trip
_BREAKER_COOLDOWN = 300.0   # seconds

_TAG = re.compile(r"<[^>]+>")


@dataclass
class Snippet:
    title: str
    url: str
    text: str
    provider: str
    answer: str = ""            # Tavily's cited summary (first snippet carries it)
    retrieved_at: float = field(default_factory=time.time)


class _Breaker:
    def __init__(self) -> None:
        self.fails = 0
        self.until = 0.0

    def ok(self) -> bool:
        return time.time() >= self.until

    def hit(self) -> None:
        self.fails += 1
        if self.fails >= _BREAKER_FAILS:
            self.until = time.time() + _BREAKER_COOLDOWN
            self.fails = 0

    def reset(self) -> None:
        self.fails = 0
        self.until = 0.0


_BREAKERS: dict[str, _Breaker] = {"tavily": _Breaker(), "serper": _Breaker()}


def _clean(s: str, limit: int = 600) -> str:
    """FG-SEARCH §3.2 injection guard: strip tags, collapse space, truncate."""
    return re.sub(r"\s+", " ", _TAG.sub(" ", s or "")).strip()[:limit]


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "wimba-factgate/1.0", **headers})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _tavily(query: str, max_results: int) -> list[Snippet]:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        raise LookupError("no TAVILY_API_KEY")
    d = _post("https://api.tavily.com/search",
              {"api_key": key, "query": query, "max_results": max_results,
               "include_answer": True}, {})
    out = []
    ans = _clean(d.get("answer") or "", 900)
    for i, r in enumerate(d.get("results") or []):
        out.append(Snippet(title=_clean(r.get("title", ""), 160), url=r.get("url", ""),
                           text=_clean(r.get("content", "")), provider="tavily",
                           answer=(ans if i == 0 else "")))
    return out


def _serper(query: str, max_results: int) -> list[Snippet]:
    key = os.environ.get("SERPER_API_KEY", "").strip()
    if not key:
        raise LookupError("no SERPER_API_KEY")
    d = _post("https://google.serper.dev/search", {"q": query, "num": max_results},
              {"X-API-KEY": key})
    return [Snippet(title=_clean(r.get("title", ""), 160), url=r.get("link", ""),
                    text=_clean(r.get("snippet", "")), provider="serper")
            for r in (d.get("organic") or [])[:max_results]]


_PROVIDERS = [("tavily", _tavily), ("serper", _serper)]


def search(query: str, max_results: int = 5) -> list[Snippet] | str:
    """Chain: Tavily → Serper. Returns snippets, or PROVIDER_DOWN when every provider is
    keyless/tripped/failing — callers must treat that as NEEDS-VERIFY, never PASS."""
    for name, fn in _PROVIDERS:
        br = _BREAKERS[name]
        if not br.ok():
            continue
        try:
            snips = fn(query, max_results)
            br.reset()
            if snips:
                return snips
        except LookupError:
            continue                     # keyless — not a failure, just unavailable
        except Exception:                # noqa: BLE001 — 4xx/5xx/timeout/quota
            br.hit()
            continue
    return PROVIDER_DOWN


def provider_status() -> dict[str, Any]:
    return {name: {"keyed": bool(os.environ.get(f"{name.upper()}_API_KEY", "").strip()),
                   "breaker_open": not br.ok()}
            for name, br in _BREAKERS.items()}
