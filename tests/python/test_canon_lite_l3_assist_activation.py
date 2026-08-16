"""L3-ASSIST ACTIVATION — the preflight, its threading, and the failure vocabulary.

Production job s0di2o1g (2026-08-11) ran with `mode=assist`, its tenant on the allowlist,
injection armed and an `assist census PASS` in the log. It spent a full outline, three
chapters and 121s of gates, then reported `outcome=unchecked stage=no_session rounds=0
chapters_repaired=0 delivery_binding=None`. Nothing had been checked: the QC credential was
absent on narration-worker, `maybe_run_metered_wave` raised `qc_provider_api_key_missing`,
and the terminal seam flattened that to `l3_metered_wave_error`. The canary looked green.

What this file makes falsifiable:

  * a deployment that cannot run assist SAYS SO BEFORE THE FIRST TOKEN, naming a bounded
    cause, instead of discovering it after the money is spent;
  * a tenant OUTSIDE the cohort is untouched — legacy behaviour, and no L3 telemetry at
    all, because assist was never requested for that job;
  * the activation decision travels as an INTERNAL argument, is honoured only when it is
    exactly `True`, and cannot be supplied, forged or approximated by a request payload;
  * a bounded failure keeps its CATEGORY: operator configuration, internal wiring, and
    activation verdicts are three different answers to "who fixes this";
  * and the happy path still works — every gate here is paired with a positive control,
    because a suite that only proves things are refused cannot tell "correctly armed" from
    "broken in a direction nobody tested".

🔴 EVERY GATE HAS A MUTATION. The closing section reverts each fix, one at a time, in a
   COPY of the tree and runs the row that claims to cover it in a subprocess. A test that
   passes against the unfixed code proves nothing, and this suite exists because that
   already happened three times on this workstream. A mutation whose pattern no longer
   matches reports PATTERN-MISS and fails — a stale harness must be louder than a silent
   one, not quieter.

No network, no provider, no Redis, no database. The QC credential used below is a literal
placeholder and is never sent anywhere.
"""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python"))

import canon_lite as cl                              # noqa: E402
import canon_lite_qc_contract as qcc                 # noqa: E402
import canon_lite_qc_meter as meter                  # noqa: E402
import canon_lite_qc_runner as runner                # noqa: E402
import canon_lite_semantic_source as css             # noqa: E402
import narration_api as na                           # noqa: E402
from orchestrator import dynamic as dyn              # noqa: E402
from orchestrator import static as st                # noqa: E402
from orchestrator.context_builder import SharedContext  # noqa: E402

CANARY = "t-canary"
OUTSIDER = "t-outsider"


async def _fake_story_bible_with_semantics(topic, outline, *, is_fiction=True, style=None,
                                           language="id", manager_model=None, timeout=None,
                                           telemetry_sink=None, extra_negative=None,
                                           structured_semantic=False):
    """P0-B test double for `orchestrator.dynamic.build_story_bible` — see the identical
    double in test_canon_lite_l3_assist_stage1.py for the full rationale. This file's own
    concern is the ACTIVATION PREFLIGHT (P0-A); P0-B adds a SECOND precondition assist must
    clear (semantic authority), so a row that asserts assist ARMS now also needs this."""
    text = "1. CHARACTERS\nTest Entity (test double bible, no LLM called)."
    if not structured_semantic:
        return text
    # P0-B round 2: the envelope binds the EXACT bible response it came from, so the double
    # must name its own `text` — a source that cannot say which bible produced it is refused.
    source = css.build_semantic_source_v1(
        outline_chapters=outline, bible_text=text,
        entities=(cl.CanonEntityV1(entity_id="ent1", canonical_name="Test Entity",
                                   aliases=(), alias_source="none"),),
    )
    # THREE values under `structured_semantic`: prose, envelope, and the advisory
    # `canon_registry` sidecar (`None` here — this double has no registry to carry).
    return text, source, None

#: Not a credential. Presence is all the gate reads; the value is never resolved, compared
#: or transmitted, and this string never leaves the process.
PLACEHOLDER_KEY = "placeholder-not-a-credential"

#: Every variable the activation gate reads, all correct. A test then breaks exactly one,
#: so the reason_code it asserts is attributable to that one change and nothing else.
CLEARED_ENV = {
    cl.MODE_ENV_VAR: "assist",
    cl.ASSIST_TENANTS_ENV_VAR: CANARY,
    meter.EXTRACTOR_CONCURRENCY_ENV: "4",
    "L2B_MAX_INFLIGHT": "8",
    qcc.QC_API_KEY_ENV: PLACEHOLDER_KEY,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def metered_host():
    """Claim the metered-host role, as narration-worker's boot code does.

    Restored on the way out: the role is a module global, and a file that leaves it set
    would silently arm the host sentinel for every suite that sorts after it.
    """
    meter.reset_host_role_for_tests()
    meter.declare_host_role(meter.HOST_ROLE_NARRATION_WORKER)
    yield
    meter.reset_host_role_for_tests()


@pytest.fixture
def env(monkeypatch):
    """Install a full activation environment, minus whatever the test wants missing."""
    def apply(overrides=None, *, drop=()):
        values = dict(CLEARED_ENV)
        values.update(overrides or {})
        for name in drop:
            values.pop(name, None)
        for name in CLEARED_ENV:
            monkeypatch.delenv(name, raising=False)
        # Observability is a separate subsystem with its own Redis; keep it off so these
        # rows measure the activation path and nothing else.
        monkeypatch.delenv("NARASI_OBSERVABILITY_ENABLED", raising=False)
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return values
    return apply


class Recorder:
    """A chapter-worker stub that records what it was ACTUALLY handed.

    `canon_text` is the tell: it is populated only when assist armed and injected. Reading
    the orchestrator's intent instead would pass even if the canon never reached a worker.
    """

    def __init__(self):
        self.calls = []

    async def __call__(self, **kw):
        self.calls.append({"no": kw["no"], "canon_text": kw.get("canon_text")})
        return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m",
                **({"canon_generation_prompt_sha256_seen":
                    cl.sha256_hex(kw["canon_text"].encode("utf-8")),
                    "context_sha256_seen": cl.context_digest(kw["ctx"]),
                    "canon_sha256": kw.get("canon_sha256"),
                    "context_sha256": kw.get("context_sha256")}
                   if kw.get("canon_text") else {})}


def _outline(n=3):
    return [{"id": i + 1, "title": f"Bab {i + 1}", "summary": f"ringkasan {i + 1}",
             "word_target": 400} for i in range(n)]


def _map_chapters(monkeypatch, *, tenant_id=CANARY, n=3, **kw):
    """Drive the REAL `narrate_chapters` and return `(result, recorder)`.

    P0-B: `NARASI_STORY_BIBLE` is "1" (not "0") and `build_story_bible` is patched to the
    instant, no-network test double — assist now requires REAL semantic authority to arm,
    which only a Story Bible call can produce, and this file's rows about ACTIVATION
    (does assist arm at all) need that second precondition satisfied to observe P0-A's own
    behaviour once it is armed.
    """
    rec = Recorder()
    monkeypatch.setattr(st, "_write_chapter", rec)
    monkeypatch.setenv("NARASI_STORY_BIBLE", "1")
    monkeypatch.setattr(dyn, "build_story_bible", _fake_story_bible_with_semantics)
    # P0-B round 2: assist additionally requires FICTION (see the P0-B suite). This file
    # passes no style, and `_is_fiction_style(None)` is False — forced True so these rows
    # keep testing the ACTIVATION preflight rather than silently exercising P0-B's
    # nonfiction refusal.
    monkeypatch.setattr(st, "_is_fiction_style", lambda style: True)
    chapters = _outline(n)
    res = asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none", max_parallel=n, tenant_id=tenant_id,
        shared_context=SharedContext(topic="topik", chapters=chapters), **kw))
    return res, rec


async def _anoop(*a, **k):
    return None


def _drive_job(monkeypatch, *, tenant_id, result=None, gen=None, job_id="j-act"):
    """Run the REAL background job body and report what it decided.

    `_run_narration_job_after_parity` is entered directly, not through the §9 parity
    wrapper: the wrapper refuses an assist job whose transported snapshot does not match,
    which would stop these rows before the code they are about.

    Returns `(result, seen)` where `result` is the FINAL result dict — the one handed to
    persistence — and `seen` records the internal arguments the router was called with.

    🔴 THE FINAL DICT, NOT THE ROUTER'S. `narration_api` does `result = dict(result or {})`
       part-way through, so from that point on it mutates a COPY. The preflight's record is
       written before the copy and survives into it; the terminal seam's is written after.
       An earlier version of this helper returned the router's original object and could
       therefore see only half the outcomes — and would have reported a seam that never
       recorded anything as passing.
    """
    payload = {"ok": True, "book": "## Bab 1\nteks", "chapters": [{"no": 1}]} \
        if result is None else result
    seen = {}
    final = {}

    async def _persist(tenant, job_uuid_, res, *a, **k):
        final.clear()
        final.update(res or {})

    async def _gen(req, **kwargs):
        seen.update(kwargs)
        seen["req"] = req
        if gen is not None:
            return await gen(req, **kwargs)
        return payload

    monkeypatch.setattr(na, "generate_narration", _gen)
    monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
    monkeypatch.setattr(na, "_finalize", _anoop)
    monkeypatch.setattr(na, "_set_status", _anoop)
    monkeypatch.setattr(na, "_safe_progress", _anoop)
    monkeypatch.setattr(na, "_settle", _anoop)
    monkeypatch.setattr(na, "_refund", _anoop)
    monkeypatch.setattr(na, "_apply_v3_gates", _anoop)
    # 🔴 THIS DRIVER STUBS `_apply_v3_gates`, so F6 never records a pre-repair state.
    #    F6 is default-ON and fail-closed on a missing one — a job that skipped the
    #    detection seam has no evidence about its manuscript and must not deliver. This
    #    suite is about a different seam, so it says so explicitly rather than relying on
    #    F6 having been lenient about jobs that never ran it.
    monkeypatch.setenv("NARASI_F6_ENABLED", "0")
    monkeypatch.setattr(na, "_reconcile_checkboxes", _anoop)
    monkeypatch.setattr(na, "_persist_chapters", _persist)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))

    async def drive():
        before = set(asyncio.all_tasks())
        await na._run_narration_job_after_parity(
            body={"chapters": [{"word_target": 400}]}, job_id=job_id, job_uuid=None,
            tenant_id=tenant_id, user_id="u", total=1, meter_op=None, model="m",
            executor="narration_worker")
        survivors = (set(asyncio.all_tasks()) - before) - {asyncio.current_task()}
        if survivors:
            await asyncio.gather(*survivors, return_exceptions=True)

    asyncio.run(drive())
    assert final, "the job never reached persistence — nothing here would be measuring it"
    return final, seen


