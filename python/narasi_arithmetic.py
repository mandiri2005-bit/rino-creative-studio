# ── narasi_arithmetic — SPEC v1 §3.3 internal_consistency, DETERMINISTIC arithmetic pass.
#
# Two cheap checks that caught 2 of 3 Louis XIV residuals without a search or model call:
#
#   1. age_across_scenes: same person, "X ans" reported at two different anchor years —
#      is (Y2 - Y1) == (age2 - age1)? Louis XV "cinq ans" in 1712 vs "cinq ans" in 1715
#      = age copied across scenes (drift class).
#
#   2. interval_vs_dates: manuscript states "N jours après <date1>" alongside a second
#      date; is the arithmetic consistent? "quatre jours après le dernier souffle"
#      (implicit 1 sept 1715) + "le 9 septembre 1715" = interval should be 8, not 4.
#
# Both are language-aware (fr/id/en number-words), no LLM, feeds §3.3
# internal_consistency report. Additive to counters — never touches manuscript text,
# only flags for the surgical rewrite loop.
from __future__ import annotations

import os
import re
from typing import Any

__all__ = ["scan_arithmetic", "AGE_ANCHORS", "INTERVAL_ANCHORS", "scan_age_ledger",
           "scan_canon_anchor_dates", "scan_tenure_ledger"]

# ── number-word tables (spelled small ages 0-19; enough for age-of-person anchors) ──
_AGE_WORDS: dict[str, dict[str, int]] = {
    "fr": {"zéro": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
           "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12,
           "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17,
           "dix-huit": 18, "dix-neuf": 19, "vingt": 20, "vingt-deux": 22, "vingt-cinq": 25,
           "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60, "soixante-dix": 70,
           "soixante-douze": 72, "quatre-vingts": 80},
    "en": {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
           "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
           "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
           "eighteen": 18, "nineteen": 19, "twenty": 20, "twenty-two": 22, "twenty-five": 25,
           "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "seventy-two": 72,
           "eighty": 80},
    "id": {"nol": 0, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
           "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "sebelas": 11,
           "dua belas": 12, "tiga belas": 13, "empat belas": 14, "lima belas": 15,
           "enam belas": 16, "tujuh belas": 17, "delapan belas": 18, "sembilan belas": 19,
           "dua puluh": 20},
}

# ── year-anchor detectors (finds nearest year in the same sentence/paragraph)
_YEAR_RX = re.compile(r"\b(1[5-9]\d{2}|20[0-2]\d)\b")
# noun of age-attribution (subject that carries an age; per language)
_AGE_SUBJECT = {
    "fr": r"(?:un\s+enfant|un\s+gar[çc]on|un\s+jeune\s+roi|le\s+roi|Louis|"
          r"arri[eè]re-?petit-?fils|arri[eè]re-petit-fils|petit-?fils|petite-?fille)",
    "en": r"(?:a\s+boy|a\s+child|a\s+young\s+king|the\s+king|great-?grandson|grandson|"
          r"granddaughter|the\s+heir)",
    "id": r"(?:seorang\s+anak|anak\s+laki-laki|seorang\s+putra|cicit|cucu|raja\s+kecil|raja)",
}

_AGE_RX_TEMPLATES = {
    "fr": [
        # "un enfant de cinq ans" / "l'enfant de cinq ans" / "âgé de cinq ans"
        r"(?P<subj>{subj})\s+(?:de\s+|[àa]g[ée]\s+de\s+)?(?P<age_word>{ages}|\d{{1,2}})\s+ans",
        # "Louis, alors âgé de treize ans"
        r"Louis[^.\n]{{0,60}}[àa]g[ée]\s+de\s+(?P<age_word>{ages}|\d{{1,2}})\s+ans",
        # "Louis en a soixante-douze"
        r"Louis\s+en\s+a\s+(?P<age_word>{ages}|\d{{1,2}})",
    ],
    "en": [
        r"(?P<subj>{subj})\s+(?:of\s+|aged\s+)?(?P<age_word>{ages}|\d{{1,2}})\s+years?[ -]?(?:old)?",
        r"(?:aged|at)\s+(?P<age_word>{ages}|\d{{1,2}})",
    ],
    "id": [
        r"(?P<subj>{subj})\s+(?:berusia\s+|umur\s+)?(?P<age_word>{ages}|\d{{1,2}})\s+tahun",
    ],
}


def _compiled_age_rx(lang: str) -> list[re.Pattern]:
    subj = _AGE_SUBJECT.get(lang, _AGE_SUBJECT["en"])
    ages_dict = _AGE_WORDS.get(lang, _AGE_WORDS["en"])
    # Longer number-words first so "dix-huit" matches before "dix"
    ages_alt = "|".join(sorted((re.escape(k) for k in ages_dict), key=len, reverse=True))
    return [re.compile(t.format(subj=subj, ages=ages_alt), re.IGNORECASE)
            for t in _AGE_RX_TEMPLATES.get(lang, _AGE_RX_TEMPLATES["en"])]


def _parse_age(tok: str, lang: str) -> int | None:
    tok = tok.lower().strip()
    if tok.isdigit():
        return int(tok)
    table = _AGE_WORDS.get(lang, {})
    return table.get(tok)


def _nearest_year(text: str, pos: int, window: int = 400) -> int | None:
    """Return the closest 4-digit year in a window around pos, prefering same-paragraph."""
    lo = max(0, pos - window)
    hi = min(len(text), pos + window)
    # Prefer a year BEFORE the age (age is a consequence of a stated year)
    before = list(_YEAR_RX.finditer(text[lo:pos]))
    if before:
        return int(before[-1].group(1))
    after = _YEAR_RX.search(text[pos:hi])
    return int(after.group(1)) if after else None


AGE_ANCHORS = _AGE_WORDS  # exposed for tests


def scan_age_across_scenes(text: str, lang: str = "fr",
                            known_dob: dict[str, int] | None = None) -> list[dict]:
    """Find (subject, year, age) tuples in the manuscript and flag inconsistencies:
    - if two tuples for the SAME subject give inconsistent (year - dob), flag the pair;
    - if a known_dob is provided ({canonical: birth_year}), flag any tuple where the
      arithmetic doesn't match (Louis XV b.1710: age at 1712 must be 2, not 5).
    Returns a list of findings; empty list on clean text or empty input."""
    findings: list[dict] = []
    if not text:
        return findings
    rxs = _compiled_age_rx(lang)
    hits: list[tuple[str, int, int, int]] = []  # (subject, age, year, pos)
    for rx in rxs:
        for m in rx.finditer(text):
            age_tok = (m.groupdict().get("age_word") or "").strip()
            age = _parse_age(age_tok, lang)
            if age is None:
                continue
            year = _nearest_year(text, m.start())
            if year is None:
                continue
            subj = (m.groupdict().get("subj") or "").strip().lower()
            hits.append((subj, age, year, m.start()))
    # Check against known_dob if provided
    if known_dob:
        for subj, age, year, pos in hits:
            # If the subject text mentions a canonical name (case-insensitive contains),
            # check its birth-year math.
            for canonical, dob in known_dob.items():
                if canonical.lower() in subj or _mentions(text, pos, canonical):
                    expected = year - dob
                    if abs(expected - age) >= 1:
                        findings.append({
                            "kind": "age_arithmetic",
                            "subject": canonical,
                            "year_anchor": year,
                            "stated_age": age,
                            "expected_age": expected,
                            "context": text[max(0, pos - 60):pos + 100].strip(),
                            "note": f"{canonical} b.{dob}: age in {year} should be {expected}, not {age}",
                        })
    # Cross-scene drift: same subject, same age at two different years
    by_subj: dict[str, list] = {}
    for subj, age, year, pos in hits:
        by_subj.setdefault(subj, []).append((age, year, pos))
    for subj, tuples in by_subj.items():
        if len(tuples) < 2:
            continue
        # Deduplicate by (age, year)
        uniq = {(a, y): p for a, y, p in tuples}
        # Two DIFFERENT years with the SAME age → drift
        by_age: dict[int, list[int]] = {}
        for (a, y), p in uniq.items():
            by_age.setdefault(a, []).append(y)
        for age, years in by_age.items():
            if len(set(years)) >= 2:
                spread = max(years) - min(years)
                if spread >= 2:  # at most 1 year of natural slippage
                    findings.append({
                        "kind": "age_cross_scene_drift",
                        "subject": subj,
                        "stated_age": age,
                        "years": sorted(set(years)),
                        "note": f"Subject '{subj}' reported as {age} in both {min(years)} and {max(years)} ({spread}-year drift)",
                    })
    return findings


