"""OperationSorter: operations run after what they need, keep their order otherwise, and a cycle among
created models is broken by taking a relation out into an AddField - a many-to-many one first - and one among
deleted models by taking a relation out into a RemoveField before its model's deletion."""

from __future__ import annotations

from typing import Any

import pytest

from hare import fields
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.declarations.declaration_values import DeclarationValues
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_definitions import MigrationDefinitions
from hare.migrations.migration import Migration
from hare.migrations.operations import AddField, CreateModel, DeleteModel, RemoveField
from hare.migrations.operations.enums import DependencyType
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models import Model
from hare.query.expressions import F, Q
from tests.migrations.autodetection.models import make_model

pytestmark = pytest.mark.database_independent


def build_state(*models: type[Model]) -> State:
    state = State()
    for model in models:
        state.models[("shop", model.__name__)] = ModelState.make_from_declaration("shop", model)
    return state


def describe(operations: list[Any]) -> list[str]:
    return [operation.describe() for operation in operations]


@pytest.mark.parametrize("same_connection", [False, True])
@pytest.mark.parametrize("override_connections", [False, True])
@pytest.mark.parametrize("same_table", [False, True])
@pytest.mark.parametrize("same_schema", [False, True])
def test_table_replacement_dependencies_use_the_effective_database_namespace(
    same_connection, override_connections, same_table, same_schema
):
    connections = {"alpha": "left", "beta": "left" if same_connection else "right"}
    captured_connections = (
        {"alpha": "left", "beta": "right" if same_connection else "left"}
        if override_connections
        else connections.copy()
    )
    state = State(default_connections=captured_connections.copy())
    CreateModel("First", [("id", fields.IntField(primary_key=True))], options={"table": "shared"}).state_forward(
        "alpha", state
    )
    operation = CreateModel(
        "Second",
        [("id", fields.IntField(primary_key=True))],
        options={"table": "shared" if same_table else "separate", "schema": None if same_schema else "other"},
    )
    sorter = OperationSorter(state, connections if override_connections else None)
    requirements = sorter.get_required("beta", operation)
    removed = [
        item for item in requirements if item.app_label == "alpha" and item.dependency_type is DependencyType.REMOVED
    ]
    assert bool(removed) is (same_connection and same_table and same_schema)
    assert state.default_connections == captured_connections
    entries = [("beta", operation), ("alpha", DeleteModel("First"))]
    assert [app for app, _ in sorter.sort(entries)] == (["alpha", "beta"] if removed else ["beta", "alpha"])


def test_operations_keep_their_order_when_nothing_needs_another():
    operations = [
        CreateModel("Zebra", [("id", fields.IntField(primary_key=True))]),
        CreateModel("Apple", [("id", fields.IntField(primary_key=True))]),
    ]
    sorted_entries = OperationSorter(State()).sort([("shop", operation) for operation in operations])
    assert DeclarationValues.get([operation for _app_label, operation in sorted_entries]) == DeclarationValues.get(
        operations
    )


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
    assert DeclarationValues.get([operation for _app_label, operation in sorted_entries]) == DeclarationValues.get(
        [author, book]
    )


def test_cycle_splitting_preserves_input_declarations_and_repeats_deterministically():
    entries = [
        (
            "shop",
            CreateModel(
                "Author",
                [
                    ("id", fields.IntField(primary_key=True)),
                    ("book", fields.ForeignKeyField("shop.Book", related_name="authors")),
                ],
            ),
        ),
        (
            "shop",
            CreateModel(
                "Book",
                [
                    ("id", fields.IntField(primary_key=True)),
                    ("author", fields.ForeignKeyField("shop.Author", related_name="books")),
                ],
            ),
        ),
    ]
    original = DeclarationValues.get(entries)
    sorter = OperationSorter(State())
    first = sorter.sort(entries)
    second = sorter.sort(entries)
    assert len(first) == 3
    assert DeclarationValues.get(first) == DeclarationValues.get(second)
    assert DeclarationValues.get(entries) == original
    assert all(len(operation.fields) == 2 for _, operation in entries)


