// Step 3: Sentry init — loaded via `node --import ./instrument.mjs` BEFORE the
// app, so @sentry/node can auto-instrument http/express (required in ESM).
// No-op when SENTRY_DSN_NODE is unset, so the server boots either way.
import * as Sentry from "@sentry/node";

// Strip secrets that ride OUTBOUND request URLs (e.g. ?key=<google-api-key> on the
// Flow native-image path) out of Sentry breadcrumb + span attributes — beforeSend's
// inbound-request scrub can't reach those. Drops the query string entirely (we never
// need it for triage) and redacts the parsed query fields.
const _Q_KEYS = ["http.query", "url.query"];
const _URL_KEYS = ["url", "http.url", "url.full", "http.target"];
function scrubUrlBag(bag) {
  if (!bag || typeof bag !== "object") return;
  for (const k of _Q_KEYS) if (typeof bag[k] === "string") bag[k] = "[redacted]";
  for (const k of _URL_KEYS) if (typeof bag[k] === "string") bag[k] = bag[k].replace(/\?.*$/s, "?[redacted]");
}

if (process.env.SENTRY_DSN_NODE) {
  Sentry.init({
    dsn: process.env.SENTRY_DSN_NODE,
    tracesSampleRate: 0.1,
    environment: process.env.NODE_ENV || "development",
    sendDefaultPii: false,
    // Redact secrets before any event leaves the process. The default
    // requestDataIntegration captures request headers + body, which would
    // otherwise ship x-admin-secret / Authorization / a pasted provider key in
    // cleartext (the Python side already scrubs; this is the Node parity fix).
    beforeSend(event) {
      try {
        const req = event.request;
        if (req) {
          const h = req.headers;
          if (h) for (const k of Object.keys(h)) {
            const kl = k.toLowerCase();
            if (kl === "authorization" || kl === "cookie"
                || kl.endsWith("-secret") || kl.endsWith("-api-key") || kl.endsWith("-token")) {
              h[k] = "[redacted]";
            }
          }
          // Sentry re-parses the Cookie header into a SEPARATE object (req.cookies),
          // so redacting the header alone still ships e.g. Clerk's __session JWT.
          if (req.cookies) req.cookies = "[redacted]";
          // Request bodies can carry pasted/BYOK provider keys (google_api_key,
          // apiKey, apiKeys) on several NON-admin routes too — strip all bodies;
          // we never need the raw body for triage.
          if (req.data !== undefined) req.data = "[redacted]";
          // defensive: a future ?key=/?token= inbound route can't leak (none today)
          if (req.query_string) req.query_string = "[redacted]";
        }
        // Outbound-HTTP breadcrumbs carry the request URL incl. query — a Google key
        // passed as ?key=… ships here; beforeSend is the only place to strip it.
        if (Array.isArray(event.breadcrumbs)) for (const b of event.breadcrumbs) scrubUrlBag(b && b.data);
      } catch { /* redaction must never throw */ }
      return event;
    },
    // Transactions SKIP beforeSend; their spans carry http.url/url.full WITH the query
    // string, so ~tracesSampleRate of outbound ?key=… URLs would leak. Scrub them too.
    beforeSendTransaction(event) {
      try {
        if (Array.isArray(event.spans)) for (const s of event.spans) scrubUrlBag(s && s.data);
        scrubUrlBag(event.contexts && event.contexts.trace && event.contexts.trace.data);
      } catch { /* never throw */ }
      return event;
    },
  });
  console.log("[sentry] Node SDK initialised (instrument.mjs)");
} else {
  console.log("[sentry] Node disabled (no SENTRY_DSN_NODE)");
}
