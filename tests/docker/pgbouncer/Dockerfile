# PgBouncer in transaction pooling in front of the test database (make test_pgbouncer_up), with
# prepared statements across transactions: the database server comes from the environment -
# PGBOUNCER_SERVER_HOST/PGBOUNCER_SERVER_PORT. PGBOUNCER_IMAGE picks the version, pinned - PgBouncer
# 1.26.1 by default; make test_pgbouncer_matrix_up passes each matrix version's image.
ARG PGBOUNCER_IMAGE=edoburu/pgbouncer:v1.26.1-p0@sha256:488422e38cff912895d0d65d8963fcd306e6ac589a724398b6eb8e294e94d624
FROM ${PGBOUNCER_IMAGE}

COPY --chmod=755 start-pgbouncer.sh /usr/local/bin/start-pgbouncer.sh

EXPOSE 6432
ENTRYPOINT ["/usr/local/bin/start-pgbouncer.sh"]
