"""PostgresqlTableOptions(partitioning=...) - declared, checked, created, changed by migrations,
read back by drift and inspectdb."""

import datetime
from typing import Any

import pytest

from hare.ddl.constraints import ExclusionConstraint, UniqueConstraint
from hare.ddl.indexes import Index, PartialIndex
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.partitioning import (
    DefaultPartition,
    HashPartitioning,
    ListPartition,
    ListPartitioning,
    RangeBound,
    RangePartition,
    RangePartitioning,
)
from hare.dialects.postgresql.partitioning.constants import HASH_PARTITION_COUNT_LIMIT
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.sqlite.constants import SQLITE_DIALECT
from hare.exceptions import ConfigurationError, IntegrityError, UnSupportedError
from hare.fields import CharField, DateField, ForeignKeyField, IntField
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.inspectdb import DatabaseCatalog, SchemaInspector
from hare.migrations.autodetection.diffs.state_model_diff import StateModelDiff
from hare.migrations.drift import detect_drift
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.migration import Migration
from hare.migrations.operations import AddPartition, AlterModelTable, RemovePartition
from hare.migrations.state.model_state import ModelState
from hare.migrations.writer import ImportManager, MigrationWriter
from hare.models import Model
from hare.query.expressions import Q
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.fake_client import FakeClient

HASH = HashPartitioning(fields=("tenant",), partition_count=3)
LIST = ListPartitioning(
    fields=("tenant",),
    partitions=[ListPartition("small", values=[1, 2]), ListPartition("big", values=[3])],
    default_partition="other",
)
RANGE = RangePartitioning(
    fields=("tenant",),
    partitions=[
        RangePartition("low", from_values=(RangeBound.MINVALUE,), to_values=(10,)),
        RangePartition("mid", from_values=(10,), to_values=(20,)),
    ],
    default_partition="rest",
)


def build_ledger(table_options: list[Any], **meta_options: Any) -> type[Model]:
    """A model with the composite key (tenant, id) and a plain code column."""
    return build_model(
        "PartitionedLedger",
        "partitioned_ledger",
        {
            "id": IntField(),
            "tenant": IntField(),
            "code": CharField(max_length=20, default=""),
            "pk": CompositePrimaryKey("tenant", "id"),
        },
        {"table_options": table_options, **meta_options},
    )


def get_postgresql_sql(model: type[Model]) -> str:
    return (
        POSTGRESQL_DIALECT.schema_editor_class(FakeClient("postgresql"))
        .table_creation.get_model_sql_data(model)
        .table_sql
    )


def test_hash_partitions_are_created_with_the_table():
    sql = get_postgresql_sql(
        build_ledger([PostgresqlTableOptions(partitioning=HASH, storage_parameters={"fillfactor": 70})])
    )

    assert ') PARTITION BY HASH ("tenant");' in sql
    for remainder in range(3):
        assert (
            f'CREATE TABLE "partitioned_ledger_p{remainder}" PARTITION OF "partitioned_ledger" '
            f"FOR VALUES WITH (MODULUS 3, REMAINDER {remainder}) WITH (fillfactor = 70);"
        ) in sql
    # A partitioned table holds no rows - the storage parameters are those of its partitions.
    assert sql.index("PARTITION BY HASH") < sql.index("WITH (fillfactor = 70)")
    assert sql.count("WITH (fillfactor = 70)") == 3


def test_list_partitions_are_created_with_the_table():
    sql = get_postgresql_sql(build_ledger([PostgresqlTableOptions(partitioning=LIST, tablespace="fast")]))

    assert ') PARTITION BY LIST ("tenant") TABLESPACE "fast";' in sql
    assert 'CREATE TABLE "partitioned_ledger_small" PARTITION OF "partitioned_ledger" FOR VALUES IN (1, 2)' in sql
    assert 'CREATE TABLE "partitioned_ledger_big" PARTITION OF "partitioned_ledger" FOR VALUES IN (3)' in sql
    assert (
        'CREATE TABLE "partitioned_ledger_other" PARTITION OF "partitioned_ledger" DEFAULT TABLESPACE "fast";' in sql
    )


def test_range_partitions_are_created_with_the_table():
    sql = get_postgresql_sql(build_ledger([PostgresqlTableOptions(partitioning=RANGE)]))

    assert ') PARTITION BY RANGE ("tenant");' in sql
    assert (
        'CREATE TABLE "partitioned_ledger_low" PARTITION OF "partitioned_ledger" FOR VALUES FROM (MINVALUE) TO (10);'
    ) in sql
    assert (
        'CREATE TABLE "partitioned_ledger_mid" PARTITION OF "partitioned_ledger" FOR VALUES FROM (10) TO (20);' in sql
    )
    assert 'CREATE TABLE "partitioned_ledger_rest" PARTITION OF "partitioned_ledger" DEFAULT;' in sql


def test_bound_values_are_written_by_the_key_field():
    model = build_model(
        "DatedEntry",
        "dated_entry",
        {
            "id": IntField(),
            "day": DateField(),
            "region": CharField(max_length=10),
            "pk": CompositePrimaryKey("region", "day", "id"),
        },
        {
            "table_options": [
                PostgresqlTableOptions(
                    partitioning=RangePartitioning(
                        fields=("region", "day"),
                        partitions=[
                            RangePartition(
                                "eu_2026",
                                from_values=("eu", datetime.date(2026, 1, 1)),
                                to_values=("eu", RangeBound.MAXVALUE),
                            )
                        ],
                    )
                )
            ]
        },
    )

    sql = get_postgresql_sql(model)

    assert 'PARTITION BY RANGE ("region", "day")' in sql
    assert "FOR VALUES FROM ('eu', '2026-01-01') TO ('eu', MAXVALUE);" in sql


