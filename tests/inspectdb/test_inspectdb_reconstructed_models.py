"""inspectdb reconstructions that must yield a model which really works against its own table -
both as exec'd ModelSourceGenerator source and as a ModelFactory.from_table_info() class, each
registered with Hare.register_live_model() and exercised with real CRUD."""

import contextlib
from collections.abc import Generator
from decimal import Decimal
from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.test import requires_features
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.postgresql.postgresql_introspector import PostgresqlIntrospector
from hare.exceptions import ConfigurationError, IntegrityError
from hare.fields.db_defaults import SqlDefault
from hare.fields.relations.fields import ForeignKeyFieldInstance, OneToOneFieldInstance
from hare.inspectdb import ModelFactory, SchemaInspector
from hare.inspectdb.enums import CatalogDiagnosticCode
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations import Migration, MigrationRunner
from hare.migrations.operations import CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from tests.utils.model_source_generator import ModelSourceGenerator

BUILD_MODES = ["source", "factory"]


@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize("layout", ["collision", "reserved", "relation-shadow"])
async def test_reconstructed_foreign_keys_use_the_allocated_target_storage_attribute(connection, build_mode, layout):
    declaration_graph = ModelBindingGraph()
    column = "save" if layout == "reserved" else "code_key"
    extra = ', "code-key" INTEGER NOT NULL UNIQUE' if layout == "collision" else ""
    reference = " REFERENCES allocated_root(id)" if layout == "relation-shadow" else ""
    scripts = [
        "CREATE TABLE allocated_root (id INTEGER PRIMARY KEY)",
        f"CREATE TABLE allocated_parent (id INTEGER PRIMARY KEY{extra}, "
        f'"{column}" INTEGER NOT NULL UNIQUE{reference})',
        "CREATE TABLE allocated_child (id INTEGER PRIMARY KEY, "
        f'parent_id INTEGER REFERENCES allocated_parent("{column}"))',
        "INSERT INTO allocated_root VALUES (21),(31)",
        "INSERT INTO allocated_parent VALUES (1,11,21),(2,21,31)"
        if layout == "collision"
        else "INSERT INTO allocated_parent VALUES (1,21),(2,31)",
        "INSERT INTO allocated_child VALUES (3,21)",
    ]
    await recreate_tables(connection, ["allocated_child", "allocated_parent", "allocated_root"], scripts)
    models = await build_models(connection, ["allocated_root", "allocated_parent", "allocated_child"], build_mode)
    parent_class, child_class = models["allocated_parent"], models["allocated_child"]
    with registered_live_models(models):
        child = await child_class.objects.select_related("parent").get(id=3)
        assert child.parent.pk == 1
        target_attribute = (
            HareContext.require_current().apps.get_binding(parent_class).fields_db_projection_reverse[column]
        )
        assert (
            HareContext.require_current().apps.get_binding(child_class).fields_map["parent"].to_field
            == target_attribute
        )
        assert (await parent_class.objects.get(pk=1)).pk == 1
    module_source = await SchemaInspector.inspect(connection, tables=["allocated_child"])
    namespace: dict[str, Any] = {"__name__": "tests.generated_single_table"}
    exec(compile(module_source, "<generated>", "exec"), namespace)  # noqa: S102
    assert "AllocatedParent" not in namespace
    assert declaration_graph.include(namespace["AllocatedChild"]).fields_map["parent"].to_field == target_attribute


@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
async def test_reconstructed_reverse_names_share_the_target_attribute_namespace(connection, build_mode):
    await recreate_tables(
        connection,
        ['"child rows"', '"child__rows"', "reverse_target"],
        [
            "CREATE TABLE reverse_target (id INTEGER PRIMARY KEY, child_rows_parent_set INTEGER)",
            'CREATE TABLE "child rows" (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES reverse_target(id))',
            'CREATE TABLE "child__rows" (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES reverse_target(id))',
            "INSERT INTO reverse_target VALUES (1,99)",
            'INSERT INTO "child rows" VALUES (2,1)',
            'INSERT INTO "child__rows" VALUES (3,1)',
        ],
    )
    models = await build_models(connection, ["reverse_target", "child rows", "child__rows"], build_mode)
    with registered_live_models(models):
        parent = await models["reverse_target"].objects.get(pk=1)
        assert parent.child_rows_parent_set == 99
        reverse_names = [
            HareContext.require_current().apps.get_binding(models[table]).fields_map["parent"].related_name
            for table in ("child rows", "child__rows")
        ]
        assert len(set(reverse_names)) == 2
        assert "child_rows_parent_set" not in reverse_names
        for expected, name in zip((2, 3), reverse_names, strict=True):
            assert [child.pk for child in await getattr(parent, name)] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize("composite", (False, True))
