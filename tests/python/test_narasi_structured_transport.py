"""P0-B TRANSPORT — does a structured-output request actually REACH the provider call?

Live canary `oehhe741` refused because the Story Bible asked for a fenced JSON block in
free text and nothing obliged the model to emit one: `orchestrator.core.Worker` had no
`response_format` field and `_sync_chat` never passed one, so `NARASI_GENAI_JSON_MIME=1`
could not apply to that call however it was configured. Everything BELOW this seam already
worked — `_NarasiFailoverClient._create` translates `call_kw["response_format"]` into
`response_json=True`, which `_vertex_gemini_create` turns into
`response_mime_type="application/json"`. Only the last hop was missing.

The P0-B suite could not have caught this: it injects a perfect fence from a mock, which
proves the PARSER handles an envelope and says nothing about whether the transport ever
demanded one. These two tests close that gap from the other end — they assert on the exact
kwargs the provider's `chat.completions.create` receives, with the REAL `run_worker` and
the REAL `_sync_chat` in the path. Only `_lz_make_client` is replaced, so the seam under
test is the one that broke.

No network, no provider, no Railway.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

from orchestrator import core as core_mod  # noqa: E402

MODEL = "gemini-2.5-flash"


def _provider_response(text: str = "ok"):
    """Minimal OpenAI-shaped response `_extract()` can read without special-casing."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def _capture_provider_kwargs(monkeypatch) -> list[dict]:
    """Install a recording client and return the list its `create()` appends to.

    Records `dict(kwargs)` — a snapshot — so a later in-place mutation by production code
    cannot rewrite what the assertion sees.
    """
    seen: list[dict] = []

    class _Completions:
        @staticmethod
        def create(**kwargs):
            seen.append(dict(kwargs))
            return _provider_response()

    class _Client:
        chat = SimpleNamespace(completions=_Completions)

    monkeypatch.setattr(core_mod, "_lz_make_client",
                        lambda model="", role="", phase="": _Client())
    return seen


def _run(worker) -> dict:
    return asyncio.run(core_mod.run_worker(worker, "task", timeout=30.0,
                                           task_id="planner:bible"))


def test_response_format_reaches_the_provider_call_intact(monkeypatch):
    """A requested `response_format` arrives at `chat.completions.create` unchanged.

    Asserts EQUALITY against the exact object requested, not truthiness. Downstream reads
    it as `bool(call_kw.get("response_format"))`, so a transport that coerced the dict to
    `True` would satisfy every boolean check while sending a provider a value it cannot
    interpret — the assertion has to pin the VALUE for this test to mean anything.
    """
    seen = _capture_provider_kwargs(monkeypatch)
    requested = {"type": "json_object"}

    res = _run(core_mod.Worker(name="bible", role="manager", phase="bible",
                               model=MODEL, response_format=requested))

    assert res["ok"] is True
    assert len(seen) == 1, "expected exactly one provider call"
    assert "response_format" in seen[0], (
        "response_format never reached the provider — this is the oehhe741 defect")
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["response_format"] is not True, "dict must not be coerced to a bool"


def test_default_worker_omits_the_response_format_key_entirely(monkeypatch):
    """The default `Worker` sends NO `response_format` key at all — not `None`.

    Absence, not falsiness, is the requirement. `_sync_chat`'s kwargs are splatted straight
    into the plain OpenAI rung's own `create()`, so an explicit `response_format=None`
    would be a new key on the wire for every off-mode and shadow-mode job — a behaviour
    change on the path P0-B is required to leave byte-identical. `in` is the only check
    that can tell those two states apart.
    """
    seen = _capture_provider_kwargs(monkeypatch)

    res = _run(core_mod.Worker(name="bible", role="manager", phase="bible", model=MODEL))

    assert res["ok"] is True
    assert len(seen) == 1, "expected exactly one provider call"
    assert "response_format" not in seen[0], (
        "default Worker leaked a response_format key onto the provider call "
        f"(value={seen[0].get('response_format')!r}) — off-path is no longer byte-identical")
    # The whole default call shape, pinned: anything ADDED here later is a wire change for
    # every non-assist job and should have to edit this line deliberately.
    assert set(seen[0]) == {"model", "messages", "max_tokens", "temperature", "stream"}
