"""A ValidationError for a ``sensitive=True`` field keeps the value out of its message and out of
its exception chain - the original exception (whose text names the value) is neither
``__cause__`` nor ``__context__``, so logging/Sentry/OpenTelemetry tracebacks don't show it."""

import datetime
import traceback
from enum import IntEnum, StrEnum
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hare import Model, fields
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.hare_context import HareContext
from hare.dialects.clickhouse.fields import ClickhouseDateField, ClickhouseDatetimeField, DynamicField, Float32Field
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields import CidrField, HStoreField, InetField
from hare.dialects.postgresql.fields.ranges import IntRangeField
from hare.exceptions import DoesNotExist, OperationalError, ValidationError
from hare.fields import ArrayField, Field
from hare.fields.validators import MaxLengthValidator
from hare.models.model_meta import ModelMeta
from tests.utils.fake_client import FakeClient

pytestmark = pytest.mark.database_independent

SECRET = "SECRET4111"
SECRET_NUMBER = 4111


class Color(StrEnum):
    RED = "red"


class Level(IntEnum):
    LOW = 1


def named_field(field: Field[Any]) -> Field[Any]:
    field.model_field_name = "secret"
    return field


def write_to_a_declared_type() -> Any:
    from pydantic import BaseModel

    class Card(BaseModel):
        number: int

    return named_field(fields.JSONField(field_type=Card, sensitive=True)).to_db_value({"number": SECRET}, None)


def get_error_chain(error: BaseException) -> list[BaseException]:
    chain = []
    pending: BaseException | None = error
    while pending is not None and pending not in chain:
        chain.append(pending)
        pending = pending.__cause__ or pending.__context__
    return chain


SENSITIVE_CONVERSIONS: list[Any] = [
    ("inet-write", lambda: named_field(InetField(sensitive=True)).to_db_value(SECRET, None)),
    ("inet-assignment", lambda: named_field(InetField(sensitive=True)).to_python(SECRET)),
    ("cidr-write", lambda: named_field(CidrField(sensitive=True)).to_db_value(SECRET, None)),
    ("cidr-assignment", lambda: named_field(CidrField(sensitive=True)).to_python(SECRET)),
    ("hstore-read", lambda: named_field(HStoreField(sensitive=True)).from_db_value(SECRET)),
    ("int", lambda: named_field(fields.IntField(sensitive=True)).to_db_value(SECRET, None)),
    ("int-read", lambda: named_field(fields.IntField(sensitive=True)).from_db_value(SECRET)),
    ("char-length", lambda: named_field(fields.CharField(max_length=4, sensitive=True)).to_db_value(SECRET, None)),
    (
        "validator",
        lambda: named_field(fields.TextField(sensitive=True, validators=[MaxLengthValidator(4)])).to_db_value(
            SECRET, None
        ),
    ),
    ("datetime", lambda: named_field(fields.DatetimeField(sensitive=True)).from_db_value(SECRET)),
    ("date", lambda: named_field(fields.DateField(sensitive=True)).from_db_value(f"2020-01-01{SECRET}")),
    ("char-enum", lambda: named_field(fields.CharEnumField(Color, sensitive=True)).to_db_value(SECRET, None)),
    ("char-enum-read", lambda: named_field(fields.CharEnumField(Color, sensitive=True)).from_db_value(SECRET)),
    ("int-enum", lambda: named_field(fields.IntEnumField(Level, sensitive=True)).to_db_value(SECRET_NUMBER, None)),
    ("int-enum-read", lambda: named_field(fields.IntEnumField(Level, sensitive=True)).from_db_value(SECRET_NUMBER)),
    ("json-read", lambda: named_field(fields.JSONField(sensitive=True)).from_db_value(f'{{"card": "{SECRET}"')),
    ("uuid-assignment", lambda: named_field(fields.UUIDField(sensitive=True)).to_python(SECRET.rjust(32, "x"))),
    ("uuid-write", lambda: named_field(fields.UUIDField(sensitive=True)).to_db_value(SECRET.rjust(32, "x"), None)),
    ("uuid-read", lambda: named_field(fields.UUIDField(sensitive=True)).from_db_value(SECRET.rjust(32, "x"))),
    (
        "ip-zone-write",
        lambda: named_field(fields.IPAddressField(sensitive=True)).to_db_zoneless_value(
            f"fe80::{SECRET_NUMBER}%example", None
        ),
    ),
    (
        "float32-overflow",
        lambda: named_field(Float32Field(sensitive=True)).to_db_value(float(f"1.{SECRET_NUMBER}e100"), None),
    ),
    (
        "float32-underflow",
        lambda: named_field(Float32Field(sensitive=True)).to_db_value(float(f"1.{SECRET_NUMBER}e-100"), None),
    ),
    (
        "clickhouse-date-range",
        lambda: named_field(ClickhouseDateField(sensitive=True)).to_db_value(datetime.date(SECRET_NUMBER, 1, 1), None),
    ),
    (
        "clickhouse-datetime-range",
        lambda: ClickhouseDatetimeField.to_db_rounded_value(
            named_field(ClickhouseDatetimeField(sensitive=True)),
            datetime.datetime(SECRET_NUMBER, 1, 1, tzinfo=datetime.UTC),
            None,
        ),
    ),
    pytest.param("json-declared-type", write_to_a_declared_type, marks=pytest.mark.requires("pydantic")),
    ("timedelta", lambda: named_field(fields.TimeDeltaField(sensitive=True)).get_timedelta(SECRET_NUMBER * 10**20)),
    (
        "array-element",
        lambda: named_field(ArrayField(fields.CharField(max_length=4), sensitive=True)).to_db_value([SECRET], None),
    ),
    ("range-bound", lambda: named_field(IntRangeField(sensitive=True)).to_db_value((SECRET, 5), None)),
    ("range-text", lambda: named_field(IntRangeField(sensitive=True)).from_db_value(f"[{SECRET},5)")),
    (
        "map-value-write",
        lambda: named_field(fields.MapField(fields.TextField(), fields.IntField(), sensitive=True)).to_db_value(
            {SECRET: SECRET}, None
        ),
    ),
    (
        "map-value-assignment",
        lambda: named_field(fields.MapField(fields.TextField(), fields.IntField(), sensitive=True)).to_python(
            {SECRET: SECRET}
        ),
    ),
    (
        "tuple-element-name",
        lambda: named_field(fields.TupleField({"number": fields.IntField()}, sensitive=True)).to_db_value(
            {SECRET: 1}, None
        ),
    ),
    (
        "array-map-value",
        lambda: named_field(
            ArrayField(fields.MapField(fields.TextField(), fields.IntField()), sensitive=True)
        ).to_db_value([{SECRET: SECRET}], None),
    ),
    (
        "tuple-array-value",
        lambda: named_field(
            fields.TupleField({"numbers": ArrayField(fields.CharField(max_length=4))}, sensitive=True)
        ).to_db_value({"numbers": [SECRET]}, None),
    ),
    (
        "array-tuple-element-name",
        lambda: named_field(ArrayField(fields.TupleField({"number": fields.IntField()}), sensitive=True)).to_db_value(
            [{SECRET: 1}], None
        ),
    ),
    (
        "dynamic-prepared-write",
        lambda: DialectRegistry.get_dialect("clickhouse").types.get_db_writer(
            named_field(DynamicField(sensitive=True))
        )({SECRET: 2**300}, None),
    ),
    (
        "dynamic-lookup",
        lambda: DialectRegistry.get_dialect("clickhouse").types.get_lookup_writer(
            named_field(DynamicField(sensitive=True))
        )({SECRET: 2**300}, None),
    ),
]


