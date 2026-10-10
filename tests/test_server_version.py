"""A connection checks the version of the server it connects to: a server older than its dialect
runs on is refused with a ConfigurationError before anything else is sent, and a feature that
depends on the version (PostgreSQL's NULLS [NOT] DISTINCT, 15+) follows the server's own."""

import sqlite3

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.dialects.postgresql.clauses.constants import POSTGRESQL_EXPLAIN_OPTIONS
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.exceptions import UnSupportedError
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament


def test_a_server_older_than_the_dialect_runs_on_is_refused():
    with pytest.raises(UnSupportedError, match="postgresql server is version 13.9, older than 14"):
        POSTGRESQL_DIALECT.check_server_version((13, 9))
    POSTGRESQL_DIALECT.check_server_version((14, 0))
    with pytest.raises(UnSupportedError, match="sqlite server is version 3.35.4, older than 3.35.5"):
        SQLITE_DIALECT.check_server_version((3, 35, 4))
    SQLITE_DIALECT.check_server_version((3, 35, 5))


#: The features PostgreSQL 18 brings, as an older server has them.
POSTGRESQL_18_FEATURES_ABSENT = {
    "supports_virtual_generated_columns": False,
    "supports_uuid_v7": False,
    "supports_without_overlaps": False,
    "supports_returning_old_new": False,
}


def test_features_follow_the_server_version():
    explain_options_of_15 = POSTGRESQL_EXPLAIN_OPTIONS - {"GENERIC_PLAN", "MEMORY", "SERIALIZE"}
    assert POSTGRESQL_DIALECT.get_server_version_features((14, 11)) == {
        "supports_nulls_distinct": False,
        "supports_partitioned_exclusion_constraints": False,
        "supports_merge": False,
        "supports_merge_returning": False,
        "supports_merge_not_matched_by_source": False,
        **POSTGRESQL_18_FEATURES_ABSENT,
        "supports_json_table": False,
        "explain_options": explain_options_of_15,
    }
    assert POSTGRESQL_DIALECT.get_server_version_features((15, 0)) == {
        "supports_nulls_distinct": True,
        "supports_partitioned_exclusion_constraints": False,
        "supports_merge": True,
        "supports_merge_returning": False,
        "supports_merge_not_matched_by_source": False,
        **POSTGRESQL_18_FEATURES_ABSENT,
        "supports_json_table": False,
        "explain_options": explain_options_of_15,
    }
    assert POSTGRESQL_DIALECT.get_server_version_features((16, 4))["supports_merge_returning"] is False
    assert POSTGRESQL_DIALECT.get_server_version_features((16, 4))["explain_options"] == (
        explain_options_of_15 | {"GENERIC_PLAN"}
    )
    assert POSTGRESQL_DIALECT.get_server_version_features((17, 2)) == {
        "supports_nulls_distinct": True,
        "supports_partitioned_exclusion_constraints": True,
        "supports_merge": True,
        "supports_merge_returning": True,
        "supports_merge_not_matched_by_source": True,
        **POSTGRESQL_18_FEATURES_ABSENT,
        "supports_json_table": True,
        "explain_options": POSTGRESQL_EXPLAIN_OPTIONS,
    }
    assert POSTGRESQL_DIALECT.get_server_version_features((18, 0)) == {
        "supports_nulls_distinct": True,
        "supports_partitioned_exclusion_constraints": True,
        "supports_merge": True,
        "supports_merge_returning": True,
        "supports_merge_not_matched_by_source": True,
        **dict.fromkeys(POSTGRESQL_18_FEATURES_ABSENT, True),
        "supports_json_table": True,
        "explain_options": POSTGRESQL_EXPLAIN_OPTIONS,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 35, 4)) == {
        "supports_unhex": False,
        "supports_drop_column": False,
        "supports_strict_tables": False,
        "supports_ordered_aggregates": False,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 35, 5)) == {
        "supports_unhex": False,
        "supports_drop_column": True,
        "supports_strict_tables": False,
        "supports_ordered_aggregates": False,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 37, 0)) == {
        "supports_unhex": False,
        "supports_drop_column": True,
        "supports_strict_tables": True,
        "supports_ordered_aggregates": False,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 40, 1)) == {
        "supports_unhex": False,
        "supports_drop_column": True,
        "supports_strict_tables": True,
        "supports_ordered_aggregates": False,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 41, 0)) == {
        "supports_unhex": True,
        "supports_drop_column": True,
        "supports_strict_tables": True,
        "supports_ordered_aggregates": False,
    }
    assert SQLITE_DIALECT.get_server_version_features((3, 44, 0)) == {
        "supports_unhex": True,
        "supports_drop_column": True,
        "supports_strict_tables": True,
        "supports_ordered_aggregates": True,
    }


def test_the_sqlite_driver_knows_whether_its_library_has_unhex():
    assert AiosqliteClient.features.supports_unhex is (sqlite3.sqlite_version_info >= (3, 41, 0))


def test_the_sqlite_driver_knows_whether_its_library_has_strict_tables():
    assert AiosqliteClient.features.supports_strict_tables is (sqlite3.sqlite_version_info >= (3, 37, 0))


def test_the_sqlite_driver_knows_whether_its_library_drops_columns():
    assert AiosqliteClient.features.supports_drop_column is (sqlite3.sqlite_version_info >= (3, 35, 5))


@pytest.mark.asyncio
async def test_sqlite_library_version_is_the_server_version():
    client = AiosqliteClient(file_path=":memory:", connection_alias="version_check")
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

    monkeypatch.setattr(AiosqliteClient, "get_server_version", old_version)
    client = AiosqliteClient(file_path=":memory:", connection_alias="version_check")
    with pytest.raises(UnSupportedError, match="older than 3.35.5"):
        await client.create_connection(with_db=True)
    assert client._connection is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgresql_features_follow_the_connected_server(db, monkeypatch):
    connection = Tournament.get_connection()
    server_version = await connection.get_server_version()
    assert server_version >= (14,)
    assert connection.features.supports_nulls_distinct is (server_version >= (15,))
    async with Transactions.atomic(connection.connection_alias) as transaction:
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
