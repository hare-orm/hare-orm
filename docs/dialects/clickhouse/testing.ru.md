# ClickHouse: тестирование

Собственные тесты ClickHouse в hare идут против серверов ClickHouse в Docker.

## <a id="one-server"></a>Один сервер

Тесты одного сервера:

```bash
make test_clickhouse_up      # ClickHouse 25.8 - HTTP на порту 8124, собственный протокол на 9124
make test_clickhouse         # tests/dialects/clickhouse на обоих драйверах
```

Сервер сам служит себе ClickHouse Keeper — порт 9182 на хосте — и работает с
`allow_experimental_transactions` (`tests/docker/clickhouse`): тесты транзакций подключаются с
`transactions=true`, тесты блокировки миграций — ещё и с `keeper_hosts=127.0.0.1:9182`, а генерируемые ключи
берутся из его серий. `make test_clickhouse` запускает `tests/dialects/clickhouse/` со своими моделями —
общим тестовым моделям остального набора нужны ограничения уникальности и внешние ключи, которых
ClickHouse не держит.

## <a id="cluster"></a>Кластер

Тесты кластера идут против двух отдельных серверов:

```bash
make test_clickhouse_cluster_up    # два сервера - HTTP на 8126 и 8127, собственный протокол на 9126 и 9127
make test_clickhouse_cluster       # tests/dialects/clickhouse/cluster на обоих драйверах
make test_clickhouse_cluster_down
```

Серверы образуют один шард из двух реплик (`hare_replicated`) и два шарда по одному серверу
(`hare_sharded`); первый сервер — их Keeper (`tests/docker/clickhouse_cluster`). Без
`HARE_TEST_CLICKHOUSE_CLUSTER_DB`, которую задаёт `make test_clickhouse_cluster`, тесты пропускаются.

## <a id="every-version"></a>Каждая версия

Матрица тестов запускает и то и другое на каждой версии ClickHouse начиная с 25.8 — сервер и кластер на
версию, каждый на портах из цифр версии (`tests/matrix/matrix_ports.py`):

```bash
make test_clickhouse_matrix_up test_clickhouse_cluster_matrix_up matrix_clickhouse_versions=26.3
make test_matrix MATRIX_OPTIONS="--python 3.14 --suite 'clickhouse-*-26.3'"
make test_clickhouse_matrix_down test_clickhouse_cluster_matrix_down matrix_clickhouse_versions=26.3
```

Сервер 26.3 слушает 32603 (HTTP), 42603 (собственный протокол) и 62603 (его Keeper), первый узел его
кластера — 52603 и 22603. Тесты блокировки миграций и генерируемых ключей матрица направляет на Keeper
версии через `HARE_TEST_CLICKHOUSE_KEEPER_HOSTS` (без неё — `127.0.0.1:9182`).
