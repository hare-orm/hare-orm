# ClickHouse for the hare-orm test suite: one server that is its own ClickHouse Keeper, so transactions,
# generateSerialID() keys and the row locks hare takes in Keeper run against it. Never use this image for
# data you want to keep.
#
# CLICKHOUSE_VERSION picks the server version - the newest one and the oldest hare supports.
ARG CLICKHOUSE_VERSION=25.8
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

COPY keeper.xml transactions.xml /etc/clickhouse-server/config.d/
