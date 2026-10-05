from __future__ import annotations

import warnings

import pytest

from hare import fields
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.fields.constants import DB_DEFAULT_NOT_SET
from hare.time import UTC
from hare.warnings import RedundantDbDefaultWarning

pytestmark = pytest.mark.database_independent

# ============================================================================
# has_db_default() tests
# ============================================================================


def test_db_default_not_set():
    f = fields.IntField()
    assert f.has_db_default() is False
    assert f.db_default is DB_DEFAULT_NOT_SET


@pytest.mark.parametrize(
    ("field_class", "options", "value"),
    [
        pytest.param(fields.IntField, {}, 42, id="db_default_set_int"),
        pytest.param(fields.CharField, {"max_length": 100}, "hello", id="db_default_set_str"),
        pytest.param(fields.BooleanField, {}, True, id="db_default_set_bool"),
        pytest.param(fields.FloatField, {}, 3.14, id="db_default_set_float"),
        # None is a valid db_default (maps to DEFAULT NULL).
        pytest.param(fields.IntField, {"null": True}, None, id="db_default_set_none"),
        # 0, "" and False are valid db_defaults, not to be confused with the sentinel.
        pytest.param(fields.IntField, {}, 0, id="db_default_set_zero"),
        pytest.param(fields.CharField, {"max_length": 100}, "", id="db_default_set_empty_string"),
        pytest.param(fields.BooleanField, {}, False, id="db_default_set_false"),
    ],
)
def test_db_default_set(field_class, options, value):
    f = field_class(db_default=value, **options)
    assert f.has_db_default() is True
    assert f.db_default == value
    assert type(f.db_default) is type(value)


# ============================================================================
# Callable db_default raises ConfigurationError
# ============================================================================


def get_forty_two():
    return 42


@pytest.mark.parametrize(
    "db_default",
    [
        pytest.param(lambda: 1, id="db_default_callable_raises"),
        pytest.param(get_forty_two, id="db_default_callable_function_raises"),
    ],
)
def test_a_callable_db_default_raises(db_default):
    with pytest.raises(ConfigurationError, match=r"db_default must be a static value or SqlDefault\(\.\.\.\)"):
        fields.IntField(db_default=db_default)


# ============================================================================
# db_default + default coexistence
# ============================================================================


def test_db_default_and_default_coexist():
    with pytest.warns(RedundantDbDefaultWarning):
        f = fields.IntField(default=1, db_default=2)
    assert f.default == 1
    assert f.db_default == 2
    assert f.has_db_default() is True


@pytest.mark.parametrize(
    ("options", "message"),
    [
        pytest.param(
            {"default": 1, "db_default": 2},
            "IntField.*both `default` and `db_default`",
            id="db_default_and_default_coexist_warns",
        ),
        # _prepare_insert_columns excludes every generated=True column from the INSERT entirely - a
        # `default` there is silently thrown away, just like the db_default case, but this used to
        # have no warning at all.
        pytest.param({"primary_key": True, "default": 999}, "generated field", id="default_on_generated_field_warns"),
    ],
)
def test_a_default_that_is_thrown_away_warns(options, message):
    with pytest.warns(RedundantDbDefaultWarning, match=message):
        fields.IntField(**options)


def test_db_default_and_default_coexist_warns_even_with_falsy_default():
    """default=0 is a real, explicitly-set default - not to be confused with "not set"."""
    with pytest.warns(RedundantDbDefaultWarning):
        fields.IntField(default=0, db_default=2)


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"db_default": 2}, id="db_default_alone_does_not_warn"),
        pytest.param({"default": 1}, id="default_alone_does_not_warn"),
        pytest.param({"primary_key": True}, id="generated_alone_does_not_warn"),
    ],
)
def test_a_default_alone_does_not_warn(options):
    with warnings.catch_warnings():
        warnings.simplefilter("error", RedundantDbDefaultWarning)
        fields.IntField(**options)


def test_default_on_explicitly_non_generated_field_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error", RedundantDbDefaultWarning)
        fields.IntField(primary_key=True, generated=False, default=999)


# ============================================================================
# deconstruct() tests
# ============================================================================


def test_deconstruct_without_db_default():
    f = fields.IntField()
    f.model_field_name = "test_field"
    path, args, kwargs = f.deconstruct()
    assert "db_default" not in kwargs


