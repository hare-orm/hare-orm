"""Model hydration preserves field values and errors across raw driver representations."""

import datetime
import uuid
from decimal import Decimal

import pytest

from hare import fields
from hare.contrib import test
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import ValidationError
from hare.fields.data.choices import CharEnumFieldInstance, IntEnumFieldInstance
from hare.query.rows.model_rows.model_columns import ModelColumns
from hare.query.rows.native.hydration_layout import HydrationLayout
from hare.query.rows.row_column import RowColumn
from tests.shared_models.testmodels import (
    BooleanFields,
    Currency,
    DateFields,
    DatetimeFields,
    EnumFields,
    JSONFields,
    Service,
    TimeDeltaFields,
    TimeFields,
    UUIDFields,
)
from tests.shared_models.testmodels_postgres import DecimalFields
from tests.utils.model_rows_queries import get_model_rows_query
from tests.utils.row_hydration import RowHydration
from tests.utils.timezone_context import override_timezone


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("field_class", "args"),
    [
        (fields.DatetimeField, ()),
        (fields.DateField, ()),
        (fields.TimeField, ()),
        (fields.TimeDeltaField, ()),
        (fields.UUIDField, ()),
        (fields.DecimalField, (10, 2)),
        (fields.JSONField, ()),
        (IntEnumFieldInstance, (Service,)),
        (CharEnumFieldInstance, (Currency,)),
    ],
)
def test_hydration_calls_custom_field_reader(field_class, args):
    calls = []
    converted = object()

    class CustomField(field_class):
        def from_db_value(self, value):
            calls.append(value)
            return converted

    field = CustomField(*args)
    reader = HydrationLayout.get_field_reader(field, SQL_DIALECT.types, frozenset())
    raw = object()
    assert RowColumn(0, reader).read((raw,)) is converted
    assert calls == [raw]


def _get_decode_plan(model, only_fields: list[str] | None = None):
    """Builds a real decode_plan for `model` from query shape alone - works on an empty table,
    since decode_plan only reflects the SELECT column list, not any actual row data. Restricting
    to `only_fields` (always include the pk) keeps a branch-coverage test from having to invent
    a plausible synthetic value for every other field on the model."""
    qs = model.objects.all()
    if only_fields:
        qs = qs.only(*only_fields)
    return RowHydration.get_decode_plan(qs)


def _synthetic_row(decode_plan, values_by_name: dict) -> tuple:
    """One row tuple in decode_plan's column order, sourcing each column's value from
    values_by_name (by model field name) - lets a test hand-craft exact raw values (an invalid
    enum int, malformed JSON text, a bool column stored as a plain int, a UUID column stored as
    a plain string) that a real driver round-trip may never actually produce, either because a
    given backend doesn't shape data that way or because the ORM's own write-path validation
    would reject it before it ever reached storage."""
    return tuple(values_by_name[name] for name, _field, _reader in decode_plan)


def _hydrate_models(model, decode_plan, is_partial: bool, rows: list[tuple]):
    columns = ModelColumns(model, decode_plan, tuple(range(len(decode_plan))), is_partial)
    return [columns.hydrate(row, model.get_connection().connection_alias) for row in rows]


def _hydrate_both(model, decode_plan, is_partial: bool, rows: list[tuple]):
    python_instances = [RowHydration.hydrate_in_python(model, row, decode_plan) for row in rows]
    model_instances = _hydrate_models(model, decode_plan, is_partial, rows)
    return python_instances, model_instances