def test_a_long_partition_table_name_stays_within_the_identifier_limit():
    model = build_model(
        "LongNamed",
        "a_table_with_a_name_long_enough_to_reach_the_limit_of_identifiers",
        {"id": IntField(), "tenant": IntField(), "pk": CompositePrimaryKey("tenant", "id")},
        {
            "table_options": [
                PostgresqlTableOptions(partitioning=HashPartitioning(fields=("tenant",), partition_count=2))
            ]
        },
    )

    sql = get_postgresql_sql(model)

    partition_names = [line.split('"')[1] for line in sql.splitlines() if "PARTITION OF" in line]
    assert len(partition_names) == len(set(partition_names)) == 2
    assert all(len(name.encode()) <= 63 for name in partition_names)


def test_sqlite_creates_a_plain_table_for_a_postgresql_partitioning():
    model = build_ledger([PostgresqlTableOptions(partitioning=HASH)])

    sql = SQLITE_DIALECT.schema_editor_class(FakeClient("sqlite")).table_creation.get_model_sql_data(model).table_sql

    assert "PARTITION" not in sql


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: HashPartitioning(fields=(), partition_count=2), "at least one key field"),
        (lambda: HashPartitioning(fields=("tenant", "tenant"), partition_count=2), "named twice"),
        (lambda: HashPartitioning(fields=("tenant",), partition_count=0), "partition_count must be a whole number"),
        (lambda: HashPartitioning(fields=("tenant",), partition_count=True), "partition_count must be a whole number"),
        (
            lambda: HashPartitioning(fields=("tenant",), partition_count=HASH_PARTITION_COUNT_LIMIT + 1),
            f"from 1 to {HASH_PARTITION_COUNT_LIMIT}",
        ),
        (lambda: ListPartition("bad name", values=[1]), "letters, digits and underscores"),
        (lambda: ListPartition("empty", values=[]), "at least one value"),
        (lambda: RangePartition("back", from_values=(5,), to_values=(5,)), "isn't below to_values"),
        (
            lambda: RangePartition("mixed", from_values=(RangeBound.MINVALUE, 1), to_values=(5, 5)),
            "every column after MINVALUE must be MINVALUE too",
        ),
        (
            lambda: ListPartitioning(fields=("tenant",), partitions=[RangePartition("x", (1,), (2,))]),
            "must be a ListPartition",
        ),
        (
            lambda: RangePartitioning(fields=("tenant",), partitions=[ListPartition("x", [1])]),
            "must be a RangePartition",
        ),
        (lambda: ListPartitioning(fields=("tenant",), default_partition="no way"), "letters, digits and underscores"),
        (lambda: PostgresqlTableOptions(partitioning="hash"), "partitioning must be a Partitioning"),
    ],
)
def test_a_wrong_declaration_is_refused(build, message):
    with pytest.raises(ConfigurationError, match=message):
        build()


@pytest.mark.parametrize(
    ("table_options", "meta_options", "message"),
    [
        (
            PostgresqlTableOptions(partitioning=HashPartitioning(fields=("missing",), partition_count=2)),
            {},
            "doesn't exist",
        ),
        (
            PostgresqlTableOptions(partitioning=HashPartitioning(fields=("code",), partition_count=2)),
            {},
            "the primary key doesn't include the partition key column",
        ),
        (
            PostgresqlTableOptions(partitioning=HASH),
            {"constraints": [UniqueConstraint(fields=("code",), name="uq_ledger_code")]},
            "the unique constraint 'uq_ledger_code' doesn't include the partition key column",
        ),
        (
            PostgresqlTableOptions(partitioning=HASH),
            {"indexes": [Index(fields=("code",), unique=True, name="uq_ledger_code_index")]},
            "the unique index 'uq_ledger_code_index' doesn't include the partition key column",
        ),
        (
            PostgresqlTableOptions(partitioning=HASH),
            {"constraints": [ExclusionConstraint(name="ex_ledger_code", expressions=(("code", "="),))]},
            "the exclusion constraint 'ex_ledger_code' doesn't include the partition key column",
        ),
        (PostgresqlTableOptions(partitioning=HASH, unlogged=True), {}, "can't be both unlogged and partitioned"),
        (
            PostgresqlTableOptions(
                partitioning=ListPartitioning(fields=("tenant", "id"), partitions=[ListPartition("one", [1])])
            ),
            {},
            "ListPartitioning takes a key of one column",
        ),
        (
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("tenant",), partitions=[ListPartition("one", [1, 2]), ListPartition("two", [2])]
                )
            ),
            {},
            "the value 2 is listed by the partitions 'one' and 'two'",
        ),
        (
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("tenant",), partitions=[ListPartition("one", [1])], default_partition="one"
                )
            ),
            {},
            "the partition name 'one' is used twice",
        ),
        (
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("tenant",),
                    partitions=[RangePartition("a", (0,), (10,)), RangePartition("b", (5,), (15,))],
                )
            ),
            {},
            "the RangePartitions 'a' and 'b' overlap",
        ),
        (
            PostgresqlTableOptions(
                partitioning=RangePartitioning(fields=("tenant",), partitions=[RangePartition("a", (0, 0), (10, 0))])
            ),
            {},
            "needs one value per key column",
        ),
        (
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("tenant",),
                    partitions=[RangePartition("a", (0,), (10,)), RangePartition("b", ("x",), ("y",))],
                )
            ),
            {},
            "must be values of one type",
        ),
    ],
)
def test_a_partitioning_that_doesnt_fit_the_model_is_refused(table_options, meta_options, message):
    model = build_ledger([table_options], **meta_options)

    with pytest.raises(ConfigurationError, match=message):
        get_postgresql_sql(model)


