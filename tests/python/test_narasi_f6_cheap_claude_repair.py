"""F6's two live-canary repair failures use a bounded, cheap Claude actuator."""
from __future__ import annotations

import asyncio
import types

import laozhang_api as lz


def test_cheap_primitive_honours_the_call_scoped_model_and_phase(monkeypatch):
    seen = {}

    class Completions:
        def create(self, **kwargs):
            seen["served_model"] = kwargs["model"]
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                finish_reason="stop", message=types.SimpleNamespace(content="repaired."))])

    class Client:
        chat = types.SimpleNamespace(completions=Completions())

    def client(model, *, phase="", **_kwargs):
        seen.update(requested_model=model, phase=phase)
        return Client()

    async def usage(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(lz, "make_narasi_client", client)
    monkeypatch.setattr(lz, "_log_narasi_usage", usage)

    text, _credits = asyncio.run(lz._narasi_cheap_call(
        "system", "chapter", tenant_id="t", user_id="u",
        model_override="claude-haiku-4-5", phase="f6_repair"))

    assert text == "repaired."
    assert seen == {
        "requested_model": "claude-haiku-4-5",
        "phase": "f6_repair",
        "served_model": "claude-haiku-4-5",
    }


def test_ceiling_reducer_sends_an_exact_mandatory_budget_to_cheap_claude(monkeypatch):
    seen = {}

    async def cheap(system, user, **kwargs):
        seen.update(system=system, user=user, kwargs=kwargs)
        return "Bab yang sudah dipadatkan.", 3

    monkeypatch.delenv("NARASI_F6_REPAIR_MODEL", raising=False)
    monkeypatch.setattr(lz, "_narasi_cheap_call", cheap)

    text, credits = asyncio.run(lz._narasi_chapter_reduce(
        "Satu dua tiga empat lima enam tujuh delapan sembilan sepuluh.",
        target_words=110, minimum_words=90, style="storytelling", language="id",
        tenant_id="t", user_id="u",
        required_beats="1. Tokoh menemukan kunci.\n2. Tokoh menyerahkan kunci."))

    assert text == "Bab yang sudah dipadatkan."
    assert credits == 3
    assert seen["kwargs"]["model_override"] == "claude-haiku-4-5-20251001"
    assert seen["kwargs"]["phase"] == "f6_repair"
    assert seen["kwargs"]["require_complete"] is True
    assert "mandatory reduction" in seen["system"]
    assert "Do NOT return the input unchanged" in seen["system"]
    assert "complete preservation authority" in seen["system"]
    assert "delete minor actions and texture" in seen["system"]
    assert "between 90 and 110 words" in seen["user"]
    assert "approximately 100 words" in seen["user"]
    assert "REQUIRED BEATS — PRESERVATION AUTHORITY" in seen["user"]
    assert "Tokoh menemukan kunci" in seen["user"]


def test_two_server_measured_teleports_force_a_cheap_chapter_repair(monkeypatch):
    original = (
        "## Chapter 2: Transit\n\n"
        "Mira menutup pintu rumah setelah tengah malam. Sesaat kemudian ia berdiri di "
        "peron stasiun yang penuh cahaya. Ia membuka surat itu. Sesaat kemudian ia berada "
        "di kantor polisi dan menyerahkan surat kepada petugas yang menunggu."
    )
    repaired = (
        "## Chapter 2: Transit\n\n"
        "Mira menutup pintu rumah setelah tengah malam. Ia berjalan ke halte, naik bus "
        "malam, dan turun di depan stasiun yang penuh cahaya. Di peron, ia membuka surat "
        "itu. Setelah keluar dari stasiun, ia naik taksi menuju kantor polisi lalu "
        "menyerahkan surat kepada petugas yang menunggu."
    )
    seen = {"calls": 0}

    class Completions:
        def create(self, **kwargs):
            seen["calls"] += 1
            seen["model"] = kwargs["model"]
            seen["system"] = kwargs["messages"][0]["content"]
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                finish_reason="stop",
                message=types.SimpleNamespace(content=repaired))])

    class Client:
        chat = types.SimpleNamespace(completions=Completions())

    def client(model, *, phase="", **_kwargs):
        seen["requested_model"] = model
        seen["phase"] = phase
        return Client()

    async def usage(*_args, **_kwargs):
        return 0

    monkeypatch.delenv("NARASI_F6_REPAIR_MODEL", raising=False)
    monkeypatch.setattr(lz, "make_narasi_client", client)
    monkeypatch.setattr(lz, "_log_narasi_usage", usage)
    monkeypatch.setenv("NARASI_REVISE_PARALLEL", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED", "0")
    monkeypatch.setenv("NARASI_REVISE_CHUNKED_AUTO_WORDS", "0")

    violations = [{
        "f6_class": "teleport", "f6_claim": f"teleport_instance:2|{ordinal}",
        "type": "spatial", "severity": "high", "chapter": 2,
        "evidence": ("chapter 2 contains 2 location changes with no transition on the "
                     f"page (occurrence {ordinal} of 2) @ch2"),
        "fix": "Account for every location change with travel, a scene break, or time."
    } for ordinal in (1, 2)]

    output, _credits = asyncio.run(lz._narasi_consistency_revise(
        original, {"violations": violations}, "storytelling", "id",
        model="claude-opus-4-6", tenant_id="t", user_id="u", job_uuid="j"))

    assert output == repaired
    assert seen["calls"] == 1
    assert seen["requested_model"] == "claude-haiku-4-5-20251001"
    assert seen["phase"] == "f6_repair"
    assert "already verified 2 unbridged location changes" in seen["system"]
    assert "Repair EVERY measured occurrence" in seen["system"]


def test_non_f6_revise_never_receives_the_mandatory_teleport_suffix():
    assert lz._f6_teleport_repair_suffix([
        {"type": "spatial", "severity": "high"}
    ]) == ""


def test_the_beat_actuator_defaults_to_the_full_dated_claude_id(monkeypatch):
    """🔴 THE BARE ALIAS IS A 503. `claude-haiku-4-5` has no channel on the production
    provider, so an actuator pointed at it would fail every call and leave every missing beat
    unresolved — a fail-closed gate refusing books because its actuator was unreachable."""
    monkeypatch.delenv("NARASI_F6_BEAT_REPAIR_MODEL", raising=False)
    assert lz._narasi_f6_beat_repair_model() == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("NARASI_F6_BEAT_REPAIR_MODEL", "   ")
    assert lz._narasi_f6_beat_repair_model() == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("NARASI_F6_BEAT_REPAIR_MODEL", "claude-haiku-4-5-20251001-test")
    assert lz._narasi_f6_beat_repair_model() == "claude-haiku-4-5-20251001-test"


def test_the_beat_actuator_is_told_the_server_already_proved_the_beat_missing(monkeypatch):
    """The canary's lane audited instead of writing. This prompt forbids that in the terms the
    canary produced: no no-op, no summary, no diff, no heading."""
    seen = {}

    async def cheap(system, user, **kwargs):
        seen.update(system=system, user=user, kwargs=kwargs)
        return "Bab yang sudah memuat beat itu.", 4

    monkeypatch.delenv("NARASI_F6_BEAT_REPAIR_MODEL", raising=False)
    monkeypatch.setattr(lz, "_narasi_cheap_call", cheap)

    text, credits = asyncio.run(lz._narasi_beat_execution_repair(
        "Min-jae menunggu di lobi sepanjang sore itu.",
        missing_beat="Tae-jun executes the deposition before a notary",
        beat_number=2, chapter_number=2,
        required_beats="  1. Min-jae waits\n  2. Tae-jun executes the deposition\n",
        style="storytelling", language="id", tenant_id="t", user_id="u",
        previous_tail="Eun-soo menutup pintu kantor itu.", word_ceiling=110))

    assert (text, credits) == ("Bab yang sudah memuat beat itu.", 4)
    assert seen["kwargs"]["model_override"] == "claude-haiku-4-5-20251001"
    assert seen["kwargs"]["phase"] == "f6_beat_repair"
    assert seen["kwargs"]["require_complete"] is True
    assert seen["kwargs"]["json_mode"] is False
    # the server has already proven it — the model writes, it does not re-audit
    assert "server has already verified" in seen["system"]
    assert "mandatory rewrite" in seen["system"]
    assert "do not return it unchanged" in seen["system"]
    assert "ENACT the missing beat" in seen["system"]
    # the four shapes the canary produced, forbidden by name
    assert "COMPLETE chapter body" in seen["system"]
    for banned in ("a diff", "a summary", "a chapter heading", "only the new scene"):
        assert banned in seen["system"], banned
    # POV, tense and the chapter's own ending must survive
    assert "point of view" in seen["system"] and "narration tense" in seen["system"]
    assert "existing ending" in seen["system"]
    # what it is handed
    assert "Tae-jun executes the deposition before a notary" in seen["user"]
    assert "beat 2 of chapter 2" in seen["user"]
    assert "PRESERVATION AUTHORITY" in seen["user"]
    assert "must not exceed 110 words" in seen["user"]
    assert "READ-ONLY CONTEXT" in seen["user"]


def test_the_beat_actuator_carries_the_correction_into_its_one_retry(monkeypatch):
    seen = {}

    async def cheap(_system, user, **_kwargs):
        seen["user"] = user
        return "ok.", 0

    monkeypatch.setattr(lz, "_narasi_cheap_call", cheap)
    asyncio.run(lz._narasi_beat_execution_repair(
        "Min-jae menunggu.", missing_beat="Tae-jun signs", beat_number=2, chapter_number=2,
        required_beats="", style="s", language="id", tenant_id="t", user_id="u",
        correction="Rejected because it returned the chapter unchanged."))
    assert "CORRECTION TO YOUR PREVIOUS ATTEMPT" in seen["user"]
    assert "returned the chapter unchanged" in seen["user"]
