# ClickHouse: testing

hare's own ClickHouse tests run against ClickHouse servers in Docker.

## <a id="one-server"></a>One server

The tests of one server:

```bash
make test_clickhouse_up      # ClickHouse 25.8 - HTTP on port 8124, the native protocol on 9124
make test_clickhouse         # tests/dialects/clickhouse over both drivers
```

The server is its own ClickHouse Keeper — port 9182 on the host — with `allow_experimental_transactions`
on (`tests/docker/clickhouse`): the tests connect with `keeper_hosts=127.0.0.1:9182`, so a test database's
key series are deleted with it, the transaction tests with `transactions=true` too, and the generated keys
come from its series.
`make test_clickhouse` runs `tests/dialects/clickhouse/`, whose models are its own — the shared test
models of the rest of the suite need the unique and foreign key constraints ClickHouse doesn't keep.

## <a id="cluster"></a>A cluster

The cluster tests run against two servers of their own:

```bash
make test_clickhouse_cluster_up    # two servers - HTTP on 8126 and 8127, the native protocol on 9126 and 9127
make test_clickhouse_cluster       # tests/dialects/clickhouse/cluster over both drivers
make test_clickhouse_cluster_down
```

The servers form one shard of two replicas (`hare_replicated`) and two shards of one server
(`hare_sharded`), the first server their Keeper (`tests/docker/clickhouse_cluster`). The tests skip
without `HARE_TEST_CLICKHOUSE_CLUSTER_DB`, which `make test_clickhouse_cluster` sets.

## <a id="every-version"></a>Every version

The test matrix runs both on each ClickHouse version from 25.8 — a server and a cluster per version, each
on ports made of the version's digits (`tests/matrix/matrix_ports.py`):

```bash
make test_clickhouse_matrix_up test_clickhouse_cluster_matrix_up matrix_clickhouse_versions=26.3
make test_matrix MATRIX_OPTIONS="--python 3.14 --suite 'clickhouse-*-26.3'"
make test_clickhouse_matrix_down test_clickhouse_cluster_matrix_down matrix_clickhouse_versions=26.3
```

The server of 26.3 listens on 32603 (HTTP), 42603 (the native protocol) and 62603 (its Keeper), the first
node of its cluster on 52603 and 22603. The matrix points the migration lock and generated key tests at the
version's Keeper with `HARE_TEST_CLICKHOUSE_KEEPER_HOSTS` (`127.0.0.1:9182` without it).
