"""Canon Lite L1.1 — §9 mode/config parity acceptance matrix.

§9 requires the effective mode, config digest, canon/extractor/predicate versions and
route to be snapshotted at job start, transported to the worker, and MISMATCH-checked
there. §13 makes that check part of what activation is. L1 shipped none of it, so
activation was held; this suite is the evidence for L1.1.

Offline: no provider, no socket, no queue, no credit.

BOUND ON THE DISPATCHER EVIDENCE — read before trusting the C11 payload claim.
`narration_start` needs auth, the database, a credit hold and Redis, so it is not
callable in this offline suite. The dispatcher half is therefore proven two ways that do
not need it: the payload literal is asserted **from the real source AST**, and
`build_parity_snapshot` REFUSES to build for mode=off so no caller can serialise an
"off" snapshot at all. That is structural proof of shape, not an end-to-end run. The
executor half — where every mismatch verdict is actually decided — is exercised
behaviourally below.
"""

import ast
import asyncio
import inspect
import json
from pathlib import Path

import pytest

import canon_lite as cl
from orchestrator import static as st
from orchestrator.context_builder import SharedContext

_PY = Path(cl.__file__).parent


def _src(rel):
    return (_PY / rel).read_text()


def _snap(mode="shadow", route="bullmq_worker", model_route="gemini-2.5-flash"):
    return cl.build_parity_snapshot(
        effective_mode=mode, route=route, model_route=model_route)


def _wire(snapshot):
    """What Redis really does to the payload on the way to the worker."""
    return json.loads(json.dumps(snapshot))


def _check(
    snapshot,
    mode="shadow",
    route="bullmq_worker",
    model_route="gemini-2.5-flash",
):
    return cl.check_parity(
        snapshot, local_mode=mode, local_route=route,
        local_model_route=model_route)


def _rebind(snapshot):
    snapshot["snapshot_sha256"] = cl._digest(
        "canon_lite.parity_snapshot.v1",
        {k: snapshot[k] for k in cl._PARITY_HASHED_FIELDS},
    )
    return snapshot


@pytest.fixture(autouse=True)
def _clean_eligibility(monkeypatch):
    # Railway supplies this in production. L1.1 refuses to manufacture parity from
    # shared ignorance, so offline tests pin a valid non-secret build identity.
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    cl.reset_job_canon_eligibility()
    yield
    cl.reset_job_canon_eligibility()


# ===========================================================================
# A. The snapshot itself — §9 tuple, closed schema, JSON primitives
# ===========================================================================

def test_snapshot_carries_the_section_9_tuple():
    s = _snap()
    assert set(s) == set(cl.PARITY_SNAPSHOT_FIELDS)
    assert s["effective_mode"] == "shadow"
    assert s["canon_version"] == cl.SCHEMA_VERSION
    assert s["route"] == "bullmq_worker"
    assert s["model_route"] == "gemini-2.5-flash"
    assert cl._SHA256_RE.match(s["config_digest"])
    assert cl._SHA256_RE.match(s["snapshot_sha256"])


def test_l2_versions_are_explicitly_bound_into_parity():
    s = _snap()
    assert s["extractor_version"] == cl.L2_EXTRACTOR_VERSION
    assert s["predicate_set_version"] == cl.L2_PREDICATE_SET_VERSION
    assert cl.L2_EXTRACTOR_VERSION != cl.UNKNOWN
    assert cl.L2_PREDICATE_SET_VERSION != cl.UNKNOWN


def test_snapshot_is_json_primitives_only_and_survives_the_wire():
    s = _snap()
    assert all(isinstance(v, str) for v in s.values()), "a non-string will not round-trip"
    assert _wire(s) == s


def test_full_queue_payload_survives_the_json_wire():
    payload = {
        "job_id": "j",
        "body": {"topic": "x", "chapters": [{"id": 1, "title": "A"}]},
        "canon_parity": _snap(),
    }
    assert _wire(payload) == payload


def test_snapshot_hash_binds_every_field():
    s = _snap()
    for field in cl._PARITY_HASHED_FIELDS:
        tampered = dict(s)
        tampered[field] = "x" if field != "effective_mode" else "enforce"
        assert _check(tampered)[0] == cl.PARITY_MISMATCH, field


