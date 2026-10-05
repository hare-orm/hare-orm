import re

import pytest
import pytest_asyncio

from hare.contrib import test as hare_test
from hare.exceptions import FieldError
from hare.query.expressions import F
from hare.query.functions import Count
from tests.testmodels import CompositePkThing, Employee, Event, IntFields, SoftDeleteStandalone, Tournament


@pytest_asyncio.fixture
async def intfields_data(db):
    """10 rows: intnum 0..9, intnum_null cycling through {0, 1} - two values share intnum_null
    for every pair, giving a real composite-ordering case."""
    return [await IntFields.objects.create(intnum=i, intnum_null=i % 2) for i in range(10)]


@pytest.mark.asyncio
async def test_after_cursor_single_column_ascending(db, intfields_data):
    page1 = await IntFields.objects.all().order_by("intnum").limit(3)
    assert [row.intnum for row in page1] == [0, 1, 2]

    page2 = await IntFields.objects.all().order_by("intnum").after_cursor(page1[-1].intnum).limit(3)
    assert [row.intnum for row in page2] == [3, 4, 5]


@pytest.mark.asyncio
async def test_after_cursor_single_column_descending(db, intfields_data):
    page1 = await IntFields.objects.all().order_by("-intnum").limit(3)
    assert [row.intnum for row in page1] == [9, 8, 7]

    page2 = await IntFields.objects.all().order_by("-intnum").after_cursor(page1[-1].intnum).limit(3)
    assert [row.intnum for row in page2] == [6, 5, 4]


@pytest.mark.asyncio
async def test_after_cursor_composite_mixed_directions(db, intfields_data):
    """intnum_null ASC, intnum DESC - a row-value tuple comparison (a,b) > (v1,v2) would silently
    give wrong results here since it assumes uniform direction across all columns."""
    full = await IntFields.objects.all().order_by("intnum_null", "-intnum")
    full_order = [(row.intnum_null, row.intnum) for row in full]

    page1 = await IntFields.objects.all().order_by("intnum_null", "-intnum").limit(4)
    last = page1[-1]
    page2 = (
        await IntFields.objects.all()
        .order_by("intnum_null", "-intnum")
        .after_cursor(last.intnum_null, last.intnum)
        .limit(4)
    )

    combined = [(row.intnum_null, row.intnum) for row in page1] + [(row.intnum_null, row.intnum) for row in page2]
    assert combined == full_order[:8]


@pytest.mark.asyncio
async def test_after_cursor_reaches_end_with_no_gaps_or_duplicates(db, intfields_data):
    """Paging all the way through with a small page size must reproduce the exact same full
    ordered set as a single unpaginated query - no row skipped, none duplicated."""
    full = [row.intnum for row in await IntFields.objects.all().order_by("intnum")]

    collected: list[int] = []
    cursor: tuple[int, ...] = ()
    while True:
        query = IntFields.objects.all().order_by("intnum")
        if cursor:
            query = query.after_cursor(*cursor)
        page = await query.limit(3)
        if not page:
            break
        collected.extend(row.intnum for row in page)
        cursor = (page[-1].intnum,)

    assert collected == full


@pytest.mark.asyncio
async def test_after_cursor_past_last_row_is_empty(db, intfields_data):
    page = await IntFields.objects.all().order_by("intnum").after_cursor(9).limit(3)
    assert page == []


@pytest.mark.asyncio
async def test_after_cursor_requires_order_by(db, intfields_data):
    with pytest.raises(ValueError, match="requires .order_by"):
        IntFields.objects.all().after_cursor(5)


@pytest.mark.asyncio
async def test_after_cursor_value_count_mismatch(db, intfields_data):
    with pytest.raises(ValueError, match="expects 2 value"):
        IntFields.objects.all().order_by("intnum_null", "intnum").after_cursor(1)