# ===========================================================================
# 1. The gate itself — one reason_code per broken thing, in a fixed order
# ===========================================================================
@pytest.mark.parametrize("broken,expected", [
    ({cl.MODE_ENV_VAR: "off"}, "assist_not_armed"),
    ({cl.MODE_ENV_VAR: "shadow"}, "assist_not_armed"),
    ({cl.ASSIST_TENANTS_ENV_VAR: "someone-else"}, "tenant_not_allowed"),
    ({cl.ASSIST_TENANTS_ENV_VAR: ""}, "tenant_not_allowed"),
    ({meter.EXTRACTOR_CONCURRENCY_ENV: ""}, "extractor_concurrency_missing"),
    ({"L2B_MAX_INFLIGHT": ""}, "inflight_policy_missing"),
    ({qcc.QC_API_KEY_ENV: "   "}, "qc_provider_api_key_missing"),
    ({qcc.QC_BASE_URL_ENV: "https://evil.example/v1/"},
     "qc_provider_route_not_ratified"),
])
def test_the_gate_names_the_first_missing_thing(metered_host, broken, expected):
    """Fixed evaluation order, so the code names the FIRST gap rather than an arbitrary one.

    A verdict that reported whichever check happened to run last would send an operator to
    fix something that was never wrong.
    """
    environ = dict(CLEARED_ENV)
    environ.update(broken)
    verdict = meter.assist_activation_ready(tenant_id=CANARY, environ=environ)
    assert verdict.passes is False
    assert verdict.reason_code == expected


def test_the_gate_passes_when_everything_is_present(metered_host):
    """The positive control. Without it every row above would also pass on a gate that
    refuses unconditionally, which is the failure mode a suite of refusals cannot see."""
    verdict = meter.assist_activation_ready(tenant_id=CANARY, environ=dict(CLEARED_ENV))
    assert verdict.passes is True
    assert verdict.reason_code == "assist_activation_ready"


def test_the_gate_refuses_off_host_however_complete_the_config_is():
    """No `metered_host` fixture: the `python` service runs this module too, and a config
    load there means a gate was skipped."""
    meter.reset_host_role_for_tests()
    verdict = meter.assist_activation_ready(tenant_id=CANARY, environ=dict(CLEARED_ENV))
    assert verdict.passes is False
    assert verdict.reason_code == "host_not_permitted"


def test_the_gate_never_returns_the_credential_or_free_text(metered_host):
    """reason_code is drawn from a fixed set. A verdict built from a value or a message is
    how bounded telemetry turns into an unbounded blob — and how a secret leaks."""
    environ = dict(CLEARED_ENV)
    environ[qcc.QC_API_KEY_ENV] = "sk-super-secret-value"
    environ[meter.EXTRACTOR_CONCURRENCY_ENV] = ""
    verdict = meter.assist_activation_ready(tenant_id=CANARY, environ=environ)
    assert "sk-super-secret" not in verdict.reason_code
    assert verdict.reason_code in (
        na.L3_CONFIG_REASON_CODES | na.L3_ACTIVATION_REASON_CODES)


# ===========================================================================
# 2. Job entry — WHO may produce a configuration failure
# ===========================================================================
def test_an_off_deployment_runs_legacy_and_records_nothing(env, monkeypatch,
                                                           metered_host):
    env({cl.MODE_ENV_VAR: "off"})
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY)
    assert "canon_lite_l3" not in result, result.get("canon_lite_l3")
    assert seen["assist_activation_ready"] is False


@pytest.mark.parametrize("tenant", [OUTSIDER, "", "T-CANARY", " t-canary-2 "])
def test_a_tenant_outside_the_allowlist_is_not_a_configuration_failure(
        env, monkeypatch, metered_host, tenant):
    """🔴 THE CORRECTION THIS ROW EXISTS FOR. An earlier draft ran the preflight whenever
       the GLOBAL mode was assist, so every tenant not in the allowlist was filed as
       `tenant_not_allowed` / `stage=config_unavailable` / `requested_mode=assist`. Assist
       was never requested for those jobs. Shipping that would have painted the whole
       non-canary fleet as misconfigured the moment the flag went on, and buried the two
       tenants that genuinely are in the cohort under the noise.

       Asserted with the QC KEY MISSING TOO: a non-cohort job must stay silent even on a
       deployment that genuinely cannot run assist, because the deployment's problem is
       not that job's problem.
    """
    env(drop=[qcc.QC_API_KEY_ENV])
    result, seen = _drive_job(monkeypatch, tenant_id=tenant)
    assert "canon_lite_l3" not in result, result.get("canon_lite_l3")
    assert seen["assist_activation_ready"] is False


def test_an_allowlisted_tenant_with_no_credential_downgrades_and_says_why(
        env, monkeypatch, metered_host):
    """The incident, inverted: the gap is named BEFORE any spend, on the result itself."""
    env(drop=[qcc.QC_API_KEY_ENV])
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY)
    l3 = result["canon_lite_l3"]
    assert l3["outcome"] == "unchecked"
    assert l3["stage"] == "config_unavailable"
    assert l3["reason_code"] == "qc_provider_api_key_missing"
    assert l3["requested_mode"] == "assist"
    assert l3["effective_mode"] == "off"
    assert l3["rounds"] == 0 and l3["chapters_repaired"] == 0
    assert seen["assist_activation_ready"] is False


def test_a_raising_preflight_is_reported_not_swallowed(env, monkeypatch, metered_host,
                                                       caplog):
    """🔴 The verdict used to be reset to None and the job downgraded in SILENCE — a
       crashed preflight looked exactly like a deployment where assist was never on. The
       code is a fixed literal: the exception's message and class are never read, because
       both are influenceable and a reason_code built from them is unbounded.

    🔴 AND THE LOG IS CHECKED, NOT ONLY THE PAYLOAD. The first version of this row asserted
       the message never reached `canon_lite_l3` and stopped there — while the handler used
       `log.exception`, which printed `RuntimeError: upstream-detail-with-a-token-abc123`
       and its traceback verbatim. Bounding the payload while the log emits the raw string
       does not bound anything; it only moves where the leak lands, and logs are the half
       that gets shipped to an aggregator.
    """
    secret = "upstream-detail-with-a-token-abc123"

    def boom(**_kw):
        raise RuntimeError(secret)

    env()
    monkeypatch.setattr(runner, "assist_activation_ready", boom)
    with caplog.at_level(logging.DEBUG):
        result, seen = _drive_job(monkeypatch, tenant_id=CANARY)
    l3 = result["canon_lite_l3"]
    assert l3["reason_code"] == "assist_preflight_error"
    assert l3["stage"] == na.L3_STAGE_ACTIVATION_ERROR
    assert l3["stage"] != na.L3_STAGE_CONFIG_UNAVAILABLE
    assert l3["requested_mode"] == "assist" and l3["effective_mode"] == "off"
    assert seen["assist_activation_ready"] is False

    payload = json.dumps(l3)
    for leak in (secret, "abc123", "RuntimeError", "Traceback"):
        assert leak not in payload, f"{leak!r} reached the outcome payload"
        assert leak not in caplog.text, f"{leak!r} reached the log"
    # The bounded facts DID make it out — otherwise the row above would also pass on a
    # handler that logs nothing at all.
    assert "assist_preflight_error" in caplog.text
    assert na.L3_STAGE_ACTIVATION_ERROR in caplog.text


# ===========================================================================
# 2b. A verdict that RETURNS but does not PARSE — malformed, not raised
# ===========================================================================
#
# 🔴 THE VERDICT THAT LIES ABOUT ITS OWN SHAPE. Everything above covers a preflight that
#    raises. This section covers one that returns cleanly with a value that is not what it
#    claims to be: `PhaseAVerdict.passes` is typed `bool` but nothing at runtime enforces
#    that, and `bool(verdict.passes)` used to trust the type annotation instead of the
#    actual value. `PhaseAVerdict` is `frozen=True, slots=True` — neither stops a caller
#    from constructing one with the wrong-typed field, which is exactly what these rows do.

def _fake_verdict(monkeypatch, verdict):
    monkeypatch.setattr(runner, "assist_activation_ready", lambda **_kw: verdict)


@pytest.mark.parametrize("passes", ["false", "False", "0", "no", 1, 1.0, [],
                                    object(), "true"])
