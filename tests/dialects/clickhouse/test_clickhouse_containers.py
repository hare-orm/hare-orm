"""Arrays, maps, tuples and nested rows on ClickHouse, held in one another to any depth: their column
types, values written by a statement and by a binary insert and read back, the paths into them, their
lookups and the errors naming the path to a refused value."""

import datetime
import decimal
import uuid

import pytest
import pytest_asyncio

from hare import fields
from hare.exceptions import ValidationError
from hare.query.expressions import F
from hare.query.functions import ArrayItem
from tests.dialects.clickhouse.models import Measurement, Shipment

MOMENT = datetime.datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=datetime.UTC)
KEY = uuid.UUID("00000000-0000-0000-0000-000000000007")


def make_shipment(shipment_id: int, **values) -> Shipment:
    defaults = {
        "tags": ["red", "blue"],
        "scores": [1, None, 3],
        "grid": [[1, 2], [3, 4]],
        "prices": {"eur": decimal.Decimal("1.50"), "usd": decimal.Decimal("2.25")},
        "labels": {"en": ["one", "two"], "de": []},
        "point": {"lon": 2.35, "lat": 48.85},
        "bounds": (5, MOMENT),
        "items": [{"sku": "a-1", "quantity": 2}, {"sku": "b-7", "quantity": 1}],
        "deep": [{"k": [(1, {10: KEY, 11: None})]}],
    }
    return Shipment(id=shipment_id, **{**defaults, **values})


EXPECTED = {
    "tags": ["red", "blue"],
    "scores": [1, None, 3],
    "grid": [[1, 2], [3, 4]],
    "prices": {"eur": decimal.Decimal("1.50"), "usd": decimal.Decimal("2.25")},
    "labels": {"en": ["one", "two"], "de": []},
    "point": {"lon": 2.35, "lat": 48.85},
    "bounds": (5, MOMENT),
    "items": [{"sku": "a-1", "quantity": 2}, {"sku": "b-7", "quantity": 1}],
    "deep": [{"k": [(1, {10: KEY, 11: None})]}],
}


@pytest_asyncio.fixture
async def shipments(clickhouse_db):
    await make_shipment(1).save()
    await Shipment.objects.bulk_create(
        [
            make_shipment(2, tags=["green"], prices={"eur": decimal.Decimal("9.00")}, point={"lon": 0.0, "lat": -1.0}),
            make_shipment(3, tags=[], scores=None, labels={}, items=[], deep=[]),
        ]
    )


def test_column_types():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

    fields_map = Shipment._meta.fields_map
    assert fields_map["tags"].get_column_type(CLICKHOUSE_DIALECT) == "Array(VARCHAR(20))"
    assert fields_map["scores"].get_column_type(CLICKHOUSE_DIALECT) == "Array(Nullable(INT))"
    assert fields_map["grid"].get_column_type(CLICKHOUSE_DIALECT) == "Array(Array(INT))"
    assert fields_map["prices"].get_column_type(CLICKHOUSE_DIALECT) == "Map(VARCHAR(3), Decimal(10, 2))"
    assert fields_map["point"].get_column_type(CLICKHOUSE_DIALECT) == "Tuple(lon Float64, lat Float64)"
    assert fields_map["items"].get_column_type(CLICKHOUSE_DIALECT) == "Array(Tuple(sku VARCHAR(10), quantity INT))"
    assert fields_map["deep"].get_column_type(CLICKHOUSE_DIALECT) == (
        "Array(Map(VARCHAR(5), Array(Tuple(INT, Map(INT, Nullable(UUID))))))"
    )


@pytest.mark.asyncio
async def test_values_round_trip_written_and_inserted(shipments):
    first = await Shipment.objects.get(id=1)
    for name, value in EXPECTED.items():
        assert getattr(first, name) == value, name
    rows = await Shipment.objects.order_by("id").values("id", *EXPECTED)
    assert {name: rows[0][name] for name in EXPECTED} == EXPECTED
    assert rows[1]["tags"] == ["green"]
    # A NULL container is written as an empty one.
    assert rows[2]["scores"] == []
    assert rows[2]["deep"] == []


