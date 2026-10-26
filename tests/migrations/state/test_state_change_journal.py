"""The state change journal records what each operation changes, so a state is taken back operation by
operation to exactly what it was before each one."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes import Index
from hare.ddl.schema_objects.trigger import Trigger
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterEnumType,
    AlterField,
    AlterModelOptions,
    AlterModelTable,
    CreateEnumType,
    CreateExtension,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropEnumType,
    DropSchema,
    Operation,
    RemoveExtension,
    RemoveField,
    RemoveIndex,
    RenameField,
    RenameModel,
    SeparateDatabaseAndState,
)
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_change_journal import StateChangeJournal
from hare.query.expressions import Q

pytestmark = pytest.mark.database_independent


class MakePagesRequired(Operation):
    """An operation of an application changing a field of a model state in place."""

    def state_forward(self, app_label: str, state: State) -> None:
        state.models[(app_label, "Book")].fields["pages"].null = False

    async def database_forward(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def database_backward(self, *args: Any, **kwargs: Any) -> None:
        return None


def get_model_description(model_state: ModelState) -> tuple[Any, ...]:
    return (
        model_state.name,
        model_state.app,
        model_state.table,
        model_state.abstract,
        model_state.description,
        model_state.bases,
        model_state.pk_field_name,
        dict(model_state.options),
        {name: (field.deconstruct(), field.null) for name, field in model_state.fields.items()},
    )


def get_state_description(state: State) -> tuple[Any, ...]:
    return (
        {key: get_model_description(model_state) for key, model_state in state.models.items()},
        set(state.extensions),
        set(state.schemas),
        dict(state.enum_types),
    )


def get_operations() -> list[Operation]:
    return [
        CreateExtension("pg_trgm"),
        CreateSchema("reports"),
        CreateEnumType("mood", ("happy", "sad")),
        CreateModel(
            name="Author",
            fields=[("id", fields.IntField(primary_key=True)), ("name", fields.CharField(max_length=50, unique=True))],
            options={"table": "author", "app": "app"},
        ),
        CreateModel(
            name="Book",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("title", fields.CharField(max_length=50)),
                ("author", fields.ForeignKeyField("app.Author", related_name="books", to_field="name")),
            ],
            options={"table": "book", "app": "app"},
        ),
        CreateModel(
            name="Tag",
            fields=[("id", fields.IntField(primary_key=True)), ("label", fields.CharField(max_length=20))],
            options={"table": "tag", "app": "app"},
        ),
        AddField(model_name="Book", name="tags", field=fields.ManyToManyField("app.Tag", related_name="books")),
        CreateModel(
            name="Profile",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("author", fields.OneToOneField("app.Author", related_name="profile")),
                ("reviewer", fields.ForeignKeyField("app.Author", related_name=False)),
            ],
            options={"table": "profile", "app": "app"},
        ),
        AddField(model_name="Book", name="pages", field=fields.IntField(null=True)),
        AlterField(model_name="Book", name="pages", field=fields.BigIntField(null=True)),
        MakePagesRequired(),
        RenameField(model_name="Book", old_name="title", new_name="heading"),
        RenameField(model_name="Author", old_name="name", new_name="full_name"),
        AddIndex("Book", Index(fields=("heading",), name="book_heading_idx")),
        AddConstraint("Book", CheckConstraint(check=Q(pages__gte=0), name="book_pages_check")),
        AddTrigger("Book", Trigger(name="book_trig", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;"))),
        AlterModelOptions("Book", {"table_description": "Books"}),
        AlterModelTable("Tag", "tag_table"),
        RenameModel("Tag", "Label"),
        SeparateDatabaseAndState(
            state_operations=[AddField(model_name="Label", name="weight", field=fields.IntField(default=0))]
        ),
        RemoveIndex("Book", "book_heading_idx"),
        RemoveField(model_name="Label", name="weight"),
        DeleteModel(name="Profile"),
        AlterEnumType("mood", ("happy", "sad"), ("happy", "sad", "calm")),
        DropEnumType("mood", ("happy", "sad", "calm")),
        RemoveExtension("pg_trgm"),
        DropSchema("reports"),
    ]


def test_each_undo_takes_the_state_back_to_before_its_operation() -> None:
    state = State()
    descriptions = []
    undos = []
    for operation in get_operations():
        descriptions.append(get_state_description(state))
        undos.append(StateChangeJournal.forward(state, "app", operation))
    for description, undo in zip(reversed(descriptions), reversed(undos), strict=True):
        state.undo(undo)
        assert get_state_description(state) == description


def test_an_operation_records_only_the_models_it_changes() -> None:
    state = State()
    for operation in get_operations()[:8]:
        StateChangeJournal.forward(state, "app", operation)
    undo = StateChangeJournal.forward(
        state, "app", AddField(model_name="Book", name="isbn", field=fields.CharField(max_length=13, null=True))
    )
    assert set(undo.model_states) == {("app", "Book")}


def test_an_operation_of_an_application_records_every_model() -> None:
    state = State()
    for operation in get_operations()[:9]:
        StateChangeJournal.forward(state, "app", operation)
    undo = StateChangeJournal.forward(state, "app", MakePagesRequired())
    assert set(undo.model_states) == set(state.models)
    state.undo(undo)
    assert state.models[("app", "Book")].fields["pages"].null is True


def test_the_rendered_classes_follow_the_state_taken_back() -> None:
    state = State()
    undos = [StateChangeJournal.forward(state, "app", operation) for operation in get_operations()[:12]]
    assert state.apps.get_model("app.Book")._meta.fields_map["heading"]
    for undo in reversed(undos[10:]):
        state.undo(undo)
    book = state.apps.get_model("app.Book")
    assert "title" in book._meta.fields_map
    assert "heading" not in book._meta.fields_map
    for undo in reversed(undos[4:10]):
        state.undo(undo)
    assert list(state.apps.apps.get("app", {})) == ["Author"]
    author = state.apps.get_model("app.Author")
    assert author._meta.backward_foreign_key_fields == set()
    assert author._meta.hidden_backward_relations == {}