@pytest.mark.parametrize(
    ("field_class", "options", "value"),
    [
        pytest.param(fields.IntField, {}, 42, id="deconstruct_with_db_default_int"),
        pytest.param(fields.CharField, {"max_length": 100}, "hello", id="deconstruct_with_db_default_str"),
        pytest.param(fields.IntField, {"null": True}, None, id="deconstruct_with_db_default_none"),
        pytest.param(fields.IntField, {}, 0, id="deconstruct_with_db_default_zero"),
        pytest.param(fields.BooleanField, {}, False, id="deconstruct_with_db_default_false"),
    ],
)
def test_deconstruct_with_db_default(field_class, options, value):
    f = field_class(db_default=value, **options)
    f.model_field_name = "test_field"
    path, args, kwargs = f.deconstruct()
    assert "db_default" in kwargs
    assert kwargs["db_default"] == value
    assert type(kwargs["db_default"]) is type(value)


def test_deconstruct_with_both_default_and_db_default():
    with pytest.warns(RedundantDbDefaultWarning):
        f = fields.IntField(default=1, db_default=2)
    f.model_field_name = "test_field"
    path, args, kwargs = f.deconstruct()
    assert kwargs["default"] == 1
    assert kwargs["db_default"] == 2


# ============================================================================
# Sentinel behavior
# ============================================================================


def test_sentinel_repr():
    assert repr(DB_DEFAULT_NOT_SET) == "NOT_PROVIDED"


def test_sentinel_bool():
    assert bool(DB_DEFAULT_NOT_SET) is False


def test_sentinel_is_singleton():
    """Verify the sentinel instance is the module-level singleton."""
    f = fields.IntField()
    assert f.db_default is DB_DEFAULT_NOT_SET


# ============================================================================
# __copy__ preserves db_default
# ============================================================================


def test_copy_preserves_db_default():
    import copy

    f = fields.IntField(db_default=42)
    f.model_field_name = "test_field"
    f2 = copy.copy(f)
    assert f2.has_db_default() is True
    assert f2.db_default == 42


def test_copy_preserves_no_db_default():
    import copy

    f = fields.IntField()
    f.model_field_name = "test_field"
    f2 = copy.copy(f)
    assert f2.has_db_default() is False


# ============================================================================
# Schema generation tests (using SQLite generator)
# ============================================================================


def _get_sqlite_default_sql(field_obj, model_class=None):
    """Helper to get the DEFAULT SQL for a field using the SQLite schema generator."""
    from unittest.mock import MagicMock

    from hare.dialects.sqlite.constants import SQLITE_DIALECT

    mock_client = MagicMock()
    mock_client.dialect = SQLITE_DIALECT
    editor = SQLITE_DIALECT.schema_editor_class(mock_client)

    if model_class is None:
        model_class = MagicMock()

    return editor.column_definitions.get_column_default_sql(field_obj, model_class)


def _get_postgres_default_sql(field_obj, model_class=None):
    """Helper to get the DEFAULT SQL for a field using the Postgres schema generator."""
    from unittest.mock import MagicMock

    from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT

    mock_client = MagicMock()
    mock_client.dialect = POSTGRESQL_DIALECT
    editor = POSTGRESQL_DIALECT.schema_editor_class(mock_client)

    if model_class is None:
        model_class = MagicMock()

    return editor.column_definitions.get_column_default_sql(field_obj, model_class)


@pytest.mark.parametrize(
    ("make_field", "expected"),
    [
        pytest.param(lambda: fields.IntField(db_default=42), " DEFAULT 42", id="schema_db_default_int"),
        pytest.param(
            lambda: fields.CharField(max_length=100, db_default="hello"),
            " DEFAULT 'hello'",
            id="schema_db_default_str",
        ),
        pytest.param(lambda: fields.BooleanField(db_default=True), " DEFAULT 1", id="schema_db_default_bool_true"),
        pytest.param(lambda: fields.BooleanField(db_default=False), " DEFAULT 0", id="schema_db_default_bool_false"),
        pytest.param(
            lambda: fields.IntField(null=True, db_default=None), " DEFAULT NULL", id="schema_db_default_none"
        ),
        pytest.param(lambda: fields.IntField(), "", id="schema_no_db_default_no_default"),
        pytest.param(lambda: fields.FloatField(db_default=3.14), " DEFAULT 3.14", id="schema_db_default_float"),
    ],
)
def test_schema_db_default(make_field, expected):
    sql = _get_sqlite_default_sql(make_field())
    assert sql == expected


