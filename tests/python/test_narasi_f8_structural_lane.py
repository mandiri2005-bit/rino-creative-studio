"""F8 — the seam violation driven through the REAL structural addressed-patch lane.

🔴 WHY THIS FILE EXISTS. The delivery-path and combined suites script the merged revise, so the
   provider never sees an F8 violation and the lane's own attribution line is never executed.
   That gap is exactly what let an EMPTY directive ship: `evidence` and `fix` were `None`, the
   prompt read `- [high/chapter_boundary_break]  -> FIX:`, and every green test agreed. Here the
   real `_narasi_structural_patch_revise` runs against a scripted client, so what the provider
   is handed and what the lane records are both observable.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from types import SimpleNamespace

import pytest


def _live(name):
    return sys.modules.get(name) or importlib.import_module(name)


lz = _live("laozhang_api")
f8 = _live("narasi_f8")

CHAPTER = (
    "## Bab 3: Ruang Rapat\n\n"
    "Ruang rapat itu penuh ketika ia masuk.\n\n"
    "Tae-jun meletakkan map cokelat di atas meja panjang.\n\n"
    "Deposisinya menutup sidang.\n"
)


def _seam_violation():
    census = f8.seam_census(
        [{"chapter_a": 1, "chapter_b": 2, "causal": "explicit",
          "location": "explicit", "time": "explicit"},
         {"chapter_a": 2, "chapter_b": 3, "causal": "missing",
          "location": "missing", "time": "missing"}],
        chapter_count=3)
    return f8.detect(census, openings={3: "Ruang rapat itu penuh ketika ia masuk."})[0]


def _plain(completions):
    from test_narasi_addressed_patch import plain_client
    return plain_client(completions)


def _run(monkeypatch, patch_json):
    captured = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=patch_json), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    async def _usage(*_a, **_k):
        return 3

    monkeypatch.setattr(lz, "make_narasi_client",
                        lambda *a, **k: _plain(_Completions()))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 2.0)

    violation = _seam_violation()
    revised, credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [violation], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "PACKET"}))
    return violation, revised, stats, captured


def _patch(*operations):
    """The lane's own payload helper — hand-rolling the envelope earns a `schema_shape`
    refusal, which is the validator working."""
    from test_narasi_addressed_patch import _payload
    return _payload(*operations)


PATCH = _patch({"op": "replace", "unit_id": "u001",
                "text": "Ia turun dari atap malam itu dan ruang rapat penuh saat ia masuk."})


def test_the_provider_prompt_carries_a_real_directive_not_an_empty_one(monkeypatch):
    """🔴 THE DEFECT THE WHOLE SUITE MISSED. `_narasi_structural_patch_revise` builds its
    directive line from `evidence` and `fix` and nothing else."""
    violation, _revised, _stats, captured = _run(monkeypatch, PATCH)
    prompt = captured["messages"][1]["content"]

    assert f"- [{violation['severity']}/{violation['type']}]  -> FIX:" not in prompt, \
        "the provider received an empty directive"
    assert "@ch3" in prompt, "the deterministic locator never reached the provider"
    for dimension in ("causal", "location", "time"):
        assert dimension in prompt, f"{dimension} is missing but the prompt never says so"
    assert "Enact it, do not summarise it." in prompt


def test_the_lane_records_the_seam_it_accepted_not_merely_the_chapter(monkeypatch):
    """🔴 THE ATTRIBUTION LINE ONLY EXECUTES HERE. Every other F8 suite scripts the private
    channel, so a lane that stopped recording which seam it accepted would look fine."""
    violation, revised, stats, _captured = _run(monkeypatch, PATCH)

    assert stats["accepted"] == 1
    assert revised != CHAPTER, "an accepted operation that changed no byte is not a repair"
    assert stats["accepted_chapter_numbers"] == {3}
    assert stats["accepted_violation_ids"] == {violation["seam"]} == {"seam:2|3"}


def test_a_rejected_patch_records_no_seam_attribution(monkeypatch):
    """A refusal must leave the attribution set empty — otherwise F8 would resolve on it."""
    _violation, revised, stats, _captured = _run(
        monkeypatch, _patch({"op": "replace", "unit_id": "u999",
                             "text": "tidak ada unit itu sama sekali di tabel"}))
    assert stats["accepted"] == 0
    assert revised == CHAPTER
    assert stats["accepted_violation_ids"] == set()


def test_untouched_units_survive_the_seam_repair_byte_for_byte(monkeypatch):
    _violation, revised, _stats, _captured = _run(monkeypatch, PATCH)
    assert "Tae-jun meletakkan map cokelat di atas meja panjang." in revised
    assert "Deposisinya menutup sidang." in revised
    assert revised.startswith("## Bab 3: Ruang Rapat\n\n")


# ---------------------------------------------------------------------------
# ADDRESS-level attribution — the round-3 finding
# ---------------------------------------------------------------------------
OTHER = {"severity": "high", "type": "outline_beat_order", "chapter": 3,
         "evidence": "Deposisinya menutup sidang.",
         "fix": "Restore the outlined order."}


def _run_two(monkeypatch, patch_json):
    """One chapter carrying BOTH an F8 seam violation and an unrelated structural finding."""
    class _Completions:
        def create(self, **kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=patch_json), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    async def _usage(*_a, **_k):
        return 3

    monkeypatch.setattr(lz, "make_narasi_client", lambda *a, **k: _plain(_Completions()))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 2.0)

    seam = _seam_violation()
    revised, _credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        CHAPTER, [seam, dict(OTHER)], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None,
        authority_text="AUTHORITY", outline_packets={"3": "PACKET"}))
    return seam, revised, stats


def test_an_accepted_operation_elsewhere_in_the_chapter_does_not_attribute_the_seam():
    """🔴 THE ROUND-3 FINDING. Recording every violation in an accepted chapter means a patch
    that repaired the OUTLINE ORDER at the chapter's end attributes the seam repair at its
    opening — a seam the provider never touched. Attribution has to follow the ADDRESS the
    accepted operation actually edited, not the chapter it happened to live in."""
    pytest.importorskip("laozhang_api")


@pytest.mark.parametrize("unit,expect_seam", [("u001", True), ("u003", False)])
def test_attribution_follows_the_edited_address_not_the_chapter(monkeypatch, unit, expect_seam):
    seam, revised, stats = _run_two(monkeypatch, _patch({
        "op": "replace", "unit_id": unit,
        "text": ("Ia turun dari atap malam itu dan ruang rapat penuh saat ia masuk."
                 if unit == "u001" else
                 "Deposisi itu akhirnya dibacakan sebelum sidang ditutup rapat.")}))
    assert stats["accepted"] == 1
    assert revised != CHAPTER
    if expect_seam:
        assert seam["seam"] in stats["accepted_violation_ids"], \
            "the opening WAS edited but the seam was not attributed"
    else:
        assert seam["seam"] not in stats["accepted_violation_ids"], \
            "an edit at the chapter's END attributed a seam repair at its opening"


def test_the_f8_counters_exclude_unrelated_structural_findings(monkeypatch):
    """🔴 THE LANE-WIDE TOTALS COVER EVERY STRUCTURAL CHAPTER. Comparing them against
    `seams_targeted` inflates F8's accounting the moment an unrelated finding rides along —
    two structural targets in one chapter would report two F8 attempts for one seam."""
    seam, _revised, stats = _run_two(monkeypatch, _patch({
        "op": "replace", "unit_id": "u001",
        "text": "Ia turun dari atap malam itu dan ruang rapat penuh saat ia masuk."}))
    assert stats["targeted"] == 1                      # one CHAPTER, lane-wide
    assert stats["f8_targeted_ids"] == {seam["seam"]}  # one SEAM, F8's own subset
    assert stats["f8_attempted_ids"] == {seam["seam"]}
    assert stats["f8_provider_calls"] == 1
    assert stats["accepted_violation_ids"] == {seam["seam"]}
    # the unrelated finding contributed nothing to any F8 number
    assert all("outline_beat_order" not in _i for _i in stats["f8_targeted_ids"])


TWO_CHAPTERS = (
    "## Bab 2: Atap\n\n"
    "Eun-soo menutup pintu atap itu pelan sekali pada malam yang dingin.\n\n"
    "Ia menatap kota yang berkedip di bawahnya tanpa berkata apa pun lagi.\n\n"
    "## Bab 3: Ruang Rapat\n\n"
    "Ruang rapat itu penuh ketika ia masuk pagi tadi sekali.\n\n"
    "Tae-jun meletakkan map cokelat di atas meja panjang itu perlahan.\n"
)


def test_an_unrelated_structural_chapter_inflates_no_f8_number(monkeypatch):
    """🔴 THE LANE-WIDE TOTALS COUNT EVERY STRUCTURAL CHAPTER. With two chapters targeted and
    only ONE carrying an F8 seam, `targeted`/`provider_calls` say two while F8's own subsets
    must say one. A single-chapter fixture cannot tell the two readings apart at all."""
    class _Completions:
        def create(self, **kwargs):
            prompt = kwargs["messages"][-1]["content"]
            opening = prompt.split("[u001]\n", 1)[1].split("\n[u", 1)[0].strip()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=_patch(
                    {"op": "replace", "unit_id": "u001",
                     "text": "Ia turun malam itu. " + opening})), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", lambda *a, **k: _plain(_Completions()))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 5.0)

    seam = _seam_violation()                      # targets chapter 3
    other = {"severity": "high", "type": "outline_beat_order", "chapter": 2,
             "evidence": "Eun-soo menutup pintu atap itu pelan sekali pada malam yang dingin.",
             "fix": "Restore the outlined order."}
    _text, _credits, stats = asyncio.run(lz._narasi_structural_patch_revise(
        TWO_CHAPTERS, [seam, other], "storytelling", "id", "test-model",
        tenant_id="t", user_id="u", job_uuid=None, authority_text="AUTHORITY",
        outline_packets={"2": "PACKET TWO", "3": "PACKET THREE"}))

    assert stats["targeted"] == 2, "the fixture must target two structural chapters"
    assert stats["provider_calls"] >= 2
    assert stats["f8_targeted_ids"] == {seam["seam"]}
    assert stats["f8_attempted_ids"] == {seam["seam"]}
    assert stats["f8_provider_calls"] == 1, "an unrelated chapter's exchange was billed to F8"