async def _assert_models_match_fields(model, create_kwargs_list: list[dict]) -> None:
    await model.objects.bulk_create([model(**kwargs) for kwargs in create_kwargs_list])

    compiler = get_model_rows_query(model.objects.all())
    compiler._make_query()
    sql, values = compiler.query.get_parameterized_sql()
    _, raw_rows = await compiler._connection.execute(sql, values)
    assert len(raw_rows) == len(create_kwargs_list)

    decode_plan = compiler._decode_plan
    assert decode_plan is not None, f"{model.__name__}.all() didn't produce a decode_plan"
    is_partial = compiler._decode_plan_is_partial
    plan_names = [name for name, _field, _reader in decode_plan]

    python_instances = [RowHydration.hydrate_in_python(model, row, decode_plan) for row in raw_rows]
    model_instances = _hydrate_models(model, decode_plan, is_partial, list(raw_rows))
    assert len(model_instances) == len(python_instances)

    for python_instance, model_instance in zip(python_instances, model_instances, strict=True):
        for field_name in plan_names:
            python_value = getattr(python_instance, field_name)
            model_value = getattr(model_instance, field_name)
            assert model_value == python_value, (
                f"{model.__name__}.{field_name}: model hydration gave {model_value!r}, "
                f"python hydration gave {python_value!r}"
            )
            assert type(model_value) is type(python_value), (
                f"{model.__name__}.{field_name}: model hydration gave a {type(model_value).__name__}, "
                f"python hydration gave a {type(python_value).__name__}"
            )


@pytest.mark.asyncio
async def test_boolean_fields_values(db):
    """Boolean columns preserve bool values and nullable values through their selected reader."""
    await _assert_models_match_fields(
        BooleanFields,
        [
            {"boolean": True, "boolean_null": False},
            {"boolean": False, "boolean_null": None},
        ],
    )


