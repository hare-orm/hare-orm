from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hare.stubs.module_stub import ModuleStub

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.typing_info.bound_models import BoundModels


class StubWriter:
    """Writes the stub of every module of models into a directory - ``typings/<package>/<module>.pyi``,
    where pyright looks first.

    Args:
        bound_models: The models.
        output_directory: The directory.
        relation_depth: How many relations a filter key crosses at most.
    """

    def __init__(self, bound_models: BoundModels, output_directory: Path, relation_depth: int) -> None:
        self.bound_models = bound_models
        self.output_directory = output_directory
        self.relation_depth = relation_depth

    def get_stub_texts(self) -> dict[Path, str]:
        """The text of each stub, by its path."""
        models_by_module: dict[str, list[tuple[type[Model], Dialect]]] = {}
        for model in self.bound_models.models:
            dialect = self.bound_models.get_dialect(model)
            if dialect is not None:
                models_by_module.setdefault(model.__module__, []).append((model, dialect))
        texts: dict[Path, str] = {}
        for module_name, models in sorted(models_by_module.items()):
            path = self.output_directory.joinpath(*module_name.split(".")).with_suffix(".pyi")
            texts[path] = ModuleStub(module_name, models, self.relation_depth).get_text()
        return texts

    def get_outdated(self) -> list[Path]:
        """The stubs missing or differing from what the models give now."""
        return [
            path
            for path, text in self.get_stub_texts().items()
            if not path.exists() or path.read_text(encoding="utf-8") != text
        ]

    def write(self) -> list[Path]:
        """Writes every stub.

        Returns:
            The stubs written.
        """
        texts = self.get_stub_texts()
        for path, text in texts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        return list(texts)
