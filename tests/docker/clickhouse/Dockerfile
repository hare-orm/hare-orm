# ClickHouse for the hare-orm test suite: one server that is its own ClickHouse Keeper, so transactions,
# generateSerialID() keys and the migration lock hare takes in Keeper run against it. Never use this image for
# data you want to keep.
#
# CLICKHOUSE_VERSION picks the server version - the newest one and the oldest hare supports.
ARG CLICKHOUSE_VERSION=25.8
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

COPY keeper.xml transactions.xml system_logs.xml /etc/clickhouse-server/config.d/
