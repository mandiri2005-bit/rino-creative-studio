"""Regression coverage for the synchronous Storyboard outline JSON path."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import laozhang_api


class _FakeResp:
    def __init__(self, content):
        choice = SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason="stop")
        self.choices = [choice]
        self.usage = None


class _RecordingClient:
    def __init__(self, content):
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            return _FakeResp(content)

        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=create))


def test_narasi_complete_forwards_phase_and_json_format(monkeypatch):
    client = _RecordingClient('{"chapters": [{"id": "1"}]}')
    factory_calls = []

    def fake_factory(model, role="", phase=""):
        factory_calls.append((model, role, phase))
        return client

    monkeypatch.setattr(laozhang_api, "make_narasi_client", fake_factory)

    response_format = {"type": "json_object"}
    response, used_model = laozhang_api._narasi_complete(
        "gemini-2.5-flash", [{"role": "user", "content": "outline"}], 4000,
        phase="outline", response_format=response_format)

    assert response.choices[0].message.content
    assert used_model == "gemini-2.5-flash"
    assert factory_calls == [("gemini-2.5-flash", "", "outline")]
    assert client.calls[0]["response_format"] == response_format


def test_vertex_json_mode_disables_small_budget_25_thinking(monkeypatch):
    captured = {}

    class _Models:
        @staticmethod
        def generate_content(*, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            return SimpleNamespace(
                text='{"chapters": [{"id": "1"}]}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10, candidates_token_count=12))

    monkeypatch.setenv("NARASI_GENAI_JSON_MIME", "1")
    monkeypatch.setattr(
        laozhang_api, "_genai_client",
        lambda _location: SimpleNamespace(models=_Models()))

    response = laozhang_api._vertex_gemini_create(
        "gemini-2.5-flash", [{"role": "user", "content": "outline"}],
        4000, timeout=2, response_json=True)

    assert response.choices[0].message.content
    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].thinking_config.thinking_budget == 0


def _capture_vertex_config(monkeypatch, *, model, max_tokens):
    """Drive _vertex_gemini_create in JSON mode and hand back the config it built."""
    captured = {}

    class _Models:
        @staticmethod
        def generate_content(*, model, contents, config):
            captured.update(model=model, contents=contents, config=config)
            return SimpleNamespace(
                text='{"chapters": [{"id": "1"}]}',
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10, candidates_token_count=12))

    monkeypatch.setenv("NARASI_GENAI_JSON_MIME", "1")
    monkeypatch.setattr(
        laozhang_api, "_genai_client",
        lambda _location: SimpleNamespace(models=_Models()))
    laozhang_api._vertex_gemini_create(
        model, [{"role": "user", "content": "outline"}],
        max_tokens, timeout=2, response_json=True)
    return captured["config"]


def test_vertex_json_mode_disables_thinking_for_a_20_chapter_outline(monkeypatch):
    """The widest outline the admission gate allows must still get thinking OFF.

    DALANG_MAX_CHAPTERS is 20 and the outline budget is max(4000, n*600 + 2000), so a
    20-chapter outline asks for 14000 output tokens. Under the old hard `<= 4000` bound
    thinking stayed ON at that size, thought tokens consumed the budget, and the reply
    came back truncated (HTTP 500 "Tidak bisa parse outline") or empty (tout=1). This is
    the case the original small-budget test could not see.
    """
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-2.5-flash", max_tokens=20 * 600 + 2000)

    assert cfg.response_mime_type == "application/json"
    assert cfg.thinking_config.thinking_budget == 0


def test_vertex_json_mode_ceiling_is_configurable_and_still_bounds(monkeypatch):
    """The ceiling still exists — it is not silently unbounded — and honours its env."""
    monkeypatch.setenv("NARASI_GENAI_NOTHINK_MAX_TOKENS", "4000")
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-2.5-flash", max_tokens=14000)

    assert cfg.response_mime_type == "application/json"
    assert getattr(cfg, "thinking_config", None) is None


def _thinking_level_of(cfg):
    """MINIMAL as a plain string, whether the SDK gave an enum member or a raw value."""
    level = getattr(cfg.thinking_config, "thinking_level", None)
    return getattr(level, "value", level)


def test_vertex_json_mode_never_sets_thinking_budget_on_gemini_3x(monkeypatch):
    """gemini-3.x REJECTS thinking_budget=0 — that knob must never be sent for 3.x."""
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-3.5-flash", max_tokens=14000)

    assert cfg.response_mime_type == "application/json"
    assert getattr(cfg.thinking_config, "thinking_budget", None) is None


def test_vertex_json_mode_sets_minimal_thinking_level_on_gemini_3x(monkeypatch):
    """3.x has no zero, so a JSON call gets the FLOOR instead of Google's default.

    Until 2026-08-11 only the 2.5 branch existed, so gemini-3.x JSON calls ran at the
    default thinking level — including the production outline (gemini-3.6-flash), whose
    3-chapter budget is 4000 output tokens. Thought tokens bill against that, which
    reproduces on 3.x exactly the truncated/empty JSON this branch fixed for 2.5.
    """
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-3.6-flash", max_tokens=4000)

    assert cfg.response_mime_type == "application/json"
    assert _thinking_level_of(cfg) == "MINIMAL"


def test_vertex_json_mode_3x_thinking_level_is_overridable(monkeypatch):
    """Escape hatch: an operator can hand thinking back to Google's default."""
    monkeypatch.setenv("NARASI_GENAI_3X_THINKING_LEVEL", "off")
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-3.6-flash", max_tokens=4000)

    assert cfg.response_mime_type == "application/json"
    assert getattr(cfg, "thinking_config", None) is None


