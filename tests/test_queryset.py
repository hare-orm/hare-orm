import asyncio
import contextlib
import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytest_asyncio

from hare import Connections
from hare.contrib import test
from hare.contrib.test import requires_features
from hare.dialects.postgresql.query import PostgresqlQuery
from hare.exceptions import (
    DoesNotExist,
    FieldError,
    MultipleObjectsReturned,
    NoValuesFetched,
    ObjectLookupError,
    ProtectedError,
    QueryError,
    UnSupportedError,
)
from hare.query.expressions import Case, Exists, F, OuterRef, Q, RawSQL, Subquery, Value, When
from hare.query.functions import Avg, Count, Max, Sum, Upper
from hare.query.plans.statement_plans import StatementPlans
from hare.query.relation_loading.prefetch import Prefetch
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    Address,
    Author,
    Book,
    BookNoConstraint,
    BulkDeleteCountCompositeParent,
    BulkDeleteCountCompositePeer,
    BulkDeleteCountGuard,
    BulkDeleteCountItem,
    BulkDeleteCountParent,
    BulkDeleteCountUnguardedItem,
    BulkDeleteCountUnguardedParent,
    CompositePkOwningFK,
    CompositePkThing,
    DirtyTrackedComposite,
    Event,
    HardDeleteUnconstrainedChildCascade,
    HardDeleteUnconstrainedParent,
    IntFields,
    LazyJoinedChild,
    LazyJoinedParent,
    LazySelectChild,
    LazySelectParent,
    MinRelation,
    Node,
    Reporter,
    SoftDeleteChildCascadeSoft,
    SoftDeleteParent,
    Team,
    TemporalBatch,
    TemporalRecord,
    Tournament,
    Tree,
)

# TODO: Test the many exceptions in QuerySet
# TODO: .filter(intnum_null=None) does not work as expected


@pytest_asyncio.fixture
async def intfields_data(db):
    """Build large dataset for IntFields tests."""
    intfields = [await IntFields.objects.create(intnum=val) for val in range(10, 100, 3)]
    return intfields


@pytest.mark.asyncio
async def test_all_count(db, intfields_data):
    assert await IntFields.objects.all().count() == 30
    assert await IntFields.objects.filter(intnum_null=80).count() == 0


@pytest.mark.asyncio
async def test_count_respects_distinct(db):
    """count() ignored .distinct() - a plain COUNT(*) counted the JOIN-multiplied rows distinct()
    exists to collapse: a parent with two matching children gave len(await qs) == 1 but
    await qs.count() == 2."""
    tournament = await Tournament.objects.create(name="t")
    await Event.objects.create(name="e1", tournament=tournament)
    await Event.objects.create(name="e2", tournament=tournament)
    queryset = Tournament.objects.filter(events__name__startswith="e")

    assert await queryset.count() == 2
    assert await queryset.distinct().count() == 1
    assert await queryset.distinct().count() == len(await queryset.distinct())


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_count_on_distinct_with_fields_counts_the_picked_rows(db):
    """count()/exists() on distinct(<fields>) count the rows DISTINCT ON picks, one per group."""
    for name in ("a", "a", "b"):
        await Tournament.objects.create(name=name)
    distinct_names = Tournament.objects.all().order_by("name", "-id").distinct("name")
    assert await distinct_names.count() == 2
    assert await distinct_names.offset(1).count() == 1
    assert await distinct_names.offset(1).exists() is True
    assert await distinct_names.offset(2).exists() is False


@pytest.mark.asyncio
async def test_exists(db, intfields_data):
    ret = await IntFields.objects.filter(intnum=0).exists()
    assert not ret

    ret = await IntFields.objects.filter(intnum=10).exists()
    assert ret

    ret = await IntFields.objects.filter(intnum__gt=10).exists()
    assert ret

    ret = await IntFields.objects.filter(intnum__lt=10).exists()
    assert not ret


@pytest.mark.asyncio
async def test_exists_respects_limit_and_offset(db, intfields_data):
    """exists() used to silently drop the queryset's own .limit()/.offset() (only count() kept
    them) - .offset(10).exists() on a queryset matching 4 rows (intnum 10, 13, 16, 19) was True
    while .count() of the very same queryset was 0."""
    matching = IntFields.objects.filter(intnum__gte=10, intnum__lt=20)

    assert await matching.exists() is True
    assert await matching.offset(3).exists() is True
    assert await matching.offset(4).exists() is False
    assert await matching.offset(10).exists() is False
    assert await matching.limit(0).exists() is False
    assert await matching.limit(2).offset(3).exists() is True
    assert await matching.offset(4).count() == 0


@pytest.mark.asyncio
async def test_exists_annotation_respects_inner_offset(db, intfields_data):
    """Exists(qs) goes through the same ExistsQuery - an inner .offset() past the last row must
    make the annotation False for every outer row, not True."""
    inner_empty = IntFields.objects.filter(intnum__gte=10, intnum__lt=20).offset(4)
    inner_full = IntFields.objects.filter(intnum__gte=10, intnum__lt=20)

    rows = await IntFields.objects.filter(intnum=10).annotate(empty=Exists(inner_empty), full=Exists(inner_full))

    assert [(row.empty, row.full) for row in rows] == [(False, True)]


@pytest.mark.asyncio
async def test_contains_on_a_sliced_queryset_raises(db, intfields_data):
    """A plain `WHERE pk = ...` can't express "is this row inside the slice" - contains() used
    to silently ignore the slice and answer for the whole table (limit(0).contains(obj) was True)."""
    obj = await IntFields.objects.filter(intnum=10).first()

    with pytest.raises(QueryError, match="sliced queryset"):
        await IntFields.objects.all().limit(0).contains(obj)
    with pytest.raises(QueryError, match="sliced queryset"):
        await IntFields.objects.all().offset(1).contains(obj)


@pytest.mark.asyncio
async def test_contains(db, intfields_data):
    obj = await IntFields.objects.filter(intnum=10).first()
    assert await IntFields.objects.all().contains(obj)

    assert await IntFields.objects.filter(intnum__lt=50).contains(obj)

    assert not await IntFields.objects.filter(intnum__gt=50).contains(obj)


@pytest.mark.asyncio
async def test_contains_when_no_pk(db, intfields_data):
    with pytest.raises(QueryError, match="The given object does not have a primary key."):
        await IntFields.objects.all().contains(IntFields(intnum=99))


@pytest.mark.asyncio
async def test_contains_when_composite_pk_unset(db):
    # obj.pk is always a non-empty tuple for a composite pk, even before every component is
    # assigned ((None, None)) - a plain truthiness check never catches that, letting an unset
    # composite pk fall through into query construction instead of this clean error.

    never_saved = CompositePkThing(thing_id=1, revision=1, name="dup")
    object.__setattr__(never_saved, "thing_id", None)
    object.__setattr__(never_saved, "revision", None)

    with pytest.raises(QueryError, match="The given object does not have a primary key."):
        await CompositePkThing.objects.all().contains(never_saved)


@pytest.mark.asyncio
async def test_contains_with_wrong_model(db, intfields_data):
    with pytest.raises(QueryError, match="The given object is not an instance of the queryset's model."):
        await IntFields.objects.all().contains(Tournament(name="test"))


@pytest.mark.asyncio
async def test_limit_count(db, intfields_data):
    assert await IntFields.objects.all().limit(10).count() == 10


@pytest.mark.asyncio
async def test_limit_zero_count(db, intfields_data):
    # limit(0) means zero rows, so count() must be 0 (not the total), matching
    # the actual limited query.
    assert await IntFields.objects.all().limit(0).count() == 0
    assert await IntFields.objects.all().limit(0).count() == len(await IntFields.objects.all().limit(0))


@pytest.mark.asyncio
async def test_limit_negative(db, intfields_data):
    with pytest.raises(QueryError, match="Limit should be non-negative number"):
        await IntFields.objects.all().limit(-10)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_limit_zero(db, intfields_data):
    sql = IntFields.objects.all().only("id").limit(0).sql()
    assert sql == 'SELECT "id" "id" FROM "intfields" LIMIT ?'


@pytest.mark.asyncio
async def test_offset_count(db, intfields_data):
    assert await IntFields.objects.all().offset(10).count() == 20


@pytest.mark.asyncio
async def test_offset_count_beyond_total(db, intfields_data):
    # An offset past the total must report 0, not a negative count (the SQL
    # LIMIT/OFFSET would return zero rows).
    assert await IntFields.objects.all().offset(100).count() == 0
    assert await IntFields.objects.all().offset(100).count() == len(await IntFields.objects.all().offset(100))


@pytest.mark.asyncio
async def test_offset_negative(db, intfields_data):
    with pytest.raises(QueryError, match="Offset should be non-negative number"):
        await IntFields.objects.all().offset(-10)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("s", "manual_ops"),
    [
        (slice(1, 5), (("offset", 1), ("limit", 4))),
        (slice(None, 5), (("limit", 5),)),
        (slice(5, None), (("offset", 5),)),
    ],
    ids=["start_and_stop", "only_limit", "only_offset"],
)
async def test_slicing_matches_manual_offset_limit(db, intfields_data, s, manual_ops):
    sliced_queryset = IntFields.objects.all().order_by("intnum")[s]
    manually_sliced_queryset = IntFields.objects.all().order_by("intnum")
    for method_name, arg in manual_ops:
        manually_sliced_queryset = getattr(manually_sliced_queryset, method_name)(arg)
    assert list(await sliced_queryset) == list(await manually_sliced_queryset)


@pytest.mark.asyncio
async def test_slicing_count(db, intfields_data):
    queryset = IntFields.objects.all().order_by("intnum")[1:5]
    assert await queryset.count() == 4


def test_slicing_negative_values(db):
    with pytest.raises(
        QueryError,
        match="Slice start should be non-negative number or None.",
    ):
        _ = IntFields.objects.all()[-1:]

    with pytest.raises(
        QueryError,
        match="Slice stop should be non-negative number or None.",
    ):
        _ = IntFields.objects.all()[:-1]


@pytest.mark.asyncio
async def test_slicing_stop_not_past_start_is_empty(db, intfields_data):
    """Like a list slice (and Django), an empty or backwards slice selects no rows."""
    queryset = IntFields.objects.all().order_by("intnum")
    for empty_slice in (queryset[2:1], queryset[3:3], queryset[1:5][4:2]):
        assert await empty_slice == []
        assert await empty_slice.count() == 0
        assert await empty_slice.exists() is False


@pytest.mark.asyncio
async def test_slicing_steps(db, intfields_data):
    sliced_queryset = IntFields.objects.all().order_by("intnum")[::1]
    manually_sliced_queryset = IntFields.objects.all().order_by("intnum")
    assert list(await sliced_queryset) == list(await manually_sliced_queryset)

    with pytest.raises(
        QueryError,
        match="Slice steps should be 1 or None.",
    ):
        _ = IntFields.objects.all()[::2]


@pytest.mark.asyncio
async def test_double_slicing_composes_like_list_slicing(db, intfields_data):
    """A second [] slice must compose with the first (like x[5:10][:3]), not overwrite it."""
    ordered = IntFields.objects.all().order_by("intnum")

    rows = await ordered[5:10][:3]
    assert [row.intnum for row in rows] == [25, 28, 31]

    rows = await ordered[5:15][2:4]
    assert [row.intnum for row in rows] == [31, 34]


@pytest.mark.asyncio
async def test_triple_slicing_composes_like_list_slicing(db, intfields_data):
    ordered = IntFields.objects.all().order_by("intnum")
    all_intnums = [row.intnum for row in await ordered]

    rows = await ordered[1:20][2:15][1:5]
    expected = all_intnums[1:20][2:15][1:5]
    assert [row.intnum for row in rows] == expected


@pytest.mark.asyncio
async def test_slicing_composes_with_direct_offset_and_limit(db, intfields_data):
    """.offset()/.limit() set an absolute value, but a subsequent [] slice still composes on
    top of whatever offset/limit is already set, the same way it composes on top of a
    previous [] slice."""
    ordered = IntFields.objects.all().order_by("intnum")
    all_intnums = [row.intnum for row in await ordered]

    rows = await ordered.offset(5).limit(10)[2:4]
    expected = all_intnums[5:15][2:4]
    assert [row.intnum for row in rows] == expected


@pytest.mark.asyncio
async def test_double_slicing_second_slice_out_of_bounds_is_empty(db, intfields_data):
    """[5:10][20:30] - the second slice starts and ends entirely past the first slice's
    5-row window, which must compose to an empty result, not a QueryError."""
    ordered = IntFields.objects.all().order_by("intnum")
    rows = await ordered[5:10][20:30]
    assert list(rows) == []


@pytest.mark.asyncio
async def test_double_slicing_open_ended_at_each_level(db, intfields_data):
    ordered = IntFields.objects.all().order_by("intnum")
    all_intnums = [row.intnum for row in await ordered]

    rows = await ordered[5:][2:5]
    expected = all_intnums[5:][2:5]
    assert [row.intnum for row in rows] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (slice(5, 10), slice(None, 3)),
        (slice(5, 15), slice(2, 4)),
        (slice(0, 30), slice(0, 30)),
        (slice(1, 20), slice(2, 15)),
        (slice(5, None), slice(2, 5)),
        (slice(0, 5), slice(2, None)),
        (slice(10, 12), slice(0, 1)),
    ],
    ids=[
        "bug_repro_1",
        "bug_repro_2",
        "full_range_twice",
        "narrowing",
        "open_stop_then_bounded",
        "bounded_then_open_stop",
        "small_window",
    ],
)
async def test_double_slicing_matches_list_slicing(db, intfields_data, first, second):
    ordered = IntFields.objects.all().order_by("intnum")
    all_intnums = [row.intnum for row in await ordered]

    rows = await ordered[first][second]
    expected = all_intnums[first][second]
    assert [row.intnum for row in rows] == expected


@pytest.mark.asyncio
async def test_join_count(db):
    tour = await Tournament.objects.create(name="moo")
    await MinRelation.objects.create(tournament=tour)

    assert await MinRelation.objects.all().count() == 1
    assert await MinRelation.objects.filter(tournament__id=tour.id).count() == 1


@pytest.mark.asyncio
async def test_modify_dataset(db, intfields_data):
    # Modify dataset
    rows_affected = await IntFields.objects.filter(intnum__gte=70).update(intnum_null=80)
    assert rows_affected == 10
    assert await IntFields.objects.filter(intnum_null=80).count() == 10
    assert await IntFields.objects.filter(intnum_null__isnull=True).count() == 20
    await IntFields.objects.filter(intnum_null__isnull=True).update(intnum_null=-1)
    assert await IntFields.objects.filter(intnum_null=None).count() == 0
    assert await IntFields.objects.filter(intnum_null=-1).count() == 20


@pytest.mark.asyncio
async def test_distinct(db, intfields_data):
    # Test distinct
    await IntFields.objects.filter(intnum__gte=70).update(intnum_null=80)
    await IntFields.objects.filter(intnum_null__isnull=True).update(intnum_null=-1)

    assert await IntFields.objects.all().order_by("intnum_null").distinct().values_list("intnum_null", flat=True) == [
        -1,
        80,
    ]

    assert await IntFields.objects.all().order_by("intnum_null").distinct().values("intnum_null") == [
        {"intnum_null": -1},
        {"intnum_null": 80},
    ]


@pytest.mark.asyncio
async def test_limit_offset_values_list(db, intfields_data):
    # Test limit/offset/ordering values_list
    assert await IntFields.objects.all().order_by("intnum").limit(10).values_list("intnum", flat=True) == [
        10,
        13,
        16,
        19,
        22,
        25,
        28,
        31,
        34,
        37,
    ]

    assert await IntFields.objects.all().order_by("intnum").limit(10).offset(10).values_list("intnum", flat=True) == [
        40,
        43,
        46,
        49,
        52,
        55,
        58,
        61,
        64,
        67,
    ]

    assert await IntFields.objects.all().order_by("intnum").limit(10).offset(20).values_list("intnum", flat=True) == [
        70,
        73,
        76,
        79,
        82,
        85,
        88,
        91,
        94,
        97,
    ]

    assert await IntFields.objects.all().order_by("intnum").limit(10).offset(30).values_list("intnum", flat=True) == []

    assert await IntFields.objects.all().order_by("-intnum").limit(10).values_list("intnum", flat=True) == [
        97,
        94,
        91,
        88,
        85,
        82,
        79,
        76,
        73,
        70,
    ]

    assert await IntFields.objects.filter(intnum__gte=40).order_by("intnum").limit(10).values_list(
        "intnum", flat=True
    ) == [
        40,
        43,
        46,
        49,
        52,
        55,
        58,
        61,
        64,
        67,
    ]


@pytest.mark.asyncio
async def test_limit_offset_values(db, intfields_data):
    # Test limit/offset/ordering values
    assert await IntFields.objects.all().order_by("intnum").limit(5).values("intnum") == [
        {"intnum": 10},
        {"intnum": 13},
        {"intnum": 16},
        {"intnum": 19},
        {"intnum": 22},
    ]

    assert await IntFields.objects.all().order_by("intnum").limit(5).offset(10).values("intnum") == [
        {"intnum": 40},
        {"intnum": 43},
        {"intnum": 46},
        {"intnum": 49},
        {"intnum": 52},
    ]

    assert await IntFields.objects.all().order_by("intnum").limit(5).offset(30).values("intnum") == []

    assert await IntFields.objects.all().order_by("-intnum").limit(5).values("intnum") == [
        {"intnum": 97},
        {"intnum": 94},
        {"intnum": 91},
        {"intnum": 88},
        {"intnum": 85},
    ]

    assert await IntFields.objects.filter(intnum__gte=40).order_by("intnum").limit(5).values("intnum") == [
        {"intnum": 40},
        {"intnum": 43},
        {"intnum": 46},
        {"intnum": 49},
        {"intnum": 52},
    ]


@pytest.mark.asyncio
async def test_first(db, intfields_data):
    # Test first
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).first()).intnum == 40
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).first().values())["intnum"] == 40
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).first().values_list())[1] == 40

    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).first() is None
    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).first().values() is None
    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).first().values_list() is None


@pytest.mark.asyncio
async def test_last(db, intfields_data):
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).last()).intnum == 97
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).last().values())["intnum"] == 97
    assert (await IntFields.objects.all().order_by("intnum").filter(intnum__gte=40).last().values_list())[1] == 97

    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).last() is None
    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).last().values() is None
    assert await IntFields.objects.all().order_by("intnum").filter(intnum__gte=400).last().values_list() is None
    assert (await IntFields.objects.all().filter(intnum__gte=40).last()).intnum == 97


@pytest.mark.asyncio
async def test_latest(db, intfields_data):
    assert (await IntFields.objects.all().latest("intnum")).intnum == 97
    assert (await IntFields.objects.all().order_by("-intnum").first()).intnum == (
        await IntFields.objects.all().latest("intnum")
    ).intnum
    assert (await IntFields.objects.all().filter(intnum__gte=40).latest("intnum")).intnum == 97
    assert (await IntFields.objects.all().filter(intnum__gte=40).latest("intnum").values())["intnum"] == 97
    assert (await IntFields.objects.all().filter(intnum__gte=40).latest("intnum").values_list())[1] == 97

    assert await IntFields.objects.all().filter(intnum__gte=400).latest("intnum") is None
    assert await IntFields.objects.all().filter(intnum__gte=400).latest("intnum").values() is None
    assert await IntFields.objects.all().filter(intnum__gte=400).latest("intnum").values_list() is None

    with pytest.raises(FieldError):
        await IntFields.objects.all().latest()

    with pytest.raises(FieldError):
        await IntFields.objects.all().latest("some_unkown_field")


