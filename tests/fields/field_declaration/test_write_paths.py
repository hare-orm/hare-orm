"""Every write path (create/save/update/bulk_create/bulk_update/update_or_create) must accept,
reject and normalize a value the same way, and every read path must return the same type."""

import datetime
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import DatabaseError, HareError, ValidationError
from hare.fields.data.json.json_codec import JsonCodec
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.enums import RowOperation
from hare.instrumentation.observers.observers import Observers
from hare.query.expressions import Case, F, RawSQL, Value, When
from hare.query.functions import Coalesce, NullIf
from hare.time import Timezone
from hare.transactions.transactions import Transactions
from tests.fields.models_write_paths import (
    PaintColor,
    Priority,
    Rank,
    WritePathChild,
    WritePathDefaults,
    WritePathParent,
    WritePathRecord,
    WritePathSecretKey,
    WritePathSecretTag,
    WritePathTaggedNote,
    WritePathTimes,
)
from tests.utils.database_under_test import DatabaseUnderTest

NULL_BYTE = chr(0)


async def create_with(field_name: str, value: Any) -> None:
    await WritePathRecord.objects.create(**{field_name: value})


async def save_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    setattr(record, field_name, value)
    await record.save()


async def update_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    await WritePathRecord.objects.filter(id=record.id).update(**{field_name: value})


async def bulk_create_with(field_name: str, value: Any) -> None:
    await WritePathRecord.objects.bulk_create([WritePathRecord(**{field_name: value})])


async def bulk_update_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    setattr(record, field_name, value)
    await WritePathRecord.objects.bulk_update([record], fields=[field_name])


async def update_or_create_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    await WritePathRecord.objects.update_or_create(id=record.id, defaults={field_name: value})


WRITE_PATHS: list[Callable[[str, Any], Awaitable[None]]] = [
    create_with,
    save_with,
    update_with,
    bulk_create_with,
    bulk_update_with,
    update_or_create_with,
]


async def get_write_path_errors(field_name: str, value: Any) -> dict[str, str]:
    """Runs every write path with ``value`` and returns each path's ValidationError message."""
    messages = {}
    for write_path in WRITE_PATHS:
        with pytest.raises(ValidationError) as exc_info:
            await write_path(field_name, value)
        messages[write_path.__name__] = str(exc_info.value)
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", [{"ratio": float("nan")}, {"limit": float("inf")}, [float("-inf")], float("nan")], ids=repr
)
async def test_json_non_finite_float_rejected_on_every_write_path(db_write_paths, value):
    messages = await get_write_path_errors("data", value)
    assert all("is not a finite number" in message for message in messages.values()), messages
    assert await WritePathRecord.objects.filter(data__isnull=False).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        {"big": 2**64},
        [-(2**63) - 1, 2**70],
        12345678901234567890123,
        {"max": 2**64 - 1, "text": "1234567890123456789012"},
    ],
    ids=repr,
)
async def test_json_integer_beyond_64_bits_round_trips_exactly(db_write_paths, value):
    created = await WritePathRecord.objects.create(data=value)
    await WritePathRecord.objects.bulk_create([WritePathRecord(data=value)])
    assert (await WritePathRecord.objects.get(id=created.id)).data == value
    assert await WritePathRecord.objects.all().values_list("data", flat=True) == [value, value]


@pytest.mark.database_independent
def test_json_codec_decodes_long_integer_exactly():
    assert JsonCodec.loads('{"a":18446744073709551616}') == {"a": 2**64}
    assert JsonCodec.loads(b"[-9223372036854775809]") == [-(2**63) - 1]
    assert JsonCodec.loads('{"a":"12345678901234567890","b":1.5}') == {"a": "12345678901234567890", "b": 1.5}
    assert JsonCodec.dumps_exact({"a": 2**70}) == '{"a":1180591620717411303424}'


def make_circular_list() -> list[Any]:
    circular: list[Any] = ["start"]
    circular.append({"back": circular})
    return circular


def make_circular_dict() -> dict[str, Any]:
    circular: dict[str, Any] = {"name": "start"}
    circular["children"] = [circular]
    return circular