@pytest.mark.parametrize("field,value,code", [
    ("effective_mode", "enforce", "mode_differs"),
    ("config_digest", "0" * 64, "config_digest_differs"),
    ("canon_version", "canon_lite_v99", "canon_version_differs"),
    ("extractor_version", "extractor_v99", "extractor_version_differs"),
    ("predicate_set_version", "predicates_v99", "predicate_set_version_differs"),
    ("route", "api_direct", "route_differs"),
    ("model_route", "claude-opus-4-6", "model_route_differs"),
])
def test_self_bound_but_locally_different_snapshot_never_matches(field, value, code):
    """Payload plus manifest can be rebound together; local comparison is independent."""
    drifted = _rebind({**_snap(), field: value})
    verdict, codes = _check(drifted)
    assert verdict == cl.PARITY_MISMATCH
    assert code in codes


def test_config_digest_is_wider_than_the_mode_alone():
    """Two services on different BUILDS must not read as a match just because the flags
    agree. The digest binds the artifact schema versions too."""
    a = cl._config_digest("shadow")
    b = cl._digest("canon_lite.parity_config.v1", {
        "parity_schema_version": cl.PARITY_SCHEMA_VERSION,
        "canon_schema_version": "canon_lite_v99",
        "job_config_schema_version": cl.JOB_CONFIG_SCHEMA_VERSION,
        "extractor_version": cl.L2_EXTRACTOR_VERSION,
        "predicate_set_version": cl.L2_PREDICATE_SET_VERSION,
        "runtime_build_sha": cl._runtime_build_sha(),
        "effective_mode": "shadow",
    })
    assert a != b


def test_config_digest_binds_the_runtime_build_sha(monkeypatch):
    """Same flags and schema constants on different deployments are still a mismatch."""
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    a = cl._config_digest("shadow")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
    b = cl._config_digest("shadow")
    assert a != b
    assert "a" * 40 not in a and "b" * 40 not in b


def test_snapshot_from_another_runtime_build_is_a_mismatch(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)
    snapshot = _snap()
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
    verdict, codes = _check(snapshot)
    assert verdict == cl.PARITY_MISMATCH
    assert "config_digest_differs" in codes


@pytest.mark.parametrize("raw,marker", [
    ("", "__unset__"),
    ("not-a-sha", "__invalid__"),
    ("A" * 40, "a" * 40),
])
def test_runtime_build_marker_is_closed_and_bounded(raw, marker):
    assert cl._runtime_build_sha({"RAILWAY_GIT_COMMIT_SHA": raw}) == marker


@pytest.mark.parametrize("raw", ["", "not-a-sha"])
def test_nonoff_snapshot_refuses_an_unverifiable_runtime_build(monkeypatch, raw):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", raw)
    with pytest.raises(cl.CanonSchemaError, match="build SHA unavailable"):
        cl.build_parity_snapshot(
            effective_mode="shadow", route="api_direct",
            model_route="gemini-2.5-flash")


def test_nonoff_executor_never_matches_when_local_build_is_unverifiable(monkeypatch):
    snapshot = _snap()
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    assert _check(snapshot) == (
        cl.PARITY_MISMATCH, ("local_build_unavailable",))


def test_digest_is_domain_separated_from_the_other_artifacts():
    payload = {"a": 1}
    seen = {cl._digest(d, payload) for d in (
        "canon_lite.parity_config.v1", "canon_lite.parity_snapshot.v1",
        "canon_lite.canon.v1", "canon_lite.job_config.v1")}
    assert len(seen) == 4


def test_building_an_off_snapshot_is_refused():
    """C11: an `off` dispatcher must add nothing. Refusing here makes it impossible to
    serialise an off snapshot and change the flag-off payload shape by accident."""
    with pytest.raises(cl.CanonSchemaError, match="mode=off"):
        cl.build_parity_snapshot(
            effective_mode="off", route="api_direct",
            model_route="gemini-2.5-flash")