@pytest.mark.asyncio
async def test_earliest(db, intfields_data):
    assert (await IntFields.objects.all().earliest("intnum")).intnum == 10
    assert (await IntFields.objects.all().order_by("intnum").first()).intnum == (
        await IntFields.objects.all().earliest("intnum")
    ).intnum
    assert (await IntFields.objects.all().filter(intnum__gte=40).earliest("intnum")).intnum == 40
    assert (await IntFields.objects.all().filter(intnum__gte=40).earliest("intnum").values())["intnum"] == 40
    assert (await IntFields.objects.all().filter(intnum__gte=40).earliest("intnum").values_list())[1] == 40

    assert await IntFields.objects.all().filter(intnum__gte=400).earliest("intnum") is None
    assert await IntFields.objects.all().filter(intnum__gte=400).earliest("intnum").values() is None
    assert await IntFields.objects.all().filter(intnum__gte=400).earliest("intnum").values_list() is None

    with pytest.raises(FieldError):
        await IntFields.objects.all().earliest()

    with pytest.raises(FieldError):
        await IntFields.objects.all().earliest("some_unkown_field")


@pytest.mark.asyncio
async def test_get_or_none(db, intfields_data):
    assert (await IntFields.objects.all().get_or_none(intnum=40)).intnum == 40
    assert (await IntFields.objects.all().get_or_none(intnum=40).values())["intnum"] == 40
    assert (await IntFields.objects.all().get_or_none(intnum=40).values_list())[1] == 40

    assert await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=400) is None

    assert await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=400).values() is None

    assert await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=400).values_list() is None

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=40)

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=40).values()

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().order_by("intnum").get_or_none(intnum__gte=40).values_list()


@pytest.mark.asyncio
async def test_get(db, intfields_data):
    await IntFields.objects.filter(intnum__gte=70).update(intnum_null=80)

    # Test get
    assert (await IntFields.objects.all().get(intnum=40)).intnum == 40
    assert (await IntFields.objects.all().get(intnum=40).values())["intnum"] == 40
    assert (await IntFields.objects.all().get(intnum=40).values_list())[1] == 40

    assert (await IntFields.objects.all().all().all().all().all().get(intnum=40)).intnum == 40
    assert (await IntFields.objects.all().all().all().all().all().get(intnum=40).values())["intnum"] == 40
    assert (await IntFields.objects.all().all().all().all().all().get(intnum=40).values_list())[1] == 40

    assert (await IntFields.objects.get(intnum=40)).intnum == 40
    assert (await IntFields.objects.get(intnum=40).values())["intnum"] == 40
    assert (await IntFields.objects.get(intnum=40).values_list())[1] == 40

    with pytest.raises(DoesNotExist):
        await IntFields.objects.all().get(intnum=41)

    with pytest.raises(DoesNotExist):
        await IntFields.objects.all().get(intnum=41).values()

    with pytest.raises(DoesNotExist):
        await IntFields.objects.all().get(intnum=41).values_list()

    with pytest.raises(DoesNotExist):
        await IntFields.objects.get(intnum=41)

    with pytest.raises(DoesNotExist):
        await IntFields.objects.get(intnum=41).values()

    with pytest.raises(DoesNotExist):
        await IntFields.objects.get(intnum=41).values_list()

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().get(intnum_null=80)

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().get(intnum_null=80).values()

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.all().get(intnum_null=80).values_list()

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.get(intnum_null=80)

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.get(intnum_null=80).values()

    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.get(intnum_null=80).values_list()


class _CustomNotFound(Exception):
    pass


@pytest.mark.asyncio
async def test_get_exception_parameter_overrides_does_not_exist(db, intfields_data):
    await IntFields.objects.filter(intnum__gte=70).update(intnum_null=80)

    # `exception=` only replaces DoesNotExist on a zero-match .get() - never MultipleObjectsReturned.
    with pytest.raises(_CustomNotFound):
        await IntFields.objects.get(intnum=41, exception=_CustomNotFound)

    with pytest.raises(_CustomNotFound):
        await IntFields.objects.all().get(intnum=41, exception=_CustomNotFound)

    with pytest.raises(_CustomNotFound):
        await IntFields.objects.get(intnum=41, exception=_CustomNotFound).values()

    with pytest.raises(_CustomNotFound):
        await IntFields.objects.get(intnum=41, exception=_CustomNotFound).values_list()

    # An already-constructed instance is raised as-is, not re-instantiated.
    marker = _CustomNotFound("no such intnum")
    with pytest.raises(_CustomNotFound) as exc_info:
        await IntFields.objects.get(intnum=41, exception=marker)
    assert exc_info.value is marker

    # MultipleObjectsReturned is unaffected by exception= - it's still the real exception, not
    # silently swapped out.
    with pytest.raises(MultipleObjectsReturned):
        await IntFields.objects.get(intnum_null=80, exception=_CustomNotFound)

    # A real match is returned normally - exception= is dormant unless the query is genuinely
    # empty.
    assert (await IntFields.objects.get(intnum=40, exception=_CustomNotFound)).intnum == 40


@pytest.mark.asyncio
async def test_delete(db, intfields_data):
    # Test delete
    await (await IntFields.objects.get(intnum=40)).delete()

    with pytest.raises(DoesNotExist):
        await IntFields.objects.get(intnum=40)

    assert await IntFields.objects.all().count() == 29

    rows_affected = await IntFields.objects.filter(intnum__gte=70).order_by("intnum").limit(10).delete()
    assert rows_affected == 10

    assert await IntFields.objects.all().count() == 19


@requires_features(supports_update_limit_order_by=True)
@pytest.mark.asyncio
async def test_delete_limit(db, intfields_data):
    await IntFields.objects.all().limit(1).delete()
    assert await IntFields.objects.all().count() == 29


@requires_features(supports_update_limit_order_by=True)
@pytest.mark.asyncio
async def test_delete_limit_order_by(db, intfields_data):
    await IntFields.objects.all().order_by("-id").limit(1).delete()
    assert await IntFields.objects.all().count() == 29
    with pytest.raises(DoesNotExist):
        await IntFields.objects.get(intnum=97)


@pytest.mark.asyncio
async def test_delete_filter_with_foreign_key(db):
    author = await Author.objects.create(name="test")
    await Book.objects.create(name="book1", author=author, rating=5.0)
    await Book.objects.create(name="book2", author=author, rating=4.0)

    author2 = await Author.objects.create(name="test2")
    await Book.objects.create(name="book3", author=author2, rating=5.0)

    # This is the failing query
    await Book.objects.filter(author__name="test").delete()

    assert await Book.objects.all().count() == 1


@pytest.mark.asyncio
async def test_delete_row_count_excludes_cascade_deleted_children(db):
    author = await Author.objects.create(name="test")
    await Book.objects.create(name="book1", author=author, rating=5.0)
    await Book.objects.create(name="book2", author=author, rating=4.0)

    rows_affected = await Author.objects.filter(id=author.id).delete()

    assert rows_affected == 1
    assert await Book.objects.all().count() == 0


@pytest.mark.asyncio
async def test_raw_delete_row_count_excludes_cascade_deleted_children(db):
    author = await Author.objects.create(name="test")
    await Book.objects.create(name="book1", author=author, rating=5.0)
    await Book.objects.create(name="book2", author=author, rating=4.0)
    db_client = Connections.get("models")

    rows_affected, _ = await db_client.execute(f'DELETE FROM "author" WHERE "id" = {author.id}')

    assert rows_affected == 1


@pytest.mark.parametrize(
    "query",
    [
        'UPDATE "tournament" SET "desc" = \'b\'',
        '/* audit */ UPDATE "tournament" SET "desc" = \'b\'',
        '-- audit\nUPDATE "tournament" SET "desc" = \'b\'',
        'WITH x AS MATERIALIZED (SELECT "id" FROM "tournament") '
        'UPDATE "tournament" SET "desc" = \'b\' WHERE "id" IN (SELECT "id" FROM x)',
        'UPDATE "tournament" SET "desc" = \'returning\'',
    ],
)
@pytest.mark.asyncio
async def test_raw_update_row_count_with_comments_ctes_and_literals(db, query):
    for name in ("a", "b", "c"):
        await Tournament.objects.create(name=name)

    rows_affected, _ = await Connections.get("models").execute(query)

    assert rows_affected == 3


@pytest.mark.asyncio
async def test_update_filter_with_foreign_key(db):
    author = await Author.objects.create(name="test")
    await Book.objects.create(name="book1", author=author, rating=5.0)

    author2 = await Author.objects.create(name="test2")
    await Book.objects.create(name="book2", author=author2, rating=5.0)

    await Book.objects.filter(author__name="test").update(rating=1.0)

    book = await Book.objects.get(name="book1")
    assert book.rating == 1.0

    book2 = await Book.objects.get(name="book2")
    assert book2.rating == 5.0


@pytest.mark.asyncio
async def test_async_iter(db, intfields_data):
    counter = 0
    async for _ in IntFields.objects.all():
        counter += 1

    assert await IntFields.objects.all().count() == counter


@pytest.mark.asyncio
async def test_iterator_chunks_results(db, intfields_data):
    seen = [obj.id async for obj in IntFields.objects.all().order_by("id").iterator(chunk_size=7)]
    expected = [obj.id for obj in await IntFields.objects.all().order_by("id")]
    assert seen == expected
    assert len(seen) == len(intfields_data)


@pytest.mark.asyncio
async def test_iterator_exact_multiple_of_chunk_size(db, intfields_data):
    assert len(intfields_data) == 30
    seen = [obj.id async for obj in IntFields.objects.all().order_by("id").iterator(chunk_size=10)]
    assert len(seen) == 30
    assert len(set(seen)) == 30


@pytest.mark.asyncio
async def test_iterator_without_order_by_pages_by_primary_key(db, intfields_data):
    expected = sorted(obj.id for obj in await IntFields.objects.all())
    assert [obj.id async for obj in IntFields.objects.all().iterator(chunk_size=7)] == expected
    assert [obj.id async for obj in IntFields.objects.all().defer("intnum").iterator(chunk_size=7)] == expected


@pytest.mark.asyncio
async def test_iterator_survives_concurrent_delete_of_yielded_row(db, intfields_data):
    """Regression test for the OFFSET-pagination row-loss bug: deleting a row that iterator()
    already yielded used to shift every later row one position left, so the next OFFSET-based
    page skipped exactly one row that was never returned - even though order_by("id") is fully
    deterministic. iterator() now pages by keyset/cursor (seeking by the last row's own id VALUE,
    not by row COUNT) whenever ordering only by direct model fields, which is immune to this."""
    deleted_id = intfields_data[0].id
    seen_ids = []
    async for row in IntFields.objects.all().order_by("id").iterator(chunk_size=7):
        seen_ids.append(row.id)
        if len(seen_ids) == 1:
            await IntFields.objects.filter(id=deleted_id).delete()

    # The deleted row was already yielded (idx 0) before it was removed, so it correctly stays in
    # seen_ids - what this test guards against is any OTHER, not-yet-fetched row being skipped as
    # a side effect of that deletion.
    all_original_ids = {obj.id for obj in intfields_data}
    assert set(seen_ids) == all_original_ids
    assert len(seen_ids) == len(set(seen_ids))
    assert {obj.id for obj in await IntFields.objects.all()} == all_original_ids - {deleted_id}


@pytest.mark.asyncio
async def test_iterator_offset_fallback_still_loses_rows_under_concurrent_delete(db):
    """KNOWN LIMITATION, documented here on purpose: ordering by a related-model lookup (e.g.
    "author__name") isn't eligible for iterator()'s keyset-pagination fast path (after_cursor()
    doesn't support it), so iterator() falls back to plain OFFSET pagination for that ordering -
    which is NOT safe under concurrent deletes, exactly like the bug
    test_iterator_survives_concurrent_delete_of_yielded_row regression-tests for the direct-field
    case. This test pins down that the fallback path still exhibits the row loss, so a future
    change extending keyset pagination to related-field orderings has a test to turn green."""
    author = await Author.objects.create(name="a")
    books = [await Book.objects.create(name=str(i), author=author, rating=1.0) for i in range(10)]

    deleted_id = books[0].id
    seen_ids = []
    async for row in Book.objects.all().order_by("author__name", "id").iterator(chunk_size=3):
        seen_ids.append(row.id)
        if len(seen_ids) == 1:
            await Book.objects.filter(id=deleted_id).delete()

    expected_ids = {book.id for book in books} - {deleted_id}
    lost_ids = expected_ids - set(seen_ids)
    assert lost_ids, (
        "expected the documented OFFSET-fallback row-loss to reproduce here - if this now fails, "
        "iterator() must have grown keyset support for related-field orderings too; update "
        "iterator()'s docstring and this test together"
    )


@pytest.mark.asyncio
@requires_features(dialect="sqlite")
async def test_stream_raises_unsupported_on_sqlite(db, intfields_data):
    with pytest.raises(UnSupportedError, match="stream"):
        async for _ in IntFields.objects.all().stream():
            pass


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_requires_active_transaction(db_simple):
    # Deliberately db_simple, not db - the plain db fixture wraps every test body in its own
    # rollback transaction for isolation, which would make self._db already a
    # TransactionClient before the test body even runs, masking exactly the "no active
    # transaction" case this test needs to exercise.
    with pytest.raises(QueryError, match="Transactions.atomic"):
        async for _ in IntFields.objects.all().stream():
            pass


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_yields_rows_incrementally(db, intfields_data):
    async with Transactions.atomic():
        seen = [obj.id async for obj in IntFields.objects.all().order_by("id").stream(chunk_size=7)]
    expected = [obj.id for obj in await IntFields.objects.all().order_by("id")]
    assert seen == expected
    assert len(seen) == len(intfields_data)


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_runs_on_the_plan_of_its_shape(db, intfields_data):
    def below(number):
        return IntFields.objects.filter(intnum__lt=number).order_by("intnum")

    async with Transactions.atomic():
        for number, expected in ((10, []), (14, [10, 13]), (17, [10, 13, 16]), (14, [10, 13])):
            hits = StatementPlans.hits
            assert [obj.intnum async for obj in below(number).stream()] == expected
            assert [value async for value in below(number).values_list("intnum", flat=True).stream()] == expected
            union = (
                IntFields.objects.filter(intnum__lt=number)
                .values_list("intnum", flat=True)
                .union(IntFields.objects.filter(intnum=97).values_list("intnum", flat=True))
            )
            assert sorted([value async for value in union.stream()]) == [*expected, 97]
            if number != 10:
                assert StatementPlans.hits - hits == 3


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_honors_explicit_null_placement(db):
    for number, score in [(1, 1), (2, None), (3, 2), (4, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)
    async with Transactions.atomic():
        nulls_first = [
            obj.intnum
            async for obj in IntFields.objects.all()
            .order_by(F("intnum_null").desc(nulls_first=True), "intnum")
            .stream(2)
        ]
        nulls_last = [
            obj.intnum
            async for obj in IntFields.objects.all()
            .order_by(F("intnum_null").asc(nulls_last=True), "intnum")
            .stream(2)
        ]
    assert nulls_first == [2, 4, 3, 1]
    assert nulls_last == [1, 3, 2, 4]


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_combines_with_filter_and_order_by(db, intfields_data):
    async with Transactions.atomic():
        seen = [obj.intnum async for obj in IntFields.objects.filter(intnum__gt=50).order_by("-intnum").stream()]
    expected = [obj.intnum for obj in await IntFields.objects.filter(intnum__gt=50).order_by("-intnum")]
    assert seen == expected
    assert seen  # sanity: the filter actually matched something


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_combines_with_select_related(db):
    author = await Author.objects.create(name="Some Author")
    books = [await Book.objects.create(name=f"book-{i}", author=author, rating=4.5) for i in range(5)]

    async with Transactions.atomic():
        seen = [
            (book.id, book.author.name)
            async for book in Book.objects.all().select_related("author").order_by("id").stream()
        ]
    assert seen == [(book.id, author.name) for book in books]


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_combines_with_only(db, intfields_data):
    async with Transactions.atomic():
        seen = [obj.id async for obj in IntFields.objects.all().only("id").order_by("id").stream()]
    expected = [obj.id for obj in intfields_data]
    assert sorted(seen) == sorted(expected)


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_stops_early_without_consuming_remaining_rows(db, intfields_data):
    """Confirms stream() can be broken out of early (the point of real server-side streaming -
    unlike .all(), which always loads the whole result into memory up front) - only the first few
    rows are pulled off the cursor/portal before the caller stops iterating."""
    seen = []
    async with Transactions.atomic():
        async for obj in IntFields.objects.all().order_by("id").stream():
            seen.append(obj.id)
            if len(seen) == 5:
                break
    assert len(seen) == 5
    expected_first_five = [obj.id for obj in await IntFields.objects.all().order_by("id").limit(5)]
    assert seen == expected_first_five


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_raises_configuration_error_for_prefetch_related(db):
    author = await Author.objects.create(name="Some Author")
    await Book.objects.create(name="a book", author=author, rating=4.5)
    async with Transactions.atomic():
        with pytest.raises(QueryError, match="prefetch_related"):
            async for _ in Author.objects.all().prefetch_related("books").stream():
                pass


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_stream_survives_concurrent_delete_of_yielded_row(db, intfields_data):
    """The whole point of a real server-side cursor/portal over iterator()'s own OFFSET/keyset
    pagination: one snapshot for the WHOLE scan, taken when the cursor/portal opens - a row
    deleted through the SAME connection/transaction partway through the scan must not cause the
    still-open stream to skip or lose any row it hadn't yielded yet, since that row was already
    part of the snapshot the cursor took before the DELETE ever ran."""
    deleted_id = intfields_data[0].id
    seen_ids = []
    async with Transactions.atomic():
        async for row in IntFields.objects.all().order_by("id").stream(chunk_size=7):
            seen_ids.append(row.id)
            if len(seen_ids) == 1:
                await IntFields.objects.filter(id=deleted_id).delete()

    all_original_ids = {obj.id for obj in intfields_data}
    assert set(seen_ids) == all_original_ids
    assert len(seen_ids) == len(set(seen_ids))
    assert {obj.id for obj in await IntFields.objects.all()} == all_original_ids - {deleted_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [0, -5])
async def test_iterator_rejects_non_positive_chunk_size_on_cursor_path(db, intfields_data, chunk_size):
    """Ordering by a direct field routes through _iterator_by_cursor() - chunk_size <= 0 must be
    rejected up front rather than reaching the broken `len(rows) < chunk_size` termination check
    there, which never fires for a non-positive chunk_size (an `IndexError` on `rows[-1]` for 0,
    an infinite loop for a negative value on backends that treat a negative LIMIT as unbounded)."""

    async def drain() -> None:
        async for _ in IntFields.objects.all().order_by("id").iterator(chunk_size=chunk_size):
            pass

    with pytest.raises(QueryError, match="chunk_size"):
        await asyncio.wait_for(drain(), timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [0, -5])
async def test_iterator_rejects_non_positive_chunk_size_on_offset_path(db, intfields_data, chunk_size):
    """Ordering by an annotation isn't eligible for keyset pagination, so it routes through
    _iterator_by_offset() instead - chunk_size <= 0 must be rejected there too, since that path's
    own `len(rows) < chunk_size` check is equally broken (an infinite loop: a zero LIMIT always
    returns zero rows without ever growing the offset, and a negative LIMIT is unbounded on
    sqlite, repeating the same rows forever)."""

    async def drain() -> None:
        async for _ in (
            IntFields.objects.all().annotate(idp=F("id") + 1).order_by("-idp").iterator(chunk_size=chunk_size)
        ):
            pass

    with pytest.raises(QueryError, match="chunk_size"):
        await asyncio.wait_for(drain(), timeout=5)


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
@pytest.mark.parametrize("chunk_size", [0, -5])
async def test_stream_rejects_non_positive_chunk_size(db, intfields_data, chunk_size):
    """stream() must reject chunk_size <= 0 with the same QueryError as iterator() - left
    unguarded, a non-positive chunk_size reaches the driver instead, which on asyncpg raises a
    client-side `asyncpg.InterfaceError` that gets misclassified as `DBConnectionError` (making a
    connection that's actually fine look like a dead one), and on rust_pg is silently ignored."""

    async def drain() -> None:
        async with Transactions.atomic():
            async for _ in IntFields.objects.all().order_by("id").stream(chunk_size=chunk_size):
                pass

    with pytest.raises(QueryError, match="chunk_size"):
        await asyncio.wait_for(drain(), timeout=5)


@pytest.mark.asyncio
async def test_update_basic(db):
    obj0 = await IntFields.objects.create(intnum=2147483647)
    await IntFields.objects.filter(id=obj0.id).update(intnum=2147483646)
    obj = await IntFields.objects.get(id=obj0.id)
    assert obj.intnum == 2147483646
    assert obj.intnum_null is None


@pytest.mark.asyncio
async def test_update_f_expression(db):
    obj0 = await IntFields.objects.create(intnum=2147483647)
    await IntFields.objects.filter(id=obj0.id).update(intnum=F("intnum") - 1)
    obj = await IntFields.objects.get(id=obj0.id)
    assert obj.intnum == 2147483646


@pytest.mark.asyncio
async def test_update_badparam(db):
    obj0 = await IntFields.objects.create(intnum=2147483647)
    with pytest.raises(FieldError, match="Unknown keyword argument"):
        await IntFields.objects.filter(id=obj0.id).update(badparam=1)


@pytest.mark.asyncio
async def test_update_pk(db):
    obj0 = await IntFields.objects.create(intnum=2147483647)
    with pytest.raises(QueryError, match="is PK and can not be updated"):
        await IntFields.objects.filter(id=obj0.id).update(id=1)


@pytest.mark.asyncio
async def test_update_virtual(db):
    tour = await Tournament.objects.create(name="moo")
    obj0 = await MinRelation.objects.create(tournament=tour)
    with pytest.raises(FieldError, match="is virtual and can not be updated"):
        await MinRelation.objects.filter(id=obj0.id).update(participants=[])


@pytest.mark.asyncio
async def test_bad_ordering(db, intfields_data):
    with pytest.raises(FieldError, match="Unknown field moo1fip for ordering: IntFields has no field 'moo1fip'"):
        await IntFields.objects.all().order_by("moo1fip")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "expected_exception", "match"),
    [
        (lambda qs: qs.values("intnum", "intnum"), FieldError, "Duplicate key intnum"),
        (lambda qs: qs.values_list("intnum", "intnum"), None, None),
        (lambda qs: qs.values("intnum", intnum="intnum_null"), FieldError, "Duplicate key intnum"),
        (lambda qs: qs.values(intnum="intnum2"), FieldError, 'Unknown field "intnum2" for model "IntFields"'),
        (
            lambda qs: qs.values("int2num"),
            FieldError,
            r"IntFields.objects.values\('int2num'\): IntFields has no field 'int2num'",
        ),
        (
            lambda qs: qs.values_list("int2num"),
            FieldError,
            r"IntFields.objects.values_list\('int2num'\): IntFields has no field 'int2num'",
        ),
    ],
    ids=[
        "duplicate_values",
        "duplicate_values_list",
        "duplicate_values_kw",
        "duplicate_values_kw_badmap",
        "bad_values",
        "bad_values_list",
    ],
)
async def test_values_field_name_validation(db, intfields_data, call, expected_exception, match):
    context = pytest.raises(expected_exception, match=match) if expected_exception else contextlib.nullcontext()
    with context:
        await call(IntFields.objects.all())


