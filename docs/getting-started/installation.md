# Installation

hare-orm installs from PyPI with pip; the drivers and integrations it doesn't need for SQLite come as
extras.

```bash
pip install hare-orm
```

Optional extras:

```bash
pip install hare-orm[asyncpg]        # the pure-Python PostgreSQL driver, instead of the Rust one
pip install hare-orm[clickhouse]     # the ClickHouse dialect over HTTP (clickhouse-connect)
pip install hare-orm[clickhouse-driver]  # the ClickHouse dialect over the native TCP protocol (clickhouse-driver)
pip install hare-orm[sqlite-vec]     # vector search on SQLite (sqlite-vec)
pip install hare-orm[ipython]        # `hare shell` with IPython
pip install hare-orm[tzlocal]        # the machine's own zone for date/time functions on PostgreSQL with use_timezone=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[phone]          # PhoneField reading a number in any form (phonenumbers)
pip install hare-orm[opentelemetry]  # an OpenTelemetry span around every query
pip install hare-orm[request-query]  # hare.contrib.request_query
pip install hare-orm[litestar]       # hare.contrib.frameworks.litestar
pip install hare-orm[fastapi]        # hare.contrib.frameworks.fastapi
pip install hare-orm[robyn]          # hare.contrib.frameworks.robyn
pip install hare-orm[taskiq]         # hare.contrib.taskiq
pip install hare-orm[redis]          # the outbox's RedisWakeup and RedisStreamsDelivery
pip install hare-orm[kafka]          # the outbox's KafkaWakeup and KafkaDelivery
pip install hare-orm[rabbitmq]       # the outbox's RabbitMQWakeup and RabbitMQDelivery
pip install hare-orm[http]           # the outbox's WebhookDelivery
pip install hare-orm[mypy]           # the mypy plugin checking queries (hare.contrib.mypy)
pip install hare-orm[pyright]        # `hare stubs` - stubs of the models for pyright and Pylance
pip install hare-orm[pytest]         # hare's pytest plugin (pytest, pytest-asyncio)
```

## <a id="supported-versions"></a>Supported versions

| Component | Supported | Tested |
| --- | --- | --- |
| Python | CPython 3.12, 3.13, 3.14 | each of them |
| PostgreSQL | 14 and newer | 14, 15, 16, 17 and 18, through the Rust driver and through `asyncpg`, on Linux |
| SQLite | 3.35.5 and newer | 3.35.5, 3.37.2, 3.38.0, 3.41.0 and 3.53.4, on Linux, Windows and macOS |
| ClickHouse | 24.3 and newer | 25.8, on Linux |

The whole matrix runs on every pull request into `main`, every night and before a release; a push
to `dev` or `main` runs the newest Python, PostgreSQL and SQLite of it.

A server older than the oldest supported version is refused when the connection opens. A feature
that a newer version brings (PostgreSQL `MERGE` from 15, SQLite `STRICT` tables from 3.37, ...) is
turned on by the version of the server the connection talks to — see
[The server version](../dialects/dialects-and-features.md#the-server-version).

The lowest versions of the dependencies that `pyproject.toml` declares are tested too, on Python
3.12.

## <a id="wheels"></a>Wheels

The Rust PostgreSQL driver and the Rust row readers/writers (the `rust.native` extension) come
prebuilt in a wheel per Python version — cp312, cp313 and cp314 — for each platform:

| Platform | Architectures | Wheel tag |
| --- | --- | --- |
| Linux, glibc 2.17 and newer | x86-64, ARM64 | `manylinux_2_17` (manylinux2014) |
| Linux, musl 1.2 and newer (Alpine) | x86-64, ARM64 | `musllinux_1_2` |
| macOS | Intel (x86-64), Apple Silicon (ARM64) | `macosx` |
| Windows | x64, ARM64 | `win_amd64`, `win_arm64` |

On any other platform pip installs the pure wheel (`py3-none-any`), without the extension: SQLite
and `postgresql+asyncpg://` work there, and a `postgresql://` connection raises
`ConfigurationError`. See
[Choosing a PostgreSQL driver](../connections/connections.md#choosing-a-postgres-driver).