@pytest.mark.asyncio
async def test_paths_into_values(shipments):
    queryset = Shipment.objects.filter(id=1)
    assert await queryset.values_list("tags__0", "tags__-1", "tags__5", "tags__len", "tags__0_1") == [
        ("red", "blue", None, 2, ["red"])
    ]
    assert await queryset.values_list("grid__1", "grid__1__0", "prices__eur", "prices__keys", "prices__len") == [
        ([3, 4], 3, decimal.Decimal("1.50"), ["eur", "usd"], 2)
    ]
    assert await queryset.values_list("point__lat", "point__0", "bounds__1", "items__sku", "items__0") == [
        (48.85, 2.35, MOMENT, ["a-1", "b-7"], {"sku": "a-1", "quantity": 2})
    ]
    assert await queryset.values_list("deep__0__k__0__1__10", flat=True) == [KEY]
    assert await queryset.annotate(first=ArrayItem("tags", 0)).values_list("first", flat=True) == ["red"]


@pytest.mark.asyncio
async def test_lookups(shipments):
    def ids(**filters):
        return Shipment.objects.filter(**filters).order_by("id").values_list("id", flat=True)

    assert await ids(tags__contains=["red"]) == [1]
    assert await ids(tags__contained_by=["red", "blue", "green"]) == [1, 2, 3]
    assert await ids(tags__overlap=["green", "x"]) == [2]
    assert await ids(tags__len=0) == [3]
    assert await ids(tags__item=(0, "green")) == [2]
    assert await ids(tags=["green"]) == [2]
    assert await ids(tags__not=["green"]) == [1, 3]
    assert await ids(tags__0="red") == [1]
    assert await ids(grid__0__1__gt=1) == [1, 2, 3]
    assert await ids(grid__0=[1, 2]) == [1, 2, 3]
    assert await ids(prices__has_key="usd") == [1, 3]
    assert await ids(prices__has_keys=["eur", "usd"]) == [1, 3]
    assert await ids(prices__has_any_keys=["usd", "x"]) == [1, 3]
    assert await ids(prices__eur__gt=5) == [2]
    assert await ids(point__lat__lt=0) == [2]
    assert await ids(point={"lon": 0.0, "lat": -1.0}) == [2]
    assert await ids(items__len=2) == [1, 2]
    assert await ids(items__sku__contains=["b-7"]) == [1, 2]
    assert await ids(labels__en__contains=["two"]) == [1, 2]
    assert await ids(deep__0__k__0__0=1) == [1, 2]


@pytest.mark.asyncio
async def test_updates_write_containers(shipments):
    await Shipment.objects.filter(id=2).update(tags=["x", "y"], prices={"gbp": decimal.Decimal("1.00")})
    second = await Shipment.objects.get(id=2)
    assert (second.tags, second.prices) == (["x", "y"], {"gbp": decimal.Decimal("1.00")})
    second.deep = [{"z": [(2, {1: None})]}]
    second.point = {"lon": 1.0, "lat": 1.0}
    await second.save()
    assert await Shipment.objects.filter(id=2).values_list("deep", "point") == [
        ([{"z": [(2, {1: None})]}], {"lon": 1.0, "lat": 1.0})
    ]
    await Shipment.objects.filter(id=2).update(scores=F("scores"))


@pytest.mark.asyncio
async def test_errors_name_the_path(clickhouse_db):
    cases = [
        ({"tags": "red"}, "tags: expected a list/tuple/set"),
        ({"tags": ["a" * 21]}, "tags[0]"),
        ({"grid": [[1, "x"]]}, "grid[0][1]"),
        ({"prices": {"eur": "abc"}}, "prices['eur']"),
        ({"point": {"lon": 1.0}}, "point: expected the elements"),
        ({"bounds": (1,)}, "bounds: expected 2 elements"),
        ({"items": [{"sku": "a", "quantity": None}]}, "items[0].quantity: a value is required"),
        ({"deep": [{"k": [(1, {10: "not-a-uuid"})]}]}, "deep[0]['k'][0].1[10]"),
    ]
    for values, message in cases:
        with pytest.raises(ValidationError, match=message.replace("[", r"\[").replace("]", r"\]").replace(".", r"\.")):
            await make_shipment(9, **values).save()


