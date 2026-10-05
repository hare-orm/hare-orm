"""Paths through array and range fields - items, slices, lengths, bounds, flags - and the range
position operators, in filters, F(), values(), order_by() and across relations."""

import datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.dialects.postgresql.fields.ranges import Range
from hare.exceptions import FieldError
from hare.query.expressions import F, Q
from hare.query.functions import Count
from tests.dialects.postgresql.models_container_paths import Crate, Shelf


@pytest_asyncio.fixture
async def crates(db_container_paths):
    shelf = await Shelf.objects.create(id=1)
    await Crate.objects.create(
        id=1,
        shelf=shelf,
        span=Range(1, 5),
        days=Range(datetime.date(2024, 1, 1), datetime.date(2024, 2, 1)),
        moments=Range(
            datetime.datetime(2024, 1, 1, 10, tzinfo=datetime.UTC), datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC)
        ),
        prices=Range(Decimal("1.5"), Decimal("9.25")),
        numbers=[3, 1, 2],
        words=["pear", "apple"],
        grid=[[1, 2], [3, 4]],
    )
    await Crate.objects.create(
        id=2,
        span=Range(7, None),
        days=Range(None, datetime.date(2024, 3, 1)),
        prices=Range(Decimal("0.1"), None),
        numbers=[9],
        words=["fig"],
        grid=[[5, 6], [7, 8], [9, 10]],
    )
    await Crate.objects.create(id=3, span=Range(3, 3), numbers=[], words=[], grid=[])
    await Crate.objects.create(id=4)


async def matching(*conditions, **lookups):
    return await Crate.objects.filter(*conditions, **lookups).order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookups, expected",
    [
        ({"numbers__0": 3}, [1]),
        ({"numbers__1__gt": 0}, [1]),
        ({"numbers__-1": 2}, [1]),
        ({"numbers__5__isnull": True}, [1, 2, 3, 4]),
        ({"numbers__0__in": [9, 3]}, [1, 2]),
        ({"numbers__0_2": [3, 1]}, [1]),
        ({"numbers__0_1__contains": [9]}, [2]),
        ({"numbers__len": 0}, [3]),
        ({"numbers__len__gte": 1}, [1, 2]),
        ({"words__1__startswith": "app"}, [1]),
        ({"words__0__icontains": "IG"}, [2]),
        ({"grid__1": [3, 4]}, [1]),
        ({"grid__1__0": 7}, [2]),
        ({"grid__0_1": [[1, 2]]}, [1]),
        ({"grid__len": 3}, [2]),
        ({"grid__0__len": 2}, [1, 2]),
    ],
)
async def test_array_paths_filter(crates, lookups, expected):
    assert await matching(**lookups) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookups, expected",
    [
        ({"span__fully_lt": Range(6, 8)}, [1]),
        ({"span__fully_gt": Range(0, 4)}, [2]),
        ({"span__not_lt": Range(3, 4)}, [2]),
        ({"span__not_gt": Range(4, 6)}, [1]),
        ({"span__adjacent_to": Range(5, 7)}, [1, 2]),
        ({"span__startswith": 7}, [2]),
        ({"span__endswith__lte": 5}, [1]),
        ({"span__isempty": True}, [3]),
        ({"span__lower_inc": True}, [1, 2]),
        ({"span__upper_inf": True}, [2]),
        ({"days__startswith": datetime.date(2024, 1, 1)}, [1]),
        ({"days__endswith__lt": datetime.date(2024, 2, 15)}, [1]),
        ({"days__startswith__year": 2024}, [1]),
        ({"days__lower_inf": True}, [2]),
        ({"moments__startswith__gte": datetime.datetime(2024, 1, 1, 9, tzinfo=datetime.UTC)}, [1]),
        ({"moments__endswith__date": datetime.date(2024, 1, 2)}, [1]),
        ({"prices__startswith__gt": Decimal("1")}, [1]),
        ({"prices__upper_inf": True}, [2]),
    ],
)
async def test_range_paths_and_position_lookups_filter(crates, lookups, expected):
    assert await matching(**lookups) == expected


