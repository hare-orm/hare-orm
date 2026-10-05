"""Tests for the CREATE EXTENSION migration operation (CreateExtension/RemoveExtension).

Covers:
- create_extension/drop_extension DDL per dialect (Postgres real DDL, base no-op)
- CreateExtension / RemoveExtension operation forward/backward runs
- Autodetector extension detection (explicit Meta.extensions and field.requires_extension)
- MigrationWriter serialization of extension operations
"""

from __future__ import annotations

import logging

import pytest

from hare import fields
from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
from hare.dialects.base.schema.tables.table_comments import TableComments
from hare.dialects.postgresql.fields.citext_field import CitextField
from hare.dialects.postgresql.fields.postgis_field import PostGISField
from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor
from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
from hare.migrations.autodetection.operation_generator import OperationGenerator
from hare.migrations.operations import (
    CreateExtension,
    CreateModel,
    DeleteModel,
    RemoveExtension,
)
from hare.migrations.state.state import State
from hare.migrations.writer import MigrationWriter
from hare.vectors import VectorField
from tests.utils.fake_client import FakeClient
from tests.utils.operations import apply_operations

pytestmark = pytest.mark.database_independent


class TestEditorTableComments(TableComments):
    __test__ = False

    def get_table_comment_sql(self, table: str, comment: str) -> str:
        return ""

    def get_column_comment_sql(self, table: str, column: str, comment: str) -> str:
        return ""


class _TestEditor(BaseSchemaEditor):
    table_comments_class = TestEditorTableComments

    """Minimal concrete editor for tests (base ANSI SQL dialect)."""


# ---------------------------------------------------------------------------
# 1. create_extension / drop_extension DDL per dialect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        pytest.param("create_extension", 'CREATE EXTENSION IF NOT EXISTS "citext";', id="create_extension_postgres"),
        pytest.param("drop_extension", 'DROP EXTENSION IF EXISTS "citext";', id="drop_extension_postgres"),
    ],
)
@pytest.mark.asyncio
async def test_an_extension_statement_postgres(method_name, expected) -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    await getattr(editor.extensions, method_name)("citext")

    assert client.executed == [expected]


@pytest.mark.asyncio
async def test_create_extension_noop_for_base(caplog: pytest.LogCaptureFixture, empty_state: State) -> None:
    """An editor with no extension support (base ANSI SQL) is a no-op with a log warning, not an
    error - create_extension no longer exists on it at all, so the CreateExtension operation's own
    isinstance(state_editor, SchemaAndExtensionSupportMixin) guard is what skips the call and logs."""
    client = FakeClient("sql")
    editor = _TestEditor(client)

    op = CreateExtension(extension_name="citext")
    with caplog.at_level(logging.WARNING):
        await apply_operations("models", empty_state, op, schema_editor=editor)

    assert client.executed == []
    assert "citext" in caplog.text


@pytest.mark.asyncio
async def test_create_extension_noop_for_sqlite(caplog: pytest.LogCaptureFixture, empty_state: State) -> None:
    """SQLite has no extensions - applying the CreateExtension operation must not raise, even
    though it happens to sit in the same migration file as other operations."""
    client = FakeClient("sqlite", inline_comment=True)
    editor = SqliteSchemaEditor(client)

    op = CreateExtension(extension_name="citext")
    with caplog.at_level(logging.WARNING):
        await apply_operations("models", empty_state, op, schema_editor=editor)

    assert client.executed == []
    assert "citext" in caplog.text


# ---------------------------------------------------------------------------
# 2. CreateExtension / RemoveExtension operation forward/backward
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation_class", "expected"),
    [
        pytest.param(
            CreateExtension, 'CREATE EXTENSION IF NOT EXISTS "citext";', id="create_extension_operation_runs"
        ),
        pytest.param(RemoveExtension, 'DROP EXTENSION IF EXISTS "citext";', id="remove_extension_operation_runs"),
    ],
)
@pytest.mark.asyncio
async def test_an_extension_operation_runs(empty_state: State, operation_class, expected) -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = operation_class(extension_name="citext")
    await apply_operations("models", empty_state, op, schema_editor=editor)

    assert expected in client.executed


