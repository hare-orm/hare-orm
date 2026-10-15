import datetime
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.contrib import test
from hare.contrib.test import requires_features
from hare.dialects.sqlite.parameters.constants import SQLITE_IN_JSON_ARRAY_THRESHOLD
from hare.exceptions import QueryError, UnSupportedError
from hare.query.enums import Connector
from hare.query.expressions import Case, F, Q, RawSQL, Subquery, When
from hare.query.functions import Coalesce, Count, Length, Lower, Max, Trim, Upper
from hare.sql import Field
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT
from hare.sql.terms.criteria.like import Like
from tests.testmodels import (
    Author,
    BinaryFields,
    Book,
    BooleanFields,
    Category,
    CharFields,
    CharFkRelatedModel,
    CharPkModel,
    DateFields,
    DatetimeFields,
    DecimalFields,
    DirtyTrackedComposite,
    DocumentRevisionNote,
    Drink,
    Employee,
    Event,
    Flavor,
    FloatFields,
    IntFields,
    Pair,
    ProtectedChildByCode,
    ProtectedParentWithCode,
    Reporter,
    Single,
    SoftDeleteComposite,
    Team,
    Tournament,
    UUIDFields,
    VersionedDocument,
)


@pytest.mark.asyncio
async def test_filtering(db):
    tournament = Tournament(name="Tournament")
    await tournament.save()

    second_tournament = Tournament(name="Tournament 2")
    await second_tournament.save()

    event_first = Event(name="1", tournament=tournament)
    await event_first.save()
    event_second = Event(name="2", tournament=second_tournament)
    await event_second.save()
    event_third = Event(name="3", tournament=tournament)
    await event_third.save()
    event_forth = Event(name="4", tournament=second_tournament)
    await event_forth.save()

    team_first = Team(name="First")
    await team_first.save()
    team_second = Team(name="Second")
    await team_second.save()

    await team_first.events.add(event_first)
    await event_second.participants.add(team_second)

    found_events = (
        await Event.objects.filter(Q(pk__in=[event_first.pk, event_second.pk]) | Q(name="3"))
        .filter(participants__not=team_second.id)
        .order_by("name", "tournament_id")
        .distinct()
    )
    assert len(found_events) == 2
    assert found_events[0].pk == event_first.pk
    assert found_events[1].pk == event_third.pk
    await Team.objects.filter(events__tournament_id=tournament.id).order_by("-events__name")
    await Tournament.objects.filter(events__name__in=["1", "3"]).distinct()

    teams = await Team.objects.filter(name__icontains="CON")
    assert len(teams) == 1
    assert teams[0].name == "Second"

    teams = await Team.objects.filter(name__iexact="SeCoNd")
    assert len(teams) == 1
    assert teams[0].name == "Second"

    tournaments = await Tournament.objects.filter(events__participants__name__startswith="Fir")
    assert len(tournaments) == 1
    assert tournaments[0] == tournament


@pytest.mark.asyncio
async def test_relation_filter_lookup_suffix_accepts_a_model_instance(db):
    """A model instance already worked as a bare `field=instance` value for a many-to-many/
    backward-FK relation filter (Q._get_actual_filter_params()'s own pk-extraction), but any
    lookup SUFFIX on that same relation (`field__not=instance`) skipped that extraction entirely
    and passed the raw instance straight into the target column's own to_db_value() - crashing
    with a confusing ValidationError ("int() ... not 'Event'") instead of comparing against its
    pk, the same way the bare form already does."""
    tournament_one = await Tournament.objects.create(name="T1")
    tournament_two = await Tournament.objects.create(name="T2")
    event_one = await Event.objects.create(name="E1", tournament=tournament_one)
    event_two = await Event.objects.create(name="E2", tournament=tournament_two)

    team = await Team.objects.create(name="Team1")
    await team.events.add(event_one)

    # Tournament.events: a backward-FK relation (Event.tournament's related_name).
    assert [t.name for t in await Tournament.objects.filter(events__not=event_two)] == ["T1"]
    # Team.events: a backward many-to-many relation (Event.participants' related_name).
    assert [t.name for t in await Team.objects.filter(events=event_one)] == ["Team1"]
    assert [t.name for t in await Team.objects.filter(events__not=event_two)] == ["Team1"]
    # Event.participants: a forward many-to-many relation.
    assert [e.name for e in await Event.objects.filter(participants__not=team)] == ["E2"]


@pytest.mark.asyncio
async def test_in_filter_combined_with_other_filter_and_inline_sql(db):
    """`ParameterizedValueWrapper` (hare.sql/terms.py, used by is_in()/not_in() in
    hare/query/filters/lookups/lookups.py) lazily binds each IN-list value's parameter at render time
    rather than eagerly - this specifically guards the case that matters: an `__in=`/`__not_in=`
    filter combined with ANOTHER filter in the same query still numbers bind parameters
    correctly (an eager/precomputed index would collide with the other filter's own parameter),
    and `.sql(parameters_inline=True)` still inlines IN-list values as literals, not placeholders.
    """
    tournament = await Tournament.objects.create(name="Tournament")
    other_tournament = await Tournament.objects.create(name="Other")
    event_first = await Event.objects.create(name="1", tournament=tournament)
    event_second = await Event.objects.create(name="2", tournament=tournament)
    await Event.objects.create(name="3", tournament=other_tournament)

    found = await Event.objects.filter(tournament_id=tournament.id, pk__in=[event_first.pk, event_second.pk]).order_by(
        "name"
    )
    assert [e.pk for e in found] == [event_first.pk, event_second.pk]

    excluded = await Event.objects.filter(tournament_id=tournament.id, pk__not_in=[event_first.pk]).order_by("name")
    assert [e.pk for e in excluded] == [event_second.pk]

    inline_sql = Event.objects.filter(name="1", pk__in=[event_first.pk, event_second.pk]).sql(parameters_inline=True)
    assert str(event_first.pk) in inline_sql
    assert str(event_second.pk) in inline_sql
    assert "?" not in inline_sql
    assert "$1" not in inline_sql


@pytest.mark.asyncio
async def test_q_object_backward_related_query(db):
    await Tournament.objects.create(name="0")
    tournament = await Tournament.objects.create(name="Tournament")
    event = await Event.objects.create(name="1", tournament=tournament)
    fetched_tournament = await Tournament.objects.filter(events=event.event_id).first()
    assert fetched_tournament.id == tournament.id

    fetched_tournament = await Tournament.objects.filter(Q(events=event.event_id)).first()
    assert fetched_tournament.id == tournament.id


@pytest.mark.asyncio
async def test_q_object_related_query(db):
    tournament_first = await Tournament.objects.create(name="0")
    tournament_second = await Tournament.objects.create(name="1")
    event = await Event.objects.create(name="1", tournament=tournament_second)
    await Event.objects.create(name="1", tournament=tournament_first)

    fetched_event = await Event.objects.filter(tournament=tournament_second).first()
    assert fetched_event.pk == event.pk

    fetched_event = await Event.objects.filter(Q(tournament=tournament_second)).first()
    assert fetched_event.pk == event.pk

    fetched_event = await Event.objects.filter(Q(tournament=tournament_second.id)).first()
    assert fetched_event.pk == event.pk


@pytest.mark.asyncio
async def test_null_filter(db):
    tournament = await Tournament.objects.create(name="Tournament")
    reporter = await Reporter.objects.create(name="John")
    await Event.objects.create(name="2", tournament=tournament, reporter=reporter)
    event = await Event.objects.create(name="1", tournament=tournament)
    fetched_events = await Event.objects.filter(reporter=None)
    assert len(fetched_events) == 1
    assert fetched_events[0].pk == event.pk


@pytest.mark.asyncio
async def test_exclude(db):
    await Tournament.objects.create(name="0")
    tournament = await Tournament.objects.create(name="1")

    tournaments = await Tournament.objects.exclude(name="0")
    assert len(tournaments) == 1
    assert tournaments[0].name == tournament.name


@pytest.mark.asyncio
async def test_exclude_with_filter(db):
    await Tournament.objects.create(name="0")
    tournament = await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.exclude(name="0").filter(id=tournament.id)
    assert len(tournaments) == 1
    assert tournaments[0].name == tournament.name


@pytest.mark.asyncio
async def test_filter_null_on_related(db):
    tournament = await Tournament.objects.create(name="Tournament")
    reporter = await Reporter.objects.create(name="John")
    event_first = await Event.objects.create(name="1", tournament=tournament, reporter=reporter)
    event_second = await Event.objects.create(name="2", tournament=tournament)

    team_first = await Team.objects.create(name="1")
    team_second = await Team.objects.create(name="2")
    await event_first.participants.add(team_first)
    await event_second.participants.add(team_second)

    fetched_teams = await Team.objects.filter(events__reporter=None)
    assert len(fetched_teams) == 1
    assert fetched_teams[0].id == team_second.id


@pytest.mark.asyncio
async def test_exclude_across_nullable_relation_includes_rows_with_no_related_object(db):
    """exclude()/~Q() across a relation used to build `NOT (joined_table.col = value)` on top of
    the plain LEFT JOIN already in place - SQL's own three-valued logic makes that UNKNOWN, not
    TRUE, whenever the joined row doesn't exist at all (the FK is NULL), so a row with no related
    object at all was silently dropped from the exclude() result even though "no reporter"
    obviously isn't "a reporter named John"."""
    tournament = await Tournament.objects.create(name="Tournament")
    reporter = await Reporter.objects.create(name="John")
    event_with_reporter = await Event.objects.create(name="has reporter", tournament=tournament, reporter=reporter)
    event_without_reporter = await Event.objects.create(name="no reporter", tournament=tournament)

    excluded = await Event.objects.exclude(reporter__name="John")
    assert {event.name for event in excluded} == {event_without_reporter.name}

    via_q = await Event.objects.filter(~Q(reporter__name="John"))
    assert {event.name for event in via_q} == {event_without_reporter.name}

    # Sanity check the OTHER direction still works: a row whose related object genuinely doesn't
    # match a filter (not just "has no related object") stays excluded.
    assert not any(event.name == event_with_reporter.name for event in excluded)


@pytest.mark.asyncio
async def test_exclude_across_relation_with_no_matches_includes_everything(db):
    """Guards against the NOT EXISTS rewrite accidentally matching nothing (e.g. an inverted
    correlation) - excluding a condition that no related row anywhere satisfies must return every
    row, not none."""
    tournament = await Tournament.objects.create(name="Tournament")
    reporter = await Reporter.objects.create(name="John")
    event = await Event.objects.create(name="1", tournament=tournament, reporter=reporter)

    excluded = await Event.objects.exclude(reporter__name="Nobody Matches This")
    assert {e.name for e in excluded} == {event.name}


@pytest.mark.asyncio
async def test_exclude_across_many_to_many_includes_rows_with_no_related_objects(db):
    """Same nullable-relation fix, exercised through an M2M join (a through table) instead of a
    plain forward FK - a row with zero related objects at all must still be included."""
    tournament = await Tournament.objects.create(name="Tournament")
    event_with_team = await Event.objects.create(name="has team", tournament=tournament)
    event_without_team = await Event.objects.create(name="no team", tournament=tournament)
    team = await Team.objects.create(name="Team A")
    await event_with_team.participants.add(team)

    excluded = await Event.objects.exclude(participants__name="Team A")
    assert {e.name for e in excluded} == {event_without_team.name}


@pytest.mark.asyncio
async def test_filter_or(db):
    await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.filter(Q(name="1") | Q(name="2"))
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"1", "2"}


@pytest.mark.asyncio
async def test_filter_not(db):
    await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")

    tournaments = await Tournament.objects.filter(~Q(name="1"))
    assert len(tournaments) == 1
    assert tournaments[0].name == "0"


@pytest.mark.asyncio
async def test_filter_with_f_expression(db):
    await IntFields.objects.create(intnum=1, intnum_null=1)
    await IntFields.objects.create(intnum=2, intnum_null=1)
    assert await IntFields.objects.filter(intnum=F("intnum_null")).count() == 1
    assert await IntFields.objects.filter(intnum__gte=F("intnum_null")).count() == 2
    assert await IntFields.objects.filter(intnum=F("intnum_null") + F("intnum_null")).count() == 1


