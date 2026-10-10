"""SQLite keeps a table's CREATE TABLE text as it was written, and takes a name in double quotes,
backticks or square brackets - its introspector reads every one of them back."""

import pytest

from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

QUOTED_TABLE_SQL = """
CREATE TABLE `stock` (
    `id` INTEGER PRIMARY KEY,
    [price] INTEGER NOT NULL,
    `qty` INTEGER NOT NULL,
    `total` INTEGER GENERATED ALWAYS AS ([price] * `qty`) STORED,
    CONSTRAINT `stock_qty_positive` CHECK (`qty` > 0),
    CONSTRAINT [stock_price_qty] UNIQUE ([price], `qty`)
);
CREATE INDEX `stock_total_expression` ON `stock` ((`total` + 1));
CREATE TRIGGER `stock_touch` AFTER UPDATE ON `stock` BEGIN SELECT 1; END;
"""


@pytest.mark.asyncio
async def test_names_in_every_quoting_are_read_back():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    await client.create_connection(with_db=True)
    try:
        await client.execute_script(QUOTED_TABLE_SQL)
        table = await DatabaseCatalog.inspect_table(client, "stock")
    finally:
        await client.close()
    total = next(column for column in table.columns if column.name == "total")
    assert total.generated_expression == "[price] * `qty`"
    assert [constraint.name for constraint in table.check_constraints] == ["stock_qty_positive"]
    assert [index.name for index in table.indexes if index.name == "stock_total_expression"]
    assert table.unparsed_indexes == []
    assert [trigger.name for trigger in table.triggers] == ["stock_touch"]
    assert "stock_price_qty" in {index.name for index in (*table.indexes, *table.column_indexes)}