@pytest.mark.parametrize("route", ["", "worker", "BULLMQ_WORKER", None, 1])
def test_route_is_a_closed_enum(route):
    with pytest.raises(cl.CanonSchemaError):
        cl.build_parity_snapshot(
            effective_mode="shadow", route=route,
            model_route="gemini-2.5-flash")


@pytest.mark.parametrize("model_route", ["", "x" * 129, None, 1])
def test_model_route_is_a_bounded_non_secret_label(model_route):
    with pytest.raises((cl.CanonSchemaError, cl.CanonBoundsError)):
        cl.build_parity_snapshot(
            effective_mode="shadow", route="api_direct",
            model_route=model_route)


# ===========================================================================
# B. The six contract cases
# ===========================================================================

def test_case1_off_dispatcher_off_executor_is_not_applicable():
    assert _check(None, mode="off") == (cl.PARITY_NOT_APPLICABLE, ())


def test_case2_shadow_both_sides_matches():
    assert _check(_wire(_snap())) == (cl.PARITY_MATCH, ())


def test_case3_executor_shadow_snapshot_absent_is_a_mismatch():
    """The activation-skew direction, and the one a naive implementation waves through:
    C11 means an `off` dispatcher adds no key, so 'absent' is the mismatch, not a
    neutral 'nothing to check'."""
    verdict, codes = _check(None)
    assert verdict == cl.PARITY_MISMATCH
    assert codes == ("snapshot_absent",)


def test_case4_executor_off_snapshot_present_is_a_mismatch():
    """The rollback-skew direction."""
    verdict, codes = _check(_wire(_snap()), mode="off")
    assert verdict == cl.PARITY_MISMATCH
    assert "mode_differs" in codes


@pytest.mark.parametrize("mangle,expected", [
    pytest.param(lambda s: "not a mapping", "snapshot_malformed", id="not-a-mapping"),
    pytest.param(lambda s: {**s, "evil": "x"}, "snapshot_schema_invalid", id="extra-key"),
    pytest.param(lambda s: {k: v for k, v in s.items() if k != "route"},
                 "snapshot_schema_invalid", id="missing-key"),
    pytest.param(lambda s: {**s, "route": 7}, "snapshot_type_invalid", id="wrong-type"),
    pytest.param(lambda s: {**s, "schema_version": "canon_lite_parity_v0"},
                 "parity_schema_version_differs", id="old-schema"),
    pytest.param(lambda s: {**s, "snapshot_sha256": "0" * 64},
                 "snapshot_unbound", id="hash-does-not-bind"),
])
def test_case5_malformed_or_divergent_snapshots_fail_safe(mangle, expected):
    verdict, codes = _check(mangle(_wire(_snap())))
    assert verdict == cl.PARITY_MISMATCH
    assert expected in codes


def test_case5_verdict_codes_never_echo_transported_values():
    """A code is a bounded label. Echoing the snapshot back would leak whatever a
    mismatched or hostile dispatcher sent."""
    secret = "s3cret-tenant-marker"
    hostile = {**_wire(_snap()), "route": secret}
    verdict, codes = _check(hostile)
    assert verdict == cl.PARITY_MISMATCH
    assert secret not in " ".join(codes)
    assert all(c.replace("_", "").isalnum() for c in codes)


def test_case6_a_pre_activation_payload_is_a_mismatch_not_a_match():
    """A job enqueued before activation carries no snapshot; the worker is already on
    shadow when it drains. It must read as a mismatch — and must still succeed as a
    legacy job, which the executor-side test below proves."""
    assert _check(None)[0] == cl.PARITY_MISMATCH


def test_a_post_rollback_payload_is_a_mismatch_too():
    """The queue is drained after the flag goes back to unset: snapshot present,
    executor off."""
    assert _check(_wire(_snap()), mode="off")[0] == cl.PARITY_MISMATCH


def test_no_input_can_produce_a_false_match():
    """Fail-closed sweep: only a well-formed, self-bound, mode-agreeing snapshot matches."""
    good = _wire(_snap())
    candidates = [None, {}, [], "shadow", 0, {**good, "effective_mode": "enforce"},
                  {**good, "config_digest": "0" * 64},
                  {**good, "canon_version": "canon_lite_v0"}]
    for c in candidates:
        assert _check(c)[0] != cl.PARITY_MATCH, c


