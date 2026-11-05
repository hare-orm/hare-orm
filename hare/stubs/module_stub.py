from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.stubs.constants import PRIMARY_KEY_ATTRIBUTE_NAME, STUB_HEADER
from hare.stubs.messages import STUB_DEPENDENCIES_REQUIRED
from hare.stubs.model_stub import ModelStub
from hare.stubs.stub_imports import StubImports

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect


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
            from hare.stubs.class_stub_editor import ClassStubEditor
            from hare.stubs.module_stub_exports import ModuleStubExports

            relative_path, text = self.get_base_stub()
            text = ModuleStubExports.preserve(text, self.module_name)
        except ModuleNotFoundError as error:
            if error.name not in {"libcst", "mypy"}:
                raise
            raise ConfigurationError(STUB_DEPENDENCIES_REQUIRED) from error

        imports = StubImports(self.module_name)
        declarations: list[str] = []
        members: dict[str, dict[str, str]] = {}
        for abstract_model in self.get_abstract_models():
            members[abstract_model.__name__] = {
                name: f"{name}: {annotation}"
                for name, annotation in ModelStub.get_field_annotations(abstract_model, imports).items()
            }
        for model, dialect in self.models:
            model_stub = ModelStub(model, dialect, imports, self.relation_depth)
            attributes = {
                name: f"{name}: {annotation}" for name, annotation in model_stub.get_attribute_annotations().items()
            }
            attributes.update(model_stub.get_manager_declarations())
            primary_key_lines = model_stub.get_primary_key_lines()
            if primary_key_lines:
                attributes[PRIMARY_KEY_ATTRIBUTE_NAME] = "\n".join(primary_key_lines)
            members[model.__name__] = attributes
            declarations.extend(["", *model_stub.get_declarations()])
        text = ClassStubEditor.update(text, members)
        return relative_path, "\n".join([STUB_HEADER, *imports.render(), text.rstrip("\n"), *declarations, ""])

    def get_abstract_models(self) -> list[type[Model]]:
        """The abstract models the module declares - their fields are typed for the models built on
        them."""
        module = sys.modules.get(self.module_name)
        return [
            value
            for value in ([] if module is None else vars(module).values())
            if isinstance(value, type)
            and issubclass(value, Model)
            and value.__module__ == self.module_name
            and value._meta.abstract
        ]
