"""OperationSorter: operations run after what they need, keep their order otherwise, and a cycle among
created models is broken by taking a relation out into an AddField - a many-to-many one first - and one among
deleted models by taking a relation out into a RemoveField before its model's deletion."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.operations import AddField, CreateModel, DeleteModel, RemoveField
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model

pytestmark = pytest.mark.database_independent


def make_model(model_name: str, table: str | None = None, **model_fields: Any) -> type[Model]:
    attributes: dict[str, Any] = {"id": fields.IntField(primary_key=True), **model_fields}
    meta_options = {"app": "shop"} if table is None else {"app": "shop", "table": table}
    attributes["Meta"] = type("Meta", (), meta_options)
    return type(model_name, (Model,), attributes)


def build_state(*models: type[Model]) -> State:
    state = State()
    for model in models:
        state.models[("shop", model.__name__)] = ModelState.make_from_model("shop", model)
    return state


def describe(operations: list[Any]) -> list[str]:
    return [operation.describe() for operation in operations]


def test_operations_keep_their_order_when_nothing_needs_another():
    operations = [
        CreateModel("Zebra", [("id", fields.IntField(primary_key=True))]),
        CreateModel("Apple", [("id", fields.IntField(primary_key=True))]),
    ]
    sorted_entries = OperationSorter(State()).sort([("shop", operation) for operation in operations])
    assert [operation for _app_label, operation in sorted_entries] == operations


def test_an_operation_runs_after_the_model_its_relation_needs():
    book = CreateModel(
        "Book",
        [
            ("id", fields.IntField(primary_key=True)),
            ("author", fields.ForeignKeyField("shop.Author", related_name="books")),
        ],
    )
    author = CreateModel("Author", [("id", fields.IntField(primary_key=True))])
    sorted_entries = OperationSorter(State()).sort([("shop", book), ("shop", author)])
    assert [operation for _app_label, operation in sorted_entries] == [author, book]


@pytest.mark.parametrize("declared_first", ["Author", "Book"])
def test_the_operations_do_not_depend_on_the_order_the_models_are_declared_in(declared_first: str):
    author = make_model("Author", editor=fields.ForeignKeyField("shop.Book", related_name="edited", null=True))
    book = make_model("Book", author=fields.ForeignKeyField("shop.Author", related_name="books"))
    models = (author, book) if declared_first == "Author" else (book, author)

    operations = OperationGenerator(State(), build_state(*models)).generate()

    assert describe(operations) == ["Create model Author", "Create model Book", "Add field editor to Author"]


def test_a_cycle_is_broken_by_a_many_to_many_relation_before_a_foreign_key():
    zebra = make_model("Zebra", tags=fields.ManyToManyField("shop.Tag", related_name="zebras", through="shop.Alink"))
    alink = make_model(
        "Alink",
        zebra=fields.ForeignKeyField("shop.Zebra", related_name="links"),
        tag=fields.ForeignKeyField("shop.Tag", related_name="links"),
    )
    tag = make_model("Tag")

    operations = OperationGenerator(State(), build_state(zebra, alink, tag)).generate()

    assert describe(operations)[-1] == "Add field tags to Zebra"
    created = next(
        operation for operation in operations if isinstance(operation, CreateModel) and operation.name == "Alink"
    )
    assert [name for name, _field in created.fields] == ["id", "zebra", "tag"]


def test_a_model_deleted_with_the_through_model_of_its_relation_loses_the_relation_first():
    """The through model's deletion waited for the relation going through it, removed only with its model,
    which waited for the through model's foreign key to it - a cycle, and makemigrations refused the change."""
    zebra = make_model("Zebra", tags=fields.ManyToManyField("shop.Tag", related_name="zebras", through="shop.Alink"))
    alink = make_model(
        "Alink",
        zebra=fields.ForeignKeyField("shop.Zebra", related_name="links"),
        tag=fields.ForeignKeyField("shop.Tag", related_name="links"),
    )
    tag = make_model("Tag")

    operations = OperationGenerator(build_state(zebra, alink, tag), build_state(make_model("Tag"))).generate()

    assert describe(operations) == ["Remove field tags from Zebra", "Delete model Alink", "Delete model Zebra"]


def test_models_deleted_together_pointing_at_each_other_lose_a_foreign_key_first():
    author = make_model("Author", favorite=fields.ForeignKeyField("shop.Book", related_name="fans", null=True))
    book = make_model("Book", author=fields.ForeignKeyField("shop.Author", related_name="books"))

    operations = OperationGenerator(build_state(author, book), State()).generate()

    assert describe(operations) == ["Remove field favorite from Author", "Delete model Book", "Delete model Author"]


def test_a_cycle_no_relation_breaks_is_refused():
    removal = RemoveField("Book", "author")
    deletion = DeleteModel("Author")
    removal.generation_dependencies = tuple(deletion.get_provided_dependencies("shop"))
    deletion.generation_dependencies = tuple(removal.get_provided_dependencies("shop"))
    with pytest.raises(ConfigurationError, match="need each other in a cycle"):
        OperationSorter(State()).sort([("shop", removal), ("shop", deletion)])


def test_a_model_takes_the_table_of_a_deleted_one_after_it_is_deleted():
    old_state = build_state(make_model("Item", table="goods", name=fields.CharField(max_length=10)))
    new_state = build_state(make_model("Thing", table="goods", label=fields.IntField()))

    operations = OperationGenerator(old_state, new_state).generate()

    assert describe(operations) == ["Delete model Item", "Create model Thing"]


def test_an_added_relation_waits_for_the_field_its_to_field_names():
    old_state = build_state(make_model("Account"))
    new_state = build_state(
        make_model(
            "Account",
            code=fields.CharField(max_length=10, unique=True, null=True),
            alias_of=fields.ForeignKeyField("shop.Account", related_name="aliases", null=True, to_field="code"),
        )
    )

    operations = OperationGenerator(old_state, new_state).generate()

    assert [type(operation) for operation in operations] == [AddField, AddField]
    assert [operation.name for operation in operations] == ["code", "alias_of"]
