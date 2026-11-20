"""Public migration runs preserve snapshots and transform every selected stored value once."""

import os
from collections import Counter

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index
from hare.exceptions import ConfigurationError, IntegrityError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.instrumentation import Observers
from hare.instrumentation.declarations import QueryExecuted
from hare.migrations import Migration, MigrationRunner, State
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.rename_questions import RenameQuestions
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations import (
    AddField,
    AlterField,
    AlterModelSchema,
    AlterModelTable,
    CreateModel,
    CreateSchema,
    RemoveField,
    RemoveIndex,
    RenameField,
)
from hare.models.enums import ModelOption
from hare.query.expressions import F, Q
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.nullable_rewrite_field import NullableRewriteField
from tests.contracts.shifted_key_field import ShiftedKeyField


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["single-key", "composite-key"])
async def test_a_model_moved_to_another_connection_creates_its_table(composite):
    config = {
        "connections": {"first": "sqlite+aiosqlite://:memory:", "second": "sqlite+aiosqlite://:memory:"},
        "apps": {},
    }
    async with TemporaryDatabases(
        config, os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"), generate_schemas=False, reuse_databases=False
    ) as context:
        first, second = context.get_connection("first"), context.get_connection("second")
        model_fields = [
            ("id", fields.IntField(primary_key=not composite, generated=False)),
            ("label", fields.TextField()),
        ]
        if composite:
            model_fields.extend([("tenant", fields.IntField()), ("pk", fields.CompositePrimaryKey("tenant", "id"))])
        before = await MigrationRunner(first).apply(
            Migration(
                "initial", "old_app", operations=[CreateModel("Entry", model_fields, options={"table": "entry"})]
            ),
            State(default_connections={"old_app": "first", "new_app": "second"}),
        )
        source_apps = before.apps
        source = source_apps.get_model("old_app.Entry")
        await source_apps.get_queryset(source).create(id=1, label="source", **({"tenant": 2} if composite else {}))
        desired = before.clone()
        moved = desired.models.pop(("old_app", "Entry"))
        moved.app = "new_app"
        moved.options[ModelOption.APP] = "new_app"
        desired.models[("new_app", "Entry")] = moved
        desired.mark_model_removed("old_app", "Entry")
        desired.mark_model_added("new_app", "Entry")
        plan = before.get_operations(desired, "new_app", RenameQuestions({"old_app.Entry": "new_app.Entry"}))
        after = await MigrationRunner(second).apply(Migration("move", "new_app", operations=plan.operations), before)
        assert await DatabaseCatalog.table_exists(second, "entry")
        assert await after.apps.get_queryset("new_app.Entry", using=second).count() == 0
        assert await source_apps.get_queryset(source).count() == 1
        assert set(before.models) == {("old_app", "Entry")}


