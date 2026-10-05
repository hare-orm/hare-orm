"""Round-trip tests against a real database: autodetector -> operations -> apply -> autodetect again.

Every test builds two versions of a live model, lets the OperationGenerator diff the migrated
(tracked) state against the second version, applies the generated operations for real, checks the
resulting schema, and finally requires a second autodetect run to report no further changes.
"""

from __future__ import annotations

from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import Index, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields import SET_DEFAULT, ForeignKeyField, OneToOneField
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.data.numeric import DecimalField, IntField
from hare.fields.data.text import CharField
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields import ForeignKeyFieldInstance
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.autodetection.state_signatures import StateSignatures
from hare.migrations.migration import Migration
from hare.migrations.operations import AlterField, CreateModel
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest

APP_LABEL = "models"


def get_schema_editor(connection):
    return connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)


def build_model(
    model_name: str,
    table: str,
    fields: dict[str, Any],
    meta_options: dict[str, Any] | None = None,
) -> type[Model]:
    """Builds a live model class the way a user's models module would declare it."""
    attributes: dict[str, Any] = {"id": IntField(primary_key=True), **fields}
    attributes["Meta"] = type("Meta", (), {"table": table, "app": APP_LABEL, **(meta_options or {})})
    attributes["_no_comments"] = True
    return type(model_name, (Model,), attributes)


def build_live_state(*models: type[Model]) -> State:
    live_state = State(models={}, apps=StateApps())
    for model in models:
        live_state.models[(APP_LABEL, model.__name__)] = ModelState.make_from_model(APP_LABEL, model)
    return live_state


