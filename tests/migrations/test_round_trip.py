"""End-to-end migration round trips driven through the real CLI: makemigrations -> migrate -> a
second makemigrations --check / drift must both report nothing left to do. Runs against sqlite
by default; set HARE_TEST_DB to a postgres URL to run the same scenarios on that backend."""

from __future__ import annotations

import contextlib
import datetime
import importlib
import importlib.util
import io
import math
import os
import sys
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

from hare.cli import hare_cli as cli_module
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from tests.utils.database_under_test import DatabaseUnderTest

APP_LABEL = "rtapp"
MODELS_PACKAGE = "rt_models_pkg"
SETTINGS_MODULE = "rt_settings_module"


class RoundTripProject:
    """A throwaway project (models package + migrations package + settings) on one database."""

    def __init__(self, project_directory: Path, monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
        self.project_directory = project_directory
        self.package_directory = project_directory / MODELS_PACKAGE
        self.package_directory.mkdir()
        (self.package_directory / "__init__.py").write_text("", encoding="utf-8")
        (self.package_directory / "models.py").write_text("", encoding="utf-8")
        self.migrations_directory = self.package_directory / "migrations"
        self.migrations_directory.mkdir()
        (self.migrations_directory / "__init__.py").write_text("", encoding="utf-8")
        (project_directory / f"{SETTINGS_MODULE}.py").write_text(
            "HARE_ORM = {\n"
            f'    "connections": {{"default": {database_url!r}}},\n'
            '    "apps": {\n'
            f'        "{APP_LABEL}": {{\n'
            f'            "models": ["{MODELS_PACKAGE}.models"],\n'
            '            "default_connection": "default",\n'
            f'            "migrations": "{MODELS_PACKAGE}.migrations",\n'
            "        },\n"
            "    },\n"
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.syspath_prepend(str(project_directory))
        importlib.invalidate_caches()
        self.purge_project_modules()

    @staticmethod
    def purge_project_modules() -> None:
        for module_name in list(sys.modules):
            if module_name.startswith((MODELS_PACKAGE, SETTINGS_MODULE)):
                del sys.modules[module_name]

    def write_models(self, source: str) -> None:
        (self.package_directory / "models.py").write_text(source, encoding="utf-8")
        self.purge_project_modules()
        importlib.invalidate_caches()

    async def run_cli(self, *arguments: str) -> SimpleNamespace:
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = await cli_module.HareCLI.run_cli_async(["-c", f"{SETTINGS_MODULE}.HARE_ORM", *arguments])
        finally:
            self.purge_project_modules()
            importlib.invalidate_caches()
        return SimpleNamespace(exit_code=exit_code, output=stdout.getvalue() + stderr.getvalue())

    async def make_and_migrate(self) -> SimpleNamespace:
        make_result = await self.run_cli("makemigrations", APP_LABEL)
        assert make_result.exit_code == 0, make_result.output
        migrate_result = await self.run_cli("migrate")
        assert migrate_result.exit_code == 0, migrate_result.output
        return make_result

    async def assert_nothing_left_to_do(self) -> None:
        check_result = await self.run_cli("makemigrations", "--check", "--dry-run")
        assert check_result.exit_code == 0, check_result.output
        drift_result = await self.run_cli("drift")
        assert drift_result.exit_code == 0, drift_result.output

    def migration_file_names(self) -> list[str]:
        return sorted(path.name for path in self.migrations_directory.glob("0*.py"))

    def migration_source(self, migration_file_name: str) -> str:
        return (self.migrations_directory / migration_file_name).read_text(encoding="utf-8")

    def load_migration_operations(self, migration_file_name: str) -> list:
        """Executes the written migration file and returns its ``operations`` list."""
        module_name = f"{MODELS_PACKAGE}.migrations.{migration_file_name.removesuffix('.py')}"
        specification = importlib.util.spec_from_file_location(
            module_name, self.migrations_directory / migration_file_name
        )
        assert specification is not None and specification.loader is not None
        migration_module = importlib.util.module_from_spec(specification)
        try:
            specification.loader.exec_module(migration_module)
        finally:
            self.purge_project_modules()
        return migration_module.Migration.operations


@pytest_asyncio.fixture
async def database_url(tmp_path: Path) -> AsyncGenerator[str]:
    raw_database_url = os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")
    if DatabaseUnderTest.is_file_database(raw_database_url):
        scheme = raw_database_url.split("://", 1)[0]
        yield f"{scheme}:///{(tmp_path / 'round_trip.sqlite3').as_posix()}"
        return
    import asyncpg

    raw_database_url = raw_database_url.replace("\\{", "{").replace("\\}", "}")
    resolved_database_url = raw_database_url.format(f"rt_{uuid.uuid4().hex}")
    credentials = DbUrlConfigGenerator.expand(resolved_database_url, testing=False)["credentials"]
    # The template may carry a per-run prefix (test_<run tag>_{}) - create exactly the database the URL names.
    database_name = credentials["database"]
    administrative_connection = await asyncpg.connect(
        host=credentials["host"],
        port=credentials["port"],
        user=credentials["user"],
        password=credentials["password"],
        database="postgres",
    )
    try:
        await administrative_connection.execute(f'CREATE DATABASE "{database_name}"')
        yield resolved_database_url
    finally:
        await administrative_connection.execute(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        await administrative_connection.close()


@pytest.fixture
def round_trip_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, database_url: str) -> RoundTripProject:
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    return RoundTripProject(project_directory, monkeypatch, database_url)


MANY_TO_MANY_MODELS = f"""
from hare import fields
from hare.models import Model


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)


class Book(Model):
    id = fields.IntField(primary_key=True)
    tags = fields.ManyToManyField("{APP_LABEL}.Tag", related_name="books")
"""


@pytest.mark.asyncio
async def test_many_to_many_without_explicit_keys_reaches_no_changes(round_trip_project: RoundTripProject) -> None:
    round_trip_project.write_models(MANY_TO_MANY_MODELS)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()
    assert len(round_trip_project.migration_file_names()) == 1


@pytest.mark.asyncio
async def test_many_to_many_with_custom_through_table_name_reaches_no_changes(
    round_trip_project: RoundTripProject,
) -> None:
    round_trip_project.write_models(
        MANY_TO_MANY_MODELS.replace('related_name="books"', 'related_name="books", through="custom_tbl"')
    )
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()
    assert len(round_trip_project.migration_file_names()) == 1


@pytest.mark.asyncio
async def test_many_to_many_with_explicit_keys_reaches_no_changes(round_trip_project: RoundTripProject) -> None:
    round_trip_project.write_models(
        MANY_TO_MANY_MODELS.replace(
            'related_name="books"', 'related_name="books", forward_key="tag_ref", backward_key="book_ref"'
        )
    )
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_arguments",
    [
        'related_name="books", forward_key="tag_ref"',
        'related_name="books", backward_key="book_ref"',
        'related_name="books", through="renamed_through_table"',
    ],
)
async def test_many_to_many_real_key_or_through_change_is_still_detected(
    round_trip_project: RoundTripProject, changed_arguments: str
) -> None:
    round_trip_project.write_models(MANY_TO_MANY_MODELS)
    await round_trip_project.make_and_migrate()

    round_trip_project.write_models(MANY_TO_MANY_MODELS.replace('related_name="books"', changed_arguments))
    check_result = await round_trip_project.run_cli("makemigrations", "--check", "--dry-run")

    assert check_result.exit_code == 1, check_result.output
    assert "Alter field tags on Book" in check_result.output


DEFAULT_SERIALIZATION_MODELS = """
import datetime
import enum

from hare import fields
from hare.models import Model


class Permission(enum.IntFlag):
    READ = 1
    WRITE = 2
    EXECUTE = 4
    READ_WRITE = 3


class Thing(Model):
    id = fields.IntField(primary_key=True)
    value = {field_source}
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_source", "expected_default"),
    [
        ("fields.DateField(default=datetime.date.today)", datetime.date.today),
        ("fields.DatetimeField(default=datetime.datetime.now)", datetime.datetime.now),
        ("fields.DatetimeField(default=datetime.datetime.utcnow)", datetime.datetime.utcnow),
        ("fields.FloatField(default=float('inf'))", math.inf),
        ("fields.FloatField(default=float('-inf'))", -math.inf),
        ("fields.FloatField(default=float('nan'))", math.nan),
        ("fields.IntField(default=Permission.READ | Permission.WRITE | Permission.EXECUTE)", 7),
        ("fields.IntField(default=Permission.READ | Permission.EXECUTE)", 5),
        ("fields.IntField(default=Permission.READ)", 1),
        ("fields.IntField(default=Permission.READ_WRITE)", 3),
        ("fields.IntField(default=Permission(0))", 0),
    ],
)
async def test_unusual_field_default_round_trips_through_a_written_migration(
    round_trip_project: RoundTripProject, field_source: str, expected_default: object
) -> None:
    round_trip_project.write_models(DEFAULT_SERIALIZATION_MODELS.replace("{field_source}", field_source))
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()
    (migration_file_name,) = round_trip_project.migration_file_names()
    create_model_operation = round_trip_project.load_migration_operations(migration_file_name)[0]
    written_default = dict(create_model_operation.fields)["value"].default
    if isinstance(expected_default, float) and math.isnan(expected_default):
        assert math.isnan(written_default)
    else:
        assert written_default == expected_default


EXPRESSION_INDEX_MODELS = f"""
from hare import fields
from hare.ddl.indexes import Index
from hare.models import Model
from hare.query.functions import Lower


class Widget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        app = "{APP_LABEL}"
        indexes = [Index(Lower("name"), name="idx_widget_lower_name")]
"""


@pytest.mark.asyncio
async def test_expression_index_reaches_no_changes_after_migrate(round_trip_project: RoundTripProject) -> None:
    """An Index(Lower("name"), name=...) used to be re-detected as a change (Remove index +
    Add index) on every makemigrations run after the first migrate - the index replayed from
    the written migration file carries a RawSQLTerm(sql_text) expression, while the live model's
    own Index still carries an unresolved Lower("name") Expression object at diff time; comparing
    those by repr() never matched, even though both describe the identical index."""
    round_trip_project.write_models(EXPRESSION_INDEX_MODELS)
    await round_trip_project.make_and_migrate()

    check_result = await round_trip_project.run_cli("makemigrations", "--check", "--dry-run")
    assert check_result.exit_code == 0, check_result.output
    assert len(round_trip_project.migration_file_names()) == 1


OWNER_MODEL = f"""
from hare import fields
from hare.models import Model


class Owner(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "{APP_LABEL}"
        table = "owner"
"""

PERSON_MODEL_RENAMED_FROM_OWNER = f"""
from hare import fields
from hare.models import Model


class Person(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        app = "{APP_LABEL}"
        table = "owner"
"""


@pytest.mark.asyncio
async def test_rename_model_keeps_explicit_table_matching_old_class_name_lowered(
    round_trip_project: RoundTripProject,
) -> None:
    """RenameModel.state_forward guesses whether Meta.table was auto-derived by comparing it
    against old_name.lower() - Owner with an EXPLICIT Meta.table = "owner" (same value auto-
    derivation would have produced anyway) looks identical to that heuristic, so renaming the
    class to Person wrongly also renamed the table to "person" in the tracked state, even though
    Meta.table itself never changed - the next makemigrations then proposed renaming the table
    right back to "owner"."""
    round_trip_project.write_models(OWNER_MODEL)
    await round_trip_project.make_and_migrate()

    round_trip_project.write_models(PERSON_MODEL_RENAMED_FROM_OWNER)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()


RELATION_SOURCE_FIELD_MODELS = f"""
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("{APP_LABEL}.Author", related_name="books", source_field="auth")
    editor = fields.OneToOneField("{APP_LABEL}.Author", related_name="edited", source_field="ed_ref", null=True)
"""


@pytest.mark.asyncio
async def test_relation_source_field_is_written_as_the_real_column(round_trip_project: RoundTripProject) -> None:
    """Relation initialization repoints a ForeignKeyField/OneToOneField's source_field at its
    shadow attribute ("author_id"), and that value used to be written into the migration - migrate
    then created "author_id" instead of the declared "auth" column, while --check stayed silent."""
    round_trip_project.write_models(RELATION_SOURCE_FIELD_MODELS)
    await round_trip_project.make_and_migrate()

    (migration_file_name,) = round_trip_project.migration_file_names()
    migration_source = round_trip_project.migration_source(migration_file_name)
    assert 'source_field="auth"' in migration_source
    assert 'source_field="ed_ref"' in migration_source
    await round_trip_project.assert_nothing_left_to_do()

    round_trip_project.write_models(
        RELATION_SOURCE_FIELD_MODELS.replace('source_field="auth"', 'source_field="auth2"')
    )
    check_result = await round_trip_project.run_cli("makemigrations", "--check", "--dry-run")
    assert check_result.exit_code == 1, check_result.output


GENERATED_FALSE_PRIMARY_KEY_MODELS = """
from hare import fields
from hare.models import Model


class ExternalRow(Model):
    number = fields.IntField(primary_key=True, generated=False)
    name = fields.CharField(max_length=10)
"""


@pytest.mark.asyncio
async def test_integer_primary_key_with_generated_false_round_trips(round_trip_project: RoundTripProject) -> None:
    """An integer primary key defaults to generated=True, and the explicit generated=False used to
    be dropped from the migration - migrate created an auto-increment column and --check reported
    "Alter field" forever."""
    round_trip_project.write_models(GENERATED_FALSE_PRIMARY_KEY_MODELS)
    await round_trip_project.make_and_migrate()

    (migration_file_name,) = round_trip_project.migration_file_names()
    assert "generated=False" in round_trip_project.migration_source(migration_file_name)
    await round_trip_project.assert_nothing_left_to_do()


SINGLE_FIELD_META_INDEX_MODELS = """
from hare import fields
from hare.ddl.indexes import Index
from hare.models import Model


class IndexedByObject(Model):
    id = fields.IntField(primary_key=True)
    note = fields.CharField(max_length=20)

    class Meta:
        indexes = (Index(fields=("note",)),)


class IndexedByTuple(Model):
    id = fields.IntField(primary_key=True)
    note = fields.CharField(max_length=20)

    class Meta:
        indexes = (("note",),)


class IndexedByName(Model):
    id = fields.IntField(primary_key=True)
    note = fields.CharField(max_length=20, source_field="note_column")

    class Meta:
        indexes = (Index(fields=("note",), name="idx_named_note"),)
"""


@pytest.mark.asyncio
async def test_single_field_meta_index_reports_no_drift(round_trip_project: RoundTripProject) -> None:
    """The introspector folds a plain single-column index into the column's has_index flag, which
    drift copied onto the field's own index flag - a Meta.indexes entry then showed up as a
    permanent "Alter field"."""
    round_trip_project.write_models(SINGLE_FIELD_META_INDEX_MODELS)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()


PARTIAL_INDEX_CONDITION_MODELS = """
from hare import fields
from hare.ddl import RawSQLTerm
from hare.ddl.indexes import PartialIndex
from hare.models import Model


class Coupon(Model):
    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=10, source_field="code_col")
    copies = fields.IntField(null=True)

    class Meta:
        indexes = (
            PartialIndex(fields=("code",), condition=RawSQLTerm("code_col = 'x'")),
            PartialIndex(fields=("code",), condition=RawSQLTerm("copies = 1"), name="coupon_code_single_copy"),
            PartialIndex(fields=("copies",), condition=RawSQLTerm("copies = 2")),
        )
"""


@pytest.mark.asyncio
async def test_partial_index_conditions_report_no_drift(round_trip_project: RoundTripProject) -> None:
    """A raw SQL condition the database writes back its own way (quoted columns, casts) - over a
    source_field column, or made of equalities - is the declared one: no Remove/Add index."""
    round_trip_project.write_models(PARTIAL_INDEX_CONDITION_MODELS)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()


ONE_TO_ONE_COMPOSITE_TARGET_MODELS = f"""
from hare import fields
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.models import Model


class Slot(Model):
    day = fields.IntField()
    hour = fields.IntField()
    pk = CompositePrimaryKey("day", "hour")


class Booking(Model):
    id = fields.IntField(primary_key=True)
    slot = fields.OneToOneField("{APP_LABEL}.Slot", related_name="booking", null=True)
"""


@pytest.mark.asyncio
async def test_one_to_one_to_a_composite_primary_key_reports_no_drift(round_trip_project: RoundTripProject) -> None:
    """The UniqueConstraint relation initialization adds for a OneToOneField to a composite
    primary key was read back from the database as a unique_together entry - a permanent
    Remove/Add constraint pair."""
    round_trip_project.write_models(ONE_TO_ONE_COMPOSITE_TARGET_MODELS)
    await round_trip_project.make_and_migrate()

    await round_trip_project.assert_nothing_left_to_do()
