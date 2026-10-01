# Установка

```bash
pip install hare-orm
```

Дополнительные пакеты ставятся по необходимости:

```bash
pip install hare-orm[asyncpg]        # драйвер PostgreSQL на Python вместо драйвера на Rust
pip install hare-orm[ipython]        # `hare shell` с IPython
pip install hare-orm[tzlocal]        # часовой пояс машины для функций даты и времени на PostgreSQL при use_tz=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[opentelemetry]  # трассировка каждого запроса через OpenTelemetry
pip install hare-orm[request-query]  # hare.contrib.request_query
pip install hare-orm[litestar]       # hare.contrib.frameworks.litestar
pip install hare-orm[fastapi]        # hare.contrib.frameworks.fastapi
pip install hare-orm[robyn]          # hare.contrib.frameworks.robyn
```

Драйвер PostgreSQL на Rust (в расширении `rust.native`) поставляется уже собранным в wheel для Python 3.14 под
Linux (x86-64 и ARM64, glibc 2.17+ и musl), macOS (Intel и Apple Silicon) и Windows (x64 и ARM64).
На других платформах pip ставит универсальный wheel без него: там работают SQLite и
`postgresql+asyncpg://`, а подключение `postgresql://` даёт `ConfigurationError`. См.
[Выбор драйвера PostgreSQL](../connections/connections.ru.md#choosing-a-postgres-driver).
