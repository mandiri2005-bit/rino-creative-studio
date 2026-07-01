/**
 * auth.js — Clerk authentication for Rino Creative Studio
 * Uses @clerk/clerk-js@5 which exposes window.Clerk after script load.
 */

(function () {
  "use strict";

  const CLERK_CDN = "https://cdn.jsdelivr.net/npm/@clerk/clerk-js@5/dist/clerk.browser.js";

  const $loading = document.getElementById("clerk-loading");
  const $gate    = document.getElementById("clerk-gate");
  const $root    = document.getElementById("root");
  const $siModal = document.getElementById("clerk-sign-in-modal");
  const $suModal = document.getElementById("clerk-sign-up-modal");
  const $siMount = document.getElementById("clerk-sign-in-mount");
  const $suMount = document.getElementById("clerk-sign-up-mount");

  let _clerk       = null;
  let _currentUser = null;
  let _siMounted   = false;
  let _suMounted   = false;

  function show(el) { if (el) el.style.display = "flex"; }
  function hide(el) { if (el) el.style.display = "none"; }
  function notifyUserChange() { window.dispatchEvent(new Event("clerk:userchange")); }
  function _revealApp(){ hide($loading); hide($gate); if ($root) $root.style.visibility = "visible"; }
  function showApp() {
    if (window.__appReady) { _revealApp(); }
    else { window.addEventListener("app:ready", _revealApp, { once: true }); }
  }
  function showGate() { hide($loading); show($gate); if ($root) $root.style.visibility = "hidden"; }
  // Fail-closed helper: keep #root hidden (like the normal gate) but surface a
  // clear 'auth unavailable' message + reload affordance when Clerk can't run
  // (missing publishable key, CDN load failure, or _clerk.load() throwing).
  // This is a COSMETIC client-side gate only — the authoritative control is the
  // backend rejecting every /api/* request without a valid Clerk session and
  // row-level tenant scoping, which CANNOT be enforced from this file.
  function showAuthUnavailable(msg) {
    showGate();
    if (!$gate) return;
    let $err = document.getElementById("clerk-gate-error");
    if (!$err) {
      $err = document.createElement("div");
      $err.id = "clerk-gate-error";
      $err.setAttribute("role", "alert");
      $err.style.cssText = "margin-top:16px;text-align:center;color:#b91c1c;font-size:14px;line-height:1.5;";
      const $reload = document.createElement("button");
      $reload.type = "button";
      $reload.textContent = "Reload";
      $reload.style.cssText = "display:block;margin:12px auto 0;padding:8px 20px;border:1px solid #6366f1;border-radius:8px;background:#6366f1;color:#fff;cursor:pointer;font-size:14px;";
      $reload.addEventListener("click", () => window.location.reload());
      const $msg = document.createElement("p");
      $msg.id = "clerk-gate-error-msg";
      $msg.style.cssText = "margin:0;";
      $err.appendChild($msg);
      $err.appendChild($reload);
      $gate.appendChild($err);
    }
    const $msgEl = document.getElementById("clerk-gate-error-msg");
    if ($msgEl) $msgEl.textContent = msg || "Sign-in is currently unavailable. Please reload.";
  }
  function closeAllModals() {
    [$siModal, $suModal].forEach(m => m && m.classList.remove("open"));
  }

  [$siModal, $suModal].forEach(modal => {
    if (!modal) return;
    modal.addEventListener("click", e => { if (e.target === modal) closeAllModals(); });
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeAllModals(); });

  // Load Clerk v5 SDK dynamically — v5 exposes window.Clerk after onload
  function loadClerkScript(pk) {
    return new Promise((resolve, reject) => {
      // Prevent Clerk v5 auto-init by setting the key AFTER script loads
      // We do NOT set window.__clerk_frontend_api before load
      const s = document.createElement("script");
      s.src = CLERK_CDN;
      s.setAttribute("data-clerk-publishable-key", pk);
      s.onload = () => {
        // After load, window.Clerk should be available
        if (typeof window.Clerk !== "undefined") {
          resolve();
        } else {
          // v5 may need a tick
          setTimeout(() => {
            if (typeof window.Clerk !== "undefined") resolve();
            else reject(new Error("window.Clerk not found after script load"));
          }, 200);
        }
      };
      s.onerror = () => reject(new Error("Failed to load Clerk SDK from CDN"));
      document.head.appendChild(s);
    });
  }

  window.__clerkAuth = {
    async getAuthToken() {
      if (!_clerk || !_clerk.session) return null;
      try {
        // Try rino-api template first, fall back to default session token
        let token = await _clerk.session.getToken({ template: "rino-api" });
        if (!token) token = await _clerk.session.getToken();
        // Last resort: read from __session cookie (Clerk dev instances sometimes
        // fail getToken() but the cookie is still valid)
        if (!token) {
          token = document.cookie.match(/__session=([^;]+)/)?.[1] || null;
        }
        return token || null;
      } catch (e) {
        console.warn("[auth] getAuthToken:", e.message);
        return document.cookie.match(/__session=([^;]+)/)?.[1] || null;
      }
    },
    openSignIn() {
      closeAllModals();
      if ($siModal) $siModal.classList.add("open");
      if (!_siMounted && _clerk && $siMount) {
        // Land on the studio app after auth, not the marketing root "/" (Clerk's
        // default), so users aren't bounced back to the landing page.
        _clerk.mountSignIn($siMount, {
          forceRedirectUrl: "/app/", signUpForceRedirectUrl: "/app/",
          afterSignInUrl: "/app/", afterSignUpUrl: "/app/",
        });
        _siMounted = true;
      }
    },
    openSignUp() {
      closeAllModals();
      if ($suModal) $suModal.classList.add("open");
      if (!_suMounted && _clerk && $suMount) {
        _clerk.mountSignUp($suMount, {
          forceRedirectUrl: "/app/", signInForceRedirectUrl: "/app/",
          afterSignUpUrl: "/app/", afterSignInUrl: "/app/",
        });
        _suMounted = true;
      }
    },
    async signOut() {
      // Wimba's marketing landing lives on a SEPARATE domain (wimba.ai); ceritaAI's is app root.
      // Clerk v5 IGNORES a cross-origin signOut redirectUrl and falls back to afterSignOutUrl ('/')
      // → nginx 302 /app/ → unauthed → /sign-up. So clear the session WITHOUT relying on Clerk's
      // redirect, then navigate to the brand-correct landing ourselves (our nav wins, last write).
      const landing = window.__BRAND === "wimba" ? "https://wimba.ai" : "/";
      if (!_clerk) { window.location.href = landing; return; }
      try {
        _currentUser = null;
        notifyUserChange();
        ["rc_lzkey","rc_imgkey","rc_veokey","rc_sorakey","rc_ds_route",
         "rc_nar_outline","rc_nar_outline_text","rc_nar_result"]
          .forEach(k => localStorage.removeItem(k));
        await _clerk.signOut();
      } catch (e) {
        console.error("[auth] signOut:", e);
      }
      window.location.href = landing;   // WE own the redirect → beats Clerk's '/' fallback
    },
    get currentUser() { return _currentUser; },
    get clerk()       { return _clerk; },
  };

  async function initClerk() {
    if ($root) $root.style.visibility = "hidden";
    show($loading);

    const pk = window.__CLERK_PK;
    if (!pk || pk.startsWith("YOUR_CLERK")) {
      // FAIL CLOSED: a missing/placeholder publishable key means auth cannot run.
      // Previously this called showApp(), revealing #root and every fetch()-able
      // control unauthenticated (every /api/* call would then 401). Keep #root
      // hidden and surface a config error in the gate instead.
      console.error("[auth] window.__CLERK_PK not set — auth unavailable, refusing to reveal app.");
      showAuthUnavailable("Sign-in is not configured. Please reload, or contact support if this persists.");
      return;
    }

    try {
      await loadClerkScript(pk);

      // window.Clerk is now the constructor (v5 exposes it after data-clerk-publishable-key is set)
      _clerk = window.Clerk;
      await _clerk.load({
        // Brand the hosted Clerk sign-in/up widgets from code (no dashboard needed).
        // Widget background is light → use the BLACK logo. Title overrides the
        // default "Sign in to <app name>".
        appearance: {
          layout: {
            logoImageUrl: window.location.origin + (window.__BRAND === "wimba" ? "/assets/wimba-mark.svg" : "/assets/ceritaai-mark-black.png"),
            logoPlacement: "inside",
          },
          variables: { colorPrimary: "#6366f1" },
        },
        localization: {
          signIn: { start: { title: window.__BRAND === "wimba" ? "Sign in to Wimba" : "Sign in to Cerita AI", subtitle: "Welcome back! Please sign in to continue" } },
          signUp: { start: { title: window.__BRAND === "wimba" ? "Create your Wimba account" : "Create your Cerita AI account", subtitle: "Welcome! Please fill in the details to get started" } },
        },
      });

      _clerk.addListener(({ user }) => {
        _currentUser = user || null;
        notifyUserChange();
        if (user) { closeAllModals(); showApp(); }
        else { showGate(); }
      });

      _currentUser = _clerk.user || null;
      notifyUserChange();
      // Fire a second time after a tick so React components have mounted
      setTimeout(notifyUserChange, 300);
      _clerk.user ? showApp() : showGate();

    } catch (e) {
      // FAIL CLOSED: CDN load failure or _clerk.load() throwing means we cannot
      // establish a session. Previously this called showApp(), revealing the app
      // with no token — users then hit scattered per-feature 401s. Keep #root
      // hidden and show a single clear retry message in the gate instead.
      console.error("[auth] init error:", e);
      showAuthUnavailable("Sign-in service unavailable. Please reload the page.");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initClerk);
  } else {
    initClerk();
  }

})();