@pytest.mark.asyncio
@pytest.mark.parametrize("make_value", [make_circular_list, make_circular_dict])
async def test_json_circular_reference_rejected_on_every_write_path(db_write_paths, make_value):
    """A list or dict holding itself hung the process: the storable-value walk never ended."""
    messages = await get_write_path_errors("data", make_value())
    assert all("circular reference" in message for message in messages.values()), messages


@pytest.mark.asyncio
async def test_json_value_holding_one_list_twice_is_stored(db_write_paths):
    shared = [1, 2]
    record = await WritePathRecord.objects.create(data={"first": shared, "second": [shared]})
    assert (await WritePathRecord.objects.get(id=record.id)).data == {"first": [1, 2], "second": [[1, 2]]}


@pytest.mark.asyncio
async def test_json_nan_rejected_by_bulk_create_use_copy(db_write_paths):
    if not DatabaseUnderTest.get_dialect().features.supports_copy:
        pytest.skip("The database has no COPY bulk-load protocol")
    with pytest.raises(ValidationError, match="is not a finite number"):
        await WritePathRecord.objects.bulk_create([WritePathRecord(data={"a": float("nan")})], use_copy=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "secret"),
    [
        ("secret_data", {"card": "4111-1111-1111-1111", "amount": Decimal("1.5")}, "4111"),
        ("secret_data", {"card": "4111-1111-1111-1111", "ratio": float("nan")}, "nan"),
        ("secret_code", "SECRET-PIN-9876", "SECRET"),
        ("secret_name", "SECRET-PIN-9876", "SECRET"),
    ],
)
async def test_sensitive_value_hidden_on_every_write_path(db_write_paths, field_name, value, secret):
    messages = await get_write_path_errors(field_name, value)
    assert len(set(messages.values())) == 1, messages
    assert secret not in messages["bulk_create_with"]
    assert "<hidden>" in messages["bulk_create_with"]


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        {"secret_name__range": ["SECRET-9917"]},
        {"secret_number__range": [918273645, 1, 2]},
        {"secret_name__isnull": "SECRET-9917"},
        {"secret_number__in": 918273645},
        {"secret_name__not_in": "SECRET-9917"},
        {"secret_data__has_keys": "SECRET-9917"},
    ],
)
async def test_sensitive_value_hidden_in_filter_errors(db_write_paths, filters):
    with pytest.raises(HareError) as error_info:
        await WritePathRecord.objects.filter(**filters)
    assert "SECRET-9917" not in str(error_info.value)
    assert "918273645" not in str(error_info.value)


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_sensitive_json_value_hidden_in_path_filter_errors(db_write_paths):
    for filters in ({"secret_data__gt": b"SECRET-9917"}, {"secret_data__exact": ["SECRET-9917", object()]}):
        with pytest.raises(ValidationError) as error_info:
            await WritePathRecord.objects.filter(**filters)
        assert "SECRET-9917" not in str(error_info.value)


@pytest.mark.database_independent
def test_sensitive_part_of_a_primary_key_hidden_in_repr_and_str():
    obj = WritePathSecretKey(region="SECRET-9917", number=918273645)
    for shown in (repr(obj), str(obj)):
        assert "SECRET-9917" not in shown
        assert "<hidden>" in shown
        assert "918273645" in shown
    assert repr(WritePathRecord(id=7)) == "<WritePathRecord: 7>"


