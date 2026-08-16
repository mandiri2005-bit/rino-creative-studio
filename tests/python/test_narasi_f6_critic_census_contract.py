"""F6 — the tense-census contract, witnessed through the REAL critic call path.

🔴 WHY THIS FILE EXISTS. The first version of the F6 work changed `laozhang_api.py` — the census
   instruction in the critic prompt and the carry-through of `tense_by_chapter` — and NOTHING
   tested it. Reverting `laozhang_api.py` byte-for-byte to `9d1e8a1` left all 41 F6 tests green
   and `prove_f6` at 19/19, because the harness only mutated `narasi_gate.py` and `narasi_f6.py`.
   A control with no witness is not a control; it is a hope with a comment attached.

   So these tests drive `_narasi_consistency_critique` itself, with the provider seam replaced,
   and assert on what the critic was actually SENT and what actually came BACK. `make_narasi_client`
   is the only seam stubbed; a real network client is never built.

🔴 AND THE PAYLOAD BOUND IS WITNESSED HERE TOO. 201 entries of 100k characters each travelled
   straight through the first version and into `result["critique"]`, which is persisted. The
   bound belongs to this call path, so its proof does too.
"""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest


def _live_lz():
    """The `laozhang_api` currently registered for imports — several legacy endpoint tests
    reload it during a full-tree run, which leaves a collection-time reference pointing at a
    dead module object."""
    import laozhang_api
    return sys.modules.get("laozhang_api", laozhang_api)


BOOK = "\n\n".join(f"## Chapter {i}: T{i}\n\nprose {i}" for i in (1, 2, 3))


@pytest.fixture
def critic(monkeypatch):
    """Run the real critic; record the system prompt it sent and control what it gets back."""
    lz = _live_lz()
    seen: dict = {"system": None, "user": None, "clients": 0}

    def make_client(reply_json: str):
        def _create(**kw):
            seen["system"] = kw["messages"][0]["content"]
            seen["user"] = kw["messages"][1]["content"]
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=reply_json), finish_reason="stop")],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=_create)))
        client.with_options = lambda **_k: client
        return client

    async def _usage(*_a, **_k):
        return 0

    def run(reply):
        payload = reply if isinstance(reply, str) else json.dumps(reply)

        def _factory(*_a, **_k):
            seen["clients"] += 1
            return make_client(payload)

        monkeypatch.setattr(lz, "make_narasi_client", _factory)
        monkeypatch.setattr(lz, "_log_narasi_usage", _usage)
        verdict, _cr = asyncio.run(lz._narasi_consistency_critique(
            BOOK, "storytelling", "id", model="test-model",
            tenant_id="t", user_id="u", job_uuid=None, credit_row=False,
            authority_text="AUTHORITY"))
        assert seen["clients"] >= 1, "the critic never called a provider — nothing was exercised"
        assert seen["system"], "no system prompt was captured; this test proves nothing"
        seen["verdict"] = verdict
        return seen

    return run


# ---------------------------------------------------------------------------
# What the critic is ASKED
# ---------------------------------------------------------------------------
def test_the_critic_is_asked_for_a_per_chapter_tense_census(critic):
    """🔴 THE WITNESS THAT WAS MISSING. Revert the prompt and this test — not a comment — fails."""
    seen = critic({"score": 9, "violations": []})
    system = seen["system"]
    assert "tense_by_chapter" in system
    assert "TENSE CENSUS" in system


def test_the_census_request_names_the_closed_vocabulary_and_the_ordering(critic):
    """The server reads entry N as chapter N and rejects any label outside three values; if the
    prompt stops saying so, every census becomes `invalid_value` or points at the wrong chapter."""
    system = critic({"score": 9, "violations": []})["system"]
    for token in ('"past"', '"present"', '"mixed"'):
        assert token in system, token
    assert "one per chapter" in system.lower() or "in chapter order" in system.lower()


