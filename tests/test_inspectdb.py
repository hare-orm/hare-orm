import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector
from hare.fields.enums import OnDelete
from hare.inspectdb import (
    DuplicateModelClassNameError,
    ModelSourceGenerator,
    SchemaInspector,
    SchemaIntrospector,
    TableNotFoundError,
)
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.models.class_building.model_field_collection import ModelFieldCollection


@pytest.fixture
def connection(db):
    alias = next(iter(Connections.current().db_config))
    return Connections.get(alias)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("insert", "INSERT"),
        ("insert   or   update", "INSERT OR UPDATE"),
        ('update of "mixedCol"', 'UPDATE OF "mixedCol"'),
        ('update of "mixedCol", plaincol', 'UPDATE OF "mixedCol", plaincol'),
        ('update of col1, "Col2", col3', 'UPDATE OF col1, "Col2", col3'),
        ("insert or update of Price_Col, delete_flag", "INSERT OR UPDATE OF Price_Col, delete_flag"),
    ],
)
def test_normalize_event_clause_preserves_quoted_identifier_case(raw: str, expected: str) -> None:
    assert SchemaIntrospector.normalize_event_clause(raw) == expected


@pytest.mark.asyncio
async def test_get_table_names_includes_known_tables(connection):
    tables = await DatabaseCatalog.get_table_names(connection)
    assert "tournament" in tables
    assert "event" in tables


@pytest.mark.asyncio
async def test_inspect_table_columns(connection):
    table = await DatabaseCatalog.inspect_table(connection, "tournament")
    column_names = {column.name for column in table.columns}
    assert column_names == {"id", "name", "desc", "created"}

    by_name = {column.name: column for column in table.columns}
    assert by_name["id"].is_pk
    assert not by_name["name"].nullable
    assert by_name["desc"].nullable


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_inspect_table_detects_foreign_key(connection):
    table = await DatabaseCatalog.inspect_table(connection, "event")
    assert "tournament_id" in table.foreign_keys
    fk = table.foreign_keys["tournament_id"]
    assert fk.target_table == "tournament"


@pytest.mark.asyncio
async def test_generate_model_source_is_valid_python(connection):
    table = await DatabaseCatalog.inspect_table(connection, "tournament")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")  # raises SyntaxError if the generated code is broken
    assert "class Tournament(Model):" in source
    assert "primary_key=True" in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_renders_foreign_key(connection):
    table = await DatabaseCatalog.inspect_table(connection, "event")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "ForeignKeyField" in source
    assert "models.Tournament" in source
    # the shadow _id column doesn't ALSO get rendered as its own plain field
    assert "tournament_id = " not in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_app_label_qualifies_fk_targets(connection):
    """app_label= (defaults to "models") lets a caller other than the plain `hare inspectdb` CLI
    - e.g. a runtime table-discovery tool registering generated models under its own dedicated
    app label - qualify every FK/composite-FK target reference consistently instead of always
    getting the CLI's own "models" placeholder."""
    table = await DatabaseCatalog.inspect_table(connection, "event")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name, app_label="myapp")

    compile(source, "<generated>", "exec")
    assert "myapp.Tournament" in source
    assert "models.Tournament" not in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_fk_target_overrides_a_single_target_reference(connection):
    """foreign_key_target_overrides= supplies the WHOLE "<app_label>.<TargetClass>" reference for one
    specific target table, bypassing app_label= and class_name_for_table() entirely for it - for
    a caller that must point an FK at an ALREADY-REGISTERED real model whose class name is an
    arbitrary Python identifier the project chose, not guaranteed to match what
    class_name_for_table() would derive from the table name itself. Every other FK target not
    named in the override mapping still falls back to app_label=/class_name_for_table() as
    before."""
    table = await DatabaseCatalog.inspect_table(connection, "event")
    source = ModelSourceGenerator.generate_model_source(
        table,
        connection.dialect.name,
        app_label="myapp",
        foreign_key_target_overrides={"tournament": "models.RealTournament"},
    )

    compile(source, "<generated>", "exec")
    assert "models.RealTournament" in source
    assert "myapp.Tournament" not in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_renders_primary_key_that_is_also_a_foreign_key(connection):
    """A OneToOneField(primary_key=True)-shaped column is both the table's PK and an FK - the FK
    branch used to never check is_pk at all, silently dropping primary_key=True entirely and
    leaving the round-tripped model with a phantom, DB-nonexistent "id" PK instead."""
    table = await DatabaseCatalog.inspect_table(connection, "address")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "primary_key=True" in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    generated_cls = namespace["Address"]
    assert "id" not in generated_cls._meta.fields_map
    assert generated_cls._meta.primary_key_attribute == "event"


@pytest.mark.asyncio
async def test_round_trip_generated_model_is_loadable(connection):
    """The real test: generated source isn't just syntactically valid, it actually defines a
    working hare-orm model when executed, with the fields inspectdb claimed it would have."""
    table = await DatabaseCatalog.inspect_table(connection, "tournament")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code

    generated_cls = namespace["Tournament"]
    field_names = set(generated_cls._meta.fields_map.keys())
    assert field_names == {"id", "name", "desc", "created"}
    assert generated_cls._meta.primary_key_attribute == "id"


@pytest.mark.asyncio
async def test_inspect_whole_schema_covers_all_tables(connection):
    source = await SchemaInspector.inspect(connection)
    compile(source, "<generated>", "exec")
    assert "class Tournament(Model):" in source
    assert "class Event(Model):" in source


@pytest.mark.asyncio
async def test_inspect_of_several_tables_imports_once(connection):
    source = await SchemaInspector.inspect(connection, tables=["tournament", "event"])
    compile(source, "<generated>", "exec")
    assert source.count("from hare.models import Model") == 1
    assert source.count("from hare import fields") == 1
    assert source.index("from hare import fields") < source.index("class ")


@pytest.mark.asyncio
async def test_inspect_specific_tables_only(connection):
    source = await SchemaInspector.inspect(connection, tables=["tournament"])
    assert "class Tournament(Model):" in source
    assert "class Event(Model):" not in source


@pytest.mark.asyncio
async def test_unique_column_detected(connection):
    """SoftDeleteChildRestrict isn't unique, but StraightFields.chars has db_index (not unique) -
    use a model with a real unique constraint to check unique detection specifically."""
    table = await DatabaseCatalog.inspect_table(connection, "re_port_er")
    by_name = {column.name: column for column in table.columns}
    # Reporter has no unique columns beyond its PK; this asserts the absence is correct, not a
    # false positive from is_unique defaulting to True somewhere.
    assert not by_name["name"].is_unique


def test_sqlite_type_map_orders_varchar_before_char():
    """ "char" is a substring of "varchar" too - with "char" checked first (first match in
    SQLITE_TYPE_MAP wins), a "VARCHAR" column would always match the "char" entry instead of the
    more specific "varchar" one. Currently harmless only because both map to identical
    CharField(max_length=255) kwargs - still fragile, and the wrong order to read the list in if
    either entry's kwargs ever diverge. Same reasoning already applied to "character varying" vs
    "character" in POSTGRESQL_TYPE_MAP: the more specific keyword must come first."""
    keywords = [keyword for keyword, _, _ in SqliteIntrospector.TYPE_MAP]
    assert keywords.index("varchar") < keywords.index("char")


def test_sqlite_json_text_column_maps_back_to_jsonfield():
    """JSONField's own SQLite column type (JSON_TEXT, chosen for its TEXT affinity) must still
    read back as a JSONField, not a TextField."""
    from hare.dialects.enums import DialectName
    from hare.fields.data.json import JSONField
    from hare.inspectdb.introspection.column_info import ColumnInfo

    column_type = JSONField().get_column_type(DialectRegistry.get_dialect("sqlite"))
    column = ColumnInfo(name="payload", db_type=column_type, nullable=True, is_pk=False, is_unique=False)

    field_path, _, is_ambiguous = ColumnTypeMapper.map_column_type(DialectName.SQLITE, column)

    assert (field_path, is_ambiguous) == ("hare.fields.data.json.JSONField", False)


def test_postgres_naive_timestamp_is_ambiguous_not_datetimefield():
    """DatetimeField always generates TIMESTAMPTZ on Postgres - the generic "timestamp"
    keyword in POSTGRESQL_TYPE_MAP matches both "timestamp with time zone" and "timestamp
    without time zone", so a genuine naive-timestamp column used to map to DatetimeField too,
    silently changing its semantics (naive -> tz-aware) the next time a migration diffs
    against the real schema."""
    from hare.dialects.enums import DialectName
    from hare.inspectdb.introspection.column_info import ColumnInfo

    def column(db_type: str) -> ColumnInfo:
        return ColumnInfo(name="created_at", db_type=db_type, nullable=False, is_pk=False, is_unique=False)

    naive_path, _, naive_ambiguous = ColumnTypeMapper.map_column_type(
        DialectName.POSTGRESQL, column("timestamp without time zone")
    )
    assert naive_path == "hare.fields.data.text.TextField"
    assert naive_ambiguous is True

    aware_path, _, aware_ambiguous = ColumnTypeMapper.map_column_type(
        DialectName.POSTGRESQL, column("timestamp with time zone")
    )
    assert aware_path == "hare.fields.data.temporal.DatetimeField"
    assert aware_ambiguous is False


def test_safe_identifier_replaces_illegal_characters():
    """safe_identifier() only special-cased a leading digit and a keyword collision -
    name.isidentifier() also returns False for a name containing any OTHER character illegal in
    a Python identifier (hyphen, space, dot, ...), all legal, common DB column/table names when
    quoted - the leading-digit branch fired anyway, prepending digit_prefix without actually
    removing the illegal character, so the result was STILL not a valid identifier."""
    from hare.inspectdb.generation.model_naming import ModelNaming

    assert ModelNaming.get_safe_identifier("my-col", digit_prefix="field_").isidentifier()
    assert ModelNaming.get_safe_identifier("my col", digit_prefix="field_").isidentifier()
    assert ModelNaming.get_safe_identifier("my.col", digit_prefix="field_").isidentifier()
    # A leading digit AFTER replacement (not just in the original name) must still get the
    # digit_prefix treatment.
    result = ModelNaming.get_safe_identifier("1-col", digit_prefix="field_")
    assert result.isidentifier()
    assert result.startswith("field_")


def test_class_name_for_table_handles_illegal_characters():
    """class_name_for_table() calls safe_identifier() too - a table name with an illegal
    character broke the generated `class ...(Model):` line itself, not just column attributes."""
    from hare.inspectdb.generation.model_naming import ModelNaming

    assert ModelNaming.get_class_name("my-table").isidentifier()


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_generate_model_source_sanitizes_illegal_identifier_characters(connection):
    """End-to-end: a table/column name with characters illegal in a Python identifier (only
    legal because SQLite lets a quoted identifier contain anything) used to generate source that
    fails to even compile - SyntaxError, not just a wrong type mapping."""
    await connection.execute_script('CREATE TABLE "weird-cols" (id INTEGER PRIMARY KEY, "my col" TEXT)')

    table = await DatabaseCatalog.inspect_table(connection, "weird-cols")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "source_field='my col'" in source
    assert "table = 'weird-cols'" in source


def test_sqlite_char_36_maps_to_uuidfield_not_charfield():
    """SQLite has no native UUID column type - UUIDField.SQL_TYPE declares one as plain
    "CHAR(36)" (hare.fields.data.uuid_field.UUIDField), which used to fall into the generic "char" keyword
    match in SQLITE_TYPE_MAP and get reconstructed as CharField(max_length=36) instead - a
    UUIDField column round-tripped through inspectdb silently lost its real type. A genuine
    VARCHAR(36) (never what UUIDField itself declares) must still map to CharField, not get
    swept up by the same heuristic."""
    from hare.dialects.enums import DialectName
    from hare.inspectdb.introspection.column_info import ColumnInfo

    def column(db_type: str) -> ColumnInfo:
        return ColumnInfo(name="token", db_type=db_type, nullable=False, is_pk=False, is_unique=False, max_length=36)

    uuid_path, uuid_kwargs, uuid_ambiguous = ColumnTypeMapper.map_column_type(DialectName.SQLITE, column("CHAR(36)"))
    assert uuid_path == "hare.fields.data.uuid_field.UUIDField"
    assert uuid_kwargs == {}
    assert uuid_ambiguous is False

    varchar_path, _, varchar_ambiguous = ColumnTypeMapper.map_column_type(DialectName.SQLITE, column("VARCHAR(36)"))
    assert varchar_path == "hare.fields.data.text.CharField"
    assert varchar_ambiguous is False


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_sqlite_uuid_column(connection):
    """End-to-end: a table created through hare-orm's own SqliteSchemaEditor for a UUIDField
    column (declared CHAR(36)) must round-trip back to UUIDField through inspectdb, not
    CharField(max_length=36)."""
    await connection.execute_script(
        "CREATE TABLE scratch_uuid (id CHAR(36) PRIMARY KEY, token CHAR(36) NOT NULL, label VARCHAR(36))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_uuid")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "token = fields.UUIDField()" in source
    assert "label = fields.CharField(max_length=36, null=True)" in source
    assert "TODO" not in source


def test_unsupported_dialect_raises():
    from hare.exceptions import UnSupportedError
    from hare.inspectdb import UnsupportedDialectError

    assert issubclass(UnsupportedDialectError, UnSupportedError)


@pytest.mark.asyncio
async def test_inspect_table_nonexistent_table_raises(connection):
    """A typo'd/nonexistent table name must raise a clear error on both dialects, not silently
    produce a TableInfo with no columns - which used to generate a class body with no fields at
    all, an IndentationError/SyntaxError in the output on SQLite (Postgres already raised here by
    accident, via a regclass cast elsewhere in the query)."""
    from hare.inspectdb import TableNotFoundError

    with pytest.raises(TableNotFoundError):
        await DatabaseCatalog.inspect_table(connection, "table_that_does_not_exist")


# ============================================================================
# Composite primary key + multi-column indexing reconstruction
# ============================================================================


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_composite_pk(connection):
    table = await DatabaseCatalog.inspect_table(connection, "compositepkthing")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "pk = fields.CompositePrimaryKey('thing_id', 'revision')" in source
    # a composite-pk member field must NOT also be individually marked primary_key=True - hare-orm
    # rejects that combination outright.
    assert "primary_key=True" not in source


