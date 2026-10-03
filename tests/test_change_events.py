"""ChangeEvents: the rows every write through the ORM changed, delivered once its transaction
commits - never for a write that was rolled back."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import IntegrityError, QueryError
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.instrumentation.observers import Observers
from hare.instrumentation.rows_changed import RowsChanged
from hare.transactions.transactions import Transactions
from tests.testmodels import (
    CompositePkThing,
    Event,
    SoftDeleteChildCascadeHard,
    SoftDeleteChildCascadeSoft,
    SoftDeleteChildSetNull,
    SoftDeleteParent,
    Team,
    Tournament,
)

CONNECTION = "models"
#: The test database URL of an in-memory database.
IN_MEMORY_DATABASE_URL_PART = ":memory:"


@pytest.fixture
def changes() -> Iterator[list[RowsChanged]]:
    events: list[RowsChanged] = []
    listener = Observers.observe(RowsChanged, events.append)
    yield events
    Observers.unobserve(RowsChanged, listener)


def described(events: list[RowsChanged]) -> list[tuple[str, str, Any, Any]]:
    return [
        (event.model.__name__, event.operation.value, event.pks, None if event.fields is None else set(event.fields))
        for event in events
    ]


@pytest.mark.asyncio
async def test_saving_a_row(db_isolated, changes):
    tournament = await Tournament.objects.create(id=1, name="Spring")
    tournament.name = "Summer"
    await tournament.save()
    await tournament.save(update_fields=["desc"])
    assert described(changes) == [
        ("Tournament", "insert", (1,), None),
        ("Tournament", "update", (1,), None),
        ("Tournament", "update", (1,), {"desc"}),
    ]
    assert {event.connection_name for event in changes} == {CONNECTION}


@pytest.mark.asyncio
async def test_a_composite_key(db_isolated, changes):
    await CompositePkThing.objects.create(thing_id=1, revision=2, name="first")
    assert described(changes) == [("CompositePkThing", "insert", ((1, 2),), None)]


@pytest.mark.asyncio
async def test_deleting_a_row_reports_what_its_on_delete_reaches(db_isolated, changes):
    tournament = await Tournament.objects.create(id=1, name="Spring")
    event = await Event.objects.create(event_id=1, name="Final", tournament=tournament)
    team = await Team.objects.create(id=1, name="Hares")
    await event.participants.add(team)
    changes.clear()

    await tournament.delete()
    summary = described(changes)
    assert summary[0] == ("Tournament", "delete", (1,), None)
    assert ("Event", "delete", None, None) in summary
    assert ("Team", "update", None, {"events"}) in summary


@pytest.mark.asyncio
async def test_queryset_writes(db_isolated, changes):
    await Tournament.objects.bulk_create([Tournament(id=1, name="a"), Tournament(id=2, name="b")])
    tournaments = list(await Tournament.objects.all().order_by("id"))
    for tournament in tournaments:
        tournament.name = tournament.name.upper()
    await Tournament.objects.bulk_update(tournaments, fields=["name"])
    await Tournament.objects.filter(id=1).update(desc="first")
    await Tournament.objects.filter(id=99).update(desc="nothing")
    await Tournament.objects.filter(id=2).delete()
    await Tournament.objects.filter(id=99).delete()
    assert [item for item in described(changes) if item[0] == "Tournament"] == [
        ("Tournament", "insert", (1, 2), None),
        ("Tournament", "update", (1, 2), {"name"}),
        ("Tournament", "update", None, {"desc"}),
        ("Tournament", "delete", None, None),
    ]


@pytest.mark.asyncio
async def test_many_to_many_links(db_isolated, changes):
    tournament = await Tournament.objects.create(id=1, name="Spring")
    event = await Event.objects.create(event_id=1, name="Final", tournament=tournament)
    hares = await Team.objects.create(id=1, name="Hares")
    foxes = await Team.objects.create(id=2, name="Foxes")
    changes.clear()

    await event.participants.add(hares, foxes)
    await event.participants.remove(foxes)
    await event.participants.clear()
    assert described(changes) == [
        ("Event", "update", (1,), {"participants"}),
        ("Team", "update", (1, 2), {"events"}),
        ("Event", "update", (1,), {"participants"}),
        ("Team", "update", (2,), {"events"}),
        ("Event", "update", (1,), {"participants"}),
        ("Team", "update", None, {"events"}),
    ]


@pytest.mark.asyncio
async def test_soft_delete_and_restore(db_isolated, changes):
    parent = await SoftDeleteParent.objects.create(name="parent")
    await SoftDeleteChildCascadeSoft.objects.create(name="child", parent=parent)
    changes.clear()

    await parent.delete()
    await parent.restore()
    summary = described(changes)
    parent_events = [item for item in summary if item[0] == "SoftDeleteParent"]
    assert parent_events == [("SoftDeleteParent", "update", (parent.pk,), {"deleted_at"})] * 2
    assert any(item[0] == "SoftDeleteChildCascadeSoft" and item[1] == "update" for item in summary)


@pytest.mark.parametrize("through_queryset", [False, True])
@pytest.mark.asyncio
async def test_a_soft_delete_reports_each_model_once(db_isolated, changes, through_queryset):
    """Bug: the writes a soft delete makes on the way - the soft-deleting UPDATE, the rows its
    cascade reaches - reported events of their own besides the delete's, so a change came twice;
    and a child without soft delete, which the soft delete really deletes, was reported as
    updated, while the rows a SET_NULL reached weren't reported by the delete at all."""
    parent = await SoftDeleteParent.objects.create(name="parent")
    await SoftDeleteChildCascadeSoft.objects.create(name="soft child", parent=parent)
    await SoftDeleteChildCascadeHard.objects.create(name="hard child", parent=parent)
    await SoftDeleteChildSetNull.objects.create(name="set-null child", parent=parent)
    changes.clear()

    if through_queryset:
        await SoftDeleteParent.objects.filter(pk=parent.pk).delete()
    else:
        await parent.delete()
    summary = described(changes)
    assert len(summary) == len({item[0] for item in summary})
    parent_pks = None if through_queryset else (parent.pk,)
    assert ("SoftDeleteParent", "update", parent_pks, {"deleted_at"}) in summary
    assert ("SoftDeleteChildCascadeSoft", "update", None, {"deleted_at"}) in summary
    assert ("SoftDeleteChildCascadeHard", "delete", None, None) in summary
    assert ("SoftDeleteChildSetNull", "update", None, {"parent"}) in summary


