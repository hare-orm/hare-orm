"""inspectdb reconstructions that must yield a model which really works against its own table -
both as exec'd ModelSourceGenerator source and as a ModelFactory.from_table_info() class, each
registered with Hare.register_live_model() and exercised with real CRUD."""

import contextlib
from collections.abc import Generator
from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.test import requires_features
from hare.core.connections import Connections
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.dialects.postgresql.introspection import PostgresqlIntrospector
from hare.exceptions import ConfigurationError, IntegrityError
from hare.fields.db_defaults import SqlDefault
from hare.fields.relations.fields import ForeignKeyFieldInstance, OneToOneFieldInstance
from hare.inspectdb import ModelFactory, ModelSourceGenerator, SchemaInspector, SchemaIntrospector
from hare.inspectdb.naming import ModelNaming
from hare.models import Model

BUILD_MODES = ["source", "factory"]


@pytest.fixture
def connection(db):
    alias = next(iter(Connections.current().db_config))
    return Connections.get(alias)


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
    for table_name in table_names:
        table_info = await SchemaIntrospector.inspect_table(connection, table_name)
        if build_mode == "factory":
            models[table_name] = ModelFactory.from_table_info(table_info, dialect, app_label="live")
            continue
        source = ModelSourceGenerator.generate_model_source(table_info, dialect, app_label="live")
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
    table_info = await SchemaIntrospector.inspect_table(connection, table_name)
    return ModelSourceGenerator.generate_model_source(table_info, connection.dialect.name)


