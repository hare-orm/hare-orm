"""Filters on a path into a JSON field on ClickHouse - a JSON column, or the document's text: a value at
the path equals the same JSON value however each side writes it, and compares and orders as the
``Dynamic`` value ClickHouse reads of it; a JSON column keeps no null - a write of one is refused, and
a missing path reads as None."""

import pytest
import pytest_asyncio

from hare import fields
from hare.dialects.clickhouse.types.clickhouse_json_values import ClickhouseJsonValues
from hare.exceptions import FieldError, ValidationError
from hare.query.expressions import F
from hare.query.functions import Cast, Max, Min
from tests.dialects.clickhouse.models import Player


@pytest_asyncio.fixture
async def documents(clickhouse_db):
    data = {"url": "http://x/y", "ratio": 1.0, "big": 1e20, "count": 10, "flag": True}
    if not Player._meta.connection.features.supports_json_type:
        # A JSON column keeps no null - the write is refused.
        data["nothing"] = None
    await Player.objects.create(id=1, name="http://x/y", data=data)
    await Player.objects.create(id=2, name="other", data={"url": "https://z", "ratio": 2.5, "count": 9})
    await Player.objects.create(id=3, name="text", data={"count": "a", "ratio": [1, 2]})


async def ids(**filters):
    return await Player.objects.filter(**filters).order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_text_with_a_slash_matches(documents):
    assert await ids(data__url="http://x/y") == [1]
    assert await ids(data__url__startswith="http://") == [1]
    assert await ids(data__url__in=["http://x/y", "https://z"]) == [1, 2]


@pytest.mark.asyncio
async def test_a_number_matches_whatever_its_text(documents):
    assert await ids(data__ratio=1.0) == [1]
    assert await ids(data__ratio=1) == [1]
    assert await ids(data__big=1e20) == [1]
    assert await ids(data__ratio__in=[1, 2.5]) == [1, 2]
    assert await ids(data__ratio__not_in=[1]) == [2, 3]


async def create_counts():
    for row_id, count in ((1, 10), (2, 9), (3, 2.5)):
        await Player.objects.create(id=row_id, name=str(row_id), data={"count": count})
    await Player.objects.create(id=4, name="4", data={})


@pytest.mark.asyncio
async def test_numbers_compare_by_value_not_by_text(clickhouse_db):
    await create_counts()
    assert await ids(data__count__gt=9) == [1]
    assert await ids(data__count__gte=9) == [1, 2]
    assert await ids(data__count__lt=10) == [2, 3]
    assert await ids(data__count__range=(9, 10)) == [1, 2]
    # A None bound is NULL - no value is above or below it.
    assert await ids(data__count__range=(9, None)) == []


@pytest.mark.asyncio
async def test_values_order_as_clickhouse_orders_a_dynamic(clickhouse_db):
    await create_counts()
    # By the name of the value's type first - a Float64 before an Int64 - then by the value.
    assert await Player.objects.order_by("data__count").values_list("id", flat=True) == [3, 2, 1, 4]
    assert await Player.objects.order_by("-data__count").values_list("id", flat=True) == [4, 1, 2, 3]


@pytest.mark.asyncio
async def test_a_json_null_is_a_value_and_a_missing_key_none(documents):
    if Player._meta.connection.features.supports_json_type:
        # A JSON column keeps no null - the path is a missing one.
        assert await ids(data__nothing=None) == [1, 2, 3]
        assert await ids(data__nothing__isnull=True) == [1, 2, 3]
    else:
        assert await ids(data__nothing=None) == [1]
        assert await ids(data__nothing__isnull=True) == [2, 3]
    assert await ids(data__count__isnull=False) == [1, 2, 3]
    assert await ids(data__flag=True) == [1]


@pytest.mark.asyncio
async def test_a_none_in_a_list_or_negated_follows_the_equality_with_none(documents):
    if Player._meta.connection.features.supports_json_type:
        # A JSON column keeps no null - None matches a missing path, as equality does.
        assert await ids(data__nothing__in=[None]) == [1, 2, 3]
        assert await ids(data__count__in=[10, None]) == [1]
        assert await ids(data__nothing__not=None) == []
        assert await ids(data__url__not_in=["https://z", None]) == [1]
    else:
        assert await ids(data__nothing__in=[None]) == [1]
        assert await ids(data__count__in=[10, None]) == [1]
        assert await ids(data__nothing__not=None) == [2, 3]
        assert await ids(data__url__not_in=["https://z", None]) == [1, 3]


@pytest.mark.asyncio
async def test_a_path_compared_with_a_column_of_text(documents):
    assert await ids(data__url=F("name")) == [1]


@pytest.mark.asyncio
async def test_strings_compare_as_text(documents):
    assert await ids(data__url__gt="https") == [2]
    assert await ids(data__url__lt="https") == [1]
    assert await ids(data__url__range=("http", "httpz")) == [1, 2]


@pytest.mark.asyncio
async def test_a_digit_indexes_an_array_and_names_a_key_of_an_object(clickhouse_db):
    await Player.objects.create(id=1, name="array", data={"list": [5, 6, 7]})
    await Player.objects.create(id=2, name="object", data={"list": {"0": 6, "-1": 7, "x": 5}})
    assert await ids(data__list__0=5) == [1]
    assert await ids(data__list__0=6) == [2]
    assert await ids(data__list__1=6) == [1]
    assert await ids(data__list__1__isnull=False) == [1]
    assert await ids(data__list__0__isnull=False) == [1, 2]