class RoundTrip:
    """Drives one real database through successive live model versions."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.dialect = DatabaseUnderTest.get_engine_name(connection.dialect)
        self.editor = get_schema_editor(connection)
        self.tracked_state = State(models={}, apps=StateApps())
        self.migration_count = 0

    async def migrate_to(self, *models: type[Model]) -> list[HareOperation]:
        """Generates and applies the operations moving the tracked state to the live models."""
        operations = OperationGenerator(self.tracked_state, build_live_state(*models)).generate()
        await self.apply(operations)
        return operations

    async def apply(self, operations: list[HareOperation]) -> None:
        self.migration_count += 1
        migration = Migration(name=f"{self.migration_count:04d}_step", app_label=APP_LABEL)
        migration.operations = list(operations)
        self.tracked_state = await migration.apply(self.tracked_state, dry_run=False, schema_editor=self.editor)

    def get_pending_operations(self, *models: type[Model]) -> list[HareOperation]:
        return OperationGenerator(self.tracked_state, build_live_state(*models)).generate()

    async def get_index_definitions(self, table: str) -> dict[str, str]:
        if self.dialect == "sqlite":
            rows = await self.connection.execute_dicts(
                f"SELECT name, sql AS definition FROM sqlite_master "
                f"WHERE type = 'index' AND tbl_name = '{table}' AND sql IS NOT NULL"
            )
        else:
            rows = await self.connection.execute_dicts(
                f"SELECT indexname AS name, indexdef AS definition FROM pg_indexes "
                f"WHERE tablename = '{table}' AND schemaname = current_schema()"
            )
        return {row["name"]: row["definition"] for row in rows}

    async def get_plain_index_names(self, table: str) -> list[str]:
        """Names of the ordinary (auto-named) indexes on `table`, excluding unique-constraint indexes."""
        return sorted(name for name in await self.get_index_definitions(table) if name.startswith("idx_"))


@pytest.fixture
def round_trip(db_isolated) -> RoundTrip:
    return RoundTrip(db_isolated.get_connection())


INDEX_TOGGLE_CASES = [
    pytest.param({}, {"db_index": True}, 1, id="index_added"),
    pytest.param({"db_index": True}, {}, 0, id="index_removed"),
    pytest.param({"db_index": True}, {"unique": True}, 0, id="index_replaced_by_unique"),
    pytest.param({"unique": True}, {"db_index": True}, 1, id="unique_replaced_by_index"),
    pytest.param({"unique": True}, {"db_index": True, "unique": True}, 1, id="index_added_to_unique"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("old_options", "new_options", "expected_index_count"), INDEX_TOGGLE_CASES)
async def test_changing_db_index_on_existing_field_creates_or_drops_the_index_once(
    round_trip: RoundTrip, old_options: dict, new_options: dict, expected_index_count: int
) -> None:
    table = "rt_index_toggle"
    old_model = build_model("Record", table, {"label": CharField(max_length=10, default="", **old_options)})
    new_model = build_model("Record", table, {"label": CharField(max_length=10, default="", **new_options)})

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    assert len(await round_trip.get_plain_index_names(table)) == expected_index_count
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_hand_written_alter_field_toggling_db_index_creates_and_drops_the_index_once(
    round_trip: RoundTrip,
) -> None:
    """A hand-written AlterField (no companion AddIndex/RemoveIndex) must still create/drop the
    index exactly once and leave tracked state matching the live model."""
    table = "rt_manual_index"
    plain_model = build_model("Record", table, {"label": CharField(max_length=10, default="")})
    indexed_model = build_model("Record", table, {"label": CharField(max_length=10, default="", db_index=True)})
    await round_trip.migrate_to(plain_model)

    await round_trip.apply(
        [AlterField(model_name="Record", name="label", field=CharField(max_length=10, default="", db_index=True))]
    )

    assert len(await round_trip.get_plain_index_names(table)) == 1
    assert round_trip.get_pending_operations(indexed_model) == []

    await round_trip.apply([AlterField(model_name="Record", name="label", field=CharField(max_length=10, default=""))])

    assert await round_trip.get_plain_index_names(table) == []
    assert round_trip.get_pending_operations(plain_model) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("old_options", "new_options"), [({"db_index": False}, {}), ({}, {"db_index": False})])
async def test_changing_db_index_on_foreign_key_creates_or_drops_the_index_once(
    round_trip: RoundTrip, old_options: dict, new_options: dict
) -> None:
    parent_model = build_model("Parent", "rt_fk_parent", {})
    old_child = build_model(
        "Child", "rt_fk_child", {"parent": ForeignKeyField("models.Parent", related_name=False, **old_options)}
    )
    new_child = build_model(
        "Child", "rt_fk_child", {"parent": ForeignKeyField("models.Parent", related_name=False, **new_options)}
    )

    await round_trip.migrate_to(parent_model, old_child)
    await round_trip.migrate_to(parent_model, new_child)

    expected_index_count = 0 if new_options else 1
    assert len(await round_trip.get_plain_index_names("rt_fk_child")) == expected_index_count
    assert round_trip.get_pending_operations(parent_model, new_child) == []


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_adding_db_default_to_a_legacy_set_default_foreign_key_makes_the_database_apply_it(
    round_trip: RoundTrip,
) -> None:
    """Upgrade path for a schema created back when on_delete=SET_DEFAULT with only default= was
    accepted: its column has no DDL DEFAULT, so the database reset it to NULL. Adding db_default
    to the model autodetects an AlterField that sets the column DEFAULT on the existing table,
    after which a real DELETE of the parent falls back to it."""
    connection = round_trip.connection
    parent_model = build_model("Parent", "rt_set_default_parent", {})
    # Constructed the way a historical migration file is loaded - today's ForeignKeyField
    # rejects this exact shape outside a replay.
    with ForeignKeyFieldInstance.replaying_migration_scope():
        legacy_field = ForeignKeyField(
            "models.Parent", related_name=False, null=True, on_delete=SET_DEFAULT, default=1
        )
    legacy_child = build_model("Child", "rt_set_default_child", {"parent": legacy_field})
    fixed_child = build_model(
        "Child",
        "rt_set_default_child",
        {
            "parent": ForeignKeyField(
                "models.Parent", related_name=False, null=True, on_delete=SET_DEFAULT, db_default=1
            )
        },
    )

    await round_trip.migrate_to(parent_model, legacy_child)
    operations = await round_trip.migrate_to(parent_model, fixed_child)

    assert [type(operation) for operation in operations] == [AlterField]
    assert round_trip.get_pending_operations(parent_model, fixed_child) == []
    await connection.execute_script(
        "INSERT INTO rt_set_default_parent (id) VALUES (1);"
        "INSERT INTO rt_set_default_parent (id) VALUES (2);"
        "INSERT INTO rt_set_default_child (id, parent_id) VALUES (1, 2);"
    )
    await connection.execute_script("DELETE FROM rt_set_default_parent WHERE id = 2")
    rows = await connection.execute_dicts("SELECT parent_id FROM rt_set_default_child WHERE id = 1")
    assert rows[0]["parent_id"] == 1


@pytest.mark.asyncio
async def test_combined_table_description_index_and_check_constraint_migrates_once(round_trip: RoundTrip) -> None:
    """A single model edit that adds Meta.table_description, an Index, and a CheckConstraint all
    at once used to autodetect an AlterModelOptions carrying the FULL new options dict (indexes
    and constraints included) alongside a separate AddIndex/AddConstraint for the very same
    entries - AlterModelOptions.state_forward()'s plain dict.update() then duplicated both in
    tracked state, and applying the combo crashed `migrate` on SQLite (_remake_table() re-issuing
    the same CREATE INDEX twice)."""
    table = "rt_options_index_constraint"
    old_model = build_model("Item", table, {"name": CharField(max_length=20)})
    new_model = build_model(
        "Item",
        table,
        {"name": CharField(max_length=20)},
        meta_options={
            "table_description": "an item",
            "indexes": [Index(fields=("name",), name="ix_item_name")],
            "constraints": [CheckConstraint(check=RawSQLTerm("id >= 0"), name="ck_item_id")],
        },
    )

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    index_definitions = await round_trip.get_index_definitions(table)
    assert "ix_item_name" in index_definitions
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_unnamed_unique_constraint_added_via_meta_constraints_does_not_loop_forever(
    round_trip: RoundTrip,
) -> None:
    """Adding an unnamed UniqueConstraint through Meta.constraints (not unique_together) used to
    autodetect a migration that recorded it under tracked state's unique_together option instead
    of constraints - AddConstraint.state_forward()'s own default guess for an unnamed
    UniqueConstraint. The live model's own state (built straight from Meta.constraints) never
    matched that, so every subsequent makemigrations run kept seeing a phantom Remove+Add pair
    for the same constraint, forever."""
    table = "rt_unnamed_unique_constraint"
    old_model = build_model("Record", table, {"a": CharField(max_length=10), "b": CharField(max_length=10)})
    new_model = build_model(
        "Record",
        table,
        {"a": CharField(max_length=10), "b": CharField(max_length=10)},
        meta_options={"constraints": [UniqueConstraint(fields=("a", "b"))]},
    )

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    assert round_trip.get_pending_operations(new_model) == []
    # A second makemigrations run must not propose dropping/re-adding the same constraint.
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_renaming_unique_and_partial_index_preserves_their_attributes(round_trip: RoundTrip) -> None:
    """RenameIndex.state_forward() used to rebuild the renamed entry as a plain
    Index(fields=..., name=new_name), discarding unique=True/PartialIndex's own condition -
    tracked state then permanently disagreed with the live model's (correctly unique/
    conditional) declaration, so every following makemigrations run kept proposing a phantom
    Remove+Add pair for both indexes, forever."""
    table = "rt_rename_unique_partial_index"
    old_model = build_model(
        "Record",
        table,
        {"name": CharField(max_length=20), "tag": CharField(max_length=20)},
        meta_options={
            "indexes": [
                Index(fields=("name",), unique=True, name="ux_rec_name"),
                PartialIndex(fields=("tag",), condition=RawSQLTerm("tag <> ''"), name="ix_rec_tag"),
            ]
        },
    )
    new_model = build_model(
        "Record",
        table,
        {"name": CharField(max_length=20), "tag": CharField(max_length=20)},
        meta_options={
            "indexes": [
                Index(fields=("name",), unique=True, name="ux_rec_name_v2"),
                PartialIndex(fields=("tag",), condition=RawSQLTerm("tag <> ''"), name="ix_rec_tag_v2"),
            ]
        },
    )

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    index_definitions = await round_trip.get_index_definitions(table)
    assert "ux_rec_name_v2" in index_definitions
    assert "ix_rec_tag_v2" in index_definitions
    assert "UNIQUE" in index_definitions["ux_rec_name_v2"].upper()
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_renaming_expression_based_index_does_not_treat_expression_as_a_field_name(
    round_trip: RoundTrip,
) -> None:
    """RenameIndex.state_forward() used to rebuild a renamed index via
    Index(fields=tuple(index.field_names), name=new_name) - for an expression-based index,
    field_names returns the raw SQL text of the expression (e.g. "((length(name)))"), which then
    got treated as an actual model field name by every downstream field-name resolution, crashing
    `migrate` with "Meta.indexes field '((length(name)))' is not a field on Rec"."""
    table = "rt_rename_expression_index"
    old_model = build_model(
        "Record",
        table,
        {"name": CharField(max_length=20)},
        meta_options={"indexes": [Index(RawSQLTerm("(length(name))"), name="ix_rec_len")]},
    )
    new_model = build_model(
        "Record",
        table,
        {"name": CharField(max_length=20)},
        meta_options={"indexes": [Index(RawSQLTerm("(length(name))"), name="ix_rec_len_v2")]},
    )

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    index_definitions = await round_trip.get_index_definitions(table)
    assert "ix_rec_len_v2" in index_definitions
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_table_description_change_combined_with_unnamed_unique_constraint_migrates_once(
    round_trip: RoundTrip,
) -> None:
    """Combining a Meta.table_description change with adding an unnamed Meta.constraints
    UniqueConstraint in the same edit used to crash `migrate` outright with a ConfigurationError
    about an unnamed UniqueConstraint duplicating a unique_together entry - AlterModelOptions
    carrying the full new options dict (fixed separately) duplicated the constraint into BOTH
    unique_together and constraints, then AddConstraint's own unique_together default guess (also
    fixed) added it there a second time, tripping the model's own unique_together/constraints
    collision guard."""
    table = "rt_options_and_unnamed_unique"
    old_model = build_model("Record", table, {"a": CharField(max_length=10), "b": CharField(max_length=10)})
    new_model = build_model(
        "Record",
        table,
        {"a": CharField(max_length=10), "b": CharField(max_length=10)},
        meta_options={
            "table_description": "a record",
            "constraints": [UniqueConstraint(fields=("a", "b"))],
        },
    )

    await round_trip.migrate_to(old_model)
    await round_trip.migrate_to(new_model)

    assert round_trip.get_pending_operations(new_model) == []


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_sqlite_check_constraint_rebuild_no_longer_silently_drops_partial_unique_constraint(
    round_trip: RoundTrip,
) -> None:
    """A conditional UniqueConstraint next to a CheckConstraint (whose addition rebuilds the
    whole table on SQLite) ends up as a real partial unique index on every database - the
    rebuild creates it as its own CREATE UNIQUE INDEX ... WHERE once the table is in place."""
    table = "rt_sqlite_check_plus_partial_unique"
    create_op = CreateModel(
        name="Item",
        fields=[
            ("id", IntField(primary_key=True)),
            ("status", CharField(max_length=20)),
            ("deleted_at", CharField(max_length=20, null=True)),
        ],
        options={
            "table": table,
            "constraints": [
                CheckConstraint(check=RawSQLTerm("id >= 0"), name="ck_item_id"),
                UniqueConstraint(
                    fields=("status",), name="uq_item_status", condition=RawSQLTerm("deleted_at IS NULL")
                ),
            ],
        },
    )

    await round_trip.apply([create_op])
    index_definitions = await round_trip.get_index_definitions(table)
    assert "uq_item_status" in index_definitions
    assert "WHERE" in index_definitions["uq_item_status"].upper()


@pytest.mark.asyncio
async def test_postgres_alter_field_type_change_recreates_dependent_generated_column(
    round_trip: RoundTrip,
) -> None:
    """Changing the type of a column a GeneratedField's own expression depends on used to crash
    `migrate` outright on Postgres with a raw asyncpg/rust_pg error ("cannot alter type of a
    column used by a generated column ...") - the shared Postgres/generic ALTER path had no
    handling for a dependent generated column at all, unlike RemoveField (which already detects
    and rejects this same dependency). It must now drop the dependent generated column first,
    apply the type change, and recreate the generated column afterward - the database recomputes
    every row's value from the new column type on its own."""
    if round_trip.dialect != "postgresql":
        pytest.skip("SQLite survives this via a full table rebuild - see B4/generated field tests")

    table = "rt_alter_type_dependent_generated"
    old_model = build_model(
        "Product",
        table,
        {
            "price": DecimalField(max_digits=10, decimal_places=2),
            "quantity": IntField(),
            "total": GeneratedField(
                expression=RawSQLTerm("price * quantity"), output_field=DecimalField(max_digits=12, decimal_places=2)
            ),
        },
    )
    new_model = build_model(
        "Product",
        table,
        {
            "price": DecimalField(max_digits=12, decimal_places=3),
            "quantity": IntField(),
            "total": GeneratedField(
                expression=RawSQLTerm("price * quantity"), output_field=DecimalField(max_digits=12, decimal_places=2)
            ),
        },
    )

    await round_trip.migrate_to(old_model)
    await round_trip.connection.execute_script(f'INSERT INTO "{table}" ("price", "quantity") VALUES (10.50, 3)')

    await round_trip.migrate_to(new_model)

    rows = await round_trip.connection.execute_dicts(f'SELECT "price", "total" FROM "{table}"')
    assert len(rows) == 1
    assert float(rows[0]["total"]) == pytest.approx(31.50)
    assert round_trip.get_pending_operations(new_model) == []


@pytest.mark.asyncio
async def test_foreign_key_to_a_model_with_one_to_one_primary_key_gets_its_column(round_trip: RoundTrip) -> None:
    """A FK whose target's primary key is a OneToOneField(primary_key=True) references that
    field's shadow column, so the FK column exists and stores the target pk."""
    account_model = build_model("Account", "rt_o2o_pk_account", {})
    profile_model = type(
        "Profile",
        (Model,),
        {
            "account": OneToOneField("models.Account", related_name="profile", primary_key=True),
            "Meta": type("Meta", (), {"table": "rt_o2o_pk_profile", "app": APP_LABEL}),
            "_no_comments": True,
        },
    )
    note_model = build_model(
        "Note", "rt_o2o_pk_note", {"profile": ForeignKeyField("models.Profile", related_name="notes")}
    )

    await round_trip.migrate_to(account_model, profile_model, note_model)

    connection = round_trip.connection
    await connection.execute_script('INSERT INTO "rt_o2o_pk_account" ("id") VALUES (1)')
    await connection.execute_script('INSERT INTO "rt_o2o_pk_profile" ("account_id") VALUES (1)')
    await connection.execute_script('INSERT INTO "rt_o2o_pk_note" ("id", "profile_id") VALUES (1, 1)')
    assert await connection.execute_dicts('SELECT "profile_id" FROM "rt_o2o_pk_note"') == [{"profile_id": 1}]
    assert round_trip.get_pending_operations(account_model, profile_model, note_model) == []


def build_composite_target_model() -> type[Model]:
    """A model keyed by a composite primary key, the target of the relations below."""
    attributes: dict[str, Any] = {
        "zone": IntField(),
        "number": IntField(),
        "pk": CompositePrimaryKey("zone", "number"),
        "Meta": type("Meta", (), {"table": "rt_rack", "app": APP_LABEL}),
        "_no_comments": True,
    }
    return type("Rack", (Model,), attributes)


async def get_column_nullability(round_trip: RoundTrip, table: str) -> dict[str, bool]:
    if round_trip.dialect == "sqlite":
        rows = await round_trip.connection.execute_dicts(f"PRAGMA table_info('{table}')")
        return {row["name"]: not row["notnull"] for row in rows}
    rows = await round_trip.connection.execute_dicts(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        f"WHERE table_name = '{table}' AND table_schema = current_schema()"
    )
    return {row["column_name"]: row["is_nullable"] == "YES" for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize(("old_null", "new_null"), [(False, True), (True, False)])
async def test_changing_null_on_a_relation_to_a_composite_key_alters_every_key_column(
    round_trip: RoundTrip, old_null: bool, new_null: bool
) -> None:
    table = "rt_box"
    target = build_composite_target_model()
    old_box = build_model("Box", table, {"rack": ForeignKeyField(f"{APP_LABEL}.Rack", null=old_null)})
    new_box = build_model("Box", table, {"rack": ForeignKeyField(f"{APP_LABEL}.Rack", null=new_null)})

    await round_trip.migrate_to(target, old_box)
    await round_trip.migrate_to(target, new_box)

    nullability = await get_column_nullability(round_trip, table)
    assert (nullability["rack_zone"], nullability["rack_number"]) == (new_null, new_null)
    assert round_trip.get_pending_operations(target, new_box) == []


@pytest.mark.asyncio
async def test_relation_to_a_composite_key_deconstructs_without_a_source_field(round_trip: RoundTrip) -> None:
    target = build_composite_target_model()
    box = build_model("Box", "rt_box", {"rack": ForeignKeyField(f"{APP_LABEL}.Rack", null=True)})
    await round_trip.migrate_to(target, box)
    initialized_field = round_trip.tracked_state.apps.get_model(f"{APP_LABEL}.Box")._meta.fields_map["rack"]
    _path, _args, kwargs = initialized_field.deconstruct()
    assert "source_field" not in kwargs
    # A migration written before spells the first key column as source_field - the same field.
    replayed_field = ForeignKeyField(
        f"{APP_LABEL}.Rack", null=True, source_field="rack_zone", to_field=("zone", "number")
    )
    replayed_field.model_field_name = "rack"
    assert StateSignatures.get_field_signature(replayed_field) == StateSignatures.get_field_signature(
        initialized_field
    )
    assert round_trip.get_pending_operations(target, box) == []


def build_initialized_live_state(*models: type[Model]) -> State:
    """The live state of `models` with their relations initialized, the way makemigrations sees them."""
    apps = StateApps()
    for model in models:
        apps.register_model(APP_LABEL, model)
    apps.init_relations()
    return build_live_state(*models)


async def get_unique_key_column_sets(round_trip: RoundTrip, table: str) -> list[tuple[str, ...]]:
    """The column sets of every unique index/constraint on `table`, primary key excluded."""
    if round_trip.dialect == "sqlite":
        indexes = await round_trip.connection.execute_dicts(f"PRAGMA index_list('{table}')")
        column_sets = []
        for index in indexes:
            if not index["unique"] or index["origin"] == "pk":
                continue
            columns = await round_trip.connection.execute_dicts(f"PRAGMA index_info('{index['name']}')")
            column_sets.append(tuple(column["name"] for column in columns))
        return column_sets
    rows = await round_trip.connection.execute_dicts(
        "SELECT array_agg(attribute.attname ORDER BY key_column.ordinality) AS columns "
        "FROM pg_index index_info "
        "JOIN pg_class table_class ON table_class.oid = index_info.indrelid "
        "CROSS JOIN LATERAL unnest(index_info.indkey) WITH ORDINALITY AS key_column(attnum, ordinality) "
        "JOIN pg_attribute attribute ON attribute.attrelid = table_class.oid AND attribute.attnum = key_column.attnum "
        f"WHERE table_class.relname = '{table}' AND index_info.indisunique AND NOT index_info.indisprimary "
        "GROUP BY index_info.indexrelid"
    )
    return [tuple(row["columns"]) for row in rows]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_renaming_a_composite_one_to_one_next_to_a_table_rebuild_keeps_one_unique_key(
    round_trip: RoundTrip,
) -> None:
    table = "rt_box"
    target = build_composite_target_model()
    old_box = build_model(
        "Box",
        table,
        {
            "spare": OneToOneField(f"{APP_LABEL}.Rack", null=True, related_name="spare_box"),
            "other": ForeignKeyField(f"{APP_LABEL}.Rack", null=True, related_name="other_boxes"),
        },
    )
    new_box = build_model(
        "Box", table, {"reserve": OneToOneField(f"{APP_LABEL}.Rack", null=True, related_name="spare_box")}
    )

    await round_trip.apply(
        OperationGenerator(round_trip.tracked_state, build_initialized_live_state(target, old_box)).generate()
    )
    new_state = build_initialized_live_state(build_composite_target_model(), new_box)
    operations = OperationGenerator(round_trip.tracked_state, new_state).generate()
    # makemigrations sees the unique key the one-to-one relation derives - it's moved along.
    assert {type(operation).__name__ for operation in operations} >= {"RenameField", "RemoveField", "AddConstraint"}
    await round_trip.apply(operations)

    unique_keys = await get_unique_key_column_sets(round_trip, table)
    assert sorted(unique_keys) == [("reserve_zone", "reserve_number")]
    assert OperationGenerator(round_trip.tracked_state, new_state).generate() == []


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_an_indexed_column_whose_name_needs_quoting_is_quoted(round_trip: RoundTrip) -> None:
    """A column name with a quote, a space or a semicolon was taken for an index expression and
    spliced into CREATE INDEX unquoted."""
    table = 'rt_odd"table'
    column = 'la"bel; DROP TABLE rt_odd --'
    model = build_model(
        "Odd",
        table,
        {"label": CharField(max_length=20, source_field=column)},
        {"indexes": (Index(fields=("label",)),)},
    )

    await round_trip.migrate_to(model)

    assert len(await round_trip.get_plain_index_names(table)) == 1
    assert round_trip.get_pending_operations(model) == []
    generator_sql = round_trip.connection.dialect.schema_editor_class(
        round_trip.connection
    ).index_statements.get_index_sql(model, [column], safe=True)
    assert '("la""bel; DROP TABLE rt_odd --")' in generator_sql


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_table_in_a_schema_is_renamed_inside_its_schema(round_trip: RoundTrip) -> None:
    """The new name was written schema-qualified, which RENAME TO doesn't take."""
    schema = "rt renamed schema"
    await round_trip.connection.execute_script(f'CREATE SCHEMA "{schema}"')
    try:
        fields = {"label": CharField(max_length=20, db_index=True)}
        await round_trip.migrate_to(build_model("InSchema", "rt_in_schema", fields, {"schema": schema}))
        await round_trip.connection.execute_script(
            f'INSERT INTO "{schema}"."rt_in_schema" (id, label) VALUES (1, \'a\')'
        )
        renamed = build_model("InSchema", "rt_in_schema_renamed", fields, {"schema": schema})

        operations = await round_trip.migrate_to(renamed)

        assert [type(operation).__name__ for operation in operations] == ["AlterModelTable"]
        rows = await round_trip.connection.execute_dicts(f'SELECT label FROM "{schema}"."rt_in_schema_renamed"')
        assert [row["label"] for row in rows] == ["a"]
        assert round_trip.get_pending_operations(renamed) == []
    finally:
        await round_trip.connection.execute_script(f'DROP SCHEMA "{schema}" CASCADE')
