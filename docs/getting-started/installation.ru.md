# Установка

hare-orm ставится из PyPI через pip; драйверы и интеграции, которые не нужны для SQLite, ставятся
дополнительными пакетами. После установки [Первая модель](first-model.ru.md) описывает модель, создаёт её
таблицу и выполняет первые запросы.

Метаданные пакета допускают CPython `>=3.12,<3.16`; текущая матрица CI охватывает 3.12, 3.13, 3.14 и 3.15.

```bash
pip install hare-orm
```

`hare.__version__` — установленная версия.

Дополнительные пакеты ставятся по необходимости:

```bash
pip install hare-orm[asyncpg]        # PostgreSQL через asyncpg
pip install hare-orm[clickhouse-connect]  # диалект ClickHouse по HTTP (clickhouse-connect)
pip install hare-orm[clickhouse-driver]   # диалект ClickHouse по родному протоколу поверх TCP (clickhouse-driver)
pip install hare-orm[clickhouse-keeper]   # блокировки миграций и очистка узлов Keeper (Kazoo)
pip install hare-orm[sqlite-vec]     # поиск по векторам на SQLite (sqlite-vec)
pip install hare-orm[ipython]        # `hare shell` с IPython
pip install hare-orm[tzlocal]        # часовой пояс машины для функций даты и времени на PostgreSQL и ClickHouse при use_timezone=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[phone]          # PhoneField читает номер в любом виде (phonenumbers)
pip install hare-orm[opentelemetry]  # трассировка каждого запроса через OpenTelemetry
pip install hare-orm[pydantic]       # JSONField(field_type=...) и схемы моделей hare.contrib.pydantic
pip install hare-orm[yaml]           # файл настроек .yml/.yaml (PyYAML)
pip install hare-orm[faker]          # объявления Faker() в hare.contrib.factories
pip install hare-orm[request-query]  # hare.contrib.request_query
pip install hare-orm[litestar]       # hare.contrib.frameworks.litestar
pip install hare-orm[fastapi]        # hare.contrib.frameworks.fastapi
pip install hare-orm[robyn]          # hare.contrib.frameworks.robyn
pip install hare-orm[taskiq]         # hare.contrib.taskiq
pip install hare-orm[redis]          # RedisWakeup и RedisStreamsDelivery очереди исходящих событий
pip install hare-orm[kafka]          # KafkaWakeup и KafkaDelivery очереди исходящих событий
pip install hare-orm[rabbitmq]       # RabbitMQWakeup и RabbitMQDelivery очереди исходящих событий
pip install hare-orm[http]           # WebhookDelivery очереди исходящих событий
pip install hare-orm[mypy]           # плагин mypy, проверяющий запросы (hare.contrib.mypy)
pip install hare-orm[pyright]        # `hare stubs` — заглушки моделей для pyright и Pylance
pip install hare-orm[pytest]         # плагин pytest для hare (pytest, pytest-asyncio)
```

Python 3.15 поддерживает ядро, asyncpg 0.32+, ClickHouse HTTP 1.12+, Pydantic 2.14+
и Kafka через aiokafka 0.15+. Robyn 0.88 и ClickHouse TCP (`clickhouse-driver` и
`clickhouse-cityhash`) пока требуют Python 3.12–3.14: их текущие зависимости не имеют
совместимых wheel-пакетов для 3.15. На 3.15 выбирайте другой веб-фреймворк или
ClickHouse HTTP. Матрица проверяет доступные интеграции, а эти две — на 3.14.

## <a id="supported-versions"></a>Поддерживаемые версии

Полная матрица баз данных ниже проверяется на Linux. На macOS и Windows [wheel-пакеты](#wheels)
каждого релиза устанавливаются и проверяются на SQLite и через CLI.

| Компонент | Поддерживается | Проверяется |
| --- | --- | --- |
| Python | CPython 3.12, 3.13, 3.14, 3.15 | каждая из них |
| PostgreSQL | 14 и новее | 14, 15, 16, 17 и 18, через `rust_pg` и через `asyncpg`, на Linux |
| PgBouncer | 1.22.1 и новее | 1.22, 1.23, 1.24, 1.25 и 1.26 перед PostgreSQL 18, 1.26 ещё и перед 14, через оба драйвера, на Linux |
| SQLite | 3.35.5 и новее | 3.35.5, 3.37.2, 3.38.0, 3.41.0 и 3.53.4, на Linux |
| ClickHouse | 25.8 и новее | 25.8, 25.10, 25.12, 26.3, 26.7, 26.8 и 26.9, сервер и кластер, через `clickhouse-connect` и `clickhouse-driver`, на Linux |
| Redis (outbox) | 7.2 и новее | 7.2 и 8.10, на Linux |
| RabbitMQ (outbox) | 4.0 и новее | 4.0 и 4.3, на Linux |
| Kafka (outbox) | 3.9 и новее | 3.9 и 4.3, на Linux |

Вся матрица проверяется на каждом pull request в `dev`, каждую неделю и перед выпуском — каждая версия
каждой строки хотя бы раз.

Сервер старше самой старой поддерживаемой версии отклоняется при открытии соединения. Возможность,
которая появилась в более новой версии (`MERGE` в PostgreSQL с 15, таблицы `STRICT` в SQLite с 3.37,
...), включается по версии сервера, с которым работает подключение, — см. [Версия
сервера](../dialects/dialects-and-features.ru.md#the-server-version).

Нижние версии зависимостей, объявленные в `pyproject.toml`, тоже проверяются — на каждом Python.
Это окружение выбирает нижние границы прямых зависимостей hare и extras. Тестовые инструменты берутся
из экспорта lock-файла Poetry с сохранением маркеров Python и платформы; экспорт выполняется в отдельном
окружении инструментов, чтобы его зависимости не попадали в проверяемое окружение. Эти прогоны также
проверяют кэшированные планы запросов с `--verify-plans`.

## <a id="wheels"></a>Wheel-пакеты

Драйвер PostgreSQL на Rust, нативные операции с атрибутами и форматами значений и [скомпилированные функции
SQLite](../dialects/sqlite/functions.ru.md#compiled-functions) (расширение `rust.native`) поставляются
уже собранными — отдельный wheel на каждую версию Python (cp312, cp313, cp314 и cp315) для каждой платформы.
Без расширения hare не работает, поэтому публикуются только эти wheel-пакеты:

| Платформа | Архитектуры | Тег wheel |
| --- | --- | --- |
| Linux, glibc 2.17 и новее | x86-64, ARM64 | `manylinux_2_17` (manylinux2014) |
| Linux, musl 1.2 и новее (Alpine) | x86-64, ARM64 | `musllinux_1_2` |
| macOS | x86-64, ARM64 | `macosx` |
| Windows | x86-64, ARM64 | `win_amd64`, `win_arm64` |

В wheel-пакетах для Windows нет драйвера PostgreSQL на Rust: там для PostgreSQL нужны extra `asyncpg`
и `postgresql+asyncpg://`; подключение `postgresql://` даёт `ConfigurationError`. См. [PostgreSQL:
драйверы](../dialects/postgresql/connecting.ru.md#choosing-a-postgres-driver).
