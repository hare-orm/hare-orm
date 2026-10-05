import pytest

from hare.exceptions import (
    QueryError,
)
from hare.query.expressions import Case, F, OuterReference, Q, When, Window
from hare.query.functions import Count
from hare.query.functions.window import RowNumber
from hare.query.relation_loading.select import Select
from tests.testmodels import Author, Book, DoubleFK, Event, Reporter, Tournament


@pytest.mark.asyncio
async def test_extra_condition_matching_hydrates_relation(db):
    tournament = await Tournament.objects.create(name="Match")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .first()
    )
    assert fetched.tournament is not None
    assert fetched.tournament.name == "Match"


@pytest.mark.asyncio
async def test_extra_condition_not_matching_leaves_relation_none(db):
    """LEFT JOIN semantics - the extra condition not matching nulls out the attribute, it does
    NOT filter the main row out of the result (that's what .filter() would do, not this)."""
    tournament = await Tournament.objects.create(name="Other")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .first()
    )
    assert fetched is not None
    assert fetched.name == "Event 1"  # the main row is still here
    assert fetched.tournament is None  # but the relation didn't hydrate


@pytest.mark.asyncio
async def test_extra_condition_mixed_batch(db):
    match_tournament = await Tournament.objects.create(name="Match")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="Event A", tournament=match_tournament)
    await Event.objects.create(name="Event B", tournament=other_tournament)

    events = (
        await Event.objects.all()
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .order_by("name")
    )
    assert len(events) == 2
    assert events[0].name == "Event A"
    assert events[0].tournament is not None
    assert events[0].tournament.name == "Match"
    assert events[1].name == "Event B"
    assert events[1].tournament is None


@pytest.mark.asyncio
async def test_extra_condition_only_affects_named_relation(db):
    """select_related() on two relations where only one is wrapped in Select(...) - the other,
    passed as a bare string, hydrates normally."""
    tournament = await Tournament.objects.create(name="Other")
    reporter = await Reporter.objects.create(name="Reporter A")
    event = await Event.objects.create(name="Event 1", tournament=tournament, reporter=reporter)

    fetched = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")), "reporter")
        .first()
    )
    assert fetched.tournament is None
    assert fetched.reporter is not None
    assert fetched.reporter.name == "Reporter A"


@pytest.mark.asyncio
async def test_extra_condition_with_multiple_criteria(db):
    tournament = await Tournament.objects.create(name="Match", desc="Final")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    matching = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match", desc="Final")))
        .first()
    )
    assert matching.tournament is not None

    non_matching = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match", desc="Other")))
        .first()
    )
    assert non_matching.tournament is None


@pytest.mark.asyncio
async def test_extra_condition_rejects_further_relation(db):
    """Scoped deliberately to direct fields of the related model only - a condition trying to
    reach through another relation raises rather than silently doing the wrong thing."""
    tournament = await Tournament.objects.create(name="Match")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    with pytest.raises(QueryError):
        await Event.objects.filter(pk=event.event_id).select_related(
            Select("tournament", extra_condition=Q(events__name="Event 1"))
        )


@pytest.mark.asyncio
async def test_no_extra_condition_behaves_as_before(db):
    """Regression: a bare relation-name string, with no Select(...) wrapper at all, must behave
    exactly as it did before this feature existed."""
    tournament = await Tournament.objects.create(name="Match")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = await Event.objects.filter(pk=event.event_id).select_related("tournament").first()
    assert fetched.tournament.name == "Match"


@pytest.mark.asyncio
async def test_select_without_extra_condition_behaves_like_bare_string(db):
    """Select(relation) with no extra_condition at all must behave identically to passing the
    bare relation name string directly."""
    tournament = await Tournament.objects.create(name="Match")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = await Event.objects.filter(pk=event.event_id).select_related(Select("tournament")).first()
    assert fetched.tournament.name == "Match"


@pytest.mark.asyncio
async def test_extra_condition_on_nested_relation_path(db):
    """The Select(...) wraps a multi-level select_related path - the extra condition applies only
    at the final level of that path, not intermediate ones."""
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    fetched = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="leaf")))
        .first()
    )
    assert fetched.left is not None
    assert fetched.left.name == "middle"
    assert fetched.left.left is not None
    assert fetched.left.left.name == "leaf"

    fetched_no_match = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="not-leaf")))
        .first()
    )
    assert fetched_no_match.left is not None
    assert fetched_no_match.left.left is None


