// Step 3: Sentry init — loaded via `node --import ./instrument.mjs` BEFORE the
// app, so @sentry/node can auto-instrument http/express (required in ESM).
// No-op when SENTRY_DSN_NODE is unset, so the server boots either way.
import * as Sentry from "@sentry/node";

if (process.env.SENTRY_DSN_NODE) {
  Sentry.init({
    dsn: process.env.SENTRY_DSN_NODE,
    tracesSampleRate: 0.1,
    environment: process.env.NODE_ENV || "development",
    sendDefaultPii: false,
  });
  console.log("[sentry] Node SDK initialised (instrument.mjs)");
} else {
  console.log("[sentry] Node disabled (no SENTRY_DSN_NODE)");
}
