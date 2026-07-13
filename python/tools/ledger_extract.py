#!/usr/bin/env python3
"""ledger_extract — auto-extract lane-ledger entries from finished rolls.

Law 4 of the 5-roll rain audit (2026-07-13): a lane converges on ITSELF —
cast names, towns, streets, aphorism frames and beats recur across rolls even
though every roll is a fresh job. Manual ledger curation does not scale past
2-3 rolls, so this tool sweeps N manuscript files and emits a paste-ready
lane_ledger.json fragment (same keys the bible addendum in
orchestrator/dynamic.py and the validator in narasi_counters._ledger_lane_patterns
already consume — no consumer change needed).

Usage:
    python tools/ledger_extract.py --lane kdrama_serial \
        [--ledger pakem/lane_ledger.json] [--date 2026-07-13] \
        [--min-rolls 2] [--json] roll1.txt roll2.txt ...

Output: a human report (which entry, which rolls, evidence) and, with --json,
the JSON fragment holding ONLY entries not already covered by --ledger.
Curation stays human: the fragment is reviewed and merged by hand (corpus
policy — curated data lives in JSON, edited deliberately).

Stdlib only. Never imports the app.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from typing import Any, Optional

# ── tokens ──────────────────────────────────────────────────────────────────
# Korean admin/place suffixes — hyphenated tokens ending in these are places,
# not given names (Jungang-ro, Haean-gil), and the suffix is the tell.
_PLACE_SUFFIX = ("ro", "gil", "dong", "gu", "si", "gun", "do", "myeon", "eup", "ri", "daero")
_GIVEN_RX = re.compile(r"\b([A-Z][a-z]+-[a-z]+)\b")
_FULLNAME_RX = re.compile(r"\b([A-Z][a-z]+)\s+([A-Z][a-z]+-[a-z]+)\b")
_TIMESTAMP_RX = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_FLOOR_RX = re.compile(r"(?i)\b(\d{1,2})(?:st|nd|rd|th)?[\s-]floor\b|\bfloor\s+(\d{1,2})\b")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_DATE_RX = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(\d{4})\b")
_NUM_UNIT_RX = re.compile(
    r"(?i)\b(\d{1,3}|" +
    "|".join(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
              "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
              "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
              "eighty", "ninety", r"twenty-\w+", r"thirty-\w+", r"forty-\w+", r"fifty-\w+",
              r"sixty-\w+", r"seventy-\w+", r"eighty-\w+", r"ninety-\w+")) +
    r")\s+(millimeters?|millimetres?|mm\b|centimeters?|percent|degrees?|kilometers?|metres?|meters?)")

_SPELLED = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
            "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
            "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
            "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}

# Foods that read as the "comfort/intimacy dish" slot in kdrama-adjacent lanes.
_FOOD_LEXICON = (
    "ramyeon", "ramen", "tteokbokki", "gimbap", "kimbap", "kimchi", "barley tea",
    "soju", "makgeolli", "kalguksu", "juk", "jjigae", "bibimbap", "chestnut",
    "hotteok", "eomuk", "odeng", "mandu", "samgyeopsal", "naengmyeon",
    "seaweed soup", "miyeok", "yakgwa", "sikhye", "dried fish",
)

# Beat families — ACTION patterns (not nouns) that the 5-roll audits keep seeing.
# label → regex. A family counts once per roll; ≥ min_rolls ⟹ recycled_beats entry.
_BEAT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("caregiver food-portioning (egg/largest piece given wordlessly)",
     r"(?i)(?:gave|giving|slid|pushed|set|placed)[^.\n]{0,50}\b(?:egg|the\s+larger|the\s+bigger|his\s+own\s+portion)\b|smallest\s+for\s+himself"),
    ("fluorescent hum as institutional ambience", r"(?i)fluorescent\s+(?:hum|light|tube)"),
    ("steady hands as competence/data signal", r"(?i)steady\s+hands?\b"),
    ("cold coffee as devotion-to-work", r"(?i)(?:cold|forgotten)\s+coffee"),
    ("letter-to-the-wind aphorism frame", r"(?i)letter\s+with(?:out)?\s+(?:an?\s+|no\s+)?address[^.\n]{0,60}wind"),
    ("umbrella mending/keeping vigil", r"(?i)umbrella[^.\n]{0,40}(?:mend|spoke|rib|shop)|(?:mend|spoke|rib)[^.\n]{0,30}umbrella"),
    ("private instrument record as counter-archive (rain gauge / barograph roll)",
     r"(?i)(?:rain\s+gauge|(?:micro)?barograph)[^.\n]{0,60}(?:log|notebook|record|column|roll|trace)"),
    ("carbon-copy / triplicate form as evidence", r"(?i)carbon\s+(?:copy|triplicate)|triplicate"),
    ("barley tea served as care gesture", r"(?i)barley\s+tea"),
    ("dead parent's handwriting recognized", r"(?i)(?:father|mother)'s\s+hand(?:writing)?\b[^.\n]{0,60}(?:recogni|knew|steady|careful)"),
)

_STOPWORDS = frozenset("""
a an and are as at be been but by for from had has have he her here him his i if in
into is it its me my no nor not of on or our out she so than that the their them
then there these they this those to too was we were what when where which who will
with would you your himself herself itself them
""".split())

# Capitalized tokens that are never lane places even when they recur mid-sentence.
_PLACE_STOP = frozenset({
    "Chapter", "The", "God", "Sea", "East", "West", "North", "South", "Korea", "Korean",
    "Seoul",  # metropolis, not a lane fingerprint — every kdrama may touch Seoul
    *_MONTHS, "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
})

# Sentence-starters that the Surname+Given-hyphen pattern misreads as surnames
# ("But Seo-jin stood…" ⟹ surname 'But').
_SURNAME_STOP = frozenset({
    "But", "And", "When", "Then", "While", "If", "So", "Yet", "Now", "Even",
    "Only", "Still", "Though", "Because", "Miss", "Madam", "Doctor", "Perhaps",
})

# First segments that mean the hyphenated token is a spelled NUMBER, not a name.
_SPELLED_CAP = frozenset(w.capitalize() for w in _SPELLED) | frozenset(
    w.capitalize() for w in _TENS)

_ROUND_MINUTES = frozenset({":00", ":15", ":30", ":45"})   # natural, not lane tics


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _prose(text: str) -> str:
    """Manuscript body only: drop the job-metadata header (everything before the
    first Chapter heading) and the chapter-title lines themselves — both are
    Title Case machine text that pollutes every extraction class."""
    m = re.search(r"(?im)^chapter\s*(?:\d+|one)\b.*$", text)
    body = text[m.start():] if m else text
    return re.sub(r"(?im)^chapter\s*\d+\s*:.*$", "", body)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _spelled_to_int(tok: str) -> Optional[int]:
    t = tok.lower()
    if t in _SPELLED:
        return _SPELLED[t]
    m = re.match(r"(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-(\w+)", t)
    if m and m.group(2) in _SPELLED and _SPELLED[m.group(2)] < 10:
        return _TENS[m.group(1)] + _SPELLED[m.group(2)]
    return None


def _is_place_token(tok: str) -> bool:
    return "-" in tok and tok.rsplit("-", 1)[-1] in _PLACE_SUFFIX


def _ledger_term(x: Any) -> str:
    """Mirror of narasi_counters._ledger_term — annotation-stripped comparable term."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(x)).strip().lstrip("…").strip()