def test_unique_guarantees_including_the_key_are_accepted():
    model = build_ledger(
        [PostgresqlTableOptions(partitioning=HASH)],
        constraints=[UniqueConstraint(fields=("tenant", "code"), name="uq_ledger_tenant_code")],
        indexes=[Index(fields=("code", "tenant"), unique=True, name="uq_ledger_code_tenant_index")],
    )

    assert "PARTITION BY HASH" in get_postgresql_sql(model)


def test_partitioning_is_written_into_migrations():
    imports = ImportManager()

    rendered = MigrationWriter.render_value(
        (
            PostgresqlTableOptions(partitioning=HASH, storage_parameters={"fillfactor": 70}),
            PostgresqlTableOptions(partitioning=LIST),
            PostgresqlTableOptions(partitioning=RANGE),
        ),
        imports,
    )

    assert "partitioning=HashPartitioning(fields=['tenant'], partition_count=3)" in rendered
    assert (
        "ListPartitioning(fields=['tenant'], partitions=[ListPartition(name='big', values=[3]), "
        "ListPartition(name='small', values=[1, 2])], default_partition='other')"
    ) in rendered
    assert "RangePartition(name='low', from_values=[RangeBound.MINVALUE], to_values=[10])" in rendered
    assert "default_partition='rest'" in rendered


def test_partitions_compare_whatever_order_they_are_listed_in():
    reordered = ListPartitioning(
        fields=("tenant",),
        partitions=[ListPartition("big", values=[3]), ListPartition("small", values=[1, 2])],
        default_partition="other",
    )

    assert reordered == LIST
    assert hash(PostgresqlTableOptions(partitioning=reordered)) == hash(PostgresqlTableOptions(partitioning=LIST))


def get_model_operations(old_model: type[Model], new_model: type[Model]) -> tuple[list[Any], list[str]]:
    diff = StateModelDiff(
        ModelState.make_from_model(APP_LABEL, old_model), ModelState.make_from_model(APP_LABEL, new_model)
    )
    return diff.generate_operations(), diff.data_loss_warnings


def describe_operations(operations: list[Any]) -> list[tuple[str, str]]:
    return [
        (type(operation).__name__, operation.partition.name if hasattr(operation, "partition") else "")
        for operation in operations
    ]


def test_added_and_removed_partitions_get_their_own_operations():
    grown = ListPartitioning(
        fields=("tenant",),
        partitions=[ListPartition("small", values=[1, 2]), ListPartition("huge", values=[9])],
        default_partition="other",
    )

    operations, warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=LIST)]),
        build_ledger([PostgresqlTableOptions(partitioning=grown)]),
    )

    assert describe_operations(operations) == [("RemovePartition", "big"), ("AddPartition", "huge")]
    assert warnings == ["PartitionedLedger: the partition 'big' is removed with its rows"]


def test_a_changed_partition_is_removed_and_added_again():
    moved = ListPartitioning(
        fields=("tenant",),
        partitions=[ListPartition("small", values=[1, 2]), ListPartition("big", values=[3, 4])],
        default_partition="other",
    )

    operations, warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=LIST)]),
        build_ledger([PostgresqlTableOptions(partitioning=moved)]),
    )

    assert describe_operations(operations) == [("RemovePartition", "big"), ("AddPartition", "big")]
    assert warnings == [
        "PartitionedLedger: the partition 'big' is removed with its rows and added again empty - "
        "its definition changed"
    ]


def test_the_default_partition_is_added_and_removed_like_any_other():
    without_default = ListPartitioning(fields=("tenant",), partitions=LIST.partitions)

    added, _warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=without_default)]),
        build_ledger([PostgresqlTableOptions(partitioning=LIST)]),
    )
    removed, _warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=LIST)]),
        build_ledger([PostgresqlTableOptions(partitioning=without_default)]),
    )

    assert describe_operations(added) == [("AddPartition", "other")]
    assert isinstance(added[0].partition, DefaultPartition)
    assert describe_operations(removed) == [("RemovePartition", "other")]


def test_other_option_changes_stay_in_alter_model_options_beside_the_partition_operations():
    grown = RangePartitioning(
        fields=("tenant",),
        partitions=[*RANGE.partitions, RangePartition("high", from_values=(20,), to_values=(30,))],
        default_partition="rest",
    )

    operations, _warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=RANGE)]),
        build_ledger([PostgresqlTableOptions(partitioning=grown, storage_parameters={"fillfactor": 80})]),
    )

    assert describe_operations(operations) == [("AlterModelOptions", ""), ("AddPartition", "high")]
    (altered_options,) = operations[0].options["table_options"]
    assert altered_options == PostgresqlTableOptions(partitioning=RANGE, storage_parameters={"fillfactor": 80})


