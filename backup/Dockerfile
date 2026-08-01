# Off-site database backup runner (Railway cron service).
#
# Base is the official PostgreSQL 18 Alpine image purely for its client tools. pg_dump
# refuses to dump a server newer than itself, and the database is PostgreSQL 18, so the
# client major version is a correctness requirement rather than a preference. Taking it
# from this image avoids pulling postgresql18-client from Alpine's `edge` repository,
# whose contents move and would let a future rebuild silently change the client version.
FROM postgres:18-alpine

RUN apk add --no-cache rclone ca-certificates \
 && pg_dump --version \
 && pg_restore --version \
 && rclone version | head -1

WORKDIR /app
COPY run-backup.sh /app/run-backup.sh
RUN chmod +x /app/run-backup.sh

# The base image's entrypoint initialises a database server. Clear it: this container is a
# one-shot backup job, not a Postgres instance.
ENTRYPOINT []

# One shot, then exit. Railway's cron scheduler starts it on the configured schedule; a
# long-running process would be the wrong shape and would bill continuously.
CMD ["/app/run-backup.sh"]
