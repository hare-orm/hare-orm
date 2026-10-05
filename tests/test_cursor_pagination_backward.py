import re

import pytest
import pytest_asyncio

from hare.contrib import test as hare_test
from hare.exceptions import FieldError, QueryError
from hare.query.expressions import F
from hare.query.functions import Count, Sum
from hare.transactions.transactions import Transactions
from tests.testmodels import DocumentRevisionNote, Employee, Event, IntFields, Tournament, VersionedDocument


@pytest_asyncio.fixture
async def intfields_data(db):
    """intnum 0..9, intnum_null cycling {0, 1}."""
    return [await IntFields.objects.create(intnum=number, intnum_null=number % 2) for number in range(10)]


@pytest_asyncio.fixture
async def intfields_with_nulls_data(db):
    """intnum 1..7, intnum_null 1, NULL, 2, 3, NULL, 3, NULL - duplicates and NULLs."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None), (6, 3), (7, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)


async def walk_forward(base, page_size: int) -> list[list[int]]:
    pages: list[list[int]] = []
    queryset = base
    while True:
        page = await queryset.limit(page_size)
        if not page:
            return pages
        pages.append([row.intnum for row in page])
        queryset = base.after_cursor(*base.cursor_values(page[-1]))


async def walk_backward_from_last_page(base, last_page_first_row, page_size: int) -> list[list[int]]:
    """Pages strictly before the last forward page's first row, walking back to the start."""
    pages: list[list[int]] = []
    cursor = base.cursor_values(last_page_first_row)
    while True:
        page = await base.before_cursor(*cursor).limit(page_size)
        if not page:
            return pages
        pages.append([row.intnum for row in page])
        cursor = base.cursor_values(page[0])


async def assert_forward_and_backward_pages_match(base, page_size: int) -> None:
    full = [row.intnum for row in await base]
    forward_pages = await walk_forward(base, page_size)
    assert [intnum for page in forward_pages for intnum in page] == full

    last_page_first_row = await IntFields.objects.get(intnum=forward_pages[-1][0])
    backward_pages = await walk_backward_from_last_page(base, last_page_first_row, page_size)
    # Every forward page but the (possibly short) last one is a full page_size page, walking
    # back from the start of the last one - the same boundaries in reverse.
    assert backward_pages[::-1] == forward_pages[:-1]


ORDERINGS = [
    pytest.param(lambda: ("intnum",), id="asc"),
    pytest.param(lambda: ("-intnum",), id="desc"),
    pytest.param(lambda: ("intnum_null", "-intnum"), id="composite-mixed"),
    pytest.param(lambda: ("-intnum_null", "intnum"), id="composite-mixed-desc-first"),
]


@pytest.mark.parametrize("ordering_factory", ORDERINGS)
@pytest.mark.parametrize("page_size", [1, 3, 4])
@pytest.mark.asyncio
async def test_forward_and_backward_walks_give_identical_pages(intfields_data, ordering_factory, page_size):
    await assert_forward_and_backward_pages_match(IntFields.objects.all().order_by(*ordering_factory()), page_size)


NULL_ORDERINGS = [
    pytest.param(lambda: ("intnum_null", "intnum"), id="asc-dialect-default"),
    pytest.param(lambda: ("-intnum_null", "intnum"), id="desc-dialect-default"),
    pytest.param(lambda: ("intnum_null", "-intnum"), id="asc-dialect-default-desc-tiebreak"),
    pytest.param(lambda: (F("intnum_null").asc(nulls_first=True), "intnum"), id="asc-nulls-first"),
    pytest.param(lambda: (F("intnum_null").asc(nulls_last=True), "intnum"), id="asc-nulls-last"),
    pytest.param(lambda: (F("intnum_null").desc(nulls_first=True), "intnum"), id="desc-nulls-first"),
    pytest.param(lambda: (F("intnum_null").desc(nulls_last=True), "-intnum"), id="desc-nulls-last"),
]


@pytest.mark.parametrize("ordering_factory", NULL_ORDERINGS)
@pytest.mark.parametrize("page_size", [1, 2, 3])
@pytest.mark.asyncio
async def test_forward_and_backward_walks_match_with_nulls(intfields_with_nulls_data, ordering_factory, page_size):
    await assert_forward_and_backward_pages_match(IntFields.objects.all().order_by(*ordering_factory()), page_size)


@pytest.mark.parametrize("ordering_factory", NULL_ORDERINGS)
@pytest.mark.asyncio
async def test_before_cursor_on_every_row_returns_everything_before_it(intfields_with_nulls_data, ordering_factory):
    """NULL and non-NULL boundary values alike, on the dialect's own default NULL placement too."""
    base = IntFields.objects.all().order_by(*ordering_factory())
    full_rows = await base
    full = [row.intnum for row in full_rows]
    for position, row in enumerate(full_rows):
        before = await base.before_cursor(*base.cursor_values(row))
        assert [item.intnum for item in before] == full[:position]
        after = await base.after_cursor(*base.cursor_values(row))
        assert [item.intnum for item in after] == full[position + 1 :]