@pytest.mark.asyncio
async def test_many_flat_values_list(db, intfields_data):
    with pytest.raises(QueryError, match=r"\.values_list\(flat=True\) selects exactly one field"):
        await IntFields.objects.all().values_list("intnum", "intnum_null", flat=True)


@pytest.mark.asyncio
async def test_all_flat_values_list(db, intfields_data):
    with pytest.raises(QueryError, match=r"\.values_list\(flat=True\) selects exactly one field"):
        await IntFields.objects.all().values_list(flat=True)


@pytest.mark.asyncio
async def test_all_values_list(db, intfields_data):
    data = await IntFields.objects.all().order_by("id").values_list()
    assert data[2] == (intfields_data[2].id, 16, None)


@pytest.mark.asyncio
async def test_all_values(db, intfields_data):
    data = await IntFields.objects.all().order_by("id").values()
    assert data[2] == {"id": intfields_data[2].id, "intnum": 16, "intnum_null": None}


@pytest.mark.asyncio
async def test_order_by_bad_value(db, intfields_data):
    with pytest.raises(FieldError, match="Unknown field badid for ordering: IntFields has no field 'badid'"):
        await IntFields.objects.all().order_by("badid").values_list()


@pytest.mark.asyncio
async def test_annotate_order_expression(db, intfields_data):
    data = await IntFields.objects.annotate(idp=F("id") + 1).order_by("-idp").first().values_list("id", "idp")
    assert data[0] + 1 == data[1]


@pytest.mark.asyncio
async def test_annotate_order_rawsql(db, intfields_data):
    qs = IntFields.objects.annotate(idp=RawSQL("id+1")).order_by("-idp")
    data = await qs.first().values_list("id", "idp")
    assert data[0] + 1 == data[1]


@pytest.mark.asyncio
async def test_annotate_expression_filter(db, intfields_data):
    count = await IntFields.objects.annotate(intnum1=F("intnum") + 1).filter(intnum1__gt=30).count()
    assert count == 23


@pytest.mark.asyncio
async def test_get_raw_sql(db, intfields_data):
    sql = IntFields.objects.all().sql()
    assert "SELECT" in sql and "FROM" in sql


@pytest.mark.asyncio
async def test_double_make_query_keeps_join_on_count_exists_update_delete(db):
    """Calling .sql() (e.g. for logging) and then awaiting the same query object runs
    _make_query() a second time - a join required by a filter on a related field must still be
    present the second time, not silently dropped because it was already in _joined_tables_set
    from the first build."""
    tournament = await Tournament.objects.create(name="t2")
    await Event.objects.create(name="e1", tournament=tournament)

    count_query = Event.objects.filter(tournament__name="t2").count()
    count_query.sql()
    assert await count_query == 1

    exists_query = Event.objects.filter(tournament__name="t2").exists()
    exists_query.sql()
    assert await exists_query is True

    update_query = Event.objects.filter(tournament__name="t2").update(name="updated")
    update_query.sql()
    assert await update_query == 1
    assert (await Event.objects.get(name="updated")).tournament_id == tournament.pk

    delete_query = Event.objects.filter(tournament__name="t2").delete()
    delete_query.sql()
    assert await delete_query == 1


@pytest.mark.asyncio
async def test_select_for_update_nowait_and_skip_locked_are_mutually_exclusive(db):
    with pytest.raises(QueryError, match="mutually exclusive"):
        IntFields.objects.filter(pk=1).select_for_update(nowait=True, skip_locked=True)


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_of_and_no_key_combine_with_skip_locked(db):
    sql = (
        IntFields.objects.filter(pk=1)
        .only("id")
        .select_for_update(skip_locked=True, of=("intfields",), no_key=True)
        .sql()
    )
    if Connections.get("models").dialect.name == "postgresql":
        assert sql == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR NO KEY UPDATE OF "intfields" SKIP LOCKED'


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update(db, intfields_data):
    sql1 = IntFields.objects.filter(pk=1).only("id").select_for_update().sql()
    sql2 = IntFields.objects.filter(pk=1).only("id").select_for_update(nowait=True).sql()
    sql3 = IntFields.objects.filter(pk=1).only("id").select_for_update(skip_locked=True).sql()
    sql4 = IntFields.objects.filter(pk=1).only("id").select_for_update(of=("intfields",)).sql()
    sql5 = IntFields.objects.filter(pk=1).only("id").select_for_update(no_key=True).sql()

    db_conn = Connections.get("models")
    dialect = db_conn.dialect.name
    if dialect == "postgresql":
        assert sql1 == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR UPDATE'
        assert sql2 == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR UPDATE NOWAIT'
        assert sql3 == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR UPDATE SKIP LOCKED'
        assert sql4 == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR UPDATE OF "intfields"'
        assert sql5 == 'SELECT "id" "id" FROM "intfields" WHERE "id"=$1 FOR NO KEY UPDATE'


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_still_works_under_ambient_test_transaction(db, intfields_data):
    """The `db` fixture itself wraps the whole test in an active transaction (see conftest.py) -
    select_for_update() must keep working exactly as before there: a real (here unobserved) lock
    on a backend that supports it. No ConfigurationError. requires_features restricts this to a
    backend that actually supports FOR UPDATE (e.g. Postgres) - see
    test_select_for_update_raises_on_unsupported_backend below for the sqlite side."""
    row = intfields_data[0]
    result = await IntFields.objects.filter(pk=row.pk).select_for_update().get()
    assert result.pk == row.pk


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_with_select_related_defaults_to_locking_the_base_table(db):
    """select_related() always builds its JOIN as LEFT OUTER regardless of the FK's own
    nullability (Book.author is declared non-null here) - Postgres rejects a bare FOR UPDATE (no
    OF) against ANY query containing an outer join at all ("FOR UPDATE cannot be applied to the
    nullable side of an outer join"), contradicting select_for_update()'s own documented "by
    default, all fetched rows are locked" behavior with a hard runtime crash instead. Defaulting
    `of` to the base table (same choice Django's own select_for_update() makes here) keeps that
    default working for the base row at least, rather than raising."""
    author = await Author.objects.create(name="Some Author")
    await Book.objects.create(name="Some Book", author=author, rating=5.0)

    sql = Book.objects.all().select_related("author").select_for_update().sql()
    assert "FOR UPDATE OF" in sql

    result = await Book.objects.all().select_related("author").select_for_update().first()
    assert result is not None
    assert result.author.id == author.id


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_with_relation_crossing_order_by_defaults_to_locking_the_base_table(db):
    """A relation-crossing .order_by()/.annotate()/nested Q() filter builds the exact same LEFT
    OUTER JOIN select_related() does (get_filters() populates self._joined_tables_set before this
    method's own select_related() branch runs) - the existing fix above only checked
    self._select_related, missing this case entirely. Confirmed live before this fix: this exact
    query raised FeatureNotSupportedError ("FOR UPDATE cannot be applied to the nullable side of
    an outer join") on real Postgres - unlike an equivalent .filter(author__name=...), which
    happens to dodge the crash via Postgres's own outer-join-elimination (a WHERE clause on the
    joined table lets the planner prove the LEFT JOIN can never actually produce a NULL row),
    .order_by() has no such WHERE clause to trigger that optimization."""
    author = await Author.objects.create(name="Some Author")
    await Book.objects.create(name="Some Book", author=author, rating=5.0)

    sql = Book.objects.filter(rating__gte=1).order_by("author__name").select_for_update().sql()
    assert "FOR UPDATE OF" in sql

    result = await Book.objects.filter(rating__gte=1).order_by("author__name").select_for_update().first()
    assert result is not None


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_of_maps_relation_names_to_join_aliases(db):
    """of=("author",) used to be rendered as a bare table name the query never joined under, and
    the relation's LEFT OUTER JOIN made Postgres refuse the lock outright."""
    author = await Author.objects.create(name="Some Author")
    book = await Book.objects.create(name="Some Book", author=author, rating=5.0)

    queryset = Book.objects.all().select_related("author").select_for_update(of=("self", "author"))
    sql = queryset.sql()
    assert 'FOR UPDATE OF "book", "book__author"' in sql
    assert "LEFT OUTER JOIN" not in sql
    result = await queryset
    assert [(item.pk, item.author.pk) for item in result] == [(book.pk, author.pk)]

    filtered = await Book.objects.filter(author__name="Some Author").select_for_update(of=("author",))
    assert [item.pk for item in filtered] == [book.pk]

    values = await Book.objects.all().select_for_update(of=("book", "author")).values("name", "author__name")
    assert values == [{"name": "Some Book", "author__name": "Some Author"}]


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_of_nested_relation_path(db):
    tournament = await Tournament.objects.create(name="Cup")
    event = await Event.objects.create(name="Final", tournament=tournament)
    await Address.objects.create(city="City", street="Street", event=event)

    queryset = Address.objects.all().select_related("event__tournament").select_for_update(of=("event__tournament",))
    assert 'FOR UPDATE OF "address__event__tournament"' in queryset.sql()
    result = await queryset
    assert result[0].event.tournament.pk == tournament.pk


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_of_keeps_plain_select_related_left_outer(db):
    sql = Book.objects.all().select_related("author").select_for_update().sql()
    assert "LEFT OUTER JOIN" in sql
    assert 'FOR UPDATE OF "book"' in sql


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("build_queryset", "message"),
    [
        (
            lambda: Book.objects.all().select_related("author").select_for_update(of=("book__author",)),
            "not a forward relation",
        ),
        (
            lambda: Book.objects.all().select_related("author").select_for_update(of=("missing",)),
            "not a forward relation",
        ),
        (lambda: Book.objects.all().select_for_update(of=("author",)), "doesn't join"),
        (lambda: Event.objects.all().select_related("reporter").select_for_update(of=("reporter",)), "nullable"),
        (
            lambda: BookNoConstraint.objects.all().select_related("author").select_for_update(of=("author",)),
            "constraint",
        ),
    ],
)
async def test_select_for_update_of_rejects_names_it_cannot_lock(db, build_queryset, message):
    with pytest.raises(QueryError, match=message):
        build_queryset().sql()


@requires_features(supports_select_for_update=False)
@pytest.mark.asyncio
async def test_select_for_update_raises_on_unsupported_backend(db, intfields_data):
    """select_for_update() on a backend with no FOR UPDATE equivalent at all (sqlite) raises
    ConfigurationError when the query runs - not silently no-op and let a caller believe rows got
    locked when nothing was ever locked."""
    row = intfields_data[0]
    queryset = IntFields.objects.filter(pk=row.pk).select_for_update()
    with pytest.raises(UnSupportedError, match="select_for_update"):
        await queryset


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_outside_transaction_raises(db_truncate):
    """A SELECT ... FOR UPDATE lock is only meaningful for the lifetime of the enclosing
    transaction - used outside Transactions.atomic(), the lock would be acquired and
    immediately released (autocommit), giving the caller no real protection while looking like
    it does. Only meaningfully asserts anything against a real Postgres connection -
    requires_features skips this on sqlite, where select_for_update() already raises
    ConfigurationError for an unrelated reason (see
    test_select_for_update_raises_on_unsupported_backend above). Needs db_truncate, not db - the
    `db` fixture's own ambient transaction would make every query here look like it's already
    inside one."""
    row = await IntFields.objects.create(intnum=1)
    with pytest.raises(QueryError, match="select_for_update"):
        await IntFields.objects.filter(pk=row.pk).select_for_update().get()


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_inside_transaction_still_works(db_truncate):
    """No regression for the documented, correct usage: select_for_update() awaited inside an
    active Transactions.atomic() block must keep working exactly as before."""
    row = await IntFields.objects.create(intnum=1)
    async with Transactions.atomic() as conn:
        locked = await IntFields.objects.filter(pk=row.pk).select_for_update().using(conn).get()
        assert locked.pk == row.pk


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_blocks_concurrent_transaction(db_truncate):
    """The real locking test: a second, genuinely separate transaction's select_for_update() on
    the SAME row must block until the first transaction commits - the exact protection sqlite
    can never demonstrate (support_for_update=False there means there is nothing to lock), which
    is why this whole ConfigurationError guard exists: a concurrency test written against sqlite
    would "pass" even with zero real locking.

    Uses db_truncate (not db) so neither task starts inside the `db` fixture's own ambient
    transaction - each Transactions.atomic() call below then genuinely acquires its OWN
    physical connection from the pool, exactly like two unrelated concurrent requests would.
    """
    row = await IntFields.objects.create(intnum=1)

    holder_locked = asyncio.Event()
    release_holder = asyncio.Event()
    events: list[str] = []

    async def holder() -> None:
        async with Transactions.atomic() as conn:
            await IntFields.objects.filter(pk=row.pk).select_for_update().using(conn).get()
            holder_locked.set()
            await release_holder.wait()
            events.append("holder_committing")
        # The row lock is released only once this transaction actually commits, here.

    async def waiter() -> None:
        await holder_locked.wait()
        async with Transactions.atomic() as conn:
            await IntFields.objects.filter(pk=row.pk).select_for_update().using(conn).get()
            events.append("waiter_acquired_lock")

    holder_task = asyncio.create_task(holder())
    await holder_locked.wait()
    waiter_task = asyncio.create_task(waiter())

    # Give the waiter every chance to actually reach the database and hit the row lock - it
    # must still be blocked (not done) here, proving a real lock is held, not a no-op.
    await asyncio.sleep(0.3)
    assert not waiter_task.done()
    assert "waiter_acquired_lock" not in events

    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, waiter_task), timeout=5)

    assert events == ["holder_committing", "waiter_acquired_lock"]


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_select_for_update_locks_prefetched_rows(db_truncate):
    """select_for_update()'s own documented "by default, all fetched rows are locked" behavior
    must also cover prefetch_related()'s own separate second query, not just the base row -
    prefetch_mixin.py's _make_prefetch_queries() builds that second query as a fresh, independent
    Model.objects.all() with no visibility into the parent's select_for_update() state at all.
    Regression: confirmed live before this fix, a concurrent transaction's UPDATE on a prefetched
    Book row succeeded immediately (no block at all) even while this transaction held
    select_for_update() with prefetch_related("books") on its author.

    Uses db_truncate (not db), same reasoning as test_select_for_update_blocks_concurrent_transaction
    above - each Transactions.atomic() call must acquire its own real connection.
    """
    author = await Author.objects.create(name="Some Author")
    book = await Book.objects.create(name="Some Book", author=author, rating=5.0)

    holder_locked = asyncio.Event()
    release_holder = asyncio.Event()
    events: list[str] = []

    async def holder() -> None:
        async with Transactions.atomic() as conn:
            authors = (
                await Author.objects.filter(pk=author.pk).select_for_update().prefetch_related("books").using(conn)
            )
            assert len(authors[0].books) == 1
            holder_locked.set()
            await release_holder.wait()
            events.append("holder_committing")

    async def waiter() -> None:
        await holder_locked.wait()
        async with Transactions.atomic() as conn:
            await Book.objects.filter(pk=book.pk).select_for_update().using(conn).get()
            events.append("waiter_acquired_lock")

    holder_task = asyncio.create_task(holder())
    await holder_locked.wait()
    waiter_task = asyncio.create_task(waiter())

    try:
        # The waiter must still be blocked here, proving the prefetched Book row was really
        # locked - not just the Author row select_for_update() was directly called on.
        await asyncio.sleep(0.3)
        assert not waiter_task.done()
        assert "waiter_acquired_lock" not in events
    finally:
        # Always released, even when the asserts above fail (no lock held) - otherwise holder_task
        # stays parked on release_holder.wait() forever, holding its transaction open and hanging
        # db_truncate's own teardown.
        release_holder.set()

    await asyncio.wait_for(asyncio.gather(holder_task, waiter_task), timeout=5)
    assert events == ["holder_committing", "waiter_acquired_lock"]


