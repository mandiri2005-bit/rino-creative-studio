FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx/nginx.conf.template /etc/nginx/templates/nginx.conf.template
COPY backend/public /usr/share/nginx/html
COPY nginx/docker-entrypoint-clerk.sh /docker-entrypoint-clerk.sh
RUN chmod +x /docker-entrypoint-clerk.sh

ENV BACKEND_URL=http://backend.railway.internal:3000
ENV PORT=8080
# Clerk publishable key, injected into the served HTML at container START (config-driven
# per deploy; overridable via the service env). Default = Wimba's instance (enabling-cowbird-6)
# so the __CLERK_PK_ENV__ placeholder is never left unresolved. ceritaAI/main does NOT use
# this mechanism (its HTML hardcodes its own pk) — DO NOT merge this branch to main.
ENV CLERK_PUBLISHABLE_KEY=pk_test_ZW5hYmxpbmctY293YmlyZC02LmNsZXJrLmFjY291bnRzLmRldiQ

CMD ["/docker-entrypoint-clerk.sh"]
