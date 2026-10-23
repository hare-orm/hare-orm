"""Declarations are compared by the call a migration file writes them as, and makemigrations checks the
migrations it made: applied, they reach the models; each file reads back as the operations it was
written from; they depend on each other in no cycle."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from hare import fields
from hare.classes.class_path import ClassPath
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes.index import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.migrations.exceptions import MigrationAutodetectorError
from hare.migrations.making.migration_checks import MigrationChecks
from hare.migrations.making.migration_maker import MigrationMaker
from hare.migrations.migration import Migration
from hare.migrations.operations import AddConstraint, CreateModel, RemoveConstraint
from hare.migrations.state.state import State
from hare.migrations.writer.migration_writer import MigrationWriter
from hare.models import Model
from hare.query.expressions import F, Q
from hare.query.functions import Lower

pytestmark = pytest.mark.database_independent


def get_model(**meta_options: Any) -> type[Model]:
    meta = type("Meta", (), {"app": "app", "table": "account", **meta_options})
    attributes: dict[str, Any] = {
        "id": fields.IntField(primary_key=True),
        "name": fields.CharField(max_length=20),
        "low": fields.IntField(),
        "high": fields.IntField(),
        "Meta": meta,
    }
    return type("Account", (Model,), attributes)


@pytest.mark.parametrize(
    "meta_options",
    [
        pytest.param(
            {"constraints": [CheckConstraint(check=Q(high__gt=F("low") + 1), name="account_range")]},
            id="check_over_an_expression",
        ),
        pytest.param({"indexes": [Index(Lower("name"), name="account_lower_name")]}, id="index_over_an_expression"),
        pytest.param(
            {
                "triggers": [
                    Trigger(name="account_touch", on=TriggerEvent.UPDATE, body=RawSQLTerm("BEGIN RETURN NEW; END;"))
                ]
            },
            id="trigger",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_second_run_over_a_written_migration_makes_nothing(meta_options) -> None:
    """A hare expression compares by identity - a declaration read back from its migration file used to
    differ from the model's own, and every makemigrations removed and added it again."""
    new_state = State.from_models([get_model(**meta_options)])
    writer = MigrationWriter("0001_initial", "app", list(State().get_operations(new_state, "app").operations))
    migration = Migration.from_source(writer.as_string(), name="0001_initial", app_label="app")
    state = await migration.apply(State(), dry_run=True)
    assert state.get_operations(new_state, "app").operations == ()


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"deferrable": True}, id="deferrable"),
        pytest.param({"include": ("name",)}, id="include"),
    ],
)
def test_a_renamed_constraint_changed_otherwise_too_is_removed_and_added(changed) -> None:
    """Only some arguments of a unique constraint were compared - a rename with another change came out
    as a RenameConstraint alone, the change lost."""
    old_state = State.from_models([get_model(constraints=[UniqueConstraint(fields=("low",), name="account_low")])])
    new_state = State.from_models(
        [get_model(constraints=[UniqueConstraint(fields=("low",), name="account_low_key", **changed)])]
    )
    operations = old_state.get_operations(new_state, "app").operations
    assert [type(operation) for operation in operations] == [RemoveConstraint, AddConstraint]


@dataclass(frozen=True)
class CommentedCheck(CheckConstraint):
    """A declaration with an argument its deconstruct() leaves out."""

    comment: str = ""


def test_an_argument_a_declaration_does_not_write_is_refused_before_the_file_is() -> None:
    constraint = CommentedCheck(check=Q(high__gt=0), name="account_high", comment="kept")
    assert ClassPath.get(CommentedCheck).endswith("CommentedCheck")
    writer = MigrationWriter("0002_check", "app", [AddConstraint(model_name="Account", constraint=constraint)])
    with pytest.raises(MigrationAutodetectorError, match=r"operation\[2\]\.constraint\[1\]\.comment"):
        MigrationChecks.raise_if_not_read_back(writer)


@pytest.mark.asyncio
async def test_migrations_not_reaching_the_models_are_refused() -> None:
    model = get_model()
    autodetector = SimpleNamespace(
        apps_config={"app": {"migrations": "app.migrations"}},
        target_app_labels=["app"],
        current_state=lambda tracked_state: State.from_models([model]),
    )
    incomplete = MigrationWriter(
        "0001_initial", "app", [CreateModel(name="Other", fields=[("id", fields.IntField(primary_key=True))])]
    )
    with pytest.raises(MigrationAutodetectorError, match="Create model Account"):
        await MigrationChecks.raise_if_not_converged(autodetector, State(), [incomplete])  # type: ignore[arg-type]


def test_new_migrations_depending_on_each_other_in_a_cycle_are_refused() -> None:
    first = MigrationWriter("0002_first", "app", [], dependencies=[("other", "0002_second")])
    second = MigrationWriter("0002_second", "other", [], dependencies=[("app", "0002_first")])
    with pytest.raises(MigrationAutodetectorError, match="cycle"):
        MigrationMaker.get_writers_in_dependency_order([first, second])
