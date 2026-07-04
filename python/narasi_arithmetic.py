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
        findings = age + interval
        return {
            "status": "FAIL" if findings else "PASS",
            "findings": findings,
            "counts": {"age": len(age), "interval": len(interval)},
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "PASS", "findings": [], "counts": {}, "_error": str(exc)}
