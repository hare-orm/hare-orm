"""Migrations an application builds while it runs and keeps in its own database: the state of the
models, the operations between two versions, the SQL before running, applying and unapplying them
with a journal of its own, the text of a migration and back - for a table whose model is
registered while the application runs, related both ways to a model of the project."""

from __future__ import annotations

from collections.abc import Iterator
from enum import StrEnum
from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.contrib.request_query import FilterField, RequestQuery
from hare.dialects.sqlite.schema.editor import SqliteSchemaEditor
from hare.exceptions import ConfigurationError
from hare.migrations import (
    AddField,
    AlterField,
    CreateModel,
    DeleteModel,
    Migration,
    MigrationRecorder,
    MigrationRunner,
    MigrationWriter,
    RemoveField,
    RenameField,
    State,
)
from hare.migrations.exceptions import MigrationLoadError
from hare.models import Model
from hare.query.enums import Lookup
from tests.testmodels import Tournament

CONTENT_APP = "content"
CONNECTION = "models"
JOURNAL_TABLE = "content_migrations"


def build_status(**extra_members: str) -> type[StrEnum]:
    return StrEnum("NewsStatus", {"DRAFT": "draft", "PUBLISHED": "published", **extra_members})


def build_news(status: type[StrEnum], **extra_fields: Any) -> type[Model]:
    attributes: dict[str, Any] = {
        "__module__": "content_runtime",
        "id": fields.IntField(primary_key=True),
        "title": fields.CharField(max_length=200),
        "status": fields.CharEnumField(status, max_length=40),
        "cover": fields.ForeignKeyField(
            "models.Tournament", related_name="covering_news", null=True, on_delete=fields.SET_NULL
        ),
        "tournaments": fields.ManyToManyField(
            "models.Tournament", related_name="news", through="content_news_tournaments"
        ),
        **extra_fields,
        "Meta": type("Meta", (), {"table": "content_news", "app": CONTENT_APP}),
    }
    return type("News", (Model,), attributes)


class ContentTypes:
    """Stands in for the panel: its migrations, their journal and the registered model."""

    def __init__(self, connection: Any) -> None:
        self.runner = MigrationRunner(connection, recorder=MigrationRecorder(connection, table_name=JOURNAL_TABLE))
        self.state = State.from_models([Tournament])
        self.model: type[Model] | None = None
        self.migrations: list[tuple[Migration, State]] = []

    async def migrate_to(self, new_model: type[Model], name: str) -> Migration:
        plan = self.state.get_operations(State.from_models([Tournament, new_model]), CONTENT_APP)
        assert plan.operations
        return await self.run(Migration(name, CONTENT_APP, operations=plan.operations), new_model)

    async def run(self, migration: Migration, new_model: type[Model] | None) -> Migration:
        # Kept as text, as the panel keeps it, and read back before running.
        source = MigrationWriter(migration.name, CONTENT_APP, migration.operations).as_string()
        migration = Migration.from_source(source, name=migration.name, app_label=CONTENT_APP)
        state_before = self.state.clone()
        self.state = await self.runner.apply(migration, self.state)
        self.migrations.append((migration, state_before))
        self.register(new_model)
        return migration

    def register(self, new_model: type[Model] | None) -> None:
        if self.model is not None:
            Hare.unregister_live_models([self.model])
        self.model = new_model
        if new_model is not None:
            Hare.register_live_models([new_model], app_label=CONTENT_APP, connection_alias=CONNECTION, managed=False)

    async def journal(self) -> list[str]:
        assert self.runner.recorder is not None
        return [key.name for key in await self.runner.recorder.applied_migrations()]


@pytest.fixture
def content(db_isolated) -> Iterator[ContentTypes]:
    content_types = ContentTypes(db_isolated.db(CONNECTION))
    yield content_types
    # A failed test leaves no model registered for the tests after it.
    content_types.register(None)


