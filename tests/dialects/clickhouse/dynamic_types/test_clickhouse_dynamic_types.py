"""``Dynamic`` and ``Variant`` columns on ClickHouse - each value kept with its own type: written and
read back on both drivers, filtered by values of their own type, read by a path of a type, refused
where they have no ClickHouse type."""

import datetime
import ipaddress
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from hare import fields
from hare.dialects.clickhouse.fields import DynamicField, VariantField
from hare.exceptions import ConfigurationError, UnSupportedError, ValidationError
from tests.dialects.clickhouse.dynamic_types.models import Sample

MOMENT = datetime.datetime(2024, 5, 6, 7, 8, 9, 123456, tzinfo=datetime.UTC)
KEY = uuid.UUID("00000000-0000-0000-0000-000000000042")

DYNAMIC_VALUES = {
    1: 5,
    2: "5",
    3: None,
    4: 2.5,
    5: True,
    6: Decimal("12.340"),
    7: datetime.date(2024, 1, 2),
    8: MOMENT,
    9: KEY,
    10: ipaddress.IPv4Address("10.0.0.1"),
    11: [1, None, 3],
    12: [[1, 2], [3]],
    13: (1, "a", None),
    14: {"a": [1.5], "b": []},
    15: 2**70,
    16: -7,
    17: "abc",
}


@pytest_asyncio.fixture
async def samples(clickhouse_dynamic_types_db):
    await Sample.objects.bulk_create(
        [Sample(id=sample_id, anything=value) for sample_id, value in DYNAMIC_VALUES.items()]
    )


async def ids(**filters):
    return await Sample.objects.filter(**filters).order_by("id").values_list("id", flat=True)


def test_column_types():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

    fields_map = Sample._meta.fields_map
    assert fields_map["anything"].get_column_type(CLICKHOUSE_DIALECT) == "Dynamic"
    assert fields_map["limited"].get_column_type(CLICKHOUSE_DIALECT) == "Dynamic(max_types=2)"
    assert fields_map["reading"].get_column_type(CLICKHOUSE_DIALECT) == "Variant(Array(Float64), Int32, String)"


@pytest.mark.asyncio
async def test_dynamic_values_round_trip(samples):
    read = dict(await Sample.objects.order_by("id").values_list("id", "anything"))
    assert read == DYNAMIC_VALUES
    assert type(read[5]) is bool and type(read[1]) is int and read[6].as_tuple().exponent == -3
    one = await Sample.objects.get(id=8)
    assert one.anything == MOMENT
    # A naive moment is the UTC wall clock.
    await Sample.objects.create(id=30, anything=MOMENT.replace(tzinfo=None))
    assert (await Sample.objects.get(id=30)).anything == MOMENT


@pytest.mark.asyncio
async def test_dynamic_filters_match_values_of_their_own_type(samples):
    assert await ids(anything=5) == [1]
    assert await ids(anything="5") == [2]
    assert await ids(anything__gt=0) == [1, 15]
    assert await ids(anything__lt=0) == [16]
    assert await ids(anything__range=(1, 10)) == [1]
    assert await ids(anything__in=[5, "abc", KEY]) == [1, 9, 17]
    assert await ids(anything__in=[None, 2.5]) == [3, 4]
    assert await ids(anything__isnull=True) == [3]
    assert await ids(anything=[1, None, 3]) == [11]
    assert await ids(anything={"a": [1.5], "b": []}) == [14]
    assert await ids(anything=MOMENT) == [8]
    assert await ids(anything=Decimal("12.340")) == [6]
    assert await ids(anything=Decimal("12.34")) == [6]
    assert await ids(anything__gt=Decimal("12.3")) == [6]
    assert await ids(anything__in=[2**70, -7]) == [15, 16]
    assert len(await ids(anything__not=5)) == len(DYNAMIC_VALUES) - 2
    assert 3 not in await ids(anything__not_in=[5])


@pytest.mark.asyncio
async def test_dynamic_paths_read_values_of_a_type(samples):
    assert await ids(anything__String__startswith="a") == [17]
    assert await ids(anything__Int64__gt=0) == [1]
    assert await ids(anything__Int128__gt=0) == [15]
    assert await Sample.objects.filter(id__in=[1, 2]).order_by("id").values_list("anything__Int64", flat=True) == [
        5,
        None,
    ]
    assert await Sample.objects.filter(anything__Float64__isnull=False).values_list("id", flat=True) == [4]


@pytest.mark.asyncio
async def test_dynamic_values_are_written_by_update(samples):
    await Sample.objects.filter(id=1).update(anything=[KEY])
    one = await Sample.objects.get(id=2)
    one.anything = {"k": MOMENT}
    await one.save()
    assert await Sample.objects.filter(id__in=[1, 2]).order_by("id").values_list("anything", flat=True) == [
        [KEY],
        {"k": MOMENT},
    ]


@pytest.mark.asyncio
async def test_dynamic_values_without_a_clickhouse_type_are_refused(clickhouse_dynamic_types_db):
    for value in ([1, "a"], b"bytes", 2**300, [[1], None], {1: "a", "b": "c"}, Decimal("NaN"), object()):
        with pytest.raises(ValidationError):
            await Sample.objects.create(id=40, anything=value)
    assert await Sample.objects.count() == 0


@pytest.mark.asyncio
async def test_variant_values(clickhouse_dynamic_types_db):
    await Sample.objects.bulk_create(
        [
            Sample(id=1, reading=5),
            Sample(id=2, reading="abc"),
            Sample(id=3, reading=[1.5, 2.0]),
            Sample(id=4, reading=None),
            Sample(id=5, reading="5"),
        ]
    )
    assert await Sample.objects.order_by("id").values_list("reading", flat=True) == [5, "abc", [1.5, 2.0], None, "5"]
    assert await ids(reading=5) == [1]
    assert await ids(reading="5") == [5]
    assert await ids(reading__in=[5, "abc"]) == [1, 2]
    assert await ids(reading__0__gt=3) == [1]
    assert await ids(reading__1__startswith="a") == [2]
    assert await ids(reading__isnull=True) == [4]
    assert await ids(reading=[1.5, 2.0]) == [3]
    with pytest.raises(ValidationError):
        await Sample.objects.create(id=9, reading={"a": 1})