def test_vertex_json_mode_3x_honours_the_shared_ceiling(monkeypatch):
    """One env var bounds BOTH families — 3.x is not exempt from the ceiling."""
    monkeypatch.setenv("NARASI_GENAI_NOTHINK_MAX_TOKENS", "4000")
    cfg = _capture_vertex_config(
        monkeypatch, model="gemini-3.6-flash", max_tokens=14000)

    assert cfg.response_mime_type == "application/json"
    assert getattr(cfg, "thinking_config", None) is None


def test_outline_requests_structured_json_on_outline_phase(monkeypatch):
    captured = {}

    def fake_complete(model, messages, max_tokens, role="", phase="", response_format=None):
        captured.update(
            model=model, messages=messages, max_tokens=max_tokens,
            role=role, phase=phase, response_format=response_format)
        return _FakeResp(
            '{"chapters": [{"id": "1", "title": "Satu", '
            '"description": "Pembuka.", "words": 1800}]}'), model

    async def fake_log_usage(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(laozhang_api, "_narasi_complete", fake_complete)
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
    monkeypatch.delenv("DALANG_BEATMAP_ENABLED", raising=False)

    result = asyncio.run(laozhang_api._narasi_outline_impl({
        "action": "outline",
        "topic": "Kontrak Cinta 30 Hari di Rooftop Seoul",
        "style": "romance",
        "language": "id",
        "word_min": 1800,
        "word_max": 2100,
        "chap_count": 3,
    }))

    assert result["ok"] is True
    assert captured["phase"] == "outline"
    assert captured["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Outline description band — the prompt must ask for a compact beat list, and the
# story word budget must never read as a budget for the outline's own prose.
#
# Root cause this locks down (prod 2026-08-11): the clause said "3-6 sentences"
# in the same breath as "FULL substance / nothing may be left out / must not
# merely tease", so the model obeyed completeness and produced 15/22/14 sentences
# and 1802 words of description against a requested word_min of 1800. At 20
# chapters that needs ~53k output tokens against a 14k budget and a 16,384 model
# ceiling — unfixable by token tuning, so it 500s at the JSON parse.
# ---------------------------------------------------------------------------

def _capture_outline_prompt(monkeypatch, **body_overrides):
    """Run _narasi_outline_impl with a stubbed LLM and return the prompt it built."""
    captured = {}
    n = int(body_overrides.get("chap_count", 3))
    chapters = ", ".join(
        '{"id": "%d", "title": "Bab %d", "description": "Beat.", "words": 100}' % (i, i)
        for i in range(1, n + 1))

    def fake_complete(model, messages, max_tokens, role="", phase="", response_format=None):
        captured.update(messages=messages, max_tokens=max_tokens)
        return _FakeResp('{"chapters": [%s]}' % chapters), model

    async def fake_log_usage(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(laozhang_api, "_narasi_complete", fake_complete)
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
    monkeypatch.delenv("DALANG_BEATMAP_ENABLED", raising=False)

    body = {"action": "outline", "topic": "Kontrak Cinta 30 Hari di Rooftop Seoul",
            "style": "romance", "language": "id",
            "word_min": 1800, "word_max": 2100, "chap_count": 3}
    body.update(body_overrides)
    result = asyncio.run(laozhang_api._narasi_outline_impl(body))
    return captured["messages"][0]["content"], result


BANNED = ("3-6 sentences", "must not merely tease", "FULL substance",
          "nothing important may be left out", "nothing plot-relevant may be left out")


def _assert_band_contract(prompt):
    assert "80-200 words" in prompt
    assert "HARD MAXIMUM" in prompt
    assert "COMPACT BEAT LIST" in prompt
    # the budget is the FINISHED MANUSCRIPT's, carried only by chapters[].words
    assert "FINISHED MANUSCRIPT" in prompt
    assert 'chapters[].words' in prompt
    for phrase in BANNED:
        assert phrase not in prompt, f"old unbounded clause survived: {phrase!r}"


def test_fresh_outline_3_chapters_asks_for_the_band(monkeypatch):
    prompt, result = _capture_outline_prompt(monkeypatch, chap_count=3)
    _assert_band_contract(prompt)
    assert result["ok"] is True


def _widest_admissible_outline_body():
    """The largest outline the REAL admission gate accepts, derived from the live ceilings.

    Hardcoding a word budget here was wrong twice over: the earlier version used
    word_max=42000, which is not a production-legal number under any particular ceiling —
    it was simply invented. Deriving it from DALANG_MAX_CHAPTERS / DALANG_MAX_TOTAL_WORDS
    keeps the test honest whichever values are deployed (the code defaults are 20 and
    120000, but Railway may set either lower).
    """
    chap_count = laozhang_api.DALANG_MAX_CHAPTERS
    word_max = min(
        laozhang_api.DALANG_MAX_TOTAL_WORDS,
        chap_count * laozhang_api.DALANG_MAX_WORDS_PER_CHAPTER)
    return chap_count, max(1, word_max - 2000), word_max


def test_fresh_outline_widest_admissible_case_asks_for_the_band(monkeypatch):
    """The widest outline admission actually allows — the case that used to die.

    Routed through the REAL _narasi_admit first: a prompt-shape assertion on a request
    production would have rejected proves nothing about production.
    """
    chap_count, word_min, word_max = _widest_admissible_outline_body()

    monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")
    # Must not raise — this is what makes the case below production-legal.
    laozhang_api._narasi_admit(
        {"chap_count": chap_count, "word_max": word_max}, kind="outline")

    prompt, result = _capture_outline_prompt(
        monkeypatch, chap_count=chap_count, word_min=word_min, word_max=word_max)
    _assert_band_contract(prompt)
    # The manuscript budget must NOT have become the outline's own output target. The
    # prompt states this with a fixed illustrative figure, independent of word_max.
    assert "does not get longer descriptions" in prompt
    assert result["ok"] is True


def test_admission_rejects_an_over_ceiling_outline(monkeypatch):
    """Guards the derivation above: the gate really does bite past the ceiling."""
    monkeypatch.setenv("DALANG_ADMISSION_ENABLED", "1")

    with pytest.raises(HTTPException):
        laozhang_api._narasi_admit(
            {"chap_count": laozhang_api.DALANG_MAX_CHAPTERS + 1, "word_max": 2100},
            kind="outline")

    with pytest.raises(HTTPException):
        laozhang_api._narasi_admit(
            {"chap_count": 3,
             "word_max": laozhang_api.DALANG_MAX_TOTAL_WORDS + 1},
            kind="outline")


def test_revise_outline_3_and_20_chapters_ask_for_the_band(monkeypatch):
    for n in (3, 20):
        prompt, result = _capture_outline_prompt(
            monkeypatch, action="revise", chap_count=n,
            current_outline="Bab 1: sesuatu", revise_instruction="perkuat konflik")
        _assert_band_contract(prompt)
        assert result["ok"] is True


def test_prompt_never_requests_an_independent_outline_text(monkeypatch):
    """FIX A's single source of truth: outline_text is DERIVED from chapters[], never asked for.

    Asking the LLM for outline_text as its own field is what let the reviewed text and the
    text that drives chapter generation diverge once beatmap/twist injection arrived.
    """
    for kwargs in ({"chap_count": 3},
                   {"chap_count": 20},
                   {"action": "revise", "chap_count": 3,
                    "current_outline": "Bab 1: sesuatu",
                    "revise_instruction": "perkuat konflik"}):
        prompt, result = _capture_outline_prompt(monkeypatch, **kwargs)
        assert "outline_text" not in prompt
        # …and it is still produced, derived from the parsed chapters
        assert result.get("outline_text")
