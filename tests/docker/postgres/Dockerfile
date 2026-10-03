# Postgres for the hare-orm test suite: PostGIS + pgvector on top of the stock contrib extensions
# (citext, btree_gist, hstore, ...), so a single server runs every Postgres-only test.
# Durability settings are relaxed at `docker run` time (see the Makefile's test_db_up target) -
# never use this image for data you want to keep.
FROM postgis/postgis:18-3.6

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-18-pgvector \
    && rm -rf /var/lib/apt/lists/*