@pytest.mark.asyncio
async def test_after_cursor_with_related_field_ordering(db):
    """.after_cursor() supports ordering by a related (``related__field``) field - it resolves
    through the same LookupPaths machinery order_by()/annotate() already use, joins included
    (see _get_cursor_criterion() in hare/query/queryset/awaitable.py)."""
    tournament_a = await Tournament.objects.create(name="A", desc="D")
    tournament_b = await Tournament.objects.create(name="B", desc="D")
    tournament_c = await Tournament.objects.create(name="C", desc="D")
    await Event.objects.create(name="E1", tournament=tournament_a)
    await Event.objects.create(name="E2", tournament=tournament_b)
    await Event.objects.create(name="E3", tournament=tournament_c)

    full = await Event.objects.all().order_by("tournament__name")
    full_order = [e.name for e in full]
    assert full_order == ["E1", "E2", "E3"]

    page1 = await Event.objects.all().order_by("tournament__name").limit(2)
    assert [e.name for e in page1] == ["E1", "E2"]

    page2 = await Event.objects.all().order_by("tournament__name").after_cursor("B").limit(2)
    assert [e.name for e in page2] == ["E3"]

    combined = [e.name for e in page1] + [e.name for e in page2]
    assert combined == full_order


@pytest.mark.asyncio
async def test_after_cursor_with_related_field_ordering_paginates_full_set_no_gaps_or_dupes(db):
    """Paging all the way through with a related-field ordering must reproduce the exact same
    full ordered set as a single unpaginated query - no row skipped, none duplicated."""
    tournaments = [await Tournament.objects.create(name=f"T{i}", desc="D") for i in range(5)]
    for i, tournament in enumerate(tournaments):
        await Event.objects.create(name=f"E{i}", tournament=tournament)

    full = [row.name for row in await Event.objects.all().order_by("tournament__name")]

    collected: list[str] = []
    cursor: tuple[str, ...] = ()
    while True:
        query = Event.objects.all().select_related("tournament").order_by("tournament__name")
        if cursor:
            query = query.after_cursor(*cursor)
        page = await query.limit(2)
        if not page:
            break
        collected.extend(row.name for row in page)
        cursor = (page[-1].tournament.name,)

    assert collected == full


@pytest.mark.asyncio
async def test_after_cursor_with_related_field_ordering_handles_null_boundary_through_nullable_relation(db):
    """A NULL cursor boundary for a related-field ordering (e.g. "manager__name") is legitimate
    whenever the RELATION itself is nullable, even though the target field (Employee.name) isn't
    nullable - the NULL comes from the LEFT JOIN producing no row for employees with no manager,
    not from the target column accepting NULL. This used to route the None boundary through the
    target field's own to_db_value()/validate(), which raised ValidationError since Employee.name
    itself has null=False - crashing on a boundary the framework itself just produced."""
    # Doesn't hardcode the full ordering itself: SQLite defaults NULLS FIRST for ASC, Postgres
    # NULLS LAST - where "Boss"/"CEO" (both manager_name=NULL) land relative to "Worker"
    # (manager_name="Boss") differs by dialect. What matters here is the NULL cursor VALUE itself
    # (the exact thing that used to crash), not where its row happens to sort.
    await Employee.objects.create(name="CEO")
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker", manager=boss)

    full_names = [row.name for row in await Employee.objects.all().order_by("manager__name", "name")]
    expected_page = full_names[full_names.index("Boss") + 1 :]

    page = await Employee.objects.all().order_by("manager__name", "name").after_cursor(None, "Boss").limit(10)
    assert [row.name for row in page] == expected_page


@pytest.mark.asyncio
async def test_after_cursor_rejects_annotation(db):
    tournament = await Tournament.objects.create(name="A", desc="D")
    await Event.objects.create(name="Event 1", tournament=tournament)

    with pytest.raises(FieldError, match="annotation"):
        Tournament.objects.annotate(event_count=Count("events")).order_by("event_count").after_cursor(1)


@pytest.mark.asyncio
async def test_after_cursor_combined_with_filter(db, intfields_data):
    page = await IntFields.objects.all().filter(intnum_null=0).order_by("intnum").after_cursor(2).limit(10)
    assert [row.intnum for row in page] == [4, 6, 8]


