"""A removed model nothing left points at is taken out of the rendered classes in place - the models it
pointed at end up as a fresh rendering of the state without it draws them."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations import AddField, CreateModel, RemoveField, RenameModel
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_change_journal import StateChangeJournal
from hare.models import Model

pytestmark = pytest.mark.database_independent


def get_create_model(name: str, *model_fields: tuple[str, Any]) -> CreateModel:
    return CreateModel(
        name=name,
        fields=[("id", fields.IntField(primary_key=True)), *model_fields],
        options={"table": name.lower(), "app": "app"},
    )


def get_operations() -> list[CreateModel]:
    return [
        get_create_model("Author"),
        get_create_model("Tag"),
        get_create_model("Reader"),
        get_create_model(
            "Book",
            ("author", fields.ForeignKeyField("app.Author", related_name="books")),
            ("editor", fields.ForeignKeyField("app.Author", related_name="edited_books", null=True)),
            ("parent", fields.ForeignKeyField("app.Book", related_name="children", null=True)),
            ("tags", fields.ManyToManyField("app.Tag", related_name="books")),
        ),
        get_create_model("Detail", ("book", fields.OneToOneField("app.Book", related_name="detail"))),
        get_create_model("Note", ("book", fields.ForeignKeyField("app.Book", related_name=False))),
        get_create_model(
            "Shelf",
            ("readers", ManyToManyFieldInstance("app.Reader", through="app.Membership", related_name="shelves")),
        ),
        get_create_model(
            "Membership",
            ("shelf", fields.ForeignKeyField("app.Shelf", related_name="memberships")),
            ("reader", fields.ForeignKeyField("app.Reader", related_name="memberships")),
        ),
        get_create_model("Review", ("author", fields.ForeignKeyField("app.Author", related_name="reviews"))),
    ]


def get_meta_description(model: type[Model]) -> tuple[Any, ...]:
    meta = model._meta
    return (
        sorted((name, type(field).__name__) for name, field in meta.fields_map.items()),
        sorted(meta.backward_foreign_key_fields),
        sorted(meta.backward_one_to_one_fields),
        sorted(meta.many_to_many_fields),
        sorted(meta.foreign_key_fields),
        sorted(meta.one_to_one_fields),
        sorted(meta.fetch_fields),
        sorted(meta.hidden_backward_relations),
        sorted(meta.fields_db_projection.items()),
        meta.db_fields,
        meta._inited,
    )


def get_apps_description(state: State) -> dict[str, tuple[Any, ...]]:
    return {name: get_meta_description(model) for name, model in state.apps.apps.get("app", {}).items()}


@pytest.fixture
def render_count(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    rendered_names: list[str] = []
    render = ModelState.render

    def counting_render(model_state: ModelState, *args: object, **kwargs: object) -> object:
        rendered_names.append(model_state.name)
        return render(model_state, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ModelState, "render", counting_render)
    return rendered_names


def test_models_taken_back_one_by_one_leave_the_classes_a_fresh_rendering_draws(render_count: list[str]) -> None:
    state = State()
    undos = [StateChangeJournal.forward(state, "app", operation) for operation in get_operations()]
    _ = state.apps
    for undo in reversed(undos):
        state.undo(undo)
        fresh_state = state.clone()
        assert get_apps_description(state) == get_apps_description(fresh_state)


def test_a_model_nothing_points_at_is_taken_out_without_rendering(render_count: list[str]) -> None:
    state = State()
    undos = [StateChangeJournal.forward(state, "app", operation) for operation in get_operations()]
    _ = state.apps
    render_count.clear()
    for undo in reversed(undos[-1:]):
        state.undo(undo)
    author = state.apps.get_model("app.Author")
    assert render_count == []
    assert "reviews" not in author._meta.fields_map
    assert sorted(author._meta.backward_foreign_key_fields) == ["books", "edited_books"]


def test_a_through_model_a_model_left_goes_through_is_rendered_again(render_count: list[str]) -> None:
    state = State()
    undos = [StateChangeJournal.forward(state, "app", operation) for operation in get_operations()[:-1]]
    _ = state.apps
    render_count.clear()
    state.undo(undos[-1])
    assert state.apps.get_model("app.Shelf")._meta._inited is False
    assert render_count != []


def test_a_model_rendered_again_keeps_the_hidden_backward_relations_pointing_at_it() -> None:
    """A relation of related_name=False has no field on its target - the model declaring it wasn't rendered
    again with the target, and the target lost the hidden backward relation hare cascades deletes through."""
    state = State()
    undos = [
        StateChangeJournal.forward(state, "app", operation)
        for operation in [
            get_create_model("Author"),
            get_create_model("Tag"),
            get_create_model(
                "Book",
                ("author", fields.ForeignKeyField("app.Author", related_name="books")),
                ("tags", fields.ManyToManyField("app.Tag", related_name="books")),
            ),
            RenameModel("Author", "Writer"),
            AddField("Tag", "writer", fields.ForeignKeyField("app.Writer", related_name=False, null=True)),
            RemoveField("Book", "tags"),
        ]
    ]
    _ = state.apps
    state.undo(undos[-1])
    assert get_apps_description(state) == get_apps_description(state.clone())
    assert list(state.apps.get_model("app.Writer")._meta.hidden_backward_relations) == ["app.Tag.writer"]
