"""inspectdb keeping what a model can declare about a table - relations across schemas,
partitioned tables, index details, constraint names and wording - and flagging the rest."""

import pytest

from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.ddl.constraints import UniqueConstraint
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.inspectdb import ModelSourceGenerator, SchemaIntrospector
from hare.migrations.drift import detect_drift
from tests.migrations.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model


@pytest.fixture
def connection(db):
    alias = next(iter(Connections.current().db_config))
    return Connections.get(alias)


async def generated_source(connection, table: str, schema: str = "public") -> str:
    table_info = await SchemaIntrospector.inspect_table(connection, table, schema=schema)
    source = ModelSourceGenerator.generate_model_source(table_info, connection.dialect.name)
    compile(source, "<generated>", "exec")
    return source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_foreign_key_to_another_schema_stays_a_plain_column(connection):
    """A FOREIGN KEY to other_schema.shared used to be rendered as a relation to the same-named
    table of the inspected schema."""
    await connection.execute_script(
        "CREATE SCHEMA inspect_cross_other; "
        "CREATE TABLE inspect_cross_shared (id serial PRIMARY KEY); "
        "CREATE TABLE inspect_cross_other.inspect_cross_shared (id serial PRIMARY KEY); "
        "CREATE TABLE inspect_cross_child (id serial PRIMARY KEY, "
        "shared_id int REFERENCES inspect_cross_other.inspect_cross_shared (id), "
        "local_id int REFERENCES inspect_cross_shared (id))"
    )

    table_info = await SchemaIntrospector.inspect_table(connection, "inspect_cross_child")
    source = await generated_source(connection, "inspect_cross_child")

    assert table_info.foreign_keys["shared_id"].target_schema == "inspect_cross_other"
    assert "shared_id = fields.IntField(null=True)  # TODO: a foreign key to inspect_cross_other." in source
    assert "local = fields.ForeignKeyField('models.InspectCrossShared'" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_partitions_are_left_out_of_the_table_list(connection):
    """Each partition of a partitioned table used to be listed - inspectdb generated a model for
    it, drift reported it as an untracked table."""
    await connection.execute_script(
        "CREATE TABLE inspect_partitioned (id int NOT NULL, created date NOT NULL, PRIMARY KEY (id, created)) "
        "PARTITION BY RANGE (created); "
        "CREATE TABLE inspect_partitioned_2024 PARTITION OF inspect_partitioned "
        "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"
    )

    table_names = await SchemaIntrospector.get_table_names(connection, schema="public")
    all_table_names = await SchemaIntrospector.get_table_names(connection, schema="public", include_partitions=True)

    assert "inspect_partitioned" in table_names
    assert "inspect_partitioned_2024" not in table_names
    assert "inspect_partitioned_2024" in all_table_names
    # A partition named explicitly is still inspected.
    [partition_info] = await SchemaIntrospector.inspect_tables(connection, ["inspect_partitioned_2024"])
    assert [column.name for column in partition_info.columns] == ["id", "created"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_index_details_a_model_cant_declare_are_flagged(connection):
    """An INCLUDE column is declared as the index's include (it used to turn into a key column),
    NULLS NOT DISTINCT as the UniqueConstraint's nulls_distinct=False, a descending key as "-a", a
    collated key as an expression, and an expression key's sort order is flagged instead of dropped
    without a word."""
    await connection.execute_script(
        "CREATE TABLE inspect_index_details (id serial PRIMARY KEY, a int NOT NULL, b int, c text, d text); "
        "CREATE INDEX inspect_index_covering ON inspect_index_details (a) INCLUDE (b); "
        "CREATE INDEX inspect_index_descending ON inspect_index_details (a DESC, b); "
        'CREATE INDEX inspect_index_collated ON inspect_index_details (d COLLATE "C"); '
        "CREATE INDEX inspect_index_expression_desc ON inspect_index_details (lower(c) DESC); "
        "CREATE UNIQUE INDEX inspect_index_nulls ON inspect_index_details (c) NULLS NOT DISTINCT; "
        "ALTER TABLE inspect_index_details ADD CONSTRAINT inspect_index_deferred UNIQUE (d) "
        "DEFERRABLE INITIALLY DEFERRED"
    )

    table_info = await SchemaIntrospector.inspect_table(connection, "inspect_index_details")
    source = await generated_source(connection, "inspect_index_details")

    indexes_by_name = {index.name: index for index in (*table_info.indexes, *table_info.column_indexes)}
    assert indexes_by_name["inspect_index_covering"].columns == ["a"]
    assert indexes_by_name["inspect_index_covering"].opclasses == []
    assert indexes_by_name["inspect_index_covering"].include == ["b"]
    assert indexes_by_name["inspect_index_covering"].unrepresentable_properties == []
    assert indexes_by_name["inspect_index_descending"].unrepresentable_properties == []
    assert indexes_by_name["inspect_index_collated"].expression_terms == ['d COLLATE "C"']
    assert indexes_by_name["inspect_index_expression_desc"].unrepresentable_properties == ["lower(c) DESC"]
    assert indexes_by_name["inspect_index_nulls"].nulls_not_distinct is True
    assert indexes_by_name["inspect_index_nulls"].unrepresentable_properties == []
    assert "Index(fields=['a'], name='inspect_index_covering', include=['b'])" in source
    assert "Index(fields=['-a', 'b'], name='inspect_index_descending')" in source
    assert """Index(RawSQLTerm('d COLLATE "C"'), name='inspect_index_collated')""" in source
    assert "UniqueConstraint(fields=['c'], name='inspect_index_nulls', nulls_distinct=False)" in source
    assert "# TODO: index 'inspect_index_expression_desc' has lower(c) DESC in the database" in source
    assert "inspect_index_descending' has" not in source
    assert "INCLUDE" not in source and "NULLS NOT DISTINCT" not in source
    assert (
        "UniqueConstraint(fields=['d'], name='inspect_index_deferred', deferrable=True, initially_deferred=True)"
        in source
    )
    assert "d = fields.TextField(null=True)\n" in source


@pytest.mark.asyncio
async def test_sqlite_sort_order_of_an_index_key_is_declared(connection):
    if connection.dialect.name != "sqlite":
        pytest.skip("SQLite CREATE INDEX text")
    await connection.execute_script(
        "CREATE TABLE inspect_sqlite_desc (id INTEGER PRIMARY KEY, a INT, b TEXT); "
        "CREATE INDEX inspect_sqlite_desc_index ON inspect_sqlite_desc (a DESC, b); "
        "CREATE INDEX inspect_sqlite_collated ON inspect_sqlite_desc (b COLLATE NOCASE); "
        "CREATE INDEX inspect_sqlite_collated_desc ON inspect_sqlite_desc (b COLLATE NOCASE DESC)"
    )

    source = await generated_source(connection, "inspect_sqlite_desc")

    assert "Index(fields=['-a', 'b'], name='inspect_sqlite_desc_index')" in source
    assert "Index(RawSQLTerm('b COLLATE NOCASE'), name='inspect_sqlite_collated')" in source
    assert "# TODO: index 'inspect_sqlite_collated_desc' has b COLLATE NOCASE DESC in the database" in source


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_constraints_created_by_migrate_come_back_as_declared(db_isolated):
    """A named multi-column UniqueConstraint came back as unique_together, a CHECK predicate
    wrapped in the extra parentheses Postgres prints, and a generated index name as an explicit
    one - each a change for makemigrations after reusing the generated model."""
    round_trip = RoundTrip(db_isolated.db())
    widget = build_model(
        "Constrained",
        "inspect_constrained",
        {"a": IntField(), "b": IntField(), "c": IntField(), "d": IntField(), "name": CharField(max_length=10)},
        {
            "constraints": [
                UniqueConstraint(fields=("a", "b"), name="inspect_constrained_ab"),
                UniqueConstraint(fields=("c", "d")),
            ],
        },
    )
    await round_trip.migrate_to(widget)
    assert (await detect_drift(round_trip.connection, build_live_state(widget), [APP_LABEL])).operations == []
    await round_trip.connection.execute_script(
        "ALTER TABLE inspect_constrained ADD CONSTRAINT inspect_constrained_positive CHECK (a >= 0)"
        if round_trip.dialect == "postgresql"
        else "SELECT 1"
    )

    source = await generated_source(round_trip.connection, "inspect_constrained")

    assert "UniqueConstraint(fields=['a', 'b'], name='inspect_constrained_ab')" in source
    assert "UniqueConstraint(fields=['c', 'd'])" in source
    if round_trip.dialect == "postgresql":
        assert "CheckConstraint(check=RawSQLTerm('a >= 0'), name='inspect_constrained_positive')" in source


def test_strip_outer_parentheses_only_removes_one_pair_wrapping_the_whole_expression():
    assert SchemaIntrospector.strip_outer_parentheses("(age >= 0)") == "age >= 0"
    assert SchemaIntrospector.strip_outer_parentheses("((a > 0) AND (b > 0))") == "(a > 0) AND (b > 0)"
    assert SchemaIntrospector.strip_outer_parentheses("(a) + (b)") == "(a) + (b)"
    assert SchemaIntrospector.strip_outer_parentheses(None) is None
