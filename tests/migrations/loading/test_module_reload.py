import importlib
import os
import py_compile
import sys

import pytest

from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from tests.utils.project_imports import ProjectImports
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.database_independent


def test_cold_load_uses_current_source_despite_valid_timestamp_bytecode(tmp_path):
    sources = ProjectSources(tmp_path)
    package = sources.write_package("library")
    old = (
        "from hare.migrations import Migration as Base\n"
        "from hare.migrations.operations import AlterField\n"
        "from hare import fields\n"
        "class Migration(Base):\n"
        "    operations = [AlterField('Book', 'title', fields.CharField(max_length=20))]\n"
    )
    path = sources.write_migration(package, "0001_initial", old)
    timestamp = path.stat()
    py_compile.compile(str(path), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    current = old.replace("max_length=20", "max_length=30")
    assert len(old) == len(current)
    path.write_text(current, encoding="utf-8")
    os.utime(path, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
    with ProjectImports(tmp_path).activate():
        loader = MigrationLoader({"library": {"migrations": "library.migrations"}}, MigrationRecorder(None))
        for _attempt in range(2):
            loader.load_disk()
            assert loader.disk_migrations[MigrationKey("library", "0001_initial")].operations[0].field.max_length == 30


def test_failed_cold_module_execution_leaves_no_partial_import(tmp_path):
    sources = ProjectSources(tmp_path)
    package = sources.write_package("library")
    sources.write_migration(package, "0001_initial", "marker = 'partial'\nraise RuntimeError('broken')\n")
    name = "library.migrations.0001_initial"
    with ProjectImports(tmp_path).activate():
        with pytest.raises(RuntimeError, match="broken"):
            MigrationLoader._import_migration_module(name)
        assert name not in sys.modules
        assert not hasattr(sys.modules["library.migrations"], "0001_initial")


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("marker = 'class removed'\n", "has no Migration class"),
        (
            "from hare.migrations import Migration as Base\nclass Migration(Base):\n    marker = removed_helper()\n",
            "NameError",
        ),
    ],
)
def test_removed_migration_definitions_do_not_survive_source_reload(tmp_path, source, error):
    sources = ProjectSources(tmp_path)
    package = sources.write_package("library")
    path = sources.write_migration(
        package,
        "0001_initial",
        "from hare.migrations import Migration as Base\ndef removed_helper(): return 'old'\n"
        "class Migration(Base):\n    marker = removed_helper()\n",
    )
    with ProjectImports(tmp_path).activate():
        loader = MigrationLoader({"library": {"migrations": "library.migrations"}}, MigrationRecorder(None))
        loader.load_disk()
        path.write_text(source, encoding="utf-8")
        with pytest.raises(MigrationLoadError, match=error):
            loader.load_disk()


def test_failed_module_execution_keeps_previous_import_and_successful_reload_publishes_fresh_namespace(tmp_path):
    sources = ProjectSources(tmp_path)
    package = sources.write_package("library")
    path = sources.write_migration(package, "0001_initial", "marker = 'old'\nremoved = 1\n")
    name = "library.migrations.0001_initial"
    with ProjectImports(tmp_path).activate():
        original = MigrationLoader._import_migration_module(name)
        parent = importlib.import_module("library.migrations")
        path.write_text("marker = 'partial'\nraise RuntimeError('broken')\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="broken"):
            MigrationLoader._import_migration_module(name)
        assert sys.modules[name] is original
        assert getattr(parent, "0001_initial") is original
        assert original.marker == "old"
        path.write_text(
            "from __future__ import annotations\nfrom dataclasses import dataclass, KW_ONLY\n"
            "@dataclass\nclass Marker:\n    _: KW_ONLY\n    value: int\nmarker = Marker(value=2)\n",
            encoding="utf-8",
        )
        replacement = MigrationLoader._import_migration_module(name)
        assert replacement is not original
        assert not hasattr(replacement, "removed")
        assert replacement.Marker.__dataclass_fields__["value"].kw_only
        assert replacement.marker.value == 2
        assert sys.modules[name] is getattr(parent, "0001_initial") is replacement
        replacement.loaded_state = "kept"
        assert MigrationLoader._import_migration_module(name) is replacement
        assert replacement.loaded_state == "kept"
