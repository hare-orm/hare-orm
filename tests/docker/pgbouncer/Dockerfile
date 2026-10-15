# PgBouncer in transaction pooling in front of the test database (make test_pgbouncer_up), with
# prepared statements across transactions: the database server comes from the environment -
# PGBOUNCER_SERVER_HOST/PGBOUNCER_SERVER_PORT. PgBouncer 1.26.0, pinned.
FROM edoburu/pgbouncer@sha256:9c78945868a6a142c7fc40ccd843bbe5a606df163c7ffce4de70e0d628d696a2

COPY --chmod=755 start-pgbouncer.sh /usr/local/bin/start-pgbouncer.sh

EXPOSE 6432
ENTRYPOINT ["/usr/local/bin/start-pgbouncer.sh"]
