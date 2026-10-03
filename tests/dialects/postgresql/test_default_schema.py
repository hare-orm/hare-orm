"""A table without a schema of its own lives in the connection's default schema - current_schema(),
the first existing schema on the search_path - which isn't always "public"."""

import pytest

from hare.contrib.test import requires_features
from hare.inspectdb import SchemaInspector, SchemaIntrospector
from hare.transactions.transactions import Transactions
from tests.testmodels import Tournament

OTHER_SCHEMA = "hare_default_schema_check"


class Rollback(Exception):
    pass


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_schema_other_than_public_as_the_default(db):
    alias = Tournament._meta.default_connection
    with pytest.raises(Rollback):
        async with Transactions.atomic(alias) as connection:
            await connection.execute_script(
                f"CREATE SCHEMA {OTHER_SCHEMA}; SET LOCAL search_path TO {OTHER_SCHEMA}, public; "
                "CREATE TABLE gadget (id INTEGER PRIMARY KEY, code VARCHAR(10) CONSTRAINT gadget_code_key UNIQUE)"
            )
            editor = connection.dialect.schema_editor_class(connection)

            # The constraint is found in the default schema, not looked for in "public".
            assert await editor._get_unique_constraint_names_from_db("gadget", ["code"]) == ["gadget_code_key"]

            assert "gadget" in await SchemaIntrospector.get_table_names(connection)
            (gadget,) = await SchemaIntrospector.inspect_tables(connection, ["gadget"])
            assert gadget.schema == OTHER_SCHEMA
            assert gadget.is_in_default_schema is True
            assert "schema = " not in await SchemaInspector.inspect(connection, ["gadget"])

            # "public" isn't the default schema here - its table keeps its schema.
            (tournament,) = await SchemaIntrospector.inspect_tables(connection, ["tournament"], schema="public")
            assert tournament.is_in_default_schema is False
            assert "schema = 'public'" in await SchemaInspector.inspect(connection, ["tournament"], schema="public")
            raise Rollback
