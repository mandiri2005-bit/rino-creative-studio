# -*- coding: utf-8 -*-
"""
Project Dalang — narration_api (WS-8, runtime convergence).

ONE production job contract for narration across the Python and Node paths.

This module is the consolidation of the (non-existent-in-repo) `narration_api.py`
prototype: a single background-job runtime that drives the unified orchestration
engine (`orchestrator.router.generate_narration`) and exposes it as a clean,
pollable job — the SAME contract whether the request arrives from the Python UI
(Gradio / direct) or from the Node Google path (backend/server.js).

It does NOT re-implement client/routing/RAG/assembly/anti-drift — those all live
in the `orchestrator` + `pakem` packages (WS-1..WS-7). WS-8 only adds the
*production envelope* around a generation run, reusing the EXACT primitives the
existing video / TTS / narasi jobs already use:

  * Auth        — auth_middleware.get_current_user (Clerk JWT → tenant/user).
  * Job row     — database.create_narasi_job / finish_narasi_job (asyncpg, RLS
                  via the tenant-scoped query helpers). The jobs table is the
                  durable source of truth for status/result/error.
  * Progress    — redis_client: a per-chapter HASH `narration:{id}:chapters`
                  (chapter:N = pending→running→done/failed) that the UI polls so
                  individual checkboxes light up as `asyncio.as_completed` lands
                  each chapter; plus rc.set_progress for the human string.
  * Cancel      — redis_client cancel flag `narration_{id}` (rc.set_cancel /
                  rc.is_cancelled), checked by the runtime between chapters.
  * Status m/c  — running → polishing → done | failed | cancelled, mirrored into
                  both Redis (`narration:{id}:status`) and the jobs row.
  * Metering    — a credit HOLD across the whole (long) job via
                  metering.begin_charge (HTTP 402 up front if short), kept warm
                  with credits.touch_hold so its TTL never lapses mid-flight, and
                  settled at ACTUAL token cost (refunded on cancel / zero output).
  * usage_logs  — cost rows written from the orchestrator's per-call telemetry
                  sink (tokens in/out + estimated USD) → database.log_usage.

Endpoints (registered on the SHARED `laozhang_api.app`):
  * POST /narration            → 202; init the Redis checkbox hash (expire 1h),
                                 create the jobs row, HOLD credits, kick off
                                 generate_narration in a background task; returns
                                 {job_id, status:"running", total}.
  * GET  /narration/{id}       → {status, done, total, chapters:[...], error,
                                 progress, output?}. Reads Redis (fast) first,
                                 falls back to the durable jobs row.
  * POST /narration/{id}/cancel→ set the cancel flag; the runtime stops after the
                                 current chapter and refunds the unused hold.

Importing this module registers the routes as a side effect (it shares the one
FastAPI app). To activate, `import narration_api` after `laozhang_api` is loaded
(e.g. add `import narration_api  # noqa: F401` near the bottom of laozhang_api,
or import it in app.py). It is additive and never shadows existing routes.

Smoke-safe: every heavy dependency (db, redis, metering, orchestrator) is reused
by import, and every call into them is wrapped so a missing live backend degrades
to a best-effort no-op rather than crashing the module import or a request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, HTTPException

# Reuse the ONE app + the real production primitives. These imports are the whole
# point of WS-8 convergence — import, never reinvent.
from laozhang_api import app, _resolve_user_uuid  # shared FastAPI app + Clerk→UUID
import redis_client as rc
import database as db
import metering
import credits as credits_lib
from auth_middleware import get_current_user, get_current_user_optional, CurrentUser

# The orchestration engine front door (WS-6). NEVER raises into us.
from orchestrator.router import generate_narration
# Telemetry record type so the usage sink can read tokens/cost off each call.
from orchestrator.core import CallTelemetry, _extract as _core_extract, estimate_cost as _core_cost

log = logging.getLogger("narration_api")

# ---------------------------------------------------------------------------
# Redis key layout for a narration job. The jobs table stays the durable source
# of truth; Redis only fronts the fast-changing per-chapter checkbox state and
# the status/progress strings so GET /narration/{id} is cheap and the DB isn't
# written on every chapter tick.
# ---------------------------------------------------------------------------
_CHAPTERS_TTL = 3600          # 1h — the checkbox hash + status expire together
_STATUS_PENDING = "pending"
_STATUS_RUNNING = "running"
_STATUS_POLISHING = "polishing"
_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_STATUS_CANCELLED = "cancelled"

# Map the runtime status → the jobs.status_enum the DB accepts (running/polishing
# both persist as 'processing'; terminal states map 1:1 except 'failed'→'error').
_DB_STATUS = {
    _STATUS_RUNNING: "processing",
    _STATUS_POLISHING: "processing",
    _STATUS_DONE: "done",
    _STATUS_FAILED: "error",
    _STATUS_CANCELLED: "cancelled",
}



def _premise_term_in_topic(term: str, topic: str) -> bool:
    """ROUND-10: moved to narasi_counters.premise_term_in_topic (static needs it too
    and cannot import this module); thin delegation kept for call sites and tests."""
    import narasi_counters as _pnc
    return _pnc.premise_term_in_topic(term, topic)


def _r7_env_on(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _wq(s, n: int = 110) -> str:
    """EVIDENCE-LOCATABILITY (2026-07-17): quote-wrap a manuscript-verbatim snippet so
    laozhang_api.py's _narasi_revise_chunked._spans() can find it — that regex only
    extracts text sitting BETWEEN literal quote characters, so an evidence string with
    no quote marks at all is silently unmappable (_spans() == [], the violation never
    reaches a chapter, NARASI_*_ENFORCE lands nothing) even when the underlying text is
    a real, locatable manuscript excerpt. _spans() also caps the captured span at 120
    chars — truncate to fit, but ONLY at a whitespace boundary, never mid-word: a
    mid-word cut would make the trailing edge fail _occ()'s \\b-bounded match against
    the original text (the char right after the cut, in the real manuscript, is not a
    word boundary). Returns '' for empty/falsy input so callers don't ship a bare '\"\"'
    violation string."""
    s = str(s or "").strip()
    if not s:
        return ""
    if len(s) > n:
        _head = s[:n]
        s = _head.rsplit(" ", 1)[0] if " " in _head else _head
    return f'"{s}"' if s else ""


def _canon_chapter_lookup(chapters) -> dict:
    """CANON-FORK CLASSIFIER WIDENING (2026-07-17, part a/b): build a {1-indexed chapter
    number -> content} map from result['chapters']. chapter_records' 'no' is 0-INDEXED
    (orchestrator/static.py chapter_records construction) while every human/model-facing
    chapter number elsewhere in this codebase (headings, @chN locators, canon-diff's own
    "chapter" field) is no+1 — keying on the raw 'no' would silently point every lookup one
    chapter early. Also registers 'id' as a secondary key when it parses as an int (outline-
    supplied id isn't guaranteed numeric; chapter_records defaults id=str(no+1), so this is
    usually the same key twice, harmlessly). Never raises."""
    out = {}
    try:
        for rec in (chapters or []):
            if not isinstance(rec, dict) or not rec.get("content"):
                continue
            _no = rec.get("no")
            if isinstance(_no, int):
                out.setdefault(_no + 1, rec["content"])
            try:
                out.setdefault(int(rec.get("id")), rec["content"])
            except (TypeError, ValueError):
                pass
    except Exception:  # noqa: BLE001
        return {}
    return out


def _canon_quote_spans(evidence) -> list:
    """Pull quoted manuscript text out of a free-text evidence/quote string (straight +
    curly quotes) — same regex idea as laozhang_api.py's _narasi_revise_chunked._spans(),
    duplicated here rather than imported (that helper is a private closure, and this file's
    own precedent — thread-tracker, canon-diff — already re-implements small locator logic
    locally instead of reaching into that function). Drops bare connector words captured
    between two unrelated quotes."""
    import re as _cre
    ev = str(evidence or "")
    sp = _cre.findall(r'["“”‘’\']([^"“”‘’\']{1,120}?)'
                       r'["“”‘’\']', ev)
    return [s.strip() for s in sp if s.strip() and _cre.search(r'\w', s)
            and not _cre.fullmatch(r'(?i)(vs|and|then|or|but|to|the|a|an)', s.strip())]


def _canon_excerpt(chapters, *, chapter_no=None, quote_source=None, cap: int = 6000):
    """CANON-FORK CLASSIFIER WIDENING (2026-07-17, part a/b): resolve a fork/violation to
    real manuscript prose — a whole chapter's text, not a ~200-char paraphrase — so the
    reveal-vs-continuity classifier can see narrative posture (hedge phrases, dramatic-irony
    framing) a short clip cuts away. Fallback ladder, never regresses, never drops an item:
      1. Direct: `chapter_no` given and present in `chapters` (canon-diff forks carry a real
         int) -> O(1) lookup, most reliable.
      2. Fuzzy fallback: pull the quoted span(s) out of `quote_source` (mechanism-1 critic
         violations have no chapter field at all; canon-diff's own 'quote' can be empty on
         an older/malformed response) and substring-search chapters in book order, first
         match wins — same best-effort precedent _narasi_revise_chunked's own docstring
         already accepts for this exact class of lookup.
      3. Neither resolves -> (None, None); caller falls back to its own short-text signal.
         Widening can only help or be neutral per item, never worse than today's behavior.
    Once a chapter is resolved (via either path), if `quote_source` yields a span found
    INSIDE that chapter's text, the excerpt is a `cap`-sized WINDOW CENTERED on that span —
    not a head-only slice — so a quote sitting late in a long chapter isn't silently cut off
    by the cap (caught in verification: a real ~20k-char chapter with its relevant quote past
    the 6000-char mark). Only when no span is locatable inside the resolved chapter does this
    fall back to a plain head[:cap] slice (still strictly better than no excerpt at all).
    Never raises."""
    try:
        _by_no = _canon_chapter_lookup(chapters)
        if not _by_no:
            return None, None
        _content, _n = None, None
        if chapter_no is not None:
            try:
                _cn = int(chapter_no)
            except (TypeError, ValueError):
                _cn = None
            if _cn is not None and _cn in _by_no:
                _content, _n = _by_no[_cn], _cn
        if _content is None and quote_source:
            for _q in _canon_quote_spans(quote_source):
                for _cn in sorted(_by_no.keys()):
                    if _q in _by_no[_cn]:
                        _content, _n = _by_no[_cn], _cn
                        break
                if _content is not None:
                    break
        if _content is None:
            return None, None
        if quote_source:
            for _q in _canon_quote_spans(quote_source):
                _pos = _content.find(_q)
                if _pos >= 0:
                    _half = cap // 2
                    _start = max(0, _pos - _half)
                    _end = min(len(_content), _pos + len(_q) + _half)
                    return _content[_start:_end], _n
        return str(_content)[:cap], _n
    except Exception:  # noqa: BLE001
        return None, None


async def _narasi_classify_canon_items(items, *, tenant_id, user_id, job_uuid,
                                       sink=None, cap: int = 12, widen_prompt: bool = True) -> set:
    """CANON-FORK CLASSIFIER, shared (2026-07-17, part b/d): ONE reveal-vs-continuity triage
    prompt/call/parse/fail-safe used by BOTH mechanism #1 (critic canon_fork violations, the
    original ROUND-13 caller) and mechanism #2 (canon-diff registry forks, new) — so the
    "default to reveal when unsure" bias can never drift between the two call sites; a future
    incident-driven prompt edit only ever has one copy to fix.

    `items`: list of {"signal": <short flagged-fact text, REQUIRED — what conflicts>,
    "excerpt": <manuscript excerpt or None/absent, OPTIONAL — narrative context>}. The short
    signal is kept even when an excerpt is attached: a single chapter's text only shows one
    side of a cross-chapter/cross-canon discrepancy, so the explicit found/expected (or
    quoted contradiction) is still what tells the model WHAT diverges; the excerpt adds
    narrative posture, not a replacement for the signal.

    `widen_prompt` (default True): mechanism #2 (brand new this round, no legacy byte-identity
    constraint) always gets the fuller guardrail prompt. Mechanism #1's call site explicitly
    passes `widen_prompt=<NARASI_CANON_FORK_CLASSIFY_CONTEXT>` so the ALREADY-LIVE
    NARASI_CANON_FORK_CLASSIFY path's prompt text stays BYTE-IDENTICAL to its pre-fix original
    unless that (already-established, default-OFF) flag is explicitly turned on — this file's
    own convention is that an already-live flag's behavior is never silently redefined by a
    deploy (adversarial audit finding, 2026-07-17: the guardrail sentences were unconditional
    regardless of any flag, a real live-behavior-drift risk despite being strictly conservative
    in direction).

    Returns the set of LIST POSITIONS classified "continuity", **indexed into the ORIGINAL
    `items` argument as passed by the caller** (not into any internally filtered/capped copy —
    adversarial audit finding, 2026-07-17: an earlier version returned positions into a
    post-filter list while callers indexed their own pre-filter lists, silently misattributing
    evidence whenever any item had a blank/missing "signal"). Caller maps a returned position
    back to its own violation/fork via items[pos] directly, with no additional adjustment.
    ANY failure -> empty set: fail-safe, every item stays protected. This mirrors ROUND-13's
    original three-layer fail-closed shape exactly — a positive opt-in set, populated only on
    explicit positive confirmation, index-bounds guarded — just generalized across both
    mechanisms' schemas."""
    # (original_index, item) pairs survive the filter+cap so returned positions can be mapped
    # back to the caller's own original list, never to this function's internal compacted one.
    _valid = [(_oi, _it) for _oi, _it in enumerate(items or [])
              if isinstance(_it, dict) and _it.get("signal")][:cap]
    if not _valid:
        return set()
    try:
        from laozhang_api import _narasi_cheap_call as _cfcall, _narasi_parse_json as _cfparse
        _lines = []
        for _pos, (_orig_i, _it) in enumerate(_valid):
            _sig = str(_it.get("signal") or "")[:300]
            _exc = _it.get("excerpt")
            if _exc:
                _lines.append(f"{_pos}. FLAGGED: {_sig}\n   EXCERPT: \"{str(_exc)[:6000]}\"")
            else:
                # No excerpt (widening flag off, or excerpt unlocatable): identical shape to
                # ROUND-13's original per-item line ("{i}. {evidence}") so mechanism-1's
                # live NARASI_CANON_FORK_CLASSIFY path gets byte-identical INPUT when its
                # sibling NARASI_CANON_FORK_CLASSIFY_CONTEXT flag is off.
                _lines.append(f"{_pos}. {_sig}")
        _cf_num = "\n".join(_lines)
        _cf_sys = (
            "You are triaging continuity flags in a mystery manuscript. Each item is a "
            "fact stated two different ways across chapters"
            + (
                "; some items include a manuscript EXCERPT for context. Classify EACH as:\n"
                if widen_prompt else ". Classify EACH as:\n"
            ) +
            "- \"reveal\": the two versions are a DELIBERATE surface-lie vs buried-truth "
            "that the mystery's twist depends on (an identity, a death, a cover-up value "
            "the plot later corrects on purpose). Fixing it would spoil the story.\n"
            "- \"continuity\": a neutral fact (a measurement, a floor count, an object's "
            "dimensions, a unit number, a passing date) stated two incompatible ways with "
            "NO in-story reason -- a plain error safe to unify.\n"
            + (
                "Only answer \"continuity\" when you have clear, unambiguous grounds to rule "
                "out a deliberate reveal -- not merely because the excerpt shown to you lacks "
                "dramatic language. A discrepancy's payoff scene often sits in a DIFFERENT "
                "chapter than the one excerpted here, so a plain-looking excerpt is NOT proof "
                "there is no reveal elsewhere in the book; do not let it manufacture false "
                "confidence. Some items may already have passed an earlier automated filter -- "
                "do not treat that as evidence of safety; apply full judgment to every item "
                "regardless of source. Getting \"reveal\" wrong costs one report-only flag that "
                "stays visible; getting \"continuity\" wrong can permanently gut a twist the "
                "story depends on -- the two mistakes are not equally costly. When unsure, "
                if widen_prompt else "When unsure, "
            ) +
            "answer \"reveal\". Return ONLY JSON: "
            "{\"items\":[{\"i\":<index>,\"class\":\"reveal|continuity\"}]}")
        _raw, _cr = await _cfcall(_cf_sys, _cf_num, tenant_id=tenant_id, user_id=user_id,
                                  job_uuid=job_uuid, json_mode=True, credit_row=False)
        if sink is not None and _cr:
            sink.credits += int(_cr)
        _d = _cfparse(_raw) if isinstance(_raw, str) else (_raw or {})
        _out = set()
        for _it in ((_d or {}).get("items") or []):
            try:
                if str(_it.get("class") or "").lower() == "continuity":
                    _pos = int(_it.get("i"))
                    if 0 <= _pos < len(_valid):
                        _out.add(_valid[_pos][0])  # map the LLM's compact position -> ORIGINAL index
            except Exception:  # noqa: BLE001
                continue
        return _out
    except Exception as _e:  # noqa: BLE001
        log.warning("canon classify failed (non-fatal, all items stay protected): %s", _e)
        return set()


def _r7_actuator_violations(result: dict) -> list[dict]:
    """ROUND-7: teeth for the round-6 report-only gates. Reads the already-computed
    reports off `result` and returns synthetic mechanical violations for the
    existing revise channel — same contract as the ledger/timeline/brand
    injections (types never collide with canon_fork, so the reveal-protection
    filter is untouched). Each actuator behind its own flag, default OFF; the
    matching SCAN flag must also be on or the report is simply absent. Never
    raises."""
    out: list[dict] = []
    try:
        _ctrs = (result.get("counter_report") or {}).get("counters") or {}
        # — abort-seam: the ban swerve shipped into prose ("made barley — no.") —
        # EVIDENCE-LOCATABILITY: `snippet` is a real character-offset slice of the actual
        # chapter text (narasi_counters.abort_seam_scan, newline→space normalized only) —
        # quote-wrap so _spans() can pull it out of an otherwise-plain evidence string.
        if _r7_env_on("NARASI_ABORT_SEAM_ENFORCE"):
            for h in ((_ctrs.get("abort_seam") or {}).get("hits") or [])[:3]:
                out.append({
                    "type": "abort_seam", "severity": "high",
                    "evidence": _wq(h.get("snippet"))[:200],
                    "fix": ("A mid-clause self-correction artifact shipped into the prose "
                            "(a started word, an em-dash, 'no.', then the corrected choice). "
                            "Delete the false start and the correction marker; keep ONLY the "
                            "corrected choice, reflowing the sentence naturally.")})
        # — coda repeat: identical chapter-closing line used twice —
        # EVIDENCE-LOCATABILITY: `coda` is now the ORIGINAL-CASE closing line (narasi_counters.
        # coda_repeat_scan case-preservation fix) — quote-wrap so _spans() can locate it.
        if _r7_env_on("NARASI_CODA_DEDUP_ENFORCE"):
            for pr in ((_ctrs.get("coda_repeat") or {}).get("pairs") or [])[:2]:
                _chs = pr.get("chapters") or ["?", "?"]
                out.append({
                    "type": "coda_repeat", "severity": "high",
                    "evidence": _wq(pr.get("coda"))[:200],
                    "fix": (f"Chapters {_chs[0]} and {_chs[1]} end with this IDENTICAL closing "
                            f"line. Rewrite the chapter-{_chs[1]} ending so each chapter closes "
                            f"distinctly — new image or plain action, not a variation of the "
                            f"same aphorism.")})
        # — domain plausibility: hard law/medicine/engineering errors —
        # EVIDENCE-LOCATABILITY: the domain-plausibility gate's post-check (see _r9_gate_domain)
        # now guarantees every surviving claim's "quote" is a verified literal substring of the
        # manuscript sent — quote-wrap it here too, else it reaches _narasi_revise_chunked with
        # no enclosing quote chars at all and _spans() finds nothing (same bug class as the
        # other 6 types below, just not one of the originally-investigated 7 — same fix applies).
        if _r7_env_on("NARASI_DOMAIN_ENFORCE"):
            _nd = 0
            for c in ((result.get("domain_plausibility_report") or {}).get("claims") or []):
                if str(c.get("severity") or "").lower() != "high":
                    continue
                out.append({
                    "type": "domain_error", "severity": "high",
                    "evidence": _wq(c.get("quote"))[:200],
                    "fix": (f"Domain error ({c.get('domain')}): {str(c.get('why') or '')[:160]} "
                            f"— correct it with the SMALLEST edit that makes the claim "
                            f"professionally plausible; keep the scene and its emotional beat "
                            f"intact.")})
                _nd += 1
                if _nd >= 3:
                    break
        # — numeric ledger: one referent, multiple values (unintentional drift) —
        # EVIDENCE-LOCATABILITY (numeric_arithmetic): `quote` comes from the ledger extractor's
        # "equations" schema, now prompted for a VERBATIM-as-written sentence (see the numeric-
        # ledger system prompt) — quote-wrap it so _spans() can find it. Belt-and-suspenders:
        # append the extractor's own "@chN" chapter locator (same pattern numeric_drift's
        # evidence already uses below) so laozhang_api.py's deterministic @chN bypass can still
        # route this violation even when the quote isn't a perfect verbatim match — and route the
        # fully-synthetic `note` fallback (no manuscript text at all) the same way.
        if _r7_env_on("NARASI_NUMERIC_LEDGER_ENFORCE"):
            for e in ((result.get("numeric_ledger_report") or {}).get("sum_errors") or [])[:2]:
                _nae = _wq(e.get("quote")) or str(e.get("note") or "")[:180]
                if e.get("chapter") is not None:
                    _nae = f"{_nae} @ch{e.get('chapter')}"
                out.append({
                    "type": "numeric_arithmetic", "severity": "high",
                    "evidence": _nae[:200],
                    "fix": (f"The stated total is wrong: {e.get('note')}. Correct the total to "
                            f"{e.get('expected')} EVERYWHERE it appears, and if a person is counted "
                            f"in two components, make the narration subtract them explicitly.")})
            for r in ((result.get("numeric_ledger_report") or {}).get("drifts") or [])[:3]:
                out.append({
                    "type": "numeric_drift", "severity": "high",
                    "evidence": str(r.get("evidence") or r.get("referent") or "")[:200],
                    "fix": (f"The referent «{r.get('referent')}» carries conflicting values "
                            f"{r.get('values')} across chapters. Pick the value the story's "
                            f"dated facts support and unify EVERY mention; do not touch "
                            f"deliberate official-vs-true contrasts.")})
        # — chapter-boundary continuity break: chapter N+1's opening contradicts or
        # redundantly re-stages something chapter N's ending already resolved —
        # orchestrator/static.py's narrate_chapters() (NARASI_CHAPTER_BOUNDARY_CHECK) detects
        # this at MAP time and reports it via "chapter_boundary_report" on `result` (same
        # result-dict plumbing as numeric_ledger_report/domain_plausibility_report — see that
        # function's own comments). Report-only unless this flag is on.
        # EVIDENCE-LOCATABILITY: use "head" — the actual chapter N+1 OPENING text (real
        # manuscript prose already in scope, a literal substring of the pre-polish chapter
        # content) — NEVER the LLM's free-text "reason" (that's a paraphrase/explanation, not
        # a verbatim excerpt; _wq()-wrapping a paraphrase would still fail _occ()'s literal
        # substring match and land the violation in _narasi_revise_chunked's UNMAPPED bucket).
        # Belt-and-suspenders: append the deterministic "@ch{N}" locator for chapter N+1 (the
        # opening side, i.e. chapter_b — the break's fix always targets THAT chapter's
        # opening) so the chunked-revise path can still route this violation even if polish
        # (which runs between this evidence being captured and the revise call) has since
        # touched the literal seam text and the quote no longer matches verbatim.
        if _r7_env_on("NARASI_CHAPTER_BOUNDARY_ENFORCE"):
            for b in ((result.get("chapter_boundary_report") or {}).get("breaks") or [])[:3]:
                _bce = _wq(b.get("head"))
                _bcb = b.get("chapter_b")
                if _bcb is not None:
                    _bce = f"{_bce} @ch{_bcb}"
                out.append({
                    "type": "chapter_boundary_break", "severity": "high",
                    "evidence": _bce[:200],
                    "fix": ("Add a brief bridging sentence or short scene resolving what the "
                            "ending of the previous chapter left unresolved before this "
                            "chapter's opening beat.")})
        # — entity-attribute drift: a named character's gender/title/age, or a mentioned-but-
        # unnamed relative's relation descriptor, contradicts itself across chapters (the
        # "Ha-neul referred to with male pronouns in most chapters, female in one chapter"
        # class) — _r9_gate_entity (NARASI_ENTITY_ATTR_CHECK) detects this and reports it via
        # "entity_attr_report" on `result` (same result-dict plumbing as numeric_ledger_report/
        # chapter_boundary_report — see those comments). Was report-only with no consumer
        # anywhere in the tree before this fix (2026-07-19) — confirmed the drift could never
        # reach a chapter revise no matter how many were found. EVIDENCE-LOCATABILITY: the
        # extraction prompt was tightened alongside this fix to require a literal "quote" +
        # explicit "chapter" per drift (previously a free-text "evidence" description that
        # _wq()-wrapping could never turn into a locatable span) — belt-and-suspenders @chN
        # locator, same convention as every enforce block above.
        if _r7_env_on("NARASI_ENTITY_ATTR_ENFORCE"):
            for d in ((result.get("entity_attr_report") or {}).get("drifts") or [])[:4]:
                _eae = _wq(d.get("quote")) or _wq(d.get("name"))
                _each = d.get("chapter")
                if _each is not None:
                    _eae = f"{_eae} @ch{_each}"
                out.append({
                    "type": "entity_attr_drift", "severity": "high",
                    "evidence": _eae[:200],
                    "fix": (f"«{d.get('name')}» has a contradicting {d.get('kind')} in this "
                            f"chapter — the story elsewhere establishes «{d.get('expected')}». "
                            f"Correct this chapter to match that established fact EVERYWHERE it "
                            f"appears here, unless the story gives an explicit in-world reason "
                            f"for the change (e.g. a disguise, a promotion, an adoption) — if so, "
                            f"leave it and do not report it as a bug.")})
    except Exception:  # noqa: BLE001
        return out
    return out


def _numeric_sum_errors(equations: list) -> list[dict]:
    """ROUND-10: three rolls of one premise produced three DIFFERENT wrong totals for
    the same subtraction (17-should-be-16, 18-should-be-17) — every one failed to
    subtract the mislabeled body counted twice. Deterministic: components must sum to
    the stated total; a named overlap means the correct total is sum-1. Pure function."""
    import re as _sre
    out: list[dict] = []
    for eq in equations or []:
        if not isinstance(eq, dict):
            continue
        try:
            comps = [int(_sre.sub(r"[^\d]", "", str(c))) for c in (eq.get("components") or []) if _sre.sub(r"[^\d]", "", str(c))]
            total = int(_sre.sub(r"[^\d]", "", str(eq.get("stated_total") or "")))
        except Exception:  # noqa: BLE001
            continue
        if not comps or total <= 0:
            continue
        _sum = sum(comps)
        _overlap = str(eq.get("overlap") or "").strip()
        _want = _sum - 1 if _overlap else _sum
        if total != _want:
            out.append({
                "stated": total, "component_sum": _sum, "overlap": _overlap,
                "expected": _want, "quote": str(eq.get("quote") or "")[:120],
                "chapter": eq.get("chapter"),
                "note": (f"stated total {total} but components sum to {_sum}"
                         + (f" with «{_overlap}» counted in two components — correct total {_want}"
                            if _overlap else f" — correct total {_want}"))})
    return out[:4]


_MONTHS_NORM = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
                "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
                "november": 11, "december": 12}


_WEEKDAYS_L = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _is_date_like(v: str) -> bool:
    """ROUND-11: roll-14's three numeric 'drifts' were one demolition date in mixed
    surface forms (Thursday / January 2010 / January twenty-seventh). Dates are the
    TIMELINE gate's domain, not the numeric-ledger's — a referent whose values are
    date descriptions must not be scored as count drift. Date-like = names a month, a
    weekday, or a bare 4-digit year."""
    import re as _re
    t = str(v).lower()
    if any(w in t for w in _WEEKDAYS_L):
        return True
    if any(w in t for w in _MONTHS_NORM):
        return True
    if _re.search(r"\b(?:19|20)\d{2}\b", t):
        return True
    return False


_RELATIVE_DATE_RX = None  # set below, after re is imported locally (module has no top-level `re`)


def _relative_date_rx():
    # Lazily-compiled so this file's established "import re locally, per-function" style is
    # kept — matches _is_date_like/_numeric_drifts right above/below. Captures: a coarse time
    # unit, a before/after direction, and a free-text event description — e.g. "the summer
    # before the plant closed" -> ("summer", "before", "plant closed").
    global _RELATIVE_DATE_RX
    if _RELATIVE_DATE_RX is None:
        import re as _re
        _RELATIVE_DATE_RX = _re.compile(
            r"^(?:the\s+)?(year|summer|spring|winter|fall|autumn|month|week|day)s?\s+"
            r"(before|after)\s+(?:the\s+)?(.+?)(?:\s+(?:happened|occurred))?\.?$",
            _re.IGNORECASE)
    return _RELATIVE_DATE_RX


def _build_date_anchor_map(referents: list) -> dict:
    """FIX 2 (numeric-ledger relative-date-phrase vs absolute-year forks): a lightweight
    event -> absolute-year lookup built from every referent's OWN values in the same
    extraction pass — e.g. a referent named "plant closure" whose only stated value is
    "2014" becomes the resolvable anchor for a phrase like "the summer before the plant
    closed" appearing under a DIFFERENT referent elsewhere in the ledger. Keyed by
    individual content words (>=4 chars, filler words dropped) from the anchor referent's
    own name, so a relative-date phrase's event description only needs to share ONE
    meaningful word with it to resolve — deliberately loose (this is a best-effort
    cross-reference, not an entity-linker); narrow blast radius since it is only ever
    consulted by the relative-date-fork check inside _numeric_drifts below. A referent
    with zero or with 2+ DISTINCT bare years is not usable as an anchor (ambiguous — could
    itself be the drift). Pure function; never raises."""
    import re as _re
    _stop = {"the", "a", "an", "was", "were", "is", "are", "of", "in", "on", "at", "to",
             "and", "or", "that", "this", "its", "year", "years", "date", "dates"}
    anchors: dict = {}
    for r in referents or []:
        if not isinstance(r, dict):
            continue
        _raws = [(v or {}).get("value") if isinstance(v, dict) else v
                 for v in (r.get("values") or [])]
        _years = {int(_m.group(0)) for _raw in _raws
                  for _m in [_re.search(r"\b(?:19|20)\d{2}\b", str(_raw or ""))] if _m}
        if len(_years) != 1:
            continue  # no year, or an ambiguous/conflicting anchor — not usable as a reference point
        _year = next(iter(_years))
        for w in _re.findall(r"[a-z]{4,}", str(r.get("name") or "").lower()):
            if w not in _stop:
                anchors.setdefault(w, _year)
    return anchors


def _resolve_relative_date(phrase: str, anchor_map: dict) -> Optional[int]:
    """FIX 2: resolve a relative-date phrase ("the summer before the plant closed") to an
    approximate absolute year via `anchor_map` (see _build_date_anchor_map) — year-level
    precision only, which is all the fork-comparison below needs. Returns None when the
    phrase doesn't match the supported "<unit> before/after <event>" shape, or no anchor
    word overlaps the event description (the common, expected case for most values — this
    must stay a no-op for anything that isn't this specific relative-date-phrase shape)."""
    import re as _re
    m = _relative_date_rx().match(str(phrase or "").strip())
    if not m:
        return None
    direction = m.group(2).lower()
    event = m.group(3).lower()
    year = None
    for w in _re.findall(r"[a-z]{4,}", event):
        if w in anchor_map:
            year = anchor_map[w]
            break
    if year is None:
        return None
    return year - 1 if direction == "before" else year + 1


def _numeric_drifts(referents: list) -> list[dict]:
    """ROUND-7 deterministic post-check for the numeric-ledger cheap call: a
    referent with ≥2 distinct normalized values that the extractor did NOT mark
    as an intentional official-vs-true contrast is a drift. Pure function —
    unit-tested against the lens-4 catch list.

    FIX 2 (relative-date-phrase vs absolute-year forks): before the blanket "mostly
    date-like -> skip, defer to the TIMELINE gate" rule below (which exists for pure
    SURFACE-FORM differences of the SAME date, e.g. "Jan 27" vs "January twenty-seventh"
    — not for a genuine year mismatch), check whether THIS referent's own values combine a
    bare absolute year with a phrase relative to another story event that resolves (via
    _build_date_anchor_map/_resolve_relative_date) to a DIFFERENT year. That is a real
    continuity conflict the blanket date-skip would otherwise silently swallow. Additive:
    a referent with no relative-date phrase, or one that fails to resolve to any anchor,
    falls through to the exact pre-existing logic unchanged."""
    import re as _re
    _words = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
              "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
              "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13",
              "fourteen": "14", "fifteen": "15", "sixteen": "16", "seventeen": "17",
              "eighteen": "18", "nineteen": "19", "twenty": "20", "thirty": "30",
              "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
              "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000"}
    _anchor_map = _build_date_anchor_map(referents)
    drifts: list[dict] = []
    for r in referents or []:
        if not isinstance(r, dict) or r.get("intentional_contrast"):
            continue
        _raws = [(v or {}).get("value") if isinstance(v, dict) else v
                 for v in (r.get("values") or [])]
        # FIX 2: relative-date-phrase vs absolute-year fork — see function docstring.
        _bare_years = {int(_m.group(0)) for _raw in _raws
                       for _m in [_re.search(r"\b(?:19|20)\d{2}\b", str(_raw or ""))] if _m}
        _resolved = [(str(_raw), _ry) for _raw in _raws
                     for _ry in [_resolve_relative_date(str(_raw or ""), _anchor_map)]
                     if _ry is not None]
        if _bare_years and _resolved:
            for _phrase, _ry in _resolved:
                if _ry not in _bare_years:
                    _by = sorted(_bare_years)[0]
                    drifts.append({
                        "referent": str(r.get("name") or "?"),
                        "values": [str(_by), str(_ry)],
                        "evidence": (f"{_by} vs \"{_phrase}\" (resolves to ~{_ry} via an "
                                     f"established anchor date elsewhere in the story) — "
                                     f"conflicting dates for the same referent")})
                    break  # one drift per referent is enough signal
            continue  # this referent's date-ness was already resolved above either way
        # skip date-referents entirely: dates belong to the timeline gate, and partial
        # date forms legitimately differ without conflicting (roll-14 3 FP).
        _rname = str(r.get("name") or "").lower()
        _date_hits = sum(1 for x in _raws if _is_date_like(str(x)))
        if _raws and ("date" in _rname or _date_hits * 2 >= len(_raws)):
            continue
        vals: list[str] = []
        for _raw in _raws:
            _s = str(_raw).lower().strip()
            _s = " ".join(_words.get(w, w) for w in _re.split(r"[\s-]+", _s))
            _s = _re.sub(r"[,\s]", "", _s)
            if _s and _s not in vals:
                vals.append(_s)
        if len(vals) >= 2:
            drifts.append({"referent": str(r.get("name") or "?"),
                           "values": vals[:4],
                           "evidence": "; ".join(
                               f"{(v or {}).get('value')}@ch{(v or {}).get('chapter')}"
                               for v in (r.get("values") or [])[:4] if isinstance(v, dict))})
    return drifts[:6]


