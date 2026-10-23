"""MigrationSplitter: the new operations of every app, ordered together, cut into migrations - a next
migration of an app where it needs a migration of another app depending on its current one."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_splitter import MigrationSplitter
from hare.migrations.operations import CreateModel, DeleteModel
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model

pytestmark = pytest.mark.database_independent


def get_leaf_migrations(app_label: str) -> list[tuple[str, str]]:
    return [(app_label, "0001_initial")]


def make_state(**models_by_app: list[type[Model]]) -> State:
    state = State()
    for app_label, models in models_by_app.items():
        for model in models:
            state.models[(app_label, model.__name__)] = ModelState.make_from_model(app_label, model)
    return state


def make_model(model_name: str, app_label: str, **model_fields: Any) -> type[Model]:
    attributes: dict[str, Any] = {"id": fields.IntField(primary_key=True), **model_fields}
    attributes["Meta"] = type("Meta", (), {"app": app_label})
    return type(model_name, (Model,), attributes)


def make_create(model_name: str, **model_fields: Any) -> CreateModel:
    return CreateModel(model_name, [("id", fields.IntField(primary_key=True)), *model_fields.items()])


def get_summary(migrations: list[Any]) -> list[tuple[str, list[str], list[int]]]:
    return [
        (
            migration.app_label,
            [operation.describe() for operation in migration.operations],
            sorted(migration.split_dependencies),
        )
        for migration in migrations
    ]


def test_a_relation_closing_a_cycle_between_apps_goes_into_a_next_migration():
    operations_by_app = {
        "owner": [make_create("Owner", shop=fields.ForeignKeyField("shop.Shop", related_name="owners", null=True))],
        "shop": [make_create("Shop", owner=fields.ForeignKeyField("owner.Owner", related_name="shops", null=True))],
    }

    migrations = MigrationSplitter(State(), ["owner", "shop"], get_leaf_migrations).split(operations_by_app)

    assert get_summary(migrations) == [
        ("owner", ["Create model Owner"], []),
        ("shop", ["Create model Shop"], [0]),
        ("owner", ["Add field shop to Owner"], [0, 1]),
    ]


def test_a_model_of_another_app_no_new_operation_creates_is_needed_from_its_existing_migrations():
    state = make_state(owner=[make_model("Owner", "owner")])
    operations_by_app = {
        "shop": [make_create("Shop", owner=fields.ForeignKeyField("owner.Owner", related_name="shops"))]
    }

    (migration,) = MigrationSplitter(state, ["owner", "shop"], get_leaf_migrations).split(operations_by_app)

    assert migration.existing_dependencies == {("owner", "0001_initial")}
    assert migration.split_dependencies == set()


def test_a_deletion_waits_for_the_other_apps_migration_dropping_its_reference():
    author = make_model("Author", "blog")
    book = make_model("Book", "shop", author=fields.ForeignKeyField("blog.Author", related_name="books"))
    state = make_state(blog=[author], shop=[book])
    operations_by_app = {"blog": [DeleteModel("Author")], "shop": [DeleteModel("Book")]}

    migrations = MigrationSplitter(state, ["blog", "shop"], get_leaf_migrations).split(operations_by_app)

    assert get_summary(migrations) == [("shop", ["Delete model Book"], []), ("blog", ["Delete model Author"], [0])]


def test_a_deletion_another_app_still_references_is_refused_with_the_operations_message():
    author = make_model("Author", "blog")
    book = make_model("Book", "shop", author=fields.ForeignKeyField("blog.Author", related_name="books"))
    state = make_state(blog=[author], shop=[book])

    with pytest.raises(
        ConfigurationError, match=r"Can't delete model blog\.Author: shop\.book\.author still references it"
    ):
        MigrationSplitter(state, ["blog", "shop"], get_leaf_migrations).split({"blog": [DeleteModel("Author")]})