# ── per-file extraction ─────────────────────────────────────────────────────
def extract_one(text: str) -> dict:
    """All raw signals from one roll. Values are dict[value] = count."""
    out: dict[str, Any] = {k: defaultdict(int) for k in (
        "given_names", "surnames", "timestamps", "small_numbers", "foods",
        "floors", "places", "dates", "beats")}

    _low_words = frozenset(re.findall(r"\b[a-z]{3,}\b", text))

    def _namelike(tok: str) -> bool:
        """Korean romanized given name: short syllable segments, not a spelled
        number (Forty-three), not a hyphenated modifier (Sun-darkened, High-risk
        — both halves live lowercase in the same prose; name syllables don't)."""
        head, _, tail = tok.partition("-")
        if head in _SPELLED_CAP or _is_place_token(tok):
            return False
        if head.lower() in _low_words and tail.lower() in _low_words:
            return False
        return 1 <= len(tail) <= 5 and 1 <= len(head) <= 6

    for m in _FULLNAME_RX.finditer(text):
        sn, gv = m.group(1), m.group(2)
        if _namelike(gv):
            if sn not in _SURNAME_STOP:
                out["surnames"][sn] += 1
            out["given_names"][gv] += 1
    for m in _GIVEN_RX.finditer(text):
        tok = m.group(1)
        if _namelike(tok):
            out["given_names"][tok] += 1
    for m in _TIMESTAMP_RX.finditer(text):
        hh = int(m.group(1))
        mm = ":" + m.group(2)
        if 0 <= hh <= 23 and mm not in _ROUND_MINUTES:   # kill 112:287 ratios + natural :00/:30
            out["timestamps"][mm] += 1
    for m in _NUM_UNIT_RX.finditer(text):
        raw = m.group(1)
        val = int(raw) if raw.isdigit() else _spelled_to_int(raw)
        if val is not None and 5 < val <= 300:   # ≤5 are everyday counts, not lane tics
            out["small_numbers"][val] += 1
    for food in _FOOD_LEXICON:
        n = len(re.findall(r"(?i)\b" + re.escape(food) + r"\b", text))
        if n:
            out["foods"][food] += n
    for m in _FLOOR_RX.finditer(text):
        val = m.group(1) or m.group(2)
        if val:
            out["floors"][int(val)] += 1
    for m in re.finditer(r"\b([A-Z][a-z]+-(?:" + "|".join(_PLACE_SUFFIX) + r"))\b", text):
        out["places"][m.group(1)] += 1
    # Capitalized mid-sentence singles (town names like Yeongdeok): count only
    # occurrences NOT at sentence start, then drop person/name/stop tokens later.
    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{3,})\b", text, flags=re.M):
        out["places"][m.group(1)] += 0  # register candidate; scored cross-roll
        out["places"][m.group(1)] += 1
    for m in _DATE_RX.finditer(text):
        out["dates"][f"{int(m.group(1))} {m.group(2)} {m.group(3)}"] += 1
    for label, rx in _BEAT_FAMILIES:
        if re.search(rx, text):
            out["beats"][label] += 1
    return out


