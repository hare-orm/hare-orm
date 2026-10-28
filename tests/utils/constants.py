#: Windows' ``SetProcessInformation()`` class of a process's power throttling state, and what of the
#: throttling is turned off: the execution speed (0x1) and ignoring the timer resolution (0x4).
POWER_THROTTLING_INFORMATION_CLASS = 4
POWER_THROTTLING_CONTROLS = 0x1 | 0x4
#: The marker of a test opening only SQLite databases of its own, whatever HARE_TEST_DB is - only the
#: suites of a SQLite database run it.
SQLITE_ONLY_MARKER = "sqlite_only"
#: The marker of a test of the models of tests.shared_models.testmodels_postgres - fields only
#: PostgreSQL's shared test database holds (a DecimalField): only its suites run it.
POSTGRESQL_MODELS_MARKER = "postgresql_models"
#: The marker of a test of exact decimals - a DecimalField or a Decimal value: only the suites of a
#: database storing them (not SQLite) run it.
DECIMAL_FIELDS_MARKER = "decimal_fields"
#: The marker of a test whose outcome depends on time - a timeout, a lease, a retry delay running out:
#: left out of every suite unless RUN_TIMING_OPTION asks for it.
TIMING_MARKER = "timing"
#: Runs the tests of TIMING_MARKER too.
RUN_TIMING_OPTION = "--run-timing"
#: The connection alias of a ServerSession - the bookkeeping session of the PostgreSQL server under test.
SERVER_SESSION_CONNECTION_ALIAS = "hare_test_server"
#: The requirement of the tests needing pydantic - its environment registers tests.shared_models.testmodels_pydantic.
PYDANTIC_REQUIREMENT = "pydantic"
#: The requirement of the tests needing phonenumbers - its environment registers the model of a PhoneField
#: with region=.
PHONE_REQUIREMENT = "phone"
#: The client classes of hare's PostgreSQL drivers, by dotted path - a test parametrized with
#: DriverClasses imports asyncpg's only in the environment installing the package.
RUST_PG_CLIENT_PATH = "hare.dialects.postgresql.drivers.rust_pg.client.RustPgClient"
RUST_PG_TRANSACTION_CLIENT_PATH = "hare.dialects.postgresql.drivers.rust_pg.client.RustPgTransactionClient"
ASYNCPG_CLIENT_PATH = "hare.dialects.postgresql.drivers.asyncpg.client.AsyncpgClient"
ASYNCPG_TRANSACTION_CLIENT_PATH = "hare.dialects.postgresql.drivers.asyncpg.client.AsyncpgTransactionClient"
POSTGRES_CLIENT_PATHS = (RUST_PG_CLIENT_PATH, ASYNCPG_CLIENT_PATH)
#: What each package of hare importing an optional library requires - the extras installing it; a package
#: inside one requires what the innermost one listed does.
PACKAGE_REQUIREMENTS = {
    "hare.dialects.postgresql.drivers.rust_pg.client": ("native",),
    "hare.dialects.postgresql.drivers.asyncpg.client": ("asyncpg",),
    "hare.dialects.postgresql.drivers.asyncpg.asyncpg_date_codec": ("asyncpg",),
    "hare.dialects.postgresql.drivers.asyncpg.asyncpg_extension_type_codecs": ("asyncpg",),
    "hare.dialects.postgresql.drivers.asyncpg.asyncpg_interval_codec": ("asyncpg",),
    "hare.dialects.postgresql.drivers.asyncpg.asyncpg_timestamp_codec": ("asyncpg",),
    "hare.dialects.postgresql.drivers.asyncpg.asyncpg_timestamptz_codec": ("asyncpg",),
    "hare.dialects.clickhouse.drivers.clickhouse_connect.client": ("clickhouse-connect",),
    "hare.dialects.clickhouse.drivers.clickhouse_driver.client": ("clickhouse-driver",),
    "hare.contrib.pydantic": ("pydantic",),
    "hare.contrib.request_query": ("request-query",),
    "hare.dialects.postgresql.request_query": ("request-query",),
    "hare.dialects.sqlite.request_query": ("request-query",),
    "hare.dialects.clickhouse.request_query": ("request-query",),
    "hare.contrib.frameworks": ("pydantic",),
    "hare.contrib.frameworks.litestar": ("litestar",),
    "hare.contrib.frameworks.fastapi": ("fastapi",),
    "hare.contrib.frameworks.robyn": ("robyn",),
    "hare.contrib.taskiq": ("taskiq",),
    "hare.contrib.opentelemetry": ("opentelemetry",),
    "hare.contrib.mypy": ("mypy",),
    "hare.contrib.pytest": ("pytest",),
}
