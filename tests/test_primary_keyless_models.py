"""A model declaring ``Meta.primary_key = None`` - a table with no primary key. Reading, filtering,
aggregating, bulk_create() and QuerySet.update()/delete() work; what identifies one row by its key
fails with a ConfigurationError naming the model."""

import os
import sys
import types
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, FieldError
from hare.migrations.operations import AddField, CreateModel
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from hare.query.expressions import Q
from hare.query.functions import Count, Sum
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip
from tests.primary_keyless_models import Venue, VisitLog, VisitNote


@pytest_asyncio.fixture
async def visits() -> AsyncGenerator[Any]:
    db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    async with hare_test_context(["tests.primary_keyless_models"], db_url=db_url) as context:
        hall = await Venue.objects.create(id=1, name="Hall")
        yard = await Venue.objects.create(id=2, name="Yard")
        await VisitLog.objects.create(venue=hall, visitor="ann", duration=5)
        await VisitLog.objects.create(venue=hall, visitor="bob", duration=7)
        await VisitLog.objects.bulk_create(
            [VisitLog(venue=yard, visitor="cid", duration=3), VisitLog(venue=yard, visitor="dan", duration=1)]
        )
        yield context


def test_the_model_has_no_primary_key():
    meta = VisitLog._meta
    assert meta.primary_key_attribute == ()
    assert meta.pk is None
    assert meta.pk_fields == ()
    assert meta.primary_key_attribute_names == ()
    assert not meta.has_primary_key
    assert "id" not in meta.fields_map


@pytest.mark.asyncio
async def test_the_table_has_no_primary_key(visits):
    sql = visits.get_connection().get_schema_sql(safe=False)
    table_sql = next(statement for statement in sql.split(";") if "visitlog" in statement.lower())
    assert "PRIMARY KEY" not in table_sql.upper()


@pytest.mark.asyncio
async def test_rows_are_read_filtered_and_aggregated(visits):
    assert await VisitLog.objects.all().values_list("visitor", flat=True) == ["ann", "bob", "cid", "dan"]
    assert await VisitLog.objects.filter(venue__name="Hall").count() == 2
    assert await VisitLog.objects.filter(duration__gte=5).exists()
    assert await VisitLog.objects.exclude(venue__name="Hall").values_list("visitor", flat=True) == ["cid", "dan"]
    first = await VisitLog.objects.filter(visitor="bob").select_related("venue").first()
    assert first is not None
    assert (first.visitor, first.venue.name) == ("bob", "Hall")
    assert await VisitLog.objects.all().aggregate(total=Sum("duration")) == {"total": 16}
    per_venue = (
        await VisitLog.objects.all().group_by("venue_id").annotate(count=Count("visitor")).values("venue_id", "count")
    )
    assert sorted((row["venue_id"], row["count"]) for row in per_venue) == [(1, 2), (2, 2)]
    fetched = await VisitLog.objects.get(visitor="cid")
    assert fetched.duration == 3
    prefetched = await VisitLog.objects.all().prefetch_related("venue")
    assert [visit.venue.name for visit in prefetched] == ["Hall", "Hall", "Yard", "Yard"]
    assert [visit.visitor async for visit in VisitLog.objects.all().iterator(chunk_size=3)] == [
        "ann",
        "bob",
        "cid",
        "dan",
    ]


@pytest.mark.asyncio
async def test_rows_are_reached_through_the_related_model(visits):
    hall = await Venue.objects.get(id=1)
    assert sorted(await hall.visits.all().values_list("visitor", flat=True)) == ["ann", "bob"]
    assert await Venue.objects.filter(visits__visitor="cid").values_list("name", flat=True) == ["Yard"]
    assert await Venue.objects.annotate(visit_count=Count("visits__visitor")).order_by("id").values_list(
        "visit_count", flat=True
    ) == [2, 2]