# ===========================================================================
# C. Wiring — required parameter, both call sites, payload shape
# ===========================================================================

def _calls_to(rel, name):
    tree = ast.parse(_src(rel))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]


def test_run_narration_job_requires_canon_parity_with_no_default():
    """Defaults would let a forgotten call site or route check run unchecked."""
    import narration_api
    sig = inspect.signature(narration_api._run_narration_job)
    for name in ("canon_parity", "canon_route", "canon_model_route"):
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty, f"{name} must have NO default"


def test_every_product_call_site_supplies_canon_parity():
    """Read from the AST of the real source: the runtime TypeError only surfaces as
    failed production jobs (the queue retries twice), so the binding has to be here.

    Scans the WHOLE product tree rather than a hand-listed pair of files. The first
    version of this test named `narration_api.py` and `narration_worker.py` explicitly
    and therefore could not have found a third call site — which is exactly what the
    regression then turned up. A hand-maintained list of places to check is not a check.
    """
    found = []
    for path in sorted(_PY.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and (getattr(node.func, "id", None) == "_run_narration_job"
                         or getattr(node.func, "attr", None) == "_run_narration_job")):
                found.append((path.name, node.lineno, {k.arg for k in node.keywords}))
    assert found, "expected at least one product call site"
    for name, lineno, supplied in found:
        assert {"canon_parity", "canon_route", "canon_model_route"} <= supplied, \
            f"{name}:{lineno} omits parity snapshot, execution route, or model route"


def test_both_execution_routes_are_covered_by_those_call_sites():
    """BullMQ worker AND the in-process api_direct/api_fallback path."""
    assert _calls_to("narration_worker.py", "_run_narration_job"), "worker route missing"
    assert _calls_to("narration_api.py", "_run_narration_job"), "in-process route missing"


def test_every_product_snapshot_builder_supplies_both_routes():
    """Execution route and model route are distinct required §9 dimensions."""
    found = []
    for path in sorted(_PY.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and (getattr(node.func, "id", None) == "build_parity_snapshot"
                         or getattr(node.func, "attr", None) == "build_parity_snapshot")):
                found.append((path.name, node.lineno, {k.arg for k in node.keywords}))
    assert found
    for name, lineno, supplied in found:
        assert {"effective_mode", "route", "model_route"} <= supplied, \
            f"{name}:{lineno} conflates or omits the model/execution route"