@pytest.mark.database_independent
def test_a_hidden_value_hides_only_whole_words():
    message = 'duplicate key value violates unique constraint "uq_name_code" Key (name, code)=(a, m) already exists'
    shown = DatabaseError.hide_values(message, ["m"])
    assert shown == (
        'duplicate key value violates unique constraint "uq_name_code" Key (name, code)=(a, <hidden>) already exists'
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_sensitive_value_hidden_in_errors_of_insert_from_and_of_a_failing_row(db_write_paths):
    await WritePathRecord.objects.create(id=1, secret_name="ab")
    with pytest.raises(HareError) as error_info:
        async with Transactions.atomic():
            await WritePathRecord.objects.insert_from(
                WritePathRecord.objects.annotate(new_id=Value(2), secret=Value("SECRET-9917")).values_list(
                    "new_id", "secret"
                ),
                fields=["id", "secret_number"],
            )
    assert "SECRET-9917" not in str(error_info.value)
    assert "SECRET-9917" not in repr(getattr(error_info.value, "parameters", None))
    await WritePathSecretKey.objects.create(region="SECRET-9917", number=1)
    with pytest.raises(HareError) as error_info:
        async with Transactions.atomic():
            await WritePathSecretKey.objects.all().update(number=RawSQL("NULL"))
    assert "Failing row contains (<hidden>)" in str(error_info.value)
    assert "SECRET-9917" not in str(error_info.value)
    assert error_info.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        # The int fields share one check: every value on one field, one value on each other field.
        *[("number", value) for value in (5.7, Decimal("-2.9"), float("nan"), Decimal("Infinity"))],
        *[(field_name, 5.7) for field_name in ("big_number", "small_number")],
    ],
    ids=repr,
)
async def test_int_non_whole_number_rejected_on_every_write_path(db_write_paths, field_name, value):
    messages = await get_write_path_errors(field_name, value)
    assert all("expected an int" in message for message in messages.values()), messages
    assert await WritePathRecord.objects.filter(**{f"{field_name}__isnull": False}).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5.0, Decimal("5"), True, "5"], ids=repr)
async def test_an_int_field_takes_an_int_alone_on_every_write_path(db_write_paths, value):
    """A whole float or Decimal, a bool and a numeric text were taken as the int they read as."""
    messages = await get_write_path_errors("number", value)
    assert all("expected an int" in message for message in messages.values()), messages


@pytest.mark.asyncio
@pytest.mark.parametrize("filler_count", [0, 1500])
async def test_int_in_lookup_with_non_whole_number(db_write_paths, filler_count):
    """``__in``/``__not_in`` refuse a float or a Decimal, the same way ``number=1.5`` does, for both a
    short list and one long enough for the backends' own large-list forms."""
    for number in (1, 2, 3, None):
        await WritePathRecord.objects.create(number=number)
    filler = list(range(1000, 1000 + filler_count))

    async def get_numbers(**filters: Any) -> list[int | None]:
        numbers = await WritePathRecord.objects.filter(**filters).values_list("number", flat=True)
        return sorted(numbers, key=lambda number: -1 if number is None else number)

    for filters in (
        {"number": 1.5},
        {"number__in": [1.5, 3, *filler]},
        {"number__in": [3.0, Decimal("2.0"), *filler]},
        {"number__in": [Decimal("1.5"), None, *filler]},
        {"number__not_in": [1.5, 3, *filler]},
        {"number__not_in": [1.5, 3, None, *filler]},
    ):
        with pytest.raises(ValidationError, match="number: expected an int"):
            await get_numbers(**filters)
    assert await get_numbers(number__in=[3, None, *filler]) == [None, 3]
    assert await get_numbers(number__not_in=[3, *filler]) == [None, 1, 2]


@pytest.mark.asyncio
async def test_defaults_are_normalized_like_assigned_values(db_write_paths):
    created = await WritePathDefaults.objects.create()
    fetched = await WritePathDefaults.objects.get(id=created.id)
    for field_name in WritePathDefaults._meta.db_fields:
        in_memory_value, read_value = getattr(created, field_name), getattr(fetched, field_name)
        assert type(in_memory_value) is type(read_value), field_name
        assert in_memory_value == read_value, field_name
        assert repr(in_memory_value) == repr(read_value), field_name
    assert created.color is PaintColor.RED
    assert created.priority is Priority.HIGH
    assert created.rank is Rank.SECOND
    if DatabaseUnderTest.holds_decimal_fields():
        assert created.price == Decimal("1.50")
    assert created.naive_moment.tzinfo is not None
    assert created.current_moment.tzinfo is not None
    assert created.text_moment == datetime.datetime(2020, 1, 1, 9, 0, tzinfo=datetime.UTC)
    assert created.identifier == uuid.UUID(int=5)
    assert created.day == datetime.date(2020, 2, 2)
    assert created.duration == datetime.timedelta(seconds=5)
    assert type(created.ratio) is float
    assert created.count == 7


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_defaults_are_normalized_by_construct(db_write_paths):
    constructed = WritePathDefaults.construct()
    assert constructed.color is PaintColor.RED
    if DatabaseUnderTest.holds_decimal_fields():
        assert constructed.price == Decimal("1.50")
    assert constructed.identifier == uuid.UUID(int=5)
    assert constructed.duration == datetime.timedelta(seconds=5)


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_datetime_default_follows_current_timezone(db_write_paths):
    first = WritePathDefaults()
    with Timezone.override("Asia/Tokyo"):
        second = WritePathDefaults()
    assert first.naive_moment.utcoffset() == datetime.timedelta(0)
    assert second.naive_moment.utcoffset() == datetime.timedelta(hours=9)


