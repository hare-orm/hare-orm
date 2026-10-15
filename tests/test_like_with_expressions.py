"""contains/startswith/endswith (and the i... variants) compared with a column or an expression: the
pattern is built in SQL from the value's text, its % _ and \\ matched literally; a NULL value
matches nothing."""

from __future__ import annotations

import pytest

from hare.query.expressions import F, Value
from hare.query.functions import Concat, Upper
from tests.testmodels import Tournament


async def names(**filters) -> list[str]:
    return sorted(await Tournament.objects.filter(**filters).values_list("name", flat=True))


@pytest.mark.asyncio
async def test_text_lookups_with_a_column(db):
    await Tournament.objects.create(id=1, name="abc", desc="xxabcxx")
    await Tournament.objects.create(id=2, name="ab", desc="abyy")
    await Tournament.objects.create(id=3, name="yy", desc="abyy")
    await Tournament.objects.create(id=4, name="1%_\\", desc="--1%_\\--")
    await Tournament.objects.create(id=5, name="1%", desc="1x")  # "%" must not be a wildcard
    await Tournament.objects.create(id=6, name="a_c", desc="abc")  # "_" must not be a wildcard
    await Tournament.objects.create(id=7, name="Mid", desc="AMIDB")
    await Tournament.objects.create(id=8, name="nothing", desc=None)

    assert await names(desc__contains=F("name")) == ["1%_\\", "ab", "abc", "yy"]
    assert await names(desc__startswith=F("name")) == ["ab"]
    assert await names(desc__endswith=F("name")) == ["yy"]
    assert await names(desc__icontains=F("name")) == ["1%_\\", "Mid", "ab", "abc", "yy"]
    assert await names(desc__istartswith=F("name")) == ["ab"]
    assert await names(desc__iendswith=F("name")) == ["yy"]
    assert await names(name__contains=F("desc")) == []
    assert await names(desc__icontains=Upper(F("name"))) == ["1%_\\", "Mid", "ab", "abc", "yy"]
    assert await names(desc__startswith=Concat(F("name"), Value("y"))) == ["ab"]
    # A NULL value matches nothing - not every row through a "%%" pattern.
    assert "nothing" not in await names(name__contains=F("desc"))
    assert sorted(await Tournament.objects.exclude(desc__contains=F("name")).values_list("id", flat=True)) == [
        5,
        6,
        7,
        8,
    ]


@pytest.mark.asyncio
async def test_text_lookups_with_an_untyped_value(db):
    """A value of no known type - an annotated Value(), a bare one - is read as text, also in the
    check that it isn't NULL."""
    await Tournament.objects.create(id=1, name="50% off")
    await Tournament.objects.create(id=2, name="50 off")
    assert await names(name__contains=Value("%")) == ["50% off"]
    annotated = Tournament.objects.annotate(pattern=Value("%")).filter(name__icontains=F("pattern"))
    assert await annotated.values_list("name", flat=True) == ["50% off"]
    assert await names(name__startswith=Value(None)) == []