def test_a_truthy_non_boolean_passes_never_arms_assist(env, monkeypatch, metered_host,
                                                        passes):
    """🔴 THE EXACT BUG. `bool("false")` is `True` in Python — a string that SPELLS a
       refusal is truthy. `bool([])` is `False` too, included here to prove the row is
       testing exact identity, not truthiness, in both directions: `[]` must ALSO fail to
       arm, but for the boundary's real reason (`passes is True` fails), not by accident of
       one truthiness table over another.
    """
    env()
    verdict = meter.PhaseAVerdict(passes=passes, reason_code="assist_activation_ready")
    _fake_verdict(monkeypatch, verdict)
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY)
    # `.get(...)`, deliberately, over the whole row: a subscript would KeyError on the
    # mutant (a truthy-but-armed job never reaches `_l3_record_outcome` at all, so
    # `canon_lite_l3` is absent, not merely wrong) and an uncaught KeyError reports as a
    # pytest ERROR rather than a FAILURE — which is a weaker, noisier kill signal for the
    # mutation harness below to key off of.
    assert seen["assist_activation_ready"] is False, \
        f"passes={passes!r} armed assist — bool() coercion is back"
    l3 = result.get("canon_lite_l3") or {}
    assert l3.get("reason_code") == "assist_preflight_invalid_verdict", l3
    assert l3.get("stage") == na.L3_STAGE_WIRING_ERROR, l3


def test_passes_true_with_the_wrong_code_does_not_arm_assist(env, monkeypatch,
                                                              metered_host):
    """`passes is True` alone is not enough — the exact success code must accompany it, or
    a verdict object could claim readiness while disagreeing with itself about why."""
    env()
    verdict = meter.PhaseAVerdict(passes=True, reason_code="tenant_not_allowed")
    _fake_verdict(monkeypatch, verdict)
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY)
    assert seen["assist_activation_ready"] is False
    l3 = result.get("canon_lite_l3") or {}
    assert l3.get("reason_code") == "assist_preflight_invalid_verdict", l3


@pytest.mark.parametrize("reason_code", [
    "some-unknown-code-nobody-registered",
    {"evil": "payload", "token": "abc123"},
    ["a", "list", "is", "not", "a", "reason"],
    "qc_run_id_required",          # real code — but not one the PREFLIGHT can produce
    "",
    None,
    12345,
])
def test_an_unparseable_reason_never_crashes_and_never_leaks(env, monkeypatch,
                                                              metered_host, caplog,
                                                              reason_code):
    """🔴 DICT AND LIST REPRODUCED. `reason_code in <frozenset>` raises on both — this row
       proves the job completes anyway, the bounded fallback is what gets stored, and
       nothing about the malformed value's CONTENT reaches the payload or the log.
    """
    env()
    verdict = meter.PhaseAVerdict(passes=False, reason_code=reason_code)
    _fake_verdict(monkeypatch, verdict)
    with caplog.at_level(logging.DEBUG):
        result, seen = _drive_job(monkeypatch, tenant_id=CANARY)   # must not raise

    assert seen["assist_activation_ready"] is False
    # `.get(...)`: under the M9 mutant, an unbounded reason_code is silently DROPPED by
    # `_l3_is_bounded_code` rather than replaced, so the key can be absent. A subscript
    # would turn that into an uncaught KeyError (pytest ERROR) instead of the assertion
    # failure (pytest FAILED) the mutation harness below expects to see.
    l3 = result.get("canon_lite_l3") or {}
    assert l3.get("reason_code") == "assist_preflight_invalid_verdict", l3
    assert l3.get("stage") == na.L3_STAGE_WIRING_ERROR, l3
    assert l3.get("stage") != na.L3_STAGE_CONFIG_UNAVAILABLE, l3

    payload = json.dumps(l3)
    for surface in (payload, caplog.text):
        assert "evil" not in surface and "abc123" not in surface
        assert "not a reason" not in surface


@pytest.mark.parametrize("verdict_factory", [
    lambda: meter.PhaseAVerdict(passes="false", reason_code="assist_activation_ready"),
    lambda: meter.PhaseAVerdict(passes=False, reason_code={"x": 1}),
    lambda: meter.PhaseAVerdict(passes=True, reason_code="host_not_permitted"),
])
def test_a_malformed_verdict_never_reaches_the_metered_wave(env, monkeypatch,
                                                             metered_host,
                                                             verdict_factory):
    """The other half of every row above: not only must `_assist_ready` read False, the
    seam must never be given the chance to spend money on the strength of a bad verdict."""
    env()
    mapped, _ = _map_chapters(monkeypatch, tenant_id=CANARY,
                              assist_activation_ready=True)
    waves = []

    async def _fake_wave(snapshot, canon, **kwargs):
        waves.append(canon)
        return None

    monkeypatch.setattr(runner, "maybe_run_metered_wave", _fake_wave)
    _fake_verdict(monkeypatch, verdict_factory())
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY, result=mapped)

    assert seen["assist_activation_ready"] is False
    assert waves == [], "a malformed verdict still reached the metered wave"
    assert result["canon_lite_l3"]["reason_code"] == "assist_preflight_invalid_verdict"


def test_legacy_delivery_survives_a_malformed_verdict_with_a_bounded_diagnosis(
        env, monkeypatch, metered_host):
    """The job's actual OUTPUT — not just its internal bookkeeping — must be unaffected:
    `ok` true, the manuscript delivered, and the L3 block explaining why assist did not
    run rather than being silent or absent."""
    env()
    verdict = meter.PhaseAVerdict(passes="yes-really", reason_code="assist_activation_ready")
    _fake_verdict(monkeypatch, verdict)
    result, _ = _drive_job(monkeypatch, tenant_id=CANARY,
                           result={"ok": True, "book": "## Bab 1\nteks final",
                                   "chapters": [{"no": 1}]})
    assert result["ok"] is True
    assert result["book"] == "## Bab 1\nteks final"
    l3 = result["canon_lite_l3"]
    assert l3["outcome"] == "unchecked"
    assert l3["reason_code"] == "assist_preflight_invalid_verdict"
    assert l3["requested_mode"] == "assist" and l3["effective_mode"] == "off"


# ── Unit-level coverage of the canonicaliser, independent of the job driver ────────────
@pytest.mark.parametrize("passes,code,expected", [
    (True, "assist_activation_ready", (True, None)),
    (False, "tenant_not_allowed", (False, "tenant_not_allowed")),
    (False, "qc_provider_api_key_missing", (False, "qc_provider_api_key_missing")),
    ("false", "assist_activation_ready", (False, "assist_preflight_invalid_verdict")),
    (1, "assist_activation_ready", (False, "assist_preflight_invalid_verdict")),
    (True, "tenant_not_allowed", (False, "assist_preflight_invalid_verdict")),
    (False, "not-a-real-code", (False, "assist_preflight_invalid_verdict")),
    (False, {"x": 1}, (False, "assist_preflight_invalid_verdict")),
    (False, ["x"], (False, "assist_preflight_invalid_verdict")),
    (None, None, (False, "assist_preflight_invalid_verdict")),
    (False, "qc_run_id_required", (False, "assist_preflight_invalid_verdict")),
])
def test_canonicalize_verdict_unit(passes, code, expected):
    """The pure function, isolated from the job driver — every shape the parametrised
    behavioural rows above exercise end-to-end, checked here in one place without a job."""
    verdict = meter.PhaseAVerdict(passes=passes, reason_code=code)
    assert na._l3_canonicalize_verdict(verdict) == expected


def test_canonicalize_verdict_tolerates_a_duck_typed_object_missing_attributes():
    """Not every malformed thing this could be handed is even a PhaseAVerdict — a plain
    object missing `.passes`/`.reason_code` must fail closed, not raise AttributeError."""
    assert na._l3_canonicalize_verdict(object()) == \
        (False, "assist_preflight_invalid_verdict")


# ===========================================================================
# 2c. ACCESSING the verdict raises — not merely a wrong VALUE
# ===========================================================================
#
# 🔴 `getattr(verdict, "passes", None)` DOES NOT MAKE THIS SAFE. `getattr`'s default only
#    fires on `AttributeError` — a `@property` getter that raises `RuntimeError` (or
#    anything else) propagates straight through, uncaught. Reproduced against round 5:
#
#        property passes raising      -> RuntimeError: secret-from-property
#        reason_code.__eq__ raising   -> RuntimeError: secret-from-eq
#
#    Both happened in the `else:` branch of a `try` that only wrapped the call to the
#    preflight itself, not the canonicalisation of its result — so either one could kill
#    `_run_narration_job_after_parity`, which is contracted to NEVER raise, over a verdict
#    object this process did not build and a job that never even needed assist to run.

class _RaisingPassesProperty:
    """`.passes` raises the instant it is READ. `reason_code` is a legitimate string so
    only the `passes` access is under test."""
    reason_code = "assist_activation_ready"

    @property
    def passes(self):
        raise RuntimeError("secret-from-property")


class _RaisingReasonCodeProperty:
    """The same hazard, on the other attribute. `passes` is legitimately `True` so only
    the `reason_code` access is under test."""
    passes = True

    @property
    def reason_code(self):
        raise RuntimeError("secret-from-property")


class _RaisingEqReasonCode:
    """A `reason_code` VALUE — attribute access succeeds — whose `__eq__`/`__hash__`
    raise. This is what `passes is True and code == _ASSIST_ACTIVATION_READY_CODE` used to
    invoke with NO type guard in front of it at all: any object, not only a `str`
    lookalike, reaches the `==` the moment `passes is True`."""
    def __eq__(self, other):
        raise RuntimeError("secret-from-eq")

    def __hash__(self):
        raise RuntimeError("secret-from-eq")


class _HostileStrSubclass(str):
    """A `str` SUBCLASS with the same poisoned `__eq__`/`__hash__`. `isinstance(x, str)`
    accepts this; only `type(x) is str` rejects it — this is the exact object shape the
    `isinstance` → `type(...) is str` fix exists for, exercised through the REFUSAL branch
    (`passes is False`), which already had an `isinstance` guard in round 5 that this
    would have slipped past."""
    def __eq__(self, other):
        raise RuntimeError("secret-from-eq")

    def __hash__(self):
        raise RuntimeError("secret-from-eq")


class _MalformedVerdict:
    def __init__(self, passes, reason_code):
        self.passes = passes
        self.reason_code = reason_code


