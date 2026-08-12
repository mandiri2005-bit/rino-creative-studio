"""P0-B — the `canon_registry` SIDECAR, from the structured Story Bible to its consumer.

The single-object Story Bible contract takes `canon_registry` out of the prose, where
`narration_api`'s canon-diff had always found it by regex-scraping
`result["canonical_facts"]`. Left unthreaded, that scrape finds nothing on an assist job
and falls through to a PAID LLM re-extraction — a brand-new per-job cost introduced by a
change that was meant to be cost-neutral. `narrate_chapters` therefore hands the decoded
registry over directly, and this file holds the consumer to it.

🔴 THE CASE THAT MOTIVATES THE WHOLE FILE: an EMPTY sidecar. `{}` is a real answer — the
   bible said this premise has no registry rows — and a truthiness check reads it as
   "nothing was threaded", which is precisely the reading that bills a provider call to
   re-derive an emptiness already known. Absence is `None`; emptiness is `{}`; only the
   first may cost money.

The provider spy asserts on the EXTRACTION prompt specifically rather than on "any cheap
call at all": `_apply_v3_gates` runs several other gates that legitimately call the same
helper, so a blanket "never called" assertion would be both fragile and, when other gates
are off, vacuously true.

No network, no provider, no Railway.
"""
import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

import laozhang_api  # noqa: E402
import narration_api as napi  # noqa: E402

#: First words of the fallback's own system prompt (`narration_api`, canon-diff block).
_EXTRACT_MARKER = "Extract the CANON REGISTRY"

#: Prose with NO fenced block anywhere — what `canonical_facts` looks like under the
#: structured contract, and the exact condition that sends the scrape home empty-handed.
_PROSE = "1. CHARACTERS\nRatna, seorang guru.\n2. TIMELINE\nTiga hari.\n"


@pytest.fixture(autouse=True)
def _diff_on(monkeypatch):
    monkeypatch.setenv("NARASI_CANON_DIFF", "1")
    monkeypatch.setenv("NARASI_CANON_REGISTRY_EXTRACT", "1")


def _spy(monkeypatch) -> list:
    """Record every cheap-call system prompt; never reach a provider."""
    seen: list = []

    async def _fake_cheap_call(system, user, *, tenant_id=None, user_id=None,
                               job_uuid=None, json_mode=False, **kw):
        seen.append(str(system))
        return "{}", None

    monkeypatch.setattr(laozhang_api, "_narasi_cheap_call", _fake_cheap_call)
    return seen


def _result(registry):
    return {
        "ok": True,
        "book": "## Bab 1\n\nIsi bab.\n",
        "chapters": [{"id": 1, "title": "Bab 1", "text": "Isi bab."}],
        "canonical_facts": _PROSE,
        "canon_registry": registry,
    }


def _run(result):
    asyncio.run(napi._apply_v3_gates(result, {"chapters": result["chapters"]},
                                     tenant_id=None, user_id=None, job_uuid=None))


def _extraction_calls(seen) -> list:
    return [s for s in seen if _EXTRACT_MARKER in s]


def test_an_empty_sidecar_still_bypasses_the_scrape_and_the_paid_fallback(monkeypatch):
    """`{}` threaded ⟹ the consumer has its answer. No re-extraction may be billed."""
    seen = _spy(monkeypatch)
    _run(_result({}))
    assert _extraction_calls(seen) == [], (
        "an empty threaded registry was misread as 'nothing threaded' and billed a "
        "provider call to re-extract it")


def test_a_populated_sidecar_also_bypasses_the_paid_fallback(monkeypatch):
    seen = _spy(monkeypatch)
    _run(_result({"events": [{"id": "e1", "summary": "s"}]}))
    assert _extraction_calls(seen) == []


def test_no_sidecar_at_all_still_reaches_the_fallback(monkeypatch):
    """The discriminator. Without this row the two assertions above could pass simply
    because the harness never reaches the fallback, and the file would prove nothing.
    `None` is a genuine absence, so paying to recover the registry is the correct
    behaviour — unchanged from before the sidecar existed."""
    seen = _spy(monkeypatch)
    _run(_result(None))
    assert len(_extraction_calls(seen)) == 1, (
        "the fallback is meant to fire when nothing was threaded and the prose carries "
        "no fence — if it does not, the other tests in this file are vacuous")