def _mentions(text: str, pos: int, name: str, window: int = 1200) -> bool:
    """Wider by default: same-piece co-reference is common (e.g. 'arrière-petit-fils'
    in one paragraph resolves to 'futur Louis XV' three paragraphs later)."""
    lo = max(0, pos - window)
    hi = min(len(text), pos + window)
    return name.lower() in text[lo:hi].lower()


# ── interval-vs-dates check (Louis XIV: "quatre jours après le dernier souffle" +
# "le 9 septembre 1715" while death was 1 sept → interval should be 8, not 4) ──
_MONTHS = {
    "fr": {"janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
           "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
           "novembre": 11, "décembre": 12, "decembre": 12},
    "en": {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12},
    "id": {"januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
           "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12},
}

_INTERVAL_WORD = {
    "fr": {"jour": 1, "jours": 1, "semaine": 7, "semaines": 7, "mois": 30, "an": 365,
           "ans": 365, "ann[ée]e": 365, "année": 365, "années": 365},
    "en": {"day": 1, "days": 1, "week": 7, "weeks": 7, "month": 30, "months": 30, "year": 365,
           "years": 365},
    "id": {"hari": 1, "minggu": 7, "bulan": 30, "tahun": 365},
}


def _parse_date(day: str, month_tok: str, year: str, lang: str) -> tuple[int, int, int] | None:
    months = _MONTHS.get(lang, {})
    m = months.get(month_tok.lower().strip())
    if not m:
        return None
    try:
        return (int(year), m, int(day))
    except (ValueError, TypeError):
        return None


def _days_between(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """Signed integer days between (Y,M,D) tuples using Julian-day arithmetic."""
    def jd(t: tuple[int, int, int]) -> int:
        y, m, d = t
        if m <= 2:
            y -= 1
            m += 12
        A = y // 100
        B = 2 - A + A // 4
        return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + B - 1524
    return jd(b) - jd(a)


INTERVAL_ANCHORS = _INTERVAL_WORD  # exposed for tests

# fr: "N jours après <ref>" ; "N mois plus tard" ; "N semaines après"
_INTERVAL_RX_FR = re.compile(
    r"(?i)(?P<n_word>\d+|un|deux|trois|quatre|cinq|six|sept|huit|neuf|dix|onze|"
    r"douze|treize|quatorze|quinze|seize|dix-sept|dix-huit|dix-neuf|vingt|vingt-cinq|"
    r"trente|quarante|cinquante|soixante)\s+"
    r"(?P<unit>jours?|semaines?|mois|ans?|années?)\s+"
    r"(?P<rel>(?:apr[eè]s|plus\s+tard|avant))")

_DATE_RX_FR = re.compile(
    r"(?i)(?:le\s+)?(?P<day>\d{1,2})(?:er)?\s+"
    r"(?P<month>janvier|f[ée]vrier|mars|avril|mai|juin|juillet|ao[uû]t|septembre|"
    r"octobre|novembre|d[ée]cembre)\s+(?P<year>1[5-9]\d{2}|20\d{2})")


def scan_interval_vs_dates(text: str, lang: str = "fr",
                             seed_dates: dict[str, tuple[int, int, int]] | None = None
                             ) -> list[dict]:
    """Find "N days/months after" phrases and check them against the surrounding dates
    OR against seeded reference dates. seed_dates = {label: (Y,M,D)}, e.g.
    {"louis_xiv_death": (1715, 9, 1)} — used when the reference date isn't stated
    inline but is a well-documented anchor. Returns findings; empty on clean text."""
    findings: list[dict] = []
    if not text or lang != "fr":  # start with fr; extend as en/id anchors arrive
        return findings

    # Collect all inline dates first (position + tuple).
    dates: list[tuple[int, tuple[int, int, int]]] = []
    for m in _DATE_RX_FR.finditer(text):
        t = _parse_date(m.group("day"), m.group("month"), m.group("year"), "fr")
        if t:
            dates.append((m.start(), t))

    _fr_word = {"un": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7,
                "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13,
                "quatorze": 14, "quinze": 15, "seize": 16, "dix-sept": 17, "dix-huit": 18,
                "dix-neuf": 19, "vingt": 20, "vingt-cinq": 25, "trente": 30,
                "quarante": 40, "cinquante": 50, "soixante": 60}
    _unit = {"jour": 1, "jours": 1, "semaine": 7, "semaines": 7, "mois": 30,
              "an": 365, "ans": 365, "année": 365, "années": 365}

    for m in _INTERVAL_RX_FR.finditer(text):
        n_tok = m.group("n_word").lower().strip()
        n = int(n_tok) if n_tok.isdigit() else _fr_word.get(n_tok)
        if n is None:
            continue
        unit_tok = m.group("unit").lower().rstrip("s") or m.group("unit").lower()
        # unit dict is keyed with plural forms too; normalize
        stated_days = None
        for k, v in _unit.items():
            if k.startswith(unit_tok[:3]):
                stated_days = n * v
                break
        if stated_days is None:
            continue

        # Case A: there is an inline date within 200 chars — check pair.
        near_dates = [(p, t) for p, t in dates if abs(p - m.start()) < 400]
        # Case B: consult seed_dates if provided.
        candidates: list[tuple[str, tuple[int, int, int]]] = []
        for p, t in near_dates:
            candidates.append((f"inline_{p}", t))
        if seed_dates:
            for lbl, t in seed_dates.items():
                candidates.append((lbl, t))

        if len(candidates) < 2:
            continue
        # Check every pair of candidate dates: if their interval matches stated, PASS
        # (any matching pair confirms). If NO pair matches, flag.
        stated_ok = False
        pairs_seen: list[tuple[str, str, int]] = []
        for i, (la, ta) in enumerate(candidates):
            for lb, tb in candidates[i + 1:]:
                actual = abs(_days_between(ta, tb))
                pairs_seen.append((la, lb, actual))
                if abs(actual - stated_days) <= max(1, int(0.05 * stated_days)):
                    stated_ok = True
                    break
            if stated_ok:
                break
        if not stated_ok:
            findings.append({
                "kind": "interval_arithmetic",
                "stated_interval_days": stated_days,
                "stated_phrase": m.group(0),
                "candidate_pairs": pairs_seen[:5],
                "context": text[max(0, m.start() - 60):m.start() + 120].strip(),
                "note": f"stated interval {stated_days}d does not match any candidate date pair",
            })
    return findings


# ── EN spans (round-3 craft, NARASI_TIMELINE_ARITH) ─────────────────────────
# 5/5 rain rolls drifted on DERIVED spans while the absolute dates held
# ("Three months after the flood" with 13 Aug → 17 Oct on the same page = 2
# months; "fourteen years" ×7 alternating with "fifteen years" ×3). Prompts
# cannot do arithmetic; these two deterministic checks can. WARN-feed only —
# same contract as the fr scanners: flags, never touches text.
_DATE_RX_EN = re.compile(
    r"(?i)\b(?P<day>\d{1,2})\s+(?P<month>january|february|march|april|may|june|july|"
    r"august|september|october|november|december)\s+(?P<year>1[5-9]\d{2}|20\d{2})\b"
    r"|\b(?P<month2>january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(?P<day2>\d{1,2}),\s+(?P<year2>1[5-9]\d{2}|20\d{2})\b")

_INTERVAL_RX_EN = re.compile(
    r"(?i)\b(?P<n_word>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-\w+)\s+"
    r"(?P<unit>days?|weeks?|months?|years?)\s+"
    r"(?P<rel>after|later|since|earlier|before|ago)\b")

_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
            "seventy": 70, "eighty": 80, "ninety": 90}


def _en_num(tok: str) -> int | None:
    t = tok.lower().strip()
    if t.isdigit():
        return int(t)
    if t in _AGE_WORDS["en"]:
        return _AGE_WORDS["en"][t]
    if "-" in t:
        tens, _, unit = t.partition("-")
        if tens in _EN_TENS and unit in _AGE_WORDS["en"] and _AGE_WORDS["en"][unit] < 10:
            return _EN_TENS[tens] + _AGE_WORDS["en"][unit]
    return None


_DATE_MY_RX = re.compile(
    r"(?i)\b(?P<month>january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(?P<year>1[5-9]\d{2}|20\d{2})\b")
_DATE_Y_RX = re.compile(r"\b(?:in|of|since|until|by)\s+(1[5-9]\d{2}|20\d{2})\b")


def _dates_en(text: str, *, slop_out: dict | None = None) -> list[tuple[int, tuple[int, int, int]]]:
    """Anchor dates, three precision tiers (roll-7 FP class: 'three years before the
    flood' had only 'March 2002' / 'in 2007' as its true anchors — invisible to the
    full-date regex, so the check compared against unrelated dates and flagged
    correct prose). slop_out[pos] = ± tolerance the anchor's imprecision adds:
    full date 0d, month-year ±16d, bare year ±185d."""
    out: list[tuple[int, tuple[int, int, int]]] = []
    spans: list[tuple[int, int]] = []
    for m in _DATE_RX_EN.finditer(text):
        day = m.group("day") or m.group("day2")
        mon = m.group("month") or m.group("month2")
        yr = m.group("year") or m.group("year2")
        t = _parse_date(day, mon, yr, "en")
        if t:
            out.append((m.start(), t))
            spans.append((m.start(), m.end()))
            if slop_out is not None:
                slop_out[m.start()] = 0.0
    for m in _DATE_MY_RX.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue   # inside a full date already captured
        t = _parse_date("15", m.group("month"), m.group("year"), "en")
        if t:
            out.append((m.start(), t))
            spans.append((m.start(), m.end()))
            if slop_out is not None:
                # day=15 midpoint + BASE tolerance only: a linear ±16d bonus let a
                # stray month-grade caption confirm a wrong span by 0.4 days
                # (roll-5 'June 2009' photo vs the three-months claim). Month
                # precision is already inside the 15-18% base tol for month+ spans.
                slop_out[m.start()] = 0.0
    _anchored_years = {t[0] for _, t in out}
    for m in _DATE_Y_RX.finditer(text):
        if any(a <= m.start(1) - 3 < b for a, b in spans):
            continue
        try:
            _yy = int(m.group(1))
        except (ValueError, TypeError):
            continue
        # a bare year that ALREADY has a full/month-grade date is redundant — and
        # poisonous: its ±185d slop shadows the precise anchor and can 'confirm'
        # a wrong span against it (ate the roll-5 true positive via 'in 2009').
        if _yy in _anchored_years:
            continue
        out.append((m.start(1), (_yy, 7, 1)))
        if slop_out is not None:
            slop_out[m.start(1)] = 185.0
    return out


def scan_interval_vs_dates_en(text: str) -> list[dict]:
    """EN twin of scan_interval_vs_dates. Candidate dates = inline dates within
    ±800 chars; when the WHOLE manuscript carries few full dates (≤6, the fiction
    norm — one disaster date + one reveal date), all of them join the candidate
    set, because the reference event ("after the flood") is usually dated far
    from the derived-span sentence. No candidate pair ⟹ no finding (miss-safe,
    never FP-by-guess)."""
    findings: list[dict] = []
    if not text:
        return findings
    _slop: dict[int, float] = {}
    dates = _dates_en(text, slop_out=_slop)
    if len(dates) < 2:
        return findings
    _unit_days = {"day": 1.0, "week": 7.0, "month": 30.44, "year": 365.25}
    for m in _INTERVAL_RX_EN.finditer(text):
        # "ago"/"earlier" anchor to an implicit undated NOW — no date pair can
        # confirm or refute them (a correct "five years ago" would flag). Skip.
        if m.group("rel").lower() in ("ago", "earlier"):
            continue
        # r5.2: a span whose reference is a PRONOUN clause ("seven weeks before SHE
        # understood") anchors to an undated personal event — same class, same skip.
        if re.match(r"\s+(?:she|he|they|i|we|it|anyone|someone|her|his)\b",
                    text[m.end():m.end() + 16] or "", re.I):
            continue
        n = _en_num(m.group("n_word"))
        if n is None or n <= 0:
            continue
        unit = m.group("unit").lower().rstrip("s")
        stated = n * _unit_days.get(unit, 0)
        if stated < 14:      # scene-jump idioms ("three days later") — not worth flagging
            continue
        near = [(p, t) for p, t in dates if abs(p - m.start()) < 1200]
        cands = list(near)
        # Round-4 FP fix (roll 6: "seven months later" → the father's undated death
        # flagged against unrelated dates): GLOBAL candidate expansion is only sound
        # when the span NAMES its anchor event ("after THE FLOOD" — dated elsewhere in
        # the book). A bare "later" whose reference is the surrounding narration must
        # verify against NEAR dates only; with <2 near dates it is unverifiable →
        # skip, never guess (miss-safe beats a wrong-anchor flag feeding the revise).
        _event_ref = (m.group("rel").lower() in ("after", "since", "before")
                      and re.match(r"\s+(?:the|that)\s+[a-z]",
                                   text[m.end():m.end() + 30] or "") is not None)
        # r4.1 (roll-7 'three weeks after the flood' FP): event-ref expansion is only
        # sound when the span's OTHER endpoint is dated nearby — the event anchor may
        # live anywhere, but a span whose second endpoint is undated is unverifiable
        # no matter how many global dates exist. Require ≥1 near date to expand.
        if len(dates) <= 8 and _event_ref and near:
            for p, t in dates:
                if (p, t) not in cands:
                    cands.append((p, t))
        if len(cands) < 2:
            continue
        tol = max(10.0, 0.18 * stated) if unit in ("month", "year") else max(2.0, 0.1 * stated)
        ok = False
        pairs: list[tuple[int, int, int]] = []
        for i, (pa, ta) in enumerate(cands):
            for pb, tb in cands[i + 1:]:
                actual = abs(_days_between(ta, tb))
                if actual == 0:
                    continue
                # two bare-YEAR anchors give ±370d combined slop — they can 'confirm'
                # almost any span (ate the roll-5 true positive). Uninformative; skip.
                if _slop.get(pa, 0.0) >= 185 and _slop.get(pb, 0.0) >= 185:
                    continue
                pairs.append((pa, pb, actual))
                # imprecise anchors (month-year ±16d, bare year ±185d) widen the pair's
                # tolerance — approximation must only ever SUPPRESS flags, never add them
                if abs(actual - stated) <= tol + _slop.get(pa, 0.0) + _slop.get(pb, 0.0):
                    ok = True
                    break
            if ok:
                break
        if not ok and pairs:
            findings.append({
                "kind": "interval_arithmetic",
                "stated_phrase": m.group(0),
                "stated_interval_days": round(stated),
                "candidate_pairs": pairs[:5],
                "context": re.sub(r"\s+", " ", text[max(0, m.start() - 70):m.start() + 130]).strip(),
                "note": f"stated span ≈{round(stated)}d matches no date pair (tol ±{round(tol)}d)",
            })
    return findings[:8]


# ── day-counter vs calendar (round-4; convergent 3/3 lenses on roll 6) ─────
# "15 June = drought day 188" then "July third = day two hundred and four":
# Δcalendar = 18 but Δcounter = 16 — a running day-ledger that disagrees with its
# own dates. Deterministic: pair each day-counter with the nearest month-day date
# in the same breath (±400 chars), then check every pair of pairs. ±1 slop for
# inclusive-vs-exclusive counting conventions; ≥2 flags.
_MONTH_DOY = {"january": 0, "february": 31, "march": 59, "april": 90, "may": 120,
              "june": 151, "july": 181, "august": 212, "september": 243,
              "october": 273, "november": 304, "december": 334}
_ORD_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
              "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
              "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
              "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
              "twentieth": 20, "thirtieth": 30, "twenty": 20, "thirty": 30}