def test_schema_db_default_bytes_does_not_crash():
    """encoders (hare/converters.py) has no entry for `bytes` - both the Postgres and SQLite
    _escape_default_value() implementations used to call encoders.get(type(default))(default)
    with no fallback for a missing encoder, so encoders.get(bytes) -> None -> None(default)
    crashed with a bare TypeError, aborting schema generation entirely instead of just that one
    field. Not asserting on the exact rendered SQL (repr() isn't a real bytea/blob literal) -
    only that generating a default for a BinaryField no longer raises."""
    f = fields.BinaryField(db_default=b"\x00\x01")
    _get_sqlite_default_sql(f)
    _get_postgres_default_sql(f)


def test_schema_db_default_overrides_default():
    """When both default and db_default are set, db_default controls the SQL."""
    with pytest.warns(RedundantDbDefaultWarning):
        f = fields.IntField(default=1, db_default=2)
    sql = _get_sqlite_default_sql(f)
    assert sql == " DEFAULT 2"


# ============================================================================
# Migration field signature tests
# ============================================================================


def test_field_signature_includes_db_default():
    """Changing db_default produces different field signatures."""
    from hare.migrations.autodetection.state_signatures import StateSignatures

    f1 = fields.IntField(db_default=1)
    f1.model_field_name = "val"
    f2 = fields.IntField(db_default=2)
    f2.model_field_name = "val"
    f_none = fields.IntField()
    f_none.model_field_name = "val"

    sig1 = StateSignatures.get_field_signature(f1)
    sig2 = StateSignatures.get_field_signature(f2)
    sig_none = StateSignatures.get_field_signature(f_none)

    assert sig1 != sig2, "Different db_default values should produce different signatures"
    assert sig1 != sig_none, "db_default=1 should differ from no db_default"
    assert sig2 != sig_none, "db_default=2 should differ from no db_default"


def test_field_signature_distinguishes_db_default_none_from_not_set():
    """db_default=None and no db_default should produce different signatures."""
    from hare.migrations.autodetection.state_signatures import StateSignatures

    f_with_none = fields.IntField(null=True, db_default=None)
    f_with_none.model_field_name = "val"
    f_without = fields.IntField(null=True)
    f_without.model_field_name = "val"

    sig_with_none = StateSignatures.get_field_signature(f_with_none)
    sig_without = StateSignatures.get_field_signature(f_without)

    assert sig_with_none != sig_without, "db_default=None should differ from no db_default in field signature"


def test_field_signature_excludes_default():
    """Changing Python-level default should NOT change the field signature."""
    from hare.migrations.autodetection.state_signatures import StateSignatures

    f1 = fields.IntField(default=1)
    f1.model_field_name = "val"
    f2 = fields.IntField(default=2)
    f2.model_field_name = "val"

    sig1 = StateSignatures.get_field_signature(f1)
    sig2 = StateSignatures.get_field_signature(f2)

    assert sig1 == sig2, "Different default values should NOT produce different signatures"


# ============================================================================
# Migration schema editor ALTER / ADD COLUMN tests
# ============================================================================


def _make_model(model_name, table, **model_fields):
    """Create a model class dynamically for migration tests."""
    from hare.models import Model

    attrs = dict(model_fields)
    meta = type("Meta", (), {"app": "models", "table": table})
    attrs["Meta"] = meta
    return type(model_name, (Model,), attrs)


def _make_test_editor():
    """Create a BaseSchemaEditor subclass with a FakeClient for testing."""
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from tests.utils.fake_client import FakeClient

    class TestSchemaEditorTableComments(TableComments):
        def get_table_comment_sql(self, table, comment):
            return ""

        def get_column_comment_sql(self, table, column, comment):
            return ""

    class TestSchemaEditor(BaseSchemaEditor):
        table_comments_class = TestSchemaEditorTableComments

    client = FakeClient("sql")
    editor = TestSchemaEditor(client)
    return editor, client


