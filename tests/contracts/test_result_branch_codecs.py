"""Selected literals use their result's storage codec without narrowing ordinary expressions."""

import sys
from dataclasses import make_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from ipaddress import IPv6Address
from types import ModuleType

import pytest
import pytest_asyncio

from hare import Model, fields
from hare.contrib.test import hare_test_context
from hare.core.hare_context import HareContext
from hare.core.registries import Registries
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.postgresql.fields import NaiveDatetimeField
from hare.query.expressions import Case, F, Value, When, Window
from hare.query.functions import Coalesce
from hare.query.functions.comparison import Cast, Greatest, Least, NullIf
from hare.query.functions.window import Lag, Lead
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.fields.field_declaration.subclass_fields import XorMaskedField
from tests.utils.database_under_test import DatabaseUnderTest


@pytest_asyncio.fixture(params=(False, True), ids=("field-hooks", "type-mapping"))
async def branch_model(monkeypatch, request):
    decimal_field = fields.DecimalField(max_digits=8, decimal_places=2)
    supports_decimal = decimal_field.exists_on(DatabaseUnderTest.get_dialect())
    previous_snapshot = Registries.snapshot
    value_field = XorMaskedField(null=True)
    if request.param:

        class MappedText(fields.CharField):
            pass

        DatabaseUnderTest.get_dialect().types.register(
            MappedText,
            TypeMapping(
                to_db=lambda field, value, model: None if value is None else value.swapcase(),
                to_python=lambda field, value: None if value is None else value.swapcase(),
            ),
        )
        value_field = MappedText(max_length=256, null=True)

    class BranchRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        value = value_field
        short_text = fields.CharField(max_length=1)
        number = fields.IntField()
        if supports_decimal:
            amount = decimal_field

    module = ModuleType("tests._result_branch_codecs")
    module.__models__ = [BranchRecord]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    try:
        async with hare_test_context([module.__name__], DatabaseUnderTest.get_url()):
            await BranchRecord.objects.bulk_create(
                [
                    BranchRecord(
                        id=index,
                        value=value,
                        short_text="x",
                        number=index,
                        **({"amount": Decimal("1.25")} if supports_decimal else {}),
                    )
                    for index, value in enumerate(("hello", None, "other"), 1)
                ]
            )
            yield BranchRecord
    finally:
        if request.param:
            Registries.snapshot = previous_snapshot
            Registries.changed()


@pytest.mark.asyncio
async def test_default_comparisons_and_window_defaults_follow_storage_conversion(branch_model):
    field = HareContext.require_current().apps.get_binding(branch_model).fields_map["value"]
    types = branch_model.get_connection().dialect.types
    assert types.get_lookup_value(field, "hello", branch_model) == "HELLO"
    assert types.get_lookup_writer(field)("hello", branch_model) == "HELLO"
    assert types.get_lookup_values(field, ["hello", "other"], branch_model) == ["HELLO", "OTHER"]
    assert await branch_model.objects.filter(value="hello").values_list("id", flat=True) == [1]
    assert await branch_model.objects.filter(value__in=["hello", "other"]).order_by("id").values_list(
        "id", flat=True
    ) == [1, 3]
    assert await branch_model.objects.filter(value__isnull=False).order_by("value", "id").after_cursor(
        "hello", 1
    ).values_list("id", flat=True) == [3]
    assert await branch_model.objects.order_by("id").annotate(
        previous=Window(Lag("value", 1, "world"), order_by=("id",))
    ).values_list("previous", flat=True) == ["world", "hello", None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression_type", "wrapped"),
    [
        pytest.param("case", False, id="case-raw"),
        pytest.param("case", True, id="case-value"),
        pytest.param("coalesce", False, id="coalesce-raw"),
        pytest.param("coalesce", True, id="coalesce-value"),
        pytest.param("literal_first", True, id="literal-first-value"),
    ],
)
async def test_selected_literals_follow_the_custom_result_codec(branch_model, wrapped, expression_type):
    for text in ("world", "later", "world"):
        literal = Value(text) if wrapped else text
        if expression_type == "case":
            expression = Case(When(id=1, then=literal), default=F("value"))
            expected = [text, None, "other"]
        elif expression_type == "coalesce":
            expression = Coalesce("value", literal)
            expected = ["hello", text, "other"]
        else:
            expression = Coalesce(Value(text), F("value"))
            expected = [text] * 3
        query = branch_model.objects.order_by("id").annotate(selected=expression)
        assert await query.values_list("selected", flat=True) == expected
        assert [row["selected"] for row in await query.values("selected")] == expected
        assert [row.selected for row in await query] == expected
        assert await branch_model.objects.order_by("id").values_list("value", flat=True) == ["hello", None, "other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", (False, True))
