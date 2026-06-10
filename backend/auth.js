/**
 * auth.js — Clerk JWT middleware for Rino Creative Studio (Node.js)
 * Uses @clerk/express  |  ESM (import/export) to match server.js
 *
 * Install: npm install @clerk/express --save  (from ./backend/)
 */

import { clerkMiddleware, getAuth } from "@clerk/express";

// ── Re-export clerkMiddleware for app.use() in server.js ─────────────────────
export { clerkMiddleware };

// ── requireAuth — protect individual routes ───────────────────────────────────
// Usage: app.get('/api/config', requireAuth, async (req, res) => { ... })
export function requireAuth(req, res, next) {
  const auth = getAuth(req);

  if (!auth || !auth.userId) {
    return res.status(401).json({ error: "Unauthorized" });
  }

  // orgId is null when user has no Clerk organisation.
  // During Phase 1 we allow this (personal tenant fallback in Python middleware).
  // Uncomment the block below in Phase 2 when orgs are mandatory:
  //
  // if (!auth.orgId) {
  //   return res.status(403).json({ error: "Forbidden — no organisation" });
  // }

  req.authData = auth;
  req.authClaims = auth.sessionClaims || {};
  next();
}

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Returns the Clerk org ID (tenant) — null if user has no org yet. */
export function getTenantId(req) {
  return (req.authData ?? req.auth)?.orgId ?? null;
}

/** Returns the Clerk user ID. */
export function getUserId(req) {
  return (req.authData ?? req.auth)?.userId ?? null;
}
