from __future__ import annotations

import functools
import sys
import textwrap
from enum import IntEnum, StrEnum
from pathlib import Path

import pytest

from hare import fields
from hare.ddl.constraints import ExclusionConstraint, UniqueConstraint
from hare.ddl.enums import TriggerEvent
from hare.ddl.indexes import Index, PartialIndex
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.ddl.schema_objects.trigger import Trigger
from hare.exceptions import ConfigurationError
from hare.migrations.operations import (
    AddConstraint,
    AddIndex,
    AddTrigger,
    AlterColumnNotNullSafe,
    AlterField,
    AlterModelOptions,
    AlterTrigger,
    BackfillColumn,
    CreateModel,
    DeleteModel,
    RemoveConstraint,
    RemoveIndex,
    RemoveTrigger,
    RenameConstraint,
    RenameField,
    RenameIndex,
    RunPython,
    RunSQL,
    SQLOperation,
)
from hare.migrations.writer import ImportManager, MigrationWriter


class Status(IntEnum):
    """Status enum for testing IntEnumField."""

    ACTIVE = 1
    INACTIVE = 2


class Role(StrEnum):
    """Role enum for testing CharEnumField."""

    ADMIN = "admin"
    USER = "user"


@pytest.fixture(autouse=True)
def forget_imported_app_package():
    """Drops the tmp_path "app" package from sys.modules so later tests import their own."""
    yield
    for module_name in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
        del sys.modules[module_name]


def _prepare_migration_package(tmp_path: Path, app_label: str) -> str:
    package_dir = tmp_path / app_label
    migrations_dir = package_dir / "migrations"
    migrations_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="ascii")
    (migrations_dir / "__init__.py").write_text("", encoding="ascii")
    return f"{app_label}.migrations"


def _write_migration(
    tmp_path: Path,
    monkeypatch,
    name: str,
    operations,
    expected: str,
) -> None:
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))

    writer = MigrationWriter(
        name,
        "app",
        operations,
        migrations_module=module_path,
    )
    migration_path = writer.write()
    content = migration_path.read_text(encoding="ascii")
    assert content == expected


def test_write_uses_lf_line_endings_on_every_platform(tmp_path: Path, monkeypatch) -> None:
    """MigrationWriter.write() must always emit LF line endings, even on Windows -
    Path.write_text()'s default text-mode newline translation would otherwise silently turn
    every "\\n" in as_string() into "\\r\\n" there."""
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))

    operations = [
        CreateModel(
            name="Widget",
            fields=[("id", fields.IntField(primary_key=True))],
        )
    ]
    writer = MigrationWriter("0001_initial", "app", operations, migrations_module=module_path)
    migration_path = writer.write()

    raw_bytes = migration_path.read_bytes()
    assert b"\r\n" not in raw_bytes
    assert b"\n" in raw_bytes


def test_writer_format_create_model_basic(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("name", fields.CharField(max_length=100)),
            ],
        )
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Widget',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('name', fields.CharField(max_length=100)),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0001_initial", operations, expected)


