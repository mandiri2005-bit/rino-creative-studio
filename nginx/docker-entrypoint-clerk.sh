#!/bin/sh
# nginx container entrypoint (Wimba/global). Two runtime config injections at START:
#   1) nginx.conf  ← PORT + BACKEND_URL via envsubst (unchanged behaviour)
#   2) window.__CLERK_PK ← CLERK_PUBLISHABLE_KEY injected into the served HTML, so the
#      Clerk publishable key is config-driven PER DEPLOY instead of hardcoded per-branch
#      (prevents the ceritaAI/Wimba kid-mismatch from recurring on a studio rebuild).
# NOTE: injection is best-effort and MUST NOT block nginx from starting (no `set -e`).

# 1) nginx.conf from template
envsubst '${PORT} ${BACKEND_URL}' < /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

# 2) Clerk publishable key → served HTML. Delimiter '|' is safe for base64 pks
#    (alphabet A-Za-z0-9+/= never contains '|'). Default baked in the Dockerfile, so
#    PK is never empty in normal operation; the empty branch is a loud last-resort guard.
PK="${CLERK_PUBLISHABLE_KEY}"
if [ -z "$PK" ]; then
  echo ">>> [clerk-pk] ERROR: CLERK_PUBLISHABLE_KEY is empty — frontend Clerk will NOT initialize"
else
  echo ">>> [clerk-pk] injecting window.__CLERK_PK = $PK"
fi
for f in /usr/share/nginx/html/*.html /usr/share/nginx/html/app/*.html /usr/share/nginx/html/image/*.html; do
  [ -f "$f" ] && sed -i "s|__CLERK_PK_ENV__|${PK}|g" "$f" 2>/dev/null || true
done

# 3) hand off to nginx as PID 1
exec nginx -g 'daemon off;'