@pytest.mark.asyncio
@pytest.mark.parametrize("remove", [False, True], ids=["rename", "remove"])
@pytest.mark.parametrize(
    ("composite_target", "key", "expected"),
    [
        (False, ("owner", "position"), ("writer_id", "position")),
        (False, ("owner_id", "position"), ("writer_id", "position")),
        (True, ("owner", "position"), ("writer_region", "writer_code", "position")),
        (True, ("owner_region", "owner_code", "position"), ("writer_region", "writer_code", "position")),
        (True, ("owner_code", "position", "owner_region"), ("writer_code", "position", "writer_region")),
        (True, ("owner_region", "owner_code"), ("writer_region", "writer_code")),
        (True, ("owner_code", "owner_region"), ("writer_code", "writer_region")),
    ],
)
async def test_relation_operations_preserve_explicit_primary_key_columns(
    contract_database, composite_target, key, expected, remove
):
    target_fields = (
        [
            ("region", fields.IntField()),
            ("code", fields.TextField()),
            ("pk", fields.CompositePrimaryKey("region", "code")),
        ]
        if composite_target
        else [("id", fields.IntField(primary_key=True, generated=False))]
    )
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[
            CreateModel("Owner", target_fields),
            CreateModel(
                "Entry",
                [
                    ("owner", fields.ForeignKeyField(f"{CONTRACT_APP}.Owner", db_constraint=False)),
                    ("position", fields.IntField()),
                    ("pk", fields.CompositePrimaryKey(*key)),
                ],
            ),
        ],
    )
    before = await initial.apply(
        State(default_connections={CONTRACT_APP: contract_database.get_connection().connection_alias}), dry_run=True
    )
    declared_key = before.models[(CONTRACT_APP, "Entry")].pk_field_name
    if remove:
        with pytest.raises(ConfigurationError, match="primary key"):
            await Migration("remove", CONTRACT_APP, operations=[RemoveField("Entry", "owner")]).apply(
                before, dry_run=True
            )
    else:
        after = await Migration("rename", CONTRACT_APP, operations=[RenameField("Entry", "owner", "writer")]).apply(
            before, dry_run=True
        )
        assert after.apps.get_binding(f"{CONTRACT_APP}.Entry").primary_key_attribute == expected
    assert before.models[(CONTRACT_APP, "Entry")].pk_field_name == declared_key
    assert "owner" in before.models[(CONTRACT_APP, "Entry")].fields
    assert "writer" not in before.models[(CONTRACT_APP, "Entry")].fields


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["single-key", "composite-key"])
@pytest.mark.parametrize("mixed", [False, True], ids=["clear-all", "keep-some"])
async def test_stored_conversion_to_null_does_not_skip_later_batches(contract_database, monkeypatch, composite, mixed):
    import hare.dialects.base.schema.plans.stored_value_rewrite as rewrite_module

    monkeypatch.setattr(rewrite_module, "STORED_VALUE_REWRITE_BATCH_SIZE", 3)
    monkeypatch.setattr(NullableRewriteField, "converted_values", [])
    runner = MigrationRunner(contract_database.get_connection())
    keys = (
        [
            ("batch", fields.IntField()),
            ("sequence", fields.IntField()),
            ("pk", fields.CompositePrimaryKey("batch", "sequence")),
        ]
        if composite
        else [("id", fields.IntField(primary_key=True, generated=False))]
    )
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[CreateModel("Entry", [*keys, ("value", fields.TextField(null=True))])],
    )
    before = await runner.apply(
        initial, State(default_connections={CONTRACT_APP: contract_database.get_connection().connection_alias})
    )
    model_apps = before.apps
    model = model_apps.get_model(f"{CONTRACT_APP}.Entry")
    values = [
        None if index % 6 == 2 else f"keep-{index}" if mixed and index % 3 == 1 else f"clear-{index}"
        for index in range(13)
    ]
    await model_apps.get_queryset(model).bulk_create(
        [
            model_apps.new(
                model, **{"batch": index % 2, "sequence": index} if composite else {"id": index}, value=value
            )
            for index, value in enumerate(values)
        ]
    )
    migration = Migration(
        "convert",
        CONTRACT_APP,
        operations=[AlterField("Entry", "value", NullableRewriteField(null=True))],
    )
    after = await runner.apply(migration, before)
    updated_apps = after.apps
    updated = updated_apps.get_model(f"{CONTRACT_APP}.Entry")
    actual = (
        await updated_apps.get_queryset(updated)
        .order_by("sequence" if composite else "id")
        .values_list("value", flat=True)
    )
    assert actual == [None if value is None or value.startswith("clear") else value.upper() for value in values]
    assert Counter(NullableRewriteField.converted_values) == Counter(value for value in values if value is not None)


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["single-key", "composite-key"])
@pytest.mark.parametrize("shift", [-10000, 10000], ids=["earlier-keys", "later-keys"])
async def test_stored_key_conversion_visits_original_keys_once(contract_database, monkeypatch, composite, shift):
    import hare.dialects.base.schema.plans.stored_value_rewrite as rewrite_module

    connection = contract_database.get_connection()
    if connection.dialect.name == "clickhouse":
        pytest.skip("ClickHouse refuses UPDATE of the key columns of these tables")
    monkeypatch.setattr(rewrite_module, "STORED_VALUE_REWRITE_BATCH_SIZE", 3)
    monkeypatch.setattr(ShiftedKeyField, "shift", shift)
    monkeypatch.setattr(ShiftedKeyField, "converted_values", [])
    runner = MigrationRunner(connection)
    keys = (
        [
            ("batch", fields.IntField()),
            ("sequence", fields.IntField()),
            ("pk", fields.CompositePrimaryKey("batch", "sequence")),
        ]
        if composite
        else [("id", fields.IntField(primary_key=True, generated=False))]
    )
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[CreateModel("Entry", [*keys, ("original", fields.IntField())])],
    )
    before = await runner.apply(initial, State(default_connections={CONTRACT_APP: connection.connection_alias}))
    model_apps = before.apps
    model = model_apps.get_model(f"{CONTRACT_APP}.Entry")
    await model_apps.get_queryset(model).bulk_create(
        [
            model_apps.new(
                model, **{"batch": index % 2, "sequence": index} if composite else {"id": index}, original=index
            )
            for index in range(10)
        ]
    )
    name = "sequence" if composite else "id"
    field = ShiftedKeyField() if composite else ShiftedKeyField(primary_key=True, generated=False)
    migration = Migration("convert", CONTRACT_APP, operations=[AlterField("Entry", name, field)])
    tables_before = set(await DatabaseCatalog.get_table_names(connection))
    statements = []
    with Observers.observing(QueryExecuted, lambda event: statements.append(event.sql)):
        after = await runner.apply(migration, before)
    reads = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT") and "ORDER BY" in sql]
    assert reads
    assert all("LIMIT 3" in sql for sql in reads)
    assert set(await DatabaseCatalog.get_table_names(connection)) == tables_before
    updated_apps = after.apps
    updated = updated_apps.get_model(f"{CONTRACT_APP}.Entry")
    assert await updated_apps.get_queryset(updated).order_by("original").values_list(name, "original") == [
        (index + shift, index) for index in range(10)
    ]
    assert Counter(ShiftedKeyField.converted_values) == Counter(range(10))