@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
@pytest.mark.parametrize("declaration", ["index", "unique", "check", "generated", "generated-index"])
def test_cycle_splitting_moves_every_structured_dependent_after_its_input(composite, declaration):
    to_field = ("region", "id") if composite else "id"
    input_name = "second_region" if composite else "second_id"
    first_fields = [
        ("id", fields.IntField(primary_key=True)),
        ("second", fields.ForeignKeyField("beta.Second", to_field=to_field, null=True)),
    ]
    options = {}
    if declaration == "index":
        options["indexes"] = (Index(F(input_name) + 1, name="second_key"),)
    elif declaration == "unique":
        options["constraints"] = (UniqueConstraint(fields=("second",), name="second_unique"),)
    elif declaration == "check":
        options["constraints"] = (CheckConstraint(check=Q(**{f"{input_name}__gte": 0}), name="second_positive"),)
    else:
        first_fields.append(("copied", fields.GeneratedField(F(input_name) + 1, output_field=fields.IntField())))
        if declaration == "generated-index":
            options["indexes"] = (Index(fields=("copied",), name="copied_key"),)
    second_fields = [("id", fields.IntField(primary_key=not composite))]
    if composite:
        second_fields.append(("region", fields.IntField()))
    second_fields.append(("first", fields.ForeignKeyField("alpha.First", null=True)))
    second_options = {"primary_key_attribute": ("region", "id")} if composite else {}
    if declaration == "index":
        second_options["indexes"] = (Index(F("first_id") + 1, name="first_key"),)
    elif declaration == "unique":
        second_options["constraints"] = (UniqueConstraint(fields=("first",), name="first_unique"),)
    elif declaration == "check":
        second_options["constraints"] = (CheckConstraint(check=Q(first_id__gte=0), name="first_positive"),)
    else:
        second_fields.append(("copied", fields.GeneratedField(F("first_id") + 1, output_field=fields.IntField())))
        if declaration == "generated-index":
            second_options["indexes"] = (Index(fields=("copied",), name="first_copied_key"),)
    entries = [
        ("alpha", CreateModel("First", first_fields, options)),
        ("beta", CreateModel("Second", second_fields, second_options)),
    ]
    original = DeclarationValues.get(entries)
    operations = OperationSorter(State()).sort(entries)
    first_creation = next(
        operation for app, operation in operations if app == "alpha" and isinstance(operation, CreateModel)
    )
    assert [name for name, _field in first_creation.fields] == ["id"]
    assert not first_creation.options.get("indexes")
    assert not first_creation.options.get("constraints")
    relation_position = next(
        position
        for position, (app, operation) in enumerate(operations)
        if app == "alpha" and isinstance(operation, AddField) and operation.name == "second"
    )
    if declaration.startswith("generated"):
        copied_position = next(
            position
            for position, (app, operation) in enumerate(operations)
            if app == "alpha" and isinstance(operation, AddField) and operation.name == "copied"
        )
        assert copied_position > relation_position
    else:
        copied_position = relation_position
    for position, (app, operation) in enumerate(operations):
        if app == "alpha" and hasattr(operation, "schema_object"):
            assert position > copied_position
    assert DeclarationValues.get(entries) == original
    state = State()
    for app, operation in entries:
        operation.state_forward(app, state)
    state.apps.get_model("alpha.First")
    deletions = OperationSorter(state).sort([("alpha", DeleteModel("First")), ("beta", DeleteModel("Second"))])
    for app, operation in deletions:
        if isinstance(operation, RemoveField) and operation.name in ("first", "second"):
            remaining = state.models[(app, operation.model_name)]
            if declaration.startswith("generated"):
                assert "copied" not in remaining.fields
        operation.state_forward(app, state)
    assert not state.models


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
    state = State()
    removal = RemoveField("Book", "author")
    deletion = DeleteModel("Author")
    removal.generation_dependencies = tuple(deletion.get_provided_dependencies("shop", state))
    deletion.generation_dependencies = tuple(removal.get_provided_dependencies("shop", state))
    with pytest.raises(ConfigurationError, match="need each other in a cycle"):
        OperationSorter(State()).sort([("shop", removal), ("shop", deletion)])


@pytest.mark.parametrize("composite", [False, True], ids=["scalar-key", "composite-key"])
@pytest.mark.parametrize("parallel", [fields.ManyToManyField, fields.ForeignKeyField, fields.OneToOneField])
@pytest.mark.asyncio
async def test_deleting_parallel_relations_breaks_each_unmet_dependency_once(monkeypatch, composite, parallel):
    def key_fields():
        result = {"id": fields.IntField(primary_key=not composite, generated=False)}
        if composite:
            result.update(region=fields.IntField(), pk=fields.CompositePrimaryKey("region", "id"))
        return result

    first = make_model(
        "First",
        app_label="alpha",
        **key_fields(),
        second=fields.ForeignKeyField("beta.Second", related_name="firsts", null=True),
        backup=parallel("beta.Second", related_name="backups"),
    )
    second = make_model(
        "Second",
        app_label="beta",
        **key_fields(),
        first=fields.ForeignKeyField("alpha.First", related_name="seconds", null=True),
    )
    connections = {"alpha": "main", "beta": "main"}
    state = State.from_models([first, second], default_connections=connections)
    original = OperationSorter.get_cycle_broken

    def break_cycle(sorter, entries, waiting):
        result = original(sorter, entries, waiting)
        removals = [
            (app, operation.model_name, operation.name)
            for app, operation in result
            if isinstance(operation, RemoveField)
        ]
        # Stop a repeated native choice deterministically instead of hanging the test suite.
        assert len(removals) == len(set(removals)), "cycle breaking repeated an existing field removal"
        return result

    monkeypatch.setattr(OperationSorter, "get_cycle_broken", break_cycle)
    apps_config = {app: {"default_connection": "main", "migrations": f"unused_{app}"} for app in connections}
    empty_definitions = MigrationDefinitions({}, frozenset(connections), frozenset())
    initial = await MigrationAutodetector(state.apps, apps_config, definitions=empty_definitions).changes()
    initial_migrations = {
        MigrationKey(writer.app_label, writer.name): Migration.from_source(
            writer.as_string(), name=writer.name, app_label=writer.app_label
        )
        for writer in initial
    }
    definitions = MigrationDefinitions(initial_migrations, frozenset(connections), frozenset())
    empty = State(default_connections=connections, environment=state.environment)
    deletions = await MigrationAutodetector(empty.apps, apps_config, definitions=definitions).changes()
    entries = [(writer.app_label, operation) for writer in deletions for operation in writer.operations]
    assert not OperationSorter(state).get_order(entries)[1]
    assert sorted((app, op.name) for app, op in entries if isinstance(op, DeleteModel)) == [
        ("alpha", "First"),
        ("beta", "Second"),
    ]


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