@pytest.mark.asyncio
async def test_round_trip_composite_pk_model_is_loadable(connection):
    table = await DatabaseCatalog.inspect_table(connection, "compositepkthing")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code

    generated_cls = namespace["Compositepkthing"]
    assert generated_cls._meta.primary_key_attribute == ("thing_id", "revision")


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_composite_pk_declared_out_of_physical_order(connection):
    """The PK constraint's own declared column order can differ from the table's physical
    column layout (here: columns physically a, b, c but PRIMARY KEY (c, a)) - the generated
    CompositePrimaryKey(...) must preserve the constraint's order, not fall back to physical
    column order."""
    await connection.execute_script(
        "CREATE TABLE scratch_composite_pk_order (a INT NOT NULL, b INT NOT NULL, c INT NOT NULL, PRIMARY KEY (c, a))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_pk_order")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "pk = fields.CompositePrimaryKey('c', 'a')" in source


@pytest.mark.asyncio
async def test_generate_model_source_composite_pk_fk_member_drops_the_relation_not_the_column(connection):
    """A single-column FK that's ALSO a composite PK member can't be rendered as a
    ForeignKeyField at all - ModelMeta._parse_composite_pk() unconditionally rejects any relation
    field as a composite PK component ("it has no single DB column of its own"), so a generated
    `pk = CompositePrimaryKey('warehouse', ...)` referencing a ForeignKeyField's own name used to
    raise ConfigurationError the moment the model was imported - code that compiled fine but was
    guaranteed to crash on the very next step. Confirmed live against a real Postgres table
    shaped exactly like this before this fix. The FK relation is now dropped (with a TODO
    comment) and the column rendered as a plain field instead, which is importable."""
    await connection.execute_script("CREATE TABLE scratch_composite_pk_warehouse (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_composite_pk_stock ("
        "warehouse_id INTEGER NOT NULL REFERENCES scratch_composite_pk_warehouse(id), "
        "sku TEXT NOT NULL, "
        "PRIMARY KEY (warehouse_id, sku))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_pk_stock")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "= fields.ForeignKeyField" not in source
    assert "warehouse_id = fields.IntField" in source
    assert "pk = fields.CompositePrimaryKey('warehouse_id', 'sku')" in source
    assert "# TODO:" in source and "composite primary key component" in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code

    generated_cls = namespace["ScratchCompositePkStock"]
    assert generated_cls._meta.primary_key_attribute == ("warehouse_id", "sku")


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_single_column_index(connection):
    table = await DatabaseCatalog.inspect_table(connection, "modelwithindexes")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    # max_length is reconstructed from the real column (16 here, not the generic 255 fallback)
    assert "indexed = fields.CharField(max_length=16, db_index=True)" in source
    # a plain (non-indexed, non-unique) column must not get either kwarg
    assert "f1 = fields.CharField(max_length=16)\n" in source


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_multi_column_index_and_unique_constraint(connection):
    table = await DatabaseCatalog.inspect_table(connection, "modelwithindexes")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "class Meta:" in source
    # The generated name stays implicit, an explicit one is kept.
    assert "indexes = [Index(fields=['f1', 'f2']), Index(fields=['f3'], name='model_with_indexes__f3')]" in source
    assert "constraints = [UniqueConstraint(fields=['u1', 'u2'])]" in source
    # the multi-column UNIQUE constraint's own member columns must not ALSO get a (wrong)
    # single-column unique=True - confirmed as a real bug on Postgres before fixing (each column
    # of a multi-column UNIQUE constraint was individually mismarked unique=True).
    assert "u1 = fields.IntField()\n" in source
    assert "u2 = fields.IntField()\n" in source


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_round_trip_indexed_model_is_loadable(connection):
    table = await DatabaseCatalog.inspect_table(connection, "modelwithindexes")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code

    generated_cls = namespace["Modelwithindexes"]
    assert [tuple(constraint.fields) for constraint in generated_cls._meta.constraints] == [("u1", "u2")]
    assert [(index.fields, index.name) for index in generated_cls._meta.indexes] == [
        (["f1", "f2"], None),
        (["f3"], "model_with_indexes__f3"),
    ]
    assert generated_cls._meta.fields_map["indexed"].index is True
    assert generated_cls._meta.fields_map["u1"].unique is False


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_deduplicates_two_physical_unique_indexes_on_the_same_columns(connection):
    """Two SEPARATE physical unique indexes covering the exact same column set (a bare CREATE
    UNIQUE INDEX alongside a later, separately-named ALTER TABLE ... ADD CONSTRAINT ... UNIQUE
    that never reused the first index) - a real, if unusual, live-schema shape. Meta.
    unique_together entries carry no name of their own, so two identical tuples used to render as
    a literal duplicate, which crashes CreateModel re-applying the generated model the same way
    an unnamed UniqueConstraint duplicating a unique_together entry does (9569954e) - confirmed
    live that CreateModel against a fresh table raised a raw, uncaught DB error instead of a
    clean ConfigurationError."""
    await connection.execute_script(
        "CREATE TABLE scratch_dup_unique (id serial primary key, a integer NOT NULL, b integer NOT NULL)"
    )
    await connection.execute_script("CREATE UNIQUE INDEX scratch_dup_unique_idx1 ON scratch_dup_unique (a, b)")
    await connection.execute_script(
        "ALTER TABLE scratch_dup_unique ADD CONSTRAINT scratch_dup_unique_ct2 UNIQUE (a, b)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_dup_unique")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")

    # Each of them has a name of its own, so each is its own named UniqueConstraint.
    assert "unique_together" not in source
    assert (
        "constraints = [UniqueConstraint(fields=['a', 'b'], name='scratch_dup_unique_ct2'), "
        "UniqueConstraint(fields=['a', 'b'], name='scratch_dup_unique_idx1')]"
    ) in source

    generated_cls = await _load_generated_model(source, "ScratchDupUnique")
    assert [constraint.name for constraint in generated_cls._meta.constraints] == [
        "scratch_dup_unique_ct2",
        "scratch_dup_unique_idx1",
    ]


# ============================================================================
# Precise metadata reconstruction: max_length/max_digits/decimal_places, FK on_delete,
# db_default, column description
# ============================================================================


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_fk_on_delete(connection):
    """The generic FK reconstruction always used to hardcode the default (CASCADE) - never read
    the real ON DELETE action off the DB, silently dropping it for a non-default FK."""
    table = await DatabaseCatalog.inspect_table(connection, "employee")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "on_delete=OnDelete.NO_ACTION" in source

    from hare.fields.enums import OnDelete
    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    assert namespace["Employee"]._meta.fields_map["manager"].on_delete == OnDelete.NO_ACTION


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_set_default_fk_with_its_column_default(connection):
    """A real ON DELETE SET DEFAULT foreign key always comes with a DDL column DEFAULT here (that's
    what the database resets to), so the generated field carries it as db_default - the shape
    ForeignKeyField now requires for on_delete=SET_DEFAULT on a real constraint."""
    table = await DatabaseCatalog.inspect_table(connection, "setdefaultdbdefaultchildnullable")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    assert "on_delete=OnDelete.SET_DEFAULT" in source
    assert "db_default=999" in source

    from hare.fields.enums import OnDelete
    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    parent_field = namespace["Setdefaultdbdefaultchildnullable"]._meta.fields_map["parent"]
    assert parent_field.on_delete == OnDelete.SET_DEFAULT
    assert parent_field.db_default == 999


@pytest.mark.parametrize(
    ("nullable", "expected_on_delete"),
    [(True, "SET_NULL"), (False, "NO_ACTION")],
)
@pytest.mark.asyncio
async def test_generate_model_source_downgrades_set_default_fk_without_a_column_default(
    connection, nullable, expected_on_delete
):
    """ON DELETE SET DEFAULT on a column with no DEFAULT resets it to NULL (or fails on NOT NULL) -
    ForeignKeyField(on_delete=SET_DEFAULT) can't express that without a db_default and would
    refuse to import, so the generator renders the action the database really performs, with a
    TODO explaining why."""
    suffix = "nullable" if nullable else "required"
    await connection.execute_script(f"CREATE TABLE scratch_set_default_parent_{suffix} (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        f"CREATE TABLE scratch_set_default_child_{suffix} (id INTEGER PRIMARY KEY, "
        f"parent_id INTEGER {'' if nullable else 'NOT NULL '}"
        f"REFERENCES scratch_set_default_parent_{suffix} (id) ON DELETE SET DEFAULT)"
    )

    table = await DatabaseCatalog.inspect_table(connection, f"scratch_set_default_child_{suffix}")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    assert f"on_delete=OnDelete.{expected_on_delete}" in source
    assert "on_delete=OnDelete.SET_DEFAULT" not in source
    assert "# TODO: the database declares ON DELETE SET DEFAULT but the column has no DEFAULT" in source

    from hare.fields.enums import OnDelete
    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    generated_cls = namespace[f"ScratchSetDefaultChild{suffix.capitalize()}"]
    assert generated_cls._meta.fields_map["parent"].on_delete == OnDelete(expected_on_delete.replace("_", " "))


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_fk_on_delete_default_is_omitted(connection):
    """The common case (CASCADE, hare-orm's own default) shouldn't clutter every FK line."""
    table = await DatabaseCatalog.inspect_table(connection, "event")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    assert "on_delete" not in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_fk_target_uses_default_app_label(connection):
    """The FK target reference defaults to "models.<Target>" - the placeholder a project using
    `hare inspectdb` normally relocates by hand - when app_label isn't passed at all."""
    table = await DatabaseCatalog.inspect_table(connection, "employee")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    assert "'models.Employee'" in source


@requires_features(supports_foreign_keys=True)
@pytest.mark.asyncio
async def test_generate_model_source_fk_target_uses_given_app_label(connection):
    """A caller building a model outside the normal `hare inspectdb` file-generation flow (the
    admin's Live DB Explorer registers every reflected table under its own "_live_admin" app
    label, not "models") must get FK target references qualified to match, or resolving that
    relation crashes looking the target up in the wrong app."""
    table = await DatabaseCatalog.inspect_table(connection, "employee")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name, app_label="_live_admin")
    assert "'_live_admin.Employee'" in source
    assert "'models.Employee'" not in source


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_literal_db_defaults(connection):
    table = await DatabaseCatalog.inspect_table(connection, "defaultmodel")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "int_default = fields.IntField(db_default=1)" in source
    assert "float_default = fields.FloatField(db_default=1.5)" in source
    assert "char_default = fields.CharField(max_length=20, db_default='hare')" in source
    # id's own auto-increment sequence default must NOT be reconstructed as a db_default= kwarg -
    # it's already implied by primary_key=True, not a real user-declared default.
    assert "id = fields.IntField(primary_key=True)\n" in source


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_db_default_on_primary_key(connection):
    """A DB-level default on a PRIMARY KEY column (e.g. a random-token PK the database itself
    generates) used to be silently dropped entirely - db_default= lived only in the branch for
    non-PK columns, even though a PK carrying its own db_default is independent of being a PK
    (unlike null/unique/db_index, which really are implied/redundant on a PK)."""
    table = await DatabaseCatalog.inspect_table(connection, "pkwithdbdefaultmodel")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.fields.db_defaults import" in source
    assert "token" in source and "primary_key=True" in source and "SqlDefault(" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_bool_default_postgres(connection):
    """SQLite has no native boolean column type (BooleanField stores as plain INT there,
    indistinguishable from IntField on introspection) - only meaningful to assert the real
    reconstructed type on Postgres, which does have one."""
    table = await DatabaseCatalog.inspect_table(connection, "defaultmodel")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    assert "bool_default = fields.BooleanField(db_default=True)" in source


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_sql_expression_defaults(connection):
    table = await DatabaseCatalog.inspect_table(connection, "sql_default_model")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.fields.db_defaults import" in source
    assert "created_at = fields.DatetimeField(db_default=Now())" in source
    assert "counter = fields.IntField(db_default=0)" in source
    assert "tracking_id" in source and "SqlDefault(" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_decimal_precision(connection):
    """Postgres has a real NUMERIC type preserving precision/scale - SQLite stores DecimalField
    as VARCHAR(40) instead (see DecimalField._db_sqlite), indistinguishable from a genuine
    CharField on introspection, so this is Postgres-only."""
    table = await DatabaseCatalog.inspect_table(connection, "decimalfields")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "decimal = fields.DecimalField(max_digits=18, decimal_places=4)" in source
    assert "decimal_nodec = fields.DecimalField(max_digits=18, decimal_places=0)" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_column_description(connection):
    """SQLite has no column-comment feature at all - Postgres-only."""
    table = await DatabaseCatalog.inspect_table(connection, "comments")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "description='Comment messages entered in the blog post'" in source
    # embedded quotes/backticks/newlines in the real comment must still round-trip into valid
    # Python source (repr() handles the escaping) - not just the simple cases.
    assert 'description="This column acts as it\'s own comment"' in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    assert namespace["Comments"]._meta.fields_map["message"].description == "Comment messages entered in the blog post"