@pytest.mark.parametrize(
    ("operation_class", "expected"),
    [
        pytest.param(CreateExtension, 'DROP EXTENSION IF EXISTS "citext";', id="create_extension_operation_backward"),
        pytest.param(
            RemoveExtension, 'CREATE EXTENSION IF NOT EXISTS "citext";', id="remove_extension_operation_backward"
        ),
    ],
)
@pytest.mark.asyncio
async def test_an_extension_operation_backward(empty_state: State, operation_class, expected) -> None:
    client = FakeClient("postgresql", inline_comment=False)
    editor = PostgresqlSchemaEditor(client)

    op = operation_class(extension_name="citext")
    old_state = empty_state.clone()
    op.state_forward("models", empty_state)
    await op.database_backward("models", old_state, empty_state, editor)

    assert expected in client.executed


def test_create_extension_describe() -> None:
    op = CreateExtension(extension_name="citext")
    assert op.describe() == "Create extension citext"


def test_remove_extension_describe() -> None:
    op = RemoveExtension(extension_name="citext")
    assert op.describe() == "Remove extension citext"


# ---------------------------------------------------------------------------
# 3. Autodetector extension detection
# ---------------------------------------------------------------------------


def test_autodetector_generates_create_extension_from_meta() -> None:
    """A model declaring Meta.extensions gets a CreateExtension before its CreateModel."""
    old_state = State()
    new_state = State()
    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "product", "app": "models", "extensions": ("citext",)},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    assert len(ops) >= 2
    assert isinstance(ops[0], CreateExtension)
    assert ops[0].extension_name == "citext"
    assert isinstance(ops[1], CreateModel)


@pytest.mark.parametrize(
    ("model_name", "field_name", "field_class", "extension_name"),
    [
        # Without a matching Meta.extensions declaration.
        pytest.param(
            "Product", "name", CitextField, "citext", id="autodetector_generates_create_extension_from_field"
        ),
        # PostGISField's requires_extension was previously missing entirely, so a PostGISField model's
        # CREATE TABLE failed with "type geography does not exist" on a fresh database that never had
        # CREATE EXTENSION postgis run manually.
        pytest.param(
            "Place",
            "location",
            PostGISField,
            "postgis",
            id="autodetector_generates_create_extension_from_postgis_field",
        ),
    ],
)
def test_autodetector_generates_create_extension_from_a_field(
    model_name, field_name, field_class, extension_name
) -> None:
    """A field's requires_extension is picked up automatically."""
    old_state = State()
    new_state = State()
    CreateModel(
        name=model_name,
        fields=[("id", fields.IntField(primary_key=True)), (field_name, field_class())],
        options={"table": model_name.lower(), "app": "models"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, CreateExtension)]
    assert len(extension_ops) == 1
    assert extension_ops[0].extension_name == extension_name
    assert isinstance(ops[0], CreateExtension)
    assert isinstance(ops[1], CreateModel)


def test_autodetector_generates_create_extension_from_vector_field() -> None:
    """A VectorField's requires_extension ("vector") is picked up automatically, the same way
    CitextField's is - no matching Meta.extensions declaration needed."""
    old_state = State()
    new_state = State()
    CreateModel(
        name="Item",
        fields=[("id", fields.IntField(primary_key=True)), ("embedding", VectorField(dimensions=3))],
        options={"table": "item", "app": "models"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, CreateExtension)]
    assert len(extension_ops) == 1
    assert extension_ops[0].extension_name == "vector"
    assert isinstance(ops[0], CreateExtension)
    assert isinstance(ops[1], CreateModel)


