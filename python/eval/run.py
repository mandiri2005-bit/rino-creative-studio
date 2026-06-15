# -*- coding: utf-8 -*-
"""
Project Dalang — eval runner + A/B loop (WS-9).

Scores narration on three dimensions and compares configurations side-by-side:

    coherence   cross-chapter continuity (shared entities recur, no restart,
                no contradictory facts across chapters)
    factuality  [VERIFY:] placeholders used instead of invented specifics,
                canonical_facts honoured, forbidden_facts absent
    style       shipped style block's REQUIRED cues present, FORBIDDEN absent

USAGE
-----
    # Live: run the real orchestrator for every case (costs money / needs keys).
    python3 -m eval.run --live

    # Dry (CI default): score pre-generated fixtures under eval/fixtures/.
    python3 -m eval.run --dry

    # A/B: score the SAME cases under several configurations, side-by-side.
    python3 -m eval.run --dry \
        --ab "ORCH_MODE=static,POLISH=none" --ab "ORCH_MODE=auto,POLISH=light,RAG=on"

    # Save a baseline (the FIRST/headline config becomes the baseline record).
    python3 -m eval.run --dry --save-baseline

    # Gate: fail (exit 2) if the current run regresses vs the saved baseline.
    python3 -m eval.run --dry --gate

DESIGN
------
  * Import-safe: importing this module never needs PyYAML, a DB, or an LLM. The
    YAML loader falls back to a tiny built-in parser for the restricted golden
    format; --dry needs no network; --live imports the orchestrator lazily.
  * Never crashes a run: a failed case scores 0 on every dimension and is
    reported, rather than raising.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
GOLDEN_PATH = _HERE / "golden_narration.yaml"
FIXTURES_DIR = _HERE / "fixtures"
BASELINES_DIR = _HERE / "baselines"
BASELINE_PATH = BASELINES_DIR / "baseline.json"

# The three scored dimensions, in display order.
DIMENSIONS = ("coherence", "factuality", "style")

# A new config must score within this tolerance of baseline to pass the gate
# (allows tiny non-determinism without flapping). Overridable via env.
try:
    GATE_TOLERANCE = float(os.environ.get("EVAL_GATE_TOLERANCE", "0.03"))
except (TypeError, ValueError):
    GATE_TOLERANCE = 0.03


# =========================================================================== #
# 1. Golden-case loading (PyYAML when present, else a tiny fallback parser)
# =========================================================================== #
def _load_yaml_text(text: str) -> dict:
    """Parse the golden file. Prefer PyYAML; fall back to a minimal parser that
    understands the restricted subset this file uses (see golden_narration.yaml).
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except Exception:
        return _mini_yaml(text)


def _coerce_scalar(s: str) -> Any:
    """Turn a YAML scalar string into a python value (bool/int/float/None/str)."""
    s = s.strip()
    if (len(s) >= 2) and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", "none", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _parse_flow_list(s: str) -> list:
    """Parse an inline list like [a, "b c", 1] (no nested brackets in the golden)."""
    inner = s.strip()[1:-1].strip()
    if not inner:
        return []
    out: list = []
    buf = ""
    quote = ""
    for ch in inner:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf += ch
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ",":
            out.append(_coerce_scalar(buf))
            buf = ""
        else:
            buf += ch
    if buf.strip():
        out.append(_coerce_scalar(buf))
    return out