@pytest.mark.asyncio
async def test_filter_not_with_or(db):
    await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.filter(Q(name="1") | ~Q(name="2"))
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"0", "1"}


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_filter_exact(db):
    obj = await DatetimeFields.objects.create(
        datetime=datetime.datetime(year=2020, month=5, day=20, hour=0, minute=0, second=0, microsecond=0)
    )
    assert await DatetimeFields.objects.filter(datetime__year=2020).count() == 1
    assert await DatetimeFields.objects.filter(datetime__quarter=2).count() == 1
    assert await DatetimeFields.objects.filter(datetime__month=5).count() == 1
    assert await DatetimeFields.objects.filter(datetime__day=20).count() == 1
    # PostgreSQL stores timestamptz and EXTRACT uses the session timezone.
    # Refresh from DB to get the tz-aware datetime that the driver returns,
    # then convert to the PG session timezone so the expected values match.
    obj = await DatetimeFields.objects.get(id=obj.id)
    tz_rows = await db.get_connection().execute_dicts("SHOW timezone")
    import zoneinfo

    server_tz = zoneinfo.ZoneInfo(tz_rows[0]["TimeZone"])
    dt = obj.datetime.astimezone(server_tz)
    week = dt.isocalendar()[1]
    assert await DatetimeFields.objects.filter(datetime__week=week).count() == 1
    assert await DatetimeFields.objects.filter(datetime__hour=dt.hour).count() == 1
    assert await DatetimeFields.objects.filter(datetime__minute=0).count() == 1
    assert await DatetimeFields.objects.filter(datetime__second=0).count() == 1
    assert await DatetimeFields.objects.filter(datetime__microsecond=0).count() == 1

    await DateFields.objects.create(date=datetime.date(year=2021, month=6, day=21))
    assert await DateFields.objects.filter(date__year=-2021).count() == 0
    assert await DateFields.objects.filter(date__year=2021).count() == 1
    assert await DateFields.objects.filter(date__month=6).count() == 1
    assert await DateFields.objects.filter(date__day=21).count() == 1
    assert await DateFields.objects.filter(date__year="2021").count() == 1
    assert await DateFields.objects.filter(date__year=2021.0).count() == 1
    assert await DateFields.objects.filter(date="20210621").count() == 1
    assert await DateFields.objects.filter(date="2021-06-21").count() == 1
    assert await DateFields.objects.filter(date=datetime.date(year=2021, month=6, day=21)).count() == 1


@test.requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_filter_second_and_microsecond_with_nonzero_microsecond(db):
    """Postgres EXTRACT(SECOND FROM ...) returns a fractional numeric (5.5, not 5) and
    EXTRACT(MICROSECOND FROM ...) folds the whole seconds field into it too (5500000, not just
    the 500000 sub-second remainder) - comparing either directly against a plain int silently
    matched zero rows for any datetime with a nonzero microsecond component. The sibling
    test_filter_exact above never caught this: it only ever creates a row with microsecond=0,
    the one case where Postgres's raw EXTRACT output happens to already equal the plain int."""
    await DatetimeFields.objects.create(
        datetime=datetime.datetime(year=2020, month=5, day=20, hour=12, minute=0, second=5, microsecond=500000)
    )
    assert await DatetimeFields.objects.filter(datetime__second=5).count() == 1
    assert await DatetimeFields.objects.filter(datetime__microsecond=500000).count() == 1
    # The old, unfixed semantics (fractional seconds; seconds folded into microseconds) must not
    # accidentally still match either.
    assert await DatetimeFields.objects.filter(datetime__second=6).count() == 0
    assert await DatetimeFields.objects.filter(datetime__microsecond=5500000).count() == 0


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_exclude_across_join_replaces_table_in_extract_lookup(db):
    """Negating a filter that crosses a relation (exclude()/~Q() with a join) rewrites the
    criterion into a correlated NOT EXISTS subquery against a freshly-aliased copy of the base
    table (see Q._negate_across_joins()), retargeting every reference to the original table via
    replace_table(). A `__year`/`__month`/etc. lookup compiles to Extract(date_part, field) -
    Extract must retarget its own wrapped `field` the same way a plain field comparison does, or
    the EXTRACT(...) call is silently left pointing at the ORIGINAL (uncorrelated) table instead
    of the aliased inner one used by the rest of the subquery."""
    sql = Tournament.objects.all().exclude(Q(events__name="E1") & Q(created__year=2024)).sql()
    assert '"tournament__negated"."created"' in sql
    assert 'EXTRACT(\'YEAR\' FROM "tournament"."created")' not in sql


@pytest.mark.asyncio
async def test_exclude_with_or_across_same_relation_path_does_not_duplicate_join(db):
    """Two (or more) Q children traversing the IDENTICAL relation path (here: manager, on both
    sides of the OR) each independently contribute the SAME join edge to modifier.joins -
    Q._negate_across_joins() used to re-add it once per child with no dedup, joining the same
    aliased table twice in the NOT EXISTS subquery and crashing with "ambiguous column name"
    (sqlite) - a single relation kwarg, a 3-level nullable chain, and an OR across two DIFFERENT
    relations all already worked; it's specifically 2+ Q children on the SAME relation path that
    crashed."""
    employee_1 = await Employee.objects.create(name="E1")
    employee_2 = await Employee.objects.create(name="E2")
    managed_by_e1 = await Employee.objects.create(name="Reports to E1", manager=employee_1)
    managed_by_e2 = await Employee.objects.create(name="Reports to E2", manager=employee_2)
    no_manager = await Employee.objects.create(name="No manager")

    excluded = await Employee.objects.exclude(Q(manager__name="E1") | Q(manager__name="E2"))

    assert {e.name for e in excluded} == {
        employee_1.name,
        employee_2.name,
        no_manager.name,
    }
    assert not any(e.name == managed_by_e1.name for e in excluded)
    assert not any(e.name == managed_by_e2.name for e in excluded)


async def create_tournaments_with_event_names(event_names_by_tournament_name):
    for tournament_name, event_names in event_names_by_tournament_name.items():
        tournament = await Tournament.objects.create(name=tournament_name)
        for event_name in event_names:
            await Event.objects.create(name=event_name, tournament=tournament)


NESTED_NEGATION_TOURNAMENTS = {
    "T1": {"a"},
    "T2": {"b"},
    "T3": {"a", "b"},
    "T4": set(),
    "T5": {"a", "c"},
    "T6": {"a", "b", "c"},
    "T7": {"b", "c"},
}


@pytest.mark.asyncio
async def test_nested_negation_across_join_keeps_inner_not_exists_correlated(db):
    """Each NOT EXISTS produced by Q._negate_across_joins() got the SAME inner alias, so when one
    negation was nested inside another the outer retargeting turned the inner correlation into a
    tautology (`tournament__negated.id = tournament__negated.id`) and the inner subquery stopped
    depending on the outer row at all."""
    await create_tournaments_with_event_names(NESTED_NEGATION_TOURNAMENTS)

    tournaments = await Tournament.objects.filter(~(Q(events__name="a") & ~Q(events__name="b"))).order_by("name")

    assert [tournament.name for tournament in tournaments] == ["T2", "T3", "T4", "T6", "T7"]


@pytest.mark.asyncio
async def test_nested_negation_across_join_in_exclude(db):
    await create_tournaments_with_event_names(NESTED_NEGATION_TOURNAMENTS)

    tournaments = await Tournament.objects.exclude(Q(events__name="a") & ~Q(events__name="b")).order_by("name")

    assert [tournament.name for tournament in tournaments] == ["T2", "T3", "T4", "T6", "T7"]


@pytest.mark.asyncio
async def test_triple_nested_negation_across_join(db):
    await create_tournaments_with_event_names(NESTED_NEGATION_TOURNAMENTS)

    tournaments = await Tournament.objects.filter(
        ~(Q(events__name="a") & ~(Q(events__name="b") & ~Q(events__name="c")))
    ).order_by("name")

    expected_names = [
        tournament_name
        for tournament_name, event_names in NESTED_NEGATION_TOURNAMENTS.items()
        if not ("a" in event_names and not ("b" in event_names and "c" not in event_names))
    ]
    assert [tournament.name for tournament in tournaments] == expected_names


@pytest.mark.asyncio
async def test_sibling_negations_across_join_inside_outer_negation(db):
    await create_tournaments_with_event_names(NESTED_NEGATION_TOURNAMENTS)

    tournaments = await Tournament.objects.filter(
        ~(Q(events__name="a") & ~Q(events__name="b") & ~Q(events__name="c"))
    ).order_by("name")

    expected_names = [
        tournament_name
        for tournament_name, event_names in NESTED_NEGATION_TOURNAMENTS.items()
        if not ("a" in event_names and "b" not in event_names and "c" not in event_names)
    ]
    assert [tournament.name for tournament in tournaments] == expected_names


@pytest.mark.asyncio
async def test_nested_negation_repeated_queries_use_shape_cache_consistently(db):
    await create_tournaments_with_event_names(NESTED_NEGATION_TOURNAMENTS)

    for event_name_a, event_name_b, expected_names in [
        ("a", "b", ["T2", "T3", "T4", "T6", "T7"]),
        ("b", "a", ["T1", "T3", "T4", "T5", "T6"]),
        ("a", "b", ["T2", "T3", "T4", "T6", "T7"]),
    ]:
        tournaments = await Tournament.objects.filter(
            ~(Q(events__name=event_name_a) & ~Q(events__name=event_name_b))
        ).order_by("name")
        assert [tournament.name for tournament in tournaments] == expected_names


