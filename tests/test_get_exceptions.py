"""get()'s two exception parameters: ``does_not_exist_exception`` (no matching row) and
``multiple_objects_returned_exception`` (more than one) - the standard exception, an exception class
or instance raised instead, or None (None for no row; one row read without counting the rest) - on
every way of calling get(), and the types its overloads give."""

from collections.abc import Callable
from typing import Any, assert_type

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import DoesNotExist, MultipleObjectsReturned
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.query.enums import GetException
from hare.query.expressions import Q
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.transactions.transactions import Transactions
from tests.testmodels import Event, Team, Tournament


class MissingRow(Exception):
    pass


class TooManyRows(Exception):
    pass


MISSING_ROW_INSTANCE = MissingRow("no row")
TOO_MANY_ROWS_INSTANCE = TooManyRows("rows")

#: The value of each exception parameter, and what it does for the case it covers.
DOES_NOT_EXIST_CHOICES = {
    "standard": GetException.STANDARD,
    "class": MissingRow,
    "instance": MISSING_ROW_INSTANCE,
    "none": None,
}
MULTIPLE_OBJECTS_RETURNED_CHOICES = {
    "standard": GetException.STANDARD,
    "class": TooManyRows,
    "instance": TOO_MANY_ROWS_INSTANCE,
    "none": None,
}


async def create_rows() -> tuple[Tournament, Event]:
    """Names matching one row ("one"), two ("two") and none ("none") - among tournaments, the events
    of one tournament and the teams of one event."""
    tournament = await Tournament.objects.create(id=1, name="one")
    await Tournament.objects.create(id=2, name="two")
    await Tournament.objects.create(id=3, name="two")
    event = await Event.objects.create(event_id=1, name="one", tournament=tournament)
    await Event.objects.create(event_id=2, name="two", tournament=tournament)
    await Event.objects.create(event_id=3, name="two", tournament=tournament)
    teams = [await Team.objects.create(id=number, name=name) for number, name in ((1, "one"), (2, "two"), (3, "two"))]
    await event.participants.add(*teams)
    return tournament, event


def get_entry_points(tournament: Tournament, event: Event) -> dict[str, Callable[..., Any]]:
    """Each way of calling get(), as ``(name, **parameters) -> awaitable``; the result's ``name``
    (or ``["name"]``) is the matched row's."""
    return {
        "manager": lambda name, **parameters: Tournament.objects.get(name=name, **parameters),
        "queryset": lambda name, **parameters: Tournament.objects.all().get(name=name, **parameters),
        "filtered_queryset": lambda name, **parameters: Tournament.objects.filter(id__gte=1).get(
            name=name, **parameters
        ),
        "q_condition": lambda name, **parameters: Tournament.objects.get(Q(name=name), **parameters),
        "values": lambda name, **parameters: Tournament.objects.values("name").get(name=name, **parameters),
        "values_list_flat": lambda name, **parameters: Tournament.objects.values_list("name", flat=True).get(
            name=name, **parameters
        ),
        "union": lambda name, **parameters: (
            Tournament.objects.filter(id=1).union(Tournament.objects.filter(id__gte=2)).get(name=name, **parameters)
        ),
        "reverse_foreign_key": lambda name, **parameters: tournament.events.get(name=name, **parameters),
        "many_to_many": lambda name, **parameters: event.participants.get(name=name, **parameters),
    }


def get_name(row: Any) -> str:
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        return row["name"]
    return row.name


