#!/usr/bin/env python3
"""Validate eval/golden_set.yaml against freeze invariants.

Usage:
    python eval/check_golden_set.py                  # defaults to eval/golden_set.yaml
    python eval/check_golden_set.py path/to/file.yaml

Exit 0 = semua lolos, 1 = ada yang gagal. Aman dipakai sebagai pre-commit gate.
"""
import sys
import collections

import yaml

# ---------------------------------------------------------------------------
# ISI DULU — samakan dengan yang kamu pakai sebelumnya.
# Ini placeholder; kalau salah, gate-nya nge-gate hal yang salah.
ALLOWED = {
    "biographical", "conversational", "discovery", "dramatic",
    "epic", "expository", "financial", "lyrical",
    "political", "reflective", "suspense", "unlabeled",
}
THICK = {
    # isi dengan style rino-tebal yang BOLEH prefer_source.
    # contoh kalau keempatnya sah:
    "discovery", "epic", "expository", "political",
}
# ---------------------------------------------------------------------------

PATH = sys.argv[1] if len(sys.argv) > 1 else "eval/golden_set.yaml"

ok_all = []


def chk(name, cond, detail=""):
    ok_all.append(bool(cond))
    mark = "OK  " if cond else "GAGAL"
    line = f"  [{mark}] {name}"
    if detail and not cond:
        line += f"  ({detail})"
    print(line)


d = yaml.safe_load(open(PATH))

print(f"Memvalidasi: {PATH}  ({len(d)} entri)\n")

# struktur dasar
ids = [e["id"] for e in d]
chk("id unik", len(ids) == len(set(ids)),
    "dup: " + ", ".join(str(k) for k, c in collections.Counter(ids).items() if c > 1))
chk("topik unik", len(d) == len({e["topic"] for e in d}))

# style & bahasa
chk("style ∈ 12 label valid", not ({e["style"] for e in d} - ALLOWED),
    ", ".join(sorted({e["style"] for e in d} - ALLOWED)))
chk("lang ∈ {id,en}", not ({e["lang"] for e in d} - {"id", "en"}))

nid = sum(e["lang"] == "id" for e in d)
nen = sum(e["lang"] == "en" for e in d)
chk("id ≥ 13", nid >= 13, f"id={nid}")
chk("en ≥ 10", nen >= 10, f"en={nen}")

# prefer_source
pref = [e for e in d if e["prefer_source"] is True]
chk("prefer_source true = 6", len(pref) == 6, str(len(pref)))
chk("prefer_source cuma di style rino-tebal", not ({e["style"] for e in pref} - THICK),
    ", ".join(sorted({e["style"] for e in pref} - THICK)))
chk("12 label semua kepakai", not (ALLOWED - {e["style"] for e in d}),
    "kurang: " + ", ".join(sorted(ALLOWED - {e["style"] for e in d})))

print()
print("  sebaran style :", dict(sorted(collections.Counter(e["style"] for e in d).items(), key=lambda x: -x[1])))
print("  prefer styles :", dict(collections.Counter(e["style"] for e in pref)))
print("\nHASIL:", "SEMUA LOLOS — aman lanjut mv + commit" if all(ok_all) else "ADA YANG GAGAL — beresin dulu")
sys.exit(0 if all(ok_all) else 1)
