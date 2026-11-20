import ast

import pytest

from hare.core.config import HareConfig
from hare.core.hare import Hare
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
        writer = StubWriter(BoundModels(Hare.bind_models(config)), output, 0)
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


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
def test_nested_model_declarations_keep_same_named_helpers_in_their_original_scopes(tmp_path, composite):
    source = (
        "from hare import Model, fields\n"
        "class Entry:\n    objects: int = 7\n    code: str = 'helper'\n"
        "class OtherScope:\n    class Entry:\n        code: str = 'nested helper'\n"
        "class Scope:\n    class Entry(Model):\n"
        f"        code = fields.IntField(primary_key={not composite!r}, generated=False)\n"
        "        title = fields.TextField()\n"
    )
    if composite:
        source += '        region = fields.IntField()\n        pk = fields.CompositePrimaryKey("region", "code")\n'
    source += "__models__ = [Scope.Entry]\n"
    ProjectSources(tmp_path).write_module("nested_models", source)
    with ProjectImports(tmp_path).activate():
        config = HareConfig.load(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"models": {"models": ["nested_models"]}},
            }
        )
        text = StubWriter(BoundModels(Hare.bind_models(config)), tmp_path / "typings", 0).get_stub_texts()[
            tmp_path / "typings/nested_models.pyi"
        ]
    module = ast.parse(text)
    classes = {node.name: node for node in module.body if isinstance(node, ast.ClassDef)}
    helper = {
        node.target.id: ast.unparse(node.annotation)
        for node in classes["Entry"].body
        if isinstance(node, ast.AnnAssign)
    }
    assert helper == {"objects": "int", "code": "str"}
    other = next(node for node in classes["OtherScope"].body if isinstance(node, ast.ClassDef))
    assert ast.unparse(next(node for node in other.body if isinstance(node, ast.AnnAssign)).annotation) == "str"
    nested = next(node for node in classes["Scope"].body if isinstance(node, ast.ClassDef))
    members = {node.target.id: ast.unparse(node.annotation) for node in nested.body if isinstance(node, ast.AnnAssign)}
    assert members["code"] == "Field[int]"
    assert members["title"] == "Field[str]"
    assert members["objects"].startswith("ClassVar[")