@pytest.mark.asyncio
async def test_after_cursor_reused_queryset_is_not_mutated(db, intfields_data):
    """.after_cursor() clones like every other QuerySet method - reusing the base queryset for a
    second page must not carry the first page's cursor along."""
    base = IntFields.objects.all().order_by("intnum")
    page1 = await base.limit(3)
    page_without_cursor = await base.limit(3)
    assert [row.intnum for row in page1] == [row.intnum for row in page_without_cursor]


# ============================================================================
# .values()/.values_list() - ValuesQuery/ValuesListQuery (FieldSelectQuery) never carried
# _cursor_values at all, so .after_cursor() silently had NO effect when the query ended in
# .values()/.values_list() instead of plain iteration - every row past the cursor came back again,
# not just the ones strictly after it. The exact bug class keyset pagination exists to prevent.
# Confirmed empirically before fixing: it returned the boundary row and everything after it.
# ============================================================================


@pytest.mark.asyncio
async def test_after_cursor_applies_to_values(db, intfields_data):
    page1 = await IntFields.objects.all().order_by("intnum").limit(3).values("intnum")
    assert [row["intnum"] for row in page1] == [0, 1, 2]

    page2 = await IntFields.objects.all().order_by("intnum").after_cursor(2).values("intnum")
    assert [row["intnum"] for row in page2][:3] == [3, 4, 5]


@pytest.mark.asyncio
async def test_after_cursor_applies_to_values_list(db, intfields_data):
    page2 = await IntFields.objects.all().order_by("intnum").after_cursor(2).values_list("intnum", flat=True)
    assert list(page2)[:3] == [3, 4, 5]


# ============================================================================
# Further combinations: select_related, prefetch_related, soft_delete_field, composite PK,
# distinct/distinct_on, limit
# ============================================================================


@pytest_asyncio.fixture
async def events_data(db):
    tournament = await Tournament.objects.create(name="T1")
    return tournament, [await Event.objects.create(name=f"E{i}", tournament=tournament) for i in range(1, 4)]


@pytest.mark.asyncio
async def test_after_cursor_with_select_related(db, events_data):
    rows = await Event.objects.all().select_related("tournament").order_by("name").after_cursor("E1")
    assert [(row.name, row.tournament.name) for row in rows] == [("E2", "T1"), ("E3", "T1")]


@pytest.mark.asyncio
async def test_after_cursor_with_prefetch_related(db, events_data):
    rows = await Event.objects.all().prefetch_related("tournament").order_by("name").after_cursor("E1")
    assert [(row.name, row.tournament.name) for row in rows] == [("E2", "T1"), ("E3", "T1")]


@pytest.mark.asyncio
async def test_after_cursor_respects_soft_delete_auto_filter(db):
    await SoftDeleteStandalone.objects.create(name="A")
    b = await SoftDeleteStandalone.objects.create(name="B")
    await SoftDeleteStandalone.objects.create(name="C")
    await b.delete()

    rows = await SoftDeleteStandalone.objects.all().order_by("name").after_cursor("A")

    assert [row.name for row in rows] == ["C"]


@pytest.mark.asyncio
async def test_after_cursor_with_composite_pk_ordering(db):
    await CompositePkThing.objects.create(thing_id=1, revision=1, name="A")
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="B")
    await CompositePkThing.objects.create(thing_id=2, revision=1, name="C")

    rows = await CompositePkThing.objects.all().order_by("thing_id", "revision").after_cursor(1, 1)

    assert [(row.thing_id, row.revision) for row in rows] == [(1, 2), (2, 1)]


@pytest.mark.asyncio
async def test_after_cursor_with_distinct(db, events_data):
    rows = await Event.objects.all().order_by("name").after_cursor("E1").distinct()
    assert [row.name for row in rows] == ["E2", "E3"]