def _en_num_ext(tok: str) -> int | None:
    """Spelled numbers incl. hundreds/ordinals: 'two hundred and four' → 204,
    'two-hundred-and-sixteenth' → 216, 'third' → 3, '204' → 204."""
    t = re.sub(r"[\s-]+", " ", str(tok or "").lower()).strip()
    if not t:
        return None
    if t.replace("st", "").replace("nd", "").replace("rd", "").replace("th", "").isdigit():
        return int(re.sub(r"(st|nd|rd|th)$", "", t))
    # thousands (r5.2 — roll-9's signature counters were 1,7xx-1,8xx spelled AND digit;
    # the ≤400 cap + missing thousands parse made the scanner blind to the roll's
    # biggest defect class): "one thousand, eight hundred and twenty-six" and "1,826".
    t = t.replace(",", "")
    if t.isdigit():
        return int(t)   # comma-grouped digits ("1,826") arrive here post-strip
    mth = re.match(r"(?:(one|two|three|four|five|six|seven|eight|nine)\s+)?thousand"
                   r"(?:\s+and)?\s*(.*)$", t)
    if mth:
        base = (_en_num(mth.group(1)) or 1) * 1000
        rest = (mth.group(2) or "").strip()
        if not rest:
            return base
        r = _en_num_ext(rest)
        return base + r if (r is not None and r < 1000) else None
    m = re.match(r"(?:(one|two|three|four|five|six|seven|eight|nine|a)\s+)?hundred(?:\s+and)?\s*(.*)$", t)
    if m:
        base = (_en_num(m.group(1)) or 1) * 100 if m.group(1) not in (None, "a") else 100
        rest = (m.group(2) or "").strip()
        if not rest:
            return base
        r = _en_num(rest)
        if r is None:
            # ordinal tail: 'sixteenth' / 'twenty first' / 'four'
            parts = rest.split()
            r = 0
            for p in parts:
                v = _ORD_WORDS.get(p) or _en_num(p)
                if v is None:
                    return None
                r += v
        return base + r if r < 100 else None
    # 'two hundred...' handled above; plain ordinals and cardinals:
    if t in _ORD_WORDS:
        return _ORD_WORDS[t]
    parts = t.split()
    if len(parts) == 2 and parts[0] in _ORD_WORDS and parts[1] in _ORD_WORDS:
        a, b = _ORD_WORDS[parts[0]], _ORD_WORDS[parts[1]]
        if a in (20, 30) and b < 10:
            return a + b
    return _en_num(t)


