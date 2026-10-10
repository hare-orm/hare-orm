# Testing on ClickHouse

hare's ClickHouse tests run against a ClickHouse server in Docker:

```bash
make test_clickhouse_up      # ClickHouse 25.8 - HTTP on port 8124, the native protocol on 9124
make test_clickhouse         # tests/dialects/clickhouse over both drivers
```

The server is its own ClickHouse Keeper — port 9182 on the host — with
`allow_experimental_transactions` on (`tests/docker/clickhouse`): the transaction tests connect with
`transactions=true`, the row lock tests with `keeper_hosts=127.0.0.1:9182` too, and the generated
keys come from its series. `make test_clickhouse` runs `tests/dialects/clickhouse/`, whose models
are its own — the shared test models of the rest of the suite need the unique and foreign key
constraints ClickHouse doesn't keep.

The cluster tests run against two servers of their own:

```bash
make test_clickhouse_cluster_up    # two servers - HTTP on 8126 and 8127, the native protocol on 9126 and 9127
make test_clickhouse_cluster       # tests/dialects/clickhouse/cluster over both drivers
make test_clickhouse_cluster_down
```

The servers form one shard of two replicas (`hare_replicated`) and two shards of one server
(`hare_sharded`), the first server their Keeper (`tests/docker/clickhouse_cluster`). The tests skip
without `HARE_TEST_CLICKHOUSE_CLUSTER_DB`, which `make test_clickhouse_cluster` sets.