@pytest.mark.asyncio
async def test_alter_field_sql_set_default():
    """ALTER TABLE generates SET DEFAULT when adding db_default."""
    OldModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(),
    )
    NewModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=10),
    )

    editor, client = _make_test_editor()
    await editor.alter_field(OldModel, NewModel, "score")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "SET DEFAULT" in sql
    assert "10" in sql


@pytest.mark.asyncio
async def test_alter_field_sql_drop_default():
    """ALTER TABLE generates DROP DEFAULT when removing db_default."""
    OldModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=10),
    )
    NewModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(),
    )

    editor, client = _make_test_editor()
    await editor.alter_field(OldModel, NewModel, "score")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "DROP DEFAULT" in sql


@pytest.mark.asyncio
async def test_alter_field_sql_change_default():
    """ALTER TABLE generates SET DEFAULT with the new value when changing db_default."""
    OldModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=10),
    )
    NewModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=20),
    )

    editor, client = _make_test_editor()
    await editor.alter_field(OldModel, NewModel, "score")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "SET DEFAULT" in sql
    assert "20" in sql


@pytest.mark.asyncio
async def test_add_field_with_db_default():
    """ADD COLUMN includes DEFAULT clause when field has db_default."""
    WidgetModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=42),
    )

    editor, client = _make_test_editor()
    await editor.add_field(WidgetModel, WidgetModel, "score")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "ADD COLUMN" in sql
    assert "DEFAULT" in sql
    assert "42" in sql


@pytest.mark.asyncio
async def test_add_field_without_db_default_no_default_clause():
    """ADD COLUMN omits DEFAULT clause when field has no db_default."""
    WidgetModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        name=fields.TextField(),
    )

    editor, client = _make_test_editor()
    await editor.add_field(WidgetModel, WidgetModel, "name")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert "ADD COLUMN" in sql
    assert "DEFAULT" not in sql


# ============================================================================
# SqlDefault and Now expression tests
# ============================================================================


def test_sql_default_construction_and_get_sql():
    """SqlDefault construction and get_sql."""
    from hare.fields.db_defaults import SqlDefault

    sd = SqlDefault("CURRENT_TIMESTAMP")
    assert sd.get_sql() == "CURRENT_TIMESTAMP"
    assert sd.get_sql(DialectRegistry.get_dialect("postgresql")) == "CURRENT_TIMESTAMP"
    assert SqlDefault("40 + 2").get_sql(DialectRegistry.get_dialect("sqlite")) == "(40 + 2)"
    assert SqlDefault("(1) + (2)").get_sql(DialectRegistry.get_dialect("sqlite")) == "((1) + (2))"
    assert SqlDefault("(datetime('now'))").get_sql(DialectRegistry.get_dialect("sqlite")) == "(datetime('now'))"
    assert SqlDefault("('(' || x)").get_sql(DialectRegistry.get_dialect("sqlite")) == "('(' || x)"
    assert SqlDefault("-1.5").get_sql(DialectRegistry.get_dialect("sqlite")) == "-1.5"
    assert SqlDefault("'a''b'").get_sql(DialectRegistry.get_dialect("sqlite")) == "'a''b'"


def test_now_construction():
    """Now construction."""
    from hare.fields.db_defaults import Now

    n = Now()
    assert n.get_sql() == "CURRENT_TIMESTAMP"


def test_now_dialect_other():
    """Now emits a dialect-specific expression for SQLite, the statement's moment on Postgres."""
    from hare.dialects.sqlite.constants import SQLITE_NOW_UTC_SQL as NOW_SQLITE_UTC_SQL
    from hare.fields.db_defaults import Now

    n = Now()
    assert n.get_sql(DialectRegistry.get_dialect("sqlite")) == NOW_SQLITE_UTC_SQL
    assert n.get_sql(DialectRegistry.get_dialect("postgresql")) == "STATEMENT_TIMESTAMP()"
    assert n.get_sql(DialectRegistry.get_dialect("sql")) == "CURRENT_TIMESTAMP"


def test_sql_default_equality_and_hashing():
    """SqlDefault equality and hashing."""
    from hare.fields.db_defaults import SqlDefault

    a = SqlDefault("X")
    b = SqlDefault("X")
    c = SqlDefault("Y")
    assert a == b
    assert a != c
    assert hash(a) == hash(b)
    # Can be used in sets/dicts
    s = {a, b, c}
    assert len(s) == 2