# ---------------------------------------------------------------------------------------------
# FK column without an "_id" suffix, several FKs to one target


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
    await create_owner_and_child_tables(connection)
    models = await build_models(connection, ["ix_owner", "ix_child"], build_mode)
    owner_class, child_class = models["ix_owner"], models["ix_child"]

    owner_code_field = child_class._meta.fields_map["owner_code_fk"]
    assert owner_code_field.source_field == "owner_code"
    assert {
        name: child_class._meta.fields_map[name].related_name for name in ("owner", "owner_code_fk", "left", "right")
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
    assert isinstance(profile_class._meta.fields_map["account"], OneToOneFieldInstance)
    assert profile_class._meta.pk_attr == "account"

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
    assert text_class._meta.fields_map["note"].index or any(
        isinstance(index, Index) and tuple(index.fields) == ("note",) for index in text_class._meta.indexes
    )
    assert text_class._meta.fields_map["body"].unique
    assert [
        tuple(constraint.fields)
        for constraint in text_class._meta.constraints
        if isinstance(constraint, UniqueConstraint)
    ] == [("a", "tag")]

    # The reconstructed model's own DDL recreates every index and unique constraint.
    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator._get_model_sql_data(text_class).get_table_creation_sql()
    await connection.execute_script("DROP TABLE ix_text")
    await connection.execute_script(table_sql)
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_text")
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


def test_text_field_accepts_indexes_but_encrypted_text_field_does_not():
    class TextFieldIndexed(Model):
        id = fields.IntField(primary_key=True)
        note = fields.TextField(db_index=True)
        body = fields.TextField(unique=True)
        tag = fields.TextField()

        class Meta:
            abstract = True
            constraints = (UniqueConstraint(fields=("note", "tag")),)

    assert TextFieldIndexed._meta.fields_map["body"].unique
    assert TextFieldIndexed._meta.fields_map["note"].index
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        fields.EncryptedTextField(unique=True)
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        fields.EncryptedTextField(db_index=True)


# ---------------------------------------------------------------------------------------------
# Partial and expression indexes


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_partial_unique_index_keeps_its_condition(db, connection, build_mode):
    await recreate_tables(
        connection,
        ["ix_partial"],
        [
            "CREATE TABLE ix_partial (id INTEGER PRIMARY KEY, code VARCHAR(10) NOT NULL, active INTEGER NOT NULL)",
            "CREATE UNIQUE INDEX ix_partial_code_active ON ix_partial (code) WHERE active = 1",
        ],
    )
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_partial")
    assert not {column.name: column for column in table_info.columns}["code"].is_unique
    [index_info] = table_info.indexes
    assert (index_info.columns, index_info.is_unique, index_info.condition_sql) == (["code"], True, "active = 1")

    # A partial unique index under a name of its own is the index of a named
    # UniqueConstraint(condition=...), the predicate kept as the database has it.
    models = await build_models(connection, ["ix_partial"], build_mode)
    partial_class = models["ix_partial"]
    assert not partial_class._meta.fields_map["code"].unique
    assert not partial_class._meta.indexes
    [constraint] = partial_class._meta.constraints
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
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_partial_raw")
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
    [index] = expression_class._meta.indexes
    assert type(index) is Index
    [expression] = index.expressions
    assert isinstance(expression, RawSQLTerm)
    # Postgres canonicalizes the term text ("lower((note)::text)").
    assert expression.sql.startswith("lower(") and "note" in expression.sql
    assert "TODO" not in await generated_source(connection, "ix_expression")

    with registered_live_models(models):
        await expression_class.objects.create(id=1, note="Abc")
        assert (await expression_class.objects.get(id=1)).note == "Abc"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_sqlite_expression_index_with_sort_order_is_flagged_without_postgres_wording(db, connection):
    await recreate_tables(
        connection,
        ["ix_expression_desc"],
        [
            "CREATE TABLE ix_expression_desc (id INTEGER PRIMARY KEY, note VARCHAR(20) NOT NULL)",
            "CREATE INDEX ix_expression_desc_lower ON ix_expression_desc (lower(note) DESC)",
        ],
    )
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_expression_desc")
    assert [name for name, _ in table_info.unparsed_indexes] == ["ix_expression_desc_lower"]
    source = await generated_source(connection, "ix_expression_desc")
    assert "sort order" in source
    assert "Postgres" not in source


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
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_line")
    assert table_info.unparsed_foreign_keys == []
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
    await recreate_tables(
        connection,
        ["ix_identity"],
        [
            "CREATE TABLE ix_identity (id SERIAL PRIMARY KEY, "
            "seq INTEGER GENERATED ALWAYS AS IDENTITY, "
            "counter BIGINT GENERATED BY DEFAULT AS IDENTITY, name VARCHAR(10) NOT NULL)"
        ],
    )
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_identity")
    identity_by_name = {column.name: column.identity_generation for column in table_info.columns}
    assert identity_by_name == {"id": None, "seq": "ALWAYS", "counter": "BY DEFAULT", "name": None}
    assert "AS IDENTITY column" in await generated_source(connection, "ix_identity")

    models = await build_models(connection, ["ix_identity"], build_mode)
    identity_class = models["ix_identity"]
    assert identity_class._meta.fields_map["seq"].generated
    assert isinstance(identity_class._meta.fields_map["counter"], fields.BigIntField)

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
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_numeric_default")
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
# Non-indexable field types with an index in the DB


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_unique_binary_column_drops_the_index_with_todo(db, connection, build_mode):
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
    assert "BinaryField can't be indexed - dropped" in source
    assert "whose field type can't be indexed" in source

    models = await build_models(connection, ["ix_binary"], build_mode)
    binary_class = models["ix_binary"]
    assert not binary_class._meta.fields_map["digest"].unique
    assert not [
        constraint for constraint in binary_class._meta.constraints if isinstance(constraint, UniqueConstraint)
    ]
    with registered_live_models(models):
        await binary_class.objects.create(id=1, digest=b"\x01", a=1, blob_part=b"\x02")
        assert (await binary_class.objects.get(id=1)).digest == b"\x01"


@requires_features(dialect="postgresql")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_gin_index_over_json_column_is_kept(db, connection, build_mode):
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
    [index] = json_class._meta.indexes
    assert (index.INDEX_TYPE, index.fields) == ("GIN", ["payload"])
    with registered_live_models(models):
        await json_class.objects.create(id=1, payload={"k": [1]})
        assert await json_class.objects.filter(payload__contains={"k": [1]}).count() == 1


def test_non_btree_index_over_non_indexable_field_passes_validation():
    from hare.dialects.postgresql.indexes import GinIndex
    from tests.testmodels import JSONFields

    meta = JSONFields._meta
    original = meta.indexes
    meta.indexes = (GinIndex(fields=("data",)),)
    try:
        meta._validate_together_field_indexability()
        meta.indexes = (Index(fields=("data",)),)
        with pytest.raises(ConfigurationError, match="can't be indexed"):
            meta._validate_together_field_indexability()
    finally:
        meta.indexes = original


# ---------------------------------------------------------------------------------------------
# Generated names that would shadow the generated module's own imports


@pytest.mark.asyncio
async def test_generated_names_never_shadow_the_modules_imports(db, connection):
    """A column "fields" shadowed `fields` for the rest of the class body, and a table "model"
    rebound the module-level `Model` every later class derives from - the module didn't import."""
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

    assert namespace["Model"] is Model
    model_class = namespace["Model_"]
    assert model_class._meta.db_table == "model"
    assert "fields.ForeignKeyField('models.Model_'" in source
    doc_fields_map = namespace["IxShadowDoc"]._meta.fields_map
    assert doc_fields_map["fields_"].source_field == "fields"
    assert "after" in doc_fields_map
    usr_fields_map = namespace["IxShadowUsr"]._meta.fields_map
    assert usr_fields_map["OnDelete_"].source_field == "OnDelete"
    assert usr_fields_map["m_fk"].on_delete == "NO ACTION"
    await connection.execute_script("DROP TABLE ix_shadow_usr")
    await connection.execute_script("DROP TABLE model")
    await connection.execute_script("DROP TABLE ix_shadow_doc")


# ---------------------------------------------------------------------------------------------
# Column defaults


@requires_features(dialect="sqlite")
@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_sqlite_expression_default_keeps_its_parentheses(db, connection, build_mode):
    """SQLite reports `DEFAULT (datetime('now'))` as "datetime('now')" - rendered back without
    the parentheses it is invalid DDL."""
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
    assert default_class._meta.fields_map["created"].db_default == SqlDefault("(datetime('now'))")
    assert default_class._meta.fields_map["code"].db_default == "x"

    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator._get_model_sql_data(default_class).get_table_creation_sql()
    await connection.execute_script("DROP TABLE ix_expression_default")
    await connection.execute_script(table_sql)
    await connection.execute_script("INSERT INTO ix_expression_default (id) VALUES (1)")
    [row] = await connection.execute_dicts("SELECT created, code FROM ix_expression_default")
    assert row["created"] and row["code"] == "x"
    table_info = await SchemaIntrospector.inspect_table(connection, "ix_expression_default")
    assert {column.name: column for column in table_info.columns}["created"].db_default == SqlDefault(
        "(datetime('now'))"
    )


@pytest.mark.parametrize("build_mode", BUILD_MODES)
@pytest.mark.asyncio
async def test_json_column_default_is_the_document_not_a_string(db, connection, build_mode):
    """DEFAULT '{}'::jsonb came back as JSONField(db_default='{}') - stored again as the JSON
    string "{}" instead of an empty object."""
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
    assert json_class._meta.fields_map["meta"].db_default == {}
    assert json_class._meta.fields_map["items"].db_default == [1, 2]

    schema_generator = connection.dialect.schema_editor_class(connection)
    table_sql = schema_generator._get_model_sql_data(json_class).get_table_creation_sql()
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
    assert isinstance(source_class._meta.fields_map["target"], OneToOneFieldInstance)

    with registered_live_models(models):
        schema_generator = connection.dialect.schema_editor_class(connection)
        table_sql = schema_generator._get_model_sql_data(source_class).get_table_creation_sql()
        await connection.execute_script("DROP TABLE ix_unique_fk_source")
        await connection.execute_script(table_sql)
        table_info = await SchemaIntrospector.inspect_table(connection, "ix_unique_fk_source")
        assert {column.name: column for column in table_info.columns}["target_id"].is_unique
        target = await target_class.objects.create(id=1)
        await source_class.objects.create(id=1, target=target)
        with pytest.raises(IntegrityError):
            await source_class.objects.create(id=2, target=target)


def test_foreign_key_field_unique_is_rejected():
    with pytest.raises(ConfigurationError, match="OneToOneField"):
        fields.ForeignKeyField("models.Tournament", unique=True)
    assert fields.OneToOneField("models.Tournament").unique
    assert not fields.ForeignKeyField("models.Tournament", unique=False).unique
    with ForeignKeyFieldInstance.replaying_migration_scope():
        fields.ForeignKeyField("models.Tournament", unique=True)