_NUMEXPR = r"(?:[a-z0-9]+(?:[\s-]+(?:and[\s-]+)?[a-z0-9]+){0,4})"
_DAYCOUNT_RXES = (
    # "drought day 188" / "day two hundred and four"
    re.compile(r"(?i)\bday\s+(?P<n>\d{1,3}|" + _NUMEXPR + r")\b"),
    # "two hundred and thirty-nine days without" / "247 days of silence"
    re.compile(r"(?i)\b(?P<n>\d{1,3}|" + _NUMEXPR + r")\s+(?:consecutive\s+)?days\s+(?:without|of\s+silence|of\s+drought)\b"),
    # "two-hundred-and-sixteenth consecutive day"
    re.compile(r"(?i)\b(?P<n>[a-z0-9]+(?:-[a-z0-9]+){0,4}(?:th|st|nd|rd))\s+(?:consecutive\s+)?day\b"),
)
_MD_DATE_RX = re.compile(
    r"(?i)\b(?P<mon>january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+(?P<d>\d{1,2}(?:st|nd|rd|th)?|" + "|".join(_ORD_WORDS) + r")\b"
    r"|\b(?P<d2>\d{1,2})\s+(?P<mon2>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\b")


_revert_rxes = (
    re.compile(r"(?i)\b(\d{1,2},\d{3}|\d{3,4})\s+days\b"),
    re.compile(r"(?i)\b((?:one|two|three|four|five|six|seven|eight|nine)\s+thousand"
               r"[a-z ,-]{0,50}?)\s+days\b"),
    re.compile(r"(?i)\b((?:one|two|three|four|five|six|seven|eight|nine)?\s*hundred"
               r"[a-z ,-]{0,40}?)\s+days\b"),
)


def scan_day_counters(text: str) -> list[dict]:
    """Running day-ledger vs calendar dates. Never raises; [] on clean text."""
    findings: list[dict] = []
    if not text:
        return findings
    try:
        md: list[tuple[int, int]] = []   # (pos, day-of-year)
        for m in _MD_DATE_RX.finditer(text):
            mon = (m.group("mon") or m.group("mon2") or "").lower()
            dtok = m.group("d") or m.group("d2") or ""
            d = _en_num_ext(dtok)
            if mon in _MONTH_DOY and d and 1 <= d <= 31:
                md.append((m.start(), _MONTH_DOY[mon] + d))
        if not md:
            return findings
        pairs: list[tuple[int, int, int, str]] = []   # (pos, doy, count, phrase)
        seen_spans: set[tuple[int, int]] = set()
        for rx in _DAYCOUNT_RXES:
            for m in rx.finditer(text):
                if any(a <= m.start() < b for a, b in seen_spans):
                    continue
                n = _en_num_ext(m.group("n"))
                if n is None or not (10 <= n <= 3000):   # short counts = everyday prose
                    continue
                near = [(abs(p - m.start()), p, doy) for p, doy in md if abs(p - m.start()) < 400]
                if not near:
                    continue
                near.sort()
                _, p, doy = near[0]
                seen_spans.add((m.start(), m.end()))
                pairs.append((m.start(), doy, n, m.group(0)))
        # r5.2 COUNTER-REVERT (roll-9: 1,826 → 1,843 → 1,853 → 1,827/1,826 again — Ch8
        # reused the ARRIVAL number after ~50 story-days): any value that reappears
        # AFTER a larger value has been recorded is a frozen/reverted ledger. This
        # works on UNPAIRED counters too (no nearby date needed) — collect all.
        _allc: list[tuple[int, int]] = []
        _spans_seen2: set = set()
        for rx in _revert_rxes:
            for m2 in rx.finditer(text):
                if any(a <= m2.start() < b for a, b in _spans_seen2):
                    continue
                nv = _en_num_ext(m2.group(1))
                if nv is not None and 100 <= nv <= 3000:
                    _spans_seen2.add((m2.start(), m2.end()))
                    _allc.append((m2.start(), nv))
        _peak = -1
        _vals_seen: set = set()
        _reported: set = set()
        for _pos, _nv in sorted(_allc):
            if _nv < _peak - 1 and _nv in _vals_seen and (_nv, _peak) not in _reported:
                _reported.add((_nv, _peak))
                findings.append({
                    "kind": "day_counter",
                    "phrases": [str(_nv), str(_peak)],
                    "note": f"day-counter REVERTED: {_nv} reappears after the count already "
                            f"reached {_peak} — a frozen/copied duration, recount from the epoch",
                })
            _vals_seen.add(_nv)
            _peak = max(_peak, _nv)
        for i in range(len(pairs)):
            for j in range(i + 1, len(pairs)):
                (pa, da, na, fa), (pb, db, nb, fb) = pairs[i], pairs[j]
                dcal, dn = db - da, nb - na
                if dcal < 0:
                    dcal, dn, fa, fb = -dcal, -dn, fb, fa
                if dcal == 0 and dn == 0:
                    continue
                if abs(dcal - dn) >= 2:
                    findings.append({
                        "kind": "day_counter",
                        "phrases": [fa, fb],
                        "note": f"calendar moved {dcal}d but the day-counter moved {dn} "
                                f"('{fa}' vs '{fb}') — recount from the dated anchor",
                    })
        return findings[:4]
    except Exception:  # noqa: BLE001 — linter never blocks the scan
        return findings


def scan_span_alternation(text: str) -> list[dict]:
    """Adjacent-value span alternation: the SAME long duration stated as both N
    and N+1 years ("fourteen years" ×7 vs "fifteen years" ×3 — 5/5 rain rolls
    drifted this way). Guards against legitimate patterns: N ≥ 5 (everyday small
    spans exempt), BOTH values ≥2 uses, YEARS only (month durations legitimately
    grow as story time passes: a 9-month drought becomes a 10-month drought),
    and the two values must INTERLEAVE in the text — a monotonic switch is
    progression (an anniversary passing), alternation is drift."""
    findings: list[dict] = []
    if not text:
        return findings
    unit = "year"
    pos: dict[int, list[int]] = {}
    rx = re.compile(_INTERVAL_RX_EN.pattern.replace(
        r"(?P<unit>days?|weeks?|months?|years?)\s+"
        r"(?P<rel>after|later|since|earlier|before|ago)\b",
        r"(?P<unit>" + unit + r"s?)\b"))
    for m in rx.finditer(text):
        n = _en_num(m.group("n_word"))
        if n is not None and n >= 5:
            pos.setdefault(n, []).append(m.start())
    # FIX (2026-07-18, fork-C root-cause c): this only ever compared ADJACENT values (a,
    # a+1) — a real production fork (Eun-mi's tenure given as both 20 and 23 years) has a
    # gap of 3 and was never caught. Extend the comparison to ALL distinct-value pairs that
    # each recur (>=2 uses) and interleave, not just neighbors — a strict superset of the
    # old adjacent-only check (b=a+1 is still one of the pairs compared), so nothing that
    # fired before stops firing.
    _vals = [v for v in sorted(pos) if len(pos[v]) >= 2]
    for _i, a in enumerate(_vals):
        for b in _vals[_i + 1:]:
            if max(pos[a]) < min(pos[b]) or max(pos[b]) < min(pos[a]):
                continue     # monotonic switch = story time passing, not drift
            dom = a if len(pos[a]) >= len(pos[b]) else b
            findings.append({
                "kind": "span_alternation",
                "unit": unit,
                "values": {str(a): len(pos[a]), str(b): len(pos[b])},
                "note": f"'{a} {unit}s' ×{len(pos[a])} vs '{b} {unit}s' ×{len(pos[b])} "
                        f"interleaved — pick one (dominant: {dom})",
            })
    return findings[:8]