_HOSTILE_VERDICTS = {
    "passes-property-raises": _RaisingPassesProperty(),
    "reason-code-property-raises": _RaisingReasonCodeProperty(),
    "reason-code-eq-raises-on-success-branch":
        _MalformedVerdict(passes=True, reason_code=_RaisingEqReasonCode()),
    "reason-code-eq-raises-on-refusal-branch":
        _MalformedVerdict(passes=False, reason_code=_RaisingEqReasonCode()),
    "hostile-str-subclass-on-refusal-branch":
        _MalformedVerdict(passes=False, reason_code=_HostileStrSubclass("tenant_not_allowed")),
}


@pytest.mark.parametrize("verdict", _HOSTILE_VERDICTS.values(), ids=_HOSTILE_VERDICTS.keys())
def test_canonicalize_verdict_is_total_even_when_reading_it_raises(verdict):
    """The pure function, isolated: every hostile shape above must return the fixed
    fallback tuple, never propagate."""
    assert na._l3_canonicalize_verdict(verdict) == \
        (False, "assist_preflight_invalid_verdict")


# 🔴 THESE TWO ARE DELIBERATELY NOT PARAMETRIZED. They are the MUTATION-KILL WITNESSES
#    for M10 (containment removed) specifically, and the choice is not stylistic:
#    empirically, of the five hostile shapes above, only these two are still crashes once
#    `type(code) is str` short-circuits BEFORE the `==`/`in` on `code` — a `code` that is
#    merely a non-str OBJECT (the `__eq__`/`__hash__` cases, str-subclass included) never
#    reaches a comparison at all if it fails the type check first, containment or not. A
#    `passes` or `reason_code` PROPERTY that raises on the `getattr` itself is different:
#    that failure happens before any type check runs, so it is containment — the
#    try/except — doing the actual work, and these are the only two rows that prove it.
def test_a_property_that_raises_on_passes_does_not_crash_canonicalize():
    assert na._l3_canonicalize_verdict(_RaisingPassesProperty()) == \
        (False, "assist_preflight_invalid_verdict")


def test_a_property_that_raises_on_reason_code_does_not_crash_canonicalize():
    assert na._l3_canonicalize_verdict(_RaisingReasonCodeProperty()) == \
        (False, "assist_preflight_invalid_verdict")


@pytest.mark.parametrize("verdict", _HOSTILE_VERDICTS.values(), ids=_HOSTILE_VERDICTS.keys())
def test_a_hostile_verdict_never_crashes_the_job_and_delivers_legacy(
        env, monkeypatch, metered_host, caplog, verdict):
    """🔴 THE JOB, NOT JUST THE FUNCTION. `_drive_job` runs the real
       `_run_narration_job_after_parity` end to end; if canonicalisation is not actually
       total, THIS is where a legacy job — one that never needed assist to complete —
       would die.
    """
    env()
    _fake_verdict(monkeypatch, verdict)
    mapped, _ = _map_chapters(monkeypatch, tenant_id=CANARY,
                              assist_activation_ready=True)
    waves = []

    async def _fake_wave(snapshot, canon, **kwargs):
        waves.append(canon)
        return None

    monkeypatch.setattr(runner, "maybe_run_metered_wave", _fake_wave)
    with caplog.at_level(logging.DEBUG):
        result, seen = _drive_job(monkeypatch, tenant_id=CANARY, result=mapped)   # must not raise

    assert result.get("ok") is True, result
    assert result.get("book"), "legacy delivery did not happen"
    assert seen["assist_activation_ready"] is False
    assert waves == [], "a hostile verdict still reached the metered wave"

    l3 = result.get("canon_lite_l3") or {}
    assert l3.get("reason_code") == "assist_preflight_invalid_verdict", l3
    assert l3.get("stage") == na.L3_STAGE_WIRING_ERROR, l3

    payload = json.dumps(l3)
    for secret in ("secret-from-property", "secret-from-eq", "RuntimeError"):
        assert secret not in payload, f"{secret!r} reached the outcome payload"
        assert secret not in caplog.text, f"{secret!r} reached the log"


def test_a_cleared_cohort_job_reaches_the_metered_wave(env, monkeypatch, metered_host):
    """🔴 THE POSITIVE TRIPWIRE. Everything above refuses something; this row is the one
       that fails if the fail-closed machinery has quietly disarmed assist for everybody.
       It drives the real chapter map to build a real canon, hands that result to the real
       job body, and asserts the terminal seam actually called the wave.
    """
    env()
    mapped, rec = _map_chapters(monkeypatch, tenant_id=CANARY,
                                assist_activation_ready=True)
    assert mapped.get("ok"), mapped
    assert all(c["canon_text"] for c in rec.calls), "assist never armed at all"

    waves = []

    async def _fake_wave(snapshot, canon, **kwargs):
        waves.append({"canon": canon, "tenant_id": kwargs.get("tenant_id")})
        return None

    monkeypatch.setattr(runner, "maybe_run_metered_wave", _fake_wave)
    result, seen = _drive_job(monkeypatch, tenant_id=CANARY, result=mapped)

    assert seen["assist_activation_ready"] is True
    assert waves, "the cleared cohort never reached the metered wave"
    assert isinstance(waves[0]["canon"], cl.CanonLiteV1), \
        "the seam ran but was handed no canon — a canon-less wave repairs nothing"


def test_a_blocked_cohort_job_never_reaches_the_wave_and_keeps_its_diagnosis(
        env, monkeypatch, metered_host):
    """The other half of the row above: with the preflight refusing, the seam must not run.

    🔴 Not only to avoid a doomed call. `_canon_lite_l3_assist_repair` records its OWN
       outcome, so letting it run overwrites `config_unavailable` /
       `qc_provider_api_key_missing` with a vague `no_manuscript` — the accurate diagnosis
       replaced by a generic one on its way to the operator, which is the whole s0di2o1g
       failure mode reproduced one layer up.
    """
    env(drop=[qcc.QC_API_KEY_ENV])
    mapped, _ = _map_chapters(monkeypatch, tenant_id=CANARY,
                              assist_activation_ready=True)
    waves = []

    async def _fake_wave(snapshot, canon, **kwargs):
        waves.append(canon)
        return None

    monkeypatch.setattr(runner, "maybe_run_metered_wave", _fake_wave)
    result, _ = _drive_job(monkeypatch, tenant_id=CANARY, result=mapped)

    assert waves == [], "a deployment that cannot meter still called the wave"
    assert result["canon_lite_l3"]["reason_code"] == "qc_provider_api_key_missing"
    assert result["canon_lite_l3"]["stage"] == "config_unavailable"


# ===========================================================================
# 3. Threading — the decision is internal, and only `True` counts
# ===========================================================================
@pytest.mark.parametrize("kwargs", [
    {},                                        # never threaded at all
    {"assist_activation_ready": None},         # threaded, but no decision
    {"assist_activation_ready": False},        # decided against
    {"assist_activation_ready": "on"},         # the old string protocol
    {"assist_activation_ready": "true"},
    {"assist_activation_ready": 1},            # truthy, and NOT the object True
    {"assist_activation_ready": "off"},
])
def test_a_decision_that_never_arrived_cannot_arm_assist(monkeypatch, kwargs):
    """🔴 FAIL CLOSED. The first draft asked whether the value was the string `"off"`, so a
       decision that was never threaded — a renamed parameter, a dropped call-site
       argument, a caller written before the preflight existed — read as CONSENT. It failed
       open in exactly the case the gate exists for: the wiring being wrong.
    """
    monkeypatch.setenv(cl.MODE_ENV_VAR, "assist")
    monkeypatch.setenv(cl.ASSIST_TENANTS_ENV_VAR, CANARY)
    res, rec = _map_chapters(monkeypatch, tenant_id=CANARY, **kwargs)
    assert res.get("ok"), res
    assert rec.calls, "no chapters ran at all — the row would pass vacuously"
    assert all(c["canon_text"] is None for c in rec.calls), \
        f"assist armed on {kwargs!r}, which is not a server decision"
    assert "canon_lite_binding" not in res


def test_exactly_true_arms_assist(monkeypatch):
    """Positive control for the row above — otherwise it would also pass if assist were
    simply broken and never armed for anyone."""
    monkeypatch.setenv(cl.MODE_ENV_VAR, "assist")
    monkeypatch.setenv(cl.ASSIST_TENANTS_ENV_VAR, CANARY)
    res, rec = _map_chapters(monkeypatch, tenant_id=CANARY,
                             assist_activation_ready=True)
    assert res.get("ok"), res
    assert all(c["canon_text"] for c in rec.calls)
    assert res.get("canon_lite_binding", {}).get("mode") == "assist"


def test_the_decision_cannot_be_supplied_by_the_user_payload(env, monkeypatch,
                                                             metered_host):
    """🔴 A REQUEST FIELD MUST NOT BE ABLE TO REACH THE DECISION. The verdict used to ride
       on a private `req` key, which put a server security decision in the same dict as the
       user-supplied body. These are the names that dict used to carry, plus the current
       parameter name, sent as ordinary payload fields on a deployment with NO credential:
       none of them may turn assist on, and none may suppress the honest report.
    """
    env(drop=[qcc.QC_API_KEY_ENV])
    forged = {
        "_assist_activation_veto": "on",
        "assist_activation_veto": "on",
        "assist_activation_ready": True,
        "canon_lite_l3": {"outcome": "clean", "stage": "delivered"},
    }
    seen = {}

    async def _gen(req, **kwargs):
        seen.update(kwargs)
        seen["req"] = req
        return {"ok": True, "book": "## Bab 1\nteks", "chapters": [{"no": 1}]}

    monkeypatch.setattr(na, "generate_narration", _gen)
    monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
    for name in ("_finalize", "_set_status", "_safe_progress", "_settle", "_refund",
                 "_apply_v3_gates", "_reconcile_checkboxes", "_persist_chapters"):
        monkeypatch.setattr(na, name, _anoop)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))

    async def drive():
        await na._run_narration_job_after_parity(
            body=dict(forged), job_id="j-forged", job_uuid=None, tenant_id=CANARY,
            user_id="u", total=1, meter_op=None, model="m", executor="narration_worker")

    asyncio.run(drive())
    # The SERVER's decision won, and it said no.
    assert seen["assist_activation_ready"] is False
    # The forged fields are inert: they are not read as the decision...
    assert seen["req"].get("assist_activation_ready") is True   # still just payload
    # ...and the honest report is the one that lands.
    result, _ = _drive_job(monkeypatch, tenant_id=CANARY)
    assert result["canon_lite_l3"]["reason_code"] == "qc_provider_api_key_missing"


