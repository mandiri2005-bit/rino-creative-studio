"""
Dalang v2 — Slice 1 quality + truncation (hermetic, pure functions).

H1 (head→tail): _narasi_prev_snippet must feed the chapter's TAIL when the flag is ON,
and stay byte-identical to the legacy HEAD slice when OFF.

H3 (truncation): _narasi_safe_max must be byte-identical to the old inline clamp when
OFF, and — when ON — lift a THINKING model's output ceiling so reasoning tokens can't
starve the chapter text (the exact defect: min(ceiling,…) collapsed the +32k overhead
back to 16384). Non-thinking and plain-Claude paths stay unchanged either way.
"""
import pytest
import laozhang_api


# ── H1: prev_tail head vs tail ───────────────────────────────────────────────

class TestPrevSnippet:
    def _text(self, n=400):
        return " ".join(f"w{i}" for i in range(n))

    def test_flag_off_keeps_head(self, monkeypatch):
        monkeypatch.delenv("DALANG_SLICE1_ENABLED", raising=False)
        snip = laozhang_api._narasi_prev_snippet(self._text(400), 300)
        toks = snip.replace("…", "").split()
        assert toks[0] == "w0"        # HEAD: starts at the beginning
        assert toks[-1] == "w299"     # HEAD: ends 300 words in
        assert snip.endswith("…")     # ellipsis trails the head slice

    def test_flag_on_keeps_tail(self, monkeypatch):
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "1")
        snip = laozhang_api._narasi_prev_snippet(self._text(400), 300)
        toks = snip.replace("…", "").split()
        assert toks[-1] == "w399"     # TAIL: ends at the chapter ending
        assert toks[0] == "w100"      # TAIL: last 300 of 400 → starts at w100
        assert snip.startswith("…")   # ellipsis leads the tail slice

    def test_short_text_no_ellipsis_either_way(self, monkeypatch):
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "1")
        snip_on = laozhang_api._narasi_prev_snippet("only a few words", 300)
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "0")
        snip_off = laozhang_api._narasi_prev_snippet("only a few words", 300)
        assert "…" not in snip_on and "…" not in snip_off
        assert snip_on == snip_off == "only a few words"


# ── H3: safe_max thinking ceiling ────────────────────────────────────────────

class TestSafeMax:
    def _off_value(self, resolved, word_max):
        """Reproduce the ORIGINAL inline math exactly (the flag-off contract)."""
        ceiling = laozhang_api.MODEL_MAX_TOKENS.get(resolved, laozhang_api.DEFAULT_MAX_TOKENS)
        base = int(word_max * laozhang_api.WORDS_TO_TOKENS_NARASI * 1.2) + 1500
        is_thinking = "thinking" in resolved
        is_plain = resolved.startswith("claude") and not is_thinking
        overhead = laozhang_api.THINKING_TOKEN_OVERHEAD if resolved in laozhang_api.THINKING_MODELS_NARASI else 0
        if is_plain:
            return min(4096, max(4000, base))
        return min(ceiling, max(8000, base + overhead))

    @pytest.mark.parametrize("resolved,word_max", [
        ("gemini-2.5-flash", 4500),   # thinking, ceiling 16384
        ("deepseek-chat", 4500),      # non-thinking, ceiling 8192
        ("claude-opus-4-6", 4500),    # plain claude → 4096 cap
        ("gemini-2.5-pro", 6000),     # thinking, ceiling 65536
    ])
    def test_flag_off_byte_identical(self, monkeypatch, resolved, word_max):
        monkeypatch.delenv("DALANG_SLICE1_ENABLED", raising=False)
        assert laozhang_api._narasi_safe_max(resolved, word_max) == self._off_value(resolved, word_max)

    def test_flag_on_thinking_not_starved(self, monkeypatch):
        """The core H3 assertion: gemini-2.5-flash was clamped to 16384 (starving text);
        with the flag on it gets the larger DALANG_THINKING_MAX_TOKENS ceiling."""
        monkeypatch.delenv("DALANG_SLICE1_ENABLED", raising=False)
        off = laozhang_api._narasi_safe_max("gemini-2.5-flash", 4500)
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "1")
        on = laozhang_api._narasi_safe_max("gemini-2.5-flash", 4500)
        assert off == 16384                                  # the documented truncation trap
        assert on > off                                      # fix actually raises the budget
        assert on == laozhang_api.DALANG_THINKING_MAX_TOKENS  # to the configured ceiling

    def test_flag_on_non_thinking_unchanged(self, monkeypatch):
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "1")
        on = laozhang_api._narasi_safe_max("deepseek-chat", 4500)
        assert on == self._off_value("deepseek-chat", 4500)

    def test_flag_on_plain_claude_unchanged(self, monkeypatch):
        monkeypatch.setenv("DALANG_SLICE1_ENABLED", "1")
        on = laozhang_api._narasi_safe_max("claude-opus-4-6", 4500)
        assert on == self._off_value("claude-opus-4-6", 4500) == 4096