@pytest.mark.postgresql_models
@pytest.mark.asyncio
async def test_decimal_fields_values(db):
    await _assert_models_match_fields(
        DecimalFields,
        [
            {"decimal": Decimal("1234.5678"), "decimal_nodec": Decimal("42"), "decimal_null": Decimal("0.0001")},
            {"decimal": Decimal("0.0000"), "decimal_nodec": Decimal("0"), "decimal_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_datetime_fields_values(db):
    """datetime is deliberately not in DB_NATIVE-shortcut territory here - from_db_value is
    overridden (Timezone.localtime), so every configured Timezone (not just UTC) must match."""
    now = datetime.datetime(2024, 3, 15, 12, 30, 45, 123456, tzinfo=datetime.timezone.utc)
    await _assert_models_match_fields(
        DatetimeFields,
        [
            {"datetime": now, "datetime_null": now},
            {"datetime": now, "datetime_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_timedelta_fields_values(db):
    await _assert_models_match_fields(
        TimeDeltaFields,
        [
            {
                "timedelta": datetime.timedelta(hours=1, minutes=2, seconds=3),
                "timedelta_null": datetime.timedelta(days=1),
            },
            {"timedelta": datetime.timedelta(0), "timedelta_null": None},
        ],
    )


@pytest.mark.parametrize(
    ("model_class", "rows"),
    [
        pytest.param(
            DateFields,
            [
                {"date": datetime.date(2024, 3, 15), "date_null": datetime.date(2000, 1, 1)},
                {"date": datetime.date(1999, 12, 31), "date_null": None},
            ],
            id="date_fields",
        ),
        pytest.param(
            TimeFields,
            [
                {"time": datetime.time(12, 30, 45), "time_null": datetime.time(0, 0, 0)},
                {"time": datetime.time(23, 59, 59), "time_null": None},
            ],
            id="time_fields",
        ),
    ],
)
@pytest.mark.asyncio
async def test_date_and_time_fields_hydrate_alike(db, model_class, rows):
    await _assert_models_match_fields(model_class, rows)


@pytest.mark.asyncio
async def test_json_fields_values(db):
    await _assert_models_match_fields(
        JSONFields,
        [
            {"data": {"a": 1, "b": [1, 2, 3]}, "data_null": {"x": "y"}},
            {"data": [1, 2, 3], "data_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_uuid_fields_values(db):
    await _assert_models_match_fields(
        UUIDFields,
        [
            {"data": uuid.uuid4(), "data_null": uuid.uuid4()},
            {"data": uuid.uuid4(), "data_null": None},
        ],
    )


@pytest.mark.asyncio
async def test_enum_fields_values(db):
    await _assert_models_match_fields(
        EnumFields,
        [
            {"service": Service.python_programming, "currency": Currency.EUR},
            {"service": Service.system_administration, "currency": Currency.USD},
        ],
    )


@pytest.mark.asyncio
@test.requires_features(dialect="sqlite")
async def test_boolean_integer_and_boolean_values(db):
    decode_plan, is_partial = _get_decode_plan(BooleanFields, only_fields=["id", "boolean", "boolean_null"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "boolean": True, "boolean_null": False}),  # already bool
        _synthetic_row(decode_plan, {"id": 2, "boolean": 1, "boolean_null": 0}),  # int - needs bool()
        _synthetic_row(decode_plan, {"id": 3, "boolean": 0, "boolean_null": None}),
    ]
    python_instances, model_instances = _hydrate_both(BooleanFields, decode_plan, is_partial, rows)
    for python_instance, model_instance in zip(python_instances, model_instances, strict=True):
        assert model_instance.boolean == python_instance.boolean
        assert type(model_instance.boolean) is bool
        assert model_instance.boolean_null == python_instance.boolean_null


@pytest.mark.asyncio
async def test_enum_invalid_value_raises_same_error(db):
    """A raw int with no matching enum member (the map-lookup-miss branch) must fall back to
    field.from_db_value() and raise the exact same error Python's own EnumClass(value) would -
    the ORM's own write-path validation means this can never come from a normal .create() +
    round-trip, only from data written some other way (a migration, a raw SQL insert, another
    process/version writing a since-removed enum member)."""
    decode_plan, is_partial = _get_decode_plan(EnumFields, only_fields=["id", "service", "currency"])
    row = _synthetic_row(decode_plan, {"id": 1, "service": 9999, "currency": "EUR"})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(EnumFields, row, decode_plan)
    with pytest.raises(ValidationError) as model_exc_info:
        _hydrate_models(EnumFields, decode_plan, is_partial, [row])

    assert str(model_exc_info.value) == str(python_exc_info.value)


@pytest.mark.asyncio
async def test_json_already_object_passthrough_branch(db):
    """A raw value that's already a dict/list (not str/bytes) exercises JSONField's passthrough
    branch - the decoder is never called at all, on either side."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": {"already": "decoded", "n": [1, 2, 3]}})
    python_instances, model_instances = _hydrate_both(JSONFields, decode_plan, is_partial, [row])
    assert model_instances[0].data == python_instances[0].data == {"already": "decoded", "n": [1, 2, 3]}


@pytest.mark.parametrize(
    "data",
    [
        pytest.param("{not valid json", id="json_invalid_value_raises_same_field_error"),
        # Bytes that are both invalid JSON AND invalid utf-8 used to mask the intended ValidationError
        # with a bare UnicodeDecodeError from the error message's own .decode() call, on both sides.
        pytest.param(b"\xff\xfe not valid utf-8 or json", id="json_invalid_utf8_bytes_raises_same_field_error"),
    ],
)
@pytest.mark.asyncio
async def test_json_invalid_data_raises_the_same_field_error(db, data):
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": data})

    with pytest.raises(ValidationError) as python_exc_info:
        RowHydration.hydrate_in_python(JSONFields, row, decode_plan)
    with pytest.raises(ValidationError) as model_exc_info:
        _hydrate_models(JSONFields, decode_plan, is_partial, [row])

    assert str(model_exc_info.value) == str(python_exc_info.value)


@pytest.mark.asyncio
async def test_json_invalid_value_preserves_context_chain(db):
    """Malformed JSON text must also chain the original decode error as __context__ - Python's
    own `raise ValidationError(...) from exc` inside `except Exception as exc:` does this, so a
    traceback shows "The above exception was the direct cause of the following exception"
    pointing at the real decode error, not just the wrapping ValidationError alone."""
    decode_plan, is_partial = _get_decode_plan(JSONFields, only_fields=["id", "data"])
    row = _synthetic_row(decode_plan, {"id": 1, "data": "{not valid json"})

    with pytest.raises(ValidationError) as model_exc_info:
        _hydrate_models(JSONFields, decode_plan, is_partial, [row])

    assert model_exc_info.value.__cause__ is not None


@pytest.mark.asyncio
async def test_uuid_string_fallback_branch(db):
    """A raw string value (sqlite's actual on-disk shape for a UUID column - there's no native
    UUID type, it's stored and read back as TEXT) exercises UUIDField.from_db_value's
    UUID(value) fallback, not the isinstance-already-UUID no-op asyncpg's native uuid columns
    hit instead."""
    decode_plan, is_partial = _get_decode_plan(UUIDFields, only_fields=["id", "data"])
    value = uuid.uuid4()
    row = _synthetic_row(decode_plan, {"id": uuid.uuid4(), "data": str(value)})
    python_instances, model_instances = _hydrate_both(UUIDFields, decode_plan, is_partial, [row])
    assert model_instances[0].data == python_instances[0].data == value
    assert type(model_instances[0].data) is uuid.UUID


@pytest.mark.asyncio
async def test_datetime_naive_raw_value_with_use_tz(db):
    decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
    naive = datetime.datetime(2024, 6, 15, 9, 30, 0)
    row = _synthetic_row(decode_plan, {"id": 1, "datetime": naive})
    python_instances, model_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, [row])
    assert model_instances[0].datetime == python_instances[0].datetime
    assert model_instances[0].datetime.tzinfo is not None


@pytest.mark.asyncio
async def test_datetime_aware_raw_value_without_use_tz(db):
    with override_timezone(use_timezone=False):
        decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
        aware = datetime.datetime(2024, 6, 15, 9, 30, 0, tzinfo=datetime.timezone.utc)
        row = _synthetic_row(decode_plan, {"id": 1, "datetime": aware})
        python_instances, model_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, [row])
        assert model_instances[0].datetime == python_instances[0].datetime
        assert model_instances[0].datetime.tzinfo is None


@pytest.mark.asyncio
async def test_datetime_non_utc_target_dst_transition_values(db):
    """Regression guard for the riskiest fast path in this whole module: the zero-Python-call
    datetime shortcut must NEVER fire for a non-UTC target, since a DST-observing zone's offset
    genuinely varies by date. Picks two real UTC instants either side of an actual US DST
    transition (2024-03-10, America/New_York) - if the shortcut wrongly fired here, both would
    come back with the SAME wall-clock offset instead of the real 1-hour jump, since the
    shortcut only ever copies fields without recomputing anything."""
    with override_timezone(use_timezone=True, timezone="America/New_York"):
        decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
        before_transition = datetime.datetime(2024, 3, 10, 6, 0, 0, tzinfo=datetime.timezone.utc)  # EST, UTC-5
        after_transition = datetime.datetime(2024, 3, 10, 8, 0, 0, tzinfo=datetime.timezone.utc)  # EDT, UTC-4
        rows = [
            _synthetic_row(decode_plan, {"id": 1, "datetime": before_transition}),
            _synthetic_row(decode_plan, {"id": 2, "datetime": after_transition}),
        ]
        python_instances, model_instances = _hydrate_both(DatetimeFields, decode_plan, is_partial, rows)
        for python_instance, model_instance in zip(python_instances, model_instances, strict=True):
            assert model_instance.datetime == python_instance.datetime
            assert model_instance.datetime.utcoffset() == python_instance.datetime.utcoffset()
        assert python_instances[0].datetime.utcoffset() != python_instances[1].datetime.utcoffset()


@test.requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_reused_reader_observes_timezone_config_change(db):
    decode_plan, is_partial = _get_decode_plan(DatetimeFields, only_fields=["id", "datetime"])
    columns = ModelColumns(DatetimeFields, decode_plan, tuple(range(len(decode_plan))), is_partial)
    moment = datetime.datetime(2024, 1, 15, 12, 0)
    row = _synthetic_row(decode_plan, {"id": 1, "datetime": moment})
    with override_timezone(use_timezone=True, timezone="UTC"):
        aware = columns.hydrate(row, None).datetime
        assert aware == moment.replace(tzinfo=datetime.UTC)
    with override_timezone(use_timezone=False, timezone="UTC"):
        naive = columns.hydrate(row, None).datetime
        assert naive == moment
        assert naive.tzinfo is None


@pytest.mark.asyncio
@test.requires_features(dialect="sqlite")
async def test_date_string_parse_and_native_passthrough_branches(db):
    decode_plan, is_partial = _get_decode_plan(DateFields, only_fields=["id", "date"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "date": "2024-03-15"}),
        _synthetic_row(decode_plan, {"id": 2, "date": datetime.date(2000, 1, 1)}),
    ]
    python_instances, model_instances = _hydrate_both(DateFields, decode_plan, is_partial, rows)
    for python_instance, model_instance in zip(python_instances, model_instances, strict=True):
        assert model_instance.date == python_instance.date
        assert type(model_instance.date) is datetime.date


@pytest.mark.asyncio
async def test_time_tzinfo_dispatch_branches(db):
    decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
    naive = datetime.time(9, 30, 0)
    aware = datetime.time(9, 30, 0, tzinfo=datetime.timezone.utc)

    # use_timezone=True (default): naive gets a tzinfo attached, already-aware is left untouched.
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "time": naive}),
        _synthetic_row(decode_plan, {"id": 2, "time": aware}),
    ]
    python_instances, model_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
    assert model_instances[0].time == python_instances[0].time
    assert model_instances[0].time.tzinfo is not None  # naive -> attached
    assert model_instances[1].time == python_instances[1].time
    assert model_instances[1].time.tzinfo is not None  # already aware -> untouched, still aware

    # use_timezone=False: aware gets its tzinfo stripped, already-naive is left untouched.
    with override_timezone(use_timezone=False):
        decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
        rows = [
            _synthetic_row(decode_plan, {"id": 1, "time": naive}),
            _synthetic_row(decode_plan, {"id": 2, "time": aware}),
        ]
        python_instances, model_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
        assert model_instances[0].time == python_instances[0].time
        assert model_instances[0].time.tzinfo is None  # already naive -> untouched, still naive
        assert model_instances[1].time == python_instances[1].time
        assert model_instances[1].time.tzinfo is None  # aware -> stripped


@pytest.mark.asyncio
async def test_time_timedelta_and_string_fallback_branches(db):
    decode_plan, is_partial = _get_decode_plan(TimeFields, only_fields=["id", "time"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "time": datetime.timedelta(hours=25)}),
        _synthetic_row(decode_plan, {"id": 2, "time": "14:30:00"}),
    ]
    python_instances, model_instances = _hydrate_both(TimeFields, decode_plan, is_partial, rows)
    assert model_instances[0].time == python_instances[0].time == datetime.timedelta(hours=25)
    assert model_instances[1].time == python_instances[1].time


@pytest.mark.asyncio
async def test_timedelta_zero_negative_and_multiday_branches(db):
    decode_plan, is_partial = _get_decode_plan(TimeDeltaFields, only_fields=["id", "timedelta"])
    rows = [
        _synthetic_row(decode_plan, {"id": 1, "timedelta": 0}),
        _synthetic_row(decode_plan, {"id": 2, "timedelta": -1_500_000}),
        _synthetic_row(decode_plan, {"id": 3, "timedelta": 3 * 24 * 60 * 60 * 1_000_000 + 123_456}),
        _synthetic_row(decode_plan, {"id": 4, "timedelta": datetime.timedelta(minutes=5)}),
    ]
    python_instances, model_instances = _hydrate_both(TimeDeltaFields, decode_plan, is_partial, rows)
    for python_instance, model_instance in zip(python_instances, model_instances, strict=True):
        assert model_instance.timedelta == python_instance.timedelta
        assert type(model_instance.timedelta) is datetime.timedelta