@pytest.mark.parametrize(
    ("old_partitioning", "new_partitioning"),
    [
        pytest.param(None, HASH, id="partitioning_added"),
        pytest.param(HASH, None, id="partitioning_removed"),
        pytest.param(HASH, HashPartitioning(fields=("tenant",), partition_count=5), id="hash_count_changed"),
        pytest.param(HASH, LIST, id="strategy_changed"),
        pytest.param(LIST, ListPartitioning(fields=("id",), partitions=LIST.partitions), id="key_changed"),
    ],
)
def test_a_change_of_how_the_table_is_partitioned_is_one_alter_model_options(old_partitioning, new_partitioning):
    operations, warnings = get_model_operations(
        build_ledger([PostgresqlTableOptions(partitioning=old_partitioning)] if old_partitioning else []),
        build_ledger([PostgresqlTableOptions(partitioning=new_partitioning)] if new_partitioning else []),
    )

    assert describe_operations(operations) == [("AlterModelOptions", "")]
    assert warnings == []


def test_partition_operations_change_the_state():
    state = build_live_state(build_ledger([PostgresqlTableOptions(partitioning=LIST)]))
    huge = ListPartition("huge", values=[9])

    AddPartition(model_name="PartitionedLedger", partition=huge).state_forward(APP_LABEL, state)
    RemovePartition(model_name="PartitionedLedger", partition=DefaultPartition("other")).state_forward(
        APP_LABEL, state
    )

    (options,) = state.models[(APP_LABEL, "PartitionedLedger")].options["table_options"]
    assert options.partitioning == ListPartitioning(fields=("tenant",), partitions=[*LIST.partitions, huge])
    with pytest.raises(IncompatibleStateError, match="already has a partition 'huge'"):
        AddPartition(model_name="PartitionedLedger", partition=huge).state_forward(APP_LABEL, state)
    with pytest.raises(IncompatibleStateError, match="has no partition 'gone'"):
        RemovePartition(model_name="PartitionedLedger", partition=ListPartition("gone", [8])).state_forward(
            APP_LABEL, state
        )
    plain_state = build_live_state(build_ledger([]))
    with pytest.raises(IncompatibleStateError, match="no Meta.table_options of the postgresql dialect"):
        AddPartition(model_name="PartitionedLedger", partition=huge).state_forward(APP_LABEL, plain_state)


def test_partition_operations_are_written_into_migrations():
    imports = ImportManager()
    operation = AddPartition(
        model_name="PartitionedLedger", partition=RangePartition("high", from_values=(20,), to_values=(30,))
    )

    path, args, kwargs = operation.deconstruct()

    assert (path, args) == ("hare.migrations.operations.AddPartition", [])
    assert MigrationWriter.render_value(kwargs["partition"], imports) == (
        "RangePartition(name='high', from_values=[20], to_values=[30])"
    )
    assert "with its rows" in RemovePartition(model_name="PartitionedLedger", partition=operation.partition).describe()