# ============================================================================
# Postgres-only index features: access method (GIN/GiST/...), opclasses, partial-index condition
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_gin_index(connection):
    await connection.execute_script("CREATE TABLE scratch_gin (id serial primary key, tags jsonb)")
    await connection.execute_script("CREATE INDEX idx_scratch_gin_tags ON scratch_gin USING gin (tags)")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_gin")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.dialects.postgresql.indexes import GinIndex" in source
    assert "GinIndex(fields=['tags'], name='idx_scratch_gin_tags')" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_hnsw_index(connection):
    """ "hnsw"/"ivfflat" (pgvector) were missing from _INDEX_TYPE_CLASS_PATHS entirely - falling
    through to the plain btree Index default, which a `vector` column has no default operator
    class for at all (re-running the generated migration would fail outright, not just lose
    tuning)."""
    try:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        pytest.skip(f"vector extension not available: {exc}")

    await connection.execute_script("CREATE TABLE scratch_hnsw (id serial primary key, embedding vector(3))")
    await connection.execute_script(
        "CREATE INDEX idx_scratch_hnsw_embedding ON scratch_hnsw USING hnsw (embedding vector_l2_ops)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_hnsw")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.dialects.postgresql.indexes import HnswIndex" in source
    assert "HnswIndex(fields=['embedding'], name='idx_scratch_hnsw_embedding', opclasses=['vector_l2_ops'])" in source
    assert "real tuning parameters" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_vector_index_storage_parameters(connection):
    """An HnswIndex/IvfflatIndex's WITH (...) parameters were never read back - the generated
    model always carried the class defaults instead of the real m/ef_construction/lists."""
    try:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        pytest.skip(f"vector extension not available: {exc}")

    await connection.execute_script("CREATE TABLE scratch_tuned (id serial primary key, embedding vector(3))")
    await connection.execute_script(
        "CREATE INDEX idx_scratch_tuned_hnsw ON scratch_tuned USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 8, ef_construction = 32)"
    )
    await connection.execute_script(
        "CREATE INDEX idx_scratch_tuned_ivfflat ON scratch_tuned USING ivfflat (embedding) WITH (lists = 5)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_tuned")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert (
        "HnswIndex(fields=['embedding'], name='idx_scratch_tuned_hnsw', opclasses=['vector_cosine_ops'], m=8, "
        "ef_construction=32)"
    ) in source
    assert "IvfflatIndex(fields=['embedding'], name='idx_scratch_tuned_ivfflat', lists=5)" in source
    assert "real tuning parameters" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_keeps_partial_index_condition_on_the_real_column(connection):
    """A partial index's condition is the database's own predicate, over the real column names -
    not the Python attribute name, which differs for a column that isn't a valid identifier."""
    await connection.execute_script('CREATE TABLE scratch_partial_column (id serial primary key, "Status Code" text)')
    await connection.execute_script(
        "CREATE INDEX idx_scratch_partial_column ON scratch_partial_column (id) WHERE \"Status Code\" = 'x'"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_partial_column")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert 'condition=RawSQLTerm(\'"Status Code" = ' in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_partial_index_condition(connection):
    await connection.execute_script("CREATE TABLE scratch_partial (id serial primary key, status text, category text)")
    await connection.execute_script(
        "CREATE INDEX idx_scratch_partial_1 ON scratch_partial (id) WHERE status = 'active'"
    )
    await connection.execute_script(
        "CREATE INDEX idx_scratch_partial_2 ON scratch_partial (id) WHERE status = 'active' AND category = 'x'"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_partial")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert (
        "PartialIndex(fields=['id'], name='idx_scratch_partial_1', "
        """condition=RawSQLTerm("status = 'active'::text"))"""
    ) in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    conditions = {index.condition for index in namespace["ScratchPartial"]._meta.indexes}
    assert conditions == {
        RawSQLTerm("status = 'active'::text"),
        RawSQLTerm("(status = 'active'::text) AND (category = 'x'::text)"),
    }


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_unique_partial_index(connection):
    """A UNIQUE partial index (btree + condition, the NULL-safe/conditional-uniqueness use case)
    used to lose its uniqueness entirely on reconstruction - under a name of its own it is a named
    UniqueConstraint(condition=...), the predicate kept as the database has it."""
    await connection.execute_script("CREATE TABLE scratch_unique_partial (id serial primary key, email text)")
    await connection.execute_script(
        "CREATE UNIQUE INDEX idx_scratch_unique_partial ON scratch_unique_partial (email) WHERE email IS NOT NULL"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_unique_partial")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "NOTE" not in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    (constraint,) = namespace["ScratchUniquePartial"]._meta.constraints
    assert (tuple(constraint.fields), constraint.name, constraint.condition) == (
        ("email",),
        "idx_scratch_unique_partial",
        RawSQLTerm("email IS NOT NULL"),
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_keeps_unparseable_condition_as_raw_sql(connection):
    """A predicate that isn't a plain AND-of-equalities is kept as raw SQL too, as the database
    reports it."""
    await connection.execute_script("CREATE TABLE scratch_unparseable (id serial primary key, price int)")
    await connection.execute_script("CREATE INDEX idx_scratch_price ON scratch_unparseable (id) WHERE price > 0")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_unparseable")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "PartialIndex(fields=['id'], name='idx_scratch_price', condition=RawSQLTerm('price > 0'))" in source
    assert "TODO" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_non_default_opclass(connection):
    """A non-default opclass alone doesn't preclude UNIQUE (only a non-btree access method does -
    Postgres itself accepts CREATE UNIQUE INDEX ... (col opclass) just fine) - Index(unique=True)
    faithfully reconstructs this, no NOTE/lost-uniqueness fallback needed."""
    await connection.execute_script("CREATE TABLE scratch_opclass (id serial primary key, code text)")
    await connection.execute_script(
        "CREATE UNIQUE INDEX idx_scratch_opclass ON scratch_opclass (code text_pattern_ops)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_opclass")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "Index(fields=['code'], name='idx_scratch_opclass', opclasses=['text_pattern_ops'], unique=True)" in source
    assert "NOTE" not in source


@pytest.mark.asyncio
async def test_generate_model_source_plain_index_has_no_special_kwargs(connection):
    """The common, everyday case (plain btree, default opclass, no condition) must render exactly
    as before this feature - no opclasses=/condition= clutter for a perfectly ordinary index."""
    table = await DatabaseCatalog.inspect_table(connection, "modelwithindexes")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    assert "opclasses=" not in source
    assert "condition=" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_table_description(connection):
    """SQLite has no table-comment feature at all - Postgres-only."""
    table = await DatabaseCatalog.inspect_table(connection, "comments")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "table_description = 'Test Table comment'" in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    assert namespace["Comments"]._meta.table_description == "Test Table comment"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_postgres_trigger(connection):
    """Postgres: the trigger AND its backing function are both read back into one Trigger(...)."""
    await connection.execute_script("CREATE TABLE scratch_trig (id serial primary key, hits int NOT NULL DEFAULT 0)")
    await connection.execute_script(
        "CREATE FUNCTION scratch_trig_bump_fn() RETURNS TRIGGER AS $body$\n"
        "BEGIN\n"
        "    NEW.hits := NEW.hits + 1;\n"
        "    RETURN NEW;\n"
        "END;\n"
        "$body$ LANGUAGE plpgsql"
    )
    await connection.execute_script(
        "CREATE TRIGGER scratch_trig_bump BEFORE UPDATE ON scratch_trig "
        "FOR EACH ROW EXECUTE FUNCTION scratch_trig_bump_fn()"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig")
    assert table.unparsed_triggers == []
    assert len(table.triggers) == 1
    trigger = table.triggers[0]
    assert trigger.name == "scratch_trig_bump"
    assert trigger.timing == "BEFORE"
    assert trigger.on == "UPDATE"
    assert trigger.for_each == "ROW"
    assert "NEW.hits := NEW.hits + 1;" in trigger.body.sql

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "from hare.ddl.schema_objects.trigger import Trigger" in source
    assert "Trigger(name='scratch_trig_bump'" in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    assert namespace["ScratchTrig"]._meta.triggers[0].name == "scratch_trig_bump"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_postgres_constraint_trigger(connection):
    """A real CONSTRAINT TRIGGER ... DEFERRABLE INITIALLY DEFERRED round-trips through
    inspectdb with its deferral semantics intact, not silently dropped or misparsed as a plain
    (non-deferrable) trigger."""
    await connection.execute_script(
        "CREATE TABLE scratch_trig_constraint (id serial primary key, hits int NOT NULL DEFAULT 0)"
    )
    await connection.execute_script(
        "CREATE FUNCTION scratch_trig_constraint_fn() RETURNS TRIGGER AS $body$\nBEGIN\n    RETURN NEW;\nEND;\n"
        "$body$ LANGUAGE plpgsql"
    )
    await connection.execute_script(
        "CREATE CONSTRAINT TRIGGER scratch_trig_constraint_bump AFTER INSERT ON scratch_trig_constraint "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION scratch_trig_constraint_fn()"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig_constraint")
    assert table.unparsed_triggers == []
    assert len(table.triggers) == 1
    trigger = table.triggers[0]
    assert trigger.name == "scratch_trig_constraint_bump"
    assert trigger.timing == "AFTER"
    assert trigger.on == "INSERT"
    assert trigger.for_each == "ROW"
    assert trigger.deferrable is True
    assert trigger.initially_deferred is True

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "deferrable=True" in source
    assert "initially_deferred=True" in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    reconstructed = namespace["ScratchTrigConstraint"]._meta.triggers[0]
    assert reconstructed.deferrable is True
    assert reconstructed.initially_deferred is True


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_postgres_constraint_trigger_initially_immediate(connection):
    """Same as the DEFERRED case above, but INITIALLY IMMEDIATE (Postgres's own default for a
    DEFERRABLE constraint) - confirms initially_deferred isn't hardcoded True whenever
    deferrable is True."""
    await connection.execute_script(
        "CREATE TABLE scratch_trig_constraint_imm (id serial primary key, hits int NOT NULL DEFAULT 0)"
    )
    await connection.execute_script(
        "CREATE FUNCTION scratch_trig_constraint_imm_fn() RETURNS TRIGGER AS $body$\nBEGIN\n    RETURN NEW;\nEND;\n"
        "$body$ LANGUAGE plpgsql"
    )
    await connection.execute_script(
        "CREATE CONSTRAINT TRIGGER scratch_trig_constraint_imm_bump AFTER INSERT ON scratch_trig_constraint_imm "
        "DEFERRABLE INITIALLY IMMEDIATE "
        "FOR EACH ROW EXECUTE FUNCTION scratch_trig_constraint_imm_fn()"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig_constraint_imm")
    assert table.unparsed_triggers == []
    assert len(table.triggers) == 1
    trigger = table.triggers[0]
    assert trigger.deferrable is True
    assert trigger.initially_deferred is False


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_falls_back_on_constraint_trigger_with_from_clause(connection):
    """A CONSTRAINT TRIGGER ... FROM other_table clause has no Trigger field to hold the
    referenced table - reconstructing without it would silently produce a different trigger, so
    this must fall back to an unparsed-trigger comment instead of a wrong Trigger(...)."""
    await connection.execute_script(
        "CREATE TABLE scratch_trig_from_a (id serial primary key)",
    )
    await connection.execute_script(
        "CREATE TABLE scratch_trig_from_b (id serial primary key)",
    )
    await connection.execute_script(
        "CREATE FUNCTION scratch_trig_from_fn() RETURNS TRIGGER AS $body$\nBEGIN\n    RETURN NEW;\nEND;\n"
        "$body$ LANGUAGE plpgsql"
    )
    await connection.execute_script(
        "CREATE CONSTRAINT TRIGGER scratch_trig_from_bump AFTER INSERT ON scratch_trig_from_a "
        "FROM scratch_trig_from_b "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION scratch_trig_from_fn()"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig_from_a")
    assert table.triggers == []
    assert len(table.unparsed_triggers) == 1
    assert table.unparsed_triggers[0][0] == "scratch_trig_from_bump"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_trigger_with_quoted_mixed_case_column(connection):
    """Regression test: blanket-uppercasing the whole `on` clause used to also uppercase a
    quoted, case-preserving column name inside `UPDATE OF "colName"` - producing a Trigger whose
    `on` referenced a column that doesn't actually exist, breaking any DDL generated from it."""
    await connection.execute_script('CREATE TABLE scratch_trig_case (id serial primary key, "mixedCol" int)')
    await connection.execute_script(
        "CREATE FUNCTION scratch_trig_case_fn() RETURNS TRIGGER AS $body$\nBEGIN\n    RETURN NEW;\nEND;\n"
        "$body$ LANGUAGE plpgsql"
    )
    await connection.execute_script(
        'CREATE TRIGGER scratch_trig_case_bump AFTER UPDATE OF "mixedCol" ON scratch_trig_case '
        "FOR EACH ROW EXECUTE FUNCTION scratch_trig_case_fn()"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig_case")
    assert table.unparsed_triggers == []
    assert len(table.triggers) == 1
    trigger = table.triggers[0]
    assert trigger.on == 'UPDATE OF "mixedCol"'

    # The reconstructed Trigger must actually be re-appliable against the real column.
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.fields.data.numeric import IntField
    from hare.migrations.operations import AddTrigger, CreateModel
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    editor = PostgresqlSchemaEditor(connection, atomic=True, collect_sql=False)
    create_op = CreateModel(
        name="ScratchTrigCaseCopy",
        fields=[("id", IntField(primary_key=True))],
        options={"table": "scratch_trig_case_copy"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await connection.execute_script('ALTER TABLE scratch_trig_case_copy ADD COLUMN "mixedCol" int')
        renamed_trigger = trigger.__class__(
            name="scratch_trig_case_copy_bump", on=trigger.on, body=trigger.body, timing=trigger.timing
        )
        add_op = AddTrigger(model_name="ScratchTrigCaseCopy", trigger=renamed_trigger)
        await add_op.run("models", state, dry_run=False, state_editor=editor)
    finally:
        try:
            await connection.execute_script("DROP TABLE IF EXISTS scratch_trig_case_copy CASCADE")
            await connection.execute_script("DROP FUNCTION IF EXISTS scratch_trig_case_copy_bump_fn()")
        except Exception:
            pass


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_sqlite_trigger(connection):
    await connection.execute_script("CREATE TABLE scratch_trig (id INTEGER PRIMARY KEY, hits INT NOT NULL DEFAULT 0)")
    await connection.execute_script(
        "CREATE TRIGGER scratch_trig_bump AFTER UPDATE ON scratch_trig BEGIN "
        "UPDATE scratch_trig SET hits = hits + 1 WHERE id = NEW.id; "
        "END"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_trig")
    assert table.unparsed_triggers == []
    assert len(table.triggers) == 1
    trigger = table.triggers[0]
    assert trigger.name == "scratch_trig_bump"
    assert trigger.timing == "AFTER"
    assert trigger.on == "UPDATE"
    assert trigger.for_each == "ROW"
    assert "UPDATE scratch_trig SET hits = hits + 1 WHERE id = NEW.id;" in trigger.body.sql

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "Trigger(name='scratch_trig_bump'" in source


@pytest.mark.asyncio
async def test_generate_model_source_no_meta_block_when_nothing_to_say(connection):
    """A table with no multi-column index/unique_together/table comment shouldn't get an empty
    class Meta: block at all."""
    table = await DatabaseCatalog.inspect_table(connection, "tournament")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    assert "class Meta:" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_range_field_types(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_ranges (id serial primary key, counts int4range, span daterange, during tstzrange)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_ranges")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "counts = IntRangeField(null=True)" in source
    assert "span = DateRangeField(null=True)" in source
    assert "during = DateTimeRangeField(null=True)" in source
    assert "from hare.dialects.postgresql.fields.ranges import" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_exclusion_constraint(connection):
    """Regression test: the EXCLUDE constraint's own backing index used to ALSO get reverse-
    engineered as a separate (meaningless - it captures none of the WITH operators) GistIndex in
    Meta.indexes, duplicating it. Also regression for a greedy operator regex that captured a
    trailing comma ("=," instead of "=") for every expression but the last."""
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist")
    await connection.execute_script(
        "CREATE TABLE scratch_booking (id serial primary key, resource int not null, during tstzrange not null)"
    )
    await connection.execute_script(
        "ALTER TABLE scratch_booking ADD CONSTRAINT no_overlap EXCLUDE USING gist (resource WITH =, during WITH &&)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_booking")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "ExclusionConstraint(name='no_overlap'" in source
    assert "expressions=(('resource', '='), ('during', '&&'))" in source
    assert "GistIndex" not in source

    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    generated_cls = namespace["ScratchBooking"]
    assert len(generated_cls._meta.constraints) == 1
    assert generated_cls._meta.constraints[0].name == "no_overlap"
    assert generated_cls._meta.constraints[0].expressions == (("resource", "="), ("during", "&&"))


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_exclusion_constraint_with_condition(connection):
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist")
    await connection.execute_script(
        "CREATE TABLE scratch_booking_cond (id serial primary key, resource int not null, "
        "during tstzrange not null, cancelled boolean not null default false)"
    )
    await connection.execute_script(
        "ALTER TABLE scratch_booking_cond ADD CONSTRAINT no_overlap_active "
        "EXCLUDE USING gist (resource WITH =, during WITH &&) WHERE (NOT cancelled)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_booking_cond")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "condition=RawSQLTerm('(NOT cancelled)')" in source


# ============================================================================
# Postgres type-mapping edge cases: bare "character" (bpchar), unrestricted "character varying"
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_flags_bare_character_as_ambiguous(connection):
    """Postgres's CHAR(n)/bpchar (information_schema.columns.data_type == "character") is
    fixed-width (space-padded to n on read) - hare-orm's CharField always generates VARCHAR(n)
    (variable-length), so silently mapping bare "character" to CharField would change the
    column's real semantics the next time a migration diffs against it, the same class of bug
    as "timestamp without time zone" silently becoming a tz-aware DatetimeField. Must fall
    through to the ambiguous/TextField fallback with a TODO instead of guessing."""
    await connection.execute_script("CREATE TABLE scratch_bpchar (id serial primary key, code character(5))")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_bpchar")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "CharField" not in source
    assert "code = fields.TextField(null=True)" in source
    assert "TODO" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_unrestricted_varchar_stays_unbounded(connection):
    """An unrestricted "character varying" (no explicit length - genuinely unbounded at the DB
    level, character_maximum_length reports NULL/None for it) used to still get a guessed
    max_length=255 CharField - silently imposing a cap the real column doesn't have. Must map to
    TextField instead, which has no length ceiling, matching the column's real nature."""
    await connection.execute_script(
        "CREATE TABLE scratch_unrestricted_varchar (id serial primary key, notes character varying)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_unrestricted_varchar")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "notes = fields.TextField(null=True)" in source
    assert "CharField" not in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_array_field(connection):
    """A Postgres ARRAY column (data_type == "ARRAY") used to fall into the ambiguous TextField
    fallback - udt_name carries the real element type ("_int4" for integer[]), which lets this
    reconstruct as a proper ArrayField(base_field=...)."""
    await connection.execute_script("CREATE TABLE scratch_array (id serial primary key, tags integer[])")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_array")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "tags = fields.ArrayField(base_field=fields.IntField(), null=True)" in source
    assert "TODO" not in source

    from hare.fields import ArrayField
    from hare.models import Model

    namespace: dict = {"Model": Model, "ArrayField": ArrayField, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    assert isinstance(namespace["ScratchArray"]._meta.fields_map["tags"], ArrayField)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_array_of_range_field(connection):
    """POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP only listed plain scalar element udt_names - an array of a
    range type (int4range[]) has its own real, unambiguous "_int4range" udt_name (unlike a
    multi-dimensional array, which reports the identical udt_name regardless of dimensionality
    and stays a genuinely unfixable case), so it used to fall into the same ambiguous/TextField
    fallback as any other unmapped element type, losing the array structure entirely."""
    await connection.execute_script("CREATE TABLE scratch_range_array (id serial primary key, spans int4range[])")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_range_array")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "spans = fields.ArrayField(base_field=IntRangeField(), null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchRangeArray")
    from hare.dialects.postgresql.fields.ranges import IntRangeField
    from hare.fields import ArrayField

    field = generated_cls._meta.fields_map["spans"]
    assert isinstance(field, ArrayField)
    assert isinstance(field.base_field, IntRangeField)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_array_of_numeric_precision(connection):
    """information_schema.columns.numeric_precision/numeric_scale are NULL for an ARRAY column
    (only ever populated for a scalar one) - POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP's own
    max_digits=20/decimal_places=6 defaults used to be applied unconditionally, silently losing
    the array element's real precision/scale (NUMERIC(10,2)[] round-tripping into
    NUMERIC(20,6)[]) with no TODO. The real size is now recovered from format_type()'s own
    "numeric(p,s)[]" rendering instead, the same way vector_dimensions already is for pgvector."""
    await connection.execute_script(
        "CREATE TABLE scratch_array_numeric (id serial primary key, amounts numeric(10,2)[])"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_array_numeric")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert (
        "amounts = fields.ArrayField(base_field=fields.DecimalField(max_digits=10, decimal_places=2), null=True)"
        in source
    )
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchArrayNumeric")
    from hare.fields import ArrayField
    from hare.fields.data.numeric import DecimalField

    field = generated_cls._meta.fields_map["amounts"]
    assert isinstance(field, ArrayField)
    assert isinstance(field.base_field, DecimalField)
    assert field.base_field.max_digits == 10
    assert field.base_field.decimal_places == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_array_of_varchar_length(connection):
    """Same root cause as the numeric[] case above: character_maximum_length is NULL for an
    ARRAY column, so a varchar(12)[] used to silently round-trip into CharField(max_length=255)
    - not just a different DDL, but one that ACCEPTS longer strings than the original schema
    ever allowed, with no TODO. The real element length is now recovered from format_type()'s
    own "character varying(N)[]" rendering."""
    await connection.execute_script("CREATE TABLE scratch_array_varchar (id serial primary key, codes varchar(12)[])")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_array_varchar")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "codes = fields.ArrayField(base_field=fields.CharField(max_length=12), null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchArrayVarchar")
    from hare.fields import ArrayField
    from hare.fields.data.text import CharField

    field = generated_cls._meta.fields_map["codes"]
    assert isinstance(field, ArrayField)
    assert isinstance(field.base_field, CharField)
    assert field.base_field.max_length == 12


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_flags_user_defined_type_as_todo(connection):
    """A Postgres USER-DEFINED column (an enum or composite type) can't be reconstructed as a
    real field automatically - it must fall back to TextField with an explicit TODO naming the
    real udt_name, not a silent, unmarked TextField."""
    await connection.execute_script("DROP TYPE IF EXISTS scratch_mood_enum CASCADE")
    await connection.execute_script("CREATE TYPE scratch_mood_enum AS ENUM ('happy', 'sad')")
    await connection.execute_script("CREATE TABLE scratch_enum (id serial primary key, mood scratch_mood_enum)")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_enum")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "mood = fields.TextField(null=True)" in source
    assert "TODO" in source and "scratch_mood_enum" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_citext_field(connection):
    """A Postgres CITEXT column reports as data_type == "USER-DEFINED" (same as an enum) with
    udt_name == "citext" - it used to fall into the same generic ambiguous/TextField fallback as
    any other unrecognized user-defined type, losing case-insensitivity and the citext extension
    link entirely. It's a real, unambiguous hare-orm field (CitextField) and must be reconstructed
    as one, not left as a TODO."""
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS citext")
    await connection.execute_script("CREATE TABLE scratch_citext (id serial primary key, email citext)")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_citext")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.dialects.postgresql.fields import CitextField" in source
    assert "email = CitextField(null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchCitext")
    from hare.dialects.postgresql.fields.citext_field import CitextField

    assert isinstance(generated_cls._meta.fields_map["email"], CitextField)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_tsvector_field(connection):
    """A Postgres TSVECTOR column reports as a plain base type (data_type == "tsvector", not
    "USER-DEFINED") - it used to fall through the whole POSTGRESQL_TYPE_MAP keyword loop entirely
    (no "tsvector" entry there) and land in the generic ambiguous/TextField fallback, losing full-
    text search semantics entirely. It's a real, unambiguous hare-orm field (TSVectorField) and
    must be reconstructed as one, not left as a TODO."""
    await connection.execute_script("CREATE TABLE scratch_tsvector (id serial primary key, body tsvector)")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_tsvector")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.dialects.postgresql.fields import TSVectorField" in source
    assert "body = TSVectorField(null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchTsvector")
    from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField

    assert isinstance(generated_cls._meta.fields_map["body"], TSVectorField)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_postgis_geography_field(connection):
    """A PostGIS geography(Point,4326) column reports as data_type == "USER-DEFINED" (same as an
    enum) with udt_name == "geography" - it used to fall into the same generic ambiguous/
    TextField fallback as any other unrecognized user-defined type, losing geospatial semantics
    entirely. It's a real, unambiguous hare-orm field (PostGISField's only supported shape) and
    must be reconstructed as one, not left as a TODO."""
    try:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS postgis")
    except Exception:
        pytest.skip("PostGIS extension not installable on this Postgres")
    await connection.execute_script(
        "CREATE TABLE scratch_geography (id serial primary key, location geography(Point,4326))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_geography")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.dialects.postgresql.fields import PostGISField" in source
    assert "location = PostGISField(null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchGeography")
    from hare.dialects.postgresql.fields.postgis_field import PostGISField

    assert isinstance(generated_cls._meta.fields_map["location"], PostGISField)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_vector_field(connection):
    """A pgvector vector(N) column reports as data_type == "USER-DEFINED" (same as an enum) with
    udt_name == "vector" - it used to fall into the same generic ambiguous/TextField fallback as
    any other unrecognized user-defined type, losing both the real field type AND the dimension
    count (VectorField's dimensions= is a required constructor argument). Must be reconstructed
    as VectorField(dimensions=768), not left as a TODO."""
    try:
        await connection.execute_script("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception:
        pytest.skip("pgvector extension not installable on this Postgres")
    await connection.execute_script("CREATE TABLE scratch_vector (id serial primary key, embedding vector(768))")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_vector")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)

    compile(source, "<generated>", "exec")
    assert "from hare.vectors import VectorField" in source
    assert "embedding = VectorField(dimensions=768, null=True)" in source
    assert "TODO" not in source

    generated_cls = await _load_generated_model(source, "ScratchVector")
    from hare.vectors import VectorField

    generated_field = generated_cls._meta.fields_map["embedding"]
    assert isinstance(generated_field, VectorField)
    assert generated_field.dimensions == 768


# ============================================================================
# Postgres expression indexes: the indkey/pg_attribute join used to silently drop an
# expression-index term (attnum = 0 never matches pg_attribute) with no fallback marker. Now
# reconstructed via Index(RawSQLTerm(...), ...) - hare.ddl.raw_sql_term.RawSQLTerm was
# already used elsewhere (MigrationWriter.render_value(), for any value with its own get_sql())
# but inspectdb itself never reached for it, falling back to a TODO comment unconditionally
# instead of actually reconstructing the index.
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_pure_expression_index(connection):
    """A pure expression index (every key term an expression, not a plain column) used to
    silently vanish entirely from the introspected schema - the inner join against pg_attribute
    drops the whole row when NO term resolves to a real attnum. Now reconstructed as
    Index(RawSQLTerm('lower(name)'))."""
    await connection.execute_script("CREATE TABLE scratch_expr_index (id serial primary key, name text)")
    await connection.execute_script("CREATE INDEX idx_scratch_expr_lower ON scratch_expr_index (lower(name))")

    table = await DatabaseCatalog.inspect_table(connection, "scratch_expr_index")
    assert table.unparsed_indexes == []
    assert len(table.indexes) == 1
    assert table.indexes[0].expression_terms == ["lower(name)"]
    assert table.indexes[0].columns == []

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "TODO" not in source
    assert "indexes = [Index(RawSQLTerm('lower(name)'), name='idx_scratch_expr_lower')]" in source

    generated_cls = await _load_generated_model(source, "ScratchExprIndex")
    assert len(generated_cls._meta.indexes) == 1
    assert generated_cls._meta.indexes[0].field_names == ["(lower(name))"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_mixed_expression_index(connection):
    """A mixed index (a real column plus an expression term) used to silently keep only the real
    column in the reconstructed Index(fields=[...]) - quietly losing the expression term instead
    of flagging the index as not fully reconstructable. Now reconstructed as
    Index(RawSQLTerm('category'), RawSQLTerm('lower(name)')) - Index.__init__ makes fields= and
    expressions mutually exclusive, so once ANY term needs RawSQLTerm, every term (the real
    column included) is rendered that way."""
    await connection.execute_script(
        "CREATE TABLE scratch_expr_mixed (id serial primary key, category text, name text)"
    )
    await connection.execute_script(
        "CREATE INDEX idx_scratch_expr_mixed ON scratch_expr_mixed (category, lower(name))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_expr_mixed")
    assert table.unparsed_indexes == []
    # must not show up as a (wrong, partial) Index(fields=['category'])
    assert not any(index.columns == ["category"] for index in table.indexes)
    assert len(table.indexes) == 1
    assert table.indexes[0].expression_terms == ["category", "lower(name)"]

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "TODO" not in source
    assert (
        "indexes = [Index(RawSQLTerm('category'), RawSQLTerm('lower(name)'), name='idx_scratch_expr_mixed')]"
    ) in source

    generated_cls = await _load_generated_model(source, "ScratchExprMixed")
    assert generated_cls._meta.indexes[0].field_names == ["(category)", "(lower(name))"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_falls_back_on_expression_index_with_opclass(connection):
    """An expression term combined with a non-default operator class can't round-trip through
    Index(*expressions) either: Postgres's own per-term ruleutils output
    (pg_get_indexdef(oid, colno, true)) never includes the opclass suffix, and there's no valid
    way to splice it back in - Index.field_names wraps every expression term in its own parens,
    and `(col opclass)` (opclass INSIDE the parens) is a Postgres syntax error; only `(col)
    opclass` (outside) is valid. Stays in unparsed_indexes, honestly flagged, rather than a
    silently wrong/truncated RawSQLTerm."""
    await connection.execute_script("CREATE TABLE scratch_expr_opclass (id serial primary key, name text)")
    await connection.execute_script(
        "CREATE INDEX idx_scratch_expr_opclass ON scratch_expr_opclass (name text_pattern_ops, lower(name))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_expr_opclass")
    assert table.indexes == []
    names = {name for name, _ in table.unparsed_indexes}
    assert "idx_scratch_expr_opclass" in names

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "TODO" in source and "idx_scratch_expr_opclass" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructed_expression_index_is_reappliable(connection):
    """The reconstructed Index(RawSQLTerm(...), ...) must produce valid, re-appliable CREATE
    INDEX SQL against Postgres - not just compile as Python."""
    await connection.execute_script(
        "CREATE TABLE scratch_expr_reapply (id serial primary key, category text, name text)"
    )
    await connection.execute_script(
        "CREATE INDEX idx_scratch_expr_reapply ON scratch_expr_reapply (category, lower(name))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_expr_reapply")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    generated_cls = await _load_generated_model(source, "ScratchExprReapply")
    index = generated_cls._meta.indexes[0]
    # The reconstructed index keeps its database name - the original gives it up for the copy.
    await connection.execute_script("DROP INDEX idx_scratch_expr_reapply")

    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
    from hare.fields.data.numeric import IntField
    from hare.fields.data.text import TextField
    from hare.migrations.operations import CreateModel
    from hare.migrations.state.state import State
    from hare.migrations.state.state_apps import StateApps

    editor = PostgresqlSchemaEditor(connection, atomic=True, collect_sql=False)
    create_op = CreateModel(
        name="ScratchExprReapplyCopy",
        fields=[
            ("id", IntField(primary_key=True)),
            ("category", TextField(null=True)),
            ("name", TextField(null=True)),
        ],
        options={"table": "scratch_expr_reapply_copy"},
    )
    state = State(models={}, apps=StateApps())
    try:
        await create_op.run("models", state, dry_run=False, state_editor=editor)
        await editor.add_index(create_op.model, index)

        rows = await connection.execute_dicts(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'scratch_expr_reapply_copy'"
        )
        assert any("lower(name)" in row["indexdef"] for row in rows)
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_expr_reapply_copy CASCADE")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_exclusion_constraint_with_expression_term(connection):
    """A mixed EXCLUDE constraint (a plain column term plus an expression term, e.g.
    "resource WITH =, lower(name) WITH =") used to silently drop the expression term entirely -
    _EXCLUSION_EXPRESSION_RE only matches a bare identifier before " WITH ", so "lower(name) WITH
    =" simply produced no match and vanished, reconstructing a WRONG ExclusionConstraint with only
    the "resource" term and no indication anything was lost. Later fixed to fall back to an
    unparsed-constraint comment instead of silently dropping the term - now fixed further to
    reconstruct the expression term as a RawSQLTerm, the same representation a hand-written
    ExclusionConstraint already uses for this exact case."""
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist")
    await connection.execute_script(
        "CREATE TABLE scratch_excl_expr (id serial primary key, resource int not null, name text not null)"
    )
    await connection.execute_script(
        "ALTER TABLE scratch_excl_expr ADD CONSTRAINT excl_expr_mixed "
        "EXCLUDE USING gist (resource WITH =, lower(name) WITH =)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_excl_expr")
    assert table.unparsed_exclusion_constraints == []
    (constraint,) = table.exclusion_constraints
    assert constraint.name == "excl_expr_mixed"
    assert constraint.expressions[0] == ("resource", "=")
    raw_field, raw_operator = constraint.expressions[1]
    assert isinstance(raw_field, RawSQLTerm)
    assert raw_field.sql == "lower(name)"
    assert raw_operator == "="

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "constraints = [" in source
    assert "RawSQLTerm('lower(name)')" in source
    assert "TODO" not in source


# ============================================================================
# Postgres non-public schemas: every introspection query used to hardcode table_schema='public',
# silently making a table in any other schema invisible, with no way to reach it at all.
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_get_table_names_respects_schema_argument(connection):
    await connection.execute_script("CREATE SCHEMA IF NOT EXISTS scratch_other_schema")
    await connection.execute_script("CREATE TABLE scratch_other_schema.other_widget (id serial primary key)")
    try:
        public_tables = await DatabaseCatalog.get_table_names(connection)
        assert "other_widget" not in public_tables

        other_tables = await DatabaseCatalog.get_table_names(connection, schema="scratch_other_schema")
        assert "other_widget" in other_tables
    finally:
        await connection.execute_script("DROP SCHEMA scratch_other_schema CASCADE")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_non_public_schema(connection):
    """A table living in a non-public Postgres schema used to generate a model with no
    Meta.schema at all - re-applying the generated model would then silently target "public"
    instead, physically relocating the table rather than just losing metadata."""
    await connection.execute_script("CREATE SCHEMA IF NOT EXISTS scratch_other_schema")
    await connection.execute_script("CREATE TABLE scratch_other_schema.scratch_widget (id serial primary key)")
    try:
        table = await DatabaseCatalog.inspect_table(connection, "scratch_widget", schema="scratch_other_schema")
        assert table.schema == "scratch_other_schema"

        source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
        compile(source, "<generated>", "exec")
        assert "schema = 'scratch_other_schema'" in source

        generated_cls = await _load_generated_model(source, "ScratchWidget")
        assert generated_cls._meta.schema == "scratch_other_schema"
    finally:
        await connection.execute_script("DROP SCHEMA scratch_other_schema CASCADE")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_omits_schema_meta_for_public_schema(connection):
    """A table in the default "public" schema must not grow a redundant Meta.schema = 'public'
    line - only a genuinely non-default schema should render one."""
    table = await DatabaseCatalog.inspect_table(connection, "tournament")
    assert table.schema == "public"

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    assert "schema = " not in source


async def _load_generated_model(source: str, class_name: str):
    from hare.models import Model

    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    return namespace[class_name]


# ============================================================================
# Identifier safety: generate_model_source didn't validate that a table/column name is actually
# usable as a Python identifier - a digit-leading table name or a keyword-colliding column name
# produced a SyntaxError in the generated output.
# ============================================================================


@pytest.mark.asyncio
async def test_generate_model_source_sanitizes_digit_leading_table_name(connection):
    await connection.execute_script('CREATE TABLE "2fa_codes" (id INTEGER PRIMARY KEY, code TEXT NOT NULL)')

    table = await DatabaseCatalog.inspect_table(connection, "2fa_codes")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "class 2fa" not in source


@pytest.mark.asyncio
async def test_generate_model_source_sanitizes_keyword_column_name(connection):
    """A column literally named "class" (a valid SQL identifier, quoted) can't be used as-is as
    a Python attribute name - it's a reserved keyword."""
    await connection.execute_script('CREATE TABLE scratch_keyword_column (id INTEGER PRIMARY KEY, "class" TEXT)')

    table = await DatabaseCatalog.inspect_table(connection, "scratch_keyword_column")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "class = fields" not in source
    assert "source_field='class'" in source

    generated_cls = await _load_generated_model(source, "ScratchKeywordColumn")
    field = generated_cls._meta.fields_map["class_"]
    assert field.source_field == "class"


@pytest.mark.asyncio
async def test_generate_model_source_sanitizes_keyword_fk_shadow_column(connection):
    """The FK branch derives its own attribute name from the raw column ("class_id" -> "class"),
    independently of the plain-field branch above - a raw FK column named to collide with a
    keyword once the "_id" suffix is stripped needed the same sanitizing."""
    await connection.execute_script("CREATE TABLE scratch_keyword_fk_parent (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        'CREATE TABLE scratch_keyword_fk_child (id INTEGER PRIMARY KEY, "class_id" INTEGER '
        "REFERENCES scratch_keyword_fk_parent (id))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_keyword_fk_child")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "class = fields" not in source
    assert "source_field='class_id'" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_table_respects_schema_argument(connection):
    """Exercises the query that reads columns (table_schema=) AND the one that reads
    indexes/uniqueness (ns.nspname=) against a real non-public-schema table, not just the
    table-existence check - both used to hardcode 'public' independently."""
    await connection.execute_script("CREATE SCHEMA IF NOT EXISTS scratch_other_schema")
    await connection.execute_script(
        "CREATE TABLE scratch_other_schema.other_widget (id serial primary key, name text not null, code text unique)"
    )
    try:
        with pytest.raises(TableNotFoundError):
            await DatabaseCatalog.inspect_table(connection, "other_widget")

        table = await DatabaseCatalog.inspect_table(connection, "other_widget", schema="scratch_other_schema")
        column_names = {column.name for column in table.columns}
        assert column_names == {"id", "name", "code"}
        by_name = {column.name: column for column in table.columns}
        assert by_name["code"].is_unique

        source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
        compile(source, "<generated>", "exec")
        assert "class OtherWidget(Model):" in source
        assert "code = fields.TextField(null=True, unique=True)" in source
    finally:
        await connection.execute_script("DROP SCHEMA scratch_other_schema CASCADE")


# ============================================================================
# Composite foreign keys - reconstructed as a single ForeignKeyField(to_field=(...)) when the
# real DB column names follow the deterministic "<field>_<pk_component>" shadow-column naming
# ForeignKeyFieldInstance's composite support requires (see apps.py's init_foreign_key_or_one_to_one_field) and the
# FK references the target's whole declared primary key; surfaced honestly as unparsed otherwise
# (RelationalField.source_field is a single string, not a tuple, so there's no way to override a
# composite FK's per-column DB names individually when they don't already fit that convention).
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_composite_foreign_key(connection):
    """A composite FK whose real column names DO follow the "<field>_<pk_component>" convention
    (parent_a/parent_b, referencing parent's own (a, b) primary key) is reconstructed as a single
    ForeignKeyField - not the previous unconditional unparsed-comment fallback."""
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_parent2 (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a, b))"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_child2 (id INTEGER PRIMARY KEY, parent_a INT NOT NULL, "
        "parent_b INT NOT NULL, FOREIGN KEY (parent_a, parent_b) REFERENCES scratch_composite_fk_parent2 (a, b) "
        "ON DELETE CASCADE)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_fk_child2")
    assert table.unparsed_foreign_keys == []
    assert "parent_a" not in table.foreign_keys
    assert "parent_b" not in table.foreign_keys
    assert len(table.composite_foreign_keys) == 1
    cfk = table.composite_foreign_keys[0]
    assert cfk.field_name == "parent"
    assert cfk.columns == ("parent_a", "parent_b")
    assert cfk.target_table == "scratch_composite_fk_parent2"

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "TODO" not in source
    assert "parent = fields.ForeignKeyField('models.ScratchCompositeFkParent2', db_index=False)" in source
    # the shadow columns are auto-derived by the ORM itself from the one ForeignKeyField above -
    # must NOT also appear as independent plain IntField()s.
    assert "parent_a = " not in source
    assert "parent_b = " not in source

    # to_field_names/the auto-derived shadow columns ("parent_a"/"parent_b" as real entries in
    # fields_map) are only resolved once the model goes through real Apps registration
    # (apps.py's init_foreign_key_or_one_to_one_field, run by Apps.init()) - a bare exec() of the generated source,
    # with no HareContext around it, never reaches that step. See
    # test_round_trip_composite_foreign_key_model_is_queryable below for that part, through a
    # real HareContext against the real DB instead.
    generated_cls = await _load_generated_model(source, "ScratchCompositeFkChild2")
    field = generated_cls._meta.fields_map["parent"]
    from hare.fields.relations.fields import ForeignKeyFieldInstance

    assert isinstance(field, ForeignKeyFieldInstance)
    assert field.model_name == "models.ScratchCompositeFkParent2"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_composite_set_default_foreign_key(connection):
    """A composite FK's ON DELETE SET DEFAULT is only importable as on_delete=SET_DEFAULT with the
    members' shared DEFAULT carried as db_default; without one the action the database really
    performs (NULL / failure) is rendered instead, with a TODO."""
    await connection.execute_script(
        "CREATE TABLE scratch_composite_set_default_parent (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a, b))"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_composite_set_default_with (id INTEGER PRIMARY KEY, parent_a INT NOT NULL DEFAULT 0, "
        "parent_b INT NOT NULL DEFAULT 0, FOREIGN KEY (parent_a, parent_b) "
        "REFERENCES scratch_composite_set_default_parent (a, b) ON DELETE SET DEFAULT)"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_composite_set_default_without (id INTEGER PRIMARY KEY, parent_a INT, "
        "parent_b INT, FOREIGN KEY (parent_a, parent_b) "
        "REFERENCES scratch_composite_set_default_parent (a, b) ON DELETE SET DEFAULT)"
    )

    from hare.fields.enums import OnDelete

    with_default_table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_set_default_with")
    with_default_source = ModelSourceGenerator.generate_model_source(with_default_table, connection.dialect.name)
    assert "on_delete=OnDelete.SET_DEFAULT" in with_default_source
    assert "db_default=0" in with_default_source
    assert "TODO" not in with_default_source
    generated_cls = await _load_generated_model(with_default_source, "ScratchCompositeSetDefaultWith")
    assert generated_cls._meta.fields_map["parent"].on_delete == OnDelete.SET_DEFAULT

    without_default_table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_set_default_without")
    without_default_source = ModelSourceGenerator.generate_model_source(without_default_table, connection.dialect.name)
    assert "on_delete=OnDelete.SET_NULL" in without_default_source
    assert "# TODO: the database declares ON DELETE SET DEFAULT but the column has no DEFAULT" in (
        without_default_source
    )
    generated_cls = await _load_generated_model(without_default_source, "ScratchCompositeSetDefaultWithout")
    assert generated_cls._meta.fields_map["parent"].on_delete == OnDelete.SET_NULL


@pytest.mark.asyncio
async def test_round_trip_composite_foreign_key_model_is_queryable(tmp_path, monkeypatch):
    """The reconstructed composite ForeignKeyField must be functionally correct through a real
    HareContext against the real DB - not just syntactically valid Python. Unlike every other
    test in this file, this one can't reuse the shared `connection` fixture: apps.py's
    init_foreign_key_or_one_to_one_field (which resolves to_field/the auto-derived shadow columns) only runs during
    real Apps/model registration, so the generated source has to be written to a real importable
    module and registered through its own HareContext, not just exec()'d in a bare namespace.

    Skips manually (instead of via @requires_features) for the same reason: that decorator's
    capability check runs before the test body, against whatever HareContext happens to already
    be "current" - since this test deliberately opens its own context rather than taking the
    shared `connection`/`db` fixture, nothing guarantees one is active yet at that point (it only
    ever appeared to work by accident, piggybacking on a still-open context left behind by an
    earlier test in the same module when the whole file/suite ran together; running this test in
    true isolation raised "No HareContext is currently active" instead of skipping cleanly)."""
    import importlib
    import os
    import uuid

    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.core.connections.connections import Connections

    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url

    async with hare_test_context(modules=["tests.testmodels"], db_url=db_url, _create_db=True):
        connection = Connections.get(next(iter(Connections.current().db_config)))
        if connection.dialect.name != "postgresql":
            pytest.skip("Capability dialect != postgres")
        await connection.execute_script(
            "CREATE TABLE scratch_composite_fk_parent3 (a INT NOT NULL, b INT NOT NULL, name TEXT NOT NULL, "
            "PRIMARY KEY (a, b))"
        )
        await connection.execute_script(
            "CREATE TABLE scratch_composite_fk_child3 (id SERIAL PRIMARY KEY, parent_a INT NOT NULL, "
            "parent_b INT NOT NULL, FOREIGN KEY (parent_a, parent_b) REFERENCES scratch_composite_fk_parent3 (a, b) "
            "ON DELETE CASCADE)"
        )
        await connection.execute_script(
            "INSERT INTO scratch_composite_fk_parent3 (a, b, name) VALUES (1, 2, 'target')"
        )
        await connection.execute_script("INSERT INTO scratch_composite_fk_child3 (parent_a, parent_b) VALUES (1, 2)")

        parent_table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_fk_parent3")
        child_table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_fk_child3")
        source = "\n\n\n".join(
            [
                ModelSourceGenerator.generate_model_source(parent_table, connection.dialect.name),
                ModelSourceGenerator.generate_model_source(child_table, connection.dialect.name),
            ]
        )

        module_name = f"generated_inspectdb_composite_fk_{uuid.uuid4().hex}"
        (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()

        async with hare_test_context(
            modules=[module_name], db_url=db_url, _create_db=False, _generate_schemas=False, _drop_db_on_exit=False
        ):
            module = importlib.import_module(module_name)
            child_cls = module.ScratchCompositeFkChild3

            row = await child_cls.objects.all().first()
            parent = await row.parent
            assert (parent.a, parent.b, parent.name) == (1, 2, "target")


# ============================================================================
# A single-column FK targeting a UNIQUE (not PK) column on the target table: generator.py's
# single-column FK render path used to never emit to_field= at all (unlike its composite-FK
# sibling above, which already reconstructs it), so ForeignKeyFieldInstance silently fell back to
# the target's PK - pointing the relation at the WRONG column entirely.
# ============================================================================


@pytest.mark.asyncio
async def test_generate_model_source_reconstructs_single_column_fk_to_non_pk_unique_target(connection):
    """Dialect-agnostic (SQLite and Postgres both reach the same generator.py render path) -
    checks ForeignKeyInfo.to_field and the generated source text directly, without the full
    Apps-registration round trip (see the dedicated round-trip test below for that, Postgres-only
    for the same reasoning as the composite-FK one above)."""
    await connection.execute_script(
        "CREATE TABLE scratch_fk_unique_target5 (id INTEGER PRIMARY KEY, code VARCHAR(50) NOT NULL UNIQUE)"
        if connection.dialect.name == "sqlite"
        else "CREATE TABLE scratch_fk_unique_target5 (id serial PRIMARY KEY, code VARCHAR(50) NOT NULL UNIQUE)"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_fk_unique_source5 (id INTEGER PRIMARY KEY, "
        "target_id VARCHAR(50) NOT NULL REFERENCES scratch_fk_unique_target5 (code))"
        if connection.dialect.name == "sqlite"
        else "CREATE TABLE scratch_fk_unique_source5 (id serial PRIMARY KEY, "
        "target_id VARCHAR(50) NOT NULL REFERENCES scratch_fk_unique_target5 (code))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_fk_unique_source5")
    fk = table.foreign_keys["target_id"]
    assert fk.target_column == "code"
    assert fk.to_field == "code"

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "to_field='code'" in source


@pytest.mark.asyncio
async def test_round_trip_single_column_fk_to_non_pk_unique_target_is_queryable(tmp_path, monkeypatch):
    """Mirrors test_round_trip_composite_foreign_key_model_is_queryable's own reasoning and
    Postgres-only skip (apps.py's to_field resolution only runs during real Apps registration)."""
    import importlib
    import os
    import uuid

    from hare.contrib.test.isolated_contexts import hare_test_context
    from hare.core.connections.connections import Connections

    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:").replace("\\{", "{").replace("\\}", "}")
    db_url = raw_db_url.format(uuid.uuid4().hex) if "{}" in raw_db_url else raw_db_url

    async with hare_test_context(modules=["tests.testmodels"], db_url=db_url, _create_db=True):
        connection = Connections.get(next(iter(Connections.current().db_config)))
        if connection.dialect.name != "postgresql":
            pytest.skip("Capability dialect != postgres")

        await connection.execute_script(
            "CREATE TABLE scratch_fk_unique_target4 (id serial primary key, code varchar(50) NOT NULL UNIQUE)"
        )
        await connection.execute_script(
            "CREATE TABLE scratch_fk_unique_source4 (id serial primary key, "
            "target_id varchar(50) NOT NULL REFERENCES scratch_fk_unique_target4 (code))"
        )
        await connection.execute_script("INSERT INTO scratch_fk_unique_target4 (id, code) VALUES (999, 'needle')")
        await connection.execute_script("INSERT INTO scratch_fk_unique_source4 (target_id) VALUES ('needle')")

        target_table = await DatabaseCatalog.inspect_table(connection, "scratch_fk_unique_target4")
        source_table = await DatabaseCatalog.inspect_table(connection, "scratch_fk_unique_source4")
        fk = source_table.foreign_keys["target_id"]
        assert fk.target_column == "code"
        assert fk.to_field == "code"

        source = "\n\n\n".join(
            [
                ModelSourceGenerator.generate_model_source(target_table, connection.dialect.name),
                ModelSourceGenerator.generate_model_source(source_table, connection.dialect.name),
            ]
        )
        assert "to_field='code'" in source

        module_name = f"generated_inspectdb_fk_unique_{uuid.uuid4().hex}"
        (tmp_path / f"{module_name}.py").write_text(source, encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))
        importlib.invalidate_caches()

        async with hare_test_context(
            modules=[module_name], db_url=db_url, _create_db=False, _generate_schemas=False, _drop_db_on_exit=False
        ):
            module = importlib.import_module(module_name)
            source_cls = module.ScratchFkUniqueSource4
            fk_field = source_cls._meta.fields_map["target"]
            assert fk_field.to_field_names == ("code",)

            row = await source_cls.objects.all().first()
            parent = await row.target
            assert parent.code == "needle"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_generate_model_source_falls_back_when_composite_fk_targets_non_pk_unique(connection):
    """A composite FK referencing a unique constraint that ISN'T the target's own primary key
    can't be reconstructed either - ForeignKeyFieldInstance's composite to_field= support is only
    ever valid when it exactly matches the target's whole declared composite primary key (see
    apps.py's own ConfigurationError for an arbitrary composite unique_together target)."""
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_parent4 (id SERIAL PRIMARY KEY, a INT NOT NULL, b INT NOT NULL, "
        "UNIQUE (a, b))"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_child4 (id INTEGER PRIMARY KEY, parent_a INT NOT NULL, "
        "parent_b INT NOT NULL, FOREIGN KEY (parent_a, parent_b) REFERENCES scratch_composite_fk_parent4 (a, b))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_fk_child4")
    assert table.composite_foreign_keys == []
    assert len(table.unparsed_foreign_keys) == 1

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "parent_a = fields.IntField()" in source
    assert "parent_b = fields.IntField()" in source
    assert "TODO" in source and "composite foreign key" in source


@pytest.mark.asyncio
async def test_composite_foreign_key_is_surfaced_as_unparsed(connection):
    """A composite FK whose real column names DON'T follow the "<field>_<pk_component>"
    convention (here: pa/pb, not parent_a/parent_b) can't be reconstructed as a single
    ForeignKeyField - it must be flagged as unparsed, like every other unparsable construct this
    module reconstructs (triggers, exclusion constraints), not silently rendered as two
    INDEPENDENT single-column FKs that misrepresent the schema."""
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_parent (a INT NOT NULL, b INT NOT NULL, PRIMARY KEY (a, b))"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_composite_fk_child (id INTEGER PRIMARY KEY, pa INT NOT NULL, pb INT NOT NULL, "
        "FOREIGN KEY (pa, pb) REFERENCES scratch_composite_fk_parent (a, b))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_composite_fk_child")
    assert "pa" not in table.foreign_keys
    assert "pb" not in table.foreign_keys
    assert table.composite_foreign_keys == []
    assert len(table.unparsed_foreign_keys) == 1
    _, columns = table.unparsed_foreign_keys[0]
    assert set(columns) == {"pa", "pb"}

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    # falls back to plain fields, not a (wrong) independent ForeignKeyField per column
    assert "pa = fields.ForeignKeyField" not in source
    assert "pb = fields.ForeignKeyField" not in source
    assert "pa = fields.IntField()" in source
    assert "pb = fields.IntField()" in source
    assert "TODO" in source and "composite foreign key" in source


# ============================================================================
# Name-collision regressions: generate_model_source() derives a class name from the table name
# and per-column attribute names from column names, but never checked for a collision before
# this fix - two tables (or two columns) that mapped to the same Python identifier used to
# silently produce a generated module where the second definition shadowed/overwrote the first,
# dropping a whole table's model or one of its fields with no error or warning.
# ============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_raises_on_case_differing_duplicate_table_names(connection):
    """Two distinct Postgres tables differing only by case are legal, distinct tables under
    Postgres's case-sensitive quoted-identifier rules - but generate_model_source()'s class-name
    derivation lowercases everything after the first letter of each underscore-separated part
    (str.capitalize()), so both used to derive the identical class name "ScratchCaseFooBar". The
    combined inspect() output used to contain two "class ScratchCaseFooBar(Model):" definitions
    back to back - the second silently shadowing the first when the module is loaded, with that
    table's model and fields vanishing entirely. Only reproducible on Postgres: SQLite compares
    table names case-insensitively, so two tables differing only by case can't coexist there at
    all."""
    await connection.execute_script('CREATE TABLE "scratch_case_foo_bar" (id serial primary key, a int not null)')
    await connection.execute_script('CREATE TABLE "scratch_case_FOO_BAR" (id serial primary key, b int not null)')
    try:
        with pytest.raises(DuplicateModelClassNameError) as exc_info:
            await SchemaInspector.inspect(connection, tables=["scratch_case_foo_bar", "scratch_case_FOO_BAR"])
        message = str(exc_info.value)
        assert "scratch_case_foo_bar" in message
        assert "scratch_case_FOO_BAR" in message
        assert "ScratchCaseFooBar" in message
    finally:
        await connection.execute_script('DROP TABLE IF EXISTS "scratch_case_foo_bar"')
        await connection.execute_script('DROP TABLE IF EXISTS "scratch_case_FOO_BAR"')


@pytest.mark.asyncio
async def test_inspect_builds_a_model_without_primary_key_for_a_table_with_none(connection):
    """A table with no primary key and no unique index over every column becomes a model with
    Meta.primary_key = None - without it, the Model metaclass would add an 'id' field pointing at
    a column the table doesn't have (on SQLite, reading it even returns the literal string "id",
    SQLite's fallback for an unknown double-quoted identifier)."""
    await connection.execute_script("CREATE TABLE scratch_no_pk (message TEXT NOT NULL)")
    try:
        source = await SchemaInspector.inspect(connection, tables=["scratch_no_pk"])
        compile(source, "<generated>", "exec")
        assert "primary_key = None" in source
        assert "primary_key=True" not in source
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_no_pk")


@pytest.mark.asyncio
async def test_inspect_skips_a_junction_table_shaped_table_with_no_unique_index(connection):
    """A ManyToManyField(unique=False) through table has no primary key AND no unique index at
    all (permitting duplicate pairs) - genuinely no natural key exists for this shape, but it's
    still hare-orm's own recognizable through-table shape (every column is a foreign key, no
    other columns), so it's skipped with an explanatory comment instead of raising
    a model without a primary key like an ordinary table would."""
    await connection.execute_script("CREATE TABLE scratch_a (id INTEGER PRIMARY KEY)")
    await connection.execute_script("CREATE TABLE scratch_b (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_junction_no_unique ("
        "scratch_a_id INTEGER NOT NULL REFERENCES scratch_a(id), "
        "scratch_b_id INTEGER NOT NULL REFERENCES scratch_b(id))"
    )
    try:
        source = await SchemaInspector.inspect(connection, tables=["scratch_junction_no_unique"])
        assert "class " not in source
        assert "scratch_junction_no_unique" in source
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_junction_no_unique")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_a")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_b")


@pytest.mark.asyncio
async def test_inspect_skips_a_junction_table_shaped_table_with_a_covering_unique_index(connection):
    """A ManyToManyField(unique=True) (the default) through table - every column is a foreign
    key, plus a covering UNIQUE index - is the same recognizable through-table shape, and is
    skipped the same way regardless of the unique index being present."""
    await connection.execute_script("CREATE TABLE scratch_a2 (id INTEGER PRIMARY KEY)")
    await connection.execute_script("CREATE TABLE scratch_b2 (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_junction_unique ("
        "scratch_a2_id INTEGER NOT NULL REFERENCES scratch_a2(id), "
        "scratch_b2_id INTEGER NOT NULL REFERENCES scratch_b2(id), "
        "UNIQUE (scratch_a2_id, scratch_b2_id))"
    )
    try:
        source = await SchemaInspector.inspect(connection, tables=["scratch_junction_unique"])
        assert "class " not in source
        assert "scratch_junction_unique" in source
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_junction_unique")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_a2")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_b2")


@pytest.mark.asyncio
async def test_inspect_uses_covering_unique_index_as_composite_pk_for_a_non_junction_table(connection):
    """A table with no PRIMARY KEY, not shaped like an M2M through table (has a non-FK column),
    but with a UNIQUE index covering every column - a genuine multi-column natural key with no
    separately-declared PRIMARY KEY - is reconstructed using that index as a composite pk,
    instead of becoming a model without a primary key or silently letting the metaclass inject a phantom
    'id' field."""
    await connection.execute_script(
        "CREATE TABLE scratch_natural_key (region TEXT NOT NULL, code TEXT NOT NULL, UNIQUE (region, code))"
    )
    try:
        source = await SchemaInspector.inspect(connection, tables=["scratch_natural_key"])
        compile(source, "<generated>", "exec")
        assert "class ScratchNaturalKey(Model):" in source
        assert "CompositePrimaryKey" in source
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_natural_key")


@pytest.mark.asyncio
async def test_generate_model_source_skip_m2m_through_tables_false_builds_a_real_model(connection):
    """generate_model_source()'s default (skip_many_to_many_through_tables=True, matching `hare
    inspectdb`'s own CLI behavior) still skips an M2M-through-shaped table with a NOTE comment -
    unchanged. With skip_many_to_many_through_tables=False (for a caller like a runtime table browser
    that must be able to open ANY discovered table), the table with NO declared PRIMARY KEY at
    all used to crash: _get_primary_key_columns()'s covering-unique-index fallback promoted
    both FK columns into a SYNTHETIC composite pk, but `column.is_pk` (only ever True for a REAL
    declared PK) didn't recognize them as pk members, so both columns still rendered as
    ForeignKeyField relations - `pk = CompositePrimaryKey('employee', 'employee_rel')` then
    referenced two relation attributes, which ModelMeta._parse_composite_pk() unconditionally
    rejects ("no single DB column of its own"), raising ConfigurationError the moment the
    generated model was imported. Confirmed live before this fix. Now widened to `column.name in
    self.pk_columns` (covers both the real-PK and this synthetic-PK case uniformly) - the same
    already-proven "drop the FK relation, reference the plain column instead" mechanism a real
    declared composite PK already used applies here too, and the model is genuinely usable."""
    await connection.execute_script("CREATE TABLE scratch_employee (id INTEGER PRIMARY KEY, name TEXT)")
    await connection.execute_script(
        "CREATE TABLE scratch_employee_employee ("
        "employee_id INTEGER NOT NULL REFERENCES scratch_employee(id), "
        "employee_rel_id INTEGER NOT NULL REFERENCES scratch_employee(id), "
        "UNIQUE (employee_id, employee_rel_id))"
    )
    try:
        table = await DatabaseCatalog.inspect_table(connection, "scratch_employee_employee")

        source_skip = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
        assert "class " not in source_skip
        assert "ManyToManyField through table" in source_skip

        source_full = ModelSourceGenerator.generate_model_source(
            table, connection.dialect.name, skip_many_to_many_through_tables=False
        )
        compile(source_full, "<generated>", "exec")
        assert "= fields.ForeignKeyField" not in source_full
        assert "employee_id = fields.IntField" in source_full
        assert "employee_rel_id = fields.IntField" in source_full
        assert "pk = fields.CompositePrimaryKey('employee_id', 'employee_rel_id')" in source_full

        from hare.models import Model

        namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
        exec(compile(source_full, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
        generated_cls = namespace["ScratchEmployeeEmployee"]
        assert generated_cls._meta.primary_key_attribute == ("employee_id", "employee_rel_id")
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_employee_employee")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_employee")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_whole_schema_still_works_when_no_class_name_collision(connection):
    """Companion to the collision test above: the new tracking must not misfire on two genuinely
    different, non-colliding table names in the same inspect() run."""
    await connection.execute_script('CREATE TABLE "scratch_case_alpha" (id serial primary key)')
    await connection.execute_script('CREATE TABLE "scratch_case_beta" (id serial primary key)')
    try:
        source = await SchemaInspector.inspect(connection, tables=["scratch_case_alpha", "scratch_case_beta"])
        compile(source, "<generated>", "exec")
        assert "class ScratchCaseAlpha(Model):" in source
        assert "class ScratchCaseBeta(Model):" in source
    finally:
        await connection.execute_script('DROP TABLE IF EXISTS "scratch_case_alpha"')
        await connection.execute_script('DROP TABLE IF EXISTS "scratch_case_beta"')


@pytest.mark.asyncio
async def test_generate_model_source_fk_field_avoids_plain_column_name_collision(connection):
    """A plain "widget" column and an FK "widget_id" column both used to map to the same Python
    attribute "widget" (the FK branch strips a trailing "_id"), so the FK's rendered line silently
    overwrote the plain column's own line in the generated class body - one field vanished on load
    with no warning. The FK must now fall back to its raw, unstripped column name plus an explicit
    source_field= (the same convention already used for a keyword/digit-prefixed column name)."""
    await connection.execute_script("CREATE TABLE scratch_fk_name_collision_parent (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_fk_name_collision_child (id INTEGER PRIMARY KEY, widget TEXT NOT NULL, "
        "widget_id INTEGER NOT NULL REFERENCES scratch_fk_name_collision_parent (id))"
    )
    try:
        table = await DatabaseCatalog.inspect_table(connection, "scratch_fk_name_collision_child")
        source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
        compile(source, "<generated>", "exec")

        # exactly one line assigning the plain "widget" attribute - the FK must not ALSO claim it
        assert source.count("\n    widget = ") == 1
        assert "widget_id = " in source
        assert "ForeignKeyField" in source
        assert "models.ScratchFkNameCollisionParent" in source
        assert "source_field='widget_id'" in source

        generated_cls = await _load_generated_model(source, "ScratchFkNameCollisionChild")
        field_names = set(generated_cls._meta.fields_map.keys())
        assert "widget" in field_names
        assert "widget_id" in field_names
        assert generated_cls._meta.fields_map["widget_id"].source_field == "widget_id"
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_fk_name_collision_child")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_fk_name_collision_parent")


@pytest.mark.asyncio
async def test_generate_model_source_fk_field_collision_reversed_column_order(connection):
    """Same collision as above, but with the FK column declared BEFORE the plain column
    physically in the table - the fix's collision detection is computed up front over every
    column, so it must not depend on which of the two columns comes first."""
    await connection.execute_script("CREATE TABLE scratch_fk_name_collision2_parent (id INTEGER PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_fk_name_collision2_child (id INTEGER PRIMARY KEY, "
        "widget_id INTEGER NOT NULL REFERENCES scratch_fk_name_collision2_parent (id), widget TEXT NOT NULL)"
    )
    try:
        table = await DatabaseCatalog.inspect_table(connection, "scratch_fk_name_collision2_child")
        source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
        compile(source, "<generated>", "exec")

        assert source.count("\n    widget = ") == 1
        assert "source_field='widget_id'" in source

        generated_cls = await _load_generated_model(source, "ScratchFkNameCollision2Child")
        field_names = set(generated_cls._meta.fields_map.keys())
        assert "widget" in field_names
        assert "widget_id" in field_names
    finally:
        await connection.execute_script("DROP TABLE IF EXISTS scratch_fk_name_collision2_child")
        await connection.execute_script("DROP TABLE IF EXISTS scratch_fk_name_collision2_parent")


# ============================================================================
# SQLite GENERATED ALWAYS AS (...) columns: PRAGMA table_info doesn't list a generated column AT
# ALL (it's hidden from it) - inspectdb used to silently drop the column entirely, with zero
# trace, instead of reconstructing it as a GeneratedField (or, failing that, leaving a comment).
# ============================================================================


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_sqlite_stored_generated_column(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_rect (id INTEGER PRIMARY KEY, width INTEGER NOT NULL, "
        "height INTEGER NOT NULL, area INTEGER GENERATED ALWAYS AS (width * height) STORED)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_rect")
    assert table.unparsed_generated_columns == []
    column_names = {column.name for column in table.columns}
    assert column_names == {"id", "width", "height", "area"}
    area = next(column for column in table.columns if column.name == "area")
    assert area.generated_expression == "width * height"
    assert area.generated_stored is True

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "area = fields.GeneratedField(expression=RawSQLTerm('width * height')" in source
    assert "output_field=fields.IntField()" in source

    generated_cls = await _load_generated_model(source, "ScratchRect")
    from hare.fields.generated_field import GeneratedField

    assert "area" in generated_cls._meta.fields_map
    area_field = generated_cls._meta.fields_map["area"]
    assert isinstance(area_field, GeneratedField)
    assert area_field.generated is True
    assert area_field.expression == RawSQLTerm("width * height")
    assert area_field.stored is True


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_sqlite_virtual_generated_column(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_rect_virtual (id INTEGER PRIMARY KEY, width INTEGER NOT NULL, "
        "height INTEGER NOT NULL, area INTEGER GENERATED ALWAYS AS (width * height) VIRTUAL)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_rect_virtual")
    assert table.unparsed_generated_columns == []
    area = next(column for column in table.columns if column.name == "area")
    assert area.generated_expression == "width * height"
    assert area.generated_stored is False

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "stored=False" in source

    generated_cls = await _load_generated_model(source, "ScratchRectVirtual")
    area_field = generated_cls._meta.fields_map["area"]
    assert area_field.stored is False


def test_parse_sqlite_generated_column_expression_returns_none_on_unbalanced_parens():
    """Direct unit test of the fallback path: table_xinfo's own "hidden" flag can say a column IS
    generated even when this file's own best-effort text parsing can't confidently extract the
    expression (e.g. genuinely unusual formatting this regex doesn't anticipate, or a truncated/
    malformed CREATE TABLE text) - must return None (surfaced by the caller as an
    unparsed_generated_columns comment), not raise or guess."""
    unbalanced_sql = "CREATE TABLE t (id INTEGER PRIMARY KEY, area INTEGER GENERATED ALWAYS AS (a * b"
    assert SqliteIntrospector._parse_sqlite_generated_column_expression(unbalanced_sql, "area") is None
    assert SqliteIntrospector._parse_sqlite_generated_column_expression("CREATE TABLE t (id INTEGER)", "area") is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_postgres_stored_generated_column(connection):
    """Unlike SQLite (whose PRAGMA table_info hides a generated column entirely, needing a
    regex over the raw CREATE TABLE text to recover its expression), Postgres's own
    information_schema.columns reports a GENERATED ALWAYS AS (...) column's expression text
    directly - inspectdb used to never read it at all, silently reconstructing the column as an
    ordinary writable field the DB would reject the moment an INSERT actually named it."""
    await connection.execute_script(
        "CREATE TABLE scratch_pg_rect (id serial PRIMARY KEY, width integer NOT NULL, "
        "height integer NOT NULL, area integer GENERATED ALWAYS AS (width * height) STORED)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_pg_rect")
    area = next(column for column in table.columns if column.name == "area")
    assert area.generated_expression == "(width * height)"
    assert area.generated_stored is True

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "area = fields.GeneratedField(" in source
    assert "output_field=fields.IntField()" in source

    generated_cls = await _load_generated_model(source, "ScratchPgRect")
    from hare.fields.generated_field import GeneratedField

    area_field = generated_cls._meta.fields_map["area"]
    assert isinstance(area_field, GeneratedField)
    assert area_field.generated is True
    assert area_field.stored is True


# ============================================================================
# CHECK constraints: never introspected at all on either dialect, despite CheckConstraint already
# being a real, supported ORM construct - a CHECK constraint used to vanish from inspectdb output
# entirely, with no unparsed_*-style comment either.
# ============================================================================


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_sqlite_named_check_constraint(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_product (id INTEGER PRIMARY KEY, name VARCHAR(50) NOT NULL, "
        "price DECIMAL(10,2) NOT NULL, CONSTRAINT price_positive CHECK (price > 0))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_product")
    assert table.unparsed_check_constraints == []
    assert len(table.check_constraints) == 1
    constraint = table.check_constraints[0]
    assert constraint.name == "price_positive"
    assert constraint.check == RawSQLTerm("price > 0")

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "CheckConstraint(check=RawSQLTerm('price > 0'), name='price_positive')" in source

    generated_cls = await _load_generated_model(source, "ScratchProduct")
    assert len(generated_cls._meta.constraints) == 1
    assert generated_cls._meta.constraints[0].name == "price_positive"
    assert generated_cls._meta.constraints[0].check == RawSQLTerm("price > 0")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_sqlite_unnamed_check_constraint_synthesizes_name(connection):
    """An inline column-level CHECK (no CONSTRAINT name of its own) has no name for SQLite to
    report at all - CheckConstraint.name is required, so one is synthesized the same way this
    file already synthesizes a name for a SQLite composite FK's own missing constraint name."""
    await connection.execute_script(
        "CREATE TABLE scratch_product_unnamed (id INTEGER PRIMARY KEY, price DECIMAL(10,2) CHECK (price > 0))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_product_unnamed")
    assert table.unparsed_check_constraints == []
    assert len(table.check_constraints) == 1
    assert table.check_constraints[0].check == RawSQLTerm("price > 0")
    assert table.check_constraints[0].name == "scratch_product_unnamed_check_1"

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    generated_cls = await _load_generated_model(source, "ScratchProductUnnamed")
    assert len(generated_cls._meta.constraints) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_postgres_check_constraint(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_product_pg (id serial primary key, name varchar(50) not null, "
        "price numeric(10,2) not null, CONSTRAINT price_positive CHECK (price > 0))"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_product_pg")
    assert table.unparsed_check_constraints == []
    assert len(table.check_constraints) == 1
    constraint = table.check_constraints[0]
    assert constraint.name == "price_positive"
    # pg_get_constraintdef() canonicalizes the expression (adds an explicit numeric cast, and its
    # own extra wrapping parens) - only the OUTER "CHECK (...)" wrapper gets stripped here, not
    # the expression's own inner parens, same treatment get_predicate_equalities() already gives a
    # partial index's predicate text.
    assert constraint.check == RawSQLTerm("price > (0)::numeric")

    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert "CheckConstraint(check=RawSQLTerm('price > (0)::numeric'), name='price_positive')" in source

    generated_cls = await _load_generated_model(source, "ScratchProductPg")
    assert len(generated_cls._meta.constraints) == 1
    assert generated_cls._meta.constraints[0].check == RawSQLTerm("price > (0)::numeric")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_table_reconstructs_postgres_check_and_exclusion_constraints_together(connection):
    """Regression test for the merged Meta.constraints = [...] rendering: a table with BOTH an
    EXCLUDE and a CHECK constraint used to risk two separate "constraints = [...]" assignment
    lines - valid Python, but the second would silently shadow (drop) the first in the class
    body."""
    await connection.execute_script("CREATE EXTENSION IF NOT EXISTS btree_gist")
    await connection.execute_script(
        "CREATE TABLE scratch_booking_check (id serial primary key, resource int not null, "
        "during tstzrange not null, price numeric(10,2) not null, "
        "CONSTRAINT price_positive CHECK (price > 0))"
    )
    await connection.execute_script(
        "ALTER TABLE scratch_booking_check ADD CONSTRAINT no_overlap EXCLUDE USING gist "
        "(resource WITH =, during WITH &&)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_booking_check")
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    compile(source, "<generated>", "exec")
    assert source.count("constraints = [") == 1
    assert "ExclusionConstraint(" in source
    assert "CheckConstraint(" in source

    generated_cls = await _load_generated_model(source, "ScratchBookingCheck")
    assert len(generated_cls._meta.constraints) == 2


async def generate_model_class(connection, table_name: str, class_name: str):
    from hare.models import Model

    table = await DatabaseCatalog.inspect_table(connection, table_name)
    source = ModelSourceGenerator.generate_model_source(table, connection.dialect.name)
    namespace: dict = {"Model": Model, "__name__": "tests.generated_inspectdb_module"}
    exec(compile(source, "<generated>", "exec"), namespace)  # noqa: S102 - test-only, own generated code
    return source, namespace[class_name]


@pytest.mark.asyncio
async def test_generate_model_source_keeps_columns_whose_python_names_collide(connection):
    """Columns "a-b", "a b" and "a_b" all mangle to a_b - every column must still be represented."""
    await connection.execute_script(
        'CREATE TABLE "inspect_coll" (id INTEGER PRIMARY KEY, "a-b" TEXT, "a b" TEXT, "a_b" TEXT)'
    )
    try:
        source, generated_cls = await generate_model_class(connection, "inspect_coll", "InspectColl")
    finally:
        await connection.execute_script('DROP TABLE "inspect_coll"')

    assert set(generated_cls._meta.fields_db_projection.values()) == {"id", "a-b", "a b", "a_b"}
    assert len(generated_cls._meta.fields_map) == 4
    assert "source_field='a-b'" in source
    assert "source_field='a b'" in source


@pytest.mark.asyncio
async def test_generate_model_source_keeps_cyrillic_columns_distinct(connection):
    await connection.execute_script(
        'CREATE TABLE "inspect_cyr" (id INTEGER PRIMARY KEY, '
        '"\u0438\u043c\u044f" TEXT, "\u0444\u0430\u043c" TEXT, "\u0438\u043c\u044f 2" TEXT)'
    )
    try:
        source, generated_cls = await generate_model_class(connection, "inspect_cyr", "InspectCyr")
    finally:
        await connection.execute_script('DROP TABLE "inspect_cyr"')

    assert set(generated_cls._meta.fields_db_projection.values()) == {
        "id",
        "\u0438\u043c\u044f",
        "\u0444\u0430\u043c",
        "\u0438\u043c\u044f 2",
    }
    assert "\u0438\u043c\u044f = fields." in source
    assert "\u0444\u0430\u043c = fields." in source


@pytest.mark.asyncio
async def test_generate_model_source_renames_columns_reserved_by_model(connection):

    reserved_columns = sorted(
        {"pk", "Meta", "save", "delete", "filter", "get", "all", "create", "first", "fetch_related", "refresh_from_db"}
        & ModelFieldCollection.get_reserved_field_names()
    )
    assert "save" in reserved_columns and "filter" not in reserved_columns
    column_definitions = ", ".join(f'"{column}" TEXT' for column in reserved_columns)
    await connection.execute_script(f'CREATE TABLE "inspect_reserved" (id INTEGER PRIMARY KEY, {column_definitions})')
    try:
        source, generated_cls = await generate_model_class(connection, "inspect_reserved", "InspectReserved")
    finally:
        await connection.execute_script('DROP TABLE "inspect_reserved"')

    assert set(generated_cls._meta.fields_db_projection.values()) == {"id", *reserved_columns}
    for column in reserved_columns:
        assert f"source_field='{column}'" in source


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_nonexistent_schema_raises(connection):
    from hare.inspectdb import SchemaNotFoundError

    with pytest.raises(SchemaNotFoundError):
        await SchemaInspector.inspect(connection, schema="no_such_schema_for_inspectdb")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_sqlite_handles_a_double_quote_in_table_and_index_names(connection):
    await connection.execute_script(
        'CREATE TABLE "odd""name" (id INTEGER PRIMARY KEY, label TEXT);CREATE INDEX "odd""idx" ON "odd""name" (label);'
    )
    try:
        table = await DatabaseCatalog.inspect_table(connection, 'odd"name')
        assert [(column.name, column.has_index) for column in table.columns] == [("id", False), ("label", True)]
    finally:
        await connection.execute_script('DROP TABLE "odd""name"')


def test_parse_sqlite_unique_constraint_names_reads_table_level_constraints():
    table_sql = (
        'CREATE TABLE "t" ("id" INTEGER PRIMARY KEY, "a" INT UNIQUE, "b" INT, '
        'CONSTRAINT "uq_t_ab" UNIQUE ("a", "b"), CONSTRAINT uq_t_b UNIQUE (b), CONSTRAINT "ck_t" CHECK (a > 0))'
    )

    assert SqliteIntrospector._parse_sqlite_unique_constraint_names(table_sql) == {
        ("a", "b"): ["uq_t_ab"],
        ("b",): ["uq_t_b"],
    }


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_inspect_table_names_a_sqlite_autoindex_after_its_unique_constraint(connection):
    """SQLite backs a table-level UNIQUE constraint with an unnamed sqlite_autoindex_* index - the
    declared constraint name was lost, so drift never matched it to its declaration."""
    await connection.execute_script(
        'CREATE TABLE scratch_named_unique ("id" INTEGER PRIMARY KEY, "a" INT UNIQUE, "b" INT, '
        'CONSTRAINT "uq_scratch_ab" UNIQUE ("a", "b"), CONSTRAINT "uq_scratch_b" UNIQUE ("b"))'
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_named_unique")

    assert [(index.name, index.columns) for index in table.indexes] == [("uq_scratch_ab", ["a", "b"])]
    column_index_names = {index.columns[0]: index.name for index in table.column_indexes}
    assert column_index_names["b"] == "uq_scratch_b"
    assert column_index_names["a"].startswith("sqlite_autoindex_")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_table_reads_the_deferral_of_a_unique_constraint(connection):
    await connection.execute_script(
        "CREATE TABLE scratch_deferred_unique (id serial primary key, a int, b int, c int, "
        "CONSTRAINT uq_scratch_deferred_ab UNIQUE (a, b) DEFERRABLE INITIALLY DEFERRED, "
        "CONSTRAINT uq_scratch_deferrable_c UNIQUE (c) DEFERRABLE)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_deferred_unique")

    (pair_index,) = table.indexes
    (column_index,) = table.column_indexes
    assert (pair_index.name, pair_index.deferrable, pair_index.initially_deferred) == (
        "uq_scratch_deferred_ab",
        True,
        True,
    )
    assert (column_index.name, column_index.deferrable, column_index.initially_deferred) == (
        "uq_scratch_deferrable_c",
        True,
        False,
    )


class CatalogQueryCounter:
    """Counts the queries issued in the current context while installed."""

    def __init__(self) -> None:
        self.query_count = 0
        self._observing = None

    def _on_query(self, event) -> None:
        self.query_count += 1

    def __enter__(self) -> "CatalogQueryCounter":
        self._observing = Observers.observing(QueryExecuted, self._on_query)
        self._observing.__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        self._observing.__exit__(*exc_info)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_whole_schema_reads_postgres_catalog_with_fixed_query_count(connection):
    """Every table's metadata is read with one catalog query per type of metadata, not several
    queries per table - the query count doesn't grow with the number of tables."""
    table_names = await DatabaseCatalog.get_table_names(connection)
    assert len(table_names) > 100

    with CatalogQueryCounter() as whole_schema_counter:
        source = await SchemaInspector.inspect(connection)
    with CatalogQueryCounter() as single_table_counter:
        await SchemaInspector.inspect(connection, tables=["tournament"])

    assert "class Tournament(Model):" in source
    assert whole_schema_counter.query_count <= 10
    assert whole_schema_counter.query_count == single_table_counter.query_count


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_tables_matches_inspect_table_per_table(connection):
    table_names = ["event", "tournament", "event"]

    table_infos = await DatabaseCatalog.inspect_tables(connection, table_names)

    assert [table_info.name for table_info in table_infos] == table_names
    assert table_infos[0] is not table_infos[2]
    for table_name, table_info in zip(table_names, table_infos, strict=True):
        assert table_info == await DatabaseCatalog.inspect_table(connection, table_name)


@pytest.mark.asyncio
async def test_inspect_tables_raises_on_missing_table(connection):
    with pytest.raises(TableNotFoundError):
        await DatabaseCatalog.inspect_tables(connection, ["tournament", "scratch_no_such_table"])


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_tables_raises_on_missing_table_without_existence_check(connection):
    with pytest.raises(TableNotFoundError):
        await DatabaseCatalog.inspect_tables(connection, ["scratch_no_such_table"], verify_exists=False)


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_foreign_keys_sharing_a_constraint_name_stay_on_their_own_table(connection):
    """FOREIGN KEY constraint names are only unique per table - two tables' same-named
    constraints must not leak each other's columns or ON DELETE rule."""
    await connection.execute_script("CREATE TABLE scratch_shared_fk_parent (id serial PRIMARY KEY)")
    await connection.execute_script(
        "CREATE TABLE scratch_shared_fk_a (id serial PRIMARY KEY, parent_id int, "
        "CONSTRAINT scratch_shared_fk FOREIGN KEY (parent_id) REFERENCES scratch_shared_fk_parent (id))"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_shared_fk_b (id serial PRIMARY KEY, other_id int, "
        "CONSTRAINT scratch_shared_fk FOREIGN KEY (other_id) REFERENCES scratch_shared_fk_parent (id) "
        "ON DELETE CASCADE)"
    )

    table_a, table_b = await DatabaseCatalog.inspect_tables(connection, ["scratch_shared_fk_a", "scratch_shared_fk_b"])

    assert list(table_a.foreign_keys) == ["parent_id"]
    assert table_a.foreign_keys["parent_id"].on_delete == OnDelete.NO_ACTION
    assert list(table_b.foreign_keys) == ["other_id"]
    assert table_b.foreign_keys["other_id"].on_delete == OnDelete.CASCADE


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_foreign_key_to_another_schema(connection):
    await connection.execute_script("CREATE SCHEMA scratch_fk_schema")
    await connection.execute_script(
        "CREATE TABLE scratch_fk_schema.scratch_ref (id serial PRIMARY KEY, "
        "tournament_id int REFERENCES public.tournament (id) ON DELETE SET NULL)"
    )

    table = await DatabaseCatalog.inspect_table(connection, "scratch_ref", schema="scratch_fk_schema")

    foreign_key = table.foreign_keys["tournament_id"]
    assert (foreign_key.target_table, foreign_key.target_column, foreign_key.to_field) == ("tournament", "id", None)
    assert foreign_key.on_delete == OnDelete.SET_NULL


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_inspect_foreign_keys_to_a_partitioned_table(connection):
    """A FOREIGN KEY to a partitioned table targets that table itself, once - not each of its
    partitions - on the partitioned referencing table and on its partitions alike."""
    await connection.execute_script(
        "CREATE TABLE scratch_part_target (id int, day date, PRIMARY KEY (id, day)) PARTITION BY RANGE (day)"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_part_target_2026 PARTITION OF scratch_part_target "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_part_target_2027 PARTITION OF scratch_part_target "
        "FOR VALUES FROM ('2027-01-01') TO ('2028-01-01')"
    )
    await connection.execute_script("CREATE TABLE scratch_by_code (code text PRIMARY KEY) PARTITION BY LIST (code)")
    await connection.execute_script("CREATE TABLE scratch_by_code_a PARTITION OF scratch_by_code FOR VALUES IN ('a')")
    await connection.execute_script("CREATE TABLE scratch_by_code_b PARTITION OF scratch_by_code FOR VALUES IN ('b')")
    await connection.execute_script(
        "CREATE TABLE scratch_part_source (id int, day date, target_id int, target_day date, "
        "code text REFERENCES scratch_by_code (code), PRIMARY KEY (id, day), "
        "FOREIGN KEY (target_id, target_day) REFERENCES scratch_part_target (id, day)) PARTITION BY RANGE (day)"
    )
    await connection.execute_script(
        "CREATE TABLE scratch_part_source_2026 PARTITION OF scratch_part_source "
        "FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')"
    )

    for table in await DatabaseCatalog.inspect_tables(connection, ["scratch_part_source", "scratch_part_source_2026"]):
        assert [
            (composite_fk.field_name, composite_fk.columns, composite_fk.target_table)
            for composite_fk in table.composite_foreign_keys
        ] == [("target", ("target_id", "target_day"), "scratch_part_target")]
        assert table.unparsed_foreign_keys == []
        assert table.foreign_keys["code"].target_table == "scratch_by_code"