@requires_features(supports_unique_constraints=True)
@pytest.mark.parametrize("batch_size", [None, 1])
@pytest.mark.asyncio
async def test_a_failed_bulk_create_reports_nothing(db_isolated, changes, batch_size):
    """Bug: a bulk_create() that failed reported every object as inserted - a failed insert rolls
    all its rows back, its earlier batches included."""
    await Tournament.objects.create(id=1, name="first")
    changes.clear()

    with pytest.raises(IntegrityError):
        await Tournament.objects.bulk_create(
            [Tournament(id=2, name="second"), Tournament(id=1, name="duplicate")], batch_size=batch_size
        )
    assert changes == []
    assert not await Tournament.objects.filter(id=2).exists()


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_events_wait_for_the_commit_and_go_with_a_rollback(db_isolated, changes):
    async with Transactions.atomic(CONNECTION):
        await Tournament.objects.create(id=1, name="kept")
        assert changes == []
    assert described(changes) == [("Tournament", "insert", (1,), None)]

    changes.clear()
    with pytest.raises(RuntimeError):
        async with Transactions.atomic(CONNECTION):
            await Tournament.objects.create(id=2, name="rolled back")
            raise RuntimeError("abort")
    assert changes == []

    async with Transactions.atomic(CONNECTION):
        await Tournament.objects.create(id=3, name="outer")
        with pytest.raises(RuntimeError):
            async with Transactions.atomic(CONNECTION):
                await Tournament.objects.create(id=4, name="savepoint")
                raise RuntimeError("abort the savepoint")
    assert described(changes) == [("Tournament", "insert", (3,), None)]


@pytest.mark.asyncio
async def test_an_autonomous_write_is_delivered_at_its_own_commit(db_isolated, changes):
    if IN_MEMORY_DATABASE_URL_PART in os.environ.get("HARE_TEST_DB", IN_MEMORY_DATABASE_URL_PART):
        pytest.skip("an independent connection to an in-memory database opens another, empty database")
    with pytest.raises(RuntimeError):
        async with Transactions.atomic(CONNECTION):
            async with Transactions.autonomous(CONNECTION) as independent:
                await Tournament.objects.using(independent).create(id=1, name="audit")
            assert described(changes) == [("Tournament", "insert", (1,), None)]
            raise RuntimeError("the outer transaction rolls back")
    assert described(changes) == [("Tournament", "insert", (1,), None)]