@pytest.mark.asyncio
async def test_select_related(db):
    tournament = await Tournament.objects.create(name="1")
    reporter = await Reporter.objects.create(name="Reporter")
    event = await Event.objects.create(name="1", tournament=tournament, reporter=reporter)
    event = await Event.objects.all().select_related("tournament", "reporter").get(pk=event.pk)
    assert event.tournament.pk == tournament.pk
    assert event.reporter.pk == reporter.pk


@pytest.mark.asyncio
async def test_select_related_with_two_same_models(db):
    parent_node = await Node.objects.create(name="1")
    child_node = await Node.objects.create(name="2")
    tree = await Tree.objects.create(parent=parent_node, child=child_node)
    tree = await Tree.objects.all().select_related("parent", "child").get(pk=tree.pk)
    assert tree.parent.pk == parent_node.pk
    assert tree.parent.name == parent_node.name
    assert tree.child.pk == child_node.pk
    assert tree.child.name == child_node.name


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgres_search(db):
    name = "hello world"
    await Tournament.objects.create(name=name)
    ret = await Tournament.objects.filter(name__search="hello").first()
    assert ret.name == name


@pytest.mark.asyncio
async def test_subquery_select(db):
    t1 = await Tournament.objects.create(name="1")
    ret = (
        await Tournament.objects.filter(pk=t1.pk)
        .annotate(ids=Subquery(Tournament.objects.filter(pk=t1.pk).values("id")))
        .values("ids", "id")
    )
    assert ret == [{"id": t1.pk, "ids": t1.pk}]


@pytest.mark.asyncio
async def test_subquery_filter(db):
    t1 = await Tournament.objects.create(name="1")
    ret = await Tournament.objects.filter(pk=Subquery(Tournament.objects.filter(pk=t1.pk).values("id"))).first()
    assert ret == t1


@pytest.mark.asyncio
async def test_raw_sql_count(db):
    t1 = await Tournament.objects.create(name="1")
    ret = await Tournament.objects.filter(pk=t1.pk).annotate(count=RawSQL("count(*)")).values("count")
    assert ret == [{"count": 1}]


@pytest.mark.asyncio
async def test_raw_sql_select(db):
    t1 = await Tournament.objects.create(id=1, name="1")
    ret = await Tournament.objects.filter(pk=t1.pk).annotate(idp=RawSQL("id + 1")).filter(idp=2).values("idp")
    assert ret == [{"idp": 2}]


@pytest.mark.asyncio
async def test_raw_sql_filter(db):
    ret = await Tournament.objects.filter(pk=RawSQL("id + 1"))
    assert ret == []


@pytest.mark.asyncio
async def test_raw_sql_filter_after_a_plain_scalar_filter_of_the_same_shape(db):
    """A prior filter(pk=<int>) call on this exact shape populates QUERY_SHAPE_CACHE with a
    template whose pk leaf is a rebindable ScalarValueRef - _leaf_value_shape() doesn't
    distinguish a RawSQL value from a plain scalar, so a later filter(pk=RawSQL(...)) used to hit
    that SAME cache entry and try to rebind the RawSQL object through field.to_db_value(),
    raising a bogus ValidationError instead of building fresh (RawSQL renders directly into the
    SQL text, it was never meant to reach the substitution path at all)."""
    t1 = await Tournament.objects.create(id=1, name="1")
    assert await Tournament.objects.filter(pk=1).first() == t1
    ret = await Tournament.objects.filter(pk=RawSQL("id + 1"))
    assert ret == []


@pytest.mark.asyncio
async def test_raw_sql_with_bind_params(db):
    t1 = await Tournament.objects.create(id=1, name="1")
    ret = await Tournament.objects.filter(pk=t1.pk).annotate(idp=RawSQL("id + %s", [1])).filter(idp=2).values("idp")
    assert ret == [{"idp": 2}]

    ret = await Tournament.objects.filter(pk=t1.pk).annotate(idp=RawSQL("id * %s + %s", [2, 1])).values("idp")
    assert ret == [{"idp": t1.pk * 2 + 1}]


@pytest.mark.asyncio
async def test_raw_sql_bind_params_never_string_interpolated(db):
    # A value that would break out of the SQL string if naively interpolated (a quote) must be
    # safely bound as a real parameter instead - if it leaked into the SQL text unescaped, this
    # would either error out or (worse) silently widen the match, not just return zero rows.
    await Tournament.objects.create(name="alpha")
    ret = await Tournament.objects.filter(name=RawSQL("%s", ["alpha' OR '1'='1"]))
    assert ret == []


@pytest.mark.asyncio
@pytest.mark.parametrize("param", [{"a": 1}, {1, 2}, frozenset({1})], ids=["dict", "set", "frozenset"])
async def test_raw_sql_refuses_a_dict_or_set_param(db, param):
    with pytest.raises(ValueError, match="pass a list for an array parameter"):
        RawSQL("%s", [param])


@requires_features(binds_array_parameters=True)
@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[1, 3], (1, 3)], ids=["list", "tuple"])
async def test_raw_sql_list_or_tuple_param_binds_as_an_array(db, ids):
    for tournament_id in (1, 2, 3):
        await Tournament.objects.create(id=tournament_id, name=str(tournament_id))
    matched = await Tournament.objects.filter(id__in=RawSQL("SELECT unnest(%s::int[])", [ids])).order_by("id")
    assert [tournament.id for tournament in matched] == [1, 3]


@requires_features(binds_array_parameters=False)
@pytest.mark.asyncio
@pytest.mark.parametrize("ids", [[1, 3], (1, 3)], ids=["list", "tuple"])
async def test_raw_sql_list_param_raises_without_array_parameters(db, ids):
    await Tournament.objects.create(id=1, name="1")
    # A plan of the same shape recorded with a plain value must not bind the list either.
    assert await Tournament.objects.filter(name=RawSQL("%s", ["1"])).count() == 1
    with pytest.raises(UnSupportedError, match="list parameter"):
        await Tournament.objects.filter(name=RawSQL("%s", [ids])).count()


@pytest.mark.asyncio
async def test_raw_sql_placeholder_count_mismatch_raises(db):
    with pytest.raises(ValueError, match="placeholder"):
        RawSQL("id + %s", [1, 2])
    with pytest.raises(ValueError, match="placeholder"):
        RawSQL("id + %s + %s", [1])
    RawSQL("id + 1")  # no placeholders, no params - must not raise


@pytest.mark.asyncio
async def test_raw_sql_escaped_percent_is_not_mistaken_for_a_placeholder(db):
    # A literal '%' that happens to be followed by 's' (e.g. LIKE '%stuff%') looks exactly like
    # a '%s' placeholder unless escaped as '%%' - previously this miscounted placeholders (a
    # ValueError even when the real placeholder count and param count matched) or, worse, silently
    # spliced a bind parameter into the middle of the LIKE pattern instead of the real placeholder.
    await Tournament.objects.create(name="stuffed")
    await Tournament.objects.create(name="other")

    ret = (
        await Tournament.objects.annotate(hit=RawSQL("name LIKE '%%stuff%%' AND id = %s", [0]))
        .filter(hit=True)
        .values("name")
    )
    assert ret == []  # the %s placeholder (id = 0) is the one actually bound, not the LIKE pattern

    matched = (
        await Tournament.objects.annotate(hit=RawSQL("name LIKE '%%stuff%%'"))
        .filter(hit=True)
        .values_list("name", flat=True)
    )
    assert matched == ["stuffed"]


@pytest.mark.asyncio
async def test_raw_sql_escaped_percent_placeholder_count_not_confused_by_literal_percent_s(db):
    # 'name LIKE '%stuff%'' contains the literal substring '%s' (from "%s|tuff") that isn't a
    # placeholder - RawSQL must not count it as one once it's properly escaped as '%%'.
    RawSQL("name LIKE '%%stuff%%' AND rating = %s", [5])  # must not raise: exactly 1 real placeholder
    with pytest.raises(ValueError, match="placeholder"):
        RawSQL("name LIKE '%%stuff%%'", [5])  # 0 real placeholders, 1 param given


@pytest.mark.asyncio
async def test_annotation_named_like_a_model_field_raises(db):
    with pytest.raises(FieldError, match="conflict with field"):
        Tournament.objects.all().annotate(id=RawSQL("id + 1"))


@pytest.mark.asyncio
async def test_f_annotation_referenced_in_annotation(db):
    instance = await IntFields.objects.create(intnum=1)

    events = (
        await IntFields.objects.filter(id=instance.id)
        .annotate(intnum_plus_1=F("intnum") + 1)
        .annotate(intnum_plus_2=F("intnum_plus_1") + 1)
    )
    assert len(events) == 1
    assert events[0].intnum_plus_1 == 2
    assert events[0].intnum_plus_2 == 3

    # in a single annotate call
    events = await IntFields.objects.filter(id=instance.id).annotate(
        intnum_plus_1=F("intnum") + 1, intnum_plus_2=F("intnum_plus_1") + 1
    )
    assert len(events) == 1
    assert events[0].intnum_plus_1 == 2
    assert events[0].intnum_plus_2 == 3


@pytest.mark.asyncio
async def test_rawsql_annotation_referenced_in_annotation(db):
    instance = await IntFields.objects.create(intnum=1)

    events = (
        await IntFields.objects.filter(id=instance.id).annotate(ten=RawSQL("20 / 2")).annotate(ten_plus_1=F("ten") + 1)
    )

    assert len(events) == 1
    assert events[0].ten == 10
    assert events[0].ten_plus_1 == 11


@pytest.mark.asyncio
async def test_joins_in_arithmetic_expressions(db):
    author = await Author.objects.create(name="1")
    await Book.objects.create(name="1", author=author, rating=1)
    await Book.objects.create(name="2", author=author, rating=5)

    ret = await Author.objects.annotate(rating=Avg(F("books__rating") + 1))
    assert len(ret) == 1
    assert ret[0].rating == 4.0

    ret = await Author.objects.annotate(rating=Avg(F("books__rating") * 2 - F("books__rating")))
    assert len(ret) == 1
    assert ret[0].rating == 3.0


@pytest.mark.asyncio
async def test_annotations_in_flat_values_list(db):
    author1 = await Author.objects.create(name="1")
    author2 = await Author.objects.create(name="2")
    author3 = await Author.objects.create(name="3")
    await Book.objects.create(name="1", author=author1, rating=1)
    await Book.objects.create(name="2", author=author2, rating=3)
    await Book.objects.create(name="3", author=author3, rating=5)

    subquery = Author.objects.annotate(rating=Avg("books__rating")).filter(rating__gte=3)

    subquery_ret = await subquery.order_by("id").values_list("id", flat=True)
    assert len(subquery_ret) == 2
    assert subquery_ret[0] == author2.pk
    assert subquery_ret[1] == author3.pk

    ret = await Author.objects.filter(id__in=Subquery(subquery.values_list("id", flat=True))).order_by("id")
    assert ret[0] == author2
    assert ret[1] == author3


# Tests for exception classes (no database needed, pure Python tests)
def test_does_not_exist():
    exp_cls: type[ObjectLookupError] = DoesNotExist
    assert str(exp_cls("old format")) == "old format"
    assert str(exp_cls(Tournament)) == exp_cls.TEMPLATE.format(Tournament.__name__)


def test_multiple_objects_returned():
    exp_cls: type[ObjectLookupError] = MultipleObjectsReturned
    assert str(exp_cls("old format")) == "old format"
    assert str(exp_cls(Tournament)) == exp_cls.TEMPLATE.format(Tournament.__name__)


@pytest.mark.asyncio
async def test_union_basic(db):
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    t3 = await Tournament.objects.create(name="T3")
    await Tournament.objects.create(name="T4")

    qs1 = Tournament.objects.filter(name__in=["T1", "T2"])
    qs2 = Tournament.objects.filter(name="T3")

    result = await qs1.union(qs2)
    assert set(result) == {t1, t2, t3}


@pytest.mark.asyncio
async def test_union_on_model_with_meta_ordering_does_not_raise(db):
    """Event has Meta.ordering = ["name"], which used to get baked into EVERY branch's own
    SELECT as a default ORDER BY (the same fallback a plain, standalone queryset applies) -
    invalid SQL once those branches are glued together via UNION, since only the combined
    result's own, final ORDER BY is legal there. Meta.ordering must not be applied to an
    individual union branch at all; an explicit .order_by() on the UnionQuery itself (see
    test_union_order_by_on_union_query_overrides_model_meta_ordering below) is the only
    supported way to get an ordered union result."""
    t1 = await Tournament.objects.create(name="T1")
    e1 = await Event.objects.create(name="E1", tournament=t1)
    e2 = await Event.objects.create(name="E2", tournament=t1)
    e3 = await Event.objects.create(name="E3", tournament=t1)

    qs1 = Event.objects.filter(name__in=["E1", "E2"])
    qs2 = Event.objects.filter(name="E3")

    result = await qs1.union(qs2)
    assert set(result) == {e1, e2, e3}


@pytest.mark.asyncio
async def test_union_order_by_on_union_query_overrides_model_meta_ordering(db):
    """An explicit .order_by() on the UnionQuery result must still work correctly for a model
    with Meta.ordering - Meta.ordering itself is never applied per-branch (see
    test_union_on_model_with_meta_ordering_does_not_raise above), but the user's own explicit
    ordering, applied once to the combined result, is unaffected."""
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="C", tournament=t1)
    await Event.objects.create(name="A", tournament=t1)
    await Event.objects.create(name="B", tournament=t1)

    qs1 = Event.objects.filter(name__in=["C", "A"])
    qs2 = Event.objects.filter(name="B")

    result = await qs1.union(qs2).order_by("name")
    assert [event.name for event in result] == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_combined_rows_raise_on_unmatched_model_column(db):
    """The per-row model dispatch of a union of model querysets used to silently drop any row
    whose model columns matched none of the given models instead of raising - unreachable through
    .union() itself (its models are always the ones of the querysets that write those columns),
    but a silent data-loss path if that ever changes; invoked directly here."""
    from hare.query.rows.model_rows import ModelRows

    await Tournament.objects.create(name="T1")
    db_client = Tournament.get_connection()
    compiler = Tournament.objects.all()._get_model_rows_query()
    compiler._make_query()
    sql = compiler.query.get_sql(compiler.query.QUERY_CLS.SQL_CONTEXT)
    _, rows = await db_client.execute(f"SELECT *, 'models' AS hare_app, 'NotARealModel' AS hare_model FROM ({sql}) t")

    with pytest.raises(QueryError, match="none of the models"):
        list(
            ModelRows(Tournament, db_client).read_combined(
                rows, list(rows[0].keys()), {Tournament}, "hare_app", "hare_model"
            )
        )


@pytest.mark.asyncio
async def test_combined_rows_annotations_survive_hydration(db):
    """An annotation column of a union row is set on the instance after it is built from the
    model's own columns, never read as one of them."""
    from hare.query.rows.model_rows import ModelRows

    await Tournament.objects.create(name="T1")
    db_client = Tournament.get_connection()
    compiler = Tournament.objects.filter(name="T1")._get_model_rows_query()
    compiler._make_query()
    sql, values = compiler.query.get_parameterized_sql()
    _, rows = await db_client.execute(
        f"SELECT *, 'models' AS hare_app, 'Tournament' AS hare_model, 42 AS extra_col FROM ({sql}) t", values
    )

    instances = list(
        ModelRows(Tournament, db_client, annotations=["extra_col"]).read_combined(
            rows, list(rows[0].keys()), {Tournament}, "hare_app", "hare_model"
        )
    )
    assert len(instances) == 1
    assert instances[0].extra_col == 42
    assert instances[0].name == "T1"


@pytest.mark.asyncio
async def test_union_all(db):
    t1 = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")

    qs1 = Tournament.objects.filter(name="T1")
    qs2 = Tournament.objects.filter(name="T1")

    result = await qs1.union(qs2, all=True)
    assert list(result) == [t1, t1]


@pytest.mark.asyncio
async def test_union_mixed_models(db):
    r1 = await Reporter.objects.create(name="R1")
    r2 = await Reporter.objects.create(name="R2")
    await Reporter.objects.create(name="R3")
    t1 = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")

    qs1 = Tournament.objects.filter(name="T1").only("id", "name")
    qs2 = Reporter.objects.filter(name__in=["R1", "R2"]).only("id", "name")

    result = await qs1.union(qs2)
    assert set(result) == {t1, r1, r2}


@pytest.mark.parametrize(
    "orderings,expected_instances",
    [
        ("name", ["t2", "t1", "r1"]),
        ("-name", ["r1", "t1", "t2"]),
    ],
)
@pytest.mark.asyncio
async def test_union_order_by(db, orderings, expected_instances):
    t1 = await Tournament.objects.create(name="C")
    await Reporter.objects.create(name="A")
    t2 = await Tournament.objects.create(name="B")
    await Reporter.objects.create(name="D")
    await Tournament.objects.create(name="E")
    r1 = await Reporter.objects.create(name="F")

    qs1 = Tournament.objects.filter(id__in=[t1.id, t2.id]).only("id", "name")
    qs2 = Reporter.objects.filter(id=r1.id).only("id", "name")

    result = await qs1.union(qs2).order_by(*orderings.split(","))

    instance_map = {"t1": t1, "t2": t2, "r1": r1}
    expected = [instance_map[k] for k in expected_instances]

    assert result == expected


