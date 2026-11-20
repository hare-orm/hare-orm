"""SQLite keeps a table's CREATE TABLE text as it was written, and takes a name in double quotes,
backticks or square brackets - its introspector reads every one of them back."""

import pytest

from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.dialects.sqlite.drivers.aiosqlite.client import AiosqliteClient
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.inspectdb import SchemaInspector
from hare.inspectdb.enums import CatalogDiagnosticCode
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog

pytestmark = pytest.mark.sqlite_only

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
    assert table.get_diagnostics(CatalogDiagnosticCode.INDEX) == []
    assert [trigger.name for trigger in table.triggers] == ["stock_touch"]
    assert "stock_price_qty" in {index.name for index in (*table.indexes, *table.column_indexes)}


@pytest.mark.asyncio
async def test_catalog_retains_complete_expression_keys():
    client = AiosqliteClient(file_path=":memory:", connection_alias="default")
    await client.create_connection(with_db=True)
    try:
        await client.execute_script(
            "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL);"
            'CREATE INDEX book_lower ON book ((lower("title")) DESC, (length(title) + 1));'
        )
        table = await DatabaseCatalog.inspect_table(client, "book")
    finally:
        await client.close()
    index = next(index for index in table.indexes if index.name == "book_lower")
    assert index.expression_terms == ['(lower("title")) DESC', "(length(title) + 1)"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
async def test_catalog_reads_table_options_with_ddl_comments(tmp_path, strict):
    if strict and not AiosqliteClient.features.supports_strict_tables:
        pytest.skip("the SQLite library has no STRICT tables")
    client = AiosqliteClient(file_path=str(tmp_path / "catalog.sqlite"), connection_alias="default")
    await client.create_connection(with_db=True)
    try:
        suffix = ", /* table option */ STRICT" if strict else ""
        await client.execute_script(
            "CREATE TABLE ordinary (id TEXT PRIMARY KEY, note TEXT DEFAULT '/* keep */');"
            "CREATE TABLE keyed (id TEXT PRIMARY KEY, note TEXT DEFAULT '/* keep */') "
            f"WITHOUT /* table option ) */ ROWID{suffix};"
        )
        ordinary = await DatabaseCatalog.inspect_table(client, "ordinary")
        keyed = await DatabaseCatalog.inspect_table(client, "keyed")
    finally:
        await client.close()

    assert ordinary.table_options is None
    assert keyed.table_options == SqliteTableOptions(without_rowid=True, strict=strict)


@pytest.mark.asyncio
async def test_catalog_reads_partial_expression_index_with_ddl_comments(tmp_path):
    client = AiosqliteClient(file_path=str(tmp_path / "catalog.sqlite"), connection_alias="default")
    await client.create_connection(with_db=True)
    try:
        await client.execute_script(
            "CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL);"
            "CREATE INDEX book_lower ON book /* skip (bogus) */ "
            "(lower(title), id DESC) /* skip WHERE bogus */ WHERE title != '/* keep */';"
        )
        table = await DatabaseCatalog.inspect_table(client, "book")
    finally:
        await client.close()

    (index,) = table.indexes
    assert index.name == "book_lower"
    assert index.expression_terms == ["lower(title)", "id DESC"]
    assert index.condition_sql == "title != '/* keep */'"
    assert table.get_diagnostics(CatalogDiagnosticCode.INDEX) == []


@pytest.mark.asyncio
async def test_inspected_index_keys_recreate_the_same_catalog(tmp_path):
    source = AiosqliteClient(file_path=str(tmp_path / "original.sqlite"), connection_alias="default")
    target = AiosqliteClient(file_path=str(tmp_path / "recreated.sqlite"), connection_alias="default")
    await source.create_connection(with_db=True)
    await target.create_connection(with_db=True)
    try:
        await source.execute_script("""
            CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "COLLATE" TEXT NOT NULL);
            CREATE INDEX descending_title ON book (title DESC /* order */);
            CREATE INDEX descending_expression ON book (lower(title) DESC -- order
                , id ASC);
            CREATE INDEX quoted_keyword ON book ("COLLATE" DESC);
            CREATE INDEX collated_title ON book (title COLLATE NOCASE DESC);
        """)
        model_source = await SchemaInspector.inspect(source, tables=["book"])
        assert "TODO" not in model_source
        namespace = {"__name__": "inspected_sqlite_book"}
        exec(compile(model_source, "<generated>", "exec"), namespace)
        binding = ModelBindingGraph().include(namespace["Book"])
        editor = target.dialect.schema_editor_class(target)
        await target.execute_script(editor.table_creation.get_create_schema_sql(safe=False, models=[binding]))

        for name in ("descending_title", "descending_expression", "quoted_keyword", "collated_title"):
            statement = f"SELECT name, desc, coll FROM pragma_index_xinfo('{name}') WHERE \"key\" ORDER BY seqno"
            assert await target.execute_dicts(statement) == await source.execute_dicts(statement)
    finally:
        await target.close()
        await source.close()