def test_the_decision_is_not_carried_on_the_request_dict(monkeypatch):
    """The producer/consumer pair must not name a `req` key at all. A grep-level check, on
    purpose: the defect was structural, and a behavioural test cannot see a key that is
    written and then simply never read."""
    from orchestrator import router as rt
    for module in (na, rt, st):
        source = Path(module.__file__).read_text("utf-8")
        assert "_assist_activation_veto" not in source, module.__file__
        assert "ASSIST_ACTIVATION_VETO_KEY" not in source, module.__file__


def test_scenario_a_and_scenario_b_both_carry_the_decision(monkeypatch):
    """🔴 SCENARIO B REACHES THE CHAPTER MAP THROUGH SCENARIO A, so it is easy to thread
       one and forget the other — and forgetting is invisible, because the result is
       merely assist silently never arming for topic-to-book jobs.
    """
    from orchestrator import router as rt
    seen = []

    async def _capture(topic, chapters, **kw):
        seen.append(kw.get("assist_activation_ready"))
        return {"ok": True, "chapters": [], "book": "", "polished": False,
                "rag_used": False, "context": {}, "strategy": "narrate_chapters"}

    async def _outline_from_topic(topic, **kw):
        return {"chapters": _outline(2), "source": "test"}

    monkeypatch.setattr(rt._static, "narrate_chapters", _capture)
    monkeypatch.setattr(rt._dynamic, "outline_from_topic", _outline_from_topic)

    # Scenario A — explicit chapters.
    asyncio.run(rt.generate_narration(
        {"topic": "t", "chapters": _outline(2), "rag": "off"},
        assist_activation_ready=True))
    # Scenario B — topic only.
    asyncio.run(rt.generate_narration(
        {"topic": "t", "n_chapters": 2, "rag": "off"},
        assist_activation_ready=True))

    assert seen == [True, True], \
        f"a scenario dropped the activation decision: {seen}"


def test_a_router_caller_that_states_nothing_gets_no_decision(monkeypatch):
    """laozhang_api's video narration and eval/run.py call the router without a preflight.
    They must inherit no decision — the default is not a convenience, it is the fail-closed
    case."""
    from orchestrator import router as rt
    seen = []

    async def _capture(topic, chapters, **kw):
        seen.append(kw.get("assist_activation_ready"))
        return {"ok": True, "chapters": [], "book": "", "polished": False,
                "rag_used": False, "context": {}, "strategy": "narrate_chapters"}

    monkeypatch.setattr(rt._static, "narrate_chapters", _capture)
    asyncio.run(rt.generate_narration({"topic": "t", "chapters": _outline(2),
                                       "rag": "off"}))
    assert seen == [None]


# ===========================================================================
# 4. The runner's gate order — the credential before the provider import
# ===========================================================================
def test_a_missing_key_refuses_before_the_provider_is_imported(tmp_path):
    """Run in a SUBPROCESS, because `sys.modules` in this one is already polluted by every
    other row in the suite. The question is whether the refusal happens BEFORE the import,
    and that is only observable in a process that has not imported it yet."""
    script = f'''
import asyncio, os, sys
sys.path.insert(0, {str(REPO / "python")!r})
os.environ["NARASI_CANON_LITE_MODE"] = "assist"
os.environ["NARASI_CANON_LITE_ASSIST_TENANTS"] = "{CANARY}"
os.environ["{meter.EXTRACTOR_CONCURRENCY_ENV}"] = "4"
os.environ["L2B_MAX_INFLIGHT"] = "8"
os.environ.pop("{qcc.QC_API_KEY_ENV}", None)

import canon_lite_qc_meter as meter
import canon_lite_qc_runner as runner
import canon_lite_extractor as ext
meter.declare_host_role(meter.HOST_ROLE_NARRATION_WORKER)

token = ext.ExtractionWaveToken() if hasattr(ext, "ExtractionWaveToken") else None
try:
    asyncio.run(runner.maybe_run_metered_wave(
        object(), object(), run_id="r-1", job_uuid=None, job_external_id=None,
        wave_token=token, tenant_id="{CANARY}"))
    print("NO_RAISE")
except RuntimeError as exc:
    print("RAISED:" + str(exc))
except Exception as exc:
    print("OTHER:" + type(exc).__name__ + ":" + str(exc))
print("PROVIDER_IMPORTED:" + str("canon_lite_qc_provider" in sys.modules))
'''
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=180)
    out = proc.stdout
    assert "RAISED:qc_provider_api_key_missing" in out, (out, proc.stderr[-2000:])
    assert "PROVIDER_IMPORTED:False" in out, (out, proc.stderr[-2000:])


# ===========================================================================
# 5. The failure vocabulary — three categories, and they do not collapse
# ===========================================================================
def test_the_three_reason_code_sets_are_disjoint():
    assert not (na.L3_CONFIG_REASON_CODES & na.L3_WIRING_REASON_CODES)
    assert not (na.L3_CONFIG_REASON_CODES & na.L3_ACTIVATION_REASON_CODES)
    assert not (na.L3_WIRING_REASON_CODES & na.L3_ACTIVATION_REASON_CODES)


def test_every_bounded_code_maps_to_the_stage_of_its_own_category():
    """🔴 `config_unavailable` IS A CLAIM ABOUT THE DEPLOYMENT. It says: a variable,
       credential or route on this service is wrong, and changing it fixes the job. Only
       codes an operator can actually act on may carry it.

       `assist_preflight_error` was filed under it while sitting in the ACTIVATION set — a
       crashed preflight reported as evidence that the environment is misconfigured, which
       sends someone to audit Railway for a defect that lives in the code. The mapping is
       derived from the code's category now, in one place, so no call site can pair a code
       with a stage that contradicts it.
    """
    for code in na.L3_CONFIG_REASON_CODES:
        assert na._l3_stage_for(code) == na.L3_STAGE_CONFIG_UNAVAILABLE, code
    for code in na.L3_WIRING_REASON_CODES:
        assert na._l3_stage_for(code) == na.L3_STAGE_WIRING_ERROR, code
    for code in na.L3_ACTIVATION_REASON_CODES:
        assert na._l3_stage_for(code) == na.L3_STAGE_ACTIVATION_ERROR, code
        assert na._l3_stage_for(code) != na.L3_STAGE_CONFIG_UNAVAILABLE, code
    # The three stages are distinct strings; collapsing any two would erase the answer to
    # "who fixes this" while every assertion above still passed.
    assert len({na.L3_STAGE_CONFIG_UNAVAILABLE, na.L3_STAGE_WIRING_ERROR,
                na.L3_STAGE_ACTIVATION_ERROR}) == 3


def test_an_unknown_code_is_never_called_operator_configuration():
    """🔴 THE DEFAULT ARM IS `wiring_error`, NOT `activation_error` — corrected after
       review. A code that is not a member of ANY of the three known sets is not "an
       activation decision we haven't named yet"; it is the caller being out of sync with
       the vocabulary, which is a wiring defect no matter which subsystem produced it.
       `activation_error` is earned only by codes that are actually IN the activation set —
       asserted separately below — never worn as the fallback for the unrecognised case.
    """
    for code in ("", "something_new_nobody_registered", None, {"evil": 1}, ["x"]):
        stage = na._l3_stage_for(code)
        assert stage == na.L3_STAGE_WIRING_ERROR, (code, stage)
        assert stage != na.L3_STAGE_CONFIG_UNAVAILABLE, (code, stage)


@pytest.mark.parametrize("code", sorted(na.L3_ACTIVATION_REASON_CODES))
def test_an_activation_code_still_reaches_activation_error(code):
    """The positive control for the row above: activation_error must still be reachable
    for the codes that actually belong there, or the default-arm fix could have collapsed
    the whole category by accident."""
    assert na._l3_stage_for(code) == na.L3_STAGE_ACTIVATION_ERROR


@pytest.mark.parametrize("junk", [{"evil": "payload"}, ["a", "b"], 42, 3.14, object()])
def test_stage_for_never_raises_on_an_unhashable_or_foreign_type(junk):
    """`in <frozenset>` raises on an unhashable value instead of returning False. A
    membership check reachable from a verdict boundary must fail the check, not crash it."""
    assert na._l3_stage_for(junk) == na.L3_STAGE_WIRING_ERROR


@pytest.mark.parametrize("junk", [{"evil": "payload"}, ["a", "b"], 42, 3.14, object()])
def test_is_bounded_code_never_raises_on_an_unhashable_or_foreign_type(junk):
    assert na._l3_is_bounded_code(junk) is False


@pytest.mark.parametrize("code", sorted(na.L3_BOUNDED_REASON_CODES))
def test_is_bounded_code_accepts_every_real_code(code):
    assert na._l3_is_bounded_code(code) is True


@pytest.mark.parametrize("code", sorted(na.L3_CONFIG_REASON_CODES))
def test_a_known_config_failure_is_not_flattened(code):
    """🔴 THE FLATTENING IS THE INCIDENT. The seam turned every wave exception into
       `l3_metered_wave_error` / `stage=no_session`, which is how a missing credential
       reported as "there was simply no session to be had"."""
    classified = na._l3_bounded_failure(RuntimeError(code))
    assert classified == (code, na.L3_STAGE_CONFIG_UNAVAILABLE)


