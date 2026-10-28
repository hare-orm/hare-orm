"""Columns of a model named as the columns hare selects itself next to them (``source_field="hare app"``) - hare
takes the next free name for its own column instead, and every row is read as written."""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test import RollbackIsolation, requires_features
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.instrumentation import Observers, RowsChanged
from hare.query.expressions import F, Value
from hare.query.hare_column_names import HareColumnNames
from tests.queries.expressions.models_hare_column_names import (
    InsertedFlagNamedKey,
    SpacedColumns,
    UpperCaseDepthNode,
)
from tests.queries.expressions.models_hare_column_names_locked import FilterFlagNamedKey
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(scope="module")
async def spaced_columns_module() -> AsyncIterator[Any]:
    async with hare_test_context(
        modules=["tests.queries.expressions.models_hare_column_names"],
        db_url=DatabaseUnderTest.get_url(),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture(scope="function")
async def spaced_columns_db(spaced_columns_module: Any) -> AsyncIterator[Any]:
    async with RollbackIsolation(spaced_columns_module) as context:
        yield context


@pytest_asyncio.fixture(scope="function")
async def spaced_columns_committed_db() -> AsyncIterator[Any]:
    """A context of its own, its writes committed - the observers hear a write once it commits."""
    async with hare_test_context(
        modules=["tests.queries.expressions.models_hare_column_names"],
        db_url=DatabaseUnderTest.get_url(),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest_asyncio.fixture(scope="function")
async def locked_db() -> AsyncIterator[Any]:
    """The model with an optimistic lock - on a database reporting the rows a write changed."""
    if not DatabaseUnderTest.get_dialect().features.reports_written_row_count:
        pytest.skip("an optimistic lock needs the count of the rows a write changed")
    async with hare_test_context(
        modules=["tests.queries.expressions.models_hare_column_names_locked"],
        db_url=DatabaseUnderTest.get_url(),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


async def create_rows() -> None:
    await SpacedColumns.objects.create(id=1, app="first", model_name="m1", window=10, ordering=2, old_id=7)
    await SpacedColumns.objects.create(id=2, app="second", model_name="m2", window=20, ordering=1, parent_id=1)


def test_a_name_of_hare_s_a_column_holds_is_the_next_free_one():
    assert HareColumnNames.get_free_name("hare app", [SpacedColumns]) == "hare app 2"
    assert HareColumnNames.get_free_name("hare app", [SpacedColumns], ["hare app 2"]) == "hare app 3"
    assert HareColumnNames.get_free_name("hare model 2", [SpacedColumns]) == "hare model 2"
    old_value_names = HareColumnNames.get_old_value_names(SpacedColumns)
    assert old_value_names["hare depth"] == "hare old hare depth 2"
    assert len(set(old_value_names.values())) == len(old_value_names)
    # Told apart regardless of letter case - SQLite reads "HARE DEPTH" as "hare depth".
    assert HareColumnNames.get_free_name("hare depth", [UpperCaseDepthNode]) == "hare depth 3"


@pytest.mark.asyncio
async def test_a_union_reads_the_columns_named_like_its_model_columns(spaced_columns_db):
    await create_rows()
    query = SpacedColumns.objects.filter(id=1)
    rows = await query.union(SpacedColumns.objects.filter(id=2), all=True).order_by("id")
    assert [(row.id, row.app, row.model_name) for row in rows] == [(1, "first", "m1"), (2, "second", "m2")]


@pytest.mark.asyncio
async def test_distinct_orders_by_its_expression_beside_a_column_named_like_its_ordering_column(spaced_columns_db):
    await create_rows()
    rows = await SpacedColumns.objects.annotate(note=Value(1)).order_by(F("window") * -1).distinct()
    assert [(row.id, row.ordering) for row in rows] == [(2, 1), (1, 2)]


@requires_features(supports_insert_conflicts=True)
@pytest.mark.asyncio
async def test_an_upsert_tells_inserted_rows_from_updated_ones_beside_a_key_named_like_its_flag(
    spaced_columns_committed_db,
):
    await InsertedFlagNamedKey.objects.create(id=1, name="first")
    changes: list[RowsChanged] = []
    listener = Observers.observe(RowsChanged, changes.append)
    try:
        await InsertedFlagNamedKey.objects.bulk_create(
            [InsertedFlagNamedKey(id=1, name="again"), InsertedFlagNamedKey(id=2, name="second")],
            update_fields=["name"],
            on_conflict=["id"],
        )
    finally:
        Observers.unobserve(RowsChanged, listener)
    upserted = [(event.operation.value, sorted(event.pks or ())) for event in changes]
    assert sorted(upserted) == [("insert", [2]), ("update", [1])]
    assert await InsertedFlagNamedKey.objects.order_by("id").values_list("id", "name") == [(1, "again"), (2, "second")]


@pytest.mark.asyncio
async def test_a_filtered_bulk_update_counts_its_rows_beside_a_key_named_like_its_flag(locked_db):
    await FilterFlagNamedKey.objects.create(id=1, value=10)
    await FilterFlagNamedKey.objects.create(id=2, value=20)
    first, second = await FilterFlagNamedKey.objects.order_by("id")
    first.value, second.value = 11, 21
    # The second row is left out by the filter - not a stale object: the update is counted, nothing raised.
    assert await FilterFlagNamedKey.objects.filter(value=10).bulk_update([first, second], ["value"]) == 1
    assert await FilterFlagNamedKey.objects.order_by("id").values_list("id", "value") == [(1, 11), (2, 20)]


@requires_features(supports_recursive_queries=True)
@pytest.mark.asyncio
async def test_a_recursive_walk_counts_its_depth_beside_a_key_named_like_its_depth(spaced_columns_db):
    await create_rows()
    walked = (
        await SpacedColumns.objects.filter(pk=1).with_recursive("children", max_depth=1).values_list("id", flat=True)
    )
    assert sorted(walked) == [1, 2]