@pytest.mark.asyncio
async def test_paths_negate_and_combine(crates):
    assert await Crate.objects.exclude(numbers__0=3).order_by("id").values_list("id", flat=True) == [2, 3, 4]
    assert await matching(Q(numbers__0=9) | Q(span__startswith=1)) == [1, 2]
    assert await matching(~Q(numbers__len=0)) == [1, 2, 4]


@pytest.mark.asyncio
async def test_paths_across_a_relation(crates):
    assert await Shelf.objects.filter(crates__numbers__0=3).values_list("id", flat=True) == [1]
    assert await Shelf.objects.filter(crates__span__startswith=1).values_list("id", flat=True) == [1]
    assert await Shelf.objects.all().values_list("crates__numbers__0", flat=True) == [3]
    await Shelf.objects.create(id=2)
    await Crate.objects.filter(id=2).update(shelf_id=2)
    assert await Shelf.objects.all().order_by("-crates__numbers__0").values_list("id", flat=True) == [2, 1]


@pytest.mark.asyncio
async def test_paths_in_f(crates):
    assert await matching(numbers__0__gt=F("numbers__1")) == [1]
    assert await matching(span__endswith__gt=F("numbers__0")) == [1]
    first_plus_one = Crate.objects.annotate(value=F("numbers__0") + 1).order_by("id").values_list("value", flat=True)
    assert await first_plus_one == [4, 10, None, None]
    low = Crate.objects.annotate(low=F("span__startswith")).filter(low__gte=5).values_list("id", flat=True)
    assert await low == [2]


@pytest.mark.asyncio
async def test_paths_in_values(crates):
    assert await Crate.objects.all().order_by("id").values(
        "id", "numbers__0_2", "span__isempty", "days__startswith", "prices__startswith", "moments__endswith"
    ) == [
        {
            "id": 1,
            "numbers__0_2": [3, 1],
            "span__isempty": False,
            "days__startswith": datetime.date(2024, 1, 1),
            "prices__startswith": Decimal("1.5"),
            "moments__endswith": datetime.datetime(2024, 1, 2, tzinfo=datetime.UTC),
        },
        {
            "id": 2,
            "numbers__0_2": [9],
            "span__isempty": False,
            "days__startswith": None,
            "prices__startswith": Decimal("0.1"),
            "moments__endswith": None,
        },
        {
            "id": 3,
            "numbers__0_2": [],
            "span__isempty": True,
            "days__startswith": None,
            "prices__startswith": None,
            "moments__endswith": None,
        },
        {
            "id": 4,
            "numbers__0_2": None,
            "span__isempty": None,
            "days__startswith": None,
            "prices__startswith": None,
            "moments__endswith": None,
        },
    ]
    assert await Crate.objects.all().order_by("id").values_list("grid__1", "grid__1__0", "grid__len") == [
        ([3, 4], 3, 2),
        ([7, 8], 7, 3),
        (None, None, 0),
        (None, None, None),
    ]


@pytest.mark.asyncio
async def test_paths_in_order_by_and_group_by(crates):
    assert await Crate.objects.all().order_by("numbers__0").values_list("id", flat=True) == [1, 2, 3, 4]
    assert await Crate.objects.all().order_by("-numbers__len", "id").values_list("id", flat=True) == [4, 1, 2, 3]
    assert await Crate.objects.filter(span__isnull=False).order_by("span__startswith").values_list(
        "id", flat=True
    ) == [
        1,
        2,
        3,
    ]
    counts = (
        Crate.objects.filter(numbers__isnull=False)
        .annotate(count=Count("id"))
        .group_by("numbers__len")
        .order_by("numbers__len")
        .values_list("numbers__len", "count")
    )
    assert await counts == [(0, 1), (1, 1), (3, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup", ["numbers__first", "span__0", "numbers__len__0"])
async def test_unknown_path_raises(crates, lookup):
    with pytest.raises(FieldError):
        await matching(**{lookup: 1})


@pytest.mark.asyncio
async def test_unknown_values_path_raises(crates):
    with pytest.raises(FieldError):
        await Crate.objects.all().values_list("numbers__0__1", flat=True)
