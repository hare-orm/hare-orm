"""Bulk writes on a driver that binds the parameters rust.native.rows.ModelWriter writes
(Features.binds_written_parameters): the rows round-trip the same, a value that can't be bound fails
where a list of the values fails, and whatever is given the parameters to look at still gets a
list."""

import datetime
import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from hare.instrumentation import Observers, QueryExecuted
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator
from tests.testmodels import (
    DatetimeFields,
    DecimalFields,
    IntFields,
    JSONFields,
    LazySelectSoftDeleteParent,
    UUIDFields,
)

pg = pytest.importorskip("rust.native.pg")


def skip_unless_written_parameters(model) -> None:
    if HydrateAccelerator.module is None or not model.get_connection().features.binds_written_parameters:
        pytest.skip("for a driver that binds written parameters")


@pytest.mark.asyncio
async def test_bulk_create_binds_written_parameters_and_round_trips(db):
    skip_unless_written_parameters(JSONFields)
    db_client = JSONFields.get_connection()
    with patch.object(type(db_client), "execute", autospec=True, side_effect=type(db_client).execute) as execute:
        await JSONFields.objects.bulk_create(
            [JSONFields(data={"a": [1, 2.5, "x"]}, data_null=None), JSONFields(data=[], data_null={"b": None})]
        )
    assert isinstance(execute.call_args.args[2], pg.PgParameters)
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
    skip_unless_written_parameters(LazySelectSoftDeleteParent)
    parents = [
        await LazySelectSoftDeleteParent.objects.create(name="a"),
        await LazySelectSoftDeleteParent.objects.create(name="b"),
    ]
    parents[0].name, parents[1].name = "c", "d"
    assert await LazySelectSoftDeleteParent.objects.bulk_update(parents, fields=["name"]) == 2
    assert [item.name for item in await LazySelectSoftDeleteParent.objects.all().order_by("id")] == ["c", "d"]


@pytest.mark.asyncio
async def test_written_parameter_rows_run_once_per_instance(db):
    skip_unless_written_parameters(IntFields)
    db_client = IntFields.get_connection()
    writer = HydrateAccelerator.get_model_writer(IntFields, ("intnum",), db_client.dialect.types)
    rows = writer.write_parameter_rows([IntFields(intnum=1), IntFields(intnum=2)], [])
    assert len(rows) == 2
    await db_client.execute_many('INSERT INTO "intfields" ("intnum") VALUES ($1)', rows)
    assert await IntFields.objects.all().order_by("intnum").values_list("intnum", flat=True) == [1, 2]


@pytest.mark.asyncio
async def test_a_value_that_cant_be_bound_fails_as_in_a_list(db):
    skip_unless_written_parameters(IntFields)
    db_client = IntFields.get_connection()
    writer = HydrateAccelerator.get_model_writer(IntFields, ("intnum",), db_client.dialect.types)
    unbindable = object()
    parameters = writer.write_parameters([], [unbindable], [])
    with pytest.raises(Exception) as from_list:
        await db_client.execute("SELECT $1", [unbindable])
    with pytest.raises(Exception) as from_parameters:
        await db_client.execute("SELECT $1", parameters)
    assert type(from_parameters.value) is type(from_list.value)
    assert str(from_parameters.value) == str(from_list.value)


@pytest.mark.asyncio
async def test_an_observer_gets_a_list_of_the_parameters(db):
    skip_unless_written_parameters(IntFields)
    seen: list[object] = []
    with Observers.observing(QueryExecuted, lambda event: seen.append(event.parameters)):
        await IntFields.objects.bulk_create([IntFields(intnum=5), IntFields(intnum=6)])
    # The rows bind as an array per column.
    assert seen == [[[5, 6], [None, None]]]
    assert type(seen[0]) is list
