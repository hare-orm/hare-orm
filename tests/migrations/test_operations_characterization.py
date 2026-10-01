"""Characterization tests for the Add*/Remove*/Rename*/Create*/Delete* Operation family.

Locks in the exact SQL each operation's database_backward() currently produces (the forward
direction is already covered by test_operations_database.py) before the operations.py mixin
refactor, so that refactor is verified against these exact strings rather than against intent.
"""

from __future__ import annotations

import pytest

from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    CreateModel,
    DeleteModel,
    RemoveConstraint,
    RemoveField,
    RemoveIndex,
    RenameConstraint,
    RenameModel,
)
from hare.migrations.state.apps import StateApps
from hare.migrations.state.project import State
from tests.migrations.test_operations_database import TestSchemaEditor, build_state, make_model
from tests.utils.fake_client import FakeClient


@pytest.mark.asyncio
async def test_add_field_backward_matches_remove_field_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddField(model_name="Widget", name="name", field=fields.TextField()).state_forward("models", state)

    op = AddField(model_name="Widget", name="name", field=fields.TextField())
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['ALTER TABLE "widget" DROP COLUMN "name" CASCADE']


@pytest.mark.asyncio
async def test_remove_field_backward_matches_add_field_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    ).state_forward("models", state)

    op = RemoveField(model_name="Widget", name="name")
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['ALTER TABLE "widget" ADD COLUMN "name" TEXT NOT NULL']


@pytest.mark.asyncio
async def test_add_index_backward_matches_remove_index_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    op = AddIndex(model_name="Widget", index=Index(fields=("id",), name="idx_widget_id"))
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['DROP INDEX "idx_widget_id"']


@pytest.mark.asyncio
async def test_remove_index_backward_matches_add_index_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddIndex(model_name="Widget", index=Index(fields=("id",), name="idx_widget_id")).state_forward("models", state)

    op = RemoveIndex(model_name="Widget", name="idx_widget_id")
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['CREATE INDEX "idx_widget_id" ON "widget" ("id");']


@pytest.mark.asyncio
async def test_add_constraint_backward_matches_remove_constraint_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)

    constraint = UniqueConstraint(fields=("id",), name="uniq_widget_id")
    op = AddConstraint(model_name="Widget", constraint=constraint)
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['ALTER TABLE "widget" DROP CONSTRAINT "uniq_widget_id"']


@pytest.mark.asyncio
async def test_remove_constraint_backward_matches_add_constraint_forward() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddConstraint(
        model_name="Widget",
        constraint=UniqueConstraint(fields=("id",), name="uniq_widget_id"),
    ).state_forward("models", state)

    op = RemoveConstraint(model_name="Widget", name="uniq_widget_id")
    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['ALTER TABLE "widget" ADD CONSTRAINT "uniq_widget_id" UNIQUE ("id")']


@pytest.mark.asyncio
async def test_create_model_backward_drops_table() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())

    op = CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    )
    op.state_forward("models", state)

    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == ['DROP TABLE "widget" CASCADE']


@pytest.mark.asyncio
async def test_delete_model_backward_recreates_table() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    state = State(models={}, apps=StateApps())
    CreateModel(
        name="Widget",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
    ).state_forward("models", state)

    op = DeleteModel(name="Widget")

    await op.database_backward("models", state, state, state_editor=editor)

    assert client.executed == [
        'CREATE TABLE "widget" (\n    "id" INT NOT NULL PRIMARY KEY,\n    "name" TEXT NOT NULL\n);'
    ]


@pytest.mark.asyncio
async def test_rename_constraint_state_forward_preserves_check_expression() -> None:
    """RenameConstraint.state_forward's CheckConstraint branch must carry over `check`, not
    just `name` - a risk if it gets merged carelessly with the UniqueConstraint branch."""
    state = State(models={}, apps=StateApps())
    CreateModel(name="Widget", fields=[("id", fields.IntField(primary_key=True))]).state_forward("models", state)
    AddConstraint(
        model_name="Widget",
        constraint=CheckConstraint(check=RawSQLTerm("id > 0"), name="ck_old"),
    ).state_forward("models", state)

    op = RenameConstraint(model_name="Widget", old_name="ck_old", new_name="ck_new")
    op.state_forward("models", state)

    constraints = state.models[("models", "Widget")].get_option_list("constraints")
    assert len(constraints) == 1
    renamed = constraints[0]
    assert isinstance(renamed, CheckConstraint)
    assert renamed.name == "ck_new"
    assert renamed.check == RawSQLTerm("id > 0")


@pytest.mark.asyncio
async def test_rename_model_backward_renames_table_back() -> None:
    client = FakeClient("sql")
    editor = TestSchemaEditor(client)

    OldWidget = make_model("Widget", id=fields.IntField(primary_key=True))
    NewGadget = make_model("Gadget", id=fields.IntField(primary_key=True), meta_options={"table": "gadget"})

    old_state = build_state("models", NewGadget)
    new_state = build_state("models", OldWidget)

    op = RenameModel(old_name="Widget", new_name="Gadget")

    await op.database_backward("models", old_state, new_state, state_editor=editor)

    assert client.executed == ['ALTER TABLE "gadget" RENAME TO "widget"']