@pytest.mark.asyncio
async def test_a_dry_run_and_apply_keep_the_state_needed_to_undo(contract_database):
    connection = contract_database.get_connection()
    runner = MigrationRunner(connection)
    before = State()
    migration = Migration(
        "create_book",
        CONTRACT_APP,
        operations=[
            CreateModel(
                "Book",
                [("id", fields.IntField(primary_key=True, generated=False))],
                options={"table": "snapshot_book"},
            )
        ],
    )
    preview = await runner.apply(migration, before, dry_run=True)
    assert before.models == {}
    assert (CONTRACT_APP, "Book") in preview.models
    assert not await DatabaseCatalog.table_exists(connection, "snapshot_book")
    after = await runner.apply(migration, before)
    assert before.models == {}
    assert await DatabaseCatalog.table_exists(connection, "snapshot_book")
    preview_undo = await runner.unapply(migration, state_before=before, dry_run=True)
    assert preview_undo.models == {}
    assert (CONTRACT_APP, "Book") in after.models
    assert await DatabaseCatalog.table_exists(connection, "snapshot_book")
    restored = await runner.unapply(migration, state_before=before)
    assert restored is not before
    assert restored.models == before.models == {}
    assert (CONTRACT_APP, "Book") in after.models
    assert not await DatabaseCatalog.table_exists(connection, "snapshot_book")


