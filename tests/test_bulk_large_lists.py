"""Value lists and bulk writes past a backend's bind-parameter ceiling: composite ``pk__in``,
relation ``__in`` lookups, ``__in`` over an annotation, many-to-many writes, ``bulk_update()``/
``bulk_create()`` batching and atomicity, argument validation, and the error a statement binding
too many parameters raises."""

import datetime
import decimal
import os
import re
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import closing
from typing import Any
from unittest import mock

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features, truncate_all_models
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.dialects.postgresql.lookups.in_list import ArrayParameter
from hare.dialects.sqlite.lookups.in_list import SqliteLargeInList
from hare.dialects.sqlite.parameters.constants import SQLITE_IN_JSON_ARRAY_THRESHOLD
from hare.exceptions import (
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    ProtectedError,
    QueryError,
    TooManyParametersError,
)
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import Q, RawSQL
from hare.query.functions import Count, Length
from tests.bulk_large_list_models import (
    LargeListArticle,
    LargeListChild,
    LargeListDefaultsOnly,
    LargeListItem,
    LargeListLabel,
    LargeListLabelling,
    LargeListPair,
    LargeListPairOwner,
    LargeListParent,
    LargeListPost,
    LargeListProtectedPair,
    LargeListProtectedTag,
    LargeListProtectingPost,
    LargeListTag,
    LargeListTenantItem,
    LargeListThingA,
    LargeListThingB,
    LargeListUniqueItem,
)

#: Bind-parameter ceiling the batching tests lower the backend's to, so a handful of rows crosses it.
SMALL_BIND_PARAMETER_LIMIT = 150


def get_test_db_url() -> str:
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    return raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url


