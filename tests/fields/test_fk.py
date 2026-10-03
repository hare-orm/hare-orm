import warnings

import pytest

from hare import prefetch_related_objects
from hare.contrib.test import requires_features
from hare.exceptions import (
    ConfigurationError,
    NoValuesFetched,
    QueryError,
    ValidationError,
)
from hare.fields import SET_DEFAULT
from hare.fields.relations.fields import ForeignKeyField, ForeignKeyFieldInstance, OneToOneField
from hare.migrations.loading.loader import MigrationLoader
from hare.migrations.loading.recorder import NoopRecorder
from hare.query.queryset import QuerySet
from hare.warnings import RedundantDbDefaultWarning
from tests import testmodels


def assert_raises_wrong_type_exception(relation_name: str):
    """Context manager that asserts ValidationError with wrong type message."""
    return pytest.raises(ValidationError, match=f"Invalid type for relationship field '{relation_name}'")


@pytest.mark.asyncio
async def test_empty(db):
    """A missing required FK value (its underlying "*_id" column is a plain IntField) now fails
    fast with ValidationError, matching CharField's own established test_empty pattern - not
    IntegrityError from the DB's NOT NULL constraint."""
    with pytest.raises(ValidationError):
        await testmodels.MinRelation.objects.create()


@pytest.mark.asyncio
async def test_minimal__create_by_id(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament_id=tour.id)
    assert rel.tournament_id == tour.id
    assert (await tour.minrelations.all())[0] == rel


