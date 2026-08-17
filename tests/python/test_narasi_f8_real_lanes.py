"""F8 — the REAL merged revise, with the provider sequences the brief demands.

🔴 WHAT THIS CLOSES. Every other combined test scripts `_narasi_consistency_revise`, so the
   structural and legacy lanes inside it never run and their SEQUENCES are never exercised. Here
   the real merged revise executes against one deterministic client double that serves both
   lanes in order:

     structural  invalid operation  →  valid addressed patch
     legacy      byte-identical no-op →  valid targeted repair

   Both sequences are the ones the acceptance brief names, and both are judged on what landed in
   the bytes rather than on a counter.

🔴 WHAT IT STILL DOES NOT CLAIM. This drives the merged revise, not the whole job: the L3 lane
   and the full nine-defect closed loop are separate, and are stated as an open gap in the
   handoff rather than implied here.
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
from narasi_addressed_patch import PATCH_SCHEMA_VERSION  # noqa: E402

BOOK = (
    "## Bab 1: Atap\n\n"
    "Eun-soo menutup pintu atap itu pelan sekali malam ini.\n\n"
    "Ia menatap kota yang berkedip di bawahnya tanpa berkata apa pun.\n\n"
    "## Bab 2: Ruang Rapat\n\n"
    "Ruang rapat itu penuh ketika ia masuk pagi tadi.\n\n"
    "Tae-jun meletakkan map cokelat di atas meja panjang itu.\n"
)

#: Short on purpose: the addressed-patch lane enforces a word band per unit, and a bridge
#: that grows a 23-word opening to 39 is refused as `word_band` — correctly.
BRIDGE = "Ia turun malam itu, tiga minggu berlalu. "


def _seam_violation():
    census = f8.seam_census(
        [{"chapter_a": 1, "chapter_b": 2, "causal": "missing",
          "location": "missing", "time": "missing"}], chapter_count=2)
    return f8.detect(census, openings={2: "Ruang rapat itu penuh ketika ia masuk pagi tadi."})[0]


LEGACY = {"severity": "high", "type": "timeline", "chapter": 1,
          "evidence": '"Ia menatap kota yang berkedip di bawahnya tanpa berkata apa pun."',
          "fix": "Align the clock with the previous scene."}


class _Sequenced:
    """One double, two lanes, scripted sequences — and it records which lane it served.

    The structural lane asks for strict JSON (`response_format`); the legacy lane asks for a
    whole chapter behind a marker. That difference is what the double routes on, so neither
    lane is simulated: both are the real code path asking in their own shape."""

    def __init__(self):
        self.structural, self.legacy = 0, 0

    def create(self, **kwargs):
        prompt = kwargs["messages"][-1]["content"]
        if kwargs.get("response_format", {}).get("type") == "json_object":
            self.structural += 1
            if self.structural == 1:
                # 🔴 SEQUENCE STEP 1 — an INVALID operation. The lane must refuse it and retry.
                return self._reply(json.dumps({
                    "schema_version": PATCH_SCHEMA_VERSION,
                    "operations": [{"op": "obliterate", "unit_id": "u001", "text": "x"}]}))
            opening = prompt.split("[u001]\n", 1)[1].split("\n[u", 1)[0].strip()
            return self._reply(json.dumps({
                "schema_version": PATCH_SCHEMA_VERSION,
                "operations": [{"op": "replace", "unit_id": "u001",
                                "text": BRIDGE + opening}]}))
        self.legacy += 1
        marker = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"
        chapter = prompt.split(marker, 1)[1].split("\n\n[CORRECTION]\n", 1)[0]
        if self.legacy == 1:
            # 🔴 SEQUENCE STEP 1 — a byte-identical no-op. F2 requires this be refused as
            #    `ineffective` and retried, never counted as `revised`.
            return self._reply(chapter)
        return self._reply(chapter.replace("tanpa berkata apa pun",
                                           "sebelum jam sepuluh malam"))

    @staticmethod
    def _reply(content):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


@pytest.fixture
def merged(monkeypatch):
    def run():
        double = _Sequenced()

        async def _usage(*_a, **_k):
            return 0

        monkeypatch.setattr(lz, "make_narasi_client", lambda *a, **k: SimpleNamespace(
            chat=SimpleNamespace(completions=double),
            with_options=lambda **_k: SimpleNamespace(
                chat=SimpleNamespace(completions=double))))
        monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
        monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 5.0)

        seam = _seam_violation()
        request = {"violations": [seam, dict(LEGACY)]}
        text, _credits = asyncio.run(lz._narasi_consistency_revise(
            BOOK, request, "storytelling", "id", model="test-model",
            tenant_id="t", user_id="u", job_uuid=None, credit_row=False,
            authority_text="AUTHORITY",
            outline_packets={"2": "CURRENT ORDERED OUTLINE BEATS:\n  1. Bab 2: Ruang Rapat\n"}))
        return seam, text, request, double

    return run


def test_the_structural_sequence_refuses_the_invalid_operation_then_lands_the_valid_one(merged):
    seam, text, request, double = merged()
    assert double.structural >= 2, "the invalid operation was never refused and retried"
    summary = request.get("structural_patch") or {}
    assert summary.get("chapters_accepted") == 1
    assert BRIDGE.strip() in text, "the valid addressed patch never landed in the bytes"
    assert request.get("_f8_structural_accepted_ids") == [seam["seam"]]


def test_the_legacy_sequence_refuses_the_no_op_then_lands_the_repair(merged):
    _seam, text, _request, double = merged()
    assert double.legacy >= 2, "the byte-identical no-op was accepted instead of retried"
    assert "sebelum jam sepuluh malam" in text, "the legacy repair never landed"
    assert "tanpa berkata apa pun" not in text


def test_both_lanes_land_in_one_manuscript_without_touching_each_others_chapter(merged):
    _seam, text, _request, _double = merged()
    assert text != BOOK
    assert "Eun-soo menutup pintu atap itu pelan sekali malam ini." in text
    assert "Tae-jun meletakkan map cokelat di atas meja panjang itu." in text
    assert text.count("## Bab ") == 2


def test_f8_counters_reflect_the_real_lane_not_the_request(merged):
    seam, _text, request, _double = merged()
    counters = request.get("_f8_structural_counters") or {}
    assert counters.get("targeted") == 1
    assert counters.get("attempted") == 1
    assert counters.get("accepted") == 1
    assert counters.get("provider_calls") >= 2, "the refused-then-retried exchange was not counted"
    assert request["_f8_structural_accepted_ids"] == [seam["seam"]]


TWO_STRUCTURAL = (
    "## Bab 1: Atap\n\n"
    "Eun-soo menutup pintu atap itu pelan sekali pada malam yang dingin.\n\n"
    "Ia menatap kota yang berkedip di bawahnya tanpa berkata apa pun lagi.\n\n"
    "## Bab 2: Ruang Rapat\n\n"
    "Ruang rapat itu penuh ketika ia masuk pagi tadi sekali.\n\n"
    "Tae-jun meletakkan map cokelat di atas meja panjang itu perlahan.\n"
)


class _Always:
    """Answers every structural ask with a valid, in-band patch against the opening unit."""

    def __init__(self):
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        prompt = kwargs["messages"][-1]["content"]
        opening = prompt.split("[u001]\n", 1)[1].split("\n[u", 1)[0].strip()
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "schema_version": PATCH_SCHEMA_VERSION,
                "operations": [{"op": "replace", "unit_id": "u001",
                                "text": "Ia turun malam itu. " + opening}]})),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def test_published_f8_counters_exclude_an_unrelated_structural_chapter(monkeypatch):
    """🔴 THE COUNTERS THAT REACH F8 ARE THE PUBLISHED ONES, NOT THE LANE'S `stats`.

    A witness that reads `stats` cannot see a mutation of what gets PUBLISHED onto the request
    — which is the only thing `_f8_finalize` ever sees. Two structural chapters, one seam: the
    lane-wide totals say two, F8's own subsets must say one."""
    double = _Always()

    async def _usage(*_a, **_k):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", lambda *a, **k: SimpleNamespace(
        chat=SimpleNamespace(completions=double),
        with_options=lambda **_k: SimpleNamespace(chat=SimpleNamespace(completions=double))))
    monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
    monkeypatch.setattr(lz, "_narasi_revise_timeout", lambda *a: 5.0)

    census = f8.seam_census(
        [{"chapter_a": 1, "chapter_b": 2, "causal": "missing",
          "location": "missing", "time": "missing"}], chapter_count=2)
    seam = f8.detect(census, openings={2: "Ruang rapat itu penuh."})[0]
    other = {"severity": "high", "type": "outline_beat_order", "chapter": 1,
             "evidence": "Eun-soo menutup pintu atap itu pelan sekali pada malam yang dingin.",
             "fix": "Restore the outlined order."}
    request = {"violations": [seam, other]}
    asyncio.run(lz._narasi_consistency_revise(
        TWO_STRUCTURAL, request, "storytelling", "id", model="test-model",
        tenant_id="t", user_id="u", job_uuid=None, credit_row=False,
        authority_text="AUTHORITY",
        outline_packets={"1": "PACKET ONE", "2": "PACKET TWO"}))

    summary = request.get("structural_patch") or {}
    counters = request.get("_f8_structural_counters") or {}
    assert summary.get("chapters_targeted") == 2, "the fixture must target two chapters"
    assert summary.get("provider_calls") >= 2
    assert counters.get("targeted") == 1, "an unrelated structural chapter inflated F8's targets"
    assert counters.get("attempted") == 1
    assert counters.get("provider_calls") == 1, "another chapter's exchange was billed to F8"
    assert request.get("_f8_structural_accepted_ids") == [seam["seam"]]