@pytest_asyncio.fixture(scope="module")
async def large_list_context() -> AsyncGenerator[Any]:
    async with hare_test_context(
        ["tests.bulk_large_list_models"], db_url=get_test_db_url(), connection_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def large_list_db(large_list_context: Any) -> AsyncGenerator[Any]:
    yield large_list_context
    await truncate_all_models()


@pytest.fixture
def small_bind_parameter_limit(large_list_db: Any) -> Generator[int]:
    # The connection's own features - a connected client keeps those of its server's version.
    connection = LargeListItem._meta.connection
    with mock.patch.object(
        connection, "features", connection.features.replace(max_bind_parameters=SMALL_BIND_PARAMETER_LIMIT)
    ):
        yield SMALL_BIND_PARAMETER_LIMIT


@pytest_asyncio.fixture
async def captured_queries() -> AsyncGenerator[list[tuple[str, list[Any]]]]:
    queries: list[tuple[str, list[Any]]] = []

    def capture(event) -> None:
        sql, params = event.sql, event.parameters
        queries.append((sql or "", list(params or [])))

    Observers.observe(QueryExecuted, capture)
    try:
        yield queries
    finally:
        await Observers.wait_for_pending()
        Observers.unobserve(QueryExecuted, capture)


def is_postgres() -> bool:
    return LargeListItem._meta.connection.dialect.name == "postgresql"


def count_bound_parameters(params: list[Any]) -> int:
    """execute_many() reports its rows as one list each - every other call its flat values."""
    return sum(len(row) if isinstance(row, (list, tuple)) else 1 for row in params)


# ---------------------------------------------------------------------------------------------
# Composite pk__in
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_pk_in_past_the_expression_depth_limit(large_list_db):
    await LargeListPair.objects.bulk_create([LargeListPair(a=index, b=index, name="x") for index in range(100)])
    pairs = [(index, index) for index in range(1200)]

    assert await LargeListPair.objects.filter(pk__in=pairs).count() == 100
    assert await LargeListPair.objects.exclude(pk__in=pairs[:1150]).count() == 0
    assert await LargeListPair.objects.exclude(pk__in=pairs[:50]).count() == 50
    assert await LargeListPair.objects.filter(Q(pk__in=pairs) | Q(name="nothing")).count() == 100
    assert await LargeListPair.objects.filter(pk__in=pairs[:60]).update(name="y") == 60
    assert await LargeListPair.objects.filter(name="y").count() == 60
    assert await LargeListPair.objects.filter(pk__in=pairs[:30]).delete() == 30
    assert await LargeListPair.objects.all().count() == 70


@pytest.mark.asyncio
async def test_composite_pk_in_binds_a_long_list_compactly(large_list_db):
    pairs = [(index, index) for index in range(1200)]

    sql = LargeListPair.objects.filter(pk__in=pairs).sql()

    assert "unnest" in sql if is_postgres() else "json_each" in sql
    assert len(re.findall(r"\?|\$\d+", sql)) <= 2


@pytest.mark.asyncio
async def test_composite_pk_in_short_list_is_a_plain_row_list(large_list_db):
    await LargeListPair.objects.bulk_create([LargeListPair(a=1, b=2), LargeListPair(a=3, b=4)])

    sql = LargeListPair.objects.filter(pk__in=[(1, 2), (3, 4)]).sql()

    # SQLite writes the rows as a VALUES list - it takes no list of row values after IN before 3.37.
    assert "IN ((" in sql or "IN (VALUES (" in sql
    assert await LargeListPair.objects.filter(pk__in=[(1, 2), (3, 5)]).count() == 1


def test_sqlite_row_container_keeps_values_json_cannot_carry_exactly():
    long_row_count = SQLITE_IN_JSON_ARRAY_THRESHOLD

    assert SqliteLargeInList.get_row_container([(index, float("nan")) for index in range(long_row_count)]) is None
    assert SqliteLargeInList.get_row_container([(index, 2**70) for index in range(long_row_count)]) is None
    assert SqliteLargeInList.get_row_container([(1, 2), (3, 4)]) is None
    container = SqliteLargeInList.get_row_container([(index, str(index)) for index in range(long_row_count)])
    assert container is not None
    assert container.decode_hex_columns == (False, False)


# ---------------------------------------------------------------------------------------------
# Many-to-many and backward FK lookups
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_m2m_and_backward_fk_in_lookups_bind_a_long_list_compactly(large_list_db):
    ids = list(range(1500))

    for sql in (
        LargeListPost.objects.filter(tags__in=ids).sql(),
        LargeListPost.objects.filter(tags__not_in=ids).sql(),
        LargeListTag.objects.filter(posts__in=ids).sql(),
        LargeListParent.objects.filter(children__in=ids).sql(),
        LargeListParent.objects.filter(children__not_in=ids).sql(),
    ):
        assert ("ANY" in sql) if is_postgres() else ("json_each" in sql)


@pytest.mark.asyncio
async def test_m2m_and_backward_fk_in_lookups_with_a_long_list(large_list_db):
    await LargeListTag.objects.bulk_create([LargeListTag(id=index) for index in range(30)])
    post = await LargeListPost.objects.create(id=1)
    await LargeListPost.objects.create(id=2)
    await post.tags.add(*await LargeListTag.objects.filter(id__lt=5))
    await LargeListParent.objects.bulk_create([LargeListParent(id=index) for index in range(3)])
    await LargeListChild.objects.bulk_create([LargeListChild(id=index, parent_id=index % 2) for index in range(10)])
    ids = list(range(3, 1503))

    assert await LargeListPost.objects.filter(tags__in=ids).distinct().values_list("id", flat=True) == [1]
    assert sorted(await LargeListPost.objects.filter(tags__not_in=ids).distinct().values_list("id", flat=True)) == [
        1,
        2,
    ]
    assert await LargeListParent.objects.filter(children__in=ids).distinct().count() == 2
    assert await LargeListParent.objects.filter(children__in=list(range(20, 1520))).count() == 0


@pytest.mark.asyncio
async def test_m2m_add_and_remove_past_the_bind_parameter_limit(large_list_db, small_bind_parameter_limit):
    tags = [LargeListTag(id=index) for index in range(90)]
    await LargeListTag.objects.bulk_create(tags)
    post = await LargeListPost.objects.create(id=1)
    for tag in tags:
        tag._saved_in_db = True

    await post.tags.add(*tags)
    assert await post.tags.all().count() == 90

    await post.tags.remove(*tags[:80])
    assert sorted(await post.tags.all().values_list("id", flat=True)) == list(range(80, 90))


@pytest.mark.asyncio
async def test_m2m_add_through_model_past_the_bind_parameter_limit(large_list_db, small_bind_parameter_limit):
    labels = [LargeListLabel(id=index) for index in range(60)]
    await LargeListLabel.objects.bulk_create(labels)
    for label in labels:
        label._saved_in_db = True
    article = await LargeListArticle.objects.create(id=1)

    await article.labels.add(*labels, through_defaults={"note": "bulk"})

    assert await LargeListLabelling.objects.filter(article_id=1, note="bulk").count() == 60
    await article.labels.add(*labels)
    assert await LargeListLabelling.objects.filter(article_id=1).count() == 60


@pytest.mark.asyncio
async def test_m2m_add_chunks_run_in_one_transaction(large_list_db, small_bind_parameter_limit, captured_queries):
    tags = [LargeListTag(id=index) for index in range(90)]
    await LargeListTag.objects.bulk_create(tags)
    for tag in tags:
        tag._saved_in_db = True
    post = await LargeListPost.objects.create(id=1)
    captured_queries.clear()

    await post.tags.add(*tags)
    await Observers.wait_for_pending()

    inserts = [(sql, params) for sql, params in captured_queries if sql.lstrip().upper().startswith("INSERT")]
    assert len(inserts) > 1
    assert all(count_bound_parameters(params) <= SMALL_BIND_PARAMETER_LIMIT for _, params in inserts)


@pytest.mark.asyncio
async def test_deleting_many_m2m_protect_targets(large_list_db, small_bind_parameter_limit):
    await LargeListProtectedTag.objects.bulk_create([LargeListProtectedTag(id=index) for index in range(200)])
    post = await LargeListProtectingPost.objects.create(id=1)
    protected = await LargeListProtectedTag.objects.get(id=150)
    await post.tags.add(protected)

    with pytest.raises(ProtectedError):
        await LargeListProtectedTag.objects.all().delete()
    assert await LargeListProtectedTag.objects.all().count() == 200

    await post.tags.clear()
    assert await LargeListProtectedTag.objects.all().delete() == 200


@pytest.mark.asyncio
async def test_deleting_many_composite_m2m_protect_targets(large_list_db):
    await LargeListProtectedPair.objects.bulk_create(
        [LargeListProtectedPair(a=index, b=index) for index in range(1200)]
    )
    owner = await LargeListPairOwner.objects.create(id=1)
    protected = await LargeListProtectedPair.objects.get(a=1100, b=1100)
    await owner.pairs.add(protected)

    with pytest.raises(ProtectedError):
        await LargeListProtectedPair.objects.all().delete()

    await owner.pairs.clear()
    assert await LargeListProtectedPair.objects.all().delete() == 1200


# ---------------------------------------------------------------------------------------------
# __in over an annotation
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_length_annotation_in_compares_integers(large_list_db):
    await LargeListItem.objects.bulk_create(
        [LargeListItem(id=index, name="x" * (index % 5 + 1)) for index in range(10)]
    )

    short_list = LargeListItem.objects.annotate(name_length=Length("name")).filter(name_length__in=[1, 2])
    long_list = LargeListItem.objects.annotate(name_length=Length("name")).filter(name_length__in=list(range(3, 40)))

    assert await short_list.count() == 4
    assert await long_list.count() == 6


@pytest.mark.asyncio
async def test_untyped_annotation_in_with_a_long_list(large_list_db):
    await LargeListParent.objects.bulk_create([LargeListParent(id=index) for index in range(5)])
    await LargeListChild.objects.bulk_create([LargeListChild(id=index, parent_id=index % 3) for index in range(9)])
    ids = list(range(1, 1500))

    raw_sql_query = LargeListChild.objects.annotate(parent_key=RawSQL('"parent_id"')).filter(parent_key__in=ids)
    count_query = LargeListParent.objects.annotate(child_count=Count("children")).filter(child_count__in=ids)

    assert await raw_sql_query.count() == 6
    assert await count_query.count() == 3
    if is_postgres():
        assert "ANY(CAST(" in raw_sql_query.sql()
        assert "ANY(CAST(" in count_query.sql()


@pytest.mark.parametrize(
    ("values", "expected_type"),
    [
        ([1, 2], "BIGINT"),
        ([True, False], "BOOLEAN"),
        ([1.5], "DOUBLE PRECISION"),
        ([decimal.Decimal("1.5")], "NUMERIC"),
        (["a"], "TEXT"),
        ([uuid.uuid4()], "UUID"),
        ([b"a"], "BYTEA"),
        ([datetime.date(2026, 1, 1)], "DATE"),
        ([datetime.datetime(2026, 1, 1)], "TIMESTAMP"),
        ([datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)], "TIMESTAMPTZ"),
        ([datetime.time(1, 2)], "TIME"),
        ([datetime.timedelta(seconds=1)], "INTERVAL"),
        ([2**70], "NUMERIC"),
        ([1, "a"], None),
        ([object()], None),
    ],
)
def test_postgres_array_element_type_is_taken_from_the_values(values, expected_type):
    assert ArrayParameter.get_array_element_type(values) == expected_type