@pytest.mark.asyncio
async def test_a_content_type_through_its_whole_life(db_isolated, content):
    connection = db_isolated.db(CONNECTION)
    await content.runner.ensure_journal()
    status = build_status()
    news_model = build_news(status)

    first = Migration(
        "0001_news",
        CONTENT_APP,
        operations=content.state.get_operations(State.from_models([Tournament, news_model]), CONTENT_APP).operations,
    )
    assert [type(operation) for operation in first.operations] == [CreateModel]
    sql = "\n".join(await content.runner.collect_sql(first, content.state))
    assert "content_news" in sql
    assert "content_news_tournaments" in sql
    assert not await connection_has_table(connection, "content_news")
    await content.run(first, news_model)
    assert await content.journal() == ["0001_news"]

    tournament = await Tournament.objects.create(name="Autumn fair")
    news = await news_model.objects.create(title="Opening", status=status.PUBLISHED, cover=tournament)
    await news.tournaments.add(tournament)
    assert [item.title for item in await tournament.news.all()] == ["Opening"]
    assert await Tournament.objects.filter(news__title="Opening").count() == 1
    assert [item.title for item in await tournament.covering_news.all()] == ["Opening"]
    first_query = RequestQuery.for_model(news_model, filters=(FilterField("status", lookups=(Lookup.EXACT,)),))
    assert [item.title for item in await first_query(status=status.PUBLISHED).fetch()] == ["Opening"]

    subtitled_model = build_news(status, subtitle=fields.CharField(max_length=200, null=True))
    second = await content.migrate_to(subtitled_model, "0002_subtitle")
    assert [type(operation) for operation in second.operations] == [AddField]
    await subtitled_model.objects.filter(id=news.id).update(subtitle="Day one")
    # A row loaded before the new version holds the relations of the old one - read it again.
    tournament = await Tournament.objects.get(id=tournament.id)
    assert [(type(item), item.subtitle) for item in await tournament.news.all()] == [(subtitled_model, "Day one")]
    with pytest.raises(ConfigurationError, match="unregistered"):
        await first_query().fetch()
    assert "subtitle" in pydantic_model_creator(subtitled_model, name="SubtitledNews").model_fields

    archived_status = build_status(ARCHIVED="archived")
    archived_model = build_news(archived_status, subtitle=fields.CharField(max_length=200, null=True))
    third_plan = content.state.get_operations(State.from_models([Tournament, archived_model]), CONTENT_APP)
    assert [type(operation) for operation in third_plan.operations] == [AlterField]
    third = Migration("0003_status", CONTENT_APP, operations=third_plan.operations)
    (effect,) = content.runner.get_effects(third, content.state)
    assert effect.reversible and not effect.loses_data
    # A dialect that rebuilds the table on any change but a rename rewrites it; one that alters
    # the column in place doesn't.
    assert effect.rewrites_table is issubclass(connection.dialect.schema_editor_class, SqliteSchemaEditor)
    await content.run(third, archived_model)
    await archived_model.objects.filter(id=news.id).update(status=archived_status.ARCHIVED)

    renamed_model = build_news(
        archived_status, headline=fields.CharField(max_length=200, null=True), title=fields.CharField(max_length=200)
    )
    rename = await content.run(
        Migration("0004_rename", CONTENT_APP, operations=[RenameField("News", "subtitle", "headline")]), renamed_model
    )
    assert (await renamed_model.objects.get(id=news.id)).headline == "Day one"
    assert await content.journal() == ["0001_news", "0002_subtitle", "0003_status", "0004_rename"]

    rename_migration, state_before_rename = content.migrations.pop()
    assert rename_migration is rename
    content.state = await content.runner.unapply(rename_migration, state_before_rename)
    content.register(archived_model)
    assert (await archived_model.objects.get(id=news.id)).subtitle == "Day one"
    assert await content.journal() == ["0001_news", "0002_subtitle", "0003_status"]

    drop_field = Migration("0005_drop_subtitle", CONTENT_APP, operations=[RemoveField("News", "subtitle")])
    drop_model = Migration("0006_drop", CONTENT_APP, operations=[DeleteModel("News")])
    (field_effect,) = content.runner.get_effects(drop_field, content.state)
    assert field_effect.loses_data and "subtitle" in (field_effect.reason or "")
    content.register(None)
    await content.run(drop_model, None)
    assert not await connection_has_table(connection, "content_news")
    assert "news" not in Tournament._meta.m2m_fields
    assert "covering_news" not in Tournament._meta.backward_fk_fields


@pytest.mark.asyncio
async def test_a_column_type_change_loses_data_and_rewrites_the_table(db_isolated):
    connection = db_isolated.db(CONNECTION)
    runner = MigrationRunner(connection)
    state = State.from_models([Tournament])
    state = await runner.apply(
        Migration(
            "0001_score",
            CONTENT_APP,
            operations=[
                CreateModel(
                    "Score",
                    fields=[("id", fields.IntField(primary_key=True)), ("points", fields.CharField(max_length=20))],
                    options={"table": "content_score"},
                )
            ],
        ),
        state,
    )
    change = Migration("0002_points", CONTENT_APP, operations=[AlterField("Score", "points", fields.IntField())])
    (effect,) = runner.get_effects(change, state)
    assert effect.loses_data and effect.rewrites_table
    assert "points" in (effect.reason or "")


def test_a_migration_source_that_declares_no_migration_is_refused():
    with pytest.raises(MigrationLoadError, match="declares no Migration class"):
        Migration.from_source("value = 1\n", name="0001_nothing", app_label=CONTENT_APP)
    with pytest.raises(MigrationLoadError, match="can't be read"):
        Migration.from_source("class Migration(:\n", name="0001_broken", app_label=CONTENT_APP)


def test_a_model_without_an_app_needs_one_for_its_state():
    orphan = type("Orphan", (Model,), {"__module__": "content_runtime", "id": fields.IntField(primary_key=True)})
    with pytest.raises(ConfigurationError, match="names no app"):
        State.from_models([orphan])
    assert (CONTENT_APP, "Orphan") in State.from_models([orphan], app_label=CONTENT_APP).models


async def connection_has_table(connection: Any, table: str) -> bool:
    # Local import: the introspector's modules import the migrations package.
    from hare.inspectdb.introspector import SchemaIntrospector

    return await SchemaIntrospector.table_exists(connection, table)