@pytest.mark.asyncio
async def test_queryset_update_and_delete_by_condition(visits):
    assert await VisitLog.objects.filter(visitor="ann").update(duration=10) == 1
    assert await VisitLog.objects.get(visitor="ann").values_list("duration", flat=True) == 10
    assert await VisitLog.objects.filter(venue__name="Yard").delete() == 2
    assert await VisitLog.objects.all().values_list("visitor", flat=True) == ["ann", "bob"]


@pytest.mark.asyncio
async def test_writes_through_a_relation(visits):
    assert await VisitLog.objects.filter(venue__name="Yard").update(duration=0) == 2
    assert await VisitLog.objects.filter(duration=0).values_list("visitor", flat=True) == ["cid", "dan"]
    assert await VisitLog.objects.exclude(Q(visitor="ann") | Q(venue__name="Yard")).values_list(
        "visitor", flat=True
    ) == ["bob"]
    assert await VisitLog.objects.filter(venue__name="Hall").delete() == 2
    assert await VisitLog.objects.all().values_list("visitor", flat=True) == ["cid", "dan"]


@pytest.mark.asyncio
async def test_more_reads(visits):
    assert await VisitLog.objects.filter(venue__name="Hall").values("visitor", "venue__name") == [
        {"visitor": "ann", "venue__name": "Hall"},
        {"visitor": "bob", "venue__name": "Hall"},
    ]
    assert sorted(await VisitLog.objects.all().distinct().values_list("venue_id", flat=True)) == [1, 2]
    assert await Venue.objects.annotate(visit_count=Count("visits")).order_by("id").values_list(
        "visit_count", flat=True
    ) == [
        2,
        2,
    ]
    assert await Venue.objects.filter(visits__isnull=True).count() == 0
    venues = await Venue.objects.all().order_by("id").prefetch_related("visits")
    assert [sorted(visit.visitor for visit in venue.visits) for venue in venues] == [["ann", "bob"], ["cid", "dan"]]
    visit, created = await VisitLog.objects.get_or_create(visitor="eve", defaults={"venue_id": 1})
    assert created
    assert visit.pk is None
    assert {visit} == {visit}


@pytest.mark.asyncio
async def test_a_cascade_hare_runs_deletes_rows_without_a_primary_key(visits):
    hall = await Venue.objects.get(id=1)
    yard = await Venue.objects.get(id=2)
    await VisitNote.objects.bulk_create([VisitNote(venue=hall, text="loud"), VisitNote(venue=yard, text="quiet")])
    await hall.delete()
    assert await VisitNote.objects.all().values_list("text", flat=True) == ["quiet"]
    await Venue.objects.filter(id=2).delete()
    assert await VisitNote.objects.all().count() == 0


@pytest.mark.asyncio
async def test_deleting_the_related_row_cascades(visits):
    hall = await Venue.objects.get(id=1)
    await hall.delete()
    assert await VisitLog.objects.all().values_list("visitor", flat=True) == ["cid", "dan"]


@pytest.mark.asyncio
async def test_what_identifies_a_row_needs_a_primary_key(visits):
    visit = await VisitLog.objects.get(visitor="ann")
    visit.duration = 9
    with pytest.raises(ConfigurationError, match=r"VisitLog has no primary key \(Meta.primary_key = None\)"):
        await visit.save()
    with pytest.raises(ConfigurationError, match="VisitLog has no primary key"):
        await visit.delete()
    with pytest.raises(ConfigurationError, match="VisitLog has no primary key"):
        await visit.refresh_from_db()
    with pytest.raises(FieldError, match="VisitLog has no primary key"):
        VisitLog.objects.filter(pk=1)
    with pytest.raises(FieldError, match="VisitLog has no primary key"):
        VisitLog.objects.all().order_by("pk")


@pytest.mark.asyncio
async def test_a_new_row_is_saved_once(visits):
    hall = await Venue.objects.get(id=1)
    visit = VisitLog(venue=hall, visitor="eve")
    await visit.save()
    assert await VisitLog.objects.filter(visitor="eve").count() == 1


