"""Columns ClickHouse computes - ``GeneratedField`` as ``MATERIALIZED`` (stored with the row) and
``ALIAS`` (computed on read): created, left out of every insert, read, filtered and ordered by, read
back by the introspector, refused as a key of the table's sort."""

import decimal

import pytest
import pytest_asyncio

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.exceptions import ConfigurationError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.models import Model
from tests.dialects.clickhouse.models import Invoice


@pytest_asyncio.fixture
async def invoices(clickhouse_db):
    await Invoice.objects.bulk_create(
        [
            Invoice(id=1, price=decimal.Decimal("2.50"), quantity=4),
            Invoice(id=2, price=decimal.Decimal("10.00"), quantity=1),
        ]
    )
    await Invoice.objects.create(id=3, price=decimal.Decimal("1.00"), quantity=3)


@pytest.mark.asyncio
async def test_columns_are_declared_as_computed(clickhouse_db):
    rows = await Invoice._meta.connection.execute_dicts("DESCRIBE TABLE invoice")
    computed_rows = sorted((row for row in rows if row["default_type"]), key=lambda row: row["name"])
    assert [(row["name"], row["type"], row["default_type"]) for row in computed_rows] == [
        ("label", "String", "ALIAS"),
        ("note", "Nullable(String)", "MATERIALIZED"),
        ("total", "Decimal(18, 2)", "MATERIALIZED"),
    ]


@pytest.mark.asyncio
async def test_computed_values_are_read_filtered_and_ordered_by(invoices):
    rows = await Invoice.objects.order_by("-total").values_list("id", "total", "label", "note")
    assert rows == [
        (1, decimal.Decimal("10.00"), "#1", "many"),
        (2, decimal.Decimal("10.00"), "#2", None),
        (3, decimal.Decimal("3.00"), "#3", "many"),
    ]
    assert await Invoice.objects.filter(label="#3").values_list("total", flat=True) == [decimal.Decimal("3.00")]
    assert await Invoice.objects.filter(total__gt=5).order_by("id").values_list("id", flat=True) == [1, 2]
    first = await Invoice.objects.get(id=1)
    assert (first.total, first.label, first.note) == (decimal.Decimal("10.00"), "#1", "many")
    await Invoice.objects.filter(id=2).update(quantity=2)
    assert (await Invoice.objects.get(id=2)).total == decimal.Decimal("20.00")


@pytest.mark.asyncio
async def test_the_introspector_reads_computed_columns(clickhouse_db):
    table = await DatabaseCatalog.inspect_table(Invoice._meta.connection, "invoice")
    computed = {
        column.name: (column.generated_expression, column.generated_stored)
        for column in table.columns
        if column.generated_expression is not None
    }
    assert computed == {
        "total": ("price * quantity", True),
        "label": ("concat('#', toString(id))", False),
        "note": ("if(quantity > 1, 'many', NULL)", True),
    }


def test_a_column_computed_on_read_is_no_key_of_the_sort():
    class Sorted(Model):
        id = fields.BigIntField(primary_key=True, generated=False)
        label = fields.GeneratedField(RawSQLTerm("toString(id)"), fields.CharField(max_length=30), stored=False)

        class Meta:
            app = "sorted_by_alias"

    with pytest.raises(ConfigurationError, match="computed on"):
        ClickhouseTableOptions(order_by=("id", "label")).raise_if_unsupported(Sorted, CLICKHOUSE_DIALECT.features)
