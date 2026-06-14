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
          forceRedirectUrl: "/index.html", signUpForceRedirectUrl: "/index.html",
          afterSignInUrl: "/index.html", afterSignUpUrl: "/index.html",
        });
        _siMounted = true;
      }
    },
    openSignUp() {
      closeAllModals();
      if ($suModal) $suModal.classList.add("open");
      if (!_suMounted && _clerk && $suMount) {
        _clerk.mountSignUp($suMount, {
          forceRedirectUrl: "/index.html", signInForceRedirectUrl: "/index.html",
          afterSignUpUrl: "/index.html", afterSignInUrl: "/index.html",
        });
        _suMounted = true;
      }
    },
    async signOut() {
      if (!_clerk) return;
      try {
        // Update UI immediately (optimistic) — don't wait for Clerk's server round-trip.
        _currentUser = null;
        notifyUserChange();
        showGate();
        _clerk.signOut();   // fire-and-forget; runs in background
        ["rc_lzkey","rc_imgkey","rc_veokey","rc_sorakey","rc_ds_route",
         "rc_nar_outline","rc_nar_outline_text","rc_nar_result"]
          .forEach(k => localStorage.removeItem(k));
        showGate();
      } catch (e) { console.error("[auth] signOut:", e); }
    },
    get currentUser() { return _currentUser; },
    get clerk()       { return _clerk; },
  };

  async function initClerk() {
    if ($root) $root.style.visibility = "hidden";
    show($loading);

    const pk = window.__CLERK_PK;
    if (!pk || pk.startsWith("YOUR_CLERK")) {
      console.warn("[auth] window.__CLERK_PK not set — running without auth.");
      showApp();
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
            logoImageUrl: window.location.origin + "/assets/ceritaai-mark-black.png",
            logoPlacement: "inside",
          },
          variables: { colorPrimary: "#6366f1" },
        },
        localization: {
          signIn: { start: { title: "Sign in to Cerita AI", subtitle: "Welcome back! Please sign in to continue" } },
          signUp: { start: { title: "Create your Cerita AI account", subtitle: "Welcome! Please fill in the details to get started" } },
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
      console.error("[auth] init error:", e);
      showApp();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initClerk);
  } else {
    initClerk();
  }

})();