@pytest.mark.parametrize(
    ("field_name", "field", "options"),
    [
        pytest.param("id_copy", fields.IntField(), {"extensions": ("citext",)}, id="declared_in_meta"),
        pytest.param("name", CitextField(), {}, id="named_by_a_field"),
    ],
)
def test_autodetector_never_drops_an_extension(field_name, field, options) -> None:
    """An extension nothing needs any more stays - the database may use it elsewhere; a hand-written
    RemoveExtension drops it."""
    old_state = State()
    new_state = State()
    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True)), (field_name, field)],
        options={"table": "product", "app": "models", **options},
    ).state_forward("models", old_state)

    ops = OperationGenerator(old_state, new_state).generate()

    assert [type(op) for op in ops] == [DeleteModel]


def test_autodetector_no_duplicate_extensions() -> None:
    """Two models needing the same extension produce only one CreateExtension."""
    old_state = State()
    new_state = State()

    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True)), ("name", CitextField())],
        options={"table": "product", "app": "models"},
    ).state_forward("models", new_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True)), ("name", CitextField())],
        options={"table": "category", "app": "models"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, CreateExtension)]
    assert len(extension_ops) == 1
    assert extension_ops[0].extension_name == "citext"


def test_autodetector_no_extension_ops_for_plain_models() -> None:
    """Models without extensions produce no extension operations."""
    old_state = State()
    new_state = State()
    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True)), ("name", fields.TextField())],
        options={"table": "product", "app": "models"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, (CreateExtension, RemoveExtension))]
    assert len(extension_ops) == 0


def test_autodetector_multiple_extensions_sorted() -> None:
    """Multiple new extensions are created in sorted order."""
    old_state = State()
    new_state = State()

    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "product", "app": "models", "extensions": ("postgis",)},
    ).state_forward("models", new_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True))],
        options={"table": "category", "app": "models", "extensions": ("citext",)},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, CreateExtension)]
    assert len(extension_ops) == 2
    assert extension_ops[0].extension_name == "citext"
    assert extension_ops[1].extension_name == "postgis"


def test_autodetector_extension_still_needed_by_another_model() -> None:
    """Deleting one model that used an extension doesn't drop it if another model still needs it."""
    old_state = State()
    new_state = State()

    CreateModel(
        name="Product",
        fields=[("id", fields.IntField(primary_key=True)), ("name", CitextField())],
        options={"table": "product", "app": "models"},
    ).state_forward("models", old_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True)), ("name", CitextField())],
        options={"table": "category", "app": "models"},
    ).state_forward("models", old_state)
    CreateModel(
        name="Category",
        fields=[("id", fields.IntField(primary_key=True)), ("name", CitextField())],
        options={"table": "category", "app": "models"},
    ).state_forward("models", new_state)

    ops = OperationGenerator(old_state, new_state).generate()

    extension_ops = [op for op in ops if isinstance(op, (CreateExtension, RemoveExtension))]
    assert len(extension_ops) == 0


# ---------------------------------------------------------------------------
# 4. MigrationWriter serialization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "operation_class", "expected"),
    [
        pytest.param(
            "0001_initial",
            CreateExtension,
            "ops.CreateExtension(extension_name='citext')",
            id="writer_serializes_create_extension",
        ),
        pytest.param(
            "0002_remove",
            RemoveExtension,
            "ops.RemoveExtension(extension_name='citext')",
            id="writer_serializes_remove_extension",
        ),
    ],
)
def test_writer_serializes_an_extension_operation(name, operation_class, expected) -> None:
    writer = MigrationWriter(
        name=name,
        app_label="models",
        operations=[operation_class(extension_name="citext")],
    )
    output = writer.as_string()
    assert expected in output


def test_writer_extension_with_create_model() -> None:
    """Full migration with CreateExtension + CreateModel serializes correctly, in order."""
    writer = MigrationWriter(
        name="0001_initial",
        app_label="models",
        operations=[
            CreateExtension(extension_name="citext"),
            CreateModel(
                name="Product",
                fields=[("id", fields.IntField(primary_key=True))],
                options={"table": "product"},
            ),
        ],
    )
    output = writer.as_string()
    assert "ops.CreateExtension(extension_name='citext')" in output
    assert "ops.CreateModel(" in output
    extension_pos = output.index("CreateExtension")
    model_pos = output.index("CreateModel")
    assert extension_pos < model_pos