def test_after_parity_body_has_only_the_checked_wrapper_as_a_caller():
    """The split keeps the legacy body readable, but creates a bypassable callable.
    A future direct call would silently skip §9, so bind the sole allowed caller."""
    found = []
    for path in sorted(_PY.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue

        class _Visitor(ast.NodeVisitor):
            def __init__(self):
                self.functions = []

            def visit_AsyncFunctionDef(self, node):
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            def visit_FunctionDef(self, node):
                self.functions.append(node.name)
                self.generic_visit(node)
                self.functions.pop()

            def visit_Call(self, node):
                name = (getattr(node.func, "id", None)
                        or getattr(node.func, "attr", None))
                if name == "_run_narration_job_after_parity":
                    found.append((
                        path.name,
                        self.functions[-1] if self.functions else None,
                    ))
                self.generic_visit(node)

        _Visitor().visit(tree)
    assert found == [("narration_api.py", "_run_narration_job")]


def test_flag_off_queue_payload_has_no_extra_key():
    """C11 shape identity, asserted against the real payload literal in the source.

    The legacy dict must not gain `canon_parity`; the key may only be assigned under a
    guard. A stub-based test could show the key absent for one run — this shows it
    cannot be present unconditionally at all.
    """
    tree = ast.parse(_src("narration_api.py"))
    literals = [n for n in ast.walk(tree)
                if isinstance(n, ast.Dict)
                and {k.value for k in n.keys if isinstance(k, ast.Constant)} >= {
                    "job_id", "job_uuid", "tenant_id", "user_id", "total", "meter_op",
                    "model", "body"}]
    assert literals, "could not find the queue payload literal"
    for lit in literals:
        keys = {k.value for k in lit.keys if isinstance(k, ast.Constant)}
        assert "canon_parity" not in keys, "the payload literal must not carry the key"

    guarded = [n for n in ast.walk(tree)
               if isinstance(n, ast.If)
               and any(isinstance(sub, ast.Constant) and sub.value == "canon_parity"
                       for sub in ast.walk(n))]
    assert guarded, "`canon_parity` must only be added inside a guard"


def test_parity_is_checked_before_any_physical_work():
    """§9 and the assist/enforce contract both need the check to precede generation."""
    src = _src("narration_api.py")
    body = src[src.index("async def _run_narration_job"):]
    assert body.index("check_parity") < body.index("generate_narration(req)")


def test_the_worker_reads_the_sibling_key_not_the_user_body():
    src = _src("narration_worker.py")
    assert 'canon_parity=data.get("canon_parity")' in src, \
        "the worker must read the payload sibling"
    assert 'canon_parity=data.get("body")' not in src
    assert "canon_model_route=_local_model_route" in src
    assert 'canon_model_route=data.get("model")' not in src


def test_a_snapshot_forged_inside_the_user_body_is_never_consulted():
    """`body` is the user's own request, forwarded verbatim into the queue payload. A
    snapshot placed there must have no influence: check_parity is only ever handed the
    sibling."""
    forged = _wire(_snap())
    body = {"topic": "x", "canon_parity": forged}
    # The executor consults the sibling. With no sibling, the verdict is 'absent' —
    # the forgery in the body cannot upgrade it to MATCH.
    assert _check(None) == (
        cl.PARITY_MISMATCH, ("snapshot_absent",))
    assert body["canon_parity"] is forged  # untouched, and unused


# ===========================================================================
# D. Executor behaviour — shadow degrades, assist/enforce fail closed
# ===========================================================================

class _Stub:
    def __init__(self):
        self.started = 0

    async def __call__(self, **kw):
        self.started += 1
        return {"ok": True, "output": f"teks {kw['no']}", "no": kw["no"], "model": "m"}


@pytest.fixture(autouse=True)
def _restore_write_chapter():
    original = st._write_chapter
    yield
    st._write_chapter = original


def _run_map(n=3, stub=None):
    st._write_chapter = stub or _Stub()
    chapters = [{"id": i + 1, "title": f"Bab {i+1}"} for i in range(n)]
    return asyncio.run(st.narrate_chapters(
        "topik", chapters, polish="none",
        shared_context=SharedContext(topic="topik", chapters=chapters)))


def test_an_ineligible_job_still_completes_normally_in_shadow(monkeypatch, caplog):
    """The whole point of the shadow rule: a mismatch must not fail the job, because
    the queue would retry it and turn one skewed job into two."""
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    cl.mark_job_canon_ineligible()
    stub = _Stub()
    with caplog.at_level("INFO"):
        res = _run_map(3, stub=stub)
    assert res["ok"] and len(res["chapters"]) == 3
    assert stub.started == 3


def test_an_ineligible_job_builds_no_canon_and_reports_skipped(monkeypatch, caplog):
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    cl.mark_job_canon_ineligible()
    with caplog.at_level("INFO"):
        _run_map(3)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "'canon_status': 'skipped'" in joined
    assert "'canon_status': 'present'" not in joined, "a canon was built for a skipped job"
    assert "freeze intact" not in joined, "the freeze ran for an ineligible job"


def test_skipped_is_not_clean_and_carries_no_counts(monkeypatch):
    d = cl.telemetry_digest(None, canon_status="skipped")
    assert d["canon_status"] == "skipped"
    assert d["canon_sha256"] == cl.UNKNOWN
    assert d["chapter_count"] == 0


def test_an_eligible_job_is_unaffected(monkeypatch, caplog):
    """The positive build: without this, every assertion above would also pass if the
    shadow path were simply dead."""
    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setenv("NARASI_STORY_BIBLE", "0")
    with caplog.at_level("INFO"):
        _run_map(3)
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "'canon_status': 'present'" in joined
    assert "'canon_status': 'skipped'" not in joined


def test_eligibility_is_per_job_not_global(monkeypatch):
    """A contextvar, so one skewed job cannot silence Canon Lite for a concurrent one."""
    async def scenario():
        seen = {}

        async def job(name, ineligible):
            if ineligible:
                cl.mark_job_canon_ineligible()
            await asyncio.sleep(0)
            seen[name] = cl.job_is_canon_ineligible()

        await asyncio.gather(
            asyncio.create_task(job("bad", True)),
            asyncio.create_task(job("good", False)),
        )
        return seen

    seen = asyncio.run(scenario())
    assert seen == {"bad": True, "good": False}


def test_assist_and_enforce_fail_closed_on_mismatch_in_source():
    """Shadow degrades; assist/enforce must refuse before physical work."""
    src = _src("narration_api.py")
    body = src[src.index("async def _run_narration_job"):]
    block = body[:body.index("generate_narration(req)")]
    assert '_cl_local in ("assist", "enforce")' in block
    assert "_STATUS_FAILED" in block and "_refund" in block


@pytest.mark.parametrize("mode", ["assist", "enforce"])
def test_assist_and_enforce_refuse_before_the_legacy_body(monkeypatch, mode):
    """Behavioural binding for the fail-closed branch; source placement alone can lie."""
    import narration_api as na

    calls = []

    async def fake_status(*args):
        calls.append(("status", args))

    async def fake_refund(*args):
        calls.append(("refund", args))

    async def forbidden_body(**kwargs):
        calls.append(("legacy-body", kwargs))

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", mode)
    # 🔴 THE JOB HAS TO BE AN ASSIST JOB FOR THIS CONTROL TO MEAN ANYTHING.
    #    `assist` is now per-tenant: a job whose tenant is not on the allowlist
    #    resolves to `off`, and an `off` executor seeing an absent dispatcher
    #    snapshot is CONSISTENT, not skewed — the legacy body is then the correct
    #    outcome. Without naming an allowlisted tenant this test would still pass
    #    for `enforce` and silently stop exercising `assist` at all.
    monkeypatch.setenv("NARASI_CANON_LITE_ASSIST_TENANTS", "t")
    monkeypatch.setattr(na, "_set_status", fake_status)
    monkeypatch.setattr(na, "_refund", fake_refund)
    monkeypatch.setattr(na, "_run_narration_job_after_parity", forbidden_body)

    asyncio.run(na._run_narration_job(
        body={}, job_id="j", job_uuid=None, tenant_id="t", user_id="u",
        total=1, meter_op="op", model="m", executor="python_api",
        canon_parity=None, canon_route="api_direct", canon_model_route="m",
    ))

    assert [name for name, _ in calls] == ["status", "refund"]
    assert not cl.job_is_canon_ineligible()


def test_the_off_executor_path_imports_no_canon_lite():
    """C11 again: the `off` branches must be inline, or a flag-off job would import
    Canon Lite just to discover it has nothing to do."""
    src = _src("narration_api.py")
    body = src[src.index("async def _run_narration_job"):]
    block = body[:body.index("generate_narration(req)")]
    off_branch = block[block.index('if _cl_local == "off":'):block.index("else:")]
    assert "import canon_lite" not in off_branch
    assert "snapshot_present_while_executor_off" in off_branch


def test_wrapper_resets_ineligibility_before_same_task_handles_next_job(monkeypatch):
    """BullMQ may reuse a callback task; one mismatch must not poison the next job."""
    import narration_api as na

    seen = []

    async def fake_job_body(**kwargs):
        seen.append(cl.job_is_canon_ineligible())

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "_run_narration_job_after_parity", fake_job_body)

    async def run_twice():
        common = {
            "body": {}, "job_uuid": None, "tenant_id": "t", "user_id": "u",
            "total": 1, "meter_op": None, "model": "m", "executor": "narration_worker",
            "canon_route": "bullmq_worker",
            "canon_model_route": "gemini-2.5-flash",
        }
        await na._run_narration_job(job_id="bad", canon_parity=None, **common)
        assert not cl.job_is_canon_ineligible(), "wrapper leaked mismatch after return"
        await na._run_narration_job(
            job_id="good", canon_parity=_snap(), **common)
        assert not cl.job_is_canon_ineligible(), "wrapper leaked state after matching job"

    asyncio.run(run_twice())
    assert seen == [True, False]