@hare_test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_after_cursor_with_distinct_on(db):
    await IntFields.objects.create(intnum=10, intnum_null=0)
    await IntFields.objects.create(intnum=11, intnum_null=0)
    await IntFields.objects.create(intnum=20, intnum_null=1)

    queryset = IntFields.objects.all().distinct("intnum_null").order_by("intnum_null", "intnum")
    assert [(row.intnum_null, row.intnum) for row in await queryset] == [(0, 10), (1, 20)]

    # The boundary applies to the rows DISTINCT ON picks - (0, 11) is never one of them.
    rows = await queryset.after_cursor(0, 10)
    assert [(row.intnum_null, row.intnum) for row in rows] == [(1, 20)]


@pytest.mark.asyncio
async def test_after_cursor_with_limit_exact_count(db, intfields_data):
    page = await IntFields.objects.all().order_by("intnum").after_cursor(2).limit(2)
    assert [row.intnum for row in page] == [3, 4]


# ============================================================================
# Nullable ordering column - a plain `term > NULL`/`term < NULL` comparison is UNKNOWN (never
# TRUE) under SQL's three-valued logic, so pagination used to stop early with no error and no
# signal, silently dropping rows. Manifested differently per dialect since hare never emits an
# explicit NULLS FIRST/LAST anywhere and SQLite/Postgres disagree on where NULLs sort by default
# (SQLite: NULLS FIRST for ASC, NULLS LAST for DESC; Postgres: the opposite).
# ============================================================================


@pytest_asyncio.fixture
async def intfields_with_nulls_data(db):
    """3 rows with intnum_null=NULL, plus intnum_null 10/20/30 - the exact shape that used to
    lose rows: SQLite returned 2 of 6, Postgres returned 3 of 6, when paging by 2."""
    for i in range(3):
        await IntFields.objects.create(intnum=i, intnum_null=None)
    for value in (10, 20, 30):
        await IntFields.objects.create(intnum=value, intnum_null=value)


@pytest.mark.asyncio
async def test_after_cursor_nullable_ordering_column_ascending_no_gaps(db, intfields_with_nulls_data):
    # "id" as a tiebreaker gives the 3 NULL rows (otherwise unordered relative to each other) a
    # deterministic position to page through.
    full = [(row.intnum_null, row.id) for row in await IntFields.objects.all().order_by("intnum_null", "id")]
    assert len(full) == 6

    collected: list[tuple[int | None, int]] = []
    cursor: tuple | None = None
    while True:
        query = IntFields.objects.all().order_by("intnum_null", "id")
        if cursor is not None:
            query = query.after_cursor(*cursor)
        page = await query.limit(2)
        if not page:
            break
        collected.extend((row.intnum_null, row.id) for row in page)
        cursor = (page[-1].intnum_null, page[-1].id)

    assert collected == full


@pytest.mark.asyncio
async def test_after_cursor_nullable_ordering_column_descending_no_gaps(db, intfields_with_nulls_data):
    full = [(row.intnum_null, row.id) for row in await IntFields.objects.all().order_by("-intnum_null", "-id")]
    assert len(full) == 6

    collected: list[tuple[int | None, int]] = []
    cursor: tuple | None = None
    while True:
        query = IntFields.objects.all().order_by("-intnum_null", "-id")
        if cursor is not None:
            query = query.after_cursor(*cursor)
        page = await query.limit(2)
        if not page:
            break
        collected.extend((row.intnum_null, row.id) for row in page)
        cursor = (page[-1].intnum_null, page[-1].id)

    assert collected == full