@pytest.mark.asyncio
async def test_inspectdb_reads_containers_and_drift_finds_none(clickhouse_db):
    from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
    from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
    from hare.migrations.drift import detect_drift
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    connection = Shipment._meta.connection
    table = await DatabaseCatalog.inspect_table(connection, "shipment")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "TODO" not in source
    assert "tags = fields.ArrayField(base_field=fields.TextField())" in source
    assert "scores = fields.ArrayField(base_field=fields.IntField(null=True))" in source
    assert "items = fields.NestedField(element_fields={'sku': fields.TextField()" in source
    state = State(models={}, apps=StateApps())
    state.models[("models", "Shipment")] = ModelState.make_from_model("models", Shipment)
    drift = await detect_drift(connection, state, ["models"])
    assert drift.mismatched_columns == []
    assert [
        operation for operation in drift.operations if getattr(operation, "model_name", "").lower() == "shipment"
    ] == []


@pytest.mark.asyncio
async def test_empty_containers_at_any_depth(clickhouse_db):
    # An empty array inside an array has no type of its own, and a column of no tuples no values.
    empties = {"tags": [], "grid": [[], []], "labels": {"en": []}, "items": [], "deep": [{"k": []}, {}]}
    await Shipment.objects.bulk_create([make_shipment(1, **empties), make_shipment(2, **empties)])
    await make_shipment(3, **empties).save()
    await Shipment.objects.filter(id=1).update(grid=[[]], deep=[])
    rows = await Shipment.objects.order_by("id").values("tags", "grid", "labels", "items", "deep")
    assert rows == [{**empties, "grid": [[]], "deep": []}, empties, empties]
    assert await Shipment.objects.filter(grid=[[], []]).order_by("id").values_list("id", flat=True) == [2, 3]


@pytest.mark.asyncio
async def test_a_moment_and_a_decimal_in_one_tuple(clickhouse_db):
    from hare.fields import DatetimeField, DecimalField, IntField, TupleField
    from hare.migrations.operations import CreateModel, DeleteModel
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    connection = Shipment._meta.connection
    field = TupleField([DatetimeField(), DecimalField(max_digits=10, decimal_places=2), IntField()])
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Stamped",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("stamp", field)],
        options={"table": "clickhouse_stamped"},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        value = (MOMENT, decimal.Decimal("12.50"), 7)
        types = connection.dialect.types
        await connection.copy(
            "clickhouse_stamped",
            ["id", "stamp"],
            [(1, types.get_db_value(field, value, None))],
            ["BIGINT", field.get_column_type(connection.dialect)],
        )
        rows = await connection.execute_dicts('SELECT "stamp" FROM "clickhouse_stamped"')
        assert types.get_python_value(field, rows[0]["stamp"]) == value
    finally:
        await DeleteModel(name="Stamped").run("models", state, dry_run=False, state_editor=editor)


@pytest.mark.asyncio
async def test_values_at_the_edges_of_their_types_round_trip_in_containers(clickhouse_db):
    lowest, highest = -(2**63), 2**63 - 1
    # A literal of an integer from 2**32 on is a UInt64, which shares no type with a negative one; a moment
    # before 1970 with a fraction of a second is written by its ticks at any depth.
    before_1970 = datetime.datetime(1959, 5, 1, 7, 23, 59, 572668, tzinfo=datetime.UTC)
    values = {
        "counts": [lowest, highest, 5],
        "totals": {lowest: highest, 2**40: -1},
        "marks": [(highest, before_1970), (lowest, before_1970)],
        "moments": [before_1970],
    }
    await Measurement.objects.create(id=1, **values)
    await Measurement.objects.bulk_create([Measurement(id=2, **values)])
    for measurement in await Measurement.objects.order_by("id"):
        assert {name: getattr(measurement, name) for name in values} == values
    assert await Measurement.objects.filter(counts__contains=[highest, lowest]).count() == 2
