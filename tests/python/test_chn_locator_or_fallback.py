"""
Regression test for the `_chn_locators` / `_spans` OR-fallback fix inside
`_narasi_revise_chunked` (laozhang_api.py).

Bug (pre-fix): the 3-tuple builder computed `_chnos = _chn_locators(ev) if not _sps else []` —
i.e. the deterministic "@chN" chapter-locator was silently DISCARDED whenever the evidence also
contained a quoted span, even if that quote no longer matches the manuscript text verbatim (e.g.
after polish touched the seam). This meant `numeric_arithmetic` and `chapter_boundary_break`
violations — which append BOTH a quote and an "@chN" locator as belt-and-suspenders — could not
fall back to the locator when the quote failed to match, and the violation silently landed in the
UNMAPPED bucket (0 chapters targeted, fix never applied).

Fix: compute `_chnos` unconditionally; `_viol_targets_part` already implemented the correct
per-part OR (quote match OR chapter-number match), so no other logic needed to change.

These tests exercise the REAL `_narasi_revise_chunked` function (not a re-implementation of its
regex logic) with a stubbed LLM client, and assert on which chapter(s) actually received an LLM
call — the only externally observable signature of "was this violation routed to this chapter".
"""
import asyncio

import pytest

import laozhang_api


MARKER = "[CHAPTER — return the corrected version, unchanged except for the fixes]\n"

FULL_TEXT = (
    "## Chapter 1\n"
    "Maya walked along the quiet shoreline, humming a half-remembered tune from childhood "
    "summers spent by the sea with her grandmother.\n"
    "\n"
    "## Chapter 2\n"
    "The old lighthouse keeper counted forty-two lanterns that winter, though the year before "
    "the ledger had listed only thirty-eight of them.\n"
)


class _FakeResp:
    def __init__(self, content):
        self.usage = type("U", (), {"prompt_tokens": 50, "completion_tokens": 20})()
        ch = type("Ch", (), {})()
        ch.message = type("M", (), {"content": content})()
        ch.finish_reason = "stop"
        self.choices = [ch]


class _EchoClient:
    """Fake LLM client: echoes back the SAME chapter body it was given (extracted from the
    prompt), so the accept-gate (word-band / heading-sequence / fidelity) always trivially
    passes and every call is recorded — letting the test assert purely on ROUTING (which
    chapter(s) got an LLM call at all), independent of the accept/reject mechanics."""

    def __init__(self):
        self.calls = []
        comp = type("Comp", (), {})()

        def create(**kw):
            self.calls.append(kw)
            user_content = kw["messages"][1]["content"]
            idx = user_content.index(MARKER) + len(MARKER)
            body = user_content[idx:]
            return _FakeResp(body)

        comp.create = create
        self.chat = type("Chat", (), {"completions": comp})()


def _targeted_chapter_headings(calls):
    """Which chapter heading(s) appear in the prompts actually sent to the LLM."""
    heads = set()
    for kw in calls:
        content = kw["messages"][1]["content"]
        if "## Chapter 1" in content.split(MARKER)[-1][:20]:
            heads.add(1)
        if "## Chapter 2" in content.split(MARKER)[-1][:20]:
            heads.add(2)
    return heads


@pytest.fixture
def _patched(monkeypatch):
    async def fake_usage(*a, **k):
        return 1
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_usage)
    client = _EchoClient()
    monkeypatch.setattr(laozhang_api, "make_narasi_client", lambda *a, **k: client)
    monkeypatch.setenv("NARASI_REVISE_MAX_CHAPTERS", "4")
    monkeypatch.delenv("NARASI_REVISE_PARALLEL", raising=False)
    return client


def _run(viol, parallel=None, monkeypatch=None):
    if parallel is not None:
        monkeypatch.setenv("NARASI_REVISE_PARALLEL", str(parallel))
    return asyncio.run(laozhang_api._narasi_revise_chunked(
        FULL_TEXT, viol, style="test-style", language="en", rev_model="gemini-2.5-flash",
        tenant_id="t1", user_id="u1", job_uuid="job-1"))