@pytest.mark.asyncio
async def test_a_dialect_without_partitions_refuses_to_add_one(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect == "postgresql":
        pytest.skip("PostgreSQL has partitions")
    model = build_ledger([])

    with pytest.raises(UnSupportedError, match="can't add a partition"):
        await round_trip.editor.table_partitions.add_partition(model, ListPartition("one", [1]))
    with pytest.raises(UnSupportedError, match="can't remove a partition"):
        await round_trip.editor.table_partitions.remove_partition(model, ListPartition("one", [1]))


@pytest.mark.asyncio
async def test_another_dialect_migrates_a_partitioned_model_as_a_plain_table(db_isolated_no_schema):
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    if round_trip.dialect == "postgresql":
        pytest.skip("The table is partitioned on PostgreSQL")
    grown = ListPartitioning(fields=("tenant",), partitions=[*LIST.partitions, ListPartition("huge", values=[9])])

    await round_trip.migrate_to(build_ledger([PostgresqlTableOptions(partitioning=LIST)]))
    operations = await round_trip.migrate_to(build_ledger([PostgresqlTableOptions(partitioning=grown)]))

    # The state follows the partitions; the table of this database has none.
    assert describe_operations(operations) == [("RemovePartition", "other"), ("AddPartition", "huge")]
    assert round_trip.get_pending_operations(build_ledger([PostgresqlTableOptions(partitioning=grown)])) == []


def get_postgresql_round_trip(context) -> RoundTrip:
    round_trip = RoundTrip(context.get_connection())
    if round_trip.dialect != "postgresql":
        pytest.skip("PostgreSQL's partitioned tables")
    return round_trip


async def get_partitions(round_trip: RoundTrip, table: str = "partitioned_ledger") -> dict[str, tuple[str, list[str]]]:
    """Each partition table of ``table`` with its bound and storage parameters."""
    rows = await round_trip.connection.execute_dicts(
        "SELECT child.relname AS name, pg_get_expr(child.relpartbound, child.oid) AS bound, "
        "child.reloptions AS options "
        "FROM pg_inherits inh JOIN pg_class child ON child.oid = inh.inhrelid "
        f"WHERE inh.inhparent = '{table}'::regclass ORDER BY child.relname"
    )
    return {row["name"]: (row["bound"], sorted(row["options"] or [])) for row in rows}


async def get_rows(round_trip: RoundTrip, table: str = "partitioned_ledger") -> list[tuple[int, int, str]]:
    rows = await round_trip.connection.execute_dicts(f'SELECT tenant, id, code FROM "{table}" ORDER BY tenant, id')
    return [(row["tenant"], row["id"], row["code"]) for row in rows]


async def insert_rows(round_trip: RoundTrip, tenants: list[int], table: str = "partitioned_ledger") -> None:
    values = ", ".join(f"({tenant}, {tenant * 100}, 't{tenant}')" for tenant in tenants)
    await round_trip.connection.execute_script(f'INSERT INTO "{table}" (tenant, id, code) VALUES {values}')


async def get_drift_operations(round_trip: RoundTrip, model: type[Model]) -> list[str]:
    drift = await detect_drift(round_trip.connection, build_live_state(model), [APP_LABEL])
    return [type(operation).__name__ for operation in drift.operations]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("partitioning", "expected_partitions", "tenant_partitions"),
    [
        pytest.param(
            HASH,
            {
                "partitioned_ledger_p0": "FOR VALUES WITH (modulus 3, remainder 0)",
                "partitioned_ledger_p1": "FOR VALUES WITH (modulus 3, remainder 1)",
                "partitioned_ledger_p2": "FOR VALUES WITH (modulus 3, remainder 2)",
            },
            None,
            id="hash",
        ),
        pytest.param(
            LIST,
            {
                "partitioned_ledger_big": "FOR VALUES IN (3)",
                "partitioned_ledger_other": "DEFAULT",
                "partitioned_ledger_small": "FOR VALUES IN (1, 2)",
            },
            {1: "partitioned_ledger_small", 3: "partitioned_ledger_big", 7: "partitioned_ledger_other"},
            id="list",
        ),
        pytest.param(
            RANGE,
            {
                "partitioned_ledger_low": "FOR VALUES FROM (MINVALUE) TO (10)",
                "partitioned_ledger_mid": "FOR VALUES FROM (10) TO (20)",
                "partitioned_ledger_rest": "DEFAULT",
            },
            {1: "partitioned_ledger_low", 10: "partitioned_ledger_mid", 25: "partitioned_ledger_rest"},
            id="range",
        ),
    ],
)
async def test_each_strategy_is_created_read_back_and_round_trips(
    db_isolated_no_schema, partitioning, expected_partitions, tenant_partitions
):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    model = build_ledger([PostgresqlTableOptions(partitioning=partitioning, storage_parameters={"fillfactor": 70})])

    await round_trip.migrate_to(model)

    assert await get_partitions(round_trip) == {
        name: (bound, ["fillfactor=70"]) for name, bound in expected_partitions.items()
    }
    for tenant, partition_table in (tenant_partitions or {}).items():
        await insert_rows(round_trip, [tenant])
        assert await get_rows(round_trip, partition_table) == [(tenant, tenant * 100, f"t{tenant}")]
    assert round_trip.get_pending_operations(model) == []
    assert await get_drift_operations(round_trip, model) == []
    # The partitions aren't tables of their own for hare.
    assert "partitioned_ledger" in await DatabaseCatalog.get_table_names(round_trip.connection)
    assert not any(
        name in expected_partitions for name in await DatabaseCatalog.get_table_names(round_trip.connection)
    )