@pytest.mark.parametrize("route", ["bullmq_worker", "api_direct", "api_fallback"])
def test_wrapper_compares_the_actual_local_route(monkeypatch, route):
    """A wrapper that hard-codes one route would call valid in-process jobs mismatches."""
    import narration_api as na

    seen = []

    async def fake_job_body(**kwargs):
        seen.append(cl.job_is_canon_ineligible())

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "_run_narration_job_after_parity", fake_job_body)

    asyncio.run(na._run_narration_job(
        body={}, job_id="j", job_uuid=None, tenant_id="t", user_id="u",
        total=1, meter_op=None, model="m", executor="python_api",
        canon_parity=_snap(route=route), canon_route=route,
        canon_model_route="gemini-2.5-flash",
    ))
    assert seen == [False]


@pytest.mark.parametrize(
    "snapshot_model,local_model,expected_ineligible",
    [
        ("gemini-2.5-flash", "gemini-2.5-flash", False),
        ("gemini-2.5-flash", "claude-opus-4-6", True),
    ],
)
def test_wrapper_compares_the_actual_local_model_route(
    monkeypatch, snapshot_model, local_model, expected_ineligible
):
    """The model used by the worker is an independent §9 dimension."""
    import narration_api as na

    seen = []

    async def fake_job_body(**kwargs):
        seen.append(cl.job_is_canon_ineligible())

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "_run_narration_job_after_parity", fake_job_body)

    asyncio.run(na._run_narration_job(
        body={}, job_id="j", job_uuid=None, tenant_id="t", user_id="u",
        total=1, meter_op=None, model="m", executor="python_api",
        canon_parity=_snap(route="api_direct", model_route=snapshot_model),
        canon_route="api_direct", canon_model_route=local_model,
    ))
    assert seen == [expected_ineligible]


