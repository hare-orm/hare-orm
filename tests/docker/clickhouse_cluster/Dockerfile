# A node of the ClickHouse cluster of the hare-orm test suite: two servers known to each other as one
# shard of two replicas (the cluster hare_replicated) and as two shards (the cluster hare_sharded), the
# first of them the ClickHouse Keeper of both. Never use this image for data you want to keep.
#
# NODE picks the node's own configuration - node1 or node2.
ARG CLICKHOUSE_VERSION=25.8
FROM clickhouse/clickhouse-server:${CLICKHOUSE_VERSION}

ARG NODE=node1
COPY cluster.xml /etc/clickhouse-server/config.d/
COPY ${NODE}.xml /etc/clickhouse-server/config.d/node.xml
