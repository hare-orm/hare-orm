"""A connection checks the version of the server it connects to: a server older than its dialect
runs on is refused with a ConfigurationError before anything else is sent, and a feature that
depends on the version (PostgreSQL's NULLS [NOT] DISTINCT, 15+) follows the server's own."""

import sqlite3

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.client import SqliteClient
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import UnSupportedError
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


def test_a_server_older_than_the_dialect_runs_on_is_refused():
    with pytest.raises(UnSupportedError, match="postgresql server is version 13.9, older than 14"):
        POSTGRESQL_DIALECT.check_server_version((13, 9))
    POSTGRESQL_DIALECT.check_server_version((14, 0))
    with pytest.raises(UnSupportedError, match="sqlite server is version 3.34.1, older than 3.35.0"):
        SQLITE_DIALECT.check_server_version((3, 34, 1))
    SQLITE_DIALECT.check_server_version((3, 35, 0))


def test_features_follow_the_server_version():
    assert POSTGRESQL_DIALECT.get_server_version_features((14, 11)) == {
        "supports_nulls_distinct": False,
        "supports_partitioned_exclusion_constraints": False,
    }
    assert POSTGRESQL_DIALECT.get_server_version_features((15, 0)) == {
        "supports_nulls_distinct": True,
        "supports_partitioned_exclusion_constraints": False,
    }
    assert POSTGRESQL_DIALECT.get_server_version_features((17, 2)) == {
        "supports_nulls_distinct": True,
        "supports_partitioned_exclusion_constraints": True,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 45, 1)) == {}


@pytest.mark.asyncio
async def test_sqlite_library_version_is_the_server_version():
    client = SqliteClient(file_path=":memory:", connection_name="version_check")
    try:
        assert await client.get_server_version() == sqlite3.sqlite_version_info
        await client.create_connection(with_db=True)
        assert client._connection is not None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_too_old_sqlite_library_is_refused_on_connect(monkeypatch):
    async def old_version(self):
        return (3, 30, 0)

    monkeypatch.setattr(SqliteClient, "get_server_version", old_version)
    client = SqliteClient(file_path=":memory:", connection_name="version_check")
    with pytest.raises(UnSupportedError, match="older than 3.35.0"):
        await client.create_connection(with_db=True)
    assert client._connection is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgresql_features_follow_the_connected_server(db, monkeypatch):
    connection = Tournament.get_connection()
    server_version = await connection.get_server_version()
    assert server_version >= (14,)
    assert connection.features.supports_nulls_distinct is (server_version >= (15,))
    async with Transactions.atomic(connection.connection_name) as transaction:
        assert transaction.features is connection.features

    features = connection.features
    try:

        async def version_14(self):
            return 140011

        monkeypatch.setattr(type(connection), "_fetch_server_version_number", version_14)
        await connection._post_connect()
        assert connection.features.supports_nulls_distinct is False
        constraint = UniqueConstraint(fields=("name",), nulls_distinct=False)
        with pytest.raises(UnSupportedError, match="nulls_distinct is not supported by the postgresql server"):
            constraint.raise_if_unsupported(connection.features, connection.dialect)

        closed = []

        async def version_13(self):
            return 130015

        async def record_close():
            closed.append(True)

        monkeypatch.setattr(type(connection), "_fetch_server_version_number", version_13)
        monkeypatch.setattr(connection, "close", record_close)
        with pytest.raises(UnSupportedError, match="version 13.15, older than 14"):
            await connection._post_connect()
        assert closed == [True]
    finally:
        connection.features = features
