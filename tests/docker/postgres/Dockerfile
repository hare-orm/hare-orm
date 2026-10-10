# Postgres for the hare-orm test suite: PostGIS + pgvector on top of the stock contrib extensions
# (citext, btree_gist, hstore, ...), so a single server runs every Postgres-only test.
# Durability settings are relaxed at `docker run` time (see the Makefile's test_db_up target) -
# never use this image for data you want to keep.
#
# PG_MAJOR picks the server version - 14 to 18, the test matrix. Every version is built the same
# way: the official image, PostGIS and pgvector from the PostgreSQL apt repository it carries.
ARG PG_MAJOR=18
FROM postgres:${PG_MAJOR}

ARG PG_MAJOR
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-${PG_MAJOR}-postgis-3 \
        postgresql-${PG_MAJOR}-postgis-3-scripts \
        postgresql-${PG_MAJOR}-pgvector \
    && rm -rf /var/lib/apt/lists/*