@pytest.mark.asyncio
async def test_a_failed_preview_preserves_existing_model_fields(contract_database):
    runner = MigrationRunner(contract_database.get_connection())
    before = await runner.apply(
        Migration(
            "initial",
            CONTRACT_APP,
            operations=[
                CreateModel(
                    "Book",
                    [("id", fields.IntField(primary_key=True))],
                )
            ],
        ),
        State(),
        dry_run=True,
    )
    original = before.models[(CONTRACT_APP, "Book")]
    with pytest.raises(IncompatibleStateError):
        await runner.apply(
            Migration(
                "invalid",
                CONTRACT_APP,
                operations=[
                    AddField("Book", "title", fields.CharField(max_length=20)),
                    RemoveField("Book", "missing"),
                ],
            ),
            before,
            dry_run=True,
        )
    assert before.models[(CONTRACT_APP, "Book")] is original
    assert set(original.fields) == {"id"}


@pytest.mark.sqlite_only
@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [False, True], ids=["virtual", "stored"])
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("depth", [1, 2, 3], ids=["single", "direct", "transitive"])
async def test_recreating_a_generated_column_preserves_its_dependent_chain(
    contract_database, stored, composite, depth
):
    connection = contract_database.get_connection()
    runner = MigrationRunner(connection)

    def generated(expression):
        return fields.GeneratedField(expression, output_field=fields.IntField(), stored=stored)

    generated_columns = [
        ("z_part", generated(F("value") * 2)),
        ("a_total", generated(F("z_part") + 1)),
        ("m_final", generated(F("a_total") + 1)),
    ][:depth]
    names = [name for name, _ in generated_columns]
    leaf = names[-1]
    declaration = [
        ("number", fields.IntField(primary_key=not composite, generated=False)),
        ("region", fields.IntField()),
        ("value", fields.IntField()),
        *generated_columns,
    ]
    if composite:
        declaration.append(("pk", fields.CompositePrimaryKey("region", "number")))
    options = {
        "table": "generated_chain",
        "indexes": [Index(fields=(leaf,), name="chain_result")],
        "constraints": [CheckConstraint(name="positive_result", check=Q(**{f"{leaf}__gte": 0}))],
    }
    initial = Migration("initial", CONTRACT_APP, operations=[CreateModel("Chain", declaration, options=options)])
    before = await runner.apply(initial, State())
    await connection.execute("INSERT INTO generated_chain(number, region, value) VALUES (1, 7, 2)")
    desired = before.clone()
    AlterField("Chain", "z_part", generated(F("value") * 3)).state_forward(CONTRACT_APP, desired)
    operations = OperationGenerator(before, desired).generate()
    assert [operation.name for operation in operations if isinstance(operation, RemoveField)] == names[::-1]
    assert [operation.name for operation in operations if isinstance(operation, AddField)] == names
    change = Migration("change", CONTRACT_APP, operations=operations)

    async def check_rows(values):
        expected = dict(zip(names, values[:depth], strict=True))
        assert await connection.execute_dicts("SELECT * FROM generated_chain") == [
            {"number": 1, "region": 7, "value": 2, **expected}
        ]
        assert [row["name"] for row in await connection.execute_dicts("PRAGMA index_info(chain_result)")] == [leaf]
        table = await DatabaseCatalog.inspect_table(connection, "generated_chain")
        assert [constraint.name for constraint in table.check_constraints] == ["positive_result"]

    await runner.apply(change, before)
    await check_rows((6, 7, 8))
    await runner.unapply(change, state_before=before)
    await check_rows((4, 5, 6))
    await runner.apply(change, before)
    await check_rows((6, 7, 8))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", (False, True), ids=("scalar", "composite"))
