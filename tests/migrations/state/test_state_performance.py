from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.migrations.migration import Migration
from hare.migrations.operations import CreateModel, Operation
from hare.migrations.state.state import State

pytestmark = pytest.mark.database_independent


def _make_create_model_op(i: int) -> CreateModel:
    field_list: list[tuple[str, Any]] = [
        ("id", fields.IntField(primary_key=True)),
        ("name", fields.CharField(max_length=255)),
        ("description", fields.TextField(null=True)),
        ("created_at", fields.DatetimeField(auto_now_add=True)),
        ("updated_at", fields.DatetimeField(auto_now=True)),
        ("is_active", fields.BooleanField(default=True)),
        ("sort_order", fields.IntField(default=0)),
    ]
    if i > 0 and i % 3 == 0:
        field_list.append(
            ("parent", fields.ForeignKeyField(f"app.Model{i - 1}", related_name=f"ch_{i}")),
        )
    return CreateModel(name=f"Model{i}", fields=field_list, options={"table": f"model_{i}"})


def _build_migrations(num_models: int, batch_size: int = 20) -> list[Migration]:
    all_ops: list[Operation] = [_make_create_model_op(i) for i in range(num_models)]
    migrations: list[Migration] = []
    for start in range(0, len(all_ops), batch_size):
        batch = all_ops[start : start + batch_size]

        class Mig(Migration):
            pass

        Mig.operations = batch
        migrations.append(Mig(name=f"mig_{start // batch_size:04d}", app_label="app"))
    return migrations


@pytest.mark.asyncio
async def test_apply_dry_run_preserves_previous_states():
    """Returned states accumulate migrations without changing earlier states or sharing fields."""
    migrations = _build_migrations(10, batch_size=5)
    states = [State()]
    for migration in migrations:
        previous = states[-1]
        model_names = tuple(previous.models)
        result = await migration.apply(previous, dry_run=True, schema_editor=None)
        assert result is not previous
        assert tuple(previous.models) == model_names
        states.append(result)

    assert [len(state.models) for state in states] == [0, 5, 10]
    states[-1].models[("app", "Model0")].fields["name"].max_length = 64
    assert states[1].models[("app", "Model0")].fields["name"].max_length == 255


def test_state_clone_produces_independent_copy():
    """Cloned state must be fully independent from the original."""
    state = State()
    for i in range(50):
        field_list: list[tuple[str, Any]] = [
            ("id", fields.IntField(primary_key=True)),
            ("name", fields.CharField(max_length=255)),
        ]
        if i > 0 and i % 5 == 0:
            field_list.append(
                ("ref", fields.ForeignKeyField(f"app.Model{i - 1}", related_name=f"ch_{i}")),
            )
        op = CreateModel(name=f"Model{i}", fields=field_list, options={"table": f"model_{i}"})
        op.state_forward("app", state)

    cloned = state.clone()

    # Mutate a field on the clone
    key = ("app", "Model0")
    cloned_field = cloned.models[key].fields["name"]
    cloned_field.max_length = 999

    # Original must be unaffected
    original_field = state.models[key].fields["name"]
    assert original_field.max_length == 255

    # Add a model to the clone
    extra_op = CreateModel(
        name="ExtraModel",
        fields=[("id", fields.IntField(primary_key=True))],
    )
    extra_op.state_forward("app", cloned)

    assert ("app", "ExtraModel") in cloned.models
    assert ("app", "ExtraModel") not in state.models


def test_state_clone_preserves_relations():
    """Cloned state must have functional FK relations."""
    state = State()
    CreateModel(
        name="Parent",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "parent"},
    ).state_forward("app", state)
    CreateModel(
        name="Child",
        fields=[
            ("id", fields.IntField(primary_key=True)),
            ("parent", fields.ForeignKeyField("app.Parent", related_name="children")),
        ],
        options={"table": "child"},
    ).state_forward("app", state)

    cloned = state.clone()

    parent_model = cloned.apps.get_model("app.Parent")
    child_model = cloned.apps.get_model("app.Child")

    fk = child_model._meta.fields_map["parent"]
    assert isinstance(fk, ForeignKeyFieldInstance)
    assert fk.related_model is parent_model