@pytest.mark.asyncio
async def test_filter_by_aggregation_field(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Event.objects.create(name="2", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(events_count=1)
    assert len(tournaments) == 1
    assert tournaments[0].id == tournament.id


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_and(db):
    tournament = await Tournament.objects.create(name="0")
    tournament_second = await Tournament.objects.create(name="1")
    await Event.objects.create(name="1", tournament=tournament)
    await Event.objects.create(name="2", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(events_count=1, name="0")
    assert len(tournaments) == 1
    assert tournaments[0].id == tournament.id


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_and_as_one_node(db):
    tournament = await Tournament.objects.create(name="0")
    tournament_second = await Tournament.objects.create(name="1")
    await Event.objects.create(name="1", tournament=tournament)
    await Event.objects.create(name="2", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(Q(events_count=1, name="0"))
    assert len(tournaments) == 1
    assert tournaments[0].id == tournament.id


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_and_as_two_nodes(db):
    tournament = await Tournament.objects.create(name="0")
    tournament_second = await Tournament.objects.create(name="1")
    await Event.objects.create(name="1", tournament=tournament)
    await Event.objects.create(name="2", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        Q(events_count=1) & Q(name="0")
    )
    assert len(tournaments) == 1
    assert tournaments[0].id == tournament.id


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_or(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")
    await Event.objects.create(name="1", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        Q(events_count=1) | Q(name="2")
    )
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"0", "2"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_or_reversed(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")
    await Event.objects.create(name="1", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        Q(name="2") | Q(events_count=1)
    )
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"0", "2"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_or_as_one_node(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")
    await Event.objects.create(name="1", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        Q.with_connector(Connector.OR, events_count=1, name="2")
    )
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"0", "2"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_not(db):
    tournament = await Tournament.objects.create(name="0")
    tournament_second = await Tournament.objects.create(name="1")
    await Event.objects.create(name="1", tournament=tournament)
    await Event.objects.create(name="2", tournament=tournament_second)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(~Q(events_count=1, name="0"))
    assert len(tournaments) == 1
    assert tournaments[0].id == tournament_second.id


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_or_not(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")
    await Event.objects.create(name="1", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        ~(Q(events_count=1) | Q(name="2"))
    )
    assert len(tournaments) == 1
    assert {t.name for t in tournaments} == {"1"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_with_or_not_reversed(db):
    tournament = await Tournament.objects.create(name="0")
    await Tournament.objects.create(name="1")
    await Tournament.objects.create(name="2")
    await Event.objects.create(name="1", tournament=tournament)

    tournaments = await Tournament.objects.annotate(events_count=Count("events")).filter(
        ~(Q(name="2") | Q(events_count=1))
    )
    assert len(tournaments) == 1
    assert {t.name for t in tournaments} == {"1"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_trim(db):
    await Tournament.objects.create(name="  1 ")
    await Tournament.objects.create(name="2  ")

    tournaments = await Tournament.objects.annotate(trimmed_name=Trim("name")).filter(trimmed_name="1")
    assert len(tournaments) == 1
    assert {(t.name, t.trimmed_name) for t in tournaments} == {("  1 ", "1")}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_length(db):
    await Tournament.objects.create(name="12345")
    await Tournament.objects.create(name="123")
    await Tournament.objects.create(name="1234")

    tournaments = await Tournament.objects.annotate(name_len=Length("name")).filter(name_len__gte=4)
    assert len(tournaments) == 2
    assert {t.name for t in tournaments} == {"1234", "12345"}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_coalesce(db):
    await Tournament.objects.create(name="1", desc="demo")
    await Tournament.objects.create(name="2")

    tournaments = await Tournament.objects.annotate(clean_desc=Coalesce("desc", "demo")).filter(clean_desc="demo")
    assert len(tournaments) == 2
    assert {(t.name, t.clean_desc) for t in tournaments} == {("1", "demo"), ("2", "demo")}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_coalesce_numeric(db):
    await IntFields.objects.create(intnum=1, intnum_null=10)
    await IntFields.objects.create(intnum=4)

    ints = await IntFields.objects.annotate(clean_intnum_null=Coalesce("intnum_null", 0)).filter(
        clean_intnum_null__in=(0, 10)
    )
    assert len(ints) == 2
    assert {(i.intnum_null, i.clean_intnum_null) for i in ints} == {(None, 0), (10, 10)}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_comparison_coalesce_numeric(db):
    await IntFields.objects.create(intnum=3, intnum_null=10)
    await IntFields.objects.create(intnum=1, intnum_null=4)
    await IntFields.objects.create(intnum=2)

    ints = await IntFields.objects.annotate(clean_intnum_null=Coalesce("intnum_null", 0)).filter(
        clean_intnum_null__gt=0
    )
    assert len(ints) == 2
    assert {i.clean_intnum_null for i in ints} == {10, 4}


@pytest.mark.asyncio
async def test_filter_by_aggregation_field_comparison_length(db):
    t1 = await Tournament.objects.create(name="Tournament")
    await Event.objects.create(name="event1", tournament=t1)
    await Event.objects.create(name="event2", tournament=t1)
    t2 = await Tournament.objects.create(name="contest")
    await Event.objects.create(name="event3", tournament=t2)
    await Tournament.objects.create(name="Championship")
    t4 = await Tournament.objects.create(name="local")
    await Event.objects.create(name="event4", tournament=t4)
    await Event.objects.create(name="event5", tournament=t4)
    tournaments = await Tournament.objects.annotate(name_len=Length("name"), event_count=Count("events")).filter(
        name_len__gt=5, event_count=2
    )
    assert len(tournaments) == 1
    assert {t.name for t in tournaments} == {"Tournament"}


@pytest.mark.asyncio
async def test_filter_by_annotation_lower(db):
    await Tournament.objects.create(name="Tournament")
    await Tournament.objects.create(name="NEW Tournament")
    tournaments = await Tournament.objects.annotate(name_lower=Lower("name"))
    assert len(tournaments) == 2
    assert {t.name_lower for t in tournaments} == {"tournament", "new tournament"}


@pytest.mark.asyncio
async def test_filter_by_annotation_lower_unicode(db):
    """Lower() used to only case-fold the ASCII range on SQLite (its native LOWER() leaves
    Cyrillic/accented-Latin input unchanged), asymmetric with Upper() which already routes
    through a Unicode-aware UDF there - confirmed live."""
    await Tournament.objects.create(name="ПРИВЕТ Café")
    tournaments = await Tournament.objects.annotate(name_lower=Lower("name"))
    assert len(tournaments) == 1
    assert tournaments[0].name_lower == "привет café"


@pytest.mark.asyncio
async def test_filter_by_annotation_upper(db):
    await Tournament.objects.create(name="ToUrnAmEnT")
    await Tournament.objects.create(name="new TOURnament")
    tournaments = await Tournament.objects.annotate(name_upper=Upper("name"))
    assert len(tournaments) == 2
    assert {t.name_upper for t in tournaments} == {"TOURNAMENT", "NEW TOURNAMENT"}


@pytest.mark.asyncio
async def test_order_by_annotation(db):
    t1 = await Tournament.objects.create(name="Tournament")
    await Event.objects.create(name="event1", tournament=t1)
    await Event.objects.create(name="event2", tournament=t1)

    res = await Event.objects.filter(tournament=t1).annotate(max_id=Max("event_id")).order_by("-event_id")
    assert len(res) == 2
    assert res[0].event_id > res[1].event_id
    assert res[0].max_id == res[0].event_id
    assert res[1].max_id == res[1].event_id


@pytest.mark.asyncio
async def test_values_select_relation(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)
    assert await Event.objects.all().values("tournament") == [{"tournament": tournament.id}]


@pytest.mark.asyncio
async def test_values_select_relation_field(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)
    event_tournaments = await Event.objects.all().values("tournament__name")
    assert event_tournaments[0]["tournament__name"] == tournament.name


@pytest.mark.asyncio
async def test_values_select_relation_field_name_override(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)
    event_tournaments = await Event.objects.all().values(tour="tournament__name")
    assert event_tournaments[0]["tour"] == tournament.name


@pytest.mark.asyncio
async def test_values_list_select_relation_field(db):
    tournament = await Tournament.objects.create(name="New Tournament")
    await Event.objects.create(name="Test", tournament_id=tournament.id)
    event_tournaments = await Event.objects.all().values_list("tournament__name")
    assert event_tournaments[0][0] == tournament.name


@pytest.mark.asyncio
async def test_annotation_in_case_when(db):
    await Tournament.objects.create(name="Tournament")
    await Tournament.objects.create(name="NEW Tournament")
    tournaments = (
        await Tournament.objects.annotate(name_lower=Lower("name"))
        .annotate(is_tournament=Case(When(Q(name_lower="tournament"), then="yes"), default="no"))
        .filter(is_tournament="yes")
    )
    assert len(tournaments) == 1
    assert tournaments[0].name == "Tournament"
    assert tournaments[0].name_lower == "tournament"
    assert tournaments[0].is_tournament == "yes"


@pytest.mark.asyncio
async def test_f_annotation_filter(db):
    event = await IntFields.objects.create(intnum=1)

    ret_events = await IntFields.objects.annotate(intnum_plus_1=F("intnum") + 1).filter(intnum_plus_1=2)
    assert ret_events == [event]


@pytest.mark.asyncio
async def test_f_annotation_custom_filter(db):
    event = await IntFields.objects.create(intnum=1)

    base_query = IntFields.objects.annotate(intnum_plus_1=F("intnum") + 1)

    ret_events = await base_query.filter(intnum_plus_1__gt=1)
    assert ret_events == [event]

    ret_events = await base_query.filter(intnum_plus_1__lt=3)
    assert ret_events == [event]

    ret_events = await base_query.filter(Q(intnum_plus_1__gt=1) & Q(intnum_plus_1__lt=3))
    assert ret_events == [event]

    ret_events = await base_query.filter(intnum_plus_1__isnull=True)
    assert ret_events == []


@pytest.mark.asyncio
async def test_f_annotation_join(db):
    tournament_a = await Tournament.objects.create(name="A")
    tournament_b = await Tournament.objects.create(name="B")
    await Tournament.objects.create(name="C")
    event_a = await Event.objects.create(name="A", tournament=tournament_a)
    await Event.objects.create(name="B", tournament=tournament_b)

    events = await Event.objects.all().annotate(tournament_name=F("tournament__name")).filter(tournament_name="A")
    assert events == [event_a]


@pytest.mark.asyncio
async def test_f_annotation_custom_filter_requiring_join(db):
    tournament_a = await Tournament.objects.create(name="A")
    tournament_b = await Tournament.objects.create(name="B")
    await Tournament.objects.create(name="C")
    await Event.objects.create(name="A", tournament=tournament_a)
    event_b = await Event.objects.create(name="B", tournament=tournament_b)

    events = await Event.objects.all().annotate(tournament_name=F("tournament__name")).filter(tournament_name__gt="A")
    assert events == [event_b]


@pytest.mark.asyncio
async def test_f_annotation_custom_filter_requiring_nested_joins(db):
    tournament = await Tournament.objects.create(name="Tournament")

    second_tournament = await Tournament.objects.create(name="Tournament 2")

    event_first = await Event.objects.create(name="1", tournament=tournament)
    event_second = await Event.objects.create(name="2", tournament=second_tournament)
    await Event.objects.create(name="3", tournament=tournament)
    await Event.objects.create(name="4", tournament=second_tournament)

    team_first = await Team.objects.create(name="First")
    team_second = await Team.objects.create(name="Second")

    await team_first.events.add(event_first)
    await event_second.participants.add(team_second)

    res = await Tournament.objects.annotate(pname=F("events__participants__name")).filter(pname__startswith="Fir")
    assert res == [tournament]


@pytest.mark.asyncio
async def test_m2m_filter_multiple_relations_to_same_table(db):
    """Two M2M relations from same model to same target should produce correct results."""
    vanilla = await Flavor.objects.create(name="vanilla")
    chocolate = await Flavor.objects.create(name="chocolate")
    mint = await Flavor.objects.create(name="mint")

    latte = await Drink.objects.create(name="Latte")
    await latte.flavors.add(vanilla, chocolate)
    await latte.toppings.add(mint)

    mocha = await Drink.objects.create(name="Mocha")
    await mocha.flavors.add(chocolate)
    await mocha.toppings.add(chocolate, vanilla)

    # Filter on both M2M relations — different values
    result = await Drink.objects.filter(flavors__name="vanilla", toppings__name="mint")
    assert len(result) == 1
    assert result[0].name == "Latte"

    # Filter on both M2M relations — same value through different relations
    result = await Drink.objects.filter(flavors__name="chocolate", toppings__name="chocolate")
    assert len(result) == 1
    assert result[0].name == "Mocha"

    # Filter that should return no results
    result = await Drink.objects.filter(flavors__name="mint", toppings__name="vanilla")
    assert len(result) == 0


@pytest.mark.asyncio
async def test_chained_filter_calls_on_multi_valued_relation_use_separate_joins(db):
    """`.filter(participants__name="A").filter(participants__name="B")` (two SEPARATE calls
    naming DIFFERENT values on the SAME to-many relation) used to reuse one JOIN for both -
    `WHERE p.name='A' AND p.name='B'` is unsatisfiable for any single row, so this silently
    returned nothing even for an Event that genuinely has both participants (as two different
    rows). Matches Django's own documented "spanning multi-valued relationships" semantics: each
    SEPARATE .filter()/.exclude() call gets its own JOIN for a to-many relation, so this must
    match any Event having SOME participant named A AND SOME (possibly different) participant
    named B - unlike a single combined .filter(participants__name="A", participants__name="B")
    call, which Python's own kwargs can't even express twice under the same key, or
    .filter(Q(participants__name="A") & Q(participants__name="B")) in one call, which correctly
    keeps sharing one JOIN (both conditions really must hold on the SAME related row)."""
    tournament = await Tournament.objects.create(name="T")
    team_a = await Team.objects.create(name="A")
    team_b = await Team.objects.create(name="B")
    team_c = await Team.objects.create(name="C")

    both = await Event.objects.create(name="Both", tournament=tournament)
    await both.participants.add(team_a, team_b)

    only_a = await Event.objects.create(name="OnlyA", tournament=tournament)
    await only_a.participants.add(team_a, team_c)

    neither = await Event.objects.create(name="Neither", tournament=tournament)
    await neither.participants.add(team_c)

    chained = await Event.objects.filter(participants__name="A").filter(participants__name="B")
    assert {e.name for e in chained} == {"Both"}

    # A single combined call naming the relation twice via Q() must still correctly require
    # BOTH conditions on the SAME related row (unaffected by the chained-call fix above).
    same_row = await Event.objects.filter(Q(participants__name="A") & Q(participants__name="B"))
    assert {e.name for e in same_row} == set()

    # exclude() must get the same per-call join separation as filter().
    excluded = await Event.objects.filter(participants__name="A").exclude(participants__name="B")
    assert {e.name for e in excluded} == {"OnlyA"}


@pytest.mark.asyncio
async def test_aggregate_over_chained_filtered_multi_valued_relation_raises(db):
    """Count("participants") crossing the SAME to-many relation two SEPARATE .filter() calls
    also split into two JOINs (see test_chained_filter_calls_on_multi_valued_relation_use_
    separate_joins above) used to silently compute a WRONG count - the aggregate only sees one
    of the two JOINs, while the WHERE clause spanning both collapses the row set via a
    cross-product-then-filter the aggregate has no visibility into (an Event with both
    participants A and B genuinely present came back with pcount=1, not 2). No general way to
    make the aggregate see the "right" JOIN (there isn't a canonically correct one), so this
    must raise a clear ConfigurationError instead of returning a wrong number."""

    tournament = await Tournament.objects.create(name="T")
    team_a = await Team.objects.create(name="A")
    team_b = await Team.objects.create(name="B")

    both = await Event.objects.create(name="Both", tournament=tournament)
    await both.participants.add(team_a, team_b)

    with pytest.raises(QueryError, match="ambiguous"):
        await (
            Event.objects.annotate(pcount=Count("participants"))
            .filter(participants__name="A")
            .filter(participants__name="B")
        )


@pytest.mark.asyncio
async def test_aggregate_over_single_filter_call_on_multi_valued_relation_still_works(db):
    """The single-.filter()-call case (annotate + filter sharing ONE join, Django's own
    documented "Count only counts rows also matching the filter" behavior) must be entirely
    unaffected by the chained-call ambiguity guard above - only a SECOND, separate call on the
    SAME relation is the problem."""
    tournament = await Tournament.objects.create(name="T")
    team_a = await Team.objects.create(name="A")
    team_b = await Team.objects.create(name="B")

    both = await Event.objects.create(name="Both", tournament=tournament)
    await both.participants.add(team_a, team_b)

    results = await Event.objects.annotate(pcount=Count("participants")).filter(participants__name="A")
    assert [(e.name, e.pcount) for e in results] == [("Both", 1)]


@pytest.mark.asyncio
async def test_aggregate_with_one_call_naming_relation_via_q_still_works(db):
    """A single .filter(Q(...)) call (even wrapped in Q(), still ONE call -> ONE generation)
    never reaches the ambiguity-detection branch at all, so an aggregate over that same relation
    keeps working exactly as before."""
    tournament = await Tournament.objects.create(name="T")
    team_a = await Team.objects.create(name="A")
    team_b = await Team.objects.create(name="B")

    both = await Event.objects.create(name="Both", tournament=tournament)
    await both.participants.add(team_a, team_b)

    results = await Event.objects.annotate(pcount=Count("participants")).filter(Q(participants__name="A"))
    assert [(e.name, e.pcount) for e in results] == [("Both", 1)]


@test.requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_search_filter_raises_not_implemented_on_unsupported_backend(db):
    """__search on SQLite needs a FullTextIndex over the field - without one it must
    raise, not silently drop the WHERE clause and match every row."""
    with pytest.raises(UnSupportedError, match="declare a FullTextIndex over the field"):
        await IntFields.objects.filter(intnum__search="1")


##############################################################################
# Non-ASCII case-insensitive matching
#
# SQLite's own UPPER() only folds ASCII - "привет".upper() via SQLite's native function stays
# "привет" unchanged, unlike Postgres's locale-aware UPPER() - silently breaking
# __iexact/__icontains/__istartswith/__iendswith for Cyrillic and accented-Latin (é/ö/ü/ñ) text.
# hare.sql.functions.Upper now routes through a Python str.upper()-backed UDF on SQLite instead
# of SQLite's native UPPER(), so these must match correctly on both dialects.
##############################################################################


@pytest.mark.parametrize(
    "stored_name,filter_value",
    [
        ("Привет", "привет"),
        ("привет", "ПРИВЕТ"),
        ("café", "CAFÉ"),
        ("MÜLLER", "müller"),
    ],
)
@pytest.mark.asyncio
async def test_iexact_matches_non_ascii_case_insensitively(db, stored_name, filter_value):
    await Team.objects.create(name=stored_name)
    teams = await Team.objects.filter(name__iexact=filter_value)
    assert len(teams) == 1
    assert teams[0].name == stored_name


@pytest.mark.parametrize(
    "stored_name,filter_value",
    [
        ("Привет Мир", "ПРИВЕТ"),
        ("привет мир", "Мир"),
        ("Café Müller", "CAFÉ"),
        ("café müller", "MÜLLER"),
    ],
)
@pytest.mark.asyncio
async def test_icontains_matches_non_ascii_case_insensitively(db, stored_name, filter_value):
    await Team.objects.create(name=stored_name)
    teams = await Team.objects.filter(name__icontains=filter_value)
    assert len(teams) == 1
    assert teams[0].name == stored_name


@pytest.mark.parametrize(
    "stored_name,filter_value",
    [
        ("Привет Мир", "ПРИВЕТ"),
        ("привет мир", "Привет"),
        ("Café Müller", "CAFÉ"),
        ("café müller", "Café"),
    ],
)
@pytest.mark.asyncio
async def test_istartswith_matches_non_ascii_case_insensitively(db, stored_name, filter_value):
    await Team.objects.create(name=stored_name)
    teams = await Team.objects.filter(name__istartswith=filter_value)
    assert len(teams) == 1
    assert teams[0].name == stored_name


@pytest.mark.parametrize(
    "stored_name,filter_value",
    [
        ("Привет Мир", "МИР"),
        ("привет мир", "Мир"),
        ("Café Müller", "MÜLLER"),
        ("café müller", "Müller"),
    ],
)
@pytest.mark.asyncio
async def test_iendswith_matches_non_ascii_case_insensitively(db, stored_name, filter_value):
    await Team.objects.create(name=stored_name)
    teams = await Team.objects.filter(name__iendswith=filter_value)
    assert len(teams) == 1
    assert teams[0].name == stored_name


@pytest.mark.asyncio
async def test_upper_annotation_case_folds_non_ascii(db):
    """hare.query.functions.text.Upper (annotate) shares the same SQLite dialect fix as the __i* lookups -
    it must actually upper-case Cyrillic/accented-Latin text, not just pass it through unchanged
    the way SQLite's native UPPER() does."""
    await Team.objects.create(name="привет")
    await Team.objects.create(name="café")
    # Ordered by id (Team.Meta's own default ordering), not name - a cross-script sort order
    # (Cyrillic vs Latin) can legitimately differ between SQLite's byte-order collation and
    # Postgres's locale-aware one, which isn't what this test is about.
    teams = await Team.objects.annotate(name_upper=Upper("name")).order_by("id")
    assert [t.name_upper for t in teams] == ["ПРИВЕТ", "CAFÉ"]


@pytest.mark.asyncio
async def test_in_with_raw_sql_subquery_value_still_works(db):
    """Regression from is_in()/not_in()'s own None-in-list fix above: `field__in=<subquery>`
    (a RawSQL/Term/Subquery, not a literal Python list) must not be iterated over looking for
    embedded None values - Field/RawSQL's __getitem__ is overloaded for slice-based .between()
    building, so iterating it crashed with a confusing TypeError instead of building the IN
    (subquery) SQL the caller actually asked for."""
    root = await Category.objects.create(name="root")
    await Category.objects.create(name="other")

    result = await Category.objects.filter(id__in=RawSQL(f"SELECT {root.id}")).values_list("name", flat=True)
    assert list(result) == ["root"]


@pytest.mark.asyncio
async def test_rawsql_bare_int_param_gets_explicit_postgres_cast(db):
    """A bare RawSQL(...) parameter with nothing else in the surrounding SQL to infer its type
    from used to default to `text` on Postgres - asyncpg's strict binder refused to bind a
    Python int against it at all (OperationalError), while rust_pg's looser binder bound it but
    silently returned a str back instead of the real int. sqlite was always correct."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1)

    rows = await Book.objects.all().annotate(x=RawSQL("%s", [5])).values("id", "x")
    assert rows[0]["x"] == 5
    assert isinstance(rows[0]["x"], int)


@pytest.mark.asyncio
async def test_rawsql_bare_float_param_gets_explicit_postgres_cast(db):
    """Same gap as test_rawsql_bare_int_param_gets_explicit_postgres_cast, for a float param."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1)

    rows = await Book.objects.all().annotate(x=RawSQL("%s", [5.5])).values("id", "x")
    assert rows[0]["x"] == 5.5
    assert isinstance(rows[0]["x"], float)


@pytest.mark.asyncio
async def test_rawsql_bare_decimal_param_gets_explicit_postgres_cast(db):
    """Same gap as test_rawsql_bare_int_param_gets_explicit_postgres_cast, for a Decimal param -
    sqlite has no native decimal type and always round-trips a RawSQL literal with no Field
    context as a plain string (unrelated to this fix, unaffected by it either way), so only the
    numeric value - not the exact Python type - is checked there; the type itself is checked on
    Postgres, where rust_pg used to silently substitute a str for the real Decimal."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=1)

    rows = await Book.objects.all().annotate(x=RawSQL("%s", [Decimal("5.5")])).values("id", "x")
    assert Decimal(str(rows[0]["x"])) == Decimal("5.5")
    if Book._meta.connection.dialect.name == "postgresql":
        assert not isinstance(rows[0]["x"], str)


@pytest.mark.asyncio
async def test_rawsql_params_in_case_when_get_explicit_postgres_cast(db):
    """Same gap as test_rawsql_bare_int_param_gets_explicit_postgres_cast, for RawSQL(...)
    params used as a Case()/When()'s own condition/then/default branches - CASE WHEN rating > %s
    THEN %s ELSE %s END, each %s with nothing else around it to infer a type from."""
    author = await Author.objects.create(name="a")
    await Book.objects.create(name="alpha", author=author, rating=10)
    await Book.objects.create(name="beta", author=author, rating=1)

    rows = (
        await Book.objects.all()
        .annotate(
            bucket=Case(
                When(rating__gt=RawSQL("%s", [5]), then=RawSQL("%s", [1])),
                default=RawSQL("%s", [0]),
            )
        )
        .order_by("rating")
        .values("name", "bucket")
    )
    assert rows == [{"name": "beta", "bucket": 0}, {"name": "alpha", "bucket": 1}]


def test_like_aliased_sql_uses_dialect_quote_char():
    """Like.get_sql()'s aliased branch hardcoded double-quote identifier quoting instead of the
    dialect's real quote char - invisible on sqlite/postgres (both happen to use ") but wrong
    for a dialect that doesn't (e.g. MySQL's backtick style, simulated here directly)."""
    field = Field("name")
    like = Like(field, field.wrap_constant("%x%")).as_("matched")
    ctx = DEFAULT_SQL_CONTEXT.copy(with_alias=True, quote_char="`", alias_quote_char="`")

    assert like.get_sql(ctx) == "`name` LIKE '%x%' ESCAPE '\\' `matched`"


@pytest.mark.asyncio
async def test_raw_query_sql_returns_the_raw_text(db):
    """RawSQLQuery never overrode _make_query()/.sql() - calling .sql() on any .raw(sql)
    queryset always raised NotImplementedError from the base AwaitableQuery instead of just
    returning the raw SQL text the caller already gave it."""
    query = IntFields.objects.raw("SELECT * FROM intfields WHERE intnum = 99")
    assert query.sql() == "SELECT * FROM intfields WHERE intnum = 99"
    assert query.sql(parameters_inline=True) == "SELECT * FROM intfields WHERE intnum = 99"


@pytest.mark.asyncio
async def test_range_with_extra_elements_raises_clear_error(db):
    """between_and() indexed value[0]/value[1] without checking length - a `__range=(1, 10,
    999)` typo silently used only the first two elements instead of raising."""
    await IntFields.objects.create(intnum=5)
    with pytest.raises(ValueError, match="__range expects exactly 2 values"):
        await IntFields.objects.filter(intnum__range=(1, 10, 999)).values_list("intnum", flat=True)


@pytest.mark.asyncio
async def test_in_with_non_iterable_value_raises_clear_error(db):
    """list_encoder() (backing __in/__not_in/__range) used to iterate `values` unconditionally -
    a bare scalar (or None) crashed with a raw, confusing `TypeError: '...' object is not
    iterable` instead of a clear ORM error, before is_in()/not_in()/between_and() ever got a
    chance to run."""
    await IntFields.objects.create(intnum=5)

    with pytest.raises(UnSupportedError):
        await IntFields.objects.filter(intnum__in=None)
    with pytest.raises(UnSupportedError):
        await IntFields.objects.filter(intnum__in=3)
    with pytest.raises(UnSupportedError):
        await IntFields.objects.filter(intnum__not_in=3)


@pytest.mark.asyncio
async def test_range_with_non_iterable_value_raises_clear_error(db):
    await IntFields.objects.create(intnum=5)

    with pytest.raises(UnSupportedError):
        await IntFields.objects.filter(intnum__range=2)
    with pytest.raises(UnSupportedError):
        await IntFields.objects.filter(intnum__range=None)


@pytest.mark.asyncio
async def test_range_with_exactly_two_values_still_works(db):
    await IntFields.objects.create(intnum=5)
    await IntFields.objects.create(intnum=50)
    result = await IntFields.objects.filter(intnum__range=(1, 10)).values_list("intnum", flat=True)
    assert list(result) == [5]


@pytest.mark.asyncio
async def test_range_with_none_lower_bound_is_open_ended(db):
    """between_and() used to pass a None boundary straight into field.between(), which reaches
    Term.wrap_constant() -> NullValue() and renders `col BETWEEN NULL AND x` - UNKNOWN in SQL's
    3-valued logic, so it silently matched zero rows regardless of data. `(None, x)` should mean
    "no lower bound", i.e. `field <= x`."""
    await IntFields.objects.create(intnum=5)
    await IntFields.objects.create(intnum=10)
    await IntFields.objects.create(intnum=50)
    result = set(await IntFields.objects.filter(intnum__range=(None, 10)).values_list("intnum", flat=True))
    assert result == {5, 10}


@pytest.mark.asyncio
async def test_range_with_none_upper_bound_is_open_ended(db):
    """Mirror of the above: `(x, None)` should mean "no upper bound", i.e. `field >= x`."""
    await IntFields.objects.create(intnum=5)
    await IntFields.objects.create(intnum=10)
    await IntFields.objects.create(intnum=50)
    result = set(await IntFields.objects.filter(intnum__range=(10, None)).values_list("intnum", flat=True))
    assert result == {10, 50}


@pytest.mark.asyncio
async def test_range_with_both_none_matches_every_non_null_row(db):
    """`(None, None)` has neither a lower nor an upper bound - matches every non-NULL row, the
    same NULL handling a one-sided range already has."""
    await IntFields.objects.create(intnum=1, intnum_null=5)
    await IntFields.objects.create(intnum=2, intnum_null=50)
    await IntFields.objects.create(intnum=3, intnum_null=None)
    result = set(await IntFields.objects.filter(intnum_null__range=(None, None)).values_list("intnum", flat=True))
    assert result == {1, 2}


@pytest.mark.asyncio
async def test_range_exclude_is_symmetric_for_null_rows(db):
    """exclude() of any range keeps exactly the rows filter() drops - a NULL row included."""
    await IntFields.objects.create(intnum=1, intnum_null=None)
    await IntFields.objects.create(intnum=2, intnum_null=5)
    await IntFields.objects.create(intnum=3, intnum_null=50)
    all_ids = {1, 2, 3}
    for bounds, expected_filtered in (((None, None), {2, 3}), ((None, 10), {2}), ((1, None), {2, 3}), ((1, 10), {2})):
        filtered = set(await IntFields.objects.filter(intnum_null__range=bounds).values_list("intnum", flat=True))
        excluded = set(await IntFields.objects.exclude(intnum_null__range=bounds).values_list("intnum", flat=True))
        assert filtered == expected_filtered, bounds
        assert excluded == all_ids - expected_filtered, bounds


@pytest.mark.asyncio
async def test_isnull_rejects_non_bool_value(db, char_fields_data):
    """bool_encoder used to coerce via bare bool(value) - any non-empty Python string (e.g.
    "false") is truthy, so it silently inverted the filter instead of raising."""
    with pytest.raises(UnSupportedError):
        await CharFields.objects.filter(char_null__isnull="false").values_list("char", flat=True)
    with pytest.raises(UnSupportedError):
        await CharFields.objects.filter(char_null__not_isnull="false").values_list("char", flat=True)


@pytest.mark.asyncio
async def test_backward_fk_isnull_rejects_non_bool_value(db):
    """The backward-FK relation's own __isnull/__not_isnull skip bool_encoder entirely (its
    value_encoder is called as (value, model) there, not bool_encoder's (value, instance,
    field) shape) - is_null()/not_null() in hare/query/filters/lookups/lookups.py must reject a
    non-bool value themselves."""
    model = await CharPkModel.objects.create(id=17)
    await CharFkRelatedModel.objects.create(model=model)

    with pytest.raises(UnSupportedError):
        await CharPkModel.objects.filter(children__isnull="false").values_list("id", flat=True)
    with pytest.raises(UnSupportedError):
        await CharPkModel.objects.filter(children__not_isnull="false").values_list("id", flat=True)


def test_q_eq_accounts_for_negation():
    """Q.__eq__ compared children/join_type/filters but never _is_negated - Q(x=1) and
    ~Q(x=1) (same children/filters, opposite negation) were wrongly considered equal."""
    assert Q(x=1) != ~Q(x=1)
    assert Q(x=1) == Q(x=1)
    assert ~Q(x=1) == ~Q(x=1)


@pytest_asyncio.fixture
async def char_fields_data(db):
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa", char_null="baa")
    await CharFields.objects.create(char="oink")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "value", "expected"),
    [
        ("char_null__in", ["baa", None], {"baa", "moo", "oink"}),
        ("char_null__not_in", ["baa", None], set()),
        ("char_null__in", ["baa"], {"baa"}),
        ("char_null__not_in", ["baa"], {"moo", "oink"}),
        ("char_null__in", [None], {"moo", "oink"}),
        ("char_null__not_in", [None], {"baa"}),
        ("char__in", ["baa", None], {"baa"}),
        ("char__not_in", ["baa", None], {"moo", "oink"}),
    ],
    ids=[
        "in_with_none_in_list_matches_null_rows",
        "not_in_with_none_in_list_excludes_null_rows",
        "in_without_none_unaffected",
        "not_in_without_none_still_includes_null_rows",
        "in_with_only_none_matches_only_null_rows",
        "not_in_with_only_none_matches_only_non_null_rows",
        "in_with_none_on_non_nullable_field_does_not_raise",
        "not_in_with_none_on_non_nullable_field_does_not_raise",
    ],
)
async def test_in_not_in_none_handling(db, char_fields_data, lookup, value, expected):
    """A literal NULL inside a plain SQL IN(...)/NOT IN(...) list never matches/excludes a NULL
    column (3-valued logic) - a None embedded in field__in=[...]/field__not_in=[...] must
    instead translate to an IS NULL/IS NOT NULL check with the same semantics Python's own
    `in`/`not in` gives a list containing None. The last two cases (`char__in`/`char__not_in`,
    unlike the nullable `char_null` cases above) cover a distinct bug: `list_encoder`
    (hare/query/filters/lookups/value_encoders.py) used to run every list element, including None, through
    field.to_db_value() before is_in()/not_in() got a chance to strip it out - `char` has no
    `null=True`, so that raised ValidationError before the None-handling below ever ran, while
    `char_null`'s own to_db_value(None, ...) is a no-op that never exercised this path."""
    result = set(await CharFields.objects.filter(**{lookup: value}).values_list("char", flat=True))
    assert result == expected


@pytest.mark.asyncio
async def test_fk_shadow_column_in_with_none_on_non_nullable_relation_does_not_raise(db):
    """Same fix, plain `list_encoder` on a forward FK's shadow column - Event.tournament has
    no `null=True`, so `tournament_id__in=[id, None]` used to raise the same way
    `char__in=[...]` did above (a bare `tournament__in=...` isn't valid syntax here at all -
    `tournament` alone is a relation-traversal prefix, not this FK's own filter key)."""
    tournament = await Tournament.objects.create(name="Tournament")
    other_tournament = await Tournament.objects.create(name="Other")
    await Event.objects.create(name="1", tournament=tournament)
    await Event.objects.create(name="2", tournament=other_tournament)

    result = await Event.objects.filter(tournament_id__in=[tournament.id, None]).values_list("name", flat=True)
    assert list(result) == ["1"]

    excluded = await Event.objects.filter(tournament_id__not_in=[tournament.id, None]).values_list("name", flat=True)
    assert list(excluded) == ["2"]


@pytest.mark.asyncio
async def test_m2m_in_with_none_raises_clear_error(db):
    """`related_list_encoder` (get_m2m_filters registers it for a ManyToManyField's own
    `__in`/`__not_in`) used to raise a confusing ValidationError trying to run `None` through
    `to_db_value()`. It's not fixed the way `list_encoder` is above - is_in()/not_in() would
    translate the `None` into `IS NULL` on the through table's own join column, which for a
    many-to-many relation means "this row's join found no match at all" (the relation is
    empty), not "an explicit None value" - silently returning every event with zero
    participants would be actively wrong, not just unsupported. `None` now raises a clear,
    intentional UnSupportedError pointing at `__isnull=`/`__not_isnull=` instead.
    """
    tournament = await Tournament.objects.create(name="Tournament")
    event = await Event.objects.create(name="1", tournament=tournament)
    team = await Team.objects.create(name="Team")
    await event.participants.add(team)

    with pytest.raises(UnSupportedError, match="__isnull"):
        await Event.objects.filter(participants__in=[team, None]).values_list("name", flat=True)

    with pytest.raises(UnSupportedError, match="__isnull"):
        await Event.objects.filter(participants__not_in=[team, None]).values_list("name", flat=True)


# ---------------------------------------------------------------------------
# NOT IN (subquery) with NULL in the subquery's result set - a raw NULL embedded in a plain SQL
# IN(...)/NOT IN(...) container poisons the whole comparison to UNKNOWN for any non-matching row
# (3-valued logic), the same underlying trap the list-literal tests above already cover - but
# there a `None` in the Python list is known and stripped at BUILD time, while a subquery's own
# NULL rows are only known at RUN time. Reporter (nullable-FK target of Event.reporter) is a
# natural fit: an Event with no reporter assigned embeds a real NULL into the subquery's result.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def reporters_and_events(db):
    alice = await Reporter.objects.create(name="Alice")
    bob = await Reporter.objects.create(name="Bob")
    tournament = await Tournament.objects.create(name="Tournament")
    await Event.objects.create(name="e1", tournament=tournament, reporter=alice)
    await Event.objects.create(name="e2", tournament=tournament, reporter=None)
    return alice, bob


@pytest.mark.asyncio
async def test_not_in_lookup_with_subquery_containing_null_keeps_non_matching_rows(db, reporters_and_events):
    """`field__not_in=<QuerySet>` used to render a plain `NOT (id IN (subquery))` - Event's own
    NULL reporter_id (the e2 fixture row) poisoned that IN check to UNKNOWN for every reporter
    that isn't actually referenced by any event, silently excluding Bob (not just the reporters
    genuinely referenced by an event) instead of the reporters that ARE referenced."""
    __, bob = reporters_and_events
    result = await Reporter.objects.filter(
        id__not_in=Event.objects.all().values_list("reporter_id", flat=True)
    ).values_list("name", flat=True)
    assert list(result) == [bob.name]


@pytest.mark.asyncio
async def test_exclude_in_lookup_with_subquery_containing_null_keeps_non_matching_rows(db, reporters_and_events):
    """Same bug, reached through exclude()'s own Q negation (`_finalize_modifier`) instead of the
    not_in() operator directly - exclude(field__in=<subquery>) must behave identically to
    filter(field__not_in=<subquery>)."""
    __, bob = reporters_and_events
    result = await Reporter.objects.exclude(
        id__in=Event.objects.all().values_list("reporter_id", flat=True)
    ).values_list("name", flat=True)
    assert list(result) == [bob.name]


@pytest.mark.asyncio
async def test_negated_q_in_lookup_with_subquery_containing_null_keeps_non_matching_rows(db, reporters_and_events):
    """Same bug, reached through an explicit ~Q(field__in=<subquery>) filter argument."""
    __, bob = reporters_and_events
    result = await Reporter.objects.filter(
        ~Q(id__in=Event.objects.all().values_list("reporter_id", flat=True))
    ).values_list("name", flat=True)
    assert list(result) == [bob.name]


@pytest.mark.asyncio
async def test_exclude_in_lookup_with_explicit_subquery_containing_null(db, reporters_and_events):
    """Same as test_exclude_in_lookup_with_subquery_containing_null_keeps_non_matching_rows, but
    with the subquery wrapped explicitly in Subquery(...) instead of relying on the auto-wrap -
    Subquery(...) is itself resolved (built) eagerly through Q._get_actual_filter_params(), a
    different code path than the auto-wrapped bare QuerySet case above."""
    __, bob = reporters_and_events
    result = await Reporter.objects.exclude(
        id__in=Subquery(Event.objects.all().values_list("reporter_id", flat=True))
    ).values_list("name", flat=True)
    assert list(result) == [bob.name]


@pytest.mark.asyncio
async def test_not_in_lookup_with_subquery_without_null_still_excludes_matching_rows(db, reporters_and_events):
    """Regression control: when the subquery has no NULL rows at all, not_in() must still exclude
    a reporter that genuinely IS referenced."""
    alice, __ = reporters_and_events
    result = await Reporter.objects.filter(
        id__not_in=Event.objects.filter(reporter_id__isnull=False).values_list("reporter_id", flat=True)
    ).values_list("name", flat=True)
    assert alice.name not in result


# ---------------------------------------------------------------------------
# exclude()/~Q() over a direct (no relation crossed) nullable column - Django semantics: a row
# stays in exclude(cond)'s result whenever `cond` isn't definitely TRUE (FALSE or NULL/UNKNOWN
# both count), matching how a row with no related row at all already survives an exclude() that
# crosses a relation (Q._negate_across_joins, untouched by this fix). A bare `NOT (cond)` used to
# only implement the FALSE case, silently dropping a NULL row from both filter(cond) and
# exclude(cond) alike.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclude_direct_nullable_column_keeps_null_rows(db):
    root = await Category.objects.create(name="root", parent_id=None)
    child = await Category.objects.create(name="child", parent_id=root.id)
    other_child = await Category.objects.create(name="other-child", parent_id=root.id)

    result = set(await Category.objects.exclude(parent_id=root.id).values_list("name", flat=True))
    assert result == {"root"}
    assert child.name not in result
    assert other_child.name not in result


@pytest.mark.asyncio
async def test_negated_q_direct_nullable_column_keeps_null_rows(db):
    """Same fix, reached through an explicit ~Q(...) rather than exclude()."""
    root = await Category.objects.create(name="root", parent_id=None)
    await Category.objects.create(name="child", parent_id=root.id)

    result = set(await Category.objects.filter(~Q(parent_id=root.id)).values_list("name", flat=True))
    assert result == {"root"}


@pytest.mark.asyncio
async def test_exclude_direct_non_nullable_column_unaffected(db):
    """Regression control: excluding by a NON-nullable column must behave exactly as before (no
    NULL row could ever exist for it, so has_nullable_column stays False and the SQL is the same
    plain NOT as before)."""
    await Category.objects.create(name="root", parent_id=None)
    await Category.objects.create(name="other", parent_id=None)

    result = set(await Category.objects.exclude(name="root").values_list("name", flat=True))
    assert result == {"other"}


@pytest.mark.asyncio
async def test_exclude_compound_or_of_nullable_and_non_nullable_column(db):
    """Player.objects.exclude(Q(score=1) | Q(nick="zzz"))-shaped repro: a compound OR combining a
    non-nullable leaf (name) with a nullable one (parent_id) must still keep a row where the
    nullable leaf is NULL, matching Django's "row stays unless the WHOLE condition is definitely
    TRUE" semantics for the combined tree, not just for each leaf in isolation."""
    root = await Category.objects.create(name="root", parent_id=None)
    other = await Category.objects.create(name="other", parent_id=root.id)
    matching = await Category.objects.create(name="match-me", parent_id=root.id)

    result = set(
        await Category.objects.exclude(Q(name="match-me") | Q(parent_id=root.id)).values_list("name", flat=True)
    )
    assert result == {"root"}
    assert matching.name not in result
    assert other.name not in result


@pytest.mark.asyncio
async def test_exclude_direct_and_relation_crossing_nullable_fk_are_consistent(db):
    """Event.reporter (a forward FK's own shadow column, no JOIN needed for bare equality) and
    Event.reporter__name (crossing the relation, already NULL-safe via Q._negate_across_joins)
    must both keep an Event with no reporter assigned - the pre-fix inconsistency this bug
    report flagged between the two."""
    alice = await Reporter.objects.create(name="Alice")
    tournament = await Tournament.objects.create(name="Tournament")
    with_reporter = await Event.objects.create(name="with-reporter", tournament=tournament, reporter=alice)
    without_reporter = await Event.objects.create(name="without-reporter", tournament=tournament, reporter=None)

    direct_result = set(await Event.objects.exclude(reporter=alice).values_list("name", flat=True))
    relation_result = set(await Event.objects.exclude(reporter__name="Alice").values_list("name", flat=True))

    assert direct_result == {without_reporter.name}
    assert relation_result == {without_reporter.name}
    assert with_reporter.name not in direct_result
    assert with_reporter.name not in relation_result


# ---------------------------------------------------------------------------
# ~~Q (double negation) crossing a multi-valued relation (reverse FK/M2M) - Q.__invert__() used
# to toggle `_is_negated` back and forth, so a SECOND negation of an already-negated Q silently
# collapsed to a plain, positive JOIN instead of the NOT (NOT EXISTS(...)) the row semantics
# (`~~q == q`) and the already-correct single-negation NOT EXISTS behavior both require - two
# separate `~Q(reverse_relation__a), ~Q(reverse_relation__b)` conditions sharing one exclude()
# call's filter_call_generation then collided onto ONE shared JOIN alias, each needing to match
# the SAME related row instead of being independently satisfiable.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def reporters_with_events(db):
    tournament = await Tournament.objects.create(name="Tournament")
    both = await Reporter.objects.create(name="both")
    only_e1 = await Reporter.objects.create(name="only_e1")
    no_events = await Reporter.objects.create(name="no_events")
    await Event.objects.create(name="e1", tournament=tournament, reporter=both)
    await Event.objects.create(name="e2", tournament=tournament, reporter=both)
    await Event.objects.create(name="e1", tournament=tournament, reporter=only_e1)
    return both, only_e1, no_events


@pytest.mark.asyncio
async def test_exclude_two_double_negated_relation_conditions_in_one_call(db, reporters_with_events):
    """One exclude() call negates the conjunction of its conditions, like Django:
    exclude(~Q(e1), ~Q(e2)) is NOT (NOT e1 AND NOT e2) - every reporter with an "e1" or an "e2"
    event survives, each double-negated condition still resolved as NOT (NOT EXISTS(...))."""
    both, only_e1, __ = reporters_with_events
    result = set(
        await Reporter.objects.exclude(~Q(events__name="e1"), ~Q(events__name="e2")).values_list("name", flat=True)
    )
    assert result == {both.name, only_e1.name}


@pytest.mark.asyncio
async def test_exclude_two_double_negated_relation_conditions_chained(db, reporters_with_events):
    """Two separate exclude() calls each drop their own rows - only the reporter with BOTH an "e1"
    and an "e2" event survives (NOT (NOT EXISTS(...)) on both sides, not two accidentally
    non-colliding positive JOINs)."""
    both, __, __ = reporters_with_events
    result = set(
        await Reporter.objects.exclude(~Q(events__name="e1"))
        .exclude(~Q(events__name="e2"))
        .values_list("name", flat=True)
    )
    assert result == {both.name}


@pytest.mark.asyncio
async def test_negated_q_double_negation_over_relation_directly(db, reporters_with_events):
    """`~(~Q(...))` (same Q object negated twice) applied directly as a filter() argument, crossing
    a multi-valued relation - the ~~q == q row-result identity itself, forced through the
    relation-crossing rewrite instead of the cheap flag-toggle-back a non-relation condition uses."""
    both, only_e1, __ = reporters_with_events
    positive = set(await Reporter.objects.filter(events__name="e1").values_list("name", flat=True))
    double_negated = set(await Reporter.objects.filter(~(~Q(events__name="e1"))).values_list("name", flat=True))
    assert double_negated == positive == {both.name, only_e1.name}


@pytest.mark.asyncio
async def test_double_negation_over_relation_does_not_fan_out_duplicates(db):
    """Side effect the bug report flagged: collapsing to a plain positive JOIN for a doubly-negated
    relation condition fans out one row per matching related row - a reporter with TWO events both
    named "e1" would come back twice from a naive positive JOIN on events__name="e1".
    NOT (NOT EXISTS(...)) never fans out."""
    tournament = await Tournament.objects.create(name="Tournament")
    reporter = await Reporter.objects.create(name="double-match")
    await Event.objects.create(name="e1", tournament=tournament, reporter=reporter)
    await Event.objects.create(name="e1", tournament=tournament, reporter=reporter)

    result = await Reporter.objects.filter(~(~Q(events__name="e1"))).values_list("name", flat=True)
    assert list(result) == [reporter.name]


@pytest.mark.asyncio
async def test_single_negation_over_relation_unaffected_by_double_negation_fix(db, reporters_with_events):
    """Regression control: a single ~Q(...)/exclude(...) over a multi-valued relation (already
    correct before this fix) must be completely unaffected."""
    both, only_e1, no_events = reporters_with_events
    result = set(await Reporter.objects.exclude(events__name="e1").values_list("name", flat=True))
    assert result == {no_events.name}
    assert both.name not in result
    assert only_e1.name not in result


# ---------------------------------------------------------------------------
# field__not=F(nullable) - not_equal()'s `field.ne(value) | field.isnull()` only guards the LEFT
# side (`field`) against NULL; when the RIGHT side is itself an F()/expression that can be NULL
# for a given row, `field <> value` is UNKNOWN there too, and the row was silently dropped even
# though `field` (non-NULL) and `value` (NULL) obviously differ. Pair.left/Pair.right are both
# nullable FKs to the same model (Single), so their shadow columns (left_id/right_id) are two
# independently-nullable, directly comparable integer columns - a natural fit, no dedicated
# fixture model needed.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_not_equal_with_f_expression_null_on_the_right_is_kept(db):
    """The bug itself: left_id is NOT NULL, right_id (the F() target) IS NULL for this row -
    left_id__not=F("right_id") must still include it (NULL is obviously "distinct from" a real
    value), not silently drop it the way a bare `<>` would."""
    single = await Single.objects.create()
    pair = await Pair.objects.create(left=single, right=None)

    result = await Pair.objects.filter(left_id__not=F("right_id")).values_list("id", flat=True)
    assert list(result) == [pair.id]


@pytest.mark.asyncio
async def test_not_equal_with_f_expression_null_on_the_left_is_kept(db):
    """Mirror of the above - NULL on the LEFT side (already handled before this fix, via
    field.isnull() - regression control, not the bug itself)."""
    single = await Single.objects.create()
    pair = await Pair.objects.create(left=None, right=single)

    result = await Pair.objects.filter(left_id__not=F("right_id")).values_list("id", flat=True)
    assert list(result) == [pair.id]


@pytest.mark.asyncio
async def test_not_equal_with_f_expression_both_null_is_excluded(db):
    """Both sides NULL - IS DISTINCT FROM/IS NOT correctly treats two NULLs as equal (not
    distinct), so this row must be excluded, same as if both held the same real value."""
    await Pair.objects.create(left=None, right=None)

    result = await Pair.objects.filter(left_id__not=F("right_id")).values_list("id", flat=True)
    assert list(result) == []


@pytest.mark.asyncio
async def test_not_equal_with_f_expression_matches_full_truth_table(db):
    """The bug report's own truth table (a/b/c/d/e-shaped), condensed onto Pair's two nullable
    columns: equal-non-null and both-null are excluded, every other combination (including either
    side alone being NULL) is included."""
    single_one = await Single.objects.create()
    single_two = await Single.objects.create()
    equal_non_null = await Pair.objects.create(left=single_one, right=single_one)
    both_null = await Pair.objects.create(left=None, right=None)
    different_non_null = await Pair.objects.create(left=single_one, right=single_two)
    left_null_only = await Pair.objects.create(left=None, right=single_one)
    right_null_only = await Pair.objects.create(left=single_one, right=None)

    result = set(await Pair.objects.filter(left_id__not=F("right_id")).values_list("id", flat=True))
    assert result == {different_non_null.id, left_null_only.id, right_null_only.id}
    assert equal_non_null.id not in result
    assert both_null.id not in result


@pytest.mark.asyncio
async def test_not_equal_with_literal_value_unaffected_by_f_expression_fix(db, char_fields_data):
    """Regression control: field__not=<literal value> (not an F()/expression) must keep using the
    original field.ne(value) | field.isnull() shape/SQL - only a Term right-hand side is routed
    through IS DISTINCT FROM."""
    result = set(await CharFields.objects.filter(char_null__not="baa").values_list("char", flat=True))
    assert result == {"moo", "oink"}


@pytest.mark.asyncio
async def test_values_alias_of_annotation_does_not_leak_into_original_queryset(db):
    """FieldSelectQuery.add_field_to_select_query() must not mutate the _annotations dict it
    was constructed with - QuerySet.values()/values_list() pass it by reference, so writing a
    return_as alias into it in place would pollute the originating QuerySet (and any other
    query later derived from it), not just the .values() call that introduced the alias."""
    from hare.query.functions import Count
    from tests.testmodels import Author, Book

    author = await Author.objects.create(name="a")
    await Book.objects.create(name="b1", author=author, rating=1.0)
    await Book.objects.create(name="b2", author=author, rating=2.0)

    annotated = Author.objects.annotate(book_count=Count("books")).filter(name="a")
    original_annotations = annotated._annotations
    assert "renamed" not in original_annotations

    renamed_result = await annotated.values(renamed="book_count")
    assert renamed_result[0]["renamed"] == 2

    # the ORIGINAL queryset's _annotations dict must be untouched by the .values() call above
    assert "renamed" not in original_annotations
    assert "renamed" not in annotated._annotations

    # a second, independent .values() call off the same annotated queryset must still work
    plain_result = await annotated.values("book_count")
    assert plain_result[0]["book_count"] == 2


@pytest.mark.asyncio
async def test_bulk_soft_delete_composite_pk_does_not_crash(db):
    """mutation.py's DeleteQuery._execute_soft_delete()/_execute() pass model._meta.primary_key_attribute
    (a tuple for a composite PK) as a single positional arg to .values_list() - a real bug in
    isolation, but verified UNREACHABLE here: a composite-PK model can never be the target of an
    FK/O2O/M2M (enforced in apps.py), so get_backward_relations() is always empty for it and the
    buggy branch never runs. Kept as a permanent regression guard - this exact combination
    (bulk delete on a composite-PK model) had no prior test coverage."""
    await SoftDeleteComposite.objects.create(a=1, b=1, name="x")
    await SoftDeleteComposite.objects.create(a=1, b=2, name="y")
    count = await SoftDeleteComposite.objects.filter(name="x").delete()
    assert count == 1


@pytest.mark.asyncio
async def test_bulk_hard_delete_composite_pk_does_not_crash(db):
    await DirtyTrackedComposite.objects.create(a=1, b=1, name="x")
    await DirtyTrackedComposite.objects.create(a=1, b=2, name="y")
    count = await DirtyTrackedComposite.objects.filter(a=1).delete()
    assert count == 2


@pytest.mark.asyncio
async def test_aggregate_filter_on_two_hop_relation_keeps_its_join(db):
    """Aggregate(..., _filter=Q(...)) must merge the filter's own required joins back into the
    surrounding query - a filter that traverses beyond the aggregated relation itself
    (events__reporter__name, not just events__name) needs an extra JOIN the aggregation's own
    subquery doesn't already provide."""
    tournament = await Tournament.objects.create(name="T")
    reporter_a = await Reporter.objects.create(name="Alice")
    reporter_b = await Reporter.objects.create(name="Bob")
    await Event.objects.create(name="E1", tournament=tournament, reporter=reporter_a)
    await Event.objects.create(name="E2", tournament=tournament, reporter=reporter_b)
    await Event.objects.create(name="E3", tournament=tournament, reporter=reporter_a)

    result = (
        await Tournament.objects.all()
        .annotate(alice_events=Count("events", _filter=Q(events__reporter__name="Alice")))
        .first()
    )
    assert result.alice_events == 2


@pytest.mark.asyncio
async def test_not_none_matches_only_non_null_rows(db):
    """field__not=None must mean IS NOT NULL - previously (before Term.__ne__'s own None fix)
    it matched only NULL rows (the opposite), and after that fix alone it would have matched
    every row (tautology) without this lookup's own None special-case."""
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa", char_null="baa")
    await CharFields.objects.create(char="oink")

    result = set(await CharFields.objects.filter(char_null__not=None).values_list("char", flat=True))
    assert result == {"baa"}


@pytest.mark.asyncio
async def test_not_none_excludes_nothing_when_all_non_null(db):
    await CharFields.objects.create(char="moo", char_null="x")
    await CharFields.objects.create(char="baa", char_null="y")

    result = set(await CharFields.objects.filter(char_null__not=None).values_list("char", flat=True))
    assert result == {"moo", "baa"}


@pytest.mark.asyncio
async def test_not_non_none_value_still_includes_nulls(db):
    """Regression control: field__not=<real value> must keep matching NULL rows too (not just
    "not equal"), unaffected by the None-specific fix above."""
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa", char_null="baa")
    await CharFields.objects.create(char="oink", char_null="oink")

    result = set(await CharFields.objects.filter(char_null__not="oink").values_list("char", flat=True))
    assert result == {"moo", "baa"}


@pytest.mark.asyncio
async def test_f_forward_fk_terminal_field_resolves_correct_table(db):
    """F("author") on Book (a forward-FK relation used as a bare, single-segment path) used to
    build its JOIN against an aliased "book__author" table but then read the term from the
    unaliased "author" table - a table never actually joined under that name, breaking with
    'no such column: author.id' (sqlite) / 'missing FROM-clause entry' (postgres)."""
    author = await Author.objects.create(name="A1")
    await Book.objects.create(name="B1", author=author, rating=4.5)

    rows = await Book.objects.all().annotate(author_ref=F("author")).values("id", "author_ref")
    assert rows[0]["author_ref"] == author.id


@pytest.mark.asyncio
async def test_self_referential_backward_relation_two_hops_deep(db):
    """team_members__team_members__name (self-referential backward FK, traversed 2 hops) used to
    thread the wrong (unaliased) table into the second hop's join, colliding with the first hop's
    already-aliased table and crashing with hare.sql's "Self-join requires an explicit alias"."""
    boss = await Employee.objects.create(name="Boss")
    mid = await Employee.objects.create(name="Mid", manager=boss)
    await Employee.objects.create(name="Leaf", manager=mid)

    result = await Employee.objects.filter(team_members__team_members__name="Leaf")
    assert [e.id for e in result] == [boss.id]


LARGE_IN_LIST_LENGTH = SQLITE_IN_JSON_ARRAY_THRESHOLD + 500

LARGE_IN_LIST_CASES = {
    "int": (IntFields, "intnum", {}, 7, lambda index: -index - 1),
    "char": (CharFields, "char", {}, "h\u00e9llo", lambda index: f"filler-{index}"),
    "decimal": (
        DecimalFields,
        "decimal",
        {"decimal_nodec": 1},
        Decimal("1.5000"),
        lambda index: Decimal(index) + Decimal("1000.25"),
    ),
    "float": (FloatFields, "floatnum", {}, 2.5, lambda index: index + 1000.5),
    "date": (
        DateFields,
        "date",
        {},
        datetime.date(2020, 1, 2),
        lambda index: datetime.date(1900, 1, 1) + datetime.timedelta(days=index),
    ),
    "datetime": (
        DatetimeFields,
        "datetime",
        {},
        datetime.datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=datetime.UTC),
        lambda index: datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC) + datetime.timedelta(seconds=index),
    ),
    "uuid": (
        UUIDFields,
        "data",
        {},
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        lambda index: uuid.UUID(int=index + 1),
    ),
    "binary": (BinaryFields, "binary", {}, b"\x00\x01ab", lambda index: b"z" + index.to_bytes(4, "big")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case_name", list(LARGE_IN_LIST_CASES))
async def test_large_in_list_matches_like_a_short_one(db, case_name):
    model, field_name, extra_kwargs, matching_value, get_filler_value = LARGE_IN_LIST_CASES[case_name]
    await model.objects.create(**{field_name: matching_value}, **extra_kwargs)
    await model.objects.create(**{field_name: get_filler_value(LARGE_IN_LIST_LENGTH + 10)}, **extra_kwargs)
    values = [get_filler_value(index) for index in range(LARGE_IN_LIST_LENGTH)] + [matching_value]

    assert await model.objects.filter(**{f"{field_name}__in": [matching_value]}).count() == 1
    assert await model.objects.filter(**{f"{field_name}__in": values}).count() == 1
    assert await model.objects.filter(**{f"{field_name}__not_in": values}).count() == 1
    assert await model.objects.exclude(**{f"{field_name}__in": values}).count() == 1
    assert await model.objects.filter(Q(**{f"{field_name}__in": values}) | Q(**{field_name: None})).count() == 1


@test.requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_large_in_list_binds_one_json_array(db):
    values = list(range(LARGE_IN_LIST_LENGTH))
    sql = IntFields.objects.filter(intnum__in=values).sql()
    assert "json_each(?)" in sql
    assert sql.count("?") == 1
    assert (
        "json_each(?)" not in IntFields.objects.filter(intnum__in=values[: SQLITE_IN_JSON_ARRAY_THRESHOLD - 1]).sql()
    )


@pytest.mark.asyncio
async def test_in_list_longer_than_bind_parameter_limit(db):
    await IntFields.objects.create(id=1, intnum=5)
    await IntFields.objects.create(id=2, intnum=-5)
    await IntFields.objects.create(id=3, intnum_null=None, intnum=100000)
    values = list(range(40000))
    assert await IntFields.objects.filter(intnum__in=values).count() == 1
    assert await IntFields.objects.filter(intnum__in=[*values, None]).count() == 1
    assert await IntFields.objects.filter(intnum_null__in=[*values, None]).count() == 3
    assert await IntFields.objects.filter(intnum__not_in=values).order_by("id").values_list("id", flat=True) == [2, 3]
    assert await IntFields.objects.exclude(intnum__in=values).order_by("id").values_list("id", flat=True) == [2, 3]
    assert await IntFields.objects.filter(intnum__in=values).update(intnum_null=1) == 1
    assert await IntFields.objects.filter(intnum_null=1).values_list("id", flat=True) == [1]
    assert await IntFields.objects.filter(intnum__in=values).delete() == 1
    assert await IntFields.objects.all().order_by("id").values_list("id", flat=True) == [2, 3]


@pytest.mark.asyncio
async def test_large_bool_in_list(db):
    await BooleanFields.objects.create(boolean=True)
    await BooleanFields.objects.create(boolean=False)
    values = [False] * LARGE_IN_LIST_LENGTH + [True]
    assert await BooleanFields.objects.filter(boolean__in=values).count() == 2
    assert await BooleanFields.objects.filter(boolean__not_in=[False] * LARGE_IN_LIST_LENGTH).count() == 1


@pytest.mark.asyncio
async def test_lookup_on_forward_relation_name_uses_its_key_column(db):
    first_tournament = await Tournament.objects.create(name="first")
    second_tournament = await Tournament.objects.create(name="second")
    third_tournament = await Tournament.objects.create(name="third")
    first_event = await Event.objects.create(name="e1", tournament=first_tournament)
    second_event = await Event.objects.create(name="e2", tournament=second_tournament)
    await Event.objects.create(name="e3", tournament=third_tournament)
    await Reporter.objects.create(name="nobody")
    reporter = await Reporter.objects.create(name="somebody")
    first_event.reporter = reporter
    await first_event.save()
    events = Event.objects.all().order_by("name")

    assert await events.filter(tournament__in=[first_tournament, second_tournament]).values_list(
        "name", flat=True
    ) == ["e1", "e2"]
    assert await events.filter(tournament__in=[first_tournament.pk]).values_list("name", flat=True) == ["e1"]
    assert await events.filter(tournament__not_in=[first_tournament]).values_list("name", flat=True) == ["e2", "e3"]
    assert await events.exclude(tournament__in=[first_tournament]).values_list("name", flat=True) == ["e2", "e3"]
    assert await events.filter(reporter__isnull=True).values_list("name", flat=True) == ["e2", "e3"]
    assert await events.filter(reporter__not_isnull=True).values_list("name", flat=True) == ["e1"]
    assert await events.filter(reporter__in=[reporter, None]).values_list("name", flat=True) == ["e1", "e2", "e3"]
    assert await events.filter(Q(tournament__in=[second_tournament]) | Q(reporter__isnull=False)).values_list(
        "name", flat=True
    ) == ["e1", "e2"]
    assert await events.annotate(
        is_second=Case(When(tournament__in=[second_tournament], then=1), default=0)
    ).values_list("is_second", flat=True) == [0, 1, 0]
    assert await events.filter(tournament__in=Tournament.objects.filter(name="third")).values_list(
        "name", flat=True
    ) == ["e3"]
    assert await events.filter(tournament__name="first").values_list("name", flat=True) == ["e1"]
    assert await events.filter(tournament__in=[first_tournament]).update(name="e1-renamed") == 1
    assert await Event.objects.filter(reporter__isnull=True).delete() == 2
    assert await Event.objects.all().values_list("name", flat=True) == ["e1-renamed"]
    assert second_event.pk not in await Event.objects.all().values_list("pk", flat=True)


@pytest.mark.asyncio
async def test_lookup_on_forward_relation_name_with_to_field(db):
    parent = await ProtectedParentWithCode.objects.create(id=1, code=555)
    other_parent = await ProtectedParentWithCode.objects.create(id=2, code=777)
    await ProtectedChildByCode.objects.create(name="child", parent=parent)
    await ProtectedChildByCode.objects.create(name="other", parent=other_parent)
    assert await ProtectedChildByCode.objects.filter(parent__in=[parent]).values_list("name", flat=True) == ["child"]
    assert await ProtectedChildByCode.objects.filter(parent__in=[555]).values_list("name", flat=True) == ["child"]
    assert await ProtectedChildByCode.objects.filter(
        parent__in=ProtectedParentWithCode.objects.filter(code=777)
    ).values_list("name", flat=True) == ["other"]


@pytest.mark.asyncio
async def test_lookup_on_forward_relation_name_rejects_bad_values(db):
    tournament = await Tournament.objects.create(name="first")
    await Event.objects.create(name="e1", tournament=tournament)
    with pytest.raises(QueryError, match="unsaved"):
        await Event.objects.filter(tournament__in=[Tournament(name="unsaved")])
    with pytest.raises(QueryError, match="expects Tournament instances"):
        await Event.objects.filter(tournament__in=[await Reporter.objects.create(name="wrong model")])


@pytest.mark.asyncio
async def test_lookup_on_composite_forward_relation_name_compares_the_whole_key(db):
    document = await VersionedDocument.objects.create(title="v1")
    other_document = await VersionedDocument.objects.create(title="v2")
    note = await DocumentRevisionNote.objects.create(document=document, note="note")
    await DocumentRevisionNote.objects.create(document=other_document, note="other")
    assert [row.id for row in await DocumentRevisionNote.objects.filter(document__in=[document])] == [note.id]
    assert [row.id for row in await DocumentRevisionNote.objects.filter(document=document.pk)] == [note.id]
    assert [
        row.id
        for row in await DocumentRevisionNote.objects.filter(document__in=VersionedDocument.objects.filter(title="v1"))
    ] == [note.id]
    assert await DocumentRevisionNote.objects.filter(document__isnull=True).count() == 0


# Case-insensitive lookups and Upper()/Lower() map character by character, like Postgres: a
# character whose full case mapping expands (ß -> SS, ﬀ -> FF) keeps its simple mapping or stays.


@pytest_asyncio.fixture
async def unicode_case_authors(db):
    for name in ("Straße", "STRASSE", "ﬀ", "İstanbul", "istanbul", "ıi", "Привет", "ПРИВЕТ", "ёлка"):
        await Author.objects.create(name=name)


async def get_author_names(queryset) -> set[str]:
    return set(await queryset.values_list("name", flat=True))


@pytest.mark.asyncio
async def test_case_insensitive_lookups_map_character_by_character(unicode_case_authors):
    assert await get_author_names(Author.objects.filter(name__iexact="straße")) == {"Straße"}
    assert await get_author_names(Author.objects.filter(name__iexact="strasse")) == {"STRASSE"}
    assert await get_author_names(Author.objects.filter(name__icontains="ss")) == {"STRASSE"}
    assert await get_author_names(Author.objects.filter(name__icontains="FF")) == set()
    assert await get_author_names(Author.objects.filter(name__iexact="istanbul")) == {"istanbul"}
    assert await get_author_names(Author.objects.filter(name__istartswith="İ")) == {"İstanbul"}
    assert await get_author_names(Author.objects.filter(name__iendswith="I")) == {"ıi"}
    assert await get_author_names(Author.objects.filter(name__iexact="привет")) == {"Привет", "ПРИВЕТ"}
    assert await get_author_names(Author.objects.filter(name__icontains="ЁЛ")) == {"ёлка"}


@pytest.mark.asyncio
async def test_upper_lower_map_character_by_character(unicode_case_authors):
    assert await get_author_names(Author.objects.annotate(upper_name=Upper("name")).filter(upper_name="ﬀ")) == {"ﬀ"}
    assert await get_author_names(Author.objects.annotate(upper_name=Upper("name")).filter(upper_name="II")) == {"ıi"}
    assert await get_author_names(Author.objects.annotate(lower_name=Lower("name")).filter(lower_name="istanbul")) == {
        "İstanbul",
        "istanbul",
    }
    assert await get_author_names(Author.objects.annotate(lower_name=Lower("name")).filter(lower_name="привет")) == {
        "Привет",
        "ПРИВЕТ",
    }


@pytest.mark.asyncio
async def test_iexact_none_means_isnull(db):
    """Like Django, `field__iexact=None` is `field__isnull=True`, not an error."""
    await Tournament.objects.create(id=1, name="with desc", desc="x")
    await Tournament.objects.create(id=2, name="without desc")

    assert await Tournament.objects.filter(desc__iexact=None).values_list("name", flat=True) == ["without desc"]
    assert await Tournament.objects.exclude(desc__iexact=None).values_list("name", flat=True) == ["with desc"]


async def get_int_field_numbers(**filter_kwargs) -> list[int]:
    return sorted(await IntFields.objects.filter(**filter_kwargs).values_list("intnum", flat=True))


@pytest.mark.asyncio
async def test_in_accepts_any_iterable(db):
    """Like Django, `__in`/`__not_in` take any iterable, not only a list/tuple/set."""
    for number in range(1, 5):
        await IntFields.objects.create(intnum=number)

    assert await get_int_field_numbers(intnum__in=(number for number in (1, 2))) == [1, 2]
    assert await get_int_field_numbers(intnum__in=frozenset({2, 3})) == [2, 3]
    assert await get_int_field_numbers(intnum__in=range(3, 10)) == [3, 4]
    assert await get_int_field_numbers(intnum__in={1: "a", 4: "b"}.keys()) == [1, 4]
    assert await get_int_field_numbers(intnum__in={"a": 2}.values()) == [2]
    assert await get_int_field_numbers(intnum__not_in=(number for number in (1, 2))) == [3, 4]
    assert await get_int_field_numbers(intnum__in=(number for number in ())) == []
    assert await IntFields.objects.filter(Q(intnum__in=iter([4]))).values_list("intnum", flat=True) == [4]
    assert await IntFields.objects.exclude(intnum__in=iter([1, 2, 3])).values_list("intnum", flat=True) == [4]


@pytest.mark.asyncio
async def test_in_generator_consumed_once_across_query_shape_cache(db):
    """A generator is read exactly once, and a later call with a list of the same length reuses
    the cached query shape with its own values."""
    for number in range(1, 5):
        await IntFields.objects.create(intnum=number)

    assert await get_int_field_numbers(intnum__in=(number for number in (1, 2))) == [1, 2]
    assert await get_int_field_numbers(intnum__in=(number for number in (3, 4))) == [3, 4]
    assert await get_int_field_numbers(intnum__in=[1, 4]) == [1, 4]


@pytest.mark.asyncio
async def test_in_large_generator(db):
    """A generator longer than SQLite's one-parameter-per-value limit still binds as one list."""
    await IntFields.objects.create(intnum=5)
    await IntFields.objects.create(intnum=LARGE_IN_LIST_LENGTH + 10)

    assert await get_int_field_numbers(intnum__in=(number for number in range(LARGE_IN_LIST_LENGTH))) == [5]
    assert await get_int_field_numbers(intnum__not_in=(number for number in range(LARGE_IN_LIST_LENGTH))) == [
        LARGE_IN_LIST_LENGTH + 10
    ]


@pytest.mark.parametrize("value", ["12", b"12", bytearray(b"12")])
def test_in_rejects_string_value(db, value):
    """A string would otherwise be matched character by character - rejected when filter() is
    called on a model set up (before Hare.init() the call is kept and rejected by it)."""
    with pytest.raises(UnSupportedError, match="expected an iterable of values"):
        IntFields.objects.filter(intnum__in=value)
    with pytest.raises(UnSupportedError, match="expected an iterable of values"):
        Q(intnum__not_in=value)


# One exclude() call drops the rows matching ALL of its conditions - NOT (a AND b), like Django and
# like exclude(Q(a, b)) - also when the conditions cross the same multi-valued relation.


@pytest_asyncio.fixture
async def authors_with_books(db):
    first = await Author.objects.create(name="first")
    second = await Author.objects.create(name="second")
    third = await Author.objects.create(name="third")
    await Author.objects.create(name="no books")
    await Book.objects.create(name="a", author=first, rating=1)
    await Book.objects.create(name="b", author=first, rating=2)
    await Book.objects.create(name="a", author=second, rating=3)
    await Book.objects.create(name="c", author=third, rating=5)
    await Book.objects.create(name="a", author=third, rating=5)
    return first


@pytest.mark.asyncio
async def test_exclude_with_several_kwargs_negates_their_conjunction(authors_with_books):
    first = authors_with_books

    assert await get_author_names(Author.objects.exclude(name="first", id=first.id + 1)) == {
        "first",
        "second",
        "third",
        "no books",
    }
    assert await get_author_names(Author.objects.exclude(name="first", id=first.id)) == {"second", "third", "no books"}


@pytest.mark.asyncio
async def test_exclude_with_several_kwargs_across_multi_valued_relation(authors_with_books):
    expected = {"first", "second", "no books"}

    assert await get_author_names(Author.objects.exclude(books__name="a", books__rating=5)) == expected
    assert await get_author_names(Author.objects.exclude(Q(books__name="a", books__rating=5))) == expected
    assert await get_author_names(Author.objects.exclude(Q(books__name="a"), Q(books__rating=5))) == expected
    assert await get_author_names(Author.objects.exclude(Q(books__name="a"), books__rating=5)) == expected
    # Separate exclude() calls still exclude each condition on its own.
    assert await get_author_names(Author.objects.exclude(books__name="a").exclude(books__rating=5)) == {"no books"}


@pytest.mark.asyncio
async def test_exclude_with_several_kwargs_on_nullable_columns(db):
    await Tournament.objects.create(id=1, name="a", desc="x")
    await Tournament.objects.create(id=2, name="a")
    await Tournament.objects.create(id=3, name="b", desc="x")

    assert sorted(await Tournament.objects.exclude(name="a", desc="x").values_list("id", flat=True)) == [2, 3]


# An empty Q() is a no-op, negated or not, like Django.


@pytest.mark.asyncio
async def test_empty_q_is_no_op(db):
    await Author.objects.create(name="first")
    await Author.objects.create(name="second")
    everyone = {"first", "second"}

    assert await get_author_names(Author.objects.filter(~Q())) == everyone
    assert await get_author_names(Author.objects.exclude(Q())) == everyone
    assert await get_author_names(Author.objects.filter(~Q() & Q(name="first"))) == {"first"}
    assert await get_author_names(Author.objects.filter(Q(~Q()) & Q(name="first"))) == {"first"}
    assert await get_author_names(Author.objects.filter(~Q() | Q(name="first"))) == {"first"}
    assert await get_author_names(Author.objects.filter(~Q(~Q()))) == everyone
    assert await get_author_names(Author.objects.filter(~(~Q() | Q(name="first")))) == {"second"}
    assert await get_author_names(Author.objects.exclude(~Q(), name="first")) == {"second"}


@pytest.mark.asyncio
async def test_empty_q_inside_case_and_aggregate_filter(db):
    first = await Author.objects.create(name="first")
    await Author.objects.create(name="second")
    await Book.objects.create(name="a", author=first, rating=1)

    book_counts = await Author.objects.annotate(book_count=Count("books", _filter=~Q())).values_list(
        "name", "book_count"
    )
    assert sorted(book_counts) == [("first", 1), ("second", 0)]
    flags = await Author.objects.annotate(flag=Case(When(~Q() & Q(name="first"), then=1), default=0)).values_list(
        "name", "flag"
    )
    assert sorted(flags) == [("first", 1), ("second", 0)]