def test_the_census_is_asked_for_as_an_observation_not_a_judgement(critic):
    """The critic reports what each chapter IS; the SERVER decides which one is the outlier.
    A prompt that asks the model to pick the wrong chapter hands the judgement back."""
    system = critic({"score": 9, "violations": []})["system"]
    assert "do not decide" in system.lower()


def test_the_json_contract_advertises_the_field(critic):
    system = critic({"score": 9, "violations": []})["system"]
    assert '"tense_by_chapter"' in system


# ---------------------------------------------------------------------------
# What the critic RETURNS
# ---------------------------------------------------------------------------
def test_a_census_the_model_returns_survives_to_the_verdict(critic):
    """The carry-through, end to end: prompt → provider → parse → normalise → verdict."""
    seen = critic({"score": 7, "violations": [],
                   "tense_by_chapter": ["past", "present", "past"]})
    assert seen["verdict"]["tense_by_chapter"] == ["past", "present", "past"]


def test_the_returned_census_drives_the_server_arithmetic(critic):
    """🔴 THE POINT OF THE WHOLE CONTRACT. What comes back must be usable by the census as-is —
    an end-to-end assertion, not a shape check."""
    import narasi_gate as ng
    seen = critic({"score": 7, "violations": [],
                   "tense_by_chapter": ["past", "present", "past"]})
    census = ng.tense_census(seen["verdict"]["tense_by_chapter"], chapter_count=3)
    assert census["valid"] is True
    assert census["majority"] == "past"
    assert census["outliers"] == [2]


def test_a_critic_that_omits_the_census_leaves_the_field_absent(critic):
    """Absent must stay absent: a critic that could not read every chapter must not be
    indistinguishable from one reporting an empty book."""
    seen = critic({"score": 9, "violations": []})
    assert "tense_by_chapter" not in seen["verdict"]


# ---------------------------------------------------------------------------
# The payload bound, on the path that persists it
# ---------------------------------------------------------------------------
def test_a_runaway_census_is_bounded_before_it_reaches_the_verdict(critic):
    """🔴 AUDIT REPRO. 201 entries x 100k characters — ~20.1M — rode through untouched into a
    verdict that `result["critique"]` persists."""
    seen = critic({"score": 9, "violations": [],
                   "tense_by_chapter": ["x" * 100_000] * 201})
    carried = seen["verdict"]["tense_by_chapter"]
    assert len(carried) == 200
    assert sum(len(v) for v in carried) <= 200 * 32


#: A tuple is deliberately absent: it cannot reach the bound as a tuple, because the reply
#: crosses JSON and arrives as a list. Only shapes that SURVIVE that round trip are cases.
@pytest.mark.parametrize(
    "scalar", ["x" * 20_000_000, 12345, {"a": 1}, True],
    ids=["huge_string", "number", "object", "bool"])
def test_a_non_list_census_is_replaced_not_passed_through(critic, scalar):
    """🔴 AUDIT REPRO. The bound only handled lists, so `tense_by_chapter: "x" * 20_000_000`
    travelled through whole — 20MB into a persisted payload, from the very field the bound
    exists to contain. Anything that is not a list is a malformed answer, and the safe
    representation of a malformed answer is an empty, bounded one."""
    seen = critic({"score": 9, "violations": [], "tense_by_chapter": scalar})
    carried = seen["verdict"]["tense_by_chapter"]
    assert carried == []

    import narasi_gate as ng
    assert ng.tense_census(carried)["valid"] is False


def test_bounding_preserves_list_length_so_entry_n_is_still_chapter_n(critic):
    """Unusable entries are REPLACED, not dropped: silently shortening the list would turn a
    malformed answer into a plausible one about a different book."""
    seen = critic({"score": 9, "violations": [],
                   "tense_by_chapter": ["past", {"nested": "junk"}, "past"]})
    carried = seen["verdict"]["tense_by_chapter"]
    assert len(carried) == 3
    assert carried[1] == ""

    import narasi_gate as ng
    assert ng.tense_census(carried, chapter_count=3)["reason"] == "invalid_value"