@pytest.mark.asyncio
async def test_select_extra_condition_combined_with_prefetch_related(db):
    """select_related(Select(..., extra_condition=...)) and prefetch_related() on a DIFFERENT
    relation, in the same query - the two mechanisms must not interfere with each other (the JOIN
    condition doesn't leak into prefetch's separate second query, and prefetch's own required-field
    handling doesn't get confused by _select_related_extra_conditions)."""
    tournament = await Tournament.objects.create(name="Match")
    reporter = await Reporter.objects.create(name="Reporter A")
    event = await Event.objects.create(name="Event 1", tournament=tournament, reporter=reporter)

    fetched = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .prefetch_related("reporter")
        .first()
    )
    assert fetched.tournament is not None
    assert fetched.tournament.name == "Match"
    assert fetched.reporter is not None
    assert fetched.reporter.name == "Reporter A"


@pytest.mark.asyncio
async def test_select_extra_condition_combined_with_only(db):
    """The extra_condition JOIN is built in _join_table_by_field before .only()'s
    _fields_for_select gate skips auto-selecting the related model's own columns - it must still
    apply even when the caller explicitly whitelists which columns to hydrate."""
    tournament = await Tournament.objects.create(name="Match")
    event = await Event.objects.create(name="Event 1", tournament=tournament)

    fetched = (
        await Event.objects.filter(pk=event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .only("name", "tournament__name")
        .first()
    )
    assert fetched.tournament is not None
    assert fetched.tournament.name == "Match"

    other_tournament = await Tournament.objects.create(name="Other")
    other_event = await Event.objects.create(name="Event 2", tournament=other_tournament)
    fetched_no_match = (
        await Event.objects.filter(pk=other_event.event_id)
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .only("name", "tournament__name")
        .first()
    )
    assert fetched_no_match.tournament is None


# ============================================================================
# Further combinations: Window()/annotate(), values()/values_list(), OuterReference as the
# extra_condition value
# ============================================================================


@pytest.mark.asyncio
async def test_select_extra_condition_combined_with_window_and_annotate(db):
    tournament = await Tournament.objects.create(name="Match")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=other_tournament)

    rows = (
        await Event.objects.all()
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .annotate(rn=Window(RowNumber(), order_by=["name"]))
        .order_by("name")
        .values("name", "rn", "tournament__name")
    )
    assert rows == [
        {"name": "E1", "rn": 1, "tournament__name": "Match"},
        {"name": "E2", "rn": 2, "tournament__name": None},
    ]


@pytest.mark.asyncio
async def test_select_extra_condition_survives_annotate_referencing_same_relation(db):
    """An annotation whose own expression references the SAME relation carrying extra_condition
    (e.g. Case/When(tournament__name=...)) used to silently lose that extra_condition -
    _get_annotate() resolved the nested field path (and built its own, unconditional JOIN)
    BEFORE _join_select_related() applied the real, extra_condition-aware one, and _join_table()'s
    own dedup-by-alias kept whichever JOIN was built first. Confirmed live before the fix: with
    the annotation present, .tournament came back as the real (non-matching) row instead of None."""
    tournament = await Tournament.objects.create(name="Match")
    await Event.objects.create(name="E1", tournament=tournament)

    event = await (
        Event.objects.filter(name="E1")
        .select_related(Select("tournament", extra_condition=Q(name="NoSuchTournament")))
        .annotate(has_name=Case(When(tournament__name="Match", then=True), default=False))
        .first()
    )
    assert event.tournament is None, "extra_condition must still exclude the mismatched relation"


