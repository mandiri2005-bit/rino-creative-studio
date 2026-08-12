"""Locks the /narasi/outline path against the EXACT env deployed on Railway.

Every earlier outline regression here was invisible to unit tests because the tests
configured a tidy hypothetical env while production ran something else. These tests
hard-code the real variables instead:

    NARASI_OUTLINE_MODEL     = gemini-3.6-flash
    NARASI_OUTLINE_GEMINI    = gemini-3.6-flash
    NARASI_OUTLINE_LAOZHANG  = 0
    NARASI_OUTLINE_KIE       = 0
    NARASI_OUTLINE_ATLASCLOUD= 0

The correct routing result under THAT env is a single-rung Vertex chain — NOT
Vertex→LaoZhang. There is no fallback while NARASI_OUTLINE_LAOZHANG=0, and a test that
asserted a fallback would be encoding an intention rather than the deployment.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import laozhang_api

OUTLINE_MODEL = "gemini-3.6-flash"

# The deployed switchboard, verbatim.
PROD_ENV = {
    "NARASI_OUTLINE_MODEL": OUTLINE_MODEL,
    "NARASI_OUTLINE_GEMINI": OUTLINE_MODEL,
    "NARASI_OUTLINE_LAOZHANG": "0",
    "NARASI_OUTLINE_KIE": "0",
    "NARASI_OUTLINE_ATLASCLOUD": "0",
}

# Verified read-only on Railway positive-radiance -> python, 2026-08-11. The CODE DEFAULT is
# 120000, so this is a genuine divergence and not a restatement of the source: production
# accepts a third of what the default allows. Patched as a module ATTRIBUTE rather than an
# env var because DALANG_MAX_TOTAL_WORDS is read once at import — setting the environment
# inside a test would silently do nothing, which is exactly how a test can look like it
# locks a production limit while asserting the default.
PROD_MAX_TOTAL_WORDS = 40_000


@pytest.fixture(autouse=True)
def _keyless_provider_chain(monkeypatch):
    """Put this file's chain in the NO-KEY state its tests are written against.

    🔴 THIS IS THE SCENARIO, NOT A NETWORK GUARD — and the distinction is load-bearing.
       conftest.py does `os.environ.setdefault("LAOZHANG_API_KEY", "sk-test-key-for-unit-
       tests")`, and the rung-skip guard in `_NarasiFailoverClient._create` is
       `if not key: continue` — so a placeholder key makes the laozhang rung read as USABLE
       and the real `_create` walks it. Emptying the keys here is what produces the KEYLESS
       chain `test_legacy_phase_less_path_keeps_the_plain_fallthrough` exists to exercise
       ("the P1 no-key case"); without it that test does not merely lose a guard, it stops
       testing the path it names, and its `make_client` fallthrough is never reached.
       Verified by removing this fixture on the integration branch: exactly that one test
       failed, with six real attempts to api.laozhang.ai.

    ⚠️ THE OUTBOUND GUARD ITSELF IS NO LONGER HERE. When this file was written on its own
       branch it also stubbed `laozhang_api.OpenAI` with a raising tripwire, because no
       suite-wide protection existed yet. `tests/python/conftest.py` now blocks at the
       SOCKET (`939f1bc`), covering every transport this repo reaches the network through —
       not just the OpenAI constructor — and FAILS the offending test in teardown rather
       than relying on the caller to surface the error. That is strictly broader than the
       tripwire it replaces, so the tripwire was removed as genuinely duplicated
       enforcement. What stays here is only the key-emptying, which is test setup.
    """
    for var in ("LAOZHANG_API_KEY", "KIE_API_KEY", "ATLASCLOUD_API_KEY", "CLAUDE_API_KEY",
                "AIMLAPI_API_KEY", "DEEPSEEK_API_KEY", "DEEPSEEK_LAOZHANG_API_KEY",
                "OPENAI_API_KEY", "GEMINI_API_KEY", "FAL_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(laozhang_api, "API_KEY", "", raising=False)


@pytest.fixture
def prod_env(monkeypatch):
    """Exact production configuration, with Vertex credentials stubbed (no network)."""
    for key, value in PROD_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(laozhang_api, "_ensure_vertex", lambda: True)
    monkeypatch.setattr(
        laozhang_api, "DALANG_MAX_TOTAL_WORDS", PROD_MAX_TOTAL_WORDS, raising=True)
    return monkeypatch


# ── Production admission ceiling ─────────────────────────────────────────────────

def test_production_total_word_ceiling_is_enforced_at_40k(prod_env):
    """Production caps a whole book at 40000 words, not the code default of 120000."""
    prod_env.setenv("DALANG_ADMISSION_ENABLED", "1")

    # Exactly at the ceiling is allowed.
    laozhang_api._narasi_admit(
        {"chap_count": 20, "word_max": PROD_MAX_TOTAL_WORDS}, kind="outline")

    # One word over is refused.
    with pytest.raises(HTTPException) as excinfo:
        laozhang_api._narasi_admit(
            {"chap_count": 20, "word_max": PROD_MAX_TOTAL_WORDS + 1}, kind="outline")
    assert excinfo.value.status_code == 400

    # And the value the code would use unconfigured must NOT pass in production.
    with pytest.raises(HTTPException):
        laozhang_api._narasi_admit({"chap_count": 20, "word_max": 120_000},
                                   kind="outline")


def test_code_default_total_word_ceiling_still_differs_from_production(prod_env):
    """Fails loudly if the default is ever changed to match, so the note above cannot rot."""
    assert laozhang_api.DALANG_MAX_TOTAL_WORDS == PROD_MAX_TOTAL_WORDS  # patched here
    # Unpatched module default, read straight from the source of truth.
    prod_env.undo()
    assert laozhang_api.DALANG_MAX_TOTAL_WORDS == 120_000, (
        "the code default changed — update PROD_MAX_TOTAL_WORDS and re-verify Railway")


def _chain(phase="outline", model=OUTLINE_MODEL):
    return [(rung[0], rung[4])
            for rung in laozhang_api._narasi_failover_chain(model, "", phase)]


# ── Routing ──────────────────────────────────────────────────────────────────────

def test_prod_config_chain_is_vertex_only(prod_env):
    """The deployed env yields ONE rung: Vertex serving the outline model."""
    assert _chain() == [("vertex", OUTLINE_MODEL)]


def test_prod_config_has_no_laozhang_rung(prod_env):
    """Explicit: NARASI_OUTLINE_LAOZHANG=0 means there is no fallback at all.

    Kept separate from the equality assertion above so a future change that adds a rung
    fails with a message naming the reason rather than a bare tuple mismatch.
    """
    rungs = [name for name, _ in _chain()]
    assert "laozhang" not in rungs, (
        "LaoZhang appeared in the outline chain while NARASI_OUTLINE_LAOZHANG=0 — either "
        "the phase gate stopped excluding it, or the deployed flag changed and PROD_ENV "
        "here is now stale.")
    assert len(rungs) == 1


@pytest.mark.parametrize("failover_flag", [None, "0", "1"])
def test_prod_config_chain_ignores_the_global_failover_flag(prod_env, failover_flag):
    """NARASI_FAILOVER_ENABLED does not influence this path.

    _narasi_will_chain short-circuits on `_phase_active` — set by NARASI_OUTLINE_GEMINI
    itself — long before it reads the global flag. Worth locking: reasoning about outline
    routing via NARASI_FAILOVER_ENABLED is a dead end, and this records that.
    """
    if failover_flag is None:
        prod_env.delenv("NARASI_FAILOVER_ENABLED", raising=False)
    else:
        prod_env.setenv("NARASI_FAILOVER_ENABLED", failover_flag)

    assert laozhang_api._narasi_will_chain(OUTLINE_MODEL, "outline") is True
    assert _chain() == [("vertex", OUTLINE_MODEL)]


def test_flipping_laozhang_on_gives_vertex_then_laozhang_on_the_same_model(prod_env):
    """The one thing _NARASI_GEMINI_FAILOVER_MODELS membership actually buys.

    Membership is a no-op under today's env. It becomes load-bearing the moment LaoZhang
    is enabled: without it the base chain does not recognise the id as gemini-family and
    falls through to the CLAUDE chain, so the laozhang rung would serve claude-opus-4-6
    AND sit ahead of Vertex. This test is what makes flipping the flag safe.
    """
    prod_env.setenv("NARASI_OUTLINE_LAOZHANG", "1")

    assert _chain() == [("vertex", OUTLINE_MODEL), ("laozhang", OUTLINE_MODEL)]


def test_outline_model_is_gemini_family_for_chain_building(prod_env):
    assert OUTLINE_MODEL in laozhang_api._NARASI_GEMINI_FAILOVER_MODELS


# ── Registration, pricing, ceiling ───────────────────────────────────────────────

def test_outline_model_is_registered_and_priced(prod_env):
    """Registration and price must land together or the model breaks in one of two ways."""
    assert OUTLINE_MODEL in laozhang_api.MODELS
    assert laozhang_api._calc_cost(OUTLINE_MODEL, 1000, 1000) > 0
    assert laozhang_api._narasi_model_ok(OUTLINE_MODEL) is True


def test_outline_model_has_an_explicit_token_ceiling(prod_env):
    """A missing MODEL_MAX_TOKENS row silently truncates at DEFAULT_MAX_TOKENS."""
    ceiling = laozhang_api.MODEL_MAX_TOKENS.get(OUTLINE_MODEL)
    assert ceiling is not None
    assert ceiling != laozhang_api.DEFAULT_MAX_TOKENS
    # The widest outline admission allows: DALANG_MAX_CHAPTERS=20 -> 20*600+2000.
    assert ceiling >= 20 * 600 + 2000


def test_outline_model_routes_to_the_vertex_global_endpoint(prod_env):
    """3.x models 404 on the regional endpoint; the omission fails silently as a fallthrough."""
    assert OUTLINE_MODEL in laozhang_api._VERTEX_GLOBAL_MODELS
    assert laozhang_api._vertex_location_for(OUTLINE_MODEL) == "global"


# ── Admission ────────────────────────────────────────────────────────────────────

def test_admission_validates_the_effective_model_not_the_body(prod_env):
    """The gate must inspect NARASI_OUTLINE_MODEL, which is what actually executes.

    A body model that would FAIL the whitelist must not matter, because the outline path
    ignores it — and an env model that fails MUST be refused even when the body is clean.
    """
    prod_env.setenv("DALANG_ADMISSION_ENABLED", "1")

    # Junk in the body is irrelevant: execution never reads it.
    laozhang_api._narasi_admit(
        {"model": "totally-fake-model-xyz", "chap_count": 3, "word_max": 2100},
        kind="outline")


def test_admission_refuses_an_unpriced_env_model_as_a_server_error(prod_env):
    """An operator pointing the env at an unregistered model is a 5xx, not a 400."""
    prod_env.setenv("DALANG_ADMISSION_ENABLED", "1")
    prod_env.setenv("NARASI_OUTLINE_MODEL", "gemini-9-imaginary")

    with pytest.raises(HTTPException) as excinfo:
        laozhang_api._narasi_admit({"chap_count": 3, "word_max": 2100}, kind="outline")

    assert excinfo.value.status_code == 500
    assert "gemini-9-imaginary" in str(excinfo.value.detail)


def test_admission_and_execution_read_the_same_helper(prod_env):
    """Guards the drift itself: one function, one answer."""
    assert laozhang_api._narasi_outline_model() == OUTLINE_MODEL
    prod_env.setenv("NARASI_OUTLINE_MODEL", "gemini-2.5-flash")
    assert laozhang_api._narasi_outline_model() == "gemini-2.5-flash"


def test_generate_admission_still_reports_a_bad_body_model_as_400(prod_env):
    """The outline branch must not have changed the client-supplied path."""
    prod_env.setenv("DALANG_ADMISSION_ENABLED", "1")

    with pytest.raises(HTTPException) as excinfo:
        laozhang_api._narasi_admit(
            {"model": "totally-fake-model-xyz",
             "chapters": [{"words": 400}]}, kind="generate")

    assert excinfo.value.status_code == 400


# ── Metering ─────────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(
            message=SimpleNamespace(content=content), finish_reason="stop")]
        self.usage = SimpleNamespace(prompt_tokens=1200, completion_tokens=900)


OUTLINE_BODY = {
    "action": "outline",
    "topic": "Kontrak Cinta 30 Hari di Rooftop Seoul",
    "style": "romance",
    "language": "id",
    "word_min": 1800,
    "word_max": 2100,
    "chap_count": 3,
}


def _run_outline_capturing_usage(monkeypatch, served_model="gemini-3.6-flash-served"):
    """Drive the outline with the persistence layer STUBBED TO SUCCEED, and return every
    usage row it logged.

    🔴 The stubs are the whole point. The redundant second _log_narasi_usage lived INSIDE
       the save_outline try/except, after the db write. With no database, db.save_outline
       raises, the except swallows it, and the second log never runs — so a test that does
       not stub persistence sees ONE row on the buggy code too and proves nothing. (Caught
       exactly that way: the first version of this test passed against the unfixed tree.)
       In production save_outline succeeds, so the second row is real.
    """
    logged = []

    def fake_complete(model, messages, max_tokens, role="", phase="",
                      response_format=None):
        return _FakeResp(
            '{"chapters": [{"id": "1", "title": "Satu", '
            '"description": "Beat.", "words": 1800}]}'), served_model

    async def fake_log_usage(tenant, user, model, resp, **kwargs):
        logged.append({"model": model, "resp": id(resp), "charge": kwargs.get("charge")})
        return 0

    async def fake_save_outline(*_args, **_kwargs):
        return "outline-id"

    async def fake_resolve_user_uuid(*_args, **_kwargs):
        return "00000000-0000-0000-0000-000000000001"

    monkeypatch.setattr(laozhang_api, "_narasi_complete", fake_complete)
    monkeypatch.setattr(laozhang_api, "_log_narasi_usage", fake_log_usage)
    monkeypatch.setattr(laozhang_api.db, "save_outline", fake_save_outline)
    monkeypatch.setattr(laozhang_api, "_resolve_user_uuid", fake_resolve_user_uuid)
    monkeypatch.delenv("DALANG_BEATMAP_ENABLED", raising=False)

    result = asyncio.run(laozhang_api._narasi_outline_impl(dict(OUTLINE_BODY)))
    return result, logged


def test_outline_logs_exactly_one_usage_row_for_the_outline_response(prod_env):
    """The main outline response must be metered ONCE, keyed on the model that SERVED.

    It used to be logged twice against the same `resp` — once with the served model and
    once with the env model — so SUM(usage_logs.credits) double-counted every outline and
    the two rows disagreed about which model ran.
    """
    result, logged = _run_outline_capturing_usage(prod_env)

    assert result["ok"] is True
    assert len(logged) == 1, f"expected one usage row, got {len(logged)}: {logged}"
    # Keyed on what served, so a failover swap stays attributable.
    assert logged[0]["model"] == "gemini-3.6-flash-served"


def test_outline_never_meters_the_same_response_object_twice(prod_env):
    """Distinct from the count above: no two rows may share one `resp` object."""
    _result, logged = _run_outline_capturing_usage(prod_env)

    resp_ids = [row["resp"] for row in logged]
    assert len(resp_ids) == len(set(resp_ids)), (
        f"the same response object was metered more than once: {logged}")


# ── The fallback loop under the real switchboard ─────────────────────────────────
#
# _narasi_complete tries `model` then _NARASI_MODEL_FALLBACKS. Those fallbacks are
# claude-* and gemini-2.5 ids, which LOOK like genuinely different models — but the
# outline phase switchboard rewrites every one of them to the same single-rung Vertex
# chain. Four candidates x _NARASI_RUNG_ATTEMPTS=2 = up to eight identical upstream calls
# that cannot fail differently, and a success on a later candidate used to be RETURNED
# under that candidate's name, so the usage row was priced as Opus while Gemini served.


class _CountingChain:
    """Stands in for make_narasi_client, recording exactly what goes upstream."""

    def __init__(self, calls, fail_times=0, served_model="gemini-3.6-flash"):
        self._calls = calls
        self._fail_times = fail_times
        self._served_model = served_model
        outer = self

        def create(**kwargs):
            # model AND max_tokens: a fallback clamps the budget to its own ceiling, so the
            # two together are what makes an attempt distinct.
            outer._calls.append({"model": kwargs["model"],
                                 "max_tokens": kwargs.get("max_tokens")})
            if len(outer._calls) <= outer._fail_times:
                return _FakeResp(None)          # empty 200 → counts as a rung failure
            resp = _FakeResp('{"chapters": [{"id": "1"}]}')
            # what the winning rung really sent, as the failover client stamps it
            object.__setattr__(resp, "_narasi_served_model", outer._served_model)
            object.__setattr__(resp, "_narasi_served_by", "vertex")
            return resp

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _patch_client(monkeypatch, calls, fail_times=0,
                  served_model="gemini-3.6-flash"):
    chain = _CountingChain(calls, fail_times=fail_times, served_model=served_model)
    monkeypatch.setattr(laozhang_api, "make_narasi_client",
                        lambda model, role="", phase="": chain)
    return chain


def test_fallback_loop_does_not_repeat_the_same_upstream_call(prod_env):
    """A primary failure must not re-issue the identical gemini-3.6-flash call.

    Every candidate collapses to [(vertex, gemini-3.6-flash)] at this env, so after the
    first attempt there is nothing left to try and the loop must give up rather than
    burn the remaining candidates on copies of the failed call.
    """
    calls = []
    _patch_client(prod_env, calls, fail_times=99)   # never succeeds

    with pytest.raises(RuntimeError):
        laozhang_api._narasi_complete(
            OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 4000,
            phase="outline", response_format={"type": "json_object"})

    assert len(calls) == 1, (
        f"expected ONE distinct upstream attempt, got {len(calls)}: {calls} — the "
        f"fallback candidates collapse onto the same chain and were retried anyway.")


def test_fallback_loop_reports_the_skipped_duplicates(prod_env):
    """The skip must be visible in the error, not silent."""
    calls = []
    _patch_client(prod_env, calls, fail_times=99)

    with pytest.raises(RuntimeError) as excinfo:
        laozhang_api._narasi_complete(
            OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 4000,
            phase="outline")

    message = str(excinfo.value)
    for fallback in laozhang_api._NARASI_MODEL_FALLBACKS:
        assert fallback in message, (
            f"{fallback} vanished from the error without explanation: {message}")
    assert "duplicate-chain" in message


def test_billing_identity_is_the_model_that_served_not_the_candidate(prod_env):
    """The returned model must be what ran, because callers price it directly.

    claude-opus-4-7 is $5/$25 per M against gemini-3.6-flash's $1.50/$7.50, and the
    brief call site passes charge=True — so a mislabelled row is a real overcharge, not
    just bad telemetry.
    """
    calls = []
    _patch_client(prod_env, calls, served_model=OUTLINE_MODEL)

    _resp, used_model = laozhang_api._narasi_complete(
        "claude-opus-4-7", [{"role": "user", "content": "outline"}], 4000,
        phase="outline")

    assert used_model == OUTLINE_MODEL, (
        f"billed as {used_model!r} but Vertex served {OUTLINE_MODEL!r}")
    # And the price that follows from it is Gemini's, not Opus's.
    assert (laozhang_api._calc_cost(used_model, 1_000_000, 1_000_000)
            < laozhang_api._calc_cost("claude-opus-4-7", 1_000_000, 1_000_000))


def test_plain_non_chaining_path_still_reports_the_requested_model(prod_env):
    """No override in play → the requested alias is already correct and must survive."""
    calls = []
    chain = _CountingChain(calls)
    # A response with no served-model stamp is what the plain make_client path yields.
    def create(**kwargs):
        calls.append(kwargs["model"])
        return _FakeResp('{"chapters": [{"id": "1"}]}')
    chain.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    prod_env.setattr(laozhang_api, "make_narasi_client",
                     lambda model, role="", phase="": chain)

    _resp, used_model = laozhang_api._narasi_complete(
        "gemini-2.5-flash", [{"role": "user", "content": "x"}], 4000)

    assert used_model == "gemini-2.5-flash"


def test_distinct_chains_are_still_tried(prod_env):
    """Dedup must not disable real failover: different chains must all get a turn.

    Guards against over-correcting the fix into "only ever try one candidate".
    """
    prod_env.delenv("NARASI_OUTLINE_GEMINI", raising=False)
    prod_env.delenv("NARASI_OUTLINE_LAOZHANG", raising=False)
    prod_env.delenv("NARASI_OUTLINE_KIE", raising=False)
    prod_env.delenv("NARASI_OUTLINE_ATLASCLOUD", raising=False)
    calls = []
    _patch_client(prod_env, calls, fail_times=99)

    with pytest.raises(RuntimeError):
        laozhang_api._narasi_complete(
            OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 4000)

    assert len(calls) > 1, (
        "with no switchboard override the candidates resolve to different models and "
        f"each must be attempted; only {calls} ran")


# ── Dedup must not eat real fallbacks ────────────────────────────────────────────
#
# The dedup above is only safe while its signature matches what _create actually does.
# Two ways it can diverge, each turning the dedup from "saves waste" into "destroys
# recovery", which is strictly worse than the duplicate calls it was added to fix.


def _make_client_tripwire(monkeypatch):
    """Install make_client as a tripwire and return the list that records reaching it.

    A raise alone is NOT enough: _narasi_complete catches Exception per candidate and
    re-raises RuntimeError at the end, so an AssertionError from inside the tripwire gets
    swallowed and `pytest.raises(RuntimeError)` would pass even though make_client ran.
    The recording list is what actually makes the assertion sound.
    """
    reached = []

    def tripwire(*_args, **_kwargs):
        reached.append(True)
        raise AssertionError("make_client must not be reached under a governed phase")

    monkeypatch.setattr(laozhang_api, "make_client", tripwire)
    return reached


def test_keyless_chain_under_a_governed_phase_never_reaches_laozhang(prod_env):
    """NARASI_OUTLINE_LAOZHANG=0 must mean LaoZhang gets nothing — Vertex down included.

    🔴 This test previously asserted the OPPOSITE and locked a provider bypass in as
       intended behaviour. The chain holds only a keyless Vertex rung, _create skips it,
       attempted==0, and the old fallthrough called make_client — which routes every
       non-DeepSeek model to LAOZHANG_API_KEY. So the provider the operator switched off
       served the call, precisely when the primary was unavailable. Fail closed instead.
    """
    prod_env.setattr(laozhang_api, "_ensure_vertex", lambda: False)
    reached = _make_client_tripwire(prod_env)

    client = laozhang_api._NarasiFailoverClient(
        OUTLINE_MODEL, role="", phase="outline")

    with pytest.raises(RuntimeError) as excinfo:
        client.chat.completions.create(
            model=OUTLINE_MODEL, messages=[{"role": "user", "content": "outline"}],
            max_tokens=4000, stream=False)

    assert not reached, (
        "make_client was reached — the hidden LaoZhang fallback is still live")
    assert "usable key" in str(excinfo.value)


def test_narasi_complete_surfaces_the_no_route_failure_once(prod_env):
    """One clear error, not four copies: with no route, candidates are equivalent."""
    prod_env.setattr(laozhang_api, "_ensure_vertex", lambda: False)
    reached = _make_client_tripwire(prod_env)

    with pytest.raises(RuntimeError) as excinfo:
        laozhang_api._narasi_complete(
            OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 4000,
            phase="outline")

    message = str(excinfo.value)
    assert not reached, "a fallback candidate leaked through to make_client"
    # The fallbacks are accounted for, as deduped rather than silently dropped.
    for fallback in laozhang_api._NARASI_MODEL_FALLBACKS:
        assert fallback in message, f"{fallback} vanished without explanation: {message}"
    assert message.count("duplicate-chain") == len(laozhang_api._NARASI_MODEL_FALLBACKS)


def test_legacy_phase_less_path_keeps_the_plain_fallthrough(prod_env):
    """Ungoverned phases must keep the historic behaviour, including per-candidate distinctness.

    Without an operator switchboard there is no intent to violate: make_client is simply
    how this file has always served a keyless chain, and each candidate resolves to its OWN
    model there — so all of them must still be attempted (the P1 no-key case, which stays
    correct on this path).
    """
    for flag in ("NARASI_OUTLINE_GEMINI", "NARASI_OUTLINE_LAOZHANG",
                 "NARASI_OUTLINE_KIE", "NARASI_OUTLINE_ATLASCLOUD"):
        prod_env.delenv(flag, raising=False)
    prod_env.setenv("NARASI_FAILOVER_ENABLED", "1")
    prod_env.setattr(laozhang_api, "_ensure_vertex", lambda: False)

    served = []

    def fake_make_client(model=""):
        def create(**kwargs):
            served.append(kwargs["model"])
            return _FakeResp(None)          # empty → keep walking the candidates
        return SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    prod_env.setattr(laozhang_api, "make_client", fake_make_client)

    with pytest.raises(RuntimeError):
        laozhang_api._narasi_complete(
            OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 4000)

    assert len(served) > 1, (
        f"the legacy plain fallthrough lost its per-candidate fallbacks: {served}")
    assert len(set(served)) == len(served), f"repeated an identical call: {served}"


def test_a_lower_token_budget_on_the_same_chain_is_still_attempted(prod_env):
    """A clamped retry is a DIFFERENT attempt, not a duplicate.

    The primary sends max_tokens verbatim; a fallback clamps to its own model ceiling. A
    primary asking 100000 can be rejected outright where the same chain at 64000 succeeds,
    so max_tokens belongs in the signature. The first version of this dedup omitted it and
    swallowed the clamped retry.
    """
    calls = []
    _patch_client(prod_env, calls, fail_times=1)   # first attempt fails, second succeeds

    _resp, _used = laozhang_api._narasi_complete(
        OUTLINE_MODEL, [{"role": "user", "content": "outline"}], 100_000,
        phase="outline")

    assert len(calls) >= 2, (
        f"the clamped retry was skipped as a duplicate: {calls}")
    assert calls[0]["max_tokens"] == 100_000
    assert calls[1]["max_tokens"] < calls[0]["max_tokens"], (
        f"expected a smaller budget on the retry: {calls}")


def test_failover_client_really_stamps_the_served_model(prod_env):
    """Drives the REAL _NarasiFailoverClient._create, not a fake that stamps itself.

    Every other billing-identity test here supplies _narasi_served_model from a stub, so
    none of them would notice if the production stamp were removed. This one would.
    """
    seen = {}

    def fake_vertex_create(model_id, messages, max_tokens, timeout,
                           temperature=None, response_json=False):
        seen["model_id"] = model_id
        return _FakeResp('{"chapters": [{"id": "1"}]}')

    prod_env.setattr(laozhang_api, "_vertex_gemini_create", fake_vertex_create)

    client = laozhang_api._NarasiFailoverClient(
        OUTLINE_MODEL, role="", phase="outline")
    resp = client.chat.completions.create(
        model=OUTLINE_MODEL, messages=[{"role": "user", "content": "outline"}],
        max_tokens=4000, stream=False)

    assert seen["model_id"] == OUTLINE_MODEL, "the vertex rung did not serve this call"
    assert getattr(resp, "_narasi_served_by", None) == "vertex"
    assert getattr(resp, "_narasi_served_model", None) == OUTLINE_MODEL, (
        "the winning rung's upstream model id was not stamped on the response — "
        "_narasi_complete then falls back to the candidate label and bills the wrong model")


def test_outline_is_still_not_charged(prod_env):
    """Records the OPEN billing state rather than pretending it is settled.

    Nothing debits for an outline today: the row carries credits but `charge` is never
    passed. That is a billing decision, deliberately not made inside a routing fix. If
    this test starts failing, someone changed what an outline costs a user — make sure
    that was on purpose.
    """
    _result, logged = _run_outline_capturing_usage(prod_env)

    assert logged[0]["charge"] in (None, False)