@pytest.mark.asyncio
async def test_a_string_looking_like_a_date_stays_a_string(clickhouse_db):
    document = {"day": "2024-01-02", "moment": "2024-01-02T03:04:05", "zoned": "2024-01-02T03:04:05.123456+00:00"}
    await Player.objects.create(id=1, name="dated", data=document)
    assert (await Player.objects.get(id=1)).data == document
    assert await ids(data__day="2024-01-02") == [1]


@pytest.mark.asyncio
async def test_a_key_holding_a_dot_is_a_key_not_a_path(clickhouse_db):
    """The JSON type read a dot in a key as a nested path, an update's mutation too: {"a.b": 1} came
    back as {"a": {"b": 1}}."""
    document = {"a.b": 1, "a": {"c": 2}, "deep": {"k.l": [1, {"m.n": 4}]}}
    player = await Player.objects.create(id=1, name="dots", data=document)
    assert (await Player.objects.get(id=1)).data == document
    assert await ids(**{"data__a.b": 1}) == [1]
    assert await ids(data__has_key="a.b") == [1]
    await Player.objects.filter(id=1).update(data={"p.q": 5})
    assert (await Player.objects.get(id=1)).data == {"p.q": 5}
    player.data = {"r.s": {"t.u": 6}}
    await player.save()
    assert (await Player.objects.get(id=1)).data == {"r.s": {"t.u": 6}}


@pytest.mark.asyncio
async def test_a_key_the_json_type_cant_hold_is_refused(clickhouse_db):
    connection = Player._meta.connection
    if not connection.dialect.stores_json_natively:
        pytest.skip("the column holds the JSON text")
    with pytest.raises(ValidationError, match="can't hold '%2E'"):
        await Player.objects.create(id=1, name="escape", data={"x%2Ey": 1})
    assert await Player.objects.count() == 0


@pytest.mark.database_independent
def test_the_json_text_of_a_dotted_key_follows_the_server():
    """A dot in a value or a number is no key - only a key's dot is escaped."""
    field = Player._meta.fields_map["data"]
    escaped = ClickhouseJsonValues.to_db_escaping_key_dots(field, {"a.b": "c.d", "n": 1.5, "l": [{"e.f": 1}]}, Player)
    assert escaped == field.to_db_value({"a%2Eb": "c.d", "n": 1.5, "l": [{"e%2Ef": 1}]}, Player)


@pytest.mark.parametrize(
    "data",
    [
        {"a": None, "b": 1},
        {"e": {}, "b": 1},
        {"n": {"x": None}},
        {"l": [{"a": 1}, {"b": None}]},
        {"big": 10**30},
        {"l": [-(2**63) - 1]},
        {"mix": [[2**60 + 1], [0.5]]},
    ],
)
@pytest.mark.asyncio
async def test_a_value_a_json_column_would_read_back_otherwise_is_refused(clickhouse_db, data):
    """A JSON column dropped a key holding null or an empty object and read an integer past 64 bits as a
    float - the text stored on the native protocol keeps them all."""
    if not Player._meta.connection.features.supports_json_type:
        await Player.objects.create(id=1, name="kept", data=data)
        assert await Player.objects.values_list("data", flat=True) == [data]
        return
    with pytest.raises(ValidationError, match="a JSON column of ClickHouse (keeps|reads)"):
        await Player.objects.create(id=1, name="refused", data=data)
    await Player.objects.create(id=1, name="kept", data={"b": 1})
    with pytest.raises(ValidationError, match="a JSON column of ClickHouse (keeps|reads)"):
        await Player.objects.filter(id=1).update(data=data)
    assert await Player.objects.values_list("data", flat=True) == [{"b": 1}]


@pytest.mark.asyncio
async def test_arithmetic_on_a_json_value_is_refused(clickhouse_db):
    """F("data__count") + 1 added a number to the JSON text ClickHouse reads - code 43."""
    from hare.exceptions import FieldError
    from hare.fields import IntField
    from hare.query.functions import Cast

    await create_counts()
    with pytest.raises(FieldError, match="arithmetic on a JSON value"):
        await Player.objects.annotate(next=F("data__count") + 1).values_list("next", flat=True)
    cast = (
        await Player.objects.filter(id=1)
        .annotate(next=Cast(F("data__count"), IntField()) + 1)
        .values_list("next", flat=True)
    )
    assert cast == [11]


@pytest.mark.asyncio
async def test_max_and_min_of_a_json_value_are_refused_and_take_a_cast_one(clickhouse_db):
    """They compared the JSON text - Max over 9, 100 and 25 gave 9, while an ordering by the path orders numbers."""
    for row_id, count in ((1, 9), (2, 100), (3, 25)):
        await Player.objects.create(id=row_id, name=str(row_id), data={"count": count})
    for aggregate in (Max("data__count"), Min("data__count")):
        with pytest.raises(FieldError, match=r"of a JSON value would compare its text .* Cast\(\)"):
            await Player.objects.aggregate(value=aggregate)
    cast_count = Cast("data__count", fields.IntField())
    assert await Player.objects.aggregate(top=Max(cast_count), bottom=Min(cast_count)) == {"top": 100, "bottom": 9}
