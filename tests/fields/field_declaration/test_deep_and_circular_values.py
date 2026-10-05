"""A value nesting without end (holding itself) or very deeply, and a very long one: refused with an
error of hare's own whose message stays short - never a hang, a RecursionError, a crashed process or
an error message the size of the value."""

from typing import Any

import pytest

from hare.exceptions import QueryError, ValidationError
from hare.fields.data.json.json_codec import JsonCodec
from hare.query.expressions import RawSQL, Value
from tests.fields.models_write_paths import WritePathRecord

SHORT_MESSAGE_LENGTH = 300


def make_circular_list() -> list[Any]:
    circular: list[Any] = [1]
    circular.append(circular)
    return circular


def make_deep_list(depth: int) -> list[Any]:
    deep: list[Any] = []
    for _ in range(depth):
        deep = [deep]
    return deep


@pytest.mark.database_independent
@pytest.mark.parametrize("make_value", [make_circular_list, lambda: make_deep_list(100_000)])
def test_a_value_or_raw_sql_parameter_nesting_without_end_is_refused(make_value):
    """Bound as a parameter, such a list crashed the process on rust_pg (stack overflow) and raised a
    RecursionError on asyncpg and ClickHouse."""
    with pytest.raises(QueryError, match="nesting deeper than 255 levels, or holding itself"):
        Value(make_value())
    with pytest.raises(QueryError, match="nesting deeper than 255 levels, or holding itself"):
        RawSQL("%s", [make_value()])


@pytest.mark.database_independent
def test_a_value_nesting_as_deep_as_a_json_text_is_taken():
    assert Value(make_deep_list(254)).value == make_deep_list(254)


@pytest.mark.asyncio
@pytest.mark.parametrize("make_value", [make_circular_list, lambda: make_deep_list(100_000)])
async def test_a_filter_value_nesting_without_end_is_refused(db_write_paths, make_value):
    """Its plan shape was read by a recursion without end - a RecursionError on ClickHouse. The value
    is refused by the field's own JSON check where it is encoded first."""
    with pytest.raises((QueryError, ValidationError)) as exc_info:
        await WritePathRecord.objects.filter(data__contains=make_value()).count()
    assert len(str(exc_info.value)) < SHORT_MESSAGE_LENGTH


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_json_value_nesting_very_deeply_is_refused_with_a_short_message(db_write_paths):
    """The message wrote the whole value by repr(), which raised a RecursionError of its own."""
    with pytest.raises(ValidationError) as exc_info:
        await WritePathRecord.objects.create(data=make_deep_list(100_000))
    assert len(str(exc_info.value)) < SHORT_MESSAGE_LENGTH


def make_deep_tuple(depth: int) -> tuple[Any, ...]:
    deep: tuple[Any, ...] = ()
    for _ in range(depth):
        deep = (deep,)
    return deep


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False], ids=["native_check", "python_check"])
@pytest.mark.parametrize(
    "make_value",
    [lambda: make_deep_tuple(5_000), lambda: make_deep_tuple(254), lambda: make_deep_list(254)],
    ids=["tuple_5000", "tuple_255_containers", "list_255_containers"],
)
async def test_a_json_value_nesting_past_254_containers_is_refused_alike(
    db_write_paths, monkeypatch, native, make_value
):
    """A tuple nested 5000 deep ended the process in orjson - a stack overflow, no exception; a list
    nested 255 deep was refused by a message naming no reason."""
    if not native:
        monkeypatch.setattr(JsonCodec, "storable_value_checker", None)
        monkeypatch.setattr(JsonCodec, "native_encoder", None)
    with pytest.raises(ValidationError, match="nests more than 254 lists, tuples and dicts"):
        await WritePathRecord.objects.create(data=make_value())
    # 254 containers - the innermost empty list is one.
    record = await WritePathRecord.objects.create(data=make_deep_list(253))
    assert (await WritePathRecord.objects.get(id=record.id)).data == make_deep_list(253)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("name", "x" * 1_000_000),
        ("amount", "x" * 1_000_000),
        ("data", set(range(200_000))),
        ("data", b"x" * 1_000_000),
        ("data", {"a": 10**5000}),
        ("identifier", "x" * 1_000_000),
    ],
    ids=["long_text", "long_decimal_text", "big_set", "long_bytes", "int_of_5000_digits", "long_uuid_text"],
)
async def test_a_refused_value_is_shown_cut_short(db_write_paths, field_name, value):
    """A refused value was written whole into the message - a megabyte for a megabyte of text - and
    an int of over 4300 digits raised Python's own ValueError while being written."""
    with pytest.raises(ValidationError) as exc_info:
        await WritePathRecord.objects.create(**{field_name: value})
    assert len(str(exc_info.value)) < SHORT_MESSAGE_LENGTH