def test_sql_default_repr():
    """SqlDefault repr."""
    from hare.fields.db_defaults import SqlDefault

    assert repr(SqlDefault("CURRENT_TIMESTAMP")) == "SqlDefault('CURRENT_TIMESTAMP')"


def test_now_repr():
    """Now repr."""
    from hare.fields.db_defaults import Now

    assert repr(Now()) == "Now()"


def test_sql_default_passes_field_validation():
    """SqlDefault is not callable -- passes field validation."""
    from hare.fields.db_defaults import SqlDefault

    # Should not raise
    f = fields.DatetimeField(db_default=SqlDefault("CURRENT_TIMESTAMP"))
    assert f.has_db_default() is True


# ============================================================================
# Schema generation with SqlDefault / Now
# ============================================================================


def test_schema_generation_with_sql_default():
    """Schema generation with SqlDefault."""
    from hare.fields.db_defaults import SqlDefault

    f = fields.DatetimeField(db_default=SqlDefault("CURRENT_TIMESTAMP"))
    sql = _get_sqlite_default_sql(f)
    assert sql == " DEFAULT CURRENT_TIMESTAMP"


def test_schema_generation_with_now():
    """Schema generation with Now() uses SQLite's higher-precision expression, with an explicit
    UTC offset so a written row reads back as an aware UTC value instead of being mislabeled as
    already being in whatever zone is configured."""
    from hare.fields.db_defaults import Now

    f = fields.DatetimeField(db_default=Now())
    sql = _get_sqlite_default_sql(f)
    from hare.dialects.sqlite.constants import SQLITE_NOW_UTC_SQL as NOW_SQLITE_UTC_SQL

    assert sql == f" DEFAULT {NOW_SQLITE_UTC_SQL}"


def test_schema_generation_with_now_postgres():
    """Schema generation with Now() on Postgres uses the statement's moment."""
    from hare.fields.db_defaults import Now

    f = fields.DatetimeField(db_default=Now())
    sql = _get_postgres_default_sql(f)
    assert sql == " DEFAULT STATEMENT_TIMESTAMP()"


def test_schema_generation_with_custom_sql_expression():
    """Schema generation with custom SQL expression."""
    from hare.fields.db_defaults import SqlDefault

    f = fields.CharField(max_length=100, db_default=SqlDefault("'unknown'"))
    sql = _get_sqlite_default_sql(f)
    assert sql == " DEFAULT 'unknown'"


# ============================================================================
# Migration editor with SqlDefault / Now
# ============================================================================


@pytest.mark.asyncio
async def test_add_field_with_sql_default():
    """Migration add_field with SqlDefault."""
    from hare.fields.db_defaults import Now

    WidgetModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        created=fields.DatetimeField(db_default=Now()),
    )

    editor, client = _make_test_editor()
    await editor.add_field(WidgetModel, WidgetModel, "created")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert sql == 'ALTER TABLE "widget" ADD COLUMN "created" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'


@pytest.mark.asyncio
async def test_alter_field_set_default_with_sql_default():
    """Migration alter_field SET DEFAULT with SqlDefault."""
    from hare.fields.db_defaults import Now

    OldModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        created=fields.DatetimeField(),
    )
    NewModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        created=fields.DatetimeField(db_default=Now()),
    )

    editor, client = _make_test_editor()
    await editor.alter_field(OldModel, NewModel, "created")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert sql == 'ALTER TABLE "widget" ALTER COLUMN "created" SET DEFAULT CURRENT_TIMESTAMP'


@pytest.mark.asyncio
async def test_alter_field_change_from_literal_to_sql_default():
    """Migration alter_field change from literal to SqlDefault."""
    from hare.fields.db_defaults import SqlDefault

    OldModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=42),
    )
    NewModel = _make_model(
        "Widget",
        "widget",
        id=fields.IntField(primary_key=True),
        score=fields.IntField(db_default=SqlDefault("NOW()")),
    )

    editor, client = _make_test_editor()
    await editor.alter_field(OldModel, NewModel, "score")

    assert len(client.executed) == 1
    sql = client.executed[0]
    assert sql == 'ALTER TABLE "widget" ALTER COLUMN "score" SET DEFAULT NOW()'