async def test_a_reconstructed_class_named_model_keeps_its_declared_primary_key(connection, build_mode, composite):
    declaration_graph = ModelBindingGraph()
    columns = (
        "region BIGINT NOT NULL, natural_key BIGINT NOT NULL, amount BIGINT NOT NULL, PRIMARY KEY(region, natural_key)"
        if composite
        else "natural_key BIGINT PRIMARY KEY NOT NULL, amount BIGINT NOT NULL"
    )
    await recreate_tables(connection, ['"model"'], [f'CREATE TABLE "model" ({columns})'])
    values = "3,7,42" if composite else "7,42"
    await connection.execute_script(f'INSERT INTO "model" VALUES ({values})')
    models = await build_models(connection, ["model"], build_mode)
    model = models["model"]
    expected_key = (3, 7) if composite else 7
    assert model.__name__ == "Model"
    assert declaration_graph.include(model).primary_key_attribute == (
        ("region", "natural_key") if composite else "natural_key"
    )
    with registered_live_models(models):
        obj = await model.objects.get(pk=expected_key)
        assert (obj.pk, obj.amount) == (expected_key, 42)
        obj.amount = 43
        await obj.save()
        assert (await model.objects.get(pk=expected_key)).amount == 43


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize(
    ("default_sql", "expected"),
    [
        ("0.123456789012345678", Decimal("0.123456789012345678")),
        ("-0.123456789012345678", Decimal("-0.123456789012345678")),
        ("123.456::numeric(8,2)", Decimal("123.46")),
    ],
)
async def test_reconstructed_numeric_defaults_replay_without_float_rounding(
    connection, build_mode, default_sql, expected
):
    source_table = "numeric_default_source"
    target_table = "numeric_default_replayed"
    await recreate_tables(
        connection,
        [target_table, source_table],
        [f'CREATE TABLE {source_table} ("Value Column" numeric(30,18) DEFAULT {default_sql})'],
    )
    await connection.execute_script(f"INSERT INTO {source_table} DEFAULT VALUES")
    assert await connection.execute_dicts(f'SELECT "Value Column" AS value FROM {source_table}') == [
        {"value": expected}
    ]
    models = await build_models(connection, [source_table], build_mode)
    with registered_live_models(models):
        snapshot = State.from_models(models.values(), default_connections={"live": connection.connection_alias})
        declaration = next(iter(snapshot.models.values()))
        migration = Migration(
            "replay_numeric_default",
            "live",
            operations=[
                CreateModel(
                    declaration.name,
                    list(declaration.fields.items()),
                    options={**declaration.options, "table": target_table, "managed": True},
                )
            ],
        )
        await MigrationRunner(connection).apply(
            migration, State(default_connections={"live": connection.connection_alias})
        )
    await connection.execute_script(f"INSERT INTO {target_table} DEFAULT VALUES")
    assert await connection.execute_dicts(f'SELECT "Value Column" AS value FROM {target_table}') == [
        {"value": expected}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize(
    "key_columns",
    [
        ("owner_region", "owner_code"),
        ("owner_code", "owner_region"),
        ("owner_region", "owner_code", "number"),
        ("owner_code", "owner_region", "number"),
        ("owner_region", "number", "owner_code"),
        ("owner_code", "number", "owner_region"),
    ],
)
async def test_reconstruction_preserves_primary_key_order_across_relation_columns(connection, build_mode, key_columns):
    await recreate_tables(
        connection,
        ["key_order_item", "key_order_target"],
        [
            "CREATE TABLE key_order_target (region INT NOT NULL, code INT NOT NULL, PRIMARY KEY(region, code))",
            "CREATE TABLE key_order_item (owner_region INT NOT NULL, number INT NOT NULL, owner_code INT NOT NULL, "
            f"label TEXT NOT NULL, PRIMARY KEY({', '.join(key_columns)}), "
            "FOREIGN KEY(owner_region, owner_code) REFERENCES key_order_target(region, code))",
        ],
    )
    models = await build_models(connection, ["key_order_target", "key_order_item"], build_mode)
    with registered_live_models(models):
        target, item = models.values()
        owner = await target.objects.create(region=10, code=20)
        created = await item.objects.create(owner=owner, number=30, label="first")
        values = {"owner_region": 10, "owner_code": 20, "number": 30}
        expected = tuple(values[name] for name in key_columns)
        assert HareContext.require_current().apps.get_binding(item).primary_key_attribute == key_columns
        assert created.pk == expected
        snapshot = State.from_models(models.values(), default_connections={"live": connection.connection_alias})
        historical_apps = snapshot.clone().apps
        historical = historical_apps.get_model(f"live.{item.__name__}")
        assert snapshot.clone().apps.get_binding(f"live.{item.__name__}").primary_key_attribute == key_columns
        assert (await historical_apps.get_queryset(historical).get(pk=expected)).pk == expected
        row = await item.objects.get(pk=expected)
        assert row.pk == expected
        row.label = "updated"
        await row.save(update_fields=["label"])
        assert await item.objects.filter(pk=expected).values_list("label", flat=True) == ["updated"]
        assert await item.objects.filter(pk=expected).delete() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize("prefix", ["refresh_from_db", "owner_", "team__member", "owner"])
async def test_reconstruction_keeps_columns_when_a_composite_relation_name_is_unusable(connection, build_mode, prefix):
    target = "prefix_target"
    child = "prefix_child"
    region, code = f"{prefix}_region", f"{prefix}_code"
    await recreate_tables(
        connection,
        [child, target],
        [
            f"CREATE TABLE {target} (region INT NOT NULL, code INT NOT NULL, PRIMARY KEY(region, code))",
            f"CREATE TABLE {child} (number INT PRIMARY KEY, {region} INT NOT NULL, {code} INT NOT NULL, "
            f"UNIQUE({region}, {code}), FOREIGN KEY({region}, {code}) REFERENCES {target}(region, code))",
        ],
    )
    await connection.execute(f"INSERT INTO {target} VALUES (10, 20), (10, 21)")
    await connection.execute(f"INSERT INTO {child} VALUES (30, 10, 20)")

    table_info = await DatabaseCatalog.inspect_table(connection, child)
    assert bool(table_info.composite_foreign_keys) is (prefix == "owner")
    assert any(diagnostic.code == CatalogDiagnosticCode.FOREIGN_KEY for diagnostic in table_info.diagnostics) is (
        prefix != "owner"
    )
    models = await build_models(connection, [target, child], build_mode)
    with registered_live_models(models):
        item = models[child]
        binding = HareContext.require_current().apps.get_binding(item)
        row = await item.objects.get(pk=30)
        key_attributes = tuple(binding.fields_db_projection_reverse[name] for name in (region, code))
        assert tuple(getattr(row, name) for name in key_attributes) == (10, 20)
        await connection.execute(f"UPDATE {child} SET {code} = 21 WHERE number = 30")
        await row.refresh_from_db()
        assert tuple(getattr(row, name) for name in key_attributes) == (10, 21)
        if prefix == "owner":
            loaded = await item.objects.select_related("owner").get(pk=30)
            assert loaded.owner.pk == (10, 21)


async def recreate_tables(connection, drop_order: list[str], create_statements: list[str]) -> None:
    """SQLite's executescript() commits the surrounding test transaction - tables are dropped
    first so every test starts from empty ones."""
    for table_name in drop_order:
        await connection.execute_script(f"DROP TABLE IF EXISTS {table_name}")
    for statement in create_statements:
        await connection.execute_script(statement)


async def build_models(connection, table_names: list[str], build_mode: str) -> dict[str, type[Model]]:
    dialect = connection.dialect.name
    models: dict[str, type[Model]] = {}
    tables = tuple([await DatabaseCatalog.inspect_table(connection, table_name) for table_name in table_names])
    for table_info in tables:
        table_name = table_info.name
        if build_mode == "factory":
            models[table_name] = ModelFactory.from_table_info(
                table_info, dialect, app_label="live", target_tables=tables
            )
            continue
        source = ModelSourceGenerator.generate_model_source(
            table_info, dialect, app_label="live", target_tables=tables
        )
        namespace: dict[str, Any] = {"__name__": "tests.generated_reconstructed_module"}
        exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
        models[table_name] = namespace[ModelNaming.get_class_name(table_name)]
    return models


@contextlib.contextmanager
def registered_live_models(models: dict[str, type[Model]]) -> Generator[None]:
    """Registers the models in the given order (targets first), unregisters them in reverse."""
    alias = next(iter(Connections.current().db_config))
    registered: list[type[Model]] = []
    try:
        for model in models.values():
            Hare.register_live_models([model], app_label="live", connection_alias=alias, managed=False)
            registered.append(model)
        yield
    finally:
        for model in reversed(registered):
            Hare.unregister_live_models([model])


async def generated_source(connection, table_name: str) -> str:
    table_info = await DatabaseCatalog.inspect_table(connection, table_name)
    return ModelSourceGenerator.generate_model_source(table_info, connection.dialect.name)


# ---------------------------------------------------------------------------------------------
# FK column without an "_id" suffix, several FKs to one target


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.parametrize("table_name", ["child rows", "9child", "clas", "child__rows", "дочерняя"])
@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.asyncio
async def test_reconstructed_reverse_relations_have_usable_attribute_names(connection, build_mode, table_name, unique):
    quote = connection.dialect.literals.quote_identifier
    child_table = quote(table_name)
    await recreate_tables(
        connection,
        [child_table, "reverse_name_parent"],
        [
            "CREATE TABLE reverse_name_parent (id INTEGER PRIMARY KEY)",
            f"CREATE TABLE {child_table} (id INTEGER PRIMARY KEY, "
            f"parent_id INTEGER NOT NULL {'UNIQUE ' if unique else ''}REFERENCES reverse_name_parent(id))",
        ],
    )
    await connection.execute_script(
        f"INSERT INTO reverse_name_parent VALUES (1); INSERT INTO {child_table} VALUES (2, 1)"
    )
    models = await build_models(connection, ["reverse_name_parent", table_name], build_mode)
    parent_class, child_class = models.values()
    expected_name = (
        "дочерняяs"
        if table_name == "дочерняя"
        else {
            "child rows": "child_rows_parent_set",
            "9child": "field_9child_parent_set",
            "clas": "clas_parent_set",
            "child__rows": "child_rows_parent_set",
        }[table_name]
    )
    with registered_live_models(models):
        child = await child_class.objects.select_related("parent").get(id=2)
        parent = child.parent
        assert parent.pk == 1
        assert isinstance(parent, parent_class)
        backward = await getattr(parent, expected_name)
        assert (backward.pk if unique else [obj.pk for obj in backward]) == (2 if unique else [2])
        prefetched = await parent_class.objects.prefetch_related(expected_name)
        assert [obj.pk for obj in prefetched] == [1]
        assert await connection.execute_dicts(f"SELECT id, parent_id FROM {child_table}") == [
            {"id": 2, "parent_id": 1}
        ]


async def create_owner_and_child_tables(connection) -> None:
    await recreate_tables(
        connection,
        ["ix_child", "ix_owner"],
        [
            "CREATE TABLE ix_owner (id INTEGER PRIMARY KEY, code VARCHAR(10) NOT NULL UNIQUE)",
            "CREATE TABLE ix_child (id INTEGER PRIMARY KEY, "
            "owner_id INTEGER NOT NULL REFERENCES ix_owner (id), "
            "owner_code VARCHAR(10) NULL REFERENCES ix_owner (code), "
            "left_id INTEGER NULL REFERENCES ix_child (id), "
            "right_id INTEGER NULL REFERENCES ix_child (id))",
        ],
    )


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_fk_column_without_id_suffix_and_repeated_targets_work(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await create_owner_and_child_tables(connection)
    models = await build_models(connection, ["ix_owner", "ix_child"], build_mode)
    owner_class, child_class = models["ix_owner"], models["ix_child"]

    owner_code_field = declaration_graph.include(child_class).fields_map["owner_code_fk"]
    assert owner_code_field.source_field == "owner_code"
    assert {
        name: declaration_graph.include(child_class).fields_map[name].related_name
        for name in ("owner", "owner_code_fk", "left", "right")
    } == {
        "owner": "ix_child_owner_set",
        "owner_code_fk": "ix_child_owner_code_fk_set",
        "left": "ix_child_left_set",
        "right": "ix_child_right_set",
    }

    with registered_live_models(models):
        owner = await owner_class.objects.create(id=1, code="c1")
        first = await child_class.objects.create(id=1, owner=owner, owner_code_fk=owner)
        second = await child_class.objects.create(id=2, owner=owner, owner_code_fk=owner, left=first, right=first)

        rows = await connection.execute_dicts("SELECT owner_code, left_id FROM ix_child WHERE id = 2")
        assert rows == [{"owner_code": "c1", "left_id": 1}]
        fetched = await child_class.objects.get(id=2).select_related("owner_code_fk")
        assert fetched.owner_code_fk.code == "c1"
        assert fetched.left_id == 1
        assert sorted(child.id for child in await owner.ix_child_owner_code_fk_set) == [1, 2]
        assert [child.id for child in await first.ix_child_left_set] == [2]
        assert await child_class.objects.filter(owner_code_fk__code="c1").count() == 2

        await child_class.objects.filter(id=2).update(owner_code_fk=None, left=None)
        cleared = await child_class.objects.get(id=2)
        assert (cleared.owner_code_fk_id, cleared.left_id) == (None, None)
        await second.delete()
        await first.delete()
        assert await child_class.objects.all().count() == 0


# ---------------------------------------------------------------------------------------------
# FK column that is also the primary key


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_fk_primary_key_column_becomes_working_one_to_one_field(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_profile", "ix_account"],
        [
            "CREATE TABLE ix_account (id INTEGER PRIMARY KEY)",
            "CREATE TABLE ix_profile (account_id INTEGER PRIMARY KEY REFERENCES ix_account (id), "
            "bio VARCHAR(20) NULL)",
        ],
    )
    source = await generated_source(connection, "ix_profile")
    assert "fields.OneToOneField('models.IxAccount', on_delete=OnDelete.NO_ACTION, primary_key=True)" in source
    models = await build_models(connection, ["ix_account", "ix_profile"], build_mode)
    account_class, profile_class = models["ix_account"], models["ix_profile"]
    assert isinstance(declaration_graph.include(profile_class).fields_map["account"], OneToOneFieldInstance)
    assert declaration_graph.include(profile_class).primary_key_attribute == "account"

    with registered_live_models(models):
        account = await account_class.objects.create(id=7)
        await profile_class.objects.create(account=account, bio="hi")
        fetched = await profile_class.objects.get(pk=7)
        assert fetched.bio == "hi"
        assert (await profile_class.objects.get(account_id=7)).bio == "hi"
        fetched.bio = "bye"
        await fetched.save()
        assert (await profile_class.objects.get(pk=7)).bio == "bye"
        await fetched.delete()
        assert await profile_class.objects.all().count() == 0


@pytest.mark.database_independent
def test_foreign_key_field_primary_key_is_rejected():
    with pytest.raises(ConfigurationError, match="OneToOneField"):
        fields.ForeignKeyField("models.Tournament", primary_key=True)
    with pytest.raises(ConfigurationError, match="OneToOneField"):
        ForeignKeyFieldInstance("models.Tournament", primary_key=True)
    assert fields.OneToOneField("models.Tournament", primary_key=True).pk


# ---------------------------------------------------------------------------------------------
# Indexed / UNIQUE TEXT columns


@requires_features(supports_unique_constraints=True)
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_indexed_and_unique_text_columns_keep_their_indexes(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_text"],
        [
            "CREATE TABLE ix_text (id INTEGER PRIMARY KEY, note TEXT NULL, body TEXT NULL UNIQUE, "
            "a INTEGER NOT NULL, tag TEXT NOT NULL, UNIQUE (a, tag))",
            "CREATE INDEX ix_text_note_idx ON ix_text (note)",
        ],
    )
    models = await build_models(connection, ["ix_text"], build_mode)
    text_class = models["ix_text"]
    # A named index is kept under its name; one the database named itself becomes db_index=True.
    assert declaration_graph.include(text_class).fields_map["note"].index or any(
        isinstance(index, Index) and tuple(index.fields) == ("note",)
        for index in declaration_graph.include(text_class).indexes
    )
    assert declaration_graph.include(text_class).fields_map["body"].unique
    assert [
        tuple(constraint.fields)
        for constraint in declaration_graph.include(text_class).constraints
        if isinstance(constraint, UniqueConstraint)
    ] == [("a", "tag")]

    # The reconstructed model's own DDL recreates every index and unique constraint.
    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator.table_creation.get_model_sql_data(
        declaration_graph.include(text_class)
    ).get_table_creation_sql()
    await connection.execute_script("DROP TABLE ix_text")
    await connection.execute_script(table_sql)
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_text")
    columns_by_name = {column.name: column for column in table_info.columns}
    assert columns_by_name["note"].has_index
    assert columns_by_name["body"].is_unique
    assert [(index.columns, index.is_unique) for index in table_info.indexes] == [(["a", "tag"], True)]

    with registered_live_models(models):
        await text_class.objects.create(id=1, note="n", body="b", a=1, tag="t")
        await text_class.objects.create(id=2, note="n", body="c", a=1, tag="u")
        assert await text_class.objects.filter(note="n").count() == 2
        # One violation per test - SQLite aborts the surrounding test transaction on the first.
        with pytest.raises(IntegrityError):
            await text_class.objects.create(id=4, body="d", a=1, tag="t")


@pytest.mark.database_independent
def test_text_field_accepts_indexes_but_encrypted_text_field_does_not():
    declaration_graph = ModelBindingGraph()

    class TextFieldIndexed(Model):
        id = fields.IntField(primary_key=True)
        note = fields.TextField(db_index=True)
        body = fields.TextField(unique=True)
        tag = fields.TextField()

        class Meta:
            abstract = True
            constraints = (UniqueConstraint(fields=("note", "tag")),)

    assert declaration_graph.include(TextFieldIndexed).fields_map["body"].unique
    assert declaration_graph.include(TextFieldIndexed).fields_map["note"].index
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        fields.EncryptedTextField(unique=True)
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        fields.EncryptedTextField(db_index=True)


# ---------------------------------------------------------------------------------------------
# Partial and expression indexes


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_partial_unique_index_keeps_its_condition(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_partial"],
        [
            "CREATE TABLE ix_partial (id INTEGER PRIMARY KEY, code VARCHAR(10) NOT NULL, active INTEGER NOT NULL)",
            "CREATE UNIQUE INDEX ix_partial_code_active ON ix_partial (code) WHERE active = 1",
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_partial")
    assert not {column.name: column for column in table_info.columns}["code"].is_unique
    [index_info] = table_info.indexes
    assert (index_info.columns, index_info.is_unique, index_info.condition_sql) == (["code"], True, "active = 1")

    # A partial unique index under a name of its own is the index of a named
    # UniqueConstraint(condition=...), the predicate kept as the database has it.
    models = await build_models(connection, ["ix_partial"], build_mode)
    partial_class = models["ix_partial"]
    assert not declaration_graph.include(partial_class).fields_map["code"].unique
    assert not declaration_graph.include(partial_class).indexes
    [constraint] = declaration_graph.include(partial_class).constraints
    assert isinstance(constraint, UniqueConstraint)
    assert (tuple(constraint.fields), constraint.name, constraint.condition) == (
        ("code",),
        "ix_partial_code_active",
        RawSQLTerm("active = 1"),
    )

    with registered_live_models(models):
        await partial_class.objects.create(id=1, code="x", active=0)
        await partial_class.objects.create(id=2, code="x", active=0)
        await partial_class.objects.create(id=3, code="x", active=1)
        assert await partial_class.objects.filter(code="x").count() == 3
        with pytest.raises(IntegrityError):
            await partial_class.objects.create(id=4, code="x", active=1)


@pytest.mark.asyncio
async def test_partial_index_with_unparsed_condition_keeps_it_as_raw_sql(db, connection):
    await recreate_tables(
        connection,
        ["ix_partial_raw"],
        [
            "CREATE TABLE ix_partial_raw (id INTEGER PRIMARY KEY, code VARCHAR(10) NULL)",
            "CREATE UNIQUE INDEX ix_partial_raw_code ON ix_partial_raw (code) WHERE code IS NOT NULL",
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_partial_raw")
    [index_info] = table_info.indexes
    assert "code IS NOT NULL" in index_info.condition_sql
    source = await generated_source(connection, "ix_partial_raw")
    assert "TODO" not in source
    assert "unique=True)" not in source.split("class Meta")[0]
    assert (
        "constraints = [UniqueConstraint(fields=['code'], name='ix_partial_raw_code', "
        "condition=RawSQLTerm('code IS NOT NULL'))]"
    ) in source


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_expression_index_is_reconstructed(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_expression"],
        [
            "CREATE TABLE ix_expression (id INTEGER PRIMARY KEY, note VARCHAR(20) NOT NULL)",
            "CREATE INDEX ix_expression_lower ON ix_expression (lower(note))",
        ],
    )
    models = await build_models(connection, ["ix_expression"], build_mode)
    expression_class = models["ix_expression"]
    [index] = declaration_graph.include(expression_class).indexes
    assert type(index) is Index
    [expression] = index.declared_expressions
    # Postgres canonicalizes the term text ("lower((note)::text)").
    expression_sql = expression.get_sql(connection.sql_context)
    assert expression_sql.startswith("lower(") and "note" in expression_sql
    assert "TODO" not in await generated_source(connection, "ix_expression")

    with registered_live_models(models):
        await expression_class.objects.create(id=1, note="Abc")
        assert (await expression_class.objects.get(id=1)).note == "Abc"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_expression_index_with_sort_order_is_reconstructed_with_it(db, connection):
    await recreate_tables(
        connection,
        ["ix_expression_desc"],
        [
            "CREATE TABLE ix_expression_desc (id INTEGER PRIMARY KEY, note VARCHAR(20) NOT NULL)",
            "CREATE INDEX ix_expression_desc_lower ON ix_expression_desc (lower(note) DESC)",
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_expression_desc")
    assert table_info.get_diagnostics(CatalogDiagnosticCode.INDEX) == []
    source = await generated_source(connection, "ix_expression_desc")
    assert "CatalogIndexKey(RawSQLTerm('lower(note) DESC'), Order.DESC)" in source
    assert "TODO" not in source


# ---------------------------------------------------------------------------------------------
# Composite FK on SQLite (and Postgres)


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_composite_foreign_key_is_reconstructed(db, connection, build_mode):
    await recreate_tables(
        connection,
        ["ix_line", "ix_part"],
        [
            "CREATE TABLE ix_part (a INTEGER NOT NULL, b INTEGER NOT NULL, label VARCHAR(10) NULL, "
            "PRIMARY KEY (a, b))",
            "CREATE TABLE ix_line (id INTEGER PRIMARY KEY, part_a INTEGER NOT NULL, part_b INTEGER NOT NULL, "
            "FOREIGN KEY (part_a, part_b) REFERENCES ix_part (a, b) ON DELETE CASCADE)",
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_line")
    assert table_info.get_diagnostics(CatalogDiagnosticCode.FOREIGN_KEY) == []
    [composite_fk] = table_info.composite_foreign_keys
    assert (composite_fk.field_name, composite_fk.columns) == ("part", ("part_a", "part_b"))

    models = await build_models(connection, ["ix_part", "ix_line"], build_mode)
    part_class, line_class = models["ix_part"], models["ix_line"]
    with registered_live_models(models):
        part = await part_class.objects.create(a=1, b=2, label="p")
        await line_class.objects.create(id=1, part=part)
        line = await line_class.objects.get(id=1).select_related("part")
        assert line.part.label == "p"
        assert await line_class.objects.filter(part__label="p").count() == 1


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_unique_composite_foreign_key_is_reconstructed_as_one_to_one(db, connection, build_mode):
    await recreate_tables(
        connection,
        ["ix_spare", "ix_part"],
        [
            "CREATE TABLE ix_part (a INTEGER NOT NULL, b INTEGER NOT NULL, label VARCHAR(10) NULL, "
            "PRIMARY KEY (a, b))",
            "CREATE TABLE ix_spare (id INTEGER PRIMARY KEY, part_a INTEGER NULL, part_b INTEGER NULL, "
            "CONSTRAINT uq_spare_part UNIQUE (part_a, part_b), "
            "FOREIGN KEY (part_a, part_b) REFERENCES ix_part (a, b) ON DELETE CASCADE)",
        ],
    )
    source = await generated_source(connection, "ix_spare")
    assert "part = fields.OneToOneField(" in source
    assert "UniqueConstraint" not in source

    models = await build_models(connection, ["ix_part", "ix_spare"], build_mode)
    part_class, spare_class = models["ix_part"], models["ix_spare"]
    with registered_live_models(models):
        part = await part_class.objects.create(a=1, b=2, label="p")
        await spare_class.objects.create(id=1, part=part)
        spare = await spare_class.objects.get(id=1).select_related("part")
        assert spare.part.label == "p"
        with pytest.raises(IntegrityError):
            await spare_class.objects.create(id=2, part=part)


# ---------------------------------------------------------------------------------------------
# Postgres identity columns and numeric defaults


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_postgres_identity_columns_are_never_written(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_identity"],
        [
            "CREATE TABLE ix_identity (id SERIAL PRIMARY KEY, "
            "seq INTEGER GENERATED ALWAYS AS IDENTITY, "
            "counter BIGINT GENERATED BY DEFAULT AS IDENTITY, name VARCHAR(10) NOT NULL)"
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_identity")
    identity_by_name = {column.name: column.identity_generation for column in table_info.columns}
    assert identity_by_name == {"id": None, "seq": "ALWAYS", "counter": "BY DEFAULT", "name": None}
    assert "AS IDENTITY column" in await generated_source(connection, "ix_identity")

    models = await build_models(connection, ["ix_identity"], build_mode)
    identity_class = models["ix_identity"]
    assert declaration_graph.include(identity_class).fields_map["seq"].generated
    assert isinstance(declaration_graph.include(identity_class).fields_map["counter"], fields.BigIntField)

    with registered_live_models(models):
        first = await identity_class.objects.create(name="a")
        assert (first.seq, first.counter) == (1, 1)
        first.name = "b"
        await first.save()
        await identity_class.objects.create(id=50, name="c")
        explicit = await identity_class.objects.get(id=50)
        assert (explicit.seq, explicit.counter) == (2, 2)
        await identity_class.objects.filter(id=50).update(name="d")
        assert sorted(row.name for row in await identity_class.objects.all()) == ["b", "d"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_postgres_identity_key_and_domain_columns_are_marked(db, connection):
    """Both came back as plain fields without a # TODO, though drift reports them until they are altered."""
    await recreate_tables(connection, ["ix_identity_key"], ["DROP DOMAIN IF EXISTS ix_positive"])
    await connection.execute_script(
        "CREATE DOMAIN ix_positive AS integer CHECK (VALUE > 0);"
        "CREATE TABLE ix_identity_key (id INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, amount ix_positive)"
    )
    try:
        lines = (await generated_source(connection, "ix_identity_key")).splitlines()
    finally:
        await connection.execute_script("DROP TABLE ix_identity_key; DROP DOMAIN ix_positive")

    (key_line,) = [line for line in lines if line.strip().startswith("id = ")]
    (amount_line,) = [line for line in lines if line.strip().startswith("amount = ")]
    assert "# TODO: GENERATED BY DEFAULT AS IDENTITY primary key" in key_line
    assert "# TODO: the column is of the domain 'ix_positive' over 'integer'" in amount_line


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_postgres_numeric_defaults_are_numbers(db, connection, build_mode):
    await recreate_tables(
        connection,
        ["ix_numeric_default"],
        [
            "CREATE TABLE ix_numeric_default (id INTEGER PRIMARY KEY, "
            "ratio DOUBLE PRECISION NOT NULL DEFAULT -1.5, big BIGINT NOT NULL DEFAULT 9000000000, "
            "small INTEGER NOT NULL DEFAULT -3, label VARCHAR(10) NOT NULL DEFAULT '-2')"
        ],
    )
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_numeric_default")
    defaults = {column.name: column.db_default for column in table_info.columns}
    assert defaults == {"id": None, "ratio": -1.5, "big": 9000000000, "small": -3, "label": "-2"}

    models = await build_models(connection, ["ix_numeric_default"], build_mode)
    numeric_class = models["ix_numeric_default"]
    with registered_live_models(models):
        await numeric_class.objects.create(id=1)
        row = await numeric_class.objects.get(id=1)
        assert (row.ratio, row.big, row.small, row.label) == (-1.5, 9000000000, -3, "-2")


@pytest.mark.parametrize(
    "raw_default, expected",
    [
        ("'-1.5'::double precision", -1.5),
        ("'9000000000'::bigint", 9000000000),
        ("'-7'::integer", -7),
        ("'-7'::text", "-7"),
        ("'abc'::character varying", "abc"),
    ],
)
def test_parse_db_default_turns_cast_numeric_literals_into_numbers(raw_default, expected):
    parsed = PostgresqlIntrospector.parse_db_default(raw_default)
    assert parsed == expected
    assert type(parsed) is type(expected)


# ---------------------------------------------------------------------------------------------
# A binary column with an index in the DB


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_a_unique_binary_column_keeps_its_index(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    binary_type = "BYTEA" if connection.dialect.name == "postgresql" else "BLOB"
    await recreate_tables(
        connection,
        ["ix_binary"],
        [
            f"CREATE TABLE ix_binary (id INTEGER PRIMARY KEY, digest {binary_type} NOT NULL UNIQUE, "
            f"a INTEGER NOT NULL, blob_part {binary_type} NOT NULL, UNIQUE (a, blob_part))"
        ],
    )
    source = await generated_source(connection, "ix_binary")
    assert "can't be indexed" not in source

    models = await build_models(connection, ["ix_binary"], build_mode)
    binary_class = models["ix_binary"]
    assert declaration_graph.include(binary_class).fields_map["digest"].unique
    assert [
        constraint.fields
        for constraint in declaration_graph.include(binary_class).constraints
        if isinstance(constraint, UniqueConstraint)
    ] == [("a", "blob_part")]
    with registered_live_models(models):
        await binary_class.objects.create(id=1, digest=b"\x01", a=1, blob_part=b"\x02")
        assert (await binary_class.objects.get(id=1)).digest == b"\x01"


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_gin_index_over_json_column_is_kept(db, connection, build_mode):
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_gin_json"],
        [
            "CREATE TABLE ix_gin_json (id INTEGER PRIMARY KEY, payload JSONB NOT NULL)",
            "CREATE INDEX ix_gin_json_payload ON ix_gin_json USING gin (payload)",
        ],
    )
    models = await build_models(connection, ["ix_gin_json"], build_mode)
    json_class = models["ix_gin_json"]
    [index] = declaration_graph.include(json_class).indexes
    assert (index.INDEX_TYPE, index.fields) == ("GIN", ["payload"])
    with registered_live_models(models):
        await json_class.objects.create(id=1, payload={"k": [1]})
        assert await json_class.objects.filter(payload__contains={"k": [1]}).count() == 1


@pytest.mark.database_independent
def test_non_btree_index_over_non_indexable_field_passes_validation():
    declaration_graph = ModelBindingGraph()
    from hare.dialects.postgresql.indexes import GinIndex
    from tests.shared_models.testmodels import JSONFields

    meta = declaration_graph.include(JSONFields)
    original = meta.indexes
    meta.indexes = (GinIndex(fields=("data",)),)
    try:
        ModelDefinitionChecks.validate_index_fields(meta)
        meta.indexes = (Index(fields=("data",)),)
        with pytest.raises(ConfigurationError, match="can't be indexed"):
            ModelDefinitionChecks.validate_index_fields(meta)
    finally:
        meta.indexes = original


# ---------------------------------------------------------------------------------------------
# Generated names that would shadow the generated module's own imports


@pytest.mark.asyncio
async def test_generated_names_never_shadow_the_modules_imports(db, connection):
    """A column "fields" shadowed `fields` for the rest of the class body, and a table "model"
    rebound the module-level `Model` every later class derives from - the module didn't import."""
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_shadow_usr", "model", "ix_shadow_doc"],
        [
            "CREATE TABLE ix_shadow_doc (id INTEGER PRIMARY KEY, fields TEXT NULL, after INTEGER NULL)",
            "CREATE TABLE model (id INTEGER PRIMARY KEY, code VARCHAR(10) NULL)",
            'CREATE TABLE ix_shadow_usr (id INTEGER PRIMARY KEY, "OnDelete" INTEGER NULL, '
            "m INTEGER NULL REFERENCES model (id) ON DELETE NO ACTION)",
        ],
    )
    source = await SchemaInspector.inspect(connection, tables=["model", "ix_shadow_usr", "ix_shadow_doc"])
    namespace: dict[str, Any] = {"__name__": "tests.generated_shadowing_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code

    assert namespace["Model2"] is Model
    model_class = namespace["Model"]
    apps = StateApps()
    for name in ("Model", "IxShadowDoc", "IxShadowUsr"):
        apps.register_model("models", namespace[name])
    apps.init_relations()
    assert apps.get_binding(model_class).db_table == "model"
    assert "fields2.ForeignKeyField('models.Model'" in source
    doc_fields_map = declaration_graph.include(namespace["IxShadowDoc"]).fields_map
    assert "fields" in doc_fields_map
    assert "after" in doc_fields_map
    usr_fields_map = declaration_graph.include(namespace["IxShadowUsr"]).fields_map
    assert "OnDelete" in usr_fields_map
    assert usr_fields_map["m_fk"].on_delete == "NO ACTION"
    await connection.execute_script("DROP TABLE ix_shadow_usr")
    await connection.execute_script("DROP TABLE model")
    await connection.execute_script("DROP TABLE ix_shadow_doc")


@pytest.mark.database_independent
def test_module_allocates_import_aliases_without_rebuilding_declarations(monkeypatch):
    declaration_graph = ModelBindingGraph()
    tables = [
        TableInfo(
            name,
            columns=[
                ColumnInfo("id", "INTEGER", False, True, False),
                ColumnInfo("fields", "TEXT", True, False, False),
                ColumnInfo("OnDelete", "INTEGER", True, False, False),
            ],
        )
        for name in ("model", "fields")
    ]
    built = []
    original = InspectedModelBuilder.build

    def build(builder):
        built.append(builder.table.name)
        return original(builder)

    monkeypatch.setattr(InspectedModelBuilder, "build", build)
    source = SchemaInspector.render_module(tables, "sqlite")
    assert built == ["model", "fields"]
    namespace = {"__name__": "tests.generated_import_aliases"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - generated declaration roundtrip
    apps = StateApps()
    for name in ("Model", "Fields"):
        apps.register_model("models", namespace[name])
    apps.init_relations()
    assert apps.get_binding(namespace["Model"]).db_table == "model"
    assert apps.get_binding(namespace["Fields"]).db_table == "fields"
    assert "fields" in declaration_graph.include(namespace["Model"]).fields_map
    assert "OnDelete" in declaration_graph.include(namespace["Fields"]).fields_map
    assert source == SchemaInspector.render_module(tables, "sqlite")
    assert built == ["model", "fields", "model", "fields"]


# ---------------------------------------------------------------------------------------------
# Column defaults


@requires_features(dialect="sqlite")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_sqlite_expression_default_keeps_its_parentheses(db, connection, build_mode):
    """SQLite reports `DEFAULT (datetime('now'))` as "datetime('now')" - rendered back without
    the parentheses it is invalid DDL."""
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_expression_default"],
        [
            "CREATE TABLE ix_expression_default (id INTEGER PRIMARY KEY, "
            "created TEXT NOT NULL DEFAULT (datetime('now')), code TEXT NOT NULL DEFAULT 'x')"
        ],
    )
    models = await build_models(connection, ["ix_expression_default"], build_mode)
    default_class = models["ix_expression_default"]
    assert declaration_graph.include(default_class).fields_map["created"].db_default == SqlDefault("(datetime('now'))")
    assert declaration_graph.include(default_class).fields_map["code"].db_default == "x"

    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator.table_creation.get_model_sql_data(
        declaration_graph.include(default_class)
    ).get_table_creation_sql()
    await connection.execute_script("DROP TABLE ix_expression_default")
    await connection.execute_script(table_sql)
    await connection.execute_script("INSERT INTO ix_expression_default (id) VALUES (1)")
    [row] = await connection.execute_dicts("SELECT created, code FROM ix_expression_default")
    assert row["created"] and row["code"] == "x"
    table_info = await DatabaseCatalog.inspect_table(connection, "ix_expression_default")
    assert {column.name: column for column in table_info.columns}["created"].db_default == SqlDefault(
        "(datetime('now'))"
    )


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_json_column_default_is_the_document_not_a_string(db, connection, build_mode):
    """DEFAULT '{}'::jsonb came back as JSONField(db_default='{}') - stored again as the JSON
    string "{}" instead of an empty object."""
    declaration_graph = ModelBindingGraph()
    json_type = "JSONB" if connection.dialect.name == "postgresql" else "JSON"
    await recreate_tables(
        connection,
        ["ix_json_default"],
        [
            f"CREATE TABLE ix_json_default (id INTEGER PRIMARY KEY, meta {json_type} NOT NULL DEFAULT '{{}}', "
            f"items {json_type} NULL DEFAULT '[1, 2]')"
        ],
    )
    models = await build_models(connection, ["ix_json_default"], build_mode)
    json_class = models["ix_json_default"]
    assert declaration_graph.include(json_class).fields_map["meta"].db_default == {}
    assert declaration_graph.include(json_class).fields_map["items"].db_default == [1, 2]

    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator.table_creation.get_model_sql_data(
        declaration_graph.include(json_class)
    ).get_table_creation_sql()
    await connection.execute_script("DROP TABLE ix_json_default")
    await connection.execute_script(table_sql)
    with registered_live_models(models):
        await connection.execute_script("INSERT INTO ix_json_default (id) VALUES (1)")
        fetched = await json_class.objects.get(id=1)
        assert (fetched.meta, fetched.items) == ({}, [1, 2])


# ---------------------------------------------------------------------------------------------
# UNIQUE foreign key column


@requires_features(supports_unique_constraints=True)
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_unique_foreign_key_column_becomes_one_to_one_field(db, connection, build_mode):
    """A single-column UNIQUE FK was rendered as ForeignKeyField(unique=True), whose DDL never
    carried the UNIQUE constraint."""
    declaration_graph = ModelBindingGraph()
    await recreate_tables(
        connection,
        ["ix_unique_fk_source", "ix_unique_fk_target"],
        [
            "CREATE TABLE ix_unique_fk_target (id INTEGER PRIMARY KEY)",
            "CREATE TABLE ix_unique_fk_source (id INTEGER PRIMARY KEY, "
            "target_id INTEGER NULL UNIQUE REFERENCES ix_unique_fk_target (id))",
        ],
    )
    source = await generated_source(connection, "ix_unique_fk_source")
    assert "fields.OneToOneField('models.IxUniqueFkTarget'" in source
    assert "unique=True" not in source
    models = await build_models(connection, ["ix_unique_fk_target", "ix_unique_fk_source"], build_mode)
    target_class, source_class = models["ix_unique_fk_target"], models["ix_unique_fk_source"]
    assert isinstance(declaration_graph.include(source_class).fields_map["target"], OneToOneFieldInstance)

    with registered_live_models(models):
        schema_generator = connection.dialect.schema_editor_class(connection)
        table_sql = schema_generator.table_creation.get_model_sql_data(
            HareContext.require_current().apps.get_binding(source_class)
        ).get_table_creation_sql()
        await connection.execute_script("DROP TABLE ix_unique_fk_source")
        await connection.execute_script(table_sql)
        table_info = await DatabaseCatalog.inspect_table(connection, "ix_unique_fk_source")
        assert {column.name: column for column in table_info.columns}["target_id"].is_unique
        target = await target_class.objects.create(id=1)
        await source_class.objects.create(id=1, target=target)
        with pytest.raises(IntegrityError):
            await source_class.objects.create(id=2, target=target)


@pytest.mark.database_independent
def test_foreign_key_field_unique_is_rejected():
    with pytest.raises(ConfigurationError, match="OneToOneField"):
        fields.ForeignKeyField("models.Tournament", unique=True)
    assert fields.OneToOneField("models.Tournament").unique
    assert not fields.ForeignKeyField("models.Tournament", unique=False).unique
    with ForeignKeyFieldInstance.replaying_migration_scope():
        fields.ForeignKeyField("models.Tournament", unique=True)


@pytest.mark.asyncio
async def test_a_single_column_unique_of_a_name_of_its_own_keeps_it(db, connection):
    """It came back as unique=True - drift then reported the database's name as a constraint to remove."""
    await recreate_tables(
        connection,
        ["ix_named_unique"],
        [
            "CREATE TABLE ix_named_unique (id INTEGER PRIMARY KEY, code TEXT NOT NULL, "
            "CONSTRAINT ix_named_uq UNIQUE (code))"
        ],
    )
    lines = (await generated_source(connection, "ix_named_unique")).splitlines()
    (code_line,) = [line for line in lines if line.strip().startswith("code = ")]
    assert "unique=True" not in code_line
    assert any("UniqueConstraint(fields=['code'], name='ix_named_uq')" in line for line in lines)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_integer_column_reads_as_int_but_the_row_id(db, connection):
    """A column declared INTEGER but the row id is the INT an IntField declares - it was read as another
    type, and a UNIQUE of the key column was left out; drift reported both."""
    await recreate_tables(
        connection,
        ["ix_composite_integer", "ix_row_id_key"],
        [
            "CREATE TABLE ix_composite_integer (x INTEGER NOT NULL, y INT NOT NULL, PRIMARY KEY (x, y))",
            "CREATE TABLE ix_row_id_key (id INTEGER NOT NULL, n INTEGER, PRIMARY KEY (id), UNIQUE (id))",
        ],
    )
    composite = await DatabaseCatalog.inspect_table(connection, "ix_composite_integer")
    row_id = await DatabaseCatalog.inspect_table(connection, "ix_row_id_key")
    assert [column.db_type for column in composite.columns] == ["INT", "INT"]
    assert [column.db_type for column in row_id.columns] == ["INTEGER", "INT"]
    assert "UniqueConstraint(fields=['id'])" in await generated_source(connection, "ix_row_id_key")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_synonyms_of_types_hare_declares_read_as_hare_declares_them(db, connection):
    """A DATETIME column another tool wrote - the field of a DatetimeField, generated with no note - was read
    as DATETIME, and drift reported the model's TIMESTAMP column as altered."""
    await recreate_tables(
        connection,
        ["ix_type_synonyms"],
        [
            "CREATE TABLE ix_type_synonyms (id INTEGER PRIMARY KEY, seen DATETIME, at datetime DEFAULT "
            "CURRENT_TIMESTAMP, ratio DOUBLE, share DOUBLE PRECISION, weight FLOAT)"
        ],
    )
    table = await DatabaseCatalog.inspect_table(connection, "ix_type_synonyms")
    assert [column.db_type for column in table.columns] == [
        "INTEGER",
        "TIMESTAMP",
        "TIMESTAMP",
        "REAL",
        "REAL",
        "REAL",
    ]
    source = await generated_source(connection, "ix_type_synonyms")
    assert "seen = fields.DatetimeField(null=True)" in source
    assert "ratio = fields.FloatField(null=True)" in source
    assert "TODO" not in source


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_indexes_are_read_back_by_name(db, connection):
    """They came back in the order they were made - a migrate of the model read back makes them in another
    order, and inspectdb then wrote the model otherwise."""
    await recreate_tables(
        connection,
        ["ix_index_order"],
        [
            "CREATE TABLE ix_index_order (id INTEGER PRIMARY KEY, a INT, b TEXT)",
            "CREATE INDEX ix_order_c ON ix_index_order (a DESC)",
            "CREATE INDEX ix_order_a ON ix_index_order (LOWER(b))",
            "CREATE INDEX ix_order_b ON ix_index_order (a) WHERE a > 0",
        ],
    )
    table = await DatabaseCatalog.inspect_table(connection, "ix_index_order")
    assert [index.name for index in table.indexes] == ["ix_order_a", "ix_order_b", "ix_order_c"]