@pytest.mark.parametrize("change_type", (False, True), ids=("rename", "rename-and-widen"))
async def test_physical_column_renames_preserve_generated_names(contract_database, composite, change_type):
    connection = contract_database.get_connection()
    runner = MigrationRunner(connection)
    declaration = [
        ("number", fields.IntField(primary_key=not composite, generated=False, source_field="old_number")),
        ("region", fields.IntField()),
        ("owner", fields.ForeignKeyField(f"{CONTRACT_APP}.Owner", source_field="old_owner", null=True)),
    ]
    if composite:
        declaration.append(("pk", fields.CompositePrimaryKey("region", "number")))
    options = {
        "table": "name_tracking",
        "indexes": [
            Index(fields=("number", "region")),
            Index(F("number") + 1),
            Index(fields=("region",), name="same_name"),
        ],
        "constraints": [UniqueConstraint(fields=("number", "region"))],
    }
    initial = Migration(
        "initial",
        CONTRACT_APP,
        operations=[
            CreateSchema("name_tracking_moved"),
            CreateModel(
                "Owner", [("id", fields.IntField(primary_key=True, generated=False))], options={"table": "name_owner"}
            ),
            CreateModel("Entry", declaration, options=options),
        ],
    )
    before = await runner.apply(initial, State())
    await connection.execute("INSERT INTO name_owner VALUES (11)")
    await connection.execute("INSERT INTO name_tracking(old_number, region, old_owner) VALUES (1, 7, 11)")

    async def names(schema, table):
        constraints = await connection.execute_dicts(
            "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass($1) AND contype IN ('p', 'f', 'u')",
            [f"{schema}.{table}"],
        )
        indexes = await connection.execute_dicts(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = $2", [schema, table]
        )
        return {row["conname"] for row in constraints}, {row["indexname"] for row in indexes}

    original = await names("public", "name_tracking")
    field_type = fields.BigIntField if change_type else fields.IntField
    rename = Migration(
        "columns",
        CONTRACT_APP,
        operations=[
            AlterField(
                "Entry", "number", field_type(primary_key=not composite, generated=False, source_field="new_number")
            ),
            AlterField(
                "Entry",
                "owner",
                fields.ForeignKeyField(
                    f"{CONTRACT_APP}.Owner",
                    source_field="new_owner",
                    null=True,
                    on_delete=fields.SET_NULL if change_type else fields.CASCADE,
                ),
            ),
        ],
    )
    renamed = await runner.apply(rename, before)
    changed = await names("public", "name_tracking")
    assert tuple(map(len, changed)) == tuple(map(len, original))
    assert changed[0].isdisjoint(original[0])
    assert changed[1] & original[1] == {"same_name"}
    move = Migration(
        "move",
        CONTRACT_APP,
        operations=[AlterModelSchema("Entry", "name_tracking_moved"), AlterModelTable("Entry", "renamed_entry")],
    )
    moved = await runner.apply(move, renamed)
    assert await connection.execute_dicts(
        "SELECT new_number, region, new_owner FROM name_tracking_moved.renamed_entry"
    ) == [{"new_number": 1, "region": 7, "new_owner": 11}]
    for invalid_values in ("(2, 7, 999)", "(1, 7, 11)"):
        with pytest.raises(IntegrityError):
            async with connection._in_transaction() as transaction:
                await transaction.execute(
                    "INSERT INTO name_tracking_moved.renamed_entry(new_number, region, new_owner) "
                    f"VALUES {invalid_values}"
                )
    inventory = await names("name_tracking_moved", "renamed_entry")
    remove_index = Migration(
        "drop_index", CONTRACT_APP, operations=[RemoveIndex("Entry", index=options["indexes"][0])]
    )
    await runner.apply(remove_index, moved)
    after_drop = await names("name_tracking_moved", "renamed_entry")
    assert after_drop[0] == inventory[0]
    assert after_drop[1] < inventory[1]
    assert len(inventory[1] - after_drop[1]) == 1
    await runner.unapply(remove_index, state_before=moved)
    assert await names("name_tracking_moved", "renamed_entry") == inventory
    await runner.unapply(move, state_before=renamed)
    assert await names("public", "name_tracking") == changed
    await runner.unapply(rename, state_before=before)
    assert await names("public", "name_tracking") == original
    assert await connection.execute_dicts("SELECT old_number, region, old_owner FROM name_tracking") == [
        {"old_number": 1, "region": 7, "old_owner": 11}
    ]
