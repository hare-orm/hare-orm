"""A removed model nothing left points at is taken out of the rendered classes in place - the models it
pointed at end up as a fresh rendering of the state without it draws them."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.migrations.operations import AddField, CreateModel, DeleteModel, RemoveField, RenameModel
from hare.migrations.state.prepared_operation_states import PreparedOperationStates
from hare.migrations.state.state import State
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


def test_each_prepared_boundary_renders_the_same_relations_as_a_fresh_snapshot() -> None:
    prepared = PreparedOperationStates("app", get_operations(), State())
    for item in reversed(prepared.operations):
        assert get_apps_description(item.before) == get_apps_description(item.before.clone())


def test_a_model_nothing_points_at_is_taken_out_without_rendering(render_count: list[str]) -> None:
    state = PreparedOperationStates("app", get_operations(), State()).after
    _ = state.apps
    render_count.clear()
    DeleteModel("Review").state_forward("app", state)
    author = state.apps.get_model("app.Author")
    assert render_count == []
    assert "reviews" not in author._meta.fields_map
    assert sorted(author._meta.backward_foreign_key_fields) == ["books", "edited_books"]


def test_a_through_model_a_model_left_goes_through_is_rendered_again(render_count: list[str]) -> None:
    state = PreparedOperationStates("app", get_operations()[:-1], State()).after
    _ = state.apps
    render_count.clear()
    DeleteModel("Membership").state_forward("app", state)
    assert state.apps.get_model("app.Shelf")._meta._inited is False
    assert render_count != []


def test_a_model_rendered_again_keeps_the_hidden_backward_relations_pointing_at_it() -> None:
    """A relation of related_name=False has no field on its target - the model declaring it wasn't rendered
    again with the target, and the target lost the hidden backward relation hare cascades deletes through."""
    prepared = PreparedOperationStates(
        "app",
        [
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
        ],
        State(),
    )
    _ = prepared.after.apps
    state = prepared.operations[-1].before
    assert get_apps_description(state) == get_apps_description(state.clone())
    assert list(state.apps.get_model("app.Writer")._meta.hidden_backward_relations) == ["app.Tag.writer"]
