from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hare.stubs.constants import STUB_HEADER
from hare.stubs.model_stub import ModelStub
from hare.stubs.stub_imports import StubImports

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model


class ModuleStub:
    """The stub of one module of models: the whole module as mypy's stubgen writes it - pyright reads
    the stub instead of the module, so every other name stays - with ``objects`` of each model typed
    and the model's declarations (``ModelStub``) added.

    Args:
        module_name: The module.
        models: Its models, each with the dialect of its connection.
        relation_depth: How many relations a filter key crosses at most.
    """

    def __init__(self, module_name: str, models: list[tuple[type[Model], Dialect]], relation_depth: int) -> None:
        self.module_name = module_name
        self.models = models
        self.relation_depth = relation_depth

    def get_base_text(self) -> str:
        """The stub mypy's stubgen writes for the module."""
        # Local import: mypy is the extra hare-orm[pyright] installs, read only here.
        from mypy.stubgen import generate_stubs, parse_options

        with tempfile.TemporaryDirectory() as output_directory:
            generate_stubs(parse_options(["--quiet", "-m", self.module_name, "-o", output_directory]))
            module_path = Path(output_directory, *self.module_name.split("."))
            stub_path = module_path / "__init__.pyi" if module_path.is_dir() else module_path.with_suffix(".pyi")
            return stub_path.read_text(encoding="utf-8")

    def get_text(self) -> str:
        """The stub's text."""
        imports = StubImports(self.module_name)
        text = self.get_base_text()
        declarations: list[str] = []
        for model, dialect in self.models:
            model_stub = ModelStub(model, dialect, imports, self.relation_depth)
            text = ModuleStub.add_class_line(text, model.__name__, model_stub.get_objects_declaration())
            text = ModuleStub.set_class_attributes(text, model.__name__, model_stub.get_attribute_annotations())
            declarations.extend(["", *model_stub.get_declarations()])
        return "\n".join([STUB_HEADER, *imports.render(), text.rstrip("\n"), *declarations, ""])

    @staticmethod
    def set_class_attributes(text: str, class_name: str, annotations: dict[str, str]) -> str:
        """Annotates attributes of a class in a stub - the ones stubgen left without a type.

        Args:
            text: The stub.
            class_name: The class.
            annotations: The annotation of each attribute.

        Returns:
            The stub.
        """
        lines = text.split(chr(10))
        header = re.compile(rf"^class {re.escape(class_name)}\b")
        inside = False
        for index, line in enumerate(lines):
            if header.match(line):
                inside = True
                continue
            if inside and line and not line.startswith(" "):
                break
            if inside:
                name, separator, _annotation = line.strip().partition(":")
                if separator and line.startswith("    ") and not line.startswith("     ") and name in annotations:
                    lines[index] = f"    {name}: {annotations[name]}"
        return chr(10).join(lines)

    @staticmethod
    def add_class_line(text: str, class_name: str, line: str) -> str:
        """Adds a line at the top of a class's body in a stub.

        Args:
            text: The stub.
            class_name: The class.
            line: The line, indented.

        Returns:
            The stub.
        """
        one_line_class = re.compile(rf"^(class {re.escape(class_name)}\b[^\n]*:)[ \t]*\.\.\.[ \t]*$", re.MULTILINE)
        if one_line_class.search(text):
            return one_line_class.sub(lambda match: f"{match.group(1)}\n{line}", text, count=1)
        class_header = re.compile(rf"^class {re.escape(class_name)}\b[^\n]*:[ \t]*$", re.MULTILINE)
        return class_header.sub(lambda match: f"{match.group(0)}\n{line}", text, count=1)
