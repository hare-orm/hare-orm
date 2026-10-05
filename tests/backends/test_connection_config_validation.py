"""Connection credentials are validated for type and range when the client is built - a typo or
an out-of-range value raises ConfigurationError at configuration time, never a raw exception
from the driver on the first query."""

import os
import sqlite3

import pytest

from hare.contrib.test import requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.config import HareConfig
from hare.core.connections.connections import Connections
from hare.core.constants import ENV_PYTEST_XDIST_WORKER
from hare.core.hare_context import HareContext
from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgClient
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.exceptions import ConfigurationError, DBConnectionError, OperationalError

POSTGRES_CLIENT_CLASSES = [AsyncpgClient]
try:
    from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
except ImportError:  # pragma: nocoverage - the native extension is not built
    pass
else:
    POSTGRES_CLIENT_CLASSES.append(RustPgClient)


def _make_postgres_client(client_class, **credentials):
    return client_class(
        **{
            "user": "postgres",
            "password": "postgres",
            "database": "test",
            "host": "127.0.0.1",
            "connection_alias": "models",
            **credentials,
        }
    )


@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES)
@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({"port": "abc"}, "port must be a whole number"),
        ({"port": 0}, "port must be between 1 and 65535"),
        ({"port": -1}, "port must be between 1 and 65535"),
        ({"port": 70000}, "port must be between 1 and 65535"),
        ({"port": True}, "port must be a whole number"),
        ({"statement_cache_size": -1}, "statement_cache_size must be between 0"),
        ({"statement_cache_size": "many"}, "statement_cache_size must be a whole number"),
        ({"connect_max_retries": -5}, "connect_max_retries must be between 0 and 100"),
        ({"connect_max_retries": 10**9}, "connect_max_retries must be between 0 and 100"),
        ({"connect_max_retries": 1.5}, "connect_max_retries must be a whole number"),
        ({"read_retry_max_retries": -1}, "read_retry_max_retries must be between 0 and 100"),
        ({"connect_retry_backoff_base_seconds": "inf"}, "connect_retry_backoff_base_seconds must be at least 0"),
        ({"connect_retry_backoff_base_seconds": "nan"}, "connect_retry_backoff_base_seconds must be at least 0"),
        ({"connect_retry_backoff_base_seconds": -1}, "connect_retry_backoff_base_seconds must be at least 0"),
        ({"connect_retry_backoff_base_seconds": "soon"}, "connect_retry_backoff_base_seconds must be a number"),
        ({"read_retry_backoff_base_seconds": float("inf")}, "read_retry_backoff_base_seconds must be at least 0"),
        ({"read_retry_backoff_base_seconds": 10**6}, "read_retry_backoff_base_seconds must be at least 0"),
        ({"comand_timeout": 5}, "Unknown connection parameter\\(s\\) \\['comand_timeout'\\]"),
    ],
)
def test_invalid_postgres_credentials_are_rejected(client_class, credentials, message):
    with pytest.raises(ConfigurationError, match=message):
        _make_postgres_client(client_class, **credentials)


@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES)
def test_valid_postgres_credentials_are_normalized(client_class):
    client = _make_postgres_client(
        client_class,
        port="5433",
        connect_max_retries="3",
        connect_retry_backoff_base_seconds="0.5",
        read_retry_max_retries=2,
        read_retry_backoff_base_seconds=0,
        statement_cache_size="10",
    )
    assert client.port == 5433
    assert (client.connect_max_retries, client.connect_retry_backoff_base_seconds) == (3, 0.5)
    assert (client.read_retry_max_retries, client.read_retry_backoff_base_seconds) == (2, 0.0)
    assert client.extra["statement_cache_size"] == 10


@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({"max_queries": 0}, "max_queries must be between 1"),
        ({"max_inactive_connection_lifetime": -1}, "max_inactive_connection_lifetime must be at least 0"),
        ({"max_cached_statement_lifetime": "nan"}, "max_cached_statement_lifetime must be at least 0"),
        ({"max_cacheable_statement_size": -1}, "max_cacheable_statement_size must be between 0"),
        ({"timeout": -1}, "timeout must be greater than 0"),
        ({"timeout": 0}, "timeout must be greater than 0"),
    ],
)
def test_invalid_asyncpg_credentials_are_rejected(credentials, message):
    with pytest.raises(ConfigurationError, match=message):
        _make_postgres_client(AsyncpgClient, **credentials)