@pytest.mark.parametrize(
    "ordering_factory,expected",
    [
        (lambda: F("intnum_null").asc(nulls_first=True), [2, 4, 1, 3]),
        (lambda: F("intnum_null").asc(nulls_last=True), [1, 3, 2, 4]),
        (lambda: F("intnum_null").desc(nulls_first=True), [2, 4, 3, 1]),
        (lambda: F("intnum_null").desc(nulls_last=True), [3, 1, 2, 4]),
    ],
)
@pytest.mark.asyncio
async def test_union_order_by_explicit_null_placement(db, ordering_factory, expected):
    for number, score in [(1, 1), (2, None), (3, 2), (4, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)

    first_half = IntFields.objects.filter(intnum__in=[1, 2]).only("id", "intnum", "intnum_null")
    second_half = IntFields.objects.filter(intnum__in=[3, 4]).only("id", "intnum", "intnum_null")

    result = await first_half.union(second_half).order_by(ordering_factory(), "intnum")
    assert [row.intnum for row in result] == expected


@pytest.mark.asyncio
async def test_union_order_by_multiple_fields(db):
    t1 = await Tournament.objects.create(name="C")
    t2 = await Tournament.objects.create(name="B")
    r1 = await Reporter.objects.create(name="C")
    await Tournament.objects.create(name="Z")
    await Reporter.objects.create(name="Z")

    qs1 = Tournament.objects.filter(id__in=[t1.id, t2.id]).only("id", "name")
    qs2 = Reporter.objects.filter(id=r1.id).only("id", "name")

    result = await qs1.union(qs2).order_by("name", "id")

    if r1.id == t1.id:
        return

    if r1.id > t1.id:
        expected = [t2, t1, r1]
    else:
        expected = [t2, r1, t1]

    assert result == expected


@pytest.mark.asyncio
async def test_union_limit(db):
    r1 = await Reporter.objects.create(name="B")
    t1 = await Tournament.objects.create(name="A")
    await Reporter.objects.create(name="D")
    await Tournament.objects.create(name="C")

    qs1 = Tournament.objects.all().only("id", "name")
    qs2 = Reporter.objects.all().only("id", "name")

    result = await qs1.union(qs2).order_by("name").limit(2)
    assert list(result) == [t1, r1]


@pytest.mark.asyncio
async def test_union_offset(db):
    await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")
    t3 = await Tournament.objects.create(name="T3")
    t4 = await Tournament.objects.create(name="T4")

    qs1 = Tournament.objects.filter(name__in=["T1", "T2"]).only("id", "name")
    qs2 = Tournament.objects.filter(name__in=["T3", "T4"]).only("id", "name")

    result = await qs1.union(qs2).order_by("name").limit(4).offset(2)
    assert list(result) == [t3, t4]


@pytest.mark.asyncio
async def test_union_offset_negative_raises(db):
    qs1 = Tournament.objects.all().only("id", "name")
    qs2 = Tournament.objects.all().only("id", "name")

    with pytest.raises(QueryError, match="Offset should be non-negative number"):
        await qs1.union(qs2).offset(-1)


@pytest.mark.asyncio
async def test_union_order_by_pk_maps_to_the_real_pk_column(db):
    # "pk" is a .filter()/.get()/.order_by() alias resolved specially on a plain QuerySet, but
    # was never registered as a real select-list name for UnionQuery - .order_by("pk") always
    # raised "Order by field must be in the select list for union queries" instead.
    t1 = await Tournament.objects.create(name="A")
    t2 = await Tournament.objects.create(name="B")

    qs1 = Tournament.objects.filter(id=t2.id).only("id", "name")
    qs2 = Tournament.objects.filter(id=t1.id).only("id", "name")

    result = await qs1.union(qs2).order_by("pk")
    assert list(result) == [t1, t2]


@pytest.mark.asyncio
async def test_union_chained(db):
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    await Tournament.objects.create(name="T3")
    r1 = await Reporter.objects.create(name="R1")
    await Reporter.objects.create(name="R2")

    qs1 = Tournament.objects.filter(name="T1").only("id", "name")
    qs2 = Tournament.objects.filter(name="T2").only("id", "name")
    qs3 = Reporter.objects.filter(name="R1").only("id", "name")

    result = await qs1.union(qs2).union(qs3)
    assert set(result) == {t1, t2, r1}


@pytest.mark.asyncio
async def test_union_count(db):
    await Tournament.objects.create(name="T1")
    await Reporter.objects.create(name="R1")
    await Tournament.objects.create(name="T2")
    await Reporter.objects.create(name="R2")

    qs1 = Tournament.objects.filter(name="T1").only("id")
    qs2 = Reporter.objects.filter(name="R1").only("id")

    assert await qs1.union(qs2).count() == 2


@pytest.mark.asyncio
async def test_union_different_select_fields_raises(db):
    await Tournament.objects.create(name="T1")

    qs1 = Tournament.objects.filter(name="T1").only("name")
    qs2 = Tournament.objects.filter(name="T1").only("desc")

    with pytest.raises(QueryError, match="Union queries must have the same select fields"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_union_different_fields__in_different_models_raises(db):
    await Tournament.objects.create(name="T1")
    await Reporter.objects.create(name="R1")

    qs1 = Tournament.objects.all()
    qs2 = Reporter.objects.all()

    with pytest.raises(QueryError, match="Union queries must have the same select fields"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_union_order_by_field_not_in_select_raises(db):
    await Tournament.objects.create(name="T1")

    qs1 = Tournament.objects.filter(name="T1").only("id", "name")
    qs2 = Tournament.objects.filter(name="T1").only("id", "name")

    qs = qs1.union(qs2)
    with pytest.raises(QueryError, match="Order by field must be in the select list"):
        await qs.order_by("desc")


@pytest.mark.asyncio
async def test_union_with_annotate_survives_hydration(db):
    """.annotate() on union branches used to be flatly rejected (QueryError) - the discriminator
    columns UnionQuery already injects via the same .annotate() mechanism proved the plumbing was
    there, it was just gated off for user-facing annotations. Each branch's own annotation value
    must actually survive onto the hydrated instance, not silently vanish (see execute_union()'s
    custom_fields handling in hare/backends/base/executor/select_mixin.py)."""
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    await Event.objects.create(name="E1", tournament=t1)
    await Event.objects.create(name="E2", tournament=t1)
    await Event.objects.create(name="E3", tournament=t2)

    qs1 = Tournament.objects.filter(id=t1.id).annotate(event_count=Count("events")).only("id", "name")
    qs2 = Tournament.objects.filter(id=t2.id).annotate(event_count=Count("events")).only("id", "name")

    result = await qs1.union(qs2).order_by("name")

    by_name = {row.name: row.event_count for row in result}
    assert by_name == {"T1": 2, "T2": 1}


@pytest.mark.asyncio
async def test_union_with_annotate_mixed_models_non_aggregate_value(db):
    """Regression guard: a non-Field/non-Function annotation term (a bare literal Value(...),
    resolving to a ValueWrapper) has no .name at all - UnionQuery._get_selects() used to read
    `select.name` unconditionally when comparing branches' select lists, crashing with
    AttributeError for exactly this shape. It must read the term's alias (its .annotate() key)
    instead, the same way every other annotation term is identified."""
    await Tournament.objects.create(name="T1")
    await Reporter.objects.create(name="R1")

    qs1 = Tournament.objects.filter(name="T1").annotate(annotated_value=Value(1)).only("id", "name", "annotated_value")
    qs2 = Reporter.objects.filter(name="R1").annotate(annotated_value=Value(1)).only("id", "name", "annotated_value")

    result = await qs1.union(qs2)
    assert {row.annotated_value for row in result} == {1}


@pytest.mark.asyncio
async def test_union_with_annotate_colliding_field_name_raises(db):
    """An annotation sharing a name with a real field is rejected before a union branch is built."""
    with pytest.raises(FieldError, match="conflict with field"):
        Tournament.objects.filter(name="T1").annotate(name=Value("clobbered"))


@pytest.mark.asyncio
async def test_union_with_alias_excluded_from_select_and_no_collision(db):
    """An .alias() key is never added to a branch's own SELECT, so it doesn't show up on the
    hydrated result."""
    t1 = await Tournament.objects.create(name="T1")
    r1 = await Reporter.objects.create(name="R1")

    qs1 = Tournament.objects.filter(id=t1.id).alias(hidden=Value("clobbered")).only("id", "name")
    qs2 = Reporter.objects.filter(id=r1.id).alias(hidden=Value("clobbered")).only("id", "name")

    result = await qs1.union(qs2)
    assert {row.name for row in result} == {"T1", "R1"}
    assert not any(hasattr(row, "hidden") for row in result)


@pytest.mark.asyncio
async def test_union_with_branch_order_by_and_limit_combines_the_sliced_rows(db):
    """A branch's own order_by()/limit()/offset() used to get baked straight into that branch's
    own SELECT, landing before the UNION keyword instead of after it - invalid SQL on both
    dialects, since ORDER BY/LIMIT are only legal on the final, parenthesized branch of a set
    operation. The branch is combined as a derived table of its own sliced rows instead."""
    await Tournament.objects.create(name="T1")
    await Reporter.objects.create(name="R1")

    await Tournament.objects.create(name="T2")

    qs1 = Tournament.objects.all().only("id", "name").order_by("-name").limit(1)
    qs2 = Reporter.objects.all().only("id", "name")

    rows = await qs1.union(qs2)
    assert sorted((type(row).__name__, row.name) for row in rows) == [("Reporter", "R1"), ("Tournament", "T2")]


@pytest.mark.asyncio
async def test_union_with_branch_offset_combines_the_sliced_rows(db):
    await Tournament.objects.create(name="T1")
    await Reporter.objects.create(name="R1")

    await Tournament.objects.create(name="T2")

    qs1 = Tournament.objects.all().only("id", "name").order_by("name").offset(1)
    qs2 = Reporter.objects.all().only("id", "name")

    rows = await qs1.union(qs2)
    assert sorted((type(row).__name__, row.name) for row in rows) == [("Reporter", "R1"), ("Tournament", "T2")]


@pytest.mark.asyncio
async def test_union_with_branch_select_related_raises(db):
    """A branch's own select_related() used to silently vanish once its rows were merged into
    the union result - execute_union() hydrates each instance off the union's own flat column
    list, with no joined columns to populate the relation from, so the FK attribute stayed an
    unfetched QuerySet stand-in instead of the real related object, with no error anywhere near
    the actual cause."""
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().select_related("tournament")
    qs2 = Event.objects.all().select_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_union_with_branch_prefetch_related_raises(db):
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().prefetch_related("tournament")
    qs2 = Event.objects.all().prefetch_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_union_with_branch_lazy_joined_relation_default_raises(db):
    """A relation's field-level lazy="joined" default (see fields.ForeignKeyField) only actually
    populates _select_related once _make_query() runs - AFTER the guard above already checked it
    for an explicit .select_related() call - so a branch that never called .select_related() at
    all, but whose model has a lazy="joined" FK, used to slip straight past that guard and hit
    the exact same silently-dropped-relation bug test_union_with_branch_select_related_raises
    covers for the explicit case, with no error anywhere near the actual cause."""
    parent = await LazyJoinedParent.objects.create(name="P1")
    await LazyJoinedChild.objects.create(name="C1", parent=parent)

    qs1 = LazyJoinedChild.objects.all()
    qs2 = LazyJoinedChild.objects.all()

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_union_with_branch_lazy_select_relation_default_raises(db):
    """Same regression as test_union_with_branch_lazy_joined_relation_default_raises, covering
    lazy="select" (auto prefetch_related()) instead of lazy="joined" (auto select_related())."""
    parent = await LazySelectParent.objects.create(name="P1")
    await LazySelectChild.objects.create(name="C1", parent=parent)

    qs1 = LazySelectChild.objects.all()
    qs2 = LazySelectChild.objects.all()

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.union(qs2)


@pytest.mark.asyncio
async def test_intersection_basic(db):
    await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    t3 = await Tournament.objects.create(name="T3")

    qs1 = Tournament.objects.filter(name__in=["T1", "T2", "T3"])
    qs2 = Tournament.objects.filter(name__in=["T2", "T3"])

    result = await qs1.intersection(qs2)
    assert set(result) == {t2, t3}


@pytest.mark.asyncio
async def test_difference_basic(db):
    t1 = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")
    await Tournament.objects.create(name="T3")

    qs1 = Tournament.objects.filter(name__in=["T1", "T2", "T3"])
    qs2 = Tournament.objects.filter(name__in=["T2", "T3"])

    result = await qs1.difference(qs2)
    assert set(result) == {t1}


@pytest.mark.asyncio
async def test_intersection_chained(db):
    t1 = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")
    await Tournament.objects.create(name="T3")

    qs1 = Tournament.objects.filter(name__in=["T1", "T2"])
    qs2 = Tournament.objects.filter(name__in=["T1", "T3"])
    qs3 = Tournament.objects.filter(name__in=["T1"])

    result = await qs1.intersection(qs2).intersection(qs3)
    assert set(result) == {t1}


@pytest.mark.asyncio
async def test_intersection_different_select_fields_raises(db):
    await Tournament.objects.create(name="T1")

    qs1 = Tournament.objects.filter(name="T1").only("name")
    qs2 = Tournament.objects.filter(name="T1").only("desc")

    with pytest.raises(QueryError, match="Union queries must have the same select fields"):
        await qs1.intersection(qs2)


@pytest.mark.asyncio
async def test_difference_different_select_fields_raises(db):
    await Tournament.objects.create(name="T1")

    qs1 = Tournament.objects.filter(name="T1").only("name")
    qs2 = Tournament.objects.filter(name="T1").only("desc")

    with pytest.raises(QueryError, match="Union queries must have the same select fields"):
        await qs1.difference(qs2)


@pytest.mark.asyncio
async def test_intersection_with_branch_select_related_raises(db):
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().select_related("tournament")
    qs2 = Event.objects.all().select_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.intersection(qs2)


@pytest.mark.asyncio
async def test_intersection_with_branch_prefetch_related_raises(db):
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().prefetch_related("tournament")
    qs2 = Event.objects.all().prefetch_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.intersection(qs2)


@pytest.mark.asyncio
async def test_difference_with_branch_select_related_raises(db):
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().select_related("tournament")
    qs2 = Event.objects.all().select_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.difference(qs2)


@pytest.mark.asyncio
async def test_difference_with_branch_prefetch_related_raises(db):
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Event.objects.all().prefetch_related("tournament")
    qs2 = Event.objects.all().prefetch_related("tournament")

    with pytest.raises(QueryError, match="do not support select_related\\(\\)/prefetch_related\\(\\)"):
        await qs1.difference(qs2)


@pytest.mark.asyncio
async def test_mixed_set_operations_apply_in_chain_order(db):
    """Each chained operator applies to the whole result before it, like SQL evaluates a flat
    chain - INTERSECT included, which binds tighter than UNION/EXCEPT on Postgres."""
    tournaments = [await Tournament.objects.create(id=number, name=f"t{number}") for number in range(1, 7)]
    first, second, third, fourth, _fifth, sixth = (tournament.id for tournament in tournaments)
    high = Tournament.objects.filter(id__in=[first, fourth, sixth])
    low = Tournament.objects.filter(id__in=[first, second, third])
    only_fourth = Tournament.objects.filter(id=fourth)
    even = Tournament.objects.filter(id__in=[second, fourth, sixth])

    async def ids(union_query):
        return [tournament.id for tournament in await union_query.order_by("id")]

    assert await ids(high.union(low).union(only_fourth, all=True)) == [first, second, third, fourth, fourth, sixth]
    assert await ids(high.union(low, all=True).union(only_fourth)) == [first, second, third, fourth, sixth]
    assert await ids(high.union(low).intersection(even)) == [second, fourth, sixth]
    assert await high.union(low, all=True).intersection(even).count() == 3
    assert await ids(high.intersection(low).union(only_fourth)) == [first, fourth]
    assert await ids(high.union(low).difference(even)) == [first, third]
    assert await ids(high.difference(low).intersection(even)) == [fourth, sixth]
    assert await ids(high.intersection(even).difference(only_fourth)) == [sixth]


@pytest.mark.asyncio
async def test_update_limit_order_by_with_join(db):
    event_db = Event._meta.db
    event_db.features = event_db.features.replace(supports_update_limit_order_by=True)
    try:
        t1 = await Tournament.objects.create(name="T1")
        e1 = await Event.objects.create(name="E1", tournament=t1)
        e2 = await Event.objects.create(name="E2", tournament=t1)

        updated = (
            await Event.objects.filter(tournament__name="T1").order_by("event_id").limit(1).update(name="E1_updated")
        )
        assert updated == 1

        await e1.refresh_from_db()
        await e2.refresh_from_db()
        assert e1.name == "E1_updated"
        assert e2.name == "E2"
    finally:
        del event_db.features


@pytest.mark.asyncio
async def test_delete_limit_order_by_with_join(db):
    event_db = Event._meta.db
    event_db.features = event_db.features.replace(supports_update_limit_order_by=True)
    try:
        t1 = await Tournament.objects.create(name="T1")
        await Event.objects.create(name="E1", tournament=t1)
        await Event.objects.create(name="E2", tournament=t1)

        deleted = await Event.objects.filter(tournament__name="T1").order_by("event_id").limit(1).delete()
        assert deleted == 1

        count = await Event.objects.all().count()
        assert count == 1
    finally:
        del event_db.features


def test_capabilities_resolves_from_bound_connection_not_model_default(db):
    """.features must reflect the connection actually bound to this query (_db, e.g. via
    .using(...)/router dispatch), not always fall back to the model's default connection -
    two connections of different dialects would otherwise make LIMIT/select_for_update/etc.
    capability checks reflect where the query WASN'T sent."""
    from hare.dialects.base.features import Features
    from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

    class FakeDB:
        dialect = POSTGRESQL_DIALECT
        features = Features()
        query_class = PostgresqlQuery

    qs = Event.objects.all()
    qs._apply_db(FakeDB())
    assert qs.features is FakeDB.features
    assert qs.features is not Event._meta.db.features


def test_capabilities_cache_invalidated_by_apply_db(db):
    """Regression: `.features` is cached on first access (see `AwaitableQuery.features`'s
    own `_features is None` guard) - `_apply_db()` used to leave that cache in place even when
    rebinding `self._db` to a DIFFERENT connection, so a queryset that had already resolved
    `.features` from one connection kept reporting THAT connection's capabilities forever,
    even after being rebound - e.g. `select_for_update()`'s own capability gate silently keeping
    treating the backend as unable to lock rows after `.using()` moved it to one that can.
    `_apply_db()` now resets `_features` to `None` on every call, so the next `.features`
    access always recomputes it from whatever `self._db` actually is at that point."""
    from hare.dialects.base.features import Features
    from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

    class FakeDB:
        dialect = POSTGRESQL_DIALECT
        features = Features()
        query_class = PostgresqlQuery

    qs = Event.objects.all()
    qs._apply_db(Event.get_connection())
    _ = qs.features  # forces caching from the model's own default connection
    assert qs.features is not FakeDB.features

    qs._apply_db(FakeDB())
    assert qs.features is FakeDB.features


@pytest.mark.asyncio
async def test_update_limit_without_join_is_emulated_via_subquery(db):
    """support_update_limit_order_by is False for every real backend (sqlite/postgres) -
    .limit() used to be silently dropped by .update() whenever there was no join to already
    force the pk-subquery path (only the WITH-a-join case above was covered), so
    `.filter(...).limit(1).update(...)` updated EVERY matching row instead of just one."""
    for i in range(3):
        await Tournament.objects.create(name=f"T{i}")

    updated = await Tournament.objects.filter(name__startswith="T").limit(1).update(desc="touched")
    assert updated == 1
    assert await Tournament.objects.filter(desc="touched").count() == 1


@pytest.mark.asyncio
async def test_delete_limit_without_join_is_emulated_via_subquery(db):
    """Same gap as test_update_limit_without_join_is_emulated_via_subquery, for .delete() -
    `.filter(...).limit(1).delete()` used to delete EVERY matching row, and
    `.all()[0:1].delete()` used to wipe the whole table."""
    for i in range(3):
        await Tournament.objects.create(name=f"T{i}")

    deleted = await Tournament.objects.filter(name__startswith="T").limit(1).delete()
    assert deleted == 1
    assert await Tournament.objects.all().count() == 2


@pytest.mark.asyncio
async def test_delete_slice_applies_offset_not_just_limit(db):
    """.offset()/slicing used to be silently ignored by .delete() - only self._limit was ever
    threaded into DeleteQuery, never self._offset. `.order_by("id")[2:4].delete()` reported 2
    rows deleted but always deleted the FIRST 2 matching rows regardless of the requested
    offset (ids 1,2 instead of the sliced-out ids 3,4), removing the wrong rows entirely."""
    tournaments = [await Tournament.objects.create(name=f"T{i}") for i in range(5)]

    deleted = await Tournament.objects.all().order_by("id")[2:4].delete()
    assert deleted == 2

    remaining_ids = set(await Tournament.objects.all().values_list("id", flat=True))
    assert remaining_ids == {tournaments[0].id, tournaments[1].id, tournaments[4].id}


@pytest.mark.asyncio
async def test_update_offset_without_limit_is_emulated_via_subquery(db):
    """Same gap as test_delete_slice_applies_offset_not_just_limit, for .update() - and for a
    bare .offset() with no .limit() at all, which used to skip the limit-emulation branch
    entirely (that branch only ever checked self._limit) and fall straight through to the
    native/no-op path, updating EVERY matching row instead of skipping the first 2."""
    tournaments = [await Tournament.objects.create(name=f"T{i}") for i in range(5)]

    updated = await Tournament.objects.all().order_by("id").offset(2).update(desc="touched")
    assert updated == 3

    touched_ids = set(await Tournament.objects.filter(desc="touched").values_list("id", flat=True))
    assert touched_ids == {tournaments[2].id, tournaments[3].id, tournaments[4].id}


@pytest.mark.asyncio
async def test_update_on_aggregate_annotated_queryset_filters_per_row(db):
    """.update()/.delete() build the WHERE via a `pk IN (SELECT pk FROM ... HAVING ...)`
    subquery whenever a join is involved - an aggregate .annotate() always joins to compute it,
    so get_filters()'s own aggregate term/HAVING clause end up in that subquery's builder
    too. Without stripping the annotation back out of the SELECT list first, .select(pk) APPENDS
    to it instead of replacing it, producing "sub-select returns 2 columns - expected 1". And
    without a GROUP BY, the HAVING clause would aggregate over the WHOLE joined result instead
    of once per row, matching either every row or none instead of just the ones whose own
    related count qualifies."""
    with_events = await Tournament.objects.create(name="T-with-events")
    without_events = await Tournament.objects.create(name="T-without-events")
    await Event.objects.create(name="E1", tournament=with_events)
    await Event.objects.create(name="E2", tournament=with_events)

    updated = (
        await Tournament.objects.annotate(event_count=Count("events__event_id"))
        .filter(event_count__gte=1)
        .update(desc="has-events")
    )
    assert updated == 1
    assert await Tournament.objects.get(pk=with_events.pk).values("desc") == {"desc": "has-events"}
    assert await Tournament.objects.get(pk=without_events.pk).values("desc") == {"desc": None}


@pytest.mark.asyncio
async def test_delete_on_aggregate_annotated_queryset_filters_per_row(db):
    """Same gap as test_update_on_aggregate_annotated_queryset_filters_per_row, for .delete()."""
    with_events = await Tournament.objects.create(name="T-with-events")
    without_events = await Tournament.objects.create(name="T-without-events")
    await Event.objects.create(name="E1", tournament=with_events)

    deleted = (
        await Tournament.objects.annotate(event_count=Count("events__event_id")).filter(event_count__gte=1).delete()
    )
    assert deleted >= 1
    remaining_ids = set(await Tournament.objects.all().values_list("id", flat=True))
    assert remaining_ids == {without_events.id}


def test_update_query_postgres_dialect_coverage(db):
    q = Event.objects.filter(tournament__name="T1").limit(1).update(name="E1_updated")
    sql = q.sql()
    assert "IN (SELECT " in sql
    assert '"_t"' not in sql
    assert "`_t`" not in sql


def test_delete_query_postgres_dialect_coverage(db):
    q = Event.objects.filter(tournament__name="T1").delete()
    sql = q.sql()
    assert "IN (SELECT " in sql
    assert '"_t"' not in sql
    assert "`_t`" not in sql


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_values_list_preserves_select_for_update(db):
    """See hare/hare-orm#2250: .values_list() silently dropped select_for_update() - the row
    lock disappeared with no error or warning.

    Needs the `db` fixture despite never awaiting anything - .sql() needs an active HareContext
    to choose the connection.
    """
    qs = Author.objects.filter(name="x").select_for_update()
    assert "FOR UPDATE" in qs.sql()

    values_list_qs = qs.values_list("id", flat=True)
    assert "FOR UPDATE" in values_list_qs.sql()


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_values_preserves_select_for_update(db):
    """Needs the `db` fixture for the same reason as test_values_list_preserves_select_for_update
    above - .sql() always needs either self._db or an active HareContext."""
    qs = Author.objects.filter(name="x").select_for_update()
    values_qs = qs.values("id")
    assert "FOR UPDATE" in values_qs.sql()


@requires_features(supports_select_for_update=True)
@pytest.mark.asyncio
async def test_values_select_for_update_with_joined_field_defaults_to_locking_the_base_table(db):
    """A "related__field"-style .values()/.values_list() field goes through the same
    _join_table()/_join_table_with_forwarded_fields() path select_related() does, always
    building a LEFT OUTER JOIN - Postgres rejects a bare FOR UPDATE (no OF) against ANY query
    containing an outer join at all, the identical crash select_related() +
    select_for_update() already had fixed (cache_shape.py). FieldSelectQuery's own
    _apply_limit_offset_group_by_and_lock() needed the same `of=` default."""
    qs = Book.objects.filter(name="x").select_for_update()
    values_qs = qs.values("id", "author__name")
    assert "FOR UPDATE OF" in values_qs.sql()

    values_list_qs = qs.values_list("id", "author__name")
    assert "FOR UPDATE OF" in values_list_qs.sql()


@pytest.mark.asyncio
async def test_subquery_params_inline_includes_inner_filter_values(db):
    """See hare/hare-orm#1800: sql(params_inline=True) didn't substitute filter values INSIDE a
    Subquery(...) - the placeholder (`?`/`$1`) was left literally in the text, losing the value.
    Critical for app/utils/managers/policy_redirect_access_manager.py: this SQL gets pasted as
    text into ANOTHER raw query, without going through parameterization again.

    Needs the `db` fixture for the same reason as the two tests above - this one doesn't even
    set .features, so without an active HareContext .sql() fails outright rather than just
    risking the wrong dialect.
    """
    query = Book.objects.filter(
        id__in=Subquery(Author.objects.filter(name="specific-author").values_list("id", flat=True))
    )
    sql = query.sql(params_inline=True)
    assert "specific-author" in sql


# ============================================================================
# QuerySet.none() - resolves to an empty result WITHOUT ever executing a query, unlike e.g.
# .filter(pk__in=[]) (still a real "WHERE 1=0" round trip to the database).
# ============================================================================


@pytest.mark.asyncio
async def test_none_returns_empty_list_without_querying(db):
    await Tournament.objects.create(name="T1")

    with patch.object(type(Tournament.objects.all()), "_make_query", autospec=True) as spy:
        result = await Tournament.objects.none()
        spy.assert_not_called()
    assert result == []


@pytest.mark.asyncio
async def test_none_iteration_yields_nothing_without_querying(db):
    await Tournament.objects.create(name="T1")

    with patch.object(type(Tournament.objects.all()), "_make_query", autospec=True) as spy:
        collected = [row async for row in Tournament.objects.none()]
        spy.assert_not_called()
    assert collected == []


@pytest.mark.asyncio
async def test_none_chained_with_filter_stays_empty(db):
    """.filter()/.annotate()/.order_by()/etc chained onto .none() must stay empty, not crash -
    every one of them is a plain _clone(), which carries _is_none along unchanged."""
    t1 = await Tournament.objects.create(name="T1")

    result = await Tournament.objects.none().filter(name="T1")
    assert result == []

    result = await Tournament.objects.none().filter(id=t1.id).annotate(event_count=Count("events")).order_by("name")
    assert result == []

    result = await Tournament.objects.none().exclude(name="T1")
    assert result == []

    result = await Tournament.objects.none().only("name").select_related()
    assert result == []


@pytest.mark.asyncio
async def test_none_chained_onto_existing_filter_still_empty(db):
    """The other call order - .none() applied AFTER other clauses - must behave the same way."""
    await Tournament.objects.create(name="T1")

    result = await Tournament.objects.filter(name="T1").none()
    assert result == []


@pytest.mark.asyncio
async def test_none_first_last_get_or_none(db):
    await Tournament.objects.create(name="T1")

    assert await Tournament.objects.none().first() is None
    assert await Tournament.objects.none().order_by("name").last() is None
    assert await Tournament.objects.none().get_or_none(name="T1") is None


@pytest.mark.asyncio
async def test_none_get_raises_does_not_exist(db):
    await Tournament.objects.create(name="T1")

    with pytest.raises(DoesNotExist):
        await Tournament.objects.none().get(name="T1")


@pytest.mark.asyncio
async def test_none_count_is_zero_without_querying(db):
    await Tournament.objects.create(name="T1")

    from hare.query.statements.summary.count_query import CountQuery

    with patch.object(CountQuery, "_make_query", autospec=True) as spy:
        result = await Tournament.objects.none().count()
        spy.assert_not_called()
    assert result == 0


@pytest.mark.asyncio
async def test_none_exists_is_false_without_querying(db):
    await Tournament.objects.create(name="T1")

    from hare.query.statements.summary.exists_query import ExistsQuery

    with patch.object(ExistsQuery, "_make_query", autospec=True) as spy:
        result = await Tournament.objects.none().exists()
        spy.assert_not_called()
    assert result is False


@pytest.mark.asyncio
async def test_none_contains_is_false(db):
    tournament = await Tournament.objects.create(name="T1")
    assert await Tournament.objects.none().contains(tournament) is False


@pytest.mark.asyncio
async def test_none_values_and_values_list_are_empty_without_querying(db):
    await Tournament.objects.create(name="T1")

    from hare.query.statements.select.values_query import ValuesQuery

    with patch.object(ValuesQuery, "_make_query", autospec=True) as spy:
        result = await Tournament.objects.none().values("name")
        spy.assert_not_called()
    assert result == []

    with patch.object(ValuesQuery, "_make_query", autospec=True) as spy:
        result = await Tournament.objects.none().values_list("name", flat=True)
        spy.assert_not_called()
    assert result == []


@pytest.mark.asyncio
async def test_none_aggregate_returns_the_values_of_an_empty_set(db):
    """.none().aggregate() runs the query with an always-false condition, so every expression
    gets exactly the value the database gives for an empty set (COUNT is 0, SUM is NULL, a
    Coalesce is its default) - the same as filter(pk__in=[]).aggregate()."""
    await Tournament.objects.create(name="T1")

    from hare.query.functions import Coalesce, Count, Sum

    result = await Tournament.objects.none().aggregate(
        total=Sum("id"), rows=Count("id"), fallback=Coalesce(Sum("id"), 0)
    )
    assert result == {"total": None, "rows": 0, "fallback": 0}
    assert result == await Tournament.objects.filter(id__in=[]).aggregate(
        total=Sum("id"), rows=Count("id"), fallback=Coalesce(Sum("id"), 0)
    )


@pytest.mark.asyncio
async def test_none_delete_and_update_do_nothing_without_querying(db):
    await Tournament.objects.create(name="T1")

    from hare.query.statements.write.delete_query import DeleteQuery
    from hare.query.statements.write.update_query import UpdateQuery

    with patch.object(UpdateQuery, "_make_query", autospec=True) as spy:
        updated = await Tournament.objects.none().update(name="changed")
        spy.assert_not_called()
    assert updated == 0
    assert await Tournament.objects.filter(name="changed").count() == 0

    with patch.object(DeleteQuery, "_make_query", autospec=True) as spy:
        deleted = await Tournament.objects.none().delete()
        spy.assert_not_called()
    assert deleted == 0
    assert await Tournament.objects.all().count() == 1


@pytest.mark.asyncio
async def test_none_as_in_subquery_matches_nothing(db):
    """A .none() queryset embedded as a subquery is only ever checked for _is_none at execution
    time of the queryset itself - embedded in another query it used to render as the full,
    unfiltered SELECT, so `__in` matched every row."""
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    assert (
        await IntFields.objects.filter(intnum__in=IntFields.objects.none().values_list("intnum", flat=True)).count()
        == 0
    )
    assert (
        await IntFields.objects.filter(
            intnum__in=Subquery(IntFields.objects.none().values_list("intnum", flat=True))
        ).count()
        == 0
    )
    assert (
        await IntFields.objects.filter(
            intnum__in=IntFields.objects.filter(intnum=1).none().values_list("intnum", flat=True)
        )
        == []
    )


@pytest.mark.asyncio
async def test_none_as_bare_queryset_in_subquery_matches_nothing(db):
    await Tournament.objects.create(name="T1")

    assert await Tournament.objects.filter(pk__in=Tournament.objects.none()) == []
    assert await Tournament.objects.filter(pk__in=Tournament.objects.none().values("id")) == []


@pytest.mark.asyncio
async def test_exclude_none_subquery_keeps_every_row(db):
    await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")

    assert await Tournament.objects.exclude(pk__in=Tournament.objects.none()).count() == 2
    assert await Tournament.objects.exclude(pk__in=Tournament.objects.none().values_list("id", flat=True)).count() == 2
    assert await Tournament.objects.filter(pk__not_in=Tournament.objects.none()).count() == 2


@pytest.mark.asyncio
async def test_none_inside_exists_is_false(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)

    annotated = await Tournament.objects.annotate(
        has_events=Exists(Event.objects.filter(tournament_id=OuterRef("id")).none())
    )
    assert [row.has_events for row in annotated] == [False]
    assert await Tournament.objects.annotate(x=Exists(Event.objects.none())).filter(x=True).count() == 0
    assert await Tournament.objects.annotate(x=Exists(Event.objects.none())).filter(x=False).count() == 1
    assert await Tournament.objects.annotate(x=~Exists(Event.objects.none())).filter(x=True).count() == 1

    non_none = await Tournament.objects.annotate(has_events=Exists(Event.objects.filter(tournament_id=OuterRef("id"))))
    assert [row.has_events for row in non_none] == [True]


@pytest.mark.asyncio
async def test_none_subquery_annotation_is_null_and_repeated_builds_stay_correct(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)

    for _ in range(3):
        annotated = await Tournament.objects.annotate(
            first_event_name=Subquery(
                Event.objects.filter(tournament_id=OuterRef("id")).none().values_list("name", flat=True)
            )
        )
        assert [row.first_event_name for row in annotated] == [None]
        assert await Tournament.objects.filter(pk__in=Tournament.objects.none()).count() == 0
        assert await Tournament.objects.filter(pk__in=Tournament.objects.all()).count() == 1


@pytest.mark.asyncio
async def test_none_subquery_does_not_change_the_none_queryset_itself(db):
    """Embedding a .none() queryset as a subquery must not disturb its own (non-database)
    resolution."""
    await Tournament.objects.create(name="T1")
    none_queryset = Tournament.objects.none()

    assert await Tournament.objects.filter(pk__in=none_queryset) == []
    assert await none_queryset == []
    assert await none_queryset.count() == 0


@pytest.mark.asyncio
async def test_filter_pk_in_empty_list_still_queries_the_database(db):
    """Contrast case for .none(): the pre-existing "empty queryset" workaround
    (.filter(pk__in=[])) still resolves to a real WHERE 1=0 round trip, unlike .none()."""
    await Tournament.objects.create(name="T1")

    from hare.query.statements.select.model_rows_query import ModelRowsQuery

    with patch.object(ModelRowsQuery, "_make_query", side_effect=ModelRowsQuery._make_query, autospec=True) as spy:
        result = await Tournament.objects.filter(pk__in=[])
        spy.assert_called()
    assert result == []


@pytest.mark.asyncio
async def test_using_is_a_thin_wrapper_over_using_db(db):
    """.using(alias) must resolve `alias` through Connections and produce exactly the queryset
    .using(Connections.get(alias)) already would - a plain ergonomic shortcut, not new
    behavior of its own."""
    await Tournament.objects.create(name="T1")

    by_alias = Tournament.objects.all().using("models")
    by_client = Tournament.objects.all().using(Connections.get("models"))
    assert by_alias._db is by_client._db
    assert by_alias._db is Connections.get("models")

    result = await by_alias
    assert [t.name for t in result] == ["T1"]


@pytest.mark.asyncio
async def test_using_straight_off_the_model_queryset(db):
    """``Model.objects`` is a QuerySet - .using() works on it directly, not only after .all()."""
    await Tournament.objects.create(name="T1")

    result = await Tournament.objects.using("models")
    assert [t.name for t in result] == ["T1"]


@pytest.mark.asyncio
async def test_prefetch_related_on_values_and_values_list_raises_clear_error(db):
    """ValuesQuery/ValuesListQuery don't define prefetch_related() at all - calling it in this
    order (build the values query first, then try to prefetch) used to raise a bare
    AttributeError instead of the clear ValueError the REVERSE call order already gives for the
    identical underlying reason. UnionQuery.prefetch_related() is no longer part of this guard -
    see test_union_prefetch_related for its own, now-supported behavior."""
    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().values("id").prefetch_related("events")
    with pytest.raises(ValueError, match="prefetch_related"):
        Tournament.objects.all().values_list("id").prefetch_related("events")


@pytest.mark.asyncio
async def test_union_prefetch_related(db):
    """UnionQuery.prefetch_related() used to unconditionally raise ValueError - it now batches
    prefetching over the union's own materialized instance list via Model.fetch_for_list(),
    after execute_union() hydrates it. Each tournament gets a DIFFERENT number of events so a
    values-mixed-up-between-rows bug (each row getting some OTHER row's events, or all of them)
    would fail this, not just a "the relation was fetched at all" check."""
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    t3 = await Tournament.objects.create(name="T3")
    await Event.objects.create(name="E1a", tournament=t1)
    await Event.objects.create(name="E1b", tournament=t1)
    await Event.objects.create(name="E2a", tournament=t2)
    await Event.objects.create(name="E3a", tournament=t3)  # t3 is in neither branch

    qs1 = Tournament.objects.filter(name="T1")
    qs2 = Tournament.objects.filter(name="T2")

    with pytest.raises(NoValuesFetched):
        # Sanity check: without prefetch_related(), a union result behaves like any other
        # unfetched relation - proves the assertions below actually exercise the prefetch, not
        # some other path that always populates .events regardless. ReverseRelation only raises
        # once actually iterated, not on bare attribute access.
        list((await qs1.union(qs2))[0].events)

    result = await qs1.union(qs2).prefetch_related("events")

    by_name = {tournament.name: tournament for tournament in result}
    assert set(by_name) == {"T1", "T2"}
    assert {event.name for event in by_name["T1"].events} == {"E1a", "E1b"}
    assert {event.name for event in by_name["T2"].events} == {"E2a"}


@pytest.mark.asyncio
async def test_union_prefetch_related_with_prefetch_object(db):
    """Prefetch(relation, queryset=..., to_attr=...) must work on a UnionQuery result too, not
    just a bare relation-name string - exercises the same custom-queryset/to_attr machinery
    QuerySet.prefetch_related() already supports."""
    t1 = await Tournament.objects.create(name="T1")
    t2 = await Tournament.objects.create(name="T2")
    await Event.objects.create(name="E1a", tournament=t1)
    await Event.objects.create(name="E1b", tournament=t1)
    await Event.objects.create(name="E2a", tournament=t2)

    qs1 = Tournament.objects.filter(name="T1")
    qs2 = Tournament.objects.filter(name="T2")

    result = await qs1.union(qs2).prefetch_related(
        Prefetch("events", queryset=Event.objects.filter(name="E1a"), to_attr="filtered_events")
    )

    by_name = {tournament.name: tournament for tournament in result}
    assert [event.name for event in by_name["T1"].filtered_events] == ["E1a"]
    assert by_name["T2"].filtered_events == []


@pytest.mark.asyncio
async def test_union_prefetch_related_m2m(db):
    """Covers the M2M prefetch strategy (not just reverse-FK) on a union result. Uses
    MinRelation, not Event - Event has Meta.ordering, which a union branch can't carry at all
    (a separate, pre-existing limitation of union() unrelated to this feature)."""
    tournament = await Tournament.objects.create(name="tournament")
    relation1 = await MinRelation.objects.create(tournament=tournament)
    relation2 = await MinRelation.objects.create(tournament=tournament)
    team1 = await Team.objects.create(name="Team1")
    team2 = await Team.objects.create(name="Team2")
    await relation1.participants.add(team1)
    await relation2.participants.add(team1, team2)

    qs1 = MinRelation.objects.filter(id=relation1.id)
    qs2 = MinRelation.objects.filter(id=relation2.id)

    result = await qs1.union(qs2).prefetch_related("participants")

    by_id = {relation.id: relation for relation in result}
    assert {team.name for team in by_id[relation1.id].participants} == {"Team1"}
    assert {team.name for team in by_id[relation2.id].participants} == {"Team1", "Team2"}


@pytest.mark.asyncio
async def test_union_prefetch_related_on_intersection(db):
    """prefetch_related() on a UnionQuery built from .intersection() (not just .union()) works
    the same way - UnionQuery has exactly one prefetch_related() implementation regardless of
    which set operation built it."""
    t1 = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=t1)

    qs1 = Tournament.objects.filter(name="T1")
    qs2 = Tournament.objects.filter(name="T1")

    result = await qs1.intersection(qs2).prefetch_related("events")

    assert len(result) == 1
    assert [event.name for event in result[0].events] == ["E1"]


@pytest.mark.asyncio
async def test_union_prefetch_related_empty_result_no_error(db):
    """prefetch_related() on a union that matches no rows must not blow up trying to fetch
    relations for an empty instance list."""
    qs1 = Tournament.objects.filter(name="does-not-exist-1")
    qs2 = Tournament.objects.filter(name="does-not-exist-2")

    result = await qs1.union(qs2).prefetch_related("events")
    assert result == []


@pytest.mark.asyncio
async def test_union_prefetch_related_mixed_models(db):
    """A union combining genuinely different model classes (see test_union_mixed_models) must
    prefetch each instance's OWN relation, off its own real model's executor - calling
    fetch_for_list() once against a single blended model would either crash (wrong _meta) or
    silently resolve the wrong relation."""
    t1 = await Tournament.objects.create(name="T1")
    r1 = await Reporter.objects.create(name="R1")
    await Event.objects.create(name="E1", tournament=t1)
    await Event.objects.create(name="E2", reporter=r1, tournament=t1)

    qs1 = Tournament.objects.filter(id=t1.id).only("id", "name")
    qs2 = Reporter.objects.filter(id=r1.id).only("id", "name")

    result = await qs1.union(qs2).prefetch_related("events")

    by_name = {row.name: row for row in result}
    assert {event.name for event in by_name["T1"].events} == {"E1", "E2"}
    assert {event.name for event in by_name["R1"].events} == {"E2"}


@pytest.mark.asyncio
async def test_select_related_on_values_and_values_list_raises_clear_error(db):
    """.values()/.values_list() return plain dicts/tuples, not model instances, so an explicit
    select_related() call (like .only()/.defer()/prefetch_related()) has nothing to attach a
    joined relation to - this used to silently succeed and just discard select_related() instead
    of raising, unlike every other incompatible option .values()/.values_list() already guard."""
    with pytest.raises(ValueError, match="select_related"):
        Book.objects.all().select_related("author").values("name", "author__name")
    with pytest.raises(ValueError, match="select_related"):
        Book.objects.all().select_related("author").values_list("name", "author__name")


@pytest_asyncio.fixture
async def temporal_batches(db):
    """Two batches with records of every date/time type, for correlated-subquery aggregates."""
    UTC = datetime.UTC
    first = await TemporalBatch.objects.create(name="A")
    second = await TemporalBatch.objects.create(name="B")
    rows = [
        (first, datetime.date(2021, 3, 1), datetime.datetime(2021, 3, 1, 10, tzinfo=UTC), 1200, "100.50"),
        (first, datetime.date(2020, 1, 5), datetime.datetime(2020, 1, 5, 10, tzinfo=UTC), 5, "49.50"),
        (second, datetime.date(2019, 1, 5), datetime.datetime(2019, 1, 5, 10, tzinfo=UTC), 7, "1.10"),
    ]
    for batch, day, started_at, seconds, amount in rows:
        await TemporalRecord.objects.create(
            batch=batch,
            day=day,
            started_at=started_at,
            duration=datetime.timedelta(seconds=seconds),
            amount=Decimal(amount),
        )
    return first, second


def per_batch_aggregate(aggregate):
    return (
        TemporalRecord.objects.filter(batch_id=OuterRef("id")).annotate(m=aggregate).group_by("batch_id").values("m")
    )


@pytest.mark.asyncio
async def test_subquery_annotation_of_a_max_date_is_a_date(temporal_batches):
    """On SQLite the raw driver value ("2021-03-01") was returned instead of a date."""
    rows = (
        await TemporalBatch.objects.all()
        .annotate(x=Subquery(per_batch_aggregate(Max("day"))))
        .order_by("name")
        .values_list("name", "x")
    )
    assert rows == [("A", datetime.date(2021, 3, 1)), ("B", datetime.date(2019, 1, 5))]
    assert all(type(value) is datetime.date for _, value in rows)


@pytest.mark.asyncio
async def test_subquery_annotation_of_a_sum_decimal_is_a_decimal(temporal_batches):
    """On SQLite the SUM came back as a float/int instead of a Decimal."""
    rows = (
        await TemporalBatch.objects.all()
        .annotate(x=Subquery(per_batch_aggregate(Sum("amount"))))
        .order_by("name")
        .values_list("name", "x")
    )
    assert rows == [("A", Decimal("150.00")), ("B", Decimal("1.10"))]
    assert all(isinstance(value, Decimal) for _, value in rows)


@pytest.mark.asyncio
async def test_subquery_annotation_of_a_max_datetime_is_an_aware_datetime(temporal_batches):
    rows = (
        await TemporalBatch.objects.all()
        .annotate(x=Subquery(per_batch_aggregate(Max("started_at"))))
        .order_by("name")
        .values_list("name", "x")
    )
    assert rows == [
        ("A", datetime.datetime(2021, 3, 1, 10, tzinfo=datetime.UTC)),
        ("B", datetime.datetime(2019, 1, 5, 10, tzinfo=datetime.UTC)),
    ]
    assert all(isinstance(value, datetime.datetime) and value.tzinfo is not None for _, value in rows)


@pytest.mark.asyncio
async def test_subquery_annotation_of_a_max_timedelta_is_a_timedelta(temporal_batches):
    """A TimeDeltaField's stored microseconds came back as a bare int on every backend."""
    subquery = Subquery(per_batch_aggregate(Max("duration")))
    expected = [("A", datetime.timedelta(seconds=1200)), ("B", datetime.timedelta(seconds=7))]
    assert await TemporalBatch.objects.all().annotate(x=subquery).order_by("name").values_list("name", "x") == expected
    dict_rows = await TemporalBatch.objects.all().annotate(x=subquery).order_by("name").values("name", "x")
    assert dict_rows == [{"name": name, "x": value} for name, value in expected]


@pytest.mark.asyncio
async def test_subquery_annotation_is_decoded_on_full_instances(temporal_batches):
    batches = (
        await TemporalBatch.objects.all().annotate(x=Subquery(per_batch_aggregate(Max("duration")))).order_by("name")
    )
    assert [batch.x for batch in batches] == [datetime.timedelta(seconds=1200), datetime.timedelta(seconds=7)]


@pytest.mark.asyncio
async def test_filter_on_a_decoded_subquery_annotation(temporal_batches):
    result = (
        await TemporalBatch.objects.all()
        .annotate(x=Subquery(per_batch_aggregate(Max("day"))))
        .filter(x__gt=datetime.date(2020, 1, 1))
        .values_list("name", "x")
    )
    assert result == [("A", datetime.date(2021, 3, 1))]


@pytest.mark.asyncio
async def test_subquery_annotation_decoding_survives_repeated_calls(temporal_batches):
    """The decoded output fields ride along in the cached query shape - a second, structurally
    identical call must decode exactly like the first."""
    for _ in range(3):
        rows = (
            await TemporalBatch.objects.all()
            .annotate(x=Subquery(per_batch_aggregate(Max("day"))))
            .order_by("name")
            .values_list("x", flat=True)
        )
        assert rows == [datetime.date(2021, 3, 1), datetime.date(2019, 1, 5)]


@pytest.mark.asyncio
async def test_subquery_annotation_of_a_non_column_stays_raw(db):
    """A subquery whose single selected column has no known field is left undecoded."""
    tournament = await Tournament.objects.create(name="1")
    rows = (
        await Tournament.objects.filter(pk=tournament.pk)
        .annotate(ids=Subquery(Tournament.objects.filter(pk=tournament.pk).values("id")))
        .values_list("ids", flat=True)
    )
    assert rows == [tournament.pk]


@pytest.mark.asyncio
async def test_only_with_annotation_and_select_related_does_not_misalign_hydration(db):
    """_get_only() recorded len(data_fields) (every name in .only(...), including
    annotation names) as the base model's hydration bucket size, but the actual SELECT excludes
    annotations from that count - misaligning the row-splitting offset for any bucket that
    follows (a select_related()'d relation), crashing with IndexError deep inside the executor."""
    author = await Author.objects.create(name="alice")
    await Book.objects.create(name="b1", author=author, rating=1.0)
    await Book.objects.create(name="b2", author=author, rating=2.0)

    books = (
        await Book.objects.all()
        .annotate(upper_name=Upper("name"))
        .select_related("author")
        .only("name", "rating", "upper_name", "author__name")
        .order_by("name")
    )
    assert [b.name for b in books] == ["b1", "b2"]
    assert [b.upper_name for b in books] == ["B1", "B2"]
    assert [b.author.name for b in books] == ["alice", "alice"]


@pytest.mark.asyncio
async def test_only_with_annotation_and_unrestricted_select_related(db):
    """Same class of misalignment as the test above, but the OTHER way round: the annotation
    isn't named in .only() at all, and select_related()'s own relation isn't EITHER - meaning
    that relation's own columns get joined later, by _join_select_related() (which runs AFTER
    the annotation's own column is already selected), rather than by _get_only() itself (which
    runs BEFORE). The bucket this bug affects, and where the annotation column physically lands
    relative to it, differ from the case above - both must stay fixed independently."""
    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E1", tournament=tournament)

    event = (
        await Event.objects.filter(pk__gte=0)
        .select_related("tournament")
        .only("event_id", "name")
        .annotate(name_copy=Upper("name"))
        .first()
    )
    assert event.name == "E1"
    assert event.name_copy == "E1"
    assert event.tournament.name == "T"


@pytest.mark.asyncio
async def test_order_by_after_after_cursor_raises_clear_error(db):
    """.after_cursor()'s cursor values are positionally bound to whatever .order_by() was active
    at the time - nothing stopped a later .order_by() call from replacing that ordering while
    leaving the stale cursor values in place, either silently mispairing them against the new
    ordering or crashing with a confusing bare ValueError deep inside query building."""
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=2)

    qs = IntFields.objects.all().order_by("intnum").after_cursor(1)
    with pytest.raises(ValueError, match="after_cursor"):
        qs.order_by("-intnum")


@pytest.mark.asyncio
async def test_order_by_before_after_cursor_still_works(db):
    """Regression control: the normal, documented call order must be unaffected."""
    a = await IntFields.objects.create(intnum=1)
    b = await IntFields.objects.create(intnum=2)
    await IntFields.objects.create(intnum=3)

    page = await IntFields.objects.all().order_by("intnum").after_cursor(a.intnum).limit(1)
    assert [row.intnum for row in page] == [b.intnum]


@pytest.mark.asyncio
async def test_contains_on_composite_pk_model_does_not_crash(db):
    """ContainsQuery._make_query() used model._meta.pk.to_db_value(...) - model._meta.pk is
    None for a composite PK (only set for a single-column PK), crashing with AttributeError."""
    obj = await DirtyTrackedComposite.objects.create(a=1, b=1, name="x")
    other = await DirtyTrackedComposite.objects.create(a=1, b=2, name="y")

    assert await DirtyTrackedComposite.objects.all().contains(obj) is True
    assert await DirtyTrackedComposite.objects.filter(name="x").contains(other) is False


@pytest.mark.asyncio
async def test_bulk_update_across_join_on_composite_pk_model(db):
    """UpdateQuery._make_query()'s JOIN-required subquery rewrite used
    model._meta.db_pk_column, which is an empty string for a composite PK (only set for a
    single-column PK) - CompositePkOwningFK.objects.filter(tournament__name=...).update(...) needs this
    exact rewrite (a JOIN through a forward FK) and a composite-PK model CAN reach it (it just
    can't be the TARGET of a relation, unlike being one's owner)."""
    t1 = await Tournament.objects.create(name="Target")
    t2 = await Tournament.objects.create(name="Other")
    await CompositePkOwningFK.objects.create(a=1, b=1, name="x", tournament=t1)
    await CompositePkOwningFK.objects.create(a=1, b=2, name="y", tournament=t2)

    count = await CompositePkOwningFK.objects.filter(tournament__name="Target").update(name="updated")
    assert count == 1

    updated_names = sorted(
        obj.name
        for obj in [await CompositePkOwningFK.objects.get(a=1, b=1), await CompositePkOwningFK.objects.get(a=1, b=2)]
    )
    assert updated_names == ["updated", "y"]


@pytest.mark.asyncio
async def test_negated_empty_q_matches_everything(db):
    """Like Django, an empty Q() is a no-op whether negated or not - ~Q() matches every row."""
    await Author.objects.create(name="a")
    await Author.objects.create(name="b")

    result = await Author.objects.filter(~Q())
    assert {a.name for a in result} == {"a", "b"}


@pytest.mark.asyncio
async def test_non_negated_empty_q_still_matches_everything(db):
    """Regression control: a plain (non-negated) empty Q() must be unaffected."""
    await Author.objects.create(name="a")
    await Author.objects.create(name="b")

    result = await Author.objects.filter(Q())
    assert {a.name for a in result} == {"a", "b"}


@pytest.mark.asyncio
async def test_when_with_aggregate_annotation_condition_does_not_crash(db):
    """When.get_result() only read modifier.where_criterion, dropping modifier.having_criterion -
    a When(Q(...)) condition on an aggregate-annotated field (event_count__gt=1, resolving into
    having_criterion not where_criterion) built an empty (always-EmptyCriterion) WHEN clause,
    which crashes with NotImplementedError the moment it's actually rendered."""
    t1 = await Tournament.objects.create(name="Busy")
    await Event.objects.create(name="E1", tournament=t1)
    await Event.objects.create(name="E2", tournament=t1)
    t2 = await Tournament.objects.create(name="Quiet")
    await Event.objects.create(name="E3", tournament=t2)

    category = Case(When(Q(event_count__gt=1), then="many"), default="few")
    rows = (
        await Tournament.objects.all()
        .annotate(event_count=Count("events"))
        .annotate(category=category)
        .order_by("name")
        .values("name", "category")
    )
    assert rows == [
        {"name": "Busy", "category": "many"},
        {"name": "Quiet", "category": "few"},
    ]


@pytest.mark.asyncio
async def test_backward_m2m_filter_with_outerref_value_does_not_crash(db):
    """_process_filter_kwarg()'s "table" in filter_info branch (backward-FK/M2M relation
    filters) ran value_encoder(value, model) unconditionally, unlike the plain-field branch
    right below it which guards with `not isinstance(value, Term)`. An already-resolved Term
    (OuterRef/Subquery/F()) is a raw hare.sql expression, not a Python value a value_encoder (a pk
    field's to_db_value) can coerce - crashed with TypeError trying int()/str() the Term itself."""
    tournament = await Tournament.objects.create(name="T")
    event = await Event.objects.create(name="E1", tournament=tournament)
    other_event = await Event.objects.create(name="E2", tournament=tournament)
    team = await Team.objects.create(name="Team A")
    await event.participants.add(team)

    # Team.events is the backward side of Event.participants (M2M) - "does this outer event
    # appear among some team's events" for each event.
    result = await Event.objects.annotate(has_team=Exists(Team.objects.filter(events=OuterRef("event_id")))).filter(
        has_team=True
    )
    assert [e.name for e in result] == ["E1"]
    assert other_event.name not in [e.name for e in result]


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_many_join_and_protect_relation_counts_each_parent_once(db):
    """On SQLite, DeleteQuery._execute() derived the returned count from the number of pks the
    matching queryset returned - and a filter through a to-many relation JOINs, yielding one row
    per matching child, so a parent with three children was counted three times although exactly
    one row was deleted (Postgres reports the driver's own affected-row count, which was right)."""
    crowded_parent = await BulkDeleteCountParent.objects.create(name="crowded")
    for item_index in range(3):
        await BulkDeleteCountItem.objects.create(name=f"item-{item_index}", parent=crowded_parent)
    single_parent = await BulkDeleteCountParent.objects.create(name="single")
    await BulkDeleteCountItem.objects.create(name="only-item", parent=single_parent)
    childless_parent = await BulkDeleteCountParent.objects.create(name="childless")

    deleted_count = await BulkDeleteCountParent.objects.filter(items__id__gt=0).delete()

    assert deleted_count == 2
    assert [parent.name async for parent in BulkDeleteCountParent.objects.all()] == ["childless"]
    assert await BulkDeleteCountItem.objects.all().count() == 0
    assert await BulkDeleteCountParent.objects.filter(pk=childless_parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_many_join_without_protect_relation_counts_each_parent_once(db):
    """Same as above for a model with no PROTECT relation, where the count came from
    COUNT(*) over the JOINed matching queryset instead of the pk list."""
    crowded_parent = await BulkDeleteCountUnguardedParent.objects.create(name="crowded")
    for item_index in range(2):
        await BulkDeleteCountUnguardedItem.objects.create(name=f"item-{item_index}", parent=crowded_parent)
    single_parent = await BulkDeleteCountUnguardedParent.objects.create(name="single")
    await BulkDeleteCountUnguardedItem.objects.create(name="only-item", parent=single_parent)
    await BulkDeleteCountUnguardedParent.objects.create(name="childless")

    deleted_count = await BulkDeleteCountUnguardedParent.objects.filter(items__id__gt=0).delete()

    assert deleted_count == 2
    assert [parent.name async for parent in BulkDeleteCountUnguardedParent.objects.all()] == ["childless"]
    assert await BulkDeleteCountUnguardedItem.objects.all().count() == 0


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_many_join_on_composite_pk_counts_each_parent_once(db):
    """Composite-PK variant of the pk-list branch - every pk is a tuple, which must be
    deduplicated the same way a scalar pk is."""
    crowded_parent = await BulkDeleteCountCompositeParent.objects.create(a=1, b=1, name="crowded")
    single_parent = await BulkDeleteCountCompositeParent.objects.create(a=1, b=2, name="single")
    await BulkDeleteCountCompositeParent.objects.create(a=2, b=1, name="childless")
    for peer_index in range(3):
        peer = await BulkDeleteCountCompositePeer.objects.create(name=f"peer-{peer_index}")
        await crowded_parent.peers.add(peer)
    await single_parent.peers.add(await BulkDeleteCountCompositePeer.objects.create(name="lonely-peer"))

    deleted_count = await BulkDeleteCountCompositeParent.objects.filter(peers__id__gt=0).delete()

    assert deleted_count == 2
    assert [parent.name async for parent in BulkDeleteCountCompositeParent.objects.all()] == ["childless"]


@pytest.mark.asyncio
async def test_bulk_delete_count_without_join_is_unchanged(db):
    """Plain filters (no JOIN) still report the exact number of deleted rows through both
    DeleteQuery._execute() branches."""
    for parent_name in ("a", "b", "c"):
        await BulkDeleteCountParent.objects.create(name=parent_name)
        await BulkDeleteCountUnguardedParent.objects.create(name=parent_name)

    assert await BulkDeleteCountParent.objects.filter(name__in=["a", "b"]).delete() == 2
    assert await BulkDeleteCountUnguardedParent.objects.filter(name__in=["a", "b"]).delete() == 2
    assert await BulkDeleteCountParent.objects.filter(name="missing").delete() == 0
    assert await BulkDeleteCountUnguardedParent.objects.filter(name="missing").delete() == 0


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_one_join_counts_each_child_row(db):
    """A filter through a forward (to-one) relation also JOINs but never multiplies rows - the
    count is still one per deleted child."""
    parent = await BulkDeleteCountUnguardedParent.objects.create(name="parent")
    for item_index in range(3):
        await BulkDeleteCountUnguardedItem.objects.create(name=f"item-{item_index}", parent=parent)

    deleted_count = await BulkDeleteCountUnguardedItem.objects.filter(parent__name="parent").delete()

    assert deleted_count == 3
    assert await BulkDeleteCountUnguardedItem.objects.all().count() == 0
    assert await BulkDeleteCountUnguardedParent.objects.filter(pk=parent.pk).exists()


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_many_join_and_limit_counts_distinct_parents(db):
    """LIMIT applies to the JOINed rows the delete's own pk subquery selects, so the count has to
    be the number of distinct parents among exactly those rows."""
    crowded_parent = await BulkDeleteCountUnguardedParent.objects.create(name="crowded")
    for item_index in range(3):
        await BulkDeleteCountUnguardedItem.objects.create(name=f"item-{item_index}", parent=crowded_parent)
    other_parent = await BulkDeleteCountUnguardedParent.objects.create(name="other")
    await BulkDeleteCountUnguardedItem.objects.create(name="other-item", parent=other_parent)

    deleted_count = (
        await BulkDeleteCountUnguardedParent.objects.filter(items__id__gt=0).order_by("id").limit(2).delete()
    )

    assert deleted_count == 1
    assert [parent.name async for parent in BulkDeleteCountUnguardedParent.objects.all()] == ["other"]


@pytest.mark.asyncio
async def test_bulk_delete_with_to_many_join_still_respects_protect_relation(db):
    """The pk list handed to the PROTECT check is deduplicated now - a guarded parent reached
    through a to-many JOIN filter must still block the whole delete."""
    guarded_parent = await BulkDeleteCountParent.objects.create(name="guarded")
    for item_index in range(2):
        await BulkDeleteCountItem.objects.create(name=f"item-{item_index}", parent=guarded_parent)
    await BulkDeleteCountGuard.objects.create(name="guard", parent=guarded_parent)

    with pytest.raises(ProtectedError):
        await BulkDeleteCountParent.objects.filter(items__id__gt=0).delete()

    assert await BulkDeleteCountParent.objects.filter(pk=guarded_parent.pk).exists()
    assert await BulkDeleteCountItem.objects.all().count() == 2


@pytest.mark.asyncio
async def test_bulk_delete_count_with_aggregate_annotation_filter_counts_each_parent_once(db):
    """An aggregate .annotate() filter JOINs the relation it aggregates over (grouped per parent),
    so it must report each matching parent once through both DeleteQuery._execute() branches."""
    for parent_model, item_model in (
        (BulkDeleteCountParent, BulkDeleteCountItem),
        (BulkDeleteCountUnguardedParent, BulkDeleteCountUnguardedItem),
    ):
        crowded_parent = await parent_model.objects.create(name="crowded")
        for item_index in range(3):
            await item_model.objects.create(name=f"item-{item_index}", parent=crowded_parent)
        sparse_parent = await parent_model.objects.create(name="sparse")
        await item_model.objects.create(name="only-item", parent=sparse_parent)

        deleted_count = (
            await parent_model.objects.annotate(item_count=Count("items")).filter(item_count__gte=2).delete()
        )

        assert deleted_count == 1
        assert [parent.name async for parent in parent_model.objects.all()] == ["sparse"]


@pytest.mark.asyncio
async def test_bulk_update_count_with_to_many_join_counts_each_parent_once(db):
    """UpdateQuery reports the driver's own affected-row count for its pk-subquery UPDATE, so a
    to-many JOIN filter never inflated it - pinned here so the delete-side fix isn't mistakenly
    mirrored into a regression."""
    crowded_parent = await BulkDeleteCountUnguardedParent.objects.create(name="crowded")
    for item_index in range(3):
        await BulkDeleteCountUnguardedItem.objects.create(name=f"item-{item_index}", parent=crowded_parent)

    updated_count = await BulkDeleteCountUnguardedParent.objects.filter(items__id__gt=0).update(name="renamed")

    assert updated_count == 1
    assert (await BulkDeleteCountUnguardedParent.objects.get(pk=crowded_parent.pk)).name == "renamed"


@pytest.mark.asyncio
async def test_bulk_delete_count_with_to_many_join_through_per_row_paths_counts_each_parent_once(db):
    """The per-row fallbacks (db_constraint=False relation, soft-delete model with a backward
    relation) load instances by pk__in, so a to-many JOIN filter's duplicate pks never reach the
    returned count - pinned as a regression guard alongside the direct-DELETE branches."""
    unconstrained_parent = await HardDeleteUnconstrainedParent.objects.create(name="unconstrained")
    for item_index in range(3):
        await HardDeleteUnconstrainedChildCascade.objects.create(
            name=f"child-{item_index}", parent=unconstrained_parent
        )
    soft_parent = await SoftDeleteParent.objects.create(name="soft")
    for item_index in range(3):
        await SoftDeleteChildCascadeSoft.objects.create(name=f"child-{item_index}", parent=soft_parent)

    assert await HardDeleteUnconstrainedParent.objects.filter(cascade_children__id__gt=0).delete() == 1
    assert await SoftDeleteParent.objects.filter(cascade_children__id__gt=0).delete() == 1
    assert not await HardDeleteUnconstrainedParent.objects.filter(pk=unconstrained_parent.pk).exists()
    assert await SoftDeleteParent.objects.all().count() == 0


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_distinct_on_two_hop_relation_path_resolves_correct_model(db):
    """resolve_distinct()'s DISTINCT ON walker used to resolve every hop past the first against
    self.model (the query's own base model) instead of related_model (the model actually
    reached so far) - a genuine multi-hop path (events__reporter__name, not just events__name)
    would look up "reporter" on Tournament (which doesn't have it) instead of on Event."""
    tournament = await Tournament.objects.create(name="T")
    reporter_a = await Reporter.objects.create(name="Alice")
    reporter_b = await Reporter.objects.create(name="Bob")
    await Event.objects.create(name="E1", tournament=tournament, reporter=reporter_a)
    await Event.objects.create(name="E2", tournament=tournament, reporter=reporter_a)
    await Event.objects.create(name="E3", tournament=tournament, reporter=reporter_b)

    rows = (
        await Tournament.objects.all()
        .order_by("events__reporter__name")
        .distinct("events__reporter__name")
        .values_list("events__reporter__name", flat=True)
    )
    assert list(rows) == ["Alice", "Bob"]


@pytest.mark.asyncio
async def test_count_group_by_non_aggregate_annotation_name_does_not_crash(db):
    """_get_group_bys() used to append a bare Term(field_name) for a group-by field that's
    an annotation name - Term is the abstract base class, whose get_sql() always raises
    NotImplementedError. Must render as a plain reference to the annotation's own SELECT alias
    instead (a Field with no table). Uses a non-aggregate annotation (Upper()) - grouping by an
    AGGREGATE's own alias (e.g. Count(...)) is itself invalid SQL, a different concern."""
    await Author.objects.create(name="alice")
    await Author.objects.create(name="Alice")
    await Author.objects.create(name="bob")

    # count() counts the instances `await qs` returns - one per row, whatever the group_by().
    queryset = Author.objects.annotate(upper_name=Upper("name")).group_by("upper_name")
    assert await queryset.count() == len(await queryset) == 3


@pytest.mark.asyncio
async def test_group_by_annotation_also_works_via_values(db):
    """Regression control: the same _get_group_bys() path via .values()/.values_list()
    (not just .count()) must keep working."""
    await Author.objects.create(name="alice")
    await Author.objects.create(name="Alice")

    rows = await Author.objects.annotate(upper_name=Upper("name")).group_by("upper_name").values("upper_name")
    assert rows == [{"upper_name": "ALICE"}]


# Known, narrower remaining gap - deliberately scoped OUT of this fix: when the group-by
# annotation's alias is NOT also present in the query's SELECT list (e.g. .values_list("name")
# excluding the "upper_name" annotation), a bare Field(field_name) reference isn't a real column
# or a resolvable alias. SQLite tolerates the resulting `GROUP BY "upper_name"` (its actual
# grouping semantics unverified) while Postgres correctly rejects it with UndefinedColumnError.
# Fully fixing this would need _get_group_bys() to embed the annotation's actual resolved
# expression instead of a bare alias reference, which needs a ExpressionContext/joins this method
# doesn't have today - out of scope for the crash this fix targets (Term(field_name)'s
# guaranteed-NotImplementedError on every dialect, for the common case where the annotation IS
# selected, covered by the two tests above). Not asserted here since the two dialects genuinely
# disagree - left as a documented gap for a future, separate fix.


@pytest_asyncio.fixture
async def books_by_author_data(db):
    """10 books, ids ascending, under three authors whose names order differently from ids."""
    authors = [await Author.objects.create(name=name) for name in ("c", "a", "b")]
    return [await Book.objects.create(name=str(index), author=authors[index % 3], rating=1.0) for index in range(10)]


ITERATOR_ORDERINGS = [("id",), ("author__name", "id")]

ITERATOR_WINDOWS = [
    pytest.param(lambda queryset, full: queryset.offset(2), lambda full: full[2:], 2, id="offset"),
    pytest.param(lambda queryset, full: queryset.limit(5), lambda full: full[:5], 2, id="limit"),
    pytest.param(lambda queryset, full: queryset.limit(4), lambda full: full[:4], 2, id="limit-exact-chunks"),
    pytest.param(lambda queryset, full: queryset.limit(0), lambda full: [], 2, id="limit-zero"),
    pytest.param(lambda queryset, full: queryset[2:7], lambda full: full[2:7], 100, id="slice-big-chunk"),
    pytest.param(lambda queryset, full: queryset[2:7], lambda full: full[2:7], 2, id="slice-small-chunk"),
    pytest.param(lambda queryset, full: queryset[3:], lambda full: full[3:], 3, id="open-slice"),
    pytest.param(lambda queryset, full: queryset.offset(20), lambda full: [], 2, id="offset-past-end"),
    pytest.param(
        lambda queryset, full: queryset.after_cursor(*queryset.cursor_values(full[2])).limit(3),
        lambda full: full[3:6],
        2,
        id="cursor-limit",
    ),
    pytest.param(
        lambda queryset, full: queryset.after_cursor(*queryset.cursor_values(full[2])).offset(1).limit(4),
        lambda full: full[4:8],
        2,
        id="cursor-offset-limit",
    ),
    pytest.param(
        lambda queryset, full: queryset.after_cursor(*queryset.cursor_values(full[2])),
        lambda full: full[3:],
        2,
        id="cursor",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering", ITERATOR_ORDERINGS)
@pytest.mark.parametrize(("build_window", "expected_slice", "chunk_size"), ITERATOR_WINDOWS)
async def test_iterator_honors_limit_and_offset(
    books_by_author_data, ordering, build_window, expected_slice, chunk_size
):
    """iterator() used to drop the queryset's own limit and re-apply its offset on every page."""
    base = Book.objects.all().select_related("author").order_by(*ordering)
    full = await base
    window = build_window(base, full)
    expected = [row.id for row in expected_slice(full)]
    assert [row.id async for row in window.iterator(chunk_size=chunk_size)] == expected
    # The same shapes run again with different values must not reuse the first run's page bounds.
    assert [row.id async for row in window.iterator(chunk_size=chunk_size)] == expected
    assert [row.id for row in await window] == expected


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
@pytest.mark.parametrize("ordering", ITERATOR_ORDERINGS)
@pytest.mark.parametrize(("build_window", "expected_slice", "chunk_size"), ITERATOR_WINDOWS)
async def test_stream_honors_limit_and_offset(
    books_by_author_data, ordering, build_window, expected_slice, chunk_size
):
    base = Book.objects.all().select_related("author").order_by(*ordering)
    full = await base
    window = build_window(base, full)
    async with Transactions.atomic():
        seen = [row.id async for row in window.stream(chunk_size=chunk_size)]
    assert seen == [row.id for row in expected_slice(full)]


@pytest.mark.asyncio
async def test_union_intersection_difference_with_a_none_branch_is_empty(db):
    """A .none() branch contributes no rows to a set operation instead of every row."""
    first = await Tournament.objects.create(name="T1")
    await Tournament.objects.create(name="T2")

    assert await Tournament.objects.none().union(Tournament.objects.filter(name="T1")) == [first]
    assert await Tournament.objects.filter(name="T1").union(Tournament.objects.none()) == [first]
    assert await Tournament.objects.all().intersection(Tournament.objects.none()) == []
    assert await Tournament.objects.none().difference(Tournament.objects.filter(name="T2")) == []
    assert set(await Tournament.objects.all().difference(Tournament.objects.none())) == set(
        await Tournament.objects.all()
    )
    assert await Tournament.objects.none().union(Tournament.objects.none()).count() == 0
    assert await Tournament.objects.none().union(Tournament.objects.filter(name="T1")).count() == 1
    assert [
        tournament.name
        for tournament in await Tournament.objects.none().union(Tournament.objects.all(), all=True).order_by("name")
    ] == [
        "T1",
        "T2",
    ]


@pytest.mark.asyncio
async def test_contains_through_a_joined_filter(db):
    """contains() qualifies its pk condition, so a filter joining another table isn't ambiguous."""
    tournament = await Tournament.objects.create(name="T1")
    other_tournament = await Tournament.objects.create(name="T2")
    team = await Team.objects.create(name="team")
    event = await Event.objects.create(name="E1", tournament=tournament)
    other_event = await Event.objects.create(name="E2", tournament=other_tournament)
    await event.participants.add(team)

    assert await Event.objects.filter(tournament__name="T1").contains(event) is True
    assert await Event.objects.filter(tournament__name="T1").contains(other_event) is False
    assert await Event.objects.filter(participants__name="team").contains(event) is True
    assert await Event.objects.filter(participants__name="team").contains(other_event) is False
    assert await Tournament.objects.filter(events__participants__name="team").contains(tournament) is True
    assert (
        await Tournament.objects.filter(events__name="E2").annotate(event_count=Count("events")).contains(tournament)
        is False
    )


@pytest.mark.asyncio
async def test_exists_respects_distinct_with_an_offset(db):
    """A sliced .distinct().exists() skips distinct rows, matching count(), not JOIN-multiplied ones."""
    tournament = await Tournament.objects.create(name="T1")
    event = await Event.objects.create(name="E1", tournament=tournament)
    for name in ("a", "b"):
        await event.participants.add(await Team.objects.create(name=name))
    joined = Event.objects.filter(participants__name__in=["a", "b"])

    assert await joined.offset(1).exists() is True
    assert await joined.distinct().offset(1).exists() is False
    assert await joined.distinct().offset(1).count() == 0
    assert await joined.distinct().offset(0).exists() is True
    assert await joined.distinct().limit(1).exists() is True
    assert await joined.distinct().exists() is True
    rows = await Tournament.objects.filter(name="T1").annotate(
        sliced_distinct=Exists(Event.objects.filter(participants__name__in=["a", "b"]).distinct().offset(1))
    )
    assert [row.sliced_distinct for row in rows] == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 100])
async def test_iterator_over_a_to_many_join_matches_awaiting(db, chunk_size):
    """Rows a to-many JOIN repeats aren't lost at a page boundary, whatever the chunk size."""
    tournament = await Tournament.objects.create(name="T1")
    teams = [await Team.objects.create(name=name) for name in ("a", "b", "c")]
    for index in range(3):
        event = await Event.objects.create(name=f"E{index}", tournament=tournament)
        await event.participants.add(*teams[: index + 1])
    joined = Event.objects.filter(participants__name__in=["a", "b", "c"]).order_by("event_id")
    distinct_joined = joined.distinct()
    annotated = Event.objects.all().annotate(participant_name=F("participants__name")).order_by("event_id")

    assert [event.name async for event in joined.iterator(chunk_size=chunk_size)] == [
        event.name for event in await joined
    ]
    assert [event.name async for event in distinct_joined.iterator(chunk_size=chunk_size)] == [
        event.name for event in await distinct_joined
    ]
    # Repeats of one event_id come back in no guaranteed order, so compare them as a multiset.
    assert sorted(
        [(event.name, event.participant_name) async for event in annotated.iterator(chunk_size=chunk_size)]
    ) == sorted((event.name, event.participant_name) for event in await annotated)


@pytest.mark.asyncio
async def test_limit_zero_update_and_delete_touch_no_rows(db):
    for value in range(3):
        await IntFields.objects.create(id=value + 1, intnum=value)
    assert await IntFields.objects.all().limit(0).update(intnum=100) == 0
    assert await IntFields.objects.filter(intnum__gte=0).limit(0).delete() == 0
    assert await IntFields.objects.all().order_by("id").values_list("intnum", flat=True) == [0, 1, 2]


@pytest.mark.asyncio
async def test_nested_set_operation_is_combined_as_one_branch(db):
    """a.union(b.intersection(c)) combines the nested set operation's whole result, like Django."""
    for name in ("T1", "T2", "T3", "T4"):
        await Tournament.objects.create(name=name)
    first_two = Tournament.objects.filter(name__in=["T1", "T2"])
    middle_two = Tournament.objects.filter(name__in=["T2", "T3"])
    last_two = Tournament.objects.filter(name__in=["T3", "T4"])

    def names(rows):
        return sorted(tournament.name for tournament in rows)

    assert names(await first_two.union(middle_two.intersection(last_two))) == ["T1", "T2", "T3"]
    assert names(await last_two.difference(first_two.union(middle_two))) == ["T4"]
    assert names(await first_two.union(middle_two).intersection(middle_two.union(last_two))) == ["T2", "T3"]
    assert await first_two.union(middle_two.intersection(last_two)).count() == 3
    assert [
        tournament.name for tournament in await first_two.union(last_two.difference(middle_two)).order_by("-name")
    ] == ["T4", "T2", "T1"]
    assert names(await first_two.union(middle_two.union(last_two).order_by("name").limit(1))) == ["T1", "T2"]


@pytest.mark.asyncio
async def test_set_operation_as_an_in_filter_value(db):
    """A union/intersection/difference works as an __in value, selecting its primary key."""
    for name in ("T1", "T2", "T3"):
        tournament = await Tournament.objects.create(name=name)
        await Event.objects.create(name=f"E{name}", tournament=tournament)
    first = Tournament.objects.filter(name="T1")
    third = Tournament.objects.filter(name="T3")

    assert await Tournament.objects.filter(pk__in=first.union(third)).order_by("name").values_list(
        "name", flat=True
    ) == [
        "T1",
        "T3",
    ]
    assert await Tournament.objects.exclude(id__in=first.union(third)).values_list("name", flat=True) == ["T2"]
    assert await Event.objects.filter(tournament__in=first.union(third)).order_by("name").values_list(
        "name", flat=True
    ) == [
        "ET1",
        "ET3",
    ]
    assert await Tournament.objects.filter(pk__in=first.union(third).order_by("name").limit(1)).values_list(
        "name", flat=True
    ) == ["T1"]
