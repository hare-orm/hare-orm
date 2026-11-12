from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.matrix import test_selection_manifest as manifest_module
from tests.matrix.test_module_requirements import TestModuleRequirements as ModuleRequirements
from tests.matrix.test_selection_manifest import TestSelectionManifest as SelectionManifest

pytestmark = pytest.mark.database_independent


@pytest.fixture
def manifest_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(manifest_module, "REPOSITORY_DIRECTORY", tmp_path)
    monkeypatch.setattr(manifest_module, "TESTS_DIRECTORY", tmp_path / "tests")
    (tmp_path / "tests").mkdir()
    return tmp_path


def test_requirements_are_read_without_importing_test_or_fixture_sources(manifest_root: Path):
    directory = manifest_root / "tests/optional"
    directory.mkdir()
    source = "raise RuntimeError('this source must never execute during selection')\n"
    (directory / "conftest.py").write_text(source, encoding="utf-8")
    module_path = directory / "test_example.py"
    module_path.write_text(source, encoding="utf-8")
    manifest_path = manifest_root / "selection.toml"
    manifest_path.write_text(
        '[scopes]\n"tests/optional" = ["framework"]\n'
        '[modules]\n"tests/optional/test_example.py" = { requires = ["adapter"], cells = ["serializer"] }\n',
        encoding="utf-8",
    )
    imported = set(sys.modules)
    manifest = SelectionManifest.read(manifest_path)
    manifest.check_inventory()
    assert manifest.get_path_requirements(directory / "conftest.py") == {"framework"}
    assert manifest.get_path_requirements(module_path) == {"framework", "adapter"}
    manifest.check_cell_requirements(module_path, {"framework", "adapter", "serializer"})
    assert not set(sys.modules) - imported


def test_scopes_use_directory_boundaries_and_accumulate(manifest_root: Path):
    manifest = SelectionManifest({}, {"tests/extra": frozenset({"a"}), "tests/extra/nested": frozenset({"b"})})
    assert manifest.get_path_requirements(manifest_root / "tests/extra") == {"a"}
    assert manifest.get_path_requirements(manifest_root / "tests/extra/nested/conftest.py") == {"a", "b"}
    assert not manifest.get_path_requirements(manifest_root / "tests/extra_other/conftest.py")
    assert not manifest.get_path_requirements(manifest_root / "outside/test_unknown.py")


def test_undeclared_module_or_cell_cannot_disappear_from_selection(manifest_root: Path):
    manifest = SelectionManifest(
        {"tests/test_example.py": ModuleRequirements(frozenset({"adapter"}), frozenset({"serializer"}))}, {}
    )
    with pytest.raises(ValueError, match="declare the test module"):
        manifest.get_path_requirements(manifest_root / "tests/test_new.py")
    with pytest.raises(ValueError, match="undeclared cell requirements.*new_extra"):
        manifest.check_cell_requirements(manifest_root / "tests/test_example.py", {"adapter", "new_extra"})


def test_inventory_reports_added_and_removed_modules(manifest_root: Path):
    (manifest_root / "tests/test_added.py").touch()
    manifest = SelectionManifest({"tests/test_removed.py": ModuleRequirements(frozenset(), frozenset())}, {})
    with pytest.raises(ValueError) as error:
        manifest.check_inventory()
    assert "undeclared modules=['tests/test_added.py']" in str(error.value)
    assert "missing files=['tests/test_removed.py']" in str(error.value)
