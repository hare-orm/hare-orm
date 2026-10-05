"""The rows of values() and values_list() as the mypy plugin types them."""

from datetime import date
from decimal import Decimal
from typing import Any, assert_type

from hare.query.expressions import F
from hare.query.functions import Count
from tests.contrib.mypy.models import Address, Shelf, ShelfStatus, Volume, Writer


async def value_rows() -> None:
    rows = await Volume.objects.values("id", "title", "writer", "writer__name", "shelf__label", "labels__name")
    row = rows[0]
    assert_type(row["id"], int)
    assert_type(row["title"], str)
    assert_type(row["writer"], int)
    assert_type(row["writer__name"], str)
    assert_type(row["shelf__label"], str | None)
    assert_type(row["labels__name"], str | None)
    row["nope"]  # E: "nope" is not a valid TypedDict key
    renamed = await Volume.objects.values(name="title", author=F("writer__name"))
    assert_type(renamed[0]["name"], str)
    assert_type(renamed[0]["author"], str)
    every_field = await Writer.objects.values()
    assert_type(every_field[0]["born"], date | None)
    assert_type(every_field[0]["address"], Address | None)
    assert_type(every_field[0]["rating"], int)
    counted = await Volume.objects.annotate(label_count=Count("labels")).values("title", "label_count")
    assert_type(counted[0]["label_count"], int)
    grouped = await Volume.objects.values("title").annotate(label_count=Count("labels"))
    assert_type(grouped[0]["label_count"], int)
    await Volume.objects.values("titel")  # E: Unknown filter param 'titel': Volume has no field 'titel'
    await Volume.objects.values("title__icontains")  # E: values(): Volume has no field 'title__icontains'


async def value_list_rows(names: list[str]) -> None:
    pairs = await Volume.objects.values_list("id", "price")
    assert_type(pairs, list[tuple[int, Decimal]])
    titles = await Volume.objects.values_list("title", flat=True)
    assert_type(titles, list[str])
    first_title = await Volume.objects.values_list("title", flat=True).first()
    assert_type(first_title, str | None)
    one_pair = await Volume.objects.values_list("id", "title").get(id=1)
    assert_type(one_pair, tuple[int, str])
    statuses = await Shelf.objects.values_list("status", flat=True)
    assert_type(statuses, list[ShelfStatus])
    named = await Volume.objects.values_list("title", named=True)
    assert_type(named, list[Any])
    expressions = await Volume.objects.values_list("id", author=F("writer__name"))
    assert_type(expressions, list[tuple[int, str]])
    unknown = await Volume.objects.values_list(*names)
    assert_type(unknown, list[tuple[Any, ...]])