def _name_uniqueness_scan(text: str) -> list[dict]:
    """NEW gate (2026-07-15): confirmed miss — a manuscript used the identical full
    name 'Yoon Hye-jin' for two unrelated characters (a council committee chair, an
    unrelated widow) introduced in different chapters. The nearest relative,
    narasi_gate.phonetic_collision_scan, solves the OPPOSITE problem (different-
    but-similar names assumed to be one person) and explicitly treats identical
    strings as the SAME person — so this is a genuinely new class. Deterministic:
    the SAME full name introduced via 'named X Y' with a DIFFERING description
    clause in >=2 places is flagged. A character introduced once and simply
    referenced afterward — or re-introduced with the same (recapped) description —
    does NOT fire. Pure function; narrow by design (mirrors phonetic_collision_
    scan's high-precision-first philosophy) — start narrow, broaden if the corpus
    demands it."""
    import re as _nre
    if not text:
        return []
    _intro_rx = _nre.compile(
        r"\bnamed\s+([A-Z][a-z]+(?:-[a-z]+)?(?:\s+[A-Z][a-z]+(?:-[a-z]+)?){1,2})"
        r"(?:,\s*([^.\n]{4,160}))?"
    )
    by_name: dict[str, list[str]] = {}
    for m in _intro_rx.finditer(text):
        name = m.group(1).strip()
        ctx = (m.group(2) or "").strip()
        by_name.setdefault(name, []).append(ctx)
    collisions: list[dict] = []
    for name, ctxs in by_name.items():
        if len(ctxs) < 2:
            continue
        # de-dupe near-identical description clauses — a verbatim recap of the SAME
        # character re-introduced later is not a collision — by comparing only the
        # FIRST comma/semicolon-delimited clause of each captured context (2026-07-16
        # fix: a blind 40-char prefix of the WHOLE context truncated across the actual
        # description-clause boundary into trailing verb-phrase text, e.g. "...the head
        # of the ICU night shift, now exhausted after a double shift." vs "...the head
        # of the ICU night shift, as she rushes down the corridor." — natural prose
        # almost never ends the sentence right at the description clause, so those two
        # diverge before the 40-char cutoff even though the description clause itself
        # is identical, producing a false collision). Splitting on the first comma/
        # semicolon isolates just the description clause for the dedup key.
        uniq: list[str] = []
        seen_heads: set = set()
        for c in ctxs:
            head = _nre.split(r"[,;]", c, maxsplit=1)[0].strip().lower()
            if head in seen_heads:
                continue
            seen_heads.add(head)
            uniq.append(c)
        if len(uniq) < 2:
            continue
        collisions.append({
            "name": name, "contexts": uniq[:4],
            "note": (f"'{name}' is introduced with 'named {name}' in {len(uniq)} "
                     f"distinct contexts — likely two different characters sharing "
                     f"one name.")})
        if len(collisions) >= 5:
            break
    return collisions


def _name_order_scan(text: str) -> list[dict]:
    """NEW gate (2026-07-16): a manuscript review found the same character rendered
    as 'Yuna Song' (Western given-family order) in one chapter and 'Song Yuna'
    (Korean surname-first order) in another — same two name tokens, reversed order,
    likely the same person but currently undetected by any existing gate.
    Deterministic, report-only: scans ALL two-token capitalized name occurrences
    (mirrors the 'named X Y' token shape used by _name_uniqueness_scan above, but
    does not require the word 'named' — it matches any two-token capitalized name
    anywhere in the prose) and groups them by the SORTED pair of tokens, so
    'Yuna Song' and 'Song Yuna' hash to the same group regardless of which order
    appears first. A group is only flagged when BOTH orderings are actually
    present — a name repeated any number of times in a single consistent order is
    not a collision. Pure function; never raises; capped at 5.
    Stop-word guarded (2026-07-16, audit fix) against the codebase's established
    closed-class filler-word list (narasi_counters._ALIAS_STOP_SN — But/And/The/
    When/She/He/etc), which kills the sentence-initial-FILLER false positive
    ('But Yuna...'). KNOWN RESIDUAL LIMITATION, not fully closed by a finite stop
    list: an ordinary English content word that is ALSO a character's surname and
    happens to open a sentence in a reduced-relative-clause ('Song Yuna had once
    loved...', where 'Song' is capitalized only by sentence position) can still
    false-positive. Closing that fully needs sentence-boundary + corroboration
    logic, not just a bigger stop list — deferred; report-only + default-off
    keeps the blast radius to a dashboard field, not manuscript mutation."""
    import re as _nre
    import narasi_counters as _nc  # reuse the established stop-word set, don't fork it
    if not text:
        return []
    _name_rx = _nre.compile(
        r"\b([A-Z][a-z]+(?:-[a-z]+)?)\s+([A-Z][a-z]+(?:-[a-z]+)?)\b")
    # group key -> literal form -> occurrence count
    groups: dict[tuple, dict[str, int]] = {}
    for m in _name_rx.finditer(text):
        tok1, tok2 = m.group(1), m.group(2)
        # Stop-word guard (audit-caught, 2026-07-16): without this, a sentence-initial
        # filler word ("But Yuna...") or a reduced-relative-clause common noun ("Song
        # Yuna had once loved...", where "Song" is capitalized only because it opens the
        # clause) gets captured as a two-token "name" and can collide with a real
        # character's actual name. Same convention already used by narasi_counters'
        # _ALIAS_SURNAME_GIVEN_RX/_ALIAS_STOP_SN for this exact problem class — checked
        # on BOTH tokens here since neither capture group has a shape constraint that
        # would already exclude a stop word.
        if tok1 in _nc._ALIAS_STOP_SN or tok2 in _nc._ALIAS_STOP_SN:
            continue
        literal = f"{tok1} {tok2}"
        key = tuple(sorted((tok1, tok2)))
        bucket = groups.setdefault(key, {})
        bucket[literal] = bucket.get(literal, 0) + 1
    inconsistencies: list[dict] = []
    for key, forms in groups.items():
        if len(forms) < 2:
            continue
        # BOTH orderings present means >=2 distinct literal forms whose token pair
        # sorts to the same key — since key is a 2-tuple, any 2+ distinct literals
        # here are, by construction, the two possible orderings of the same tokens.
        inconsistencies.append({
            "tokens": list(key),
            "forms": [{"text": lit, "count": cnt} for lit, cnt in sorted(
                forms.items(), key=lambda kv: -kv[1])],
            "note": (f"Name tokens {key[0]!r}/{key[1]!r} appear in both orders "
                     f"({', '.join(sorted(forms.keys()))}) — likely the same "
                     f"character rendered inconsistently.")})
        if len(inconsistencies) >= 5:
            break
    return inconsistencies


def _name_typo_scan(text: str) -> list[dict]:
    """NEW gate (2026-07-16): a manuscript review found a chore-list line "Call
    Seo-ra's clinic" where the established character name elsewhere in the same
    manuscript is "So-ra" (part of "Seol So-ra") — a one-letter typo, and also
    confusingly one letter off from an unrelated protagonist name ("Seo-an"), a
    real reader-confusion risk. Deterministic, report-only: (1) builds a registry
    of canonical hyphenated Korean-style given names ([A-Z][a-z]+-[a-z]+) that
    appear 3+ times in the text (repetition threshold — establishes it as a real,
    recurring character name rather than a one-off), (2) scans for any OTHER
    hyphenated-name-shaped token NOT in that canonical set but within a local
    edit-distance-1 of a canonical name, (3) flags it as a likely typo. Pure
    function; never raises; capped at 5."""
    import re as _nre

    def _edit_distance_1(a: str, b: str) -> bool:
        """True iff a and b differ by exactly one character substitution,
        insertion, or deletion. O(n), length-difference-gated — sufficient for
        short name tokens; not a full DP Levenshtein implementation."""
        if a == b:
            return False
        la, lb = len(a), len(b)
        if abs(la - lb) > 1:
            return False
        if la == lb:
            # substitution: exactly one differing position
            diffs = sum(1 for x, y in zip(a, b) if x != y)
            return diffs == 1
        # insertion/deletion: la != lb by exactly 1 — walk both, allow one skip
        shorter, longer = (a, b) if la < lb else (b, a)
        i = j = 0
        skipped = False
        while i < len(shorter) and j < len(longer):
            if shorter[i] == longer[j]:
                i += 1
                j += 1
                continue
            if skipped:
                return False
            skipped = True
            j += 1
        return True

    if not text:
        return []
    _hy_rx = _nre.compile(r"\b[A-Z][a-z]+-[a-z]+\b")
    counts: dict[str, int] = {}
    for m in _hy_rx.finditer(text):
        tok = m.group(0)
        counts[tok] = counts.get(tok, 0) + 1
    canonical = {tok: cnt for tok, cnt in counts.items() if cnt >= 3}
    if not canonical:
        return []
    typos: list[dict] = []
    flagged: set = set()
    for tok, cnt in counts.items():
        if tok in canonical or tok in flagged:
            continue
        # Repetition guard (audit-caught, 2026-07-16): a token repeated 2+ times reads as
        # an intentional, independently-established name (e.g. "Min-i" used 2+ times
        # alongside "Min-a" 3+ times — plausible distinct sibling/cousin names, not a
        # slip), not a one-off typo. A genuine typo like "Seo-ra" appearing once in a
        # chore-list line is exactly the cnt==1 signature this preserves.
        if cnt > 1:
            continue
        for canon, canon_cnt in canonical.items():
            if tok == canon:
                continue
            if _edit_distance_1(tok, canon):
                typos.append({
                    "typo": tok, "typo_count": cnt,
                    "canonical": canon, "canonical_count": canon_cnt,
                    "note": (f"{tok!r} appears {cnt} time(s) and is one character "
                             f"off from the established name {canon!r} (seen "
                             f"{canon_cnt} times) — likely a typo.")})
                flagged.add(tok)
                break
        if len(typos) >= 5:
            break
    return typos


def _chapters_key(job_id: str) -> str:
    return f"narration:{job_id}:chapters"


def _status_key(job_id: str) -> str:
    return f"narration:{job_id}:status"


def _cancel_token(job_id: str) -> str:
    # rc.set_cancel/is_cancelled prefix this with 'cancel:' internally.
    return f"narration_{job_id}"


# ---------------------------------------------------------------------------
# Redis helpers — all best-effort (never raise; a Redis outage must not wedge a
# job, exactly like redis_client's own contract).
# ---------------------------------------------------------------------------
async def _redis():
    """The shared async Redis client, or None if unavailable."""
    try:
        return rc.client()
    except Exception:  # noqa: BLE001
        return None