@pytest.mark.asyncio
async def test_a_listener_that_raises_breaks_nothing_and_an_async_one_is_awaited(db_isolated):
    seen: list[RowOperation] = []

    def broken(event: RowsChanged) -> None:
        raise ValueError("listener failed")

    async def collect(event: RowsChanged) -> None:
        seen.append(event.operation)

    Observers.observe(RowsChanged, broken)
    Observers.observe(RowsChanged, collect)
    try:
        tournament = await Tournament.objects.create(id=1, name="Spring")
        await tournament.delete()
    finally:
        Observers.unobserve(RowsChanged, broken)
        Observers.unobserve(RowsChanged, collect)
    assert seen[:2] == [RowOperation.INSERT, RowOperation.DELETE]
    assert not await Tournament.objects.filter(id=1).exists()


@pytest.mark.asyncio
async def test_a_listener_may_write_and_its_writes_report_too(db_isolated, changes):
    async def mirror(event: RowsChanged) -> None:
        if event.model is Tournament and event.operation is RowOperation.INSERT:
            await Team.objects.create(id=event.pks[0], name="mirror")

    Observers.observe(RowsChanged, mirror)
    try:
        await Tournament.objects.create(id=7, name="Spring")
    finally:
        Observers.unobserve(RowsChanged, mirror)
    assert ("Team", "insert", (7,), None) in described(changes)


@pytest.fixture
def listener_of_models() -> Iterator[list[RowsChanged]]:
    events: list[RowsChanged] = []
    yield events
    Observers.unobserve(RowsChanged, events.append)


@pytest.mark.asyncio
async def test_a_listener_of_some_models_hears_only_them(db_isolated, listener_of_models):
    listener = Observers.observe(RowsChanged, listener_of_models.append, models=[Event])
    assert ChangeEvents.is_observed(Event) and not ChangeEvents.is_observed(Tournament)
    tournament = await Tournament.objects.create(id=1, name="Spring")
    await Event.objects.create(event_id=1, name="Final", tournament=tournament)
    assert described(listener_of_models) == [("Event", "insert", (1,), None)]

    # The rows its model loses to a delete of another model reach it too.
    listener_of_models.clear()
    await tournament.delete()
    assert described(listener_of_models) == [("Event", "delete", None, None)]

    Observers.observe(RowsChanged, listener, models=[Tournament])
    listener_of_models.clear()
    await Tournament.objects.create(id=2, name="Autumn")
    assert described(listener_of_models) == [("Tournament", "insert", (2,), None)]


@pytest.mark.asyncio
async def test_a_listener_of_an_abstract_base_hears_every_model_built_on_it(db_isolated, listener_of_models):
    from tests.testmodels import MyAbstractBaseModel, MyDerivedModel, MyOtherDerivedModel

    Observers.observe(RowsChanged, listener_of_models.append, models=[MyAbstractBaseModel])
    await MyDerivedModel.objects.create(id=1, name="first")
    await MyOtherDerivedModel.objects.create(id=1, name="second")
    await Tournament.objects.create(id=1, name="Spring")
    assert [event.model for event in listener_of_models] == [MyDerivedModel, MyOtherDerivedModel]


def get_rows_changed_observers():
    """The process's observers of RowsChanged, by callback."""
    return Observers.process_observers.observers_by_event_type.get(RowsChanged, {})


def get_observed_models(callback):
    """The models a RowsChanged observer gets the changes of, None for every model."""
    return get_rows_changed_observers()[callback].models


def test_registering_again_and_unregistering_models(listener_of_models):
    listener = Observers.observe(RowsChanged, listener_of_models.append, models=[Tournament])
    Observers.observe(RowsChanged, listener, models=[Event])
    assert get_observed_models(listener) == frozenset({Tournament, Event})
    Observers.unobserve(RowsChanged, listener, models=[Tournament])
    assert get_observed_models(listener) == frozenset({Event})
    Observers.unobserve(RowsChanged, listener, models=[Event])
    assert listener not in get_rows_changed_observers()

    # A listener of every model stays one; registering it without models makes it one.
    Observers.observe(RowsChanged, listener)
    Observers.observe(RowsChanged, listener, models=[Tournament])
    assert get_observed_models(listener) is None
    with pytest.raises(QueryError, match="observes every event"):
        Observers.unobserve(RowsChanged, listener, models=[Tournament])
    Observers.unobserve(RowsChanged, listener)
    Observers.observe(RowsChanged, listener, models=[Tournament])
    Observers.observe(RowsChanged, listener)
    assert get_observed_models(listener) is None


def test_only_model_classes_are_listened_to(listener_of_models):
    with pytest.raises(TypeError, match="model classes"):
        Observers.observe(RowsChanged, listener_of_models.append, models=["models.Tournament"])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="model classes"):
        Observers.observe(RowsChanged, listener_of_models.append, models=[Tournament(id=1, name="Spring")])  # type: ignore[arg-type]
    assert listener_of_models.append not in get_rows_changed_observers()
