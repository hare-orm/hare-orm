"""Every write path (create/save/update/bulk_create/bulk_update/update_or_create) must accept,
reject and normalize a value the same way, and every read path must return the same type."""

import datetime
import uuid
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import pytest

from hare.exceptions import ValidationError
from hare.fields.data.json import JsonCodec
from hare.query.expressions import F
from hare.utils import Timezone
from tests.fields.models_write_paths import (
    PaintColor,
    Priority,
    Rank,
    WritePathChild,
    WritePathDefaults,
    WritePathParent,
    WritePathRecord,
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


def test_json_codec_decodes_long_integer_exactly():
    assert JsonCodec.loads('{"a":18446744073709551616}') == {"a": 2**64}
    assert JsonCodec.loads(b"[-9223372036854775809]") == [-(2**63) - 1]
    assert JsonCodec.loads('{"a":"12345678901234567890","b":1.5}') == {"a": "12345678901234567890", "b": 1.5}
    assert JsonCodec.dumps_exact({"a": 2**70}) == '{"a":1180591620717411303424}'


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{"a": "x" + NULL_BYTE}, ["x" + NULL_BYTE], {"k" + NULL_BYTE: 1}], ids=repr)
async def test_json_null_byte_rejected_on_every_write_path(db_write_paths, value):
    messages = await get_write_path_errors("data", value)
    assert all("null byte" in message for message in messages.values()), messages


@pytest.mark.asyncio
async def test_json_null_byte_rejected_by_bulk_create_use_copy(db_write_paths):
    if not DatabaseUnderTest.get_dialect().supports_copy:
        pytest.skip("The database has no COPY bulk-load protocol")
    with pytest.raises(ValidationError, match="null byte"):
        await WritePathRecord.objects.bulk_create([WritePathRecord(data={"a": "x" + NULL_BYTE})], use_copy=True)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("field_name", ["number", "big_number", "small_number"])
@pytest.mark.parametrize("value", [5.7, Decimal("-2.9"), float("nan"), Decimal("Infinity")], ids=repr)
async def test_int_non_whole_number_rejected_on_every_write_path(db_write_paths, field_name, value):
    messages = await get_write_path_errors(field_name, value)
    assert all("is not a whole number" in message for message in messages.values()), messages
    assert await WritePathRecord.objects.filter(**{f"{field_name}__isnull": False}).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [5.0, Decimal("5"), Decimal("5.000"), True], ids=repr)
async def test_int_whole_number_accepted_as_int(db_write_paths, value):
    expected = int(value)
    created = await WritePathRecord.objects.create(number=value)
    assert type(created.number) is int
    assert created.number == expected
    await WritePathRecord.objects.filter(id=created.id).update(number=value)
    await WritePathRecord.objects.bulk_create([WritePathRecord(number=value)])
    assert await WritePathRecord.objects.all().values_list("number", flat=True) == [expected, expected]
    assert await WritePathRecord.objects.filter(number=value).count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("filler_count", [0, 1500])
async def test_int_in_lookup_with_non_whole_number(db_write_paths, filler_count):
    """``__in``/``__not_in`` compare a non-integer number exactly, the same way ``number=1.5`` does,
    for both a short list and one long enough for the backends' own large-list forms."""
    for number in (1, 2, 3, None):
        await WritePathRecord.objects.create(number=number)
    filler = list(range(1000, 1000 + filler_count))

    async def get_numbers(**filters: Any) -> list[int | None]:
        numbers = await WritePathRecord.objects.filter(**filters).values_list("number", flat=True)
        return sorted(numbers, key=lambda number: -1 if number is None else number)

    assert await get_numbers(number=1.5) == []
    assert await get_numbers(number__in=[1.5, 3, *filler]) == [3]
    assert await get_numbers(number__in=[3.0, Decimal("2.0"), *filler]) == [2, 3]
    assert await get_numbers(number__in=[Decimal("1.5"), None, *filler]) == [None]
    assert await get_numbers(number__not_in=[1.5, 3, *filler]) == [None, 1, 2]
    assert await get_numbers(number__not_in=[1.5, 3, None, *filler]) == [1, 2]


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
    assert created.price == Decimal("1.50")
    assert created.naive_moment.tzinfo is not None
    assert created.current_moment.tzinfo is not None
    assert created.text_moment == datetime.datetime(2020, 1, 1, 9, 0, tzinfo=datetime.UTC)
    assert created.identifier == uuid.UUID(int=5)
    assert created.day == datetime.date(2020, 2, 2)
    assert created.duration == datetime.timedelta(seconds=5)
    assert type(created.ratio) is float
    assert created.count == 7


@pytest.mark.asyncio
async def test_defaults_are_normalized_by_construct(db_write_paths):
    constructed = WritePathDefaults.construct()
    assert constructed.color is PaintColor.RED
    assert constructed.price == Decimal("1.50")
    assert constructed.identifier == uuid.UUID(int=5)
    assert constructed.duration == datetime.timedelta(seconds=5)


@pytest.mark.asyncio
async def test_datetime_default_follows_current_timezone(db_write_paths, monkeypatch):
    first = WritePathDefaults()
    monkeypatch.setattr(Timezone, "name", staticmethod(lambda: "Asia/Tokyo"))
    second = WritePathDefaults()
    assert first.naive_moment.utcoffset() == datetime.timedelta(0)
    assert second.naive_moment.utcoffset() == datetime.timedelta(hours=9)


@pytest.mark.asyncio
async def test_timedelta_int_microseconds_accepted_on_every_write_path(db_write_paths):
    for write_path in WRITE_PATHS:
        await write_path("duration", 5_000_000)
    durations = await WritePathRecord.objects.filter(duration__isnull=False).values_list("duration", flat=True)
    assert durations == [datetime.timedelta(seconds=5)] * len(WRITE_PATHS)
    assert await WritePathRecord.objects.filter(duration=5_000_000).count() == len(WRITE_PATHS)


@pytest.mark.asyncio
async def test_timedelta_int_out_of_range_rejected(db_write_paths):
    messages = await get_write_path_errors("duration", 2**70)
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