async def _init_checkboxes(job_id: str, total: int) -> None:
    """Seed the per-chapter checkbox hash (all 'pending') + the status, expire 1h.
    The UI renders one checkbox per `chapter:N` field and flips it as the field
    moves pending → running → done/failed."""
    r = await _redis()
    if r is None:
        return
    try:
        mapping = {f"chapter:{i}": _STATUS_PENDING for i in range(total)}
        mapping["total"] = str(total)
        mapping["done"] = "0"
        key = _chapters_key(job_id)
        await r.delete(key)
        if mapping:
            await r.hset(key, mapping=mapping)
            await r.expire(key, _CHAPTERS_TTL)
        await r.set(_status_key(job_id), _STATUS_RUNNING, ex=_CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("init_checkboxes(%s) failed: %s", job_id, e)


async def _set_chapter_state(job_id: str, no: int, state: str) -> None:
    """Flip one chapter's checkbox field; bump the 'done' counter on terminal states."""
    r = await _redis()
    if r is None:
        return
    try:
        key = _chapters_key(job_id)
        await r.hset(key, f"chapter:{no}", state)
        if state in (_STATUS_DONE, _STATUS_FAILED):
            await r.hincrby(key, "done", 1)
        await r.expire(key, _CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("set_chapter_state(%s,%d,%s) failed: %s", job_id, no, state, e)


async def _set_status(job_id: str, status: str) -> None:
    r = await _redis()
    if r is None:
        return
    try:
        await r.set(_status_key(job_id), status, ex=_CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.warning("set_status(%s,%s) failed: %s", job_id, status, e)


async def _read_checkboxes(job_id: str) -> tuple[Optional[str], int, int, list[dict]]:
    """Read (status, done, total, chapters[]) from Redis. Returns (None,0,0,[]) if
    the hash is gone (expired/never-existed) so GET can fall back to the DB."""
    r = await _redis()
    if r is None:
        return None, 0, 0, []
    try:
        status = await r.get(_status_key(job_id))
        h = await r.hgetall(_chapters_key(job_id))
        if not h:
            return status, 0, 0, []
        total = int(h.get("total", 0) or 0)
        done = int(h.get("done", 0) or 0)
        chapters = []
        for i in range(total):
            chapters.append({"no": i, "state": h.get(f"chapter:{i}", _STATUS_PENDING)})
        return status, done, total, chapters
    except Exception as e:  # noqa: BLE001
        log.warning("read_checkboxes(%s) failed: %s", job_id, e)
        return None, 0, 0, []


# ---------------------------------------------------------------------------
# Telemetry → usage_logs. The orchestrator emits one CallTelemetry per LLM call
# (worker chapters AND the manager polish). We (a) accumulate the run total so the
# credit hold settles at ACTUAL cost, and (b) write a usage_logs row per call so
# nothing is invisible — mirroring how _log_narasi_usage records each chapter.
# ---------------------------------------------------------------------------
class _UsageSink:
    """A telemetry sink (callable taking a CallTelemetry) that totals tokens/cost
    for hold-settlement and fans each call out to a usage_logs row. Must never
    raise back into the generation path (the orchestrator guards this too)."""

    __slots__ = ("tenant_id", "user_id", "job_uuid", "tokens_in", "tokens_out",
                 "cost_usd", "calls", "credits", "_ckpt", "_loop")

    def __init__(self, tenant_id: str, user_id: Optional[str], job_uuid: Optional[str]):
        self.tenant_id = tenant_id
        self.user_id = user_id
        self.job_uuid = job_uuid
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.calls = 0
        # A1 crash-safe billing (mirrors classic _meter_actual): running CREDIT total,
        # durably checkpointed to jobs.input_payload._meter.actual after each call so the
        # orphan sweep can COMMIT delivered work after a crash instead of refunding it.
        # The UPDATE also bumps jobs.updated_at (trg_jobs_updated_at) → an actively
        # generating job can never look stale to the sweep. _ckpt: None=unresolved,
        # False=crashsafe off (skip), True=on.
        self.credits = 0
        self._ckpt: Optional[bool] = None
        try:
            self._loop = asyncio.get_event_loop()
        except Exception:  # noqa: BLE001
            self._loop = None

    def __call__(self, t: CallTelemetry) -> None:
        # Accumulate synchronously (the sink is called from worker threads/coros).
        try:
            self.tokens_in += int(t.tokens_in or 0)
            self.tokens_out += int(t.tokens_out or 0)
            self.cost_usd += float(t.cost_usd or 0.0)
            self.calls += 1
        except Exception:  # noqa: BLE001
            pass
        # Fan one durable usage row out, best-effort. Schedule it on the loop so we
        # don't block generation on a DB round-trip; swallow everything.
        try:
            loop = self._loop or asyncio.get_event_loop()
            loop.create_task(self._log_one(t))
        except Exception:  # noqa: BLE001
            pass

    async def _log_one(self, t: CallTelemetry) -> None:
        # F8 (FIX_F8_NARASI_REFUND_ON_FAILOVER, default OFF): when the failover chain
        # served a rung whose model id isn't in the orchestrator's pricing table,
        # estimate_cost returns 0.0 and the per-call usage_logs row silently logs
        # cost_usd=0 for delivered tokens — margin analytics under-report COGS.
        # Floor the LOGGED cost at a conservative blended rate ($1/M in, $5/M out) so
        # the row reflects real work. Settlement is unaffected (already floored by A4).
        _cost_usd = float(t.cost_usd or 0.0)
        try:
            if str(os.environ.get("FIX_F8_NARASI_REFUND_ON_FAILOVER", "0")).strip().lower() in ("1", "true", "yes", "on"):
                if getattr(t, "ok", False) and _cost_usd <= 0.0 and int(t.tokens_out or 0) > 0:
                    _cost_usd = (int(t.tokens_in or 0) * 1.0 + int(t.tokens_out or 0) * 5.0) / 1_000_000.0
                    log.warning("usage sink: pricing-table miss for model=%s provider=%s — "
                                "flooring cost_usd=%.6f (tin=%d tout=%d)", t.model,
                                (getattr(t, "provider", "") or "laozhang"),
                                _cost_usd, int(t.tokens_in or 0), int(t.tokens_out or 0))
        except Exception:  # noqa: BLE001 - logging floor must never break generation
            _cost_usd = float(t.cost_usd or 0.0)
        try:
            await db.log_usage(
                self.tenant_id, self.user_id, t.model, "narasi",
                int(t.tokens_in or 0), int(t.tokens_out or 0), _cost_usd,
                # Actual serving aggregator when the narasi failover client handled the
                # call (kie/laozhang/atlascloud, stamped through CallTelemetry.provider);
                # "laozhang" only as the plain-client default. NOT NULL column.
                job_id=self.job_uuid, provider=(getattr(t, "provider", "") or "laozhang"),
                latency_ms=int(t.latency_ms or 0),
                finish_reason=t.finish_reason or ("error" if not t.ok else "stop"),
                http_status=200 if t.ok else 502, credits=0)
        except Exception as e:  # noqa: BLE001
            log.debug("usage sink log_one failed (non-fatal): %s", e)
        # Catalog-parity credit total (classic parity, per-call per-model): _settle
        # charges THIS number — the orchestrator's provider-usd table undercharged narasi
        # ~4x vs the catalog. Accumulated for EVERY ok call; the A1 durable checkpoint
        # write below stays gated on DALANG_CRASHSAFE_ENABLED (checkpoint_narasi_meter
        # also no-ops when the row carries no _meter).
        try:
            if t.ok:
                import credit_catalog as _cat
                self.credits += int(_cat.credit_cost(
                    "narasi", t.model,
                    {"tokens_in": int(t.tokens_in or 0), "tokens_out": int(t.tokens_out or 0)}) or 0)
            if self._ckpt is None:
                try:
                    from laozhang_api import _dalang_crashsafe_enabled as _cse
                    self._ckpt = bool(_cse())
                except Exception:  # noqa: BLE001
                    self._ckpt = False
            if self._ckpt and self.job_uuid and t.ok:
                await db.checkpoint_narasi_meter(self.tenant_id, self.job_uuid, self.credits)
        except Exception as e:  # noqa: BLE001
            log.debug("usage sink meter checkpoint failed (non-fatal): %s", e)
        # P0A: bounded provider/phase/token/latency/attempts only. `model`, raw `role`,
        # raw `task_id`, `finish_reason`, `error` and response data are never forwarded.
        # This piggybacks on the task __call__ ALREADY owns for this telemetry row rather
        # than scheduling a second one: __call__ runs on the generation path (and from
        # worker threads, where create_task is not even safe), while _log_one is already
        # off it. Placed last, after every existing statement, so it cannot reorder or
        # delay the usage row, the credit accumulation or the crash-safe checkpoint — and
        # reached whether or not db.log_usage() above succeeded, since that failure is
        # swallowed by its own handler.
        #
        # `attempts` is forwarded as its OWN dimension: it is the run_worker-level retry
        # count folded into this one telemetry, not a second logical call and not the
        # physical per-rung HTTP attempts, which are not visible from here at all.
        #
        # Nothing is logged on failure. A log line carrying `%s` of the exception is a
        # channel for whatever that exception happens to quote — a URL, a credential, a
        # provider error body — so the failure is swallowed silently and shows up where it
        # belongs, as a writer:fail counter and a coverage gap.
        try:
            import narasi_observability as _obs
            if _obs.enabled():
                await _obs.record_provider_call(
                    provider=getattr(t, "provider", ""),
                    phase=_obs.normalize_phase(getattr(t, "task_id", ""),
                                               getattr(t, "role", "")),
                    ok=bool(getattr(t, "ok", False)),
                    tokens_in=int(getattr(t, "tokens_in", 0) or 0),
                    tokens_out=int(getattr(t, "tokens_out", 0) or 0),
                    latency_ms=int(getattr(t, "latency_ms", 0) or 0),
                    attempts=int(getattr(t, "attempts", 0) or 0),
                    # A fresh HARD TIMEOUT for this write alone — never a deadline shared
                    # across the sink's lifetime. A lifetime budget starts ticking at the
                    # first provider call, so on a book that runs for an hour every later
                    # call would be discarded even against a perfectly healthy Redis, and
                    # the metric would stop measuring exactly the long jobs it is for.
                    # Bounding each write individually is free here: nothing awaits this
                    # task, so the timeout costs the product nothing.
                    budget=_obs.Budget(_obs.PROVIDER_WRITE_TIMEOUT_MS))
        except Exception:  # noqa: BLE001 - telemetry never escapes into generation
            pass


# ---------------------------------------------------------------------------
# Chapter-checkbox telemetry sink — wraps the REAL _UsageSink so it remains a
# drop-in Callable[[CallTelemetry], None] (core.py's _emit only ever does
# `sink(t)`, so a callable class instance satisfies every existing call site
# identically to the bare closure it replaces), while additionally exposing a
# `.credits` property that proxies straight through to the wrapped sink's real
# `.credits` slot. This lets downstream orchestrator code that duck-types a
# `.credits` running-total onto whatever telemetry_sink it receives (see
# orchestrator/dynamic.py's _reveal_dedup_amend `_sink_can_absorb` check) fold
# its cheap-call costs into the REAL settlement total (_settle reads
# sink.credits as credits_actual) instead of silently degrading to a no-op.
# ---------------------------------------------------------------------------
class _ChapterCheckboxSink:
    """Fans telemetry to the real usage sink AND flips per-chapter Redis
    checkboxes as chapter workers report in. `credits` is a passthrough
    property onto the wrapped `_UsageSink`, not separate state — mutating it
    (e.g. `telemetry_sink.credits += cr`) mutates the real sink directly, the
    same synchronous get-then-set with no intervening `await` that every other
    `sink.credits += x` site in this module already relies on for safety under
    asyncio's single-threaded cooperative scheduling."""

    __slots__ = ("_sink", "_job_id", "_total", "_chapters_done", "_polish_progress_fired")

    def __init__(self, sink: "_UsageSink", *, job_id: str, total: int):
        self._sink = sink
        self._job_id = job_id
        self._total = total
        self._chapters_done: set = set()
        self._polish_progress_fired = False

    @property
    def credits(self) -> int:
        return self._sink.credits

    @credits.setter
    def credits(self, value) -> None:
        self._sink.credits = value

    def __call__(self, t: CallTelemetry) -> None:
        self._sink(t)  # keep accounting + usage logging
        tid = (t.task_id or "")
        if tid.startswith("ch") and tid[2:].isdigit():
            no = int(tid[2:]) - 1
            state = _STATUS_DONE if t.ok else _STATUS_FAILED
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_set_chapter_state(self._job_id, no, state))
                self._chapters_done.add(no)
                # Rino: "Composing narration gak bisa dibuat lebih cepat" — chapters
                # write in parallel already; the lingering banner after all boxes green
                # is polish. Flip the progress message the moment the last chapter's
                # telemetry lands so the UI doesn't stall on "Composing".
                if (not self._polish_progress_fired
                        and len(self._chapters_done) >= max(1, int(self._total))):
                    self._polish_progress_fired = True
                    loop.create_task(_safe_progress(self._job_id, "Polishing final draft…"))
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Cooperative cancel — a telemetry sink can't cancel, but the orchestrator runs
# chapters via asyncio.as_completed inside narrate_chapters. We can't reach into
# that loop, so cancellation is enforced at the JOB boundary: we race the whole
# generate_narration coroutine against a cancel-watcher; if the flag flips we
# cancel the task, mark the job cancelled, and refund the hold. Chapters already
# completed are still persisted by the runtime's own progress writes.
# ---------------------------------------------------------------------------
async def _cancel_watcher(job_id: str, poll: float = 1.5) -> None:
    """Resolve as soon as the cancel flag is observed in Redis."""
    while True:
        try:
            if await rc.is_cancelled(_cancel_token(job_id)):
                return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(poll)


# ---------------------------------------------------------------------------
# The background runtime — drives generate_narration with the production envelope.
# ---------------------------------------------------------------------------

# ── P0A aggregate observability (NARASI_P0A_OBSERVABILITY_ENABLED, default OFF) ──
# Every helper is flag-gated, lazily imported and swallows everything: a metric must
# never fail, delay materially, cancel, retry, refund, settle or alter a narration job.
# Only bounded enum labels and bucketed integers are passed; no body text, no identifier.
async def _p0a(event: str, **kw) -> None:
    try:
        import narasi_observability as _obs
        if not _obs.enabled():
            return
        await getattr(_obs, event)(**kw)
    except Exception:  # noqa: BLE001 - telemetry never escapes into generation
        return


def _p0a_on() -> bool:
    """The flag, read without importing anything into the product path when it is off."""
    try:
        import narasi_observability as _obs
        return bool(_obs.enabled())
    except Exception:  # noqa: BLE001
        return False


def _p0a_route(background, route: str, body: dict, total: int, job_id: str) -> None:
    """Queue the DISPATCH event on the framework's own background-task list.

    Starlette runs these after the response has been sent, so the caller's 202 is never
    behind a Redis round-trip — and the task is owned and awaited by the framework, not
    detached with create_task, so nothing is left orphaned if it fails. Nothing at all is
    queued while P0A is off, which is what keeps the disabled path free of any new task.

    Recording the dispatch HERE (rather than from the job) is what keeps `route` a
    dispatch census: a job that is enqueued successfully and never executed still counts
    as bullmq_worker, and the gap against `executions:total` stays visible."""
    if not _p0a_on():
        return
    background.add_task(_p0a, "record_job_start", route=route, chapter_count=total,
                        total_words=_p0a_size_words(body), job_id=job_id,
                        budget=_p0a_new_budget(early=True))


def _p0a_new_budget(early: bool = False):
    """ONE telemetry allowance for this job, covering every P0A await it will make.

    A per-event timeout multiplies — eight events at a second each is eight seconds of
    latency handed to the user by a metrics package. A single budget means a hung Redis
    costs the job this much in total, and the events it can no longer afford are simply
    not written. Returns None when P0A is off, so the disabled path allocates nothing."""
    try:
        import narasi_observability as _obs
        if not _obs.enabled():
            return None
        return _obs.Budget(_obs.EARLY_WRITE_BUDGET_MS if early
                           else _obs.TELEMETRY_BUDGET_MS)
    except Exception:  # noqa: BLE001
        return None


def _p0a_size_words(body: dict) -> object:
    """Server-resolved requested word TARGET only — never manuscript bytes or prose.

    The request carries the target PER CHAPTER, so this mirrors the shape production
    already uses at the counters site (`word_target` → `words`, dict entries only,
    non-numeric tolerated, negatives clamped) with the same per-chapter default of 800
    that admission validation and the credit hold both resolve. Reading top-level keys
    instead was simply wrong — nearly every real job would have bucketed `unknown` — and
    defaulting a chapter to 0 would be wrong in the other direction, since the server
    itself validates and charges that chapter at 800.

    Returns None, never 0, when no chapter target can be resolved: the metrics module then
    records `unknown` instead of the smallest real band. The value is bucketed immediately
    and the exact count is never persisted."""
    try:
        chapters = (body or {}).get("chapters")
        if not isinstance(chapters, list):
            return None
        total = 0
        seen = 0
        for c in chapters:
            if not isinstance(c, dict):
                continue
            seen += 1
            try:
                w = int(c.get("word_target") or c.get("words") or 800)
            except (TypeError, ValueError):
                w = 800
            total += max(0, w)
        return total if seen else None
    except Exception:  # noqa: BLE001
        return None


async def _run_narration_job(
    *, body: dict, job_id: str, job_uuid: Optional[str],
    tenant_id: str, user_id: Optional[str], total: int,
    meter_op: Optional[str], model: str, executor: str = "unknown",
) -> None:
    """Background task: hold → generate → settle/refund, with per-chapter Redis
    checkboxes, a status machine, durable persistence, and cancel handling.
    NEVER raises (it's a fire-and-forget create_task; an escaping exception would
    be an unhandled-task warning and a stranded hold)."""
    sink = _UsageSink(tenant_id, user_id, job_uuid)
    started = time.monotonic()
    charge_settled = False
    # ── P0A telemetry, one allowance for the whole job ──────────────────────────────
    # Only ONE P0A write happens before user work: the EXECUTION event, under a hard cap
    # of its own. It stays here on purpose — a job that dies mid-flight would otherwise be
    # invisible, and terminals/executions would then reconcile to 1.0 by construction.
    # The dispatch event is a separate counter recorded by the API after its response.
    # Everything else (gate, phase wall times, terminal) is buffered and flushed at the
    # very end, after persistence and after settle/refund. The executor label never enters
    # the request body, job row, result payload, logs or billing.
    _p0a_budget = _p0a_new_budget()
    await _p0a("record_execution", executor=executor, job_id=job_id,
               budget=_p0a_new_budget(early=True))
    _p0a_gate_ms: Optional[int] = None
    _p0a_timings: Optional[dict] = {} if _p0a_budget is not None else None

    async def _p0a_flush(terminal: str) -> None:
        """Every remaining P0A write, issued only once this job has persisted its result
        and settled or refunded.

        Telemetry must never sit between a user's manuscript and their money, nor between
        a failure and its refund: a metrics call that hangs there delays a settlement.
        All three writes share the job's ONE budget, so the whole tail is bounded no
        matter how many of them there are."""
        if _p0a_gate_ms is not None:
            await _p0a("record_gate_total", elapsed_ms=_p0a_gate_ms, job_id=job_id,
                       budget=_p0a_budget)
        for _phase, _elapsed in sorted((_p0a_timings or {}).items()):
            await _p0a("record_phase_timing", phase=_phase,
                       elapsed_ms=int(max(0.0, float(_elapsed)) * 1000),
                       job_id=job_id, budget=_p0a_budget)
        await _p0a("record_terminal", terminal=terminal,
                   duration_ms=int(max(0.0, time.monotonic() - started) * 1000),
                   job_id=job_id, budget=_p0a_budget)

    # CC v3 R-FG4: refresh the known-bad-claims registry (global reference data) into the
    # gate's in-process cache — best-effort; the gate carries a seed fallback regardless.
    try:
        import narasi_gate as _ngate
        _ngate.set_db_claims(await db.get_known_bad_claims(body.get("project_id")))
        # alt_history (§3): contextvar — inherited by every task this job spawns, so
        # concurrent worker jobs can't race each other's canon enforcement.
        _ngate.set_alt_history(bool(body.get("alt_history")))
    except Exception as e:  # noqa: BLE001
        log.warning("known_bad_claims refresh skipped (non-fatal): %s", e)
    try:
        import narasi_factscan as _nfs
        _nfs.set_known_good(await db.get_known_good_claims(body.get("project_id")))
    except Exception as e:  # noqa: BLE001
        log.warning("known_good_claims refresh skipped (non-fatal): %s", e)

    # Per-chapter checkbox driver. generate_narration doesn't stream chapter
    # completions back to us, so we approximate live checkbox lighting by polling
    # the durable narasi_chapters writes the runtime makes — but the orchestrator
    # writes chapters all at once at the end. To still light checkboxes AS work
    # lands, we pass a telemetry sink that flips the chapter field when its worker
    # call returns. CallTelemetry.task_id is "chN" (1-based) for chapter workers.
    # _ChapterCheckboxSink wraps the real `sink` (_UsageSink) and exposes a
    # `.credits` passthrough so downstream duck-typed credit folding (e.g.
    # orchestrator/dynamic.py's _reveal_dedup_amend) reaches real settlement.
    _checkbox_sink = _ChapterCheckboxSink(sink, job_id=job_id, total=total)

    req = dict(body or {})
    req.update({
        "job_id": job_id,
        "tenant_id": tenant_id,
        "telemetry_sink": _checkbox_sink,
    })

    # Keep the credit hold's TTL warm across a long job so it never lapses and
    # strands the reservation. Runs alongside the cancel watcher.
    async def _keep_hold_warm() -> None:
        if not meter_op:
            return
        while True:
            try:
                await credits_lib.touch_hold(tenant_id, meter_op)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(60)

    gen_task = asyncio.ensure_future(generate_narration(req))
    cancel_task = asyncio.ensure_future(_cancel_watcher(job_id))
    warm_task = asyncio.ensure_future(_keep_hold_warm())

    result: Optional[dict] = None
    cancelled = False
    try:
        await _set_status(job_id, _STATUS_RUNNING)
        await _safe_progress(job_id, "Composing narration…")

        done, pending = await asyncio.wait(
            {gen_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and not gen_task.done():
            # Cancel requested mid-flight → stop generation after the in-flight call.
            cancelled = True
            gen_task.cancel()
            try:
                await gen_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        else:
            # Generation finished first (or together) → take its result.
            try:
                result = await gen_task
            except Exception as exc:  # noqa: BLE001 - router never raises, belt+braces
                log.exception("narration job %s: generate_narration raised", job_id)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        for t in (cancel_task, warm_task):
            if not t.done():
                t.cancel()
        # Drain cancellations quietly.
        for t in (cancel_task, warm_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # --------------------- terminal handling ------------------------------
    if cancelled:
        await _finalize(
            job_id, job_uuid, tenant_id, status=_STATUS_CANCELLED,
            result=None, error="Job cancelled by user")
        log.info("narration job %s cancelled and finalized after %.1fs",
                 job_id, time.monotonic() - started)
        # A7 (Rino 2026-07-04): cancel charges the chapters ALREADY generated (partial
        # commit), refunding only the unused remainder — the sink holds the real cost of
        # every completed call at cancel time, and charge.settle() commits actual + refunds
        # the rest. Cancel before ANY chapter completed (sink empty) → full refund as
        # before. Completed chapters stay recoverable via their narasi_chapters
        # checkpoints (S2 resume writes them as each chapter lands).
        if (sink.tokens_out or 0) > 0:
            log.info("narration job %s cancelled after %d calls — settling partial "
                     "(tok_out=%d) instead of full refund", job_id, sink.calls, sink.tokens_out)
            await _settle(meter_op, tenant_id, user_id, model, job_uuid, sink)
        else:
            await _refund(meter_op, tenant_id, job_id)
        await _p0a_flush("cancelled")
        return

    result = dict(result or {})
    ok = bool(result.get("ok")) and bool(result.get("book") or result.get("output"))

    # Light any chapter checkboxes the telemetry path didn't catch (e.g. a chapter
    # whose worker was short-circuited) from the final chapter records.
    await _reconcile_checkboxes(job_id, result, total)

    if not ok:
        await _finalize(
            job_id, job_uuid, tenant_id, status=_STATUS_FAILED,
            result=_result_payload(result), error=str(result.get("error") or "generation_failed"))
        await _refund(meter_op, tenant_id, job_id)
        await _p0a_flush("failed")
        return

    # Success: persist chapters + the assembled script, settle the hold at ACTUAL.
    await _set_status(job_id, _STATUS_POLISHING if result.get("polished") else _STATUS_DONE)
    # The gates phase (counters/diet → strip → register → factscan/verify → header) can
    # take minutes on a big book — surface it so the UI doesn't look hung at 10/10 done.
    await _safe_progress(job_id, "Finalizing: quality gates & verification…")
    # CC v3 gates — terminal bracket/known-bad gate (R-FG4/5/6, ALL scenarios incl. C/D/E
    # whose result carries "output" not "book"), the harari register scorecard (R-H10,
    # report-only), and the "> **Gaya:** ..." metadata header. Never raises.
    _t_gates = time.monotonic()  # timing: GATES phase (Rino 2026-07-06)
    # P0A: `_p0a_timings` collects the critic/revise WALL-TIME timers the gate phase
    # already keeps. It is None unless P0A is on, so with the flag off the gates run
    # byte-identically. Those two phases build their provider client directly
    # (make_narasi_client + _log_narasi_usage) and never reach _UsageSink, so their
    # provider latency is unobservable; phase wall time is a different quantity and is
    # recorded under its own name. Nothing is written here — the elapsed values are only
    # captured, and every write waits for the flush after settlement.
    await _apply_v3_gates(result, body, tenant_id=tenant_id, user_id=user_id, job_uuid=job_uuid,
                          sink=sink, job_id=job_id, p0a_timings=_p0a_timings)
    log.info("narration job %s: GATES done in %.1fs", job_id, time.monotonic() - _t_gates)
    _p0a_gate_ms = int(max(0.0, time.monotonic() - _t_gates) * 1000)
    # POST-GATES DEDUP GUARD (narasi round-16 postmortem, second layer — orchestrator.static's
    # narrate_chapters already runs this BEFORE polish/critique/revise; this is the LAST point
    # before the manuscript is persisted/returned, after _apply_v3_gates' critique-revise and
    # canon-diff-revise passes have made their own text edits. Reuses the exact same helper so
    # the two layers share one definition. Never raises; a no-op when nothing duplicated.
    try:
        _dgkey = "book" if result.get("book") else "output"
        _dgtxt = result.get(_dgkey) or ""
        if _dgtxt:
            from orchestrator.static import _dedup_chapter_blocks as _dedup_final
            _dgtxt2, _dg_dropped = _dedup_final(_dgtxt)
            if _dg_dropped:
                result[_dgkey] = _dgtxt2
                log.warning("narration job %s: POST-GATES dedup guard collapsed %d duplicate "
                            "chapter-heading block(s)", job_id, _dg_dropped)
    except Exception as _dge:  # noqa: BLE001 - a dedup bug must never break generation
        log.debug("narration job %s: post-gates dedup guard skipped (%s)", job_id, _dge)
    await _persist_chapters(tenant_id, job_uuid, result)
    await _finalize(
        job_id, job_uuid, tenant_id, status=_STATUS_DONE,
        result=_result_payload(result), error=None)
    # Settle the credit hold at the real token total the sink accumulated.
    await _settle(meter_op, tenant_id, user_id, model, job_uuid, sink)
    charge_settled = True
    log.info("narration job %s done in %.1fs (%d calls, tok_in=%d tok_out=%d)",
             job_id, time.monotonic() - started, sink.calls, sink.tokens_in, sink.tokens_out)
    # Defensive: if we somehow reached here without settling, refund.
    if not charge_settled:
        await _refund(meter_op, tenant_id, job_id)
    await _p0a_flush("done")


# ---------------------------------------------------------------------------
# CC v3 (Stop the Pendulum) — terminal gates for the ⚡ engine. All best-effort.
# ---------------------------------------------------------------------------
async def _apply_v3_gates(result: dict, body: dict, *, tenant_id=None, user_id=None, job_uuid=None,
                          sink: "Optional[_UsageSink]" = None, job_id: Optional[str] = None,
                          p0a_timings: Optional[dict] = None) -> None:
    """Mutates `result` in place: (1) R-FG4/5/6 deterministic gate on the final book +
    every chapter record (the per-chapter gate in static.py covers scenario A/B workers;
    this terminal pass also covers C/D/E outputs and anything the polish reintroduced);
    (2) R-H10 register scorecard for harari (report-only, one cheap call); (3) the
    "> **Gaya:** ..." metadata header, so ⚡ output matches the classic engine."""
    try:
        import narasi_gate as _ngate
    except Exception:  # noqa: BLE001
        return
    style = str(body.get("style") or "").strip()
    language = str(body.get("language") or "id").strip()

    # ── (0.05) FRONT-MATTER STRIP — runs BEFORE every scan (NARASI_FRONTMATTER_STRIP, default OFF, round-8):
    # roll-12 exported the ENTIRE production brief — bilingual synopsis, episode
    # targets, markdown tables — as 339 lines before "Chapter 1:" (input-passthrough),
    # which also exploded the revise splitter to 27 parts. Deterministic: when a
    # Chapter-1 heading exists and the preamble before it is large AND carries brief
    # markers (markdown headers / target lines / synopsis labels), drop the preamble.
    # Never fires on a clean book (preamble < 400 chars or no markers). Never raises.
    try:
        if str(os.environ.get("NARASI_FRONTMATTER_STRIP", "0")).strip().lower() in ("1", "true", "yes", "on"):
            import re as _fmre
            _fmkey = "book" if result.get("book") else "output"
            _fmbk = result.get(_fmkey) or ""
            _fmm = _fmre.search(r"(?m)^Chapter\s+1\s*[:.]", _fmbk)
            if _fmm and _fmm.start() > 400:
                _fmpre = _fmbk[:_fmm.start()]
                if _fmre.search(r"(?m)^#{1,3} |\*\*Target|Target\s*:|Sinopsis|Logline|Estimasi|Episode \d+ —", _fmpre):
                    result[_fmkey] = _fmbk[_fmm.start():]
                    log.warning("front-matter STRIPPED: %d chars of pre-Chapter-1 brief echo removed "
                                "(%d markdown/target markers)", _fmm.start(),
                                len(_fmre.findall(r"(?m)^#{1,3} |Target\s*:", _fmpre)))
    except Exception as e:  # noqa: BLE001
        log.warning("front-matter strip failed (non-fatal): %s", e)

    # ── (0) CC v4 §1: deterministic counters + surgical diet loop (max 2). Budgets come
    # from the style's style_spec (only harari is tuned today; others = OFF/UNMEASURED).
    # Runs BEFORE the terminal gate so a diet rewrite can never ship bracket residue.
    try:
        import narasi_counters as _nc
        entry = None
        try:
            from pakem import resolve_style as _rs
            entry = _rs(style)
        except Exception:  # noqa: BLE001
            entry = None
        has_budgets = bool(((entry or {}).get("style_spec") or {}).get("counters"))
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book and entry is not None:
            wt = 0
            for c in (body.get("chapters") or []):
                if isinstance(c, dict):
                    try:
                        wt += int(c.get("word_target") or c.get("words") or 0)
                    except (TypeError, ValueError):
                        pass
            embed = None
            try:
                import dalang_dedup as _dd
                embed = getattr(_dd, "embed", None)
            except Exception:  # noqa: BLE001
                embed = None
            # ROUND-9: the 20k-word regex sweep runs OFF the event loop — a starved loop
            # misses bull lock renewals and the job gets stalled-redelivered mid-run.
            rep = await asyncio.to_thread(
                _nc.scan_manuscript, book, lang=language, style_entry=entry,
                word_target=wt or None, embed_fn=embed,
                bible=str(result.get("canonical_facts") or ""))

            # Tolerance band: one diet round = ONE full-book Opus stream (~5-6 min on a
            # 5k-word book — itaatga7's whole "why is it stuck" phase). Not worth it for
            # a marginal overshoot: budgeted counters must exceed budget × TOLERANCE to
            # justify the rewrite; discrete violations (scene_dup/anchor_voice/epithet,
            # no numeric budget) always qualify.
            _diet_tol = float(os.environ.get("NARASI_DIET_TOLERANCE", "1.3"))
            # Hard cap on diet rounds (Rino 2026-07-06). NARASI_DIET_MAX_LOOPS=0 turns
            # the editorial-refinement diet loop OFF entirely — the slowest post-chapter
            # phase (each round = one full-book Opus rewrite). Default 2 = prior behavior.
            _diet_max_loops = max(0, int(os.environ.get("NARASI_DIET_MAX_LOOPS", "2")))

            def _diet_worthy(r: dict) -> list:
                worthy = []
                for k in r.get("over_budget") or []:
                    v = (r.get("counters") or {}).get(k) or {}
                    b = v.get("budget")
                    if b and int(v.get("count") or 0) <= int(b) * _diet_tol:
                        continue   # marginal overshoot — report it, don't burn a rewrite
                    worthy.append(k)
                return worthy

            loops = 0
            while has_budgets and _diet_worthy(rep) and loops < _diet_max_loops:
                loops += 1
                if job_id:
                    await _safe_progress(job_id, "Editorial refinement …")
                try:
                    from laozhang_api import make_narasi_client, _resolve_narasi_lang as _rl
                    instr = _nc.surgical_prompt(rep, language=_rl(language))
                    model = str(body.get("worker_model") or "claude-opus-4-6")
                    cli = make_narasi_client(model)
                    # Rino: "editorial refinement paling lama" — b92lvku8 diet call
                    # burned 4800 output tokens over 3.5 min on a 2200-word book. The
                    # surgical prompt tells Opus to touch only listed sentences and
                    # return the WHOLE book — so max_tokens ≈ book size × ~1.15 is
                    # plenty. Older 1.45 × 1.25 = 1.81 bloat gave Opus room to expand.
                    _mult = float(os.environ.get("NARASI_DIET_MAX_TOKENS_MULT", "1.15"))
                    mt = min(32000, int(len(book.split()) * 1.45 * _mult) + 400)
                    resp = await asyncio.wait_for(asyncio.to_thread(
                        lambda: cli.chat.completions.create(
                            model=model,
                            messages=[{"role": "user", "content": instr + "\n\nMANUSCRIPT:\n" + book}],
                            max_tokens=mt, stream=False)),
                        timeout=float(os.environ.get("NARASI_DIET_TIMEOUT", "300")))
                    # A2: this is a real (up to 32k-token Opus) call — it MUST be metered
                    # and logged, or an over-budget book delivers a large rewrite billed to
                    # nobody and invisible in usage_logs. Feed the job's sink: it accumulates
                    # cost_usd for _settle AND fans out a usage_logs row. Also extract the
                    # finish_reason so a truncated rewrite is rejected (A15), not shipped.
                    out, _din, _dout, _dfin = _core_extract(resp)
                    if sink is not None:
                        try:
                            sink(CallTelemetry(
                                model=model, role="editor", ok=True,
                                tokens_in=_din, tokens_out=_dout,
                                cost_usd=_core_cost(model, _din, _dout),
                                finish_reason=_dfin, task_id=f"diet{loops}",
                                provider=str(getattr(resp, "_narasi_served_by", "") or "")))
                        except Exception:  # noqa: BLE001 - never let metering break the gate
                            pass
                    _dtrunc = str(_dfin or "").strip().lower() in ("length", "max_tokens", "max_output_tokens")
                    # a surgical edit can only shrink modestly — reject a gutted rewrite;
                    # A15: reject a truncated rewrite regardless of ratio (it would replace
                    # the full book with a mid-sentence cut).
                    if out and not _dtrunc and len(out.split()) >= int(len(book.split()) * 0.7):
                        book = out
                        result[key] = book
                        rep = await asyncio.to_thread(
                            _nc.scan_manuscript, book, lang=language, style_entry=entry,
                            word_target=wt or None, embed_fn=embed,
                            bible=str(result.get("canonical_facts") or ""))
                    else:
                        if _dtrunc:
                            log.warning("counter diet loop: rewrite truncated (finish=%s) — kept original", _dfin)
                        break
                except Exception as e:  # noqa: BLE001
                    log.warning("counter diet loop failed (non-fatal): %s", e)
                    break
            rep["diet_loops"] = loops
            result["counter_report"] = {
                "over_budget": rep.get("over_budget", []),
                "diet_loops": loops,
                "counters": {k: {kk: vv for kk, vv in v.items() if kk != "sentences"}
                             for k, v in rep.get("counters", {}).items()},
            }
            # Ledger validator (report-only): a BIBLE-level ledger hit poisons every
            # chapter (the eleven-month-drought class) — surface as WARN, never a gate.
            _lhits = (rep.get("counters") or {}).get("ledger_hits") or {}
            if _lhits.get("bible_hits"):
                log.warning("ledger validator: %d bible-level ledger hit(s) (poison every chapter): %s",
                            _lhits["bible_hits"],
                            sorted({str(h.get("term")) for h in _lhits.get("hits") or []
                                    if h.get("where") == "bible"})[:10])
            # Timeline arithmetic (report-only WARN): derived spans vs dated events
            # ("Three months after the flood" beside 13 Aug → 17 Oct) + adjacent
            # year-span alternation (fourteen×7 vs fifteen×3). Enforcement rides the
            # critique injection below (NARASI_LEDGER_ENFORCE), not this WARN.
            _brep = (rep.get("counters") or {}).get("real_brands") or {}
            if _brep.get("hits"):
                log.warning("REAL-BRAND scan: %d hit(s) — real conglomerate as in-story entity (legal risk): %s",
                            len(_brep["hits"]),
                            [f"{h.get('brand')}@{h.get('where')}" for h in _brep["hits"]][:5])
            _fzrep = (rep.get("counters") or {}).get("ledger_fuzzy") or {}
            if _fzrep.get("hits"):
                # premise-supplied names (Do-yun) inevitably near-match some banned name;
                # they are the user's, not a dodge — same exemption as the enforce path.
                import re as _fre
                _ftopic = str(body.get("topic") or body.get("goal") or body.get("brief") or "")
                _fhits = [h for h in _fzrep["hits"]
                          if not _fre.search(r"(?i)\b" + _fre.escape(str(h.get("name") or "")) + r"\b", _ftopic)]
                if _fhits:
                    log.warning("ledger fuzzy: %d near-variant name(s) dodging bans: %s",
                                len(_fhits),
                                [f"{h.get('name')}≈{h.get('near')}" for h in _fhits][:6])
            _tlrep = (rep.get("counters") or {}).get("timeline_arith") or {}
            if _tlrep.get("count"):
                log.warning("timeline arithmetic: %d derived-span mismatch(es): %s",
                            _tlrep["count"],
                            [str(f.get("stated_phrase") or f.get("values"))
                             for f in _tlrep.get("findings") or []][:5])
            _nmrep = (rep.get("counters") or {}).get("numeric_magnitude") or {}
            if _nmrep.get("magnitude") or _nmrep.get("year_forks"):
                log.warning("numeric magnitude: %d scale fork(s), %d year fork(s): %s",
                            len(_nmrep.get("magnitude") or []), len(_nmrep.get("year_forks") or []),
                            [f.get("note") for f in (_nmrep.get("magnitude") or []) + (_nmrep.get("year_forks") or [])][:4])
            _alrep = (rep.get("counters") or {}).get("age_ledger") or {}
            if _alrep.get("count"):
                log.warning("age ledger: %d age/span fork(s): %s",
                            _alrep["count"], [f.get("note") for f in _alrep.get("findings") or []][:4])
            _axrep = (rep.get("counters") or {}).get("alias_ledger") or {}
            if _axrep.get("forks") or _axrep.get("misfiled"):
                log.warning("alias ledger: %d alias/legal fork(s), %d doc-misfile(s): %s",
                            len(_axrep.get("forks") or []), len(_axrep.get("misfiled") or []),
                            [f.get("note") for f in (_axrep.get("misfiled") or []) + (_axrep.get("forks") or [])][:4])
            _carep = (rep.get("counters") or {}).get("canon_anchor") or {}
            if _carep.get("count"):
                log.warning("canon anchor: %d date fork(s): %s",
                            _carep["count"], [f.get("note") for f in _carep.get("findings") or []][:4])
            _phrep = (rep.get("counters") or {}).get("placeholder") or {}
            if _phrep.get("count"):
                log.warning("placeholder scan: %d unsubstituted token(s): %s",
                            _phrep["count"], [h.get("token") for h in _phrep.get("hits") or []][:4])
            _eqrep = (rep.get("counters") or {}).get("entity_qty") or {}
            if _eqrep.get("forks"):
                log.warning("entity quantity: %d entity count fork(s): %s",
                            len(_eqrep["forks"]), [f.get("note") for f in _eqrep["forks"]][:4])
            _knrep = (rep.get("counters") or {}).get("kinship") or {}
            if _knrep.get("mismatches"):
                log.warning("kinship term: %d term/label mismatch(es): %s",
                            len(_knrep["mismatches"]), [m.get("note") for m in _knrep["mismatches"]][:4])
            # location_continuity: WARN-only by design (low-confidence heuristic, never injected).
            _lcrep = (rep.get("counters") or {}).get("location_continuity") or {}
            if _lcrep.get("count"):
                log.warning("location continuity (heuristic, report-only): %d possible gap(s): %s",
                            _lcrep["count"], [f.get("note") for f in _lcrep.get("findings") or []][:4])
            if rep.get("over_budget"):
                log.warning("counters still over budget after %d diet loop(s): %s",
                            loops, rep["over_budget"])
    except Exception as e:  # noqa: BLE001
        log.warning("counter engine failed (non-fatal): %s", e)

    _mode = "video" if str(body.get("mode") or "").strip() == "video" else "book"

    # ── (0.7) SPEC v1 §3.2: UNIFIED proper_noun_verify pass — persons + institutions +
    # places + treaties in ONE call. Falls back to the legacy scholar-only entity_pass
    # if the unified module isn't importable. Runs before the terminal gate so its
    # corrections are themselves swept. Title/body treaty consistency now checked here. ──
    try:
        _title = str(body.get("topic") or body.get("goal") or body.get("brief") or "")
        _pnv = None
        try:
            import narasi_proper_noun as _pnv
        except Exception:  # noqa: BLE001
            _pnv = None
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book and _pnv is not None:
            fixed, ent_report = _pnv.verify_pass(book, title=_title, lang=language)
            result[key] = fixed
            for rec in result.get("chapters") or []:
                if rec.get("content"):
                    rec["content"], _er = _pnv.verify_pass(rec["content"], title=_title,
                                                            lang=language)
            result["entity_report"] = ent_report
            if ent_report.get("self_debate"):
                log.warning("proper_noun: self-debate conflict(s) flagged: %s",
                            [d.get("scholar") for d in ent_report["self_debate"]])
            if ent_report.get("title_body_conflict"):
                log.warning("proper_noun: treaty title/body conflict: %s",
                            ent_report["title_body_conflict"])
            if ent_report.get("merge_candidates"):
                log.info("proper_noun: same-surname merge candidates: %s",
                         [c.get("surname") for c in ent_report["merge_candidates"]])
        elif book:
            # legacy fallback (scholars only)
            import narasi_entities as _nent
            fixed, ent_report = _nent.entity_pass(book)
            result[key] = fixed
            for rec in result.get("chapters") or []:
                if rec.get("content"):
                    rec["content"], _er = _nent.entity_pass(rec["content"])
            result["entity_report"] = ent_report
    except Exception as e:  # noqa: BLE001
        log.warning("proper_noun_verify pass failed (non-fatal): %s", e)

    # ── (0.75) PHANTOM-NAME scan (report-only) — story-bible bleed: a PERSON whose FIRST
    # mention falls in the final 25% of the book with <=2 total mentions (kdrama eky9gcge
    # "Shim Ro-ha" class: bible cast member surfaces once, in the finale, with
    # presupposition phrasing). Runs AFTER the 0.7 proper_noun pass so table-driven variant
    # unification has already collapsed aliases, and reads the story bible from
    # result["canonical_facts"] for in_bible annotation. status FLAG/PASS only — never
    # over_budget, never edits text (same FLAG-never-OVER contract as opening_motif).
    # Gated NARASI_PHANTOM_NAME_SCAN (default OFF → skipped → byte-identical). FICTION-only:
    # the defect class is story-bible cast bleed; nonfiction legitimately names a closing
    # authority once near the end (fail-soft: unresolvable style → skip). Never raises.
    try:
        if str(os.environ.get("NARASI_PHANTOM_NAME_SCAN", "0")).strip().lower() in ("1", "true", "yes", "on"):
            _ph_fic = False
            try:
                from pakem import resolve_style as _ph_rs
                _phe = _ph_rs(str(body.get("style") or "")) or {}
                _ph_fic = bool(_phe.get("is_fiction")) or str(
                    _phe.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
            except Exception:  # noqa: BLE001
                _ph_fic = False
            import narasi_proper_noun as _ppn
            _phkey = "book" if result.get("book") else "output"
            _phbk = result.get(_phkey) or ""
            if _ph_fic and _phbk and hasattr(_ppn, "phantom_name_scan"):
                # Tunables parsed separately so a malformed value disables only the
                # override (with a named warning), never the whole scan silently.
                try:
                    _ph_tf = float(os.environ.get("NARASI_PHANTOM_TAIL_FRAC", "0.25"))
                except Exception:  # noqa: BLE001
                    log.warning("NARASI_PHANTOM_TAIL_FRAC malformed — using 0.25")
                    _ph_tf = 0.25
                _ph_tf = min(max(_ph_tf, 0.05), 1.0)
                try:
                    _ph_mm = int(os.environ.get("NARASI_PHANTOM_MAX_MENTIONS", "2"))
                except Exception:  # noqa: BLE001
                    log.warning("NARASI_PHANTOM_MAX_MENTIONS malformed — using 2")
                    _ph_mm = 2
                _phrep = _ppn.phantom_name_scan(
                    _phbk, bible=str(result.get("canonical_facts") or ""),
                    tail_frac=_ph_tf, max_mentions=_ph_mm)
                result["phantom_name_report"] = _phrep
                if _phrep.get("status") == "FLAG":
                    log.warning("phantom-name scan: %d late-first-mention name(s) flagged (report-only): %s",
                                _phrep.get("count", 0),
                                [h.get("name") for h in _phrep.get("names") or []])
    except Exception as e:  # noqa: BLE001
        log.warning("phantom-name scan failed (non-fatal): %s", e)

    # ── (0.76) INTRODUCTION-ORDER scan (report-only) — "pre-introduction leak":
    # OPPOSITE polarity from the (0.75) phantom-name scan above. A character casually
    # name-dropped (dialogue topic / narrator exposition, presupposing phrasing) well
    # BEFORE their formal narrative introduction (a live scene/POV appearance) — the
    # "Cha Hyun-soo"/"Song Dae-il" class: both named early, formally introduced much
    # later, reading as an unintroduced-character bug. Independent function
    # (narasi_proper_noun.introduction_order_scan) — does NOT touch or reuse
    # phantom_name_scan's position+frequency logic (a prior in-session attempt to loosen
    # phantom_name_scan's own mentions threshold could not fix this polarity and
    # reintroduced ensemble-cast false positives). Runs AFTER the 0.7 proper_noun pass
    # for the same alias-unification reason as 0.75. status FLAG/PASS/OFF only — never
    # over_budget, never edits text. Gated NARASI_INTRO_ORDER_SCAN (default OFF →
    # skipped → byte-identical). FICTION-only, same rationale as 0.75 (nonfiction
    # legitimately name-drops a scholar/figure before a fuller treatment later). Never
    # raises.
    try:
        if str(os.environ.get("NARASI_INTRO_ORDER_SCAN", "0")).strip().lower() in ("1", "true", "yes", "on"):
            _io_fic = False
            try:
                from pakem import resolve_style as _io_rs
                _ioe = _io_rs(str(body.get("style") or "")) or {}
                _io_fic = bool(_ioe.get("is_fiction")) or str(
                    _ioe.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
            except Exception:  # noqa: BLE001
                _io_fic = False
            import narasi_proper_noun as _ppn2
            _iokey = "book" if result.get("book") else "output"
            _iobk = result.get(_iokey) or ""
            if _io_fic and _iobk and hasattr(_ppn2, "introduction_order_scan"):
                try:
                    _io_prox = int(os.environ.get("NARASI_INTRO_ORDER_PROXIMITY_CHARS", "1200"))
                except Exception:  # noqa: BLE001
                    log.warning("NARASI_INTRO_ORDER_PROXIMITY_CHARS malformed — using 1200")
                    _io_prox = 1200
                _iorep = _ppn2.introduction_order_scan(_iobk, proximity_chars=_io_prox)
                result["introduction_order_report"] = _iorep
                if _iorep.get("status") == "FLAG":
                    log.warning("introduction-order scan: %d pre-introduction leak(s) flagged (report-only): %s",
                                _iorep.get("count", 0),
                                [h.get("name") for h in _iorep.get("names") or []])
    except Exception as e:  # noqa: BLE001
        log.warning("introduction-order scan failed (non-fatal): %s", e)

    # ── (0.77) LANGUAGE-CONSISTENCY word scan (report-only) — a job whose target language
    # is NOT Indonesian occasionally leaks a stray Indonesian function word/particle into
    # the prose (the "Kapan mereka datang" class: a short clause built entirely from
    # words absent from the two existing checks' seeded sets — narasi_gate's sentence-level
    # language_consistency_scan needs a WHOLE sentence with zero job-language overlap, and
    # #A3's _home_lang_bleed_scan in narasi_counters.py is word-level but deliberately
    # tight and lacks "kapan"/"mereka" too). This is a THIRD, independent word-level check
    # (narasi_counters.language_consistency_word_scan): curated Indonesian function-word
    # list, whole-word regex, skipped entirely for ID-family targets (native there).
    # status FLAG/PASS only — never edits text. Gated NARASI_LANGUAGE_CONSISTENCY_SCAN
    # (default OFF -> skipped -> byte-identical). A separate, independently-gated
    # NARASI_LANGUAGE_CONSISTENCY_ENFORCE (default OFF, checked further below where the
    # other mechanical violations feed the merged critic/revise pool) can additionally
    # route hits into a rename/rewrite pass — this block is report-only. Never raises.
    try:
        if str(os.environ.get("NARASI_LANGUAGE_CONSISTENCY_SCAN", "0")).strip().lower() in ("1", "true", "yes", "on"):
            import narasi_counters as _lcc
            _lckey = "book" if result.get("book") else "output"
            _lcbk = result.get(_lckey) or ""
            if _lcbk and hasattr(_lcc, "language_consistency_word_scan"):
                _lcrep = _lcc.language_consistency_word_scan(_lcbk, language)
                result["language_consistency_report"] = _lcrep
                if _lcrep.get("status") == "FLAG":
                    log.warning("language-consistency word scan: %d Indonesian function-word hit(s) "
                                "in a %s-language manuscript (report-only): %s",
                                _lcrep.get("count", 0), language,
                                [h.get("term") for h in _lcrep.get("samples") or []][:8])
    except Exception as e:  # noqa: BLE001
        log.warning("language-consistency word scan failed (non-fatal): %s", e)

    # ── (0.8) TITLE INTEGRITY (round-6, deterministic, log-only): two historical rolls
    # shipped a CLIPPED chapter-4 title ("Of the Shadow", "Of the Drought") and nothing
    # noticed — assembled headers were never compared to the outline-derived records.
    # Also catches duplicate and missing headers. Never edits; never raises.
    try:
        import re as _tire
        _tikey = "book" if result.get("book") else "output"
        _tibk = result.get(_tikey) or ""
        if _tibk:
            _tihdrs = _tire.findall(r"(?m)^Chapter\s+(\d+)\s*[:.]\s*(.+?)\s*$", _tibk)
            _tiwarn = []
            _tiseen: dict = {}
            for _tn, _tt in _tihdrs:
                if _tt in _tiseen:
                    _tiwarn.append(f"duplicate title '{_tt}' (ch {_tiseen[_tt]} & {_tn})")
                _tiseen[_tt] = _tn
                if len(_tt) < 8 or _tt.lower().startswith("of "):
                    _tiwarn.append(f"suspect/clipped title ch {_tn}: '{_tt}'")
            _tirecs = [str(r.get("title") or "") for r in (result.get("chapters") or []) if r.get("title")]
            if _tirecs and _tihdrs and len(_tirecs) == len(_tihdrs):
                for _ti, ((_tn, _tt), _tr) in enumerate(zip(_tihdrs, _tirecs), 1):
                    _ta, _tb = _tt.strip().lower(), _tr.strip().lower()
                    if _ta and _tb and _ta != _tb and _ta not in _tb and _tb not in _ta:
                        _tiwarn.append(f"ch {_tn} header '{_tt}' != outline title '{_tr}'")
            try:
                # ROUND-10: 'Weight' shipped again in a FINAL header — check final
                # headers against the lane's banned tokens too (outline check can be
                # bypassed by writer retitles).
                from orchestrator.dynamic import _title_ban_hits as _tbh_fn
                _tbh2 = _tbh_fn([{"title": t} for _, t in _tihdrs], str(body.get("style") or ""))
                if _tbh2:
                    _tiwarn.extend(f"banned-token: {h}" for h in _tbh2[:3])
            except Exception:  # noqa: BLE001
                pass
            if _tiwarn:
                log.warning("title integrity: %d issue(s): %s", len(_tiwarn), _tiwarn[:4])
    except Exception as e:  # noqa: BLE001
        log.warning("title integrity check failed (non-fatal): %s", e)

    async def _r9_gate_domain():
        # ── (0.85) DOMAIN PLAUSIBILITY (NARASI_DOMAIN_PLAUSIBILITY, default OFF, round-6):
        # the SBF 4-lens review surfaced a defect family no deterministic scan can reach —
        # legal procedure (US-style class action + discovery in a Korean court, a charge
        # that doesn't fit the act, a 4-month filing-to-dissolution timeline), medicine
        # (one temporal-bone fragment destroying BOTH cochlear nerves), engineering
        # (7-story "unreinforced" slab). One bounded cheap extract-and-check call lists
        # implausible domain claims; report-only WARN (selection/verification lane — no
        # prompt rules were added for this). Fiction-only. Never raises.
        try:
            if str(os.environ.get("NARASI_DOMAIN_PLAUSIBILITY", "0")).strip().lower() in ("1", "true", "yes", "on"):
                _dp_fic = False
                try:
                    from pakem import resolve_style as _dp_rs
                    _dpe = _dp_rs(str(body.get("style") or "")) or {}
                    _dp_fic = bool(_dpe.get("is_fiction")) or str(
                        _dpe.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
                except Exception:  # noqa: BLE001
                    _dp_fic = False
                _dpkey = "book" if result.get("book") else "output"
                _dpbk = result.get(_dpkey) or ""
                if _dp_fic and _dpbk:
                    from laozhang_api import _narasi_cheap_call as _dpcall, _narasi_parse_json as _dpparse
                    # HARDENING (2026-07-17, confirmed hallucination/echo mechanism): a cheap/fast
                    # model given unlabeled illustrative examples in the system prompt AND an
                    # undelimited manuscript blob in the user turn will sometimes return the
                    # EXAMPLE wording verbatim as its "quote" (or otherwise paraphrase/invent one)
                    # instead of a real manuscript excerpt — the schema never said "verbatim" and
                    # nothing marked the examples as off-limits. Three prompt-side fixes plus a
                    # deterministic post-check (belt-and-suspenders — catches this regardless of
                    # whether the wording changes fully close the model's tendency to hallucinate).
                    _dpsys = (
                        "You are a domain-plausibility checker for fiction. Scan the MANUSCRIPT TEXT "
                        "below for claims about LAW/legal procedure, MEDICINE/anatomy, or ENGINEERING/"
                        "physics that a professional in that field would call clearly wrong or "
                        "impossible — the kind that breaks reader trust. The following are ILLUSTRATIVE "
                        "CATEGORIES ONLY, describing the KIND of error to look for — they are NOT "
                        "manuscript text and must NEVER be echoed or reused as your answer, verbatim or "
                        "paraphrased (a metal hammer kept in a prison cell; a charge name that does not "
                        "match the act; one lateral impact destroying both cochlear nerves; an "
                        "unreinforced 7-story concrete slab). IGNORE stylistic choices, genre "
                        "conventions, and anything merely unlikely. Return ONLY JSON: "
                        "{\"claims\":[{\"quote\":\"<verbatim substring copied character-for-character "
                        "from the MANUSCRIPT TEXT below — never from these instructions or examples>\","
                        "\"domain\":\"law|medicine|engineering\",\"why\":\"<one line>\","
                        "\"severity\":\"high|low\"}]} — max 8, hard errors only.")
                    _dpsrc = _dpbk[:60000]
                    _dpuser = "MANUSCRIPT TEXT:\n\"\"\"\n" + _dpsrc + "\n\"\"\""
                    _dpraw, _dpcc = await _dpcall(_dpsys, _dpuser, tenant_id=tenant_id,
                                                  user_id=user_id, job_uuid=job_uuid, json_mode=True,
                                                  credit_row=False)
                    if sink is not None and _dpcc:
                        sink.credits += int(_dpcc)
                    _dpd = _dpparse(_dpraw) if isinstance(_dpraw, str) else (_dpraw or {})
                    _dpcl = (_dpd or {}).get("claims") if isinstance(_dpd, dict) else None
                    if isinstance(_dpcl, list) and _dpcl:
                        # DETERMINISTIC POST-CHECK: drop any claim whose "quote" is not an actual
                        # literal substring of the exact manuscript slice sent — this is the robust
                        # half of the fix, independent of prompt compliance (same idiom as
                        # _numeric_drifts/_numeric_sum_errors' deterministic post-checks elsewhere
                        # in this file). A dropped claim never reaches domain_plausibility_report,
                        # so NARASI_DOMAIN_ENFORCE can never inject a hallucinated/echoed "fix".
                        _dpverified = [c for c in _dpcl if isinstance(c, dict)
                                       and str(c.get("quote") or "").strip()
                                       and str(c.get("quote")) in _dpsrc]
                        _dpdropped = len(_dpcl) - len(_dpverified)
                        if _dpdropped:
                            log.warning("domain plausibility: dropped %d/%d claim(s) — quote not "
                                        "found verbatim in manuscript (hallucinated/echoed)",
                                        _dpdropped, len(_dpcl))
                        if _dpverified:
                            result["domain_plausibility_report"] = {"claims": _dpverified[:8]}
                            log.warning("domain plausibility: %d implausible claim(s): %s",
                                        len(_dpverified[:8]),
                                        [f"{c.get('domain')}: {str(c.get('quote') or '')[:60]}"
                                         for c in _dpverified[:4] if isinstance(c, dict)])
                        else:
                            log.info("domain plausibility: no verified hard errors flagged "
                                     "(%d claim(s) dropped as unverifiable)", _dpdropped)
                    else:
                        log.info("domain plausibility: no hard errors flagged")
        except Exception as e:  # noqa: BLE001
            log.warning("domain plausibility check failed (non-fatal): %s", e)

    async def _r9_gate_numeric():
        # ── (0.87) NUMERIC LEDGER (NARASI_NUMERIC_LEDGER, default OFF, round-7 — lens-4
        # P1.1, the single gate that would have caught the most findings across 11 QA'd
        # files): one bounded cheap call extracts every plot-load-bearing number WITH its
        # referent; a deterministic post-check flags referents carrying >=2 distinct
        # values. Deliberate official-vs-true contrasts (COUNTERPOINT NUMBERS, heading
        # 17) are marked intentional by the extractor and skipped. Report-only unless
        # NARASI_NUMERIC_LEDGER_ENFORCE. Fiction-only. Never raises.
        try:
            if _r7_env_on("NARASI_NUMERIC_LEDGER"):
                _nl_fic = False
                try:
                    from pakem import resolve_style as _nl_rs
                    _nle = _nl_rs(str(body.get("style") or "")) or {}
                    _nl_fic = bool(_nle.get("is_fiction")) or str(
                        _nle.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
                except Exception:  # noqa: BLE001
                    _nl_fic = False
                _nlkey = "book" if result.get("book") else "output"
                _nlbk = result.get(_nlkey) or ""
                if _nl_fic and _nlbk:
                    from laozhang_api import _narasi_cheap_call as _nlcall, _narasi_parse_json as _nlparse
                    _nlsys = (
                        "You are a numeric-continuity extractor for a multi-chapter story. List every "
                        "PLOT-LOAD-BEARING number with its referent: death/injury tolls, ages and age "
                        "gaps, money amounts, durations, day-counts, list positions, measurements, "
                        "classification levels, and years an object/event is dated to (a founding, an "
                        "opening, a closure). For each referent collect EVERY distinct value the text "
                        "states, with the chapter number. Where the story DELIBERATELY contrasts an "
                        "official/covered-up value with a true value (cover-up plots), set "
                        "intentional_contrast=true for that referent. Return ONLY JSON: "
                        "{\"referents\":[{\"name\":\"<referent>\",\"intentional_contrast\":false,"
                        "\"values\":[{\"value\":\"<as written>\",\"chapter\":<n>}]}],"
                        "\"equations\":[{\"stated_total\":\"<number>\",\"components\":[\"<n1>\",\"<n2>\"],"
                        "\"overlap\":\"<name of any person counted in TWO components, else empty>\","
                        "\"quote\":\"<verbatim sentence containing the total, copied exactly as "
                        "written>\",\"chapter\":<n>}]} — max 20 referents; equations = every "
                        "stated arithmetic claim (a total with its parts); values as PLAIN NUMBERS "
                        "without units, EXCEPT: if a date is stated only as a phrase relative to another "
                        "established story event rather than as a bare year (e.g. \"the year before the "
                        "factory closed\", \"the summer the war ended\"), capture that phrase VERBATIM as "
                        "the value instead of inventing a number for it — do not omit it. If the story "
                        "itself establishes that one person appears in two components (a mislabeled body "
                        "counted both officially and among the hidden), NAME them in overlap — that is a "
                        "double-count the total must subtract.")
                    # FIX (2026-07-18, truncation root-cause): a flat [:60000] head-slice covered
                    # only ~chapters 1-3 of a 10-chapter/~39K-word book, silently exempting later
                    # chapters from ever being ledgered. DALANG_CHEAP_MODEL (gemini-2.5-flash-lite)
                    # has a multi-hundred-K-token context window — the thread-tracker Pass-1 scan
                    # (same cheap model, same class of whole-book call) already reads up to
                    # NARASI_CRITIQUE_MAX_CHARS (default 300000) chars; reuse that same budget here
                    # instead of a much smaller ad-hoc cap so the ledger actually covers the book.
                    _nl_max_chars = int(os.environ.get("NARASI_CRITIQUE_MAX_CHARS", "300000"))
                    _nlraw, _nlcc = await _nlcall(_nlsys, _nlbk[:_nl_max_chars], tenant_id=tenant_id,
                                                  user_id=user_id, job_uuid=job_uuid, json_mode=True,
                                                  credit_row=False)
                    if sink is not None and _nlcc:
                        sink.credits += int(_nlcc)
                    _nld = _nlparse(_nlraw) if isinstance(_nlraw, str) else (_nlraw or {})
                    _nlrefs = (_nld or {}).get("referents") if isinstance(_nld, dict) else None
                    if not _nlrefs:
                        # ROUND-8: two rolls running returned "0 referent(s)" SILENTLY on
                        # number-saturated books while a 17-vs-16 toll error sat in the text —
                        # same truncation family as the registry fallback. Salvage individually
                        # balanced referent objects, then WARN with the head if still empty.
                        _nls = str(_nlraw or "")
                        _nsal = []
                        import re as _nre
                        import json as _njson
                        for _nbm in _nre.finditer(r"\{", _nls):
                            _nst = _nbm.start()
                            if not _nre.search(r"\"name\"", _nls[_nst:_nst + 120]):
                                continue
                            _nd2, _nin, _nesc = 0, False, False
                            for _ni in range(_nst, min(len(_nls), _nst + 4000)):
                                _nc = _nls[_ni]
                                if _nin:
                                    if _nesc:
                                        _nesc = False
                                    elif _nc == "\\":
                                        _nesc = True
                                    elif _nc == '"':
                                        _nin = False
                                elif _nc == '"':
                                    _nin = True
                                elif _nc == "{":
                                    _nd2 += 1
                                elif _nc == "}":
                                    _nd2 -= 1
                                    if _nd2 == 0:
                                        try:
                                            _nobj = _njson.loads(_nre.sub(r",\s*([}\]])", r"\1", _nls[_nst:_ni + 1]))
                                            if isinstance(_nobj, dict) and _nobj.get("name") and _nobj.get("values"):
                                                _nsal.append(_nobj)
                                        except Exception:  # noqa: BLE001
                                            pass
                                        break
                            if len(_nsal) >= 20:
                                break
                        if _nsal:
                            _nlrefs = _nsal
                            log.info("numeric ledger: SALVAGED %d referent(s) from truncated response", len(_nsal))
                        elif len(_nlbk) > 20000:
                            log.warning("numeric ledger returned no referents on a %d-char book — raw head: %s",
                                        len(_nlbk), str(_nlraw)[:200].replace("\n", " "))
                    _nldr = _numeric_drifts(_nlrefs if isinstance(_nlrefs, list) else [])
                    _nleq = (_nld or {}).get("equations") if isinstance(_nld, dict) else None
                    _nlse = _numeric_sum_errors(_nleq if isinstance(_nleq, list) else [])
                    result["numeric_ledger_report"] = {
                        "referents": len(_nlrefs or []), "drifts": _nldr, "sum_errors": _nlse}
                    if _nlse:
                        log.warning("numeric ledger: %d arithmetic error(s): %s",
                                    len(_nlse), [e["note"][:90] for e in _nlse[:3]])
                    if _nldr:
                        log.warning("numeric ledger: %d referent(s) with conflicting values: %s",
                                    len(_nldr), [f"{d['referent']}={d['values']}" for d in _nldr[:4]])
                    else:
                        log.info("numeric ledger: %d referent(s), no unintentional drift",
                                 len(_nlrefs or []))
        except Exception as e:  # noqa: BLE001
            log.warning("numeric ledger check failed (non-fatal): %s", e)

    async def _r9_gate_entity():
        # ── (0.88) ENTITY ATTRIBUTES (NARASI_ENTITY_ATTR_CHECK, default OFF, round-8):
        # roll-12 shipped Prosecutor Kim as "She" in Ch8 and "a man whose nameplate read
        # only KIM" in Ch9 — the R4 attribute-fork class (ages 7/9/26, Dr. Chae vs
        # Director Yun) in its gender form. One bounded cheap extract-and-check call;
        # report-only. Fiction-only. Never raises.
        # (2026-07-15) confirmed miss: Han So-ra's child was "daughter" in one chapter,
        # "son" in another chapter — same referent — and this gate said "no drift"
        # because it only tracked NAMED characters, never relation-descriptors of
        # people mentioned-but-not-independently-tracked. Prompt now also tracks
        # relation slots (named char + relation type, e.g. "So-ra's child") as a
        # fourth drift kind. Still report-only, same flag, same never-raise contract.
        # FIX (2026-07-19, "Love on the Wrong Pitch" review — a character's sex flipping
        # between chapters is exactly this gate's job and it never fired): two real bugs
        # found on independent investigation, same shape as fixes already applied to
        # numeric-ledger/canon-diff/thread-tracker: (1) _eabk[:60000] is a flat head-slice
        # — on any book longer than ~2-3 chapters a later-chapter drift is structurally
        # invisible; raised to the shared NARASI_CRITIQUE_MAX_CHARS budget. (2) this gate
        # only ever wrote result["entity_attr_report"] and logged — grepped the whole
        # tree, "entity_attr_report" had exactly one other reference before this fix
        # (nothing consumed it) — it could never reach a chapter revise no matter how
        # many drifts it found. See _r7_actuator_violations below for the new
        # NARASI_ENTITY_ATTR_ENFORCE block that fixes that. The extraction prompt is also
        # tightened to require a literal quote + explicit chapter number per drift
        # (matching the numeric-ledger "VERBATIM-as-written" fix precedent) since the
        # prior "<the two contradicting usages, chapter-tagged>" wording asked for a free-
        # text description, which _wq()-wrapping cannot turn into a locatable span.
        try:
            if _r7_env_on("NARASI_ENTITY_ATTR_CHECK"):
                _ea_fic = False
                try:
                    from pakem import resolve_style as _ea_rs
                    _eae = _ea_rs(str(body.get("style") or "")) or {}
                    _ea_fic = bool(_eae.get("is_fiction")) or str(
                        _eae.get("factual_regime") or "").strip().lower() in ("fiction", "fictional")
                except Exception:  # noqa: BLE001
                    _ea_fic = False
                _eakey = "book" if result.get("book") else "output"
                _eabk = result.get(_eakey) or ""
                if _ea_fic and _eabk:
                    from laozhang_api import _narasi_cheap_call as _eacall, _narasi_parse_json as _eaparse
                    _ea_max_chars = int(os.environ.get("NARASI_CRITIQUE_MAX_CHARS", "300000"))
                    _easys = (
                        "You are an entity-attribute continuity checker for a multi-chapter story. For "
                        "every NAMED character, track three attributes across chapters: gender pronouns "
                        "used for them, professional title/rank, and stated age. ALSO track relation "
                        "descriptors for people who are only MENTIONED in relation to a named character "
                        "and never independently named/tracked themselves — e.g. a character's daughter, "
                        "son, wife, husband, mother, father, sister, or brother. Treat each such relation "
                        "as its own tracked slot keyed by the named character plus the relation type (so "
                        "'So-ra's daughter' and 'So-ra's son' referring to the same child are the SAME "
                        "slot). Report ONLY cases where an attribute or relation descriptor CONTRADICTS "
                        "between chapters without in-story explanation (a promotion explains a title "
                        "change; a disguise explains a pronoun change; an adoption or remarriage explains "
                        "a relation change). Return ONLY JSON: {\"drifts\":[{\"name\":\"<char, or '<char>'s "
                        "<relation slot>' for a mentioned-but-unnamed relative>\",\"kind\":\"gender|title|"
                        "age|relation\",\"chapter\":<int, the LATER chapter carrying the contradicting "
                        "usage>,\"quote\":\"<short excerpt from THAT chapter, copied character-for-"
                        "character from the book text, containing the contradicting usage — never "
                        "paraphrased>\",\"expected\":\"<the earlier, canonical usage this contradicts>\"}]} "
                        "— max 6, real contradictions only.")
                    _earaw, _eacc = await _eacall(_easys, _eabk[:_ea_max_chars], tenant_id=tenant_id,
                                                  user_id=user_id, job_uuid=job_uuid, json_mode=True,
                                                  credit_row=False)
                    if sink is not None and _eacc:
                        sink.credits += int(_eacc)
                    _ead = _eaparse(_earaw) if isinstance(_earaw, str) else (_earaw or {})
                    _eadr = (_ead or {}).get("drifts") if isinstance(_ead, dict) else None
                    if isinstance(_eadr, list) and _eadr:
                        result["entity_attr_report"] = {"drifts": _eadr[:6]}
                        log.warning("entity attributes: %d drift(s): %s",
                                    len(_eadr[:6]),
                                    [f"{d.get('name')}/{d.get('kind')}" for d in _eadr[:4] if isinstance(d, dict)])
                    else:
                        log.info("entity attributes: no drift")
        except Exception as e:  # noqa: BLE001
            log.warning("entity attribute check failed (non-fatal): %s", e)

    def _r9_gate_name_uniqueness():
        # ── (0.89) NAME UNIQUENESS (NARASI_NAME_UNIQUENESS_CHECK, default OFF, NEW
        # gate, 2026-07-15): confirmed miss — a manuscript used the identical full
        # name "Yoon Hye-jin" for two unrelated characters (a council committee
        # chair, an unrelated widow) introduced in different chapters. No existing
        # gate catches this; phonetic_collision_scan (narasi_gate.py) is the nearest
        # relative and solves the OPPOSITE problem (different-but-similar names
        # assumed to be one person), so it does not overlap this flag. Deterministic
        # regex — see _name_uniqueness_scan — so unlike its two async siblings above
        # this needs no LLM call and runs synchronously rather than joining the
        # cheap-call gather. New flag (not a shared one): this is a genuinely new
        # gate, not an extension of an existing on/off-gated check. Report-only.
        # Never raises.
        try:
            if _r7_env_on("NARASI_NAME_UNIQUENESS_CHECK"):
                _nukey = "book" if result.get("book") else "output"
                _nubk = result.get(_nukey) or ""
                _nucol = _name_uniqueness_scan(_nubk)
                if _nucol:
                    result["name_uniqueness_report"] = {"collisions": _nucol}
                    log.warning("name uniqueness: %d name(s) reused across distinct characters: %s",
                                len(_nucol), [c.get("name") for c in _nucol[:4]])
                else:
                    log.info("name uniqueness: no collision")
        except Exception as e:  # noqa: BLE001
            log.warning("name uniqueness check failed (non-fatal): %s", e)

    def _r9_gate_name_order():
        # ── NAME ORDER CONSISTENCY (NARASI_NAME_ORDER_CHECK, default OFF, NEW gate,
        # 2026-07-16): a manuscript review found the same character rendered as
        # "Yuna Song" (Western given-family order) in one chapter and "Song Yuna"
        # (Korean surname-first order) in another — same two name tokens, reversed
        # order, likely the same person but currently undetected. Deterministic
        # regex — see _name_order_scan — synchronous, no LLM call. Report-only.
        # Never raises.
        try:
            if _r7_env_on("NARASI_NAME_ORDER_CHECK"):
                _nokey = "book" if result.get("book") else "output"
                _nobk = result.get(_nokey) or ""
                _noinc = _name_order_scan(_nobk)
                if _noinc:
                    result["name_order_report"] = {"inconsistencies": _noinc}
                    log.warning("name order: %d name(s) rendered in both orders: %s",
                                len(_noinc), [i.get("tokens") for i in _noinc[:4]])
                else:
                    log.info("name order: no inconsistency")
        except Exception as e:  # noqa: BLE001
            log.warning("name order check failed (non-fatal): %s", e)

    def _r9_gate_name_typo():
        # ── NAME TYPO / NEAR-MISS (NARASI_NAME_TYPO_CHECK, default OFF, NEW gate,
        # 2026-07-16): a manuscript review found a chore-list line "Call Seo-ra's
        # clinic" where the established character name elsewhere in the same
        # manuscript is "So-ra" — a one-letter typo, and also confusingly one
        # letter off from an unrelated protagonist name ("Seo-an"), a real
        # reader-confusion risk. Deterministic — see _name_typo_scan — synchronous,
        # no LLM call. Report-only. Never raises.
        try:
            if _r7_env_on("NARASI_NAME_TYPO_CHECK"):
                _ntkey = "book" if result.get("book") else "output"
                _ntbk = result.get(_ntkey) or ""
                _nttyp = _name_typo_scan(_ntbk)
                if _nttyp:
                    result["name_typo_report"] = {"typos": _nttyp}
                    log.warning("name typo: %d likely typo(s): %s",
                                len(_nttyp), [t.get("typo") for t in _nttyp[:4]])
                else:
                    log.info("name typo: no likely typo")
        except Exception as e:  # noqa: BLE001
            log.warning("name typo check failed (non-fatal): %s", e)

    # ROUND-9 (speed): the three cheap-call gates are independent — run them
    # CONCURRENTLY instead of serially (sum→max: ~60-110s → ~50s when all on).
    # Each keeps its own flag check and its own never-raise try inside.
    await asyncio.gather(_r9_gate_domain(), _r9_gate_numeric(), _r9_gate_entity())
    # Deterministic, no LLM call — runs synchronously right after (not folded into
    # the gather above, which exists specifically to overlap LLM latency).
    _r9_gate_name_uniqueness()
    _r9_gate_name_order()
    _r9_gate_name_typo()


    # ── (1) terminal deterministic gate (localized per §2/§3) ──
    # Phase 3 (2026-07-05): pass `style` through so gate_text's per-style R-FG counters
    # (M threshold table, J source-note density, LL factual-ending, HH human-anchor,
    # IIIIII entity-consistency) can look up per-style thresholds. Backward-compatible:
    # gate_text falls back to genre-agnostic defaults when style is None or unknown.
    gate_report: dict = {}
    try:
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        if book:
            gated, gate_report = _ngate.gate_text(book, lang=language, mode=_mode, style=style)
            result[key] = gated
        for rec in result.get("chapters") or []:
            if rec.get("content"):
                rec["content"], _r = _ngate.gate_text(rec["content"], lang=language, mode=_mode, style=style)
        result["gate_report"] = gate_report
        # Observability fix (2026-07-16): unattributed_voice_scan (and every other
        # gate_text()-internal scanner) only ever populates stats[...] — narasi_gate.py
        # has zero logging of its own by design (pure scanner, caller decides what to
        # log). Every OTHER new gate this round (name_uniqueness/order/typo, entity
        # attrs) logs its own outcome at its narration_api.py call site; this one
        # didn't, so a job with NARASI_UNATTRIBUTED_VOICE_SCAN=1 genuinely on gave no
        # way to tell from Railway logs whether it ran or found anything — confirmed
        # via a live job today where the flag was reportedly on the whole time but no
        # log line ever appeared. Log it the same way its narration_api.py-native
        # siblings do; no behavior change, pure visibility.
        _uv_flags = (gate_report or {}).get("flags") or {}
        if _uv_flags.get("unattributed_voice_flag"):
            log.warning("unattributed_voice: %d hit(s): %s",
                        _uv_flags.get("unattributed_voice_hits") or 0,
                        (_uv_flags.get("unattributed_voice_samples") or [])[:2])
        elif "unattributed_voice_hits" in _uv_flags:
            log.info("unattributed_voice: no hits")
    except Exception as e:  # noqa: BLE001
        log.warning("v3 terminal gate failed (non-fatal): %s", e)

    # ══════════════════════════════════════════════════════════════════════════════════
    # (1.5)-(2.76) FOUR GATE MECHANISMS — critic, register-gate, canon-diff, thread-tracker.
    # PERF REFACTOR (round-?): each mechanism's DETECTION (scan/audit call(s) + its own
    # reveal-protection/classify step + its own enforce-flag kill-switch, producing a final
    # eligible-for-revise violation list that may be empty) used to run as four sequential
    # top-to-bottom `await`s, each re-fetching result.get("book") fresh at its own point in
    # the file — which meant each stage incidentally saw the PRECEDING stage's edit. That
    # was an artifact of sequential file order, not a real data dependency: all four
    # detections are pure/stateless LLM scans over a text snapshot; none reads or writes
    # another mechanism's state (confirmed by inspection — same conclusion the three
    # cheap-call gates fanned out via asyncio.gather at ~line 2065 above already rely on).
    # Detection now runs CONCURRENTLY against ONE shared book snapshot taken once, below,
    # before any of the four starts. Each mechanism's own trigger condition (env flag,
    # chapter-count guard, style/register_spec presence, registry/thread extraction success)
    # is still evaluated first inside its own coroutine — a mechanism whose condition is
    # false contributes nothing, exactly like today's "if X_on: ..." skip.
    #
    # After all four detections (+ their own classify steps) finish, each mechanism's final
    # eligible violations are concatenated into ONE list and fed to exactly ONE revise call
    # against the shared snapshot — never 2+ concurrent revises against the same book text
    # (that would be a last-write-wins race on result[key]); if every mechanism ends up with
    # zero eligible violations (the common case), the revise call is skipped entirely, same
    # as each mechanism already does today when ITS OWN violation list is empty. Each
    # mechanism's enforce-flag kill-switch is evaluated BEFORE concatenation — an OFF
    # mechanism contributes zero violations to the merged list even though its
    # detection/scan still ran and still logs its own report-only findings exactly as today.
    #
    # DELIBERATE, DOCUMENTED trade-off of collapsing four call-sites into one revise call:
    # today canon-diff's NARASI_CANON_ENFORCE_NONREVEAL path and the thread-tracker enforce
    # path call laozhang_api._narasi_revise_chunked DIRECTLY — phase="revise" (default),
    # model=body.model-or-"claude-opus-4-6", UNCONDITIONALLY chunked, no whole-book fallback
    # if the chunk split itself fails — while critic, register-gate, and canon-diff's
    # NARASI_CANON_DIFF_REVISE path all go through the _narasi_consistency_revise DISPATCHER
    # (phase="canon_diff_revise" always; model=NARASI_CRITIQUE_MODEL-or-body.model-or-
    # DALANG_CHEAP_MODEL; chunked only when NARASI_REVISE_CHUNKED / the auto-chunk word
    # threshold says so, else whole-book, with a chunked-raises→whole-book fallback net).
    # The single merged call below goes through that same dispatcher — the majority
    # convention (3 of 4 mechanisms already use it), so THEIR routing/model-selection/
    # fallback-safety is byte-for-byte unchanged. The two mechanisms whose revise dispatch
    # convention changes are canon-diff's NONREVEAL path and thread-tracker: their revise
    # now rides phase="canon_diff_revise" and NARASI_CRITIQUE_MODEL's override precedence,
    # and is chunked only when NARASI_REVISE_CHUNKED/the auto-word threshold is configured
    # (today they force chunked unconditionally, with no whole-book fallback net — the
    # dispatcher's fallback is a strict robustness improvement for them). This is the one
    # unavoidable consequence of "exactly one call" over four previously-disagreeing call-
    # sites; if NARASI_CANON_ENFORCE_NONREVEAL / NARASI_THREAD_TRACKER_ENFORCE are used in
    # prod, verify NARASI_REVISE_CHUNKED / NARASI_CRITIQUE_MODEL / any phase-scoped provider
    # override (NARASI_REVISE_* vs NARASI_CANON_DIFF_REVISE_*-shaped switchboard flags) are
    # what you expect before relying on identical canon-diff/thread-tracker behavior.
    #
    # Each mechanism's "did MY fix land" bookkeeping (the `revised` flag / "book updated" vs
    # "revise landed nothing" log) can now only observe whether the ONE merged revise
    # changed the shared book at all — there is a single before/after diff now, not four —
    # not whether ITS OWN violations specifically were the ones addressed. Every mechanism
    # that contributed >=1 eligible violation reports that same shared "book changed?"
    # outcome. Credit/cost accounting: each mechanism's own DETECTION/classify LLM calls
    # still meter into `sink` individually, exactly as today; the ONE merged revise call's
    # cost is metered once into `sink` (the billed running total is exactly as correct as
    # before — it is simply no longer broken out per mechanism, which was never separately
    # itemized downstream anyway).
    # ══════════════════════════════════════════════════════════════════════════════════
    _gk = "book" if result.get("book") else "output"
    _gbook0 = result.get(_gk) or ""

    async def _v3g_critic_detect():
        # ── (1.5) #53 whole-draft CONSISTENCY critic for the VIDEO/orchestrator path. The
        # critic lives in laozhang_api._narasi_generate_impl, but Output=video narasi runs
        # through the orchestrator and never hit that path — so wire the SAME critic here.
        # Reads the FULL gated book (no truncation), object-provenance/timeline/causality/
        # entity/spatial/POV checklist. Gated NARASI_CRITIQUE_ENABLED (report-only) +
        # NARASI_CRITIQUE_REVISE (contributes to the merged revise below). OFF ⟹ inert. ──
        _out = {"ran": False, "eligible": []}
        try:
            from laozhang_api import (_narasi_critique_enabled, _narasi_critique_revise_enabled,
                                      _narasi_consistency_critique,
                                      NARASI_CRITIQUE_MIN_CHAPTERS)
            _cbk = _gbook0
            _nch = (len(result.get("chapters") or [])
                    or len(body.get("chapters") or [])
                    or _cbk.count("\n## "))
            _crit_on = _narasi_critique_enabled()
            if _crit_on and _cbk and _nch >= NARASI_CRITIQUE_MIN_CHAPTERS:
                _cmodel = (body.get("model") or "")
                _t_crit0 = time.monotonic()
                _cq, _cqc = await _narasi_consistency_critique(
                    _cbk, style, language, model=_cmodel,
                    tenant_id=tenant_id, user_id=user_id, job_uuid=job_uuid,
                    canonical_facts=(result.get("canonical_facts") or ""), credit_row=False)
                _t_crit = time.monotonic() - _t_crit0
                # P0A: hand the ALREADY-elapsed critic timer to the caller. A plain dict
                # store — no await, no I/O, no new timer inside the gate phase, and the
                # dict is None entirely when the flag is off.
                if p0a_timings is not None:
                    p0a_timings["critic"] = _t_crit
                if sink is not None and _cqc:
                    sink.credits += int(_cqc)
                _cpay = _cq
                try:
                    log.info("critic findings preview: %s",
                             [f"{(v.get('type') or '?')}:{str(v.get('evidence') or v.get('description') or '')[:60]}"
                              for v in (_cq.get("violations") or [])[:12]])
                except Exception:  # noqa: BLE001
                    pass
                # ── LEDGER/TIMELINE ENFORCEMENT (NARASI_LEDGER_ENFORCE, default OFF) ──
                if os.environ.get("NARASI_LEDGER_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on"):
                    try:
                        _mech: list[dict] = []
                        _mctrs = (result.get("counter_report") or {}).get("counters") or {}
                        import re as _mre
                        _mtopic = str(body.get("topic") or body.get("goal") or body.get("brief") or "")
                        _mvdem = os.environ.get("NARASI_LEDGER_VALUE_DEMOTE", "0").strip().lower() in ("1", "true", "yes", "on")
                        for h in ((_mctrs.get("ledger_hits") or {}).get("hits") or [])[:8]:
                            if h.get("where") != "manuscript":
                                continue
                            if _mvdem and str(h.get("term") or "").startswith("floor:"):
                                continue
                            _mterm = str(h.get("term") or "").split(":", 1)[-1]
                            if _mterm and _premise_term_in_topic(_mterm, _mtopic):
                                continue
                            _mech.append({
                                "type": "ledger_hit", "severity": "high",
                                "evidence": str(h.get("snippet") or _mterm)[:200],
                                "fix": f"Replace the lane-overused item «{_mterm}» with a fresh, "
                                       f"premise-specific choice (previous stories in this lane already "
                                       f"used it); keep the sentence's meaning and rhythm."})
                        _gflags = gate_report.get("flags") or {}
                        for _irs in (_gflags.get("instruction_residue_samples") or [])[:6]:
                            _mech.append({
                                "type": "instruction_residue", "severity": "high",
                                "evidence": str(_irs)[:200],
                                "fix": ("This is an unresolved template slot/instruction that leaked "
                                        "into prose (a hedge word like 'around' standing in for a value "
                                        "the generator never filled in). Replace it with the actual, "
                                        "specific value the story establishes; remove the hedge wording "
                                        "entirely.")})
                        for _pls in (_gflags.get("placeholder_leak_samples") or [])[:6]:
                            _mech.append({
                                "type": "placeholder_leak", "severity": "high",
                                "evidence": str(_pls)[:200],
                                "fix": ("This is an unresolved descriptive-slot hedge ('sekitar/kira-"
                                        "kira' + an unfilled instruction) that leaked into prose. "
                                        "Replace it with the actual, specific value the story "
                                        "establishes; remove the hedge wording entirely.")})
                        for f in ((_mctrs.get("timeline_arith") or {}).get("findings") or [])[:4]:
                            if (f.get("kind") == "span_alternation"
                                    and _r7_env_on("NARASI_NUMERIC_LEDGER")):
                                continue
                            _mech.append({
                                "type": "timeline_arithmetic", "severity": "high",
                                "evidence": str(f.get("context") or f.get("note") or "")[:200],
                                "fix": (f"Unify the alternating duration — {f.get('note')}"
                                        if f.get("kind") == "span_alternation" else
                                        f"Correct the stated span to match the dated events — {f.get('note')}")})
                        if _r7_env_on("NARASI_NUMERIC_MAGNITUDE"):
                            for f in ((_mctrs.get("numeric_magnitude") or {}).get("magnitude") or [])[:2] + ((_mctrs.get("numeric_magnitude") or {}).get("year_forks") or [])[:2]:
                                _mech.append({"type": "numeric_magnitude", "severity": "high",
                                    "evidence": str(f.get("note") or "")[:200],
                                    "fix": (f"Numeric scale/date inconsistency: {f.get('note')}. Reconcile the "
                                            f"figures so per-item × count matches the stated total (or correct "
                                            f"the total), and pin the referent to ONE year everywhere.")})
                        if _r7_env_on("NARASI_AGE_LEDGER"):
                            for f in ((_mctrs.get("age_ledger") or {}).get("findings") or [])[:4]:
                                _mech.append({"type": "age_ledger", "severity": "high",
                                    "evidence": str(f.get("note") or "")[:200],
                                    "fix": (f"{f.get('note')}. Unify to the single value the story's dated facts "
                                            f"support at EVERY mention; do not touch a deliberate official-vs-true contrast.")})
                        if _r7_env_on("NARASI_ALIAS_LEDGER"):
                            for f in ((_mctrs.get("alias_ledger") or {}).get("misfiled") or [])[:3]:
                                _mech.append({"type": "alias_legal_lock", "severity": "high",
                                    "evidence": str(f.get("context") or f.get("note") or "")[:200],
                                    "fix": (f"{f.get('note')}. On a system-generated document (envelope, registry, "
                                            f"court file) use the character's LEGAL/registered name «{f.get('legal')}» at "
                                            f"EVERY such mention; reserve the alias for informal in-world speech, and never "
                                            f"label the legal name as the 'working name'.")})
                        if _r7_env_on("NARASI_CANON_ANCHOR"):
                            for f in ((_mctrs.get("canon_anchor") or {}).get("findings") or [])[:3]:
                                _mech.append({"type": "canon_anchor", "severity": "high",
                                    "evidence": str(f.get("note") or "")[:200],
                                    "fix": (f"{f.get('note')}. Anchor this event to ONE absolute date at EVERY "
                                            f"mention" + (f" — the canonical date is {f.get('canon')}."
                                                           if f.get("canon") else "."))})
                        if _r7_env_on("NARASI_PLACEHOLDER_SCAN"):
                            for h in ((_mctrs.get("placeholder") or {}).get("hits") or [])[:4]:
                                _mech.append({"type": "placeholder_token", "severity": "high",
                                    "evidence": str(h.get("snippet") or h.get("token"))[:200],
                                    "fix": (f"Replace the unsubstituted template token «{h.get('token')}» with "
                                            f"the actual value the story establishes (or the correct current "
                                            f"in-story date/name).")})
                        if _r7_env_on("NARASI_ENTITY_QTY"):
                            for f in ((_mctrs.get("entity_qty") or {}).get("forks") or [])[:3]:
                                _mech.append({"type": "entity_quantity", "severity": "high",
                                    "evidence": str(f.get("note") or "")[:200],
                                    "fix": (f"{f.get('note')}. Pin ONE count for this entity and use it at "
                                            f"every mention; if a larger figure is intentional (a broader total "
                                            f"vs a specific sub-list), name that distinction explicitly in the prose.")})
                        if _r7_env_on("NARASI_KINSHIP_SCAN"):
                            for f in ((_mctrs.get("kinship") or {}).get("mismatches") or [])[:3]:
                                _mech.append({"type": "kinship_mismatch", "severity": "high",
                                    "evidence": str(f.get("context") or f.get("note") or "")[:200],
                                    "fix": (f"{f.get('note')}. Use the consistent side (maternal/paternal) "
                                            f"established elsewhere in the manuscript for this relationship "
                                            f"at EVERY mention.")})
                        if os.environ.get("NARASI_STYLE_COUNTER_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on"):
                            _over = set(rep.get("over_budget") or [])
                            _rawctrs = rep.get("counters") or {}
                            for _ckey2 in ("epithet", "anchors", "aphorisms", "anchor_voice"):
                                if _ckey2 not in _over:
                                    continue
                                _cdat = _rawctrs.get(_ckey2) or {}
                                for _sent in (_cdat.get("sentences") or [])[:3]:
                                    _mech.append({"type": f"style_counter_{_ckey2}", "severity": "high",
                                        "evidence": str(_sent)[:200],
                                        "fix": (f"This line contributes to the manuscript exceeding its "
                                                f"'{_ckey2}' budget ({_cdat.get('count')}/{_cdat.get('budget', '?')}). "
                                                f"Rewrite it to remove the repeated device while preserving "
                                                f"the sentence's meaning.")})
                            if "reglossing" in _over:
                                _rg = _rawctrs.get("reglossing") or {}
                                for _term, _cnt in list((_rg.get("terms") or {}).items())[:3]:
                                    _mech.append({"type": "style_counter_reglossing", "severity": "high",
                                        "evidence": f"term «{_term}» re-glossed {_cnt}× (budget {_rg.get('budget', '?')})",
                                        "fix": (f"The term «{_term}» is re-defined with an em-dash gloss "
                                                f"{_cnt} times. Define it ONCE on first use; every later "
                                                f"mention should use the bare term with no re-gloss.")})
                        if os.environ.get("NARASI_BRAND_REPORT", "0").strip().lower() in ("1", "true", "yes", "on"):
                            try:
                                import narasi_counters as _brc
                                import re as _bre
                                _brbook = result.get("book") or result.get("output") or ""
                                _brent = []
                                for _brand in list(_brc._REAL_BRANDS_LONG):
                                    _brx = _bre.compile(r"\b" + _bre.escape(_brand) + r"\b")
                                    _bm = _brx.search(_brbook)
                                    if not _bm or _brc._brand_role(_brbook, _brx) == "culpable":
                                        continue
                                    if _brc._brand_entity_use(_brbook, _brx):
                                        _brent.append(_brand)
                                        if len(_brent) <= 6:
                                            _snip = _bre.sub(r"\s+", " ", _brbook[max(0, _bm.start() - 40):_bm.end() + 80])
                                            _mech.append({"type": "real_brand_entity", "severity": "low",
                                                "evidence": (_wq(_snip) or _wq(_brand))[:200],
                                                "fix": (f"The real company «{_brand}» is used as an in-story entity "
                                                        f"(firm/client/employer). Consider renaming to a clearly "
                                                        f"fictional company to avoid brand/legal risk; nominative prop use is fine.")})
                                if _brent:
                                    log.warning("REAL-BRAND entity-use (report-only): %d non-culpable in-story entity hit(s): %s", len(_brent), _brent[:5])
                            except Exception:  # noqa: BLE001
                                pass
                        _mech.extend(_r7_actuator_violations(result))
                        if os.environ.get("NARASI_METALEAK_SCAN", "0").strip().lower() in ("1", "true", "yes", "on"):
                            try:
                                import narasi_counters as _mlc
                                _mlk = "book" if result.get("book") else "output"
                                _mlr = _mlc.meta_reference_scan(result.get(_mlk) or "")
                                for _h in (_mlr.get("hits") or [])[:3]:
                                    _mech.append({
                                        "type": "meta_leak", "severity": "high",
                                        "evidence": _wq(_h.get("snippet"))[:200],
                                        "fix": ("This line contains an out-of-world reference to a chapter/"
                                                "episode number ('Ch9', 'chapter 5', 'episode 3') -- a "
                                                "generator artifact. Rewrite the sentence to remove the "
                                                "chapter reference entirely; a character never names the "
                                                "story's own chapters. Keep the surrounding meaning.")})
                                if _mlr.get("count"):
                                    log.warning("meta-leak: %d out-of-world chapter reference(s) in prose", _mlr["count"])
                            except Exception:  # noqa: BLE001
                                pass
                        if os.environ.get("NARASI_PROVENANCE_LEAK", "0").strip().lower() in ("1", "true", "yes", "on"):
                            try:
                                import narasi_counters as _plc
                                _plk = "book" if result.get("book") else "output"
                                _plr = _plc.provenance_leak_scan(result.get(_plk) or "")
                                for _h in (_plr.get("hits") or [])[:3]:
                                    _mech.append({"type": "provenance_leak", "severity": "high",
                                        "evidence": _wq(_h.get("snippet"))[:200],
                                        "fix": ("This line cites the story's own scaffolding (story bible / "
                                                "outline / canon / fact-sheet) — a generator artifact, not "
                                                "in-world text. Delete the citation phrase and state the fact "
                                                "plainly; a character never references the story bible.")})
                                if _plr.get("count"):
                                    log.warning("provenance-leak: %d scaffolding citation(s) in prose", _plr["count"])
                            except Exception:  # noqa: BLE001
                                pass
                        if _mech:
                            _cq["violations"] = (_mech + list(_cq.get("violations") or []))[:20]
                            log.info("ledger-enforce: injected %d mechanical violation(s) into critique/revise",
                                     len(_mech))
                    except Exception as _mie:  # noqa: BLE001
                        log.warning("ledger-enforce injection failed (non-fatal): %s", _mie)
                # ── REAL-BRAND ENFORCEMENT (NARASI_BRAND_ENFORCE, default OFF) — checked
                # independently of NARASI_LEDGER_ENFORCE. Previously this lived nested inside
                # the ledger-enforce block above, so a real-brand hit could only reach revise
                # when NARASI_LEDGER_ENFORCE was ALSO on (a different, unrelated flag) — and
                # even then only "culpable"-role hits qualified, silently dropping ordinary
                # in-story-entity mentions (e.g. "Daesung employed him") that the REAL-BRAND
                # scan's own log already calls out as the legal-risk class to police, because
                # _brand_role()'s ±150-char proximity check often misses a brand that's clearly
                # the story's antagonist company but not always textually adjacent to liability
                # vocabulary. Detection (real_brands scan, role classification) is unchanged;
                # only the enforcement gating moved. FIRST FIX ATTEMPT this round dropped the
                # role filter entirely, which over-corrected: it started forcing renames on
                # harmless background PROP mentions (e.g. "a dented grey Hyundai") that the
                # original design explicitly meant to leave as WARN-only, and on FUZZY hits
                # (edit-distance-1 near-misses to a blocklisted name, not a confirmed real
                # brand — e.g. "Hanshin" flagged only because it's 1 edit from "Hanjin"),
                # which would falsely instruct a rename of a name that may not even be a real
                # brand. Restored selectivity: qualify on role=="culpable" (the original signal)
                # OR on a high manuscript-wide mention COUNT (>=5 — a one-off background prop
                # realistically isn't repeated that often, but a central antagonist company
                # mentioned dozens of times, e.g. the real "Daesung 72x" defect, clearly is),
                # and always exclude fuzzy (unconfirmed) hits from forced enforcement.
                if os.environ.get("NARASI_BRAND_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on"):
                    try:
                        _brmctrs = (result.get("counter_report") or {}).get("counters") or {}
                        _brmech: list[dict] = []
                        for h in ((_brmctrs.get("real_brands") or {}).get("hits") or [])[:3]:
                            if (h.get("where") == "manuscript" and not h.get("fuzzy")
                                    and (h.get("role") == "culpable" or int(h.get("count") or 0) >= 5)):
                                _brmech.append({
                                    "type": "real_brand", "severity": "high",
                                    "evidence": str(h.get("snippet") or h.get("brand"))[:200],
                                    "fix": (f"The real-world company «{h.get('brand')}» is used as a "
                                            f"real conglomerate in the story — legal risk. Rename it to a "
                                            f"clearly fictional company (phonetically distinct from any "
                                            f"real conglomerate) at EVERY occurrence, keeping scene content intact.")})
                        if _brmech:
                            _cq["violations"] = (_brmech + list(_cq.get("violations") or []))[:20]
                            log.info("brand-enforce: injected %d real-brand violation(s) into critique/revise",
                                     len(_brmech))
                    except Exception as _bre2:  # noqa: BLE001
                        log.warning("brand-enforce injection failed (non-fatal): %s", _bre2)
                # ── LANGUAGE-CONSISTENCY ENFORCEMENT (NARASI_LANGUAGE_CONSISTENCY_ENFORCE,
                # default OFF) — checked independently, self-contained (recomputes the word
                # scan itself, same as the metaleak/provenance-leak mechanisms below, so this
                # works whether or not NARASI_LANGUAGE_CONSISTENCY_SCAN's report-only pass
                # above ran). Detection is the same narasi_counters.language_consistency_
                # word_scan used report-only above; this only adds the enforcement wiring.
                if os.environ.get("NARASI_LANGUAGE_CONSISTENCY_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on"):
                    try:
                        import narasi_counters as _lce
                        _lcekey = "book" if result.get("book") else "output"
                        _lcebk = result.get(_lcekey) or ""
                        _lcemech: list[dict] = []
                        if _lcebk and hasattr(_lce, "language_consistency_word_scan"):
                            _lcerep = _lce.language_consistency_word_scan(_lcebk, language)
                            for h in (_lcerep.get("samples") or [])[:5]:
                                _lcemech.append({
                                    "type": "language_consistency", "severity": "high",
                                    "evidence": str(h.get("snippet") or h.get("term"))[:200],
                                    "fix": (f"The Indonesian word «{h.get('term')}» leaked into this "
                                            f"{language}-language manuscript — a language-consistency "
                                            f"slip. Rewrite it in {language}, keeping the sentence's "
                                            f"meaning and rhythm intact.")})
                        if _lcemech:
                            _cq["violations"] = (_lcemech + list(_cq.get("violations") or []))[:20]
                            log.info("language-consistency-enforce: injected %d violation(s) into critique/revise",
                                     len(_lcemech))
                    except Exception as _lcee:  # noqa: BLE001
                        log.warning("language-consistency-enforce injection failed (non-fatal): %s", _lcee)
                # CANON_FORK-REVISE SAFETY (see original docstring above this block, unchanged).
                try:
                    from orchestrator.static import _is_fiction_style as _isf_rev
                    _isfic_rev = bool(_isf_rev(style))
                except Exception:  # noqa: BLE001
                    _isfic_rev = True
                _fork_revise = os.environ.get("NARASI_CANON_FORK_REVISE", "0").strip().lower() in ("1", "true", "yes", "on")
                _revisable_fork_ev = set()
                if (_isfic_rev and not _fork_revise
                        and os.environ.get("NARASI_CANON_FORK_CLASSIFY", "0").strip().lower() in ("1", "true", "yes", "on")):
                    try:
                        _cf_list = [v for v in (_cq.get("violations") or [])
                                    if str(v.get("type", "")).lower() == "canon_fork"][:12]
                        if _cf_list:
                            _cf_widen = os.environ.get("NARASI_CANON_FORK_CLASSIFY_CONTEXT", "0").strip().lower() in (
                                "1", "true", "yes", "on")
                            _cf_chapters = (result.get("chapters") or []) if _cf_widen else []
                            _cf_items = []
                            for _v in _cf_list:
                                _ev = str(_v.get("evidence") or "")
                                _exc = None
                                if _cf_widen:
                                    _exc, _ = _canon_excerpt(_cf_chapters, quote_source=_ev)
                                _cf_items.append({"signal": _ev[:200], "excerpt": _exc})
                            _cf_continuity = await _narasi_classify_canon_items(
                                _cf_items, tenant_id=tenant_id, user_id=user_id, job_uuid=job_uuid, sink=sink,
                                widen_prompt=_cf_widen)
                            for _idx in _cf_continuity:
                                _revisable_fork_ev.add(str(_cf_list[_idx].get("evidence") or ""))
                            if _revisable_fork_ev:
                                log.info("canon-fork classify: %d/%d fork(s) are plain continuity errors -> revisable",
                                         len(_revisable_fork_ev), len(_cf_list))
                    except Exception as _cfe:  # noqa: BLE001
                        log.warning("canon-fork classify failed (non-fatal, all forks stay protected): %s", _cfe)

                def _fork_protected(_v) -> bool:
                    return (str(_v.get("type", "")).lower() == "canon_fork"
                            and (not _fork_revise or not _isfic_rev)
                            and str(_v.get("evidence") or "") not in _revisable_fork_ev)

                _cbad = [v for v in (_cq.get("violations") or [])
                         if str(v.get("severity", "")).lower() in ("critical", "high")
                         and not _fork_protected(v)]
                _eligible = []
                if _narasi_critique_revise_enabled() and _cbad:
                    import re
                    _cq_rev = dict(_cq)
                    _cq_rev["violations"] = [
                        v for v in (_cq.get("violations") or [])
                        if not _fork_protected(v)
                        and not any(t in str(v.get("fix", "")).lower()[:60]
                                    for t in ("retracted", "no change needed", "no fix needed"))
                        and not re.search(r"(?i)\b(dramatiz|insert (a|the|one)? ?scene|add (a|the) scene|"
                                          r"new scene|could fit at the opening)",
                                          str(v.get("fix", "")))]
                    if len(_cq_rev["violations"]) != len(_cq.get("violations") or []):
                        log.info("revise input filtered: %d → %d violation(s) (canon_fork report-only / retracted dropped)",
                                 len(_cq.get("violations") or []), len(_cq_rev["violations"]))
                    _eligible = _cq_rev["violations"]
                _out.update({"ran": True, "cq": _cq, "cpay": _cpay, "t_crit": _t_crit, "cmodel": _cmodel,
                             "nch": _nch, "crit_on": _crit_on, "cbk": _cbk, "eligible": _eligible})
            else:
                log.info("narration job %s: consistency critic SKIPPED "
                         "(enabled=%s, chapters=%s, min=%s, book_chars=%s)",
                         job_id, _crit_on, _nch, NARASI_CRITIQUE_MIN_CHAPTERS, len(_cbk))
        except Exception as e:  # noqa: BLE001
            log.warning("consistency critic (video path) failed (non-fatal): %s", e)
        return _out

    def _v3g_critic_finalize(_out, _changed, _t_rev):
        if not _out.get("ran"):
            return
        try:
            from laozhang_api import NARASI_CRITIQUE_MODEL
        except Exception:  # noqa: BLE001
            NARASI_CRITIQUE_MODEL = ""
        _cq = _out["cq"]
        _cpay = _out["cpay"]
        if _out.get("eligible") and _changed:
            _cpay = dict(_cq)
            _cpay["revised"] = True
        result["critique"] = _cpay
        log.info("narration job %s: consistency critic RAN — score=%s, %d violation(s)%s "
                 "[critic=%.1fs revise=%.1fs model=%s]",
                 job_id, _cq.get("score"), len(_cq.get("violations") or []),
                 " -> REVISED" if _cpay.get("revised") else " (report-only)",
                 _out["t_crit"], _t_rev if _out.get("eligible") else 0.0,
                 (NARASI_CRITIQUE_MODEL or _out["cmodel"] or "cheap"))

    async def _v3g_register_detect():
        # ── (2) R-H10 register scorecard — entry-driven (any style with a register_spec in
        # the pakem registry), report-only, one cheap call. Deterministic half = banned-
        # tells substring scan; LLM half = counting the style's required moves. ──
        _out = {"ran": False, "eligible": []}
        try:
            if str(os.environ.get("NARASI_REGISTER_GATE", "1")).strip().lower() not in ("0", "false", "no", "off"):
                spec = None
                style_key = style
                try:
                    from pakem import resolve_style, resolve_style_key
                    entry = resolve_style(style)
                    spec = entry.get("register_spec")
                    style_key = resolve_style_key(style) or style
                except Exception:  # noqa: BLE001
                    spec = None
                if spec and (spec.get("required_moves") or spec.get("banned_tells")):
                    book = _gbook0
                    low = book.lower()
                    banned = [t for t in (spec.get("banned_tells") or []) if t and t.lower() in low]
                    moves = list(spec.get("required_moves") or [])
                    # FIX (register-gate truncation root-cause): several required_moves are literally
                    # named "*_per_chapter" (a multi-chapter arc/beat), but the LLM count below only
                    # ever saw book[:12000] when this flag was off -- for a full-length manuscript
                    # that's chapter 1 plus a sliver of chapter 2, so any move anchored later in the
                    # book scored a false 0 (false off_register). The head+middle+tail sample a few
                    # lines down was already built to fix exactly this and costs the SAME ~12,000
                    # chars (just better distributed, not more expensive) -- it was just left opt-in.
                    # Default is now ON; NARASI_REGISTER_GATE_FAILOPEN=0 restores the old head-only scan.
                    _rg_failopen = str(os.environ.get("NARASI_REGISTER_GATE_FAILOPEN", "1")).strip().lower() in ("1", "true", "yes", "on")
                    counts: dict = {}
                    if moves and book:
                        try:
                            from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
                            _sys = ("You are a strict register auditor. For the declared style, count how many times "
                                    "each REQUIRED MOVE genuinely occurs in the text (a real, executed instance — not a "
                                    "faint echo). Moves: " + ", ".join(moves) + ". "
                                    "Return ONLY JSON mapping each move name to an integer count.")
                            _rg_text = (book or "")[:12000]
                            if _rg_failopen and len(book or "") > 12000:
                                _rg_n = len(book)
                                _rg_text = (book[:6000] + "\n[...]\n"
                                            + book[_rg_n // 2 - 1500:_rg_n // 2 + 1500]
                                            + "\n[...]\n" + book[-3000:])
                            for _rg_attempt in (0, 1):
                                raw, _cr = await _narasi_cheap_call(_sys, _rg_text,
                                                                    tenant_id=tenant_id, user_id=user_id,
                                                                    job_uuid=job_uuid, json_mode=True)
                                d = _narasi_parse_json(raw) if isinstance(raw, str) else (raw or {})
                                if not (isinstance(d, dict) and d):
                                    log.warning("register-gate cheap scan attempt %d unparsable — raw head: %r",
                                                _rg_attempt, (raw or "")[:200])
                                # FIX (register-gate retry dead-code): the `or not _rg_failopen` on both
                                # lines below made the retry unreachable whenever failopen was off (the
                                # then-default) -- both conditions collapsed to unconditionally-True, so
                                # the loop broke after attempt 0 no matter what the model returned, and an
                                # empty/unparsable response silently became all-zero counts. Retry now
                                # fires on ANY empty/unparsable attempt 0, regardless of _rg_failopen;
                                # _rg_failopen still only governs the SAMPLING strategy above and the
                                # inconclusive-vs-off_register verdict below.
                                if isinstance(d, dict) and d:
                                    counts = {m: int(d.get(m) or 0) for m in moves}
                                if counts:
                                    break
                                if _rg_attempt == 0:
                                    log.info("register-gate LLM scan returned empty/unparsable move-counts — retrying once")
                        except Exception as e:  # noqa: BLE001
                            log.warning("register-gate LLM scan failed (non-fatal): %s", e)
                    on_register = (not banned) and all(counts.get(m, 0) >= 1 for m in moves) if counts or not moves else False
                    verdict = "on_register" if on_register else "off_register"
                    if _rg_failopen and moves and not counts and not banned:
                        verdict = "inconclusive"
                        log.info("register-gate scan inconclusive for %s (empty LLM move-counts after retry) — skipping off_register flag", style_key)
                    result["register_gate"] = {
                        "style": style_key, "moves": counts,
                        "banned_tells": banned, "verdict": verdict,
                    }
                    _eligible = []
                    if verdict == "off_register":
                        log.warning("register-gate: manuscript flagged off_register for %s (moves=%s banned=%s)",
                                    style_key, counts, banned)
                        if str(os.environ.get("NARASI_REGISTER_GATE_ENFORCE", "0")).strip().lower() in ("1", "true", "yes", "on"):
                            try:
                                _rv = [{"type": "register", "severity": "high",
                                        "evidence": f"required move not executed: {m}",
                                        "fix": f"execute the '{m}' move at least once in the book"}
                                       for m in moves if counts.get(m, 0) < 1]

                                def _bp_ev(t):
                                    _bidx = low.find(t.lower())
                                    _rc = book[_bidx:_bidx + len(t)] if _bidx >= 0 else ""
                                    return _wq(_rc) if _rc and _rc.lower() == t.lower() else f"banned phrasing present: {t}"
                                _rv += [{"type": "register", "severity": "high",
                                         "evidence": _bp_ev(t),
                                         "fix": f"remove the banned phrasing '{t}'"} for t in banned]
                                if _rv and book:
                                    _eligible = _rv
                            except Exception as _e:  # noqa: BLE001
                                log.warning("register-gate enforce build failed (non-fatal): %s", _e)
                    _out.update({"ran": True, "eligible": _eligible})
        except Exception as e:  # noqa: BLE001
            log.warning("register gate failed (non-fatal): %s", e)
        return _out

    def _v3g_register_finalize(_out, _changed):
        if _out.get("ran") and _out.get("eligible") and _changed:
            try:
                result["register_gate"]["revised"] = True
            except Exception:  # noqa: BLE001
                pass

    async def _v3g_canon_diff_detect():
        # ── (2.75) CANON DIFF (Phase 2b) — diff each load-bearing fact against the
        # canon_registry the bible emitted. Catches canon-FORKS the canon-BLIND critic
        # misses. Bounded to ONE cheap-call per registry item across FOUR array types —
        # events (<=8, scaled up to <=14 by chapter count, priority-ranked), exhibit_sets
        # (<=6), chains (<=6), quantities (<=8) — report-only, gated NARASI_CANON_DIFF
        # (default OFF → skipped → no cost, byte-identical). Enforce is a SEPARATE opt-in
        # (NARASI_CANON_DIFF_REVISE / NARASI_CANON_ENFORCE_NONREVEAL, both default OFF).
        # Never raises. ──
        _out = {"ran": False, "eligible": [], "nonreveal_eligible": [], "diffrevise_eligible": []}
        try:
            if str(os.environ.get("NARASI_CANON_DIFF", "0")).strip().lower() in ("1", "true", "yes", "on"):
                import json as _cjson, re as _cre
                _nrviol: list = []
                _cv: list = []
                _cf = str(result.get("canonical_facts") or "")
                _reg = None
                _reg_dbg = ""
                # FIX (2026-07-18, event-cap root-cause): computed once, up front, so BOTH the
                # fallback-extraction prompt (below) and the main diff loop's own triage cap
                # (further down) agree on the same scaled-by-chapter-count budget instead of two
                # independently-drifting flat "8"s. min(8 + max(0, chapters-6), 14): a 10-chapter
                # conspiracy plot gets more registry slots than a 4-chapter one.
                _cd_nch0 = (len(result.get("chapters") or [])
                            or len(body.get("chapters") or [])
                            or _gbook0.count("\n## "))
                _cd_event_cap = min(8 + max(0, _cd_nch0 - 6), 14)
                if _cf:
                    _cands = []
                    _m = _cre.search(r"```(?:json)?\s*(\{.*?\})\s*```", _cf, _cre.S)
                    if _m:
                        _cands.append(_m.group(1))
                    _m = _cre.search(r"canon_registry\"?\s*[:=]\s*(\{.*\})", _cf, _cre.S)
                    if _m:
                        _cands.append(_m.group(1))
                    for _bm in _cre.finditer(r"\{", _cf):
                        if len(_cands) >= 6:
                            break
                        _st = _bm.start()
                        if not _cre.search(r"\"events\"", _cf[_st:_st + 400]):
                            continue
                        _depth, _in_s, _esc = 0, False, False
                        for _i in range(_st, min(len(_cf), _st + 20000)):
                            _c = _cf[_i]
                            if _in_s:
                                if _esc:
                                    _esc = False
                                elif _c == "\\":
                                    _esc = True
                                elif _c == '"':
                                    _in_s = False
                            elif _c == '"':
                                _in_s = True
                            elif _c == "{":
                                _depth += 1
                            elif _c == "}":
                                _depth -= 1
                                if _depth == 0:
                                    _cands.append(_cf[_st:_i + 1])
                                    break
                    for _cand in _cands:
                        for _txt in (_cand, _cre.sub(r",\s*([}\]])", r"\1", _cand)):
                            try:
                                _p = _cjson.loads(_txt)
                            except Exception:  # noqa: BLE001
                                _reg_dbg = _reg_dbg or _txt[:200]
                                continue
                            if isinstance(_p, dict):
                                if "events" not in _p and isinstance(_p.get("canon_registry"), dict):
                                    _p = _p["canon_registry"]
                                _reg = _p
                                break
                        if _reg is not None:
                            break
                if (_reg is None and _cf
                        and str(os.environ.get("NARASI_CANON_REGISTRY_EXTRACT", "0")).strip().lower() in ("1", "true", "yes", "on")):
                    try:
                        from laozhang_api import _narasi_cheap_call as _xcall, _narasi_parse_json as _xparse
                        # FIX (2026-07-18, world-state fork): mirror the irreversible/occurs_chapter
                        # optional event fields (orchestrator/dynamic.py's bible-prompt addendum)
                        # here too, so a registry recovered via this prose-fallback path can still
                        # feed the world-state diff loop below — gated with the SAME
                        # NARASI_CANON_WORLDSTATE flag so the extraction prompt is byte-identical
                        # when that feature is off.
                        _xws_on = str(os.environ.get("NARASI_CANON_WORLDSTATE", "0")).strip().lower() in (
                            "1", "true", "yes", "on")
                        _xsys = (
                            "Extract the CANON REGISTRY from this story fact-sheet. Return ONLY JSON: "
                            "{\"events\":[{\"id\":\"<slug>\",\"summary\":\"<short>\","
                            "\"when\":{\"actor_age\":<int or null>,\"anchor\":\"<slug>\"},"
                            "\"where\":\"<location slug, if the event's location is load-bearing>\","
                            "\"participants\":{\"<role>\":\"<name>\"},\"key_action\":\"<slug>\","
                            "\"false_versions\":[{\"claim\":\"<the official/cover version>\",\"corrected_in_chapter\":<n>}],"
                            + ("\"irreversible\":<bool, true only for a permanent physical one-way "
                               "world-state change with chapters on both sides of it>,"
                               "\"occurs_chapter\":<n where dramatized on-page, or null>," if _xws_on else "")
                            + "\"chapters\":[<n>]}],"
                            "\"timeline\":[{\"id\":\"<slug>\",\"order\":<int>}],"
                            "\"kinship\":[{\"a\":\"<id>\",\"b\":\"<id>\",\"relation\":\"<str>\"}],"
                            "\"exhibit_sets\":[{\"id\":\"<slug>\",\"entries\":[{\"name\":\"<str>\",\"date\":\"<str>\","
                            "\"holder_or_issuer\":\"<str>\",\"detail\":\"<str>\"}]}],"
                            "\"chains\":[{\"id\":\"<slug>\",\"links\":[{\"entity\":\"<str>\",\"transferred_from\":\"<str>\","
                            "\"transferred_to\":\"<str>\",\"date\":\"<str>\"}]}],"
                            "\"quantities\":[{\"id\":\"<slug>\",\"value\":\"<str|int>\",\"unit\":\"<str>\","
                            "\"anchor_chapter\":<n>,\"since_event\":<bool, optional>}]} — ONLY load-bearing plot events (max " + str(_cd_event_cap) + "); "
                            "caps: <=12 timeline anchors, <=10 kinship pairs (person-to-person family relations only), "
                            "<=6 exhibit_sets, <=6 chains, <=8 quantities. timeline = ordering anchors for events. "
                            "exhibit_sets = a LIST-TYPE document/exhibit packet, one row per set with its entry list; "
                            "near-duplicate documents (two similar memos/ledgers) each get their OWN set, never merged, "
                            "each with its own date and label. chains = a multi-entity ownership/custody transfer "
                            "sequence (never in kinship). quantities = a standalone pinned duration/count/total; "
                            "anchor_chapter is where it is first pinned. Set since_event:true when the quantity is a "
                            "duration/tenure/age measured forward from a fixed past point (may legitimately grow as "
                            "story-time passes, or be smaller in a flashback chapter narrating an earlier point in "
                            "the story's own chronology) rather than a flat count with no time dimension (a bag "
                            "total, a headcount) — leave it unset for flat counts. Omit any array with no qualifying facts. "
                            "Where the sheet keeps an "
                            "official value AND a true value for one fact, the TRUE value is canonical and the official "
                            "one goes into false_versions. Include every named QUANTITY, list POSITION, and role-holder "
                            "the plot turns on. No prose.")
                        _xraw, _xcc = await _xcall(_xsys, _cf[:20000], tenant_id=tenant_id,
                                                   user_id=user_id, job_uuid=job_uuid, json_mode=True,
                                                   credit_row=False)
                        if sink is not None and _xcc:
                            sink.credits += int(_xcc)
                        _xd = _xparse(_xraw) if isinstance(_xraw, str) else (_xraw or {})
                        if isinstance(_xd, dict) and "events" not in _xd and isinstance(_xd.get("canon_registry"), dict):
                            _xd = _xd["canon_registry"]
                        if isinstance(_xd, dict) and isinstance(_xd.get("events"), list) and _xd["events"]:
                            _reg = _xd
                            log.info("canon-registry: fallback extraction recovered %d event(s) from prose bible",
                                     len(_xd["events"]))
                        else:
                            _xs = str(_xraw or "")
                            _sal = []
                            import re as _xre
                            import json as _xjson
                            for _bm in _xre.finditer(r"\{", _xs):
                                _st = _bm.start()
                                if not _xre.search(r"\"(?:id|summary)\"", _xs[_st:_st + 200]):
                                    continue
                                _depth, _in_s, _esc = 0, False, False
                                for _i in range(_st, min(len(_xs), _st + 8000)):
                                    _c = _xs[_i]
                                    if _in_s:
                                        if _esc:
                                            _esc = False
                                        elif _c == "\\":
                                            _esc = True
                                        elif _c == '"':
                                            _in_s = False
                                    elif _c == '"':
                                        _in_s = True
                                    elif _c == "{":
                                        _depth += 1
                                    elif _c == "}":
                                        _depth -= 1
                                        if _depth == 0:
                                            _cand = _xs[_st:_i + 1]
                                            for _txt in (_cand, _xre.sub(r",\s*([}\]])", r"\1", _cand)):
                                                try:
                                                    _obj = _xjson.loads(_txt)
                                                except Exception:  # noqa: BLE001
                                                    continue
                                                if isinstance(_obj, dict) and _obj.get("id") and _obj.get("summary"):
                                                    _sal.append(_obj)
                                                break
                                            break
                                if len(_sal) >= 8:
                                    break
                            if _sal:
                                _reg = {"events": _sal}
                                log.info("canon-registry: fallback SALVAGED %d complete event(s) from truncated response",
                                         len(_sal))
                            else:
                                log.warning("canon-registry fallback returned no events — raw head: %s",
                                            str(_xraw)[:220].replace("\n", " "))
                    except Exception as _xe:  # noqa: BLE001
                        log.warning("canon-registry fallback extraction failed (non-fatal): %s", _xe)
                _events = (_reg or {}).get("events") if isinstance(_reg, dict) else None
                # FIX (2026-07-18, schema-wiring root-cause): exhibit_sets/chains/quantities are
                # NEW registry array types (bible-emission schema in orchestrator/dynamic.py) that
                # the bible-writer can now emit, but until this fix nothing downstream ever read
                # them — only `events` was ever consumed here (confirmed via grep, zero other
                # hits). Read all four so a diffable registry item of ANY of these shapes actually
                # gets scanned, not silently discarded.
                _exsets = (_reg or {}).get("exhibit_sets") if isinstance(_reg, dict) else None
                _chains = (_reg or {}).get("chains") if isinstance(_reg, dict) else None
                _quants = (_reg or {}).get("quantities") if isinstance(_reg, dict) else None
                # FIX (2026-07-18, entities/timeline/kinship investigation): `timeline` and
                # `kinship` were the same "asked, never read" gap as exhibit_sets/chains/quantities
                # above (confirmed via grep — zero `_reg.get("timeline"/"kinship")` hits anywhere
                # before this fix). `entities` is deliberately NOT read here and was dropped from
                # the bible-emission schema (orchestrator/dynamic.py) in the same change: its
                # `name` field duplicates three already-wired deterministic gates
                # (_name_uniqueness_scan/_name_order_scan/_name_typo_scan), its nested `kinship`
                # dict is a second encoding of this same top-level `kinship` array, and its
                # `knowledge` sub-array needs a structurally different revealed-before-pinned-
                # chapter check that this per-item canonical-vs-prose diff loop doesn't fit —
                # left for a separate, deliberately-flagged design pass rather than a silent
                # free ride inside a field nothing consumed.
                _timeline = (_reg or {}).get("timeline") if isinstance(_reg, dict) else None
                _kinship = (_reg or {}).get("kinship") if isinstance(_reg, dict) else None
                _has_events = isinstance(_events, list) and bool(_events)
                _has_exsets = isinstance(_exsets, list) and bool(_exsets)
                _has_chains = isinstance(_chains, list) and bool(_chains)
                _has_quants = isinstance(_quants, list) and bool(_quants)
                _has_timeline = isinstance(_timeline, list) and bool(_timeline)
                _has_kinship = isinstance(_kinship, list) and bool(_kinship)
                _eligible = []
                if _has_events or _has_exsets or _has_chains or _has_quants or _has_timeline or _has_kinship:
                    from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
                    _cbook = _gbook0
                    # FIX (2026-07-18, truncation root-cause): a flat [:60000] head-slice reused
                    # for EVERY checked event covered only ~chapters 1-3 of a longer book, so any
                    # event/fork living later in the book could never be diffed at all (confirmed
                    # root cause of forks A/B/D/E). Same cheap model + same budget precedent as the
                    # numeric ledger and thread-tracker whole-book scans (NARASI_CRITIQUE_MAX_CHARS,
                    # default 300000) — reuse it here instead of the much smaller ad-hoc cap.
                    _cbk_max_chars = int(os.environ.get("NARASI_CRITIQUE_MAX_CHARS", "300000"))
                    _forks = []
                    _cpt = ""
                    _cpm = _cre.search(r"(?is)\bCOUNTERPOINT\s+NUMBERS?\b\s*[—:\-]?\s*"
                                       r"(.{0,500}?)(?=\n\s*(?:\d{1,2}\.|[A-Z][A-Z &]{6,})|\Z)", _cf)
                    if _cpm and _cpm.group(1).strip().rstrip(".").strip("'\"").lower() != "none":
                        _cpt = _cre.sub(r"\s+", " ", _cpm.group(1)).strip()[:400]
                    # FIX (2026-07-18, event-cap root-cause): a flat <=8 FIFO cap silently dropped
                    # later-triaged events on longer books and never prioritized events with more
                    # cross-chapter reach or a tracked false_versions (deliberate-misdirection)
                    # entry. Rank by those two priority signals instead of taking the bible's own
                    # emission order verbatim, then cut at the SAME chapter-scaled cap computed at
                    # the top of this gate (_cd_event_cap).
                    _events_ranked = sorted(
                        [e for e in _events if isinstance(e, dict)],
                        key=lambda e: (1 if e.get("false_versions") else 0,
                                       len(e.get("chapters")) if isinstance(e.get("chapters"), list) else 0),
                        reverse=True)[:_cd_event_cap] if _has_events else []
                    for _ev in _events_ranked:
                        if not _cbook:
                            continue
                        _canon = {k: _ev.get(k) for k in ("when", "where", "participants", "key_action", "summary") if _ev.get(k)}
                        _fv = _ev.get("false_versions") or []
                        _csys = (
                            "You are a canon auditor with a fact sheet you must trust over your own reading. "
                            "CANONICAL values for one event: " + _cjson.dumps(_canon, ensure_ascii=False) + ". "
                            "Sanctioned FALSE versions (LEGAL only in chapters BEFORE their corrected_in_chapter): "
                            + _cjson.dumps(_fv, ensure_ascii=False) + ". Scan the book and report EVERY chapter that "
                            "renders this event with a value DIFFERENT from the canonical one and NOT a sanctioned "
                            "false version before its correction — even if it reads like an intended reveal. Return "
                            "ONLY JSON: {\"forks\":[{\"chapter\":<int>,\"field\":\"<field>\",\"found\":\"<value>\","
                            "\"expected\":\"<canonical value>\",\"quote\":\"<short excerpt from THIS chapter, copied "
                            "character-for-character from the book text, that shows the found value — never "
                            "paraphrased>\"}]}. Empty list if the book is consistent with canon."
                            + ((" SANCTIONED COUNTERPOINT PAIRS (the story keeps BOTH values alive by design "
                                "— never report either as a fork): " + _cpt) if _cpt else ""))
                        try:
                            _raw, _cc = await _narasi_cheap_call(_csys, (_cbook or "")[:_cbk_max_chars],
                                                                 tenant_id=tenant_id, user_id=user_id,
                                                                 job_uuid=job_uuid, json_mode=True,
                                                                 credit_row=False)
                            if sink is not None and _cc:
                                sink.credits += int(_cc)
                            _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                            for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                if isinstance(_f, dict) and _f.get("found"):
                                    _forks.append({"event": _ev.get("id") or _ev.get("summary"),
                                                   "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                   "found": str(_f.get("found"))[:160],
                                                   "expected": str(_f.get("expected"))[:160],
                                                   "quote": str(_f.get("quote") or "")[:160]})
                        except Exception as _e:  # noqa: BLE001
                            log.warning("canon-diff event scan failed (non-fatal): %s", _e)
                    # ── WORLD-STATE / IRREVERSIBLE EVENTS (Phase 2c) — same one-cheap-call-per-
                    # item pattern as the loops in this function, but the CHECK is a different
                    # KIND: not "does this chapter render a different VALUE for a fact" (the
                    # per-event loop above) but "does this chapter narrate an irreversible event's
                    # OCCURRENCE STATUS (has it happened yet) inconsistently with the ONE chapter
                    # where it is dramatized as happening". Root-caused against job funym2wo (the
                    # settlement-clearance world-state fork): Ch4 narrated the clearance as already
                    # complete ("the flat, cleared expanse... where there had been the low roofs of
                    # the settlement") while Ch5 still called it future ("They're going to take the
                    # settlement apart") and Ch6 dramatized it happening for the first time ("a
                    # first bite at... Mrs. Baek's"). The existing events/false_versions/timeline
                    # schema tracks WHAT happened and WHEN it is referenced or corrected, but nothing
                    # tracked whether a one-way event's aftermath may legally appear yet at a given
                    # chapter — that is the gap this closes, via the new optional
                    # irreversible/occurs_chapter fields on the events schema (orchestrator/
                    # dynamic.py's bible-prompt addendum). Separately flag-gated
                    # (NARASI_CANON_DIFF_WORLDSTATE) so existing NARASI_CANON_DIFF deployments are
                    # unaffected until explicitly opted in on top of it. Capped at <=6 events since
                    # irreversible plot events are rare relative to the general event registry.
                    # Findings feed the SAME _forks list as every other check above, so they ride the
                    # existing report/NARASI_CANON_DIFF_REVISE/NARASI_CANON_ENFORCE_NONREVEAL wiring
                    # and the existing merged revise call — no second revise path.
                    _ws_checked = 0
                    if (_has_events and _cbook
                            and str(os.environ.get("NARASI_CANON_DIFF_WORLDSTATE", "0")).strip().lower()
                            in ("1", "true", "yes", "on")):
                        _ws_events = [e for e in _events if isinstance(e, dict) and e.get("irreversible")
                                      and isinstance(e.get("occurs_chapter"), (int, float))][:6]
                        _ws_checked = len(_ws_events)
                        for _we in _ws_events:
                            _we_ch = int(_we.get("occurs_chapter"))
                            _we_desc = {k: _we.get(k) for k in ("summary", "key_action", "moral_load") if _we.get(k)}
                            _wssys = (
                                "You are a canon auditor checking EVENT-STATUS consistency, not fact "
                                "values. This IRREVERSIBLE, one-way plot event is dramatized as actually "
                                "happening ON-PAGE in chapter " + str(_we_ch) + ": "
                                + _cjson.dumps(_we_desc, ensure_ascii=False) + ". Once it happens it "
                                "cannot un-happen. Scan the WHOLE book and report: (a) any chapter "
                                "NUMBERED LOWER than " + str(_we_ch) + " that narrates or implies this "
                                "event's AFTERMATH as already complete (past tense, the thing already "
                                "gone/destroyed/dead/cleared) — BUT FIRST check whether that chapter is a "
                                "DELIBERATE flash-forward, prologue, or framed cold-open (explicit "
                                "retrospective narration, a labeled Prologue/cold-open, phrasing like "
                                "'months later I'd learn' or 'looking back', a clear shift in narrative "
                                "distance from the story's main timeline) rather than a linear-time "
                                "rendering error — if it is, that chapter is CORRECT and must NOT be "
                                "reported; and (b) any chapter NUMBERED HIGHER than "
                                + str(_we_ch) + " that narrates or implies the event has NOT happened "
                                "yet (future tense, still pending, still standing/alive/intact) after it "
                                "was already dramatized as done — BUT FIRST check whether that chapter is "
                                "a DELIBERATE flashback or memory sequence (explicit retrospective "
                                "framing, phrasing like 'six months earlier' or 'she remembered the day "
                                "before', a labeled flashback, a clear shift to an EARLIER point in the "
                                "story's own chronology) rather than a linear-time rendering error — if "
                                "it is, that chapter is CORRECT and must NOT be reported. Ignore chapters "
                                "that merely foreshadow, threaten, or plan the event as a future event — "
                                "that is CORRECT before "
                                "chapter " + str(_we_ch) + ". Only report an actual linear-time "
                                "contradiction of occurrence status — never a deliberate flash-forward, "
                                "prologue, frame chapter, or flashback. Return ONLY JSON: {\"forks\":[{\"chapter\":<int>,"
                                "\"field\":\"world_state_pre\" or \"world_state_post\","
                                "\"found\":\"<what this chapter implies about whether the event has "
                                "happened>\",\"expected\":\"<what SHOULD be true at this chapter, given "
                                "occurs_chapter=" + str(_we_ch) + ">\",\"quote\":\"<short excerpt from "
                                "THIS chapter, copied character-for-character from the book text, that "
                                "shows the contradiction — never paraphrased>\"}]}. Empty list if the "
                                "book is consistent.")
                            try:
                                _raw, _cc = await _narasi_cheap_call(_wssys, (_cbook or "")[:_cbk_max_chars],
                                                                     tenant_id=tenant_id, user_id=user_id,
                                                                     job_uuid=job_uuid, json_mode=True,
                                                                     credit_row=False)
                                if sink is not None and _cc:
                                    sink.credits += int(_cc)
                                _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                                for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                    if isinstance(_f, dict) and _f.get("found"):
                                        _forks.append({"event": _we.get("id") or _we.get("summary") or "world_state",
                                                       "chapter": _f.get("chapter"),
                                                       "field": _f.get("field") or "world_state",
                                                       "found": str(_f.get("found"))[:160],
                                                       "expected": str(_f.get("expected"))[:160],
                                                       "quote": str(_f.get("quote") or "")[:160]})
                            except Exception as _e:  # noqa: BLE001
                                log.warning("canon-diff world-state scan failed (non-fatal): %s", _e)
                    # ── FIX (2026-07-18, schema-wiring): exhibit_sets / chains / quantities
                    # scan-and-compare, mirroring the per-event pattern above. Each bounded to
                    # ONE cheap call per item, same order-of-magnitude caps the bible-emission
                    # schema itself uses (<=6/<=6/<=8).
                    for _ex in (_exsets[:6] if _has_exsets else []):
                        if not isinstance(_ex, dict) or not _cbook:
                            continue
                        _ex_entries = _ex.get("entries") or []
                        _exsys = (
                            "You are a canon auditor with a fact sheet you must trust over your own reading. "
                            "CANONICAL exhibit/document set — the COMPLETE, FIXED enumerated list, entry count "
                            "included: " + _cjson.dumps(_ex_entries, ensure_ascii=False) + ". Scan the book and "
                            "report EVERY chapter that renders this SAME set with: a DIFFERENT total entry count, "
                            "a NEW entry not in the canonical list, a canonical entry DROPPED/missing, or any "
                            "entry's date/holder_or_issuer/detail changed from its canonical row. Return ONLY "
                            "JSON: {\"forks\":[{\"chapter\":<int>,\"field\":\"<entry name, or 'count'>\","
                            "\"found\":\"<value>\",\"expected\":\"<canonical value>\",\"quote\":\"<short excerpt "
                            "from THIS chapter, copied character-for-character from the book text, that shows the "
                            "found value — never paraphrased>\"}]}. Empty list if the book is consistent with "
                            "canon.")
                        try:
                            _raw, _cc = await _narasi_cheap_call(_exsys, (_cbook or "")[:_cbk_max_chars],
                                                                 tenant_id=tenant_id, user_id=user_id,
                                                                 job_uuid=job_uuid, json_mode=True,
                                                                 credit_row=False)
                            if sink is not None and _cc:
                                sink.credits += int(_cc)
                            _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                            for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                if isinstance(_f, dict) and _f.get("found"):
                                    _forks.append({"event": _ex.get("id") or "exhibit_set",
                                                   "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                   "found": str(_f.get("found"))[:160],
                                                   "expected": str(_f.get("expected"))[:160],
                                                   "quote": str(_f.get("quote") or "")[:160]})
                        except Exception as _e:  # noqa: BLE001
                            log.warning("canon-diff exhibit_set scan failed (non-fatal): %s", _e)
                    for _ch in (_chains[:6] if _has_chains else []):
                        if not isinstance(_ch, dict) or not _cbook:
                            continue
                        _ch_links = _ch.get("links") or []
                        _chsys = (
                            "You are a canon auditor with a fact sheet you must trust over your own reading. "
                            "CANONICAL ownership/custody TRANSFER CHAIN — the fixed sequence of named-entity "
                            "transfers, in order: " + _cjson.dumps(_ch_links, ensure_ascii=False) + ". Scan the "
                            "book and report EVERY chapter that renders this SAME chain with a different "
                            "transferred_from or transferred_to entity at any link, a different transfer date, "
                            "an inserted or dropped link, or a reordered sequence. Return ONLY JSON: "
                            "{\"forks\":[{\"chapter\":<int>,\"field\":\"<link index or entity>\","
                            "\"found\":\"<value>\",\"expected\":\"<canonical value>\",\"quote\":\"<short excerpt "
                            "from THIS chapter, copied character-for-character from the book text, that shows the "
                            "found value — never paraphrased>\"}]}. Empty list if the book is consistent with "
                            "canon.")
                        try:
                            _raw, _cc = await _narasi_cheap_call(_chsys, (_cbook or "")[:_cbk_max_chars],
                                                                 tenant_id=tenant_id, user_id=user_id,
                                                                 job_uuid=job_uuid, json_mode=True,
                                                                 credit_row=False)
                            if sink is not None and _cc:
                                sink.credits += int(_cc)
                            _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                            for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                if isinstance(_f, dict) and _f.get("found"):
                                    _forks.append({"event": _ch.get("id") or "chain",
                                                   "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                   "found": str(_f.get("found"))[:160],
                                                   "expected": str(_f.get("expected"))[:160],
                                                   "quote": str(_f.get("quote") or "")[:160]})
                        except Exception as _e:  # noqa: BLE001
                            log.warning("canon-diff chain scan failed (non-fatal): %s", _e)
                    for _qt in (_quants[:8] if _has_quants else []):
                        if not isinstance(_qt, dict) or not _cbook:
                            continue
                        _qt_canon = {k: _qt.get(k) for k in ("value", "unit", "anchor_chapter", "since_event")
                                     if _qt.get(k) is not None}
                        # FIX (2026-07-19, duration-since-event false-fork/false-negative): a plain
                        # "different value = fork" check (below) cannot distinguish a real bug (Ch1/
                        # Ch9 "ten years" -> Ch10 "eleven years" with only ~3 story-months elapsed)
                        # from a LEGITIMATE increment (real story-time passed, so the restated
                        # duration correctly grew) — it would also misfire on the negative-test case
                        # where an exact tenure figure and a separately-rounded, distinct span for the
                        # same entity are conflated as one referent just because both are "years", and
                        # (adversarial-audit-caught, fixed before ship) it must not misfire on a
                        # deliberate flashback chapter narrating an earlier point in the story's own
                        # chronology, where a SMALLER value is correct, nor be a one-sided ceiling
                        # check blind to an UNDERSHOOT (a stated precise interval implying MORE change
                        # than what's shown is just as much a fork as an unjustified overshoot).
                        # Gated on the bible-writer's own since_event:true flag (schema addition,
                        # orchestrator/dynamic.py + this file's fallback extractor) so a flat count
                        # (a bag total, a headcount) is unaffected and keeps the original flat-diff
                        # wording. Appends to the SAME _qtsys prompt / SAME _forks list / SAME
                        # NARASI_CANON_DIFF_REVISE+NARASI_CANON_ENFORCE_NONREVEAL enforcement wiring
                        # as every other canon-diff check above — no new mechanism, no new revise path.
                        _qt_since = bool(_qt_canon.get("since_event"))
                        _qtsys = (
                            "You are a canon auditor with a fact sheet you must trust over your own reading. "
                            "CANONICAL pinned quantity: " + _cjson.dumps(_qt_canon, ensure_ascii=False) + ". Scan "
                            "the book and report EVERY chapter that states a DIFFERENT value for this SAME "
                            "quantity (the same duration/count/measurement, for the same referent) than the "
                            "canonical one — including a second, contradicting value stated ELSEWHERE IN THE "
                            "SAME chapter as the anchor_chapter, not only in a different chapter. "
                            + ("This quantity is marked since_event:true — a DURATION/TENURE/AGE MEASURED "
                               "FORWARD FROM A FIXED PAST POINT, not a flat count. Its value is EXPECTED to "
                               "change as the story's own internal clock moves — check the book's own "
                               "internal story-time markers (an explicit interval, a stated season/month/"
                               "year change, 'X months/years later', dated references) between the anchor "
                               "chapter and any chapter stating a different value BEFORE judging a fork. "
                               "Report a fork ONLY when: (1) a chapter numbered AFTER the anchor chapter "
                               "states a SMALLER value (a duration must never shrink going forward in the "
                               "story's chronology) — UNLESS that chapter is a deliberate flashback or "
                               "memory sequence narrating an EARLIER point in the story's own chronology "
                               "than the anchor chapter, in which case a smaller value there is CORRECT and "
                               "must NOT be reported; (2) the value increases by MORE than the elapsed "
                               "story-time the book itself establishes would justify (e.g. only a few "
                               "story-months pass between the two mentions but the stated figure jumps a "
                               "full year or more); or (3) the book states a SPECIFIC, PRECISE elapsed "
                               "interval between the two mentions (not just a loose/vague sense of time "
                               "passing) and the stated value increased by MATERIALLY LESS than that precise "
                               "interval implies — an undershoot is just as much a fork as an overshoot when "
                               "the book gives you an exact interval to check against; when the book's time-"
                               "passage markers are vague or approximate, do not report a small/uncertain "
                               "undershoot, only a clear overshoot or a decrease. Do NOT report a fork merely "
                               "because the number changed — check the elapsed story-time first. Separately: "
                               "do NOT treat two different-looking duration mentions for the same person/"
                               "thing as the SAME quantity (and so do NOT report them as a fork against each "
                               "other) when they plausibly describe DIFFERENT spans — an exact, precisely-"
                               "dated figure (e.g. 'twenty-four years, since 2001') and a separate, "
                               "deliberately looser or colloquial rounding of a related-but-distinct span "
                               "(e.g. 'about twenty years of collecting') are DIFFERENT referents unless the "
                               "book itself treats them as describing the identical measured span. "
                               if _qt_since else "")
                            + "Return ONLY "
                            "JSON: {\"forks\":[{\"chapter\":<int>,\"field\":\"value\",\"found\":\"<value>\","
                            "\"expected\":\"<canonical value>\",\"quote\":\"<short excerpt from THIS chapter, "
                            "copied character-for-character from the book text, that shows the found value — "
                            "never paraphrased>\"}]}. Empty list if the book is consistent with canon.")
                        try:
                            _raw, _cc = await _narasi_cheap_call(_qtsys, (_cbook or "")[:_cbk_max_chars],
                                                                 tenant_id=tenant_id, user_id=user_id,
                                                                 job_uuid=job_uuid, json_mode=True,
                                                                 credit_row=False)
                            if sink is not None and _cc:
                                sink.credits += int(_cc)
                            _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                            for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                if isinstance(_f, dict) and _f.get("found"):
                                    _forks.append({"event": _qt.get("id") or "quantity",
                                                   "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                   "found": str(_f.get("found"))[:160],
                                                   "expected": str(_f.get("expected"))[:160],
                                                   "quote": str(_f.get("quote") or "")[:160]})
                        except Exception as _e:  # noqa: BLE001
                            log.warning("canon-diff quantity scan failed (non-fatal): %s", _e)
                    # ── FIX (2026-07-18, entities/timeline/kinship): `kinship` is the same
                    # shape as `chains` (entity-to-entity edges with a label) — a flat list of
                    # relation pairs rather than chains' nested {id, links:[...]} grouping, so
                    # each pair is its own diff item (same one-cheap-call-per-item pattern as
                    # exhibit_sets/chains/quantities above). Cap <=10 — the bible-emission schema
                    # (orchestrator/dynamic.py) previously stated NO cap for kinship; this is the
                    # same edit that adds one there.
                    for _kn in (_kinship[:10] if _has_kinship else []):
                        if not isinstance(_kn, dict) or not _cbook:
                            continue
                        _kn_canon = {k: _kn.get(k) for k in ("a", "b", "relation") if _kn.get(k) is not None}
                        if not (_kn_canon.get("a") and _kn_canon.get("b") and _kn_canon.get("relation")):
                            continue
                        _knsys = (
                            "You are a canon auditor with a fact sheet you must trust over your own reading. "
                            "CANONICAL family/kinship RELATION between two named entities: "
                            + _cjson.dumps(_kn_canon, ensure_ascii=False) + ". Scan the book and report "
                            "EVERY chapter that renders the relation between these SAME two entities as "
                            "something DIFFERENT from the canonical relation — a different relation label, "
                            "or the relation reversed/contradicted (e.g. 'husband' in one chapter and "
                            "'brother' in another, for the same pair). Return ONLY JSON: {\"forks\":["
                            "{\"chapter\":<int>,\"field\":\"relation\",\"found\":\"<value>\","
                            "\"expected\":\"<canonical value>\",\"quote\":\"<short excerpt from THIS "
                            "chapter, copied character-for-character from the book text, that shows the "
                            "found value — never paraphrased>\"}]}. Empty list if the book is consistent "
                            "with canon.")
                        try:
                            _raw, _cc = await _narasi_cheap_call(_knsys, (_cbook or "")[:_cbk_max_chars],
                                                                 tenant_id=tenant_id, user_id=user_id,
                                                                 job_uuid=job_uuid, json_mode=True,
                                                                 credit_row=False)
                            if sink is not None and _cc:
                                sink.credits += int(_cc)
                            _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                            for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                if isinstance(_f, dict) and _f.get("found"):
                                    _forks.append({"event": f"kinship:{_kn_canon['a']}-{_kn_canon['b']}",
                                                   "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                   "found": str(_f.get("found"))[:160],
                                                   "expected": str(_f.get("expected"))[:160],
                                                   "quote": str(_f.get("quote") or "")[:160]})
                        except Exception as _e:  # noqa: BLE001
                            log.warning("canon-diff kinship scan failed (non-fatal): %s", _e)
                    # ── FIX (2026-07-18, entities/timeline/kinship): `timeline` is a global
                    # ORDERING constraint across its whole anchor set, not an independently
                    # diffable single item like exhibit_sets/chains/quantities/kinship above — a
                    # per-item loop can't check "out of order" without every other anchor's
                    # position, so this is ONE cheap call over the full (capped) ordered list.
                    # `timeline` entries are bare {id, order} with no summary of their own;
                    # resolve each id to a human description by cross-referencing `events` whose
                    # when.anchor matches that id, falling back to the bare slug when unmatched.
                    _tl_checked = 0
                    if _has_timeline and _cbook:
                        _tl_items = [t for t in _timeline if isinstance(t, dict)
                                     and t.get("id") is not None
                                     and isinstance(t.get("order"), (int, float))]
                        _tl_sorted = sorted(_tl_items, key=lambda t: t["order"])[:12]
                        _events_by_anchor: dict = {}
                        for _ev0 in (_events or []):
                            if isinstance(_ev0, dict):
                                _when0 = _ev0.get("when")
                                _anc0 = _when0.get("anchor") if isinstance(_when0, dict) else None
                                if _anc0 and _anc0 not in _events_by_anchor:
                                    _events_by_anchor[_anc0] = _ev0.get("summary") or _ev0.get("id")
                        _tl_ordered = [_events_by_anchor.get(t["id"], t["id"]) for t in _tl_sorted]
                        _tl_checked = len(_tl_ordered)
                        if len(_tl_ordered) >= 2:
                            # FIX (2026-07-18, report-vs-closure date-arithmetic fork): the order-only
                            # check above missed a class of bug where a chapter states a NUMERIC
                            # interval ("six months after my report") whose implied direction/gap
                            # contradicts the canonical order or an absolute date the book states
                            # elsewhere for the SAME two anchors (root-caused against job funym2wo: one
                            # passage pins the report to "August 2015" and the closure to "March 2015"
                            # five months earlier; another passage claims the closure came "six months
                            # after my report", i.e. the reverse direction). narasi_arithmetic.py's
                            # scan_interval_vs_dates_en already declines to guess this one deterministically
                            # — it correctly self-gates (via its own len(dates)<=8 candidate-expansion cap
                            # and its "after THE X"/"after my X" event-ref heuristic) rather than risk a
                            # false positive across a date-dense manuscript (52 distinct date mentions
                            # here). That gap is real but not closeable with more regex; this call already
                            # reads the whole book, so it is asked to do the arithmetic explicitly instead.
                            # FIX (2026-07-19, generalized beyond the report/closure pair): the paragraph
                            # above was root-caused against, and its prompt text literally scoped to,
                            # ONE anchor pair ("these SAME anchors" = only the two anchors in that one
                            # bug). An independent audit found a second, equally-real fork of the exact
                            # same defect class on a DIFFERENT pair that the canonical `_tl_ordered` list
                            # doesn't even carry: a character's dismissal date is stated as "same week as
                            # the [report]" (an interval — implying one month) in some chapters and as a
                            # flatly different absolute month elsewhere, with no interval language at all
                            # in the conflicting passage. Since this call already reads the whole book,
                            # the instruction below no longer restricts the arithmetic check to the
                            # pre-supplied anchor list — it asks the model to find ANY named anchor event
                            # stated with a numeric span, or with two directly conflicting absolute dates,
                            # whether or not that event is one of the anchors enumerated above.
                            _tlsys = (
                                "You are a canon auditor with a fact sheet you must trust over your own "
                                "reading. CANONICAL chronological ORDER of these named story anchors, "
                                "EARLIEST first: " + _cjson.dumps(_tl_ordered, ensure_ascii=False) + ". "
                                "Scan the book and report EVERY chapter that narrates or implies TWO OR "
                                "MORE of these anchors in an order DIFFERENT from the canonical one (e.g. "
                                "treating a later anchor as though it happened before an earlier one). "
                                "ALSO check DERIVED-DATE ARITHMETIC — and this check is NOT limited to the "
                                "named anchors above: it applies to ANY named story event in the book (the "
                                "anchors above, or any other event with a name, e.g. a firing, dismissal, "
                                "filing, or arrest) that is stated with a numeric span (\"N days/weeks/"
                                "months/years before/after/since\" that event) in one place, when the book "
                                "elsewhere gives an absolute or month-level date for that SAME event — "
                                "restated directly, or implied by a different span — that conflicts with "
                                "it. Verify any such span or restatement against (a) the canonical order "
                                "above when both ends of the span are anchors in that list, and (b) any "
                                "absolute or month-level date the book states ELSEWHERE for that same "
                                "event or a closely-related one (e.g. a firing/dismissal date stated "
                                "multiple ways in different chapters). Report a fork if the stated span "
                                "contradicts the canonical order, or if it implies or directly states a "
                                "date/gap that does not match another date the book gives for that same "
                                "event — even if no single sentence uses \"before\"/\"after\" incorrectly "
                                "in isolation, even if the span's own local sentence has no date nearby to "
                                "check it against directly, and even if the conflicting event never "
                                "appears in the canonical anchor list above. "
                                "Return ONLY JSON: {\"forks\":[{\"chapter\":<int>,\"field\":\"<the "
                                "anchor(s) or named event(s) involved>\",\"found\":\"<the order, interval, "
                                "or date implied in this chapter>\",\"expected\":\"<canonical order, or "
                                "the date/interval implied by dates stated elsewhere for that event>\","
                                "\"quote\":\"<short excerpt "
                                "from THIS chapter, copied character-for-character from the book text, "
                                "that shows the found order, interval, or date — never paraphrased>\"}]}. "
                                "Empty list if the book is consistent with canon.")
                            try:
                                _raw, _cc = await _narasi_cheap_call(_tlsys, (_cbook or "")[:_cbk_max_chars],
                                                                     tenant_id=tenant_id, user_id=user_id,
                                                                     job_uuid=job_uuid, json_mode=True,
                                                                     credit_row=False)
                                if sink is not None and _cc:
                                    sink.credits += int(_cc)
                                _d = _narasi_parse_json(_raw) if isinstance(_raw, str) else (_raw or {})
                                for _f in ((_d.get("forks") or []) if isinstance(_d, dict) else []):
                                    if isinstance(_f, dict) and _f.get("found"):
                                        _forks.append({"event": "timeline",
                                                       "chapter": _f.get("chapter"), "field": _f.get("field"),
                                                       "found": str(_f.get("found"))[:160],
                                                       "expected": str(_f.get("expected"))[:160],
                                                       "quote": str(_f.get("quote") or "")[:160]})
                            except Exception as _e:  # noqa: BLE001
                                log.warning("canon-diff timeline scan failed (non-fatal): %s", _e)
                    result["canon_diff"] = {
                        "events_checked": len(_events_ranked),
                        "exhibit_sets_checked": len(_exsets[:6]) if _has_exsets else 0,
                        "chains_checked": len(_chains[:6]) if _has_chains else 0,
                        "quantities_checked": len(_quants[:8]) if _has_quants else 0,
                        "kinship_checked": len(_kinship[:10]) if _has_kinship else 0,
                        "timeline_checked": _tl_checked,
                        "worldstate_checked": _ws_checked,
                        "forks": _forks[:20]}
                    if _forks:
                        log.warning("canon-diff: %d canon-fork(s) flagged for job %s (report-only): %s",
                                    len(_forks), job_id, [str(f)[:90] for f in _forks[:5]])
                        _nrviol = []
                        try:
                            if str(os.environ.get("NARASI_CANON_ENFORCE_NONREVEAL", "0")).strip().lower() in ("1", "true", "yes", "on"):
                                _fast_path = [f for f in _forks
                                              if str((f or {}).get("field") or "").split(".")[0] in ("when", "participants")]
                                _fast_ids = {id(f) for f in _fast_path}
                                _other = [f for f in _forks if id(f) not in _fast_ids]
                                _classified = []
                                if (_other and len(_fast_path) < 4
                                        and str(os.environ.get("NARASI_CANON_DIFF_CLASSIFY", "0")).strip().lower() in (
                                            "1", "true", "yes", "on")):
                                    try:
                                        _cd_chapters = result.get("chapters") or []
                                        _cd_items = []
                                        for _f in _other:
                                            _sig = (f"Field: {_f.get('field')}. Chapter {_f.get('chapter')} states "
                                                    f"this as {_f.get('found')!r}; canon registry expects "
                                                    f"{_f.get('expected')!r}.")
                                            _exc, _ = _canon_excerpt(_cd_chapters, chapter_no=_f.get("chapter"),
                                                                     quote_source=_wq(_f.get("quote")))
                                            _cd_items.append({"signal": _sig, "excerpt": _exc})
                                        _cd_continuity = await _narasi_classify_canon_items(
                                            _cd_items, tenant_id=tenant_id, user_id=user_id,
                                            job_uuid=job_uuid, sink=sink)
                                        _classified = [_other[_i] for _i in _cd_continuity if 0 <= _i < len(_other)]
                                        if _classified:
                                            log.info("canon-diff classify: %d/%d residual fork(s) (non when/"
                                                     "participants) are plain continuity errors -> eligible for "
                                                     "enforce", len(_classified), len(_other))
                                    except Exception as _cde:  # noqa: BLE001
                                        log.warning("canon-diff classify failed (non-fatal, residual forks stay "
                                                    "protected): %s", _cde)
                                _nrv = (_fast_path + _classified)[:4]
                                if _nrv:
                                    _chn_locator_on = os.environ.get(
                                        "NARASI_CANON_ENFORCE_CHN_LOCATOR", "0").strip().lower() in (
                                        "1", "true", "yes", "on")
                                    for f in _nrv:
                                        _ev = _wq(f.get("quote")) or str(f.get("found") or "")[:200]
                                        _chn = f.get("chapter")
                                        if _chn is not None and _chn_locator_on:
                                            _ev = f"{_ev} @ch{_chn}"
                                        _nrviol.append({
                                            "type": "canon_attribution", "severity": "high",
                                            "evidence": _ev[:200],
                                            "fix": (f"The fact sheet pins event «{f.get('event')}» {f.get('field')} "
                                                    f"differently than this chapter states — align the chapter to "
                                                    f"the fact sheet's version; change nothing else.")})
                        except Exception as _nre:  # noqa: BLE001
                            log.warning("canon-enforce non-reveal failed (non-fatal): %s", _nre)
                        if str(os.environ.get("NARASI_CANON_DIFF_REVISE", "0")).strip().lower() in ("1", "true", "yes", "on"):
                            try:
                                _cv = [{"type": "canon_fork", "severity": "high",
                                        "evidence": "Bab %s renders %s as '%s'; canon = '%s'" % (
                                            _fk.get("chapter"), _fk.get("field"), _fk.get("found"), _fk.get("expected")),
                                        "fix": "align this chapter's rendering to the canonical value"}
                                       for _fk in _forks] if _cbook else []
                            except Exception as _e:  # noqa: BLE001
                                log.warning("canon-diff enforce revise failed (non-fatal): %s", _e)
                                _cv = []
                        _eligible = list(_nrviol) + list(_cv)
                    else:
                        _cd_total_checked = (len(_events_ranked)
                                              + (len(_exsets[:6]) if _has_exsets else 0)
                                              + (len(_chains[:6]) if _has_chains else 0)
                                              + (len(_quants[:8]) if _has_quants else 0))
                        log.info("canon-diff: 0 fork(s) across %d item(s) for job %s (clean run)",
                                 _cd_total_checked, job_id)
                else:
                    _reason = ("canonical_facts absent" if not _cf else
                               "registry parse failed" if _reg is None else
                               "registry has no events/exhibit_sets/chains/quantities")
                    if _reason == "registry parse failed" and not _reg_dbg:
                        _reg_dbg = "no registry-shaped block found; bible tail: " + _cf[-160:]
                    result["canon_diff"] = {"events_checked": 0, "forks": [], "skipped": _reason}
                    log.info("canon-diff: skipped for job %s — %s%s", job_id, _reason,
                             (" | " + _cre.sub(r"\s+", " ", _reg_dbg)) if _reg_dbg else "")
                _out.update({"ran": True, "nonreveal_eligible": _nrviol,
                             "diffrevise_eligible": _cv, "eligible": _eligible})
        except Exception as e:  # noqa: BLE001
            log.warning("canon-diff gate failed (non-fatal): %s", e)
        return _out

    def _v3g_canon_diff_finalize(_out, _changed):
        if not _out.get("ran"):
            return
        try:
            if _out.get("nonreveal_eligible") and _changed:
                log.info("canon-enforce: %d non-reveal fork(s) sent to revise — book updated",
                         len(_out["nonreveal_eligible"]))
            elif _out.get("nonreveal_eligible"):
                log.info("canon-enforce: %d non-reveal fork(s) sent — revise landed nothing",
                         len(_out["nonreveal_eligible"]))
            if _out.get("diffrevise_eligible") and _changed and result.get("canon_diff") is not None:
                result["canon_diff"]["revised"] = True
        except Exception:  # noqa: BLE001
            pass

    async def _v3g_thread_tracker_detect():
        # ── (2.76) UNRESOLVED-THREAD TRACKER — catches a setup the mega-critic already
        # missed anywhere in the book by reading the WHOLE assembled book once (Pass 1,
        # extraction) and then checking just the ending once more (Pass 2, verification).
        # Gated NARASI_THREAD_TRACKER (default OFF → skipped → no cost, byte-identical).
        # Enforce is a separate opt-in (NARASI_THREAD_TRACKER_ENFORCE, default OFF). ──
        _out = {"ran": False, "eligible": []}
        try:
            if str(os.environ.get("NARASI_THREAD_TRACKER", "0")).strip().lower() in ("1", "true", "yes", "on"):
                from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
                _ttbook = _gbook0
                if _ttbook:
                    _tt_max = int(os.environ.get("NARASI_CRITIQUE_MAX_CHARS", "300000"))
                    _tt_threads: list = []
                    # FIX (2026-07-18, thread-tracker coverage-gap root-cause): Pass 1 originally
                    # applied conservatism TWICE (once here, again in Pass 2) -- a real production
                    # job extracted only 1 of 6+ confirmed dropped threads, and the one it found
                    # was the earliest/most salient (classic primacy bias on a single exhaustive-
                    # enumeration call). Root cause: 5 of 6 misses fit the EXISTING taxonomy fine
                    # (this was a recall problem, not a taxonomy problem) -- but 1 of 6 (a committee
                    # explicitly grants a character the floor, then the chapter cuts away before she
                    # speaks) had no home in the old 4-type taxonomy at all. Fix: (a) Pass 1 is now
                    # a wide-net candidate generator -- drop its own "when unsure, omit" filter and
                    # let Pass 2's still-conservative verification (unchanged below) be the sole
                    # precision gate; (b) add a 5th thread_type, unfulfilled_scene, naming this exact
                    # shape explicitly.
                    _tt_sys1 = (
                        "You are reading a complete manuscript once, looking for every THREAD the "
                        "story itself opens and appears to leave unresolved. Five shapes qualify: "
                        "character_fate (a character's fate left hanging), unpunished_enabler (a "
                        "named enabler or co-conspirator whose culpability is established on-page "
                        "but never addressed), promised_consequence (a promised future consequence), "
                        "open_mystery (a mystery the text frames as a specific question), and "
                        "unfulfilled_scene (the text explicitly sets up a scene and cuts away before "
                        "it happens -- a character is granted the floor, the opportunity, or the "
                        "platform to do something on-page, and the narrative moves on before they do "
                        "it). List every candidate generously -- a later, separate pass will verify "
                        "against the ending, so over-including a borderline case here costs nothing; "
                        "only skip something that is unmistakably a deliberate, artful open ending, "
                        "not a genuine omission. Before listing a candidate, check whether the passage "
                        "is itself closing a concern raised earlier rather than opening a new one -- a "
                        "hedged-but-final beat late in the book (a stated prognosis, an acknowledged "
                        "ongoing legal process, an explicitly incomplete-but-addressed physical harm) "
                        "is a resolution, not a new thread, even when its phrasing is uncertain rather "
                        "than triumphant. Never set chapter_introduced to the manuscript's own final "
                        "chapter unless the concern truly has no earlier textual setup at all. Return ONLY "
                        "JSON: {\"threads\":[{\"id\":\"<short slug>\",\"thread_type\":"
                        "\"character_fate|unpunished_enabler|promised_consequence|open_mystery|"
                        "unfulfilled_scene\","
                        "\"description\":\"<one-line: what is left open>\",\"chapter_introduced\":<int>,"
                        "\"quote\":\"<short excerpt from THIS chapter, copied character-for-character "
                        "from the book text, that establishes the thread — never paraphrased>\"}]} — "
                        "at most 8 threads. If you find more than 8: first note every candidate "
                        "across the WHOLE book, then choose your final list of up to 8 by taking "
                        "roughly equal numbers from the book's first third, middle third, and final "
                        "third of chapters -- do NOT simply keep the first 8 you noticed while "
                        "reading front-to-back, since that silently drops threads seeded or paid off "
                        "later in the book. Empty list if nothing qualifies.")
                    try:
                        _tt_max_tok1 = int(os.environ.get("NARASI_THREAD_TRACKER_MAX_TOKENS", "3000"))
                        _tt_multipass = str(os.environ.get(
                            "NARASI_THREAD_TRACKER_MULTIPASS", "0")).strip().lower() in ("1", "true", "yes", "on")
                        # MULTIPASS (new flag, default OFF -- this is a genuine 3x cost increase to
                        # Pass 1, not a free correctness fix, so it stays an explicit opt-in rather
                        # than silently tripling an already-live mechanism's cost): the root-cause
                        # investigation's PRIMARY finding was that a single exhaustive-enumeration
                        # call over a 44k-word/10-chapter book has an inherent recall ceiling
                        # (primacy bias -- it surfaces the earliest/most salient candidate and stops).
                        # Mirrors this session's adversarial-verify pattern: 3 independent extraction
                        # calls at different temperatures see different candidates; merge before
                        # Pass 2 does the real precision filtering (unchanged).
                        if _tt_multipass:
                            _tt_passes = await asyncio.gather(*[
                                _narasi_cheap_call(_tt_sys1, _ttbook[:_tt_max],
                                                    tenant_id=tenant_id, user_id=user_id,
                                                    job_uuid=job_uuid, json_mode=True,
                                                    max_tokens=_tt_max_tok1, temperature=_t,
                                                    credit_row=False)
                                for _t in (0.2, 0.5, 0.8)
                            ], return_exceptions=True)
                            _tt_seen_keys = set()
                            for _pi, _pass_result in enumerate(_tt_passes):
                                if isinstance(_pass_result, Exception):
                                    log.warning("thread-tracker pass-1 (multipass slot %d) failed "
                                                "(non-fatal): %s", _pi, _pass_result)
                                    continue
                                _tt_raw1, _tt_cc1 = _pass_result
                                if sink is not None and _tt_cc1:
                                    sink.credits += int(_tt_cc1)
                                _tt_d1 = _narasi_parse_json(_tt_raw1) if isinstance(_tt_raw1, str) else (_tt_raw1 or {})
                                if not (isinstance(_tt_d1, dict) and _tt_d1):
                                    log.warning("thread-tracker pass-1 (multipass slot %d) unparsable "
                                                "— raw head: %r", _pi, (_tt_raw1 or "")[:200])
                                    continue
                                for _t in ((_tt_d1.get("threads") or []) if isinstance(_tt_d1, dict) else []):
                                    if not (isinstance(_t, dict) and _t.get("id")):
                                        continue
                                    # dedupe on (chapter_introduced, thread_type, normalized
                                    # description) -- catches literal repeats across passes without
                                    # needing fuzzy matching; near-duplicate wording from different
                                    # passes is accepted (Pass 2 + the per-chapter revise merge both
                                    # tolerate redundant evidence for the same underlying thread).
                                    _tt_dkey = (_t.get("chapter_introduced"), _t.get("thread_type"),
                                                " ".join(str(_t.get("description") or "").lower().split()))
                                    if _tt_dkey in _tt_seen_keys:
                                        continue
                                    _tt_seen_keys.add(_tt_dkey)
                                    # FIX (audit finding): a monotonic counter (len(_tt_threads), the
                                    # count of threads ALREADY accepted) guarantees a unique id no
                                    # matter what slug the model returns -- the prior "p{pass}_{slug}"
                                    # scheme only prevented CROSS-pass collisions; two distinct threads
                                    # from the SAME pass reusing the same model-invented slug (different
                                    # descriptions, so they survive the dedup check above) still
                                    # collided, and the id-keyed dict below would silently drop one,
                                    # risking misattributed evidence in the emitted violation.
                                    _t["id"] = f"p{_pi}_{len(_tt_threads)}_{_t.get('id')}"
                                    _tt_threads.append(_t)
                            _tt_threads = _tt_threads[:16]
                            log.info("thread-tracker pass-1 (multipass): %d/%d successful pass(es), "
                                     "%d unique thread(s) after dedup",
                                     sum(1 for r in _tt_passes if not isinstance(r, Exception)),
                                     len(_tt_passes), len(_tt_threads))
                        else:
                            _tt_raw1, _tt_cc1 = await _narasi_cheap_call(_tt_sys1, _ttbook[:_tt_max],
                                                                         tenant_id=tenant_id, user_id=user_id,
                                                                         job_uuid=job_uuid, json_mode=True,
                                                                         max_tokens=_tt_max_tok1,
                                                                         credit_row=False)
                            if sink is not None and _tt_cc1:
                                sink.credits += int(_tt_cc1)
                            _tt_d1 = _narasi_parse_json(_tt_raw1) if isinstance(_tt_raw1, str) else (_tt_raw1 or {})
                            if not (isinstance(_tt_d1, dict) and _tt_d1):
                                log.warning("thread-tracker pass-1 unparsable — raw head: %r", (_tt_raw1 or "")[:200])
                            _tt_threads = [t for t in ((_tt_d1.get("threads") or []) if isinstance(_tt_d1, dict) else [])
                                           if isinstance(t, dict) and t.get("id")][:8]
                            # FIX (same id-collision class caught in the multipass path's audit): a
                            # single Pass-1 response can itself return 2+ threads that reuse the same
                            # model-invented slug (different descriptions, so nothing upstream
                            # dedupes them) -- rewrite with a monotonic index so the id-keyed dict
                            # built further down can never silently drop one.
                            for _tti, _tt_t in enumerate(_tt_threads):
                                _tt_t["id"] = f"s{_tti}_{_tt_t.get('id')}"
                    except Exception as _tte1:  # noqa: BLE001
                        log.warning("thread-tracker pass-1 extraction failed (non-fatal): %s", _tte1)
                        _tt_threads = []
                    _tt_unresolved: list = []
                    if _tt_threads:
                        _tt_tail_n = int(os.environ.get("NARASI_THREAD_TRACKER_TAIL_CHARS", "15000"))
                        _tt_tail = _ttbook[-_tt_tail_n:]
                        _tt_sys2 = (
                            "You are given a list of THREADS extracted from earlier in a manuscript, "
                            "and the manuscript's ENDING (final chapter(s) only). For each thread, "
                            "decide whether the ending resolves it, plausibly leaves it open on "
                            "purpose, or simply never addresses it. Only flag a thread as unresolved "
                            "if the ending NEITHER resolves it NOR plausibly leaves it open on purpose "
                            "— when genuinely unsure, do NOT flag it. Treat a hedged-but-stated future "
                            "as RESOLVED, not unresolved: a stated medical prognosis even if recovery "
                            "is slow or uncertain ('would fade over years'), a legal/institutional "
                            "process explicitly described as underway, or a physical/environmental "
                            "harm explicitly acknowledged as still-present-but-being-addressed ('might "
                            "never fully lift'). ALSO treat as RESOLVED a character who was missing, "
                            "captive, or silent and reappears on the page giving a full account/"
                            "testimony of what happened to them, even if a further legal step (a "
                            "charge, a trial) is still pending — the reappearance and account are the "
                            "resolution; the pending legal step is a separate, expected open thread, "
                            "not evidence this one is unresolved. These deliberately open-ended, "
                            "non-triumphant closings are how real-world harms are resolved in literary "
                            "fiction — do not flag them for lacking a definitive, tidy outcome. But do "
                            "NOT extend this leniency to a thread the ending merely gestures at without "
                            "actually addressing — a vague, hopeful aside naming no concrete step, no "
                            "named outcome, and no connection to the specific harm or culpability the "
                            "thread raised earlier is NOT a resolution and should still be flagged. "
                            "THREADS: "
                            + json.dumps([{"id": t.get("id"), "thread_type": t.get("thread_type"),
                                           "description": t.get("description")} for t in _tt_threads],
                                         ensure_ascii=False)
                            + ". Return ONLY JSON: {\"unresolved\":[{\"id\":\"<thread id from the list "
                            "above>\",\"why\":\"<one-line: what the ending fails to address>\"}]} — "
                            "empty list if the ending accounts for every thread.")
                        try:
                            _tt_max_tok2 = int(os.environ.get("NARASI_THREAD_TRACKER_MAX_TOKENS2", "1500"))
                            _tt_raw2, _tt_cc2 = await _narasi_cheap_call(_tt_sys2, _tt_tail,
                                                                         tenant_id=tenant_id, user_id=user_id,
                                                                         job_uuid=job_uuid, json_mode=True,
                                                                         max_tokens=_tt_max_tok2,
                                                                         credit_row=False)
                            if sink is not None and _tt_cc2:
                                sink.credits += int(_tt_cc2)
                            _tt_d2 = _narasi_parse_json(_tt_raw2) if isinstance(_tt_raw2, str) else (_tt_raw2 or {})
                            if not (isinstance(_tt_d2, dict) and _tt_d2):
                                log.warning("thread-tracker pass-2 unparsable — raw head: %r", (_tt_raw2 or "")[:200])
                            _tt_unresolved = [u for u in ((_tt_d2.get("unresolved") or []) if isinstance(_tt_d2, dict) else [])
                                              if isinstance(u, dict) and u.get("id")]
                        except Exception as _tte2:  # noqa: BLE001
                            log.warning("thread-tracker pass-2 verification failed (non-fatal): %s", _tte2)
                            _tt_unresolved = []
                    _tt_by_id = {t.get("id"): t for t in _tt_threads}
                    _tt_violations = []
                    for _u in _tt_unresolved:
                        _t = _tt_by_id.get(_u.get("id"))
                        if not _t:
                            continue
                        _tt_ev = _wq(_t.get("quote"))
                        _tt_chn = _t.get("chapter_introduced")
                        if _tt_chn is not None:
                            _tt_ev = f"{_tt_ev} @ch{_tt_chn}"
                        _tt_ev = _tt_ev[:200]
                        _tt_violations.append({
                            "type": "unresolved_thread", "severity": "high",
                            "evidence": _tt_ev,
                            "fix": (f"Chapter {_t.get('chapter_introduced')} raises "
                                    f"{str(_t.get('thread_type') or '').replace('_', ' ')} "
                                    f"({_t.get('description')}) but the ending never addresses it — "
                                    f"{_u.get('why')}. Add a brief beat in the final chapter(s) that "
                                    f"resolves or explicitly closes this thread.")})
                    result["thread_tracker"] = {"threads_checked": len(_tt_threads), "violations": _tt_violations}
                    _eligible = []
                    if _tt_violations:
                        log.warning("thread-tracker: %d unresolved thread(s) flagged for job %s (report-only): %s",
                                    len(_tt_violations), job_id, [str(v)[:90] for v in _tt_violations[:5]])
                        if str(os.environ.get("NARASI_THREAD_TRACKER_ENFORCE", "0")).strip().lower() in ("1", "true", "yes", "on"):
                            _eligible = _tt_violations
                    else:
                        log.info("thread-tracker: 0 unresolved thread(s) across %d extracted for job %s (clean run)",
                                  len(_tt_threads), job_id)
                    _out.update({"ran": True, "eligible": _eligible})
        except Exception as e:  # noqa: BLE001
            log.warning("thread-tracker gate failed (non-fatal): %s", e)
        return _out

    def _v3g_thread_tracker_finalize(_out, _changed):
        if _out.get("ran") and _out.get("eligible"):
            if _changed:
                log.info("thread-tracker enforce: %d violation(s) sent to revise — book updated",
                         len(_out["eligible"]))
            else:
                log.info("thread-tracker enforce: %d violation(s) sent — revise landed nothing",
                         len(_out["eligible"]))

    # ── run the four independent detections concurrently, then merge into ONE revise ──
    _crit_out, _register_out, _canon_out, _tt_out = await asyncio.gather(
        _v3g_critic_detect(), _v3g_register_detect(), _v3g_canon_diff_detect(), _v3g_thread_tracker_detect())

    _v3g_merged = (
        list(_crit_out.get("eligible") or [])
        + list(_register_out.get("eligible") or [])
        + list(_canon_out.get("eligible") or [])
        + list(_tt_out.get("eligible") or []))

    _v3g_changed = False
    _v3g_t_rev = 0.0
    if _v3g_merged:
        try:
            from laozhang_api import _narasi_consistency_revise
            _v3g_t_rev0 = time.monotonic()
            _v3g_new, _v3g_cr = await _narasi_consistency_revise(
                _gbook0, {"violations": _v3g_merged}, style, language,
                model=(body.get("model") or ""),
                tenant_id=tenant_id, user_id=user_id, job_uuid=job_uuid, credit_row=False)
            _v3g_t_rev = time.monotonic() - _v3g_t_rev0
            # P0A: recorded HERE, inside the branch that actually ran a revise — reading
            # the variable after the block would report the 0.0 initialiser as a
            # measured zero on every job whose gates found nothing to revise.
            if p0a_timings is not None:
                p0a_timings["revise"] = _v3g_t_rev
            if sink is not None and _v3g_cr:
                sink.credits += int(_v3g_cr)
            if _v3g_new and _v3g_new != _gbook0:
                result[_gk] = _v3g_new
                _v3g_changed = True
        except Exception as _v3ge:  # noqa: BLE001
            log.warning("merged gate revise failed (non-fatal): %s", _v3ge)

    _v3g_critic_finalize(_crit_out, _v3g_changed, _v3g_t_rev)
    _v3g_register_finalize(_register_out, _v3g_changed)
    _v3g_canon_diff_finalize(_canon_out, _v3g_changed)
    _v3g_thread_tracker_finalize(_tt_out, _v3g_changed)


    # ── (2.7) R-FG9/R-FG10 fact scan — scan-and-report on the FINAL text (post-gates,
    # pre-header). Report-only by spec ("scan first, block second"); regime = style
    # default, job-overridable via body.factual_regime (refactor §4).
    try:
        import narasi_factscan as _nfs
        regime = _effective_regime(body)
        # Biopic rule (precedence §1): a named historical person floats person-claim
        # scanning to hybrid even when the style regime is fictional.
        if body.get("_person_floor") == "hybrid" and regime == "fictional":
            regime = "hybrid"
        _book = result.get("book") or result.get("output") or ""
        if _book:
            result["fact_report"] = _nfs.fact_scan(_book, factual_regime=regime, lang=language)
            _of = result["fact_report"].get("overfiring")
            if _of:
                log.warning("fact-scan detectors over-firing (tune before enforcement): %s", _of)
            # FG-SEARCH verify pass (dormant: FACTGATE_SEARCH_ENABLED=0 / keyless →
            # PROVIDER_DOWN → NEEDS-VERIFY). Report-only per §6; writes the cache stores.
            if regime == "strict":
                try:
                    import narasi_verify as _nv
                    if _nv.verify_enabled():
                        if job_id:
                            await _safe_progress(job_id, "Fact verification …")
                        # gap_fill claims (§2 file-2): a claim ADDED to satisfy a
                        # previously-flagged gap is guilty-until-verified. Sources:
                        # (1) outline's angka_tesis strings (final chapter's payoff stats)
                        # (2) body._gap_fill_claims (reviewer/CI-injected: "the manuscript
                        # is missing X" beats added on the next draft). Mandatory-search.
                        _gap: list = []
                        for ch in (body.get("chapters") or []):
                            for k in ("angka_tesis", "gap_fill", "required_beat"):
                                v = (ch or {}).get(k) if isinstance(ch, dict) else None
                                if isinstance(v, str) and v.strip():
                                    _gap.append(v.strip())
                                elif isinstance(v, list):
                                    _gap.extend(x for x in v if isinstance(x, str) and x.strip())
                        for x in (body.get("_gap_fill_claims") or []):
                            if isinstance(x, str) and x.strip():
                                _gap.append(x.strip())
                        result["fact_report"]["verify"] = await _nv.verify_report(
                            result["fact_report"], project_id=body.get("project_id"),
                            tenant_id=tenant_id,
                            lang=str(language or "en"),
                            gap_fill_claims=_gap or None)
                except Exception as _ve:  # noqa: BLE001
                    log.warning("verify pass failed (non-fatal): %s", _ve)
            # Regime-mismatch detector (§4, warn-only): a FICTIONAL job dense with real
            # anchors probably meant hybrid/strict. Cheap call; log always.
            if regime == "fictional" and str(os.environ.get("NARRATION_MISMATCH_DETECT", "1")).strip().lower() not in ("0", "false", "no", "off"):
                try:
                    from laozhang_api import _narasi_cheap_call, _narasi_parse_json
                    _sys2 = ("Count REAL-WORLD anchors in this fiction: recognizable real persons, "
                             "places, events, institutions. Return ONLY JSON "
                             "{\"real_persons\": [str], \"real_anchor_count\": int}")
                    raw2, _c2 = await _narasi_cheap_call(_sys2, _book[:12000], tenant_id=tenant_id,
                                                         user_id=user_id, json_mode=True)
                    d2 = _narasi_parse_json(raw2) if isinstance(raw2, str) else {}
                    persons2 = (d2 or {}).get("real_persons") or []
                    anchors = int((d2 or {}).get("real_anchor_count") or 0)
                    per_1k = anchors / max(1, len(_book.split()) / 1000.0)
                    if len(persons2) > 3 or per_1k > 8:
                        result["regime_mismatch_warn"] = {
                            "real_persons": persons2[:6], "anchors_per_1000w": round(per_1k, 1),
                            "message": "Fictional job references substantial real-world material "
                                       "and none of it is verified — did you mean hybrid/strict?"}
                        log.warning("regime-mismatch WARN: %s", result["regime_mismatch_warn"])
                except Exception as _me:  # noqa: BLE001
                    log.warning("mismatch detector failed (non-fatal): %s", _me)
    except Exception as e:  # noqa: BLE001
        log.warning("fact scan failed (non-fatal): %s", e)

    # ── (2.9) ID-path §7: number rendering keyed to Output — video = speakable spelled
    # forms (uniform; ends the '"seribu delapan ratus…" beside "1827"' mix), book = digits.
    try:
        if _mode == "video" and str(language or "").split("-")[0].lower() == "id":
            from narasi_counters import render_numbers_id as _rn
            key = "book" if result.get("book") else "output"
            book = result.get(key) or ""
            if book:
                rendered, n_sp = _rn(book)
                result[key] = rendered
                for rec in result.get("chapters") or []:
                    if rec.get("content"):
                        rec["content"], _n2 = _rn(rec["content"])
                result["number_rendering"] = {"path": "video", "spelled": n_sp}
    except Exception as e:  # noqa: BLE001
        log.warning("number rendering failed (non-fatal): %s", e)

    # §1: the manifest built at job start ships in the editor report.
    if body.get("_gates_manifest"):
        result["gates_manifest"] = body["_gates_manifest"]

    # ── (3) Gaya metadata header (matches the classic stitch header; gate-whitelisted) ──
    try:
        key = "book" if result.get("book") else "output"
        book = result.get(key) or ""
        try:
            from laozhang_api import (_resolve_narasi_lang, _narasi_header_labels,
                                       _NARASI_HEADER_LABELS, _retrofit_legacy_chapter_labels)
            _hdr_prefixes = tuple(f"> **{_v['style']}:**" for _v in _NARASI_HEADER_LABELS.values())
        except Exception:  # noqa: BLE001
            _resolve_narasi_lang = lambda x: x
            _narasi_header_labels = lambda _l: {"style": "Style", "output": "Output",
                                                 "language": "Language", "words": "words",
                                                 "note": "Note", "alt": "alternate history"}
            _hdr_prefixes = ("> **Gaya:**", "> **Style:**")
            _retrofit_legacy_chapter_labels = lambda md, _l: md
        # Retrofit legacy "## Bab N:" chapter headers (pre-2026-07-05 stored markdown baked
        # them in regardless of narrative language). Skip if the book already has the right
        # prefix — no-op is safe.
        book = _retrofit_legacy_chapter_labels(book, language)
        result[key] = book
        # HEADER-RESTAMP (flag NARASI_HEADER_RESTAMP, default OFF): the header is normally WRITE-ONCE
        # (the `not ...startswith(_hdr_prefixes)` guard), so on a resume/re-gate — or if a later gate
        # trims the body after the stamp — the "N words" count goes stale (claim > actual). When ON,
        # strip an existing header and ALWAYS recompute+restamp on the final body so claim == actual.
        # The strip is anchored to the first "\n\n---\n\n" within 600 chars AND only when the header
        # prefix matches at lstrip-start, so it can never clip real body. OFF ⟹ original behavior.
        _restamp = os.environ.get("NARASI_HEADER_RESTAMP", "0").strip().lower() in ("1", "true", "yes", "on")
        if _restamp and book and book.lstrip().startswith(_hdr_prefixes):
            _sep_i = book.find("\n\n---\n\n")
            if 0 <= _sep_i < 600:
                book = book[_sep_i + len("\n\n---\n\n"):]
                result[key] = book
        if book and (_restamp or not book.lstrip().startswith(_hdr_prefixes)):
            try:
                lang_label = _resolve_narasi_lang(language)
            except Exception:  # noqa: BLE001
                lang_label = language
            # JJ (Phase 1 patch — DALANG_INFRA_FIXES): CJK tokenizer word_count fix.
            # `.split()` on CJK produces 1-3 tokens for entire narasi (sample-9 Salt 169字
            # claimed vs ~13k chars; sample-12 Sahara 143語 claimed vs ~10k chars). For CJK
            # scripts count printable-alpha chars instead; non-CJK unchanged.
            words = len(book.split())
            try:
                from narasi_gate import _INFRA_FIXES_ON as _phase1_on  # local import to keep cycle-free
            except Exception:  # noqa: BLE001
                _phase1_on = lambda: False  # noqa: E731
            if _phase1_on():
                _lang_code = str(language or "").strip().lower().replace("_", "-").split("-", 1)[0]
                if _lang_code in ("zh", "ja", "ko", "th"):
                    # Count Unicode alpha chars only — skips whitespace, punctuation,
                    # and the frontmatter markdown noise. Matches how CJK readers
                    # actually measure narasi length ("字/文字/字符").
                    words = sum(1 for _c in book if _c.isalpha())
            # Gaya shows the DISPLAY name ("Big History"), never the raw registry key
            # ("harari") the FE submits.
            _style_label = style or "narasi"
            try:
                from pakem import resolve_style as _rs3
                _style_label = (_rs3(style) or {}).get("display_name") or _style_label
            except Exception:  # noqa: BLE001
                pass
            # v4 §5: header gains the Output field so the editor/dual-path filters are auditable.
            _out_path = "video" if str(body.get("mode") or "").strip() == "video" else "book"
            # UUUUU (Phase 1 patch — DALANG_INFRA_FIXES): metadata output_type default bug.
            # CROSS-7-SAMPLE (sample-3 Oberon + sample-4 Flannan + sample-6 Vale + sample-7
            # Drifting-Station + sample-11 King-Rain-v2 + sample-12 Sahara + sample-10
            # Chicken) all mislabel non-video output as `Output: book`, including VO-first
            # styles (popular_science, natgeo, cinematic_voiceover). Consult the style's
            # pakem metadata: medium_origin=ear or output_support=video_only → emit
            # "narration". `body["mode"] == "video"` continues to force "video" for the
            # explicit VI path. Non-VO styles unchanged (default "book").
            if _phase1_on() and _out_path == "book":
                try:
                    from pakem import resolve_style as _rs_p1  # cycle-free local import
                    _sp = _rs_p1(style) or {}
                    _mo = str(_sp.get("medium_origin", "")).strip().lower()
                    _os = str(_sp.get("output_support", "")).strip().lower()
                    if _mo == "ear" or _os == "video_only":
                        _out_path = "narration"
                except Exception:  # noqa: BLE001
                    pass
            # Header labels rendered in the narrative's own language (id/en/es/fr/de/pt/nl/it/
            # ja/ko/zh/ar/hi/th/vi/ms/jv/su/tl); unknown language → English fallback.
            _lbl = _narasi_header_labels(language)
            _alt = f" | **{_lbl['note']}:** {_lbl['alt']}" if body.get("alt_history") else ""
            result[key] = (f"> **{_lbl['style']}:** {_style_label} | **{_lbl['output']}:** {_out_path} | "
                           f"**{_lbl['language']}:** {lang_label} | **{words} {_lbl['words']}**{_alt}\n\n---\n\n") + book
    except Exception as e:  # noqa: BLE001
        log.warning("Gaya header failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Terminal / persistence helpers — all best-effort.
# ---------------------------------------------------------------------------
def _result_payload(result: dict) -> dict:
    """The durable result_payload stored on the jobs row. Keep it bounded so we
    don't bloat the row with megabytes — the full chapters live in
    narasi_chapters; here we keep the assembled markdown + run metadata."""
    book = result.get("book") or result.get("output") or ""
    return {
        "markdown": book,
        "scenario": result.get("scenario"),
        "strategy": result.get("strategy"),
        "polished": bool(result.get("polished")),
        "rag_used": bool(result.get("rag_used")),
        "n_ok": result.get("n_ok"),
        "n_total": result.get("n_total"),
        "settings": result.get("settings"),
        "outline_source": result.get("outline_source"),
        # CC v3/v4 reports (bounded dicts; absent when the gates didn't run)
        "gate_report": result.get("gate_report"),
        "register_gate": result.get("register_gate"),
        "counter_report": result.get("counter_report"),
        "fact_report": result.get("fact_report"),
        # Consistency-critic verdict (bounded: score + <=20 violations + <=600-char
        # summary, per _narasi_normalize_critique). Absent when the critic didn't run.
        # Persisted so a low score can be classified post-hoc (violations were log-only).
        "critique": result.get("critique"),
        # Canon-diff verdict (Phase 2b, bounded: <=20 forks). Absent unless NARASI_CANON_DIFF ran.
        "canon_diff": result.get("canon_diff"),
        # Unresolved-thread tracker verdict (bounded: <=8 threads checked/flagged). Absent
        # unless NARASI_THREAD_TRACKER ran.
        "thread_tracker": result.get("thread_tracker"),
        # ID-path fixes: §1 manifest + §5 entity report + §7 rendering stats
        "gates_manifest": result.get("gates_manifest"),
        "entity_report": result.get("entity_report"),
        "number_rendering": result.get("number_rendering"),
        # phantom-name scan (0.75) — bounded: <=8 names, one short snippet each
        "phantom_name_report": result.get("phantom_name_report"),
    }


async def _safe_progress(job_id: str, msg: str) -> None:
    try:
        await rc.set_progress(job_id, msg, ttl=_CHAPTERS_TTL)
    except Exception:  # noqa: BLE001
        pass


async def _reconcile_checkboxes(job_id: str, result: dict, total: int) -> None:
    """Make the checkbox hash agree with the final chapter records (in case a
    worker telemetry event was missed)."""
    chapters = result.get("chapters")
    if not isinstance(chapters, list):
        return
    r = await _redis()
    if r is None:
        return
    try:
        key = _chapters_key(job_id)
        ndone = 0
        for rec in chapters:
            no = int(rec.get("no", 0))
            state = _STATUS_DONE if rec.get("ok") else _STATUS_FAILED
            await r.hset(key, f"chapter:{no}", state)
            ndone += 1
        await r.hset(key, "done", str(ndone))
        await r.expire(key, _CHAPTERS_TTL)
    except Exception as e:  # noqa: BLE001
        log.debug("reconcile_checkboxes(%s) failed: %s", job_id, e)


async def _persist_chapters(tenant_id: str, job_uuid: Optional[str], result: dict) -> None:
    """Write each chapter to narasi_chapters (durable read-back). Idempotent on
    (job_id, chapter_index). Skips if we have no internal job UUID (RLS needs it)."""
    if not job_uuid:
        return
    chapters = result.get("chapters")
    if not isinstance(chapters, list):
        return
    for rec in chapters:
        try:
            content = rec.get("content") or ""
            wc = len((content or "").split())
            await db.save_narasi_chapter(
                tenant_id, job_uuid, int(rec.get("no", 0)), content,
                word_count=wc, source_prompt="", retrieved_ids=[],
                version=1, approved=False)
        except Exception as e:  # noqa: BLE001
            log.warning("persist chapter %s failed (non-fatal): %s", rec.get("no"), e)


async def _finalize(job_id: str, job_uuid: Optional[str], tenant_id: str, *,
                    status: str, result: Optional[dict], error: Optional[str]) -> None:
    """Write the terminal status to BOTH Redis (fast) and the jobs row (durable)."""
    await _set_status(job_id, status)
    try:
        await rc.set_progress(
            job_id,
            {"done": "Done", "failed": f"Failed: {error}",
             "cancelled": "Cancelled"}.get(status, status),
            ttl=_CHAPTERS_TTL)
    except Exception:  # noqa: BLE001
        pass
    try:
        # Pass the dict RAW — the pool's jsonb codec encodes it. json.dumps here would
        # DOUBLE-ENCODE (payload stored as a JSON string → result_payload->>'markdown'
        # NULL, every consumer needs a defensive json.loads). Same rule as
        # rcs-ledger-metadata-double-encode.
        await db.finish_narasi_job(
            tenant_id, job_id, _DB_STATUS.get(status, "error"),
            result=result, error=error)
    except Exception as e:  # noqa: BLE001
        log.warning("finish_narasi_job(%s,%s) failed (non-fatal): %s", job_id, status, e)


async def _settle(meter_op: Optional[str], tenant_id: str, user_id: Optional[str],
                  model: str, job_uuid: Optional[str], sink: _UsageSink) -> None:
    """Settle the credit hold at the ACTUAL blended cost. The sink accumulates the
    real per-call cost across every model used (workers AND a possibly different,
    pricier manager), so we settle from that true USD — NOT by re-pricing the whole
    token total at the single worker `model` (which undercharges when the manager
    model is more expensive)."""
    if not meter_op:
        return
    # A4: never settle DELIVERED work at usd=0 — a 0.0 cost makes charge.settle() treat the
    # job as free and FULL-REFUND the hold. sink.cost_usd is 0 only when every model name
    # missed the pricing table (an operator routing to a genuinely new model family). Floor
    # from the token totals at a conservative blended rate so the platform recovers
    # something and the hold isn't wiped; log loudly so the misconfig is visible.
    _usd = float(sink.cost_usd or 0.0)
    if _usd <= 0.0 and (sink.tokens_out or 0) > 0:
        _usd = (int(sink.tokens_in) * 1.0 + int(sink.tokens_out) * 5.0) / 1_000_000.0
        log.warning("settle(%s): sink priced 0 for %d out tokens (pricing-table miss for model=%s?) "
                    "— flooring usd=%.5f to avoid a free-book full refund",
                    meter_op, sink.tokens_out, model, _usd)
    try:
        charge = metering.Charge(
            tenant_id=tenant_id, user_id=user_id, op_id=meter_op,
            operation="narasi", model=model, held=0)
        # Catalog parity: sink.credits priced EVERY call at catalog rates (per model,
        # blended). The usd path priced from the orchestrator's provider table and
        # undercharged ~4x (itaatga7: 488 vs catalog 1988). usd stays as fallback + the
        # COGS number on the usage row.
        # zero_usage_totals=True: the ⚡ sink already writes its own per-call
        # usage_logs row for every CallTelemetry (_UsageSink.__call__ → _log_one →
        # db.log_usage, credits=0 each). Without this flag, Charge.settle()'s
        # generic "always record real totals" write adds a 22nd phantom row
        # repeating the job's aggregate tok_in/tok_out/cost_usd as if it were one
        # more upstream call, double-counting those columns on any downstream
        # usage_logs aggregation. The settle row is still WRITTEN (not skipped) —
        # only its tok_in/tok_out/cost_usd are logged as 0 — so `credits` (and the
        # revenue_idr/markup_factor/GL codes database.log_usage derives from it)
        # still lands correctly; only the per-call-duplicating fields are zeroed.
        # Credits/GL are unaffected regardless (commit() uses the in-process
        # sink.credits scalar, never SUMs usage_logs).
        await charge.settle(
            {"tokens_in": sink.tokens_in, "tokens_out": sink.tokens_out},
            job_id=job_uuid, tok_in=sink.tokens_in, tok_out=sink.tokens_out,
            usd=_usd, credits_actual=(sink.credits if (sink.credits or 0) > 0 else None),
            zero_usage_totals=True)
    except Exception as e:  # noqa: BLE001
        log.warning("settle hold(%s) failed (non-fatal): %s", meter_op, e)


async def _refund(meter_op: Optional[str], tenant_id: str, job_id: str) -> None:
    """Refund the unused hold (cancel / failure / zero output)."""
    if not meter_op:
        return
    try:
        await credits_lib.refund(tenant_id, meter_op)
    except Exception as e:  # noqa: BLE001
        log.warning("refund hold(%s) for %s failed (non-fatal): %s", meter_op, job_id, e)


# ===========================================================================
# Endpoints — the ONE job contract. Registered on the shared laozhang_api.app.
# ===========================================================================
# ── CONTENT-SAFETY SCOPE MARKER (regime-precedence spec §5) ──────────────────
# The fact-gate + living-person guard reduce ACCURACY and DEFAMATION exposure. They do
# NOT cover content safety: harmful-instruction-in-fiction, medical misinformation framed
# as story ("ramuan X menyembuhkan Y" in a dongeng), or platform-policy violations wrapped
# in narrative. The strict regime was never a safety net for these; the fictional regime
# just makes the absence visible. This is a SEPARATE, currently-UNBUILT layer — owner
# decision pending (Rino). Do not mistake the guards below for covering it.
# ─────────────────────────────────────────────────────────────────────────────


def _effective_regime(body: dict) -> str:
    """Precedence chain floor input (regime-precedence spec §1): job override else style
    default. The living-guard and person-floor sit ABOVE this.
    Normalizes the registry-P1 spelling 'fiction' (kdrama_serial / romance_contemporary /
    remaja_coming_of_age) to canonical 'fictional' (registry.py schema-v2 domain:
    strict | hybrid | fictional) ON THE STYLE-DEFAULT BRANCH ONLY: every downstream
    comparison tests == "fictional", so the raw 'fiction' value ran the FULL nonfiction
    fact scan on fiction manuscripts (job eky9gcge) and skipped the mismatch detector +
    biopic person-floor. An EXPLICIT body override of 'fiction' keeps its legacy
    semantics (unknown value → fall through to the style default) so this bug fix
    cannot widen which job overrides are honored. mythic_history / mythic_narrative
    pass through unchanged."""
    r = str(body.get("factual_regime") or "").strip().lower()
    if r in ("strict", "hybrid", "fictional"):
        return r
    try:
        from pakem import resolve_style
        s = str((resolve_style(str(body.get("style") or "")) or {}).get("factual_regime", "strict"))
        return "fictional" if s.strip().lower() == "fiction" else s
    except Exception:  # noqa: BLE001
        return "strict"


async def _living_person_guard(body: dict, tenant_id) -> None:
    """Regime-precedence spec §2 — BLOCKING, pre-generation, zero tokens spent on a
    blocked job. Fires only when regime ∈ {fictional, hybrid} and a living/recently-
    deceased real person is the subject/central character. Deaths are monotonic, so the
    model's knowledge suffices for 'historical'; uncertain → treated as LIVING
    (fail-closed on status). Internal detector errors fail-OPEN (log, never block users
    on our bug). Kill switch NARRATION_LIVING_GUARD=0; admin override via
    LIVING_GUARD_ADMIN_TENANTS + body.override_living_guard (always logged)."""
    if str(os.environ.get("NARRATION_LIVING_GUARD", "1")).strip().lower() in ("0", "false", "no", "off"):
        return
    regime = _effective_regime(body)
    if regime == "strict":
        return
    if body.get("override_living_guard"):
        admins = {t.strip() for t in os.environ.get("LIVING_GUARD_ADMIN_TENANTS", "").split(",") if t.strip()}
        if str(tenant_id) in admins:
            log.warning("living-guard OVERRIDDEN by admin tenant %s (job topic: %.60s)",
                        tenant_id, str(body.get("topic") or ""))
            return
    try:
        text = " | ".join(filter(None, [
            str(body.get("topic") or ""), str(body.get("brief") or "")[:800],
            " ; ".join(f"{c.get('title','')}: {c.get('summary', c.get('description',''))}"
                       for c in (body.get("chapters") or [])[:20] if isinstance(c, dict))[:1200],
        ]))[:2500]
        if not text.strip():
            return
        from laozhang_api import _narasi_cheap_call, _narasi_parse_json  # lazy
        _sys = "You are a legal-risk screener for a fiction/video-narasi product. You read a short story brief (in any language) and identify whether it is written ABOUT one or more SPECIFIC, REAL, IDENTIFIABLE public figures. Your output gates a defamation check, so you must be PRECISE: flagging invented characters breaks the product (false positive), and missing a real public figure creates legal exposure (false negative). Most briefs contain NO real people \u2014 an empty list is very often the correct answer.\n\nOUTPUT\nReturn ONLY a single JSON object, no prose, no markdown, no code fences:\n{\"persons\":[{\"name\":str,\"role\":\"subject|central|minor\",\"status\":\"living|recently_deceased|historical|unsure\"}]}\nIf no real identifiable person is present, return exactly {\"persons\":[]}.\n\nWHAT COUNTS AS A REAL, IDENTIFIABLE PERSON (list ONLY these)\nList a person ONLY if the brief pins them to one SPECIFIC actual real-world public figure \u2014 a particular human the average reader could name and point to. It qualifies when at least one is true:\n1. FULL REAL NAME of a known public figure (e.g., \"Taylor Swift\", \"Barack Obama\", \"Prabowo Subianto\", \"Elon Musk\", \"Soekarno\").\n2. TITLE/ROLE + NAME that identifies exactly one actual person (e.g., \"Queen Elizabeth II\", \"President Prabowo\", \"the singer Adele\").\n3. UNMISTAKABLE REAL-WORLD CONTEXT that leaves no doubt which real person is meant, even if the name is partial \u2014 e.g., a single name plus a real, specific, verifiable biographical anchor (\"Beyonc\u00e9\", \"Rihanna\", \"Messi at the 2022 World Cup final\", \"Barack's presidency\", \"Elon's SpaceX launch\"). The anchor must tie to real, public, factual events or works \u2014 NOT to an invented everyday life.\nThe test is IDENTIFIABILITY: could an ordinary reader point to one specific real human this story is about? If it could be anyone, do not list.\n\nWHAT IS FICTIONAL (never list these). This holds in EVERY language.\n- A character known only by a COMMON FIRST NAME (Julian, Sarah, David, Budi, Ani, Dilan, Milea, Yoon Jae-won, Han So-yi) paired with an INVENTED EVERYDAY ROLE or ordinary life (architect, florist, student, barista, high-schooler, teacher, \"anak SMA\", \"anak motor\", office worker, neighbor). This is an invented character even if the name coincides with a real person's, and even if it echoes an existing novel/film/song. A common first name never identifies a specific real public figure on its own. No full real-world identifier = fictional. Do NOT list them \u2014 not as minor, not as unsure.\n- Any character with no name (generic unnamed roles: \"the two leads\", \"a narrator\", \"a soldier\", \"the mother\", \"two high-school kids\").\n- Characters that merely ECHO characters from another novel, film, myth, or franchise. Only real, living-or-once-living PEOPLE count; a borrowed-feeling fictional character is not a real public figure.\n- Invented characters remain fictional no matter how central they are to the plot.\n\nNON-PERSON ENTITIES\nBands, brands, companies, teams, franchises, songs, places, and fictional universes are NOT persons. \"A Coldplay song plays\", \"they drive a Tesla\", \"set in a Marvel-style world\" \u2014 none of these is a person. Do not list them at all.\n\nMENTIONS vs SUBJECTS\nA real person, band, or brand mentioned only in PASSING (a song on the radio, a logo, background scenery) is NOT the subject or central figure, and its presence does NOT pull invented leads into the list. Never list a passing mention as subject/central. A song/band merely playing in a scene is scenery \u2014 omit it entirely. You may list a genuinely-named passing real HUMAN as role=\"minor\" only if the story truly concerns them; when unsure whether a passing real person matters, prefer omitting.\n\nCORE RULE\nIdentifiability, not centrality, decides listing. A common first name + invented occupation is fictional. Only a specific, verifiable real public figure is listed. When a brief only echoes a known work through common first names (no full real name, no real-world anchor), treat the characters as invented and list nothing. When genuinely torn between \"invented character with a real-sounding name\" and \"real public figure\" with no concrete identifier (A/B/C), PREFER treating it as fictional and skip it.\n\nROLE (only for people you list)\n- subject: the story is primarily ABOUT this real person (their life, diary, days, biography; \"reimagining X's diary\", \"the last days of X\", \"the youth of X\").\n- central: a real person is a main character/protagonist but the premise is a fictional/alternate scenario around them (\"Obama discovers he is a wizard\", \"Prabowo as a teacher\").\n- minor: a real, named human genuinely appears but is secondary. (Passing scenery like a background song is not a person and is not listed.)\n\nSTATUS (only for people you list) \u2014 judge when the real person died relative to today.\n- living: alive now.\n- recently_deceased: died within roughly the last 20 years.\n- historical: died more than roughly 20 years ago (e.g., Soekarno, d. 1970).\n- unsure: use ONLY when you are confident the person is a real, identifiable figure but cannot resolve their identity or life status. NEVER use unsure to hedge a common-first-name invented character into the list \u2014 those are simply omitted.\n\nPROCEDURE\n1. Extract every named human in the brief.\n2. For each, ask: does a full real name, a title+name, or unmistakable real-world context pin ONE specific actual public figure? If NO \u2192 drop it (fictional). If YES \u2192 keep it.\n3. Assign role by how central the real person is to the premise.\n4. Assign status.\n5. Output the JSON. Empty list if nothing qualifies.\n\nWORKED EXAMPLES (follow exactly)\n- \"Yoon Jae-won, an architect, and Han So-yi, a florist, restore an old house.\" -> {\"persons\":[]}\n- \"Julian walks through the door; the narrator recalls their breakup in London.\" -> {\"persons\":[]}\n- \"Budi dan Ani, dua anak SMA, pelan-pelan jatuh cinta.\" -> {\"persons\":[]}\n- \"A reimagining of Taylor Swift's secret diary during her Eras tour.\" -> {\"persons\":[{\"name\":\"Taylor Swift\",\"role\":\"subject\",\"status\":\"living\"}]}\n- \"Barack Obama discovers he is a wizard on his 40th birthday.\" -> {\"persons\":[{\"name\":\"Barack Obama\",\"role\":\"central\",\"status\":\"living\"}]}\n- \"The last days of Queen Elizabeth II, imagined.\" -> {\"persons\":[{\"name\":\"Elizabeth II\",\"role\":\"subject\",\"status\":\"recently_deceased\"}]}\n- \"The youth of Soekarno before independence.\" -> {\"persons\":[{\"name\":\"Soekarno\",\"role\":\"subject\",\"status\":\"historical\"}]}\n- \"Prabowo Subianto as a high-school teacher in an alternate 2024.\" -> {\"persons\":[{\"name\":\"Prabowo Subianto\",\"role\":\"central\",\"status\":\"living\"}]}\n- \"Dilan, an anak motor in Bandung 1990, falls for Milea.\" -> {\"persons\":[]}\n- \"In a cafe, someone plays a Coldplay song while the two leads (invented) talk.\" -> {\"persons\":[]}\n\nOutput the JSON object now. Nothing else."
        raw, _cr = await _narasi_cheap_call(_sys, text, tenant_id=tenant_id, user_id=None,
                                            json_mode=True)
        d = _narasi_parse_json(raw) if isinstance(raw, str) else (raw or {})
        persons = (d or {}).get("persons") or []
        hits, historical = [], []
        for p in persons:
            if not isinstance(p, dict):
                continue
            status = str(p.get("status") or "unsure").lower()
            role = str(p.get("role") or "minor").lower()
            # unsure no longer HARD-blocks (corpus: fiction char 'Julian' false-positived a
            # whole job). Only CONFIRMED living / recently-deceased SUBJECT/CENTRAL block;
            # unsure floors the regime to hybrid like historical. Restore strict fail-closed
            # with NARRATION_LIVING_GUARD_UNSURE_BLOCKS=1.
            _block_statuses = (("living", "recently_deceased", "unsure")
                               if os.environ.get("NARRATION_LIVING_GUARD_UNSURE_BLOCKS") == "1"
                               else ("living", "recently_deceased"))
            if status in _block_statuses and role in ("subject", "central"):
                hits.append(p.get("name") or "?")
            elif status in ("historical", "unsure"):
                historical.append(p.get("name") or "?")
        if hits:
            log.warning("living-person guard BLOCKED job (regime=%s): %s", regime, hits)
            raise HTTPException(422, {
                "error": "living_person_guard",
                "persons": hits[:5],
                "message": ("Cerita fiksi/hybrid tentang tokoh nyata yang masih hidup (atau baru "
                            "wafat) diblokir. Dua jalur: (1) jadikan komposit — ganti nama & "
                            "samarkan detail identitas, atau (2) tulis sebagai nonfiksi strict "
                            "dengan fact-gate penuh (set factual_regime: strict)."),
            })
        # Biopic rule (§1): named HISTORICAL person + fictional regime → person-claims
        # float to hybrid; the world stays unverified. alt_history lowers it for the dead.
        if historical and regime == "fictional" and not body.get("alt_history"):
            body["_person_floor"] = "hybrid"
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — detector bug must never block a user
        log.warning("living-person guard failed OPEN (non-fatal): %s", e)


def _narration_admit(body: dict) -> None:
    """CC v3 admission for the ⚡ engine — mirrors the classic _narasi_admit caps (same
    env-tunable constants): ≤20 chapters, ≤8,000 words/chapter, ≤120,000 words total.
    Previously /narration/start had NO cap (the classic caps live on /narasi/*), so a
    direct API caller could exceed them. Raises HTTPException(400)."""
    try:
        from laozhang_api import (DALANG_MAX_CHAPTERS, DALANG_MAX_TOTAL_WORDS,
                                  DALANG_MAX_WORDS_PER_CHAPTER)  # lazy: import-order safe
    except Exception:  # noqa: BLE001 - never block a job on an import hiccup
        return
    chapters = None
    for key in ("chapters", "outline", "titles"):
        v = body.get(key)
        if isinstance(v, list) and v:
            chapters = v
            break
    n = len(chapters) if chapters else 0
    if not n:
        try:
            n = int(body.get("n_chapters") or body.get("num_chapters") or 1)
        except (TypeError, ValueError):
            n = 1
    if n > DALANG_MAX_CHAPTERS:
        raise HTTPException(400, f"too many chapters: {n} > {DALANG_MAX_CHAPTERS}")
    total = 0
    for c in chapters or []:
        if not isinstance(c, dict):
            continue
        try:
            w = int(c.get("word_target") or c.get("words") or 800)
        except (TypeError, ValueError):
            w = 800
        if w > DALANG_MAX_WORDS_PER_CHAPTER:
            raise HTTPException(400, f"chapter words {w} > {DALANG_MAX_WORDS_PER_CHAPTER}")
        total += max(0, w)
    if total > DALANG_MAX_TOTAL_WORDS:
        raise HTTPException(400, f"total words {total} > {DALANG_MAX_TOTAL_WORDS}")


def _count_chapters(body: dict) -> int:
    """Best-effort estimate of how many chapters the run will produce, so we can
    seed the right number of checkboxes up front. Mirrors the router's shape
    inspection: an explicit list wins; else n_chapters; else 1."""
    for key in ("chapters", "outline", "titles"):
        v = body.get(key)
        if isinstance(v, list) and v:
            return len(v)
    for key in ("n_chapters", "num_chapters"):
        v = body.get(key)
        try:
            if v and int(v) > 0:
                return int(v)
        except (TypeError, ValueError):
            pass
    return 1


@app.post("/narration", status_code=202)
async def narration_start(body: dict, background: BackgroundTasks,
                          user: CurrentUser = Depends(get_current_user)):
    """Start a unified narration job. Returns 202 immediately with the job id.

    Body is the orchestrator request (topic / chapters / brief / goal / style /
    language / mode / n_chapters / ...). The SHAPE drives routing — see
    orchestrator.router. We add the production envelope: Redis checkboxes, a
    credit hold, a durable jobs row, and a background generation task.
    """
    tenant_id = user.tenant_id
    try:
        user_uuid = await _resolve_user_uuid(user.tenant_id, user.user_id)
    except Exception:  # noqa: BLE001
        user_uuid = None

    body = dict(body or {})
    _narration_admit(body)   # CC v3: ⚡ caps (chapters/words) — 400 BEFORE any hold
    await _living_person_guard(body, user.tenant_id)   # §2: blocking, pre-hold, pre-tokens
    # ID-path §1: gates-active manifest — BEFORE the hold, zero tokens on a job whose
    # enforcement state can't be fully resolved. UNMEASURED/n-a are honest states and
    # pass; a rule with NO state fails the start (silent absence is the killed class).
    try:
        import narasi_manifest as _nm
        _entry = None
        try:
            from pakem import resolve_style as _rs_m
            _entry = _rs_m(str(body.get("style") or ""))
        except Exception:  # noqa: BLE001
            _entry = None
        body["_gates_manifest"] = _nm.build_manifest(
            style_entry=_entry, lang=str(body.get("language") or "id"),
            regime=_effective_regime(body),
            mode="video" if str(body.get("mode") or "").strip() == "video" else "book",
            style=str(body.get("style") or ""))
    except Exception as _me:
        # ManifestError = deliberate fail-closed; anything else must not block a job.
        import narasi_manifest as _nm2
        if isinstance(_me, _nm2.ManifestError):
            raise HTTPException(422, {"error": "gates_manifest", "message": str(_me)})
        log.warning("gates manifest build failed (non-fatal): %s", _me)
    # BullMQ S3 fairness: cap concurrent narasi jobs per tenant (0 = off, default).
    _cap = int(os.environ.get("NARRATION_MAX_ACTIVE_PER_TENANT", "0") or 0)
    if _cap > 0:
        try:
            if await db.count_active_narasi_jobs(tenant_id) >= _cap:
                raise HTTPException(429, f"too many narrations in flight (max {_cap}) — "
                                         "wait for one to finish")
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 - never block on a count hiccup
            pass
    job_id = (str(body.get("pre_job_id") or uuid.uuid4().hex[:8]))[:16]
    topic = str(body.get("topic") or body.get("goal") or body.get("brief") or "").strip()
    model = str(body.get("worker_model") or os.environ.get("WORKER_MODEL")
                or "gemini-2.5-flash").strip()
    total = _count_chapters(body)

    # ── Credit HOLD up front (HTTP 402 if short). BYOK pays upstream → no hold. ──
    # A6: price the hold at the model the workers will ACTUALLY run on. Manager-routed
    # styles (harari/academic-popular/literary-essay) route their worker to
    # MANAGER_MODEL=claude-sonnet-4-6 (~14× the gemini `model` estimate); pricing the hold
    # at the cheap `model` under-reserves, then the F4 clamp caps the debit at the too-small
    # hold and the platform eats the delta. Resolve the routed model here so the hold covers
    # the real blended cost. (settle still bills the sink's true per-call USD.)
    _style_for_hold = str(body.get("style") or "").strip()
    hold_model = model
    try:
        from orchestrator.core import route_model as _route_model
        hold_model = _route_model(role="worker", style=_style_for_hold,
                                  override=body.get("worker_model")) or model
    except Exception:  # noqa: BLE001
        hold_model = model
    try:
        is_byok = bool(_byok())
    except Exception:  # noqa: BLE001
        is_byok = False
    meter_op = None
    try:
        if not is_byok:
            # Hold shape must track REALITY or the F4 clamp (settle ≤ hold) silently
            # under-bills: itaatga7 actually consumed ~150k in / ~40k out (shared prefix
            # ~12k×chapter + continuations + gates) but the old 1500×n/words×2 estimate
            # held only 488cr where the catalog said 1988 — settle got clamped to the
            # hold. Tunable without deploy: NARASI_HOLD_TOKENS_IN_PER_CH /
            # NARASI_HOLD_OUT_MULT. Unused hold is refunded at settle as always.
            _in_per_ch = int(os.environ.get("NARASI_HOLD_TOKENS_IN_PER_CH", "13000"))
            _out_mult = float(os.environ.get("NARASI_HOLD_OUT_MULT", "3.0"))
            _total_words = sum(
                int((c.get("word_target") or c.get("words") or 800))
                for c in (body.get("chapters") or [{}] * total)) or (800 * total)
            est_units = {
                "tokens_in": _in_per_ch * max(1, total),
                "tokens_out": int(_total_words * _out_mult),
            }
            meter_op = f"narration:{job_id}:{uuid.uuid4().hex[:8]}"
            await metering.begin_charge(
                tenant_id=tenant_id, user_id=user_uuid, operation="narasi",
                model=hold_model, estimate_units=est_units, op_id=meter_op)
    except HTTPException:
        raise  # 402 surfaces to the client untouched
    except Exception as e:  # noqa: BLE001 - never let a metering hiccup block a job
        log.warning("narration hold skipped (non-fatal): %s", e)
        meter_op = None

    # ── Durable jobs row (poll can see it immediately) ──
    # A1 (crash-safe billing, mirrors classic laozhang_api narasi_start): stamp the hold's
    # op_id into input_payload._meter so the orphan sweep (narasi_jobs_sweep_stale / 0054)
    # can settle/refund the hold after a crash — without it a SIGKILL/OOM/redeploy mid-run
    # strands the hold ~6h AND leaks the per-tenant active cap via the stuck-'processing'
    # row. Gated on DALANG_CRASHSAFE_ENABLED exactly like the classic callsite.
    job_uuid = None
    try:
        _ckpt_op = None
        try:
            from laozhang_api import _dalang_crashsafe_enabled as _cse  # lazy — no top-level cycle
            _ckpt_op = meter_op if _cse() else None
        except Exception:  # noqa: BLE001
            _ckpt_op = None
        await db.create_narasi_job(tenant_id, user_uuid, job_id, topic, total, op_id=_ckpt_op)
        _row = await db.get_job_by_external(tenant_id, job_id)
        job_uuid = _row.get("id") if _row else None
    except Exception as e:  # noqa: BLE001
        log.warning("create narration job row failed (non-fatal): %s", e)

    # ── Seed the per-chapter checkbox hash (expire 1h) + clear any stale cancel ──
    try:
        await rc.clear_cancel(_cancel_token(job_id))
    except Exception:  # noqa: BLE001
        pass
    await _init_checkboxes(job_id, total)
    await _safe_progress(job_id, "Starting narration…")

    # ── Kick off generation; return the id immediately ──
    # ── BullMQ S1 (NARRATION_BULLMQ_ENABLED, default OFF): enqueue to the durable
    # `narration` queue instead of running in-process — the narration-worker service
    # picks it up (survives API restarts; S2 resume continues checkpointed chapters).
    # Any enqueue failure falls back to the in-process path (never lose a job).
    # A8: a BYOK job must NEVER be enqueued. The worker runs in a separate process that
    # cannot reconstruct the per-request BYOK key (a contextvar), so it would generate on
    # the PLATFORM key while meter_op=None means _settle never runs — platform pays the full
    # upstream cost and recovers nothing. BYOK always runs in-process.
    _bullmq_on = str(os.environ.get("NARRATION_BULLMQ_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    _p0a_tried_bullmq = bool(_bullmq_on and not is_byok)
    if _bullmq_on and not is_byok:
        _enq_ok = False
        try:
            from bullmq import Queue as _BullQueue
            _q = _BullQueue(os.environ.get("NARRATION_QUEUE", "narration"),
                            {"connection": os.environ.get("REDIS_URL", "redis://localhost:6379")})
            try:
                await _q.add("narration", {
                    "job_id": job_id, "job_uuid": job_uuid, "tenant_id": tenant_id,
                    "user_id": user_uuid, "total": total, "meter_op": meter_op,
                    "model": model, "body": body,
                }, {"jobId": job_id, "removeOnComplete": True, "attempts": 2})
                _enq_ok = True   # the job is durably enqueued the instant add() returns
            finally:
                try:
                    await _q.close()   # a close() error must NOT trigger the in-process fallback
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            log.warning("bullmq enqueue failed — falling back in-process: %s", e)
        # A9: only fall through to the in-process path if the ADD itself failed. Enqueued +
        # in-process = the same job_id runs twice (doubled COGS, duplicate usage_logs,
        # racing Redis/gate state) even though customer credits stay op_id-idempotent.
        if _enq_ok:
            # Enqueued IS the dispatch outcome — a Queue.close() failure after a
            # successful add stays bullmq_worker, matching production behaviour. Handed
            # to the framework, so it runs after the response is sent.
            _p0a_route(background, "bullmq_worker", body, total, job_id)
            return {"ok": True, "job_id": job_id, "status": _STATUS_RUNNING,
                    "total": total, "queued": True}

    _p0a_route(background, "api_fallback" if _p0a_tried_bullmq else "api_direct",
               body, total, job_id)
    asyncio.create_task(_run_narration_job(
        body=body, job_id=job_id, job_uuid=job_uuid,
        tenant_id=tenant_id, user_id=user_uuid, total=total,
        meter_op=meter_op, model=model, executor="python_api",
    ))
    return {"ok": True, "job_id": job_id, "status": _STATUS_RUNNING, "total": total}


@app.get("/narration/queue/health")
async def narration_queue_health(user: Optional[CurrentUser] = Depends(get_current_user_optional)):
    """BullMQ S4: queue depth/health for ops. `enabled` mirrors the S1 flag; counts are
    best-effort (absent when bullmq isn't installed or the flag is off)."""
    enabled = str(os.environ.get("NARRATION_BULLMQ_ENABLED", "0")).strip().lower() in ("1", "true", "yes", "on")
    out: dict = {"enabled": enabled, "queue": os.environ.get("NARRATION_QUEUE", "narration")}
    if enabled:
        try:
            from bullmq import Queue as _BullQueue
            _q = _BullQueue(out["queue"], {"connection": os.environ.get("REDIS_URL", "redis://localhost:6379")})
            out["counts"] = await _q.getJobCounts("waiting", "active", "failed", "delayed")
            await _q.close()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
    return out


@app.get("/narration/{job_id}")
async def narration_status(job_id: str, user: CurrentUser = Depends(get_current_user)):
    """Poll a narration job. Redis (fast, per-chapter checkboxes) first; falls back
    to the durable jobs row when the hash has expired. Tenant-scoped via RLS."""
    status, done, total, chapters = await _read_checkboxes(job_id)

    # Durable row (source of truth for terminal state + the assembled output).
    row = None
    try:
        row = await db.get_job_by_external(user.tenant_id, job_id)
    except Exception as e:  # noqa: BLE001
        log.warning("narration_status get_job(%s) failed: %s", job_id, e)
    if not row and not chapters:
        raise HTTPException(404, "job not found")

    # Prefer the durable terminal status when the job has finished; otherwise the
    # live Redis status (running/polishing).
    #
    # RACE FIX (Rino "kejadian lagi" — b92lvku8 blank output): `_set_status` writes to
    # REDIS first, then `finish_narasi_job` writes the DB status + result_payload later.
    # If the FE polled during the ~100ms window between those, Redis said "done" but the
    # DB row still had `status='processing'` and NULL `result_payload`. Old code:
    # eff_status = status or _RUNNING → "done" — but out["output"] stayed undefined
    # (no dict payload yet). FE saw status=done + empty output → setNarasi("") → blank.
    # New rule: NEVER report `done` unless the DB row has BOTH status=done AND a
    # non-empty result_payload. Otherwise stay `polishing`, let the FE keep polling.
    db_status = (row or {}).get("status")
    _payload_ready = bool(row and row.get("result_payload"))
    eff_status = status or _STATUS_RUNNING
    if db_status in ("done", "error", "cancelled"):
        eff_status = {"done": _STATUS_DONE, "error": _STATUS_FAILED,
                      "cancelled": _STATUS_CANCELLED}.get(db_status, eff_status)
    elif status == _STATUS_DONE and not _payload_ready:
        # Redis says done but DB is mid-commit → downgrade so the FE keeps polling
        # instead of resolving with an empty output.
        eff_status = _STATUS_POLISHING

    if row:
        total = total or int(row.get("progress_total") or 0)
        done = done or int(row.get("progress_current") or 0)

    result = (row or {}).get("result_payload")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:  # noqa: BLE001
            result = {"markdown": result}

    out: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "status": eff_status,
        "done": done,
        "total": total,
        "chapters": chapters,
        "progress": None,
        "error": (row or {}).get("error_message"),
        "found": True,
    }
    try:
        out["progress"] = await rc.get_progress(job_id)
    except Exception:  # noqa: BLE001
        pass
    if eff_status == _STATUS_DONE and isinstance(result, dict):
        out["output"] = result.get("markdown")
        out["result"] = result
    return out


@app.post("/narration/{job_id}/cancel")
async def narration_cancel(job_id: str, user: CurrentUser = Depends(get_current_user)):
    """Request cancellation. The runtime stops after the in-flight chapter, marks
    the job cancelled, and refunds the unused credit hold. Tenant-scoped."""
    row = None
    try:
        row = await db.get_job_by_external(user.tenant_id, job_id)
    except Exception:  # noqa: BLE001
        row = None
    if not row:
        # Still allow setting the flag if the live checkbox hash exists (the row
        # may not be readable, but a running job should still be cancellable).
        _, _, total, chapters = await _read_checkboxes(job_id)
        if not chapters:
            raise HTTPException(404, "job not found")
    try:
        await rc.set_cancel(_cancel_token(job_id))
    except Exception as e:  # noqa: BLE001
        log.warning("set_cancel(%s) failed: %s", job_id, e)
    await _set_status(job_id, _STATUS_CANCELLED)
    return {"ok": True, "status": "cancel_requested", "job_id": job_id}


# ---------------------------------------------------------------------------
# BYOK detection — reuse the laozhang_api helper if importable; else env fallback.
# Kept tiny + local so this module imports even when laozhang_api's BYOK plumbing
# isn't fully wired in a given environment.
# ---------------------------------------------------------------------------
def _byok() -> bool:
    try:
        from laozhang_api import _byok_active  # type: ignore
        return bool(_byok_active())
    except Exception:  # noqa: BLE001
        return False


__all__ = ["narration_start", "narration_status", "narration_cancel"]