# ============================================================================
# deconstruct() with SqlDefault / Now
# ============================================================================


def test_deconstruct_with_now():
    """deconstruct() preserves Now instance."""
    from hare.fields.db_defaults import Now

    n = Now()
    f = fields.DatetimeField(db_default=n)
    f.model_field_name = "created"
    path, args, kwargs = f.deconstruct()
    assert kwargs["db_default"] is n


def test_render_value_with_now():
    """MigrationWriter.render_value() preserves Now() in migration files."""
    from hare.fields.db_defaults import Now
    from hare.migrations.writer import ImportManager, MigrationWriter

    imports = ImportManager()
    n = Now()
    result = MigrationWriter.render_value(n, imports)
    assert result == "Now()"
    assert "Now" in str(imports)


def test_render_value_with_sql_default():
    """MigrationWriter.render_value() preserves SqlDefault in migration files."""
    from hare.fields.db_defaults import SqlDefault
    from hare.migrations.writer import ImportManager, MigrationWriter

    imports = ImportManager()
    sd = SqlDefault("gen_random_uuid()")
    result = MigrationWriter.render_value(sd, imports)
    assert result == "SqlDefault('gen_random_uuid()')"
    assert "SqlDefault" in str(imports)


def test_render_value_keeps_the_class_of_a_default_of_its_own():
    from hare.fields.db_defaults import SqlDefault
    from hare.migrations.writer import ImportManager, MigrationWriter

    class SevenDefault(SqlDefault):
        """Written by a dialect's renderer - its own SQL is empty."""

        def __init__(self) -> None:
            super().__init__("")

    SevenDefault.__module__ = "tests.fields.seven_defaults"
    SevenDefault.__qualname__ = "SevenDefault"
    imports = ImportManager()
    assert MigrationWriter.render_value(SevenDefault(), imports) == "SevenDefault()"
    assert "SevenDefault" in imports.imports["tests.fields.seven_defaults"]
    # A class the migration can't import is written by its SQL.
    SevenDefault.__qualname__ = "test_render.<locals>.SevenDefault"
    imports = ImportManager()
    assert MigrationWriter.render_value(SevenDefault(), imports) == "SqlDefault('')"


def test_render_value_random_hex():
    """MigrationWriter.render_value() serializes RandomHex as RandomHex(), not as SqlDefault(...)."""
    from hare.fields.db_defaults import RandomHex
    from hare.migrations.writer import ImportManager, MigrationWriter

    imports = ImportManager()
    rh = RandomHex()
    result = MigrationWriter.render_value(rh, imports)
    assert result == "RandomHex()"
    assert "hare.fields.db_defaults" in imports.imports
    assert "RandomHex" in imports.imports["hare.fields.db_defaults"]


def test_render_value_sql_default_still_works():
    """MigrationWriter.render_value() still serializes plain SqlDefault correctly (regression check)."""
    from hare.fields.db_defaults import SqlDefault
    from hare.migrations.writer import ImportManager, MigrationWriter

    imports = ImportManager()
    sd = SqlDefault("custom")
    result = MigrationWriter.render_value(sd, imports)
    assert result == "SqlDefault('custom')"
    assert "hare.fields.db_defaults" in imports.imports
    assert "SqlDefault" in imports.imports["hare.fields.db_defaults"]


def test_render_value_now_still_works():
    """MigrationWriter.render_value() still serializes Now correctly (regression check)."""
    from hare.fields.db_defaults import Now
    from hare.migrations.writer import ImportManager, MigrationWriter

    imports = ImportManager()
    n = Now()
    result = MigrationWriter.render_value(n, imports)
    assert result == "Now()"
    assert "hare.fields.db_defaults" in imports.imports
    assert "Now" in imports.imports["hare.fields.db_defaults"]


# ============================================================================
# Static date/time db_default literals match Python-side writes
# ============================================================================