# ---------------------------------------------------------------------------------------------
# bulk_update()/bulk_create() batching and atomicity
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_batches_leave_room_for_the_tenant_filter(
    large_list_db, small_bind_parameter_limit, captured_queries
):
    with Tenancy.scope(1):
        await LargeListTenantItem.objects.bulk_create(
            [LargeListTenantItem(id=index, name="a") for index in range(200)]
        )
        objects = await LargeListTenantItem.objects.all()
        for obj in objects:
            obj.name = "b"
        captured_queries.clear()

        assert await LargeListTenantItem.objects.bulk_update(objects, ["name"]) == 200
        await Observers.wait_for_pending()

        assert await LargeListTenantItem.objects.filter(name="b").count() == 200
    updates = [params for sql, params in captured_queries if sql.lstrip().upper().startswith("UPDATE")]
    assert len(updates) > 1
    assert all(len(params) <= SMALL_BIND_PARAMETER_LIMIT for params in updates)


@pytest.mark.asyncio
async def test_bulk_update_batches_leave_room_for_the_queryset_filter(
    large_list_db, small_bind_parameter_limit, captured_queries
):
    await LargeListItem.objects.bulk_create([LargeListItem(id=index, name="a") for index in range(200)])
    objects = await LargeListItem.objects.all()
    for obj in objects:
        obj.name = "b"
    captured_queries.clear()

    filtered = LargeListItem.objects.filter(name__in=["a", "c", "d", "e", "f"])
    assert await filtered.bulk_update(objects, ["name"]) == 200
    await Observers.wait_for_pending()

    updates = [params for sql, params in captured_queries if sql.lstrip().upper().startswith("UPDATE")]
    assert all(len(params) <= SMALL_BIND_PARAMETER_LIMIT for params in updates)


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_update_implicit_batches_are_all_or_nothing(large_list_db, small_bind_parameter_limit):
    await LargeListUniqueItem.objects.bulk_create([LargeListUniqueItem(name=f"n{index}") for index in range(200)])
    objects = await LargeListUniqueItem.objects.all().order_by("id")
    for obj in objects:
        obj.name = f"{obj.name}_new"
    objects[-1].name = objects[0].name

    with pytest.raises(IntegrityError):
        await LargeListUniqueItem.objects.bulk_update(objects, ["name"])

    assert await LargeListUniqueItem.objects.filter(name__endswith="_new").count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_bulk_update_explicit_batches_stay_independent(large_list_db):
    await LargeListUniqueItem.objects.bulk_create([LargeListUniqueItem(name=f"n{index}") for index in range(4)])
    objects = await LargeListUniqueItem.objects.all().order_by("id")
    for obj in objects:
        obj.name = f"{obj.name}_new"
    objects[-1].name = objects[0].name

    with pytest.raises(IntegrityError):
        await LargeListUniqueItem.objects.bulk_update(objects, ["name"], batch_size=2)

    assert await LargeListUniqueItem.objects.filter(name__endswith="_new").count() == 2