@pytest.mark.asyncio
async def test_after_cursor_nullable_ordering_column_boundary_value_null(db, intfields_with_nulls_data):
    """The boundary row's own value was NULL - the cursor's db_value is None, exercising the
    other branch of the NULL-aware comparison (not just a NULL row being paged INTO).

    Whether every NULL row (secondary sentinel id -1/10**9 is picked to be more extreme than any
    real id, so every NULL-tied row always qualifies on the secondary comparison) plus every
    non-null row too, or only the NULL rows, is the right answer depends on whether NULLs sort
    FIRST or LAST for this direction - which is dialect-specific (SQLite/Postgres disagree on
    both ASC and DESC, see ``_nulls_sort_first()``'s own docstring) - so this derives the expected
    shape from an actual, independently-run "no cursor" query instead of hardcoding one dialect's
    placement.
    """

    async def expected_for(*orderings: str) -> list[tuple[int | None, int]]:
        full = [(row.intnum_null, row.id) for row in await IntFields.objects.all().order_by(*orderings)]
        null_group = [pair for pair in full if pair[0] is None]
        nulls_sort_first = full[: len(null_group)] == null_group
        return full if nulls_sort_first else null_group

    # Sentinel ids (-1 / 10**9) are deliberately more extreme than any real row's id, in the
    # direction that matters for each ordering, so every NULL-tied row always passes the
    # secondary ("id") comparison regardless of which way NULLs happen to sort.
    ascending = await IntFields.objects.all().order_by("intnum_null", "id").after_cursor(None, -1)
    assert [(row.intnum_null, row.id) for row in ascending] == await expected_for("intnum_null", "id")

    descending = await IntFields.objects.all().order_by("-intnum_null", "-id").after_cursor(None, 10**9)
    assert [(row.intnum_null, row.id) for row in descending] == await expected_for("-intnum_null", "-id")


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["last", "latest", "earliest"])
async def test_reordering_methods_after_cursor_raise(db, intfields_data, method_name):
    """last()/latest()/earliest() rewrite the ordering after .after_cursor() bound its values to
    the ORIGINAL one - the cursor comparison silently flipped direction along with it (last()
    returned None instead of the final row), while order_by() already refused the same
    combination with a ValueError."""
    queryset = IntFields.objects.all().order_by("intnum").after_cursor(1)
    with pytest.raises(ValueError, match=re.escape(f".{method_name}() cannot be called after .after_cursor()")):
        if method_name == "last":
            queryset.last()
        else:
            getattr(queryset, method_name)("intnum")


# ============================================================================
# Explicit NULLS FIRST/LAST orderings (F("field").asc()/.desc(nulls_first=/nulls_last=)) - the
# cursor comparison follows the ordering's own NULL placement, not the dialect default.
# ============================================================================

NULL_PLACEMENT_ORDERINGS = [
    pytest.param(lambda: F("intnum_null").asc(nulls_first=True), [2, 5, 1, 3, 4], id="asc-nulls-first"),
    pytest.param(lambda: F("intnum_null").asc(nulls_last=True), [1, 3, 4, 2, 5], id="asc-nulls-last"),
    pytest.param(lambda: F("intnum_null").desc(nulls_first=True), [2, 5, 4, 3, 1], id="desc-nulls-first"),
    pytest.param(lambda: F("intnum_null").desc(nulls_last=True), [4, 3, 1, 2, 5], id="desc-nulls-last"),
]


@pytest_asyncio.fixture
async def intfields_scores_data(db):
    """intnum 1..5 with intnum_null 1, NULL, 2, 3, NULL."""
    for number, score in [(1, 1), (2, None), (3, 2), (4, 3), (5, None)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)


@pytest.mark.parametrize("ordering_factory,expected", NULL_PLACEMENT_ORDERINGS)
@pytest.mark.parametrize("page_size", [1, 2, 3])
@pytest.mark.asyncio
async def test_after_cursor_explicit_null_placement_no_gaps_or_duplicates(
    intfields_scores_data, ordering_factory, expected, page_size
):
    """Every page boundary - value rows, the first NULL row, the last NULL row - keeps the full
    walk identical to the unpaginated ordering, on every dialect."""
    full = [row.intnum for row in await IntFields.objects.all().order_by(ordering_factory(), "intnum")]
    assert full == expected

    collected: list[int] = []
    cursor: tuple | None = None
    while True:
        query = IntFields.objects.all().order_by(ordering_factory(), "intnum")
        if cursor is not None:
            query = query.after_cursor(*cursor)
        page = await query.limit(page_size)
        if not page:
            break
        collected.extend(row.intnum for row in page)
        cursor = (page[-1].intnum_null, page[-1].intnum)

    assert collected == expected


