# Тестирование на ClickHouse

Тесты ClickHouse в hare идут против сервера ClickHouse в Docker:

```bash
make test_clickhouse_up      # ClickHouse 25.8 - HTTP на порту 8124, собственный протокол на 9124
make test_clickhouse         # tests/dialects/clickhouse на обоих драйверах
```

Сервер сам служит себе ClickHouse Keeper — порт 9182 на хосте — и работает с
`allow_experimental_transactions` (`tests/docker/clickhouse`): тесты транзакций подключаются с
`transactions=true`, тесты блокировок строк — ещё и с `keeper_hosts=127.0.0.1:9182`, а
генерируемые ключи берутся из его серий. `make test_clickhouse` запускает `tests/dialects/clickhouse/`
со своими моделями — общим тестовым моделям остального набора нужны ограничения уникальности и
внешние ключи, которых ClickHouse не держит.

Тесты кластера идут против двух отдельных серверов:

```bash
make test_clickhouse_cluster_up    # два сервера - HTTP на 8126 и 8127, собственный протокол на 9126 и 9127
make test_clickhouse_cluster       # tests/dialects/clickhouse/cluster на обоих драйверах
make test_clickhouse_cluster_down
```

Серверы образуют один шард из двух реплик (`hare_replicated`) и два шарда по одному серверу
(`hare_sharded`); первый сервер — их Keeper (`tests/docker/clickhouse_cluster`). Без
`HARE_TEST_CLICKHOUSE_CLUSTER_DB`, которую задаёт `make test_clickhouse_cluster`, тесты пропускаются.