@pytest.mark.asyncio
async def test_a_number_is_no_duration_on_any_write_path(db_write_paths):
    messages = await get_write_path_errors("duration", 5_000_000)
    assert all("expected a datetime.timedelta" in message for message in messages.values()), messages


@pytest.mark.asyncio
async def test_timedelta_out_of_range_rejected(db_write_paths):
    messages = await get_write_path_errors("duration", datetime.timedelta(days=999_999_999))
    assert all("out of range" in message for message in messages.values()), messages


@pytest.mark.asyncio
async def test_char_enum_with_non_str_values_update_from_expression(db_write_paths):
    record = await WritePathRecord.objects.create(rank=Rank.SECOND)
    assert await WritePathRecord.objects.filter(id=record.id).update(rank=F("rank")) == 1
    assert (await WritePathRecord.objects.get(id=record.id)).rank is Rank.SECOND


@pytest.mark.asyncio
async def test_uuid_read_back_as_plain_uuid_on_every_read_path(db_write_paths):
    parent = await WritePathParent.objects.create(id=uuid.UUID(int=9), name="p")
    await WritePathChild.objects.create(parent=parent)
    record = await WritePathRecord.objects.create(identifier=uuid.UUID(int=7))

    values = [
        (await WritePathRecord.objects.get(id=record.id)).identifier,
        (await WritePathRecord.objects.filter(id=record.id))[0].identifier,
        (await WritePathRecord.objects.filter(id=record.id).values("identifier"))[0]["identifier"],
        (await WritePathRecord.objects.filter(id=record.id).values_list("identifier", flat=True))[0],
        (await WritePathRecord.objects.filter(id=record.id).annotate(copy=F("identifier")))[0].copy,
        (await WritePathParent.objects.get(id=parent.id)).id,
        (await WritePathChild.objects.all().select_related("parent"))[0].parent.id,
        (await WritePathChild.objects.all().prefetch_related("parent"))[0].parent.id,
        (await WritePathChild.objects.all())[0].parent_id,
        (await WritePathChild.objects.all().values_list("parent__id", flat=True))[0],
        (await WritePathParent.objects.all().prefetch_related("children"))[0].id,
    ]
    assert all(type(value) is uuid.UUID for value in values), [type(value) for value in values]


@pytest.mark.asyncio
async def test_snapshot_repr_hides_sensitive_values(db_write_paths):
    record = await WritePathRecord.objects.create(number=4, secret_name="TKN1", secret_number=7777)
    snapshot = record.snapshot()
    shown = repr(snapshot)
    assert "TKN1" not in shown
    assert "7777" not in shown
    assert "'secret_name': <hidden>" in shown
    assert "'number': 4" in shown
    record.secret_name = "TKN2"
    assert record.diff_against(snapshot) == {"secret_name": ("TKN1", "TKN2")}


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_rows_changed_event_hides_the_sensitive_part_of_a_key(db_write_paths):
    """The event's primary keys held a sensitive key part as it was - every observer, log and outbox
    reading the event got the secret, which repr(obj) and the errors hide."""
    client = WritePathSecretKey.get_connection()
    event = ChangeEvents.build_event(client, WritePathSecretKey, RowOperation.INSERT, [("SECRET-4411", 7)], None)
    assert event.pks == (("<hidden>", 7),)
    assert "SECRET-4411" not in repr(event)
    assert ChangeEvents.build_event(client, WritePathRecord, RowOperation.INSERT, [5], None).pks == (5,)