def _gnomic_lines(text: str) -> list[str]:
    """Standalone-paragraph gnomic lines (aphorism candidates)."""
    lines = []
    for para in re.split(r"\n\s*\n", text):
        p = _norm_ws(para)
        words = p.split()
        if not (4 <= len(words) <= 24):
            continue
        if any(ch in p for ch in '"“”') or re.search(r"\d", p):
            continue
        if not p.endswith((".", "!", "?")):
            continue
        if p.startswith(("Chapter", "CHAPTER")):
            continue
        lines.append(p)
    return lines


def _content_tokens(s: str) -> frozenset:
    return frozenset(w for w in re.findall(r"[a-z']+", s.lower())
                     if w not in _STOPWORDS and len(w) >= 3)


def _shingles(text: str, n: int = 6) -> set[str]:
    toks = re.findall(r"[a-z']+", text.lower())
    return {" ".join(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}


def _ch1_window(text: str, words: int = 250) -> str:
    m = re.search(r"(?im)^chapter\s*(?:1|one)\b.*$", text)
    body = text[m.end():] if m else text
    return " ".join(body.split()[:words])


# ── cross-roll aggregation ──────────────────────────────────────────────────
def aggregate(files: list[str], raw_texts: list[str], min_rolls: int) -> dict:
    # chapter titles live OUTSIDE prose (stripped by _prose) but are their own
    # cross-roll convergence surface (roll 6: 'A False Map of the Sky' ~ roll 3's
    # 'The False Map of the Sky'; 'The Weight of X' frame 4/6 rolls).
    titles_per = [re.findall(r"(?im)^chapter\s*\d+\s*:\s*(.+)$", t) for t in raw_texts]
    texts = [_prose(t) for t in raw_texts]     # header/title lines poison every class
    per = [extract_one(t) for t in texts]
    n = len(texts)
    labels = [f"R{i+1}" for i in range(n)]

    def roll_map(key: str) -> dict[Any, list[str]]:
        hits: dict[Any, list[str]] = defaultdict(list)
        for i, p in enumerate(per):
            for val, cnt in p[key].items():
                if cnt > 0:
                    hits[val].append(labels[i])
        return hits

    agg: dict[str, Any] = {"n_rolls": n, "files": dict(zip(labels, files))}

    names = roll_map("given_names")
    surnames = roll_map("surnames")
    person_tokens = set()
    for full in list(names) + list(surnames):
        person_tokens.update(full.split("-")[0:1] + [full])

    # given names: EVERY name used in any roll is lane history (the ledger is a
    # used-names registry, not just a repeat-offender list) — but flag repeats.
    agg["given_names"] = {v: rs for v, rs in sorted(names.items())}
    agg["overused_surnames"] = {v: rs for v, rs in surnames.items() if len(rs) >= min_rolls}
    agg["surnames_all"] = {v: rs for v, rs in sorted(surnames.items())}

    # stems: leading + trailing syllables shared by ≥2 distinct given names, or
    # one name recurring across rolls.
    stems: dict[str, set] = defaultdict(set)
    for nm in names:
        parts = nm.split("-")
        if len(parts) == 2:
            stems[parts[0] + "-"].add(nm)
            stems["-" + parts[1]].add(nm)
    # A stem is only a SHAPE ban when ≥2 distinct names share it — a single
    # recurring name is already covered by its own given_names entry.
    agg["name_stems"] = {
        stem: sorted(members) for stem, members in sorted(stems.items())
        if len(members) >= 2}

    agg["timestamp_minutes"] = {v: rs for v, rs in roll_map("timestamps").items()
                                if len(rs) >= min_rolls}
    agg["small_numbers"] = {v: rs for v, rs in roll_map("small_numbers").items()
                            if len(rs) >= min_rolls}
    agg["foods"] = {v: rs for v, rs in roll_map("foods").items()}      # 1-roll foods still curatable
    agg["floors"] = {v: rs for v, rs in roll_map("floors").items() if len(rs) >= min_rolls}
    agg["dates"] = {v: rs for v, rs in roll_map("dates").items() if len(rs) >= min_rolls}
    agg["recycled_beats"] = {v: rs for v, rs in roll_map("beats").items()
                             if len(rs) >= min_rolls}

    # lowercase-twin filter: a real toponym (Yeongdeok, Pohang, Jungang-ro) never
    # appears lowercased in prose; capitalized common nouns/pronouns do ("Water"
    # mid-clause vs "the water"). One lowercase sighting anywhere ⟹ not a place.
    _lower_twins: set[str] = set()
    for t in texts:
        _lower_twins.update(re.findall(r"\b[a-z][a-z]{3,}\b", t))
    places = {}
    for v, rs in roll_map("places").items():
        if len(rs) < min_rolls:
            continue
        if v in _PLACE_STOP or v in person_tokens:
            continue
        if v.lower() in _lower_twins:
            continue
        if any(v == nm.split("-")[0] for nm in names):   # 'Seo' from Seo-jin
            continue
        places[v] = rs
    agg["places"] = places

    # cross-roll verbatim shingles (6-gram) → maximal repeated phrases
    sh = [_shingles(t) for t in texts]
    shared: dict[str, list[str]] = defaultdict(list)
    for i in range(n):
        for j in range(i + 1, n):
            for s in (sh[i] & sh[j]):
                toks = s.split()
                if all(t in _STOPWORDS for t in toks):
                    continue
                if not any(len(t) >= 4 and t not in _STOPWORDS for t in toks):
                    continue
                pair = shared[s]
                for lab in (labels[i], labels[j]):
                    if lab not in pair:
                        pair.append(lab)
    # collapse overlapping shingles: consecutive 6-grams of one longer repeated
    # phrase overlap by exactly n-1 tokens, so demand a DEEP overlap (≥ 5) and
    # bound the growth loop — a 1-token overlap on 'the'/'of' is coincidence and
    # made the naive merge effectively unbounded on 5×9k-word inputs.
    merged: dict[str, list[str]] = {}
    cands = sorted(shared.items(), key=lambda kv: (-len(kv[1]), -len(kv[0])))[:400]
    used: set[str] = set()
    for s, rs in cands:
        if s in used:
            continue
        cur = s
        for _ in range(60):                     # hard bound per phrase
            grew = False
            a = cur.split()
            for t, trs in cands:
                if t in used or t == cur or set(trs) != set(rs):
                    continue
                b = t.split()
                k = min(len(a), len(b)) - 1     # 6-gram chain ⟹ overlap n-1
                if k >= 5:
                    if a[-k:] == b[:k]:
                        cur = " ".join(a + b[k:]); used.add(t); grew = True; break
                    if b[-k:] == a[:k]:
                        cur = " ".join(b + a[k:]); used.add(t); grew = True; break
            if not grew:
                break
            a = cur.split()
        used.add(s)
        if not any(cur in kept and set(rs) <= set(krs) for kept, krs in merged.items()):
            merged[cur] = rs
    agg["verbatim_phrases"] = dict(sorted(merged.items(),
                                          key=lambda kv: (-len(kv[1]), -len(kv[0])))[:15])

    # aphorism frames: gnomic standalone lines, cross-roll content-token Jaccard
    gn = [_gnomic_lines(t) for t in texts]
    frames = []
    for i in range(n):
        for j in range(i + 1, n):
            for a in gn[i]:
                ta = _content_tokens(a)
                if not ta:
                    continue
                for b in gn[j]:
                    tb = _content_tokens(b)
                    if not tb:
                        continue
                    jac = len(ta & tb) / len(ta | tb)
                    # ≥3 shared content words: tiny gnomic lines ("She did not
                    # stop." ~ "He did not stop her.") over-fire on Jaccard alone.
                    if jac >= 0.5 and len(ta & tb) >= 3 and a != b:
                        frames.append({"rolls": [labels[i], labels[j]],
                                       "a": a, "b": b, "jaccard": round(jac, 2)})
    agg["aphorism_frames"] = frames[:10]

    # Ch1 opener echoes: 5-gram overlap inside the first ~250 words
    op = [_shingles(_ch1_window(t), n=5) for t in texts]
    opener: dict[str, list[str]] = defaultdict(list)
    for i in range(n):
        for j in range(i + 1, n):
            for s in (op[i] & op[j]):
                if any(len(t) >= 4 and t not in _STOPWORDS for t in s.split()):
                    for lab in (labels[i], labels[j]):
                        if lab not in opener[s]:
                            opener[s].append(lab)
    agg["ch1_opener_echoes"] = dict(sorted(opener.items(), key=lambda kv: -len(kv[1]))[:8])

    # ── FACT-TUPLES (round-5.1; Law-4 qualitative escalation: R8 remixed R6's whole
    # fact-set — Ik-ro + Level 4→2 + 3-vs-14 + Aug-13. Token bans can't see a SCHEMA;
    # extract the recurring fact-shapes themselves.) ──
    def _facts_one(t: str) -> dict:
        f: dict = {"md": set(), "lvl": set(), "toll": set()}
        for m in re.finditer(r"(?i)\b(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b", t):
            f["md"].add(f"{int(m.group(1))} {m.group(2).title()}")
        for m in re.finditer(r"(?i)\bLevel\s+(\d)\b.{0,160}?\bLevel\s+(\d)\b", t, re.S):
            a, b = int(m.group(1)), int(m.group(2))
            if a != b:
                f["lvl"].add(f"Level {max(a,b)}→{min(a,b)}")
        nums = []
        for m in re.finditer(r"(?i)\b(three|nine|fourteen|\d{1,2})\b(?=[^.\n]{0,70}(?:dead|death|casualt|missing|drowned|victim|fatalit))", t):
            tok = m.group(1).lower()
            v = int(tok) if tok.isdigit() else {"three": 3, "nine": 9, "fourteen": 14}.get(tok)
            if v and 2 <= v <= 60:
                nums.append(v)
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if nums[i] != nums[j]:
                    f["toll"].add(f"{min(nums[i], nums[j])}-vs-{max(nums[i], nums[j])}")
        return f

    _facts = [_facts_one(t) for t in texts]
    tuples: dict[str, list[str]] = defaultdict(list)
    for i, f in enumerate(_facts):
        for kind, vals in f.items():
            for v in vals:
                key = {"md": "disaster/flashback date", "lvl": "classification pair",
                       "toll": "toll pair"}[kind] + f": {v}"
                if labels[i] not in tuples[key]:
                    tuples[key].append(labels[i])
    agg["fact_tuples"] = {k: rs for k, rs in sorted(tuples.items(), key=lambda kv: -len(kv[1]))
                          if len(rs) >= min_rolls}

    # chapter-title echoes: shared content tokens + near-dup titles across rolls
    ttok: dict[str, list[str]] = defaultdict(list)
    for i, ts in enumerate(titles_per):
        for t in ts:
            for w in _content_tokens(t):
                if len(w) >= 4 and labels[i] not in ttok[w]:
                    ttok[w].append(labels[i])
    agg["title_token_echoes"] = {w: rs for w, rs in sorted(ttok.items(), key=lambda kv: -len(kv[1]))
                                 if len(rs) >= max(min_rolls, 3)}
    tdups = []
    for i in range(n):
        for j in range(i + 1, n):
            for a in titles_per[i]:
                ta = _content_tokens(a)
                if not ta:
                    continue
                for b in titles_per[j]:
                    tb = _content_tokens(b)
                    if tb and a != b and len(ta & tb) >= 2 and len(ta & tb) / len(ta | tb) >= 0.6:
                        tdups.append({"rolls": [labels[i], labels[j]], "a": a, "b": b})
    agg["title_near_dups"] = tdups[:8]
    return agg


# ── ledger fragment emission ────────────────────────────────────────────────
def emit_fragment(agg: dict, lane: str, existing: Optional[dict], date: str,
                  min_rolls: int) -> dict:
    """Paste-ready lane fragment: ONLY consumer-known keys, ONLY new terms."""
    known: dict[str, set] = defaultdict(set)
    lane_now = (existing or {}).get(lane) or {}
    for key, vals in lane_now.items():
        if isinstance(vals, list):
            for v in vals:
                known[key].add(_ledger_term(v).lower())

    def new_terms(key: str, items: dict, fmt) -> list:
        outs = []
        for val, rolls in items.items():
            entry = fmt(val, rolls)
            term = _ledger_term(entry)
            if term.lower() in known[key]:
                continue
            if isinstance(val, int) and str(val) in {str(x) for x in known[key]}:
                continue
            outs.append(entry)
        return outs

    def ann(rolls: list[str]) -> str:
        return f"({len(rolls)}/{agg['n_rolls']} rolls {date}: {','.join(rolls)})"

    frag: dict[str, Any] = {}
    frag["given_names"] = new_terms(
        "given_names", agg["given_names"],
        lambda v, r: v if len(r) < min_rolls else f"{v} {ann(r)}")
    frag["overused_surnames"] = new_terms(
        "overused_surnames", agg["overused_surnames"], lambda v, r: f"{v} {ann(r)}")
    frag["name_stems"] = [
        f"{stem} shared by {', '.join(members)} {date}"
        for stem, members in agg["name_stems"].items()
        if stem.lower().rstrip('-').lstrip('-') not in
        {t.split()[0].lower().rstrip('-').lstrip('-') for t in known["name_stems"] if t}]
    frag["timestamp_minutes"] = new_terms(
        "timestamp_minutes", agg["timestamp_minutes"], lambda v, r: f"{v} {ann(r)}")
    frag["small_numbers"] = new_terms(
        "small_numbers", agg["small_numbers"], lambda v, r: f"{v} {ann(r)}")
    frag["foods"] = new_terms(
        "foods", {k: v for k, v in agg["foods"].items() if len(v) >= min_rolls},
        lambda v, r: f"{v} {ann(r)}")
    frag["floors"] = new_terms("floors", agg["floors"], lambda v, r: f"{v} {ann(r)}")
    frag["places"] = new_terms("places", agg["places"], lambda v, r: f"{v} {ann(r)}")
    frag["verbatim_phrases"] = new_terms(
        "verbatim_phrases",
        {**agg["verbatim_phrases"],
         **{f"{d}": r for d, r in agg["dates"].items()}},   # recycled disaster dates ban verbatim
        lambda v, r: f"{v} {ann(r)}")
    beats = dict(agg["recycled_beats"])
    for fr in agg["aphorism_frames"]:
        key = f'aphorism frame near-dup: "{min(fr["a"], fr["b"], key=len)}"'
        beats.setdefault(key, fr["rolls"])
    for s, r in list(agg["ch1_opener_echoes"].items())[:3]:
        beats.setdefault(f"Ch1 opener echo: “{s}”", r)
    for td in agg.get("title_near_dups") or []:
        beats.setdefault(f"chapter-title near-dup: '{min(td['a'], td['b'], key=len)}'", td["rolls"])
    for w, r in list((agg.get("title_token_echoes") or {}).items())[:4]:
        beats.setdefault(f"chapter-title token echo: '{w}'", r)
    for ft, r in list((agg.get("fact_tuples") or {}).items())[:8]:
        beats.setdefault(f"FACT-TUPLE {ft}", r)
    frag["recycled_beats"] = new_terms("recycled_beats", beats, lambda v, r: f"{v} {ann(r)}")
    return {lane: {k: v for k, v in frag.items() if v}}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("files", nargs="+", help="roll manuscript .txt files, oldest first")
    ap.add_argument("--lane", required=True, help="pakem style key, e.g. kdrama_serial")
    ap.add_argument("--ledger", help="existing lane_ledger.json (emit only NEW entries)")
    ap.add_argument("--date", default="", help="annotation date, e.g. 2026-07-13")
    ap.add_argument("--min-rolls", type=int, default=2,
                    help="cross-roll threshold for most classes (default 2)")
    ap.add_argument("--json", action="store_true", help="print ONLY the JSON fragment")
    args = ap.parse_args(argv)

    texts = [_read(p) for p in args.files]
    agg = aggregate(args.files, texts, args.min_rolls)
    existing = None
    if args.ledger:
        with open(args.ledger, encoding="utf-8") as f:
            existing = json.load(f)
    frag = emit_fragment(agg, args.lane, existing, args.date or "undated", args.min_rolls)

    if args.json:
        print(json.dumps(frag, ensure_ascii=False, indent=2))
        return 0

    print(f"# lane {args.lane} — {agg['n_rolls']} rolls")
    for lab, path in agg["files"].items():
        print(f"#   {lab}: {path}")
    print("\n## cross-roll signals")
    for key in ("overused_surnames", "name_stems", "timestamp_minutes", "small_numbers",
                "foods", "floors", "places", "dates", "recycled_beats"):
        items = agg.get(key) or {}
        if items:
            print(f"\n{key}:")
            for v, rs in list(items.items())[:20]:
                print(f"  {v}  [{', '.join(rs if isinstance(rs, list) else [str(rs)])}]")
    if agg["verbatim_phrases"]:
        print("\nverbatim_phrases (cross-roll 6-gram+, merged):")
        for s, rs in agg["verbatim_phrases"].items():
            print(f"  “{s}”  [{', '.join(rs)}]")
    if agg["aphorism_frames"]:
        print("\naphorism frames (content-token Jaccard ≥ 0.5):")
        for fr in agg["aphorism_frames"]:
            print(f"  [{', '.join(fr['rolls'])}] j={fr['jaccard']}")
            print(f"    a: {fr['a']}")
            print(f"    b: {fr['b']}")
    if agg["ch1_opener_echoes"]:
        print("\nCh1 opener echoes (5-gram):")
        for s, rs in agg["ch1_opener_echoes"].items():
            print(f"  “{s}”  [{', '.join(rs)}]")
    print("\n## paste-ready NEW-entry fragment (--json for raw)")
    print(json.dumps(frag, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
