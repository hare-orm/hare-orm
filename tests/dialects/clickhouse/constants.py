import os

#: The Keeper of the ClickHouse test server - on 9182 of the host for `make test_clickhouse_up`, on the
#: port of its version for a server of the test matrix (HARE_TEST_CLICKHOUSE_KEEPER_HOSTS).
CLICKHOUSE_KEEPER_HOSTS = os.environ.get("HARE_TEST_CLICKHOUSE_KEEPER_HOSTS", "127.0.0.1:9182")