@pytest.mark.asyncio
async def test_select_extra_condition_survives_cache_collision_across_annotate_calls(db):
    """Third sibling of the same bug family (1a27ab4a/d1de1f48), this time in QUERY_SHAPE_CACHE
    itself rather than a single call's own JOIN: two calls share the same query SHAPE (same
    filter/select_related/annotate field names) but use DIFFERENT extra_condition literal
    VALUES, both crossed with an annotate(Case(When(<relation>__field=...))) touching the SAME
    relation. d1de1f48 fixed the single-call JOIN loss by threading select_related_extra_
    conditions into _get_annotate()'s own ExpressionContext, but never added the matching
    _query_is_plannable() exclusion (unlike the .order_by()/nested-filter sibling, which
    got both) - so the SECOND call silently reused the FIRST call's cached JOIN, extra_condition
    literal baked in and all. Confirmed live before this test's own fix: the second call's
    tournament came back None instead of hydrating from its own ('Other') extra_condition."""
    tournament_match = await Tournament.objects.create(name="Match")
    tournament_other = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament_match)
    await Event.objects.create(name="E2", tournament=tournament_other)

    first = await (
        Event.objects.filter(name="E1")
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .annotate(has_name=Case(When(tournament__name="Match", then=True), default=False))
        .first()
    )
    assert first.tournament is not None
    assert first.tournament.name == "Match"

    second = await (
        Event.objects.filter(name="E2")
        .select_related(Select("tournament", extra_condition=Q(name="Other")))
        .annotate(has_name=Case(When(tournament__name="Match", then=True), default=False))
        .first()
    )
    assert second.tournament is not None, (
        "second call's OWN extra_condition (name='Other') matches E2's real tournament "
        "('Other') - a cache collision would instead reuse the FIRST call's cached JOIN "
        "(ON tournament.name='Match'), which E2's tournament does not satisfy"
    )
    assert second.tournament.name == "Other"


@pytest.mark.asyncio
async def test_select_extra_condition_survives_bare_f_annotate_referencing_same_relation(db):
    """Sibling of test_select_extra_condition_survives_annotate_referencing_same_relation, but
    for a bare F("relation__field") annotation instead of Case/When - F() resolves through
    LookupPaths.get_nested_field() directly, a completely separate code path from Q's own
    _get_nested_filter() that Case/When goes through, and it never threaded
    select_related_extra_conditions at all: the unconditioned JOIN it built won _join_table()'s
    dedup-by-table-identity race the exact same way, silently dropping extra_condition from both
    the annotation's own value AND the hydrated .tournament relation."""
    tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament)

    event = await (
        Event.objects.filter(name="E1")
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .annotate(tname=F("tournament__name"))
        .first()
    )
    assert event.tournament is None, "extra_condition must still exclude the mismatched relation"
    assert event.tname is None, "F()'s own resolved value must respect the same extra_condition"


@pytest.mark.asyncio
async def test_select_extra_condition_survives_count_annotate_referencing_same_relation(db):
    """Same gap as the bare-F() sibling above, but for Count("relation__field") - Aggregate/
    Function share the identical LookupPaths.get_nested_field() call site (Function.
    _get_nested_field(), inherited by Aggregate), so this is a distinct instance of the same bug,
    not just a duplicate of the F() case."""
    tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament)

    event = await (
        Event.objects.filter(name="E1")
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .annotate(tname_count=Count("tournament__name"))
        .group_by("event_id")
        .first()
    )
    assert event.tournament is None, "extra_condition must still exclude the mismatched relation"
    assert event.tname_count == 0, "COUNT of the excluded (NULL-joined) relation's name must be 0"


@pytest.mark.asyncio
async def test_select_extra_condition_combined_with_group_by_aggregate(db):
    tournament = await Tournament.objects.create(name="Match")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=tournament)

    rows = (
        await Event.objects.all()
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .annotate(total=Count("event_id"))
        .group_by("tournament_id")
        .values("tournament_id", "total")
    )
    assert rows == [{"tournament_id": tournament.id, "total": 2}]


@pytest.mark.asyncio
async def test_select_extra_condition_applies_to_values(db):
    """Previously silently ignored - values()/values_list() build their own JOIN for a related
    field reference (e.g. "tournament__name"), independent of select_related()'s own join-
    building, and that separate path didn't know about _select_related_extra_conditions at all.
    Confirmed empirically before fixing: it returned the related row's real value regardless of
    whether it matched the condition."""
    tournament = await Tournament.objects.create(name="Match")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=other_tournament)

    rows = (
        await Event.objects.all()
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .order_by("name")
        .values("name", "tournament__name")
    )
    assert rows == [
        {"name": "E1", "tournament__name": "Match"},
        {"name": "E2", "tournament__name": None},
    ]


@pytest.mark.asyncio
async def test_select_extra_condition_applies_to_values_list(db):
    tournament = await Tournament.objects.create(name="Match")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="E1", tournament=tournament)
    await Event.objects.create(name="E2", tournament=other_tournament)

    rows = (
        await Event.objects.all()
        .select_related(Select("tournament", extra_condition=Q(name="Match")))
        .order_by("name")
        .values_list("name", "tournament__name")
    )
    assert rows == [("E1", "Match"), ("E2", None)]


