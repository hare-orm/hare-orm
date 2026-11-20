# Installation

hare-orm installs from PyPI with pip; the drivers and integrations it doesn't need for SQLite come as
extras. Once it is installed, [Your first model](first-model.md) defines a model, creates its table and
runs the first queries.

Package metadata permits CPython `>=3.12,<3.16`; the current CI matrix covers 3.12, 3.13, 3.14 and 3.15.

```bash
pip install hare-orm
```

`hare.__version__` is the version installed.

Optional extras:

```bash
pip install hare-orm[asyncpg]        # PostgreSQL through asyncpg
pip install hare-orm[clickhouse-connect]  # the ClickHouse dialect over HTTP (clickhouse-connect)
pip install hare-orm[clickhouse-driver]   # the ClickHouse dialect over the native TCP protocol (clickhouse-driver)
pip install hare-orm[clickhouse-keeper]   # Keeper migration locks and node cleanup (Kazoo)
pip install hare-orm[sqlite-vec]     # vector search on SQLite (sqlite-vec)
pip install hare-orm[ipython]        # `hare shell` with IPython
pip install hare-orm[tzlocal]        # the machine's own zone for date/time functions on PostgreSQL and ClickHouse with use_timezone=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[phone]          # PhoneField reading a number in any form (phonenumbers)
pip install hare-orm[opentelemetry]  # an OpenTelemetry span around every query
pip install hare-orm[pydantic]       # JSONField(field_type=...) and hare.contrib.pydantic's schemas of models
pip install hare-orm[yaml]           # a .yml/.yaml config file (PyYAML)
pip install hare-orm[faker]          # Faker() declarations of hare.contrib.factories
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

Python 3.15 supports the core, asyncpg 0.32+, ClickHouse HTTP 1.12+, Pydantic 2.14+
and Kafka through aiokafka 0.15+. Robyn 0.88 and ClickHouse TCP (`clickhouse-driver`
and `clickhouse-cityhash`) currently need Python 3.12–3.14: their current dependencies
have no compatible 3.15 wheels. On 3.15 choose another web framework or ClickHouse HTTP.
The matrix tests available integrations there and these two on 3.14.

## <a id="supported-versions"></a>Supported versions

The full database matrix below runs on Linux. On macOS and Windows every release's
[wheels](#wheels) are installed and checked on SQLite and through the CLI.

| Component | Supported | Tested |
| --- | --- | --- |
| Python | CPython 3.12, 3.13, 3.14, 3.15 | each of them |
| PostgreSQL | 14 and newer | 14, 15, 16, 17 and 18, through `rust_pg` and through `asyncpg`, on Linux |
| PgBouncer | 1.22.1 and newer | 1.22, 1.23, 1.24, 1.25 and 1.26 in front of PostgreSQL 18, 1.26 also in front of 14, through both drivers, on Linux |
| SQLite | 3.35.5 and newer | 3.35.5, 3.37.2, 3.38.0, 3.41.0 and 3.53.4, on Linux |
| ClickHouse | 25.8 and newer | 25.8, 25.10, 25.12, 26.3, 26.7, 26.8 and 26.9, a server and a cluster, through `clickhouse-connect` and `clickhouse-driver`, on Linux |
| Redis (outbox) | 7.2 and newer | 7.2 and 8.10, on Linux |
| RabbitMQ (outbox) | 4.0 and newer | 4.0 and 4.3, on Linux |
| Kafka (outbox) | 3.9 and newer | 3.9 and 4.3, on Linux |

The whole matrix runs on every pull request into `dev`, every week and before a release — each version of
each row at least once.

A server older than the oldest supported version is refused when the connection opens. A feature that a
newer version brings (PostgreSQL `MERGE` from 15, SQLite `STRICT` tables from 3.37, ...) is turned on by
the version of the server the connection talks to — see [The server
version](../dialects/dialects-and-features.md#the-server-version).

The lowest versions of the dependencies that `pyproject.toml` declares are tested too, on each Python.
That environment selects the lower bounds of hare's direct dependencies and extras. Test tools come
from Poetry's locked export, retaining Python and platform markers; exporting runs in a separate
tool environment so its dependencies do not enter the environment being tested. These runs also
verify cached query plans with `--verify-plans`.

## <a id="wheels"></a>Wheels

The Rust PostgreSQL driver, the native attribute and value-format helpers and the compiled
[SQLite functions](../dialects/sqlite/functions.md#compiled-functions) (the `rust.native` extension) come
prebuilt in a wheel per Python version — cp312, cp313, cp314 and cp315 — for each platform. hare needs the
extension, so it is published as these wheels alone:

| Platform | Architectures | Wheel tag |
| --- | --- | --- |
| Linux, glibc 2.17 and newer | x86-64, ARM64 | `manylinux_2_17` (manylinux2014) |
| Linux, musl 1.2 and newer (Alpine) | x86-64, ARM64 | `musllinux_1_2` |
| macOS | x86-64, ARM64 | `macosx` |
| Windows | x86-64, ARM64 | `win_amd64`, `win_arm64` |

The Windows wheels carry no Rust PostgreSQL driver: PostgreSQL there needs the `asyncpg` extra and
`postgresql+asyncpg://`; a `postgresql://` connection raises `ConfigurationError`. See [PostgreSQL: the
drivers](../dialects/postgresql/connecting.md#choosing-a-postgres-driver).
