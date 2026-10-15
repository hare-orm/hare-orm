"""Filters on a path into a JSON field on ClickHouse - a JSON column, or the document's text on a server
without the JSON type: a value at the path compares and orders as the same JSON value does on
PostgreSQL, however each side writes it; a JSON column keeps no null, which reads as a missing path."""

import pytest
import pytest_asyncio

from hare.dialects.clickhouse.types.clickhouse_json_values import ClickhouseJsonValues
from hare.exceptions import ValidationError
from hare.query.expressions import F
from tests.dialects.clickhouse.models import Player


@pytest_asyncio.fixture
async def documents(clickhouse_db):
    await Player.objects.create(
        id=1,
        name="http://x/y",
        data={"url": "http://x/y", "ratio": 1.0, "big": 1e20, "count": 10, "nothing": None, "flag": True},
    )
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


@pytest.mark.asyncio
async def test_numbers_compare_by_value_not_by_text(documents):
    assert await ids(data__count__gt=9) == [1]
    assert await ids(data__count__gte=9) == [1, 2]
    assert await ids(data__count__lt=10) == [2, 3]
    assert await ids(data__count__range=(9, 10)) == [1, 2]
    # A None bound is the JSON null, below every value - as on PostgreSQL.
    assert await ids(data__count__range=(9, None)) == []
    assert await ids(data__count__range=(None, 9)) == [2, 3]


@pytest.mark.asyncio
async def test_values_order_as_jsonb(documents):
    # A string is below every number, a missing value counts as no value.
    assert await Player.objects.order_by("data__count").values_list("id", flat=True) == [3, 2, 1]
    assert await Player.objects.order_by("-data__ratio", "id").values_list("id", flat=True) == [3, 2, 1]


@pytest.mark.asyncio
async def test_a_json_null_is_a_value_and_a_missing_key_none(documents):
    if Player._meta.connection.features.supports_json_type:
        # A JSON column drops a null - the path reads as a missing one.
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
        # A JSON column drops a null - None matches a missing path, as equality does.
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
    # A string is below every number on PostgreSQL too.
    assert await ids(data__count__lt="b") == [3]


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
    assert await ids(data__contains={"a.b": 1}) == [1]
    assert await ids(data__contained_by={**document, "other": 5}) == [1]
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
    if connection.features.escapes_json_key_dots:
        with pytest.raises(ValidationError, match="can't hold '%2E'"):
            await Player.objects.create(id=1, name="escape", data={"x%2Ey": 1})
    else:
        with pytest.raises(ValidationError, match="can't hold a dot"):
            await Player.objects.create(id=1, name="dot", data={"a.b": 1})
    assert await Player.objects.count() == 0


def test_the_json_text_of_a_dotted_key_follows_the_server():
    """A dot in a value or a number is no key - only a key's dot is escaped, or refused before 25.8."""
    field = Player._meta.fields_map["data"]
    escaped = ClickhouseJsonValues.to_db_escaping_key_dots(field, {"a.b": "c.d", "n": 1.5, "l": [{"e.f": 1}]}, Player)
    assert escaped == field.to_db_value({"a%2Eb": "c.d", "n": 1.5, "l": [{"e%2Ef": 1}]}, Player)
    plain = {"a": "b.c", "n": 1.5}
    assert ClickhouseJsonValues.to_db_refusing_key_dots(field, plain, Player) == field.to_db_value(plain, Player)
    with pytest.raises(ValidationError, match="can't hold a dot"):
        ClickhouseJsonValues.to_db_refusing_key_dots(field, {"x": {"a.b": 1}}, Player)