def test_schema_sqlite_datetime_db_default_rendered_as_utc_text():
    """A static DatetimeField db_default on SQLite is stored as the same UTC text (space
    separator) a Python-side write of the same instant produces, not isoformat()'s "T" form
    with the value's own offset."""
    from datetime import datetime

    from hare.time import ZoneInfo
    from tests.utils.timezone_context import override_timezone

    with override_timezone(use_timezone=True, timezone="Asia/Tokyo"):
        tokyo_field = fields.DatetimeField(db_default=datetime(2020, 1, 2, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
        assert _get_sqlite_default_sql(tokyo_field) == " DEFAULT '2020-01-02 03:00:00+00:00'"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            naive_field = fields.DatetimeField(db_default=datetime(2020, 1, 2, 12, 0))
            assert _get_sqlite_default_sql(naive_field) == " DEFAULT '2020-01-02 03:00:00+00:00'"
    with override_timezone(use_timezone=True):
        micro_field = fields.DatetimeField(db_default=datetime(2020, 1, 2, 3, 4, 5, 678901, tzinfo=UTC))
        assert _get_sqlite_default_sql(micro_field) == " DEFAULT '2020-01-02 03:04:05.678901+00:00'"


def test_schema_sqlite_datetime_db_default_naive_local_text_with_use_tz_false():
    """Under use_timezone=False a naive static default stays naive local text, like every naive write."""
    from datetime import datetime

    from tests.utils.timezone_context import override_timezone

    with override_timezone(use_timezone=False):
        field = fields.DatetimeField(db_default=datetime(2020, 1, 2, 12, 0))
        assert _get_sqlite_default_sql(field) == " DEFAULT '2020-01-02 12:00:00'"


@pytest.mark.asyncio
async def test_sqlite_schema_editor_datetime_default_and_backfill_rendered_as_utc_text():
    """The SQLite migration editor renders a DatetimeField db_default and a default backfill the
    same way generate_schemas() does."""
    from datetime import datetime

    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.time import ZoneInfo
    from tests.utils.fake_client import FakeClient
    from tests.utils.timezone_context import override_timezone

    value = datetime(2020, 1, 2, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    with override_timezone(use_timezone=True, timezone="Asia/Tokyo"):
        WidgetModel = _make_model(
            "Widget",
            "widget",
            id=fields.IntField(primary_key=True),
            stamped=fields.DatetimeField(db_default=value),
        )
        client = FakeClient("sqlite")
        editor = SqliteSchemaEditor(client)
        await editor.add_field(WidgetModel, WidgetModel, "stamped")
        assert client.executed[0].endswith("DEFAULT '2020-01-02 03:00:00+00:00'")
        backfill_field = fields.DatetimeField(default=value)
        assert (
            editor.column_backfill.backfill_default_sql_literal(backfill_field, value, WidgetModel)
            == "'2020-01-02 03:00:00+00:00'"
        )


def test_schema_postgres_naive_datetime_db_default_carries_local_offset_with_use_tz_false():
    """Under use_timezone=False both Postgres drivers bind a naive datetime as local system time - a
    naive static default literal must carry that same local offset instead of being read in the
    server session's zone."""
    from datetime import datetime

    from tests.utils.timezone_context import override_timezone

    value = datetime(2020, 1, 2, 12, 0)
    with override_timezone(use_timezone=False):
        field = fields.DatetimeField(db_default=value)
        assert _get_postgres_default_sql(field) == f" DEFAULT '{value.astimezone().isoformat()}'"


def test_schema_postgres_time_db_default_keeps_its_offset():
    """A static TimeField db_default on Postgres keeps the offset to_db_value() attaches (the
    configured zone's standard offset) instead of taking the server session's."""
    from datetime import time

    from tests.utils.timezone_context import override_timezone

    with override_timezone(use_timezone=True, timezone="Asia/Tokyo"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            field = fields.TimeField(db_default=time(12, 0))
            assert _get_postgres_default_sql(field) == " DEFAULT '12:00:00+09:00'"
    with override_timezone(use_timezone=False):
        field = fields.TimeField(db_default=time(12, 0, 0, 500))
        assert _get_postgres_default_sql(field) == " DEFAULT '12:00:00.000500+00:00'"


def test_schema_postgres_bool_db_default_rendered_as_sql_keyword():
    """generate_schemas() and the migration editor render a Postgres boolean default alike."""
    assert _get_postgres_default_sql(fields.BooleanField(db_default=True)) == " DEFAULT TRUE"
    assert _get_postgres_default_sql(fields.BooleanField(db_default=False)) == " DEFAULT FALSE"
