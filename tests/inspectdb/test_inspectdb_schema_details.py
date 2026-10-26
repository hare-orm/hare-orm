"""inspectdb keeping what a model can declare about a table - relations across schemas,
partitioned tables, index details, constraint names and wording - and flagging the rest."""

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.exceptions import HareError
from hare.fields.data.numeric import IntField
from hare.fields.data.text import CharField
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.drift import detect_drift
from tests.migrations.round_trips.test_round_trip_real_db import APP_LABEL, RoundTrip, build_live_state, build_model
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.model_source_generator import ModelSourceGenerator


@pytest.fixture
def connection(db):
    alias = next(iter(Connections.current().db_config))
    return Connections.get(alias)


async def generated_source(connection, table: str, schema: str | None = None) -> str:
    table_info = await DatabaseCatalog.inspect_table(
        connection, table, schema=schema or DatabaseUnderTest.get_public_schema()
    )
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

    table_info = await DatabaseCatalog.inspect_table(connection, "inspect_cross_child")
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

    table_names = await DatabaseCatalog.get_table_names(connection, schema="public")
    all_table_names = await DatabaseCatalog.get_table_names(connection, schema="public", include_partitions=True)

    assert "inspect_partitioned" in table_names
    assert "inspect_partitioned_2024" not in table_names
    assert "inspect_partitioned_2024" in all_table_names
    # A partition named explicitly is still inspected.
    [partition_info] = await DatabaseCatalog.inspect_tables(connection, ["inspect_partitioned_2024"])
    assert [column.name for column in partition_info.columns] == ["id", "created"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_partitioning_no_model_declares_is_flagged(connection):
    """A key over an expression and a partition partitioned itself were left out silently - the model came back
    unpartitioned without a word."""
    await connection.execute_script(
        "CREATE TABLE inspect_expression_parts (id int NOT NULL, ts timestamptz NOT NULL) "
        "PARTITION BY RANGE (date_trunc('month', ts AT TIME ZONE 'UTC')); "
        "CREATE TABLE inspect_expression_parts_default PARTITION OF inspect_expression_parts DEFAULT; "
        "CREATE TABLE inspect_nested_parts (id int NOT NULL, region text NOT NULL, d date NOT NULL) "
        "PARTITION BY LIST (region); "
        "CREATE TABLE inspect_nested_parts_eu PARTITION OF inspect_nested_parts FOR VALUES IN ('eu') "
        "PARTITION BY RANGE (d); "
        "CREATE TABLE inspect_nested_parts_eu_2024 PARTITION OF inspect_nested_parts_eu "
        "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')"
    )

    expression_source = await generated_source(connection, "inspect_expression_parts")
    nested_source = await generated_source(connection, "inspect_nested_parts")

    assert '# TODO: the table is partitioned as "PARTITION BY RANGE (date_trunc(' in expression_source
    assert "partitioned partitions inspect_nested_parts_eu" in nested_source
    [expression_table] = await DatabaseCatalog.inspect_tables(connection, ["inspect_expression_parts"])
    assert expression_table.unparsed_partitioning is not None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_partitioning_no_model_declares_is_drift(db_isolated_no_schema):
    """A table partitioned by an expression matched an unpartitioned model - drift saw no difference."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    reading = build_model(
        "Reading",
        "inspect_expression_drift",
        {"id": IntField(), "value": IntField()},
        {"primary_key": None},
    )
    await round_trip.migrate_to(reading)
    assert (await detect_drift(round_trip.connection, build_live_state(reading), [APP_LABEL])).operations == []
    await round_trip.connection.execute_script(
        "DROP TABLE inspect_expression_drift; "
        "CREATE TABLE inspect_expression_drift (id int NOT NULL, value int NOT NULL) PARTITION BY RANGE ((id % 10)); "
        "CREATE TABLE inspect_expression_drift_default PARTITION OF inspect_expression_drift DEFAULT"
    )

    operations = (await detect_drift(round_trip.connection, build_live_state(reading), [APP_LABEL])).operations

    assert [type(operation).__name__ for operation in operations] == ["AlterModelOptions"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_invalid_index_is_flagged_and_drift(db_isolated_no_schema):
    """A unique index whose concurrent build failed is kept INVALID - it enforces nothing, yet drift matched it
    with the declared index and inspectdb rendered it as a working one."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
    loan = build_model("Loan", "inspect_invalid_index", {"status": CharField(max_length=10)})
    await round_trip.migrate_to(loan)
    await round_trip.connection.execute_script(
        "INSERT INTO inspect_invalid_index (id, status) VALUES (1, 'a'), (2, 'a')"
    )
    with pytest.raises(HareError):
        await round_trip.connection.execute_script(
            "CREATE UNIQUE INDEX CONCURRENTLY inspect_invalid_status ON inspect_invalid_index (status)"
        )
    indexed_loan = build_model(
        "Loan",
        "inspect_invalid_index",
        {"status": CharField(max_length=10)},
        {"indexes": [Index(fields=["status"], name="inspect_invalid_status", unique=True)]},
    )

    operations = (await detect_drift(round_trip.connection, build_live_state(indexed_loan), [APP_LABEL])).operations
    source = await generated_source(round_trip.connection, "inspect_invalid_index")

    assert [type(operation).__name__ for operation in operations] == ["RemoveIndex", "AddIndex"]
    assert "# TODO: index 'inspect_invalid_status' is invalid in the database" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_index_details_a_model_cant_declare_are_flagged(connection):
    """An INCLUDE column is declared as the index's include (it used to turn into a key column),
    NULLS NOT DISTINCT as the UniqueConstraint's nulls_distinct=False, a descending key as "-a", a
    collated key as an expression, and an expression key with its sort order."""
    # Connected first, so the features follow the server's own version (NULLS NOT DISTINCT is 15+).
    async with connection.acquire_connection():
        pass
    has_nulls_distinct = connection.features.supports_nulls_distinct
    nulls_index_sql = (
        "CREATE UNIQUE INDEX inspect_index_nulls ON inspect_index_details (c) NULLS NOT DISTINCT; "
        if has_nulls_distinct
        else ""
    )
    await connection.execute_script(
        "CREATE TABLE inspect_index_details (id serial PRIMARY KEY, a int NOT NULL, b int, c text, d text); "
        "CREATE INDEX inspect_index_covering ON inspect_index_details (a) INCLUDE (b); "
        "CREATE INDEX inspect_index_descending ON inspect_index_details (a DESC, b); "
        'CREATE INDEX inspect_index_collated ON inspect_index_details (d COLLATE "C"); '
        "CREATE INDEX inspect_index_expression_desc ON inspect_index_details (lower(c) DESC); "
        f"{nulls_index_sql}"
        "ALTER TABLE inspect_index_details ADD CONSTRAINT inspect_index_deferred UNIQUE (d) "
        "DEFERRABLE INITIALLY DEFERRED"
    )

    table_info = await DatabaseCatalog.inspect_table(connection, "inspect_index_details")
    source = await generated_source(connection, "inspect_index_details")

    indexes_by_name = {index.name: index for index in (*table_info.indexes, *table_info.column_indexes)}
    assert indexes_by_name["inspect_index_covering"].columns == ["a"]
    assert indexes_by_name["inspect_index_covering"].opclasses == []
    assert indexes_by_name["inspect_index_covering"].include == ["b"]
    assert indexes_by_name["inspect_index_covering"].unrepresentable_properties == []
    assert indexes_by_name["inspect_index_descending"].unrepresentable_properties == []
    assert indexes_by_name["inspect_index_collated"].expression_terms == ['d COLLATE "C"']
    assert indexes_by_name["inspect_index_expression_desc"].unrepresentable_properties == []
    if has_nulls_distinct:
        assert indexes_by_name["inspect_index_nulls"].nulls_not_distinct is True
        assert indexes_by_name["inspect_index_nulls"].unrepresentable_properties == []
        assert "UniqueConstraint(fields=['c'], name='inspect_index_nulls', nulls_distinct=False)" in source
    assert "Index(fields=['a'], name='inspect_index_covering', include=['b'])" in source
    assert "Index(fields=['-a', 'b'], name='inspect_index_descending')" in source
    assert """Index(RawSQLTerm('d COLLATE "C"'), name='inspect_index_collated')""" in source
    assert (
        "Index(OrderedIndexKey(RawSQLTerm('lower(c)'), Order.DESC_NULLS_FIRST), name='inspect_index_expression_desc')"
        in source
    )
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
async def test_constraints_created_by_migrate_come_back_as_declared(db_isolated_no_schema):
    """A named multi-column UniqueConstraint came back as unique_together, a CHECK predicate
    wrapped in the extra parentheses Postgres prints, and a generated index name as an explicit
    one - each a change for makemigrations after reusing the generated model."""
    round_trip = RoundTrip(db_isolated_no_schema.get_connection())
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


@pytest.mark.database_independent
def test_strip_outer_parentheses_only_removes_one_pair_wrapping_the_whole_expression():
    assert SchemaIntrospector.strip_outer_parentheses("(age >= 0)") == "age >= 0"
    assert SchemaIntrospector.strip_outer_parentheses("((a > 0) AND (b > 0))") == "(a > 0) AND (b > 0)"
    assert SchemaIntrospector.strip_outer_parentheses("(a) + (b)") == "(a) + (b)"
    assert SchemaIntrospector.strip_outer_parentheses(None) is None


@pytest.mark.asyncio
async def test_a_column_of_a_type_no_field_writes_is_flagged(connection):
    """drift reported the column's type against the field's, with nothing in the model saying why."""
    column_type, written_type = (
        ("real", "DOUBLE PRECISION") if connection.dialect.name == "postgresql" else ("BOOLEAN", "INT")
    )
    await connection.execute_script(f"CREATE TABLE inspect_typed (id INTEGER PRIMARY KEY, v {column_type})")

    source = await generated_source(connection, "inspect_typed")

    assert f"# TODO: the column is {column_type!r}, the field writes {written_type!r}" in source


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_boolean_default_of_an_integer_is_a_bool(connection):
    """A BOOLEAN DEFAULT 1 came back as db_default=1, which the field refuses - drift failed on it instead of
    reporting the column's type."""
    await connection.execute_script(
        "CREATE TABLE inspect_flags (id INTEGER PRIMARY KEY, on_flag BOOLEAN DEFAULT 1, off_flag BOOLEAN DEFAULT 0)"
    )

    source = await generated_source(connection, "inspect_flags")

    assert "on_flag = fields.BooleanField(null=True, db_default=True)" in source
    assert "off_flag = fields.BooleanField(null=True, db_default=False)" in source


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_a_quoted_default_is_the_value_the_field_reads(connection):
    """A quoted default came back as its text, which the field refuses - drift failed on it."""
    await connection.execute_script(
        "CREATE TABLE inspect_quoted (id INTEGER PRIMARY KEY, flag BOOLEAN DEFAULT 't', count INT DEFAULT '7')"
    )

    source = await generated_source(connection, "inspect_quoted")

    assert "flag = fields.BooleanField(null=True, db_default=True)" in source
    assert "count = fields.IntField(null=True, db_default=7)" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_default_no_value_of_the_field_holds_is_its_sql(connection):
    """'NaN' of a float, 'infinity' of a date and '1 day' of an interval came back as text the field refuses -
    drift failed on them instead of comparing the columns."""
    await connection.execute_script(
        "CREATE TABLE inspect_special_defaults (id serial PRIMARY KEY, ratio double precision DEFAULT 'NaN', "
        "until date DEFAULT 'infinity', wait interval DEFAULT '1 day', nothing jsonb DEFAULT 'null')"
    )

    source = await generated_source(connection, "inspect_special_defaults")

    # A JSON null as a value would be None - no default at all.
    for expected in (
        "SqlDefault(\"'NaN'\")",
        "SqlDefault(\"'infinity'\")",
        "SqlDefault(\"'1 day'\")",
        "SqlDefault(\"'null'\")",
    ):
        assert expected in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_default_holding_a_backslash_is_its_sql_as_the_database_reads_it(connection):
    """The text of a default the field takes no value of was quoted by hand - a backslash in it was written
    into a plain literal, which a server without standard_conforming_strings reads as an escape."""
    await connection.execute_script(
        "CREATE TABLE inspect_escaped_defaults (id serial PRIMARY KEY, wait interval DEFAULT '1 day', "
        "data bytea DEFAULT '\\x0102')"
    )

    source = await generated_source(connection, "inspect_escaped_defaults")

    assert "SqlDefault(\"'1 day'\")" in source
    assert r"""data = fields.BinaryField(null=True, db_default=SqlDefault("E'\\\\x0102'"))""" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_the_sequence_of_a_column_of_a_composite_key_is_kept(connection):
    """A SERIAL column of a composite primary key lost its default and its sequence - taken for a generated
    key, which takes its own: drift reported the column, a create() left it None."""
    await connection.execute_script("CREATE TABLE inspect_serial_composite (id serial, at int, PRIMARY KEY (id, at))")

    source = await generated_source(connection, "inspect_serial_composite")

    assert "db_default=SqlDefault(\"nextval('inspect_serial_composite_id_seq'::regclass)\")" in source
    assert "DatabaseSequence(name='inspect_serial_composite_id_seq'" in source
    assert "owned_by='id'" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_sequence_no_column_owns_is_kept_by_the_table_taking_a_default_from_it(connection):
    """A sequence no column owns, which a column's default reads, was named in the note of sequences no column
    reads - the model's migration failed on the missing sequence."""
    await connection.execute_script(
        "CREATE SEQUENCE inspect_standalone_seq START 10; "
        "CREATE TABLE inspect_standalone_user (id int PRIMARY KEY, "
        "n bigint DEFAULT nextval('inspect_standalone_seq'))"
    )

    source = await generated_source(connection, "inspect_standalone_user")

    assert "db_default=SqlDefault(\"nextval('inspect_standalone_seq'::regclass)\")" in source
    assert "DatabaseSequence(name='inspect_standalone_seq', start=10)" in source
    unattached_sequences, _functions, _grants = await DatabaseCatalog.get_unattached_schema_objects(
        connection, DatabaseUnderTest.get_public_schema()
    )
    assert "inspect_standalone_seq" not in [sequence.name for sequence in unattached_sequences]


@pytest.mark.asyncio
async def test_set_null_on_a_not_null_column_is_rendered_as_no_action(connection):
    """inspectdb wrote ForeignKeyField(on_delete=SET_NULL) of a NOT NULL column - the model refused
    to load, the error text ending up in the generated file."""
    await connection.execute_script(
        "CREATE TABLE inspect_sn_target (id INTEGER PRIMARY KEY); "
        "CREATE TABLE inspect_sn_source (id INTEGER PRIMARY KEY, "
        "target_id INTEGER NOT NULL REFERENCES inspect_sn_target (id) ON DELETE SET NULL)"
    )

    source = await generated_source(connection, "inspect_sn_source")

    assert "on_delete=OnDelete.NO_ACTION" in source
    assert "ON DELETE SET NULL on a NOT NULL column" in source


@pytest.mark.asyncio
async def test_a_table_of_one_unique_composite_foreign_key_is_a_one_to_one_model(connection):
    """It was taken for a many-to-many through table and skipped - one foreign key joins no two sides."""
    await connection.execute_script(
        "CREATE TABLE inspect_cp_parent (a INTEGER NOT NULL, b INTEGER NOT NULL, PRIMARY KEY (a, b)); "
        "CREATE TABLE inspect_cp_child (parent_a INTEGER NOT NULL, parent_b INTEGER NOT NULL, "
        "UNIQUE (parent_a, parent_b), FOREIGN KEY (parent_a, parent_b) REFERENCES inspect_cp_parent (a, b))"
    )

    source = await generated_source(connection, "inspect_cp_child")

    assert "fields.OneToOneField('models.InspectCpParent'" in source
    assert "primary_key = None" in source
    assert "ManyToManyField through table" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_an_array_default_is_a_list(connection):
    """It was the literal's text - '{1,2}' - which an ArrayField refuses, so migrate failed."""
    await connection.execute_script(
        "CREATE TABLE inspect_array_defaults (id serial PRIMARY KEY, empty_texts text[] DEFAULT '{}', "
        "numbers integer[] DEFAULT '{1,2}', texts text[] DEFAULT '{\"a b\",NULL}', "
        "grid integer[] DEFAULT '{{1,2},{3,4}}')"
    )

    source = await generated_source(connection, "inspect_array_defaults")

    for expected in ("db_default=[]", "db_default=[1, 2]", "db_default=['a b', None]", "db_default=[[1, 2], [3, 4]]"):
        assert expected in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_a_sequence_no_column_owns_read_by_two_tables_is_declared_once(connection):
    """A sequence no column owns, whose values columns of two tables take by default, was declared in the
    Meta.sequences of both models - their migrations created it twice."""
    await connection.execute_script(
        "CREATE SEQUENCE inspect_shared_seq; "
        "CREATE TABLE inspect_shared_seq_a (id int PRIMARY KEY, n bigint DEFAULT nextval('inspect_shared_seq')); "
        "CREATE TABLE inspect_shared_seq_b (id int PRIMARY KEY, n bigint DEFAULT nextval('inspect_shared_seq'))"
    )

    first_source = await generated_source(connection, "inspect_shared_seq_a")
    second_source = await generated_source(connection, "inspect_shared_seq_b")

    assert "sequences = [DatabaseSequence(name='inspect_shared_seq')]" in first_source
    assert "DatabaseSequence" not in second_source
    assert (
        "# TODO: sequence 'inspect_shared_seq' a default reads is declared by the model of table "
        "'inspect_shared_seq_a'"
    ) in second_source


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("owner", ["inspect_key_seq_a.id", "NONE"], ids=["owned by the key", "owned by none"])
@pytest.mark.asyncio
async def test_a_sequence_a_key_and_another_tables_column_read_is_declared(connection, owner):
    """The sequence a primary key of one column takes its default from was taken for the generated key's own and
    declared by no model - the default of the other table's column read a sequence no migration made."""
    await connection.execute_script(
        "CREATE SEQUENCE inspect_key_seq; "
        "CREATE TABLE inspect_key_seq_a (id bigint PRIMARY KEY DEFAULT nextval('inspect_key_seq'), n int); "
        "CREATE TABLE inspect_key_seq_b (id serial PRIMARY KEY, ticket bigint DEFAULT nextval('inspect_key_seq')); "
        f"ALTER SEQUENCE inspect_key_seq OWNED BY {owner}"
    )

    key_source = await generated_source(connection, "inspect_key_seq_a")
    reading_source = await generated_source(connection, "inspect_key_seq_b")

    if owner == "NONE":
        # The key takes its own sequence once migrated - the model of the column reading it declares it.
        assert "DatabaseSequence" not in key_source
        assert "sequences = [DatabaseSequence(name='inspect_key_seq')]" in reading_source
    else:
        assert "sequences = [DatabaseSequence(name='inspect_key_seq', owned_by='id')]" in key_source
        assert "DatabaseSequence" not in reading_source