@pytest.mark.asyncio
async def test_bulk_create_upsert_returning_leaves_room_for_the_tenant_scope(
    large_list_db, small_bind_parameter_limit, captured_queries
):
    if not is_postgres():
        pytest.skip("bulk_create(returning=True) is Postgres-only")
    # Four columns per row: 37 rows fill a 148-parameter ceiling exactly, leaving no room for the
    # tenant scope's own parameter unless the batch size accounts for it.
    bind_parameter_limit = 148
    connection = LargeListTenantItem._meta.connection
    limited_features = connection.features.replace(max_bind_parameters=bind_parameter_limit)
    # The VALUES form, whose rows count against the ceiling - not an array per column.
    values_form = mock.patch.object(
        type(connection.dialect.parameters), "get_column_arrays_rows_source_sql", return_value=None
    )
    with mock.patch.object(connection, "features", limited_features), values_form, Tenancy.scope(1):
        objects = [LargeListTenantItem(id=index, name="a") for index in range(37)]
        captured_queries.clear()

        await LargeListTenantItem.objects.bulk_create(
            objects, update_fields=["name"], on_conflict=["id"], returning=True
        )
        await Observers.wait_for_pending()

        assert await LargeListTenantItem.objects.all().count() == 37
    inserts = [params for sql, params in captured_queries if sql.lstrip().upper().startswith("INSERT")]
    assert len(inserts) > 1
    assert all(len(params) <= bind_parameter_limit for params in inserts)


