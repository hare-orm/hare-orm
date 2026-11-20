"""A migration state is data; its model classes are rendered only when an operation asks for them, and
then model by model, as the operations change it."""

from __future__ import annotations

from types import ModuleType

import pytest

from hare import Model, fields
from hare.core.hare_context import HareContext
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import InconsistentMigrationStateError
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    AlterField,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    RenameField,
)
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", (False, True))
@pytest.mark.parametrize("relation_type", ("fk", "hidden-fk", "o2o", "hidden-o2o", "m2m"))
@pytest.mark.parametrize("operation", ("delete", "rename", "alter"))
async def test_historical_model_changes_preserve_external_target_snapshots(composite, relation_type, operation):
    class Parent(Model):
        number = fields.IntField(primary_key=not composite)
        region = fields.IntField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            managed = False

    factory = (
        fields.ManyToManyField
        if relation_type == "m2m"
        else (fields.OneToOneField if "o2o" in relation_type else fields.ForeignKeyField)
    )

    class Child(Model):
        parent = factory(Parent, related_name=False if "hidden" in relation_type else "children", db_constraint=False)
        note = fields.CharField(max_length=10)

        class Meta:
            app = "historical"

    module = ModuleType("tests._historical_external_target")
    module.__models__ = [Parent]
    async with HareContext() as context:
        await context.init(
            {"connections": {"default": "sqlite+aiosqlite://:memory:"}, "apps": {"live": {"models": [module]}}},
            connect=False,
        )
        live_binding = context.apps.get_binding(Parent)
        live_fields = dict(live_binding.fields_map)
        live_hidden = dict(live_binding.hidden_backward_relations)
        state = State.from_models([Child], default_connections={"historical": "default"})
        historical = state.apps.get_model("historical.Child")
        retained = state.apps.get_binding("live.Parent")
        fields_before = dict(retained.fields_map)
        hidden_before = dict(retained.hidden_backward_relations)
        assert any(
            getattr(field, "related_model", None) is historical
            for field in (*fields_before.values(), *hidden_before.values())
        )
        if operation == "delete":
            change = DeleteModel("Child")
        elif operation == "rename":
            change = RenameField("Child", "parent", "renamed")
        else:
            change = AlterField("Child", "note", fields.CharField(max_length=20))
        change.state_forward("historical", state)
        if operation == "delete":
            assert "Child" not in state.apps.apps.get("historical", {})
        else:
            replacement = state.apps.get_model("historical.Child")
            assert replacement is not historical
        assert retained.fields_map == fields_before
        assert retained.hidden_backward_relations == hidden_before
        assert context.apps.get_binding(Parent) is live_binding
        assert live_binding.fields_map == live_fields
        assert live_binding.hidden_backward_relations == live_hidden
        if operation != "delete":
            new_parent = state.apps.get_binding("live.Parent")
            assert new_parent is not retained
            assert (
                sum(
                    getattr(field, "related_model", None) is replacement
                    for field in (*new_parent.fields_map.values(), *new_parent.hidden_backward_relations.values())
                )
                == 1
            )


def get_create_model(name: str, *relation_targets: str, **options: object) -> CreateModel:
    model_fields: list[tuple[str, object]] = [("id", fields.IntField(primary_key=True))]
    model_fields.extend(
        (f"to_{target.lower()}", fields.ForeignKeyField(f"app.{target}", related_name=f"{name.lower()}_set"))
        for target in relation_targets
    )
    return CreateModel(name=name, fields=model_fields, options={"table": name.lower(), "app": "app", **options})


@pytest.mark.asyncio
async def test_replaying_migrations_renders_no_model(render_count: list[str]) -> None:
    migration = Migration(
        "0001_initial",
        "app",
        operations=[
            get_create_model("Author"),
            get_create_model("Book", "Author"),
            AddField(model_name="Book", name="pages", field=fields.IntField(default=0)),
            DeleteModel(name="Book"),
        ],
    )
    state = await migration.apply(State(), dry_run=True)
    assert render_count == []
    assert list(state.models) == [("app", "Author")]


def test_models_created_one_after_another_are_rendered_once_each(render_count: list[str]) -> None:
    state = State()
    names = [f"Model{number}" for number in range(12)]
    for position, name in enumerate(names):
        get_create_model(name, *names[:position][-1:]).state_forward("app", state)
        assert state.apps.get_model(f"app.{name}").__name__ == name
    assert sorted(render_count) == sorted(names)


def test_a_changed_model_rebuilds_related_bindings_and_reuses_unchanged_classes(render_count: list[str]) -> None:
    state = State()
    get_create_model("Author").state_forward("app", state)
    get_create_model("Book", "Author").state_forward("app", state)
    get_create_model("Shelf").state_forward("app", state)
    _ = state.apps
    render_count.clear()
    AddField(model_name="Book", name="pages", field=fields.IntField(default=0)).state_forward("app", state)
    book = state.apps.get_model("app.Book")
    assert "pages" in state.apps.get_binding(book).fields_map
    assert sorted(render_count) == ["Book"]
    assert state.apps.get_binding(book).fields_map["to_author"].related_model is state.apps.get_model("app.Author")


def test_a_clone_keeps_its_source_rendered_and_renders_only_when_asked(render_count: list[str]) -> None:
    state = State()
    get_create_model("Author").state_forward("app", state)
    _ = state.apps
    render_count.clear()
    clone = state.clone()
    assert render_count == []
    _ = state.apps
    assert render_count == []
    _ = clone.apps
    assert render_count == ["Author"]


def test_a_relation_to_a_model_no_migration_creates_is_refused_without_rendering(render_count: list[str]) -> None:
    state = State()
    get_create_model("Book", "Author").state_forward("app", state)
    with pytest.raises(InconsistentMigrationStateError, match=r"app\.Book\.to_author -> app\.Author"):
        state.validate_relations_initialized()
    assert render_count == []


def test_an_extension_and_a_schema_a_deleted_model_used_are_not_created_again() -> None:
    """The extension and the schema stay in the database after the model is deleted - a model needing
    them again gets no second CreateExtension/CreateSchema, whose rollback would drop them from under
    the first migration."""
    old_state = State()
    for operation in (
        CreateSchema(schema_name="archive"),
        CreateExtension(extension_name="citext"),
        get_create_model("Note", extensions=("citext",), schema="archive"),
        DeleteModel(name="Note"),
    ):
        operation.state_forward("app", old_state)
    new_state = State()
    get_create_model("Note", extensions=("citext",), schema="archive").state_forward("app", new_state)
    operations = OperationGenerator(old_state, new_state).generate()
    assert [type(operation) for operation in operations] == [CreateModel]