@pytest.mark.parametrize("code", sorted(na.L3_WIRING_REASON_CODES))
def test_a_wiring_defect_is_never_labelled_operator_configuration(code):
    """`qc_run_id_required` and `qc_wave_token_required` mean a CALLER omitted an argument.
    No environment variable, credential or Railway setting can produce or fix either one.
    Filing them as configuration sends an operator to audit a deployment that is fine."""
    classified = na._l3_bounded_failure(RuntimeError(code))
    assert classified is not None
    assert classified[1] == na.L3_STAGE_WIRING_ERROR
    assert classified[1] != na.L3_STAGE_CONFIG_UNAVAILABLE
    assert code not in na.L3_CONFIG_REASON_CODES


@pytest.mark.parametrize("code", sorted(na.L3_ACTIVATION_REASON_CODES))
def test_an_activation_code_cannot_be_impersonated_by_an_exception(code):
    """Activation verdicts are decisions this process made. If unrelated code could raise
    `RuntimeError("tenant_not_allowed")` and have the seam echo it, a runtime fault would
    be filed as a decision that was never taken."""
    assert na._l3_bounded_failure(RuntimeError(code)) is None


@pytest.mark.parametrize("message", [
    "boom", "", "   ", "Traceback (most recent call last)",
    "qc_provider_api_key_missing extra", "sk-live-0000-secret",
    "<script>alert(1)</script>", "qc_run_id_required: caller lost the id",
])
def test_an_unknown_message_stays_generic(message):
    """Membership is the filter, not sanitisation: the value returned always comes FROM the
    closed set, so provider- or attacker-controlled text cannot reach telemetry."""
    assert na._l3_bounded_failure(RuntimeError(message)) is None


@pytest.mark.parametrize("message", [
    "qc_run_id_required\n", "  qc_provider_api_key_missing  ", "\tinflight_policy_missing",
])
def test_surrounding_whitespace_does_not_hide_a_known_code(message):
    """Stripping is deliberate, and safe for the same reason the whole scheme is: the value
    RETURNED still comes from the closed set. Without it, an exception raised with a
    trailing newline would fall through to generic and lose its category."""
    classified = na._l3_bounded_failure(RuntimeError(message))
    assert classified is not None
    assert classified[0] == message.strip()


def _seam_outcome(env_apply, monkeypatch, raises):
    """Drive the REAL terminal seam with a wave that raises `raises`, return its L3 block.

    🔴 THE CLASSIFIER IS NOT THE SEAM. Calling `_l3_bounded_failure` directly proves only
       that the vocabulary is right; it says nothing about whether the seam still throws
       the answer away, which is precisely what it did in production. This drives the code
       path that flattened `qc_provider_api_key_missing` into `l3_metered_wave_error`.
    """
    env_apply()
    mapped, _ = _map_chapters(monkeypatch, tenant_id=CANARY,
                              assist_activation_ready=True)

    async def _fake_wave(snapshot, canon, **kwargs):
        raise raises

    monkeypatch.setattr(runner, "maybe_run_metered_wave", _fake_wave)
    result, _ = _drive_job(monkeypatch, tenant_id=CANARY, result=mapped)
    return result.get("canon_lite_l3", {})


@pytest.mark.parametrize("code", ["qc_provider_api_key_missing",
                                  "qc_provider_route_mismatch",
                                  "inflight_policy_missing_or_invalid"])
def test_the_seam_keeps_a_config_failure_legible(env, monkeypatch, metered_host, code):
    l3 = _seam_outcome(env, monkeypatch, RuntimeError(code))
    assert l3.get("stage") == na.L3_STAGE_CONFIG_UNAVAILABLE, l3
    assert l3.get("reason_code") == code, l3
    assert l3.get("outcome") == "unchecked", l3


@pytest.mark.parametrize("code", sorted(na.L3_WIRING_REASON_CODES))
def test_the_seam_files_a_wiring_defect_under_its_own_stage(env, monkeypatch,
                                                            metered_host, code):
    l3 = _seam_outcome(env, monkeypatch, RuntimeError(code))
    assert l3.get("stage") == na.L3_STAGE_WIRING_ERROR, l3
    assert l3.get("stage") != na.L3_STAGE_CONFIG_UNAVAILABLE, l3
    assert l3.get("reason_code") == code, l3


def test_the_seam_stays_generic_for_an_unrecognised_fault(env, monkeypatch,
                                                          metered_host):
    """The other direction: an engine fault must NOT acquire a bounded config code, or the
    category would stop meaning anything."""
    l3 = _seam_outcome(env, monkeypatch, RuntimeError("something-unbounded-happened"))
    assert "reason_code" not in l3, l3
    assert l3.get("stage") != na.L3_STAGE_CONFIG_UNAVAILABLE, l3
    assert "something-unbounded" not in json.dumps(l3)


def test_the_recorded_outcome_drops_a_code_outside_the_vocabulary():
    result = {}
    na._l3_record_outcome(result, outcome="unchecked", stage="config_unavailable",
                          reason_code="totally-made-up", requested_mode="banana",
                          effective_mode="assist")
    bounded = result["canon_lite_l3"]
    assert "reason_code" not in bounded
    assert "requested_mode" not in bounded
    assert bounded["effective_mode"] == "assist"
    # ...and the category claim the caller made alongside it does not survive either.
    assert bounded["stage"] != na.L3_STAGE_CONFIG_UNAVAILABLE
    assert bounded["stage"] == na.L3_STAGE_WIRING_ERROR


# ---------------------------------------------------------------------------
# 5b. The RECORDER is the enforcement point, not the helper
# ---------------------------------------------------------------------------
#
# 🔴 A HELPER THAT AGREES WITH ITSELF IS NOT AN INVARIANT. `_l3_stage_for` mapped every code
#    to the right category and `_l3_record_outcome` went on storing whatever stage the call
#    site passed next to it — so `reason_code=host_not_permitted` with
#    `stage=config_unavailable` was accepted and written out. The rows below drive the
#    RECORDER, because that is the function that decides what an operator reads.

def test_the_exact_contradiction_from_review_is_corrected():
    """The pair reported against round 3, reproduced and pinned."""
    result = {}
    na._l3_record_outcome(result, outcome="unchecked",
                          stage=na.L3_STAGE_CONFIG_UNAVAILABLE,
                          reason_code="host_not_permitted")
    bounded = result["canon_lite_l3"]
    assert bounded["reason_code"] == "host_not_permitted"
    assert bounded["stage"] == na.L3_STAGE_ACTIVATION_ERROR
    assert bounded["stage"] != na.L3_STAGE_CONFIG_UNAVAILABLE


@pytest.mark.parametrize("wrong_stage", sorted(na.L3_CATEGORY_STAGES) +
                         ["no_manuscript", "no_session", "delivered", ""])
@pytest.mark.parametrize("code", sorted(na.L3_BOUNDED_REASON_CODES))
def test_the_recorder_derives_the_stage_and_ignores_the_caller(code, wrong_stage):
    """Every bounded code, against every stage a caller could pass — including the two
    that are RIGHT for other codes, which is how a copy-pasted call site goes wrong."""
    result = {}
    na._l3_record_outcome(result, outcome="unchecked", stage=wrong_stage,
                          reason_code=code)
    bounded = result["canon_lite_l3"]
    assert bounded["reason_code"] == code
    assert bounded["stage"] == na._l3_stage_for(code), (
        f"{code!r} was stored under {bounded['stage']!r} because the caller said so")
    if code not in na.L3_CONFIG_REASON_CODES:
        assert bounded["stage"] != na.L3_STAGE_CONFIG_UNAVAILABLE, (
            f"{code!r} was filed as operator configuration")


@pytest.mark.parametrize("junk", ["totally-made-up", "", "config_unavailable",
                                  "qc_provider_api_key_missing extra", None])
def test_an_unknown_code_can_never_produce_a_configuration_claim(junk):
    """An unrecognised code is no evidence about the deployment. Dropping the code while
    keeping the caller's `config_unavailable` would leave the false report standing with
    nothing at all behind it."""
    result = {}
    na._l3_record_outcome(result, outcome="unchecked",
                          stage=na.L3_STAGE_CONFIG_UNAVAILABLE, reason_code=junk)
    bounded = result["canon_lite_l3"]
    assert "reason_code" not in bounded
    assert bounded["stage"] != na.L3_STAGE_CONFIG_UNAVAILABLE


@pytest.mark.parametrize("lifecycle", ["no_manuscript", "no_session", "delivered",
                                       "unsyncable", ""])
def test_a_lifecycle_stage_with_no_code_is_left_alone(lifecycle):
    """The recorder's original vocabulary. These say WHERE in the job the outcome was
    taken, not who is at fault, so there is no attribution to contradict — policing them
    would erase information the seam has always reported."""
    result = {}
    na._l3_record_outcome(result, outcome="unchecked", stage=lifecycle)
    assert result["canon_lite_l3"]["stage"] == lifecycle


@pytest.mark.parametrize("category", sorted(na.L3_CATEGORY_STAGES))
def test_a_category_stage_with_no_code_behind_it_is_downgraded(category):
    """A claim about who is at fault, with no bounded code as evidence. Allowed to stand,
    it would be the same false report by a shorter route."""
    result = {}
    na._l3_record_outcome(result, outcome="unchecked", stage=category)
    assert result["canon_lite_l3"]["stage"] == na.L3_STAGE_WIRING_ERROR


# ===========================================================================
# 6. MUTATION HARNESS — every fix above, reverted, must break a row above
# ===========================================================================
#
# Each mutant is applied to a COPY of the tree and the rows that claim to cover it are run
# there in a subprocess. Two distinct failures are reported:
#
#   PATTERN-MISS — the text no longer exists, so the mutation described nothing. A stale
#                  harness proves as little as a missing one and must be just as loud.
#   SURVIVED     — the mutation applied and the rows still passed. The fix is unguarded.

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