@pytest.mark.asyncio
async def test_select_extra_condition_nested_path_applies_to_values(db):
    leaf = await DoubleFK.objects.create(name="leaf", left=None)
    middle = await DoubleFK.objects.create(name="middle", left=leaf)
    root = await DoubleFK.objects.create(name="root", left=middle)

    rows = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="leaf")))
        .values("name", "left__left__name")
    )
    assert rows == [{"name": "root", "left__left__name": "leaf"}]

    rows_no_match = (
        await DoubleFK.objects.filter(pk=root.pk)
        .select_related("left", Select("left__left", extra_condition=Q(name="not-leaf")))
        .values("name", "left__left__name")
    )
    assert rows_no_match == [{"name": "root", "left__left__name": None}]


@pytest.mark.asyncio
async def test_extra_condition_survives_order_by_same_relation(db):
    """Regression: `.order_by("author__name")` on the SAME relation the `extra_condition` targets
    used to build its own unconditioned JOIN in `get_ordering()` - which ran BEFORE
    `_join_select_related()` in `_make_query()`, so it won `_join_table()`'s table-identity dedup
    and silently discarded `extra_condition` from the real query, even on this first,
    never-cached build."""
    await Author.objects.create(name="a1")
    author_a2 = await Author.objects.create(name="a2")
    await Book.objects.create(name="beta", author=author_a2, rating=1.0)

    fetched = (
        await Book.objects.filter(name="beta")
        .select_related(Select("author", extra_condition=Q(name="a1")))
        .order_by("author__name")
        .first()
    )
    assert fetched is not None
    assert fetched.author is None  # "a1" doesn't match the real author's name ("a2")


@pytest.mark.asyncio
async def test_extra_condition_survives_nested_filter_on_same_relation(db):
    """Same hazard as ordering above, via a nested filter kwarg instead: `.filter(author__id=...)`
    on the SAME relation builds its own JOIN through `Q._get_nested_filter()`, which must not
    win the dedup over the extra_condition-aware JOIN either. Checked via the generated SQL, not
    row-level results: a LEFT JOIN whose ON clause is extra_condition-restricted, combined with a
    WHERE on the very same joined table, makes "extra_condition applied" and "relation is simply
    empty" indistinguishable at the row level whenever the nested filter and extra_condition touch
    the same column - using `author__id` (a different column than extra_condition's `name`) for
    the nested filter avoids that ambiguity."""
    author = await Author.objects.create(name="a1")
    book = await Book.objects.create(name="beta", author=author, rating=1.0)

    queryset = Book.objects.filter(pk=book.pk, author__id=author.pk).select_related(
        Select("author", extra_condition=Q(name="a1"))
    )
    generated_sql = queryset.sql(parameters_inline=True)
    assert "'a1'" in generated_sql
    assert generated_sql.count("JOIN") == 1

    fetched = await queryset.first()
    assert fetched is not None
    assert fetched.author is not None
    assert fetched.author.name == "a1"


@pytest.mark.asyncio
async def test_outerref_as_extra_condition_value_raises_clearly(db):
    """OuterReference() is only meaningful inside a queryset wrapped in Exists(...) - using it directly
    as a Q() value inside extra_condition (which is never wrapped in Exists) must fail with its
    own clear, documented error, not something confusing."""
    tournament = await Tournament.objects.create(name="Match")
    await Event.objects.create(name="E1", tournament=tournament)

    with pytest.raises(QueryError, match="Exists"):
        await (
            Event.objects.all()
            .select_related(Select("tournament", extra_condition=Q(pk=OuterReference("id"))))
            .first()
        )


@pytest.mark.asyncio
async def test_counting_summarizing_and_deleting_match_the_rows_a_filter_reads_through_the_condition(db):
    match_tournament = await Tournament.objects.create(name="Match")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="Event A", tournament=match_tournament)
    await Event.objects.create(name="Event B", tournament=other_tournament)
    # The filter reads the tournament through the JOIN the condition is folded into - only the
    # matching one is there.
    queryset = Event.objects.select_related(Select("tournament", extra_condition=Q(name="Match"))).filter(
        tournament__name__isnull=False
    )

    assert [event.name for event in await queryset] == ["Event A"]
    assert await queryset.count() == 1
    assert await queryset.exists()
    assert await queryset.aggregate(events=Count("event_id")) == {"events": 1}
    assert await queryset.delete() == 1
    assert [event.name for event in await Event.objects.order_by("name")] == ["Event B"]