@pytest.mark.asyncio
async def test_an_update_of_a_relation_to_a_sensitive_key_hides_the_key(db_write_paths):
    """update(tag=...) wrote the sensitive key into the relation's column as a shown parameter - the
    event and the log held it, though create() and save() hid it."""
    tag = await WritePathSecretTag.objects.create(code="SECRET-7731")
    note = await WritePathTaggedNote.objects.create(id=1)
    shown_parameters: list[Any] = []

    def hook(event: QueryExecuted) -> None:
        shown_parameters.append(event.parameters)

    Observers.observe(QueryExecuted, hook)
    try:
        await WritePathTaggedNote.objects.filter(id=note.id).update(tag=tag)
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)
    assert shown_parameters
    assert "SECRET-7731" not in repr(shown_parameters)


@pytest.mark.asyncio
async def test_a_value_no_driver_encodes_leaves_no_trace_of_itself_in_the_error(db_write_paths):
    """The ValidationError raised for a lone surrogate was raised while the driver's error was handled
    - its __context__ kept the driver's error and with it the sensitive value."""
    with pytest.raises(ValidationError) as error_info:
        await WritePathRecord.objects.create(secret_name="S\ud800")
    assert error_info.value.__context__ is None
    assert error_info.value.__cause__ is None


@pytest.mark.decimal_fields
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
async def test_a_bool_is_no_decimal(db_write_paths, value):
    """Decimal(True) is 1 - a flag was stored as 1.00."""
    messages = await get_write_path_errors("amount", value)
    assert all("is not a valid decimal number" in message for message in messages.values()), messages


@pytest.mark.database_independent
@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{}, [1, 2], "ab", 5], ids=["dict", "list_of_ints", "text", "int"])
async def test_binary_data_takes_bytes_like_values_only(db_write_paths, value):
    """bytes() turned {} into b"" and [1, 2] into b"\x01\x02" - stored silently."""
    with pytest.raises(ValidationError, match="expected bytes-like data"):
        await WritePathTimes.objects.create(data=value)


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_timedelta_is_no_time_of_day(db_write_paths):
    """A timedelta reached the driver as it was: a raw error on SQLite and PostgreSQL, a time on
    ClickHouse. The text 24:00 is the end of a day - Python 3.14 read it as midnight at its start."""
    for value in (datetime.timedelta(hours=1), datetime.timedelta(hours=-1)):
        # Refused as it is assigned - a model held it until a write refused it.
        with pytest.raises(ValidationError, match="a timedelta is no time of day"):
            WritePathTimes(clock=value)
        with pytest.raises(ValidationError, match="a timedelta is no time of day"):
            await WritePathTimes.objects.create(clock=value)
        with pytest.raises(ValidationError, match="a timedelta is no time of day"):
            await WritePathTimes.objects.filter(clock=value).count()
    with pytest.raises(ValidationError, match="the end of a day"):
        await WritePathTimes.objects.create(clock="24:00")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filters",
    [
        {"number__lt": float("nan")},
        {"id__gt": float("nan")},
        pytest.param({"amount__lt": float("nan")}, marks=pytest.mark.decimal_fields),
        pytest.param({"amount__lt": Decimal("NaN")}, marks=pytest.mark.decimal_fields),
        pytest.param({"amount__in": [float("inf")]}, marks=pytest.mark.decimal_fields),
        {"data__a": float("nan")},
        {"data__a__gt": float("inf")},
    ],
    ids=["int_nan", "pk_nan", "decimal_float_nan", "decimal_nan", "decimal_inf_list", "json_nan", "json_inf"],
)
async def test_a_number_no_write_stores_is_refused_in_a_filter(db_write_paths, filters):
    """NaN and the infinities - refused by every write - were compared each database its own way:
    SQLite matched nothing or everything, PostgreSQL raised for some, ClickHouse failed (Code 69)."""
    with pytest.raises(ValidationError, match="NaN|not a finite"):
        await WritePathRecord.objects.filter(**filters).count()


EXPRESSION_LITERAL_SHAPES: dict[str, Callable[[str, Any], Any]] = {
    "case": lambda field_name, value: Case(When(id__gte=0, then=Value(value)), default=F(field_name)),
    "coalesce": lambda field_name, value: Coalesce(Value(None), Value(value)),
    "null_if": lambda field_name, value: NullIf(Value(value), Value(None)),
}


async def expression_update_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    await WritePathRecord.objects.filter(id=record.id).update(**{field_name: value})


async def expression_save_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    setattr(record, field_name, value)
    await record.save()