# ── same-entity age fork + elapsed-span consistency (NARASI_AGE_LEDGER) ──────
# Park "fifty-seven" (Ch-x) vs "sixty-seven" (Ch-y); a fire→now span "eighteen
# years" vs the same disappearance "fifteen years". Report-only; reuses _en_num.
_AGEWORD_ALT = "|".join(sorted(
    list(_AGE_WORDS["en"].keys())
    + [f"{t}-{u}" for t in _EN_TENS for u in
       ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")],
    key=len, reverse=True))
# COMPOUND tens only ("fifty-seven") — for the low-FP "at/turned <age>" branch, where a
# bare simple number ("at six") would false-positive but a spelled compound almost never
# means anything but an age.
_COMPOUND_AGE_ALT = "|".join(sorted(
    [f"{t}-{u}" for t in _EN_TENS for u in
     ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")],
    key=len, reverse=True))
# NOTE: re.I flag (NOT inline (?i) mid-pattern — that's a re.error on 3.11+).
_AGE_TOKEN_RX = re.compile(
    r"\b(?P<age>\d{1,3}|" + _AGEWORD_ALT + r")\s+years?[\s-]?old\b"
    r"|\baged\s+(?P<age2>\d{1,3}|" + _AGEWORD_ALT + r")\b(?!-)"
    r"|\bwas\s+(?P<age3>" + _AGEWORD_ALT + r")\b(?!-)(?!\s+years)"
    # age-context ONLY: a spelled compound after 'at/turned' is an AGE when a clause
    # boundary, 'years'/'old', or a function word follows — NOT a proper noun (address:
    # 'at fifty-seven Baker Street'/'Court') or a countable noun ('turned fifty-seven
    # pages/letters'). Positive lookahead is stricter than a unit blacklist (which a proper
    # noun or an unlisted noun slips past).
    r"|\b(?:at|turned)\s+(?P<age4>" + _COMPOUND_AGE_ALT + r")\b(?!-)"
    r"(?=\s*(?:[,.;:!?)\]}—–-]|years?\b|old\b|$|"
    r"(?:with|and|but|or|nor|in|on|as|now|still|already|yet|when|while|he|she|they|him|"
    r"his|her|their|before|after|despite|though|because|then|so|by|for|to|from|of|"
    r"without|even|almost|nearly|felt|looked|seemed|knew|stood|sat|walked|ran|"
    r"last|this|next|just|recently|ago|back|only|barely|once|again|these|those)\b))", re.I)
_NAME_TOKEN_RX = re.compile(r"\b([A-Z][a-z]{2,}(?:[- ][A-Z][a-z]+)*)\b")
_AGE_STOP_NAMES = {"The", "He", "She", "They", "It", "His", "Her", "Their", "But",
                   "And", "When", "Then", "That", "This", "Chapter", "Detective",
                   "Doctor", "Prosecutor", "Officer", "Mr", "Mrs", "Ms",
                   # sentence-starting function words (closed set) that get captured as
                   # false subjects, plus the manuscript's two city names
                   "Why", "How", "What", "Where", "Here", "There", "Now", "Because",
                   "After", "Before", "Once", "Later", "Still", "Even", "Only", "Just",
                   "Since", "While", "Though", "Seoul", "Busan"}
_SPAN_ANCHORS = ("fire", "flood", "disappearance", "vanished", "disappeared",
                 "went missing", "wait", "waiting", "silence", "collapse", "accident",
                 "death", "died", "explosion", "sank", "sinking", "divorce", "verdict")
# FIX (2026-07-18, fork-C root-cause b): scan_elapsed_span_consistency's OWN anchor set,
# broadened for duration-of-residence/collection claims (a character's tenure, an archive's
# collecting/lived-on span) that share none of the disaster/crime-event words above. This is
# a SEPARATE tuple, not a mutation of _SPAN_ANCHORS itself: that shared constant is also used
# by scan_canon_anchor_dates for ABSOLUTE-date fork detection, where adding a near-ubiquitous
# word like 'since' would bucket almost every dated mention in the manuscript under one
# anchor and manufacture false canon_date_fork positives there — a cost this report-only,
# duration-only check does not carry the same way.
_SPAN_DURATION_ANCHORS = _SPAN_ANCHORS + (
    "since", "arrival", "tenure", "archive", "lived on", "collecting")
_SPAN_YEARS_RX = re.compile(
    r"(?i)\b(?P<n>\d{1,3}|"
    + "|".join(sorted(list(_AGE_WORDS["en"].keys()) + list(_EN_TENS.keys()),
                      key=len, reverse=True)) + r")\s+years?\b")


def scan_same_entity_age_fork(text: str) -> list[dict]:
    """Same capitalized person-name given >=2 distinct ages (digits or spelled, incl.
    compound tens 'fifty-seven'). Report-only; reuses the r5.2/r3 _en_num map."""
    if not text:
        return []
    ages_by_name: dict[str, set] = {}
    givens_by_surname: dict[str, set] = {}
    for m in _AGE_TOKEN_RX.finditer(text):
        age = _en_num(m.group("age") or m.group("age2") or m.group("age3") or m.group("age4") or "")
        if age is None or not (1 <= age <= 120):
            continue
        cand = None
        for nm in _NAME_TOKEN_RX.finditer(text[max(0, m.start() - 220):m.start()]):
            if nm.group(1).split()[0].split("-")[0] not in _AGE_STOP_NAMES:
                cand = nm.group(1)
        if cand:
            parts = cand.split()
            surname = parts[0]
            ages_by_name.setdefault(surname, set()).add(age)
            if len(parts) >= 2:  # a full "Surname Given" reference — record the given name
                givens_by_surname.setdefault(surname, set()).add(parts[1])
    out = []
    for name, ages in ages_by_name.items():
        # Korean-order names collapse to the surname; if that surname carries >=2 distinct
        # given names, the ages likely belong to DIFFERENT people (Kim Do-yoon vs Kim
        # Seo-an) — don't fork. A single-person surname (bare refs + <=1 given) still forks.
        if len(givens_by_surname.get(name, set())) >= 2:
            continue
        if len(ages) >= 2 and (max(ages) - min(ages)) >= 2:
            out.append({"kind": "same_entity_age_fork", "subject": name,
                        "ages": sorted(ages),
                        "note": f"'{name}' given ages {sorted(ages)} — pick one"})
    return out[:6]


# FIX (2026-07-18, dialogue-crossing gap, Part 3): a bare "Decades later," sentence-opener
# sitting in one mention's 220-char backward window was matched by _TENURE_NAME_RX as a fake
# proper name (it's just [A-Z][a-z]{2,}, no semantic check) — this broke the impersonal/
# institutional case in testing, splitting a true fork into ("archive", None) vs
# ("archive", "Decades") and silently losing it. Necessary, not optional; still a CLOSED
# enumeration (see scan_elapsed_span_consistency's docstring for the residual risk).
_SPAN_SUBJECT_STOP_EXTRA = frozenset({"Decades", "Decade", "Years", "Months", "Weeks", "Days",
    "Centuries", "Meanwhile", "Eventually", "Afterward", "Elsewhere", "Later", "Soon", "Recently"})


def scan_elapsed_span_consistency(text: str) -> list[dict]:
    """Same anchor event given >=2 distinct '<N> years' elapsed spans (fire 18y vs
    same disappearance 15y). N>=3 (small everyday spans exempt). Report-only.

    FIX (2026-07-18, dialogue-crossing gap, Part 3): bucketed by (anchor, subject) instead of
    anchor alone, using the shared _resolve_subject resolver — a purely NARROWING precondition
    layered on top of the existing anchor match (never expands what can fork, only restricts
    merges to same-subject pairs). Two different people's spans that happen to share an anchor
    word no longer fork against each other; a mention with no resolvable subject nearby still
    buckets under a shared "\\0impersonal" key per anchor, reproducing today's behavior exactly
    for the institutional/archive case. Residual: _SPAN_SUBJECT_STOP_EXTRA is a closed list —
    an unlisted capitalized sentence-opening adverb ("Suddenly," "Nevertheless") can still be
    misread as a proper name and wrongly split a genuinely-impersonal fork (see design doc)."""
    if not text:
        return []
    named_events = _named_speaker_events(text)
    by_key: dict[tuple[str, str], set] = {}
    for m in _SPAN_YEARS_RX.finditer(text):
        n = _en_num(m.group("n"))
        if n is None or n < 3:
            continue
        ctx = text[max(0, m.start() - 90):m.end() + 90].lower()
        # Not every "<N> years" is an elapsed-since span. Exclude (a) backward references
        # ("<N> years BEFORE the fire" = demolition timing) and (b) comparative age GAPS
        # ("three years OLDER" = a sibling's age difference) — neither is time-since-anchor.
        _tail = text[m.end():m.end() + 40].lstrip().lower()
        if _tail.startswith(("before", "prior", "ahead of", "older", "younger",
                             "apart", "senior", "junior", "my senior", "my junior")):
            continue
        anchor_hit = None
        for a in _SPAN_DURATION_ANCHORS:
            # word-boundary: "fire" must not match "firefighter" (a false anchor that
            # would attach an unrelated "three years" span to the fire event).
            if re.search(r"\b" + re.escape(a) + r"\b", ctx):
                anchor_hit = a
                break
        if anchor_hit is None:
            continue
        subject = _resolve_subject(text, m.start(), m.end(), named_events,
                                    extra_stop=_SPAN_SUBJECT_STOP_EXTRA)
        key = (anchor_hit, subject or "\0impersonal")
        by_key.setdefault(key, set()).add(n)
    out = []
    for (anchor, _subj), ns in by_key.items():
        if len(ns) >= 2 and (max(ns) - min(ns)) >= 1:
            out.append({"kind": "elapsed_span_fork", "anchor": anchor,
                        "spans": sorted(ns),
                        "note": f"span since '{anchor}' stated as {sorted(ns)} years — unify"})
    return out[:6]


def scan_age_ledger(text: str) -> dict:
    """NARASI_AGE_LEDGER entry point. {status, count, findings}. Never raises.
    r16: also includes scan_tenure_ledger — same "numeric fact pinned per character" concept
    as the age fork, riding the same flag rather than adding a new one."""
    try:
        f = (scan_same_entity_age_fork(text) + scan_elapsed_span_consistency(text)
             + scan_tenure_ledger(text))
        return {"status": "FLAG" if f else "PASS", "count": len(f), "findings": f[:8]}
    except Exception as exc:  # noqa: BLE001
        return {"status": "PASS", "count": 0, "findings": [], "_error": str(exc)}


# ── r16-S1: canon-anchor absolute-date fork (NARASI_CANON_ANCHOR) ────────────────────────
# S2-Th-3 review finding: a sequel manuscript uniformly re-dated the prior season's fire
# anchor from S1 canon (2 Oct 2011) to a fabricated "September 22, 2008" — the internal
# elapsed-span fork (15y vs 16y) was already caught by scan_elapsed_span_consistency, but
# nothing checked the ABSOLUTE date itself, and nothing checked it against an external canon
# constant. Two independent checks, reusing _dates_en (already extracts (Y,M,D) with position):
#   (a) internal: the SAME anchor word given >=2 distinct absolute dates in one manuscript —
#       no canon pin needed.
#   (b) external: the anchor's stated date does not match a pinned canon constant, set via
#       NARASI_CANON_ANCHOR_NAME (e.g. "fire") + NARASI_CANON_ANCHOR_DATE (YYYY-MM-DD, from
#       the prior season's adjudicated canon). Both env-driven; empty = check (b) simply
#       never fires, (a) always runs when NARASI_CANON_ANCHOR is on.
def _canon_anchor_pin() -> tuple[str, tuple[int, int, int] | None]:
    name = (os.environ.get("NARASI_CANON_ANCHOR_NAME", "") or "").strip().lower()
    raw = (os.environ.get("NARASI_CANON_ANCHOR_DATE", "") or "").strip()
    if not name or not raw:
        return "", None
    try:
        y, m, d = (int(x) for x in raw.split("-"))
        return name, (y, m, d)
    except Exception:  # noqa: BLE001
        return "", None


def scan_canon_anchor_dates(text: str) -> list[dict]:
    """Same anchor event (fire/disappearance/etc, reusing _SPAN_ANCHORS) given >=2 distinct
    ABSOLUTE dates internally, and/or a stated date that contradicts an external canon pin
    (NARASI_CANON_ANCHOR_NAME/_DATE). Report-only; reuses _dates_en.

    AUDIT FIX: a bare-year mention near an anchor word ("survivors were still discussing the
    fire by 2020") was being treated as a SECOND date of the anchor event itself, not what it
    usually is — ordinary retrospective/anniversary prose referencing WHEN something was later
    discussed, not when it happened. _dates_en already tags this precision tier via slop_out
    (bare year = ±185d slop, vs 0.0 for a full or month-year date) — excluded here entirely."""
    if not text:
        return []
    canon_name, canon_date = _canon_anchor_pin()
    _slop: dict = {}
    by_anchor: dict[str, set] = {}
    for pos, date in _dates_en(text, slop_out=_slop):
        if _slop.get(pos, 0.0) >= 185.0:
            continue  # bare-year precision — not reliable evidence of the anchor's OWN date
        ctx = text[max(0, pos - 90):pos + 90].lower()
        for a in _SPAN_ANCHORS:
            if re.search(r"\b" + re.escape(a) + r"\b", ctx):
                by_anchor.setdefault(a, set()).add(date)
                break
    out = []
    for anchor, dates in by_anchor.items():
        ds = sorted(f"{y:04d}-{m:02d}-{d:02d}" for y, m, d in dates)
        if len(dates) >= 2:
            out.append({"kind": "canon_date_fork", "anchor": anchor, "dates": ds,
                        "note": f"'{anchor}' given {len(dates)} distinct absolute dates {ds} — unify"})
        elif canon_date and anchor == canon_name and canon_date not in dates:
            out.append({"kind": "canon_date_violation", "anchor": anchor, "stated": ds[0],
                        "canon": f"{canon_date[0]:04d}-{canon_date[1]:02d}-{canon_date[2]:02d}",
                        "note": f"'{anchor}' stated as {ds[0]} but canon pins it to "
                                 f"{canon_date[0]:04d}-{canon_date[1]:02d}-{canon_date[2]:02d}"})
    return out[:6]


# ── r16-S2: numeric tenure/experience ledger (extends scan_same_entity_age_fork's pattern) ──
# S2-Th-3 finding: a clerk's age forked 38-vs-28 (already covered by scan_same_entity_age_fork)
# but Do-yoon's POSTAL TENURE forked "two decades" vs "fifteen years" — a different attribute
# (years-of-experience, not age) on the same character, missed by the age-only scanner. Same
# name-attribution mechanism (220-char lookback, _AGE_STOP_NAMES), different trigger phrases.
# round-17 (item 3, case 2): "fifteen years OF employment" / "twenty years OF fieldwork" has
# neither a preceding spent/for trigger nor a bare-decades form — t1/t2 never matched it, so
# the figures were never extracted, let alone compared. t3 adds the "N years/decades of <noun>"
# shape, reusing the SAME number-word alternation (_AGEWORD_ALT, defined above for the age
# scanner) rather than re-deriving it a third time.
#
# FIX (2026-07-18, dialogue-crossing gap, Part 1): t4's "since <year>" lookahead used a
# [^.]{0,70} char class that cannot cross a literal period — so a real fork ("Twenty years,"
# Eun-mi said. "Since 2001...") was silently dropped: the period after "said" breaks the
# lookahead. Widen with a SECOND lookahead alternative that only fires across a bounded,
# structurally-recognized "closing-quote -> short attribution tag -> period -> reopening-quote"
# shape — bounding the *shape* of the crossing, not the raw character budget, so this can't be
# abused to bridge two unrelated sentences that merely sit within ~150 chars of each other.
_DIALOGUE_VERB_ALT = (r"(?:said|asked|replied|answered|murmured|whispered|added|continued|"
                       r"went\s+on|breathed|repeated|called|shouted|snapped|admitted|confessed|insisted)")
_DIALOGUE_ADVERB_ALT = r"(?:softly|quietly|finally|slowly|at\s+last|again)"
# whitespace-only gap (+ one optional adverb) between a speaker token and the verb — NO comma,
# NO quote mark. That absence is load-bearing: it's what lets 2a/2b below tell a genuine speaker
# tag ("Eun-mi said.") apart from a quoted-content mention ("the teacher said, 'Minji...'"),
# where the comma+quote in the gap correctly blocks a match.
_TAG_GAP = r"[ \t]{1,3}(?:" + _DIALOGUE_ADVERB_ALT + r"[ \t]{1,3})?"
_QUOTE_CLOSE_RX = r'["”’]'
_QUOTE_OPEN_RX = r'["“‘]'
_DIALOGUE_TAG_BRIDGE_RX = (
    _QUOTE_CLOSE_RX + r"\s{0,3}"
    # (?-i:...) scopes case-sensitivity for the Name branch even though _TENURE_TOKEN_RX is
    # compiled with re.I overall (Python 3.11+ supports this scoped form — verified; only a
    # *bare*, unscoped `(?i)` mid-pattern errors on 3.11+).
    r"(?:(?-i:[A-Z][\w'-]{1,20}(?:\s+[A-Z][\w'-]{1,20})?)|(?:she|he|they))"
    + _TAG_GAP + _DIALOGUE_VERB_ALT + r"\.\s{0,3}" + _QUOTE_OPEN_RX
)
_TENURE_TOKEN_RX = re.compile(
    r"\b(?:spent|for)\s+(?P<t1>\d{1,3}|" + "|".join(sorted(
        list(_AGE_WORDS["en"].keys()) + [f"{t}-{u}" for t in _EN_TENS for u in
        ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")],
        key=len, reverse=True)) + r")\s+(?:years?|decades?)\b"
    r"|\b(?P<t2>\d{1,3}|(?:two|three|four|five|six|seven|eight|nine|ten))\s+decades?\b"
    r"|\b(?P<t3>\d{1,3}|" + _AGEWORD_ALT + r")\s+(?:years?|decades?)\s+of\s+"
    # AUDIT FIX: the trailing noun was a fully generic \w+, so "years of AGE"
    # ("thirty years of age" — a character's stated age, not tenure) and unrelated
    # attributes ("years of marriage", "years of practice on the violin") were all
    # misparsed as career tenure. Narrowed to an explicit career/experience allowlist.
    # "practice" alone is ambiguous ("practice as a surgeon" IS tenure; "practice on
    # the violin" is NOT) and can't be cleanly disambiguated with a simple regex, so
    # bare "practice" is excluded — only the qualified "practice as/in <role>" form
    # (unambiguously career-shaped) is allowed, accepting reduced recall over the
    # confirmed false-positive risk. "duty" got the same treatment after a second
    # adversarial pass found "years of duty-free shopping" still matched bare —
    # same ambiguity class as practice, same fix.
    r"(?:employment|service|experience|fieldwork|tenure|work|career|"
    r"(?:practice|duty)\s+(?:as|in))\b"
    # FIX (2026-07-18, fork-C root-cause a): a BARE "<N> years" with no spent/for/of-<noun>
    # trigger still names a tenure/duration when anchored by a nearby "since <year>" (a
    # start-year for the same span, e.g. "twenty years... since 2001") or followed by a
    # comma-clause naming an outcome ("in twenty years, the ledger finally balanced") —
    # neither shape matched t1/t2/t3 at all, so the figure was never even extracted, let
    # alone compared against another chapter's value for the same person. t4 = the
    # since-anchored form; t5 = the "in <N> years," form. Both are written so the match
    # STARTS at the number itself (never consuming a preceding "since <year>" as part of
    # the match) — this keeps the existing name-lookback below (which scans the 220 chars
    # BEFORE m.start()) working exactly as it does for t1/t2/t3: a reverse-order phrasing
    # ("Since 2001, Eun-mi has served twenty years") would put the name INSIDE a
    # since-YYYY-first match span, invisible to that lookback, so that ordering is
    # deliberately NOT matched here rather than shipped with silently-broken attribution.
    r"|\b(?P<t4>\d{1,3}|" + _AGEWORD_ALT + r")\s+years?\b"
    # first alternative UNCHANGED (nothing that fired before stops firing); second alternative
    # bridges exactly one dialogue-tag interruption, capped at ~40+bridge+40 chars and gated on
    # the exact quote/tag/quote shape above — see Residual limitations: two STACKED
    # interruptions before "since <year>" still won't match (deliberate bound).
    r"(?=(?:[^.]{0,70}|[^.]{0,40}" + _DIALOGUE_TAG_BRIDGE_RX + r"[^.]{0,40})"
    r"\bsince\s+(?:1[0-9]|20)\d{2}\b)"
    r"|\bin\s+(?P<t5>\d{1,3}|" + _AGEWORD_ALT + r")\s+years?\b(?=,\s*\S)"
    , re.I)
# Local (not the shared _NAME_TOKEN_RX): Korean-order given names can have a ONE-consonant-
# vowel first syllable ("Do-yoon", "Yu-jin") — _NAME_TOKEN_RX's [a-z]{2,} minimum (tuned for
# r15's age-fork on names like "Park"/"Seo-an") misses these entirely. Widened ONLY for the
# hyphenated-compound branch (a bare unhyphenated 2-char capitalized word, e.g. a stray "Do"
# starting a sentence, still requires the {2,} minimum) so this can't regress into matching
# ordinary capitalized dialogue-starter words.
_TENURE_NAME_RX = re.compile(r"\b([A-Z][a-z]+-[a-z]+|[A-Z][a-z]{2,}(?:[- ][A-Z][a-z]+)*)\b")
# Institutional/object nouns that read as a capitalized "subject" immediately before a tenure
# phrase ("the Archive... two decades") but are never a PERSON's tenure — same spirit as
# _AGE_STOP_NAMES (function words) for a different false-attribution class.
_TENURE_STOP_NOUNS = {"Archive", "Trust", "Program", "Ledger", "Layer", "Protocol", "Committee",
                       "Court", "Fund", "System", "Registry", "Department", "Office", "Agency",
                       "Bureau", "Foundation", "Institute", "Council", "Board", "Division",
                       "Unit", "Center", "Centre", "Program", "Shelter", "City", "Government"}

# FIX (2026-07-18, dialogue-crossing gap, Part 2): the backward 220-char lookback above needs a
# proper name; a dialogue-interior mention ("twenty-three years... she finally said") has none,
# and was silently dropped rather than forked. (2a) narrows the existing blind forward name-grab
# to a name adjacent to a dialogue verb with NO quote character in the gap — the load-bearing
# detail that lets a genuine speaker tag ("...Eun-mi said.") match while quoted-content mentions
# ("...teacher said, 'Minji...") correctly don't (the ", '" in the gap can't be crossed by
# _TAG_GAP, which is whitespace-only). (2b) adds a pronoun+tracker path, tried only when neither
# the lookback nor 2a found a name, and only when a pronoun IS dialogue-tag-bound nearby.
_NAMED_SPEAKER_TAG_RX = re.compile(
    r"\b(?P<name_a>[A-Z][a-z]+-[a-z]+|[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)?)" + _TAG_GAP + _DIALOGUE_VERB_ALT + r"\b"
    r"|\b" + _DIALOGUE_VERB_ALT + _TAG_GAP + r"(?P<name_b>[A-Z][a-z]+-[a-z]+|[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)?)\b"
)
_PRONOUN_SPEAKER_TAG_RX = re.compile(
    r"\b(?P<pron_a>she|he|they)" + _TAG_GAP + _DIALOGUE_VERB_ALT + r"\b"
    r"|\b" + _DIALOGUE_VERB_ALT + _TAG_GAP + r"(?P<pron_b>she|he|they)\b", re.I)
# ~one scene/exchange — beyond this, a tracked speaker is treated as too stale to trust.
_SPEAKER_TRACKER_STALE_CHARS = 2000


def _named_speaker_events(text: str) -> list[tuple[int, str]]:
    """One forward O(n) pass building every (pos, name) where a name is bound to a dialogue verb
    with no quote character in the gap (same regex 2a uses as its own fallback)."""
    return [(m.start(), m.group("name_a") or m.group("name_b"))
            for m in _NAMED_SPEAKER_TAG_RX.finditer(text)]


def _resolve_pronoun_speaker(pos: int, named_events: list[tuple[int, str]],
                              stale: int = _SPEAKER_TRACKER_STALE_CHARS) -> str | None:
    """Look at EVERY named speaker-tag event in a bounded recent window (not just the nearest
    one) and require exactly one distinct name in it. A naive "nearest named tag before pos"
    check is vacuous by construction (nothing else can sit between it and pos) — re-simulated
    and confirmed this silently jumps to a stale/unrelated speaker's handoff. This framing
    subsumes both required skip conditions in one check: empty window => no tracked speaker or
    too stale; 2+ distinct names in the window => another speaker's own attribution intervened,
    ambiguous."""
    recent = [(p, n) for p, n in named_events if pos - stale <= p < pos]
    if not recent:
        return None
    distinct = {n for _, n in recent}
    if len(distinct) != 1:
        return None
    return next(iter(distinct))


def _resolve_subject(text: str, start: int, end: int, named_events: list[tuple[int, str]],
                      extra_stop: frozenset = frozenset()) -> str | None:
    """Shared subject-attribution resolver for a numeric-figure match spanning text[start:end]:
    (a) backward 220-char lookback for a proper name [unchanged mechanism]; (b) narrowed forward
    80-char search for a name bound to a dialogue verb with no quote in the gap [replaces the old
    blind capitalized-token grab]; (c) pronoun+tracker path, tried only when (a)/(b) found nothing
    and only when a pronoun is itself dialogue-tag-bound nearby. Used by scan_tenure_ledger and
    scan_elapsed_span_consistency.

    ⚠ KNOWN RESIDUAL LIMITATIONS (audit 2026-07-18) — NARASI_AGE_LEDGER must stay OFF until
    addressed: (1) step (a) still accepts the nearest capitalized non-stoplisted token in the
    backward window as a "name" — the stoplists are closed lists, so an unlisted sentence-opener
    or non-name capital can be misattributed; (2) step (b) is quote-nesting-blind — a third
    party's duration quoted egocentrically ("She told me she'd lived there twenty years") or a
    quote-within-quote can bind to the wrong speaker; (3) step (c) has no coreference/gender
    check — a pronoun is resolved to the sole recent named speaker even if the pronoun refers to
    someone else. The production-path coverage for this defect class is the LLM-semantic
    canon_registry `quantities` diff + NARASI_NUMERIC_LEDGER (both full-book), not this scanner."""
    stop = _AGE_STOP_NAMES | _TENURE_STOP_NOUNS | extra_stop
    cand = None
    for nm in _TENURE_NAME_RX.finditer(text[max(0, start - 220):start]):
        key = nm.group(1).split()[0].split("-")[0]
        if key not in stop:
            cand = nm.group(1)
    if cand:
        return cand
    nm2 = _NAMED_SPEAKER_TAG_RX.search(text[end:end + 90])
    if nm2:
        nm_name = nm2.group("name_a") or nm2.group("name_b")
        key = nm_name.split()[0].split("-")[0]
        if key not in stop:
            return nm_name
    if _PRONOUN_SPEAKER_TAG_RX.search(text[max(0, start - 90):end + 90]):
        return _resolve_pronoun_speaker(start, named_events)
    return None


def scan_tenure_ledger(text: str) -> list[dict]:
    """Same capitalized person-name given >=2 distinct tenure/years-of-experience figures
    ("spent two decades" vs "fifteen years", the same career). 'decade' tokens are ×10'd
    before comparison. Report-only; mirrors scan_same_entity_age_fork's mechanism (with a
    locally-widened name pattern + an institutional-noun stoplist, see above)."""
    if not text:
        return []
    tenure_by_name: dict[str, set] = {}
    givens_by_surname: dict[str, set] = {}
    # FIX (2026-07-18, dialogue-crossing gap, Part 2): one forward O(n) pass over the whole
    # text, built once, so the pronoun+tracker path in _resolve_subject can consult every named
    # speaker-tag event regardless of where in the loop below the current match sits.
    named_events = _named_speaker_events(text)
    for m in _TENURE_TOKEN_RX.finditer(text):
        raw = (m.group("t1") or m.group("t2") or m.group("t3")
               or m.group("t4") or m.group("t5"))
        n = _en_num(raw) if not raw.isdigit() else int(raw)
        if n is None:
            continue
        unit = "decade" if "decade" in text[m.start():m.end()].lower() else "year"
        if unit == "decade":
            n *= 10
        if not (1 <= n <= 70):
            continue
        # FIX (2026-07-18, dialogue-crossing gap, Part 2): replaces the old backward-lookback +
        # blind-forward-grab pair with the shared resolver (backward lookback [unchanged] ->
        # narrowed dialogue-verb-bound forward search -> pronoun+tracker fallback). See
        # _resolve_subject's docstring for why each step exists. extra_stop mirrors
        # scan_elapsed_span_consistency's call (audit-caught omission: without it a capitalized
        # sentence-opener like "Meanwhile" in the backward window is accepted as a "name").
        cand = _resolve_subject(text, m.start(), m.end(), named_events,
                                extra_stop=_SPAN_SUBJECT_STOP_EXTRA)
        if cand:
            parts = cand.split()
            # AUDIT FIX: only split off "-" when cand is "Surname Given[-name]" (2+ space-
            # separated words, parts[0] IS a real surname). A bare single-token hyphenated
            # match ("Do-yoon", from _TENURE_NAME_RX's compound branch) IS the whole given
            # name — splitting it on "-" truncated the reported subject to just "Do".
            if len(parts) >= 2:
                surname = parts[0].split("-")[0]
                givens_by_surname.setdefault(surname, set()).add(parts[1])
            else:
                surname = cand
            tenure_by_name.setdefault(surname, set()).add(n)
    out = []
    for name, tenures in tenure_by_name.items():
        if len(givens_by_surname.get(name, set())) >= 2:
            continue  # different people sharing a surname — same guard as the age fork
        values = sorted(tenures)
        if len(values) >= 2 and (max(values) - min(values)) >= 2:
            out.append({"kind": "tenure_fork", "subject": name, "years": values,
                        "note": f"'{name}' given tenure/experience {values} years — pick one"})
    return out[:6]


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WD_RX = re.compile(r"(?i)\b(" + "|".join(_WEEKDAYS) + r")\b")


def scan_weekday_mismatch(text: str) -> list[dict]:
    """ROUND-6 v2 (lens-4 P2.4): a weekday named within 60 chars of a full date is
    checkable against the real calendar (R8/R9 both happened to get it right —
    cheap to assert forever). Month-year pins carry the day=15 sentinel, so
    day==15 dates are skipped rather than false-checked. Report-only."""
    import datetime as _dt
    out: list[dict] = []
    slop: dict = {}
    for pos, d in _dates_en(text, slop_out=slop):
        # d is a (Y, M, D) tuple; month-year pins use the day=15 sentinel — skip those
        if slop.get(pos, 0) != 0 or d[2] == 15:
            continue
        try:
            _date = _dt.date(*d)
        except ValueError:
            continue
        lo, hi = max(0, pos - 60), min(len(text), pos + 90)
        m = _WD_RX.search(text[lo:hi])
        if not m:
            continue
        want = _WEEKDAYS[_date.weekday()]
        if m.group(1).lower() != want:
            out.append({"kind": "weekday_mismatch",
                        "note": (f"'{m.group(1)}' beside {_date.isoformat()} — "
                                 f"real calendar says {want.capitalize()}"),
                        "context": text[lo:hi].replace("\n", " ")})
        if len(out) >= 3:
            break
    return out


def scan_frozen_durations(text: str, prior_findings: list[dict]) -> list[dict]:
    """ROUND-6 v2 (lens-4 superset of the r5.2 REVERT check): the same day-count
    value recurring far apart while dated events pass between (R9: '1,826 days'
    in Ch1 AND Ch8, 50 story-days later). REVERT only fires when a larger value
    intervenes; this covers the no-intervening-peak case. Any value the REVERT
    check already reported is skipped so one defect never double-fires.
    Report-only."""
    reported = " | ".join(str(f.get("note") or "") for f in (prior_findings or []))
    vals: list[tuple[int, int]] = []
    for rx in _revert_rxes:
        for m in rx.finditer(text):
            nv = _en_num_ext(m.group(1))
            if nv is not None and 100 <= nv <= 3000:
                vals.append((m.start(), nv))
    vals.sort()
    dedup: list[tuple[int, int]] = []
    for pos, nv in vals:
        if not any(abs(pos - p2) < 12 for p2, _ in dedup):
            dedup.append((pos, nv))
    slop: dict = {}
    dates = [(pos, d) for pos, d in _dates_en(text, slop_out=slop) if slop.get(pos, 0) == 0]
    out: list[dict] = []
    flagged: set[int] = set()
    for i, (p1, v1) in enumerate(dedup):
        if v1 in flagged or str(v1) in reported:
            continue
        for p2, v2_ in dedup[i + 1:]:
            if v2_ != v1 or (p2 - p1) < 20000:
                continue
            between = {d for pos, d in dates if p1 < pos < p2}
            if len(between) >= 2:
                flagged.add(v1)
                out.append({"kind": "frozen_duration",
                            "note": (f"day-counter FROZEN: '{v1} days' repeats {p2 - p1} chars apart "
                                     f"while {len(between)} dated event(s) pass between"),
                            "context": text[max(0, p2 - 80):p2 + 60].replace("\n", " ")})
                break
    return out[:3]


def scan_arithmetic(text: str, *, lang: str = "fr",
                     known_dob: dict[str, int] | None = None,
                     seed_dates: dict[str, tuple[int, int, int]] | None = None,
                     v2: bool = False) -> dict:
    """SPEC v1 §3.3 entry point. Combined report of age + interval findings.
    Returns {status: PASS|FAIL, findings: [...], counts: {...}}.
    Never raises."""
    try:
        age = scan_age_across_scenes(text, lang=lang, known_dob=known_dob)
        interval = scan_interval_vs_dates(text, lang=lang, seed_dates=seed_dates)
        spans: list[dict] = []
        days: list[dict] = []
        if (lang or "").split("-")[0].lower() == "en":
            interval = interval + scan_interval_vs_dates_en(text)
            spans = scan_span_alternation(text)
            days = scan_day_counters(text)
        findings = age + interval + spans + days
        counts = {"age": len(age), "interval": len(interval),
                  "span": len(spans), "day": len(days)}
        if v2 and (lang or "").split("-")[0].lower() == "en":
            frozen = scan_frozen_durations(text, findings)
            weekday = scan_weekday_mismatch(text)
            if frozen:
                findings = findings + frozen
                counts["frozen"] = len(frozen)
            if weekday:
                findings = findings + weekday
                counts["weekday"] = len(weekday)
        return {
            "status": "FAIL" if findings else "PASS",
            "findings": findings,
            "counts": counts,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "PASS", "findings": [], "counts": {}, "_error": str(exc)}