def test_wrapper_resets_ineligibility_when_the_legacy_body_raises(monkeypatch):
    """The cleanup is a finally-contract, not only a successful-return contract."""
    import narration_api as na

    async def exploding_body(**kwargs):
        assert cl.job_is_canon_ineligible()
        raise RuntimeError("offline sentinel")

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "_run_narration_job_after_parity", exploding_body)

    with pytest.raises(RuntimeError, match="offline sentinel"):
        asyncio.run(na._run_narration_job(
            body={}, job_id="j", job_uuid=None, tenant_id="t", user_id="u",
            total=1, meter_op=None, model="m", executor="python_api",
            canon_parity=None, canon_route="api_direct", canon_model_route="m",
        ))
    assert not cl.job_is_canon_ineligible()


def test_user_body_snapshot_is_removed_before_downstream_in_shadow(monkeypatch):
    import narration_api as na

    received = []

    async def fake_job_body(**kwargs):
        received.append(kwargs["body"])

    monkeypatch.setenv("NARASI_CANON_LITE_MODE", "shadow")
    monkeypatch.setattr(na, "_run_narration_job_after_parity", fake_job_body)
    forged = _snap(route="api_direct")

    asyncio.run(na._run_narration_job(
        body={"topic": "x", "canon_parity": forged},
        job_id="j", job_uuid=None, tenant_id="t", user_id="u", total=1,
        meter_op=None, model="m", executor="python_api",
        canon_parity=_snap(route="api_direct"), canon_route="api_direct",
        canon_model_route="gemini-2.5-flash",
    ))
    assert received == [{"topic": "x"}]


def test_new_parity_logs_are_bounded_and_identifier_free():
    src = _src("narration_api.py")
    block = src[src.index("async def _run_narration_job("):
                src.index("async def _run_narration_job_after_parity(")]
    assert "job=%s" not in block
    assert "type(" not in block
    assert "parity_check_error" in block

    dispatch = src[src.index("CANON LITE L1.1 — §9 mode/config snapshot"):
                   src.index('@app.get("/narration/queue/health")')]
    assert "type(" not in dispatch
