from __future__ import annotations

import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.stubs.constants import STUB_HEADER
from hare.stubs.messages import STUB_DEPENDENCIES_REQUIRED
from hare.stubs.model_stub import ModelStub
from hare.stubs.stub_imports import StubImports

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.models.model_binding import ModelBinding


class ModuleStub:
    """The stub of one module of models: the whole module as mypy's stubgen writes it - pyright reads
    the stub instead of the module, so every other name stays - with ``objects`` of each model typed
    and the model's declarations (``ModelStub``) added.

    Args:
        module_name: The module.
        models: Its models, each with the dialect of its connection.
        relation_depth: How many relations a filter key crosses at most.
    """

    def __init__(self, module_name: str, models: list[tuple[ModelBinding, Dialect]], relation_depth: int) -> None:
        self.module_name = module_name
        self.models = models
        self.relation_depth = relation_depth

    def get_base_stub(self) -> tuple[Path, str]:
        """The relative path and source mypy's stubgen writes, preserving package layout."""
        # Local import: mypy is the extra hare-orm[pyright] installs, read only here.
        from mypy.stubgen import generate_stubs, parse_options

        with tempfile.TemporaryDirectory() as output_directory:
            generate_stubs(parse_options(["--quiet", "-m", self.module_name, "-o", output_directory]))
            module_path = Path(output_directory, *self.module_name.split("."))
            stub_path = module_path / "__init__.pyi" if module_path.is_dir() else module_path.with_suffix(".pyi")
            return stub_path.relative_to(output_directory), stub_path.read_text(encoding="utf-8")

    def get_stub(self) -> tuple[Path, str]:
        """The relative path and completed stub source."""
        try:
            # Load the CST parser only when generating a stub.
            from libcst import parse_module

            from hare.stubs.class_stub_editor import ClassStubEditor
            from hare.stubs.module_stub_exports import ModuleStubExports

            relative_path, text = self.get_base_stub()
            stub = ModuleStubExports.preserve(parse_module(text), self.module_name)
        except ModuleNotFoundError as error:
            if error.name not in {"libcst", "mypy"}:
                raise
            raise ConfigurationError(STUB_DEPENDENCIES_REQUIRED) from error

        imports = StubImports(self.module_name)
        declarations: list[str] = []
        members: dict[str, dict[str, str]] = {}
        from hare.core.apps.model_binding_graph import ModelBindingGraph

        declaration_graph = ModelBindingGraph()
        for abstract_model in self.get_abstract_models():
            members[abstract_model.__qualname__] = self._get_field_members(
                declaration_graph.include(abstract_model), imports
            )
        for binding, dialect in self.models:
            model = binding._model
            model_stub = ModelStub(binding, dialect, imports, self.relation_depth)
            attributes = self._get_field_members(binding, imports)
            attributes.update(model_stub.get_manager_declarations())
            primary_key_lines = model_stub.get_primary_key_lines()
            if primary_key_lines:
                attributes[PRIMARY_KEY_ALIAS] = "\n".join(primary_key_lines)
            members[model.__qualname__] = attributes
            declarations.extend(["", *model_stub.get_declarations()])
        text = stub.visit(ClassStubEditor(members)).code
        return relative_path, "\n".join([STUB_HEADER, *imports.render(), text.rstrip("\n"), *declarations, ""])

    @staticmethod
    def _get_field_members(binding: ModelBinding, imports: StubImports) -> dict[str, str]:
        """Render stored and relational fields as class member declarations."""
        return {
            name: f"{name}: {annotation}"
            for name, annotation in ModelStub.get_field_annotations(binding, imports).items()
        }

    def get_abstract_models(self) -> list[type[Model]]:
        """The module's abstract declarations, including bases nested inside local classes."""
        module = sys.modules.get(self.module_name)
        pending = deque(() if module is None else vars(module).values())
        seen: set[int] = set()
        models: list[type[Model]] = []
        while pending:
            value = pending.popleft()
            if not isinstance(value, type) or value.__module__ != self.module_name or id(value) in seen:
                continue
            seen.add(id(value))
            if issubclass(value, Model) and value._declaration.abstract:
                models.append(value)
            pending.extend(vars(value).values())
        return models