def test_a_primary_key_alongside_primary_key_none_is_rejected():
    with pytest.raises(ConfigurationError, match="declares both Meta.primary_key = None and the primary key field"):

        class Conflicting(Model):
            code = fields.CharField(max_length=5, primary_key=True)

            class Meta:
                primary_key = None

    with pytest.raises(ConfigurationError, match="Meta.primary_key only takes None"):

        class Named(Model):
            code = fields.CharField(max_length=5)

            class Meta:
                primary_key = "code"


@pytest.mark.asyncio
async def test_a_relation_to_a_model_without_primary_key_is_rejected():
    class Target(Model):
        name = fields.CharField(max_length=5)

        class Meta:
            primary_key = None

    class Pointer(Model):
        target = fields.ForeignKeyField("pointer_app.Target")

    module_name = "tests._primary_keyless_relation_models"
    module = types.ModuleType(module_name)
    module.Target = Target  # type: ignore[attr-defined]
    module.Pointer = Pointer  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    context = HareContext()
    await context.__aenter__()
    try:
        with pytest.raises(ConfigurationError, match="Target has no primary key"):
            await context.init(
                config={
                    "connections": {"default": "sqlite+aiosqlite://:memory:"},
                    "apps": {"pointer_app": {"models": [module_name], "default_connection": "default"}},
                }
            )
    finally:
        await context.__aexit__(None, None, None)
        sys.modules.pop(module_name, None)


def build_primary_keyless_model(fields_by_name: dict[str, Any]) -> type[Model]:
    attributes: dict[str, Any] = dict(fields_by_name)
    attributes["Meta"] = type("Meta", (), {"table": "pk_less_reading", "app": APP_LABEL, "primary_key": None})
    attributes["_no_comments"] = True
    return type("PkLessReading", (Model,), attributes)


@pytest.mark.asyncio
async def test_migrations_create_and_change_a_table_without_primary_key(db_isolated):
    round_trip = RoundTrip(db_isolated.get_connection())
    first = build_primary_keyless_model({"sensor": fields.CharField(max_length=10), "value": fields.IntField()})
    (create_model,) = await round_trip.migrate_to(first)
    assert isinstance(create_model, CreateModel)
    assert create_model.options["primary_key"] is None
    rendered = MigrationWriter.render_value(create_model.options, ImportManager())
    assert "ModelOption.PRIMARY_KEY: None" in rendered
    await round_trip.connection.execute_script("INSERT INTO pk_less_reading (sensor, value) VALUES ('a', 1)")
    assert round_trip.get_pending_operations(first) == []

    second = build_primary_keyless_model(
        {
            "sensor": fields.CharField(max_length=10),
            "value": fields.IntField(),
            "unit": fields.CharField(max_length=5, default="C"),
        }
    )
    operations = await round_trip.migrate_to(second)
    assert [type(operation) for operation in operations] == [AddField]
    assert await round_trip.connection.execute_dicts("SELECT sensor, value, unit FROM pk_less_reading") == [
        {"sensor": "a", "value": 1, "unit": "C"}
    ]
    assert round_trip.get_pending_operations(second) == []
    assert not create_model.model._meta.has_primary_key


@pytest.mark.asyncio
async def test_migrations_refuse_to_add_or_remove_a_primary_key(db_isolated):
    """Like any change of a model's primary key, adding one to a table without it or taking it
    away needs a hand-written migration."""
    round_trip = RoundTrip(db_isolated.get_connection())
    without_key = build_primary_keyless_model({"sensor": fields.CharField(max_length=10)})
    attributes: dict[str, Any] = {
        "sensor": fields.CharField(max_length=10, primary_key=True),
        "Meta": type("Meta", (), {"table": "pk_less_reading", "app": APP_LABEL}),
        "_no_comments": True,
    }
    with_key = type("PkLessReading", (Model,), attributes)
    await round_trip.migrate_to(without_key)
    with pytest.raises(ConfigurationError, match=r"PkLessReading \(no primary key -> 'sensor'\)"):
        round_trip.get_pending_operations(with_key)