ENTRY_POINT_NAMES = (
    "manager",
    "queryset",
    "filtered_queryset",
    "q_condition",
    "values",
    "values_list_flat",
    "union",
    "reverse_foreign_key",
    "many_to_many",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point_name", ENTRY_POINT_NAMES)
@pytest.mark.parametrize("does_not_exist_choice", DOES_NOT_EXIST_CHOICES)
@pytest.mark.parametrize("multiple_objects_returned_choice", MULTIPLE_OBJECTS_RETURNED_CHOICES)
async def test_get_handles_no_one_and_several_rows_by_its_exception_parameters(
    db, entry_point_name, does_not_exist_choice, multiple_objects_returned_choice
):
    tournament, event = await create_rows()
    get = get_entry_points(tournament, event)[entry_point_name]
    parameters: dict[str, Any] = {}
    if does_not_exist_choice != "standard":
        parameters["does_not_exist_exception"] = DOES_NOT_EXIST_CHOICES[does_not_exist_choice]
    if multiple_objects_returned_choice != "standard":
        parameters["multiple_objects_returned_exception"] = MULTIPLE_OBJECTS_RETURNED_CHOICES[
            multiple_objects_returned_choice
        ]

    # One row: returned, whatever the parameters.
    assert get_name(await get("one", **parameters)) == "one"

    # No row.
    if does_not_exist_choice == "none":
        assert await get("none", **parameters) is None
    else:
        expected_exception = {"standard": DoesNotExist, "class": MissingRow, "instance": MissingRow}[
            does_not_exist_choice
        ]
        with pytest.raises(expected_exception) as raised:
            await get("none", **parameters)
        if does_not_exist_choice == "instance":
            assert raised.value is MISSING_ROW_INSTANCE

    # Two rows.
    if multiple_objects_returned_choice == "none":
        assert get_name(await get("two", **parameters)) == "two"
    else:
        expected_exception = {"standard": MultipleObjectsReturned, "class": TooManyRows, "instance": TooManyRows}[
            multiple_objects_returned_choice
        ]
        with pytest.raises(expected_exception) as raised:
            await get("two", **parameters)
        if multiple_objects_returned_choice == "instance":
            assert raised.value is TOO_MANY_ROWS_INSTANCE


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point_name", ENTRY_POINT_NAMES)
async def test_get_twice_on_the_same_key_keeps_each_calls_own_parameters(db, entry_point_name):
    """The second get() of a key runs on the plan the first one kept - with its own parameters."""
    tournament, event = await create_rows()
    get = get_entry_points(tournament, event)[entry_point_name]
    for _ in range(3):
        with pytest.raises(MultipleObjectsReturned):
            await get("two")
        assert get_name(await get("two", multiple_objects_returned_exception=None)) == "two"
        with pytest.raises(DoesNotExist):
            await get("none")
        assert await get("none", does_not_exist_exception=None) is None
        with pytest.raises(MissingRow):
            await get("none", does_not_exist_exception=MissingRow)


@pytest.mark.asyncio
async def test_get_without_counting_reads_one_row(db):
    """Without counting, get() asks for one row; counting, for two - the bound integer of a query
    filtering by a name is its row limit."""
    await create_rows()
    executed: list[QueryExecuted] = []
    with Observers.observing(QueryExecuted, executed.append):
        await Tournament.objects.get(name="two", multiple_objects_returned_exception=None)
        await Tournament.objects.get(
            name="two", does_not_exist_exception=None, multiple_objects_returned_exception=None
        )
        with pytest.raises(MultipleObjectsReturned):
            await Tournament.objects.get(name="two")
    clauses = Tournament._meta.connection.dialect.clauses

    def asks_for(event: QueryExecuted, row_count: int) -> bool:
        bound_integers = [value for value in event.parameters or () if type(value) is int]
        if bound_integers:
            return bound_integers == [row_count]
        return clauses.get_limit_offset_sql(str(row_count), None) in event.sql

    assert len(executed) == 3
    assert asks_for(executed[0], 1)
    assert asks_for(executed[1], 1)
    assert asks_for(executed[2], 2)


@pytest.mark.asyncio
@requires_features(supports_select_for_update=True)
async def test_get_with_a_row_lock_takes_the_exception_parameters(db):
    await create_rows()
    async with Transactions.atomic():
        locked = Tournament.objects.select_for_update()
        assert (await locked.get(name="two", multiple_objects_returned_exception=None)).name == "two"
        assert await locked.get(name="none", does_not_exist_exception=None) is None
        with pytest.raises(TooManyRows):
            await locked.get(name="two", multiple_objects_returned_exception=TooManyRows)


@pytest.mark.parametrize("value", ["missing", 5, object(), int, MissingRow.__name__])
@pytest.mark.parametrize("parameter_name", ["does_not_exist_exception", "multiple_objects_returned_exception"])
def test_get_refuses_an_exception_parameter_that_is_no_exception(value, parameter_name):
    with pytest.raises(TypeError, match=parameter_name):
        Tournament.objects.get(name="one", **{parameter_name: value})


def test_check_get_exception_argument_takes_every_allowed_value():
    for value in (GetException.STANDARD, None, MissingRow, MISSING_ROW_INSTANCE, KeyboardInterrupt):
        GetExceptions.check_exception_argument("does_not_exist_exception", value)


@pytest.mark.asyncio
async def test_get_is_typed_by_its_does_not_exist_exception(db):
    """The overloads: None for ``does_not_exist_exception`` types the result optional; anything else
    leaves it the row."""
    await create_rows()
    row = await Tournament.objects.get(name="one")
    assert_type(row, Tournament)
    optional_row = await Tournament.objects.get(name="none", does_not_exist_exception=None)
    assert_type(optional_row, Tournament | None)
    assert optional_row is None
    raising_row = await Tournament.objects.get(name="one", does_not_exist_exception=MissingRow)
    assert_type(raising_row, Tournament)
    counted_row = await Tournament.objects.get(name="two", multiple_objects_returned_exception=None)
    assert_type(counted_row, Tournament)
    optional_counted_row = await Tournament.objects.all().get(
        name="two", does_not_exist_exception=None, multiple_objects_returned_exception=None
    )
    assert_type(optional_counted_row, Tournament | None)