@pytest.mark.asyncio
async def test_before_cursor_limit_takes_the_rows_closest_to_the_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert [row.intnum for row in await base.before_cursor(7).limit(3)] == [4, 5, 6]
    assert [row.intnum for row in await base.before_cursor(2).limit(3)] == [0, 1]
    assert await base.before_cursor(0).limit(3) == []
    assert [row.intnum for row in await base.before_cursor(7).limit(3).offset(1)] == [3, 4, 5]


@pytest.mark.asyncio
async def test_before_cursor_values_and_values_list_keep_the_original_order(intfields_data):
    base = IntFields.objects.all().order_by("-intnum")
    assert await base.before_cursor(3).limit(3).values_list("intnum", flat=True) == [6, 5, 4]
    assert await base.before_cursor(3).limit(2).values_list("intnum", "intnum_null") == [(5, 1), (4, 0)]
    assert await base.before_cursor(3).limit(2).values("intnum") == [{"intnum": 5}, {"intnum": 4}]


@pytest.mark.asyncio
async def test_before_cursor_first_is_the_row_right_before_the_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert (await base.before_cursor(5).first()).intnum == 4
    assert await base.before_cursor(5).first().values_list("intnum", flat=True) == 4


@pytest.mark.asyncio
async def test_after_and_before_cursor_window_in_either_order(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    for window in (base.after_cursor(2).before_cursor(7), base.before_cursor(7).after_cursor(2)):
        assert [row.intnum for row in await window] == [3, 4, 5, 6]
        assert [row.intnum for row in await window.limit(2)] == [5, 6]
        assert await window.count() == 4
        assert await window.values_list("intnum", flat=True) == [3, 4, 5, 6]
    assert await base.after_cursor(5).before_cursor(3) == []


@pytest.mark.asyncio
async def test_repeated_cursor_call_replaces_its_boundary(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert [row.intnum for row in await base.before_cursor(7).before_cursor(3)] == [0, 1, 2]
    assert [row.intnum for row in await base.after_cursor(7).after_cursor(5)] == [6, 7, 8, 9]
    assert [row.intnum for row in await base.after_cursor(1).before_cursor(8).after_cursor(4)] == [5, 6, 7]


@pytest.mark.asyncio
async def test_same_base_queryset_with_changing_cursor_values(intfields_data):
    """Every call below shares one query shape per direction - the query-shape cache must
    rebind the cursor values, never reuse the first call's."""
    base = IntFields.objects.all().order_by("intnum_null", "-intnum")
    full_rows = await base
    full = [row.intnum for row in full_rows]
    for __ in range(2):
        for position, row in enumerate(full_rows):
            cursor = base.cursor_values(row)
            assert [item.intnum for item in await base.before_cursor(*cursor).limit(2)] == full[
                max(position - 2, 0) : position
            ]
            assert [item.intnum for item in await base.after_cursor(*cursor).limit(2)] == full[
                position + 1 : position + 3
            ]
            assert await base.before_cursor(*cursor).values_list("intnum", flat=True) == full[:position]
            assert await base.before_cursor(*cursor).count() == position
        assert [row.intnum for row in await base] == full


@pytest.mark.asyncio
async def test_before_cursor_requires_order_by(intfields_data):
    with pytest.raises(ValueError, match=re.escape(".before_cursor() requires .order_by()")):
        IntFields.objects.all().before_cursor(5)


@pytest.mark.asyncio
async def test_before_cursor_value_count_mismatch(intfields_data):
    with pytest.raises(ValueError, match=re.escape(".before_cursor() expects 2 value(s)")):
        IntFields.objects.all().order_by("intnum_null", "intnum").before_cursor(1)
    with pytest.raises(ValueError, match=re.escape(".after_cursor() expects 2 value(s)")):
        IntFields.objects.all().order_by("intnum_null", "intnum").before_cursor(1, 2).after_cursor(1)


@pytest.mark.asyncio
async def test_before_cursor_rejects_annotation_ordering(db):
    with pytest.raises(FieldError, match="annotation"):
        Tournament.objects.annotate(event_count=Count("events")).order_by("event_count").before_cursor(1)
    tournament = await Tournament.objects.create(name="A")
    with pytest.raises(FieldError, match="annotation"):
        Tournament.objects.annotate(event_count=Count("events")).order_by("event_count").cursor_values(tournament)


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["order_by", "last", "latest", "earliest"])
async def test_reordering_methods_after_before_cursor_raise(intfields_data, method_name):
    queryset = IntFields.objects.all().order_by("intnum").before_cursor(5)
    with pytest.raises(ValueError, match=re.escape(f".{method_name}() cannot be called after .before_cursor()")):
        if method_name == "last":
            queryset.last()
        else:
            getattr(queryset, method_name)("intnum")


@pytest.mark.asyncio
async def test_iterator_rejects_before_cursor(intfields_data):
    with pytest.raises(QueryError, match="before_cursor"):
        async for __ in IntFields.objects.all().order_by("intnum").before_cursor(5).iterator(2):
            pass


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_stream_rejects_before_cursor(intfields_data):
    async with Transactions.atomic():
        with pytest.raises(QueryError, match="before_cursor"):
            async for __ in IntFields.objects.all().order_by("intnum").before_cursor(5).stream():
                pass


# ============================================================================
# cursor_values()
# ============================================================================


@pytest.mark.asyncio
async def test_cursor_values_of_the_last_row_is_the_next_page_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum_null", "-intnum")
    page1 = await base.limit(4)
    assert base.cursor_values(page1[-1]) == (page1[-1].intnum_null, page1[-1].intnum)
    page2 = await base.after_cursor(*base.cursor_values(page1[-1])).limit(4)
    assert [row.intnum for row in page1 + page2] == [row.intnum for row in await base.limit(8)]
    previous = await base.before_cursor(*base.cursor_values(page2[0])).limit(4)
    assert [row.intnum for row in previous] == [row.intnum for row in page1]


@pytest.mark.asyncio
async def test_cursor_values_pk_ordering(intfields_data):
    base = IntFields.objects.all().order_by("-pk")
    row = intfields_data[4]
    assert base.cursor_values(row) == (row.pk,)
    assert [item.intnum for item in await base.before_cursor(*base.cursor_values(row))] == [9, 8, 7, 6, 5]


@pytest.mark.asyncio
async def test_cursor_values_requires_order_by_and_model_instance(intfields_data):
    with pytest.raises(ValueError, match=re.escape(".cursor_values() requires .order_by()")):
        IntFields.objects.all().cursor_values(intfields_data[0])
    tournament = await Tournament.objects.create(name="T")
    with pytest.raises(QueryError):
        IntFields.objects.all().order_by("intnum").cursor_values(tournament)


@pytest.mark.asyncio
async def test_cursor_values_related_field_ordering(db):
    tournaments = [await Tournament.objects.create(name=f"T{number}") for number in range(5)]
    for number, tournament in enumerate(tournaments):
        await Event.objects.create(name=f"E{number}", tournament=tournament)
    base = Event.objects.all().select_related("tournament").order_by("tournament__name", "name")
    rows = await base
    assert base.cursor_values(rows[2]) == ("T2", "E2")
    assert [event.name for event in await base.before_cursor(*base.cursor_values(rows[2]))] == ["E0", "E1"]
    assert [event.name for event in await base.after_cursor(*base.cursor_values(rows[2]))] == ["E3", "E4"]

    unloaded = await Event.objects.get(name="E2")
    with pytest.raises(ValueError, match="isn't loaded"):
        base.cursor_values(unloaded)


@pytest.mark.asyncio
async def test_cursor_values_through_a_null_relation_is_none(db):
    await Employee.objects.create(name="CEO")
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker", manager=boss)
    base = Employee.objects.all().select_related("manager").order_by(F("manager__name").asc(nulls_last=True), "name")
    rows = await base
    assert [employee.name for employee in rows] == ["Worker", "Boss", "CEO"]
    assert base.cursor_values(rows[1]) == (None, "Boss")
    assert [employee.name for employee in await base.before_cursor(*base.cursor_values(rows[2]))] == [
        "Worker",
        "Boss",
    ]
    assert [employee.name for employee in await base.before_cursor(*base.cursor_values(rows[1]))] == ["Worker"]


@pytest.mark.asyncio
async def test_cursor_values_rejects_a_to_many_relation(db):
    tournament = await Tournament.objects.create(name="T")
    await Event.objects.create(name="E", tournament=tournament)
    with pytest.raises(FieldError, match="to-many"):
        Tournament.objects.all().order_by("events__name").cursor_values(tournament)


# ============================================================================
# count()/exists()/aggregate()/contains()/update()/delete() built from a cursor-paged queryset
# used to drop the keyset boundary entirely and act on every row the filters matched.
# ============================================================================


@pytest.mark.asyncio
async def test_count_and_exists_honor_the_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert await base.after_cursor(5).count() == 4
    assert await base.before_cursor(5).count() == 5
    assert not await base.after_cursor(9).exists()
    assert not await base.before_cursor(0).exists()
    assert await base.before_cursor(1).exists()


@pytest.mark.asyncio
async def test_aggregate_and_contains_honor_the_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert await base.after_cursor(6).aggregate(total=Sum("intnum")) == {"total": 7 + 8 + 9}
    assert await base.before_cursor(3).aggregate(total=Sum("intnum")) == {"total": 0 + 1 + 2}
    assert await base.after_cursor(6).contains(intfields_data[8])
    assert not await base.after_cursor(6).contains(intfields_data[2])


@pytest.mark.asyncio
async def test_update_and_delete_honor_the_cursor(intfields_data):
    base = IntFields.objects.all().order_by("intnum")
    assert await base.after_cursor(7).update(intnum_null=100) == 2
    assert await IntFields.objects.filter(intnum_null=100).values_list("intnum", flat=True) == [8, 9]
    assert await base.before_cursor(5).limit(2).update(intnum_null=200) == 2
    assert sorted(await IntFields.objects.filter(intnum_null=200).values_list("intnum", flat=True)) == [3, 4]
    assert await base.before_cursor(2).delete() == 2
    assert sorted(await IntFields.objects.all().values_list("intnum", flat=True)) == [2, 3, 4, 5, 6, 7, 8, 9]


@pytest_asyncio.fixture
async def employee_chain_data(db):
    """Two bosses, middles under them, workers under middles - plus workers with no manager and
    workers whose manager has no manager, so "manager__manager" meets NULL at both hops."""
    first_boss = await Employee.objects.create(name="boss-1")
    second_boss = await Employee.objects.create(name="boss-2")
    first_middle = await Employee.objects.create(name="middle-1", manager=second_boss)
    second_middle = await Employee.objects.create(name="middle-2", manager=first_boss)
    for index in range(3):
        await Employee.objects.create(name=f"worker-a{index}", manager=first_middle)
        await Employee.objects.create(name=f"worker-b{index}", manager=second_middle)
        await Employee.objects.create(name=f"worker-c{index}", manager=first_boss)
    return first_boss, second_boss


@pytest.mark.asyncio
async def test_order_by_nested_fk_orders_by_its_key_column(db):
    assert [field_name for field_name, __ in Employee.objects.all().order_by("manager__manager", "id")._orderings] == [
        "manager__manager_id",
        "id",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("ordering", ["manager__manager", "-manager__manager"])
async def test_cursor_values_nested_fk_returns_key_and_pages_through(employee_chain_data, ordering):
    """cursor_values() on a nested FK ordering used to return the related object (or a
    NoneAwaitableType for a NULL relation) instead of its key, which after_cursor() then rejected."""
    base = Employee.objects.all().select_related("manager").order_by(ordering, "id")
    full = [row.id for row in await base]

    for row in await base:
        manager_manager_id, row_id = base.cursor_values(row)
        assert manager_manager_id is None or isinstance(manager_manager_id, int)
        assert row_id == row.id

    seen: list[int] = []
    queryset = base
    while True:
        page = await queryset.limit(4)
        if not page:
            break
        seen.extend(row.id for row in page)
        queryset = base.after_cursor(*base.cursor_values(page[-1]))
    assert seen == full

    backward_pages: list[list[int]] = []
    cursor = base.cursor_values(page_last := (await base)[-1])
    backward_pages.append([page_last.id])
    while True:
        page = await base.before_cursor(*cursor).limit(4)
        if not page:
            break
        backward_pages.insert(0, [row.id for row in page])
        cursor = base.cursor_values(page[0])
    assert [row_id for page in backward_pages for row_id in page] == full


@pytest.mark.asyncio
async def test_cursor_values_nested_fk_null_relation_is_none(employee_chain_data):
    base = Employee.objects.all().select_related("manager").order_by("manager__manager", "id")
    first_boss, __ = employee_chain_data
    assert base.cursor_values(await Employee.objects.get(id=first_boss.id).select_related("manager")) == (
        None,
        first_boss.id,
    )
    worker = await Employee.objects.get(name="worker-c0").select_related("manager")
    assert base.cursor_values(worker) == (None, worker.id)


@pytest.mark.asyncio
async def test_iterator_nested_fk_ordering_matches_plain_query(employee_chain_data):
    base = Employee.objects.all().order_by("manager__manager", "id")
    full = [row.id for row in await base]
    assert [row.id async for row in base.iterator(chunk_size=3)] == full
    assert [row.id async for row in base[2:9].iterator(chunk_size=3)] == full[2:9]


@pytest.mark.asyncio
async def test_order_by_nested_fk_to_composite_key_orders_by_every_key_column(db):
    queryset = VersionedDocument.objects.all().order_by("revision_notes__document", "id")
    assert [field_name for field_name, __ in queryset._orderings] == [
        "revision_notes__document_id",
        "revision_notes__document_version",
        "id",
    ]
    document = await VersionedDocument.objects.create(title="v1")
    await DocumentRevisionNote.objects.create(document=document, note="n")
    assert [row.id for row in await queryset] == [document.id]