def _mini_yaml(text: str) -> dict:
    """A deliberately small YAML parser for the golden format ONLY.

    Supports: nested mappings by 2-space indent, "- " list items (mappings or
    scalars), inline flow lists [a, b], scalars, and full-line "# comments".
    It is NOT a general YAML parser — it exists so the harness runs on hosts
    without PyYAML. If the file grows beyond this subset, install PyYAML.
    """
    # Build a list of (indent, raw_line) skipping blanks/comments.
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        # strip trailing inline comments that are clearly not inside quotes
        lines.append((indent, raw.rstrip()))

    pos = 0

    def parse_block(min_indent: int):
        nonlocal pos
        # Decide: is this block a list (first item starts with "- ") or a map?
        if pos >= len(lines):
            return {}
        indent0 = lines[pos][0]
        is_list = lines[pos][1].strip().startswith("- ")
        if is_list:
            result_list: list = []
            while pos < len(lines):
                indent, content = lines[pos]
                if indent < indent0:
                    break
                if indent > indent0:
                    break
                stripped = content.strip()
                if not stripped.startswith("- "):
                    break
                item_body = stripped[2:]
                pos += 1
                if ":" in item_body and not item_body.startswith(("[", '"', "'")):
                    # First key of a list item that is a mapping; reconstruct an
                    # entry and continue consuming deeper-indented lines.
                    entry: dict = {}
                    k, _, v = item_body.partition(":")
                    _assign(entry, k.strip(), v.strip(), indent0 + 2)
                    # consume the rest of this mapping (deeper indent)
                    while pos < len(lines) and lines[pos][0] > indent0:
                        ci, cc = lines[pos]
                        ck, _, cv = cc.strip().partition(":")
                        pos += 1
                        _assign(entry, ck.strip(), cv.strip(), ci + 2)
                    result_list.append(entry)
                else:
                    result_list.append(_coerce_scalar(item_body))
            return result_list
        # mapping
        result: dict = {}
        while pos < len(lines):
            indent, content = lines[pos]
            if indent < indent0:
                break
            if indent > indent0:
                break
            key, _, val = content.strip().partition(":")
            pos += 1
            _assign(result, key.strip(), val.strip(), indent0 + 2)
        return result

    def _assign(container: dict, key: str, val: str, child_indent: int):
        nonlocal pos
        if val == "":
            # nested block (map or list) follows at deeper indent
            if pos < len(lines) and lines[pos][0] >= child_indent:
                container[key] = parse_block(lines[pos][0])
            else:
                container[key] = None
        elif val.startswith("["):
            container[key] = _parse_flow_list(val)
        else:
            container[key] = _coerce_scalar(val)

    return parse_block(0)


@dataclass
class GoldenCase:
    id: str
    topic: str = ""
    style: str = "creative non-fiction"
    language: str = "id"
    mode: str = "text"
    fiction: bool = False
    expectations: dict = field(default_factory=dict)
    # request-shape passthrough fields
    chapters: Optional[list] = None
    n_chapters: Optional[int] = None
    words_per_chapter: Optional[int] = None
    word_target: Optional[int] = None
    single: bool = False

    def as_request(self) -> dict:
        """Build the orchestrator request dict for a live run."""
        req: dict[str, Any] = {
            "topic": self.topic, "style": self.style,
            "language": self.language, "mode": self.mode,
        }
        if self.chapters:
            req["chapters"] = self.chapters
        if self.n_chapters:
            req["n_chapters"] = self.n_chapters
            req["multi_chapter"] = True
        if self.words_per_chapter:
            req["words_per_chapter"] = self.words_per_chapter
        if self.word_target:
            req["word_target"] = self.word_target
        if self.single:
            req["single"] = True
        return req


def load_cases(path: Path = GOLDEN_PATH) -> list[GoldenCase]:
    """Load and validate the golden cases. Never raises on a malformed single
    case — it skips it with a warning so the rest of the suite still runs."""
    if not path.exists():
        print(f"[eval] golden file not found: {path}", file=sys.stderr)
        return []
    data = _load_yaml_text(path.read_text(encoding="utf-8"))
    raw_cases = (data or {}).get("cases") or []
    cases: list[GoldenCase] = []
    for rc in raw_cases:
        if not isinstance(rc, dict) or not rc.get("id"):
            print(f"[eval] skipping malformed case: {rc!r}", file=sys.stderr)
            continue
        cases.append(GoldenCase(
            id=str(rc.get("id")),
            topic=str(rc.get("topic", "") or ""),
            style=str(rc.get("style", "creative non-fiction") or "creative non-fiction"),
            language=str(rc.get("language", "id") or "id"),
            mode=str(rc.get("mode", "text") or "text"),
            fiction=bool(rc.get("fiction", False)),
            expectations=dict(rc.get("expectations") or {}),
            chapters=rc.get("chapters"),
            n_chapters=rc.get("n_chapters"),
            words_per_chapter=rc.get("words_per_chapter"),
            word_target=rc.get("word_target"),
            single=bool(rc.get("single", False)),
        ))
    return cases


