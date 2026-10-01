# Installation

```bash
pip install hare-orm
```

Optional extras:

```bash
pip install hare-orm[asyncpg]        # the pure-Python PostgreSQL driver, instead of the Rust one
pip install hare-orm[ipython]        # `hare shell` with IPython
pip install hare-orm[tzlocal]        # the machine's own zone for date/time functions on PostgreSQL with use_tz=False
pip install hare-orm[encryption]     # EncryptedTextField / EncryptedJSONField
pip install hare-orm[opentelemetry]  # an OpenTelemetry span around every query
pip install hare-orm[request-query]  # hare.contrib.request_query
pip install hare-orm[litestar]       # hare.contrib.frameworks.litestar
pip install hare-orm[fastapi]        # hare.contrib.frameworks.fastapi
pip install hare-orm[robyn]          # hare.contrib.frameworks.robyn
```

The Rust PostgreSQL driver (in the `rust.native` extension) comes prebuilt in the wheels for Python 3.14 on Linux
(x86-64 and ARM64, glibc 2.17+ and musl), macOS (Intel and Apple Silicon) and Windows (x64 and
ARM64). On any other platform pip installs the pure wheel, without it: SQLite and
`postgresql+asyncpg://` work there, and a `postgresql://` connection raises `ConfigurationError`.
See [Choosing a PostgreSQL driver](../connections/connections.md#choosing-a-postgres-driver).
