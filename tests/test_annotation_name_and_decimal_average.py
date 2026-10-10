from decimal import Decimal

import pytest

from hare.exceptions import FieldError
from hare.query.expressions import F, Window
from hare.query.functions import Avg, Length
from hare.query.functions.window import Avg as WindowAvg
from tests.testmodels import Author, Book, DecimalFields


@pytest.mark.asyncio
async def test_annotation_named_after_a_field_raises(db):
    with pytest.raises(FieldError, match="conflict with field"):
        Author.objects.annotate(name=Length("name"))
    with pytest.raises(FieldError, match="conflict with field"):
        Author.objects.all().alias(name=Length("name"))
    with pytest.raises(FieldError, match="conflict with field"):
        Author.objects.annotate(pk=F("id"))
    with pytest.raises(FieldError, match="conflict with field"):
        Book.objects.all().values(rating=F("rating") * 2)
    with pytest.raises(FieldError, match="conflict with field"):
        Book.objects.all().values_list(rating=F("rating") * 2)


@pytest.mark.asyncio
async def test_values_renaming_a_field_is_still_allowed(db):
    author = await Author.objects.create(name="First")
    await Book.objects.create(name="A", author=author, rating=1.0)
    assert await Book.objects.all().values(rating="name") == [{"rating": "A"}]
    assert await Book.objects.annotate(doubled=F("rating") * 2).values("doubled", "rating") == [
        {"doubled": 2.0, "rating": 1.0}
    ]


@pytest.mark.asyncio
async def test_decimal_average_is_not_rounded_to_the_fields_scale(db):
    for value in ("1.0000", "0.0000", "0.0000"):
        await DecimalFields.objects.create(decimal=Decimal(value), decimal_nodec=1)
    exact_third = Decimal(1) / 3
    average = (await DecimalFields.objects.all().aggregate(average=Avg("decimal")))["average"]
    assert type(average) is Decimal
    assert abs(average - exact_third) < Decimal("1e-12")
    annotated = (
        await DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(average=Avg("decimal"))
        .values_list("average", flat=True)
    )
    assert abs(annotated[0] - exact_third) < Decimal("1e-12")
    windowed = (
        await DecimalFields.objects.all()
        .annotate(average=Window(WindowAvg("decimal")))
        .values_list("average", flat=True)
    )
    assert all(type(value) is Decimal and abs(value - exact_third) < Decimal("1e-12") for value in windowed)
