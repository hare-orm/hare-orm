"""The index a foreign key (and an automatic many-to-many through table) gets by default: detected
for relations a migration file created without it, applied, rolled back, compared by drift and
reconstructed by inspectdb - against a real database of either dialect."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from hare.contrib.test import requires_features
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import PartialIndex
from hare.fields import CompositePrimaryKey, ForeignKeyField, ManyToManyField, OnDelete
from hare.fields.data.numeric import IntField
from hare.fields.relations.fields import RelationalField
from hare.inspectdb import ModelSourceGenerator
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.drift import detect_drift
from hare.migrations.operations import AddField, AddIndex, AlterField, CreateModel
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.query.expressions import Q
from tests.migrations.test_rename_relations_real_db import ReversibleRoundTrip
from tests.migrations.test_round_trip_real_db import APP_LABEL, build_live_state, build_model

ModelsFactory = Callable[[], tuple[type[Model], ...]]


def build_pair_model(table: str) -> type[Model]:
    """A live model with a composite primary key."""
    attributes: dict[str, Any] = {
        "left": IntField(),
        "right": IntField(),
        "pk": CompositePrimaryKey("left", "right"),
        "Meta": type("Meta", (), {"table": table, "app": APP_LABEL}),
        "_no_comments": True,
    }
    return type("Pair", (Model,), attributes)


def build_models(models_factory: ModelsFactory, legacy: bool = False) -> tuple[type[Model], ...]:
    """Builds one version of the models with their relations initialized, as app startup does.

    Args:
        models_factory: Builds the model classes.
        legacy: Constructs the relations the way a migration file written without ``db_index``
            declares them.

    Returns:
        The models.
    """
    if legacy:
        with RelationalField.replaying_migration_scope():
            models = models_factory()
    else:
        models = models_factory()
    apps = StateApps()
    for model in models:
        apps.register_model(APP_LABEL, model)
    apps.init_relations()
    return models


async def get_index_columns(round_trip: ReversibleRoundTrip, table: str) -> list[tuple[str, ...]]:
    """The column lists of every plain (non-unique) index on `table`."""
    if round_trip.dialect == "sqlite":
        index_rows = await round_trip.query(f"SELECT name FROM pragma_index_list('{table}') WHERE \"unique\" = 0")
        index_columns = []
        for index_row in index_rows:
            column_rows = await round_trip.query(
                f"SELECT name FROM pragma_index_info('{index_row['name']}') ORDER BY seqno"
            )
            index_columns.append(tuple(row["name"] for row in column_rows))
        return sorted(index_columns)
    rows = await round_trip.query(
        "SELECT index_class.relname AS name, attribute.attname AS column_name, keys.position "
        "FROM pg_index index_info "
        "JOIN pg_class index_class ON index_class.oid = index_info.indexrelid "
        "JOIN pg_class table_class ON table_class.oid = index_info.indrelid "
        "JOIN pg_namespace namespace ON namespace.oid = table_class.relnamespace "
        "CROSS JOIN LATERAL unnest(index_info.indkey) WITH ORDINALITY AS keys(attribute_number, position) "
        "JOIN pg_attribute attribute ON attribute.attrelid = table_class.oid "
        "AND attribute.attnum = keys.attribute_number "
        f"WHERE table_class.relname = '{table}' AND namespace.nspname = current_schema() "
        "AND NOT index_info.indisunique AND NOT index_info.indisprimary"
    )
    columns_by_index_name: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        columns_by_index_name.setdefault(row["name"], []).append((row["position"], row["column_name"]))
    return sorted(tuple(column for _position, column in sorted(columns)) for columns in columns_by_index_name.values())


async def get_drift_operation_types(round_trip: ReversibleRoundTrip, *models: type[Model]) -> list[type]:
    result = await detect_drift(round_trip.connection, build_live_state(*models), [APP_LABEL])
    return [type(operation) for operation in result.operations]


@pytest.fixture
def round_trip(db_isolated) -> ReversibleRoundTrip:
    return ReversibleRoundTrip(db_isolated.get_connection())


def build_parent_and_child(child_relation_kwargs: dict[str, Any] | None = None) -> tuple[type[Model], ...]:
    parent = build_model("Parent", "fkm_parent", {})
    child = build_model(
        "Child",
        "fkm_child",
        {"parent": ForeignKeyField("models.Parent", related_name="children", **(child_relation_kwargs or {}))},
    )
    return parent, child


@pytest.mark.asyncio
async def test_foreign_key_from_a_migration_without_db_index_gets_its_index_and_loses_it_on_rollback(
    round_trip: ReversibleRoundTrip,
) -> None:
    await round_trip.migrate_to(*build_models(build_parent_and_child, legacy=True))
    assert await get_index_columns(round_trip, "fkm_child") == []

    models = build_models(build_parent_and_child)
    assert await get_drift_operation_types(round_trip, *models) == [AlterField]
    operations = await round_trip.migrate_to(*models)

    assert [type(operation) for operation in operations] == [AlterField]
    assert await get_index_columns(round_trip, "fkm_child") == [("parent_id",)]
    assert round_trip.get_pending_operations(*models) == []
    assert await get_drift_operation_types(round_trip, *models) == []

    await round_trip.roll_back()

    assert await get_index_columns(round_trip, "fkm_child") == []


@pytest.mark.asyncio
async def test_foreign_key_with_db_index_false_generates_no_changes(round_trip: ReversibleRoundTrip) -> None:
    await round_trip.migrate_to(*build_models(build_parent_and_child, legacy=True))

    models = build_models(lambda: build_parent_and_child({"db_index": False}))

    assert round_trip.get_pending_operations(*models) == []
    assert await get_drift_operation_types(round_trip, *models) == []


@pytest.mark.asyncio
async def test_new_model_creates_the_foreign_key_index_and_drift_reports_it_missing(
    round_trip: ReversibleRoundTrip,
) -> None:
    def build_version() -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_new_parent", {})
        child = build_model(
            "Child",
            "fkm_new_child",
            {
                "parent": ForeignKeyField("models.Parent", related_name="children"),
                "loose": ForeignKeyField("models.Parent", related_name="loose_children", db_constraint=False),
            },
        )
        return parent, child

    models = build_models(build_version)
    await round_trip.migrate_to(*models)

    assert await get_index_columns(round_trip, "fkm_new_child") == [("loose_id",), ("parent_id",)]
    assert await get_drift_operation_types(round_trip, *models) == []

    index_names = await round_trip.get_plain_index_names("fkm_new_child")
    await round_trip.query(f'DROP INDEX "{index_names[0]}"')

    assert await get_drift_operation_types(round_trip, *models) == [AlterField]


@pytest.mark.asyncio
async def test_composite_foreign_key_gets_one_index_over_its_key_columns(round_trip: ReversibleRoundTrip) -> None:
    def build_version() -> tuple[type[Model], ...]:
        pair = build_pair_model("fkm_pair")
        child = build_model(
            "Child", "fkm_pair_child", {"pair": ForeignKeyField("models.Pair", related_name="children")}
        )
        return pair, child

    await round_trip.migrate_to(*build_models(build_version, legacy=True))
    assert await get_index_columns(round_trip, "fkm_pair_child") == []

    models = build_models(build_version)
    operations = await round_trip.migrate_to(*models)

    assert [type(operation) for operation in operations] == [AlterField]
    assert await get_index_columns(round_trip, "fkm_pair_child") == [("pair_left", "pair_right")]
    assert round_trip.get_pending_operations(*models) == []
    assert await get_drift_operation_types(round_trip, *models) == []

    await round_trip.roll_back()

    assert await get_index_columns(round_trip, "fkm_pair_child") == []


@pytest.mark.asyncio
async def test_drift_reports_a_missing_composite_foreign_key_index(round_trip: ReversibleRoundTrip) -> None:
    def build_version() -> tuple[type[Model], ...]:
        pair = build_pair_model("fkm_drift_pair")
        child = build_model(
            "Child", "fkm_drift_pair_child", {"pair": ForeignKeyField("models.Pair", related_name="children")}
        )
        return pair, child

    models = build_models(build_version)
    await round_trip.migrate_to(*models)
    assert await get_drift_operation_types(round_trip, *models) == []

    index_names = await round_trip.get_plain_index_names("fkm_drift_pair_child")
    await round_trip.query(f'DROP INDEX "{index_names[0]}"')

    assert await get_drift_operation_types(round_trip, *models) == [AlterField]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unique", "expected_index_columns"),
    [(True, [("parent_id",)]), (False, [("fkm_tagged_id",), ("parent_id",)])],
)
async def test_automatic_through_table_from_a_migration_without_db_index_gets_its_key_indexes(
    round_trip: ReversibleRoundTrip, unique: bool, expected_index_columns: list[tuple[str, ...]]
) -> None:
    def build_version() -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_tag_parent", {})
        tagged = build_model(
            "Tagged",
            "fkm_tagged",
            {"tags": ManyToManyField("models.Parent", related_name="tagged", through="fkm_tags", unique=unique)},
        )
        return parent, tagged

    legacy_models = build_models(build_version, legacy=True)
    await round_trip.migrate_to(*legacy_models)
    assert await get_index_columns(round_trip, "fkm_tags") == []
    assert await get_drift_operation_types(round_trip, *legacy_models) == []

    models = build_models(build_version)
    assert await get_drift_operation_types(round_trip, *models) == [AlterField]
    operations = await round_trip.migrate_to(*models)

    assert [type(operation) for operation in operations] == [AlterField]
    assert await get_index_columns(round_trip, "fkm_tags") == expected_index_columns
    assert round_trip.get_pending_operations(*models) == []
    assert await get_drift_operation_types(round_trip, *models) == []

    await round_trip.roll_back()

    assert await get_index_columns(round_trip, "fkm_tags") == []


@pytest.mark.asyncio
async def test_unique_together_leading_with_the_foreign_key_replaces_its_index(
    round_trip: ReversibleRoundTrip,
) -> None:
    def build_version(meta_options: dict[str, Any]) -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_cover_parent", {})
        child = build_model(
            "Child",
            "fkm_cover_child",
            {"parent": ForeignKeyField("models.Parent", related_name="children"), "code": IntField()},
            meta_options,
        )
        return parent, child

    await round_trip.migrate_to(*build_models(lambda: build_version({})))
    assert await get_index_columns(round_trip, "fkm_cover_child") == [("parent_id",)]

    covered_models = build_models(
        lambda: build_version({"constraints": [UniqueConstraint(fields=("parent", "code"))]})
    )
    await round_trip.migrate_to(*covered_models)

    assert await get_index_columns(round_trip, "fkm_cover_child") == []
    assert round_trip.get_pending_operations(*covered_models) == []
    assert await get_drift_operation_types(round_trip, *covered_models) == []

    await round_trip.roll_back()

    assert await get_index_columns(round_trip, "fkm_cover_child") == [("parent_id",)]


@pytest.mark.asyncio
async def test_unnamed_partial_index_on_the_foreign_key_column_takes_the_place_of_its_index(
    round_trip: ReversibleRoundTrip,
) -> None:
    def build_version() -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_partial_parent", {})
        child = build_model(
            "Child",
            "fkm_partial_child",
            {"parent": ForeignKeyField("models.Parent", related_name="children"), "code": IntField()},
            {"indexes": (PartialIndex(fields=("parent",), condition=Q(code=1)),)},
        )
        return parent, child

    models = build_models(build_version)
    await round_trip.migrate_to(*models)

    assert len(await round_trip.get_plain_index_names("fkm_partial_child")) == 1
    assert round_trip.get_pending_operations(*models) == []


@pytest.mark.asyncio
async def test_through_model_indexes_the_relation_its_unique_constraint_does_not_lead_with(
    round_trip: ReversibleRoundTrip,
) -> None:
    def build_version() -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_member_parent", {})
        group = build_model("Group", "fkm_member_group", {})
        membership = build_model(
            "Membership",
            "fkm_membership",
            {
                "owner": ForeignKeyField("models.Parent", related_name="memberships"),
                "group": ForeignKeyField("models.Group", related_name="memberships"),
            },
            {"constraints": (UniqueConstraint(fields=("owner", "group"), name="fkm_membership_unique"),)},
        )
        return parent, group, membership

    models = build_models(build_version)
    await round_trip.migrate_to(*models)

    assert await get_index_columns(round_trip, "fkm_membership") == [("group_id",)]
    assert await get_drift_operation_types(round_trip, *models) == []


@pytest.mark.asyncio
async def test_foreign_keys_in_a_cycle_are_added_back_with_their_indexes(round_trip: ReversibleRoundTrip) -> None:
    def build_version() -> tuple[type[Model], ...]:
        team = build_model(
            "Team", "fkm_team", {"captain": ForeignKeyField("models.Player", related_name="captained", null=True)}
        )
        player = build_model("Player", "fkm_player", {"team": ForeignKeyField("models.Team", related_name="players")})
        return team, player

    models = build_models(build_version)
    operations = OperationGenerator(State(models={}, apps=StateApps()), build_live_state(*models)).generate()

    deferred_operations = [operation for operation in operations if not isinstance(operation, CreateModel)]
    assert [type(operation) for operation in deferred_operations] == [AddField, AddIndex]
    await round_trip.apply(operations)

    assert await get_index_columns(round_trip, "fkm_team") == [("captain_id",)]
    assert await get_index_columns(round_trip, "fkm_player") == [("team_id",)]
    assert round_trip.get_pending_operations(*models) == []


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_inspectdb_reconstructs_foreign_keys_without_a_meta_index_entry(round_trip: ReversibleRoundTrip) -> None:
    def build_version() -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_inspect_parent", {})
        pair = build_pair_model("fkm_inspect_pair")
        child = build_model(
            "Child",
            "fkm_inspect_child",
            {
                "parent": ForeignKeyField("models.Parent", related_name="children"),
                "unindexed": ForeignKeyField("models.Parent", related_name="unindexed_children", db_index=False),
                "covered": ForeignKeyField("models.Parent", related_name="covered_children"),
                "code": IntField(),
                "pair": ForeignKeyField("models.Pair", related_name="children"),
                "unindexed_pair": ForeignKeyField("models.Pair", related_name="unindexed_children", db_index=False),
            },
            {"constraints": [UniqueConstraint(fields=("covered", "code"))]},
        )
        return parent, pair, child

    await round_trip.migrate_to(*build_models(build_version))

    connection = round_trip.connection
    table_info = await DatabaseCatalog.inspect_table(connection, "fkm_inspect_child")
    source = ModelSourceGenerator.generate_model_source(table_info, connection.dialect.name, app_label="models")

    field_lines = {line.strip().split(" = ")[0]: line.strip() for line in source.splitlines() if " = fields." in line}
    assert "db_index" not in field_lines["parent"]
    assert "db_index=False" in field_lines["unindexed"]
    assert "db_index" not in field_lines["covered"]
    assert "db_index" not in field_lines["pair"]
    assert "db_index=False" in field_lines["unindexed_pair"]
    assert "indexes" not in source


@pytest.mark.asyncio
async def test_through_table_keeps_one_set_of_key_indexes_when_on_delete_changes_too(
    round_trip: ReversibleRoundTrip,
) -> None:
    def build_version(relation_kwargs: dict[str, Any]) -> tuple[type[Model], ...]:
        parent = build_model("Parent", "fkm_ondelete_parent", {})
        tagged = build_model(
            "Tagged",
            "fkm_ondelete_tagged",
            {
                "tags": ManyToManyField(
                    "models.Parent", related_name="tagged", through="fkm_ondelete_tags", **relation_kwargs
                )
            },
        )
        return parent, tagged

    await round_trip.migrate_to(*build_models(lambda: build_version({"db_index": False})))

    models = build_models(lambda: build_version({"on_delete": OnDelete.RESTRICT}))
    await round_trip.migrate_to(*models)

    assert await get_index_columns(round_trip, "fkm_ondelete_tags") == [("parent_id",)]
    assert round_trip.get_pending_operations(*models) == []

    await round_trip.roll_back()

    assert await get_index_columns(round_trip, "fkm_ondelete_tags") == []
