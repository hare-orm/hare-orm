"""Schema introspection goes through the introspector of the connection's dialect."""

import types

import pytest

from hare.dialects.base.sql_dialect import SqlDialect
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.introspection import PostgresqlIntrospector
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.dialects.sqlite.introspection import SqliteIntrospector
from hare.inspectdb.introspector import SchemaIntrospector, UnsupportedDialectError
from hare.inspectdb.types import ColumnInfo


def test_builtin_dialects_have_their_own_introspectors():
    assert SQLITE_DIALECT.introspector_class is SqliteIntrospector
    assert POSTGRESQL_DIALECT.introspector_class is PostgresqlIntrospector
    assert SqlDialect().introspector_class is None


@pytest.mark.asyncio
async def test_a_dialect_without_an_introspector_is_rejected():
    connection = types.SimpleNamespace(dialect=SqlDialect())
    with pytest.raises(UnsupportedDialectError):
        await SchemaIntrospector.get_table_names(connection)


def test_type_comparison_follows_the_dialect():
    column = ColumnInfo(
        name="code",
        db_type="character varying",
        nullable=False,
        is_pk=False,
        is_unique=False,
        full_type="character varying(20)",
    )
    assert PostgresqlIntrospector.column_types_differ("VARCHAR(30)", column)
    assert not PostgresqlIntrospector.column_types_differ("VARCHAR(20)", column)
    assert not SqliteIntrospector.column_types_differ("VARCHAR(30)", column)
    assert not SchemaIntrospector.column_types_differ("INTEGER", column)