MUTANTS = [
    {
        "id": "M1-fail-open-veto",
        "why": "the fail-closed decision test reverted to the old string protocol",
        "edits": [(
            "python/orchestrator/static.py",
            "        if _cl_mode == \"assist\" and assist_activation_ready is not True:",
            "        if _cl_mode == \"assist\" and str(\n"
            "                assist_activation_ready or \"\").strip() == \"off\":",
        )],
        "nodes": ["test_a_decision_that_never_arrived_cannot_arm_assist"],
    },
    {
        "id": "M2-cohort-check-removed",
        "why": "the effective-mode narrowing dropped, so every tenant looks like the cohort",
        "edits": [(
            "python/narration_api.py",
            "            _assist_cohort = (_cl_pre.resolve_effective_mode(tenant_id=tenant_id)\n"
            "                              == _cl_pre.MODE_ASSIST)",
            "            _assist_cohort = True",
        )],
        "nodes": ["test_a_tenant_outside_the_allowlist_is_not_a_configuration_failure"],
    },
    {
        "id": "M3-preflight-exception-silent",
        "why": "a raising preflight goes back to being swallowed without a reason_code",
        "edits": [(
            "python/narration_api.py",
            "            _assist_ready = False\n"
            "            _assist_reason = L3_REASON_PREFLIGHT_ERROR",
            "            _assist_ready = False\n"
            "            _assist_reason = None",
        )],
        "nodes": ["test_a_raising_preflight_is_reported_not_swallowed"],
    },
    {
        "id": "M4-scenario-b-drops-the-decision",
        "why": "topic-to-book stops relaying the decision to the chapter map",
        "edits": [(
            "python/orchestrator/router.py",
            "        # Scenario B reaches the chapter map THROUGH scenario A, so the decision has to be\n"
            "        # relayed here too. Omitting it would leave every topic-to-book job with assist\n"
            "        # silently disarmed — a fail-closed bug, but still a bug, and an invisible one.\n"
            "        assist_activation_ready=assist_activation_ready,\n",
            "",
        )],
        "nodes": ["test_scenario_a_and_scenario_b_both_carry_the_decision"],
    },
    {
        "id": "M5-key-check-after-provider-import",
        "why": "the credential check moves back below the provider import",
        "edits": [(
            "python/canon_lite_qc_runner.py",
            "        raise RuntimeError(\"qc_provider_api_key_missing\")\n"
            "\n"
            "    import canon_lite_extractor as _ext\n"
            "    import canon_lite_qc_provider as _qc          # LAZY — §6.0a, and only once configured\n",
            "        raise RuntimeError(\"qc_provider_api_key_missing\")\n",
        ), (
            "python/canon_lite_qc_runner.py",
            "    env = os.environ if environ is None else environ",
            "    import canon_lite_extractor as _ext\n"
            "    import canon_lite_qc_provider as _qc\n"
            "    env = os.environ if environ is None else environ",
        )],
        "nodes": ["test_a_missing_key_refuses_before_the_provider_is_imported"],
    },
    {
        "id": "M6-seam-flattens-config-error",
        "why": "the terminal seam goes back to flattening every wave exception",
        "edits": [(
            "python/narration_api.py",
            "        _known = _l3_bounded_failure(_wave_exc)",
            "        _known = None",
        )],
        # 🔴 THE BEHAVIOURAL ROWS, NOT THE CLASSIFIER ROWS. The first version of this
        #    mutant SURVIVED: it pointed at the rows that call `_l3_bounded_failure`
        #    directly, and those keep passing while the seam throws the answer away —
        #    exactly the "test passes over the bug it claims to close" pattern this
        #    workstream hit three times already.
        "nodes": ["test_the_seam_keeps_a_config_failure_legible",
                  "test_the_seam_files_a_wiring_defect_under_its_own_stage"],
    },
    {
        "id": "M7-preflight-traceback-in-the-log",
        "why": ("the preflight handler goes back to log.exception, which prints the raw "
                "exception class and message the reason_code refuses to carry"),
        "edits": [(
            "python/narration_api.py",
            "            log.warning(\n"
            "                \"canon lite: assist preflight raised — job runs legacy \"",
            "            log.exception(\n"
            "                \"canon lite: assist preflight raised — job runs legacy \"",
        )],
        "nodes": ["test_a_raising_preflight_is_reported_not_swallowed"],
    },
    {
        "id": "M8-recorder-stores-the-callers-stage",
        "why": ("the recorder goes back to storing whatever stage the call site passed, so "
                "a code and a contradicting category can be written out together again"),
        "edits": [(
            "python/narration_api.py",
            "        \"stage\": _l3_enforced_stage(stage, reason_code),",
            "        \"stage\": stage,",
        )],
        "nodes": ["test_the_exact_contradiction_from_review_is_corrected",
                  "test_the_recorder_derives_the_stage_and_ignores_the_caller",
                  "test_an_unknown_code_can_never_produce_a_configuration_claim"],
    },
    {
        "id": "M9-verdict-coercion-and-raw-leak",
        "why": ("the preflight block goes back to bool(verdict.passes) and reads "
                "verdict.reason_code raw — the exact fail-open + unguarded-membership "
                "pair found in review of round 4"),
        "edits": [(
            "python/narration_api.py",
            "        else:\n"
            "            # 🔴 CANONICALISED BEFORE ANYTHING ELSE TOUCHES IT — no line between the call\n"
            "            #    and this one. `_verdict.passes`/`_verdict.reason_code` must never reach a\n"
            "            #    log statement, a membership check, or an arm/disarm decision in their raw\n"
            "            #    form: `bool(_verdict.passes)` used to arm assist on any truthy `passes`\n"
            "            #    (a string `\"false\"` included), and an unguarded `reason_code in <frozenset>`\n"
            "            #    raises outright on a dict or a list. See `_l3_canonicalize_verdict`.\n"
            "            _assist_ready, _assist_reason = _l3_canonicalize_verdict(_verdict)",
            "        else:\n"
            "            _assist_ready = bool(_verdict.passes)\n"
            "            _assist_reason = None if _verdict.passes else _verdict.reason_code",
        )],
        "nodes": ["test_a_truthy_non_boolean_passes_never_arms_assist",
                  "test_an_unparseable_reason_never_crashes_and_never_leaks"],
    },
    {
        "id": "M10-canonicalize-loses-its-containment",
        "why": ("the try/except wrapping _l3_canonicalize_verdict's body is removed, so a "
                "verdict whose `.passes`/`.reason_code` raise on ACCESS (a property, not "
                "merely a bad value) propagates out and can kill a legacy job"),
        "edits": [(
            "python/narration_api.py",
            "    try:\n"
            "        passes = getattr(verdict, \"passes\", None)\n"
            "        code = getattr(verdict, \"reason_code\", None)\n"
            "\n"
            "        if (passes is True and type(code) is str\n"
            "                and code == _ASSIST_ACTIVATION_READY_CODE):\n"
            "            return True, None\n"
            "        if (passes is False and type(code) is str\n"
            "                and code in _PREFLIGHT_REFUSAL_CODES):\n"
            "            return False, code\n"
            "    except Exception:  # noqa: BLE001 — see docstring: this function may not raise, ever\n"
            "        pass\n"
            "    return False, L3_REASON_PREFLIGHT_INVALID_VERDICT",
            "    passes = getattr(verdict, \"passes\", None)\n"
            "    code = getattr(verdict, \"reason_code\", None)\n"
            "\n"
            "    if (passes is True and type(code) is str\n"
            "            and code == _ASSIST_ACTIVATION_READY_CODE):\n"
            "        return True, None\n"
            "    if (passes is False and type(code) is str\n"
            "            and code in _PREFLIGHT_REFUSAL_CODES):\n"
            "        return False, code\n"
            "    return False, L3_REASON_PREFLIGHT_INVALID_VERDICT",
        )],
        # 🔴 ONLY THESE TWO, NOT ALL FIVE HOSTILE-VERDICT ROWS. Verified empirically before
        #    wiring this in: with `type(code) is str` still in place (this mutant does not
        #    touch it), the three `__eq__`/`__hash__`-raising shapes — including the
        #    str-SUBCLASS one — never reach a comparison at all, because the type check
        #    short-circuits first. Listing them here would claim a kill this mutant cannot
        #    produce, which is the false-witness failure mode this suite exists to avoid.
        "nodes": ["test_a_property_that_raises_on_passes_does_not_crash_canonicalize",
                  "test_a_property_that_raises_on_reason_code_does_not_crash_canonicalize"],
    },
]


def _build_mutant_tree(root, edits):
    shutil.copytree(REPO / "python", root / "python", ignore=_IGNORE)
    (root / "tests" / "python").mkdir(parents=True)
    for name in ("conftest.py", Path(__file__).name):
        shutil.copy2(REPO / "tests" / "python" / name, root / "tests" / "python" / name)
    for rel, old, new in edits:
        target = root / rel
        text = target.read_text("utf-8")
        found = text.count(old)
        assert found == 1, (
            f"PATTERN-MISS in {rel}: expected exactly 1 occurrence of the pre-mutation "
            f"text, found {found}. The code moved and this mutation now describes "
            f"nothing — the harness is stale, not the tree.")
        target.write_text(text.replace(old, new, 1), "utf-8")