@pytest.mark.asyncio
async def test_partitions_are_added_and_removed_by_migrations_and_back(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    options = PostgresqlTableOptions(partitioning=RANGE, storage_parameters={"fillfactor": 70})
    await round_trip.migrate_to(build_ledger([options]))
    await insert_rows(round_trip, [1, 15, 25])
    grown = RangePartitioning(
        fields=("tenant",),
        partitions=[RANGE.partitions[0], RangePartition("high", from_values=(30,), to_values=(40,))],
        default_partition="rest",
    )
    state_before = round_trip.tracked_state.clone()

    operations = await round_trip.migrate_to(
        build_ledger([PostgresqlTableOptions(partitioning=grown, storage_parameters={"fillfactor": 70})])
    )

    assert describe_operations(operations) == [("RemovePartition", "mid"), ("AddPartition", "high")]
    assert await get_partitions(round_trip) == {
        "partitioned_ledger_high": ("FOR VALUES FROM (30) TO (40)", ["fillfactor=70"]),
        "partitioned_ledger_low": ("FOR VALUES FROM (MINVALUE) TO (10)", ["fillfactor=70"]),
        "partitioned_ledger_rest": ("DEFAULT", ["fillfactor=70"]),
    }
    # The removed partition went with its rows.
    assert await get_rows(round_trip) == [(1, 100, "t1"), (25, 2500, "t25")]

    migration = Migration(name="0002_step", app_label=APP_LABEL)
    migration.operations = list(operations)
    await migration.unapply(state_before, schema_editor=round_trip.editor)

    assert sorted(await get_partitions(round_trip)) == [
        "partitioned_ledger_low",
        "partitioned_ledger_mid",
        "partitioned_ledger_rest",
    ]
    assert await get_drift_operations(round_trip, build_ledger([options])) == []


@pytest.mark.asyncio
async def test_storage_parameters_and_partitions_change_together_in_place(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    await round_trip.migrate_to(
        build_ledger([PostgresqlTableOptions(partitioning=LIST, storage_parameters={"fillfactor": 70})])
    )
    await insert_rows(round_trip, [1, 3])
    grown = ListPartitioning(
        fields=("tenant",), partitions=[*LIST.partitions, ListPartition("huge", values=[9])], default_partition="other"
    )
    changed = build_ledger(
        [PostgresqlTableOptions(partitioning=grown, storage_parameters={"autovacuum_enabled": False})]
    )

    operations = await round_trip.migrate_to(changed)

    assert describe_operations(operations) == [("AlterModelOptions", ""), ("AddPartition", "huge")]
    partitions = await get_partitions(round_trip)
    assert {name: options for name, (_bound, options) in partitions.items()} == dict.fromkeys(
        [
            "partitioned_ledger_big",
            "partitioned_ledger_huge",
            "partitioned_ledger_other",
            "partitioned_ledger_small",
        ],
        ["autovacuum_enabled=false"],
    )
    assert await get_rows(round_trip) == [(1, 100, "t1"), (3, 300, "t3")]
    assert await get_drift_operations(round_trip, changed) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_partitioning", "new_partitioning", "expected_partition_count"),
    [
        pytest.param(None, HASH, 3, id="partitioning_added"),
        pytest.param(HASH, None, 0, id="partitioning_removed"),
        pytest.param(HASH, HashPartitioning(fields=("tenant",), partition_count=5), 5, id="hash_count_changed"),
        pytest.param(HASH, LIST, 3, id="strategy_changed"),
        pytest.param(
            LIST, ListPartitioning(fields=("id",), partitions=[], default_partition="every"), 1, id="key_changed"
        ),
    ],
)
async def test_a_change_of_how_the_table_is_partitioned_keeps_the_rows_and_everything_around(
    db_isolated_no_schema, old_partitioning, new_partitioning, expected_partition_count
):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    owner = build_model("LedgerOwner", "ledger_owner", {"name": CharField(max_length=20, default="")})

    def build(partitioning):
        ledger = build_ledger(
            [PostgresqlTableOptions(partitioning=partitioning)] if partitioning else [],
            indexes=[Index(fields=("code",), name="idx_ledger_code")],
            constraints=[UniqueConstraint(fields=("tenant", "id", "code"), name="uq_ledger_tenant_code")],
        )
        entry = build_model(
            "LedgerEntry",
            "ledger_entry",
            {"ledger": ForeignKeyField(f"{APP_LABEL}.PartitionedLedger", related_name="entries")},
        )
        return ledger, entry

    old_ledger, old_entry = build(old_partitioning)
    await round_trip.migrate_to(owner, old_ledger, old_entry)
    await insert_rows(round_trip, [1, 2, 3])
    await round_trip.connection.execute_script("INSERT INTO ledger_entry (ledger_tenant, ledger_id) VALUES (2, 200)")
    new_ledger, new_entry = build(new_partitioning)

    operations = await round_trip.migrate_to(owner, new_ledger, new_entry)

    assert describe_operations(operations) == [("AlterModelOptions", "")]
    assert len(await get_partitions(round_trip)) == expected_partition_count
    assert await get_rows(round_trip) == [(1, 100, "t1"), (2, 200, "t2"), (3, 300, "t3")]
    indexes = await round_trip.get_index_definitions("partitioned_ledger")
    assert {"idx_ledger_code", "uq_ledger_tenant_code"} <= set(indexes)
    # The foreign key of the other table references the new table - and still holds.
    (foreign_keys,) = await round_trip.connection.execute_dicts(
        "SELECT COUNT(*) AS count FROM pg_constraint WHERE contype = 'f' AND conparentid = 0 "
        "AND conrelid = 'ledger_entry'::regclass AND confrelid = 'partitioned_ledger'::regclass"
    )
    assert foreign_keys["count"] == 1
    with pytest.raises(IntegrityError):
        await round_trip.connection.execute_script(
            "INSERT INTO ledger_entry (ledger_tenant, ledger_id) VALUES (9, 900)"
        )
    assert await get_drift_operations(round_trip, new_ledger) == []
    assert round_trip.get_pending_operations(owner, new_ledger, new_entry) == []


@pytest.mark.asyncio
async def test_recreating_a_table_moves_its_key_sequence_past_the_rows(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)

    def build(partitioning):
        return build_model(
            "CountedRow",
            "counted_row",
            {"name": CharField(max_length=20, default="")},
            {"table_options": [PostgresqlTableOptions(partitioning=partitioning)] if partitioning else []},
        )

    await round_trip.migrate_to(build(None))
    await round_trip.connection.execute_script("INSERT INTO counted_row (name) VALUES ('a'), ('b'), ('c')")

    await round_trip.migrate_to(build(HashPartitioning(fields=("id",), partition_count=2)))
    await round_trip.connection.execute_script("INSERT INTO counted_row (name) VALUES ('d')")

    rows = await round_trip.connection.execute_dicts("SELECT id, name FROM counted_row ORDER BY id")
    assert [(row["id"], row["name"]) for row in rows] == [(1, "a"), (2, "b"), (3, "c"), (4, "d")]


@pytest.mark.asyncio
async def test_renaming_the_table_renames_its_partitions(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    options = [PostgresqlTableOptions(partitioning=LIST)]
    await round_trip.migrate_to(build_ledger(options))
    await insert_rows(round_trip, [1, 3, 7])
    renamed = build_model(
        "PartitionedLedger",
        "renamed_ledger",
        {
            "id": IntField(),
            "tenant": IntField(),
            "code": CharField(max_length=20, default=""),
            "pk": CompositePrimaryKey("tenant", "id"),
        },
        {"table_options": options},
    )

    operations = await round_trip.migrate_to(renamed)

    assert [type(operation) for operation in operations] == [AlterModelTable]
    assert sorted(await get_partitions(round_trip, "renamed_ledger")) == [
        "renamed_ledger_big",
        "renamed_ledger_other",
        "renamed_ledger_small",
    ]
    assert await get_rows(round_trip, "renamed_ledger") == [(1, 100, "t1"), (3, 300, "t3"), (7, 700, "t7")]
    assert await get_drift_operations(round_trip, renamed) == []


@pytest.mark.asyncio
async def test_a_partitioned_table_in_a_schema_is_created_recreated_and_renamed(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    schema = "partitioned schema"
    await round_trip.connection.execute_script(f'CREATE SCHEMA "{schema}"')

    def build_in_schema(table: str, partitioning: Any) -> type[Model]:
        return build_model(
            "PartitionedLedger",
            table,
            {
                "id": IntField(),
                "tenant": IntField(),
                "code": CharField(max_length=20, default=""),
                "pk": CompositePrimaryKey("tenant", "id"),
            },
            {"table_options": [PostgresqlTableOptions(partitioning=partitioning)], "schema": schema},
        )

    async def get_partition_schemas(table: str) -> dict[str, str]:
        rows = await round_trip.connection.execute_dicts(
            "SELECT child.relname AS name, namespace.nspname AS schema FROM pg_inherits inh "
            "JOIN pg_class child ON child.oid = inh.inhrelid "
            "JOIN pg_namespace namespace ON namespace.oid = child.relnamespace "
            f"JOIN pg_class parent ON parent.oid = inh.inhparent WHERE parent.relname = '{table}'"
        )
        return {row["name"]: row["schema"] for row in rows}

    try:
        hashed = build_in_schema("schema_ledger", HASH)
        await round_trip.migrate_to(hashed)
        assert await get_partition_schemas("schema_ledger") == dict.fromkeys(
            ("schema_ledger_p0", "schema_ledger_p1", "schema_ledger_p2"), schema
        )
        assert await get_drift_operations(round_trip, hashed) == []
        await round_trip.connection.execute_script(
            f"INSERT INTO \"{schema}\".\"schema_ledger\" (tenant, id, code) VALUES (1, 100, 't1'), (7, 700, 't7')"
        )

        listed = build_in_schema("schema_ledger", LIST)
        await round_trip.migrate_to(listed)
        assert await get_partition_schemas("schema_ledger") == dict.fromkeys(
            ("schema_ledger_small", "schema_ledger_big", "schema_ledger_other"), schema
        )
        assert await get_drift_operations(round_trip, listed) == []

        renamed = build_in_schema("schema_ledger_renamed", LIST)
        operations = await round_trip.migrate_to(renamed)
        assert [type(operation) for operation in operations] == [AlterModelTable]
        assert await get_partition_schemas("schema_ledger_renamed") == dict.fromkeys(
            ("schema_ledger_renamed_small", "schema_ledger_renamed_big", "schema_ledger_renamed_other"), schema
        )
        rows = await round_trip.connection.execute_dicts(
            f'SELECT tenant, id FROM "{schema}"."schema_ledger_renamed" ORDER BY tenant'
        )
        assert [(row["tenant"], row["id"]) for row in rows] == [(1, 100), (7, 700)]
        assert await get_drift_operations(round_trip, renamed) == []
    finally:
        await round_trip.connection.execute_script(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.asyncio
async def test_indexes_of_a_partitioned_table_plain_partial_and_concurrent(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    options = [PostgresqlTableOptions(partitioning=HASH)]
    await round_trip.migrate_to(build_ledger(options))
    await insert_rows(round_trip, [1, 2, 3, 4])
    indexed = build_ledger(
        options,
        indexes=[
            Index(fields=("code",), name="idx_ledger_code"),
            PartialIndex(fields=("code",), name="idx_ledger_code_partial", condition=Q(tenant=1)),
        ],
    )

    await round_trip.migrate_to(indexed)

    async def get_valid_index_names() -> dict[str, bool]:
        rows = await round_trip.connection.execute_dicts(
            "SELECT ic.relname AS name, i.indisvalid AS valid FROM pg_index i "
            "JOIN pg_class ic ON ic.oid = i.indexrelid WHERE i.indrelid = 'partitioned_ledger'::regclass"
        )
        return {row["name"]: row["valid"] for row in rows}

    assert {"idx_ledger_code": True, "idx_ledger_code_partial": True}.items() <= (
        await get_valid_index_names()
    ).items()

    # CONCURRENTLY can't run in the transaction of an atomic migration.
    concurrent_editor = round_trip.connection.dialect.schema_editor_class(round_trip.connection, atomic=False)
    concurrent_index = Index(fields=("id",), name="idx_ledger_id_concurrent")
    await concurrent_editor.add_index(indexed, concurrent_index, concurrently=True)

    assert (await get_valid_index_names())["idx_ledger_id_concurrent"] is True
    partition_indexes = await round_trip.connection.execute_dicts(
        "SELECT tablename, indexname FROM pg_indexes WHERE indexname LIKE '%idx_ledger_id_concurrent' "
        "ORDER BY tablename"
    )
    assert [row["tablename"] for row in partition_indexes] == [
        "partitioned_ledger",
        "partitioned_ledger_p0",
        "partitioned_ledger_p1",
        "partitioned_ledger_p2",
    ]

    await concurrent_editor.remove_index(indexed, concurrent_index, concurrently=True)
    assert "idx_ledger_id_concurrent" not in await get_valid_index_names()
    assert round_trip.get_pending_operations(indexed) == []


@pytest.mark.asyncio
async def test_drift_reports_what_was_changed_by_hand(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    model = build_ledger([PostgresqlTableOptions(partitioning=LIST, storage_parameters={"fillfactor": 70})])
    await round_trip.migrate_to(model)
    assert await get_drift_operations(round_trip, model) == []

    await round_trip.connection.execute_script("ALTER TABLE partitioned_ledger_big SET (fillfactor = 90)")
    assert await get_drift_operations(round_trip, model) == ["AlterModelOptions"]
    await round_trip.connection.execute_script("ALTER TABLE partitioned_ledger_big SET (fillfactor = 70)")
    assert await get_drift_operations(round_trip, model) == []

    await round_trip.connection.execute_script(
        "CREATE TABLE partitioned_ledger_extra PARTITION OF partitioned_ledger "
        "FOR VALUES IN (50) WITH (fillfactor = 70)"
    )
    # The operations lead from the database to the declaration: the extra partition goes.
    assert await get_drift_operations(round_trip, model) == ["RemovePartition"]
    await round_trip.connection.execute_script("DROP TABLE partitioned_ledger_extra")

    await round_trip.connection.execute_script("DROP TABLE partitioned_ledger_other")
    assert await get_drift_operations(round_trip, model) == ["AddPartition"]


@pytest.mark.asyncio
async def test_drift_reports_a_table_that_should_be_partitioned(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    await round_trip.migrate_to(build_ledger([]))

    assert await get_drift_operations(round_trip, build_ledger([PostgresqlTableOptions(partitioning=HASH)])) == [
        "AlterModelOptions"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("partitioning", [HASH, LIST, RANGE], ids=["hash", "list", "range"])
async def test_inspectdb_writes_the_partitioning_and_it_round_trips(db_isolated_no_schema, partitioning):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    options = PostgresqlTableOptions(partitioning=partitioning, storage_parameters={"fillfactor": 70})
    await round_trip.migrate_to(build_ledger([options]))

    source = await SchemaInspector.inspect(round_trip.connection)

    assert "class PartitionedLedger" in source
    assert "PartitionedLedgerP0" not in source and "PartitionedLedgerSmall" not in source
    namespace: dict[str, Any] = {}
    exec(source.replace('app = "models"', 'app = "inspected"'), namespace)  # noqa: S102
    (inspected_options,) = namespace["PartitionedLedger"]._meta.table_options
    assert inspected_options == options


def test_an_exclusion_constraint_on_a_partitioned_table_needs_a_server_that_has_them():
    model = build_ledger(
        [PostgresqlTableOptions(partitioning=HASH)],
        constraints=[ExclusionConstraint(name="ex_ledger_tenant", expressions=(("tenant", "="), ("id", "=")))],
    )
    old_server = FakeClient("postgresql")
    old_server.features = old_server.features.replace(supports_partitioned_exclusion_constraints=False)
    new_server = FakeClient("postgresql")
    new_server.features = new_server.features.replace(supports_partitioned_exclusion_constraints=True)

    with pytest.raises(UnSupportedError, match="from PostgreSQL 17 on"):
        POSTGRESQL_DIALECT.schema_editor_class(old_server).table_creation.get_model_sql_data(model)
    assert (
        "PARTITION BY HASH"
        in POSTGRESQL_DIALECT.schema_editor_class(new_server).table_creation.get_model_sql_data(model).table_sql
    )


@pytest.mark.asyncio
async def test_storage_and_tablespace_changes_go_to_every_partition():
    editor = POSTGRESQL_DIALECT.schema_editor_class(FakeClient("postgresql"), collect_sql=True)
    old_options = PostgresqlTableOptions(
        partitioning=HASH, storage_parameters={"fillfactor": 70, "autovacuum_enabled": False}
    )
    new_options = PostgresqlTableOptions(partitioning=HASH, storage_parameters={"fillfactor": 80}, tablespace="fast")

    await editor.table_partitions.alter_table_options(build_ledger([new_options]), old_options, new_options)

    partitions = [f'"partitioned_ledger_p{remainder}"' for remainder in range(3)]
    assert editor.collected_sql == [
        # The table's own tablespace is the one its new partitions are created in.
        *(f'ALTER TABLE {table} SET TABLESPACE "fast"' for table in ['"partitioned_ledger"', *partitions]),
        *(
            statement
            for partition in partitions
            for statement in (
                f"ALTER TABLE {partition} RESET (autovacuum_enabled)",
                f"ALTER TABLE {partition} SET (fillfactor = 80)",
            )
        ),
    ]


@pytest.mark.asyncio
async def test_a_storage_parameter_the_partitions_dont_share_is_read_as_such(db_isolated_no_schema):
    round_trip = get_postgresql_round_trip(db_isolated_no_schema)
    await round_trip.migrate_to(
        build_ledger([PostgresqlTableOptions(partitioning=HASH, storage_parameters={"fillfactor": 70})])
    )
    await round_trip.connection.execute_script("ALTER TABLE partitioned_ledger_p1 SET (fillfactor = 90)")
    await round_trip.connection.execute_script("ALTER TABLE partitioned_ledger_p2 RESET (fillfactor)")

    (table_info,) = await DatabaseCatalog.inspect_tables(round_trip.connection, ["partitioned_ledger"])

    assert table_info.table_options.storage_parameters == {
        "fillfactor": (
            "differs between partitions: partitioned_ledger_p0=70, partitioned_ledger_p1=90, "
            "partitioned_ledger_p2=(not set)"
        )
    }
    assert table_info.table_options.partitioning == HASH