@pytest.mark.parametrize("ordering_factory,expected", NULL_PLACEMENT_ORDERINGS)
@pytest.mark.asyncio
async def test_after_cursor_explicit_null_placement_null_boundary_value(
    intfields_scores_data, ordering_factory, expected
):
    """A cursor sitting on the FIRST NULL row: what follows is every remaining row of the ordering."""
    page = await IntFields.objects.all().order_by(ordering_factory(), "intnum").after_cursor(None, 2).limit(10)
    assert [row.intnum for row in page] == expected[expected.index(2) + 1 :]

    last_page = await IntFields.objects.all().order_by(ordering_factory(), "intnum").after_cursor(None, 5).limit(10)
    assert [row.intnum for row in last_page] == expected[expected.index(5) + 1 :]


@pytest.mark.parametrize("ordering_factory,expected", NULL_PLACEMENT_ORDERINGS)
@pytest.mark.asyncio
async def test_after_cursor_explicit_null_placement_value_boundary(intfields_scores_data, ordering_factory, expected):
    """A cursor on a real value row (intnum 3, score 2): the NULL group counts as after it exactly
    when the ordering places NULLs last."""
    page = await IntFields.objects.all().order_by(ordering_factory(), "intnum").after_cursor(2, 3).limit(10)
    assert [row.intnum for row in page] == expected[expected.index(3) + 1 :]


@pytest.mark.parametrize("ordering_factory,expected", NULL_PLACEMENT_ORDERINGS)
@pytest.mark.asyncio
async def test_iterator_explicit_null_placement_visits_every_row_once(
    intfields_scores_data, ordering_factory, expected
):
    iterated = [row.intnum async for row in IntFields.objects.all().order_by(ordering_factory(), "intnum").iterator(2)]
    assert iterated == expected


@pytest.mark.asyncio
async def test_after_cursor_explicit_null_placement_with_duplicate_values(db):
    """Duplicate non-NULL values and duplicate NULLs across page boundaries."""
    for number, score in [(1, 3), (2, None), (3, 3), (4, None), (5, 1), (6, 3)]:
        await IntFields.objects.create(intnum=number, intnum_null=score)
    ordering = F("intnum_null").desc(nulls_last=True)
    full = [row.intnum for row in await IntFields.objects.all().order_by(ordering, "intnum")]
    assert full == [1, 3, 6, 5, 2, 4]

    collected: list[int] = []
    cursor: tuple | None = None
    while True:
        query = IntFields.objects.all().order_by(ordering, "intnum")
        if cursor is not None:
            query = query.after_cursor(*cursor)
        page = await query.limit(2)
        if not page:
            break
        collected.extend(row.intnum for row in page)
        cursor = (page[-1].intnum_null, page[-1].intnum)
    assert collected == full


@pytest.mark.asyncio
async def test_after_cursor_explicit_null_placement_related_field_from_left_join(db):
    await Employee.objects.create(name="CEO")
    boss = await Employee.objects.create(name="Boss")
    await Employee.objects.create(name="Worker A", manager=boss)
    await Employee.objects.create(name="Worker B", manager=boss)
    ordering = F("manager__name").asc(nulls_last=True)
    full = [employee.name for employee in await Employee.objects.all().order_by(ordering, "name")]
    assert full == ["Worker A", "Worker B", "Boss", "CEO"]

    collected: list[str] = []
    cursor: tuple | None = None
    while True:
        query = Employee.objects.all().order_by(ordering, "name")
        if cursor is not None:
            query = query.after_cursor(*cursor)
        page = await query.limit(1)
        if not page:
            break
        collected.append(page[0].name)
        manager_name = "Boss" if page[0].name.startswith("Worker") else None
        cursor = (manager_name, page[0].name)
    assert collected == full