async def expression_update_or_create_with(field_name: str, value: Any) -> None:
    record = await WritePathRecord.objects.create()
    await WritePathRecord.objects.update_or_create(id=record.id, defaults={field_name: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", list(EXPRESSION_LITERAL_SHAPES))
@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [("name", "abcde", "Length of"), ("small_number", 2**15, "small_number"), ("rank", "3", "rank")],
    ids=["max_length", "integer_range", "enum_member"],
)
@pytest.mark.parametrize(
    "write_path",
    [expression_update_with, expression_save_with, expression_update_or_create_with],
    ids=["update", "save", "update_or_create"],
)
async def test_a_literal_an_expression_writes_is_refused_as_the_plain_value_is(
    db_write_paths, shape, field_name, value, message, write_path
):
    """A literal inside Case()/Coalesce()/NullIf() wasn't converted by the field it was written into - a
    text past max_length, a number of no member of an enum were stored, the row unreadable after."""
    expression = EXPRESSION_LITERAL_SHAPES[shape](field_name, value)
    # Twice - the second update runs on its plan, whose literals bind without their field.
    for _ in range(2):
        with pytest.raises(ValidationError, match=message):
            await write_path(field_name, expression)
    assert await WritePathRecord.objects.filter(**{f"{field_name}__isnull": False}).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    [
        ("small_number", 40000, "small_number"),
        ("name", "abcdef", "name"),
        ("identifier", "not-a-uuid", "identifier"),
    ],
    ids=["smallint_out_of_range", "char_too_long", "uuid_garbage"],
)
async def test_insert_from_refuses_a_literal_its_field_refuses(db_write_paths, field_name, value, message):
    """A Value() the source of insert_from() selected reached the table unchecked - SQLite and ClickHouse kept
    it as it was, PostgreSQL raised its own error - where update() refuses it."""
    await WritePathRecord.objects.create(id=1)
    with pytest.raises(ValidationError, match=message):
        await WritePathRecord.objects.insert_from(
            WritePathRecord.objects.annotate(new_id=Value(2), literal=Value(value)).values_list("new_id", "literal"),
            fields=["id", field_name],
        )
    assert await WritePathRecord.objects.count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["text", 5, 1.5, [None], {"a": 1}, datetime.date(2024, 1, 2), uuid.UUID(int=3)], ids=repr
)
async def test_a_literal_beside_a_json_column_is_written_as_its_json(db_write_paths, value):
    """A literal in Coalesce() beside a JSON column was bound as itself - SQLite and ClickHouse stored text
    that is no JSON, the row then unreadable; PostgreSQL found no common type."""
    # A JSON field writes a date as its ISO text and a UUID as its text.
    expected = (
        value.isoformat()
        if isinstance(value, datetime.date)
        else str(value)
        if isinstance(value, uuid.UUID)
        else value
    )
    record = await WritePathRecord.objects.create()
    for expression in (Coalesce(Value(value), F("data")), Coalesce(F("data"), Value(value))):
        await WritePathRecord.objects.filter(id=record.id).update(data=expression)
        assert (await WritePathRecord.objects.get(id=record.id)).data == expected
        annotated = (
            await WritePathRecord.objects.filter(id=record.id)
            .annotate(value=expression)
            .values_list("value", flat=True)
        )
        assert list(annotated) == [expected]
        await WritePathRecord.objects.filter(id=record.id).update(data=None)


@pytest.mark.asyncio
async def test_a_uuid_literal_first_in_coalesce_shares_the_uuid_column_s_type(db_write_paths):
    """A UUID first in Coalesce() was bound as text - PostgreSQL found no common type with a uuid column."""
    record = await WritePathRecord.objects.create()
    await WritePathRecord.objects.filter(id=record.id).update(
        identifier=Coalesce(Value(uuid.UUID(int=7)), F("identifier"))
    )
    assert (await WritePathRecord.objects.get(id=record.id)).identifier == uuid.UUID(int=7)


@pytest.mark.asyncio
async def test_an_int_enum_refusal_names_its_field_once(db_write_paths):
    record = await WritePathDefaults.objects.create()
    with pytest.raises(ValidationError) as error_info:
        await WritePathDefaults.objects.filter(id=record.id).update(priority="2")
    assert "priority: expected an int" in str(error_info.value)
    assert "priority: priority:" not in str(error_info.value)