@pytest.mark.asyncio
async def test_bulk_create_copy_with_and_without_explicit_pk(large_list_db):
    if not is_postgres():
        pytest.skip("COPY is Postgres-only")
    objects = [LargeListUniqueItem(id=100, name="x"), LargeListUniqueItem(name="x")]

    with pytest.raises(IntegrityError):
        await LargeListUniqueItem.objects.bulk_create(objects, use_copy=True)

    assert await LargeListUniqueItem.objects.all().count() == 0
    assert not any(obj._saved_in_db for obj in objects)


@pytest.mark.asyncio
async def test_bulk_create_copy_batches_keep_object_state_in_line_with_the_database(large_list_db):
    if not is_postgres():
        pytest.skip("COPY is Postgres-only")
    objects = [LargeListUniqueItem(id=index, name=f"n{index}") for index in range(1, 5)]
    objects[-1].name = objects[0].name

    with pytest.raises(IntegrityError):
        await LargeListUniqueItem.objects.bulk_create(objects, use_copy=True, batch_size=2)

    saved_ids = sorted(await LargeListUniqueItem.objects.all().values_list("id", flat=True))
    assert saved_ids == []
    assert [obj.id for obj in objects if obj._saved_in_db] == saved_ids


@pytest.mark.asyncio
async def test_bulk_create_of_database_default_rows(large_list_db, captured_queries):
    objects = [LargeListDefaultsOnly() for _ in range(30)]
    returning = is_postgres()
    captured_queries.clear()

    await LargeListDefaultsOnly.objects.bulk_create(objects, returning=returning)
    await Observers.wait_for_pending()

    assert await LargeListDefaultsOnly.objects.filter(price=3).count() == 30
    inserts = [sql for sql, _ in captured_queries if sql.lstrip().upper().startswith("INSERT")]
    if returning:
        assert len(inserts) == 1
        assert sorted(obj.id for obj in objects) == sorted(
            await LargeListDefaultsOnly.objects.all().values_list("id", flat=True)
        )
        assert {obj.price for obj in objects} == {3}


# ---------------------------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_writes_reject_objects_of_another_model(large_list_db):
    thing_b = LargeListThingB(id=1, name="b")

    with pytest.raises(QueryError, match="LargeListThingB"):
        LargeListThingA.objects.bulk_create([LargeListThingA(id=1, name="a"), thing_b])
    with pytest.raises(QueryError, match="LargeListThingB"):
        LargeListThingA.objects.all().bulk_create([thing_b])
    with pytest.raises(QueryError, match="LargeListThingB"):
        LargeListThingA.objects.bulk_update([thing_b], ["name"])

    assert await LargeListThingA.objects.all().count() == 0


@pytest.mark.asyncio
async def test_bulk_create_rejects_a_partially_loaded_object(large_list_db):
    await LargeListItem.objects.create(id=1, name="a", value=1)
    partial = await LargeListItem.objects.get(id=1).only("id", "name")
    partial.id = 2

    with pytest.raises(IncompleteInstanceError, match="value"):
        LargeListItem.objects.bulk_create([partial])


@pytest.mark.asyncio
async def test_bulk_update_rejects_a_partial_object_missing_an_updated_field(large_list_db):
    await LargeListItem.objects.create(id=1, name="a", value=1)
    partial = await LargeListItem.objects.get(id=1).only("id", "name")

    with pytest.raises(IncompleteInstanceError, match="value"):
        LargeListItem.objects.bulk_update([partial], ["value"])
    partial.name = "b"
    assert await LargeListItem.objects.bulk_update([partial], ["name"]) == 1


@pytest.mark.asyncio
async def test_bulk_update_fields_validation(large_list_db):
    await LargeListItem.objects.bulk_create([LargeListItem(id=index, name="x") for index in range(3)])
    objects = await LargeListItem.objects.all().order_by("id")

    with pytest.raises(QueryError, match="at least one field"):
        LargeListItem.objects.bulk_update(objects, [])
    with pytest.raises(QueryError, match="string"):
        LargeListItem.objects.bulk_update(objects, "name")
    with pytest.raises(QueryError, match="primary key"):
        LargeListItem.objects.bulk_update(objects, ["pk"])
    with pytest.raises(QueryError, match="primary key"):
        LargeListItem.objects.bulk_update(objects, ["id"])
    with pytest.raises(FieldError, match="nope"):
        LargeListItem.objects.bulk_update(objects, ["nope"])

    for obj in objects:
        obj.name = "y"
    assert await LargeListItem.objects.bulk_update(objects, ["name", "name"]) == 3
    assert await LargeListItem.objects.filter(name="y").count() == 3


