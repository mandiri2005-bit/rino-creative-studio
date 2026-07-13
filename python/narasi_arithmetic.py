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

import re
from typing import Any

__all__ = ["scan_arithmetic", "AGE_ANCHORS", "INTERVAL_ANCHORS"]

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
        _revert_rxes = (
            re.compile(r"(?i)\b(\d{1,2},\d{3}|\d{3,4})\s+days\b"),
            re.compile(r"(?i)\b((?:one|two|three|four|five|six|seven|eight|nine)\s+thousand"
                       r"[a-z ,-]{0,50}?)\s+days\b"),
            re.compile(r"(?i)\b((?:one|two|three|four|five|six|seven|eight|nine)?\s*hundred"
                       r"[a-z ,-]{0,40}?)\s+days\b"),
        )
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
    for a in sorted(pos):
        b = a + 1
        if b not in pos or len(pos[a]) < 2 or len(pos[b]) < 2:
            continue
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
    return findings


def scan_arithmetic(text: str, *, lang: str = "fr",
                     known_dob: dict[str, int] | None = None,
                     seed_dates: dict[str, tuple[int, int, int]] | None = None
                     ) -> dict:
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
        return {
            "status": "FAIL" if findings else "PASS",
            "findings": findings,
            "counts": {"age": len(age), "interval": len(interval),
                       "span": len(spans), "day": len(days)},
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "PASS", "findings": [], "counts": {}, "_error": str(exc)}
