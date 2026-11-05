import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tests.utils.project_imports import ProjectImports

pytestmark = pytest.mark.database_independent


def test_project_imports_restore_conflicting_modules_paths_and_directory(tmp_path, monkeypatch):
    previous = ModuleType("project_models")
    unrelated = ModuleType("project_models_extra")
    monkeypatch.setitem(sys.modules, "project_models", previous)
    monkeypatch.setitem(sys.modules, "project_models_extra", unrelated)
    source = tmp_path / "project_models.py"
    source.write_text("value = 1\n", encoding="utf-8")
    imports = ProjectImports(tmp_path)
    original_path, original_directory = sys.path[:], Path.cwd()

    with pytest.raises(RuntimeError, match="command failed"), imports.activate():
        assert importlib.import_module("project_models").value == 1
        assert sys.modules["project_models_extra"] is unrelated
        raise RuntimeError("command failed")

    assert sys.modules["project_models"] is previous
    assert sys.modules["project_models_extra"] is unrelated
    assert sys.path == original_path and Path.cwd() == original_directory
    source.write_text("value = 2\n", encoding="utf-8")
    with imports.activate():
        assert importlib.import_module("project_models").value == 2
    assert sys.modules["project_models"] is previous


def test_module_scope_cleans_sources_created_inside_it(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    with ProjectImports(tmp_path).isolate_modules():
        (tmp_path / "created_model.py").write_text("value = 1\n", encoding="utf-8")
        importlib.invalidate_caches()
        assert importlib.import_module("created_model").value == 1
    assert "created_model" not in sys.modules


def test_module_scope_restores_names_after_the_project_directory_is_removed(tmp_path, monkeypatch):
    previous = ModuleType("temporary_models")
    monkeypatch.setitem(sys.modules, "temporary_models", previous)
    monkeypatch.syspath_prepend(str(tmp_path))
    source = tmp_path / "temporary_models.py"
    source.write_text("value = 1\n", encoding="utf-8")
    with ProjectImports(tmp_path).isolate_modules():
        assert importlib.import_module("temporary_models").value == 1
        source.unlink()
        tmp_path.rmdir()
    assert sys.modules["temporary_models"] is previous
