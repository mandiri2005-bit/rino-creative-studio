// ─────────────────────────────────────────────────────────────────────────────
// CR-29 item 4 — assent identity safety.
//
// WHY THIS EXISTS. Four production assent rows were recorded against the wrong
// account: the reader's browser held a session for a different tenant, and nothing
// on the page said so. Two of those rows landed 0.83 s apart because the buttons
// were re-enabled after a POST. Every row is permanent — the table is append-only —
// so the fix has to make the identity visible BEFORE the decision and make a second
// decision impossible without a deliberate reload.
//
// The client half of this file does not read the source and assert about it: it
// EXECUTES the shipped <script> from tos-assent.html inside a vm with a minimal DOM,
// a stubbed fetch and a stubbed confirm, then drives the real buttons. So "two clicks
// produce one POST" is observed, not inferred.
// ─────────────────────────────────────────────────────────────────────────────
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash, webcrypto } from "node:crypto";
import vm from "node:vm";

const page = readFileSync(new URL("../../backend/public/tos-assent.html", import.meta.url), "utf8");
const server = readFileSync(new URL("../../backend/server.js", import.meta.url), "utf8");

const stripJs = (s) => s.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|\s)\/\/[^\n]*/g, "$1");
const stripHtml = (s) => stripJs(s.replace(/<!--[\s\S]*?-->/g, ""));

const TENANT = "102cfad9-395f-5c6e-850b-052fb641c2a0";
const ACTOR  = "5cafd74d-f21e-4168-811d-cd0ec6ae76a6";
const OTHER_ACTOR = "be19f7f7-be5e-4642-b3ef-29d1baed399f";