async def test_case_writes_preserve_custom_storage_and_returned_values(branch_model, wrapped):
    literal = Value("world") if wrapped else "world"
    query = branch_model.objects.all().update(value=Case(When(id=1, then=literal), default=F("value")))
    if DatabaseUnderTest.get_dialect().features.supports_returning:
        rows = await query.returning()
        assert sorted((row.id, row.value) for row in rows) == [(1, "world"), (2, None), (3, "other")]
    else:
        await query
    assert await branch_model.objects.order_by("id").values_list("value", flat=True) == ["world", None, "other"]


@pytest.mark.asyncio
async def test_result_codecs_keep_numeric_widening_and_plain_text_literals(branch_model):
    class TextValue(StrEnum):
        LONG = "longer"

    query = branch_model.objects.order_by("id").annotate(
        text=Case(When(id=1, then=TextValue.LONG), default=F("short_text")),
        number_result=Case(When(id=1, then=2.5), default=F("number")),
    )
    assert await query.values_list("text", "number_result") == [("longer", 2.5), ("x", 2.0), ("x", 3.0)]
    if "amount" in HareContext.require_current().apps.get_binding(branch_model).fields_map:
        query = query.annotate(decimal_result=Case(When(id=1, then=Decimal("2.555")), default=F("amount")))
        assert await query.values_list("decimal_result", flat=True) == [
            Decimal("2.555"),
            Decimal("1.250"),
            Decimal("1.250"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("function", [Coalesce, Greatest, Least, NullIf, Case])
@pytest.mark.parametrize("literal_first", [False, True])
async def test_selected_network_literals_keep_the_columns_sql_type(contract_database, function, literal_first):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("The inet parameter type is specific to this database")

    class NetworkRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        address = fields.IPAddressField()

    contract_database.register_live_models([NetworkRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    stored = IPv6Address("2001:db8::2")
    await NetworkRecord.objects.create(id=1, address=stored)
    for suffix in (1, 2, 3, 1):
        literal = IPv6Address(f"2001:db8::{suffix}")
        main, other = (literal, stored) if literal_first else (stored, literal)
        if function in (Coalesce, Case):
            expected = main
        elif function is Greatest:
            expected = max(main, other)
        elif function is Least:
            expected = min(main, other)
        else:
            expected = None if main == other else main
        if function is Case:
            expression = (
                Case(When(id=1, then=Value(literal)), default=F("address"))
                if literal_first
                else Case(When(id=1, then=F("address")), default=Value(literal))
            )
        else:
            expression = (
                function(Value(literal), F("address")) if literal_first else function("address", Value(literal))
            )
        query = NetworkRecord.objects.annotate(selected=expression)
        actual = (await query.values_list("selected", flat=True))[0]
        assert actual == expected
        assert actual is None or type(actual) is IPv6Address


@pytest.mark.asyncio
@pytest.mark.parametrize("literal_first", [False, True])
async def test_selected_wall_clock_literals_keep_the_columns_timezone_contract(contract_database, literal_first):
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("The explicit timestamp parameter type is specific to this database")

    class WallClockRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        stamp = NaiveDatetimeField()

    contract_database.register_live_models([WallClockRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    stored = datetime(2020, 1, 2, 3, 4)
    await WallClockRecord.objects.create(id=1, stamp=stored)
    for day in (3, 4, 3):
        literal = datetime(2020, 1, day, 5, 6)
        expression = Coalesce(Value(literal), F("stamp")) if literal_first else Coalesce("stamp", Value(literal))
        actual = (await WallClockRecord.objects.annotate(selected=expression).values_list("selected", flat=True))[0]
        assert actual == (literal if literal_first else stored)
        assert actual.tzinfo is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["literal-first", "field-first", "case"])
async def test_selected_map_literals_keep_the_map_type_and_nested_codecs(contract_database, route):
    if DatabaseUnderTest.get_dialect().name != "clickhouse":
        pytest.skip("Map columns are specific to this database")

    class MapRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        labels = fields.MapField(XorMaskedField(), fields.ArrayField(XorMaskedField()))

        class Meta:
            table = "mapped_literal_values"

    contract_database.register_live_models([MapRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    original = [{"Ab": ["cd"]}, {"Ef": ["gh"]}]
    await MapRecord.objects.bulk_create([MapRecord(id=index, labels=value) for index, value in enumerate(original, 1)])
    for literal in ({"Qr": ["uv"]}, {"Xy": ["ab"]}, {"Qr": ["uv"]}):
        if route == "literal-first":
            expression = Coalesce(Value(literal), F("labels"))
            expected = [literal, literal]
        elif route == "field-first":
            expression = Coalesce("labels", Value(literal))
            expected = original
        else:
            expression = Case(When(id=1, then=literal), default=F("labels"))
            expected = [literal, original[1]]
        query = MapRecord.objects.order_by("id").annotate(selected=expression)
        assert await query.values_list("selected", flat=True) == expected
        assert await query.values("selected") == [{"selected": value} for value in expected]
        assert await MapRecord.objects.order_by("id").values_list("labels", flat=True) == original
    raw = await MapRecord.get_connection().execute_dicts(
        'SELECT mapKeys("labels") AS keys, mapValues("labels") AS vals FROM mapped_literal_values ORDER BY id'
    )
    assert raw == [{"keys": ["aB"], "vals": [["CD"]]}, {"keys": ["eF"], "vals": [["GH"]]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["enum", "callback", "unhashable_callback"])
async def test_cast_plans_keep_the_current_output_fields_reader(contract_database, configuration):
    class ReaderText(fields.CharField):
        def __init__(self, reader, **kwargs):
            self.reader = reader
            super().__init__(**kwargs)

        def from_db_value(self, value):
            return self.reader(super().from_db_value(value))

    class CastRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)

    contract_database.register_live_models([CastRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await CastRecord.objects.create(id=1)
    if configuration == "enum":
        first = Enum("Choice", {"FIRST": "literal", "SECOND": "other"})
        second = Enum("Choice", {"FIRST": "literal", "SECOND": "other"})
        variants = [(fields.CharEnumField(enum, max_length=20), enum) for enum in (first, second)]
    else:
        if configuration == "unhashable_callback":
            reader_class = make_dataclass(
                "Reader", [("tag", str)], namespace={"__call__": lambda self, value: f"{self.tag}:{value}"}
            )
            readers = [reader_class(tag) for tag in ("first", "second")]
            assert all(reader.__hash__ is None for reader in readers)
        else:
            readers = [(lambda value, tag=tag: f"{tag}:{value}") for tag in ("first", "second")]
        variants = [(ReaderText(reader, max_length=20), reader) for reader in readers]
    for index in (0, 1, 0, 1):
        field, read = variants[index]
        value = "literal" if index == 0 else "other"
        expected = read(value)
        query = CastRecord.objects.annotate(result=Cast(Value(value), field))
        flat = await query.values_list("result", flat=True)
        mapping = await query.values("result")
        obj = await query.get(id=1)
        for actual in (flat[0], mapping[0]["result"], obj.result):
            assert actual == expected
            assert type(actual) is type(expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("filter", "cursor", "lag", "lead", "case", "coalesce", "literal-first"))
async def test_enum_read_expressions_compare_the_columns_read_representation(contract_database, operation):
    class Color(Enum):
        RED = "red"
        BLUE = "blue"

    class ColorRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        color = fields.CharEnumField(Color)

    contract_database.register_live_models([ColorRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await ColorRecord.objects.bulk_create([ColorRecord(id=1, color=Color.RED), ColorRecord(id=2, color=Color.BLUE)])
    for default in (Color.RED, Color.BLUE, Color.RED):
        if operation == "filter":
            assert await ColorRecord.objects.filter(color=default).values_list("id", flat=True) == [
                1 if default is Color.RED else 2
            ]
            continue
        if operation == "cursor":
            key = 1 if default is Color.RED else 2
            assert await ColorRecord.objects.order_by("color", "id").after_cursor(default, key).values_list(
                "id", flat=True
            ) == ([] if default is Color.RED else [1])
            continue
        if operation == "lag":
            expression = Window(Lag("color", 1, default), order_by=("id",))
            expected = [default, Color.RED]
        elif operation == "lead":
            expression = Window(Lead("color", 1, default), order_by=("id",))
            expected = [Color.BLUE, default]
        elif operation == "case":
            expression = Case(When(id=1, then=default), default=F("color"))
            expected = [default, Color.BLUE]
        elif operation == "coalesce":
            expression = Coalesce("color", default)
            expected = [Color.RED, Color.BLUE]
        else:
            expression = Coalesce(Value(default), F("color"))
            expected = [default, default]
        values = await ColorRecord.objects.annotate(result=expression).order_by("id").values_list("result", flat=True)
        assert values == expected
        assert all(type(value) is Color for value in values)


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["char-array", "int-array", "map", "tuple"])
@pytest.mark.parametrize("operation", ["case-then", "case-default", "cursor"])
async def test_enum_container_literals_keep_storage_type_before_read_conversion(
    contract_database, container, operation
):
    if contract_database.get_connection().dialect.name != "clickhouse":
        pytest.skip("ClickHouse enum storage types")

    class Color(StrEnum):
        LAST = "zebra"
        FIRST = "apple"

    class Priority(IntEnum):
        FIRST = 20
        LAST = 1

    if container == "char-array":
        field = fields.ArrayField(fields.CharEnumField(Color))
        first, second = [Color.LAST], [Color.FIRST]
    elif container == "int-array":
        field = fields.ArrayField(fields.IntEnumField(Priority))
        first, second = [Priority.FIRST], [Priority.LAST]
    elif container == "map":
        field = fields.MapField(fields.CharEnumField(Color), fields.ArrayField(fields.IntEnumField(Priority)))
        first, second = {Color.LAST: [Priority.FIRST]}, {Color.FIRST: [Priority.LAST]}
    else:
        field = fields.TupleField([fields.CharEnumField(Color), fields.IntEnumField(Priority)])
        first, second = (Color.LAST, Priority.FIRST), (Color.FIRST, Priority.LAST)

    class ContainerRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        payload = field

    contract_database.register_live_models([ContainerRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    original = [first, second, first]
    await ContainerRecord.objects.bulk_create(
        [ContainerRecord(id=index, payload=value) for index, value in enumerate(original, 1)]
    )
    if operation == "cursor":
        ordered = [(second, 2), (first, 1), (first, 3)]
        ordered_ids = [2, 1, 3]
        for position in (0, 1, 2, 0):
            value, key = ordered[position]
            query = ContainerRecord.objects.order_by("payload", "id")
            assert await query.after_cursor(value, key).values_list("id", flat=True) == ordered_ids[position + 1 :]
            assert await query.before_cursor(value, key).values_list("id", flat=True) == ordered_ids[:position]
        return
    for literal in (first, second, first):
        if operation == "case-then":
            expression = Case(When(id=1, then=literal), default=F("payload"))
            expected = [literal, second, first]
        else:
            expression = Case(When(id=1, then=F("payload")), default=literal)
            expected = [first, literal, literal]
        query = ContainerRecord.objects.annotate(selected=expression).order_by("id")
        actual = await query.values_list("selected", flat=True)
        assert actual == expected
        assert [type(value) for value in actual] == [type(value) for value in expected]
        assert await ContainerRecord.objects.order_by("id").values_list("payload", flat=True) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("argument", ["raw", "custom-raw", "custom-value"])
async def test_container_literal_typing_preserves_sql_null_and_custom_value_conversion(contract_database, argument):
    if contract_database.get_connection().dialect.name != "clickhouse":
        pytest.skip("ClickHouse non-nullable container types")

    class Color(StrEnum):
        RED = "red"
        BLUE = "blue"

    class ContainerRecord(Model):
        id = fields.IntField(primary_key=True, generated=False)
        payload = fields.ArrayField(fields.CharEnumField(Color))

    class CustomDefault(Coalesce):
        converted = []

        @staticmethod
        def encode(value):
            CustomDefault.converted.append(value)
            return [Color.BLUE] if value is None else value

        def _get_default_literal_encoder(self, value, function_arg, expression_context):
            return self.encode

    contract_database.register_live_models([ContainerRecord], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    await ContainerRecord.objects.create(id=1, payload=[Color.RED])
    expression = (
        Coalesce("payload", None)
        if argument == "raw"
        else CustomDefault("payload", Value(None) if argument == "custom-value" else None)
    )
    assert await ContainerRecord.objects.annotate(selected=expression).values_list("selected", flat=True) == [
        [Color.RED]
    ]
    assert CustomDefault.converted == ([None] if argument == "custom-value" else [])
