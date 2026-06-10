FROM nginx:1.27-alpine

RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx/nginx.conf.template /etc/nginx/templates/nginx.conf.template
COPY backend/public /usr/share/nginx/html

ENV BACKEND_URL=http://backend.railway.internal:3000
ENV PORT=8080

CMD ["/bin/sh", "-c", "envsubst '${PORT} ${BACKEND_URL}' < /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf && nginx -g 'daemon off;'"]
