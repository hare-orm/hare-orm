"""Prepared boundaries remain independent of later operations and rendered models."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes import Index
from hare.ddl.schema_objects.trigger import Trigger
from hare.migrations.making.declared_attribute_values import DeclaredAttributeValues
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
from hare.migrations.state.prepared_operation_states import PreparedOperationStates
from hare.migrations.state.state import State
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


def test_each_prepared_boundary_keeps_the_declaration_before_and_after_its_operation() -> None:
    source = State()
    operations = get_operations()
    prepared = PreparedOperationStates("app", operations, source)
    expected = source.clone()
    for declaration, item in zip(operations, prepared.operations, strict=True):
        assert DeclaredAttributeValues.get(get_state_description(item.before)) == DeclaredAttributeValues.get(
            get_state_description(expected)
        )
        declaration.snapshot().state_forward("app", expected)
        assert DeclaredAttributeValues.get(get_state_description(item.after)) == DeclaredAttributeValues.get(
            get_state_description(expected)
        )
    assert source.models == {}
    assert source.extensions == source.schemas == set()


def test_custom_field_changes_leave_prior_boundaries_and_input_unchanged() -> None:
    source = PreparedOperationStates("app", get_operations()[:9], State()).after
    prepared = PreparedOperationStates("app", [MakePagesRequired()], source)
    assert source.models[("app", "Book")].fields["pages"].null is True
    assert prepared.before.models[("app", "Book")].fields["pages"].null is True
    assert prepared.after.models[("app", "Book")].fields["pages"].null is False


def test_rendering_one_boundary_does_not_change_another_boundary() -> None:
    prepared = PreparedOperationStates("app", get_operations()[:12], State())
    assert "heading" in prepared.after.apps.get_model("app.Book")._meta.fields_map
    before_rename = prepared.operations[-1].before
    book = before_rename.apps.get_model("app.Book")
    assert "title" in book._meta.fields_map
    assert "heading" not in book._meta.fields_map
    before_book = prepared.operations[4].before
    assert list(before_book.apps.apps.get("app", {})) == ["Author"]
    author = before_book.apps.get_model("app.Author")
    assert author._meta.backward_foreign_key_fields == set()
    assert author._meta.hidden_backward_relations == {}