def test_asyncpg_accepts_its_own_connection_parameters():
    client = _make_postgres_client(
        AsyncpgClient, timeout="30", max_queries="100", ssl=True, max_inactive_connection_lifetime=0
    )
    assert (client.extra["timeout"], client.extra["max_queries"], client.extra["ssl"]) == (30.0, 100, True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "engine",
    [
        "postgresql",
        "postgresql",
        "postgresql+asyncpg",
    ],
)
async def test_dict_config_with_a_misspelled_postgres_parameter_fails_init(engine):
    """rust_pg (and the postgres dispatcher picking it) silently dropped a misspelled credential
    that the DB_URL form already rejected."""
    if engine != "postgresql+asyncpg" and len(POSTGRES_CLIENT_CLASSES) == 1:
        pytest.skip("the rust_pg native extension is not built")
    config = {
        "connections": {
            "default": {
                "engine": engine,
                "credentials": {"host": "127.0.0.1", "user": "postgres", "database": "db", "comand_timeout": 5},
            }
        },
        "apps": {"models": {"models": ["tests.testmodels"]}},
    }
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="comand_timeout"):
            await ctx.init(config=config)


class _RecordingPostgresClient:
    """Captures the statements db_create()/db_delete() would send."""

    @staticmethod
    def make(client_class, **credentials):
        client = _make_postgres_client(client_class, **credentials)
        client.sent_statements = []

        async def create_connection(with_db):
            client.connected_with_db = with_db

        async def execute_script(statement):
            client.sent_statements.append(statement)

        async def close():
            pass

        client.create_connection = create_connection
        client.execute_script = execute_script
        client.close = close
        return client


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES)
async def test_database_name_is_quoted_and_owner_is_omitted_without_a_user(client_class):
    client = _RecordingPostgresClient.make(client_class, database='my "db"', user=None)

    await client.db_create()
    await client.db_delete()

    assert client.sent_statements == ['CREATE DATABASE "my ""db"""', 'DROP DATABASE "my ""db"""']


@pytest.mark.asyncio
@pytest.mark.parametrize("client_class", POSTGRES_CLIENT_CLASSES)
async def test_database_owner_is_quoted(client_class):
    client = _RecordingPostgresClient.make(client_class, database="plain", user='odd"role')

    await client.db_create()

    assert client.sent_statements == ['CREATE DATABASE "plain" OWNER "odd""role"']


@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({}, "needs a non-empty file_path"),
        ({"file_path": ""}, "needs a non-empty file_path"),
        (
            {"file_path": ":memory:", "jurnal_mode": "WAL"},
            "Unknown connection parameter\\(s\\) \\['jurnal_mode'\\] for the sqlite driver",
        ),
        ({"file_path": ":memory:", "journal_mode": "BOGUS"}, "Invalid value 'BOGUS' for journal_mode"),
        (
            {"file_path": ":memory:", "journal_mode": "WAL;CREATE TABLE injected(x)"},
            "Invalid value .* for journal_mode",
        ),
        ({"file_path": ":memory:", "journal_size_limit": -5}, "journal_size_limit must be between -1"),
        ({"file_path": ":memory:", "journal_size_limit": "abc"}, "journal_size_limit must be a whole number"),
        ({"file_path": ":memory:", "busy_timeout": True}, "busy_timeout must be a whole number"),
        ({"file_path": ":memory:", "page_size": 1000}, "page_size must be a power of two"),
        ({"file_path": ":memory:", "foreign_keys": "maybe"}, "foreign_keys must be a boolean"),
        ({"file_path": ":memory:", "install_regexp_functions": "maybe"}, "install_regexp_functions must be a bool"),
    ],
)
def test_invalid_sqlite_credentials_are_rejected(credentials, message):
    with pytest.raises(ConfigurationError, match=message):
        AiosqliteClient(connection_alias="default", **credentials)


def test_valid_sqlite_pragmas_are_normalized():
    client = AiosqliteClient(
        file_path=":memory:",
        connection_alias="default",
        journal_mode="delete",
        foreign_keys=False,
        case_sensitive_like="off",
        busy_timeout="5000",
        synchronous="normal",
    )
    assert client.pragmas == {
        "journal_mode": "DELETE",
        "journal_size_limit": 16384,
        "foreign_keys": "OFF",
        "case_sensitive_like": "OFF",
        "busy_timeout": 5000,
        "synchronous": "NORMAL",
        **({"automatic_index": "OFF"} if AiosqliteClient.has_automatic_index_collation_fault else {}),
    }