@pytest.mark.asyncio
async def test_minimal__create_by_name(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([rel], "tournament")
    assert rel.tournament == tour
    assert (await tour.minrelations.all())[0] == rel


@pytest.mark.asyncio
async def test_minimal__by_name__created_prefetched(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    assert rel.tournament == tour
    assert (await tour.minrelations.all())[0] == rel


@pytest.mark.asyncio
async def test_minimal__by_name__unfetched(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    rel = await testmodels.MinRelation.objects.get(id=rel.id)
    assert isinstance(rel.tournament, QuerySet)


@pytest.mark.asyncio
async def test_minimal__by_name__re_awaited(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([rel], "tournament")
    assert rel.tournament == tour
    assert await rel.tournament == tour


@pytest.mark.asyncio
async def test_minimal__by_name__awaited(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    rel = await testmodels.MinRelation.objects.get(id=rel.id)
    assert await rel.tournament == tour
    assert (await tour.minrelations.all())[0] == rel


@pytest.mark.asyncio
async def test_event__create_by_id(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.Event.objects.create(name="Event1", tournament_id=tour.id)
    assert rel.tournament_id == tour.id
    assert (await tour.events.all())[0] == rel


@pytest.mark.asyncio
async def test_event__create_by_name(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.Event.objects.create(name="Event1", tournament=tour)
    await prefetch_related_objects([rel], "tournament")
    assert rel.tournament == tour
    assert (await tour.events.all())[0] == rel


@pytest.mark.asyncio
async def test_update_by_name(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    tour2 = await testmodels.Tournament.objects.create(name="Team2")
    rel0 = await testmodels.Event.objects.create(name="Event1", tournament=tour)

    await testmodels.Event.objects.filter(pk=rel0.pk).update(tournament=tour2)
    rel = await testmodels.Event.objects.get(event_id=rel0.event_id)

    await prefetch_related_objects([rel], "tournament")
    assert rel.tournament == tour2
    assert await tour.events.all() == []
    assert (await tour2.events.all())[0] == rel


@pytest.mark.asyncio
async def test_update_by_id(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    tour2 = await testmodels.Tournament.objects.create(name="Team2")
    rel0 = await testmodels.Event.objects.create(name="Event1", tournament_id=tour.id)

    await testmodels.Event.objects.filter(event_id=rel0.event_id).update(tournament_id=tour2.id)
    rel = await testmodels.Event.objects.get(pk=rel0.pk)

    assert rel.tournament_id == tour2.id
    assert await tour.events.all() == []
    assert (await tour2.events.all())[0] == rel


@pytest.mark.asyncio
async def test_minimal__uninstantiated_create(db):
    tour = testmodels.Tournament(name="Team1")
    with pytest.raises(QueryError, match="You should first call .save()"):
        await testmodels.MinRelation.objects.create(tournament=tour)


@pytest.mark.asyncio
async def test_fk_to_bulk_created_object_without_pk_raises(db):
    """bulk_create() without returning=True marks its objects saved but leaves a generated pk
    unset - referencing such an object used to store NULL silently, with no error at all."""
    bulk_reporters = [testmodels.Reporter(name="bulk")]
    await testmodels.Reporter.objects.bulk_create(bulk_reporters)
    assert bulk_reporters[0]._saved_in_db is True
    assert bulk_reporters[0].pk is None
    tournament = await testmodels.Tournament.objects.create(name="T")

    with pytest.raises(QueryError, match="its 'id' is None"):
        await testmodels.Event.objects.create(name="E", tournament=tournament, reporter=bulk_reporters[0])

    event = await testmodels.Event.objects.create(name="E", tournament=tournament)
    with pytest.raises(QueryError, match="its 'id' is None"):
        event.reporter = bulk_reporters[0]
    assert event.reporter_id is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_fk_to_bulk_created_object_with_returning_is_allowed(db):
    tournament = await testmodels.Tournament.objects.create(name="T")
    returned_reporters = [testmodels.Reporter(name="returned")]
    await testmodels.Reporter.objects.bulk_create(returned_reporters, returning=True)

    event = await testmodels.Event.objects.create(name="E", tournament=tournament, reporter=returned_reporters[0])

    assert event.reporter_id == returned_reporters[0].pk
    assert (await testmodels.Event.objects.get(pk=event.pk)).reporter_id == returned_reporters[0].pk


@pytest.mark.asyncio
async def test_fk_to_none_and_direct_id_assignment_are_not_affected(db):
    tournament = await testmodels.Tournament.objects.create(name="T")
    reporter = await testmodels.Reporter.objects.create(name="R")
    event = await testmodels.Event.objects.create(name="E", tournament=tournament, reporter=None)
    assert event.reporter_id is None

    event.reporter_id = reporter.pk
    await event.save()
    assert (await testmodels.Event.objects.get(pk=event.pk)).reporter_id == reporter.pk

    event.reporter = None
    assert event.reporter_id is None


@pytest.mark.asyncio
async def test_o2o_to_bulk_created_object_without_pk_raises(db):
    tournament = await testmodels.Tournament.objects.create(name="T")
    bulk_events = [testmodels.Event(name="E", tournament=tournament)]
    await testmodels.Event.objects.bulk_create(bulk_events)
    assert bulk_events[0]._saved_in_db is True
    assert bulk_events[0].pk is None

    with pytest.raises(QueryError, match="its 'event_id' is None"):
        await testmodels.Address.objects.create(city="c", street="s", event=bulk_events[0])


@pytest.mark.asyncio
async def test_minimal__uninstantiated_iterate(db):
    tour = testmodels.Tournament(name="Team1")
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        async for _ in tour.minrelations:
            pass


@pytest.mark.asyncio
async def test_minimal__uninstantiated_await(db):
    tour = testmodels.Tournament(name="Team1")
    with pytest.raises(QueryError, match="This objects hasn't been instanced, call .save()"):
        await tour.minrelations


@pytest.mark.asyncio
async def test_minimal__unfetched_contains(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        "a" in tour.minrelations  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_minimal__unfetched_iter(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        for _ in tour.minrelations:
            pass


@pytest.mark.asyncio
async def test_minimal__unfetched_len(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        len(tour.minrelations)


@pytest.mark.asyncio
async def test_minimal__unfetched_bool(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        bool(tour.minrelations)


@pytest.mark.asyncio
async def test_minimal__unfetched_getitem(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    with pytest.raises(
        NoValuesFetched,
        match="No values were fetched for this relation - await it or prefetch it",
    ):
        tour.minrelations[0]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_minimal__instantiated_create(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    relation = await testmodels.MinRelation.objects.create(tournament=tour)
    assert (await testmodels.MinRelation.objects.get(id=relation.id)).tournament_id == tour.id


@pytest.mark.asyncio
async def test_minimal__instantiated_create_wrong_type(db):
    author = await testmodels.Author.objects.create(name="Author1")
    with assert_raises_wrong_type_exception("tournament"):
        await testmodels.MinRelation.objects.create(tournament=author)


@pytest.mark.asyncio
async def test_minimal__instantiated_iterate(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    relation = await testmodels.MinRelation.objects.create(tournament=tour)
    assert [item async for item in tour.minrelations] == [relation]


@pytest.mark.asyncio
async def test_minimal__instantiated_await(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    relation = await testmodels.MinRelation.objects.create(tournament=tour)
    assert list(await tour.minrelations) == [relation]


@pytest.mark.asyncio
async def test_minimal__fetched_contains(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([tour], "minrelations")
    assert rel in tour.minrelations


@pytest.mark.asyncio
async def test_minimal__fetched_iter(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([tour], "minrelations")
    assert list(tour.minrelations) == [rel]


@pytest.mark.asyncio
async def test_minimal__fetched_len(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([tour], "minrelations")
    assert len(tour.minrelations) == 1


@pytest.mark.asyncio
async def test_minimal__fetched_bool(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    await prefetch_related_objects([tour], "minrelations")
    assert not bool(tour.minrelations)
    await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([tour], "minrelations")
    assert bool(tour.minrelations)


@pytest.mark.asyncio
async def test_minimal__fetched_getitem(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    await prefetch_related_objects([tour], "minrelations")
    assert tour.minrelations[0] == rel

    with pytest.raises(IndexError):
        tour.minrelations[1]  # pylint: disable=W0104


@pytest.mark.asyncio
async def test_event__filter(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    event1 = await testmodels.Event.objects.create(name="Event1", tournament=tour)
    event2 = await testmodels.Event.objects.create(name="Event2", tournament=tour)
    assert await tour.events.filter(name="Event1") == [event1]
    assert await tour.events.filter(name="Event2") == [event2]
    assert await tour.events.filter(name="Event3") == []


@pytest.mark.asyncio
async def test_event__all(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    event1 = await testmodels.Event.objects.create(name="Event1", tournament=tour)
    event2 = await testmodels.Event.objects.create(name="Event2", tournament=tour)
    assert set(await tour.events.all()) == {event1, event2}


@pytest.mark.asyncio
async def test_event__order_by(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    event1 = await testmodels.Event.objects.create(name="Event1", tournament=tour)
    event2 = await testmodels.Event.objects.create(name="Event2", tournament=tour)
    assert await tour.events.order_by("-name") == [event2, event1]
    assert await tour.events.order_by("name") == [event1, event2]


@pytest.mark.asyncio
async def test_event__limit(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    event1 = await testmodels.Event.objects.create(name="Event1", tournament=tour)
    event2 = await testmodels.Event.objects.create(name="Event2", tournament=tour)
    await testmodels.Event.objects.create(name="Event3", tournament=tour)
    assert await tour.events.order_by("name").limit(2) == [event1, event2]


@pytest.mark.asyncio
async def test_event__offset(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    await testmodels.Event.objects.create(name="Event1", tournament=tour)
    event2 = await testmodels.Event.objects.create(name="Event2", tournament=tour)
    event3 = await testmodels.Event.objects.create(name="Event3", tournament=tour)
    assert await tour.events.order_by("name").offset(1) == [event2, event3]


@pytest.mark.asyncio
async def test_fk_correct_type_assignment(db):
    tour1 = await testmodels.Tournament.objects.create(name="Team1")
    tour2 = await testmodels.Tournament.objects.create(name="Team2")
    event = await testmodels.Event(name="Event1", tournament=tour1)

    event.tournament = tour2
    await event.save()
    assert event.tournament_id == tour2.id


@pytest.mark.asyncio
async def test_fk_wrong_type_assignment(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    author = await testmodels.Author.objects.create(name="Author")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)

    with assert_raises_wrong_type_exception("tournament"):
        rel.tournament = author


@pytest.mark.asyncio
async def test_fk_none_assignment(db):
    manager = await testmodels.Employee.objects.create(name="Manager")
    employee = await testmodels.Employee.objects.create(name="Employee", manager=manager)

    employee.manager = None
    await employee.save()
    assert employee.manager is None


@pytest.mark.asyncio
async def test_fk_update_wrong_type(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    rel = await testmodels.MinRelation.objects.create(tournament=tour)
    author = await testmodels.Author.objects.create(name="Author1")

    with assert_raises_wrong_type_exception("tournament"):
        await testmodels.MinRelation.objects.filter(id=rel.id).update(tournament=author)


@pytest.mark.asyncio
async def test_fk_bulk_create_wrong_type(db):
    author = await testmodels.Author.objects.create(name="Author")
    with assert_raises_wrong_type_exception("tournament"):
        await testmodels.MinRelation.objects.bulk_create(
            [testmodels.MinRelation(tournament=author) for _ in range(10)]
        )


@pytest.mark.asyncio
async def test_fk_bulk_update_wrong_type(db):
    tour = await testmodels.Tournament.objects.create(name="Team1")
    await testmodels.MinRelation.objects.bulk_create([testmodels.MinRelation(tournament=tour) for _ in range(1, 10)])
    author = await testmodels.Author.objects.create(name="Author")

    with assert_raises_wrong_type_exception("tournament"):
        relations = await testmodels.MinRelation.objects.all()
        await testmodels.MinRelation.objects.bulk_update(
            [testmodels.MinRelation(id=rel.id, tournament=author) for rel in relations],
            fields=["tournament"],
        )


@pytest.mark.parametrize("field_factory", [ForeignKeyField, OneToOneField])
def test_set_default_with_real_constraint_rejects_python_only_default(field_factory):
    """With a real FK constraint the database itself runs ON DELETE SET DEFAULT and resets the
    column to its DDL DEFAULT - a Python-side default= is never emitted there, so it used to be
    silently ignored (NULL, or an IntegrityError on a NOT NULL column) on a real delete while
    hare's own Python-side cascade applied it."""
    with pytest.raises(ConfigurationError, match="db_default set when db_constraint=True") as exception_info:
        field_factory("models.Tournament", on_delete=SET_DEFAULT, default=1)
    assert "never emitted into the DDL" in str(exception_info.value)
    with pytest.raises(ConfigurationError, match="db_default set when db_constraint=True"):
        field_factory("models.Tournament", on_delete=SET_DEFAULT, default=1, null=True)


@pytest.mark.parametrize("field_factory", [ForeignKeyField, OneToOneField])
def test_set_default_with_real_constraint_rejects_no_default_at_all(field_factory):
    with pytest.raises(ConfigurationError, match="db_default set when db_constraint=True"):
        field_factory("models.Tournament", on_delete=SET_DEFAULT)


@pytest.mark.parametrize("field_factory", [ForeignKeyField, OneToOneField])
def test_set_default_with_real_constraint_accepts_db_default(field_factory):
    field = field_factory("models.Tournament", on_delete=SET_DEFAULT, db_default=1)
    assert field.on_delete == SET_DEFAULT
    assert field.has_db_default()


@pytest.mark.parametrize("field_factory", [ForeignKeyField, OneToOneField])
def test_set_default_without_real_constraint_accepts_python_only_default(field_factory):
    """No real FK constraint means only hare's own Python-side cascade resets the column, which
    reads default= - so default= alone is enough."""
    field = field_factory("models.Tournament", on_delete=SET_DEFAULT, default=1, db_constraint=False)
    assert field.on_delete == SET_DEFAULT
    assert not field.has_db_default()


@pytest.mark.parametrize("field_factory", [ForeignKeyField, OneToOneField])
def test_set_default_without_real_constraint_still_needs_some_default(field_factory):
    with pytest.raises(ConfigurationError, match="field must have default or db_default set"):
        field_factory("models.Tournament", on_delete=SET_DEFAULT, db_constraint=False)


def test_set_default_with_real_constraint_accepts_default_alongside_db_default_without_warning():
    """db_default next to default= is not redundant here - the database's own ON DELETE SET
    DEFAULT reads the column's DDL DEFAULT, while default= is what Python-side inserts use."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", RedundantDbDefaultWarning)
        field = ForeignKeyField("models.Tournament", on_delete=SET_DEFAULT, default=1, db_default=1)
    assert field.default == 1
    assert field.has_db_default()


def test_default_alongside_db_default_still_warns_for_other_on_delete_modes():
    """The warning is only suppressed where the database actually reads db_default - a plain FK
    (or SET_DEFAULT without a real constraint) keeps warning like any other field."""
    with pytest.warns(RedundantDbDefaultWarning):
        ForeignKeyField("models.Tournament", default=1, db_default=1)
    with pytest.warns(RedundantDbDefaultWarning):
        ForeignKeyField("models.Tournament", on_delete=SET_DEFAULT, default=1, db_default=1, db_constraint=False)


def test_set_default_requirement_is_skipped_while_replaying_a_migration():
    """A migration file describes a schema that was already applied - the same field must still
    construct there, but is rejected again once the replay scope ends."""
    with ForeignKeyFieldInstance.replaying_migration_scope():
        field = ForeignKeyField("models.Tournament", on_delete=SET_DEFAULT, default=1)
    assert field.on_delete == SET_DEFAULT
    with pytest.raises(ConfigurationError, match="db_default set when db_constraint=True"):
        ForeignKeyField("models.Tournament", on_delete=SET_DEFAULT, default=1)


def test_migration_loader_loads_a_historical_python_only_set_default_field(tmp_path, monkeypatch):
    """A migration written back when default= alone was accepted must keep loading - otherwise
    the whole migration graph (including the AlterField that fixes the field) becomes unusable."""
    package_dir = tmp_path / "legacy_set_default_app"
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "0001_initial.py").write_text(
        "\n".join(
            [
                "from hare import fields, migrations",
                "",
                "class Migration(migrations.Migration):",
                "    dependencies = []",
                "    operations = [",
                "        migrations.CreateModel(",
                "            name='Child',",
                "            fields=[",
                "                ('id', fields.IntField(primary_key=True)),",
                "                ('parent', fields.ForeignKeyField('legacy_set_default_app.Parent',",
                "                    on_delete=fields.SET_DEFAULT, default=1)),",
                "            ],",
                "        ),",
                "    ]",
                "",
            ]
        ),
        encoding="ascii",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    loader = MigrationLoader(
        apps_config={"legacy_set_default_app": {"migrations": "legacy_set_default_app.migrations"}},
        recorder=NoopRecorder(),
    )
    loader.load_disk()

    assert len(loader.disk_migrations) == 1
    with pytest.raises(ConfigurationError, match="db_default set when db_constraint=True"):
        ForeignKeyField("legacy_set_default_app.Parent", on_delete=SET_DEFAULT, default=1)


@pytest.mark.asyncio
async def test_set_default_with_db_default_resets_nullable_fk_on_model_delete(db):
    default_parent = await testmodels.SetDefaultDbDefaultParent.objects.create(id=999, name="Default")
    parent = await testmodels.SetDefaultDbDefaultParent.objects.create(name="P")
    child = await testmodels.SetDefaultDbDefaultChildNullable.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await testmodels.SetDefaultDbDefaultChildNullable.objects.get(pk=child.pk)
    assert refreshed.parent_id == default_parent.pk == 999


@pytest.mark.asyncio
async def test_set_default_with_db_default_resets_required_fk_on_model_delete(db):
    """A NOT NULL column used to fail the whole delete with IntegrityError when only default= was
    given (the database reset it to NULL) - db_default lets it fall back to a real value."""
    await testmodels.SetDefaultDbDefaultParent.objects.create(id=999, name="Default")
    parent = await testmodels.SetDefaultDbDefaultParent.objects.create(name="P")
    child = await testmodels.SetDefaultDbDefaultChildRequired.objects.create(name="C", parent=parent)

    await parent.delete()

    assert not await testmodels.SetDefaultDbDefaultParent.objects.filter(pk=parent.pk).exists()
    refreshed = await testmodels.SetDefaultDbDefaultChildRequired.objects.get(pk=child.pk)
    assert refreshed.parent_id == 999


@pytest.mark.asyncio
async def test_set_default_with_db_default_resets_fk_on_queryset_delete(db):
    await testmodels.SetDefaultDbDefaultParent.objects.create(id=999, name="Default")
    parent_a = await testmodels.SetDefaultDbDefaultParent.objects.create(name="A")
    parent_b = await testmodels.SetDefaultDbDefaultParent.objects.create(name="B")
    child_a = await testmodels.SetDefaultDbDefaultChildNullable.objects.create(name="CA", parent=parent_a)
    child_b = await testmodels.SetDefaultDbDefaultChildRequired.objects.create(name="CB", parent=parent_b)

    await testmodels.SetDefaultDbDefaultParent.objects.filter(pk__in=[parent_a.pk, parent_b.pk]).delete()

    assert (await testmodels.SetDefaultDbDefaultChildNullable.objects.get(pk=child_a.pk)).parent_id == 999
    assert (await testmodels.SetDefaultDbDefaultChildRequired.objects.get(pk=child_b.pk)).parent_id == 999


@pytest.mark.asyncio
async def test_set_default_with_db_default_resets_one_to_one_on_model_delete(db):
    await testmodels.SetDefaultDbDefaultParent.objects.create(id=999, name="Default")
    parent = await testmodels.SetDefaultDbDefaultParent.objects.create(name="P")
    child = await testmodels.SetDefaultDbDefaultChildOneToOne.objects.create(name="C", parent=parent)

    await parent.delete()

    refreshed = await testmodels.SetDefaultDbDefaultChildOneToOne.objects.get(pk=child.pk)
    assert refreshed.parent_id == 999