@pytest.mark.parametrize(
    ("case", "convert"),
    SENSITIVE_CONVERSIONS,
    ids=[getattr(case, "values", case)[0] for case in SENSITIVE_CONVERSIONS],
)
def test_sensitive_validation_error_has_no_chained_exception(case, convert):
    with pytest.raises(ValidationError) as error_info:
        convert()
    error = error_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "4111" not in "".join(traceback.format_exception(error))
    assert all("4111" not in str(chained) for chained in get_error_chain(error))


def test_non_sensitive_validation_error_keeps_its_cause():
    with pytest.raises(ValidationError) as error_info:
        named_field(fields.IntField()).from_db_value(SECRET)
    assert isinstance(error_info.value.__cause__, ValueError)
    assert SECRET in str(error_info.value)


@pytest.mark.parametrize("sensitive_key", [False, True])
@pytest.mark.parametrize("prepared", [False, True])
def test_map_error_path_respects_key_sensitivity(sensitive_key, prepared):
    field = named_field(fields.MapField(fields.TextField(sensitive=sensitive_key), fields.IntField()))
    writer = field.get_value_writer(None) if prepared else field.to_db_value
    with pytest.raises(ValidationError) as error_info:
        writer({SECRET: "not-an-integer"}, None)
    assert (SECRET not in str(error_info.value)) is sensitive_key
    assert "expected an int" in str(error_info.value)


def test_sensitive_container_marks_all_descendants_without_changing_other_fields():
    leaf = fields.CharField(max_length=4)
    nested = fields.MapField(fields.TextField(), ArrayField(leaf))
    root = fields.TupleField({"items": nested}, sensitive=True)
    assert root.sensitive and nested.sensitive and nested.key_field.sensitive
    assert nested.value_field.sensitive and leaf.sensitive
    assert not fields.IntField().sensitive
    assert root.to_db_value({"items": {"key": ["ok"]}}, None) == ({"key": ["ok"]},)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, ValidationError])
async def test_sensitive_model_key_conversion_keeps_no_original_exception(error_type):
    declaration_graph = ModelBindingGraph()

    class Credential(Model):
        code = fields.TextField(primary_key=True, generated=False, sensitive=True)

    binding = declaration_graph.include(Credential)
    queryset = SimpleNamespace(get=AsyncMock(side_effect=error_type(SECRET)))
    binding.manager = SimpleNamespace(create_queryset=lambda **kwargs: queryset)
    context = HareContext()
    with pytest.raises(DoesNotExist) as caught:
        await ModelMeta._get_by_primary_key(binding, context, SECRET)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize("old_sensitive", [False, True])
async def test_conversion_preflight_hides_sensitive_server_errors(old_sensitive):
    client = FakeClient("clickhouse")
    editor = client.dialect.schema_editor_class(client)
    old_field = fields.TextField(sensitive=old_sensitive)
    new_field = fields.IntField(sensitive=not old_sensitive)
    calls = 0

    async def execute_dicts(sql):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [{"old_type": "String", "new_type": "Int32"}]
        raise OperationalError(SECRET)

    client.execute_dicts = execute_dicts
    with pytest.raises(OperationalError) as caught:
        await editor.column_conversion_check.check_alter_field_conversion('"entry"', old_field, new_field, '"value"')
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(caught.value))