def test_variant_declarations_are_checked():
    from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

    with pytest.raises(ConfigurationError):
        VariantField([fields.CharField(max_length=5), fields.TextField()]).get_column_type(CLICKHOUSE_DIALECT)
    with pytest.raises(UnSupportedError):
        VariantField([fields.IntField(null=True)]).get_column_type(CLICKHOUSE_DIALECT)
    for arguments in ([], fields.IntField(), [1]):
        with pytest.raises(ConfigurationError):
            VariantField(arguments)
    with pytest.raises(ConfigurationError):
        DynamicField(max_types=255)


@pytest.mark.asyncio
async def test_values_written_in_statements_read_as_bulk_loaded_ones(clickhouse_dynamic_types_db):
    # Written into the SQL text, not by the binary insert of bulk_create().
    for sample_id, value in DYNAMIC_VALUES.items():
        await Sample.objects.create(id=sample_id, anything=value, reading=None if value is None else sample_id)
    assert dict(await Sample.objects.order_by("id").values_list("id", "anything")) == DYNAMIC_VALUES
    assert await Sample.objects.filter(anything__Int64=5).values_list("reading", flat=True) == [1]


@pytest.mark.asyncio
async def test_a_value_beyond_the_types_kept_apart_is_read_back(clickhouse_dynamic_types_db):
    values = [1, "a", 2.5, Decimal("1.5"), [MOMENT], {"k": KEY}, (datetime.date(2024, 1, 1), None)]
    await Sample.objects.bulk_create([Sample(id=index, limited=value) for index, value in enumerate(values)])
    for index, value in enumerate(values):
        await Sample.objects.create(id=100 + index, limited=value)
    read = await Sample.objects.order_by("id").values_list("limited", flat=True)
    assert read == values + values


@pytest.mark.asyncio
async def test_arrays_of_dynamic_values(clickhouse_dynamic_types_db):
    bags = {1: [], 2: [1, "a", None], 3: [[], MOMENT], 4: [{}]}
    await Sample.objects.bulk_create([Sample(id=sample_id, bag=bag) for sample_id, bag in bags.items()])
    for sample_id, bag in bags.items():
        await Sample.objects.create(id=10 + sample_id, bag=bag)
    await Sample.objects.filter(id=11).update(bag=[KEY])
    read = dict(await Sample.objects.order_by("id").values_list("id", "bag"))
    assert read == {**bags, **{10 + sample_id: bag for sample_id, bag in bags.items()}, 11: [KEY]}


@pytest.mark.asyncio
async def test_inspectdb_reads_the_types_and_drift_finds_a_changed_one(clickhouse_dynamic_types_db):
    from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
    from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
    from hare.migrations.drift import detect_drift
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    connection = Sample._meta.connection
    table = await DatabaseCatalog.inspect_table(connection, "sample")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    for expected in ("anything = DynamicField(", "limited = DynamicField(max_types=2", "reading = VariantField("):
        assert expected in source, (expected, source)
    state = State(models={}, apps=StateApps())
    state.models[("models", "Sample")] = ModelState.make_from_model("models", Sample)

    async def get_altered_fields():
        drift = await detect_drift(connection, state, ["models"])
        assert [mismatch for mismatch in drift.mismatched_columns if mismatch.table == "sample"] == []
        return sorted(
            operation.name
            for operation in drift.operations
            if type(operation).__name__ == "AlterField" and operation.model_name.lower() == "sample"
        )

    assert await get_altered_fields() == []
    await connection.execute_script('ALTER TABLE "sample" MODIFY COLUMN "limited" Dynamic')
    try:
        assert await get_altered_fields() == ["limited"]
    finally:
        await connection.execute_script('ALTER TABLE "sample" MODIFY COLUMN "limited" Dynamic(max_types=2)')


@pytest.mark.asyncio
async def test_a_variant_of_fields_read_in_their_own_ways(clickhouse_dynamic_types_db):
    import ipaddress

    from hare.dialects.clickhouse.fields import FixedStringField
    from hare.migrations.operations import CreateModel, DeleteModel
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    connection = Sample._meta.connection
    # A FixedString is read as its padded bytes by one driver and as text by the other; an address
    # field takes an address object, though it converts a text too.
    field = VariantField([FixedStringField(6), fields.IPv4AddressField(), fields.BooleanField()], null=True)
    editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
    state = State(models={}, apps=StateApps())
    await CreateModel(
        name="Mixed",
        fields=[("id", fields.BigIntField(primary_key=True, generated=False)), ("value", field)],
        options={"table": "clickhouse_mixed"},
    ).run("models", state, dry_run=False, state_editor=editor)
    try:
        values = ["ab", ipaddress.IPv4Address("10.1.2.3"), True, None]
        types = connection.dialect.types
        await connection.copy(
            "clickhouse_mixed",
            ["id", "value"],
            [(index, types.get_db_value(field, value, None)) for index, value in enumerate(values)],
            ["BIGINT", field.get_column_type(connection.dialect)],
        )
        rows = await connection.execute_dicts('SELECT "value" FROM "clickhouse_mixed" ORDER BY "id"')
        assert [types.get_python_value(field, row["value"]) for row in rows] == values
    finally:
        await DeleteModel(name="Mixed").run("models", state, dry_run=False, state_editor=editor)
