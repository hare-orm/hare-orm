"""A migration state is data; its model classes are rendered only when an operation asks for them, and
then model by model, as the operations change it."""

from __future__ import annotations

import pytest

from hare import fields
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.exceptions import InconsistentMigrationStateError
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddField,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


def get_create_model(name: str, *relation_targets: str, **options: object) -> CreateModel:
    model_fields: list[tuple[str, object]] = [("id", fields.IntField(primary_key=True))]
    model_fields.extend(
        (f"to_{target.lower()}", fields.ForeignKeyField(f"app.{target}", related_name=f"{name.lower()}_set"))
        for target in relation_targets
    )
    return CreateModel(name=name, fields=model_fields, options={"table": name.lower(), "app": "app", **options})


@pytest.fixture
def render_count(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered_names: list[str] = []
    render = ModelState.render

    def counting_render(model_state: ModelState, *args: object, **kwargs: object) -> object:
        rendered_names.append(model_state.name)
        return render(model_state, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ModelState, "render", counting_render)
    return rendered_names


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


def test_a_changed_model_is_rendered_again_with_the_models_it_reaches(render_count: list[str]) -> None:
    state = State()
    get_create_model("Author").state_forward("app", state)
    get_create_model("Book", "Author").state_forward("app", state)
    get_create_model("Shelf").state_forward("app", state)
    _ = state.apps
    render_count.clear()
    AddField(model_name="Book", name="pages", field=fields.IntField(default=0)).state_forward("app", state)
    book = state.apps.get_model("app.Book")
    assert "pages" in book._meta.fields_map
    assert sorted(render_count) == ["Author", "Book"]
    assert book._meta.fields_map["to_author"].related_model is state.apps.get_model("app.Author")


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
