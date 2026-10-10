# Установка

hare-orm ставится из PyPI через pip; драйверы и интеграции, которые не нужны для SQLite, ставятся
дополнительными пакетами.

```bash
pip install hare-orm
```

Дополнительные пакеты ставятся по необходимости:

```bash
pip install hare-orm[asyncpg]        # драйвер PostgreSQL на Python вместо драйвера на Rust
pip install hare-orm[clickhouse]     # диалект ClickHouse по HTTP (clickhouse-connect)
pip install hare-orm[clickhouse-driver]  # диалект ClickHouse по родному протоколу поверх TCP (clickhouse-driver)
pip install hare-orm[sqlite-vec]     # поиск по векторам на SQLite (sqlite-vec)
pip install hare-orm[ipython]        # `hare shell` с IPython
pip install hare-orm[tzlocal]        # часовой пояс машины для функций даты и времени на PostgreSQL при use_timezone=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[phone]          # PhoneField читает номер в любом виде (phonenumbers)
pip install hare-orm[opentelemetry]  # трассировка каждого запроса через OpenTelemetry
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

## <a id="supported-versions"></a>Поддерживаемые версии

| Компонент | Поддерживается | Проверяется |
| --- | --- | --- |
| Python | CPython 3.12, 3.13, 3.14 | каждая из них |
| PostgreSQL | 14 и новее | 14, 15, 16, 17 и 18, через драйвер на Rust и через `asyncpg`, на Linux |
| SQLite | 3.35.5 и новее | 3.35.5, 3.37.2, 3.38.0, 3.41.0 и 3.53.4, на Linux, Windows и macOS |
| ClickHouse | 24.3 и новее | 25.8, на Linux |

Вся матрица проверяется на каждом pull request в `main`, каждую ночь и перед выпуском; push в `dev`
или `main` проверяет самые новые Python, PostgreSQL и SQLite из неё.

Сервер старше самой старой поддерживаемой версии отклоняется при открытии соединения. Возможность,
которая появилась в более новой версии (`MERGE` в PostgreSQL с 15, таблицы `STRICT` в SQLite с
3.37, ...), включается по версии сервера, с которым работает подключение, — см.
[Версия сервера](../dialects/dialects-and-features.ru.md#the-server-version).

Нижние версии зависимостей, объявленные в `pyproject.toml`, тоже проверяются — на Python 3.12.

## <a id="wheels"></a>Wheel-пакеты

Драйвер PostgreSQL на Rust и чтение и запись строк на Rust (расширение `rust.native`) поставляются
уже собранными — отдельный wheel на каждую версию Python (cp312, cp313 и cp314) для каждой
платформы:

| Платформа | Архитектуры | Тег wheel |
| --- | --- | --- |
| Linux, glibc 2.17 и новее | x86-64, ARM64 | `manylinux_2_17` (manylinux2014) |
| Linux, musl 1.2 и новее (Alpine) | x86-64, ARM64 | `musllinux_1_2` |
| macOS | Intel (x86-64), Apple Silicon (ARM64) | `macosx` |
| Windows | x64, ARM64 | `win_amd64`, `win_arm64` |

На других платформах pip ставит универсальный wheel (`py3-none-any`) без расширения: там работают
SQLite и `postgresql+asyncpg://`, а подключение `postgresql://` даёт `ConfigurationError`. См.
[Выбор драйвера PostgreSQL](../connections/connections.ru.md#choosing-a-postgres-driver).
