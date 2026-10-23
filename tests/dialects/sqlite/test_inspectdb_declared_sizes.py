"""inspectdb on SQLite, which keeps any declared type as written: a DECIMAL without a scale took the
default scale of 6 past its precision, sizes no field takes (DECIMAL(5, 10), NUMERIC(0), VARCHAR(0))
failed the whole inspection, a size written with spaces was missed, and a primary key column declared
without NOT NULL became a nullable key field the generated model couldn't be imported with."""

import pytest

from hare.contrib.test import hare_test_context
from hare.core.connections.connections import Connections
from hare.inspectdb import SchemaInspector

pytestmark = pytest.mark.sqlite_only

TABLES = {
    "numeric_of_precision": "CREATE TABLE numeric_of_precision (id INTEGER PRIMARY KEY, v NUMERIC(5))",
    "scale_past_precision": "CREATE TABLE scale_past_precision (id INTEGER PRIMARY KEY, v DECIMAL(5,10))",
    "numeric_of_no_digits": "CREATE TABLE numeric_of_no_digits (id INTEGER PRIMARY KEY, v NUMERIC(0))",
    "text_of_no_length": "CREATE TABLE text_of_no_length (id INTEGER PRIMARY KEY, v VARCHAR(0))",
    "spaced_size": "CREATE TABLE spaced_size (id INTEGER PRIMARY KEY, v DECIMAL ( 8 , 3 ))",
    "nullable_key": "CREATE TABLE nullable_key (a INTEGER, b TEXT, v INTEGER, PRIMARY KEY (a, b))",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("numeric_of_precision", "v = fields.DecimalField(max_digits=5, decimal_places=0, null=True)"),
        ("scale_past_precision", "v = fields.DecimalField(max_digits=20, decimal_places=6, null=True)"),
        ("numeric_of_no_digits", "v = fields.DecimalField(max_digits=20, decimal_places=6, null=True)"),
        ("text_of_no_length", "v = fields.TextField(null=True)"),
        ("spaced_size", "v = fields.DecimalField(max_digits=8, decimal_places=3, null=True)"),
        ("nullable_key", "a = fields.IntField()\n    b = fields.TextField()"),
    ],
)
async def test_a_declared_size_is_read_as_a_field_takes_it(table: str, expected: str) -> None:
    async with hare_test_context(
        ["tests.shared_models.testmodels"], db_url="sqlite+aiosqlite://:memory:", connection_label="models"
    ):
        connection = Connections.get("models")
        await connection.execute_script(TABLES[table])
        source = await SchemaInspector.inspect(connection, tables=[table])
        assert expected in source
        namespace: dict[str, object] = {}
        exec(source, namespace)  # noqa: S102 - the generated module imports
