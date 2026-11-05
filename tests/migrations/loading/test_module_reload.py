import importlib
import sys

import pytest

from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.loading.migration_loader import MigrationLoader
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from tests.utils.project_imports import ProjectImports
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.database_independent


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