// ═════════════════════════════════════════════════════════════════════════════
// A. SERVER — identity is derived, never accepted
// ═════════════════════════════════════════════════════════════════════════════
describe("assent identity — the server decides who you are", () => {
  const code = stripJs(server);
  const applicable = code.match(/app\.get\("\/tos\/applicable"[\s\S]*?\n\}\);/)?.[0] || "";
  const assent     = code.match(/app\.post\("\/tos\/assent"[\s\S]*?\n\}\);/)?.[0] || "";

  test("NON-VACUITY: both routes were found", () => {
    assert.ok(applicable.length > 200, "GET /tos/applicable must be located");
    assert.ok(assent.length > 400, "POST /tos/assent must be located");
  });

  test("GET resolves the actor server-side and 403s when it cannot", () => {
    assert.match(applicable, /const userId = await resolveUserId\(req, tenantId\)/,
      "the actor must be resolved from the request, on the read");
    assert.match(applicable, /if \(!userId\) return res\.status\(403\)/,
      "no identity must mean 403, not a presentable agreement");
    assert.match(applicable, /tenant_id: tenantId, actor_id: userId/,
      "the response must carry the server-derived identity");
    // and it must refuse BEFORE resolving/returning any agreement
    assert.ok(applicable.indexOf("if (!userId) return res.status(403)")
              < applicable.indexOf("applicableArtifact"),
      "403 must short-circuit before an artifact is resolved");
  });

  test("POST derives identity from the session and NEVER from the body", () => {
    assert.match(assent, /const userId = await resolveUserId\(req, tenantId\)/);
    // the only assignments to userId/tenantId are the server-derived ones
    assert.equal((assent.match(/userId\s*=/g) || []).length, 1, "userId is assigned exactly once");
    assert.equal((assent.match(/tenantId\s*=/g) || []).length, 1, "tenantId is assigned exactly once");
    assert.doesNotMatch(assent, /userId\s*=\s*(b|req\.body)/, "userId must never come from the body");
    assert.doesNotMatch(assent, /tenantId\s*=\s*(b|req\.body)/, "tenantId must never come from the body");
    // the row is written with the server-derived pair
    assert.match(assent, /recordAssent\(\{\s*\n?\s*tenantId, actorId: userId,/,
      "the row must be written with the derived identity");
  });

  test("the expectation is COMPARED, and a mismatch writes nothing", () => {
    assert.match(assent, /const expTenant = String\(b\.expected_tenant_id \|\| ""\)\.trim\(\)/);
    assert.match(assent, /const expActor\s+= String\(b\.expected_actor_id\s+\|\| ""\)\.trim\(\)/);
    assert.match(assent, /if \(!expTenant \|\| !expActor\)[\s\S]{0,120}assent_identity_expectation_required/,
      "a missing expectation must be refused, not treated as agreement");
    assert.match(assent, /if \(expTenant !== String\(tenantId\) \|\| expActor !== String\(userId\)\)/,
      "the comparison must be against the SERVER values");
    assert.match(assent, /res\.status\(409\)[\s\S]{0,200}assent_identity_changed/,
      "a mismatch must be 409 assent_identity_changed");

    // zero writes on mismatch: the check returns before recordAssent is ever reached
    const cmpAt = assent.indexOf("expTenant !== String(tenantId)");
    const recAt = assent.indexOf("recordAssent");
    assert.ok(cmpAt >= 0 && recAt > cmpAt, "the identity check must precede the write");
    const between = assent.slice(cmpAt, recAt);
    assert.match(between, /return res\.status\(409\)/, "the mismatch branch must RETURN before the write");
    // …and before the artifact is even re-resolved, so nothing else runs on a mismatch
    assert.ok(cmpAt < assent.indexOf("applicableArtifact"),
      "identity must be settled before the artifact work");
  });

  test("the success response repeats the SERVER identity, not the client's claim", () => {
    const ok = assent.match(/res\.json\(\{\s*\n?\s*acceptance_event_id[\s\S]*?\}\);/)?.[0] || "";
    assert.ok(ok, "the success response must be found");
    assert.match(ok, /tenant_id: tenantId, actor_id: userId/);
    assert.doesNotMatch(ok, /expected_/, "it must not echo the client's expectation back");
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// B. CLIENT — the shipped script, actually run
// ═════════════════════════════════════════════════════════════════════════════
const ARTIFACT = "<!doctype html><html><body><h1>Fixture agreement</h1></body></html>";
const ARTIFACT_BYTES = Buffer.from(ARTIFACT, "utf8");
const ARTIFACT_SHA = createHash("sha256").update(ARTIFACT_BYTES).digest("hex");

/**
 * Minimal element: only what the page touches. The INITIAL state must mirror the markup
 * — `#ui` and `#unavailable` ship `hidden`, and both buttons ship `disabled`. Starting
 * them the other way round makes every settle-loop condition true before the page has
 * done anything, and the harness silently tests nothing.
 */
function makeEl(id) {
  const listeners = {};
  const hidden = id === "ui" || id === "unavailable";
  const disabled = id === "accept" || id === "reject" || id === "privacy";
  return {
    id, textContent: "", hidden, disabled, href: "", _srcdoc: null,
    _listeners: listeners,
    setAttribute() {},
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    set srcdoc(v) {
      this._srcdoc = v;
      // the real browser fires load asynchronously once the document is parsed
      queueMicrotask(() => (listeners.load || []).forEach((f) => f()));
    },
    get srcdoc() { return this._srcdoc; },
  };
}

/**
 * Run the page's script with a stub environment.
 * `applicableBody` is what GET /tos/applicable returns; `confirmReturns` drives confirm().
 */
async function runPage({ applicableBody, applicableStatus = 200, confirmReturns = true,
                         assentResponse } = {}) {
  const els = {};
  for (const id of ["ui", "unavailable", "agreement", "m-actor", "m-tenant", "m-version",
                    "m-locale", "m-hash", "m-rule", "m-deploy", "download", "accept",
                    "reject", "status", "privacy"]) els[id] = makeEl(id);

  const posts = [];
  const confirms = [];

  const fetchStub = async (url, opts = {}) => {
    const u = String(url);
    if (u.startsWith("/tos/applicable")) {
      return {
        status: applicableStatus, ok: applicableStatus === 200,
        json: async () => applicableBody,
      };
    }
    if (u === "/tos/assent") {
      posts.push({ body: JSON.parse(opts.body), credentials: opts.credentials });
      const resp = assentResponse || {
        status: 200, body: { acceptance_event_id: "evt-1", event: JSON.parse(opts.body).event,
                             event_at: "2026-08-09T00:00:00Z", tenant_id: TENANT, actor_id: ACTOR },
      };
      return { status: resp.status, ok: resp.status === 200, json: async () => resp.body };
    }
    // the public artifact, cross-origin
    return {
      status: 200, ok: true,
      arrayBuffer: async () => ARTIFACT_BYTES.buffer.slice(
        ARTIFACT_BYTES.byteOffset, ARTIFACT_BYTES.byteOffset + ARTIFACT_BYTES.byteLength),
    };
  };

  const sandbox = {
    document: { getElementById: (id) => els[id] || null },
    window: { confirm: (msg) => { confirms.push(msg); return confirmReturns; } },
    location: { search: "" },
    fetch: fetchStub,
    crypto: webcrypto,
    URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
    Blob: globalThis.Blob,
    TextDecoder: globalThis.TextDecoder,
    URLSearchParams: globalThis.URLSearchParams,
    console,
    queueMicrotask,
  };
  sandbox.globalThis = sandbox;

  // Strip HTML comments FIRST: one of them explains that the artifact's own script tags
  // never execute, and it contains a literal <script> that an unguarded regex grabs
  // instead of the real block.
  const script = page.replace(/<!--[\s\S]*?-->/g, "").match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInNewContext(script, vm.createContext(sandbox));

  // settle: wait until the page either enabled the buttons or revealed a failure
  for (let i = 0; i < 200; i++) {
    if (els.accept.disabled === false || els.unavailable.hidden === false) break;
    await new Promise((r) => setTimeout(r, 1));
  }
  // guard against a harness that "settles" because nothing ever ran
  return { els, posts, confirms,
           settled: els.accept.disabled === false || els.unavailable.hidden === false };
}

const goodBody = () => ({
  tos_version: "2026-08-09.1", locale: "en", artifact_sha256: ARTIFACT_SHA,
  public_route: "https://wimba.ai/terms/", byte_size: ARTIFACT_BYTES.byteLength,
  content_type: "text/html; charset=utf-8", effective_at: "2026-08-09T02:11:16.931522Z",
  determination_rule: "global_default_en", tenant_id: TENANT, actor_id: ACTOR,
});

describe("assent identity — the shipped page, executed", () => {
  test("NON-VACUITY: the harness really drives the page to a usable state", async () => {
    const { els, posts, settled } = await runPage({ applicableBody: goodBody() });
    assert.ok(settled, "the page must actually reach a terminal state, not time out");
    assert.equal(els.unavailable.hidden, true,
      `no failure path was taken (message was: ${els.unavailable.textContent})`);
    assert.equal(els.ui.hidden, false, "the decision UI is shown");
    assert.equal(els.accept.disabled, false, "Accept became usable");
    assert.equal(posts.length, 0, "nothing was posted merely by loading");
    assert.ok(els.agreement.srcdoc.includes("Fixture agreement"), "the frame got the document");
  });

  test("the identity is displayed, and it is the server's", async () => {
    const { els } = await runPage({ applicableBody: goodBody() });
    assert.equal(els["m-actor"].textContent, ACTOR);
    assert.equal(els["m-tenant"].textContent, TENANT);
  });

  test("buttons stay disabled when the server states no identity", async () => {
    const body = goodBody(); delete body.actor_id;
    const { els, posts } = await runPage({ applicableBody: body });
    assert.equal(els.accept.disabled, true, "Accept must never become usable");
    assert.equal(els.reject.disabled, true, "Reject must never become usable");
    assert.equal(els.ui.hidden, true, "the decision UI must be hidden");
    assert.equal(posts.length, 0);
    assert.match(els.unavailable.textContent, /did not identify/);
  });

  test("the confirmation names the decision and the actor", async () => {
    const { els, confirms } = await runPage({ applicableBody: goodBody() });
    await els.accept.onclick();
    assert.equal(confirms.length, 1);
    assert.match(confirms[0], /"accepted"/);
    assert.match(confirms[0], new RegExp(ACTOR));
    assert.match(confirms[0], new RegExp(TENANT));
    assert.match(confirms[0], /ONE decision/i);
  });

  test("cancelling the confirmation posts nothing and leaves the buttons usable", async () => {
    const { els, posts } = await runPage({ applicableBody: goodBody(), confirmReturns: false });
    await els.accept.onclick();
    assert.equal(posts.length, 0, "a cancelled decision must not be sent");
    assert.equal(els.accept.disabled, false, "cancelling must not lock the page");
    await els.reject.onclick();
    assert.equal(posts.length, 0);
  });

  test("the POST asserts identity — it does not choose one", async () => {
    const { els, posts } = await runPage({ applicableBody: goodBody() });
    await els.accept.onclick();
    assert.equal(posts.length, 1);
    const body = posts[0].body;
    assert.equal(body.expected_tenant_id, TENANT);
    assert.equal(body.expected_actor_id, ACTOR);
    assert.equal(posts[0].credentials, "include", "the session must still be sent");
    // no field that could be read as a selection of identity
    assert.ok(!("actor_id" in body), "the body must not carry actor_id");
    assert.ok(!("tenant_id" in body), "the body must not carry tenant_id");
    assert.ok(!("user_id" in body) && !("userId" in body));
    assert.deepEqual(Object.keys(body).sort(),
      ["artifact_sha256", "event", "expected_actor_id", "expected_tenant_id", "locale", "tos_version"]);
  });

  test("ACCEPT then REJECT in one page load produces exactly ONE post", async () => {
    const { els, posts } = await runPage({ applicableBody: goodBody() });
    await els.accept.onclick();
    await els.reject.onclick();
    await els.accept.onclick();
    assert.equal(posts.length, 1, "the second and third clicks must send nothing");
    assert.equal(posts[0].body.event, "accepted");
    assert.equal(els.accept.disabled, true, "both buttons stay locked after a decision");
    assert.equal(els.reject.disabled, true);
  });

  test("REJECT then ACCEPT in one page load produces exactly ONE post", async () => {
    const { els, posts } = await runPage({ applicableBody: goodBody() });
    await els.reject.onclick();
    await els.accept.onclick();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].body.event, "rejected", "the first confirmed decision is the only one");
    assert.equal(els.accept.disabled, true);
    assert.equal(els.reject.disabled, true);
  });

  test("a FAILED post does not unlock the page either", async () => {
    const { els, posts } = await runPage({
      applicableBody: goodBody(),
      assentResponse: { status: 409, body: { error: "assent_identity_changed",
                                             tenant_id: TENANT, actor_id: OTHER_ACTOR } },
    });
    await els.reject.onclick();
    assert.equal(posts.length, 1);
    assert.equal(els.accept.disabled, true, "a failure must not re-open the decision");
    assert.equal(els.reject.disabled, true);
    await els.accept.onclick();
    assert.equal(posts.length, 1, "still one");
    assert.match(els.status.textContent, /identity changed/i);
    assert.match(els.status.textContent, new RegExp(OTHER_ACTOR), "it must name who the server sees");
    assert.match(els.status.textContent, /Nothing was written/i);
  });

  test("the success status repeats the identity the SERVER used", async () => {
    const { els } = await runPage({
      applicableBody: goodBody(),
      assentResponse: { status: 200, body: { acceptance_event_id: "e1", event: "rejected",
                                             tenant_id: TENANT, actor_id: ACTOR } },
    });
    await els.reject.onclick();
    assert.match(els.status.textContent, /Recorded: rejected/);
    assert.match(els.status.textContent, new RegExp(ACTOR));
    assert.match(els.status.textContent, new RegExp(TENANT));
    assert.match(els.status.textContent, /Reload/i);
  });

  test("401 and 403 keep their own messages and never enable a decision", async () => {
    for (const [status, re] of [[401, /not signed in/i], [403, /could not be identified/i]]) {
      const { els, posts } = await runPage({ applicableBody: {}, applicableStatus: status });
      assert.match(els.unavailable.textContent, re, `status ${status}`);
      assert.equal(els.accept.disabled, true);
      assert.equal(els.ui.hidden, true);
      assert.equal(posts.length, 0);
    }
  });
});

// ═════════════════════════════════════════════════════════════════════════════
// C. the untouched invariants, restated here so this file fails if they regress
// ═════════════════════════════════════════════════════════════════════════════
describe("assent identity — the older invariants are unchanged", () => {
  const code = stripHtml(page);

  test("hash, sandbox, credentials, download and the 401 branch all stand", () => {
    assert.match(code, /<iframe id="agreement" sandbox=""/);
    assert.doesNotMatch(code, /allow-scripts|allow-same-origin/);
    assert.equal(code.match(/frame\.srcdoc = new TextDecoder\("utf-8"\)\.decode\(bytes\);/g).length, 1);
    assert.doesNotMatch(code, /innerHTML|<base/i);
    assert.match(code, /actualHash !== art\.artifact_sha256/);
    assert.match(code, /URL\.createObjectURL\(new Blob\(\[bytes\]/);
    assert.equal((code.match(/credentials:\s*"omit"/g) || []).length, 1);
    assert.equal((code.match(/credentials:\s*"include"/g) || []).length, 2);
    assert.match(code, /if \(r\.status === 401\) return fail\("You are not signed in\. Sign in and reopen this page\."\);/);
  });

  test("the decision lock is a one-way latch, set only after confirmation", () => {
    assert.match(code, /let decisionLocked = false;/);
    assert.match(code, /if \(decisionLocked\) return;/);
    assert.equal((code.match(/decisionLocked = true/g) || []).length, 1, "set exactly once");
    assert.doesNotMatch(code, /decisionLocked = false;\s*\n[\s\S]*decisionLocked = false/,
      "it must never be cleared again");
    // the latch is set AFTER the confirm, so cancelling leaves the page usable
    assert.ok(code.indexOf("if (!confirmed) return;") < code.indexOf("decisionLocked = true"));
    // Exactly ONE site enables the buttons — the gated one in load(), after the awaited
    // render. send()'s old re-enable is gone: that is what produced two rows 0.83 s apart.
    const enables = [...code.matchAll(/\$\("accept"\)\.disabled = \$\("reject"\)\.disabled = false/g)]
      .map((m) => m.index);
    assert.equal(enables.length, 1, "there must be exactly one enable site");
    assert.ok(enables[0] > code.indexOf("await rendered"), "it must follow the awaited render");
    assert.ok(enables[0] < code.indexOf("async function send("),
      "it must live in load(), not in the submit path");
  });
});