class TestBugScenarioQuoteMissesButChnValid:
    """The exact bug repro: evidence has a quoted span that does NOT literally occur anywhere
    in the manuscript (simulating polish having touched the seam) but DOES carry a correct,
    valid @chN suffix. Must route to chapter N via the locator fallback."""

    def test_chapter_boundary_break_serial(self, _patched):
        viol = [{
            "type": "chapter_boundary_break",
            "severity": "high",
            "evidence": '"This exact sentence never appears anywhere in the manuscript text" @ch2',
            "fix": "Add a bridging sentence.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {2}, f"expected only chapter 2 targeted, got {targeted} (calls={len(_patched.calls)})"

    def test_chapter_boundary_break_parallel(self, _patched, monkeypatch):
        viol = [{
            "type": "chapter_boundary_break",
            "severity": "high",
            "evidence": '"This exact sentence never appears anywhere in the manuscript text" @ch2',
            "fix": "Add a bridging sentence.",
        }]
        _run(viol, parallel=2, monkeypatch=monkeypatch)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {2}, f"parallel fast-path: expected only chapter 2 targeted, got {targeted}"

    def test_numeric_arithmetic_quote_miss_falls_back_to_chn(self, _patched):
        viol = [{
            "type": "numeric_arithmetic",
            "severity": "high",
            "evidence": '"a totally different unquoted-in-manuscript sentence" @ch1',
            "fix": "Correct the total.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {1}, f"expected only chapter 1 targeted, got {targeted}"


class TestNormalQuoteMatchStillWorks:
    """A violation whose quote DOES literally occur in the manuscript must keep routing via the
    existing quote-match path, unaffected by the fix (sanity / no-regression check)."""

    def test_quote_matches_chapter1_no_chn(self, _patched):
        viol = [{
            "type": "domain_error",
            "severity": "high",
            "evidence": '"Maya walked along the quiet shoreline"',
            "fix": "Fix the domain error.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {1}, f"expected only chapter 1 targeted via quote match, got {targeted}"

    def test_quote_matches_and_chn_agrees_no_double_target(self, _patched):
        # Quote matches chapter 1 AND @ch1 also says chapter 1 — same chapter via both signals.
        # Must be idempotent (no double-call / no duplicate targeting of the same chapter).
        viol = [{
            "type": "numeric_arithmetic",
            "severity": "high",
            "evidence": '"Maya walked along the quiet shoreline" @ch1',
            "fix": "Fix it.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {1}
        assert len(_patched.calls) == 1, "same chapter targeted via both signals must not double-call"


class TestNumericDriftUnaffectedByFix:
    """numeric_drift's evidence never has quote marks at all (_spans() == [] regardless of the
    fix) — this type already worked pre-fix and must be unaffected (no regression)."""

    def test_numeric_drift_no_quotes_routes_via_chn(self, _patched):
        viol = [{
            "type": "numeric_drift",
            "severity": "high",
            "evidence": "34@ch1; 30@ch2",
            "fix": "Unify the value.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {1, 2}, f"expected both chapters targeted (both @chN present), got {targeted}"


class TestMismatchedDoubleTarget:
    """Quote coincidentally matches one chapter's text while @chN names a DIFFERENT chapter —
    both chapters get the (harmless, no-op-safe) directive. Not a corruption risk per the
    investigation; just documents the accepted, tolerated fanout behavior."""

    def test_quote_and_chn_disagree_targets_both(self, _patched):
        viol = [{
            "type": "numeric_arithmetic",
            "severity": "high",
            # quote literally occurs in chapter 2's text, but @ch1 points at chapter 1.
            "evidence": '"forty-two lanterns that winter" @ch1',
            "fix": "Fix it.",
        }]
        _run(viol)
        targeted = _targeted_chapter_headings(_patched.calls)
        assert targeted == {1, 2}, f"expected both chapters targeted on mismatch, got {targeted}"
