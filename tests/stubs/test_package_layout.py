import ast

import pytest

from hare.core.config import HareConfig
from hare.stubs.stub_writer import StubWriter
from hare.typing_info.bound_models import BoundModels
from tests.utils.project_imports import ProjectImports
from tests.utils.project_sources import ProjectSources

pytestmark = pytest.mark.database_independent


def test_package_models_and_child_modules_keep_their_stub_paths_and_relative_imports(tmp_path):
    sources = ProjectSources(tmp_path)
    sources.write_module("library/labels", "from enum import StrEnum\nclass Label(StrEnum):\n    BOOK = 'book'\n")
    sources.write_module(
        "library/__init__",
        "from hare import Model, fields\nfrom .labels import Label as Label\n"
        "class Book(Model):\n    title = fields.TextField()\n",
    )
    sources.write_module(
        "library/chapters",
        "from hare import Model, fields\nfrom . import Book as Book\n"
        "class Chapter(Model):\n    title = fields.TextField()\n",
    )
    with ProjectImports(tmp_path).activate():
        config = HareConfig.load(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"models": {"models": ["library", "library.chapters"]}},
            }
        )
        output = tmp_path / "typings"
        writer = StubWriter(BoundModels(config), output, 0)
        texts = writer.get_stub_texts()
    assert set(texts) == {output / "library/__init__.pyi", output / "library/chapters.pyi"}
    package = ast.parse(texts[output / "library/__init__.pyi"])
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "labels"
        and any(alias.name == "Label" for alias in node.names)
        for node in package.body
    )
    assert any(isinstance(node, ast.ClassDef) and node.name == "Book" for node in package.body)
    child = ast.parse(texts[output / "library/chapters.pyi"])
    assert any(isinstance(node, ast.ClassDef) and node.name == "Chapter" for node in child.body)