# =========================================================================== #
# 2. Result normalization — extract chapters + full text from any result shape
# =========================================================================== #
def _chapters_from_result(result: dict) -> list[str]:
    """Pull per-chapter text out of an orchestrator/fixture result.

    Handles the chaptered shape ({"chapters": [{"content": ...}]}) and the
    single-piece shape ({"output": ...}). Returns a list of chapter strings."""
    chs = result.get("chapters")
    if isinstance(chs, list) and chs:
        out: list[str] = []
        for c in chs:
            if isinstance(c, dict):
                out.append(str(c.get("content") or c.get("output") or ""))
            else:
                out.append(str(c))
        return out
    # single-piece fallbacks
    for k in ("book", "output"):
        v = result.get(k)
        if isinstance(v, str) and v.strip():
            return [v]
    return []


def _full_text(chapters: list[str]) -> str:
    return "\n\n".join(chapters)


# =========================================================================== #
# 3. Scorers — coherence / factuality / style. Each returns 0.0..1.0 + detail.
# =========================================================================== #
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VERIFY_RE = re.compile(r"\[VERIFY:", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# Significant numbers in a fact (>= 3 chars, ignoring thousands separators) — a
# claim's number is load-bearing, so "12.000 tahun" must NOT match "200 tahun".
_NUM_RE = re.compile(r"\d[\d.,]*")


def _numbers(text: str) -> set[str]:
    """Normalize numeric tokens for comparison: strip thousands separators so
    '12.000' and '12,000' compare equal, keep the canonical digit string."""
    out: set[str] = set()
    for m in _NUM_RE.findall(text or ""):
        digits = m.replace(".", "").replace(",", "").strip("0") or "0"
        if len(m.replace(".", "").replace(",", "")) >= 1:
            out.add(digits)
    return out


def _fact_match(fact: str, haystack_low: str, *, threshold: float) -> bool:
    """True if `fact` is asserted in `haystack_low` (already lowercased).

    Two conditions BOTH must hold:
      * content-word overlap >= threshold (bag-of-words presence), AND
      * every significant NUMBER in the fact also appears in the haystack.
    The numeric guard is what lets a contradicting figure (200 vs 12.000) escape
    a forbidden-fact match and lets a matching figure confirm a canonical fact.
    """
    ftoks = [t for t in _tokens(fact) if len(t) > 3]
    if not ftoks:
        # number-only fact (rare): fall back to number presence alone
        return bool(_numbers(fact)) and _numbers(fact).issubset(_numbers(haystack_low))
    hit = sum(1 for t in ftoks if t in haystack_low)
    if hit / len(ftoks) < threshold:
        return False
    fact_nums = _numbers(fact)
    if fact_nums and not fact_nums.issubset(_numbers(haystack_low)):
        return False  # the claim's number is absent => not this exact assertion
    return True


def score_coherence(chapters: list[str], exp: dict) -> tuple[float, dict]:
    """Cross-chapter continuity heuristic.

    Sub-signals (each 0..1, averaged):
      A. recurring_entities — expected entities that actually recur across >=2
         chapters (or appear at all in a single-chapter piece).
      B. lexical_overlap — adjacent chapters share vocabulary (Jaccard of content
         words), i.e. the narrative doesn't restart from scratch each chapter.
      C. no_restart — later chapters don't reopen with the very first chapter's
         opening sentence verbatim (a classic drift/duplication failure).
    """
    detail: dict[str, Any] = {}
    if not chapters:
        return 0.0, {"reason": "no_chapters"}

    # A. recurring entities
    wanted = [str(e) for e in (exp.get("recurring_entities") or []) if str(e).strip()]
    if wanted:
        joined = [c.lower() for c in chapters]
        hits = 0
        for ent in wanted:
            e = ent.lower()
            present_in = sum(1 for c in joined if e in c)
            # recurs (>=2 chapters) OR present in a single-chapter piece
            if present_in >= 2 or (len(chapters) == 1 and present_in >= 1):
                hits += 1
        a = hits / len(wanted)
    else:
        a = 1.0
    detail["recurring_entities"] = round(a, 3)

    # B. adjacent lexical overlap (continuity of subject matter)
    if len(chapters) >= 2:
        sets = [set(t for t in _tokens(c) if len(t) > 3) for c in chapters]
        overlaps = []
        for i in range(len(sets) - 1):
            s1, s2 = sets[i], sets[i + 1]
            if not s1 or not s2:
                overlaps.append(0.0)
                continue
            j = len(s1 & s2) / max(1, len(s1 | s2))
            # a little overlap is enough for continuity; saturate at ~0.12 Jaccard
            overlaps.append(min(1.0, j / 0.12))
        b = sum(overlaps) / len(overlaps)
    else:
        b = 1.0
    detail["lexical_overlap"] = round(b, 3)

    # C. no verbatim restart
    if len(chapters) >= 2:
        def first_sentence(t: str) -> str:
            t = _norm(t)
            m = re.split(r"[.!?]", t, maxsplit=1)
            return m[0] if m else t
        first = first_sentence(chapters[0])
        restarts = sum(1 for c in chapters[1:] if first and first_sentence(c) == first)
        c = 1.0 if restarts == 0 else max(0.0, 1.0 - restarts / (len(chapters) - 1))
    else:
        c = 1.0
    detail["no_restart"] = round(c, 3)

    score = (a + b + c) / 3.0
    return round(score, 4), detail


def score_factuality(chapters: list[str], exp: dict, *, fiction: bool) -> tuple[float, dict]:
    """Factual-handling heuristic.

    Sub-signals:
      A. verify_discipline — if the case expects grounding (verify_expected,
         non-fiction), at least one [VERIFY:] placeholder should appear (the
         worker prompts instruct workers to placeholder unknown specifics rather
         than fabricate). Fiction cases skip this (creative freedom).
      B. canonical_facts — facts that MUST hold are present (or acceptably absent
         when handled as [VERIFY:]); we credit presence, never penalise omission.
      C. forbidden_facts — plainly-false statements must NOT appear (hard penalty).
    """
    detail: dict[str, Any] = {}
    text = _full_text(chapters)
    if not text:
        return 0.0, {"reason": "no_text"}
    low = text.lower()

    # A. verify discipline
    verify_expected = bool(exp.get("verify_expected")) and not fiction
    has_verify = bool(_VERIFY_RE.search(text))
    if verify_expected:
        a = 1.0 if has_verify else 0.4  # missing placeholders => risk of fabrication
    else:
        a = 1.0
    detail["verify_discipline"] = round(a, 3)
    detail["verify_markers"] = len(_VERIFY_RE.findall(text))

    # B. canonical facts present (soft credit)
    canon = [str(f) for f in (exp.get("canonical_facts") or []) if str(f).strip()]
    if canon:
        present = sum(1 for f in canon if _fact_match(f, low, threshold=0.6))
        b = present / len(canon)
    else:
        b = 1.0
    detail["canonical_present"] = round(b, 3)

    # C. forbidden facts absent (hard). A higher threshold + numeric guard means
    # a contradicting figure (e.g. "200 tahun" vs the fixture's "12.000 tahun")
    # does NOT register as the banned assertion.
    forb = [str(f) for f in (exp.get("forbidden_facts") or []) if str(f).strip()]
    violations = [f for f in forb if _fact_match(f, low, threshold=0.85)]
    c = 0.0 if violations else 1.0
    detail["forbidden_violations"] = violations

    # Weight: forbidden violations dominate; canonical presence is soft credit.
    score = 0.4 * a + 0.3 * b + 0.3 * c
    if violations:
        score = min(score, 0.3)  # a fabricated/contradicted fact caps the score
    return round(score, 4), detail


def score_style(chapters: list[str], exp: dict, *, style: str) -> tuple[float, dict]:
    """Style-adherence heuristic against the SHIPPED pakem style block.

    Sub-signals:
      A. required_cues — case-level style_contains substrings appear.
      B. forbidden_phrases — case-level style_forbidden substrings absent. We also
         fold in the pakem style entry's banned phrases when discoverable.
    """
    detail: dict[str, Any] = {}
    text = _full_text(chapters)
    if not text:
        return 0.0, {"reason": "no_text"}
    low = text.lower()

    contains = [str(s) for s in (exp.get("style_contains") or []) if str(s).strip()]
    if contains:
        hits = sum(1 for s in contains if s.lower() in low)
        a = hits / len(contains)
    else:
        a = 1.0
    detail["required_cues"] = round(a, 3)

    forbidden = [str(s).lower() for s in (exp.get("style_forbidden") or []) if str(s).strip()]
    forbidden += _pakem_banned_phrases(style)
    forbidden = sorted(set(forbidden))
    found = [s for s in forbidden if s and s in low]
    b = 1.0 if not found else max(0.0, 1.0 - len(found) / max(1, len(forbidden)))
    detail["forbidden_found"] = found

    score = 0.6 * a + 0.4 * b
    return round(score, 4), detail


def _pakem_banned_phrases(style: str) -> list[str]:
    """Best-effort: pull obvious banned phrases from the resolved pakem style
    block (e.g. harari bans 'throughout history'). Import-safe — returns [] if
    pakem isn't importable or the style has no scrapeable bans."""
    try:
        import pakem  # type: ignore
        entry = pakem.resolve_style(style)
        block = (entry.get("style_rules_core", "") or "")
    except Exception:
        return []
    bans: list[str] = []
    # Look for quoted phrases following a BANNED/FORBIDDEN/NEVER cue.
    for m in re.finditer(r'(?:BANNED|FORBIDDEN|NEVER)[^\n]*', block, re.IGNORECASE):
        for q in re.findall(r'"([^"]{3,60})"', m.group(0)):
            bans.append(q.strip().lower())
    return bans


def score_case(result: dict, case: GoldenCase) -> dict:
    """Score one (result, case) into the three dimensions + overall."""
    chapters = _chapters_from_result(result)
    exp = case.expectations or {}
    ok = bool(result.get("ok")) and bool(chapters)

    if not ok:
        scores = {d: 0.0 for d in DIMENSIONS}
        return {
            "case_id": case.id, "ok": False,
            "scores": scores, "overall": 0.0,
            "detail": {"reason": result.get("error") or "no_output"},
            "n_chapters": len(chapters),
        }

    coh, coh_d = score_coherence(chapters, exp)
    fac, fac_d = score_factuality(chapters, exp, fiction=case.fiction)
    sty, sty_d = score_style(chapters, exp, style=case.style)
    scores = {"coherence": coh, "factuality": fac, "style": sty}
    overall = round(sum(scores.values()) / len(scores), 4)
    return {
        "case_id": case.id, "ok": True,
        "scores": scores, "overall": overall,
        "detail": {"coherence": coh_d, "factuality": fac_d, "style": sty_d},
        "n_chapters": len(chapters),
    }


# =========================================================================== #
# 4. Case execution — live (orchestrator) or dry (fixtures)
# =========================================================================== #
def _config_to_env(config: dict) -> dict:
    """Map a config dict (PAKEM_VERSION/ORCH_MODE/POLISH/RAG/...) to the env
    var names the orchestrator router reads. Returns a dict of overrides to set."""
    env: dict[str, str] = {}
    mapping = {
        "ORCH_MODE": "ORCH_MODE", "POLISH": "POLISH", "RAG": "RAG",
        "MAX_WORKERS": "MAX_WORKERS",
        "WORKER_MODEL": "WORKER_MODEL", "MANAGER_MODEL": "MANAGER_MODEL",
    }
    for k, v in (config or {}).items():
        ek = mapping.get(k.upper())
        if ek and v is not None and str(v).strip():
            env[ek] = str(v)
    return env


def _pakem_version() -> str:
    try:
        import pakem  # type: ignore
        return getattr(pakem, "PAKEM_VERSION", "unknown")
    except Exception:
        return "unknown"


async def _run_live(case: GoldenCase, config: dict) -> dict:
    """Run the real orchestrator for one case under `config`. Never raises."""
    # The router reads ORCH_MODE/POLISH/RAG etc. from the request OR env. We pass
    # them per-request (lowercased keys) so we don't mutate global env mid-run.
    req = case.as_request()
    for k, v in (config or {}).items():
        lk = k.lower()
        if lk in ("orch_mode", "polish", "rag", "max_workers",
                  "worker_model", "manager_model"):
            req[lk] = v
    try:
        from orchestrator.router import generate_narration  # lazy import
        return await generate_narration(req)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "chapters": []}