@pytest.mark.asyncio
async def test_sqlite_url_pragmas_reach_the_connection(tmp_path):
    database_path = (tmp_path / "pragmas.db").as_posix()
    url = f"sqlite+aiosqlite://{database_path}?journal_mode=DELETE&foreign_keys=off&busy_timeout=1234"
    async with HareContext() as ctx:
        await ctx.init(HareConfig.from_db_url(url, {"models": ["tests.testmodels"]}))
        rows = [
            await ctx.get_connection().execute_dicts(f"PRAGMA {name}") for name in ("journal_mode", "foreign_keys")
        ]
        busy_timeout_rows = await ctx.get_connection().execute_dicts("PRAGMA busy_timeout")
    assert rows == [[{"journal_mode": "delete"}], [{"foreign_keys": 0}]]
    assert list(busy_timeout_rows[0].values()) == [1234]


@pytest.mark.asyncio
async def test_sqlite_url_pragma_injection_is_refused_before_anything_runs(tmp_path):
    database_path = tmp_path / "injection.db"
    url = f"sqlite+aiosqlite://{database_path.as_posix()}?journal_mode=WAL%3BCREATE%20TABLE%20injected(x)"
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="for journal_mode"):
            await ctx.init(HareConfig.from_db_url(url, {"models": ["tests.testmodels"]}))
    assert not database_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["maybe", "2"])
async def test_sqlite_url_rejects_a_non_boolean_install_regexp_functions(value):
    async with HareContext() as ctx:
        with pytest.raises(ConfigurationError, match="install_regexp_functions"):
            await ctx.init(
                HareConfig.from_db_url(
                    f"sqlite+aiosqlite://:memory:?install_regexp_functions={value}", {"models": ["tests.testmodels"]}
                )
            )


@pytest.mark.asyncio
async def test_test_context_refuses_an_existing_sqlite_file_and_leaves_it_untouched(tmp_path, monkeypatch):
    """hare_test_context() (like Hare.init(_create_db=True)) used to silently reuse an existing
    SQLite file and then delete it on exit - data included."""
    monkeypatch.delenv(ENV_PYTEST_XDIST_WORKER, raising=False)
    database_path = tmp_path / "prod.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE important (x)")
        connection.execute("INSERT INTO important VALUES (42)")
    connection.close()

    with pytest.raises(OperationalError, match="already exists"):
        async with hare_test_context(["tests.testmodels"], db_url=f"sqlite+aiosqlite://{database_path.as_posix()}"):
            pass

    assert os.path.exists(database_path)
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT x FROM important").fetchall() == [(42,)]
    connection.close()


@pytest.mark.asyncio
async def test_test_context_creates_and_removes_a_new_sqlite_file(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_PYTEST_XDIST_WORKER, raising=False)
    database_path = tmp_path / "fresh.db"
    async with hare_test_context(["tests.testmodels"], db_url=f"sqlite+aiosqlite://{database_path.as_posix()}"):
        assert database_path.exists()
    assert not database_path.exists()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"password": "hare_orm_definitely_wrong_password"}, "Authentication failed"),
        ({"user": "hare_orm_definitely_missing_role"}, "Authentication failed"),
        ({"database": "hare_orm_definitely_missing_database"}, 'database "hare_orm_definitely_missing_database"'),
    ],
)
async def test_connection_failures_no_retry_can_fix_are_configuration_errors(db_simple, override, message):
    """A wrong password, an unknown role or a missing database never succeed on a retry - both
    drivers raise ConfigurationError carrying the server's own reason (rust_pg used to raise a
    retryable DBConnectionError with only "Can't establish connection to database X")."""
    shared = Connections.get("models")
    credentials = {
        "connection_alias": "unfixable_connection_failure_test",
        "user": shared.user,
        "password": shared.password,
        "database": shared.database,
        "host": shared.host,
        "port": shared.port,
        "transaction_pooling": shared.transaction_pooling,
        **override,
    }
    client = type(shared)(**credentials)
    try:
        with pytest.raises(ConfigurationError, match=message):
            await client.create_connection(with_db=True)
    finally:
        await client.close()


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_unreachable_server_error_carries_the_driver_reason(db_simple):
    shared = Connections.get("models")
    client = type(shared)(
        connection_alias="unreachable_server_test",
        user=shared.user,
        password=shared.password,
        database=shared.database,
        host=shared.host,
        port=1,
    )
    expected_message = f"Can't establish connection to database {shared.database}. Exception: "
    with pytest.raises(DBConnectionError, match=expected_message):
        await client.create_connection(with_db=True)
