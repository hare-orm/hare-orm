import ast

import pytest

from hare.stubs.class_stub_editor import ClassStubEditor


@pytest.mark.parametrize(
    "source",
    [
        "class Book: ...\n",
        "class Book(Base): objects: Any; other: int\n",
        "@decorate\nclass Book(\n    Base,\n):\n    objects: (\n        Any\n    )  # manager\n",
    ],
)
def test_class_members_are_replaced_structurally(source):
    text = ClassStubEditor.update(source, {"Book": {"objects": "objects: ClassVar[BookQuerySet]"}})
    tree = ast.parse(text)
    model = tree.body[0]
    assert isinstance(model, ast.ClassDef)
    names = [node.target.id for node in model.body if isinstance(node, ast.AnnAssign)]
    assert names.count("objects") == 1
    assert "objects: ClassVar[BookQuerySet]" in text
    if "# manager" in source:
        assert "# manager" in text
        assert text.count("@decorate") == 1
    if "other:" in source:
        assert "other: int" in text


def test_class_edits_preserve_unrelated_scope_and_chained_targets():
    source = '''# module comment
class Book:
    """A book."""
    objects = alias = ...
    class Meta:
        objects: str
    def method(self) -> str: ...

class Other: ...
'''
    text = ClassStubEditor.update(
        source,
        {"Book": {"objects": "objects: BookQuerySet", "pk": "@property\ndef pk(self) -> int: ..."}},
    )
    ast.parse(text)
    assert text.startswith("# module comment\n")
    assert '"""A book."""' in text
    assert "alias = ..." in text
    assert "    class Meta:\n        objects: str" in text
    assert "    def method(self) -> str: ..." in text
    assert text.endswith("class Other: ...\n")
    assert text.count("@property") == 1


def test_class_edit_is_deterministic():
    declarations = {"Book": {"objects": "objects: BookQuerySet"}}
    text = ClassStubEditor.update("class Book: ...\n", declarations)
    assert ClassStubEditor.update(text, declarations) == text
