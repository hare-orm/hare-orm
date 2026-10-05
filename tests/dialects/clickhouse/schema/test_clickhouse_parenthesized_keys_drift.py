"""Keys of a ClickHouse table written in parentheses: a partition key of a tuple or of one parenthesized
expression, a data skipping index of one raw tuple or of raw column names, the sorting and partition keys
of a materialized view's own storage - a tuple, one key in parentheses, ``tuple()``, names in quotes.
ClickHouse 26.7+ keeps the parentheses in its catalog, an older server drops them or reports a tuple as
its elements - drift saw a change no migration made after migrating them."""

import os
import textwrap

import pytest

from tests.utils.cli_project_factory import project_factory  # noqa: F401

TWO_KEYS_QUERY = "SELECT a AS k1, b AS k2, count() AS n FROM item GROUP BY k1, k2"
ONE_KEY_QUERY = "SELECT a AS k, count() AS n FROM item GROUP BY k"
SPACED_KEY_QUERY = 'SELECT a AS "c d", count() AS n FROM item GROUP BY "c d"'


def view_meta(query: str, options: str) -> str:
    """A model's materialized view summing its rows into storage of its own, sorted and partitioned by
    ``options``."""
    return (
        f'materialized_views = [ClickhouseMaterializedView("item_totals", RawSQLTerm({query!r}), '
        f'engine="SummingMergeTree", {options})]'
    )


@pytest.fixture(autouse=True)
def skip_without_clickhouse():
    if not os.getenv("HARE_TEST_DB", "").startswith("clickhouse"):
        pytest.skip("runs against the ClickHouse test server - make test_clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "meta",
    [
        pytest.param(
            'table_options = [ClickhouseTableOptions(partition_by=RawSQLTerm("(a, b)"))]', id="partition_tuple"
        ),
        pytest.param('table_options = [ClickhouseTableOptions(partition_by=RawSQLTerm("(a)"))]', id="partition_paren"),
        pytest.param('indexes = [MinMaxIndex(RawSQLTerm("(a, b)"), name="ix_tuple")]', id="index_tuple"),
        pytest.param(
            'indexes = [MinMaxIndex(RawSQLTerm("a"), RawSQLTerm("b"), name="ix_columns")]', id="index_raw_columns"
        ),
        pytest.param('indexes = [SetIndex(RawSQLTerm("(a, b)"), name="ix_set", max_rows=10)]', id="set_index_tuple"),
        pytest.param('indexes = [Index(RawSQLTerm("(a, b)"), name="ix_plain")]', id="plain_index_tuple"),
        pytest.param(view_meta(TWO_KEYS_QUERY, 'order_by=(RawSQLTerm("(k1, k2)"),)'), id="view_order_tuple"),
        pytest.param(view_meta(ONE_KEY_QUERY, 'order_by=(RawSQLTerm("(k)"),)'), id="view_order_parenthesized"),
        pytest.param(view_meta(TWO_KEYS_QUERY, 'order_by=(RawSQLTerm("tuple()"),)'), id="view_order_none"),
        pytest.param(
            view_meta(TWO_KEYS_QUERY, "order_by=(RawSQLTerm('(\"k1\", `k2`)'),)"), id="view_order_quoted_tuple"
        ),
        pytest.param(
            view_meta(SPACED_KEY_QUERY, "order_by=(RawSQLTerm('\"c d\"'),), partition_by=RawSQLTerm('(\"c d\")')"),
            id="view_keys_named_with_a_space",
        ),
        pytest.param(
            view_meta(TWO_KEYS_QUERY, 'order_by=("k1",), partition_by=RawSQLTerm("(intDiv(k2, 10))")'),
            id="view_partition_parenthesized",
        ),
    ],
)
async def test_a_table_of_parenthesized_keys_is_in_sync_after_its_migration(
    project_factory,  # noqa: F811
    meta: str,
) -> None:
    project = await project_factory()
    project.write_models(
        textwrap.dedent(
            f"""
            from hare.ddl import Index
            from hare.ddl.raw_sql_term import RawSQLTerm
            from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
            from hare.dialects.clickhouse.indexes.min_max_index import MinMaxIndex
            from hare.dialects.clickhouse.indexes.set_index import SetIndex
            from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import (
                ClickhouseMaterializedView,
            )

            class Item(Model):
                id = fields.IntField(primary_key=True, generated=False)
                a = fields.IntField()
                b = fields.IntField()

                class Meta:
                    {meta}
            """
        )
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