@pytest.mark.parametrize("mutant", MUTANTS, ids=[m["id"] for m in MUTANTS])
def test_the_mutation_is_killed(tmp_path, mutant):
    root = tmp_path / "tree"
    root.mkdir()
    _build_mutant_tree(root, mutant["edits"])

    this_file = Path(__file__).name
    nodes = [f"tests/python/{this_file}::{n}" for n in mutant["nodes"]]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", *nodes],
        cwd=root, capture_output=True, text=True, timeout=600)
    assert proc.returncode != 0, (
        f"SURVIVED {mutant['id']} — {mutant['why']}. The mutation applied cleanly and "
        f"{', '.join(mutant['nodes'])} still passed, so the fix is unguarded.\n"
        f"{proc.stdout[-3000:]}")
    # 🔴 A NON-ZERO EXIT IS NOT A KILL. A syntax error from a clumsy mutation, a collection
    #    failure, or a node id that no longer resolves all exit non-zero while proving
    #    nothing about the guard. Require that pytest actually RAN the rows and that they
    #    failed as assertions.
    assert " failed" in proc.stdout, (
        f"{mutant['id']} exited non-zero WITHOUT a test failure — the mutant broke "
        f"collection rather than being caught:\n{proc.stdout[-3000:]}\n"
        f"{proc.stderr[-2000:]}")
    assert "error" not in proc.stdout.splitlines()[-1].lower(), (
        f"{mutant['id']} ended in errors, not failures:\n{proc.stdout[-2000:]}")


# ===========================================================================
# F1 (BRIEF-FOR-CODEX-2026-08-14-POST-CANARY-V9.md) — driven through the REAL
# `_run_narration_job_after_parity`, not just the standalone `scrub_and_verify_
# generation_leak` function. An adversarial audit of F1 found the call-site wiring
# had ZERO integration coverage: deleting the whole 15-line block from this
# function left the full suite green. These two tests close that gap and, in the
# same motion, prove the HIGH-severity mode-gate fix (F1 must be assist-only —
# shadow's own canon can share an id with a bracket that coincidentally appears in
# prose shadow never injected, and shadow's one promise is to never change what a
# user receives).
# ===========================================================================

def _leaking_canon():
    outline = _outline(1)
    cfg = cl.build_job_config_snapshot(outline_chapters=outline, target_language="id",
                                       narration_style="kdrama_serial")
    return cl.build_canon_lite_v1(outline_chapters=outline, job_config=cfg, anchors=[
        cl.CanonAnchorV1("anc1", "time", "five years"),
    ])


def test_f1_blocks_delivery_through_the_real_call_site_when_assist_leaks_a_marker(
        monkeypatch, env, metered_host):
    """`[anc1]` is a well-formed match for a bound id, so `scrub_bound_markers` alone
    would clean it up and delivery would correctly proceed (verified separately: the
    PRIMARY mechanism works end-to-end through this same real call site). To observe
    the DEFENSE-IN-DEPTH half — detect blocking delivery when scrub could not do its
    job — neuter `scrub_bound_markers` to a no-op here, exactly as the standalone
    seam-function tests already do; `detect_bound_markers` stays real."""
    env()  # mode=assist, tenant=CANARY armed
    canon = _leaking_canon()
    finalize_calls = []
    monkeypatch.setattr(cl, "scrub_bound_markers", lambda text, _c: (text, 0))

    async def _gen(req, **kwargs):
        return {"ok": True, "book": "## Bab 1\nechoed [anc1] into prose\n",
                "chapters": [{"no": 1, "content": "echoed [anc1] into prose"}],
                "_canon_lite_canon": canon, "_canon_lite_canon_status": "present"}

    async def _finalize_spy(job_id, job_uuid, tenant_id, *, status, result, error):
        finalize_calls.append({"status": status, "error": error})

    monkeypatch.setattr(na, "generate_narration", _gen)
    monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
    monkeypatch.setattr(na, "_finalize", _finalize_spy)
    # F6 is default-ON and fail-closed on a job with no pre-repair state; `_apply_v3_gates`
    # is stubbed out below, so this job never records one. This suite is about a different
    # seam, not F6.
    monkeypatch.setenv("NARASI_F6_ENABLED", "0")
    for name in ("_set_status", "_safe_progress", "_settle", "_refund",
                 "_apply_v3_gates", "_reconcile_checkboxes", "_persist_chapters"):
        monkeypatch.setattr(na, name, _anoop)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))

    async def drive():
        await na._run_narration_job_after_parity(
            body={"chapters": [{"word_target": 400}]}, job_id="j-f1-leak", job_uuid=None,
            tenant_id=CANARY, user_id="u", total=1, meter_op=None, model="m",
            executor="narration_worker")

    asyncio.run(drive())
    assert finalize_calls, "the real call site never reached _finalize at all"
    assert finalize_calls[-1]["status"] == na._STATUS_FAILED
    assert finalize_calls[-1]["error"] == "internal_marker_leak"


def test_f1_scrubs_the_leak_cleanly_and_delivers_normally_through_the_real_call_site(
        monkeypatch, env, metered_host):
    """The PRIMARY mechanism, unmodified — no monkeypatching of scrub — driven
    through the real call site. `[anc1]` is well-formed, so `scrub_bound_markers`
    removes it and delivery proceeds as `done`, never reaching the fail-closed
    branch at all. This is the common case F1 is FOR: catching the leak so quietly
    the customer never sees a failure, not just refusing to ship a leaking book."""
    env()  # mode=assist, tenant=CANARY armed
    canon = _leaking_canon()
    finalize_calls = []

    async def _gen(req, **kwargs):
        return {"ok": True, "book": "## Bab 1\nechoed [anc1] into prose\n",
                "chapters": [{"no": 1, "content": "echoed [anc1] into prose"}],
                "_canon_lite_canon": canon, "_canon_lite_canon_status": "present"}

    async def _finalize_spy(job_id, job_uuid, tenant_id, *, status, result, error):
        finalize_calls.append({"status": status, "error": error, "result": result})

    monkeypatch.setattr(na, "generate_narration", _gen)
    monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
    monkeypatch.setattr(na, "_finalize", _finalize_spy)
    # F6 is default-ON and fail-closed on a job with no pre-repair state; `_apply_v3_gates`
    # is stubbed out below, so this job never records one. This suite is about a different
    # seam, not F6.
    monkeypatch.setenv("NARASI_F6_ENABLED", "0")
    for name in ("_set_status", "_safe_progress", "_settle", "_refund",
                 "_apply_v3_gates", "_reconcile_checkboxes", "_persist_chapters"):
        monkeypatch.setattr(na, name, _anoop)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))

    async def drive():
        await na._run_narration_job_after_parity(
            body={"chapters": [{"word_target": 400}]}, job_id="j-f1-clean", job_uuid=None,
            tenant_id=CANARY, user_id="u", total=1, meter_op=None, model="m",
            executor="narration_worker")

    asyncio.run(drive())
    assert finalize_calls
    assert finalize_calls[-1]["status"] == na._STATUS_DONE
    assert finalize_calls[-1]["error"] is None
    assert "[anc1]" not in finalize_calls[-1]["result"].get("markdown", "")


def test_f1_does_not_block_shadow_mode_on_the_same_colliding_bracket(
        monkeypatch, env, metered_host):
    """Mirror of the test above, mode=shadow instead of assist: the SAME [anc1]
    bracket and the SAME canon (which shadow's own build could equally produce)
    must NOT block delivery — shadow never injects a canon into a chapter prompt,
    so a matching bracket is coincidence, not a leak."""
    env({cl.MODE_ENV_VAR: "shadow"})
    canon = _leaking_canon()
    finalize_calls = []

    async def _gen(req, **kwargs):
        return {"ok": True, "book": "## Bab 1\ncoincidental [anc1] text\n",
                "chapters": [{"no": 1, "content": "coincidental [anc1] text"}],
                "_canon_lite_canon": canon, "_canon_lite_canon_status": "present"}

    async def _finalize_spy(job_id, job_uuid, tenant_id, *, status, result, error):
        finalize_calls.append({"status": status, "error": error})

    monkeypatch.setattr(na, "generate_narration", _gen)
    monkeypatch.setattr(na, "_cancel_watcher", lambda *a, **k: asyncio.sleep(3600))
    monkeypatch.setattr(na, "_finalize", _finalize_spy)
    # F6 is default-ON and fail-closed on a job with no pre-repair state; `_apply_v3_gates`
    # is stubbed out below, so this job never records one. This suite is about a different
    # seam, not F6.
    monkeypatch.setenv("NARASI_F6_ENABLED", "0")
    for name in ("_set_status", "_safe_progress", "_settle", "_refund",
                 "_apply_v3_gates", "_reconcile_checkboxes", "_persist_chapters"):
        monkeypatch.setattr(na, name, _anoop)
    monkeypatch.setattr(na, "credits_lib", types.SimpleNamespace(touch_hold=_anoop))
    monkeypatch.setattr(na, "db", types.SimpleNamespace(
        get_known_bad_claims=_anoop, get_known_good_claims=_anoop, log_usage=_anoop,
        checkpoint_narasi_meter=_anoop))

    async def drive():
        await na._run_narration_job_after_parity(
            body={"chapters": [{"word_target": 400}]}, job_id="j-f1-shadow", job_uuid=None,
            tenant_id=CANARY, user_id="u", total=1, meter_op=None, model="m",
            executor="narration_worker")

    asyncio.run(drive())
    assert finalize_calls, "the real call site never reached _finalize at all"
    assert finalize_calls[-1]["error"] != "internal_marker_leak", (
        "shadow mode must never be blocked by F1 — it never injected the canon "
        "that could leak in the first place")


def test_the_mutation_harness_itself_can_fail(tmp_path):
    """The control for the control. A harness that reports every mutant as killed because
    the subprocess errors on import, or because pytest never collected anything, would look
    identical to a working one — so drive it with a NO-OP mutation and require a PASS."""
    root = tmp_path / "tree"
    root.mkdir()
    _build_mutant_tree(root, [])
    this_file = Path(__file__).name
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         f"tests/python/{this_file}::test_the_three_reason_code_sets_are_disjoint"],
        cwd=root, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        "the unmutated copy of the tree does not pass, so every SURVIVED/killed verdict "
        f"from this harness is meaningless:\n{proc.stdout[-3000:]}\n{proc.stderr[-2000:]}")