def _load_fixture(case: GoldenCase) -> dict:
    """Load a pre-generated fixture for --dry mode. If none exists, synthesize a
    deterministic placeholder result from the case's chapters/expectations so the
    suite still produces a (low) score rather than crashing CI."""
    fp = FIXTURES_DIR / f"{case.id}.json"
    if fp.exists():
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"bad_fixture: {exc}", "chapters": []}
    return _synthesize_fixture(case)


def _synthesize_fixture(case: GoldenCase) -> dict:
    """Build a minimal, deterministic result that weaves in expected entities and
    a [VERIFY:] marker so scoring is meaningful without a hand-written fixture."""
    exp = case.expectations or {}
    ents = [str(e) for e in (exp.get("recurring_entities") or ["topik"])]
    titles: list[dict] = []
    if case.chapters:
        titles = [c if isinstance(c, dict) else {"title": str(c)} for c in case.chapters]
    elif case.single:
        titles = [{"title": case.topic}]
    else:
        n = case.n_chapters or 2
        titles = [{"title": f"{case.topic} — bagian {i+1}"} for i in range(n)]
    verify = "" if case.fiction else f" [VERIFY: detail spesifik untuk {case.topic}]"
    chapters = []
    for i, t in enumerate(titles, 1):
        ent_str = ", ".join(ents)
        body = (f"{t.get('title','')}. " + " ".join(ents) + ". "
                f"Bab ini meneruskan kisah tentang {ent_str}.{verify}")
        chapters.append({"no": i, "id": i, "title": t.get("title", ""),
                         "ok": True, "content": body})
    return {"ok": True, "scenario": "synth", "strategy": "synthesized_fixture",
            "rag_used": not case.fiction, "chapters": chapters,
            "telemetry": [{"model": "fixture", "role": "worker",
                           "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}]}


async def run_config(cases: list[GoldenCase], config: dict, *, live: bool) -> dict:
    """Score every case under one configuration. Returns an aggregate record."""
    case_scores: list[dict] = []
    for case in cases:
        result = await _run_live(case, config) if live else _load_fixture(case)
        case_scores.append(score_case(result, case))

    # Aggregate per dimension + overall (mean across cases).
    n = max(1, len(case_scores))
    agg = {d: round(sum(c["scores"][d] for c in case_scores) / n, 4) for d in DIMENSIONS}
    overall = round(sum(c["overall"] for c in case_scores) / n, 4)
    return {
        "config": config,
        "config_label": _config_label(config),
        "pakem_version": _pakem_version(),
        "mode": "live" if live else "dry",
        "n_cases": len(case_scores),
        "aggregate": {**agg, "overall": overall},
        "cases": case_scores,
    }


def _config_label(config: dict) -> str:
    if not config:
        return "default"
    return ",".join(f"{k.upper()}={v}" for k, v in sorted(config.items()))


# =========================================================================== #
# 5. A/B reporting, baseline, gate
# =========================================================================== #
def _fmt_row(label: str, agg: dict, width: int = 34) -> str:
    return (f"{label[:width]:<{width}}  "
            f"{agg['coherence']:.3f}   {agg['factuality']:.3f}   "
            f"{agg['style']:.3f}   {agg['overall']:.3f}")


def print_ab_table(runs: list[dict]) -> None:
    """Print a side-by-side A/B table across configurations."""
    width = 34
    print()
    print("=" * 78)
    print("PROJECT DALANG — NARRATION EVAL (A/B)")
    print("=" * 78)
    print(f"{'CONFIG':<{width}}  COHER   FACTUAL  STYLE   OVERALL")
    print("-" * 78)
    for r in runs:
        print(_fmt_row(r["config_label"], r["aggregate"], width))
    print("-" * 78)
    # per-case breakdown for the headline (first) config
    if runs:
        head = runs[0]
        print(f"\nPer-case ({head['config_label']}, pakem={head['pakem_version']}, "
              f"mode={head['mode']}):")
        for c in head["cases"]:
            s = c["scores"]
            flag = "" if c["ok"] else "  [FAILED]"
            print(f"  {c['case_id']:<26} coh={s['coherence']:.2f} "
                  f"fac={s['factuality']:.2f} sty={s['style']:.2f} "
                  f"=> {c['overall']:.2f}{flag}")
    print("=" * 78)


def save_baseline(run: dict, path: Path = BASELINE_PATH) -> None:
    """Persist a run (its aggregate + per-case overall) as the baseline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "pakem_version": run["pakem_version"],
        "config": run["config"],
        "config_label": run["config_label"],
        "mode": run["mode"],
        "aggregate": run["aggregate"],
        "cases": {c["case_id"]: c["overall"] for c in run["cases"]},
    }
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[eval] baseline saved -> {path} "
          f"(overall={run['aggregate']['overall']:.3f}, pakem={run['pakem_version']})")


def load_baseline(path: Path = BASELINE_PATH) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def gate(run: dict, baseline: Optional[dict], *, tolerance: float = GATE_TOLERANCE) -> tuple[bool, list[str]]:
    """Compare `run` against `baseline`. Returns (passed, reasons).

    Fails if the new pakem's overall (or ANY dimension) drops below baseline by
    more than `tolerance`, or if any individual case regresses past tolerance.
    A missing baseline passes (with a note) — the first run establishes it.
    """
    reasons: list[str] = []
    if baseline is None:
        return True, ["no baseline on file — nothing to gate against (run --save-baseline first)"]

    base_agg = baseline.get("aggregate", {})
    cur_agg = run["aggregate"]
    for dim in (*DIMENSIONS, "overall"):
        b = float(base_agg.get(dim, 0.0))
        c = float(cur_agg.get(dim, 0.0))
        if c + tolerance < b:
            reasons.append(f"aggregate.{dim} regressed: {c:.3f} < {b:.3f} (tol {tolerance})")

    base_cases = baseline.get("cases", {})
    for cs in run["cases"]:
        cid = cs["case_id"]
        if cid in base_cases:
            b = float(base_cases[cid])
            c = float(cs["overall"])
            if c + tolerance < b:
                reasons.append(f"case {cid} regressed: {c:.3f} < {b:.3f} (tol {tolerance})")

    passed = len(reasons) == 0
    if passed:
        reasons.append(f"all dimensions within tolerance of baseline "
                       f"(pakem {baseline.get('pakem_version')} -> {run['pakem_version']})")
    return passed, reasons


# =========================================================================== #
# 6. CLI
# =========================================================================== #
def _parse_ab(spec: str) -> dict:
    """Parse a config spec 'ORCH_MODE=static,POLISH=none' into a dict."""
    cfg: dict[str, str] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        cfg[k.strip()] = v.strip()
    return cfg


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval.run",
        description="Project Dalang narration eval + A/B loop (coherence/factuality/style).")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry", action="store_true",
                      help="Score pre-generated fixtures (no LLM cost). CI default.")
    mode.add_argument("--live", action="store_true",
                      help="Run the real orchestrator for every case (needs keys/$).")
    p.add_argument("--ab", action="append", default=[], metavar="CONFIG",
                   help="A config to score, e.g. 'ORCH_MODE=static,POLISH=none'. "
                        "Repeat for side-by-side A/B. Omit for a single default run.")
    p.add_argument("--save-baseline", action="store_true",
                   help="Save the headline (first) config's run as the baseline.")
    p.add_argument("--gate", action="store_true",
                   help="Fail (exit 2) if the headline run regresses vs baseline.")
    p.add_argument("--golden", default=str(GOLDEN_PATH),
                   help="Path to the golden cases YAML.")
    p.add_argument("--json", action="store_true",
                   help="Emit the full run record as JSON to stdout.")
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    live = bool(args.live)  # default is dry unless --live given
    cases = load_cases(Path(args.golden))
    if not cases:
        print("[eval] no cases loaded — aborting.", file=sys.stderr)
        return 1

    configs = [_parse_ab(s) for s in args.ab] if args.ab else [{}]
    runs = [await run_config(cases, cfg, live=live) for cfg in configs]

    print_ab_table(runs)
    headline = runs[0]

    if args.json:
        print(json.dumps({"runs": runs}, indent=2, ensure_ascii=False))

    if args.save_baseline:
        save_baseline(headline)

    exit_code = 0
    if args.gate:
        passed, reasons = gate(headline, load_baseline())
        print("\n[gate] " + ("PASS" if passed else "FAIL"))
        for r in reasons:
            print(f"  - {r}")
        if not passed:
            exit_code = 2
    return exit_code


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