def test_writer_format_rename_and_alter(tmp_path: Path, monkeypatch) -> None:
    operations = [
        RenameField(model_name="Widget", old_name="title", new_name="name"),
        AlterField(
            model_name="Widget",
            name="name",
            field=fields.CharField(max_length=120, null=True),
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.RenameField(model_name='Widget', old_name='title', new_name='name'),
                ops.AlterField(model_name='Widget', name='name', field=fields.CharField(max_length=120, null=True)),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0002_rename_alter", operations, expected)


def test_writer_format_rename_field_with_field_argument(tmp_path: Path, monkeypatch) -> None:
    """RenameField's optional field= (the autodetector passes the live new field so its real
    source_field survives the rename) must round-trip through the written migration file too -
    a written migration that dropped it would silently regress to the old bare-rename behavior on
    its next run."""
    operations = [
        RenameField(
            model_name="Widget",
            old_name="title",
            new_name="headline",
            field=fields.CharField(max_length=120, source_field="title"),
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.RenameField(
                    model_name='Widget',
                    old_name='title',
                    new_name='headline',
                    field=fields.CharField(max_length=120, source_field='title'),
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0003_rename_with_field", operations, expected)


def test_writer_format_options_indexes_constraints(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Widget",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("name", fields.CharField(max_length=100)),
                ("status", fields.CharField(max_length=20)),
            ],
            options={
                "unique_together": (("name",),),
                "indexes": [
                    Index(fields=("name",), name="idx_widget_name"),
                    PartialIndex(fields=("status",), name="idx_widget_status", condition=RawSQLTerm("active")),
                ],
                "constraints": [
                    UniqueConstraint(fields=("name", "status"), name="uniq_widget_name_status"),
                ],
            },
        ),
        AddIndex("Widget", Index(fields=("name",), name="idx_widget_name")),
        AddConstraint("Widget", UniqueConstraint(fields=("name",), name="uniq_widget_name")),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.ddl import RawSQLTerm
        from hare import fields
        from hare.ddl.indexes import Index, PartialIndex
        from hare.ddl.constraints import UniqueConstraint

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Widget',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('name', fields.CharField(max_length=100)),
                        ('status', fields.CharField(max_length=20)),
                    ],
                    options={'unique_together': (('name',),), 'indexes': [Index(fields=['name'], name='idx_widget_name'), PartialIndex(fields=['status'], name='idx_widget_status', condition=RawSQLTerm('active'))], 'constraints': [UniqueConstraint(fields=['name', 'status'], name='uniq_widget_name_status')]},
                ),
                ops.AddIndex(model_name='Widget', index=Index(fields=['name'], name='idx_widget_name')),
                ops.AddConstraint(model_name='Widget', constraint=UniqueConstraint(fields=['name'], name='uniq_widget_name')),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0003_options", operations, expected)


def test_writer_format_exclusion_constraint(tmp_path: Path, monkeypatch) -> None:
    operations = [
        AddConstraint(
            "Order",
            ExclusionConstraint(name="no_overlap", expressions=(("resource", "="), ("during", "&&"))),
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.ddl.enums import ExclusionConstraintUsing
        from hare.ddl.constraints import ExclusionConstraint

        class Migration(migrations.Migration):
            operations = [
                ops.AddConstraint(
                    model_name='Order',
                    constraint=ExclusionConstraint(name='no_overlap', expressions=(('resource', '='), ('during', '&&')), using=ExclusionConstraintUsing.GIST),
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0004_exclusion", operations, expected)


def test_writer_format_exclusion_constraint_with_raw_sql_expression(tmp_path: Path, monkeypatch) -> None:
    """A RawSQLTerm expression entry round-trips through migration-file generation the same way
    a raw SQL condition (RawSQLTerm) does."""
    operations = [
        AddConstraint(
            "Order",
            ExclusionConstraint(
                name="no_overlap_range",
                expressions=(
                    ("resource", "="),
                    (RawSQLTerm("tsrange(start_date, end_date)"), "&&"),
                ),
            ),
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.ddl import RawSQLTerm
        from hare.ddl.enums import ExclusionConstraintUsing
        from hare.ddl.constraints import ExclusionConstraint

        class Migration(migrations.Migration):
            operations = [
                ops.AddConstraint(
                    model_name='Order',
                    constraint=ExclusionConstraint(name='no_overlap_range', expressions=(('resource', '='), (RawSQLTerm('tsrange(start_date, end_date)'), '&&')), using=ExclusionConstraintUsing.GIST),
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0004_exclusion_raw_sql", operations, expected)


def test_writer_handles_tuple_indexes_in_options(tmp_path: Path, monkeypatch) -> None:
    """Tuple-style indexes in options should be normalised to Index objects without crashing."""
    operations = [
        CreateModel(
            name="Token",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("user_id", fields.IntField()),
                ("revoked_at", fields.DatetimeField(null=True)),
            ],
            options={
                "indexes": [
                    ("user_id", "revoked_at"),
                ],
            },
        ),
    ]
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))
    writer = MigrationWriter(
        "0001_initial",
        "app",
        operations,
        migrations_module=module_path,
    )
    content = writer.as_string()
    assert "Index(fields=['user_id', 'revoked_at'])" in content
    assert "from hare.ddl.indexes import Index" in content


def test_writer_renders_fk_field(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Author",
            fields=[("id", fields.IntField(primary_key=True))],
        ),
        CreateModel(
            name="Post",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("author", fields.ForeignKeyField("app.Author", related_name="posts")),
            ],
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Author',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                    ],
                ),
                ops.CreateModel(
                    name='Post',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('author', fields.ForeignKeyField('app.Author', related_name='posts', db_index=True)),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0004_fk", operations, expected)


def test_writer_renders_composite_target_fk_to_field(tmp_path: Path, monkeypatch) -> None:
    """A composite `to_field=("id", "version")` (targeting a CompositePrimaryKey model) rounds
    through `deconstruct()` -> writer `repr()` as a valid Python tuple literal, and the written
    migration module re-imports back into a field whose `to_field` is that exact tuple - not
    just a string-match on the rendered text."""
    operations = [
        CreateModel(
            name="VersionedDoc",
            fields=[
                ("id", fields.UUIDField(primary_key=True)),
                ("version", fields.IntField()),
            ],
        ),
        CreateModel(
            name="Note",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                (
                    "document",
                    fields.ForeignKeyField("app.VersionedDoc", to_field=("id", "version"), related_name="notes"),
                ),
            ],
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from uuid import uuid4
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='VersionedDoc',
                    fields=[
                        ('id', fields.UUIDField(primary_key=True, default=uuid4)),
                        ('version', fields.IntField()),
                    ],
                ),
                ops.CreateModel(
                    name='Note',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('document', fields.ForeignKeyField('app.VersionedDoc', related_name='notes', db_index=True, to_field=('id', 'version'))),
                    ],
                ),
            ]
        """
    )
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))

    writer = MigrationWriter("0006_composite_fk", "app", operations, migrations_module=module_path)
    migration_path = writer.write()
    content = migration_path.read_text(encoding="utf-8")
    assert content == expected

    import importlib

    importlib.invalidate_caches()
    written_module = importlib.import_module(f"{module_path}.0006_composite_fk")
    note_fields = dict(written_module.Migration.operations[1].fields)
    assert note_fields["document"].to_field == ("id", "version")


def test_writer_excludes_fk_source_field(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Post",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("author", fields.ForeignKeyField("app.Author", related_name="posts")),
                ("author_id", fields.IntField(source_field="author_id")),
            ],
        )
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Post',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('author', fields.ForeignKeyField('app.Author', related_name='posts', db_index=True)),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0005_fk_source", operations, expected)


def test_writer_serializes_on_delete_enum(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Post",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                (
                    "author",
                    fields.ForeignKeyField(
                        "app.Author",
                        related_name="posts",
                        on_delete=fields.OnDelete.SET_NULL,
                        null=True,
                    ),
                ),
            ],
        )
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.fields.enums import OnDelete
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Post',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('author', fields.ForeignKeyField('app.Author', related_name='posts', on_delete=OnDelete.SET_NULL, null=True, db_index=True)),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0006_enum", operations, expected)


def test_writer_skips_missing_db_index(tmp_path: Path, monkeypatch) -> None:
    operations = [
        CreateModel(
            name="Message",
            fields=[("body", fields.TextField())],
        )
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Message',
                    fields=[
                        ('body', fields.TextField()),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0007_textfield", operations, expected)


def test_writer_rejects_lambda_default(tmp_path: Path, monkeypatch) -> None:
    operations = [
        AlterField(
            model_name="Widget",
            name="name",
            field=fields.CharField(max_length=120, default=lambda: "x"),
        )
    ]
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))
    writer = MigrationWriter(
        "0004_lambda",
        "app",
        operations,
        migrations_module=module_path,
    )
    with pytest.raises(ConfigurationError, match="Cannot serialize lambda"):
        writer.as_string()


def _default_value() -> str:
    return "ok"


def test_writer_allows_partial_default(tmp_path: Path, monkeypatch) -> None:
    operations = [
        AlterField(
            model_name="Widget",
            name="name",
            field=fields.CharField(max_length=120, default=functools.partial(_default_value)),
        )
    ]
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))
    writer = MigrationWriter(
        "0005_partial",
        "app",
        operations,
        migrations_module=module_path,
    )
    content = writer.as_string()
    assert "functools.partial" in content


def test_writer_rejects_partial_lambda(tmp_path: Path, monkeypatch) -> None:
    operations = [
        AlterField(
            model_name="Widget",
            name="name",
            field=fields.CharField(max_length=120, default=functools.partial(lambda: "x")),
        )
    ]
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))
    writer = MigrationWriter(
        "0006_partial_lambda",
        "app",
        operations,
        migrations_module=module_path,
    )
    with pytest.raises(ConfigurationError, match="lambda"):
        writer.as_string()


def test_writer_rejects_local_function_default(tmp_path: Path, monkeypatch) -> None:
    def _local_default() -> str:
        return "local"

    operations = [
        AlterField(
            model_name="Widget",
            name="name",
            field=fields.CharField(max_length=120, default=_local_default),
        )
    ]
    module_path = _prepare_migration_package(tmp_path, "app")
    monkeypatch.syspath_prepend(str(tmp_path))
    writer = MigrationWriter(
        "0007_local_default",
        "app",
        operations,
        migrations_module=module_path,
    )
    with pytest.raises(ConfigurationError, match="local function"):
        writer.as_string()


def test_writer_handles_one_to_one_field(tmp_path: Path, monkeypatch) -> None:
    """Test that OneToOneField is rendered without unique=True (it's implicit)."""
    operations = [
        CreateModel(
            name="Profile",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                (
                    "user",
                    fields.OneToOneField("app.User", related_name="profile", on_delete=fields.CASCADE),
                ),
            ],
        ),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Profile',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('user', fields.OneToOneField('app.User', related_name='profile')),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0009_one_to_one", operations, expected)


def test_writer_handles_enum_fields(tmp_path: Path, monkeypatch) -> None:
    """Test that IntEnumField and CharEnumField are rendered correctly (not as FieldInstance)."""
    operations = [
        CreateModel(
            name="Entity",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("status", fields.IntEnumField(Status, default=Status.ACTIVE)),  # type: ignore[list-item]
                ("role", fields.CharEnumField(Role)),  # type: ignore[list-item]
            ],
        ),
    ]
    # The migration should use fields.IntEnumField and fields.CharEnumField
    # NOT fields.IntEnumFieldInstance or fields.CharEnumFieldInstance
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from tests.migrations.test_writer import Role, Status
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='Entity',
                    fields=[
                        ('id', fields.IntField(primary_key=True)),
                        ('status', fields.IntEnumField(enum_type=Status, default=Status.ACTIVE, description='ACTIVE: 1\\nINACTIVE: 2')),
                        ('role', fields.CharEnumField(enum_type=Role, max_length=5, description='ADMIN: admin\\nUSER: user')),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0010_enum_fields", operations, expected)


def _runpython_forward(apps, schema_editor) -> None:
    _ = (apps, schema_editor)


def _runpython_reverse(apps, schema_editor) -> None:
    _ = (apps, schema_editor)


def test_writer_format_runpython(tmp_path: Path, monkeypatch) -> None:
    operations = [RunPython(_runpython_forward, reverse_code=_runpython_reverse, atomic=False)]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from tests.migrations.test_writer import _runpython_forward, _runpython_reverse

        class Migration(migrations.Migration):
            operations = [
                ops.RunPython(code=_runpython_forward, reverse_code=_runpython_reverse, atomic=False),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0008_runpython", operations, expected)


def test_writer_format_runsql(tmp_path: Path, monkeypatch) -> None:
    operations = [RunSQL(sql="SELECT 1", reverse_sql="SELECT 2", atomic=False)]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            operations = [
                ops.RunSQL(sql='SELECT 1', reverse_sql='SELECT 2', atomic=False),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0009_runsql", operations, expected)


def test_writer_format_backfill_column(tmp_path: Path, monkeypatch) -> None:
    operations = [BackfillColumn(model_name="Widget", field_name="status", value="active", batch_size=500)]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            operations = [
                ops.BackfillColumn(model_name='Widget', field_name='status', value='active', batch_size=500),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0009_backfill_column", operations, expected)


def test_writer_format_alter_column_not_null_safe(tmp_path: Path, monkeypatch) -> None:
    operations = [AlterColumnNotNullSafe(model_name="Widget", field_name="status")]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            operations = [
                ops.AlterColumnNotNullSafe(model_name='Widget', field_name='status'),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0009_alter_column_not_null_safe", operations, expected)


class _NoDeconstruct:
    """Object with no .deconstruct()/migration_import_path - baseline: serializes as repr()."""


class _Deconstructable:
    def __init__(self, tag: str) -> None:
        self.tag = tag

    def deconstruct(self):
        return "tests.migrations.test_writer._Deconstructable", [self.tag], {}


class _SingletonRef:
    """Mimics Crypto: not reconstructed through the constructor (that would put a secret in the
    file) - imported by a fixed path as an already-existing object instead."""

    def __init__(self) -> None:
        self.migration_import_path = "app.utils.crypto.some_singleton"


def test_render_value_timedelta_preserves_microsecond_precision() -> None:
    """render_value() used total_seconds() (a float) to serialize a timedelta - for a large
    enough value, converting through a float lost microsecond precision (total_seconds() needing
    more significant decimal digits than a double can represent once the day count competes with
    the microsecond digits for precision). days/seconds/microseconds are timedelta's own
    normalized, exact integer components."""
    import datetime

    imports = ImportManager()
    td = datetime.timedelta(days=100000, microseconds=1)
    rendered = MigrationWriter.render_value(td, imports)
    assert eval(rendered) == td  # noqa: S307


def test_render_value_falls_back_to_repr_without_protocol() -> None:
    imports = ImportManager()
    obj = _NoDeconstruct()
    assert MigrationWriter.render_value(obj, imports) == repr(obj)


def test_render_value_uses_deconstruct_protocol_for_arbitrary_objects() -> None:
    """Any object without explicit support (e.g. a Crypto instance passed as
    EncryptedJSONField(crypto=...)) used to serialize as an invalid repr()."""
    imports = ImportManager()
    rendered = MigrationWriter.render_value(_Deconstructable("x"), imports)
    assert rendered == "_Deconstructable('x')"
    assert "_Deconstructable" in imports.imports.get("tests.migrations.test_writer", set())


def test_render_value_prefers_migration_import_path_over_deconstruct() -> None:
    """migration_import_path - a reference to an existing singleton by name, WITHOUT calling the
    constructor again (critical for secrets like Crypto: reconstructing it would mean writing the
    encryption key literally into the migration file)."""
    imports = ImportManager()
    rendered = MigrationWriter.render_value(_SingletonRef(), imports)
    assert rendered == "some_singleton"
    assert "some_singleton" in imports.imports.get("app.utils.crypto", set())


def test_field_deconstruct_uses_migration_import_path_override() -> None:
    """A Field subclass can pin a permanent import path - moving the class to a different module
    afterward doesn't break already-generated migrations (__module__ at generation time is no
    longer the sole source of truth)."""

    class MovedField(fields.CharField):
        migration_import_path = "app.core.fields.MovedField"

    field = MovedField(max_length=10)
    path, _args, _kwargs = field.deconstruct()
    assert path == "app.core.fields.MovedField"


def test_writer_keeps_field_with_self_referencing_source_field(tmp_path: Path, monkeypatch) -> None:
    """_format_create_model used to compute "which duplicate fields to strike out" from the
    source_field of ANY field (not just relation fields) - an ordinary, explicit self-referencing
    id = Field(source_field='id') (with no relation nearby at all) silently struck ITSELF out of
    the generated migration entirely (see hare/hare-orm#2251 - abstract models lost ALL their
    fields from the first migration for this exact reason). Scoped the dedup to relation fields
    only (FK/O2O) - ordinary fields with a self-referencing source_field now correctly stay."""
    operations = [
        CreateModel(
            name="NewModel",
            fields=[
                ("id", fields.BigIntField(primary_key=True, generated=True, source_field="id")),
                ("created", fields.DatetimeField(auto_now_add=True, source_field="created")),
                ("name", fields.CharField(max_length=255)),
            ],
        )
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare import fields

        class Migration(migrations.Migration):
            operations = [
                ops.CreateModel(
                    name='NewModel',
                    fields=[
                        ('id', fields.BigIntField(source_field='id', primary_key=True)),
                        ('created', fields.DatetimeField(auto_now_add=True, source_field='created')),
                        ('name', fields.CharField(max_length=255)),
                    ],
                ),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0009_self_source_field", operations, expected)


def test_writer_format_index_and_constraint_removal_and_rename(tmp_path: Path, monkeypatch) -> None:
    operations = [
        DeleteModel(name="OldModel"),
        RemoveIndex(model_name="Widget", name="idx_widget_name"),
        RenameIndex(model_name="Widget", new_name="idx_widget_name_new", old_name="idx_widget_name"),
        RemoveConstraint(model_name="Widget", name="uq_widget_name"),
        RenameConstraint(model_name="Widget", old_name="uq_widget_name", new_name="uq_widget_name_new"),
        AlterModelOptions(name="Widget", options={"table": "widgets_v2"}),
        SQLOperation("CREATE INDEX idx_manual ON widget (name)", values=[]),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            operations = [
                ops.DeleteModel(name='OldModel'),
                ops.RemoveIndex(model_name='Widget', name='idx_widget_name'),
                ops.RenameIndex(model_name='Widget', new_name='idx_widget_name_new', old_name='idx_widget_name'),
                ops.RemoveConstraint(model_name='Widget', name='uq_widget_name'),
                ops.RenameConstraint(model_name='Widget', old_name='uq_widget_name', new_name='uq_widget_name_new'),
                ops.AlterModelOptions(name='Widget', options={'table': 'widgets_v2'}),
                ops.SQLOperation(query='CREATE INDEX idx_manual ON widget (name)', values=[]),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0010_index_constraint_ops", operations, expected)


def test_writer_format_trigger_ops(tmp_path: Path, monkeypatch) -> None:
    operations = [
        AddTrigger(
            model_name="Widget",
            trigger=Trigger(name="widget_bump", on=TriggerEvent.INSERT, body=RawSQLTerm("RETURN NEW;")),
        ),
        AlterTrigger(
            model_name="Widget",
            trigger=Trigger(name="widget_bump", on="INSERT OR UPDATE", body=RawSQLTerm("RETURN NEW;")),
        ),
        RemoveTrigger(model_name="Widget", name="widget_bump"),
    ]
    expected = textwrap.dedent(
        """\
        from hare import migrations
        from hare.migrations import operations as ops
        from hare.ddl import RawSQLTerm
        from hare.ddl.enums import TriggerEvent
        from hare.ddl.schema_objects.trigger import Trigger

        class Migration(migrations.Migration):
            operations = [
                ops.AddTrigger(
                    model_name='Widget',
                    trigger=Trigger(name='widget_bump', on=TriggerEvent.INSERT, body=RawSQLTerm('RETURN NEW;')),
                ),
                ops.AlterTrigger(
                    model_name='Widget',
                    trigger=Trigger(name='widget_bump', on='INSERT OR UPDATE', body=RawSQLTerm('RETURN NEW;')),
                ),
                ops.RemoveTrigger(model_name='Widget', name='widget_bump'),
            ]
        """
    )
    _write_migration(tmp_path, monkeypatch, "0011_trigger_ops", operations, expected)
