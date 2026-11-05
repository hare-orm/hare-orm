"""Native PostgreSQL writes preserve values, binding failures and observable parameters."""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from hare.dialects.postgresql.drivers.rust_pg.client.rust_pg_client import RustPgClient
from hare.instrumentation import Observers, QueryExecuted
from hare.query.statements.write.bulk.bulk_write_batches import BulkWriteBatches
from tests.shared_models.testmodels import (
    DatetimeFields,
    IntFields,
    JSONFields,
    LazySelectSoftDeleteParent,
    UUIDFields,
)
from tests.shared_models.testmodels_postgres import DecimalFields


def skip_unless_native_postgresql(model) -> None:
    if not isinstance(model.get_connection(), RustPgClient):
        pytest.skip("requires the native PostgreSQL driver")


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_bulk_create_binds_serialized_values_and_round_trips(db):
    skip_unless_native_postgresql(JSONFields)
    db_client = JSONFields.get_connection()
    with patch.object(type(db_client), "execute", autospec=True, side_effect=type(db_client).execute) as execute:
        await JSONFields.objects.bulk_create(
            [JSONFields(data={"a": [1, 2.5, "x"]}, data_null=None), JSONFields(data=[], data_null={"b": None})]
        )
    assert isinstance(execute.call_args.args[2], list)
    assert [(item.data, item.data_null) for item in await JSONFields.objects.all().order_by("id")] == [
        ({"a": [1, 2.5, "x"]}, None),
        ([], {"b": None}),
    ]
    moment = datetime.datetime(2024, 3, 10, 6, 59, 59, 999999, tzinfo=datetime.UTC)
    await DatetimeFields.objects.bulk_create([DatetimeFields(datetime=moment, datetime_null=None)])
    assert (await DatetimeFields.objects.get()).datetime == moment
    await DecimalFields.objects.bulk_create([DecimalFields(decimal=Decimal("-1.5"), decimal_nodec=Decimal("7"))])
    created = await DecimalFields.objects.get()
    assert (created.decimal, created.decimal_nodec) == (Decimal("-1.5000"), Decimal("7"))
    identifier = uuid.uuid4()
    await UUIDFields.objects.bulk_create([UUIDFields(data=identifier)])
    assert (await UUIDFields.objects.get()).data == identifier


@pytest.mark.asyncio
async def test_bulk_update_binds_the_values_before_and_after_the_rows(db):
    skip_unless_native_postgresql(LazySelectSoftDeleteParent)
    parents = [
        await LazySelectSoftDeleteParent.objects.create(name="a"),
        await LazySelectSoftDeleteParent.objects.create(name="b"),
    ]
    parents[0].name, parents[1].name = "c", "d"
    assert await LazySelectSoftDeleteParent.objects.bulk_update(parents, fields=["name"]) == 2
    assert [item.name for item in await LazySelectSoftDeleteParent.objects.all().order_by("id")] == ["c", "d"]


@pytest.mark.asyncio
async def test_written_parameter_rows_run_once_per_instance(db):
    skip_unless_native_postgresql(IntFields)
    db_client = IntFields.get_connection()
    rows = BulkWriteBatches.serialize_instances(
        IntFields, db_client.dialect.types, [IntFields(intnum=1), IntFields(intnum=2)], ["intnum"]
    )
    assert len(rows) == 2
    await db_client.execute_many('INSERT INTO "intfields" ("intnum") VALUES ($1)', rows)
    assert await IntFields.objects.all().order_by("intnum").values_list("intnum", flat=True) == [1, 2]


@pytest.mark.asyncio
async def test_unbindable_values_fail_for_lists_and_tuples(db):
    skip_unless_native_postgresql(IntFields)
    db_client = IntFields.get_connection()
    unbindable = object()
    parameters = (unbindable,)
    with pytest.raises(Exception) as from_list:
        await db_client.execute("SELECT $1", [unbindable])
    with pytest.raises(Exception) as from_parameters:
        await db_client.execute("SELECT $1", parameters)
    assert type(from_parameters.value) is type(from_list.value)
    assert str(from_parameters.value) == str(from_list.value)


@pytest.mark.asyncio
async def test_an_observer_gets_a_list_of_the_parameters(db):
    skip_unless_native_postgresql(IntFields)
    seen: list[object] = []
    with Observers.observing(QueryExecuted, lambda event: seen.append(event.parameters)):
        await IntFields.objects.bulk_create([IntFields(intnum=5), IntFields(intnum=6)])
    # The rows bind as an array per column.
    assert seen == [[[5, 6], [None, None]]]
    assert type(seen[0]) is list