@pytest.mark.asyncio
async def test_bulk_update_of_a_relation_and_its_key_field_together(large_list_db):
    await LargeListParent.objects.bulk_create([LargeListParent(id=1), LargeListParent(id=2)])
    await LargeListChild.objects.create(id=1, parent_id=1)
    child = await LargeListChild.objects.get(id=1)
    child.parent_id = 2

    assert await LargeListChild.objects.bulk_update([child], ["parent", "parent_id"]) == 1
    assert (await LargeListChild.objects.get(id=1)).parent_id == 2


# ---------------------------------------------------------------------------------------------
# A statement binding too many parameters
# ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_many_parameters_is_an_operational_error_not_a_connection_error(large_list_db):
    connection = LargeListItem._meta.connection
    # PostgreSQL takes at most 65535 (32767 through asyncpg); SQLite's limit is how its library was
    # built - 32766 by default, 250000 in Debian's and Ubuntu's.
    if is_postgres():
        parameter_count = 70000
    else:
        with closing(sqlite3.connect(":memory:")) as limits_connection:
            parameter_count = limits_connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) + 1
    placeholders = ",".join(f"${index + 1}" if is_postgres() else "?" for index in range(parameter_count))

    with pytest.raises(TooManyParametersError):
        await connection.execute(f"SELECT 1 WHERE 1 IN ({placeholders})", list(range(parameter_count)))

    assert await LargeListItem.objects.all().count() == 0


def test_postgres_rows_source_binds_an_array_per_column_but_not_for_an_array_column(large_list_db):
    if not is_postgres():
        pytest.skip("the column-array rows source is Postgres's")
    dialect = LargeListItem._meta.connection.dialect
    assert (
        dialect.parameters.get_column_arrays_rows_source_sql(["INT", "VARCHAR(100)"], 1)
        == "SELECT * FROM unnest($1::INT[],$2::VARCHAR(100)[])"
    )
    assert dialect.parameters.get_column_arrays_rows_source_sql(["INT", "INT[]"], 1) is None


@pytest.mark.asyncio
async def test_bulk_create_binds_an_array_per_column_past_the_ceiling(
    large_list_db, small_bind_parameter_limit, captured_queries
):
    if not is_postgres():
        pytest.skip("the column-array rows source is Postgres's")
    objects = [
        LargeListItem(id=index, name=f"item {index}", value=None if index % 3 else index) for index in range(1, 1001)
    ]

    # Rows read back take the multi-row statement on every driver.
    await LargeListItem.objects.bulk_create(objects, returning=True)
    await Observers.wait_for_pending()

    inserts = [(sql, params) for sql, params in captured_queries if sql.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "unnest(" in sql
    assert len(params) == 3
    assert [(item.id, item.name, item.value) for item in await LargeListItem.objects.all().order_by("id")] == [
        (index, f"item {index}", None if index % 3 else index) for index in range(1, 1001)
    ]


@pytest.mark.asyncio
async def test_bulk_create_upsert_returning_on_column_arrays(large_list_db, captured_queries):
    if not is_postgres():
        pytest.skip("the column-array rows source is Postgres's")
    with Tenancy.scope(1):
        await LargeListTenantItem.objects.bulk_create([LargeListTenantItem(id=index, name="a") for index in range(50)])
        updated = [LargeListTenantItem(id=index, name=f"b{index}") for index in range(50)]
        captured_queries.clear()

        await LargeListTenantItem.objects.bulk_create(
            updated, update_fields=["name"], on_conflict=["id"], returning=True
        )
        await Observers.wait_for_pending()

        assert [(item.id, item.name) for item in await LargeListTenantItem.objects.all().order_by("id")] == [
            (index, f"b{index}") for index in range(50)
        ]
    inserts = [sql for sql, _params in captured_queries if sql.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1
    assert "unnest(" in inserts[0]
    assert all(item._saved_in_db for item in updated)


@pytest.mark.asyncio
async def test_bulk_create_composite_key_rows_on_column_arrays(large_list_db):
    if not is_postgres():
        pytest.skip("the column-array rows source is Postgres's")
    await LargeListPair.objects.bulk_create(
        [LargeListPair(a=index, b=index * 2, name=None if index % 2 else f"n{index}") for index in range(300)]
    )

    assert [(pair.a, pair.b, pair.name) for pair in await LargeListPair.objects.all().order_by("a")] == [
        (index, index * 2, None if index % 2 else f"n{index}") for index in range(300)
    ]
